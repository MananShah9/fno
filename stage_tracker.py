import os
import sys
import time
import json
import inspect
import logging
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List, Union

from sqlalchemy.orm import Session
import db
from models import Message, Trade, MessageStageTrace

logger = logging.getLogger("tracker")

def get_code_location(depth: int = 1) -> str:
    """
    Returns the caller's code location formatted as filename:function:lineno.
    """
    try:
        stack = inspect.stack()
        # Find the first frame outside stage_tracker.py
        target_frame = None
        for frame_info in stack[depth:]:
            filename = os.path.basename(frame_info.filename)
            if filename != "stage_tracker.py":
                target_frame = frame_info
                break

        if target_frame:
            filename = os.path.basename(target_frame.filename)
            func_name = target_frame.function
            line_no = target_frame.lineno
            return f"{filename}:{func_name}:{line_no}"
    except Exception:
        pass
    return "unknown:unknown:0"

def _serialize_details(details: Any) -> Optional[str]:
    """Converts details dictionary or object into structured JSON string."""
    if details is None:
        return None
    if isinstance(details, str):
        return details
    try:
        return json.dumps(details, default=str, indent=2)
    except Exception:
        return str(details)

def record_stage(
    stage: str,
    status: str = "SUCCESS",
    message_id: Optional[int] = None,
    telegram_message_id: Optional[int] = None,
    trade_id: Optional[int] = None,
    revision: int = 0,
    duration_ms: Optional[float] = None,
    location: Optional[str] = None,
    details: Any = None,
    error_message: Optional[str] = None,
    stack_trace: Optional[str] = None,
    session: Optional[Session] = None
) -> Optional[MessageStageTrace]:
    """
    Safely records an execution stage trace in the database.
    Guaranteed never to raise uncaught exceptions to ensure trading stability.
    """
    loc = location or get_code_location(depth=2)
    details_str = _serialize_details(details)
    
    close_session_at_end = False
    active_session = session
    if active_session is None:
        try:
            active_session = db.SessionLocal()
            close_session_at_end = True
        except Exception as e:
            logger.error(f"Failed to create DB session for stage tracking ({stage}): {e}")
            return None

    try:
        trace_record = MessageStageTrace(
            message_id=message_id,
            telegram_message_id=telegram_message_id,
            trade_id=trade_id,
            revision=revision,
            stage=stage,
            status=status.upper(),
            timestamp=datetime.utcnow(),
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            location=loc,
            details=details_str,
            error_message=str(error_message) if error_message else None,
            stack_trace=str(stack_trace) if stack_trace else None
        )
        active_session.add(trace_record)

        # Also update summary fields in Message row if message_id is known
        if message_id:
            msg = active_session.query(Message).filter(Message.id == message_id).first()
            if msg:
                msg.last_stage = stage
                msg.last_status = status.upper()
                if error_message:
                    msg.last_error = str(error_message)[:500]

        active_session.commit()
        return trace_record

    except Exception as err:
        logger.warning(f"Error persisting stage trace [{stage}]: {err}")
        try:
            active_session.rollback()
        except Exception:
            pass
        return None

    finally:
        if close_session_at_end and active_session:
            try:
                active_session.close()
            except Exception:
                pass


class StageContext:
    """
    Context manager for measuring and tracking execution stages.
    Automatically captures duration, code location, and handles errors/tracebacks.
    """
    def __init__(
        self,
        stage: str,
        message_id: Optional[int] = None,
        telegram_message_id: Optional[int] = None,
        trade_id: Optional[int] = None,
        revision: int = 0,
        session: Optional[Session] = None,
        details: Any = None
    ):
        self.stage = stage
        self.message_id = message_id
        self.telegram_message_id = telegram_message_id
        self.trade_id = trade_id
        self.revision = revision
        self.session = session
        self.details = details or {}
        self.status = "SUCCESS"
        self.error_message: Optional[str] = None
        self.stack_trace: Optional[str] = None
        self.start_time: float = 0.0
        self.location: str = ""

    def __enter__(self):
        self.start_time = time.perf_counter()
        self.location = get_code_location(depth=2)
        return self

    def set_details(self, details: Any):
        if isinstance(details, dict) and isinstance(self.details, dict):
            self.details.update(details)
        else:
            self.details = details

    def set_status(self, status: str):
        self.status = status.upper()

    def set_error(self, error: str):
        self.status = "ERROR"
        self.error_message = error

    def set_warning(self, warning: str):
        self.status = "WARNING"
        self.error_message = warning

    def set_trade_id(self, trade_id: int):
        self.trade_id = trade_id

    def set_message_id(self, message_id: int):
        self.message_id = message_id

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.perf_counter() - self.start_time) * 1000.0

        if exc_type is not None:
            self.status = "ERROR"
            self.error_message = str(exc_val)
            self.stack_trace = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))

        record_stage(
            stage=self.stage,
            status=self.status,
            message_id=self.message_id,
            telegram_message_id=self.telegram_message_id,
            trade_id=self.trade_id,
            revision=self.revision,
            duration_ms=duration_ms,
            location=self.location,
            details=self.details,
            error_message=self.error_message,
            stack_trace=self.stack_trace,
            session=self.session
        )
        # We do not suppress exceptions; re-raise naturally
        return False


# ============================================================================
# DIAGNOSTIC AND QUERY UTILITIES
# ============================================================================

def get_message_history(message_id_or_tg_id: int, is_tg_id: bool = False, session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Fetches full chronological list of stage traces for a given message.
    """
    close_sess = False
    s = session
    if s is None:
        s = db.SessionLocal()
        close_sess = True

    try:
        query = s.query(MessageStageTrace)
        if is_tg_id:
            query = query.filter(MessageStageTrace.telegram_message_id == message_id_or_tg_id)
        else:
            query = query.filter(MessageStageTrace.message_id == message_id_or_tg_id)

        records = query.order_by(MessageStageTrace.id.asc()).all()

        results = []
        for r in records:
            results.append({
                "id": r.id,
                "stage": r.stage,
                "status": r.status,
                "revision": r.revision,
                "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if r.timestamp else "N/A",
                "duration_ms": r.duration_ms,
                "location": r.location,
                "details": r.details,
                "error_message": r.error_message,
                "stack_trace": r.stack_trace,
                "trade_id": r.trade_id
            })
        return results
    finally:
        if close_sess and s:
            s.close()

def get_stuck_or_failed_messages(limit: int = 50, session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Retrieves messages that encountered an ERROR, WARNING, or remain unprocessed/stuck.
    """
    close_sess = False
    s = session
    if s is None:
        s = db.SessionLocal()
        close_sess = True

    try:
        # Messages with error status, unanalysed, or error in stage traces
        messages = s.query(Message).filter(
            (Message.last_status.in_(["ERROR", "WARNING"])) |
            (Message.processed == False)
        ).order_by(Message.id.desc()).limit(limit).all()

        results = []
        for m in messages:
            results.append({
                "id": m.id,
                "telegram_message_id": m.telegram_message_id,
                "date": m.date.strftime("%Y-%m-%d %H:%M:%S") if m.date else "N/A",
                "text_snippet": m.text[:60] if m.text else "",
                "processed": m.processed,
                "analysed_by_ai": m.analysed_by_ai,
                "last_stage": m.last_stage or "N/A",
                "last_status": m.last_status or ("PENDING" if not m.processed else "SUCCESS"),
                "last_error": m.last_error,
                "total_traces": len(m.stage_traces) if m.stage_traces else 0
            })
        return results
    finally:
        if close_sess and s:
            s.close()

def get_recent_messages_diagnostics(limit: int = 30, session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Returns recent messages with their diagnostic health summaries.
    """
    close_sess = False
    s = session
    if s is None:
        s = db.SessionLocal()
        close_sess = True

    try:
        messages = s.query(Message).order_by(Message.id.desc()).limit(limit).all()
        results = []
        for m in messages:
            stages_count = len(m.stage_traces) if m.stage_traces else 0
            has_errors = any(t.status == "ERROR" for t in m.stage_traces) if m.stage_traces else False
            has_warnings = any(t.status == "WARNING" for t in m.stage_traces) if m.stage_traces else False
            
            overall_status = "SUCCESS"
            if has_errors or m.last_status == "ERROR":
                overall_status = "ERROR"
            elif has_warnings or m.last_status == "WARNING":
                overall_status = "WARNING"
            elif not m.processed:
                overall_status = "IN_PROGRESS"
            elif m.last_status:
                overall_status = m.last_status

            results.append({
                "id": m.id,
                "telegram_message_id": m.telegram_message_id or "N/A",
                "revision": m.revision or 0,
                "date": m.date.strftime("%Y-%m-%d %H:%M:%S") if m.date else "N/A",
                "text_snippet": m.text.replace("\n", " ")[:45] if m.text else "",
                "processed": m.processed,
                "last_stage": m.last_stage or ("COMPLETED" if m.processed else "SYNCED"),
                "overall_status": overall_status,
                "stages_count": stages_count,
                "last_error": m.last_error
            })
        return results
    finally:
        if close_sess and s:
            s.close()

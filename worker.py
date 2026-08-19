import os
import re
import asyncio
import logging
import sys
import json
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from telethon import events, Button, utils

import db
from models import Message, Trade, Action
from telegram_client import client, check_login, get_channel_entity
from gemini_client import (
    analyze_message_with_ai, clean_symbol, is_poke_message, classify_sl_trigger,
    is_emergency_exit_phrase, extract_exit_strikes_and_prices, ActionSchema
)
from instruments_manager import (
    resolve_nfo_instrument, parse_price_value, calculate_lots_from_budget,
    calculate_position_size, classify_strategy_type, get_margin_tier_estimate,
    get_max_lot_cap, is_index_symbol, get_spot_instrument_key
)
from zerodha_client import (
    place_zerodha_order, check_existing_zerodha_order_or_position, get_nfo_ltp,
    get_spot_ltp, get_multiple_ltp, calculate_basket_margin,
    verify_zerodha_order_confirmation, get_zerodha_order_status,
    get_zerodha_net_positions, verify_zerodha_positions_zero
)
from stage_tracker import record_stage, StageContext, get_code_location
from time_filter import is_telegram_time_active, get_schedule_description

load_dotenv()

# Setup Logging
raw_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, raw_log_level, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("worker")

AUTO_PLACE_ORDERS = os.getenv("AUTO_PLACE_ORDERS", "false").lower() in ("true", "1", "t", "yes")

def is_adjustment_planning_text(text: Optional[str]) -> bool:
    """Checks if message is forward-looking planning commentary (e.g. 'planning to average')."""
    if not text:
        return False
    t = text.lower().strip()
    planning_phrases = [
        "planning to average", "plan to average", "thinking to average",
        "we might average", "plan is to average", "will plan to average"
    ]
    return any(p in t for p in planning_phrases)

def is_adjustment_reminder_text(text: Optional[str]) -> bool:
    """Checks if message is reminder/commentary on an already ongoing or completed averaging window."""
    if not text:
        return False
    t = text.lower().strip()
    reminder_phrases = [
        "average around", "if you missed it", "add in 220", "add in 230", "add in 240",
        "add that lot", "averaged earlier", "average done", "hold average",
        "keep holding average", "holding average", "we have averaged", "already averaged",
        "if not averaged", "those who missed", "average here", "averaging at"
    ]
    return any(p in t for p in reminder_phrases)

def evaluate_and_deduplicate_adjustments(
    session: Session,
    trade: Trade,
    db_message: Message,
    analysis: Any,
    rev: int = 0
) -> bool:
    """
    Evaluates whether the incoming message is an averaging/adjustment attempt on an existing open trade.
    Enforces the Trade Adjustment Lifecycle State Machine:
      1. Differentiates conversational planning/status/reminder commentary vs explicit new execution instructions.
      2. Enforces trade.max_adjustments limit per trade.
      3. Performs rolling time window (e.g. 30 mins) & pending order deduplication for same trade/strike.
      4. If approved, increments trade.adjustment_count, updates last_adjustment_at/price/strike,
         and marks parsed actions with is_adjustment=True and adjustment_number.
    Returns True if an adjustment was processed/evaluated, False if this is not an adjustment message.
    """
    if not trade or trade.status != "OPEN":
        return False

    # Check if there are actionable entry legs in analysis
    entry_actions = [a for a in analysis.actions if getattr(a, "action_type", "").upper() in ["BUY", "SELL"]]
    if not entry_actions:
        return False

    # Check if trade already has existing entry actions in DB (meaning new entry legs are adjustments/averaging)
    existing_entries = session.query(Action).filter(
        Action.trade_id == trade.id,
        Action.action_type.in_(["BUY", "SELL"])
    ).count()

    is_adj = bool(
        getattr(analysis, "is_adjustment", False) or
        any(getattr(a, "is_adjustment", False) for a in analysis.actions) or
        (getattr(analysis, "is_continuation", False) and existing_entries > 0) or
        (existing_entries > 0 and trade.status == "OPEN")
    )

    if not is_adj:
        return False

    max_allowed = trade.max_adjustments if trade.max_adjustments is not None else int(os.getenv("MAX_ADJUSTMENTS_PER_TRADE", "1"))
    window_minutes = float(os.getenv("ADJUSTMENT_DEDUPLICATION_WINDOW_MINUTES", "30"))
    max_adj_lots = int(os.getenv("ADJUSTMENT_MAX_LOTS", "1"))
    msg_date = db_message.date or datetime.utcnow()
    current_adj_count = trade.adjustment_count or 0

    # 1. Check if message is forward-looking planning commentary (e.g. "We are planning to average at 241")
    if is_adjustment_planning_text(db_message.text):
        logger.info(f"Message ID {db_message.id} identified as adjustment planning commentary for Trade #{trade.id}.")
        for a in analysis.actions:
            if getattr(a, "action_type", "").upper() in ["BUY", "SELL"]:
                a.action_type = "INFO"
                a.details = f"Adjustment planning commentary for Trade #{trade.id}: ongoing averaging window."
                a.is_adjustment = True

        record_stage(
            stage="ADJUSTMENT_REMINDER_DETECTED",
            status="INFO",
            message_id=db_message.id,
            telegram_message_id=db_message.telegram_message_id,
            trade_id=trade.id,
            revision=rev,
            details={
                "trade_id": trade.id,
                "reason": "Forward-looking planning commentary regarding potential averaging level",
                "text_snippet": (db_message.text or "")[:100]
            },
            session=session
        )
        return True

    # 2. Check if trade has reached max adjustments cap
    if current_adj_count >= max_allowed:
        logger.warning(
            f"Trade #{trade.id} has already reached maximum allowed adjustments ({current_adj_count}/{max_allowed}). "
            f"Blocking additional averaging orders from Message ID {db_message.id}."
        )
        for a in analysis.actions:
            if getattr(a, "action_type", "").upper() in ["BUY", "SELL"]:
                a.action_type = "INFO"
                a.details = f"Averaging blocked: Trade #{trade.id} reached max adjustments limit ({current_adj_count}/{max_allowed})."
                a.is_adjustment = True

        record_stage(
            stage="ADJUSTMENT_LIMIT_REACHED",
            status="WARNING",
            message_id=db_message.id,
            telegram_message_id=db_message.telegram_message_id,
            trade_id=trade.id,
            revision=rev,
            error_message=f"Trade #{trade.id} reached max adjustments limit ({current_adj_count}/{max_allowed}).",
            details={
                "trade_id": trade.id,
                "current_adjustments": current_adj_count,
                "max_allowed": max_allowed
            },
            session=session
        )
        return True

    # 3. Check rolling time window & pending order deduplication
    is_dup_window = False
    window_diff_mins = None
    if trade.last_adjustment_at:
        diff_secs = (msg_date - trade.last_adjustment_at).total_seconds()
        if 0 <= diff_secs < (window_minutes * 60):
            is_dup_window = True
            window_diff_mins = round(diff_secs / 60.0, 1)

    # Check for pending / placed adjustment actions on this trade
    has_pending_adj = any(
        a.order_status in ["PENDING", "PLACED"] and getattr(a, "is_adjustment", False)
        for a in trade.actions
    )

    is_reminder = (
        getattr(analysis, "is_adjustment_reminder", False) is True or
        is_adjustment_reminder_text(db_message.text)
    )

    if is_dup_window or has_pending_adj or (current_adj_count > 0 and is_reminder):
        logger.info(
            f"Averaging adjustment for Trade #{trade.id} deduplicated: "
            f"Already executed/active within {window_diff_mins or 'N/A'} mins (window: {window_minutes} mins, pending: {has_pending_adj}, reminder: {is_reminder})."
        )
        for a in analysis.actions:
            if getattr(a, "action_type", "").upper() in ["BUY", "SELL"]:
                a.action_type = "INFO"
                a.details = f"Averaging deduplicated: Adjustment already active/recorded for Trade #{trade.id} within rolling window."
                a.is_adjustment = True

        stage_name = "ADJUSTMENT_REMINDER_DETECTED" if is_reminder else "ADJUSTMENT_DEDUPLICATED"
        record_stage(
            stage=stage_name,
            status="INFO",
            message_id=db_message.id,
            telegram_message_id=db_message.telegram_message_id,
            trade_id=trade.id,
            revision=rev,
            details={
                "trade_id": trade.id,
                "window_minutes": window_minutes,
                "mins_since_last_adjustment": window_diff_mins,
                "has_pending_adj": has_pending_adj,
                "is_reminder": is_reminder
            },
            session=session
        )
        return True

    # 4. Valid New Adjustment Approved
    new_adj_number = current_adj_count + 1
    trade.adjustment_count = new_adj_number
    trade.last_adjustment_at = msg_date
    
    first_entry_act = entry_actions[0]
    p_val = parse_price_value(getattr(first_entry_act, "price", None))
    if p_val:
        trade.last_adjustment_price = p_val
    if getattr(first_entry_act, "strike", None):
        trade.last_adjustment_strike = float(first_entry_act.strike)

    session.commit()

    logger.info(f"Approved Adjustment #{new_adj_number} for Trade #{trade.id} (capped at {max_adj_lots} lot(s)).")
    for a in analysis.actions:
        if getattr(a, "action_type", "").upper() in ["BUY", "SELL"]:
            a.is_adjustment = True
            a.lots = min(getattr(a, "lots", 1) or 1, max_adj_lots)

    record_stage(
        stage="ADJUSTMENT_APPROVED",
        status="SUCCESS",
        message_id=db_message.id,
        telegram_message_id=db_message.telegram_message_id,
        trade_id=trade.id,
        revision=rev,
        details={
            "trade_id": trade.id,
            "adjustment_number": new_adj_number,
            "max_adjustments": max_allowed,
            "allocated_lots": max_adj_lots,
            "actions_count": len(entry_actions)
        },
        session=session
    )
    return True

def format_important_notice_telegram_html(trade: Trade, unexecuted_actions: list) -> str:
    """
    Formats a high-priority 'IMPORTANT NOTICE' Telegram HTML alert
    for any identified trade action item that was not executed on Zerodha.
    """
    msg_parts = [
        "🚨 <b>IMPORTANT NOTICE: ACTION(S) NOT EXECUTED</b> 🚨\n",
        f"<b>Trade ID:</b> #{trade.id}",
        f"<b>Underlying:</b> {trade.underlying or 'N/A'}"
    ]
    if trade.structure_type:
        msg_parts.append(f"<b>Strategy:</b> {trade.structure_type}")

    msg_parts.append("\n⚠️ <b>The following identified action items were NOT executed on Zerodha:</b>\n")

    for act in unexecuted_actions:
        action_prefix = "• "
        if act.action_type == "BUY":
            action_prefix += "🟢 <b>BUY:</b> "
        elif act.action_type == "SELL":
            action_prefix += "🔴 <b>SELL:</b> "
        elif act.action_type in ["EXIT", "CLOSE_LEG"]:
            action_prefix += "🚪 <b>EXIT:</b> "
        elif act.action_type == "UPDATE_SL":
            action_prefix += "🛡️ <b>UPDATE SL:</b> "
        else:
            action_prefix += "ℹ️ <b>ACTION:</b> "

        leg_title = f"{action_prefix}"
        if act.tradingsymbol:
            leg_title += f"<code>{act.tradingsymbol}</code> "
        elif act.instrument_name:
            leg_title += f"<code>{act.instrument_name}</code> "
        else:
            leg_title += f"<code>{act.action_type} {act.underlying or ''}</code> "

        detail_parts = []
        if act.transaction_type:
            detail_parts.append(f"Side: <b>{act.transaction_type}</b>")
        if act.quantity:
            lots_num = act.lots or 1
            lots_text = f"{lots_num} lot" if lots_num == 1 else f"{lots_num} lots"
            detail_parts.append(f"Qty: <b>{act.quantity}</b> ({lots_text})")
        if act.order_type:
            detail_parts.append(f"Type: <b>{act.order_type}</b>")
        if act.price:
            detail_parts.append(f"Price: <code>{act.price}</code>")

        if detail_parts:
            leg_title += f"({', '.join(detail_parts)})"

        msg_parts.append(leg_title)

        reason = act.zerodha_response or "Order placement was blocked or rejected"
        status_label = act.order_status or "UNEXECUTED"
        msg_parts.append(f"  ❌ <b>Status:</b> <code>{status_label}</code>")
        msg_parts.append(f"  ⚠️ <b>Reason:</b> <i>{reason}</i>\n")

    msg_parts.append("🛠️ <b>Manual Action Required:</b> Please check your Zerodha account immediately to manage open risk or place the required leg(s) manually.")

    return "\n".join(msg_parts)


def format_spot_sl_triggered_telegram_html(trade: Trade, action: Action, spot_ltp: float, exec_results: list) -> str:
    """
    Formats a high-priority alert when an underlying cash/spot index or stock stop-loss threshold
    is crossed, triggering emergency market square-off of open positions.
    """
    msg_parts = [
        "🛡️ <b>STOP-LOSS TRIGGERED (UNDERLYING SPOT HIT)</b> 🚨\n",
        f"<b>Trade ID:</b> #{trade.id}",
        f"<b>Underlying:</b> {trade.underlying or 'N/A'}"
    ]
    if trade.structure_type:
        msg_parts.append(f"<b>Strategy:</b> {trade.structure_type}")

    dir_text = "falling to/below" if getattr(action, "sl_trigger_direction", "BELOW") == "BELOW" else "rising to/above"
    target_str = f"{action.sl_trigger_price:,.2f}" if action.sl_trigger_price is not None else str(action.stoploss)
    msg_parts.append(f"\n📍 <b>Spot LTP:</b> <code>{spot_ltp:,.2f}</code> (Crossed SL threshold <code>{target_str}</code> {dir_text})")
    if action.stoploss:
        msg_parts.append(f"<b>Signal SL Rule:</b> <i>{action.stoploss}</i>")

    msg_parts.append("\n🚪 <b>Emergency Square-Off Execution Results:</b>")
    if exec_results:
        for r in exec_results:
            icon = "✅" if r.get("success") else "❌"
            status_desc = f"Order ID: <code>{r.get('order_id')}</code>" if r.get("success") else f"Error: {r.get('message')}"
            msg_parts.append(f"• {icon} <code>{r.get('tradingsymbol')}</code> -> {status_desc}")
    else:
        msg_parts.append("• ℹ️ Position square-off queued.")

    msg_parts.append("\n✅ <b>Trade Status:</b> <code>CLOSED</code>")
    return "\n".join(msg_parts)


def format_action_telegram_message_html(trade: Trade, actions: list) -> str:
    """Formats actions into a beautiful Telegram HTML message with Zerodha execution info."""
    is_exit = any(a.action_type == 'EXIT' for a in actions)
    is_update = any(a.action_type in ['UPDATE_SL', 'CLOSE_LEG', 'INFO'] for a in actions)
    is_adj = any(getattr(a, "is_adjustment", False) for a in actions)
    
    msg_parts = []
    
    if is_exit:
        msg_parts.append("🚪 <b>TRADE CLOSED</b>")
    elif is_adj:
        adj_num = getattr(actions[0], "adjustment_number", None) or trade.adjustment_count or 1
        msg_parts.append(f"🔄 <b>TRADE ADJUSTMENT #{adj_num} DETECTED</b>")
    elif is_update:
        msg_parts.append("🔄 <b>TRADE UPDATE</b>")
    else:
        msg_parts.append("🔔 <b>NEW TRADE DETECTED</b>")
        
    msg_parts.append(f"<b>Trade ID:</b> #{trade.id}")
    msg_parts.append(f"<b>Underlying:</b> {trade.underlying or 'N/A'}")
    if trade.structure_type:
        msg_parts.append(f"<b>Strategy:</b> {trade.structure_type}")
    if trade.adjustment_count:
        max_adj = trade.max_adjustments if trade.max_adjustments is not None else 1
        msg_parts.append(f"<b>Adjustments:</b> {trade.adjustment_count}/{max_adj}")
        
    msg_parts.append("\n<b>Actions & Zerodha Order Details:</b>")
    for action in actions:
        action_prefix = "• "
        adj_tag = f" (Adj #{action.adjustment_number})" if getattr(action, "is_adjustment", False) and action.adjustment_number else ""
        if action.action_type == "BUY":
            action_prefix += f"🟢 <b>BUY{adj_tag}:</b> "
        elif action.action_type == "SELL":
            action_prefix += f"🔴 <b>SELL{adj_tag}:</b> "
        elif action.action_type == "EXIT":
            action_prefix += "🚪 <b>EXIT:</b> "
        elif action.action_type == "UPDATE_SL":
            action_prefix += "🛡️ <b>UPDATE SL:</b> "
        elif action.action_type == "CLOSE_LEG":
            action_prefix += "❌ <b>CLOSE LEG:</b> "
        else:
            action_prefix += "ℹ️ <b>INFO:</b> "
            
        action_line = f"{action_prefix}"
        if action.tradingsymbol:
            action_line += f"<code>{action.tradingsymbol}</code> "
        elif action.instrument_name:
            action_line += f"<code>{action.instrument_name}</code> "
            
        detail_parts = []
        if action.transaction_type:
            detail_parts.append(f"Side: <b>{action.transaction_type}</b>")
        if action.quantity:
            lots_num = action.lots or 1
            lots_text = f"{lots_num} lot" if lots_num == 1 else f"{lots_num} lots"
            detail_parts.append(f"Qty: <b>{action.quantity}</b> ({lots_text})")
        if action.order_type:
            detail_parts.append(f"Type: <b>{action.order_type}</b>")
        if action.price:
            detail_parts.append(f"Price: <code>{action.price}</code>")
        if action.stoploss:
            if getattr(action, "sl_trigger_type", None) == "UNDERLYING_SPOT_TRIGGER":
                detail_parts.append(f"SL: <code>{action.stoploss}</code> (Spot Monitor 📡)")
            elif getattr(action, "sl_trigger_type", None) == "OPTION_PREMIUM_TRIGGER":
                detail_parts.append(f"SL: <code>{action.stoploss}</code> (Option Premium 🎯)")
            else:
                detail_parts.append(f"SL: <code>{action.stoploss}</code>")
        if action.target:
            detail_parts.append(f"Target: <code>{action.target}</code>")
            
        if detail_parts:
            action_line += f"({', '.join(detail_parts)})"

        if action.order_status and action.order_status != "PENDING":
            status_icon = "✅" if action.order_status in ["PLACED", "EXECUTED"] else "❌"
            action_line += f"\n  Status: {status_icon} <b>{action.order_status}</b>"
            if action.zerodha_order_id:
                action_line += f" (Order ID: <code>{action.zerodha_order_id}</code>)"
            if action.order_status == "FAILED" and action.zerodha_response:
                action_line += f"\n  ⚠️ <i>Error: {action.zerodha_response}</i>"

        if action.details:
            action_line += f"\n  <i>Note: {action.details}</i>"
            
        msg_parts.append(action_line)
        
    if trade.context_summary:
        msg_parts.append(f"\n📝 <b>Current Trade Context:</b>\n<i>{trade.context_summary}</i>")
        
    return "\n".join(msg_parts)

def compute_trade_net_positions(session: Optional[Session], trade: Union[Trade, int]) -> Dict[str, Dict[str, Any]]:
    """
    Computes the net active position balance directly by aggregating all filled/placed entry quantities
    minus all filled/placed exit quantities for every instrument token / tradingsymbol within the trade.
    Filters out FAILED or CANCELLED actions.

    Returns:
        dict: {
            tradingsymbol: {
                "instrument_token": int,
                "tradingsymbol": str,
                "net_quantity": int,       # positive for NET LONG, negative for NET SHORT, 0 for FLAT
                "abs_quantity": int,       # abs(net_quantity)
                "net_lots": int,           # computed lots based on instrument lot size
                "position_side": str,      # "LONG", "SHORT", or "FLAT"
                "required_exit_side": str, # "SELL" to close long, "BUY" to close short, None if FLAT
                "template_action": Action, # representative Action object for leg metadata
                "total_entry_qty": int,
                "total_exit_qty": int,
            },
            ...
        }
    """
    trade_id = trade.id if isinstance(trade, Trade) else trade
    all_actions = []
    if session and trade_id:
        all_actions = session.query(Action).filter(
            Action.trade_id == trade_id,
            Action.order_status.notin_(["FAILED", "CANCELLED"]),
            Action.tradingsymbol != None
        ).order_by(Action.id.asc()).all()
    elif isinstance(trade, Trade) and getattr(trade, "actions", None):
        all_actions = [
            a for a in trade.actions
            if a.order_status not in ["FAILED", "CANCELLED"] and a.tradingsymbol
        ]

    pos_map = {}
    for act in all_actions:
        sym = act.tradingsymbol
        if not sym:
            continue
        if sym not in pos_map:
            pos_map[sym] = {
                "instrument_token": act.instrument_token,
                "tradingsymbol": sym,
                "net_quantity": 0,
                "abs_quantity": 0,
                "net_lots": 0,
                "position_side": "FLAT",
                "required_exit_side": None,
                "template_action": act,
                "total_entry_qty": 0,
                "total_exit_qty": 0,
            }

        qty = act.quantity or 0
        tt = (act.transaction_type or act.action_type or "").upper()
        is_entry = act.action_type in ["BUY", "SELL"]
        is_exit = act.action_type in ["EXIT", "CLOSE_LEG"]

        if is_entry:
            pos_map[sym]["total_entry_qty"] += qty
            if tt == "BUY":
                pos_map[sym]["net_quantity"] += qty
            elif tt == "SELL":
                pos_map[sym]["net_quantity"] -= qty
        elif is_exit:
            pos_map[sym]["total_exit_qty"] += qty
            if tt == "BUY":
                pos_map[sym]["net_quantity"] += qty
            elif tt == "SELL":
                pos_map[sym]["net_quantity"] -= qty

    # Compute derived sizing, lots, and required reverse transaction sides
    for sym, info in pos_map.items():
        net_q = info["net_quantity"]
        info["abs_quantity"] = abs(net_q)
        template_act = info["template_action"]

        per_lot_qty = int(template_act.quantity / (template_act.lots or 1)) if template_act.quantity and template_act.lots else (template_act.quantity or 1)
        info["net_lots"] = max(1, int(round(abs(net_q) / per_lot_qty))) if (per_lot_qty > 0 and abs(net_q) > 0) else (0 if net_q == 0 else (template_act.lots or 1))

        if net_q > 0:
            info["position_side"] = "LONG"
            info["required_exit_side"] = "SELL"
        elif net_q < 0:
            info["position_side"] = "SHORT"
            info["required_exit_side"] = "BUY"
        else:
            info["position_side"] = "FLAT"
            info["required_exit_side"] = None

    return pos_map


def get_open_trades_context(session: Session) -> List[Dict[str, Any]]:
    """
    Fetches all open trades formatted with minimal active open legs for Gemini context.
    Excludes historical closed actions, cancelled/failed actions, full timestamps,
    and extraneous metadata from prompt context to minimize LLM latency and avoid cross-contamination.
    """
    open_trades = session.query(Trade).filter(Trade.status == "OPEN").all()
    
    context = []
    for trade in open_trades:
        active_legs = []
        net_positions = compute_trade_net_positions(session, trade)
        
        has_active_net = False
        if net_positions:
            for sym, pos_info in net_positions.items():
                if pos_info.get("position_side") != "FLAT" and pos_info.get("abs_quantity", 0) > 0:
                    has_active_net = True
                    template_act = pos_info.get("template_action")
                    ot = template_act.option_type if template_act else None
                    if not ot and sym:
                        sym_u = sym.strip().upper()
                        if sym_u.endswith("PE"):
                            ot = "PE"
                        elif sym_u.endswith("CE"):
                            ot = "CE"
                        elif sym_u.endswith("FUT") or "FUT" in sym_u:
                            ot = "FUT"

                    side = "BUY" if pos_info.get("position_side") == "LONG" else "SELL"
                    active_legs.append({
                        "tradingsymbol": sym,
                        "strike": template_act.strike if template_act else None,
                        "option_type": ot,
                        "transaction_type": side,
                        "action_type": side,
                        "is_main": getattr(template_act, "is_main", True),
                        "is_adjustment": getattr(template_act, "is_adjustment", False) or False,
                        "quantity": pos_info.get("abs_quantity"),
                        "net_lots": pos_info.get("net_lots", 1)
                    })

        # Fallback if compute_trade_net_positions produced no net positions (e.g. actions without tradingsymbol or unit test fixtures)
        if not has_active_net and trade.actions:
            valid_entries = [
                a for a in trade.actions
                if a.action_type in ["BUY", "SELL"] and (a.order_status or "PENDING") not in ["FAILED", "CANCELLED"]
            ]
            for action in valid_entries:
                ot = action.option_type
                if not ot and action.tradingsymbol:
                    sym_u = action.tradingsymbol.strip().upper()
                    if sym_u.endswith("PE"):
                        ot = "PE"
                    elif sym_u.endswith("CE"):
                        ot = "CE"
                    elif sym_u.endswith("FUT") or "FUT" in sym_u:
                        ot = "FUT"

                active_legs.append({
                    "tradingsymbol": action.tradingsymbol or action.instrument_name,
                    "strike": action.strike,
                    "option_type": ot,
                    "transaction_type": action.transaction_type or action.action_type,
                    "action_type": action.action_type,
                    "is_main": action.is_main if action.is_main is not None else True,
                    "is_adjustment": getattr(action, "is_adjustment", False) or False,
                    "quantity": action.quantity,
                    "lots": action.lots or 1
                })

        clean_u = clean_symbol(trade.underlying)
        context.append({
            "id": trade.id,
            "status": trade.status,
            "structure_type": trade.structure_type,
            "underlying": clean_u,
            "active_legs": active_legs,
            "existing_orders": active_legs,  # alias for backward compatibility
            "adjustment_count": trade.adjustment_count or 0,
            "max_adjustments": trade.max_adjustments if trade.max_adjustments is not None else 1,
            "last_adjustment_price": trade.last_adjustment_price,
            "last_adjustment_at": trade.last_adjustment_at.isoformat() if trade.last_adjustment_at else None,
        })
    return context


def ensure_square_off_actions(session: Session, trade: Trade, db_message: Message, parsed_actions: list = None) -> list:
    """
    Scans a trade's net open positions and creates reverse square-off Action records
    if the trade is being closed or exited, computing net active quantities
    to ensure 100% of initial and averaged lots are completely closed without leaving orphaned legs.
    """
    net_positions = compute_trade_net_positions(session, trade)
    if not net_positions:
        return []

    parsed_actions = parsed_actions or []
    new_square_off_actions = []

    for sym, pos_info in net_positions.items():
        if pos_info["position_side"] == "FLAT" or pos_info["abs_quantity"] <= 0:
            logger.info(f"Leg {sym} for Trade #{trade.id} already has net 0 open position. Skipping square-off.")
            continue

        net_qty = pos_info["abs_quantity"]
        net_lots = pos_info["net_lots"]
        reverse_tt = pos_info["required_exit_side"]
        entry_act = pos_info["template_action"]

        # Check if AI parsed explicit price or order_type for this leg in parsed_actions
        matching_parsed = None
        for pa in parsed_actions:
            pa_strike = getattr(pa, "strike", None)
            pa_is_main = getattr(pa, "is_main", None)
            pa_details = str(getattr(pa, "details", "") or "").lower()
            pa_inst_name = str(getattr(pa, "instrument_name", "") or "").lower()
            is_hedge_ref = (pa_is_main is False) or ("hedge" in pa_details) or ("hedge" in pa_inst_name)
            is_main_ref = (pa_is_main is True) or ("main" in pa_details) or ("main" in pa_inst_name)

            if pa_strike is not None and entry_act.strike is not None:
                if abs(entry_act.strike - pa_strike) < 0.01:
                    matching_parsed = pa
                    break
            elif is_hedge_ref and not getattr(entry_act, "is_main", True):
                matching_parsed = pa
                break
            elif is_main_ref and getattr(entry_act, "is_main", True):
                matching_parsed = pa
                break
            elif pa_inst_name and sym and pa_inst_name == sym.lower():
                matching_parsed = pa
                break

        # Determine order type and price: LIMIT if explicit price specified, MARKET otherwise
        if matching_parsed and (matching_parsed.price or matching_parsed.order_type == "LIMIT" or matching_parsed.is_limit):
            ord_type = "LIMIT"
            is_lim = True
            limit_price = matching_parsed.price
        else:
            ord_type = "MARKET"
            is_lim = False
            limit_price = matching_parsed.price if matching_parsed else None

        square_off_action = Action(
            trade_id=trade.id,
            message_id=db_message.id,
            action_type="EXIT",
            is_main=getattr(entry_act, "is_main", True),
            instrument_name=entry_act.instrument_name,
            price=limit_price,
            stoploss=None,
            target=None,
            is_limit=is_lim,
            details=f"Square-off exit leg for {sym} (Net Qty: {net_qty})",
            telegram_sent=False,
            underlying=entry_act.underlying,
            option_type=entry_act.option_type,
            strike=entry_act.strike,
            expiry=entry_act.expiry,
            lots=net_lots,
            quantity=net_qty,
            tradingsymbol=sym,
            instrument_token=entry_act.instrument_token,
            transaction_type=reverse_tt,
            order_type=ord_type,
            product=entry_act.product or "NRML",
            order_status="PENDING"
        )
        session.add(square_off_action)
        new_square_off_actions.append(square_off_action)
        logger.info(f"Generated square-off action for Trade #{trade.id}: {reverse_tt} {net_qty} ({net_lots}L) x {sym} ({ord_type})")

    # Order square-off actions so BUY legs (closing short positions) precede SELL legs (closing long hedge positions)
    new_square_off_actions.sort(key=lambda a: 0 if (a.transaction_type or a.action_type or "").upper() == "BUY" else 1)

    session.commit()
    return new_square_off_actions


def process_trade_actions_and_sizing(
    trade: Trade,
    db_message_id: int,
    parsed_actions: list,
    target_budget: float = None
) -> list:
    """
    Resolves NFO instruments, determines main vs hedge leg roles,
    calculates trade lots from target investment budget (.env) or adjustment caps,
    ensures hedge quantity matches main quantity, and returns list of Action DB objects.
    """
    if target_budget is None:
        raw_budget = os.getenv("TARGET_INVESTMENT_BUDGET") or os.getenv("TARGET_INVESTMENT_BUDGET_MAIN")
        if raw_budget:
            try:
                target_budget = float(raw_budget)
            except (ValueError, TypeError):
                target_budget = None

    max_adj_lots = int(os.getenv("ADJUSTMENT_MAX_LOTS", "1"))
    resolved_items = []
    entry_indices = []

    for idx, action_schema in enumerate(parsed_actions):
        u_symbol = clean_symbol(getattr(action_schema, "underlying", None) or (trade.underlying if trade else None))
        o_type = getattr(action_schema, "option_type", None)
        if o_type:
            o_type = str(o_type).strip().upper()
        strike_val = getattr(action_schema, "strike", None)
        expiry_str = getattr(action_schema, "expiry_info", None)
        is_adj_leg = bool(getattr(action_schema, "is_adjustment", False))
        action_type = getattr(action_schema, "action_type", "INFO").upper()

        # Context-aware cross-referencing for exit actions or leg updates
        matched_pa = None
        is_exit_action = action_type in ["EXIT", "CLOSE_LEG"] or (trade and trade.status == "CLOSED")

        if trade and getattr(trade, "actions", None):
            open_acts = [
                pa for pa in trade.actions
                if pa.action_type in ["BUY", "SELL"] and pa.order_status not in ["FAILED", "CANCELLED"]
            ]

            def _get_act_option_type(act: Action) -> Optional[str]:
                if act.option_type:
                    return act.option_type.strip().upper()
                if act.tradingsymbol:
                    sym = act.tradingsymbol.strip().upper()
                    if sym.endswith("PE"):
                        return "PE"
                    elif sym.endswith("CE"):
                        return "CE"
                    elif sym.endswith("FUT") or "FUT" in sym:
                        return "FUT"
                return None

            is_hedge_ref = (
                getattr(action_schema, "is_main", None) is False or
                "hedge" in str(getattr(action_schema, "details", "")).lower() or
                "hedge" in str(getattr(action_schema, "instrument_name", "")).lower()
            )
            is_main_ref = (
                getattr(action_schema, "is_main", None) is True or
                "main" in str(getattr(action_schema, "details", "")).lower() or
                "main" in str(getattr(action_schema, "instrument_name", "")).lower()
            )

            # 1. Exact strike match across open trade actions
            if strike_val is not None:
                for pa in open_acts:
                    if pa.strike is not None and abs(pa.strike - strike_val) < 0.01:
                        pa_ot = _get_act_option_type(pa)
                        o_type = o_type or pa_ot
                        expiry_str = expiry_str or pa.expiry
                        u_symbol = u_symbol or pa.underlying
                        matched_pa = pa
                        break

            # 2. Explicit hedge exit without strike
            if not matched_pa and (is_exit_action or o_type is None) and is_hedge_ref:
                for pa in open_acts:
                    if (not getattr(pa, "is_main", True)) or (pa.transaction_type or pa.action_type).upper() == "BUY":
                        strike_val = strike_val if strike_val is not None else pa.strike
                        pa_ot = _get_act_option_type(pa)
                        o_type = o_type or pa_ot
                        expiry_str = expiry_str or pa.expiry
                        u_symbol = u_symbol or pa.underlying
                        matched_pa = pa
                        break

            # 3. Explicit main exit without strike
            if not matched_pa and (is_exit_action or o_type is None) and is_main_ref and len(open_acts) > 1:
                for pa in open_acts:
                    if getattr(pa, "is_main", True) or (pa.transaction_type or pa.action_type).upper() == "SELL":
                        strike_val = strike_val if strike_val is not None else pa.strike
                        pa_ot = _get_act_option_type(pa)
                        o_type = o_type or pa_ot
                        expiry_str = expiry_str or pa.expiry
                        u_symbol = u_symbol or pa.underlying
                        matched_pa = pa
                        break

            # 4. If single open leg exists in trade
            if not matched_pa and (is_exit_action or o_type is None) and len(open_acts) == 1:
                pa = open_acts[0]
                strike_val = strike_val if strike_val is not None else pa.strike
                pa_ot = _get_act_option_type(pa)
                o_type = o_type or pa_ot
                expiry_str = expiry_str or pa.expiry
                u_symbol = u_symbol or pa.underlying
                matched_pa = pa

            # 5. Positional match if multi-leg exit count equals open legs count
            if not matched_pa and (is_exit_action or o_type is None) and len(parsed_actions) == len(open_acts) and len(open_acts) > 1 and idx < len(open_acts):
                pa = open_acts[idx]
                strike_val = strike_val if strike_val is not None else pa.strike
                pa_ot = _get_act_option_type(pa)
                o_type = o_type or pa_ot
                expiry_str = expiry_str or pa.expiry
                u_symbol = u_symbol or pa.underlying
                matched_pa = pa

        # Resolve NFO Instrument - Strictly forbid arbitrary default fallbacks to 'CE'
        inst = None
        if matched_pa and matched_pa.tradingsymbol:
            pa_ot = _get_act_option_type(matched_pa) or o_type
            inst = {
                "tradingsymbol": matched_pa.tradingsymbol,
                "instrument_token": matched_pa.instrument_token,
                "lot_size": int(matched_pa.quantity / (matched_pa.lots or 1)) if matched_pa.quantity and matched_pa.lots else 1,
                "expiry": matched_pa.expiry or expiry_str,
                "strike": matched_pa.strike if matched_pa.strike is not None else strike_val,
                "option_type": pa_ot
            }
            if pa_ot and not o_type:
                o_type = pa_ot
        elif u_symbol:
            inst = resolve_nfo_instrument(u_symbol, strike_val, o_type, expiry_str)

        resolved_items.append({
            "schema": action_schema,
            "action_type": action_type,
            "underlying": u_symbol,
            "option_type": o_type,
            "strike": strike_val,
            "expiry": inst["expiry"] if inst else expiry_str,
            "inst": inst,
            "is_main": True,
            "is_adjustment": is_adj_leg,
            "adjustment_number": getattr(trade, "adjustment_count", None) if is_adj_leg else None,
            "lots": getattr(action_schema, "lots", None) or 1,
            "quantity": None
        })

        if action_type in ["BUY", "SELL"]:
            entry_indices.append(idx)

    # If there are entry legs, determine main vs hedge and calculate lot sizing
    if entry_indices:
        # 1. Identify MAIN leg index
        main_idx = entry_indices[0]
        explicit_mains = [i for i in entry_indices if getattr(resolved_items[i]["schema"], "is_main", None) is True]
        if len(explicit_mains) == 1:
            main_idx = explicit_mains[0]
        else:
            # Prefer FUT leg as main
            fut_legs = [i for i in entry_indices if resolved_items[i]["option_type"] == "FUT"]
            if fut_legs:
                main_idx = fut_legs[0]
            else:
                # Prefer SELL leg as main in credit spreads
                sell_legs = [i for i in entry_indices if resolved_items[i]["action_type"] == "SELL"]
                buy_legs = [i for i in entry_indices if resolved_items[i]["action_type"] == "BUY"]
                if sell_legs and buy_legs:
                    main_idx = sell_legs[0]
                else:
                    # Choose leg with highest price
                    best_price = -1.0
                    for i in entry_indices:
                        p = parse_price_value(getattr(resolved_items[i]["schema"], "price", None)) or 0.0
                        if p > best_price:
                            best_price = p
                            main_idx = i

        main_item = resolved_items[main_idx]
        main_inst = main_item["inst"]
        main_lot_size = main_inst["lot_size"] if main_inst else 1

        # 2. Determine price for main leg
        main_price = parse_price_value(getattr(main_item["schema"], "price", None))
        if (main_price is None or main_price <= 0) and main_inst:
            try:
                main_price = get_nfo_ltp(main_inst.get("tradingsymbol"))
            except Exception as le:
                logger.warning(f"Failed to fetch live LTP for {main_inst.get('tradingsymbol')}: {le}")

        # 3. Check for live Zerodha Margin via Basket Margin API for multi-leg / short / futures orders
        live_margin = None
        entry_legs_list = [resolved_items[i] for i in entry_indices]
        strat_type = classify_strategy_type(entry_legs_list, main_item["underlying"])

        if strat_type in ["SPREAD", "SINGLE_FUTURES", "NAKED_SHORT_OPTION"]:
            margin_order_params = []
            for item in entry_legs_list:
                leg_inst = item["inst"]
                if leg_inst and leg_inst.get("tradingsymbol"):
                    leg_price = parse_price_value(getattr(item["schema"], "price", None)) or 0.0
                    tt = getattr(item["schema"], "transaction_type", None) or item["action_type"]
                    if tt not in ["BUY", "SELL"]:
                        tt = "BUY" if item["action_type"] == "BUY" else "SELL"
                    margin_order_params.append({
                        "exchange": leg_inst.get("exchange", "NFO"),
                        "tradingsymbol": leg_inst.get("tradingsymbol"),
                        "transaction_type": tt,
                        "variety": "regular",
                        "product": getattr(item["schema"], "product", None) or "NRML",
                        "order_type": "LIMIT" if leg_price > 0 else "MARKET",
                        "quantity": leg_inst.get("lot_size", 1),
                        "price": leg_price
                    })

            if margin_order_params:
                try:
                    live_margin = calculate_basket_margin(margin_order_params)
                except Exception as me:
                    logger.warning(f"Error querying live Zerodha basket margin: {me}")

        # 4. Calculate lots and sizing via margin-based engine
        is_adjustment_run = any(item.get("is_adjustment") for item in entry_legs_list)

        if is_adjustment_run:
            # Averaging legs are strictly sized by ADJUSTMENT_MAX_LOTS to prevent position bloating
            calculated_lots = max_adj_lots
            logger.info(f"Trade #{trade.id if trade else 'N/A'} is an ADJUSTMENT leg: Sizing set to {calculated_lots} lot(s) (ADJUSTMENT_MAX_LOTS cap).")
        else:
            sizing_result = calculate_position_size(
                entry_legs=entry_legs_list,
                target_budget=target_budget,
                underlying=main_item["underlying"],
                live_margin=live_margin,
                main_price=main_price
            )
            calculated_lots = sizing_result["lots"]

            logger.info(
                f"Trade #{trade.id if trade else 'N/A'} Margin Sizing Result: "
                f"underlying={main_item['underlying']} (Index={sizing_result['is_index']}), "
                f"strategy={sizing_result['strategy_type']}, method={sizing_result['sizing_method']}, "
                f"budget=Rs.{target_budget or 0:,.2f}, per_lot_capital=Rs.{sizing_result['per_lot_capital']:,.2f}, "
                f"raw_lots={sizing_result['raw_lots']:.2f}, max_cap={sizing_result['max_lot_cap']} -> "
                f"final_lots={calculated_lots}, main_qty={calculated_lots * main_lot_size}"
            )

        main_quantity = calculated_lots * main_lot_size

        # 5. Set sizing and role for all entry legs
        for i in entry_indices:
            is_this_main = (i == main_idx)
            leg_inst = resolved_items[i]["inst"]
            leg_lot_size = leg_inst["lot_size"] if leg_inst else main_lot_size
            resolved_items[i]["is_main"] = is_this_main
            resolved_items[i]["lots"] = calculated_lots
            # Assign quantity based on leg lot size and calculated lots
            resolved_items[i]["quantity"] = calculated_lots * leg_lot_size

    # For exit legs directly in message, size using net active quantity
    for idx, item in enumerate(resolved_items):
        if item["action_type"] in ["EXIT", "CLOSE_LEG"] and item["quantity"] is None:
            inst = item["inst"]
            sym = inst.get("tradingsymbol") if inst else None
            lot_sz = inst["lot_size"] if inst else 1
            matched_qty = None
            matched_lots = 1
            matching_entry_leg = None

            if trade and getattr(trade, "actions", None):
                matching_entry_qty = 0
                matching_exit_qty = 0
                entry_lots_total = 0

                for prior_act in trade.actions:
                    if prior_act.order_status in ["FAILED", "CANCELLED"]:
                        continue
                    is_match = False
                    if sym and prior_act.tradingsymbol and prior_act.tradingsymbol == sym:
                        is_match = True
                    elif prior_act.strike and item["strike"] and abs(prior_act.strike - item["strike"]) < 0.01:
                        is_match = True

                    if is_match:
                        if prior_act.action_type in ["BUY", "SELL"]:
                            matching_entry_qty += (prior_act.quantity or 0)
                            entry_lots_total += (prior_act.lots or 1)
                            if not matching_entry_leg:
                                matching_entry_leg = prior_act
                        elif prior_act.action_type in ["EXIT", "CLOSE_LEG"]:
                            matching_exit_qty += (prior_act.quantity or 0)

                net_open = matching_entry_qty - matching_exit_qty
                if net_open > 0:
                    matched_qty = net_open
                    matched_lots = max(1, int(round(net_open / lot_sz))) if lot_sz > 0 else entry_lots_total
                elif matching_entry_qty > 0:
                    matched_qty = matching_entry_qty
                    matched_lots = entry_lots_total

                if matching_entry_leg:
                    item["is_main"] = getattr(matching_entry_leg, "is_main", True)

            item["lots"] = matched_lots
            item["quantity"] = matched_qty or (matched_lots * lot_sz)

    # Build and return Action database objects
    actions_to_add = []
    for item in resolved_items:
        schema = item["schema"]
        inst = item["inst"]
        action_type = item["action_type"]
        o_type = item["option_type"]

        # Resolve transaction type
        trans_type = getattr(schema, "transaction_type", None)
        if not trans_type or trans_type not in ["BUY", "SELL"]:
            if action_type == "SELL":
                trans_type = "SELL"
            elif action_type == "BUY":
                trans_type = "BUY"
            elif action_type in ["EXIT", "CLOSE_LEG"]:
                # Check if there is an existing leg in trade.actions to invert
                matching_entry = None
                if trade and getattr(trade, "actions", None):
                    for pa in trade.actions:
                        if pa.action_type in ["BUY", "SELL"] and pa.order_status not in ["FAILED", "CANCELLED"]:
                            if (inst and pa.tradingsymbol == inst.get("tradingsymbol")) or \
                               (item["strike"] and pa.strike and abs(pa.strike - item["strike"]) < 0.01):
                                 matching_entry = pa
                                 break
                if matching_entry:
                    trans_type = "BUY" if (matching_entry.transaction_type or matching_entry.action_type).upper() == "SELL" else "SELL"
                else:
                    # In credit spreads / short fut spreads, main is short (exit BUY), hedge is long (exit SELL)
                    if item["is_main"]:
                        trans_type = "BUY"
                    else:
                        trans_type = "SELL"

        # Resolve order type
        ord_type = "LIMIT" if (getattr(schema, "order_type", None) == "LIMIT" or getattr(schema, "is_limit", False)) else "MARKET"

        # Resolve SL classification and parameters
        raw_sl = getattr(schema, "stoploss", None)
        sl_type = getattr(schema, "sl_trigger_type", None)
        sl_price = getattr(schema, "sl_trigger_price", None)
        sl_dir = getattr(schema, "sl_trigger_direction", None)

        if raw_sl and (not sl_type or sl_price is None or not sl_dir):
            sl_cls = classify_sl_trigger(
                raw_stoploss=raw_sl,
                underlying=item["underlying"],
                strike=item["strike"],
                option_type=o_type,
                entry_price=getattr(schema, "price", None),
                is_main=item["is_main"],
                transaction_type=trans_type
            )
            sl_type = sl_cls["sl_trigger_type"]
            sl_price = sl_cls["sl_trigger_price"]
            sl_dir = sl_cls["sl_trigger_direction"]

        is_spot_monitored = bool(sl_type == "UNDERLYING_SPOT_TRIGGER" and sl_price is not None)
        sl_ord_status = "MONITORING" if is_spot_monitored else ("PENDING" if sl_type == "OPTION_PREMIUM_TRIGGER" else None)

        db_action = Action(
            trade_id=trade.id if trade else None,
            message_id=db_message_id,
            action_type=action_type,
            is_main=item["is_main"],
            is_adjustment=item.get("is_adjustment", False),
            adjustment_number=item.get("adjustment_number", None),
            instrument_name=getattr(schema, "instrument_name", None),
            price=getattr(schema, "price", None),
            stoploss=raw_sl,
            sl_trigger_type=sl_type,
            sl_trigger_price=sl_price,
            sl_trigger_direction=sl_dir,
            sl_monitoring_active=is_spot_monitored,
            sl_triggered=False,
            sl_order_status=sl_ord_status,
            target=getattr(schema, "target", None),
            is_limit=getattr(schema, "is_limit", False),
            details=getattr(schema, "details", None),
            telegram_sent=False,
            underlying=item["underlying"],
            option_type=o_type,
            strike=item["strike"],
            expiry=item["expiry"],
            lots=item["lots"],
            quantity=item["quantity"],
            tradingsymbol=inst["tradingsymbol"] if inst else None,
            instrument_token=inst["instrument_token"] if inst else None,
            transaction_type=trans_type,
            order_type=ord_type,
            product=getattr(schema, "product", None) or "NRML",
            order_status="PENDING"
        )
        actions_to_add.append(db_action)

    # Sort actions so BUY (long/hedge) legs precede SELL (short/main) legs
    actions_to_add.sort(key=lambda a: 0 if (a.transaction_type or a.action_type or "").upper() == "BUY" else 1)

    return actions_to_add


def execute_trade_actions(session: Session, trade_id: int, auto_mode: bool = False) -> list:
    """
    Executes Zerodha orders for pending actionable legs of a given trade with deduplication checks.
    Enforces strict execution ordering and two-phase verification for multi-leg / hedged setups:
      - Phase 1: Submits and confirms all BUY (hedge/long) legs first on the exchange.
      - Phase 2: Waits for successful BUY confirmation before placing SELL (short/main) legs to guarantee
                 Zerodha / exchange margin relief (~35k vs ~1.5L) and prevent unhedged naked short exposure.
    If auto_mode is True, checks AUTO_PLACE_ORDERS for entry actions and AUTO_PLACE_EXIT_ORDERS for exit actions.
    """
    auto_entry = os.getenv("AUTO_PLACE_ORDERS", "false").lower() in ("true", "1", "t", "yes")
    auto_exit = os.getenv("AUTO_PLACE_EXIT_ORDERS", "false").lower() in ("true", "1", "t", "yes")

    actions = session.query(Action).filter(
        Action.trade_id == trade_id,
        Action.order_status.in_(["PENDING", "FAILED"]),
        Action.action_type.in_(["BUY", "SELL", "EXIT", "CLOSE_LEG"])
    ).all()

    if not actions:
        return []

    # Separate actions into Phase 1 (BUY / Long / Hedge legs) and Phase 2 (SELL / Short / Main legs)
    buy_actions = []
    sell_actions = []
    other_actions = []

    for act in actions:
        side = (act.transaction_type or act.action_type or "").upper()
        if side == "BUY":
            buy_actions.append(act)
        elif side == "SELL":
            sell_actions.append(act)
        else:
            other_actions.append(act)

    results = []
    hedge_failed = False
    failed_hedge_symbols = []

    def _execute_single_action(action: Action) -> dict:
        if not action.tradingsymbol or not action.quantity:
            logger.warning(f"Action ID {action.id} missing tradingsymbol or quantity. Skipping order placement.")
            record_stage(
                stage="ORDER_EXECUTION_SKIPPED",
                status="WARNING",
                message_id=action.message_id,
                trade_id=action.trade_id,
                details={"action_id": action.id, "reason": "Missing tradingsymbol or quantity", "action_type": action.action_type},
                error_message="Missing tradingsymbol or quantity for order execution",
                session=session
            )
            return {
                "action_id": action.id,
                "tradingsymbol": action.tradingsymbol,
                "success": False,
                "confirmed": False,
                "order_id": None,
                "message": "Missing tradingsymbol or quantity"
            }

        is_exit = action.action_type in ["EXIT", "CLOSE_LEG"]

        # If in automated background mode, check feature flags
        if auto_mode:
            if is_exit and not auto_exit:
                logger.info(f"Skipping auto-placement for Exit Action ID {action.id} (AUTO_PLACE_EXIT_ORDERS is false).")
                record_stage(
                    stage="ORDER_AUTO_PLACEMENT_SKIPPED",
                    status="INFO",
                    message_id=action.message_id,
                    trade_id=action.trade_id,
                    details={"action_id": action.id, "tradingsymbol": action.tradingsymbol, "reason": "AUTO_PLACE_EXIT_ORDERS is false", "is_exit": True},
                    session=session
                )
                return {
                    "action_id": action.id,
                    "tradingsymbol": action.tradingsymbol,
                    "success": False,
                    "confirmed": False,
                    "skipped_auto": True,
                    "order_id": None,
                    "message": "AUTO_PLACE_EXIT_ORDERS is false"
                }
            elif not is_exit and not auto_entry:
                logger.info(f"Skipping auto-placement for Entry Action ID {action.id} (AUTO_PLACE_ORDERS is false).")
                record_stage(
                    stage="ORDER_AUTO_PLACEMENT_SKIPPED",
                    status="INFO",
                    message_id=action.message_id,
                    trade_id=action.trade_id,
                    details={"action_id": action.id, "tradingsymbol": action.tradingsymbol, "reason": "AUTO_PLACE_ORDERS is false", "is_exit": False},
                    session=session
                )
                return {
                    "action_id": action.id,
                    "tradingsymbol": action.tradingsymbol,
                    "success": False,
                    "confirmed": False,
                    "skipped_auto": True,
                    "order_id": None,
                    "message": "AUTO_PLACE_ORDERS is false"
                }

        # 1. Deduplication check against existing Zerodha orders & positions
        dedup_check = check_existing_zerodha_order_or_position(
            tradingsymbol=action.tradingsymbol,
            transaction_type=action.transaction_type or ("BUY" if action.action_type == "BUY" else "SELL"),
            quantity=action.quantity,
            is_exit=is_exit
        )

        if dedup_check["duplicate"]:
            logger.info(f"Action ID {action.id} skipped via Zerodha deduplication: {dedup_check['message']}")
            action.order_status = "PLACED" if dedup_check["reason"] != "position_already_closed" else "EXECUTED"
            action.zerodha_response = f"Deduplicated: {dedup_check['message']}"
            if dedup_check.get("order_id"):
                action.zerodha_order_id = dedup_check["order_id"]
            session.commit()

            record_stage(
                stage="ORDER_DEDUPLICATED",
                status="INFO",
                message_id=action.message_id,
                trade_id=action.trade_id,
                details={
                    "action_id": action.id,
                    "tradingsymbol": action.tradingsymbol,
                    "reason": dedup_check["reason"],
                    "order_id": action.zerodha_order_id,
                    "message": dedup_check["message"]
                },
                session=session
            )

            return {
                "action_id": action.id,
                "tradingsymbol": action.tradingsymbol,
                "success": True,
                "confirmed": True,
                "order_id": action.zerodha_order_id or "DEDUPLICATED",
                "message": dedup_check["message"]
            }

        # Extract numerical price if limit order
        limit_price = None
        if action.order_type == "LIMIT":
            if action.price:
                limit_price = parse_price_value(action.price)
            if limit_price is None or limit_price <= 0:
                logger.info(f"Action ID {action.id} has LIMIT order_type but unparseable price '{action.price}'. Converting to MARKET order.")
                action.order_type = "MARKET"
                limit_price = None

        logger.info(f"Executing Zerodha order for Action ID {action.id}: {action.transaction_type} {action.quantity} x {action.tradingsymbol} ({action.order_type})")

        res = place_zerodha_order(
            tradingsymbol=action.tradingsymbol,
            transaction_type=action.transaction_type or "BUY",
            quantity=action.quantity,
            exchange="NFO",
            order_type=action.order_type or "MARKET",
            product=action.product or "NRML",
            price=limit_price
        )

        if res["success"]:
            action.order_status = "PLACED"
            action.zerodha_order_id = res["order_id"]
            action.zerodha_response = res["message"]
            action.placed_at = datetime.utcnow()
            record_stage(
                stage="ORDER_PLACED",
                status="SUCCESS",
                message_id=action.message_id,
                trade_id=action.trade_id,
                details={
                    "action_id": action.id,
                    "tradingsymbol": action.tradingsymbol,
                    "transaction_type": action.transaction_type,
                    "quantity": action.quantity,
                    "order_type": action.order_type,
                    "price": limit_price,
                    "order_id": res["order_id"]
                },
                session=session
            )
            session.commit()
            return {
                "action_id": action.id,
                "tradingsymbol": action.tradingsymbol,
                "success": True,
                "confirmed": True,
                "order_id": res["order_id"],
                "message": res["message"]
            }
        else:
            action.order_status = "FAILED"
            action.zerodha_response = res["message"]
            record_stage(
                stage="ORDER_FAILED",
                status="ERROR",
                message_id=action.message_id,
                trade_id=action.trade_id,
                error_message=res["message"],
                details={
                    "action_id": action.id,
                    "tradingsymbol": action.tradingsymbol,
                    "transaction_type": action.transaction_type,
                    "quantity": action.quantity,
                    "order_type": action.order_type,
                    "price": limit_price
                },
                session=session
            )
            session.commit()
            return {
                "action_id": action.id,
                "tradingsymbol": action.tradingsymbol,
                "success": False,
                "confirmed": False,
                "order_id": res.get("order_id"),
                "message": res["message"]
            }

    # =========================================================================
    # PHASE 1: Execute BUY (Hedge / Long / Short-Covering) Legs First
    # =========================================================================
    if buy_actions:
        logger.info(f"Phase 1: Submitting {len(buy_actions)} BUY leg(s) first to establish exchange margin relief...")
        for act in buy_actions:
            r = _execute_single_action(act)
            results.append(r)
            if not r["success"] or not r.get("confirmed", False):
                if not r.get("skipped_auto"):
                    hedge_failed = True
                    failed_hedge_symbols.append(act.tradingsymbol or f"Action #{act.id}")

        if not hedge_failed and not any(r.get("skipped_auto") for r in results):
            logger.info(f"Phase 1 SUCCESS: All BUY/hedge legs confirmed on exchange for Trade #{trade_id}.")
            record_stage(
                stage="HEDGE_LEGS_CONFIRMED",
                status="SUCCESS",
                trade_id=trade_id,
                details={
                    "trade_id": trade_id,
                    "buy_legs_count": len(buy_actions),
                    "legs": [a.tradingsymbol for a in buy_actions]
                },
                session=session
            )

    # =========================================================================
    # PHASE 2: Two-Phase Verification Gate & SELL Leg Execution
    # =========================================================================
    if sell_actions:
        if hedge_failed:
            failed_str = ", ".join(failed_hedge_symbols)
            abort_msg = (
                f"Two-phase verification failed: Long hedge leg ({failed_str}) failed confirmation. "
                f"SELL short leg placement blocked to prevent naked margin rejection (~1.5L vs ~35k) and unhedged risk."
            )
            logger.error(f"Trade #{trade_id} Phase 2 ABORTED: {abort_msg}")

            for act in sell_actions:
                act.order_status = "FAILED"
                act.zerodha_response = abort_msg
                session.commit()

                record_stage(
                    stage="ORDER_EXECUTION_BLOCKED",
                    status="ERROR",
                    message_id=act.message_id,
                    trade_id=trade_id,
                    error_message=abort_msg,
                    details={
                        "action_id": act.id,
                        "tradingsymbol": act.tradingsymbol,
                        "transaction_type": act.transaction_type,
                        "failed_hedge_symbols": failed_hedge_symbols,
                        "reason": "Hedge confirmation failed; short order placement aborted"
                    },
                    session=session
                )

                results.append({
                    "action_id": act.id,
                    "tradingsymbol": act.tradingsymbol,
                    "success": False,
                    "confirmed": False,
                    "order_id": None,
                    "message": abort_msg
                })
        else:
            logger.info(f"Phase 2: Submitting {len(sell_actions)} SELL leg(s) with margin relief confirmed...")
            for act in sell_actions:
                r = _execute_single_action(act)
                results.append(r)

    # Execute any remaining actions
    for act in other_actions:
        r = _execute_single_action(act)
        results.append(r)

    return results

# Register Telethon Callback Handler for Telegram "Place Order(s)" inline button and MessageEdited events
def setup_telegram_event_handlers():
    if not client:
        return

    @client.on(events.CallbackQuery(pattern=r"^place_order:(\d+)$"))
    async def on_place_order_callback(event):
        try:
            trade_id = int(event.data.decode().split(":")[1])
            logger.info(f"Telegram user clicked 'Place Order(s)' button for Trade ID {trade_id}")
            await event.answer("Processing Zerodha order placement...", alert=False)

            session = db.SessionLocal()
            try:
                record_stage(
                    stage="TELEGRAM_BUTTON_CLICKED",
                    status="INFO",
                    trade_id=trade_id,
                    details={"action": "place_order", "trade_id": trade_id},
                    session=session
                )

                trade = session.query(Trade).filter(Trade.id == trade_id).first()
                if not trade:
                    await event.respond("❌ Trade not found in database.")
                    return

                with StageContext("MANUAL_ORDER_EXECUTION", trade_id=trade_id, session=session) as ctx:
                    results = execute_trade_actions(session, trade_id)
                    ctx.set_details({"trade_id": trade_id, "results": results})

                if not results:
                    await event.respond(f"ℹ️ No pending orders found for Trade #{trade_id} or orders already placed.")
                    return

                # Format results summary message
                lines = [f"🚀 <b>Zerodha Order Execution Results for Trade #{trade_id}:</b>\n"]
                for r in results:
                    icon = "✅" if r["success"] else "❌"
                    status_text = f"Order ID: <code>{r['order_id']}</code>" if r["success"] else f"Error: {r['message']}"
                    lines.append(f"{icon} <code>{r['tradingsymbol']}</code> -> {status_text}")

                if trade.status == "CLOSED":
                    trade_symbols = list({a.tradingsymbol for a in trade.actions if a.tradingsymbol})
                    if trade_symbols:
                        verif = verify_zerodha_positions_zero(trade_symbols)
                        if verif["verified"]:
                            if verif["all_zero"]:
                                lines.append("\n✅ <b>Zerodha Position Book:</b> Confirmed all positions FLAT (0).")
                            else:
                                lines.append(f"\n⚠️ <b>Zerodha Position Alert:</b> {verif['message']}")

                res_msg = "\n".join(lines)
                await event.respond(res_msg, parse_mode='html')

                # If any action failed or was blocked during manual execution, send Important Notice
                failed_manual = [r for r in results if not r.get("success")]
                if failed_manual:
                    unexec_acts = [
                        a for a in trade.actions
                        if a.order_status in ["FAILED", "CANCELLED"] or any(f.get("tradingsymbol") == a.tradingsymbol for f in failed_manual)
                    ]
                    if unexec_acts:
                        notice_html = format_important_notice_telegram_html(trade, unexec_acts)
                        await event.respond(notice_html, parse_mode='html')

                # Update original message in channel if possible
                session.refresh(trade)
                html_msg = format_action_telegram_message_html(trade, trade.actions)
                await event.edit(html_msg, parse_mode='html', buttons=None)

            finally:
                session.close()

        except Exception as e:
            logger.exception(f"Error handling CallbackQuery: {e}")
            await event.respond(f"❌ Execution error: {e}")

    @client.on(events.MessageEdited)
    async def on_message_edited_callback(event):
        try:
            msg = event.message
            if not msg or not msg.text:
                return

            source_channel_id = os.getenv("TELEGRAM_SOURCE_CHANNEL")
            if not source_channel_id:
                return

            # Check if edit belongs to source channel
            source_entity = await get_channel_entity(source_channel_id)
            if not source_entity:
                return

            source_peer_id = utils.get_peer_id(source_entity)
            event_peer_id = utils.get_peer_id(event.chat_id) if event.chat_id else None

            source_clean = str(source_channel_id).replace("-100", "").replace("-", "").strip()
            chat_clean = str(event.chat_id).replace("-100", "").replace("-", "").strip() if event.chat_id else ""

            is_match = (
                (event_peer_id and event_peer_id == source_peer_id) or
                (chat_clean and source_clean and chat_clean == source_clean) or
                (getattr(source_entity, "id", None) == getattr(event.chat, "id", None))
            )
            if not is_match:
                return

            # Check if message edit is within active Telegram schedule
            is_active, sched_reason = is_telegram_time_active(msg.date or datetime.utcnow())
            if not is_active:
                logger.info(f"Message edit event for Message ID {msg.id} ignored: {sched_reason}")
                return

            logger.info(f"✏️ Message edit detected for Message ID {msg.id} in source channel.")

            session = db.SessionLocal()
            try:
                # Look up existing message in DB
                db_message = session.query(Message).filter(
                    Message.telegram_message_id == msg.id
                ).first()

                if db_message:
                    # Increment message revision
                    current_rev = (db_message.revision or 0) + 1
                    db_message.revision = current_rev

                    record_stage(
                        stage="MESSAGE_EDIT_DETECTED",
                        status="INFO",
                        message_id=db_message.id,
                        telegram_message_id=msg.id,
                        revision=current_rev,
                        details={"text_snippet": msg.text[:100], "revision": current_rev},
                        session=session
                    )

                    # Check if this message produced action items (Action records in DB)
                    action_count = session.query(Action).filter(Action.message_id == db_message.id).count()
                    if action_count > 0:
                        logger.info(
                            f"Message ID {msg.id} (DB ID {db_message.id}) was already processed as an ACTION ITEM "
                            f"({action_count} actions exist). Skipping reprocessing."
                        )
                        record_stage(
                            stage="EDIT_REPROCESSING_SKIPPED",
                            status="SKIPPED",
                            message_id=db_message.id,
                            telegram_message_id=msg.id,
                            revision=current_rev,
                            details={
                                "reason": f"Message already has {action_count} executed action items in DB. Skipping duplicate processing.",
                                "action_count": action_count
                            },
                            session=session
                        )
                        return

                    logger.info(
                        f"Message ID {msg.id} (DB ID {db_message.id}) was a poke / non-action / unprocessed message. "
                        f"Updating content and reprocessing through full pipeline."
                    )
                    db_message.text = msg.text
                    db_message.processed = False
                    db_message.analysed_by_ai = False
                    db_message.ai_response = None
                    session.commit()

                    record_stage(
                        stage="EDIT_REPROCESSING_STARTED",
                        status="INFO",
                        message_id=db_message.id,
                        telegram_message_id=msg.id,
                        revision=current_rev,
                        details={"revision": current_rev, "updated_text": msg.text[:100]},
                        session=session
                    )
                else:
                    logger.info(f"Edited Message ID {msg.id} was not present in DB. Creating new record for processing.")
                    db_message = Message(
                        telegram_message_id=msg.id,
                        channel_id=str(source_channel_id),
                        date=msg.date or datetime.utcnow(),
                        text=msg.text,
                        processed=False,
                        analysed_by_ai=False,
                        revision=1
                    )
                    session.add(db_message)
                    session.commit()
                    session.refresh(db_message)

                    record_stage(
                        stage="MESSAGE_EDIT_NEW_ENTRY",
                        status="INFO",
                        message_id=db_message.id,
                        telegram_message_id=msg.id,
                        revision=1,
                        details={"text_snippet": msg.text[:100]},
                        session=session
                    )

                # Mirror updated message if mirror channel is configured
                mirror_channel_id = os.getenv("TELEGRAM_MIRROR_CHANNEL")
                if mirror_channel_id:
                    mirror_entity = await get_channel_entity(mirror_channel_id)
                    if mirror_entity:
                        try:
                            await client.send_message(mirror_entity, f"✏️ [UPDATED MESSAGE]\n{msg.text}")
                            logger.info(f"Mirrored updated message ID {msg.id}")
                            record_stage(
                                stage="MIRROR_UPDATED_MESSAGE",
                                status="SUCCESS",
                                message_id=db_message.id,
                                telegram_message_id=msg.id,
                                revision=db_message.revision,
                                details={"mirror_channel": str(mirror_channel_id)},
                                session=session
                            )
                        except Exception as me:
                            logger.error(f"Failed to mirror updated message ID {msg.id}: {me}")
                            record_stage(
                                stage="MIRROR_UPDATED_MESSAGE_FAILED",
                                status="WARNING",
                                message_id=db_message.id,
                                telegram_message_id=msg.id,
                                revision=db_message.revision,
                                error_message=str(me),
                                details={"mirror_channel": str(mirror_channel_id)},
                                session=session
                            )

                # Process the updated message through full flow
                actions_channel_id = os.getenv("TELEGRAM_ACTIONS_CHANNEL")
                actions_entity = await get_channel_entity(actions_channel_id) if actions_channel_id else None
                await process_single_message(session, db_message, actions_entity)

            finally:
                session.close()

        except Exception as e:
            logger.exception(f"Error handling MessageEdited event: {e}")

async def process_single_message(session: Session, db_message: Message, actions_entity=None) -> bool:
    """
    Executes Gemini AI analysis, trade creation/matching, lot sizing, order execution,
    and Telegram action notification for a single Message record.
    Returns True if processed successfully, False otherwise.
    """
    session.refresh(db_message)
    rev = db_message.revision or 0

    if db_message.analysed_by_ai:
        logger.info(f"Message ID {db_message.id} (TG ID: {db_message.telegram_message_id}) is already analysed. Skipping.")
        record_stage(
            stage="ANALYSIS_CHECK",
            status="SKIPPED",
            message_id=db_message.id,
            telegram_message_id=db_message.telegram_message_id,
            revision=rev,
            details={"reason": "Already analysed by AI previously"},
            session=session
        )
        return True

    # Check if message timestamp falls within configured active schedule
    if db_message.date:
        is_active, sched_reason = is_telegram_time_active(db_message.date)
        if not is_active:
            logger.info(f"Message ID {db_message.id} (date: {db_message.date}) skipped by schedule filter: {sched_reason}")
            record_stage(
                stage="TIME_WINDOW_FILTER",
                status="SKIPPED",
                message_id=db_message.id,
                telegram_message_id=db_message.telegram_message_id,
                revision=rev,
                details={"reason": sched_reason, "msg_date": str(db_message.date)},
                session=session
            )
            db_message.analysed_by_ai = True
            db_message.processed = True
            db_message.processed_at = datetime.utcnow()
            session.commit()
            return True

    if is_poke_message(db_message.text):
        logger.info(f"Message ID {db_message.id} is a poke message ('{db_message.text.strip()}'). Skipping processing.")
        record_stage(
            stage="POKE_FILTER",
            status="SKIPPED",
            message_id=db_message.id,
            telegram_message_id=db_message.telegram_message_id,
            revision=rev,
            details={"reason": "Poke ping / dot message ignored", "raw_text": db_message.text.strip()},
            session=session
        )
        db_message.analysed_by_ai = True
        db_message.processed = True
        db_message.processed_at = datetime.utcnow()
        session.commit()
        return True

    logger.info(f"Analyzing message ID {db_message.id} (TG ID: {db_message.telegram_message_id or 'N/A'})...")
    
    # 1. Fetch open trades context
    with StageContext("CONTEXT_FETCH", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, revision=rev, session=session) as ctx:
        open_trades = get_open_trades_context(session)
        ctx.set_details({"open_trades_count": len(open_trades), "open_trade_ids": [t["id"] for t in open_trades]})

    # 2. AI Analysis via Gemini
    analysis = None
    try:
        with StageContext("AI_ANALYSIS", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, revision=rev, session=session) as ctx:
            analysis = analyze_message_with_ai(db_message.text, open_trades)
            if not analysis:
                ctx.set_error("Gemini AI analysis returned None")
                logger.error(f"Failed to analyze message ID {db_message.id} with AI. Skipping and keeping it unanalysed for retry.")
                return False
            ctx.set_details({
                "is_valid_trade_msg": analysis.is_valid_trade_msg,
                "is_continuation": analysis.is_continuation,
                "related_open_trade_id": analysis.related_open_trade_id,
                "underlying": analysis.underlying,
                "structure_type": analysis.structure_type,
                "actions_count": len(analysis.actions),
                "trade_status_update": analysis.trade_status_update,
                "context_summary": analysis.context_summary
            })
    except Exception as ge:
        logger.error(f"Gemini analysis exception for Message ID {db_message.id}: {ge}")
        return False

    db_message.ai_response = json.dumps(analysis.model_dump(), default=str)
    
    if not analysis.is_valid_trade_msg:
        # High-Priority Deterministic Emergency Exit Rule:
        # If unconditional exit phrase is detected and an unambiguous open trade exists, bypass AI non-trade classification.
        if is_emergency_exit_phrase(db_message.text):
            open_trades_db = session.query(Trade).filter(Trade.status == "OPEN").all()
            target_trade = None
            if len(open_trades_db) == 1:
                target_trade = open_trades_db[0]
            elif len(open_trades_db) > 1:
                msg_nums = re.findall(r'\b\d{4,6}\b', db_message.text)
                if msg_nums:
                    msg_strikes = [float(n) for n in msg_nums]
                    for ot in open_trades_db:
                        ot_strikes = [a.strike for a in ot.actions if a.strike is not None]
                        if any(s in ot_strikes for s in msg_strikes):
                            target_trade = ot
                            break
                if not target_trade:
                    try:
                        broker_pos = get_zerodha_net_positions()
                        if broker_pos.get("success"):
                            active_syms = [s for s, q in broker_pos.get("positions", {}).items() if q != 0]
                            matching_trades = [
                                ot for ot in open_trades_db
                                if any(a.tradingsymbol in active_syms for a in ot.actions if a.tradingsymbol)
                            ]
                            if len(matching_trades) == 1:
                                target_trade = matching_trades[0]
                    except Exception as bpe:
                        logger.warning(f"Error checking broker positions during emergency exit rescue: {bpe}")

            if target_trade:
                logger.warning(
                    f"[EMERGENCY EXIT DETECTED] Message ID {db_message.id} ('{db_message.text.strip()}') "
                    f"matches unambiguous open Trade #{target_trade.id} ({target_trade.underlying}). "
                    f"Bypassing AI non-trade classification!"
                )
                analysis.is_valid_trade_msg = True
                analysis.is_continuation = True
                analysis.trade_status_update = "CLOSED"
                analysis.related_open_trade_id = target_trade.id
                analysis.underlying = target_trade.underlying
                analysis.structure_type = target_trade.structure_type
                analysis.context_summary = f"Emergency exit override: Closing full position for Trade #{target_trade.id} ({target_trade.underlying})"

                record_stage(
                    stage="DETERMINISTIC_EMERGENCY_EXIT_OVERRIDE",
                    status="SUCCESS",
                    message_id=db_message.id,
                    telegram_message_id=db_message.telegram_message_id,
                    trade_id=target_trade.id,
                    revision=rev,
                    details={
                        "trade_id": target_trade.id,
                        "underlying": target_trade.underlying,
                        "raw_text": db_message.text.strip(),
                        "reason": "Emergency exit regex matched with unambiguous open trade in DB/portfolio"
                    },
                    session=session
                )

    if not analysis.is_valid_trade_msg:
        record_stage(
            stage="AI_NON_TRADE_MESSAGE",
            status="SKIPPED",
            message_id=db_message.id,
            telegram_message_id=db_message.telegram_message_id,
            revision=rev,
            details={"reason": "AI classified as non-trade commentary or informational message", "context_summary": analysis.context_summary},
            session=session
        )
        db_message.analysed_by_ai = True
        db_message.processed = True
        db_message.processed_at = datetime.utcnow()
        session.commit()
        return True

    # If actions is empty on an emergency exit message, check if strikes/prices can be extracted
    if is_emergency_exit_phrase(db_message.text) and not analysis.actions:
        extracted_legs = extract_exit_strikes_and_prices(db_message.text)
        if extracted_legs:
            u_for_leg = analysis.underlying
            for leg in extracted_legs:
                analysis.actions.append(ActionSchema(
                    action_type="EXIT",
                    underlying=u_for_leg,
                    strike=leg["strike"],
                    option_type=leg.get("option_type"),
                    price=leg.get("price"),
                    is_limit=bool(leg.get("price")),
                    order_type="LIMIT" if leg.get("price") else "MARKET",
                    is_main=leg.get("is_main", True),
                    lots=1
                ))

    logger.info(f"Valid trade detected by AI for message ID {db_message.id}.")
    trade = None

    # 3. Trade Matching or Creation
    with StageContext("TRADE_ROUTING", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, revision=rev, session=session) as ctx:
        if analysis.is_continuation and analysis.related_open_trade_id:
            trade = session.query(Trade).filter(Trade.id == analysis.related_open_trade_id).first()
            if trade:
                logger.info(f"Mapping message ID {db_message.id} to existing Trade ID {trade.id}")

        # Fallback matching: if AI did not provide continuation id but this is an exit/closure or leg update
        if not trade:
            u_clean = clean_symbol(analysis.underlying)
            # 1. Try matching by underlying or prefix against open trades
            if u_clean:
                candidate_trade = session.query(Trade).filter(
                    Trade.status == "OPEN",
                    Trade.underlying == u_clean
                ).order_by(Trade.id.desc()).first()
                
                # If exact match not found, check prefix match against open trades
                if not candidate_trade:
                    open_trades = session.query(Trade).filter(Trade.status == "OPEN").all()
                    for ot in open_trades:
                        if ot.underlying and (u_clean.startswith(ot.underlying) or ot.underlying.startswith(u_clean)):
                            candidate_trade = ot
                            break

                if candidate_trade:
                    has_new_structure = bool(analysis.structure_type and candidate_trade.structure_type and analysis.structure_type.upper() != candidate_trade.structure_type.upper() and not candidate_trade.structure_type.startswith(analysis.structure_type))
                    if not has_new_structure or analysis.is_continuation or analysis.trade_status_update == "CLOSED" or is_emergency_exit_phrase(db_message.text) or any(a.action_type in ["EXIT", "CLOSE_LEG", "UPDATE_SL"] for a in analysis.actions) or not analysis.actions:
                        trade = candidate_trade
                        logger.info(f"Fallback mapped message ID {db_message.id} to open Trade ID {trade.id} ({candidate_trade.underlying})")

            # 2. Try matching by strike against open trade actions
            if not trade:
                msg_strikes = []
                if analysis.actions:
                    msg_strikes = [getattr(a, "strike", None) for a in analysis.actions if getattr(a, "strike", None)]
                if not msg_strikes and db_message.text:
                    msg_nums = re.findall(r'\b\d{4,6}\b', db_message.text)
                    if msg_nums:
                        msg_strikes = [float(n) for n in msg_nums]

                if msg_strikes:
                    open_trades_with_actions = session.query(Trade).filter(Trade.status == "OPEN").all()
                    for ot in open_trades_with_actions:
                        ot_strikes = [a.strike for a in ot.actions if a.strike]
                        if any(s in ot_strikes for s in msg_strikes):
                            trade = ot
                            logger.info(f"Fallback mapped message ID {db_message.id} to open Trade ID {trade.id} via matching strike {msg_strikes}")
                            break

            # 3. If still not matched, and there is only 1 open trade, and message is an exit/closure
            if not trade and (analysis.trade_status_update == "CLOSED" or is_emergency_exit_phrase(db_message.text) or any(a.action_type in ["EXIT", "CLOSE_LEG"] for a in analysis.actions)):
                open_trades_list = session.query(Trade).filter(Trade.status == "OPEN").all()
                if len(open_trades_list) == 1:
                    trade = open_trades_list[0]
                    logger.info(f"Fallback mapped message ID {db_message.id} to single open Trade ID {trade.id}")

        # Check for explicit closing keywords in message text
        msg_text_lower = (db_message.text or "").lower()
        is_closing_phrase = is_emergency_exit_phrase(db_message.text) or any(w in msg_text_lower for w in [
            "close the trade", "close full position", "closing the trade",
            "sl hit", "exit the full position", "exit full position",
            "profit booking in this trade", "close the entire position",
            "close full", "exit full"
        ])

        # Create new trade only if not mapped to existing open trade
        if not trade:
            trade = Trade(
                status="CLOSED" if (analysis.trade_status_update == "CLOSED" or is_closing_phrase) else "OPEN",
                structure_type=analysis.structure_type,
                underlying=clean_symbol(analysis.underlying),
                opened_at=db_message.date or datetime.utcnow()
            )
            session.add(trade)
            session.commit()
            session.refresh(trade)
            logger.info(f"Created new Trade ID {trade.id}")

        # Update Trade state and status
        if analysis.underlying and (not trade.underlying or "REF" in trade.underlying):
            trade.underlying = clean_symbol(analysis.underlying)
        if analysis.structure_type and not trade.structure_type:
            trade.structure_type = analysis.structure_type

        trade.context_summary = analysis.context_summary or trade.context_summary
        if trade.context_summary and len(trade.context_summary) > 300:
            trade.context_summary = trade.context_summary[:300]
        if analysis.trade_status_update == "CLOSED" or is_closing_phrase:
            trade.status = "CLOSED"
            trade.closed_at = db_message.date or datetime.utcnow()
            logger.info(f"Trade ID {trade.id} status updated to CLOSED")
        
        session.commit()
        ctx.set_trade_id(trade.id)
        ctx.set_details({
            "trade_id": trade.id,
            "is_continuation": analysis.is_continuation,
            "underlying": trade.underlying,
            "structure_type": trade.structure_type,
            "status": trade.status,
            "adjustment_count": trade.adjustment_count
        })

    # 3b. Trade Adjustment & Averaging Lifecycle State Machine Check
    if trade and trade.status == "OPEN" and analysis.actions:
        evaluate_and_deduplicate_adjustments(session, trade, db_message, analysis, rev)

    # 4. Instrument Resolution and Budget Lot Sizing
    db_actions = []
    with StageContext("INSTRUMENT_RESOLUTION_AND_SIZING", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, trade_id=trade.id, revision=rev, session=session) as ctx:
        db_actions = process_trade_actions_and_sizing(
            trade=trade,
            db_message_id=db_message.id,
            parsed_actions=analysis.actions
        )
        for db_action in db_actions:
            session.add(db_action)
        session.commit()

        # Record Stop-Loss Classification & Spot Monitoring Registration stages
        for a in db_actions:
            if getattr(a, "sl_trigger_type", None) == "UNDERLYING_SPOT_TRIGGER" and a.sl_trigger_price is not None:
                record_stage(
                    stage="SPOT_SL_MONITOR_REGISTERED",
                    status="SUCCESS",
                    message_id=db_message.id,
                    telegram_message_id=db_message.telegram_message_id,
                    trade_id=trade.id,
                    revision=rev,
                    details={
                        "action_id": a.id,
                        "underlying": a.underlying or trade.underlying,
                        "sl_trigger_type": a.sl_trigger_type,
                        "sl_trigger_price": a.sl_trigger_price,
                        "sl_trigger_direction": a.sl_trigger_direction,
                        "raw_stoploss": a.stoploss
                    },
                    session=session
                )
            elif getattr(a, "sl_trigger_type", None) == "OPTION_PREMIUM_TRIGGER" and a.sl_trigger_price is not None:
                record_stage(
                    stage="PREMIUM_SL_IDENTIFIED",
                    status="INFO",
                    message_id=db_message.id,
                    telegram_message_id=db_message.telegram_message_id,
                    trade_id=trade.id,
                    revision=rev,
                    details={
                        "action_id": a.id,
                        "tradingsymbol": a.tradingsymbol,
                        "sl_trigger_type": a.sl_trigger_type,
                        "sl_trigger_price": a.sl_trigger_price,
                        "sl_trigger_direction": a.sl_trigger_direction,
                        "raw_stoploss": a.stoploss
                    },
                    session=session
                )

        unresolved = [a.instrument_name or a.action_type for a in db_actions if not a.tradingsymbol]
        if unresolved:
            ctx.set_warning(f"Could not resolve NFO tradingsymbol for: {', '.join(unresolved)}")

        ctx.set_details({
            "actions_count": len(db_actions),
            "legs": [
                {
                    "action_id": a.id,
                    "action_type": a.action_type,
                    "symbol": a.tradingsymbol,
                    "is_main": a.is_main,
                    "quantity": a.quantity,
                    "lots": a.lots,
                    "price": a.price,
                    "sl_trigger_type": getattr(a, "sl_trigger_type", None),
                    "sl_trigger_price": getattr(a, "sl_trigger_price", None),
                    "sl_monitoring_active": getattr(a, "sl_monitoring_active", False)
                }
                for a in db_actions
            ]
        })

    # 5. Automatic Square-Off Generation for exits / closed trades
    if trade and (analysis.trade_status_update == "CLOSED" or trade.status == "CLOSED" or is_emergency_exit_phrase(db_message.text) or any(a.action_type in ["EXIT", "CLOSE_LEG"] for a in analysis.actions)):
        with StageContext("SQUARE_OFF_GENERATION", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, trade_id=trade.id, revision=rev, session=session) as ctx:
            sq_actions = ensure_square_off_actions(session, trade, db_message, analysis.actions)
            for sq_a in sq_actions:
                if sq_a not in db_actions:
                    db_actions.append(sq_a)
            ctx.set_details({
                "square_off_actions_count": len(sq_actions),
                "legs": [f"{a.transaction_type} {a.quantity} x {a.tradingsymbol}" for a in sq_actions]
            })

    # 6. Automatic Order Placement Check
    with StageContext("ORDER_EXECUTION", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, trade_id=trade.id, revision=rev, session=session) as ctx:
        logger.info(f"Running automated order placement check for Trade ID {trade.id}...")
        exec_results = execute_trade_actions(session, trade.id, auto_mode=True)
        failed_orders = [r for r in exec_results if not r.get("success")]
        if failed_orders:
            ctx.set_warning(f"{len(failed_orders)} orders failed: {', '.join(r.get('message', '') for r in failed_orders)}")
        ctx.set_details({"results": exec_results, "auto_mode": True})

    # 6b. Live Position Book Closure Verification (if trade is closed or exiting)
    if trade and (trade.status == "CLOSED" or analysis.trade_status_update == "CLOSED" or is_emergency_exit_phrase(db_message.text) or any(a.action_type in ["EXIT", "CLOSE_LEG"] for a in analysis.actions)):
        with StageContext("TRADE_POSITION_CLOSURE_VERIFICATION", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, trade_id=trade.id, revision=rev, session=session) as ctx:
            all_trade_acts = session.query(Action).filter(Action.trade_id == trade.id, Action.tradingsymbol != None).all()
            trade_symbols = list({a.tradingsymbol for a in all_trade_acts if a.tradingsymbol})
            if trade_symbols:
                verif = verify_zerodha_positions_zero(trade_symbols)
                if verif["verified"]:
                    if verif["all_zero"]:
                        logger.info(f"Trade #{trade.id} position closure verified: All {len(trade_symbols)} Zerodha positions are zero ({', '.join(trade_symbols)}).")
                        ctx.set_details(verif)
                    else:
                        logger.warning(f"Trade #{trade.id} closure verification ALERT: Non-zero positions remain on Zerodha: {verif['open_positions']}")
                        ctx.set_warning(verif["message"])
                        ctx.set_details(verif)
                else:
                    ctx.set_warning(verif["message"])
                    ctx.set_details(verif)

    # 7. Action Notification to Telegram
    if actions_entity and db_actions:
        with StageContext("TELEGRAM_NOTIFICATION", message_id=db_message.id, telegram_message_id=db_message.telegram_message_id, trade_id=trade.id, revision=rev, session=session) as ctx:
            try:
                session.refresh(trade)
                for a in db_actions:
                    session.refresh(a)
                    
                html_msg = format_action_telegram_message_html(trade, db_actions)

                # If any actionable orders remain PENDING, show "Place Order(s)" inline button
                has_pending_orders = any(
                    a.order_status == "PENDING" and a.action_type in ["BUY", "SELL", "EXIT", "CLOSE_LEG"]
                    for a in db_actions
                )

                buttons = None
                if has_pending_orders:
                    buttons = [Button.inline("🚀 Place Order(s)", data=f"place_order:{trade.id}")]

                await client.send_message(actions_entity, html_msg, parse_mode='html', buttons=buttons)

                # Check for any unexecuted / failed action items to dispatch an immediate IMPORTANT NOTICE
                unexecuted_actions = [
                    a for a in db_actions
                    if a.action_type in ["BUY", "SELL", "EXIT", "CLOSE_LEG"]
                    and a.order_status in ["FAILED", "CANCELLED"]
                ]
                # Also include actionable legs missing tradingsymbol or quantity
                for ua in db_actions:
                    if ua.action_type in ["BUY", "SELL", "EXIT", "CLOSE_LEG"] and (not ua.tradingsymbol or not ua.quantity) and ua not in unexecuted_actions:
                        if not ua.zerodha_response:
                            ua.zerodha_response = "Missing tradingsymbol or quantity; could not be resolved from master instruments"
                        unexecuted_actions.append(ua)

                if unexecuted_actions:
                    logger.warning(f"Sending Important Notice for {len(unexecuted_actions)} unexecuted action(s) in Trade #{trade.id}")
                    notice_msg = format_important_notice_telegram_html(trade, unexecuted_actions)
                    
                    notice_buttons = [Button.inline("🚀 Retry / Place Order(s)", data=f"place_order:{trade.id}")]
                    await client.send_message(actions_entity, notice_msg, parse_mode='html', buttons=notice_buttons)
                    
                    record_stage(
                        stage="TELEGRAM_IMPORTANT_NOTICE_SENT",
                        status="WARNING",
                        message_id=db_message.id,
                        telegram_message_id=db_message.telegram_message_id,
                        trade_id=trade.id,
                        revision=rev,
                        details={
                            "unexecuted_actions_count": len(unexecuted_actions),
                            "unexecuted_symbols": [a.tradingsymbol or a.instrument_name or a.action_type for a in unexecuted_actions],
                            "reasons": [a.zerodha_response for a in unexecuted_actions]
                        },
                        session=session
                    )

                # Mark actions as sent
                for a in db_actions:
                    a.telegram_sent = True
                session.commit()
                logger.info(f"Action notifications sent to Telegram for Trade ID {trade.id}")
                ctx.set_details({
                    "has_pending_orders": has_pending_orders,
                    "actions_notified": len(db_actions),
                    "unexecuted_notified": len(unexecuted_actions)
                })
            except Exception as ae:
                logger.error(f"Failed to send actions notification: {ae}")
                ctx.set_error(str(ae))

    db_message.analysed_by_ai = True
    db_message.processed = True
    db_message.processed_at = datetime.utcnow()
    session.commit()

    record_stage(
        stage="MESSAGE_COMPLETED",
        status="SUCCESS",
        message_id=db_message.id,
        telegram_message_id=db_message.telegram_message_id,
        trade_id=trade.id if trade else None,
        revision=rev,
        details={"trade_id": trade.id if trade else None, "actions_count": len(db_actions) if 'db_actions' in locals() else 0},
        session=session
    )
    return True

async def check_active_spot_stoplosses(actions_entity=None, session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Active Market-Data Monitoring Loop for Stop-Losses based on underlying cash/spot index or stock level.
    Monitors live spot LTP for all open trades with Action.sl_monitoring_active == True.
    When the spot threshold is crossed:
      1. Marks action as sl_triggered=True and sl_monitoring_active=False.
      2. Updates trade.status to 'CLOSED'.
      3. Automatically executes market exits (square-off) across all open positions.
      4. Dispatches immediate Telegram alert to actions_channel.
    """
    close_sess = False
    s = session
    if s is None:
        s = db.SessionLocal()
        close_sess = True

    try:
        monitored_actions = s.query(Action).join(Trade, Action.trade_id == Trade.id).filter(
            Trade.status == "OPEN",
            Action.sl_monitoring_active == True,
            Action.sl_triggered == False,
            Action.sl_trigger_type == "UNDERLYING_SPOT_TRIGGER",
            Action.sl_trigger_price != None
        ).all()

        if not monitored_actions:
            return []

        results = []
        trades_to_check = {}
        for act in monitored_actions:
            t_id = act.trade_id
            if t_id not in trades_to_check:
                trades_to_check[t_id] = {
                    "trade": act.trade,
                    "actions": []
                }
            trades_to_check[t_id]["actions"].append(act)

        for trade_id, t_info in trades_to_check.items():
            trade = t_info["trade"]
            actions = t_info["actions"]
            underlying = clean_symbol(trade.underlying) or (actions[0].underlying if actions else None)
            if not underlying:
                continue

            spot_ltp = None
            try:
                spot_ltp = get_spot_ltp(underlying)
            except Exception as le:
                logger.warning(f"Failed to fetch live spot LTP for {underlying} during SL monitoring: {le}")

            if spot_ltp is None or spot_ltp <= 0:
                continue

            for act in actions:
                target_sl = act.sl_trigger_price
                direction = (act.sl_trigger_direction or "BELOW").upper()
                is_triggered = False

                if direction == "BELOW" and spot_ltp <= target_sl:
                    is_triggered = True
                elif direction == "ABOVE" and spot_ltp >= target_sl:
                    is_triggered = True

                if is_triggered:
                    logger.warning(
                        f"[SPOT SL TRIGGERED] Trade #{trade.id} ({underlying}): "
                        f"Current Spot LTP {spot_ltp:,.2f} crossed threshold {target_sl:,.2f} ({direction}). "
                        f"Triggering immediate market exit across all positions!"
                    )

                    act.sl_triggered = True
                    act.sl_monitoring_active = False
                    act.sl_triggered_at = datetime.utcnow()
                    act.sl_order_status = "TRIGGERED"

                    trade.status = "CLOSED"
                    trade.closed_at = datetime.utcnow()
                    trade.context_summary = f"Trade closed: Spot SL triggered at {spot_ltp:,.2f} (Threshold: {target_sl:,.2f})"
                    s.commit()

                    record_stage(
                        stage="SPOT_SL_TRIGGERED",
                        status="SUCCESS",
                        message_id=act.message_id,
                        trade_id=trade.id,
                        details={
                            "trade_id": trade.id,
                            "action_id": act.id,
                            "underlying": underlying,
                            "spot_ltp": spot_ltp,
                            "sl_trigger_price": target_sl,
                            "direction": direction,
                            "stoploss_text": act.stoploss
                        },
                        session=s
                    )

                    # Generate emergency square-off actions
                    sq_actions = ensure_square_off_actions(s, trade, db_message=act.message)

                    # Execute square off orders immediately (emergency market exit)
                    auto_sl_exit = os.getenv("AUTO_EXECUTE_STOPLOSS_EXITS", "true").lower() in ("true", "1", "t", "yes")
                    exec_results = execute_trade_actions(s, trade.id, auto_mode=not auto_sl_exit)

                    trigger_result = {
                        "trade_id": trade.id,
                        "action_id": act.id,
                        "underlying": underlying,
                        "spot_ltp": spot_ltp,
                        "sl_trigger_price": target_sl,
                        "direction": direction,
                        "square_off_actions": len(sq_actions),
                        "exec_results": exec_results
                    }
                    results.append(trigger_result)

                    # Send Telegram alert if actions entity is present
                    if actions_entity and client:
                        try:
                            alert_html = format_spot_sl_triggered_telegram_html(trade, act, spot_ltp, exec_results)
                            await client.send_message(actions_entity, alert_html, parse_mode='html')
                        except Exception as te:
                            logger.error(f"Failed to send Spot SL Telegram alert: {te}")

        return results
    finally:
        if close_sess and s:
            s.close()

async def sync_and_process():
    """Main worker iteration to sync messages and run Gemini processing."""
    # Check if currently within active Telegram schedule
    is_active, sched_reason = is_telegram_time_active()
    if not is_active:
        logger.info(f"Sync skipped: {sched_reason}")
        return

    source_channel_id = os.getenv("TELEGRAM_SOURCE_CHANNEL")
    mirror_channel_id = os.getenv("TELEGRAM_MIRROR_CHANNEL")
    actions_channel_id = os.getenv("TELEGRAM_ACTIONS_CHANNEL")
    
    if not source_channel_id:
        logger.error("TELEGRAM_SOURCE_CHANNEL not configured. Run interactive setup or configure .env.")
        return

    logger.info("Connecting to Telegram...")
    if not await check_login():
        logger.error("Telegram is not authorized. Please run interactive setup first!")
        return

    # Resolve entities
    source_entity = await get_channel_entity(source_channel_id)
    if not source_entity:
        logger.error(f"Could not resolve source channel: {source_channel_id}")
        return

    mirror_entity = await get_channel_entity(mirror_channel_id) if mirror_channel_id else None
    actions_entity = await get_channel_entity(actions_channel_id) if actions_channel_id else None

    if not mirror_entity:
        logger.warning("TELEGRAM_MIRROR_CHANNEL not configured or resolved. Raw message mirroring will be skipped.")
    if not actions_entity:
        logger.warning("TELEGRAM_ACTIONS_CHANNEL not configured or resolved. Action notifications will be skipped.")

    # Create DB Session
    session = db.SessionLocal()
    try:
        # Find maximum message ID processed
        max_msg_row = session.query(Message).filter(Message.channel_id == str(source_channel_id)).order_by(Message.telegram_message_id.desc()).first()
        min_id = max_msg_row.telegram_message_id if max_msg_row else 0

        logger.info(f"Syncing messages since ID {min_id} (progressive)...")

        # Fetch messages in chronological order (reverse=True)
        async for msg in client.iter_messages(source_entity, min_id=min_id, reverse=True):
            if not msg.text:
                continue

            # Check if message timestamp falls within active schedule
            msg_active, msg_sched_reason = is_telegram_time_active(msg.date)
            if not msg_active:
                logger.info(f"Skipping message ID {msg.id} (sent at {msg.date}): {msg_sched_reason}")
                # Store message as processed to advance sync min_id without triggering trade analysis/actions
                db_message = Message(
                    telegram_message_id=msg.id,
                    channel_id=str(source_channel_id),
                    date=msg.date,
                    text=msg.text,
                    processed=True,
                    analysed_by_ai=True,
                    processed_at=datetime.utcnow(),
                    revision=0
                )
                session.add(db_message)
                session.commit()
                session.refresh(db_message)

                record_stage(
                    stage="TIME_WINDOW_FILTER",
                    status="SKIPPED",
                    message_id=db_message.id,
                    telegram_message_id=msg.id,
                    revision=0,
                    details={"reason": msg_sched_reason, "msg_date": str(msg.date), "raw_text": msg.text[:80]},
                    session=session
                )
                continue

            if is_poke_message(msg.text):
                logger.info(f"Skipping poke message ID {msg.id}: '{msg.text.strip()}' (poke notification, not a trade recommendation)")
                # Store raw message as already processed to advance sync min_id without triggering trade analysis/actions
                db_message = Message(
                    telegram_message_id=msg.id,
                    channel_id=str(source_channel_id),
                    date=msg.date,
                    text=msg.text,
                    processed=True,
                    analysed_by_ai=True,
                    processed_at=datetime.utcnow()
                )
                session.add(db_message)
                session.commit()
                session.refresh(db_message)

                record_stage(
                    stage="POKE_FILTER",
                    status="SKIPPED",
                    message_id=db_message.id,
                    telegram_message_id=msg.id,
                    revision=0,
                    details={"reason": "Poke notification ping, not a trade recommendation", "raw_text": msg.text.strip()},
                    session=session
                )
                continue

            logger.info(f"Syncing new message ID {msg.id}: {msg.text[:50]}...")

            # 1. Store raw message in DB
            db_message = Message(
                telegram_message_id=msg.id,
                channel_id=str(source_channel_id),
                date=msg.date,
                text=msg.text,
                processed=False,
                analysed_by_ai=False,
                revision=0
            )
            session.add(db_message)
            session.commit()
            session.refresh(db_message)

            record_stage(
                stage="SYNC_RECEIVED",
                status="SUCCESS",
                message_id=db_message.id,
                telegram_message_id=msg.id,
                revision=0,
                details={"source_channel": str(source_channel_id), "date": str(msg.date), "text_snippet": msg.text[:80]},
                session=session
            )

            # 2. Mirror raw message
            if mirror_entity:
                try:
                    await client.send_message(mirror_entity, msg.text)
                    logger.info(f"Mirrored message ID {msg.id}")
                    record_stage(
                        stage="MIRROR_FORWARDED",
                        status="SUCCESS",
                        message_id=db_message.id,
                        telegram_message_id=msg.id,
                        revision=0,
                        details={"mirror_channel": str(mirror_channel_id)},
                        session=session
                    )
                except Exception as me:
                    logger.error(f"Failed to mirror message ID {msg.id}: {me}")
                    record_stage(
                        stage="MIRROR_FORWARD_FAILED",
                        status="WARNING",
                        message_id=db_message.id,
                        telegram_message_id=msg.id,
                        revision=0,
                        error_message=str(me),
                        details={"mirror_channel": str(mirror_channel_id)},
                        session=session
                    )

        # Check active underlying spot stoplosses before and after sync
        await check_active_spot_stoplosses(actions_entity, session=session)

        # 3. Process all unanalysed messages in chronological order
        unanalysed_messages = session.query(Message).filter(Message.analysed_by_ai == False).order_by(Message.id.asc()).all()
        if unanalysed_messages:
            logger.info(f"Found {len(unanalysed_messages)} unanalysed messages in DB. Processing...")
            for db_message in unanalysed_messages:
                await process_single_message(session, db_message, actions_entity)

        # Check active underlying spot stoplosses after processing messages
        await check_active_spot_stoplosses(actions_entity, session=session)

    except Exception as e:
        logger.exception(f"Error in sync_and_process loop: {e}")
        session.rollback()
    finally:
        session.close()

async def worker_loop():
    """Worker loop that runs periodically."""
    logger.info("Initializing DB tables...")
    db.init_db()
    
    # Register Telegram CallbackQuery button listeners
    setup_telegram_event_handlers()

    logger.info(f"Telegram active schedule: {get_schedule_description()}")
    logger.info("Starting background sync and processing loop...")
    
    last_inactive_log_time = 0.0

    while True:
        try:
            is_active, sched_reason = is_telegram_time_active()
            if not is_active:
                now_sec = asyncio.get_event_loop().time()
                # Log once every 60 seconds when waiting outside active hours
                if now_sec - last_inactive_log_time >= 60.0:
                    logger.info(f"⏳ {sched_reason}. Pausing Telegram sync.")
                    last_inactive_log_time = now_sec
            else:
                last_inactive_log_time = 0.0
                await sync_and_process()
        except Exception as e:
            logger.error(f"Unhandled error in worker main loop: {e}")
        
        try:
            poll_interval = float(os.getenv("TELEGRAM_REFRESH_INTERVAL", "10"))
        except (ValueError, TypeError):
            poll_interval = 10.0
            
        await asyncio.sleep(poll_interval)

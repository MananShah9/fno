from datetime import datetime
from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from db import Base

class Message(Base):
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_message_id = Column(Integer, nullable=True, index=True)
    channel_id = Column(String, nullable=True, index=True)
    date = Column(DateTime, default=datetime.utcnow)
    text = Column(Text, nullable=False)
    processed = Column(Boolean, default=False, index=True)
    processed_at = Column(DateTime, nullable=True)
    analysed_by_ai = Column(Boolean, default=False, index=True)
    ai_response = Column(Text, nullable=True)
    revision = Column(Integer, default=0, index=True)
    last_stage = Column(String, nullable=True)
    last_status = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)

    # Relationships
    actions = relationship("Action", back_populates="message", cascade="all, delete-orphan")
    stage_traces = relationship("MessageStageTrace", back_populates="message", cascade="all, delete-orphan", order_by="MessageStageTrace.id.asc()")

    def __repr__(self):
        return f"<Message(id={self.id}, tg_id={self.telegram_message_id}, processed={self.processed})>"


class Trade(Base):
    __tablename__ = 'trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(String, default="OPEN", index=True)  # "OPEN", "CLOSED"
    structure_type = Column(String, nullable=True)       # e.g. "NIFTY PE SPREAD", "VBL BEAR FUT SPREAD"
    underlying = Column(String, nullable=True, index=True)  # e.g. "NIFTY", "VBL"
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    context_summary = Column(Text, nullable=True)         # Short details about the trade for LLM context

    # Relationships
    actions = relationship("Action", back_populates="trade")
    stage_traces = relationship("MessageStageTrace", back_populates="trade")

    def __repr__(self):
        return f"<Trade(id={self.id}, status={self.status}, underlying={self.underlying}, type={self.structure_type})>"


class Action(Base):
    __tablename__ = 'actions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey('trades.id'), nullable=True)
    message_id = Column(Integer, ForeignKey('messages.id'), nullable=False)
    action_type = Column(String, nullable=False)  # "BUY", "SELL", "EXIT", "UPDATE_SL", "CLOSE_LEG", "INFO"
    instrument_name = Column(String, nullable=True)  # e.g. "NIFTY 28JUL2026 24000 PE" (Zerodha copyable search)
    price = Column(String, nullable=True)
    stoploss = Column(String, nullable=True)
    target = Column(String, nullable=True)
    is_limit = Column(Boolean, default=False)
    details = Column(Text, nullable=True)
    telegram_sent = Column(Boolean, default=False, index=True)

    # Zerodha Execution Parameters
    is_main = Column(Boolean, default=True)           # True if main/primary leg, False if hedge leg
    underlying = Column(String, nullable=True)
    option_type = Column(String, nullable=True)
    strike = Column(Float, nullable=True)
    expiry = Column(String, nullable=True)
    lots = Column(Integer, default=1)
    quantity = Column(Integer, nullable=True)
    tradingsymbol = Column(String, nullable=True)
    instrument_token = Column(Integer, nullable=True)
    transaction_type = Column(String, nullable=True)  # "BUY" or "SELL"
    order_type = Column(String, nullable=True)        # "MARKET" or "LIMIT"
    product = Column(String, default="NRML")           # "NRML" or "MIS"
    order_status = Column(String, default="PENDING", index=True)  # "PENDING", "PLACED", "FAILED", "EXECUTED", "CANCELLED"
    zerodha_order_id = Column(String, nullable=True)
    zerodha_response = Column(Text, nullable=True)
    placed_at = Column(DateTime, nullable=True)

    # Relationships
    trade = relationship("Trade", back_populates="actions")
    message = relationship("Message", back_populates="actions")

    def __repr__(self):
        return f"<Action(id={self.id}, type={self.action_type}, symbol={self.tradingsymbol}, status={self.order_status})>"


class MessageStageTrace(Base):
    __tablename__ = 'message_stage_traces'

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=True, index=True)
    telegram_message_id = Column(Integer, nullable=True, index=True)
    trade_id = Column(Integer, ForeignKey('trades.id', ondelete='SET NULL'), nullable=True, index=True)
    revision = Column(Integer, default=0, index=True)
    stage = Column(String, nullable=False, index=True)
    status = Column(String, default="SUCCESS", index=True)  # "SUCCESS", "WARNING", "ERROR", "SKIPPED", "INFO", "IN_PROGRESS"
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    duration_ms = Column(Float, nullable=True)
    location = Column(String, nullable=True)
    details = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    stack_trace = Column(Text, nullable=True)

    # Relationships
    message = relationship("Message", back_populates="stage_traces")
    trade = relationship("Trade", back_populates="stage_traces")

    def __repr__(self):
        return f"<MessageStageTrace(id={self.id}, msg_id={self.message_id}, stage={self.stage}, status={self.status}, loc={self.location})>"

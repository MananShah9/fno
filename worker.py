import os
import asyncio
import logging
import sys
import json
from datetime import datetime
from sqlalchemy.orm import Session
from dotenv import load_dotenv

import db
from models import Message, Trade, Action
from telegram_client import client, check_login, get_channel_entity
from gemini_client import analyze_message_with_ai

load_dotenv()

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("worker")

def get_open_trades_context(session: Session):
    """Fetches all open trades formatted as context for Gemini."""
    open_trades = session.query(Trade).filter(Trade.status == "OPEN").all()
    
    context = []
    for trade in open_trades:
        actions_list = []
        for action in trade.actions:
            actions_list.append({
                "action_type": action.action_type,
                "instrument_name": action.instrument_name,
                "price": action.price,
                "stoploss": action.stoploss,
                "target": action.target,
                "details": action.details
            })
            
        context.append({
            "id": trade.id,
            "status": trade.status,
            "structure_type": trade.structure_type,
            "underlying": trade.underlying,
            "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
            "context_summary": trade.context_summary,
            "existing_orders": actions_list
        })
    return context

def format_action_telegram_message_html(trade: Trade, actions: list) -> str:
    """Formats actions into a beautiful Telegram HTML message with click-to-copy fields."""
    is_exit = any(a.action_type == 'EXIT' for a in actions)
    is_update = any(a.action_type in ['UPDATE_SL', 'CLOSE_LEG', 'INFO'] for a in actions)
    
    msg_parts = []
    
    if is_exit:
        msg_parts.append("🚪 <b>TRADE CLOSED</b>")
    elif is_update:
        msg_parts.append("🔄 <b>TRADE UPDATE</b>")
    else:
        msg_parts.append("🔔 <b>NEW TRADE DETECTED</b>")
        
    msg_parts.append(f"<b>Trade ID:</b> #{trade.id}")
    msg_parts.append(f"<b>Underlying:</b> {trade.underlying or 'N/A'}")
    if trade.structure_type:
        msg_parts.append(f"<b>Strategy:</b> {trade.structure_type}")
        
    msg_parts.append("\n<b>Actions Required:</b>")
    for action in actions:
        action_prefix = "• "
        if action.action_type == "BUY":
            action_prefix += "🟢 <b>BUY:</b> "
        elif action.action_type == "SELL":
            action_prefix += "🔴 <b>SELL:</b> "
        elif action.action_type == "EXIT":
            action_prefix += "🚪 <b>EXIT:</b> "
        elif action.action_type == "UPDATE_SL":
            action_prefix += "🛡️ <b>UPDATE SL:</b> "
        elif action.action_type == "CLOSE_LEG":
            action_prefix += "❌ <b>CLOSE LEG:</b> "
        else:
            action_prefix += "ℹ️ <b>INFO:</b> "
            
        action_line = f"{action_prefix}"
        if action.instrument_name:
            action_line += f"<code>{action.instrument_name}</code> "
            
        detail_parts = []
        if action.price:
            detail_parts.append(f"Price: <code>{action.price}</code>")
        if action.stoploss:
            detail_parts.append(f"SL: <code>{action.stoploss}</code>")
        if action.target:
            detail_parts.append(f"Target: <code>{action.target}</code>")
            
        if detail_parts:
            action_line += f"({', '.join(detail_parts)})"
            
        if action.details:
            action_line += f"\n  <i>Note: {action.details}</i>"
            
        msg_parts.append(action_line)
        
    if trade.context_summary:
        msg_parts.append(f"\n📝 <b>Current Trade Context:</b>\n<i>{trade.context_summary}</i>")
        
    return "\n".join(msg_parts)

async def sync_and_process():
    """Main worker iteration to sync messages and run Gemini processing."""
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

            logger.info(f"Syncing new message ID {msg.id}: {msg.text[:50]}...")

            # 1. Store raw message in DB
            db_message = Message(
                telegram_message_id=msg.id,
                channel_id=str(source_channel_id),
                date=msg.date,
                text=msg.text,
                processed=False,
                analysed_by_ai=False
            )
            session.add(db_message)
            session.commit()
            session.refresh(db_message)

            # 2. Mirror raw message
            if mirror_entity:
                try:
                    await client.send_message(mirror_entity, msg.text)
                    logger.info(f"Mirrored message ID {msg.id}")
                except Exception as me:
                    logger.error(f"Failed to mirror message ID {msg.id}: {me}")

        # 3. Process all unanalysed messages in chronological order
        unanalysed_messages = session.query(Message).filter(Message.analysed_by_ai == False).order_by(Message.id.asc()).all()
        if unanalysed_messages:
            logger.info(f"Found {len(unanalysed_messages)} unanalysed messages in DB. Processing...")
            
            for db_message in unanalysed_messages:
                logger.info(f"Analyzing message ID {db_message.id} (TG ID: {db_message.telegram_message_id or 'N/A'})...")
                
                # Fetch open trades context and send to Gemini
                open_trades = get_open_trades_context(session)
                analysis = analyze_message_with_ai(db_message.text, open_trades)

                if analysis:
                    db_message.ai_response = json.dumps(analysis.model_dump(), default=str)
                    
                    if analysis.is_valid_trade_msg:
                        logger.info(f"Valid trade detected by AI for message ID {db_message.id}.")
                        
                        trade = None
                        # Is it a continuation? Try to find existing open trade
                        if analysis.is_continuation and analysis.related_open_trade_id:
                            trade = session.query(Trade).filter(Trade.id == analysis.related_open_trade_id).first()
                            if trade:
                                logger.info(f"Mapping message ID {db_message.id} to existing Trade ID {trade.id}")
                        
                        # Create new trade if not a continuation or parent trade not found
                        if not trade:
                            trade = Trade(
                                status="OPEN",
                                structure_type=analysis.structure_type,
                                underlying=analysis.underlying,
                                opened_at=db_message.date or datetime.utcnow()
                            )
                            session.add(trade)
                            session.commit()
                            session.refresh(trade)
                            logger.info(f"Created new Trade ID {trade.id}")

                        # Update Trade state and status
                        trade.context_summary = analysis.context_summary or trade.context_summary
                        if analysis.trade_status_update == "CLOSED":
                            trade.status = "CLOSED"
                            trade.closed_at = db_message.date or datetime.utcnow()
                            logger.info(f"Trade ID {trade.id} status updated to CLOSED")
                        
                        session.commit()

                        # Save Actions
                        db_actions = []
                        for action in analysis.actions:
                            db_action = Action(
                                trade_id=trade.id,
                                message_id=db_message.id,
                                action_type=action.action_type,
                                instrument_name=action.instrument_name,
                                price=action.price,
                                stoploss=action.stoploss,
                                target=action.target,
                                is_limit=action.is_limit,
                                details=action.details,
                                telegram_sent=False
                            )
                            session.add(db_action)
                            db_actions.append(db_action)
                        
                        session.commit()

                        # Send formatted message to actions channel
                        if actions_entity and db_actions:
                            try:
                                # Refresh objects
                                session.refresh(trade)
                                for a in db_actions:
                                    session.refresh(a)
                                    
                                html_msg = format_action_telegram_message_html(trade, db_actions)
                                await client.send_message(actions_entity, html_msg, parse_mode='html')
                                
                                # Mark actions as sent
                                for a in db_actions:
                                    a.telegram_sent = True
                                session.commit()
                                logger.info(f"Action notifications sent to Telegram for Trade ID {trade.id}")
                            except Exception as ae:
                                logger.error(f"Failed to send actions notification: {ae}")

                    db_message.analysed_by_ai = True
                    db_message.processed = True
                    db_message.processed_at = datetime.utcnow()
                    session.commit()
                else:
                    logger.error(f"Failed to analyze message ID {db_message.id} with AI. Skipping and keeping it unanalysed for retry.")

    except Exception as e:
        logger.exception(f"Error in sync_and_process loop: {e}")
        session.rollback()
    finally:
        session.close()

async def worker_loop():
    """Worker loop that runs periodically."""
    logger.info("Initializing DB tables...")
    db.init_db()
    
    logger.info("Starting background sync and processing loop...")
    while True:
        try:
            await sync_and_process()
        except Exception as e:
            logger.error(f"Unhandled error in worker main loop: {e}")
        await asyncio.sleep(10)  # Polling interval: 10 seconds

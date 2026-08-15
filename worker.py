import os
import asyncio
import logging
import sys
import json
from datetime import datetime
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from telethon import events, Button, utils

import db
from models import Message, Trade, Action
from telegram_client import client, check_login, get_channel_entity
from gemini_client import analyze_message_with_ai, clean_symbol, is_poke_message
from instruments_manager import resolve_nfo_instrument, parse_price_value, calculate_lots_from_budget
from zerodha_client import place_zerodha_order, check_existing_zerodha_order_or_position, get_nfo_ltp
from stage_tracker import record_stage, StageContext, get_code_location

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
                "tradingsymbol": action.tradingsymbol,
                "transaction_type": action.transaction_type,
                "price": action.price,
                "stoploss": action.stoploss,
                "target": action.target,
                "details": action.details
            })
            
        clean_u = clean_symbol(trade.underlying)
        summary = (trade.context_summary or "")[:200]
        context.append({
            "id": trade.id,
            "status": trade.status,
            "structure_type": trade.structure_type,
            "underlying": clean_u,
            "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
            "context_summary": summary,
            "existing_orders": actions_list
        })
    return context

def format_action_telegram_message_html(trade: Trade, actions: list) -> str:
    """Formats actions into a beautiful Telegram HTML message with Zerodha execution info."""
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
        
    msg_parts.append("\n<b>Actions & Zerodha Order Details:</b>")
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
            detail_parts.append(f"SL: <code>{action.stoploss}</code>")
        if action.target:
            detail_parts.append(f"Target: <code>{action.target}</code>")
            
        if detail_parts:
            action_line += f"({', '.join(detail_parts)})"

        if action.order_status and action.order_status != "PENDING":
            status_icon = "✅" if action.order_status == "PLACED" else "❌"
            action_line += f"\n  Status: {status_icon} <b>{action.order_status}</b>"
            if action.zerodha_order_id:
                action_line += f" (Order ID: <code>{action.zerodha_order_id}</code>)"

        if action.details:
            action_line += f"\n  <i>Note: {action.details}</i>"
            
        msg_parts.append(action_line)
        
    if trade.context_summary:
        msg_parts.append(f"\n📝 <b>Current Trade Context:</b>\n<i>{trade.context_summary}</i>")
        
    return "\n".join(msg_parts)

def ensure_square_off_actions(session: Session, trade: Trade, db_message: Message, parsed_actions: list = None) -> list:
    """
    Scans a trade's open entry legs and creates reverse square-off Action records
    if the trade is being closed or exited, avoiding duplicate square-offs.
    """
    # Find all prior entry legs for this trade that have a resolved tradingsymbol
    entry_actions = session.query(Action).filter(
        Action.trade_id == trade.id,
        Action.action_type.in_(["BUY", "SELL"]),
        Action.tradingsymbol != None
    ).order_by(Action.id.asc()).all()

    if not entry_actions:
        return []

    # Find already existing exit actions for this trade
    existing_exit_actions = session.query(Action).filter(
        Action.trade_id == trade.id,
        Action.action_type.in_(["EXIT", "CLOSE_LEG"])
    ).all()
    already_exited_symbols = {a.tradingsymbol for a in existing_exit_actions if a.tradingsymbol}

    parsed_actions = parsed_actions or []
    new_square_off_actions = []

    for entry_act in entry_actions:
        if entry_act.tradingsymbol in already_exited_symbols:
            logger.info(f"Leg {entry_act.tradingsymbol} already has a square-off action. Skipping.")
            continue

        # Determine reverse transaction type: SELL -> BUY, BUY -> SELL
        original_tt = (entry_act.transaction_type or entry_act.action_type).upper()
        reverse_tt = "BUY" if original_tt == "SELL" else "SELL"

        # Check if AI parsed explicit price or order_type for this leg in parsed_actions
        matching_parsed = None
        for pa in parsed_actions:
            pa_sym = clean_symbol(getattr(pa, "underlying", None) or getattr(pa, "instrument_name", None))
            pa_strike = getattr(pa, "strike", None)
            if (entry_act.strike and pa_strike and abs(entry_act.strike - pa_strike) < 0.01) or \
               (entry_act.tradingsymbol and pa_sym and pa_sym in entry_act.tradingsymbol):
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
            details=f"Square-off exit leg for {entry_act.tradingsymbol}",
            telegram_sent=False,
            underlying=entry_act.underlying,
            option_type=entry_act.option_type,
            strike=entry_act.strike,
            expiry=entry_act.expiry,
            lots=entry_act.lots,
            quantity=entry_act.quantity,
            tradingsymbol=entry_act.tradingsymbol,
            instrument_token=entry_act.instrument_token,
            transaction_type=reverse_tt,
            order_type=ord_type,
            product=entry_act.product or "NRML",
            order_status="PENDING"
        )
        session.add(square_off_action)
        new_square_off_actions.append(square_off_action)
        already_exited_symbols.add(entry_act.tradingsymbol)
        logger.info(f"Generated square-off action for Trade #{trade.id}: {reverse_tt} {entry_act.quantity} x {entry_act.tradingsymbol} ({ord_type})")

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
    calculates trade lots from target investment budget (.env), ensures hedge
    quantity matches main quantity, and returns list of Action DB objects.
    """
    if target_budget is None:
        raw_budget = os.getenv("TARGET_INVESTMENT_BUDGET") or os.getenv("TARGET_INVESTMENT_BUDGET_MAIN")
        if raw_budget:
            try:
                target_budget = float(raw_budget)
            except (ValueError, TypeError):
                target_budget = None

    resolved_items = []
    entry_indices = []

    for idx, action_schema in enumerate(parsed_actions):
        u_symbol = clean_symbol(getattr(action_schema, "underlying", None) or trade.underlying)
        o_type = getattr(action_schema, "option_type", None) or "CE"
        strike_val = getattr(action_schema, "strike", None)
        expiry_str = getattr(action_schema, "expiry_info", None)

        inst = resolve_nfo_instrument(u_symbol, strike_val, o_type, expiry_str)
        action_type = getattr(action_schema, "action_type", "INFO").upper()

        resolved_items.append({
            "schema": action_schema,
            "action_type": action_type,
            "underlying": u_symbol,
            "option_type": o_type,
            "strike": strike_val,
            "expiry": inst["expiry"] if inst else expiry_str,
            "inst": inst,
            "is_main": True,
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

        # 3. Calculate lots for main leg
        calculated_lots = calculate_lots_from_budget(main_price, main_lot_size, target_budget)
        main_quantity = calculated_lots * main_lot_size

        logger.info(
            f"Trade #{trade.id if trade else 'N/A'} Target Budget Sizing: "
            f"budget={target_budget}, main_price={main_price}, lot_size={main_lot_size} -> "
            f"lots={calculated_lots}, main_qty={main_quantity}"
        )

        # 4. Set sizing and role for all entry legs
        for i in entry_indices:
            is_this_main = (i == main_idx)
            resolved_items[i]["is_main"] = is_this_main
            resolved_items[i]["lots"] = calculated_lots
            # "hedge qty will be always same as main qty"
            resolved_items[i]["quantity"] = main_quantity

    # For exit legs directly in message, try to match existing open legs
    for idx, item in enumerate(resolved_items):
        if item["action_type"] in ["EXIT", "CLOSE_LEG"] and item["quantity"] is None:
            inst = item["inst"]
            lot_sz = inst["lot_size"] if inst else 1
            matched_qty = None
            matched_lots = 1
            if trade and getattr(trade, "actions", None):
                for prior_act in trade.actions:
                    if prior_act.action_type in ["BUY", "SELL"] and prior_act.quantity:
                        if (prior_act.tradingsymbol and inst and prior_act.tradingsymbol == inst.get("tradingsymbol")) or \
                           (prior_act.strike and item["strike"] and abs(prior_act.strike - item["strike"]) < 0.01):
                            matched_qty = prior_act.quantity
                            matched_lots = prior_act.lots or 1
                            break
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
                        if pa.action_type in ["BUY", "SELL"]:
                            if (inst and pa.tradingsymbol == inst.get("tradingsymbol")) or \
                               (strike_val and pa.strike and abs(pa.strike - strike_val) < 0.01):
                                matching_entry = pa
                                break
                if matching_entry:
                    trans_type = "BUY" if matching_entry.transaction_type == "SELL" else "SELL"
                else:
                    # In credit spreads / short fut spreads, main is short (exit BUY), hedge is long (exit SELL)
                    if item["is_main"]:
                        trans_type = "BUY"
                    else:
                        trans_type = "SELL"

        # Resolve order type
        ord_type = "LIMIT" if (getattr(schema, "order_type", None) == "LIMIT" or getattr(schema, "is_limit", False)) else "MARKET"

        db_action = Action(
            trade_id=trade.id if trade else None,
            message_id=db_message_id,
            action_type=action_type,
            is_main=item["is_main"],
            instrument_name=getattr(schema, "instrument_name", None),
            price=getattr(schema, "price", None),
            stoploss=getattr(schema, "stoploss", None),
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

    return actions_to_add


def execute_trade_actions(session: Session, trade_id: int, auto_mode: bool = False) -> list:
    """
    Executes Zerodha orders for pending actionable legs of a given trade with deduplication checks.
    If auto_mode is True, checks AUTO_PLACE_ORDERS for entry actions and AUTO_PLACE_EXIT_ORDERS for exit actions.
    """
    auto_entry = os.getenv("AUTO_PLACE_ORDERS", "false").lower() in ("true", "1", "t", "yes")
    auto_exit = os.getenv("AUTO_PLACE_EXIT_ORDERS", "false").lower() in ("true", "1", "t", "yes")

    actions = session.query(Action).filter(
        Action.trade_id == trade_id,
        Action.order_status.in_(["PENDING", "FAILED"]),
        Action.action_type.in_(["BUY", "SELL", "EXIT", "CLOSE_LEG"])
    ).all()

    results = []
    for action in actions:
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
            continue

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
                continue
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
                continue

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

            results.append({
                "action_id": action.id,
                "tradingsymbol": action.tradingsymbol,
                "success": True,
                "order_id": action.zerodha_order_id or "DEDUPLICATED",
                "message": dedup_check["message"]
            })
            continue

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
        results.append({
            "action_id": action.id,
            "tradingsymbol": action.tradingsymbol,
            "success": res["success"],
            "order_id": res["order_id"],
            "message": res["message"]
        })

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

                res_msg = "\n".join(lines)
                await event.respond(res_msg, parse_mode='html')

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
                    if not has_new_structure or analysis.is_continuation or analysis.trade_status_update == "CLOSED" or any(a.action_type in ["EXIT", "CLOSE_LEG", "UPDATE_SL"] for a in analysis.actions) or not analysis.actions:
                        trade = candidate_trade
                        logger.info(f"Fallback mapped message ID {db_message.id} to open Trade ID {trade.id} ({candidate_trade.underlying})")

            # 2. Try matching by strike against open trade actions
            if not trade and analysis.actions:
                msg_strikes = [getattr(a, "strike", None) for a in analysis.actions if getattr(a, "strike", None)]
                if msg_strikes:
                    open_trades_with_actions = session.query(Trade).filter(Trade.status == "OPEN").all()
                    for ot in open_trades_with_actions:
                        ot_strikes = [a.strike for a in ot.actions if a.strike]
                        if any(s in ot_strikes for s in msg_strikes):
                            trade = ot
                            logger.info(f"Fallback mapped message ID {db_message.id} to open Trade ID {trade.id} via matching strike {msg_strikes}")
                            break

            # 3. If still not matched, and there is only 1 open trade, and message is an exit/closure
            if not trade and (analysis.trade_status_update == "CLOSED" or any(a.action_type in ["EXIT", "CLOSE_LEG"] for a in analysis.actions)):
                open_trades_list = session.query(Trade).filter(Trade.status == "OPEN").all()
                if len(open_trades_list) == 1:
                    trade = open_trades_list[0]
                    logger.info(f"Fallback mapped message ID {db_message.id} to single open Trade ID {trade.id}")

        # Check for explicit closing keywords in message text
        msg_text_lower = (db_message.text or "").lower()
        is_closing_phrase = any(w in msg_text_lower for w in [
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
            "status": trade.status
        })

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
                    "price": a.price
                }
                for a in db_actions
            ]
        })

    # 5. Automatic Square-Off Generation for exits / closed trades
    if trade and (analysis.trade_status_update == "CLOSED" or trade.status == "CLOSED" or any(a.action_type in ["EXIT", "CLOSE_LEG"] for a in analysis.actions)):
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
                
                # Mark actions as sent
                for a in db_actions:
                    a.telegram_sent = True
                session.commit()
                logger.info(f"Action notifications sent to Telegram for Trade ID {trade.id}")
                ctx.set_details({"has_pending_orders": has_pending_orders, "actions_notified": len(db_actions)})
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

        # 3. Process all unanalysed messages in chronological order
        unanalysed_messages = session.query(Message).filter(Message.analysed_by_ai == False).order_by(Message.id.asc()).all()
        if unanalysed_messages:
            logger.info(f"Found {len(unanalysed_messages)} unanalysed messages in DB. Processing...")
            for db_message in unanalysed_messages:
                await process_single_message(session, db_message, actions_entity)

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

    logger.info("Starting background sync and processing loop...")
    while True:
        try:
            await sync_and_process()
        except Exception as e:
            logger.error(f"Unhandled error in worker main loop: {e}")
        
        try:
            poll_interval = float(os.getenv("TELEGRAM_REFRESH_INTERVAL", "10"))
        except (ValueError, TypeError):
            poll_interval = 10.0
            
        await asyncio.sleep(poll_interval)

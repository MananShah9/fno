import os
import sys
import asyncio
import json
from datetime import datetime
from dotenv import load_dotenv

import db
from models import Message, Trade, Action
from gemini_client import analyze_message_with_ai, clean_symbol
from instruments_manager import resolve_nfo_instrument
from worker import get_open_trades_context, format_action_telegram_message_html, process_trade_actions_and_sizing

load_dotenv()

SAMPLE_MESSAGES = [
    # Message 1
    "DATE: 13/07/2026\n\nDEPLOY: JULY NIFTY 24000 PE SPREAD\n\nSELL: 28JUL2026 24000 PE @183 SL 220\n\n\nBUY: 28JUL2026 23600 PE @81",
    
    # Message 2
    "Update on position\nSL for entire position in when 24000 PE hits 220",
    
    # Message 3
    "SL hit Exit the full position",
    
    # Message 4
    "DATE: 14/07/2026\n\nDEPLOY: JULY VBL BEAR FUT SPREAD\n\nSELL: VBL FUT 467-468 (LIMIT ORDER) SL: 475\n\nTARGET: 453\n\nBUY: VBL 480 CE @6.7",
    
    # Message 5
    "Close the future position at or below 461.9 (put limit orders)\n\nAll call at 3.9\n\nWe are closing the trade Congratulations on your first profit\n\nAnd you got to experience a future spread and a shorting trade"
]

async def run_simulation():
    # Load settings
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key":
        print("[-] Error: GEMINI_API_KEY is not configured in .env file.")
        print("Please set your Gemini API key in the .env file before running the simulator.")
        return

    print("=" * 70)
    print("      📈 F&O TELEGRAM TRADER SIMULATION RUN")
    print("=" * 70)
    print("[*] Initializing Database...")
    db.init_db()
    
    session = db.SessionLocal()
    try:
        # Clear database to have a clean slate for simulation
        print("[*] Clearing database of any existing messages/trades/actions...")
        session.query(Action).delete()
        session.query(Message).delete()
        session.query(Trade).delete()
        session.commit()
        print("[+] Database cleared.")

        for i, raw_text in enumerate(SAMPLE_MESSAGES, 1):
            print("\n" + "-" * 70)
            print(f"📥 [Message {i}] Incoming Text:\n")
            print(raw_text)
            print("-" * 70)
            
            # 1. Store message in DB
            msg_obj = Message(
                text=raw_text,
                date=datetime.utcnow(),
                processed=False,
                analysed_by_ai=False
            )
            session.add(msg_obj)
            session.commit()
            session.refresh(msg_obj)
            
            # 2. Get Open Trades Context
            open_trades = get_open_trades_context(session)
            print(f"[*] Active Open Trades Context count sent to AI: {len(open_trades)}")
            if open_trades:
                print("  Current Open Trades in DB:")
                for ot in open_trades:
                    print(f"   - ID #{ot['id']} | Strategy: {ot['structure_type']} | Context: {ot['context_summary']}")

            # 3. Analyze with AI
            print("[*] Sending to Gemini for processing...")
            analysis = analyze_message_with_ai(raw_text, open_trades)
            
            if not analysis:
                print("[-] Failed to analyze message.")
                continue
                
            print(f"[+] AI parsed message. Valid Trade: {analysis.is_valid_trade_msg}")
            
            if not analysis.is_valid_trade_msg:
                print("[*] AI determined this is not a trade message or action update.")
                msg_obj.processed = True
                msg_obj.processed_at = datetime.utcnow()
                session.commit()
                continue
                
            # Process analysis
            msg_obj.ai_response = json.dumps(analysis.model_dump(), default=str)
            
            trade = None
            if analysis.is_continuation and analysis.related_open_trade_id:
                trade = session.query(Trade).filter(Trade.id == analysis.related_open_trade_id).first()
                if trade:
                    print(f"[+] Mapping to existing Trade ID #{trade.id}")
            
            if not trade:
                trade = Trade(
                    status="OPEN",
                    structure_type=analysis.structure_type,
                    underlying=analysis.underlying,
                    opened_at=datetime.utcnow()
                )
                session.add(trade)
                session.commit()
                session.refresh(trade)
                print(f"[+] Created new Trade ID #{trade.id}")

            trade.context_summary = analysis.context_summary or trade.context_summary
            if analysis.trade_status_update == "CLOSED":
                trade.status = "CLOSED"
                trade.closed_at = datetime.utcnow()
                print(f"[+] Trade ID #{trade.id} is now CLOSED.")
            session.commit()
            
            # Create Actions and resolve NFO instruments with target budget lot sizing
            db_actions = process_trade_actions_and_sizing(
                trade=trade,
                db_message_id=msg_obj.id,
                parsed_actions=analysis.actions
            )
            for db_action in db_actions:
                session.add(db_action)
            session.commit()

            # Automatic Square-Off Generation for closed trades / exits
            if trade and (analysis.trade_status_update == "CLOSED" or trade.status == "CLOSED" or any(a.action_type in ["EXIT", "CLOSE_LEG"] for a in analysis.actions)):
                from worker import ensure_square_off_actions
                sq_actions = ensure_square_off_actions(session, trade, msg_obj, analysis.actions)
                for sq_a in sq_actions:
                    if sq_a not in db_actions:
                        db_actions.append(sq_a)

            # Test Order Execution & Deduplication
            from worker import execute_trade_actions
            exec_res = execute_trade_actions(session, trade.id)
            if exec_res:
                print(f"[*] Zerodha Execution Results: {json.dumps(exec_res, indent=2)}")
            
            # Refresh DB state for formatting
            session.refresh(trade)
            for action in db_actions:
                session.refresh(action)
                
            # Generate and print the HTML message
            html_msg = format_action_telegram_message_html(trade, db_actions)
            print("\n🔔 [TELEGRAM ACTION CHANNEL NOTIFICATION SIMULATION] 🔔")
            print("=" * 60)
            print(html_msg)
            print("=" * 60)
            
            # Mark processed
            msg_obj.processed = True
            msg_obj.analysed_by_ai = True
            msg_obj.processed_at = datetime.utcnow()
            session.commit()
            
            # Small delay to keep things structured
            await asyncio.sleep(1)

        print("\n" + "=" * 70)
        print("[+] Simulation complete. Database contains the final states.")
        print("=" * 70)
        
    except Exception as e:
        print(f"[-] Simulation error: {e}", file=sys.stderr)
        session.rollback()
    finally:
        session.close()

if __name__ == "__main__":
    asyncio.run(run_simulation())

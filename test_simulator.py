import os
import sys
import asyncio
import json
from datetime import datetime
from dotenv import load_dotenv

import db
from models import Message, Trade, Action, MessageStageTrace
from worker import process_single_message
import stage_tracker
from cli import display_message_stage_timeline

load_dotenv()

SAMPLE_MESSAGES = [
    # Message 1 - New Trade Entry
    "DATE: 13/07/2026\n\nDEPLOY: JULY NIFTY 24000 PE SPREAD\n\nSELL: 28JUL2026 24000 PE @183 SL 220\n\n\nBUY: 28JUL2026 23600 PE @81",
    
    # Message 2 - Poke message (should be skipped by POKE_FILTER)
    "trade incoming...",

    # Message 3 - Update SL on position
    "Update on position\nSL for entire position in when 24000 PE hits 220",
    
    # Message 4 - Exit position (should trigger Square-off generation)
    "SL hit Exit the full position",
    
    # Message 5 - New Future Spread Entry
    "DATE: 14/07/2026\n\nDEPLOY: JULY VBL BEAR FUT SPREAD\n\nSELL: VBL FUT 467-468 (LIMIT ORDER) SL: 475\n\nTARGET: 453\n\nBUY: VBL 480 CE @6.7",
    
    # Message 6 - Closing Future Spread
    "Close the future position at or below 461.9 (put limit orders)\n\nAll call at 3.9\n\nWe are closing the trade Congratulations on your first profit"
]

async def run_simulation():
    # Load settings
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key":
        print("[-] Error: GEMINI_API_KEY is not configured in .env file.")
        print("Please set your Gemini API key in the .env file before running the simulator.")
        return

    print("=" * 75)
    print("      📈 F&O TELEGRAM TRADER SIMULATION & STAGE TRACING TEST")
    print("=" * 75)
    print("[*] Initializing Database & Running Migrations...")
    db.init_db()
    
    session = db.SessionLocal()
    try:
        # Clear database to have a clean slate for simulation
        print("[*] Clearing database of any existing messages/trades/actions/traces...")
        session.query(MessageStageTrace).delete()
        session.query(Action).delete()
        session.query(Message).delete()
        session.query(Trade).delete()
        session.commit()
        print("[+] Database cleared.")

        for i, raw_text in enumerate(SAMPLE_MESSAGES, 1):
            print("\n" + "=" * 75)
            print(f"📥 [Message {i}] Incoming Text:\n{raw_text}")
            print("=" * 75)
            
            # 1. Store message in DB
            msg_obj = Message(
                telegram_message_id=1000 + i,
                channel_id="simulation_channel",
                text=raw_text,
                date=datetime.utcnow(),
                processed=False,
                analysed_by_ai=False,
                revision=0
            )
            session.add(msg_obj)
            session.commit()
            session.refresh(msg_obj)

            # Record ingestion stage
            stage_tracker.record_stage(
                stage="SYNC_RECEIVED",
                status="SUCCESS",
                message_id=msg_obj.id,
                telegram_message_id=msg_obj.telegram_message_id,
                revision=0,
                details={"text_snippet": raw_text[:80]},
                session=session
            )

            # 2. Run full pipeline
            success = await process_single_message(session, msg_obj, actions_entity=None)
            print(f"[+] Pipeline completed for Message #{msg_obj.id}. Success: {success}")

            # 3. Print Stage Diagnostics Timeline
            display_message_stage_timeline(msg_obj.id, is_tg_id=False)
            
            # Small delay
            await asyncio.sleep(0.5)

        print("\n" + "=" * 75)
        print("[+] All simulation messages processed successfully with full stage tracing!")
        print("=" * 75)
        
    except Exception as e:
        print(f"[-] Simulation error: {e}", file=sys.stderr)
        session.rollback()
    finally:
        session.close()

if __name__ == "__main__":
    asyncio.run(run_simulation())

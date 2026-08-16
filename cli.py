import os
import sys
import asyncio
from tabulate import tabulate
from datetime import datetime
from sqlalchemy.orm import Session
from dotenv import load_dotenv

import db
import config
from models import Message, Trade, Action, MessageStageTrace
import telegram_client
import stage_tracker

load_dotenv()

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def truncate_text(text: str, max_len: int = 50) -> str:
    if not text:
        return ""
    text_clean = text.replace("\n", " ")
    return text_clean[:max_len] + "..." if len(text_clean) > max_len else text_clean

def print_header(title: str):
    print("\n" + "="*70)
    print(f" {title.center(68)} ")
    print("="*70)

def format_status_badge(status: str) -> str:
    status_clean = str(status or "").upper()
    if status_clean in ["SUCCESS", "OK", "COMPLETE", "COMPLETED"]:
        return "✅ SUCCESS"
    elif status_clean in ["WARNING", "WARN"]:
        return "⚠️ WARNING"
    elif status_clean in ["ERROR", "FAILED"]:
        return "❌ ERROR"
    elif status_clean in ["SKIPPED", "SKIP"]:
        return "⏭️ SKIPPED"
    elif status_clean in ["IN_PROGRESS", "RUNNING", "PENDING"]:
        return "⏳ IN_PROGRESS"
    return f"ℹ️ {status_clean}"

def display_message_stage_timeline(message_id: int, is_tg_id: bool = False, interactive: bool = True):
    """Renders a detailed diagnostic breakdown for a message's lifecycle across all stages."""
    session = db.SessionLocal()
    try:
        # Look up message
        if is_tg_id:
            msg = session.query(Message).filter(Message.telegram_message_id == message_id).first()
        else:
            msg = session.query(Message).filter(Message.id == message_id).first()

        if not msg:
            print(f"[-] No message found with {'Telegram ID' if is_tg_id else 'DB ID'} #{message_id}")
            return

        traces = stage_tracker.get_message_history(msg.id, is_tg_id=False, session=session)

        if interactive:
            clear_screen()
        print_header(f"DIAGNOSTIC STAGE TIMELINE FOR MESSAGE #{msg.id}")
        print(f"DB Message ID:       #{msg.id}")
        print(f"Telegram Msg ID:     #{msg.telegram_message_id or 'N/A'}")
        print(f"Current Revision:    Rev {msg.revision or 0}")
        print(f"Received At:         {msg.date}")
        print(f"Processed:           {'✅ Yes' if msg.processed else '❌ No'}")
        print(f"Analysed by AI:      {'✅ Yes' if msg.analysed_by_ai else '❌ No'}")
        print(f"Last Known Stage:    {msg.last_stage or 'N/A'}")
        print(f"Overall Status:      {format_status_badge(msg.last_status)}")
        if msg.last_error:
            print(f"Last Error:          ❌ {msg.last_error}")
        print(f"\nMessage Text:\n\"\"\"\n{msg.text}\n\"\"\"")

        if not traces:
            print("\n⚠️ No stage traces recorded for this message yet.")
            return

        print("\n--- Chronological Execution Stages ---")
        table_rows = []
        for idx, t in enumerate(traces, 1):
            dur_str = f"{t['duration_ms']:.1f} ms" if t['duration_ms'] is not None else "instant"
            table_rows.append([
                idx,
                f"Rev {t['revision']}",
                t["stage"],
                format_status_badge(t["status"]),
                t["timestamp"],
                dur_str,
                t["location"] or "N/A"
            ])

        print(tabulate(
            table_rows,
            headers=["#", "Rev", "Stage", "Status", "Timestamp (UTC)", "Duration", "Code Location"],
            tablefmt="grid"
        ))

        # Show detailed stage payload metadata and errors
        print("\n--- Stage Detailed Diagnostics & Metadata ---")
        for idx, t in enumerate(traces, 1):
            has_details = bool(t["details"])
            has_error = bool(t["error_message"])
            has_stack = bool(t["stack_trace"])

            if has_details or has_error or has_stack or t["status"] in ["ERROR", "WARNING", "SKIPPED"]:
                status_icon = "❌" if t["status"] == "ERROR" else ("⚠️" if t["status"] == "WARNING" else "•")
                print(f"\n{status_icon} [Stage {idx}: {t['stage']}] (Status: {t['status']}, Location: {t['location'] or 'N/A'})")
                if t["error_message"]:
                    print(f"  🛑 Error / Warning: {t['error_message']}")
                if t["details"]:
                    print(f"  📝 Details:\n{t['details']}")
                if t["stack_trace"]:
                    print(f"  📜 Stack Trace:\n{t['stack_trace']}")

    finally:
        session.close()

def view_diagnostics_menu():
    """Interactive Diagnostics and Stage Tracing menu."""
    while True:
        clear_screen()
        print_header("MESSAGE DIAGNOSTICS & EXECUTION TRACES")
        print(" [1] 📋 View Recent Messages & Diagnostic Health Summary")
        print(" [2] ⚠️  View Stuck, Errored & Incomplete Messages")
        print(" [3] 🔎 Inspect Full Stage Timeline for a Message (by DB ID)")
        print(" [4] 🔎 Inspect Full Stage Timeline for a Message (by Telegram Msg ID)")
        print(" [5] 💾 Export Trace Diagnostics to JSON / Markdown File")
        print(" [6] 🔙 Return to Main Menu")
        print("="*70)

        choice = input("Enter choice (1-6): ").strip()

        if choice == '1':
            view_recent_diagnostics_summary(interactive=True)
        elif choice == '2':
            view_stuck_messages(interactive=True)
        elif choice == '3':
            msg_id_input = input("\nEnter DB Message ID: ").strip()
            if msg_id_input.isdigit():
                display_message_stage_timeline(int(msg_id_input), is_tg_id=False, interactive=True)
                input("\nPress Enter to return to Diagnostics menu...")
        elif choice == '4':
            tg_id_input = input("\nEnter Telegram Message ID: ").strip()
            if tg_id_input.isdigit():
                display_message_stage_timeline(int(tg_id_input), is_tg_id=True, interactive=True)
                input("\nPress Enter to return to Diagnostics menu...")
        elif choice == '5':
            export_diagnostics_to_file(interactive=True)
        elif choice == '6':
            break
        else:
            print("Invalid choice. Press Enter to retry...")
            input()

def view_recent_diagnostics_summary(interactive: bool = False):
    if interactive:
        clear_screen()
    print_header("RECENT MESSAGES & DIAGNOSTIC HEALTH SUMMARY")
    recent = stage_tracker.get_recent_messages_diagnostics(limit=30)
    if not recent:
        print("\nNo messages found in the database.")
    else:
        table_rows = []
        for r in recent:
            table_rows.append([
                r["id"],
                r["telegram_message_id"],
                f"Rev {r['revision']}",
                r["date"],
                r["last_stage"],
                format_status_badge(r["overall_status"]),
                f"{r['stages_count']} stages",
                r["text_snippet"]
            ])
        print(tabulate(
            table_rows,
            headers=["ID", "TG ID", "Rev", "Date/Time", "Last Stage", "Health", "Traces", "Snippet"],
            tablefmt="grid"
        ))

        if interactive:
            inspect_choice = input("\nEnter Message ID to inspect full timeline (or press Enter to go back): ").strip()
            if inspect_choice.isdigit():
                display_message_stage_timeline(int(inspect_choice), is_tg_id=False, interactive=True)
            input("\nPress Enter to return...")

def view_stuck_messages(interactive: bool = False):
    if interactive:
        clear_screen()
    print_header("STUCK, ERRORED & INCOMPLETE MESSAGES")
    stuck = stage_tracker.get_stuck_or_failed_messages(limit=40)
    if not stuck:
        print("\n🎉 Great news! No stuck or failed messages detected in database.")
    else:
        table_rows = []
        for s in stuck:
            err_preview = truncate_text(s["last_error"] or "Stuck in pipeline", 30)
            table_rows.append([
                s["id"],
                s["telegram_message_id"] or "N/A",
                s["date"],
                s["last_stage"],
                format_status_badge(s["last_status"]),
                err_preview,
                s["text_snippet"]
            ])
        print(tabulate(
            table_rows,
            headers=["ID", "TG ID", "Date/Time", "Last Stage", "Status", "Error / Reason", "Text Snippet"],
            tablefmt="grid"
        ))

        if interactive:
            inspect_choice = input("\nEnter Message ID to inspect full timeline (or press Enter to go back): ").strip()
            if inspect_choice.isdigit():
                display_message_stage_timeline(int(inspect_choice), is_tg_id=False, interactive=True)
            input("\nPress Enter to return...")

def export_diagnostics_to_file(interactive: bool = False):
    target_id = ""
    if interactive:
        target_id = input("\nEnter DB Message ID to export (or leave empty to export all recent traces): ").strip()
    session = db.SessionLocal()
    try:
        if target_id.isdigit():
            traces = stage_tracker.get_message_history(int(target_id), session=session)
            msg = session.query(Message).filter(Message.id == int(target_id)).first()
            export_data = {
                "message": {
                    "id": msg.id if msg else int(target_id),
                    "telegram_message_id": msg.telegram_message_id if msg else None,
                    "text": msg.text if msg else None,
                    "processed": msg.processed if msg else None,
                    "last_stage": msg.last_stage if msg else None,
                    "last_status": msg.last_status if msg else None,
                    "last_error": msg.last_error if msg else None
                },
                "traces": traces
            }
            filename = f"trace_message_{target_id}.json"
        else:
            recent_msgs = stage_tracker.get_recent_messages_diagnostics(limit=50, session=session)
            stuck_msgs = stage_tracker.get_stuck_or_failed_messages(limit=50, session=session)
            export_data = {
                "exported_at": datetime.utcnow().isoformat(),
                "recent_messages": recent_msgs,
                "stuck_messages": stuck_msgs
            }
            filename = "diagnostics_export.json"

        with open(filename, "w", encoding="utf-8") as f:
            import json
            json.dump(export_data, f, indent=2, default=str)

        print(f"\n[+] Successfully exported diagnostics to '{filename}'!")
    except Exception as e:
        print(f"\n[-] Error exporting diagnostics: {e}")
    finally:
        session.close()
    if interactive:
        input("\nPress Enter to return...")

async def run_setup():
    print_header("INTERACTIVE SETUP & TELEGRAM LOGIN")
    
    # 1. Initialize Database
    print("[*] Initializing Database...")
    db.init_db()
    print("[+] Database initialized successfully.")
    
    # 2. Telegram Auth
    print("\n[*] Checking Telegram authentication...")
    try:
        success = await telegram_client.interactive_login()
        if success:
            print("[+] Telegram account is connected and remembered!")
        else:
            print("[-] Telegram login failed. Check your API credentials in .env.")
    except Exception as e:
        print(f"[-] Telegram login error: {e}")
    
    # 3. Channel configuration
    cfg = config.load_config()
    print("\n[*] Current Telegram Channel Configurations:")
    print(f"  Source Channel:  {cfg['TELEGRAM_SOURCE_CHANNEL'] or 'Not Configured'}")
    print(f"  Mirror Channel:  {cfg['TELEGRAM_MIRROR_CHANNEL'] or 'Not Configured'}")
    print(f"  Actions Channel: {cfg['TELEGRAM_ACTIONS_CHANNEL'] or 'Not Configured'}")
    
    configure_now = input("\nDo you want to configure these Telegram channels now? (y/n): ").strip().lower()
    if configure_now == 'y':
        src = input("Enter Source Channel ID or Username (e.g. @my_channel or -1001234567): ").strip()
        if src:
            config.update_env_variable("TELEGRAM_SOURCE_CHANNEL", src)
            
        mir = input("Enter Mirror Channel ID or Username (optional, e.g. @my_mirror): ").strip()
        if mir:
            config.update_env_variable("TELEGRAM_MIRROR_CHANNEL", mir)
            
        act = input("Enter Actions Channel ID or Username (optional, e.g. @my_actions): ").strip()
        if act:
            config.update_env_variable("TELEGRAM_ACTIONS_CHANNEL", act)
            
        print("[+] Channels configured!")
        
    # 4. Gemini Configuration
    if not cfg['GEMINI_API_KEY'] or cfg['GEMINI_API_KEY'] == 'your_gemini_api_key':
        gem_key = input("\nEnter Gemini API Key (optional): ").strip()
        if gem_key:
            config.update_env_variable("GEMINI_API_KEY", gem_key)
            
    input("\nSetup complete. Press Enter to return to main menu...")

def view_messages():
    print_header("RECENT SYNCED MESSAGES")
    session = db.SessionLocal()
    try:
        messages = session.query(Message).order_by(Message.id.desc()).limit(20).all()
        if not messages:
            print("\nNo synced messages found in the database. Run the worker to sync messages!")
        else:
            table_data = []
            for msg in messages:
                table_data.append([
                    msg.id,
                    msg.telegram_message_id or "N/A",
                    msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "N/A",
                    truncate_text(msg.text, 40),
                    "✅ Yes" if msg.processed else "❌ No"
                ])
            print(tabulate(table_data, headers=["ID", "TG Msg ID", "Date/Time", "Raw Text (Truncated)", "Processed?"], tablefmt="grid"))
    finally:
        session.close()
    input("\nPress Enter to return to main menu...")

def view_trades(status_filter: str):
    print_header(f"{status_filter} TRADES")
    session = db.SessionLocal()
    try:
        trades = session.query(Trade).filter(Trade.status == status_filter).order_by(Trade.id.desc()).all()
        if not trades:
            print(f"\nNo {status_filter.lower()} trades found.")
        else:
            table_data = []
            for trade in trades:
                table_data.append([
                    trade.id,
                    trade.underlying or "N/A",
                    trade.structure_type or "N/A",
                    trade.opened_at.strftime("%Y-%m-%d %H:%M") if trade.opened_at else "N/A",
                    truncate_text(trade.context_summary, 45)
                ])
            print(tabulate(table_data, headers=["ID", "Ticker", "Strategy Type", "Opened At", "Context Summary"], tablefmt="grid"))
            
            # Allow user to inspect details of a specific trade
            view_id = input(f"\nEnter Trade ID to view full details (or press Enter to go back): ").strip()
            if view_id.isdigit():
                view_trade_details(int(view_id), session)
                return
    finally:
        session.close()
    input("\nPress Enter to return to main menu...")

def view_trade_details(trade_id: int, session: Session):
    trade = session.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        print(f"[-] Trade with ID #{trade_id} not found.")
        input("\nPress Enter to go back...")
        return
        
    clear_screen()
    print_header(f"TRADE DETAILS FOR ID #{trade.id}")
    print(f"STATUS:          {trade.status}")
    print(f"Underlying:      {trade.underlying or 'N/A'}")
    print(f"Strategy Type:   {trade.structure_type or 'N/A'}")
    print(f"Opened At:       {trade.opened_at}")
    if trade.closed_at:
        print(f"Closed At:       {trade.closed_at}")
    print(f"Context Summary: {trade.context_summary or 'N/A'}")
    
    print("\n--- Associated Execution Orders/Actions ---")
    actions = session.query(Action).filter(Action.trade_id == trade.id).order_by(Action.id.asc()).all()
    if not actions:
        print("No actions linked to this trade.")
    else:
        actions_data = []
        for act in actions:
            role = "Main" if getattr(act, "is_main", True) else "Hedge"
            qty_str = f"{act.quantity} ({act.lots or 1}L)" if act.quantity else "N/A"
            actions_data.append([
                act.id,
                act.action_type,
                role,
                act.tradingsymbol or act.instrument_name or "N/A",
                qty_str,
                act.price or "N/A",
                act.stoploss or "N/A",
                act.target or "N/A",
                act.order_status or "PENDING",
                "✅ Yes" if act.telegram_sent else "❌ No"
            ])
        print(tabulate(actions_data, headers=["ID", "Type", "Role", "Instrument", "Qty (Lots)", "Price", "SL", "Target", "Status", "Tg Sent?"], tablefmt="grid"))
        
    input("\nPress Enter to go back...")

def view_all_actions():
    print_header("ALL SYSTEM ACTIONS / ORDER LOG")
    session = db.SessionLocal()
    try:
        actions = session.query(Action).order_by(Action.id.desc()).limit(40).all()
        if not actions:
            print("\nNo action orders found in database.")
        else:
            table_data = []
            for act in actions:
                role = "Main" if getattr(act, "is_main", True) else "Hedge"
                qty_str = f"{act.quantity} ({act.lots or 1}L)" if act.quantity else "N/A"
                table_data.append([
                    act.id,
                    f"Trade #{act.trade_id}" if act.trade_id else "N/A",
                    act.action_type,
                    role,
                    act.tradingsymbol or act.instrument_name or "N/A",
                    qty_str,
                    act.price or "N/A",
                    act.stoploss or "N/A",
                    act.target or "N/A",
                    act.order_status or "PENDING",
                    "✅ Yes" if act.telegram_sent else "❌ No"
                ])
            print(tabulate(table_data, headers=["ID", "Trade Ref", "Type", "Role", "Instrument", "Qty (Lots)", "Price", "SL", "Target", "Status", "Sent to Tg?"], tablefmt="grid"))
    finally:
        session.close()
    input("\nPress Enter to return to main menu...")

def manage_config_menu():
    while True:
        clear_screen()
        print_header("MANAGE SYSTEM CONFIGURATION (.env)")
        cfg = config.load_config()
        
        print(f"1. TELEGRAM_API_ID:          {cfg['TELEGRAM_API_ID'] or 'Not Configured'}")
        print(f"2. TELEGRAM_API_HASH:        {cfg['TELEGRAM_API_HASH'] or 'Not Configured'}")
        print(f"3. TELEGRAM_PHONE:           {cfg['TELEGRAM_PHONE'] or 'Not Configured'}")
        print(f"4. TELEGRAM_SOURCE_CHAN:     {cfg['TELEGRAM_SOURCE_CHANNEL'] or 'Not Configured'}")
        print(f"5. TELEGRAM_MIRROR_CHAN:     {cfg['TELEGRAM_MIRROR_CHANNEL'] or 'Not Configured'}")
        print(f"6. TELEGRAM_ACTIONS_CHAN:    {cfg['TELEGRAM_ACTIONS_CHANNEL'] or 'Not Configured'}")
        print(f"7. TELEGRAM_REFRESH_INT:     {cfg['TELEGRAM_REFRESH_INTERVAL']} seconds")
        print(f"8. TELEGRAM_TIME_FILTER:     {cfg.get('TELEGRAM_TIME_FILTER_ENABLED', False)}")
        print(f"9. TELEGRAM_START_TIME:      {cfg.get('TELEGRAM_START_TIME', '08:30')}")
        print(f"10. TELEGRAM_END_TIME:       {cfg.get('TELEGRAM_END_TIME', '16:30')}")
        print(f"11. TELEGRAM_WEEKDAYS_ONLY:  {cfg.get('TELEGRAM_WEEKDAYS_ONLY', True)}")
        print(f"12. TELEGRAM_TIMEZONE:       {cfg.get('TELEGRAM_TIMEZONE', 'Asia/Kolkata')}")
        print(f"13. GEMINI_API_KEY:          {'*' * 10 if cfg['GEMINI_API_KEY'] else 'Not Configured'}")
        print(f"14. GEMINI_MODEL:            {cfg['GEMINI_MODEL']}")
        print(f"15. AUTO_PLACE_ORDERS:        {cfg['AUTO_PLACE_ORDERS']}")
        print(f"16. AUTO_PLACE_EXIT_ORDERS:   {cfg['AUTO_PLACE_EXIT_ORDERS']}")
        print(f"17. TARGET_INVESTMENT_BUDGET: Rs. {cfg.get('TARGET_INVESTMENT_BUDGET', '100000')}")
        print(f"18. MAX_STOCK_LOTS:          {cfg.get('MAX_STOCK_LOTS', '2')} lots")
        print(f"19. MAX_INDEX_LOTS:          {cfg.get('MAX_INDEX_LOTS', '4')} lots")
        print(f"20. EST_INDEX_SPREAD_MARGIN: Rs. {cfg.get('ESTIMATED_INDEX_SPREAD_MARGIN', '40000')}")
        print(f"21. EST_STOCK_SPREAD_MARGIN: Rs. {cfg.get('ESTIMATED_STOCK_SPREAD_MARGIN', '120000')}")
        print(f"22. EST_FUTURES_MARGIN:      Rs. {cfg.get('ESTIMATED_INDEX_FUTURES_MARGIN', '130000')}")
        print(f"23. LOG_LEVEL:               {cfg.get('LOG_LEVEL', 'INFO')}")
        print(f"24. 🔙 Return to Main Menu")
        
        choice = input("\nSelect setting to edit (1-24): ").strip()
        if choice == '24':
            break
            
        env_map = {
            '1': 'TELEGRAM_API_ID',
            '2': 'TELEGRAM_API_HASH',
            '3': 'TELEGRAM_PHONE',
            '4': 'TELEGRAM_SOURCE_CHANNEL',
            '5': 'TELEGRAM_MIRROR_CHANNEL',
            '6': 'TELEGRAM_ACTIONS_CHANNEL',
            '7': 'TELEGRAM_REFRESH_INTERVAL',
            '8': 'TELEGRAM_TIME_FILTER_ENABLED',
            '9': 'TELEGRAM_START_TIME',
            '10': 'TELEGRAM_END_TIME',
            '11': 'TELEGRAM_WEEKDAYS_ONLY',
            '12': 'TELEGRAM_TIMEZONE',
            '13': 'GEMINI_API_KEY',
            '14': 'GEMINI_MODEL',
            '15': 'AUTO_PLACE_ORDERS',
            '16': 'AUTO_PLACE_EXIT_ORDERS',
            '17': 'TARGET_INVESTMENT_BUDGET',
            '18': 'MAX_STOCK_LOTS',
            '19': 'MAX_INDEX_LOTS',
            '20': 'ESTIMATED_INDEX_SPREAD_MARGIN',
            '21': 'ESTIMATED_STOCK_SPREAD_MARGIN',
            '22': 'ESTIMATED_INDEX_FUTURES_MARGIN',
            '23': 'LOG_LEVEL'
        }
        
        if choice in env_map:
            var_name = env_map[choice]
            new_val = input(f"Enter new value for {var_name}: ").strip()
            if new_val:
                config.update_env_variable(var_name, new_val)
                input("\nSetting updated! Press Enter to refresh...")
            else:
                print("Skipped update.")
                input("\nPress Enter to go back...")

async def cli_main():
    """Main CLI Menu loop."""
    db.init_db()
    
    while True:
        clear_screen()
        print("\n" + "="*60)
        print("          📈 F&O TELEGRAM TRADER SYSTEM          ")
        print("="*60)
        print(" [1] 🔐 Setup & Telegram Authorization")
        print(" [2] 📬 View Synced Messages")
        print(" [3] 🟢 View Open Trades")
        print(" [4] 🔴 View Closed Trades")
        print(" [5] 🛡️ View Action Orders Log")
        print(" [6] 🔍 Message Diagnostics & Stage Traces")
        print(" [7] ⚙️  Manage Configuration (.env)")
        print(" [8] 🚪 Exit CLI")
        print("="*60)
        
        choice = input("Enter choice (1-8): ").strip()
        
        if choice == '1':
            await run_setup()
        elif choice == '2':
            view_messages()
        elif choice == '3':
            view_trades("OPEN")
        elif choice == '4':
            view_trades("CLOSED")
        elif choice == '5':
            view_all_actions()
        elif choice == '6':
            view_diagnostics_menu()
        elif choice == '7':
            manage_config_menu()
        elif choice == '8':
            print("\nExiting CLI. Goodbye!")
            break
        else:
            print("Invalid choice. Press Enter to retry...")
            input()

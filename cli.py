import os
import sys
import asyncio
from tabulate import tabulate
from datetime import datetime
from sqlalchemy.orm import Session
from dotenv import load_dotenv

import db
import config
from models import Message, Trade, Action
import telegram_client

load_dotenv()

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def truncate_text(text: str, max_len: int = 50) -> str:
    if not text:
        return ""
    text_clean = text.replace("\n", " ")
    return text_clean[:max_len] + "..." if len(text_clean) > max_len else text_clean

def print_header(title: str):
    print("\n" + "="*60)
    print(f" {title.center(58)} ")
    print("="*60)

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
            actions_data.append([
                act.id,
                act.action_type,
                act.instrument_name or "N/A",
                act.price or "N/A",
                act.stoploss or "N/A",
                act.target or "N/A",
                "Yes" if act.is_limit else "No",
                truncate_text(act.details, 30),
                "✅ Yes" if act.telegram_sent else "❌ No"
            ])
        print(tabulate(actions_data, headers=["ID", "Type", "Instrument", "Price", "SL", "Target", "Limit?", "Note", "Tg Sent?"], tablefmt="grid"))
        
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
                table_data.append([
                    act.id,
                    f"Trade #{act.trade_id}" if act.trade_id else "N/A",
                    act.action_type,
                    act.instrument_name or "N/A",
                    act.price or "N/A",
                    act.stoploss or "N/A",
                    act.target or "N/A",
                    "✅ Yes" if act.telegram_sent else "❌ No"
                ])
            print(tabulate(table_data, headers=["ID", "Trade Ref", "Type", "Instrument Search (Zerodha)", "Price", "SL", "Target", "Sent to Tg?"], tablefmt="grid"))
    finally:
        session.close()
    input("\nPress Enter to return to main menu...")

def manage_config_menu():
    while True:
        clear_screen()
        print_header("MANAGE SYSTEM CONFIGURATION (.env)")
        cfg = config.load_config()
        
        print(f"1. TELEGRAM_API_ID:       {cfg['TELEGRAM_API_ID'] or 'Not Configured'}")
        print(f"2. TELEGRAM_API_HASH:     {cfg['TELEGRAM_API_HASH'] or 'Not Configured'}")
        print(f"3. TELEGRAM_PHONE:        {cfg['TELEGRAM_PHONE'] or 'Not Configured'}")
        print(f"4. TELEGRAM_SOURCE_CHAN:  {cfg['TELEGRAM_SOURCE_CHANNEL'] or 'Not Configured'}")
        print(f"5. TELEGRAM_MIRROR_CHAN:  {cfg['TELEGRAM_MIRROR_CHANNEL'] or 'Not Configured'}")
        print(f"6. TELEGRAM_ACTIONS_CHAN: {cfg['TELEGRAM_ACTIONS_CHANNEL'] or 'Not Configured'}")
        print(f"7. GEMINI_API_KEY:        {'*' * 10 if cfg['GEMINI_API_KEY'] else 'Not Configured'}")
        print(f"8. GEMINI_MODEL:          {cfg['GEMINI_MODEL']}")
        print(f"9. 🔙 Return to Main Menu")
        
        choice = input("\nSelect setting to edit (1-9): ").strip()
        if choice == '9':
            break
            
        env_map = {
            '1': 'TELEGRAM_API_ID',
            '2': 'TELEGRAM_API_HASH',
            '3': 'TELEGRAM_PHONE',
            '4': 'TELEGRAM_SOURCE_CHANNEL',
            '5': 'TELEGRAM_MIRROR_CHANNEL',
            '6': 'TELEGRAM_ACTIONS_CHANNEL',
            '7': 'GEMINI_API_KEY',
            '8': 'GEMINI_MODEL'
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
        print(" [6] ⚙️  Manage Configuration (.env)")
        print(" [7] 🚪 Exit CLI")
        print("="*60)
        
        choice = input("Enter choice (1-7): ").strip()
        
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
            manage_config_menu()
        elif choice == '7':
            print("\nExiting CLI. Goodbye!")
            break
        else:
            print("Invalid choice. Press Enter to retry...")
            input()

import os
import sys
import asyncio
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")

# Ensure sessions directory exists
os.makedirs("sessions", exist_ok=True)
SESSION_PATH = os.path.join("sessions", "telegram_user")

# Initialize client if API credentials are present
client = None
if API_ID and API_HASH:
    try:
        client = TelegramClient(SESSION_PATH, int(API_ID), API_HASH)
    except ValueError:
        print("Error: TELEGRAM_API_ID must be an integer", file=sys.stderr)
else:
    print("Warning: TELEGRAM_API_ID or TELEGRAM_API_HASH is missing from .env", file=sys.stderr)

async def check_login():
    """Checks if client is authorized, returns boolean."""
    if not client:
        return False
    if not client.is_connected():
        await client.connect()
    return await client.is_user_authorized()

async def interactive_login():
    """Runs interactive login in terminal."""
    if not client:
        print("Cannot login: Telegram Client not initialized. Check .env variables.")
        return False
    
    if not client.is_connected():
        await client.connect()
        
    authorized = await client.is_user_authorized()
    if authorized:
        print("Telegram Account is already authorized!")
        me = await client.get_me()
        print(f"Logged in as: {me.first_name} (@{me.username or 'NoUsername'})")
        return True
        
    # Standard Telethon interactive login
    print("Starting interactive Telegram authorization...")
    phone_to_use = PHONE or input("Enter Telegram phone number (with country code, e.g. +919876543210): ")
    
    try:
        await client.start(phone=phone_to_use)
        me = await client.get_me()
        print(f"Successfully logged in as: {me.first_name} (@{me.username or 'NoUsername'})")
        return True
    except Exception as e:
        print(f"Error during authorization: {e}", file=sys.stderr)
        return False

async def get_channel_entity(channel_identifier):
    """
    Resolves a channel identifier (int ID, username, link) to a Telethon entity.
    """
    if not client:
        return None
    if not channel_identifier:
        return None
        
    if not client.is_connected():
        await client.connect()
        
    # Clean identifier
    channel_str = str(channel_identifier).strip()
    
    # Try parsing as integer ID (e.g. -100123456 or 123456)
    try:
        if channel_str.replace("-", "").isdigit():
            # Telethon expects specific formats for IDs.
            # Usually, -100 prefix needs to be parsed as int or entity
            val = int(channel_str)
            return await client.get_entity(val)
    except Exception:
        pass
        
    # Try parsing as username or link
    try:
        return await client.get_entity(channel_str)
    except Exception as e:
        print(f"Error resolving entity '{channel_identifier}': {e}", file=sys.stderr)
        return None

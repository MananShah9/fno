import os
import sys
import asyncio
import logging
from typing import Optional
from telethon import TelegramClient
from telethon.sessions import StringSession, SQLiteSession
from dotenv import load_dotenv
import config

load_dotenv()

logger = logging.getLogger("telegram_client")

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")


def get_session_string() -> str:
    """
    Retrieves the Telegram StringSession from environment variables.
    Checks TELEGRAM_STRING_SESSION or TELEGRAM_SESSION_STRING.
    If not found in environment, checks for a legacy SQLite session file ('sessions/telegram_user.session'),
    extracts the auth key and DC into StringSession format, and auto-saves it to .env.
    """
    # 1. Check environment variables
    session_str = os.getenv("TELEGRAM_STRING_SESSION") or os.getenv("TELEGRAM_SESSION_STRING")
    if session_str and session_str.strip():
        return session_str.strip()

    # 2. Check legacy SQLite session file for migration
    legacy_path = os.path.join("sessions", "telegram_user")
    legacy_file = legacy_path + ".session"
    if os.path.exists(legacy_file):
        try:
            legacy_session = SQLiteSession(legacy_path)
            if legacy_session.auth_key:
                migrated_str = StringSession.save(legacy_session)
                legacy_session.close()
                if migrated_str:
                    logger.info("Migrated legacy SQLite Telegram session to in-memory StringSession.")
                    try:
                        config.update_env_variable("TELEGRAM_STRING_SESSION", migrated_str)
                    except Exception as e:
                        logger.warning(f"Could not auto-save migrated TELEGRAM_STRING_SESSION to .env: {e}")
                    return migrated_str
            else:
                legacy_session.close()
        except Exception as e:
            logger.warning(f"Failed to extract session from legacy SQLite file: {e}")

    return ""


def create_telegram_client(session_str: Optional[str] = None) -> Optional[TelegramClient]:
    """
    Creates and returns a Telethon TelegramClient configured with an in-memory StringSession.
    Eliminates SQLite database locking issues across multiple concurrent processes/terminals.
    """
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        return None

    try:
        api_id_int = int(api_id)
    except ValueError:
        print("Error: TELEGRAM_API_ID must be an integer", file=sys.stderr)
        return None

    if session_str is None:
        session_str = get_session_string()

    session = StringSession(session_str)
    return TelegramClient(session, api_id_int, api_hash)


# Initialize global default client instance
client: Optional[TelegramClient] = create_telegram_client()
if not client and (not API_ID or not API_HASH):
    print("Warning: TELEGRAM_API_ID or TELEGRAM_API_HASH is missing from .env", file=sys.stderr)


async def check_login(target_client: Optional[TelegramClient] = None) -> bool:
    """Checks if client is authorized, returns boolean."""
    cli = target_client or client
    if not cli:
        return False
    if not cli.is_connected():
        await cli.connect()
    return await cli.is_user_authorized()


async def interactive_login(target_client: Optional[TelegramClient] = None) -> bool:
    """Runs interactive login in terminal and persists TELEGRAM_STRING_SESSION."""
    global client
    cli = target_client or client
    if not cli:
        cli = create_telegram_client()
        if not cli:
            print("Cannot login: Telegram Client not initialized. Check TELEGRAM_API_ID and TELEGRAM_API_HASH in .env.")
            return False
        if target_client is None:
            client = cli

    if not cli.is_connected():
        await cli.connect()

    authorized = await cli.is_user_authorized()
    if authorized:
        print("Telegram Account is already authorized!")
        me = await cli.get_me()
        print(f"Logged in as: {me.first_name} (@{me.username or 'NoUsername'})")
        # Ensure StringSession is persisted in .env if not present
        session_str = cli.session.save()
        if session_str and not (os.getenv("TELEGRAM_STRING_SESSION") or os.getenv("TELEGRAM_SESSION_STRING")):
            config.update_env_variable("TELEGRAM_STRING_SESSION", session_str)
        return True

    # Standard Telethon interactive login
    print("Starting interactive Telegram authorization...")
    phone_to_use = os.getenv("TELEGRAM_PHONE") or PHONE or input("Enter Telegram phone number (with country code, e.g. +919876543210): ")

    try:
        await cli.start(phone=phone_to_use)
        me = await cli.get_me()
        print(f"Successfully logged in as: {me.first_name} (@{me.username or 'NoUsername'})")

        # Save generated StringSession to .env
        saved_session = cli.session.save()
        if saved_session:
            config.update_env_variable("TELEGRAM_STRING_SESSION", saved_session)
            print("[+] In-memory StringSession successfully saved to TELEGRAM_STRING_SESSION in .env!")
            print(f"[+] StringSession: {saved_session[:15]}... (length: {len(saved_session)})")

        return True
    except Exception as e:
        print(f"Error during authorization: {e}", file=sys.stderr)
        return False


async def get_channel_entity(channel_identifier, target_client: Optional[TelegramClient] = None):
    """
    Resolves a channel identifier (int ID, username, link) to a Telethon entity.
    """
    cli = target_client or client
    if not cli:
        return None
    if not channel_identifier:
        return None

    if not cli.is_connected():
        await cli.connect()

    # Clean identifier
    channel_str = str(channel_identifier).strip()

    # Try parsing as integer ID (e.g. -100123456 or 123456)
    try:
        if channel_str.replace("-", "").isdigit():
            # Telethon expects specific formats for IDs.
            # Usually, -100 prefix needs to be parsed as int or entity
            val = int(channel_str)
            return await cli.get_entity(val)
    except Exception:
        pass

    # Try parsing as username or link
    try:
        return await cli.get_entity(channel_str)
    except Exception as e:
        print(f"Error resolving entity '{channel_identifier}': {e}", file=sys.stderr)
        return None

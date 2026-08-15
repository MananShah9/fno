import os
from dotenv import load_dotenv, set_key

ENV_PATH = ".env"

def load_config():
    load_dotenv(ENV_PATH)
    return {
        "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH"),
        "TELEGRAM_PHONE": os.getenv("TELEGRAM_PHONE"),
        "TELEGRAM_SOURCE_CHANNEL": os.getenv("TELEGRAM_SOURCE_CHANNEL"),
        "TELEGRAM_MIRROR_CHANNEL": os.getenv("TELEGRAM_MIRROR_CHANNEL"),
        "TELEGRAM_ACTIONS_CHANNEL": os.getenv("TELEGRAM_ACTIONS_CHANNEL"),
        "TELEGRAM_REFRESH_INTERVAL": os.getenv("TELEGRAM_REFRESH_INTERVAL", "10"),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
        "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
        "DB_HOST": os.getenv("DB_HOST", "db"),
        "DB_PORT": os.getenv("DB_PORT", "5432"),
        "DB_USER": os.getenv("DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", "postgres_pass"),
        "DB_NAME": os.getenv("DB_NAME", "fno_db"),
        "ZERODHA_API_KEY": os.getenv("ZERODHA_API_KEY"),
        "ZERODHA_API_SECRET": os.getenv("ZERODHA_API_SECRET"),
        "ZERODHA_USER_ID": os.getenv("ZERODHA_USER_ID"),
        "ZERODHA_PASSWORD": os.getenv("ZERODHA_PASSWORD"),
        "ZERODHA_EXTERNAL_2FA_TOTP": os.getenv("ZERODHA_EXTERNAL_2FA_TOTP"),
        "ZERODHA_PROXY_URL": os.getenv("ZERODHA_PROXY_URL", "http://100.125.89.97:8888"),
        "AUTO_PLACE_ORDERS": os.getenv("AUTO_PLACE_ORDERS", "false").lower() in ("true", "1", "t", "yes"),
        "AUTO_PLACE_EXIT_ORDERS": os.getenv("AUTO_PLACE_EXIT_ORDERS", "false").lower() in ("true", "1", "t", "yes"),
    }

def update_env_variable(key: str, value: str):
    """Updates a variable in the .env file and reloads config."""
    # Ensure file exists
    if not os.path.exists(ENV_PATH):
        with open(ENV_PATH, "w") as f:
            f.write("")
            
    set_key(ENV_PATH, key, value)
    # Reload environment
    load_dotenv(ENV_PATH, override=True)
    print(f"Updated {key} to {value} in .env file.")

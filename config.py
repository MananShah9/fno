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
        "TELEGRAM_TIME_FILTER_ENABLED": os.getenv("TELEGRAM_TIME_FILTER_ENABLED", "false").lower() in ("true", "1", "t", "yes"),
        "TELEGRAM_START_TIME": os.getenv("TELEGRAM_START_TIME", "08:30"),
        "TELEGRAM_END_TIME": os.getenv("TELEGRAM_END_TIME", "16:30"),
        "TELEGRAM_WEEKDAYS_ONLY": os.getenv("TELEGRAM_WEEKDAYS_ONLY", "true").lower() in ("true", "1", "t", "yes"),
        "TELEGRAM_TIMEZONE": os.getenv("TELEGRAM_TIMEZONE", "Asia/Kolkata"),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
        "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
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
        "TARGET_INVESTMENT_BUDGET": os.getenv("TARGET_INVESTMENT_BUDGET", "100000"),
        "MAX_STOCK_LOTS": os.getenv("MAX_STOCK_LOTS", "2"),
        "MAX_INDEX_LOTS": os.getenv("MAX_INDEX_LOTS", "4"),
        "MAX_ADJUSTMENTS_PER_TRADE": os.getenv("MAX_ADJUSTMENTS_PER_TRADE", "1"),
        "ADJUSTMENT_DEDUPLICATION_WINDOW_MINUTES": os.getenv("ADJUSTMENT_DEDUPLICATION_WINDOW_MINUTES", "30"),
        "ADJUSTMENT_MAX_LOTS": os.getenv("ADJUSTMENT_MAX_LOTS", "1"),
        "ESTIMATED_INDEX_SPREAD_MARGIN": os.getenv("ESTIMATED_INDEX_SPREAD_MARGIN", "40000"),
        "ESTIMATED_STOCK_SPREAD_MARGIN": os.getenv("ESTIMATED_STOCK_SPREAD_MARGIN", "120000"),
        "ESTIMATED_INDEX_FUTURES_MARGIN": os.getenv("ESTIMATED_INDEX_FUTURES_MARGIN", "130000"),
        "ESTIMATED_STOCK_FUTURES_MARGIN": os.getenv("ESTIMATED_STOCK_FUTURES_MARGIN", "200000"),
        "ESTIMATED_INDEX_SHORT_OPTION_MARGIN": os.getenv("ESTIMATED_INDEX_SHORT_OPTION_MARGIN", "130000"),
        "ESTIMATED_STOCK_SHORT_OPTION_MARGIN": os.getenv("ESTIMATED_STOCK_SHORT_OPTION_MARGIN", "200000"),
        "FREEZE_LIMIT_NIFTY": os.getenv("FREEZE_LIMIT_NIFTY", "1800"),
        "FREEZE_LIMIT_BANKNIFTY": os.getenv("FREEZE_LIMIT_BANKNIFTY", "900"),
        "FREEZE_LIMIT_FINNIFTY": os.getenv("FREEZE_LIMIT_FINNIFTY", "1800"),
        "FREEZE_LIMIT_MIDCPNIFTY": os.getenv("FREEZE_LIMIT_MIDCPNIFTY", "4200"),
        "DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER": os.getenv("DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER", "20"),
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO").upper(),
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

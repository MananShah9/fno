import os
import csv
import io
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests

logger = logging.getLogger("instruments")

DATA_DIR = "data"
INSTRUMENTS_FILE = os.path.join(DATA_DIR, "nfo_instruments.csv")

_cached_instruments: Optional[List[Dict[str, Any]]] = None
_cached_fetch_date: Optional[str] = None

def get_proxy_dict():
    proxy_url = os.getenv("ZERODHA_PROXY_URL", "http://100.125.89.97:8888")
    if proxy_url:
        return {
            "http": proxy_url,
            "https": proxy_url
        }
    return None

def download_and_cache_nfo_instruments() -> str:
    """
    Downloads NFO instruments CSV from Kite API using proxy and saves to disk.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Check if already cached today
    if os.path.exists(INSTRUMENTS_FILE):
        file_mtime = datetime.fromtimestamp(os.path.getmtime(INSTRUMENTS_FILE)).strftime("%Y-%m-%d")
        if file_mtime == today_str and os.path.getsize(INSTRUMENTS_FILE) > 1000:
            logger.info(f"Using cached NFO instruments file: {INSTRUMENTS_FILE}")
            with open(INSTRUMENTS_FILE, "r", encoding="utf-8") as f:
                return f.read()

    logger.info("Downloading fresh NFO instruments CSV from Kite API...")
    url = "https://api.kite.trade/instruments/NFO"
    proxies = get_proxy_dict()

    try:
        r = requests.get(url, proxies=proxies, timeout=15)
        r.raise_for_status()
        csv_content = r.text

        with open(INSTRUMENTS_FILE, "w", encoding="utf-8") as f:
            f.write(csv_content)

        logger.info(f"Successfully downloaded and cached NFO instruments to {INSTRUMENTS_FILE}")
        return csv_content
    except Exception as e:
        logger.error(f"Error downloading NFO instruments: {e}")
        # Fallback to existing file if available
        if os.path.exists(INSTRUMENTS_FILE):
            logger.warning("Falling back to existing cached NFO instruments file")
            with open(INSTRUMENTS_FILE, "r", encoding="utf-8") as f:
                return f.read()
        raise

def get_nfo_instruments() -> List[Dict[str, Any]]:
    global _cached_instruments, _cached_fetch_date
    today_str = datetime.now().strftime("%Y-%m-%d")

    if _cached_instruments and _cached_fetch_date == today_str:
        return _cached_instruments

    csv_content = download_and_cache_nfo_instruments()
    reader = csv.DictReader(io.StringIO(csv_content))
    
    instruments = []
    for row in reader:
        instruments.append(row)

    _cached_instruments = instruments
    _cached_fetch_date = today_str
    return instruments

def resolve_nfo_instrument(
    underlying: str,
    strike: Optional[float] = None,
    option_type: str = "CE",
    expiry_hint: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Looks up exact NFO instrument given underlying, strike, option_type, and optional expiry.
    Returns dict with tradingsymbol, instrument_token, lot_size, tick_size, expiry, etc.
    """
    if not underlying:
        return None

    instruments = get_nfo_instruments()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    clean_underlying = underlying.strip().upper()
    clean_option_type = option_type.strip().upper() if option_type else "CE"

    candidates = []
    for row in instruments:
        # Match underlying name
        row_name = row.get("name", "").strip().upper()
        if row_name != clean_underlying:
            continue

        # Match instrument_type
        row_type = row.get("instrument_type", "").strip().upper()
        if clean_option_type == "FUT":
            if row_type != "FUT":
                continue
        else:
            if row_type != clean_option_type:
                continue

            # Check strike match for options
            if strike is not None:
                try:
                    row_strike = float(row.get("strike", 0))
                    if abs(row_strike - float(strike)) > 0.01:
                        continue
                except (ValueError, TypeError):
                    continue

        # Filter out expired instruments
        row_expiry = row.get("expiry", "")
        if row_expiry < today_str:
            continue

        candidates.append(row)

    if not candidates:
        logger.warning(f"No NFO instrument found for underlying={underlying}, strike={strike}, option_type={option_type}")
        return None

    # Sort candidates by expiry date ascending
    candidates.sort(key=lambda x: x.get("expiry", ""))

    # If expiry hint provided, try to find matching candidate
    if expiry_hint:
        clean_hint = expiry_hint.strip().upper()
        for c in candidates:
            # Check if hint in expiry string (e.g. "2026-08-11" or "AUG" or "28JUL")
            c_exp = c.get("expiry", "").upper()
            c_sym = c.get("tradingsymbol", "").upper()
            if clean_hint in c_exp or clean_hint in c_sym:
                return format_instrument_result(c)

    # Default to nearest active expiry contract
    return format_instrument_result(candidates[0])

def format_instrument_result(row: Dict[str, Any]) -> Dict[str, Any]:
    lot_size = int(row.get("lot_size", 1))
    return {
        "tradingsymbol": row.get("tradingsymbol"),
        "instrument_token": int(row.get("instrument_token", 0)),
        "exchange_token": row.get("exchange_token"),
        "name": row.get("name"),
        "expiry": row.get("expiry"),
        "strike": float(row.get("strike", 0)),
        "tick_size": float(row.get("tick_size", 0.05)),
        "lot_size": lot_size,
        "instrument_type": row.get("instrument_type"),
        "segment": row.get("segment", "NFO-OPT"),
        "exchange": row.get("exchange", "NFO")
    }

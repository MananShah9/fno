import os
import re
import csv
import io
import math
import logging
from decimal import Decimal
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests

logger = logging.getLogger("instruments")

DATA_DIR = "data"
INSTRUMENTS_FILE = os.path.join(DATA_DIR, "nfo_instruments.csv")

_cached_instruments: Optional[List[Dict[str, Any]]] = None
_cached_fetch_date: Optional[str] = None
_cached_underlyings_list: Optional[List[str]] = None
_cached_underlyings_set: Optional[set] = None

# Known market indices for derivatives
KNOWN_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "NIFTYIT", "NIFTY50", "NIFTYBANK", "CNXNIFTY", "CNXIT", "CNXFINANCE"
}

# Standard Indian Derivative Month Name Mappings
MONTH_NAME_TO_INT = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12
}

MONTHS_PAT = r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"

# Known Exchange Freeze Quantity Limits (NSE / BSE F&O)
# NSE enforces strict maximum quantity per single order (Freeze Limits).
# Orders exceeding freeze limit are rejected outright by broker RMS if not sliced.
KNOWN_FREEZE_LIMITS = {
    # Market Indices
    "NIFTY": 1800,
    "NIFTY50": 1800,
    "NIFTY 50": 1800,
    "BANKNIFTY": 900,
    "NIFTYBANK": 900,
    "NIFTY BANK": 900,
    "FINNIFTY": 1800,
    "CNXFINANCE": 1800,
    "MIDCPNIFTY": 4200,
    "NIFTYNXT50": 1800,
    "SENSEX": 1000,
    "BANKEX": 900,
    # High-volume Stock F&O standard freeze limits (NSE circular caps)
    "RELIANCE": 4500,
    "TATASTEEL": 55000,
    "HDFCBANK": 5500,
    "ICICIBANK": 7000,
    "SBIN": 15000,
    "INFY": 2400,
    "TCS": 1750,
    "BAJFINANCE": 1250,
    "AXISBANK": 6250,
    "KOTAKBANK": 4000,
    "LT": 1800,
    "ITC": 16000,
    "MARUTI": 500,
    "BHARTIARTL": 9500,
    "COALINDIA": 21000,
    "BHEL": 35000,
    "NATIONALUM": 37500,
    "VBL": 6000,
    "INDIGO": 3000,
}

DEFAULT_INDEX_FREEZE_LIMIT = 1800
DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER = 20
DEFAULT_STOCK_FREEZE_LIMIT = 5000

# Fallback tickers list used if NFO instruments master is inaccessible
DEFAULT_FALLBACK_TICKERS = [
    "BANKNIFTY", "MIDCPNIFTY", "FINNIFTY", "NIFTY", "SENSEX", "BANKEX", "NIFTYNXT50",
    "RELIANCE", "TATASTEEL", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS",
    "BAJFINANCE", "VBL", "INDIGO", "NATIONALUM", "COALINDIA", "BHEL", "MARUTI",
    "AXISBANK", "KOTAKBANK", "LT", "ITC"
]

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
    global _cached_instruments, _cached_fetch_date, _cached_underlyings_list, _cached_underlyings_set
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
    _cached_underlyings_list = None
    _cached_underlyings_set = None
    return instruments

def get_known_underlyings() -> List[str]:
    """
    Returns a dynamically loaded list of all underlying derivative equity and index symbols
    parsed directly from the daily NSE NFO instruments master database and known market indices.
    The list is sorted by length in descending order (longest first) to ensure specific/longer
    symbols match before shorter prefixes.
    """
    global _cached_underlyings_list, _cached_underlyings_set, _cached_fetch_date
    today_str = datetime.now().strftime("%Y-%m-%d")

    if _cached_underlyings_list and _cached_fetch_date == today_str:
        return _cached_underlyings_list

    symbols_set = set(KNOWN_INDEX_SYMBOLS)
    try:
        instruments = get_nfo_instruments()
        for row in instruments:
            name = row.get("name")
            if name:
                clean_name = str(name).strip().upper()
                if clean_name:
                    symbols_set.add(clean_name)
    except Exception as e:
        logger.warning(f"Could not load underlyings from NFO instruments CSV: {e}. Using fallback tickers.")
        symbols_set.update(DEFAULT_FALLBACK_TICKERS)

    # Sort by length descending, then alphabetical for deterministic ordering
    sorted_symbols = sorted(list(symbols_set), key=lambda x: (len(x), x), reverse=True)
    _cached_underlyings_list = sorted_symbols
    _cached_underlyings_set = symbols_set
    return sorted_symbols

def get_known_underlyings_set() -> set:
    """
    Returns a set of all known underlying tickers for fast O(1) membership lookup.
    """
    global _cached_underlyings_set, _cached_fetch_date
    today_str = datetime.now().strftime("%Y-%m-%d")

    if _cached_underlyings_set and _cached_fetch_date == today_str:
        return _cached_underlyings_set

    get_known_underlyings()
    return _cached_underlyings_set or set(DEFAULT_FALLBACK_TICKERS)

def is_monthly_contract(row: Dict[str, Any]) -> bool:
    """
    Determines if an instrument contract row is a standard Monthly expiry contract.
    Monthly contracts in NSE/BSE F&O have 3-letter month abbreviations (JAN..DEC) in tradingsymbol
    immediately following the 2-digit year prefix (e.g. NIFTY26AUG24500CE, TATASTEEL26AUG192.5PE).
    Weekly contracts have single-digit or single-char month codes followed by 2-digit days
    (e.g. NIFTY2680424500CE, NIFTY2690124500CE, NIFTY26O0624500CE).
    """
    sym = str(row.get("tradingsymbol", "")).strip().upper()
    name = str(row.get("name", "")).strip().upper()
    if name and sym.startswith(name):
        rem = sym[len(name):]
        if re.match(rf"^\d{{2}}{MONTHS_PAT}", rem):
            return True
        return False
    clean_name = re.sub(r"[^A-Z0-9]", "", name)
    if clean_name and sym.startswith(clean_name):
        rem = sym[len(clean_name):]
        if re.match(rf"^\d{{2}}{MONTHS_PAT}", rem):
            return True
        return False
    return bool(re.search(rf"\d{{2}}{MONTHS_PAT}", sym))


def parse_expiry_hint(hint: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Translates natural language expiry phrases (e.g., 'Aug Series', 'Monthly', '11 Aug', '4th Aug',
    'Current Month', 'Next Month', '28JUL2026', '2026-08-04') into structured normalized metadata.
    
    Returns a dict with metadata:
      - 'type': 'exact_date' | 'specific_day_month' | 'month_series' | 'relative_month' | 'relative_week' | 'raw'
      - 'iso': 'YYYY-MM-DD' (if exact_date)
      - 'day': int (if specific_day_month)
      - 'month': int (1-12)
      - 'year': int (e.g. 2026) or None
      - 'offset': int (0 for current, 1 for next, 2 for far)
      - 'is_monthly': bool
    """
    if not hint:
        return None
    raw = str(hint).strip().upper()
    if not raw:
        return None

    # 1. ISO Date: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD
    m_iso = re.search(r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b", raw)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return {"type": "exact_date", "year": y, "month": m, "day": d, "iso": f"{y:04d}-{m:02d}-{d:02d}"}

    # 2. Standard numeric: DD-MM-YYYY or DD/MM/YYYY or DD-MM-YY
    m_num = re.search(r"\b(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.](20\d{2}|\d{2})\b", raw)
    if m_num:
        d, m, y_str = int(m_num.group(1)), int(m_num.group(2)), m_num.group(3)
        y = 2000 + int(y_str) if len(y_str) == 2 else int(y_str)
        return {"type": "exact_date", "year": y, "month": m, "day": d, "iso": f"{y:04d}-{m:02d}-{d:02d}"}

    months_re = "|".join(sorted(MONTH_NAME_TO_INT.keys(), key=lambda x: len(x), reverse=True))

    # 3. Day + Month (+ optional Year) e.g. '4th Aug Series', '4 Aug', '11 Aug', '28JUL', '4th Aug 2026', '28JUL2026', '28-Jul-2026'
    m_day_month = re.search(r"\b(0?[1-9]|[12]\d|3[01])(?:ST|ND|RD|TH)?[\s\-_]*(" + months_re + r")(?:\s*[\-_]?\s*(20\d{2}|\d{2}))?\b", raw)
    if m_day_month:
        d = int(m_day_month.group(1))
        m = MONTH_NAME_TO_INT[m_day_month.group(2)]
        y_str = m_day_month.group(3)
        y = (2000 + int(y_str) if len(y_str) == 2 else int(y_str)) if y_str else None
        return {"type": "specific_day_month", "day": d, "month": m, "year": y}

    # 4. Month + 4-digit Year (e.g. 'Aug 2026', 'August 2026 Monthly Series')
    m_month_year = re.search(r"\b(" + months_re + r")[\s\-_]+(20\d{2})\b", raw)
    if m_month_year:
        m = MONTH_NAME_TO_INT[m_month_year.group(1)]
        y = int(m_month_year.group(2))
        return {"type": "month_series", "month": m, "year": y, "is_monthly": True}

    # 5. Month + Day (+ optional Year) e.g. 'Aug 4th', 'August 11', 'Aug 4, 2026'
    m_month_day = re.search(r"\b(" + months_re + r")[\s\-_]+(0?[1-9]|[12]\d|3[01])(?:ST|ND|RD|TH)?(?!\d)(?:\s*[\-_,\s]?\s*(20\d{2}|\d{2}))?\b", raw)
    if m_month_day:
        m = MONTH_NAME_TO_INT[m_month_day.group(1)]
        d = int(m_month_day.group(2))
        y_str = m_month_day.group(3)
        y = (2000 + int(y_str) if len(y_str) == 2 else int(y_str)) if y_str else None
        return {"type": "specific_day_month", "day": d, "month": m, "year": y}

    # 6. Month Only / Month Series e.g. 'Aug Series', 'Aug Monthly', 'August', 'AUG', 'AUG EXPIRY'
    m_month_only = re.search(r"\b(" + months_re + r")(?:\s*(?:SERIES|EXPIRY|MONTH(?:LY)?|CONTRACT))?\b", raw)
    if m_month_only:
        m = MONTH_NAME_TO_INT[m_month_only.group(1)]
        return {"type": "month_series", "month": m, "year": None, "is_monthly": True}

    # 7. Generic Relative Month (when no specific month name is mentioned)
    if re.search(r"\bFAR\s+MONTH(?:LY)?\b", raw):
        return {"type": "relative_month", "offset": 2, "is_monthly": True}
    if re.search(r"\bNEXT\s+MONTH(?:LY)?\b", raw):
        return {"type": "relative_month", "offset": 1, "is_monthly": True}
    if re.search(r"\b(THIS|CURRENT)\s+MONTH(?:LY)?\b|\bMONTHLY\b|\bMONTH\s+END\b", raw):
        return {"type": "relative_month", "offset": 0, "is_monthly": True}

    # 8. Generic Relative Week
    if re.search(r"\bNEXT\s+WEEK(?:LY)?\b", raw):
        return {"type": "relative_week", "offset": 1, "is_weekly": True}
    if re.search(r"\b(THIS|CURRENT)\s+WEEK(?:LY)?\b|\bWEEKLY\b", raw):
        return {"type": "relative_week", "offset": 0, "is_weekly": True}

    return {"type": "raw", "raw": raw}


def match_candidate_by_expiry(
    candidates: List[Dict[str, Any]],
    expiry_hint: Optional[str]
) -> Dict[str, Any]:
    """
    Selects the best matching candidate contract from a list of eligible active candidates
    (sorted by expiry date ascending) using natural language expiry parsing and explicit distinction
    between Index Weekly, Index Monthly, and Stock Monthly contracts.
    
    If expiry_hint is omitted or cannot be resolved, falls back gracefully to the nearest active expiry.
    """
    if not candidates:
        return {}
    if not expiry_hint or not str(expiry_hint).strip():
        return candidates[0]

    clean_hint = str(expiry_hint).strip().upper()
    parsed = parse_expiry_hint(expiry_hint)

    if parsed:
        p_type = parsed.get("type")

        # Tier 1: Exact ISO date match (e.g. "2026-08-04")
        if p_type == "exact_date":
            target_iso = parsed["iso"]
            for c in candidates:
                if c.get("expiry") == target_iso:
                    return c

        # Tier 2: Specific day + month (+ optional year) (e.g. "4th Aug", "11 Aug", "28JUL", "Aug 4th")
        if p_type == "specific_day_month":
            t_day = parsed["day"]
            t_month = parsed["month"]
            t_year = parsed.get("year")
            # 1. Exact day + month match
            for c in candidates:
                c_exp = c.get("expiry", "")
                try:
                    dt = datetime.strptime(c_exp, "%Y-%m-%d")
                    if dt.day == t_day and dt.month == t_month:
                        if t_year is None or dt.year == t_year:
                            return c
                except Exception:
                    pass
            # 2. Near day match in same month (exchange holiday shift +-2 days)
            near_match = None
            min_diff = 999
            for c in candidates:
                c_exp = c.get("expiry", "")
                try:
                    dt = datetime.strptime(c_exp, "%Y-%m-%d")
                    if dt.month == t_month and (t_year is None or dt.year == t_year):
                        diff = abs(dt.day - t_day)
                        if diff <= 2 and diff < min_diff:
                            min_diff = diff
                            near_match = c
                except Exception:
                    pass
            if near_match:
                return near_match

        # Tier 3: Month series / Monthly contract for specific month (e.g. "Aug Series", "AUG", "August", "August 2026")
        if p_type == "month_series":
            t_month = parsed["month"]
            t_year = parsed.get("year")
            month_candidates = []
            for c in candidates:
                c_exp = c.get("expiry", "")
                try:
                    dt = datetime.strptime(c_exp, "%Y-%m-%d")
                    if dt.month == t_month and (t_year is None or dt.year == t_year):
                        month_candidates.append((c, is_monthly_contract(c), dt))
                except Exception:
                    pass
            if month_candidates:
                # Prefer explicit monthly contract (3-letter month code in tradingsymbol)
                for c, is_m, dt in month_candidates:
                    if is_m:
                        return c
                # Fallback to latest expiry date in that month
                month_candidates.sort(key=lambda x: x[2], reverse=True)
                return month_candidates[0][0]

        # Tier 4: Relative month ("Monthly", "Next Month", "Far Month")
        if p_type == "relative_month":
            offset = parsed.get("offset", 0)
            monthly_cands = [c for c in candidates if is_monthly_contract(c)]
            if monthly_cands:
                if offset < len(monthly_cands):
                    return monthly_cands[offset]
                return monthly_cands[-1]

        # Tier 5: Relative week ("Weekly", "Next Week")
        if p_type == "relative_week":
            offset = parsed.get("offset", 0)
            if offset < len(candidates):
                return candidates[offset]
            return candidates[-1]

    # Tier 6: Substring / Tradingsymbol Token match fallback
    for c in candidates:
        c_exp = c.get("expiry", "").upper()
        c_sym = c.get("tradingsymbol", "").upper()
        if clean_hint in c_exp or clean_hint in c_sym:
            return c

    # Tier 7: Default to nearest active expiry contract
    logger.info(
        f"Expiry hint '{expiry_hint}' could not be matched explicitly. "
        f"Defaulting to nearest active expiry: {candidates[0].get('expiry')} ({candidates[0].get('tradingsymbol')})"
    )
    return candidates[0]


def resolve_nfo_instrument(
    underlying: str,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
    expiry_hint: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Looks up exact NFO instrument given underlying, strike, option_type, and optional expiry.
    Returns dict with tradingsymbol, instrument_token, lot_size, tick_size, expiry, etc.
    Strictly forbids arbitrary default fallbacks to 'CE' for options.
    Distinguishes explicitly between Index Weekly, Index Monthly, and Stock Monthly contracts.
    """
    if not underlying:
        return None

    instruments = get_nfo_instruments()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    clean_underlying = underlying.strip().upper()
    clean_option_type = option_type.strip().upper() if option_type else None

    # If strike is specified for an option, option_type MUST be provided ('CE' or 'PE').
    # Arbitrary defaulting to 'CE' is strictly forbidden to prevent unintended opposite-side trades.
    if strike is not None:
        if clean_option_type not in ["CE", "PE"]:
            logger.warning(
                f"Cannot resolve NFO option instrument for underlying={underlying}, strike={strike}: "
                f"option_type '{option_type}' is missing or invalid (must be 'CE' or 'PE')."
            )
            return None

    # Conversely, for options ('CE' or 'PE'), strike MUST be provided.
    # Resolving an option without a strike would pick an arbitrary contract and pollute records.
    if clean_option_type in ["CE", "PE"] and strike is None:
        logger.warning(
            f"Cannot resolve NFO option instrument for underlying={underlying}: "
            f"strike is missing for option_type '{clean_option_type}'."
        )
        return None

    candidates = []
    for row in instruments:
        # Match underlying name
        row_name = row.get("name", "").strip().upper()
        if row_name != clean_underlying:
            continue

        # Match instrument_type
        row_type = row.get("instrument_type", "").strip().upper()
        if clean_option_type == "FUT" or (clean_option_type is None and strike is None):
            if row_type != "FUT":
                continue
        elif clean_option_type in ["CE", "PE"]:
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
        else:
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

    # Resolve matching candidate based on parsed expiry hint or nearest active expiry
    selected = match_candidate_by_expiry(candidates, expiry_hint)
    return format_instrument_result(selected)

def get_freeze_quantity(
    tradingsymbol: Optional[str] = None,
    underlying: Optional[str] = None,
    lot_size: Optional[int] = None,
    instrument_row: Optional[Dict[str, Any]] = None
) -> int:
    """
    Returns the maximum single order quantity allowed by exchange RMS (Freeze Limit)
    for a given derivative contract / underlying.
    
    Order of precedence:
      1. Explicit freeze quantity from NFO instruments CSV row (freeze_qty, freeze_quantity, max_quantity, freeze_limit)
      2. Environment variable override (e.g., FREEZE_LIMIT_NIFTY, FREEZE_LIMIT_BANKNIFTY, FREEZE_LIMIT_<UNDERLYING>)
      3. Static lookup table for known indices and stocks (KNOWN_FREEZE_LIMITS)
      4. Default index freeze cap (1800) if underlying is an index
      5. Stock lot size multiplier (e.g. lot_size * DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER) or fallback stock limit (5000)
    """
    # 1. Check instrument row if provided
    if instrument_row:
        for k in ("freeze_qty", "freeze_quantity", "max_quantity", "freeze_limit"):
            val = instrument_row.get(k)
            if val is not None and str(val).strip() != "":
                try:
                    ival = int(float(val))
                    if ival > 0:
                        return ival
                except (ValueError, TypeError):
                    pass

    # Extract clean underlying
    clean_underlying = None
    if underlying:
        clean_underlying = re.sub(r'[^A-Z0-9]', '', str(underlying).strip().upper())
    elif tradingsymbol:
        sym = str(tradingsymbol).strip().upper()
        # Check against known index symbols first
        for idx in ("MIDCPNIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYNXT50", "NIFTY", "SENSEX", "BANKEX"):
            if sym.startswith(idx):
                clean_underlying = idx
                break
        if not clean_underlying:
            # Match alphabetic prefix before digits (e.g., "TATASTEEL26AUG..." -> "TATASTEEL")
            m = re.match(r'^([A-Z]+)', sym)
            if m:
                clean_underlying = m.group(1)

    # 2. Check environment variable override
    if clean_underlying:
        env_key = f"FREEZE_LIMIT_{clean_underlying}"
        env_val = os.getenv(env_key)
        if env_val:
            try:
                ival = int(env_val)
                if ival > 0:
                    return ival
            except (ValueError, TypeError):
                pass

    # 3. Static lookup table for known symbols
    if clean_underlying and clean_underlying in KNOWN_FREEZE_LIMITS:
        return KNOWN_FREEZE_LIMITS[clean_underlying]

    # 4. Check if index symbol
    if clean_underlying and is_index_symbol(clean_underlying):
        return int(os.getenv("DEFAULT_INDEX_FREEZE_LIMIT", DEFAULT_INDEX_FREEZE_LIMIT))

    # 5. For stocks, use lot_size * multiplier if lot_size available
    multiplier = int(os.getenv("DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER", DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER))
    if lot_size and lot_size > 0:
        return max(lot_size, lot_size * multiplier)

    # Final fallback
    return int(os.getenv("DEFAULT_STOCK_FREEZE_LIMIT", DEFAULT_STOCK_FREEZE_LIMIT))


def slice_order_quantity(
    total_quantity: int,
    freeze_limit: int,
    lot_size: int = 1
) -> List[int]:
    """
    Slices a large order quantity into broker/exchange compliant sub-order slices (iceberg slicing)
    such that each slice is <= freeze_limit, > 0, and preserves lot size multiples.
    
    Args:
        total_quantity: Total target order quantity
        freeze_limit: Maximum allowed single order quantity
        lot_size: Contract lot size (default 1)
        
    Returns:
        List[int] representing the quantity for each slice order.
        e.g., total_quantity=3900, freeze_limit=1800, lot_size=65 -> [1755, 1755, 390]
        e.g., total_quantity=1800, freeze_limit=900, lot_size=30 -> [900, 900]
        e.g., total_quantity=65, freeze_limit=1800, lot_size=65 -> [65]
    """
    if total_quantity is None or total_quantity <= 0:
        return []

    if freeze_limit is None or freeze_limit <= 0 or total_quantity <= freeze_limit:
        return [int(total_quantity)]

    ls = max(1, int(lot_size or 1))
    
    # Calculate max slice size as a multiple of lot_size <= freeze_limit
    if ls > 1 and freeze_limit >= ls:
        max_slice = (freeze_limit // ls) * ls
    else:
        max_slice = freeze_limit

    if max_slice <= 0:
        max_slice = min(int(total_quantity), max(1, int(freeze_limit)))

    slices = []
    remaining = int(total_quantity)

    while remaining > 0:
        if remaining <= max_slice:
            slices.append(remaining)
            break
        else:
            slices.append(max_slice)
            remaining -= max_slice

    return slices


get_instrument_freeze_limit = get_freeze_quantity


def format_instrument_result(row: Dict[str, Any]) -> Dict[str, Any]:
    lot_size = int(row.get("lot_size", 1))
    freeze_qty = get_freeze_quantity(
        tradingsymbol=row.get("tradingsymbol"),
        underlying=row.get("name"),
        lot_size=lot_size,
        instrument_row=row
    )
    return {
        "tradingsymbol": row.get("tradingsymbol"),
        "instrument_token": int(row.get("instrument_token", 0)),
        "exchange_token": row.get("exchange_token"),
        "name": row.get("name"),
        "expiry": row.get("expiry"),
        "strike": float(row.get("strike", 0)),
        "tick_size": float(row.get("tick_size", 0.05)),
        "lot_size": lot_size,
        "freeze_qty": freeze_qty,
        "instrument_type": row.get("instrument_type"),
        "segment": row.get("segment", "NFO-OPT"),
        "exchange": row.get("exchange", "NFO")
    }

def round_to_tick(
    price: Optional[float],
    tick_size: float = 0.05,
    direction: Optional[str] = None
) -> Optional[float]:
    """
    Rounds a price or trigger price to the nearest valid exchange tick size (default 0.05 INR for Indian equity derivatives).
    
    Args:
        price: Numeric price value
        tick_size: Minimum price variation (tick size), defaults to 0.05
        direction: 'BUY'/'DOWN'/'FLOOR'/'BID' to round down to nearest valid tick,
                   'SELL'/'UP'/'CEIL'/'CEILING'/'ASK' to round up to nearest valid tick,
                   or None/'NEAREST' for standard nearest tick rounding.
                   
    Returns:
        float rounded to valid tick precision (e.g., 1.525 with BUY -> 1.50, with SELL -> 1.55)
    """
    if price is None:
        return None
    try:
        val = float(price)
        if val <= 0:
            return None
        if not tick_size or tick_size <= 0:
            tick_size = 0.05
        
        # Calculate decimal precision of tick_size
        try:
            d = Decimal(str(tick_size)).normalize()
            decimals = max(2, -d.as_tuple().exponent)
        except Exception:
            decimals = 2
        
        dir_norm = str(direction).strip().upper() if direction else ""
        ticks = round(val / tick_size, 8)
        
        if dir_norm in ("BUY", "DOWN", "FLOOR", "BID"):
            rounded_ticks = math.floor(ticks)
        elif dir_norm in ("SELL", "UP", "CEIL", "CEILING", "ASK"):
            rounded_ticks = math.ceil(ticks)
        else:
            rounded_ticks = round(ticks)
            
        result = round(rounded_ticks * tick_size, decimals)
        return result
    except (ValueError, TypeError, ZeroDivisionError):
        return None

def parse_price_value(
    price_val: Any,
    tick_size: float = 0.05,
    direction: Optional[str] = None
) -> Optional[float]:
    """
    Parses a price string, range, or number into a single float price rounded to valid exchange tick size.
    Examples:
      '183' -> 183.0
      '467-468' -> 467.5 (midpoint)
      '4.5-4.7' -> 4.6
      '1.5-1.55' (BUY) -> 1.50
      '1.5-1.55' (SELL) -> 1.55
      '@ 81' -> 81.0
      461.9 -> 461.9
    """
    if price_val is None:
        return None
    
    raw_val: Optional[float] = None
    if isinstance(price_val, (int, float)):
        try:
            val = float(price_val)
            if val > 0:
                raw_val = val
        except (ValueError, TypeError):
            return None
    else:
        s = str(price_val).strip()
        # Extract numeric portions (integers or decimals)
        nums = re.findall(r'\d+(?:\.\d+)?', s)
        if not nums:
            return None
        try:
            if len(nums) == 1:
                raw_val = float(nums[0])
            else:
                # If range like 467-468 or 1.5-1.55, return average of the first two numbers
                raw_val = (float(nums[0]) + float(nums[1])) / 2.0
        except (ValueError, TypeError):
            return None

    if raw_val is None or raw_val <= 0:
        return None

    return round_to_tick(raw_val, tick_size=tick_size, direction=direction)

# Zerodha Kite Connect spot instrument quote key mappings for indices
INDEX_SPOT_KEY_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "NIFTY50": "NSE:NIFTY 50",
    "NIFTY 50": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "NIFTYBANK": "NSE:NIFTY BANK",
    "NIFTY BANK": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "CNXFINANCE": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "NIFTYNXT50": "NSE:NIFTY NEXT 50",
    "SENSEX": "BSE:SENSEX",
    "BANKEX": "BSE:BANKEX"
}

def get_spot_instrument_key(underlying: Optional[str]) -> Optional[str]:
    """
    Returns the Zerodha Kite Connect instrument quote key for the underlying spot/cash index or stock.
    Examples:
      'NIFTY' -> 'NSE:NIFTY 50'
      'BANKNIFTY' -> 'NSE:NIFTY BANK'
      'SENSEX' -> 'BSE:SENSEX'
      'TATASTEEL' -> 'NSE:TATASTEEL'
      'VBL' -> 'NSE:VBL'
    """
    if not underlying:
        return None
    raw_u = str(underlying).strip().upper()
    clean = re.sub(r'[^A-Z0-9]', '', raw_u)
    for k, v in INDEX_SPOT_KEY_MAP.items():
        clean_k = re.sub(r'[^A-Z0-9]', '', k)
        if clean == clean_k or clean.startswith(clean_k):
            return v
    # Default to NSE stock symbol
    return f"NSE:{clean}"

# Default estimated margin tiers in INR per lot (used as fallbacks when broker API is unavailable)
DEFAULT_MARGIN_TIERS = {
    "INDEX_SPREAD": 40000.0,            # e.g., Nifty/BankNifty Hedged Credit/Debit Spreads (~35k-50k)
    "STOCK_SPREAD": 120000.0,           # e.g., Stock Hedged Spreads (~1.0L-1.5L)
    "INDEX_FUTURES": 130000.0,          # e.g., Single Index Futures (~1.2L-1.5L)
    "STOCK_FUTURES": 200000.0,          # e.g., Single Stock Futures (~1.5L-2.5L)
    "INDEX_SHORT_OPTION": 130000.0,     # e.g., Naked Short Index Option (~1.2L-1.5L)
    "STOCK_SHORT_OPTION": 200000.0,     # e.g., Naked Short Stock Option (~1.8L-2.5L)
}

# Default strict maximum lot caps per underlying type
DEFAULT_MAX_INDEX_LOTS = 4  # max 2-4 lots for index
DEFAULT_MAX_STOCK_LOTS = 2  # max 1-2 lots for stock


def is_index_symbol(symbol: Optional[str]) -> bool:
    """
    Determines whether a symbol/underlying is a market index contract.
    """
    if not symbol:
        return False
    clean = re.sub(r'[^A-Z0-9]', '', str(symbol).strip().upper())
    if clean in KNOWN_INDEX_SYMBOLS:
        return True
    for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]:
        if clean.startswith(idx):
            return True
    return False


def classify_strategy_type(
    entry_legs: List[Dict[str, Any]],
    underlying: Optional[str] = None
) -> str:
    """
    Classifies a set of trade entry legs into a recognized trading strategy:
    - NAKED_OPTION_BUY: Only BUY options (CE/PE), no SELL or FUT
    - NAKED_SHORT_OPTION: Only SELL options (CE/PE), no BUY or FUT
    - SINGLE_FUTURES: Only FUT leg (BUY or SELL), no option hedge
    - SPREAD: Hedged setups (Credit spread, Debit spread, Bull/Bear Fut spread with option hedge)
    """
    if not entry_legs:
        return "NAKED_OPTION_BUY"

    action_types = []
    option_types = []

    for leg in entry_legs:
        # Check action_type from schema or dict
        at = getattr(leg.get("schema", None), "action_type", None) or leg.get("action_type") or "INFO"
        ot = getattr(leg.get("schema", None), "option_type", None) or leg.get("option_type")
        action_types.append(str(at).upper())
        if ot:
            option_types.append(str(ot).upper())

    has_fut = any(ot == "FUT" for ot in option_types)
    has_sell = any(at == "SELL" for at in action_types)
    has_buy = any(at == "BUY" for at in action_types)
    has_options = any(ot in ["CE", "PE"] for ot in option_types)

    # If both buy and sell legs exist, or fut + option hedge
    if (has_sell and has_buy) or (has_fut and has_options):
        return "SPREAD"
    
    if has_fut:
        return "SINGLE_FUTURES"

    if has_sell and not has_buy:
        return "NAKED_SHORT_OPTION"

    # All buy options
    return "NAKED_OPTION_BUY"


def get_margin_tier_estimate(underlying: Optional[str], strategy_type: str) -> float:
    """
    Returns estimated margin requirement per lot based on strategy type and underlying,
    consulting environment variables first with fallbacks to standard exchange tiers.
    """
    is_index = is_index_symbol(underlying)

    if strategy_type == "SPREAD":
        if is_index:
            return float(os.getenv("ESTIMATED_INDEX_SPREAD_MARGIN", DEFAULT_MARGIN_TIERS["INDEX_SPREAD"]))
        else:
            return float(os.getenv("ESTIMATED_STOCK_SPREAD_MARGIN", DEFAULT_MARGIN_TIERS["STOCK_SPREAD"]))
    elif strategy_type == "SINGLE_FUTURES":
        if is_index:
            return float(os.getenv("ESTIMATED_INDEX_FUTURES_MARGIN", DEFAULT_MARGIN_TIERS["INDEX_FUTURES"]))
        else:
            return float(os.getenv("ESTIMATED_STOCK_FUTURES_MARGIN", DEFAULT_MARGIN_TIERS["STOCK_FUTURES"]))
    elif strategy_type == "NAKED_SHORT_OPTION":
        if is_index:
            return float(os.getenv("ESTIMATED_INDEX_SHORT_OPTION_MARGIN", DEFAULT_MARGIN_TIERS["INDEX_SHORT_OPTION"]))
        else:
            return float(os.getenv("ESTIMATED_STOCK_SHORT_OPTION_MARGIN", DEFAULT_MARGIN_TIERS["STOCK_SHORT_OPTION"]))
    
    # Default fallback
    return float(os.getenv("ESTIMATED_STOCK_SPREAD_MARGIN", DEFAULT_MARGIN_TIERS["STOCK_SPREAD"]))


def get_max_lot_cap(underlying: Optional[str]) -> int:
    """
    Returns the maximum allowable lots per trade for the given underlying.
    Enforces strict risk caps: e.g. max 1-2 lots for stock, max 2-4 lots for index.
    """
    is_index = is_index_symbol(underlying)
    if is_index:
        raw_cap = os.getenv("MAX_INDEX_LOTS") or os.getenv("MAX_INDEX_SPREAD_LOTS")
        try:
            return max(1, int(raw_cap)) if raw_cap else DEFAULT_MAX_INDEX_LOTS
        except (ValueError, TypeError):
            return DEFAULT_MAX_INDEX_LOTS
    else:
        raw_cap = os.getenv("MAX_STOCK_LOTS") or os.getenv("MAX_STOCK_SPREAD_LOTS")
        try:
            return max(1, int(raw_cap)) if raw_cap else DEFAULT_MAX_STOCK_LOTS
        except (ValueError, TypeError):
            return DEFAULT_MAX_STOCK_LOTS


def calculate_position_size(
    entry_legs: List[Dict[str, Any]],
    target_budget: Optional[float] = None,
    underlying: Optional[str] = None,
    live_margin: Optional[float] = None,
    main_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Computes position sizing in lots and quantity for derivative trades using margin-based model.
    - For Naked Option Buying: uses total premium cost per lot (price * lot_size)
    - For Spreads, Short Options, Futures: uses live Zerodha margin (if provided) or estimated margin tiers
    - Enforces strict maximum lot caps per underlying type (Stock vs Index).
    
    Returns dict:
      {
        "lots": int,
        "strategy_type": str,
        "is_index": bool,
        "per_lot_capital": float,
        "sizing_method": str ("ZERODHA_LIVE_MARGIN" | "ESTIMATED_MARGIN_TIER" | "PREMIUM_COST"),
        "max_lot_cap": int,
        "raw_lots": float
      }
    """
    if target_budget is None or target_budget <= 0:
        raw_b = os.getenv("TARGET_INVESTMENT_BUDGET") or os.getenv("TARGET_INVESTMENT_BUDGET_MAIN")
        if raw_b:
            try:
                target_budget = float(raw_b)
            except (ValueError, TypeError):
                target_budget = 100000.0
        else:
            target_budget = 100000.0

    if not underlying and entry_legs:
        underlying = entry_legs[0].get("underlying")

    is_idx = is_index_symbol(underlying)
    strat_type = classify_strategy_type(entry_legs, underlying)
    max_cap = get_max_lot_cap(underlying)

    # 1. Option Buying (Pure Long Options)
    if strat_type == "NAKED_OPTION_BUY":
        lot_cost = 0.0
        for leg in entry_legs:
            leg_inst = leg.get("inst")
            ls = leg_inst.get("lot_size", 1) if leg_inst else 1
            p = parse_price_value(getattr(leg.get("schema"), "price", None) if hasattr(leg.get("schema"), "price") else leg.get("price"))
            if not p or p <= 0:
                p = main_price or 0.0
            lot_cost += (p * ls)

        if lot_cost <= 0:
            lot_cost = 1000.0  # Fallback non-zero cost

        raw_lots = target_budget / lot_cost
        calculated_lots = max(1, int(round(raw_lots)))
        final_lots = min(calculated_lots, max_cap)

        return {
            "lots": final_lots,
            "strategy_type": strat_type,
            "is_index": is_idx,
            "per_lot_capital": lot_cost,
            "sizing_method": "PREMIUM_COST",
            "max_lot_cap": max_cap,
            "raw_lots": raw_lots
        }

    # 2. Spreads, Short Options, Futures (Margin-Based)
    if live_margin and live_margin > 0:
        per_lot_margin = float(live_margin)
        sizing_method = "ZERODHA_LIVE_MARGIN"
    else:
        per_lot_margin = get_margin_tier_estimate(underlying, strat_type)
        sizing_method = "ESTIMATED_MARGIN_TIER"

    raw_lots = target_budget / per_lot_margin
    calculated_lots = max(1, int(round(raw_lots)))
    final_lots = min(calculated_lots, max_cap)

    return {
        "lots": final_lots,
        "strategy_type": strat_type,
        "is_index": is_idx,
        "per_lot_capital": per_lot_margin,
        "sizing_method": sizing_method,
        "max_lot_cap": max_cap,
        "raw_lots": raw_lots
    }


def calculate_lots_from_budget(
    main_price: Optional[float],
    lot_size: int,
    target_budget: Optional[float] = None,
    underlying: Optional[str] = None,
    strategy_type: Optional[str] = None,
    margin_per_lot: Optional[float] = None,
    max_cap: Optional[int] = None
) -> int:
    """
    Calculates the number of lots to trade based on the target investment budget and margin.
    If margin_per_lot is provided, sizing is based on required margin.
    If strategy_type is Short Option, Futures, or Spread, margin tier estimates are used.
    Enforces maximum lot caps per underlying type.
    """
    if target_budget is None or target_budget <= 0:
        raw_b = os.getenv("TARGET_INVESTMENT_BUDGET") or os.getenv("TARGET_INVESTMENT_BUDGET_MAIN")
        if raw_b:
            try:
                target_budget = float(raw_b)
            except (ValueError, TypeError):
                target_budget = 100000.0
        else:
            target_budget = 100000.0

    cap = max_cap or get_max_lot_cap(underlying)

    # If margin is explicitly supplied
    if margin_per_lot and margin_per_lot > 0:
        raw_lots = target_budget / margin_per_lot
        lots = max(1, int(round(raw_lots)))
        return min(lots, cap)

    # If strategy_type specified and requires margin
    if strategy_type in ["SPREAD", "SINGLE_FUTURES", "NAKED_SHORT_OPTION"]:
        est_margin = get_margin_tier_estimate(underlying, strategy_type)
        raw_lots = target_budget / est_margin
        lots = max(1, int(round(raw_lots)))
        return min(lots, cap)

    # Pure premium cost calculation (e.g. Option Buying)
    if not main_price or main_price <= 0 or not lot_size or lot_size <= 0:
        return 1

    single_lot_cost = main_price * lot_size
    if single_lot_cost <= 0:
        return 1

    target_lots = target_budget / single_lot_cost
    lots = max(1, int(round(target_lots)))
    return min(lots, cap)



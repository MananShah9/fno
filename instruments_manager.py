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



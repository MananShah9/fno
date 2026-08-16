import os
import re
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

def parse_price_value(price_val: Any) -> Optional[float]:
    """
    Parses a price string, range, or number into a single float price.
    Examples:
      '183' -> 183.0
      '467-468' -> 467.5 (midpoint)
      '4.5-4.7' -> 4.6
      '@ 81' -> 81.0
      461.9 -> 461.9
    """
    if price_val is None:
        return None
    if isinstance(price_val, (int, float)):
        try:
            val = float(price_val)
            return val if val > 0 else None
        except (ValueError, TypeError):
            return None
    
    s = str(price_val).strip()
    # Extract numeric portions (integers or decimals)
    nums = re.findall(r'\d+(?:\.\d+)?', s)
    if not nums:
        return None
    try:
        if len(nums) == 1:
            return float(nums[0])
        # If range like 467-468 or 4.5-4.7, return average of the first two numbers
        return (float(nums[0]) + float(nums[1])) / 2.0
    except (ValueError, TypeError):
        return None

# Known market indices for derivatives
KNOWN_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "NIFTYIT", "NIFTY50", "NIFTYBANK", "CNXNIFTY", "CNXIT", "CNXFINANCE"
}

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
        ot = getattr(leg.get("schema", None), "option_type", None) or leg.get("option_type") or "CE"
        action_types.append(str(at).upper())
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



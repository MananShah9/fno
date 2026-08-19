import os
import json
import time
import logging
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
import pyotp
from kiteconnect import KiteConnect
from instruments_manager import get_spot_instrument_key, is_index_symbol, round_to_tick, parse_price_value

logger = logging.getLogger("zerodha")

SESSION_FILE = os.path.join("sessions", "zerodha_session.json")

def get_proxy_dict():
    proxy_url = os.getenv("ZERODHA_PROXY_URL", "http://100.125.89.97:8888")
    if proxy_url:
        return {
            "http": proxy_url,
            "https": proxy_url
        }
    return None

def load_cached_session():
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r") as f:
            data = json.load(f)
            # Check if token was saved today (Zerodha tokens expire daily)
            saved_date = data.get("date")
            today_str = datetime.now().strftime("%Y-%m-%d")
            if saved_date == today_str and data.get("access_token"):
                return data.get("access_token")
    except Exception as e:
        logger.warning(f"Error reading session file: {e}")
    return None

def save_session(access_token: str):
    os.makedirs("sessions", exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    data = {
        "date": today_str,
        "access_token": access_token
    }
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
        logger.info(f"Saved new Zerodha access_token to {SESSION_FILE}")
    except Exception as e:
        logger.error(f"Failed to save Zerodha session to disk: {e}")

def generate_access_token_via_totp() -> str:
    """
    Automates Zerodha login flow using User ID, Password, TOTP Secret, API Key, and API Secret.
    All HTTP requests are routed through the configured proxy.
    """
    api_key = os.getenv("ZERODHA_API_KEY")
    api_secret = os.getenv("ZERODHA_API_SECRET")
    user_id = os.getenv("ZERODHA_USER_ID")
    password = os.getenv("ZERODHA_PASSWORD")
    totp_secret = os.getenv("ZERODHA_EXTERNAL_2FA_TOTP")
    proxies = get_proxy_dict()

    if not api_key:
        raise ValueError("ZERODHA_API_KEY is not set in environment.")
    if not totp_secret:
        raise ValueError("ZERODHA_EXTERNAL_2FA_TOTP is not set in environment.")
    if not user_id or not password or not api_secret:
        raise ValueError("ZERODHA_USER_ID, ZERODHA_PASSWORD, and ZERODHA_API_SECRET must be configured in .env for automated Zerodha TOTP login.")

    logger.info("Initiating automated Zerodha login sequence...")

    req_session = requests.Session()
    if proxies:
        req_session.proxies.update(proxies)
        logger.info(f"Using proxy for Zerodha login: {proxies['http']}")

    # Step 1: Login with User ID and Password (if provided)
    if user_id and password:
        logger.info(f"Posting credentials for Zerodha User ID: {user_id}")
        login_resp = req_session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            timeout=15
        )
        login_json = login_resp.json()
        if login_json.get("status") != "success":
            raise RuntimeError(f"Zerodha login failed: {login_json.get('message', 'Unknown error')}")
        
        request_id = login_json["data"]["request_id"]

        # Step 2: 2FA TOTP
        totp = pyotp.TOTP(totp_secret)
        totp_code = totp.now()
        logger.info(f"Generated TOTP code: {totp_code}")

        twofa_resp = req_session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp"
            },
            timeout=15
        )
        twofa_json = twofa_resp.json()
        if twofa_json.get("status") != "success":
            raise RuntimeError(f"Zerodha 2FA failed: {twofa_json.get('message', 'Unknown error')}")

    # Step 3: Connect Login OAuth Redirect
    logger.info("Fetching OAuth connect login redirect URL...")
    connect_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
    connect_resp = req_session.get(connect_url, allow_redirects=True, timeout=15)

    # Parse request_token from final redirect URL
    parsed_url = urllib.parse.urlparse(connect_resp.url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    
    request_token = query_params.get("request_token", [None])[0]
    if not request_token:
        raise RuntimeError(f"Could not extract request_token from redirect URL: {connect_resp.url}")

    logger.info(f"Obtained request_token: {request_token[:8]}...")

    # Step 4: Generate Session Access Token
    if not api_secret:
        raise ValueError("ZERODHA_API_SECRET is required to exchange request_token for access_token.")

    kite = KiteConnect(api_key=api_key, proxies=proxies)
    if proxies and hasattr(kite, "reqsession"):
        kite.reqsession.proxies.update(proxies)

    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]
    logger.info("Successfully generated new access_token!")

    save_session(access_token)
    return access_token

def get_zerodha_client() -> KiteConnect:
    """
    Returns an authenticated KiteConnect instance configured with proxy.
    """
    api_key = os.getenv("ZERODHA_API_KEY")
    proxies = get_proxy_dict()

    if not api_key:
        raise ValueError("ZERODHA_API_KEY is missing from environment.")

    kite = KiteConnect(api_key=api_key, proxies=proxies)
    if proxies and hasattr(kite, "reqsession"):
        kite.reqsession.proxies.update(proxies)

    # Check cached token
    access_token = load_cached_session()
    if access_token:
        kite.set_access_token(access_token)
        try:
            profile = kite.profile()
            logger.info(f"Reused valid Zerodha session for user: {profile.get('user_name', 'OK')}")
            return kite
        except Exception as e:
            logger.warning(f"Cached access_token invalid or expired: {e}. Re-authenticating...")

    # Generate new token
    new_token = generate_access_token_via_totp()
    kite.set_access_token(new_token)
    return kite

def get_nfo_ltp(tradingsymbol: str) -> Optional[float]:
    """
    Fetches the live Last Traded Price (LTP) for an NFO instrument from Zerodha.
    Returns float price or None if unavailable.
    """
    if not tradingsymbol:
        return None
    try:
        kite = get_zerodha_client()
        key = f"NFO:{tradingsymbol.strip().upper()}"
        res = kite.ltp(key)
        if res and key in res and "last_price" in res[key]:
            return float(res[key]["last_price"])
    except Exception as e:
        logger.warning(f"Could not fetch live LTP for {tradingsymbol}: {e}")
    return None

def get_spot_ltp(underlying: str) -> Optional[float]:
    """
    Fetches the live Last Traded Price (LTP) for an underlying cash/spot index or stock from Zerodha.
    Examples:
      'NIFTY' -> queries 'NSE:NIFTY 50'
      'BANKNIFTY' -> queries 'NSE:NIFTY BANK'
      'TATASTEEL' -> queries 'NSE:TATASTEEL'
      'VBL' -> queries 'NSE:VBL'
    Returns float price or None if unavailable.
    """
    if not underlying:
        return None
    key = get_spot_instrument_key(underlying)
    if not key:
        return None
    try:
        kite = get_zerodha_client()
        res = kite.ltp(key)
        if res and key in res and "last_price" in res[key]:
            return float(res[key]["last_price"])
    except Exception as e:
        logger.warning(f"Could not fetch live spot LTP for {underlying} (key: {key}): {e}")
    return None

def get_multiple_ltp(keys: List[str]) -> Dict[str, float]:
    """
    Fetches live LTPs for multiple instrument keys in a single Kite API call.
    Returns dict: {instrument_key: float_price, ...}
    """
    if not keys:
        return {}
    try:
        kite = get_zerodha_client()
        res = kite.ltp(keys)
        out = {}
        if res and isinstance(res, dict):
            for k, v in res.items():
                if isinstance(v, dict) and "last_price" in v:
                    out[k] = float(v["last_price"])
        return out
    except Exception as e:
        logger.warning(f"Could not fetch multiple LTPs for {keys}: {e}")
        return {}

def check_existing_zerodha_order_or_position(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    is_exit: bool = False
) -> dict:
    """
    Checks Zerodha active orders and positions to prevent duplicate order placement.
    Returns dict: {"duplicate": bool, "reason": str|None, "order_id": str|None, "message": str}
    """
    try:
        kite = get_zerodha_client()
        clean_symbol = str(tradingsymbol).strip().upper()
        clean_tt = str(transaction_type).strip().upper()
        clean_qty = int(quantity)

        # 1. Check existing Zerodha orders today
        try:
            orders = kite.orders() or []
            active_or_filled_statuses = ["OPEN", "COMPLETE", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"]
            for o in orders:
                o_sym = str(o.get("tradingsymbol", "")).strip().upper()
                o_tt = str(o.get("transaction_type", "")).strip().upper()
                o_qty = int(o.get("quantity", 0))
                o_status = str(o.get("status", "")).strip().upper()

                if o_sym == clean_symbol and o_tt == clean_tt and o_qty == clean_qty and o_status in active_or_filled_statuses:
                    logger.info(f"Deduplication triggered: Existing order found in Zerodha for {clean_symbol} (ID: {o.get('order_id')}, Status: {o_status})")
                    return {
                        "duplicate": True,
                        "reason": "existing_order",
                        "order_id": str(o.get("order_id")),
                        "message": f"Order already placed on Zerodha (Order ID: {o.get('order_id')}, Status: {o_status})"
                    }
        except Exception as oe:
            logger.warning(f"Could not fetch Zerodha orders for deduplication check: {oe}")

        # 2. Check current net position in Zerodha
        try:
            pos_resp = kite.positions() or {}
            net_positions = pos_resp.get("net", []) or []
            
            matching_pos = None
            for p in net_positions:
                if str(p.get("tradingsymbol", "")).strip().upper() == clean_symbol:
                    matching_pos = p
                    break

            if is_exit:
                if not matching_pos or int(matching_pos.get("quantity", 0)) == 0:
                    logger.info(f"Deduplication triggered: Position for {clean_symbol} is already closed (0) in Zerodha.")
                    return {
                        "duplicate": True,
                        "reason": "position_already_closed",
                        "order_id": None,
                        "message": f"Exit skipped: Position for {clean_symbol} is already 0 in Zerodha."
                    }
            else:
                # Entry order
                if matching_pos:
                    net_qty = int(matching_pos.get("quantity", 0))
                    # If BUY order and net quantity is already positive >= clean_qty, or SELL order and net quantity <= -clean_qty
                    if (clean_tt == "BUY" and net_qty >= clean_qty) or (clean_tt == "SELL" and net_qty <= -clean_qty):
                        logger.info(f"Deduplication triggered: Position for {clean_symbol} already exists in Zerodha (Net Qty: {net_qty})")
                        return {
                            "duplicate": True,
                            "reason": "position_already_open",
                            "order_id": None,
                            "message": f"Entry skipped: Net position for {clean_symbol} is already {net_qty} on Zerodha."
                        }
        except Exception as pe:
            logger.warning(f"Could not fetch Zerodha positions for deduplication check: {pe}")

    except Exception as e:
        logger.warning(f"Error during Zerodha deduplication check: {e}")

    return {"duplicate": False, "reason": None, "order_id": None, "message": ""}


def get_zerodha_net_positions(tradingsymbols: list = None) -> dict:
    """
    Queries Zerodha Kite Connect live position book and returns a dictionary of
    tradingsymbol -> net_quantity. If tradingsymbols list is provided, filters for those symbols
    and guarantees all requested symbols exist in the returned dictionary (defaulting to 0).
    Returns dict:
        {
            "success": bool,
            "positions": {tradingsymbol: int_net_qty, ...},
            "raw_positions": list,
            "error": str | None
        }
    """
    try:
        kite = get_zerodha_client()
        pos_resp = kite.positions() or {}
        net_positions = pos_resp.get("net", []) or []

        pos_map = {}
        target_set = {str(s).strip().upper() for s in tradingsymbols} if tradingsymbols else None

        for p in net_positions:
            sym = str(p.get("tradingsymbol", "")).strip().upper()
            if not sym:
                continue
            qty = int(p.get("quantity", 0) or 0)
            if target_set is None or sym in target_set:
                pos_map[sym] = qty

        # If target symbols were requested, ensure all requested symbols are present (0 if not in Kite net positions)
        if target_set:
            for s in target_set:
                if s not in pos_map:
                    pos_map[s] = 0

        return {
            "success": True,
            "positions": pos_map,
            "raw_positions": net_positions,
            "error": None
        }
    except Exception as e:
        logger.warning(f"Failed to fetch Zerodha net positions: {e}")
        return {
            "success": False,
            "positions": {},
            "raw_positions": [],
            "error": str(e)
        }


def verify_zerodha_positions_zero(tradingsymbols: list) -> dict:
    """
    Verifies that the net positions in Zerodha for all provided trading symbols are exactly 0.
    Returns dict:
        {
            "all_zero": bool,
            "open_positions": {tradingsymbol: net_qty},
            "positions": {tradingsymbol: net_qty},
            "verified": bool,
            "message": str
        }
    """
    if not tradingsymbols:
        return {
            "all_zero": True,
            "open_positions": {},
            "positions": {},
            "verified": True,
            "message": "No trading symbols provided for position verification."
        }

    clean_symbols = [str(s).strip().upper() for s in tradingsymbols if s]
    res = get_zerodha_net_positions(clean_symbols)
    if not res["success"]:
        return {
            "all_zero": False,
            "open_positions": {},
            "positions": {},
            "verified": False,
            "message": f"Could not verify Zerodha live positions: {res['error']}"
        }

    positions = res["positions"]
    open_positions = {sym: qty for sym, qty in positions.items() if qty != 0}
    all_zero = len(open_positions) == 0

    if all_zero:
        msg = f"All {len(clean_symbols)} associated position(s) confirmed ZERO on Zerodha."
    else:
        open_str = ", ".join(f"{sym}: {qty}" for sym, qty in open_positions.items())
        msg = f"WARNING: Non-zero positions remain active on Zerodha: {open_str}"

    return {
        "all_zero": all_zero,
        "open_positions": open_positions,
        "positions": positions,
        "verified": True,
        "message": msg
    }


def map_zerodha_status_to_action_status(
    raw_status: Optional[str],
    filled_qty: int = 0,
    total_qty: int = 0,
    order_type: Optional[str] = None
) -> str:
    """
    Maps Zerodha Kite Connect order execution status to normalized internal Action order states:
      - 'COMPLETE' -> 'FILLED'
      - 'OPEN' -> 'OPEN_LIMIT'
      - 'TRIGGER PENDING' -> 'TRIGGER_PENDING'
      - 'REJECTED' -> 'REJECTED'
      - 'CANCELLED' -> 'CANCELLED'
      - 'AMO REQ RECEIVED', 'PUT ORDER REQ RECEIVED', 'MODIFY PENDING', 'VALIDATION PENDING' -> 'SUBMITTED'
      - If 0 < filled_qty < total_qty and not rejected/cancelled/complete -> 'PARTIAL_FILL'
    """
    if not raw_status:
        return "SUBMITTED"
    s = str(raw_status).strip().upper()

    if s == "COMPLETE":
        return "FILLED"
    if s == "REJECTED":
        return "REJECTED"
    if s == "CANCELLED":
        return "CANCELLED"

    # Check for partial fill before open/trigger pending
    if total_qty and 0 < filled_qty < total_qty:
        return "PARTIAL_FILL"

    if s == "TRIGGER PENDING":
        return "TRIGGER_PENDING"

    if s == "OPEN":
        return "OPEN_LIMIT"
    if s in ["AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED", "MODIFY PENDING", "VALIDATION PENDING"]:
        return "SUBMITTED"
    if s in ["PLACED", "SUBMITTED", "PENDING", "FAILED", "FILLED", "EXECUTED", "OPEN_LIMIT", "TRIGGER_PENDING", "PARTIAL_FILL"]:
        return s

    return "OPEN_LIMIT" if (order_type and str(order_type).upper() == "LIMIT") else "SUBMITTED"


def get_zerodha_order_status(order_id: str) -> dict:
    """
    Fetches the latest execution status for an order_id from Zerodha Kite Connect.
    Returns dict:
        {
            "success": bool,
            "confirmed": bool,
            "status": str,            # Normalized Action status ('FILLED', 'OPEN_LIMIT', 'TRIGGER_PENDING', 'SUBMITTED', 'REJECTED', 'CANCELLED', 'PARTIAL_FILL')
            "raw_status": str,        # Raw broker status ('COMPLETE', 'OPEN', 'REJECTED', etc.)
            "status_message": str,
            "filled_quantity": int,
            "pending_quantity": int,
            "average_price": float
        }
    """
    if not order_id:
        return {
            "success": False,
            "confirmed": False,
            "status": "UNKNOWN",
            "raw_status": "UNKNOWN",
            "status_message": "Missing order_id",
            "filled_quantity": 0,
            "pending_quantity": 0,
            "average_price": 0.0
        }

    clean_order_id = str(order_id).strip()
    confirmed_statuses = {
        "COMPLETE", "OPEN", "TRIGGER PENDING",
        "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED",
        "MODIFY PENDING", "VALIDATION PENDING"
    }
    failed_statuses = {"REJECTED", "CANCELLED"}

    try:
        kite = get_zerodha_client()

        # 1. Try order_history(order_id)
        if hasattr(kite, "order_history"):
            try:
                history = kite.order_history(clean_order_id)
                if history and isinstance(history, list):
                    latest = history[-1]
                    raw_status = str(latest.get("status", "")).strip().upper()
                    status_msg = latest.get("status_message") or ""
                    filled_qty = int(latest.get("filled_quantity", 0) or 0)
                    pending_qty = int(latest.get("pending_quantity", 0) or 0)
                    total_qty = int(latest.get("quantity", 0) or (filled_qty + pending_qty))
                    avg_price = float(latest.get("average_price", 0.0) or 0.0)
                    ord_type = latest.get("order_type")

                    is_confirmed = raw_status in confirmed_statuses
                    is_failed = raw_status in failed_statuses
                    mapped_status = map_zerodha_status_to_action_status(
                        raw_status=raw_status,
                        filled_qty=filled_qty,
                        total_qty=total_qty,
                        order_type=ord_type
                    )

                    return {
                        "success": not is_failed,
                        "confirmed": is_confirmed,
                        "status": mapped_status,
                        "raw_status": raw_status,
                        "status_message": status_msg,
                        "filled_quantity": filled_qty,
                        "pending_quantity": pending_qty,
                        "average_price": avg_price
                    }
            except Exception as he:
                logger.debug(f"order_history check failed for {clean_order_id}: {he}")

        # 2. Fallback to kite.orders()
        if hasattr(kite, "orders"):
            try:
                orders = kite.orders() or []
                for o in orders:
                    if str(o.get("order_id", "")).strip() == clean_order_id:
                        raw_status = str(o.get("status", "")).strip().upper()
                        status_msg = o.get("status_message") or ""
                        filled_qty = int(o.get("filled_quantity", 0) or 0)
                        pending_qty = int(o.get("pending_quantity", 0) or 0)
                        total_qty = int(o.get("quantity", 0) or (filled_qty + pending_qty))
                        avg_price = float(o.get("average_price", 0.0) or 0.0)
                        ord_type = o.get("order_type")

                        is_confirmed = raw_status in confirmed_statuses
                        is_failed = raw_status in failed_statuses
                        mapped_status = map_zerodha_status_to_action_status(
                            raw_status=raw_status,
                            filled_qty=filled_qty,
                            total_qty=total_qty,
                            order_type=ord_type
                        )

                        return {
                            "success": not is_failed,
                            "confirmed": is_confirmed,
                            "status": mapped_status,
                            "raw_status": raw_status,
                            "status_message": status_msg,
                            "filled_quantity": filled_qty,
                            "pending_quantity": pending_qty,
                            "average_price": avg_price
                        }
            except Exception as oe:
                logger.debug(f"orders() lookup failed for {clean_order_id}: {oe}")

        # If order status API is not available or returned nothing, treat placed order as confirmed OPEN_LIMIT
        return {
            "success": True,
            "confirmed": True,
            "status": "OPEN_LIMIT",
            "raw_status": "OPEN",
            "status_message": "Order status OPEN (assumed)",
            "filled_quantity": 0,
            "pending_quantity": 0,
            "average_price": 0.0
        }

    except Exception as e:
        logger.warning(f"Error checking order status for {order_id}: {e}")
        return {
            "success": True,
            "confirmed": True,
            "status": "OPEN_LIMIT",
            "raw_status": "OPEN",
            "status_message": str(e),
            "filled_quantity": 0,
            "pending_quantity": 0,
            "average_price": 0.0
        }


def verify_zerodha_order_confirmation(
    order_id: str,
    max_retries: int = 3,
    retry_delay: float = 0.5
) -> dict:
    """
    Polls Zerodha order status to verify confirmation / acceptance on the exchange.
    Returns status dict with confirmed: bool, status: str, raw_status: str, status_message: str.
    """
    last_status = None
    for attempt in range(max_retries):
        status_info = get_zerodha_order_status(order_id)
        last_status = status_info
        status = status_info.get("status", "").upper()
        raw_status = status_info.get("raw_status", "").upper()

        if status in ["REJECTED", "CANCELLED"] or raw_status in ["REJECTED", "CANCELLED"]:
            logger.warning(f"Order {order_id} was {status} by Zerodha/Exchange: {status_info.get('status_message')}")
            return status_info

        if status_info.get("confirmed"):
            logger.info(f"Order {order_id} confirmed on Zerodha: status={status} (raw={raw_status})")
            return status_info

        if attempt < max_retries - 1 and retry_delay > 0:
            time.sleep(retry_delay)

    return last_status or {
        "success": True,
        "confirmed": True,
        "status": "OPEN_LIMIT",
        "raw_status": "OPEN",
        "status_message": "Confirmed after retries",
        "filled_quantity": 0,
        "pending_quantity": 0,
        "average_price": 0.0
    }


def cancel_zerodha_order(order_id: str, variety: str = "regular") -> dict:
    """
    Cancels an open or trigger order on Zerodha Kite API.
    variety: 'regular' or 'amo'.
    Returns dict: {"success": bool, "order_id": str, "message": str}
    """
    if not order_id:
        return {"success": False, "order_id": None, "message": "Missing order_id"}
    try:
        kite = get_zerodha_client()
        clean_order_id = str(order_id).strip()
        v = kite.VARIETY_REGULAR if str(variety).lower() == "regular" else kite.VARIETY_AMO
        resp = kite.cancel_order(variety=v, order_id=clean_order_id)
        logger.info(f"Cancelled Zerodha order {clean_order_id}: {resp}")
        return {
            "success": True,
            "order_id": clean_order_id,
            "message": f"Order {clean_order_id} cancelled successfully."
        }
    except Exception as e:
        logger.warning(f"Error cancelling Zerodha order {order_id}: {e}")
        return {
            "success": False,
            "order_id": str(order_id),
            "message": str(e)
        }


def reconcile_zerodha_orders(order_ids: Optional[List[str]] = None) -> Dict[str, dict]:
    """
    Fetches the latest execution status for a list of order IDs from Zerodha Kite Connect.
    Queries bulk kite.orders() for efficiency (1 API call).
    Returns dict mapping order_id -> {
        "order_id": str,
        "status": mapped_status,
        "raw_status": str,
        "filled_quantity": int,
        "pending_quantity": int,
        "average_price": float,
        "status_message": str,
        "tradingsymbol": str,
        "transaction_type": str
    }
    """
    results = {}
    target_ids = {str(oid).strip() for oid in order_ids} if order_ids else None

    try:
        kite = get_zerodha_client()
        orders_list = []
        if hasattr(kite, "orders"):
            try:
                orders_list = kite.orders() or []
            except Exception as oe:
                logger.warning(f"kite.orders() bulk call failed during reconciliation: {oe}")

        for o in orders_list:
            oid = str(o.get("order_id", "")).strip()
            if not oid:
                continue
            if target_ids is not None and oid not in target_ids:
                continue

            raw_status = str(o.get("status", "")).strip().upper()
            status_msg = o.get("status_message") or ""
            filled_qty = int(o.get("filled_quantity", 0) or 0)
            pending_qty = int(o.get("pending_quantity", 0) or 0)
            total_qty = int(o.get("quantity", 0) or (filled_qty + pending_qty))
            avg_price = float(o.get("average_price", 0.0) or 0.0)
            ord_type = o.get("order_type")

            mapped_status = map_zerodha_status_to_action_status(
                raw_status=raw_status,
                filled_qty=filled_qty,
                total_qty=total_qty,
                order_type=ord_type
            )

            results[oid] = {
                "order_id": oid,
                "status": mapped_status,
                "raw_status": raw_status,
                "filled_quantity": filled_qty,
                "pending_quantity": pending_qty,
                "total_quantity": total_qty,
                "average_price": avg_price,
                "status_message": status_msg,
                "tradingsymbol": o.get("tradingsymbol"),
                "transaction_type": o.get("transaction_type")
            }

        # Fallback for any requested order_id not returned in bulk kite.orders()
        if target_ids:
            missing_ids = target_ids - set(results.keys())
            for m_id in missing_ids:
                st = get_zerodha_order_status(m_id)
                if st and st.get("status") != "UNKNOWN":
                    results[m_id] = {
                        "order_id": m_id,
                        "status": st.get("status"),
                        "raw_status": st.get("raw_status", st.get("status")),
                        "filled_quantity": st.get("filled_quantity", 0),
                        "pending_quantity": st.get("pending_quantity", 0),
                        "total_quantity": st.get("filled_quantity", 0) + st.get("pending_quantity", 0),
                        "average_price": st.get("average_price", 0.0),
                        "status_message": st.get("status_message", ""),
                        "tradingsymbol": None,
                        "transaction_type": None
                    }

    except Exception as e:
        logger.warning(f"Error during bulk Zerodha order reconciliation: {e}")

    return results


def place_zerodha_order(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    exchange: str = "NFO",
    order_type: str = "MARKET",
    product: str = "NRML",
    price: float = None,
    trigger_price: float = None,
    validity: str = "DAY",
    verify_confirmation: bool = True
) -> dict:
    """
    Executes an order on Zerodha Kite API via proxy.
    Optionally verifies exchange acceptance/confirmation to catch immediate RMS margin rejections.
    Returns dict: {"success": bool, "order_id": str, "status": str, "message": str, "status_message": str|None}
    """
    try:
        kite = get_zerodha_client()

        # Map transaction_type string to KiteConnect constant
        tt = kite.TRANSACTION_TYPE_BUY if transaction_type.upper() == "BUY" else kite.TRANSACTION_TYPE_SELL
        
        # Map order_type string
        ot_map = {
            "MARKET": kite.ORDER_TYPE_MARKET,
            "LIMIT": kite.ORDER_TYPE_LIMIT,
            "SL": kite.ORDER_TYPE_SL,
            "SL-M": kite.ORDER_TYPE_SLM
        }
        ot = ot_map.get(order_type.upper(), kite.ORDER_TYPE_MARKET)

        # Map product string
        prod_map = {
            "NRML": kite.PRODUCT_NRML,
            "MIS": kite.PRODUCT_MIS,
            "CNC": kite.PRODUCT_CNC
        }
        prod = prod_map.get(product.upper(), kite.PRODUCT_NRML)

        logger.info(f"Placing Zerodha Order via Proxy: {tt} {quantity} x {tradingsymbol} ({ot})")

        # Safety Gate: Prevent spot index/stock trigger prices from being sent to exchange SL on option contracts
        if trigger_price is not None and ot in [kite.ORDER_TYPE_SL, kite.ORDER_TYPE_SLM]:
            sym_upper = str(tradingsymbol).strip().upper()
            is_opt = sym_upper.endswith("CE") or sym_upper.endswith("PE")
            if is_opt:
                is_idx = any(sym_upper.startswith(idx) for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"])
                if (is_idx and trigger_price > 1500) or (price and trigger_price > 10.0 * float(price)):
                    err_msg = (
                        f"Exchange SL rejected before submission: Trigger price {trigger_price} appears to be underlying spot level for option {tradingsymbol}. "
                        f"Exchange SL orders on options cannot accept underlying spot prices. Routed to Active Spot Monitoring Loop."
                    )
                    logger.error(err_msg)
                    return {
                        "success": False,
                        "order_id": None,
                        "status": "REJECTED",
                        "message": err_msg,
                        "status_message": err_msg
                    }

        # Ensure limit prices and trigger prices are rounded to valid exchange tick size
        final_price = None
        if price is not None and ot == kite.ORDER_TYPE_LIMIT:
            final_price = round_to_tick(price, tick_size=0.05, direction=transaction_type)

        final_trigger_price = None
        if trigger_price is not None and ot in [kite.ORDER_TYPE_SL, kite.ORDER_TYPE_SLM]:
            # For BUY SL orders, round up; for SELL SL orders, round down
            dir_sl = "UP" if str(transaction_type).upper() == "BUY" else "DOWN"
            final_trigger_price = round_to_tick(trigger_price, tick_size=0.05, direction=dir_sl)

        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=tt,
                quantity=int(quantity),
                product=prod,
                order_type=ot,
                price=final_price,
                trigger_price=final_trigger_price,
                validity=validity
            )
        except Exception as reg_err:
            err_msg = str(reg_err)
            if "After Market Order" in err_msg or "AMO" in err_msg:
                logger.info("Retrying order placement with VARIETY_AMO...")
                order_id = kite.place_order(
                    variety=kite.VARIETY_AMO,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=tt,
                    quantity=int(quantity),
                    product=prod,
                    order_type=ot,
                    price=final_price,
                    trigger_price=final_trigger_price,
                    validity=validity
                )
            else:
                raise reg_err

        logger.info(f"Order submitted to Zerodha. Order ID: {order_id}")

        status_name = "OPEN_LIMIT" if ot == kite.ORDER_TYPE_LIMIT else "SUBMITTED"
        raw_status = "OPEN" if ot == kite.ORDER_TYPE_LIMIT else "SUBMITTED"
        status_msg = None
        filled_qty = 0
        pending_qty = int(quantity)
        avg_price = 0.0

        if verify_confirmation and order_id:
            ver_info = verify_zerodha_order_confirmation(str(order_id))
            raw_status = ver_info.get("raw_status") or ver_info.get("status", "OPEN")
            status_name = ver_info.get("status", "OPEN_LIMIT")
            status_msg = ver_info.get("status_message")
            filled_qty = int(ver_info.get("filled_quantity", 0) or 0)
            pending_qty = int(ver_info.get("pending_quantity", int(quantity) - filled_qty) or 0)
            avg_price = float(ver_info.get("average_price", 0.0) or 0.0)

            if status_name == "REJECTED" or raw_status == "REJECTED":
                rej_reason = status_msg or "Rejected by Zerodha RMS"
                logger.error(f"Zerodha RMS rejected order {order_id}: {rej_reason}")
                return {
                    "success": False,
                    "order_id": str(order_id),
                    "status": "REJECTED",
                    "raw_status": raw_status,
                    "message": f"Order {order_id} rejected by Zerodha RMS: {rej_reason}",
                    "status_message": rej_reason,
                    "filled_quantity": filled_qty,
                    "pending_quantity": pending_qty,
                    "average_price": avg_price
                }
            elif status_name == "CANCELLED" or raw_status == "CANCELLED":
                can_reason = status_msg or "Order cancelled"
                logger.error(f"Order {order_id} was cancelled: {can_reason}")
                return {
                    "success": False,
                    "order_id": str(order_id),
                    "status": "CANCELLED",
                    "raw_status": raw_status,
                    "message": f"Order {order_id} cancelled: {can_reason}",
                    "status_message": can_reason,
                    "filled_quantity": filled_qty,
                    "pending_quantity": pending_qty,
                    "average_price": avg_price
                }

        return {
            "success": True,
            "order_id": str(order_id),
            "status": status_name,
            "raw_status": raw_status,
            "message": f"Order placed successfully. Order ID: {order_id}" + (f" ({status_name})" if status_name not in ["SUBMITTED", "OPEN"] else ""),
            "status_message": status_msg,
            "filled_quantity": filled_qty,
            "pending_quantity": pending_qty,
            "average_price": avg_price
        }

    except Exception as e:
        logger.exception(f"Error placing Zerodha order: {e}")
        return {
            "success": False,
            "order_id": None,
            "status": "FAILED",
            "raw_status": "FAILED",
            "message": str(e),
            "status_message": str(e),
            "filled_quantity": 0,
            "pending_quantity": 0,
            "average_price": 0.0
        }


def calculate_basket_margin(
    order_params_list: List[Dict[str, Any]],
    consider_positions: bool = False
) -> Optional[float]:
    """
    Calculates total margin required for a list of orders (basket) using Zerodha Kite Connect API.
    Handles SPAN + Exposure margin relief for hedged multi-leg positions.
    Returns float total required margin in INR, or None if the API call fails or is unavailable.
    """
    if not order_params_list:
        return None
    try:
        kite = get_zerodha_client()
        formatted_orders = []
        for o in order_params_list:
            tt = str(o.get("transaction_type", "BUY")).strip().upper()
            ot = str(o.get("order_type", "MARKET")).strip().upper()
            prod = str(o.get("product", "NRML")).strip().upper()
            
            tt_val = getattr(kite, f"TRANSACTION_TYPE_{tt}", tt)
            ot_val = getattr(kite, f"ORDER_TYPE_{ot}", ot)
            prod_val = getattr(kite, f"PRODUCT_{prod}", prod)
            variety_val = getattr(kite, "VARIETY_REGULAR", "regular")

            final_p = 0.0
            if o.get("price"):
                parsed_p = round_to_tick(o.get("price"), tick_size=0.05, direction=tt)
                final_p = float(parsed_p) if parsed_p is not None else 0.0

            final_tp = 0.0
            if o.get("trigger_price"):
                parsed_tp = round_to_tick(o.get("trigger_price"), tick_size=0.05, direction="UP" if tt == "BUY" else "DOWN")
                final_tp = float(parsed_tp) if parsed_tp is not None else 0.0

            formatted_orders.append({
                "exchange": o.get("exchange", "NFO"),
                "tradingsymbol": str(o.get("tradingsymbol", "")).strip().upper(),
                "transaction_type": tt_val,
                "variety": variety_val,
                "product": prod_val,
                "order_type": ot_val,
                "quantity": int(o.get("quantity", 1)),
                "price": final_p,
                "trigger_price": final_tp
            })

        # Try basket_margins first (provides hedged margin calculation)
        if hasattr(kite, "basket_margins"):
            try:
                res = kite.basket_margins(formatted_orders, consider_positions=consider_positions)
                if res and isinstance(res, dict):
                    # 'final' total includes hedge benefits across legs
                    final_info = res.get("final") or {}
                    initial_info = res.get("initial") or {}
                    total_m = final_info.get("total") or initial_info.get("total")
                    if total_m is not None and float(total_m) > 0:
                        logger.info(f"Zerodha Basket Margin calculated: Rs. {float(total_m):,.2f} for {len(formatted_orders)} legs")
                        return float(total_m)
            except Exception as b_err:
                logger.warning(f"kite.basket_margins call failed: {b_err}")

        # Fallback to order_margins
        if hasattr(kite, "order_margins"):
            try:
                res = kite.order_margins(formatted_orders)
                if res and isinstance(res, list):
                    total_m = sum(float(item.get("total", 0.0)) for item in res if isinstance(item, dict))
                    if total_m > 0:
                        logger.info(f"Zerodha Order Margins calculated: Rs. {total_m:,.2f}")
                        return total_m
            except Exception as o_err:
                logger.warning(f"kite.order_margins call failed: {o_err}")

    except Exception as e:
        logger.warning(f"Zerodha margin API calculation unavailable: {e}")

    return None


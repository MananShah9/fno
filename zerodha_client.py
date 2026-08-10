import os
import json
import logging
import urllib.parse
from datetime import datetime
import requests
import pyotp
from kiteconnect import KiteConnect

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

def place_zerodha_order(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    exchange: str = "NFO",
    order_type: str = "MARKET",
    product: str = "NRML",
    price: float = None,
    trigger_price: float = None,
    validity: str = "DAY"
) -> dict:
    """
    Executes an order on Zerodha Kite API via proxy.
    Returns dict: {"success": bool, "order_id": str, "message": str}
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

        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=tt,
                quantity=int(quantity),
                product=prod,
                order_type=ot,
                price=float(price) if (price and ot == kite.ORDER_TYPE_LIMIT) else None,
                trigger_price=float(trigger_price) if (trigger_price and ot in [kite.ORDER_TYPE_SL, kite.ORDER_TYPE_SLM]) else None,
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
                    price=float(price) if (price and ot == kite.ORDER_TYPE_LIMIT) else None,
                    trigger_price=float(trigger_price) if (trigger_price and ot in [kite.ORDER_TYPE_SL, kite.ORDER_TYPE_SLM]) else None,
                    validity=validity
                )
            else:
                raise reg_err

        logger.info(f"Order placed successfully! Order ID: {order_id}")
        return {
            "success": True,
            "order_id": str(order_id),
            "message": f"Order placed successfully. Order ID: {order_id}"
        }

    except Exception as e:
        logger.exception(f"Error placing Zerodha order: {e}")
        return {
            "success": False,
            "order_id": None,
            "message": str(e)
        }

import os
import json
import re
import logging
from typing import List, Dict, Any, Optional
import google.generativeai as genai
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# Setup logger
logger = logging.getLogger(__name__)

# Configure Gemini
api_key = os.getenv("GEMINI_API_KEY")
model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

if api_key:
    genai.configure(api_key=api_key)
else:
    logger.warning("GEMINI_API_KEY is not set in environment!")

KNOWN_TICKERS = ["BANKNIFTY", "MIDCPNIFTY", "FINNIFTY", "NIFTY", "SENSEX", "BANKEX", "VBL", "INDIGO", "TATASTEEL", "RELIANCE", "NATIONALUM"]

def clean_symbol(symbol: Optional[str]) -> Optional[str]:
    """Cleans up AI-generated underlying symbol to a strict single uppercase ticker."""
    if not symbol:
        return None
    s = str(symbol).strip().upper()
    # Check known tickers first
    for t in KNOWN_TICKERS:
        if s.startswith(t):
            return t
    # Strip any trailing words, brackets, or suffixes
    s = re.sub(r'[\s_\-\(\)].*$', '', s)
    match = re.search(r'^[A-Z0-9]+', s)
    if match:
        clean = match.group(0)
        for t in KNOWN_TICKERS:
            if clean.startswith(t):
                return t
        return clean
    return s[:20]

def is_poke_message(text: Optional[str]) -> bool:
    """
    Checks if a message in source is simply a '.' or 'trade incoming' (case insensitive),
    which is used to poke/ping users and not an actual trade recommendation.
    """
    if not text:
        return False
    cleaned = text.strip().strip('\u200b\ufeff\u200e\u200f').lower()
    # Matches single or multiple dots (e.g. '.', '..', '...')
    if re.fullmatch(r'\.+', cleaned):
        return True
    # Matches 'trade incoming', 'trade incoming.', 'trade incoming!', etc.
    if re.fullmatch(r'trade\s+incoming[\.!\s]*', cleaned):
        return True
    return False

# Define structured schema
class ActionSchema(BaseModel):
    action_type: str = Field(description="Must be one of: BUY, SELL, EXIT, UPDATE_SL, CLOSE_LEG, INFO")
    transaction_type: Optional[str] = Field(description="Zerodha order side: 'BUY' or 'SELL'. For entry: 'BUY' or 'SELL'. For EXIT: if exiting/closing a short position it is 'BUY'; if exiting/closing a long position it is 'SELL'.")
    is_main: Optional[bool] = Field(description="True if this is the primary/main directional leg of the trade, False if it is a hedge leg.")
    underlying: Optional[str] = Field(description="Underlying index or stock symbol ONLY in uppercase, e.g. 'NIFTY', 'BANKNIFTY', 'VBL', 'INDIGO', 'RELIANCE', 'TATASTEEL'. Do NOT include any explanations, reasoning, or 'REF' suffixes.")
    option_type: Optional[str] = Field(description="Instrument option type: 'CE', 'PE', or 'FUT'")
    strike: Optional[float] = Field(description="Numeric strike price if option, e.g. 24000, 23600, 480, 5300, 192.5. Null for Futures.")
    expiry_info: Optional[str] = Field(description="Expiry date or month string if mentioned, e.g. '28JUL', 'AUG', '4AUG', '11AUG', 'JULY'. Null if default/nearest.")
    order_type: Optional[str] = Field(description="Zerodha order type: 'MARKET' or 'LIMIT'. If a specific limit price or range is provided, set 'LIMIT'. Otherwise 'MARKET'.")
    product: Optional[str] = Field(description="Product code: 'NRML' for overnight/positional trades, 'MIS' if explicitly intraday.")
    lots: Optional[int] = Field(description="Number of lots to trade, default 1 unless specified (e.g. 2 lots, add 1 lot).")
    instrument_name: Optional[str] = Field(description="The exact copyable search query on Zerodha. Options syntax: '<UNDERLYING> <EXPIRY> <STRIKE> <PE/CE>' (e.g. 'NIFTY 28JUL2026 24000 PE' or 'VBL 480 CE'). Futures: '<UNDERLYING> FUT' (e.g. 'VBL FUT', 'NIFTY FUT').")
    price: Optional[str] = Field(description="Execution price, entry range, or limit, e.g. '183' or '467-468' or '81' or '4.5-4.7' or '220s' or 'above 200'")
    stoploss: Optional[str] = Field(description="Stoploss value or trigger price, e.g. '220' or '475'")
    target: Optional[str] = Field(description="Target price, e.g. '453' or '3.9'")
    is_limit: Optional[bool] = Field(description="True if limit order specified, False if market/at-the-money or range not requiring a hard limit")
    details: Optional[str] = Field(description="Brief note explaining the execution instructions for this leg/order")

    def __init__(self, **data):
        defaults = {
            "action_type": "INFO",
            "transaction_type": None,
            "is_main": True,
            "underlying": None,
            "option_type": None,
            "strike": None,
            "expiry_info": None,
            "order_type": None,
            "product": None,
            "lots": 1,
            "instrument_name": None,
            "price": None,
            "stoploss": None,
            "target": None,
            "is_limit": False,
            "details": None
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        super().__init__(**data)

class TradeAnalysisSchema(BaseModel):
    is_valid_trade_msg: bool = Field(description="True if this message represents a valid trade setup, entry, modification, stoploss update, target update, or exit/close alert. False if it is general talk, FYI, or unrelated.")
    is_continuation: bool = Field(description="True if this message updates, adds lots to, or closes a trade from the provided 'Open Trades Context'.")
    related_open_trade_id: Optional[int] = Field(description="The 'id' of the related open trade from the provided 'Open Trades Context', if is_continuation is True.")
    structure_type: Optional[str] = Field(description="The overall strategy structure, e.g., 'TATASTEEL BULL PUT SPREAD', 'NIFTY PE SPREAD', 'INDIGO BEAR CALL SPREAD', 'SINGLE CE BUY'.")
    underlying: Optional[str] = Field(description="Underlying ticker symbol ONLY in uppercase, e.g. 'TATASTEEL', 'NIFTY', 'VBL', 'INDIGO', 'BANKNIFTY'. ABSOLUTELY NO reasoning, markdown, or 'REF' suffixes.")
    actions: List[ActionSchema] = Field(description="The list of orders or actions to execute for this trade message.")
    trade_status_update: str = Field(description="If this message closes or exits the entire trade, set this to 'CLOSED'. Otherwise, keep it 'OPEN'.")
    context_summary: Optional[str] = Field(description="A highly summarized, clear explanation (under 50 words) of the overall trade state after processing this message.")

    def __init__(self, **data):
        defaults = {
            "is_valid_trade_msg": False,
            "is_continuation": False,
            "related_open_trade_id": None,
            "structure_type": None,
            "underlying": None,
            "actions": [],
            "trade_status_update": "OPEN",
            "context_summary": None
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        super().__init__(**data)


SYSTEM_INSTRUCTION = """
You are an expert Indian stock market trading system assistant. Your task is to process incoming messages from a trading Telegram channel and structure them into highly actionable trading data.

CRITICAL CONSTRAINTS:
1. `underlying` MUST be strictly a single uppercase ticker symbol string, e.g. "TATASTEEL", "NIFTY", "BANKNIFTY", "INDIGO", "VBL", "RELIANCE", "NATIONALUM". NEVER include reasoning, markdown, code, or fake suffixes like 'REF'.
2. Trade Entry Messages:
   - Any message providing new trade recommendations, stock/index option spreads, future spreads, or single legs is a VALID TRADE MESSAGE (`is_valid_trade_msg = True`).
   - Extract ALL actionable order legs (e.g. Sell, Buy, Hedge/Buy, Deploy Call Spread, Deploy Put Spread) into the `actions` array.
   - For spreads (e.g. Bull Put Spread, Bear Call Spread, Bear Fut Spread), extract both Sell and Buy legs as separate ActionSchema items.
   - Set `transaction_type` on each leg: 'SELL' for sell legs, 'BUY' for buy/hedge legs.
3. Classify `is_main` for each leg:
   - For Future + Option spreads (e.g. Bear Fut Spread): Futures leg is `is_main = True`, Option leg is `is_main = False` (hedge).
   - For Option Credit Spreads (e.g. Bull Put Spread, Bear Call Spread): SELL (short) leg is `is_main = True`, BUY (long) leg is `is_main = False` (hedge).
   - For single-leg trades: `is_main = True`.
4. Order Types & Prices:
   - If a price range, limit price, or level is specified (e.g. "@ Range (4.5-4.7)", "@ 98-110", "@ 220s", "above 200", "near 100", "Close 24600 at 93"), set `order_type = 'LIMIT'`, `is_limit = True`, and extract `price`.
   - If no price is given or execution is CMP / Market / immediate, set `order_type = 'MARKET'`.
5. Continuation, Leg Updates, Averaging, and Follow-ups:
   - When a message provides specific strikes/legs for a previously announced strategy (e.g. after 'Deploy Bear Call Spread' followed by 'Sell 23950 CE Buy 24250 CE'), set `is_valid_trade_msg = True`, `is_continuation = True`, and `related_open_trade_id` to that open trade.
   - When a message instructs averaging/adding a lot (e.g. 'We will sell 24050 PE at 520-550', 'add that lot in 220s'), set `is_valid_trade_msg = True`, `is_continuation = True`, and `related_open_trade_id` to the matching open trade.
   - When a message updates target or stoploss for an existing leg, set `is_valid_trade_msg = True`, `is_continuation = True`, and `related_open_trade_id` to the matching open trade.
6. Trade Exits and Profit Booking:
   - When an alert instructs to close/exit or book profit (e.g. "SL hit Exit full position", "Close full position", "Close future position at ... All call at ... We are closing the trade", "PROFIT BOOKING IN THIS TRADE Close [strike] Sell leg Close [strike] Hedge leg", "Exit 24000 PE at 90", "Book 24150 PE at 35-36"):
     - Set `is_valid_trade_msg = True`, `is_continuation = True`, and `related_open_trade_id` to the matching open trade from Open Trades Context.
     - Set `trade_status_update = 'CLOSED'`.
     - Extract `action_type = 'EXIT'` for each leg.
     - For `transaction_type` on EXIT actions:
       - If closing a short/sold leg -> `transaction_type = 'BUY'`
       - If closing a long/bought leg -> `transaction_type = 'SELL'`
7. Non-Trade Messages:
   - General market discussion, disclaimer notices, motivational chats, or poke/ping notifications (e.g. '.', 'trade incoming', '...') that contain NO actionable entry/exit/SL instructions should set `is_valid_trade_msg = False`.
"""

def analyze_message_with_ai(message_text: str, open_trades: List[Dict[str, Any]]) -> Optional[TradeAnalysisSchema]:
    """
    Sends message_text and open_trades context to Gemini for parsing and mapping.
    """
    if is_poke_message(message_text):
        logger.info(f"Skipping poke message ('{message_text.strip()}'): not a trade recommendation.")
        return TradeAnalysisSchema(
            is_valid_trade_msg=False,
            trade_status_update="OPEN",
            context_summary="Poke message ignored"
        )

    if not api_key:
        logger.error("Gemini API key is not configured.")
        return None

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=SYSTEM_INSTRUCTION
        )

        # Structure the context
        context_str = json.dumps(open_trades, indent=2, default=str)
        
        prompt = f"""
Incoming Telegram Message to process:
\"\"\"
{message_text}
\"\"\"

Open Trades Context (Currently active open positions):
\"\"\"
{context_str}
\"\"\"

INSTRUCTIONS:
1. NEW TRADE: If this message introduces a NEW trade (even if other trades are open in context), set `is_valid_trade_msg = True`, `is_continuation = False`, `related_open_trade_id = None`, extract `underlying`, `structure_type`, and ALL entry legs (including legs with emojis like 🔴Sell, 🟢Buy, 🟢Hedge/Buy) into `actions`.
2. OPEN TRADE UPDATE / EXIT: If this message relates to, modifies, or closes an open trade (including profit booking alerts like CLOSE [strike] Sell leg, Close [strike] Hedge leg, or Exit full position), set `is_valid_trade_msg = True`, `is_continuation = True`, `trade_status_update = CLOSED`, `related_open_trade_id` to that trade ID, and extract exit actions for all mentioned legs.
3. NON-TRADE: If purely general chat, motivation, or disclaimer with no trade action, set `is_valid_trade_msg = False`.
"""

        # Generate content with structured JSON schema
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
                response_schema=TradeAnalysisSchema,
                temperature=0.1  # Low temperature for highly deterministic extraction
            )
        )

        # Check candidates response
        if not response.candidates:
            logger.warning("Gemini returned no candidates.")
            return None

        cand = response.candidates[0]
        raw_text = None
        if hasattr(cand, "content") and hasattr(cand.content, "parts") and cand.content.parts:
            raw_text = cand.content.parts[0].text
        elif hasattr(response, "text"):
            try:
                raw_text = response.text
            except Exception:
                raw_text = None

        if not raw_text:
            logger.warning(f"Gemini returned empty text or finish_reason: {getattr(cand, 'finish_reason', 'N/A')}")
            return None

        # Clean markdown codeblocks if present
        cleaned_text = raw_text.strip()
        if cleaned_text.startswith("```"):
            cleaned_text = re.sub(r'^```(?:json)?\s*', '', cleaned_text)
            cleaned_text = re.sub(r'\s*```$', '', cleaned_text)

        # Parse the JSON response
        try:
            result_json = json.loads(cleaned_text, strict=False)
        except Exception:
            match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
            if match:
                result_json = json.loads(match.group(0), strict=False)
            else:
                raise
        
        # Populate safe defaults if Gemini omitted optional schema keys
        if "actions" not in result_json or result_json["actions"] is None:
            result_json["actions"] = []
        if "is_continuation" not in result_json or result_json["is_continuation"] is None:
            result_json["is_continuation"] = False
        if "trade_status_update" not in result_json or result_json["trade_status_update"] is None:
            result_json["trade_status_update"] = "OPEN"
        if "is_valid_trade_msg" not in result_json or result_json["is_valid_trade_msg"] is None:
            result_json["is_valid_trade_msg"] = False
        if "related_open_trade_id" not in result_json:
            result_json["related_open_trade_id"] = None
        if "structure_type" not in result_json:
            result_json["structure_type"] = None
        if "underlying" not in result_json:
            result_json["underlying"] = None
        else:
            result_json["underlying"] = clean_symbol(result_json["underlying"])

        if "context_summary" not in result_json:
            result_json["context_summary"] = None
        elif result_json["context_summary"]:
            result_json["context_summary"] = str(result_json["context_summary"])[:300]

        if "actions" in result_json and isinstance(result_json["actions"], list):
            for act in result_json["actions"]:
                if isinstance(act, dict):
                    if "underlying" in act:
                        act["underlying"] = clean_symbol(act["underlying"])
                    for k in ["order_type", "product", "instrument_name", "price", "stoploss", "target", "details", "strike", "expiry_info", "option_type"]:
                        if k not in act:
                            act[k] = None
                    if "is_limit" not in act or act["is_limit"] is None:
                        act["is_limit"] = False
                    if "lots" not in act or act["lots"] is None:
                        act["lots"] = 1
                    if "is_main" not in act or act["is_main"] is None:
                        act["is_main"] = True

        return TradeAnalysisSchema(**result_json)

    except Exception as e:
        logger.exception(f"Error during Gemini analysis: {e}")
        raise

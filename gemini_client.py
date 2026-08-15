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

def clean_symbol(symbol: Optional[str]) -> Optional[str]:
    """Cleans up AI-generated underlying symbol to a strict single uppercase ticker."""
    if not symbol:
        return None
    s = str(symbol).strip().upper()
    # If symbol contains text like "TATASTEEL league/index..." or "NIFTY strategy...", extract first token
    match = re.search(r'^[A-Z0-9]+', s)
    if match:
        return match.group(0)
    return s[:20]

# Define structured schema
class ActionSchema(BaseModel):
    action_type: str = Field(description="Must be one of: BUY, SELL, EXIT, UPDATE_SL, CLOSE_LEG, INFO")
    underlying: Optional[str] = Field(description="Underlying index or stock symbol ONLY in uppercase, e.g. 'NIFTY', 'BANKNIFTY', 'VBL', 'INDIGO', 'RELIANCE', 'TATASTEEL'. Do NOT include any explanations or reasoning.")
    option_type: Optional[str] = Field(description="Instrument option type: 'CE', 'PE', or 'FUT'")
    strike: Optional[float] = Field(description="Numeric strike price if option, e.g. 24000, 23600, 480, 5300, 192.5. Null for Futures.")
    expiry_info: Optional[str] = Field(description="Expiry date or month string if mentioned, e.g. '28JUL', 'AUG', '4AUG', '11AUG', 'JULY'. Null if default/nearest.")
    order_type: Optional[str] = Field(description="Zerodha order type: 'MARKET' or 'LIMIT'. If a specific limit price or range is provided, set 'LIMIT'. Otherwise 'MARKET'.")
    product: Optional[str] = Field(description="Product code: 'NRML' for overnight/positional trades, 'MIS' if explicitly intraday.")
    lots: Optional[int] = Field(description="Number of lots to trade, default 1 unless specified (e.g. 2 lots, add 1 lot).")
    instrument_name: Optional[str] = Field(description="The exact copyable search query on Zerodha. Options syntax: '<UNDERLYING> <EXPIRY> <STRIKE> <PE/CE>' (e.g. 'NIFTY 28JUL2026 24000 PE' or 'VBL 480 CE'). Futures: '<UNDERLYING> FUT' (e.g. 'VBL FUT', 'NIFTY FUT').")
    price: Optional[str] = Field(description="Execution price, entry range, or limit, e.g. '183' or '467-468' or '81' or '4.5-4.7'")
    stoploss: Optional[str] = Field(description="Stoploss value or trigger price, e.g. '220' or '475'")
    target: Optional[str] = Field(description="Target price, e.g. '453' or '3.9'")
    is_limit: Optional[bool] = Field(description="True if limit order specified, False if market/at-the-money or range not requiring a hard limit")
    details: Optional[str] = Field(description="Brief note explaining the execution instructions for this leg/order")

class TradeAnalysisSchema(BaseModel):
    is_valid_trade_msg: bool = Field(description="True if this message represents a valid trade setup, entry, modification, stoploss update, target update, or exit/close alert. False if it is general talk, FYI, or unrelated.")
    is_continuation: bool = Field(description="True if this message updates or closes a trade from the provided 'Open Trades Context' (e.g. updates stoploss, closes a position, target hit).")
    related_open_trade_id: Optional[int] = Field(description="The 'id' of the related open trade from the provided 'Open Trades Context', if is_continuation is True.")
    structure_type: Optional[str] = Field(description="The overall strategy structure, e.g., 'TATASTEEL BULL PUT SPREAD', 'NIFTY PE SPREAD', 'SINGLE CE BUY'.")
    underlying: Optional[str] = Field(description="Underlying ticker symbol ONLY in uppercase, e.g. 'TATASTEEL', 'NIFTY', 'VBL', 'BANKNIFTY'. ABSOLUTELY NO reasoning or extra text.")
    actions: List[ActionSchema] = Field(description="The list of orders or actions to execute for this trade message.")
    trade_status_update: str = Field(description="If this message closes or exits the entire trade, set this to 'CLOSED'. Otherwise, keep it 'OPEN'.")
    context_summary: Optional[str] = Field(description="A highly summarized, clear explanation (under 50 words) of the overall trade state after processing this message.")


SYSTEM_INSTRUCTION = """
You are an expert Indian stock market trading system assistant. Your task is to process incoming messages from a trading Telegram channel and structure them into highly actionable trading data.

CRITICAL CONSTRAINTS:
1. `underlying` MUST be strictly a single uppercase ticker symbol string, e.g. "TATASTEEL", "NIFTY", "BANKNIFTY", "INDIGO", "VBL", "RELIANCE". NEVER include reasoning, markdown, code, or extra words in `underlying`.
2. Extract ALL actionable order legs into the `actions` array. For spreads (e.g. Bull Put Spread), extract both Sell and Buy legs as separate ActionSchema items.
3. If a price range or limit price is specified (e.g. "@ Range (4.5-4.7) Place limit orders", "Close 24600 at 93"), set `order_type = 'LIMIT'`, `is_limit = True`, and `price` to the price value or range.
4. For trade exit/close alerts (e.g., "SL hit Exit full position", "Close full position", "Exit 24000 PE at 90"):
   - Set `is_valid_trade_msg = True`, `is_continuation = True`, `related_open_trade_id` to the matching trade from Open Trades Context, and `trade_status_update = 'CLOSED'`.
   - If specific exit prices are given for legs, extract `action_type = 'EXIT'` with `order_type = 'LIMIT'` and the `price`.
5. Keep `context_summary` extremely concise (under 50 words).
"""

def analyze_message_with_ai(message_text: str, open_trades: List[Dict[str, Any]]) -> Optional[TradeAnalysisSchema]:
    """
    Sends message_text and open_trades context to Gemini for parsing and mapping.
    """
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

Please analyze the message above against the active open trades and return a highly structured response.
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

        # Parse the JSON response
        result_json = json.loads(response.text)
        
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
                if isinstance(act, dict) and "underlying" in act:
                    act["underlying"] = clean_symbol(act["underlying"])

        return TradeAnalysisSchema(**result_json)

    except Exception as e:
        logger.exception(f"Error during Gemini analysis: {e}")
        return None

import os
import json
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

# Define structured schema
class ActionSchema(BaseModel):
    action_type: str = Field(description="Must be one of: BUY, SELL, EXIT, UPDATE_SL, CLOSE_LEG, INFO")
    instrument_name: Optional[str] = Field(description="The exact copyable search query on Zerodha. Options syntax: '<UNDERLYING> <STRIKE> <PE/CE>' or '<UNDERLYING> <EXPIRY_DATE> <STRIKE> <PE/CE>' (e.g. 'NIFTY 28JUL2026 24000 PE' or 'VBL 480 CE'). Futures: '<UNDERLYING> FUT' (e.g. 'VBL FUT', 'NIFTY FUT').")
    price: Optional[str] = Field(description="Execution price, entry range, or limit, e.g. '183' or '467-468' or '81'")
    stoploss: Optional[str] = Field(description="Stoploss value or trigger price, e.g. '220' or '475'")
    target: Optional[str] = Field(description="Target price, e.g. '453' or '3.9'")
    is_limit: bool = Field(description="True if limit order specified, False if market/at-the-money or range not requiring a hard limit")
    details: Optional[str] = Field(description="Brief note explaining the execution instructions for this leg/order")

class TradeAnalysisSchema(BaseModel):
    is_valid_trade_msg: bool = Field(description="True if this message represents a valid trade setup, entry, modification, stoploss update, target update, or exit/close alert. False if it is general talk, FYI, or unrelated.")
    is_continuation: bool = Field(description="True if this message updates or closes a trade from the provided 'Open Trades Context' (e.g. updates stoploss, closes a position, target hit).")
    related_open_trade_id: Optional[int] = Field(description="The 'id' of the related open trade from the provided 'Open Trades Context', if is_continuation is True.")
    structure_type: Optional[str] = Field(description="The overall strategy structure, e.g., 'NIFTY PE SPREAD', 'VBL BEAR FUT SPREAD', 'SINGLE CE BUY', 'IRON CONDOR'.")
    underlying: Optional[str] = Field(description="Underlying security, e.g., 'NIFTY', 'VBL', 'BANKNIFTY'")
    actions: List[ActionSchema] = Field(description="The list of orders or actions to execute for this trade message.")
    trade_status_update: str = Field(description="If this message closes or exits the entire trade, set this to 'CLOSED'. Otherwise, keep it 'OPEN'.")
    context_summary: Optional[str] = Field(description="A highly summarized, clear explanation of the overall trade state after processing this message (e.g. 'Spread opened: Sell 24000 PE @ 183, Buy 23600 PE @ 81. SL 220'). This will be used as history context for subsequent messages.")


SYSTEM_INSTRUCTION = """
You are an expert Indian stock market trading system assistant. Your task is to process incoming messages from a trading Telegram channel and structure them into highly actionable trading data.

### Trading Types & Conventions:
1. Spreads / Multi-Leg Trades (e.g., Bear Put Spread, Bull Call Spread, Iron Condor, Straddle, Strangle):
   - These are SINGLE trades that contain MULTIPLE legs/orders.
   - For example:
     - BUY NIFTY 23600 PE @ 81
     - SELL NIFTY 24000 PE @ 183
   - Represent these as ONE single trade (TradeAnalysisSchema) containing MULTIPLE actions (ActionSchema).

2. Continuations and Updates:
   - Subsequent messages are often updates on existing open trades (e.g., "SL for entire position in when 24000 PE hits 220", "SL hit Exit the full position", "Close the future position at or below 461.9").
   - You MUST examine the provided 'Open Trades Context' to find which open trade this message refers to.
   - Identify the correct 'related_open_trade_id' and map the actions to it.
   - If a message says "Exit the full position" or "Close the trade", set 'trade_status_update' to 'CLOSED' and add an EXIT action.

3. Zerodha Search Queries:
   - For Zerodha F&O, search terms must be highly copyable. Always optimize for Zerodha search queries.
   - Options standard syntax: `<UNDERLYING_NAME> <EXPIRY_OR_MONTH> <STRIKE> <PE/CE>` (e.g., "NIFTY 21st JUL 24000 PE" or "VBL 480 CE").
   - Futures standard syntax: `<UNDERLYING_NAME> FUT` (e.g. "VBL FUT", "NIFTY FUT").
   - Ensure the `instrument_name` contains the exact search query so that clicking/tapping it on Telegram can be done easily. Keep it clean and direct without extra symbols.

4. Plain Simple Actions:
   - Simplify complex terminology (spreads, condors, etc.) into plain BUY/SELL actions. Do not use financial jargon in the action types; use the direct execution actions.
   - Avoid fluff or unnecessary commentary. Focus on the actionable trade instructions.
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
        return TradeAnalysisSchema(**result_json)

    except Exception as e:
        logger.exception(f"Error during Gemini analysis: {e}")
        return None

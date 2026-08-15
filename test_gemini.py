import os
import unittest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

load_dotenv()

from gemini_client import (
    clean_symbol,
    is_poke_message,
    get_genai_client,
    analyze_message_with_ai,
    TradeAnalysisSchema,
    ActionSchema
)

class TestGeminiClient(unittest.TestCase):
    def test_clean_symbol(self):
        self.assertEqual(clean_symbol("NIFTY"), "NIFTY")
        self.assertEqual(clean_symbol("nifty_ref"), "NIFTY")
        self.assertEqual(clean_symbol("BANKNIFTY (JULY)"), "BANKNIFTY")
        self.assertEqual(clean_symbol("TATASTEEL-EQ"), "TATASTEEL")
        self.assertEqual(clean_symbol("RELIANCE REF"), "RELIANCE")
        self.assertIsNone(clean_symbol(None))
        self.assertIsNone(clean_symbol(""))

    def test_is_poke_message(self):
        self.assertTrue(is_poke_message("."))
        self.assertTrue(is_poke_message("..."))
        self.assertTrue(is_poke_message("trade incoming"))
        self.assertTrue(is_poke_message("Trade Incoming..."))
        self.assertTrue(is_poke_message("  trade incoming!  "))
        self.assertFalse(is_poke_message("BUY NIFTY 24000 CE"))
        self.assertFalse(is_poke_message(None))

    def test_analyze_message_poke(self):
        result = analyze_message_with_ai("...", [])
        self.assertIsNotNone(result)
        self.assertFalse(result.is_valid_trade_msg)
        self.assertEqual(result.context_summary, "Poke message ignored")

    @patch("gemini_client.get_genai_client")
    def test_analyze_message_mock_parsed(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_parsed = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=False,
            related_open_trade_id=None,
            structure_type="NIFTY PE SPREAD",
            underlying="NIFTY",
            actions=[
                ActionSchema(
                    action_type="SELL",
                    transaction_type="SELL",
                    is_main=True,
                    underlying="NIFTY",
                    option_type="PE",
                    strike=24000.0,
                    expiry_info="28JUL2026",
                    order_type="LIMIT",
                    product="NRML",
                    lots=1,
                    instrument_name="NIFTY 28JUL2026 24000 PE",
                    price="183",
                    stoploss="220"
                )
            ],
            trade_status_update="OPEN",
            context_summary="Test summary"
        )

        mock_candidate = MagicMock()
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.parsed = mock_parsed
        mock_response.text = None
        mock_client.models.generate_content.return_value = mock_response

        res = analyze_message_with_ai("SELL 24000 PE", [])
        self.assertIsNotNone(res)
        self.assertTrue(res.is_valid_trade_msg)
        self.assertEqual(res.underlying, "NIFTY")
        self.assertEqual(len(res.actions), 1)
        self.assertEqual(res.actions[0].action_type, "SELL")

    @patch("gemini_client.get_genai_client")
    def test_analyze_message_mock_raw_json(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [MagicMock(text='{"is_valid_trade_msg": true, "underlying": "BANKNIFTY", "structure_type": "SINGLE CE BUY", "actions": [{"action_type": "BUY", "transaction_type": "BUY", "underlying": "BANKNIFTY", "strike": 52000, "option_type": "CE", "price": "150"}]}')]
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.parsed = None
        mock_response.text = '{"is_valid_trade_msg": true, "underlying": "BANKNIFTY", "structure_type": "SINGLE CE BUY", "actions": [{"action_type": "BUY", "transaction_type": "BUY", "underlying": "BANKNIFTY", "strike": 52000, "option_type": "CE", "price": "150"}]}'
        mock_client.models.generate_content.return_value = mock_response

        res = analyze_message_with_ai("BUY BANKNIFTY 52000 CE @ 150", [])
        self.assertIsNotNone(res)
        self.assertTrue(res.is_valid_trade_msg)
        self.assertEqual(res.underlying, "BANKNIFTY")
        self.assertEqual(len(res.actions), 1)
        self.assertEqual(res.actions[0].action_type, "BUY")
        self.assertEqual(res.actions[0].strike, 52000.0)

    def test_live_gemini_api(self):
        client = get_genai_client()
        if not client:
            self.skipTest("GEMINI_API_KEY is not configured for live test")

        res = analyze_message_with_ai("DEPLOY: JULY NIFTY 24000 PE SPREAD\nSELL: 28JUL2026 24000 PE @183 SL 220\nBUY: 28JUL2026 23600 PE @81", [])
        self.assertIsNotNone(res)
        self.assertTrue(res.is_valid_trade_msg)
        self.assertEqual(res.underlying, "NIFTY")
        self.assertEqual(len(res.actions), 2)
        self.assertEqual(res.actions[0].action_type, "SELL")
        self.assertEqual(res.actions[1].action_type, "BUY")

if __name__ == "__main__":
    unittest.main()

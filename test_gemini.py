import os
import unittest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

load_dotenv()

from gemini_client import (
    clean_symbol,
    is_poke_message,
    get_genai_client,
    get_known_tickers,
    analyze_message_with_ai,
    TradeAnalysisSchema,
    ActionSchema
)
from instruments_manager import get_known_underlyings, get_known_underlyings_set

class TestGeminiClient(unittest.TestCase):
    def test_clean_symbol(self):
        # Index symbols
        self.assertEqual(clean_symbol("NIFTY"), "NIFTY")
        self.assertEqual(clean_symbol("nifty_ref"), "NIFTY")
        self.assertEqual(clean_symbol("BANKNIFTY (JULY)"), "BANKNIFTY")
        self.assertEqual(clean_symbol("FINNIFTY 23500 CE"), "FINNIFTY")
        self.assertEqual(clean_symbol("MIDCPNIFTY"), "MIDCPNIFTY")
        self.assertEqual(clean_symbol("NIFTYNXT50"), "NIFTYNXT50")
        self.assertEqual(clean_symbol("SENSEX"), "SENSEX")
        self.assertEqual(clean_symbol("BANKEX (AUG)"), "BANKEX")

        # Initial hardcoded stocks
        self.assertEqual(clean_symbol("TATASTEEL-EQ"), "TATASTEEL")
        self.assertEqual(clean_symbol("RELIANCE REF"), "RELIANCE")
        self.assertEqual(clean_symbol("VBL"), "VBL")
        self.assertEqual(clean_symbol("INDIGO"), "INDIGO")
        self.assertEqual(clean_symbol("NATIONALUM"), "NATIONALUM")

        # Dynamically loaded F&O stocks (previously outside hardcoded whitelist)
        self.assertEqual(clean_symbol("HDFCBANK (JULY)"), "HDFCBANK")
        self.assertEqual(clean_symbol("hdfcbank_ref"), "HDFCBANK")
        self.assertEqual(clean_symbol("INFY-EQ"), "INFY")
        self.assertEqual(clean_symbol("ICICIBANK 1200 CE"), "ICICIBANK")
        self.assertEqual(clean_symbol("SBIN"), "SBIN")
        self.assertEqual(clean_symbol("BAJFINANCE_REF"), "BAJFINANCE")
        self.assertEqual(clean_symbol("MARUTI (AUG)"), "MARUTI")
        self.assertEqual(clean_symbol("COALINDIA FUT"), "COALINDIA")
        self.assertEqual(clean_symbol("BHEL"), "BHEL")
        self.assertEqual(clean_symbol("TCS"), "TCS")
        self.assertEqual(clean_symbol("360ONE 26AUG FUT"), "360ONE")
        self.assertEqual(clean_symbol("BAJAJ-AUTO (AUG)"), "BAJAJ-AUTO")
        self.assertEqual(clean_symbol("M&M REF"), "M&M")
        self.assertEqual(clean_symbol("MUTHOOTFIN (JUL)"), "MUTHOOTFIN")
        self.assertEqual(clean_symbol("TATAMOTORS 26AUG FUT"), "TATAMOTORS")
        self.assertEqual(clean_symbol("ADANIENT-EQ"), "ADANIENT")
        self.assertEqual(clean_symbol("PNBHOUSING 800 CE"), "PNBHOUSING")
        self.assertEqual(clean_symbol("PNB 100 CE"), "PNB")
        self.assertEqual(clean_symbol("LTF 150 CE"), "LTF")
        self.assertEqual(clean_symbol("LT 3800 CE"), "LT")

        # Edge cases
        self.assertIsNone(clean_symbol(None))
        self.assertIsNone(clean_symbol(""))

    def test_dynamic_underlyings_loaded(self):
        underlyings = get_known_underlyings()
        underlyings_set = get_known_underlyings_set()
        self.assertIsInstance(underlyings, list)
        self.assertIsInstance(underlyings_set, set)
        self.assertGreater(len(underlyings), 180)
        self.assertIn("HDFCBANK", underlyings_set)
        self.assertIn("INFY", underlyings_set)
        self.assertIn("ICICIBANK", underlyings_set)
        self.assertIn("SBIN", underlyings_set)
        self.assertIn("BAJFINANCE", underlyings_set)
        self.assertIn("MARUTI", underlyings_set)
        self.assertIn("COALINDIA", underlyings_set)
        self.assertIn("BHEL", underlyings_set)
        self.assertIn("TCS", underlyings_set)
        self.assertIn("NIFTY", underlyings_set)
        self.assertIn("BANKNIFTY", underlyings_set)
        self.assertIn("SENSEX", underlyings_set)
        self.assertIn("BANKEX", underlyings_set)

        # Verify sorted by length descending
        for i in range(len(underlyings) - 1):
            self.assertGreaterEqual(len(underlyings[i]), len(underlyings[i+1]))

    def test_get_known_tickers(self):
        tickers = get_known_tickers()
        self.assertIsInstance(tickers, list)
        self.assertGreater(len(tickers), 180)
        self.assertIn("HDFCBANK", tickers)
        self.assertIn("NIFTY", tickers)

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

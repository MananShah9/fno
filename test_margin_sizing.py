import os
import unittest
from unittest.mock import patch, MagicMock
from dotenv import load_dotenv

load_dotenv()

from instruments_manager import (
    is_index_symbol,
    classify_strategy_type,
    get_margin_tier_estimate,
    get_max_lot_cap,
    calculate_position_size,
    calculate_lots_from_budget,
    DEFAULT_MARGIN_TIERS,
    DEFAULT_MAX_INDEX_LOTS,
    DEFAULT_MAX_STOCK_LOTS
)
from zerodha_client import calculate_basket_margin
from worker import process_trade_actions_and_sizing
from models import Trade


class MockActionSchema:
    def __init__(self, action_type="BUY", option_type="CE", strike=None, price=None, underlying=None, lots=1, is_main=None, product="NRML"):
        self.action_type = action_type
        self.option_type = option_type
        self.strike = strike
        self.price = price
        self.underlying = underlying
        self.expiry_info = None
        self.lots = lots
        self.is_main = is_main
        self.product = product
        self.instrument_name = None
        self.stoploss = None
        self.target = None
        self.is_limit = False
        self.details = None


class TestMarginSizingLogic(unittest.TestCase):

    def test_is_index_symbol(self):
        self.assertTrue(is_index_symbol("NIFTY"))
        self.assertTrue(is_index_symbol("BANKNIFTY"))
        self.assertTrue(is_index_symbol("FINNIFTY"))
        self.assertTrue(is_index_symbol("MIDCPNIFTY"))
        self.assertTrue(is_index_symbol("SENSEX"))
        self.assertTrue(is_index_symbol("BANKEX"))
        self.assertTrue(is_index_symbol("NIFTY 50"))
        self.assertTrue(is_index_symbol("NIFTY BANK"))
        
        self.assertFalse(is_index_symbol("TATASTEEL"))
        self.assertFalse(is_index_symbol("RELIANCE"))
        self.assertFalse(is_index_symbol("VBL"))
        self.assertFalse(is_index_symbol("INFY"))
        self.assertFalse(is_index_symbol(None))
        self.assertFalse(is_index_symbol(""))

    def test_classify_strategy_type(self):
        # 1. Naked Option Buy
        buy_call = [{"action_type": "BUY", "option_type": "CE"}]
        self.assertEqual(classify_strategy_type(buy_call), "NAKED_OPTION_BUY")

        # 2. Naked Option Sell
        sell_put = [{"action_type": "SELL", "option_type": "PE"}]
        self.assertEqual(classify_strategy_type(sell_put), "NAKED_SHORT_OPTION")

        # 3. Spread (Sell + Buy)
        credit_spread = [
            {"action_type": "SELL", "option_type": "PE"},
            {"action_type": "BUY", "option_type": "PE"}
        ]
        self.assertEqual(classify_strategy_type(credit_spread), "SPREAD")

        # 4. Bear Future Spread (Sell FUT + Buy CE)
        fut_spread = [
            {"action_type": "SELL", "option_type": "FUT"},
            {"action_type": "BUY", "option_type": "CE"}
        ]
        self.assertEqual(classify_strategy_type(fut_spread), "SPREAD")

        # 5. Single Futures
        single_fut = [{"action_type": "SELL", "option_type": "FUT"}]
        self.assertEqual(classify_strategy_type(single_fut), "SINGLE_FUTURES")

    def test_margin_tier_estimate(self):
        # Index spread default ~40,000 INR
        self.assertEqual(get_margin_tier_estimate("NIFTY", "SPREAD"), 40000.0)
        # Stock spread default ~120,000 INR
        self.assertEqual(get_margin_tier_estimate("TATASTEEL", "SPREAD"), 120000.0)
        # Index futures default ~130,000 INR
        self.assertEqual(get_margin_tier_estimate("NIFTY", "SINGLE_FUTURES"), 130000.0)
        # Stock short option default ~200,000 INR
        self.assertEqual(get_margin_tier_estimate("TATASTEEL", "NAKED_SHORT_OPTION"), 200000.0)

    def test_max_lot_caps(self):
        # Default index max lots: 4
        self.assertEqual(get_max_lot_cap("NIFTY"), 4)
        self.assertEqual(get_max_lot_cap("BANKNIFTY"), 4)
        # Default stock max lots: 2
        self.assertEqual(get_max_lot_cap("TATASTEEL"), 2)
        self.assertEqual(get_max_lot_cap("VBL"), 2)

    def test_tata_steel_short_put_sizing(self):
        """
        Selling Tata Steel 192.5 PE @ 4.60 INR, lot size 5,500.
        Previously: 100,000 / (4.60 * 5500) = 4 lots (and up to 12 lots in channel runs).
        With margin model: estimated margin ~200,000 INR -> 100,000 / 200,000 = 0.5 -> 1 lot (capped at 2).
        """
        legs = [{
            "action_type": "SELL",
            "option_type": "PE",
            "underlying": "TATASTEEL",
            "strike": 192.5,
            "price": 4.60,
            "inst": {"lot_size": 5500, "tradingsymbol": "TATASTEEL26AUG192.5PE"}
        }]

        result = calculate_position_size(
            entry_legs=legs,
            target_budget=100000.0,
            underlying="TATASTEEL",
            main_price=4.60
        )

        self.assertEqual(result["strategy_type"], "NAKED_SHORT_OPTION")
        self.assertFalse(result["is_index"])
        self.assertEqual(result["sizing_method"], "ESTIMATED_MARGIN_TIER")
        self.assertEqual(result["per_lot_capital"], 200000.0)
        self.assertEqual(result["lots"], 1)
        self.assertEqual(result["max_lot_cap"], 2)

    def test_nifty_credit_spread_sizing(self):
        """
        NIFTY Credit Spread: SELL 24000 PE @ 183 + BUY 23600 PE @ 81, lot size 65.
        Target Budget: 100,000 INR.
        Margin per lot for Index spread: ~40,000 INR.
        100,000 / 40,000 = 2.5 -> 2 or 3 lots (capped at 4).
        """
        legs = [
            {
                "action_type": "SELL",
                "option_type": "PE",
                "underlying": "NIFTY",
                "strike": 24000,
                "price": 183.0,
                "inst": {"lot_size": 65, "tradingsymbol": "NIFTY26AUG24000PE"}
            },
            {
                "action_type": "BUY",
                "option_type": "PE",
                "underlying": "NIFTY",
                "strike": 23600,
                "price": 81.0,
                "inst": {"lot_size": 65, "tradingsymbol": "NIFTY26AUG23600PE"}
            }
        ]

        result = calculate_position_size(
            entry_legs=legs,
            target_budget=100000.0,
            underlying="NIFTY",
            main_price=183.0
        )

        self.assertEqual(result["strategy_type"], "SPREAD")
        self.assertTrue(result["is_index"])
        self.assertEqual(result["sizing_method"], "ESTIMATED_MARGIN_TIER")
        self.assertEqual(result["per_lot_capital"], 40000.0)
        self.assertIn(result["lots"], [2, 3])
        self.assertLessEqual(result["lots"], 4)

    def test_stock_future_spread_sizing(self):
        """
        Stock Bear Future Spread: VBL FUT Sell @ 467.5 + VBL 480 CE Buy @ 6.7, lot size 600.
        Target budget: 100,000 INR.
        Margin per lot: ~120,000 INR -> 100,000 / 120,000 = 0.83 -> 1 lot (capped at 2).
        """
        legs = [
            {
                "action_type": "SELL",
                "option_type": "FUT",
                "underlying": "VBL",
                "strike": None,
                "price": 467.5,
                "inst": {"lot_size": 600, "tradingsymbol": "VBL26AUGFUT"}
            },
            {
                "action_type": "BUY",
                "option_type": "CE",
                "underlying": "VBL",
                "strike": 480,
                "price": 6.7,
                "inst": {"lot_size": 600, "tradingsymbol": "VBL26AUG480CE"}
            }
        ]

        result = calculate_position_size(
            entry_legs=legs,
            target_budget=100000.0,
            underlying="VBL",
            main_price=467.5
        )

        self.assertEqual(result["strategy_type"], "SPREAD")
        self.assertFalse(result["is_index"])
        self.assertEqual(result["lots"], 1)
        self.assertEqual(result["max_lot_cap"], 2)

    def test_naked_option_buying_uses_premium(self):
        """
        Naked Option Buying: NIFTY 24000 CE Buy @ 100, lot size 65.
        Cost per lot = 100 * 65 = 6,500 INR.
        Budget 100,000 -> 100,000 / 6500 = 15.38 -> capped by max index cap (4 lots).
        """
        legs = [{
            "action_type": "BUY",
            "option_type": "CE",
            "underlying": "NIFTY",
            "strike": 24000,
            "price": 100.0,
            "inst": {"lot_size": 65, "tradingsymbol": "NIFTY26AUG24000CE"}
        }]

        result = calculate_position_size(
            entry_legs=legs,
            target_budget=100000.0,
            underlying="NIFTY",
            main_price=100.0
        )

        self.assertEqual(result["strategy_type"], "NAKED_OPTION_BUY")
        self.assertEqual(result["sizing_method"], "PREMIUM_COST")
        self.assertEqual(result["per_lot_capital"], 6500.0)
        self.assertEqual(result["lots"], 4)  # Capped at max index cap = 4

    def test_live_zerodha_margin_override(self):
        """
        When live Zerodha margin is provided from basket_margins API,
        it should be utilized for sizing instead of estimated tiers.
        """
        legs = [
            {
                "action_type": "SELL",
                "option_type": "PE",
                "underlying": "NIFTY",
                "strike": 24000,
                "price": 183.0,
                "inst": {"lot_size": 65, "tradingsymbol": "NIFTY26AUG24000PE"}
            },
            {
                "action_type": "BUY",
                "option_type": "PE",
                "underlying": "NIFTY",
                "strike": 23600,
                "price": 81.0,
                "inst": {"lot_size": 65, "tradingsymbol": "NIFTY26AUG23600PE"}
            }
        ]

        # Live margin from Zerodha API = 33,000 INR
        result = calculate_position_size(
            entry_legs=legs,
            target_budget=100000.0,
            underlying="NIFTY",
            live_margin=33000.0,
            main_price=183.0
        )

        self.assertEqual(result["sizing_method"], "ZERODHA_LIVE_MARGIN")
        self.assertEqual(result["per_lot_capital"], 33000.0)
        # 100,000 / 33,000 = 3.03 -> 3 lots (capped at 4)
        self.assertEqual(result["lots"], 3)

    def test_strict_cap_enforcement_with_huge_budget(self):
        """
        With 1 Crore budget, lots must be strictly capped at MAX_STOCK_LOTS / MAX_INDEX_LOTS.
        """
        stock_legs = [{
            "action_type": "SELL",
            "option_type": "PE",
            "underlying": "TATASTEEL",
            "strike": 190,
            "price": 5.0,
            "inst": {"lot_size": 5500}
        }]

        result_stock = calculate_position_size(
            entry_legs=stock_legs,
            target_budget=10000000.0,  # 1 Crore
            underlying="TATASTEEL"
        )
        self.assertEqual(result_stock["lots"], 2)  # Capped at MAX_STOCK_LOTS = 2

        index_legs = [{
            "action_type": "SELL",
            "option_type": "PE",
            "underlying": "NIFTY",
            "strike": 24000,
            "price": 150.0,
            "inst": {"lot_size": 65}
        }]

        result_index = calculate_position_size(
            entry_legs=index_legs,
            target_budget=10000000.0,  # 1 Crore
            underlying="NIFTY"
        )
        self.assertEqual(result_index["lots"], 4)  # Capped at MAX_INDEX_LOTS = 4

    @patch("zerodha_client.get_zerodha_client")
    def test_calculate_basket_margin_api_success(self, mock_get_kite):
        mock_kite = MagicMock()
        mock_kite.basket_margins.return_value = {
            "final": {"total": 38450.0},
            "initial": {"total": 42000.0}
        }
        mock_get_kite.return_value = mock_kite

        margin = calculate_basket_margin([
            {"tradingsymbol": "NIFTY26AUG24000PE", "transaction_type": "SELL", "quantity": 65},
            {"tradingsymbol": "NIFTY26AUG23600PE", "transaction_type": "BUY", "quantity": 65}
        ])

        self.assertEqual(margin, 38450.0)

    @patch("zerodha_client.get_zerodha_client")
    def test_calculate_basket_margin_api_failure_returns_none(self, mock_get_kite):
        mock_get_kite.side_effect = Exception("Zerodha API connection error")

        margin = calculate_basket_margin([
            {"tradingsymbol": "NIFTY26AUG24000PE", "transaction_type": "SELL", "quantity": 65}
        ])

        self.assertIsNone(margin)

    def test_process_trade_actions_and_sizing_end_to_end(self):
        """
        Verify process_trade_actions_and_sizing generates correct Action objects with
        margin-sized lots for multi-leg trade.
        """
        trade = Trade(id=101, underlying="NIFTY", status="OPEN")
        parsed_actions = [
            MockActionSchema(action_type="SELL", option_type="PE", strike=24000, price=183.0, underlying="NIFTY", is_main=True),
            MockActionSchema(action_type="BUY", option_type="PE", strike=23600, price=81.0, underlying="NIFTY", is_main=False)
        ]

        actions = process_trade_actions_and_sizing(
            trade=trade,
            db_message_id=501,
            parsed_actions=parsed_actions,
            target_budget=100000.0
        )

        self.assertEqual(len(actions), 2)
        # Check lot sizing and quantity consistency
        sell_act = actions[0]
        buy_act = actions[1]

        self.assertEqual(sell_act.action_type, "SELL")
        self.assertEqual(buy_act.action_type, "BUY")
        self.assertTrue(sell_act.is_main)
        self.assertFalse(buy_act.is_main)
        self.assertEqual(sell_act.lots, buy_act.lots)
        self.assertEqual(sell_act.quantity, buy_act.quantity)
        self.assertLessEqual(sell_act.lots, 4)
        self.assertGreaterEqual(sell_act.lots, 1)


if __name__ == "__main__":
    unittest.main()

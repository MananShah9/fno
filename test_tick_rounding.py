import unittest
from unittest.mock import MagicMock, patch
from instruments_manager import round_to_tick, parse_price_value
from zerodha_client import place_zerodha_order
from gemini_client import classify_sl_trigger
from models import Action, Trade, Message
from db import get_db, init_db
from worker import execute_trade_actions

class TestTickRounding(unittest.TestCase):
    def setUp(self):
        self.session = get_db()

    def tearDown(self):
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()
        self.session.close()

    def test_round_to_tick_buy_and_sell_directions(self):
        """Test that round_to_tick correctly rounds down for BUY and up for SELL to valid 0.05 ticks."""
        # 1.525 (midpoint of 1.5 - 1.55)
        self.assertEqual(round_to_tick(1.525, tick_size=0.05, direction="BUY"), 1.50)
        self.assertEqual(round_to_tick(1.525, tick_size=0.05, direction="DOWN"), 1.50)
        self.assertEqual(round_to_tick(1.525, tick_size=0.05, direction="BID"), 1.50)
        self.assertEqual(round_to_tick(1.525, tick_size=0.05, direction="SELL"), 1.55)
        self.assertEqual(round_to_tick(1.525, tick_size=0.05, direction="UP"), 1.55)
        self.assertEqual(round_to_tick(1.525, tick_size=0.05, direction="ASK"), 1.55)

        # 4.62
        self.assertEqual(round_to_tick(4.62, tick_size=0.05, direction="BUY"), 4.60)
        self.assertEqual(round_to_tick(4.62, tick_size=0.05, direction="SELL"), 4.65)

        # 1.53
        self.assertEqual(round_to_tick(1.53, tick_size=0.05, direction="BUY"), 1.50)
        self.assertEqual(round_to_tick(1.53, tick_size=0.05, direction="SELL"), 1.55)

        # Exact ticks stay exact
        self.assertEqual(round_to_tick(4.60, tick_size=0.05), 4.60)
        self.assertEqual(round_to_tick(183.0, tick_size=0.05), 183.0)
        self.assertEqual(round_to_tick(467.5, tick_size=0.05), 467.5)

    def test_round_to_tick_spot_and_custom_tick_sizes(self):
        """Test round_to_tick with large numbers and custom tick sizes."""
        # Index spot levels (0.05 tick)
        self.assertEqual(round_to_tick(24200.12, tick_size=0.05, direction="BUY"), 24200.10)
        self.assertEqual(round_to_tick(24200.12, tick_size=0.05, direction="SELL"), 24200.15)

        # Currency tick size (0.0025)
        self.assertEqual(round_to_tick(82.1234, tick_size=0.0025, direction="BUY"), 82.1225)
        self.assertEqual(round_to_tick(82.1234, tick_size=0.0025, direction="SELL"), 82.1250)

    def test_round_to_tick_edge_cases(self):
        """Test round_to_tick handling of None, zero, negative, and invalid values."""
        self.assertIsNone(round_to_tick(None))
        self.assertIsNone(round_to_tick(0))
        self.assertIsNone(round_to_tick(-10.5))
        self.assertIsNone(round_to_tick("invalid_num"))

    def test_parse_price_value_range_midpoint_rounding(self):
        """Test that parse_price_value rounds range midpoints to valid exchange ticks."""
        # 1.5-1.55 range (midpoint is 1.525)
        self.assertEqual(parse_price_value("1.5-1.55", direction="BUY"), 1.50)
        self.assertEqual(parse_price_value("1.5-1.55", direction="SELL"), 1.55)

        # 4.5-4.7 range (midpoint is 4.60)
        self.assertEqual(parse_price_value("4.5-4.7"), 4.60)

        # 467-468 range (midpoint is 467.5)
        self.assertEqual(parse_price_value("467-468"), 467.5)

        # Exact single prices
        self.assertEqual(parse_price_value("183"), 183.0)
        self.assertEqual(parse_price_value("@ 81"), 81.0)
        self.assertEqual(parse_price_value(240.0), 240.0)
        self.assertEqual(parse_price_value(461.9), 461.9)

        # Float input with non-tick decimal
        self.assertEqual(parse_price_value(1.525, direction="BUY"), 1.50)
        self.assertEqual(parse_price_value(1.525, direction="SELL"), 1.55)

        # Empty / None / invalid
        self.assertIsNone(parse_price_value(None))
        self.assertIsNone(parse_price_value(""))
        self.assertIsNone(parse_price_value("market"))
        self.assertIsNone(parse_price_value(0))
        self.assertIsNone(parse_price_value(-5.0))

    def test_classify_sl_trigger_tick_rounding(self):
        """Test that classify_sl_trigger enforces 0.05 tick size on parsed trigger prices."""
        res = classify_sl_trigger(
            raw_stoploss="1.525",
            raw_message_text="BUY 24000 CE @ 2.50 SL: 1.525",
            underlying="NIFTY",
            strike=24000.0,
            option_type="CE",
            entry_price="2.50",
            transaction_type="BUY"
        )
        self.assertEqual(res["sl_trigger_type"], "OPTION_PREMIUM_TRIGGER")
        self.assertEqual(res["sl_trigger_price"], 1.50)  # BUY option stoploss rounded down

        res_short = classify_sl_trigger(
            raw_stoploss="1.525",
            raw_message_text="SELL 24000 PE @ 0.90 SL: 1.525",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="0.90",
            transaction_type="SELL"
        )
        self.assertEqual(res_short["sl_trigger_type"], "OPTION_PREMIUM_TRIGGER")
        self.assertEqual(res_short["sl_trigger_price"], 1.55)  # Short option stoploss rounded up

    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_enforces_tick_rounding(self, mock_get_kite):
        """Test that place_zerodha_order rounds non-tick limit and trigger prices before submission."""
        mock_kite = MagicMock()
        mock_kite.place_order.return_value = "260819000001"
        mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
        mock_kite.ORDER_TYPE_SL = "SL"
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.TRANSACTION_TYPE_SELL = "SELL"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"
        mock_get_kite.return_value = mock_kite

        # 1. LIMIT BUY with 1.525 price -> rounded down to 1.50
        res_buy = place_zerodha_order(
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="BUY",
            quantity=65,
            order_type="LIMIT",
            price=1.525,
            verify_confirmation=False
        )
        self.assertTrue(res_buy["success"])
        mock_kite.place_order.assert_called_with(
            variety="regular",
            exchange="NFO",
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="BUY",
            quantity=65,
            product="NRML",
            order_type="LIMIT",
            price=1.50,
            trigger_price=None,
            validity="DAY"
        )

        # 2. LIMIT SELL with 1.525 price -> rounded up to 1.55
        res_sell = place_zerodha_order(
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="SELL",
            quantity=65,
            order_type="LIMIT",
            price=1.525,
            verify_confirmation=False
        )
        self.assertTrue(res_sell["success"])
        mock_kite.place_order.assert_called_with(
            variety="regular",
            exchange="NFO",
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="SELL",
            quantity=65,
            product="NRML",
            order_type="LIMIT",
            price=1.55,
            trigger_price=None,
            validity="DAY"
        )

        # 3. SL BUY with 220.02 trigger price -> rounded up to 220.05
        res_sl_buy = place_zerodha_order(
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="BUY",
            quantity=65,
            order_type="SL",
            price=225.0,
            trigger_price=220.02,
            verify_confirmation=False
        )
        self.assertTrue(res_sl_buy["success"])
        mock_kite.place_order.assert_called_with(
            variety="regular",
            exchange="NFO",
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="BUY",
            quantity=65,
            product="NRML",
            order_type="SL",
            price=None,
            trigger_price=220.05,
            validity="DAY"
        )

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_worker_executes_limit_order_with_range_tick_rounding(self, mock_dedup, mock_place_order):
        """Test that worker parse action.price range and submits tick-rounded limit order."""
        mock_dedup.return_value = {"duplicate": False, "reason": "not_duplicated", "message": "OK"}
        mock_place_order.return_value = {
            "success": True,
            "order_id": "260819000002",
            "status": "OPEN",
            "message": "Order placed successfully"
        }

        # Create trade and action with range '1.5-1.55'
        msg = Message(
            telegram_message_id=9901,
            date=None,
            text="BUY NIFTY 24000 PE @ 1.5-1.55",
            processed=True,
            analysed_by_ai=True
        )
        self.session.add(msg)
        self.session.commit()

        trade = Trade(
            underlying="NIFTY",
            status="OPEN",
            structure_type="SINGLE PE BUY"
        )
        self.session.add(trade)
        self.session.commit()

        act = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            order_type="LIMIT",
            price="1.5-1.55",
            order_status="PENDING"
        )
        self.session.add(act)
        self.session.commit()

        results = execute_trade_actions(self.session, trade.id, auto_mode=False)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])

        # Check that place_zerodha_order was invoked with price=1.50 (rounded down for BUY)
        mock_place_order.assert_called_once_with(
            tradingsymbol="NIFTY2681824000PE",
            transaction_type="BUY",
            quantity=65,
            exchange="NFO",
            order_type="LIMIT",
            product="NRML",
            price=1.50,
            freeze_limit=1800,
            lot_size=65
        )


if __name__ == "__main__":
    unittest.main()

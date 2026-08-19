import os
import unittest
import asyncio
from unittest.mock import patch, MagicMock
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from gemini_client import classify_sl_trigger, TradeAnalysisSchema, ActionSchema
from instruments_manager import get_spot_instrument_key
from zerodha_client import get_spot_ltp, place_zerodha_order
from worker import (
    process_trade_actions_and_sizing,
    check_active_spot_stoplosses,
    format_spot_sl_triggered_telegram_html,
    format_action_telegram_message_html,
    process_single_message
)


class TestSpotStopLossMonitoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.session = db.SessionLocal()
        # Clean test records
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()

    def tearDown(self):
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()
        self.session.close()

    # =========================================================================
    # 1. Classification Unit Tests: classify_sl_trigger
    # =========================================================================
    def test_classify_option_premium_triggers(self):
        """Test classification of option and derivative premium trigger stoplosses."""
        # Nifty 24000 PE sold at 183, SL 220
        res1 = classify_sl_trigger(
            raw_stoploss="220",
            raw_message_text="SELL 28JUL2026 24000 PE @183 SL 220",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="183",
            transaction_type="SELL"
        )
        self.assertEqual(res1["sl_trigger_type"], "OPTION_PREMIUM_TRIGGER")
        self.assertEqual(res1["sl_trigger_price"], 220.0)
        self.assertEqual(res1["sl_trigger_direction"], "ABOVE")

        # Explicit derivative text: "when 24000 PE hits 220"
        res2 = classify_sl_trigger(
            raw_stoploss="when 24000 PE hits 220",
            raw_message_text="SL: when 24000 PE hits 220",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="183",
            transaction_type="SELL"
        )
        self.assertEqual(res2["sl_trigger_type"], "OPTION_PREMIUM_TRIGGER")
        self.assertEqual(res2["sl_trigger_price"], 220.0)
        self.assertEqual(res2["sl_trigger_direction"], "ABOVE")

        # Long Option: Buy Nifty 24000 CE @ 100, SL 75
        res3 = classify_sl_trigger(
            raw_stoploss="75",
            raw_message_text="BUY NIFTY 24000 CE @ 100 SL 75",
            underlying="NIFTY",
            strike=24000.0,
            option_type="CE",
            entry_price="100",
            transaction_type="BUY"
        )
        self.assertEqual(res3["sl_trigger_type"], "OPTION_PREMIUM_TRIGGER")
        self.assertEqual(res3["sl_trigger_price"], 75.0)
        self.assertEqual(res3["sl_trigger_direction"], "BELOW")

        # Futures contract: VBL FUT @ 467.5, SL 475
        res4 = classify_sl_trigger(
            raw_stoploss="475",
            raw_message_text="SELL VBL FUT 467-468 SL: 475",
            underlying="VBL",
            strike=None,
            option_type="FUT",
            entry_price="467.5",
            transaction_type="SELL"
        )
        self.assertEqual(res4["sl_trigger_type"], "OPTION_PREMIUM_TRIGGER")
        self.assertEqual(res4["sl_trigger_price"], 475.0)
        self.assertEqual(res4["sl_trigger_direction"], "ABOVE")

    def test_classify_underlying_spot_triggers(self):
        """Test classification of underlying spot index/stock level triggers."""
        # "when Nifty reaches 24200" on Nifty option
        res1 = classify_sl_trigger(
            raw_stoploss="when Nifty reaches 24200",
            raw_message_text="SELL 24000 PE @ 183 SL: when Nifty reaches 24200",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="183"
        )
        self.assertEqual(res1["sl_trigger_type"], "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(res1["sl_trigger_price"], 24200.0)
        self.assertEqual(res1["sl_trigger_direction"], "ABOVE")

        # "SL: 24200 spot"
        res2 = classify_sl_trigger(
            raw_stoploss="24200 spot",
            raw_message_text="SL: 24200 spot",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="183"
        )
        self.assertEqual(res2["sl_trigger_type"], "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(res2["sl_trigger_price"], 24200.0)

        # "when stock price hits 186" on Tata Steel option (strike 192.5 PE @ 4.60)
        res3 = classify_sl_trigger(
            raw_stoploss="when stock price hits 186",
            raw_message_text="SELL TATASTEEL 192.5 PE @ 4.60 SL: when stock price hits 186",
            underlying="TATASTEEL",
            strike=192.5,
            option_type="PE",
            entry_price="4.60"
        )
        self.assertEqual(res3["sl_trigger_type"], "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(res3["sl_trigger_price"], 186.0)
        self.assertEqual(res3["sl_trigger_direction"], "BELOW")

        # Implicit spot magnitude on stock option: "SL: 186" on Tata Steel 190 PE @ 4.50
        # (186 is ~40x the 4.50 premium, exactly matching stock strike range)
        res4 = classify_sl_trigger(
            raw_stoploss="186",
            raw_message_text="SELL TATASTEEL 190 PE @ 4.50 SL 186",
            underlying="TATASTEEL",
            strike=190.0,
            option_type="PE",
            entry_price="4.50"
        )
        self.assertEqual(res4["sl_trigger_type"], "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(res4["sl_trigger_price"], 186.0)
        self.assertEqual(res4["sl_trigger_direction"], "BELOW")

        # Implicit index spot level on index option: "SL: 24200" on NIFTY 24000 PE @ 183
        # (24200 is index level > 2000)
        res5 = classify_sl_trigger(
            raw_stoploss="24200",
            raw_message_text="SELL NIFTY 24000 PE @ 183 SL 24200",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="183"
        )
        self.assertEqual(res5["sl_trigger_type"], "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(res5["sl_trigger_price"], 24200.0)

        # "closing below 24150"
        res6 = classify_sl_trigger(
            raw_stoploss="closing below 24150",
            raw_message_text="SL: Nifty closing below 24150",
            underlying="NIFTY",
            strike=24000.0,
            option_type="PE",
            entry_price="183"
        )
        self.assertEqual(res6["sl_trigger_type"], "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(res6["sl_trigger_price"], 24150.0)
        self.assertEqual(res6["sl_trigger_direction"], "BELOW")

    # =========================================================================
    # 2. Spot Instrument Quote Key Mapping
    # =========================================================================
    def test_spot_instrument_key_resolution(self):
        """Test resolution of spot instrument keys for Zerodha Kite Connect."""
        self.assertEqual(get_spot_instrument_key("NIFTY"), "NSE:NIFTY 50")
        self.assertEqual(get_spot_instrument_key("BANKNIFTY"), "NSE:NIFTY BANK")
        self.assertEqual(get_spot_instrument_key("FINNIFTY"), "NSE:NIFTY FIN SERVICE")
        self.assertEqual(get_spot_instrument_key("MIDCPNIFTY"), "NSE:NIFTY MID SELECT")
        self.assertEqual(get_spot_instrument_key("SENSEX"), "BSE:SENSEX")
        self.assertEqual(get_spot_instrument_key("BANKEX"), "BSE:BANKEX")
        self.assertEqual(get_spot_instrument_key("TATASTEEL"), "NSE:TATASTEEL")
        self.assertEqual(get_spot_instrument_key("RELIANCE"), "NSE:RELIANCE")
        self.assertEqual(get_spot_instrument_key("VBL"), "NSE:VBL")
        self.assertIsNone(get_spot_instrument_key(None))

    # =========================================================================
    # 3. Exchange SL Prevention for Spot Prices on Options
    # =========================================================================
    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_blocks_spot_trigger_on_option_contract(self, mock_get_kite):
        """
        Verify that place_zerodha_order prevents sending spot index/stock trigger prices
        (e.g., 24200.0 or 186.0) as exchange SL orders on option contracts.
        """
        mock_kite = MagicMock()
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_SL = "SL"
        mock_kite.ORDER_TYPE_SLM = "SL-M"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_get_kite.return_value = mock_kite

        # Attempt to place SL order on option contract with NIFTY spot index trigger price (24200)
        res = place_zerodha_order(
            tradingsymbol="NIFTY26AUG24000PE",
            transaction_type="BUY",
            quantity=65,
            order_type="SL",
            price=220.0,
            trigger_price=24200.0,  # Invalid spot trigger price for option
            verify_confirmation=False
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("appears to be underlying spot level", res["message"])
        self.assertIn("Routed to Active Spot Monitoring Loop", res["message"])
        # Ensure kite.place_order was NEVER called
        mock_kite.place_order.assert_not_called()

    # =========================================================================
    # 4. Action Creation, Spot SL Registration & Stage Tracking
    # =========================================================================
    def test_process_trade_actions_registers_spot_sl_monitoring(self):
        """
        Verify that process_trade_actions_and_sizing correctly identifies UNDERLYING_SPOT_TRIGGER
        and marks sl_monitoring_active = True and sl_order_status = 'MONITORING'.
        """
        trade = Trade(id=10, underlying="NIFTY", status="OPEN")
        self.session.add(trade)
        self.session.commit()

        parsed_actions = [
            ActionSchema(
                action_type="SELL",
                option_type="PE",
                strike=24000.0,
                underlying="NIFTY",
                price="183.0",
                stoploss="when Nifty reaches 24200",
                is_main=True
            ),
            ActionSchema(
                action_type="BUY",
                option_type="PE",
                strike=23600.0,
                underlying="NIFTY",
                price="81.0",
                is_main=False
            )
        ]

        actions = process_trade_actions_and_sizing(
            trade=trade,
            db_message_id=101,
            parsed_actions=parsed_actions
        )

        self.assertEqual(len(actions), 2)
        sell_act = [a for a in actions if a.action_type == "SELL"][0]
        self.assertEqual(sell_act.sl_trigger_type, "UNDERLYING_SPOT_TRIGGER")
        self.assertEqual(sell_act.sl_trigger_price, 24200.0)
        self.assertEqual(sell_act.sl_trigger_direction, "ABOVE")
        self.assertTrue(sell_act.sl_monitoring_active)
        self.assertEqual(sell_act.sl_order_status, "MONITORING")
        self.assertFalse(sell_act.sl_triggered)

    # =========================================================================
    # 5. Active Market-Data Spot Stop-Loss Monitoring Loop Tests
    # =========================================================================
    @patch("worker.get_spot_ltp")
    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_spot_monitoring_loop_no_trigger_when_threshold_not_crossed(self, mock_dedup, mock_place, mock_spot_ltp):
        """
        Verify that when live spot LTP has NOT crossed the trigger threshold:
        - Position remains OPEN.
        - sl_triggered remains False.
        - No square-off orders are executed.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        mock_spot_ltp.return_value = 24100.0  # Spot is at 24,100 (below SL threshold of 24,200)

        trade = Trade(underlying="NIFTY", status="OPEN", structure_type="NIFTY PE SPREAD")
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        msg = Message(telegram_message_id=8001, text="Test SL Setup", processed=True, analysed_by_ai=True)
        self.session.add(msg)
        self.session.commit()

        act = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            tradingsymbol="NIFTY26AUG24000PE",
            strike=24000.0,
            quantity=65,
            lots=1,
            order_status="PLACED",
            stoploss="when Nifty reaches 24200",
            sl_trigger_type="UNDERLYING_SPOT_TRIGGER",
            sl_trigger_price=24200.0,
            sl_trigger_direction="ABOVE",
            sl_monitoring_active=True,
            sl_triggered=False
        )
        self.session.add(act)
        self.session.commit()

        triggered_events = asyncio.run(check_active_spot_stoplosses(actions_entity=None, session=self.session))

        self.assertEqual(len(triggered_events), 0)
        self.session.refresh(trade)
        self.session.refresh(act)
        self.assertEqual(trade.status, "OPEN")
        self.assertFalse(act.sl_triggered)
        self.assertTrue(act.sl_monitoring_active)
        mock_place.assert_not_called()

    @patch("worker.get_spot_ltp")
    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    @patch("worker.client.send_message")
    def test_spot_monitoring_loop_triggers_exit_when_spot_threshold_crossed(self, mock_send, mock_dedup, mock_place, mock_spot_ltp):
        """
        Verify that when live spot LTP crosses the stop-loss threshold:
        1. sl_triggered becomes True and sl_monitoring_active becomes False.
        2. Trade status is updated to CLOSED.
        3. Automatic emergency square-off orders are generated and executed immediately.
        4. SPOT_SL_TRIGGERED stage trace is recorded.
        5. High-priority Telegram alert is dispatched.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        mock_place.return_value = {
            "success": True,
            "order_id": "EXIT_SQ_901",
            "status": "COMPLETE",
            "message": "Market square-off executed"
        }
        mock_send.return_value = MagicMock()

        # Spot crosses above threshold 24200 -> live spot is 24215.50
        mock_spot_ltp.return_value = 24215.50

        trade = Trade(underlying="NIFTY", status="OPEN", structure_type="NIFTY PE SPREAD")
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        msg = Message(telegram_message_id=8002, text="Initial Trade Message", processed=True, analysed_by_ai=True)
        self.session.add(msg)
        self.session.commit()

        # Short main leg (65 qty)
        act_short = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            tradingsymbol="NIFTY26AUG24000PE",
            strike=24000.0,
            quantity=65,
            lots=1,
            order_status="PLACED",
            stoploss="when Nifty reaches 24200",
            sl_trigger_type="UNDERLYING_SPOT_TRIGGER",
            sl_trigger_price=24200.0,
            sl_trigger_direction="ABOVE",
            sl_monitoring_active=True,
            sl_triggered=False
        )
        # Long hedge leg (65 qty)
        act_hedge = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            tradingsymbol="NIFTY26AUG23600PE",
            strike=23600.0,
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add_all([act_short, act_hedge])
        self.session.commit()

        # Run spot SL monitoring tick
        fake_actions_entity = MagicMock()
        triggered_events = asyncio.run(check_active_spot_stoplosses(actions_entity=fake_actions_entity, session=self.session))

        self.assertEqual(len(triggered_events), 1)
        ev = triggered_events[0]
        self.assertEqual(ev["trade_id"], trade.id)
        self.assertEqual(ev["spot_ltp"], 24215.50)
        self.assertEqual(ev["sl_trigger_price"], 24200.0)

        # Check DB State updates
        self.session.refresh(trade)
        self.session.refresh(act_short)
        self.assertEqual(trade.status, "CLOSED")
        self.assertIsNotNone(trade.closed_at)
        self.assertTrue(act_short.sl_triggered)
        self.assertFalse(act_short.sl_monitoring_active)
        self.assertEqual(act_short.sl_order_status, "TRIGGERED")
        self.assertIsNotNone(act_short.sl_triggered_at)

        # Check SPOT_SL_TRIGGERED stage trace
        trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == trade.id,
            MessageStageTrace.stage == "SPOT_SL_TRIGGERED"
        ).first()
        self.assertIsNotNone(trace)
        self.assertEqual(trace.status, "SUCCESS")
        self.assertIn("24215.5", trace.details or "")

        # Verify emergency square-off orders were executed for both legs (BUY to close short, SELL to close hedge)
        self.assertEqual(mock_place.call_count, 2)
        call_symbols = [c.kwargs["tradingsymbol"] for c in mock_place.call_args_list]
        self.assertIn("NIFTY26AUG24000PE", call_symbols)
        self.assertIn("NIFTY26AUG23600PE", call_symbols)

        # Check Telegram emergency alert was sent
        self.assertTrue(mock_send.called)
        sent_html = mock_send.call_args[0][1]
        self.assertIn("STOP-LOSS TRIGGERED (UNDERLYING SPOT HIT)", sent_html)
        self.assertIn("24,215.50", sent_html)

    @patch("worker.get_spot_ltp")
    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_stock_spot_stoploss_downward_crossing(self, mock_dedup, mock_place, mock_spot_ltp):
        """
        Verify that for a stock position (e.g. Tata Steel), a downward spot price cross
        (e.g., spot falls below 186.0) triggers emergency market square off.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        mock_place.return_value = {"success": True, "order_id": "EXIT_TATA_1", "status": "COMPLETE", "message": "Exit OK"}

        # Tata Steel stock falls to 184.50 (below SL threshold of 186.0)
        mock_spot_ltp.return_value = 184.50

        trade = Trade(underlying="TATASTEEL", status="OPEN", structure_type="TATASTEEL BULL PUT SPREAD")
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        msg = Message(telegram_message_id=8003, text="Tata Steel Trade Setup", processed=True, analysed_by_ai=True)
        self.session.add(msg)
        self.session.commit()

        act = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            tradingsymbol="TATASTEEL26AUG192.5PE",
            strike=192.5,
            quantity=5500,
            lots=1,
            order_status="PLACED",
            stoploss="when stock price hits 186",
            sl_trigger_type="UNDERLYING_SPOT_TRIGGER",
            sl_trigger_price=186.0,
            sl_trigger_direction="BELOW",
            sl_monitoring_active=True,
            sl_triggered=False
        )
        self.session.add(act)
        self.session.commit()

        triggered_events = asyncio.run(check_active_spot_stoplosses(actions_entity=None, session=self.session))

        self.assertEqual(len(triggered_events), 1)
        self.session.refresh(trade)
        self.session.refresh(act)
        self.assertEqual(trade.status, "CLOSED")
        self.assertTrue(act.sl_triggered)
        self.assertFalse(act.sl_monitoring_active)
        self.assertEqual(mock_place.call_count, 1)
        self.assertEqual(mock_place.call_args.kwargs["tradingsymbol"], "TATASTEEL26AUG192.5PE")
        self.assertEqual(mock_place.call_args.kwargs["transaction_type"], "BUY")

    def test_format_spot_sl_triggered_telegram_html(self):
        """Test formatting of the spot SL trigger Telegram HTML alert."""
        trade = Trade(id=42, underlying="NIFTY", structure_type="NIFTY BULL PUT SPREAD")
        act = Action(
            id=101,
            trade_id=42,
            stoploss="when Nifty reaches 24200",
            sl_trigger_type="UNDERLYING_SPOT_TRIGGER",
            sl_trigger_price=24200.0,
            sl_trigger_direction="ABOVE"
        )
        exec_results = [
            {"tradingsymbol": "NIFTY26AUG24000PE", "success": True, "order_id": "ORD_9911"},
            {"tradingsymbol": "NIFTY26AUG23600PE", "success": True, "order_id": "ORD_9912"}
        ]

        html = format_spot_sl_triggered_telegram_html(trade, act, spot_ltp=24210.0, exec_results=exec_results)
        self.assertIn("STOP-LOSS TRIGGERED (UNDERLYING SPOT HIT)", html)
        self.assertIn("#42", html)
        self.assertIn("NIFTY", html)
        self.assertIn("24,210.00", html)
        self.assertIn("24,200.00", html)
        self.assertIn("ORD_9911", html)
        self.assertIn("CLOSED", html)


if __name__ == "__main__":
    unittest.main()

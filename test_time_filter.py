import os
import unittest
import asyncio
from datetime import datetime, time, timezone, timedelta
from unittest.mock import patch, MagicMock
import zoneinfo

from time_filter import (
    parse_time_str,
    parse_end_time_str,
    get_configured_timezone,
    is_telegram_time_active,
    get_schedule_description
)
try:
    import db
    from models import Message, Trade, Action, MessageStageTrace
    from worker import (
        process_single_message,
        execute_trade_actions,
        process_ready_for_market_open_orders
    )
    from gemini_client import ActionSchema
    HAS_DB = True
except ImportError:
    HAS_DB = False


class TestTimeFilter(unittest.TestCase):
    def setUp(self):
        self.ist = zoneinfo.ZoneInfo("Asia/Kolkata")

    def test_parse_time_str_various_formats(self):
        self.assertEqual(parse_time_str("08:30", time(0, 0)), time(8, 30))
        self.assertEqual(parse_time_str("8:30", time(0, 0)), time(8, 30))
        self.assertEqual(parse_time_str("16:30", time(0, 0)), time(16, 30))
        self.assertEqual(parse_time_str("4:30 PM", time(0, 0)), time(16, 30))
        self.assertEqual(parse_time_str("04:30 PM", time(0, 0)), time(16, 30))
        self.assertEqual(parse_time_str("8:30 AM", time(0, 0)), time(8, 30))
        self.assertEqual(parse_time_str("8:30am", time(0, 0)), time(8, 30))
        self.assertEqual(parse_time_str("4:30pm", time(0, 0)), time(16, 30))
        self.assertEqual(parse_time_str("08:30:00", time(0, 0)), time(8, 30, 0))
        self.assertEqual(parse_time_str("8:30 IST", time(0, 0)), time(8, 30))
        self.assertEqual(parse_time_str("4:30 PM IST", time(0, 0)), time(16, 30))
        self.assertEqual(parse_time_str("invalid", time(9, 15)), time(9, 15))
        self.assertEqual(parse_time_str("", time(9, 15)), time(9, 15))
        self.assertEqual(parse_time_str(None, time(9, 15)), time(9, 15))

    def test_parse_end_time_str(self):
        end_t = parse_end_time_str("16:30")
        self.assertEqual(end_t.hour, 16)
        self.assertEqual(end_t.minute, 30)
        self.assertEqual(end_t.second, 59)

        end_t_explicit = parse_end_time_str("16:30:00")
        self.assertEqual(end_t_explicit.hour, 16)
        self.assertEqual(end_t_explicit.minute, 30)
        self.assertEqual(end_t_explicit.second, 0)

    def test_get_configured_timezone(self):
        tz_ist = get_configured_timezone("Asia/Kolkata")
        self.assertIsNotNone(tz_ist)

        tz_ist_abbr = get_configured_timezone("IST")
        self.assertIsNotNone(tz_ist_abbr)

        tz_utc = get_configured_timezone("UTC")
        self.assertEqual(tz_utc, timezone.utc)

        tz_offset = get_configured_timezone("+05:30")
        self.assertIsNotNone(tz_offset)

    def test_is_telegram_time_active_disabled(self):
        cfg = {"TELEGRAM_TIME_FILTER_ENABLED": False}
        active, reason = is_telegram_time_active(datetime.now(), cfg)
        self.assertTrue(active)
        self.assertIn("disabled", reason.lower())

    def test_is_telegram_time_active_weekdays_and_hours(self):
        cfg = {
            "TELEGRAM_TIME_FILTER_ENABLED": True,
            "TELEGRAM_START_TIME": "08:30",
            "TELEGRAM_END_TIME": "16:30",
            "TELEGRAM_WEEKDAYS_ONLY": True,
            "TELEGRAM_TIMEZONE": "Asia/Kolkata"
        }

        # Monday 10:00 AM IST (2026-08-17 is a Monday)
        monday_10am = datetime(2026, 8, 17, 10, 0, 0, tzinfo=self.ist)
        active, reason = is_telegram_time_active(monday_10am, cfg)
        self.assertTrue(active, f"Expected Monday 10am to be active: {reason}")

        # Monday 08:30 AM IST (start boundary)
        monday_start = datetime(2026, 8, 17, 8, 30, 0, tzinfo=self.ist)
        active, reason = is_telegram_time_active(monday_start, cfg)
        self.assertTrue(active, f"Expected Monday 8:30am to be active: {reason}")

        # Monday 16:30:30 IST (inside 16:30 minute)
        monday_end = datetime(2026, 8, 17, 16, 30, 30, tzinfo=self.ist)
        active, reason = is_telegram_time_active(monday_end, cfg)
        self.assertTrue(active, f"Expected Monday 16:30:30 to be active: {reason}")

        # Monday 07:00 AM IST (before start time)
        monday_early = datetime(2026, 8, 17, 7, 0, 0, tzinfo=self.ist)
        active, reason = is_telegram_time_active(monday_early, cfg)
        self.assertFalse(active)
        self.assertIn("Outside active hours", reason)

        # Monday 17:00 IST (after end time)
        monday_late = datetime(2026, 8, 17, 17, 0, 0, tzinfo=self.ist)
        active, reason = is_telegram_time_active(monday_late, cfg)
        self.assertFalse(active)
        self.assertIn("Outside active hours", reason)

        # Sunday 10:00 AM IST (2026-08-16 is a Sunday)
        sunday_10am = datetime(2026, 8, 16, 10, 0, 0, tzinfo=self.ist)
        active, reason = is_telegram_time_active(sunday_10am, cfg)
        self.assertFalse(active)
        self.assertIn("Outside active weekdays", reason)

        # Saturday 10:00 AM IST (2026-08-15 is a Saturday)
        saturday_10am = datetime(2026, 8, 15, 10, 0, 0, tzinfo=self.ist)
        active, reason = is_telegram_time_active(saturday_10am, cfg)
        self.assertFalse(active)
        self.assertIn("Outside active weekdays", reason)

    def test_utc_message_conversion(self):
        # UTC 03:00 is IST 08:30 (Monday)
        cfg = {
            "TELEGRAM_TIME_FILTER_ENABLED": True,
            "TELEGRAM_START_TIME": "08:30",
            "TELEGRAM_END_TIME": "16:30",
            "TELEGRAM_WEEKDAYS_ONLY": True,
            "TELEGRAM_TIMEZONE": "Asia/Kolkata"
        }
        utc_msg_time = datetime(2026, 8, 17, 3, 0, 0, tzinfo=timezone.utc)
        active, reason = is_telegram_time_active(utc_msg_time, cfg)
        self.assertTrue(active, f"Expected UTC 03:00 (IST 08:30) to be active: {reason}")

        # UTC 02:00 is IST 07:30 (before start)
        utc_early_time = datetime(2026, 8, 17, 2, 0, 0, tzinfo=timezone.utc)
        active, reason = is_telegram_time_active(utc_early_time, cfg)
        self.assertFalse(active)

    def test_get_schedule_description(self):
        cfg_on = {
            "TELEGRAM_TIME_FILTER_ENABLED": True,
            "TELEGRAM_START_TIME": "08:30",
            "TELEGRAM_END_TIME": "16:30",
            "TELEGRAM_WEEKDAYS_ONLY": True,
            "TELEGRAM_TIMEZONE": "Asia/Kolkata"
        }
        self.assertIn("Mon-Fri 08:30 - 16:30 Asia/Kolkata", get_schedule_description(cfg_on))

        cfg_off = {"TELEGRAM_TIME_FILTER_ENABLED": False}
        self.assertIn("Disabled", get_schedule_description(cfg_off))


class MockTradeAnalysis:
    def __init__(self, is_valid=True, underlying="NIFTY", actions=None):
        self.is_valid_trade_msg = is_valid
        self.is_continuation = False
        self.related_open_trade_id = None
        self.trade_status_update = "OPEN"
        self.structure_type = "NIFTY CE BUY"
        self.underlying = underlying
        self.context_summary = "Test analysis"
        self.actions = actions or [
            ActionSchema(
                action_type="BUY",
                option_type="CE",
                strike=24500.0,
                price="100",
                underlying=underlying,
                lots=1,
                is_main=True,
                product="NRML"
            )
        ]

    def model_dump(self):
        return {"underlying": self.underlying, "is_valid": self.is_valid_trade_msg}


@unittest.skipUnless(HAS_DB, "Database / SQLAlchemy dependencies not installed in local environment")
class TestWorkerTimeFilterIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.session = db.SessionLocal()
        # Clean up database
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Trade).delete()
        self.session.query(Message).delete()
        self.session.commit()

    def tearDown(self):
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Trade).delete()
        self.session.query(Message).delete()
        self.session.commit()
        self.session.close()

    @patch("worker.analyze_message_with_ai")
    @patch("worker.resolve_nfo_instrument")
    def test_process_single_message_ingests_and_analyzes_pre_market(self, mock_resolve, mock_analyze):
        """
        Verify that pre-market messages (outside active execution hours) are NOT skipped during ingestion.
        They are fully parsed by AI, saved into the DB, and actionable orders are queued as READY_FOR_MARKET_OPEN.
        """
        mock_analyze.return_value = MockTradeAnalysis()
        mock_resolve.return_value = {
            "tradingsymbol": "NIFTY26AUG24500CE",
            "instrument_token": 12345,
            "lot_size": 65,
            "expiry": "2026-08-27",
            "strike": 24500.0,
            "name": "NIFTY",
            "instrument_type": "CE"
        }

        # Set time filter to active (Mon-Fri 09:15 - 15:30) and simulate pre-market 08:45 AM
        os.environ["TELEGRAM_TIME_FILTER_ENABLED"] = "true"
        os.environ["TELEGRAM_START_TIME"] = "09:15"
        os.environ["TELEGRAM_END_TIME"] = "15:30"
        os.environ["AUTO_PLACE_ORDERS"] = "true"

        pre_market_date = datetime(2026, 8, 17, 8, 45, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))

        msg = Message(
            telegram_message_id=88888,
            channel_id="test_filter_channel",
            text="BUY NIFTY 24500 CE @ 100",
            date=pre_market_date,
            processed=False,
            analysed_by_ai=False,
            revision=0
        )
        self.session.add(msg)
        self.session.commit()
        self.session.refresh(msg)

        try:
            with patch("worker.is_telegram_time_active", return_value=(False, "Outside active hours: 08:45 IST is before 09:15")):
                result = asyncio.run(process_single_message(self.session, msg))
                self.assertTrue(result)

            self.session.refresh(msg)
            # Message is fully ingested & analyzed (not skipped)
            self.assertTrue(msg.processed)
            self.assertTrue(msg.analysed_by_ai)
            self.assertEqual(msg.last_status, "SUCCESS")

            # Trade was created in DB
            trade = self.session.query(Trade).first()
            self.assertIsNotNone(trade)
            self.assertEqual(trade.underlying, "NIFTY")

            # Action was created and held in READY_FOR_MARKET_OPEN queue
            actions = self.session.query(Action).filter(Action.trade_id == trade.id).all()
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].order_status, "READY_FOR_MARKET_OPEN")
            self.assertIn("Held in READY_FOR_MARKET_OPEN queue", actions[0].zerodha_response)

            # Trace record exists for ORDER_HELD_FOR_MARKET_OPEN
            held_trace = self.session.query(MessageStageTrace).filter(
                MessageStageTrace.stage == "ORDER_HELD_FOR_MARKET_OPEN"
            ).first()
            self.assertIsNotNone(held_trace)
            self.assertEqual(held_trace.status, "INFO")
        finally:
            os.environ["TELEGRAM_TIME_FILTER_ENABLED"] = "false"
            os.environ["AUTO_PLACE_ORDERS"] = "false"

    @patch("worker.place_zerodha_order")
    @patch("worker.verify_zerodha_order_confirmation")
    def test_process_ready_for_market_open_orders(self, mock_verif, mock_place):
        """
        Verify that when market hours activate, process_ready_for_market_open_orders automatically
        submits queued READY_FOR_MARKET_OPEN orders to Zerodha.
        """
        os.environ["AUTO_PLACE_ORDERS"] = "true"
        mock_place.return_value = {
            "success": True,
            "order_id": "ORD_OPEN_915",
            "status": "SUBMITTED",
            "message": "Order placed on exchange"
        }
        mock_verif.return_value = {
            "verified": True,
            "confirmed": True,
            "status": "SUBMITTED",
            "filled_quantity": 0,
            "pending_quantity": 65,
            "average_price": 0.0
        }

        # Create pre-existing trade with READY_FOR_MARKET_OPEN action
        msg = Message(telegram_message_id=99991, channel_id="c1", text="BUY NIFTY 24500 CE", processed=True, analysed_by_ai=True)
        self.session.add(msg)
        self.session.commit()

        trade = Trade(status="OPEN", underlying="NIFTY", structure_type="NIFTY BUY CE")
        self.session.add(trade)
        self.session.commit()

        act = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY26AUG24500CE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="READY_FOR_MARKET_OPEN"
        )
        self.session.add(act)
        self.session.commit()

        try:
            # Simulate market hours active (09:15 AM)
            with patch("worker.is_telegram_time_active", return_value=(True, "Within active schedule")):
                results = asyncio.run(process_ready_for_market_open_orders(session=self.session))

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["success"])

            self.session.refresh(act)
            self.assertEqual(act.order_status, "SUBMITTED")
            self.assertEqual(act.zerodha_order_id, "ORD_OPEN_915")

            # Stage trace recorded
            trace = self.session.query(MessageStageTrace).filter(
                MessageStageTrace.stage == "MARKET_OPEN_QUEUE_PROCESSED"
            ).first()
            self.assertIsNotNone(trace)
            self.assertEqual(trace.status, "SUCCESS")
        finally:
            os.environ["AUTO_PLACE_ORDERS"] = "false"

    @patch("worker.place_zerodha_order")
    @patch("worker.verify_zerodha_order_confirmation")
    def test_manual_execution_executes_ready_for_market_open(self, mock_verif, mock_place):
        """
        Verify that manual order placement (e.g. clicking Telegram button) targets and executes
        orders currently in READY_FOR_MARKET_OPEN state.
        """
        mock_place.return_value = {
            "success": True,
            "order_id": "ORD_MANUAL_123",
            "status": "FILLED",
            "filled_quantity": 65,
            "average_price": 105.0,
            "message": "Order executed"
        }
        mock_verif.return_value = {
            "verified": True,
            "confirmed": True,
            "status": "FILLED",
            "filled_quantity": 65,
            "pending_quantity": 0,
            "average_price": 105.0
        }

        msg = Message(telegram_message_id=99992, channel_id="c1", text="BUY NIFTY 24500 CE", processed=True, analysed_by_ai=True)
        self.session.add(msg)
        self.session.commit()

        trade = Trade(status="OPEN", underlying="NIFTY")
        self.session.add(trade)
        self.session.commit()

        act = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY26AUG24500CE",
            quantity=65,
            lots=1,
            order_status="READY_FOR_MARKET_OPEN"
        )
        self.session.add(act)
        self.session.commit()

        # Execute manually (auto_mode=False)
        results = execute_trade_actions(self.session, trade.id, auto_mode=False)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])

        self.session.refresh(act)
        self.assertEqual(act.order_status, "FILLED")
        self.assertEqual(act.zerodha_order_id, "ORD_MANUAL_123")

    def test_multileg_spread_pre_market_queueing(self):
        """
        Verify that multi-leg spread orders held in READY_FOR_MARKET_OPEN queue do not trigger hedge_failed
        for Phase 2 SELL legs, but instead queue both BUY and SELL legs in READY_FOR_MARKET_OPEN.
        """
        msg = Message(telegram_message_id=99993, channel_id="c1", text="SELL NIFTY SPREAD", processed=True, analysed_by_ai=True)
        self.session.add(msg)
        self.session.commit()

        trade = Trade(status="OPEN", underlying="NIFTY", structure_type="NIFTY PE SPREAD")
        self.session.add(trade)
        self.session.commit()

        buy_hedge = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            tradingsymbol="NIFTY26AUG24000PE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="PENDING"
        )
        sell_main = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            tradingsymbol="NIFTY26AUG24500PE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="PENDING"
        )
        self.session.add_all([buy_hedge, sell_main])
        self.session.commit()

        os.environ["AUTO_PLACE_ORDERS"] = "true"
        try:
            with patch("worker.is_telegram_time_active", return_value=(False, "Outside active hours: pre-market 08:30")):
                results = execute_trade_actions(self.session, trade.id, auto_mode=True)

            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["status"], "READY_FOR_MARKET_OPEN")
            self.assertEqual(results[1]["status"], "READY_FOR_MARKET_OPEN")
            self.assertTrue(results[0]["held_for_market_open"])
            self.assertTrue(results[1]["held_for_market_open"])

            self.session.refresh(buy_hedge)
            self.session.refresh(sell_main)
            self.assertEqual(buy_hedge.order_status, "READY_FOR_MARKET_OPEN")
            self.assertEqual(sell_main.order_status, "READY_FOR_MARKET_OPEN")
        finally:
            os.environ["AUTO_PLACE_ORDERS"] = "false"


if __name__ == "__main__":
    unittest.main()

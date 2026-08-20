import os
import unittest
from datetime import datetime, time, timezone, timedelta
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
    from models import Message, MessageStageTrace
    from worker import process_single_message
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


@unittest.skipUnless(HAS_DB, "Database / SQLAlchemy dependencies not installed in local environment")
class TestWorkerTimeFilterIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.session = db.SessionLocal()

    def tearDown(self):
        self.session.close()

    def test_process_single_message_skips_out_of_hours(self):
        # Temporarily enable time filter in env
        os.environ["TELEGRAM_TIME_FILTER_ENABLED"] = "true"
        os.environ["TELEGRAM_START_TIME"] = "08:30"
        os.environ["TELEGRAM_END_TIME"] = "16:30"
        os.environ["TELEGRAM_WEEKDAYS_ONLY"] = "true"
        os.environ["TELEGRAM_TIMEZONE"] = "Asia/Kolkata"

        # Sunday 10:00 AM IST
        sunday_date = datetime(2026, 8, 16, 10, 0, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))

        msg = Message(
            telegram_message_id=88888,
            channel_id="test_filter_channel",
            text="BUY NIFTY 24000 CE @ 100",
            date=sunday_date,
            processed=False,
            analysed_by_ai=False,
            revision=0
        )
        self.session.add(msg)
        self.session.commit()
        self.session.refresh(msg)

        try:
            import asyncio
            result = asyncio.run(process_single_message(self.session, msg))
            self.assertTrue(result)

            self.session.refresh(msg)
            self.assertTrue(msg.processed)
            self.assertTrue(msg.analysed_by_ai)
            self.assertEqual(msg.last_stage, "TIME_WINDOW_FILTER")
            self.assertEqual(msg.last_status, "SKIPPED")

            # Check trace record
            traces = self.session.query(MessageStageTrace).filter(MessageStageTrace.message_id == msg.id).all()
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].stage, "TIME_WINDOW_FILTER")
            self.assertEqual(traces[0].status, "SKIPPED")
        finally:
            os.environ["TELEGRAM_TIME_FILTER_ENABLED"] = "false"
            self.session.query(MessageStageTrace).filter(MessageStageTrace.message_id == msg.id).delete()
            self.session.query(Message).filter(Message.id == msg.id).delete()
            self.session.commit()

if __name__ == "__main__":
    unittest.main()

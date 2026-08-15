import os
import sys
import unittest
from datetime import datetime
import db
from models import Message, Trade, Action, MessageStageTrace
import stage_tracker
from cli import display_message_stage_timeline, format_status_badge

class TestStageTracker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.session = db.SessionLocal()
        # Create a test message
        self.msg = Message(
            telegram_message_id=99999,
            channel_id="test_channel",
            text="BUY NIFTY 24000 CE @ 100",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False,
            revision=0
        )
        self.session.add(self.msg)
        self.session.commit()
        self.session.refresh(self.msg)

    def tearDown(self):
        # Cleanup
        self.session.query(MessageStageTrace).filter(MessageStageTrace.message_id == self.msg.id).delete()
        self.session.query(Action).filter(Action.message_id == self.msg.id).delete()
        self.session.query(Message).filter(Message.id == self.msg.id).delete()
        self.session.commit()
        self.session.close()

    def test_record_stage_point_in_time(self):
        trace = stage_tracker.record_stage(
            stage="SYNC_RECEIVED",
            status="SUCCESS",
            message_id=self.msg.id,
            telegram_message_id=self.msg.telegram_message_id,
            revision=0,
            details={"channel": "test_channel"},
            session=self.session
        )
        self.assertIsNotNone(trace)
        self.assertEqual(trace.stage, "SYNC_RECEIVED")
        self.assertEqual(trace.status, "SUCCESS")
        self.assertEqual(trace.message_id, self.msg.id)
        self.assertTrue("test_diagnostics.py" in trace.location)

        # Check Message summary was updated
        self.session.refresh(self.msg)
        self.assertEqual(self.msg.last_stage, "SYNC_RECEIVED")
        self.assertEqual(self.msg.last_status, "SUCCESS")

    def test_stage_context_success(self):
        with stage_tracker.StageContext("CONTEXT_FETCH", message_id=self.msg.id, revision=0, session=self.session) as ctx:
            ctx.set_details({"open_trades_count": 2})
            ctx.set_status("SUCCESS")

        traces = stage_tracker.get_message_history(self.msg.id, session=self.session)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["stage"], "CONTEXT_FETCH")
        self.assertEqual(traces[0]["status"], "SUCCESS")
        self.assertIsNotNone(traces[0]["duration_ms"])
        self.assertTrue(traces[0]["duration_ms"] >= 0)

    def test_stage_context_exception_captured(self):
        with self.assertRaises(RuntimeError):
            with stage_tracker.StageContext("AI_ANALYSIS", message_id=self.msg.id, revision=0, session=self.session) as ctx:
                raise RuntimeError("Simulated Gemini Quota Exceeded")

        traces = stage_tracker.get_message_history(self.msg.id, session=self.session)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["stage"], "AI_ANALYSIS")
        self.assertEqual(traces[0]["status"], "ERROR")
        self.assertIn("Simulated Gemini Quota Exceeded", traces[0]["error_message"])
        self.assertIsNotNone(traces[0]["stack_trace"])
        self.assertIn("RuntimeError", traces[0]["stack_trace"])

        # Check message table summary
        self.session.refresh(self.msg)
        self.assertEqual(self.msg.last_status, "ERROR")
        self.assertIn("Simulated Gemini Quota Exceeded", self.msg.last_error)

    def test_stuck_messages_query(self):
        # Create an errored stage
        stage_tracker.record_stage(
            stage="ORDER_EXECUTION",
            status="ERROR",
            message_id=self.msg.id,
            error_message="Insufficient margin on Zerodha",
            session=self.session
        )
        stuck = stage_tracker.get_stuck_or_failed_messages(session=self.session)
        matching = [s for s in stuck if s["id"] == self.msg.id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["last_status"], "ERROR")

if __name__ == "__main__":
    unittest.main()

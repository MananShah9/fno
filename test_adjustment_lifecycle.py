import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from worker import (
    get_open_trades_context,
    is_adjustment_planning_text,
    is_adjustment_reminder_text,
    evaluate_and_deduplicate_adjustments,
    process_trade_actions_and_sizing,
    ensure_square_off_actions,
    process_single_message
)
from gemini_client import TradeAnalysisSchema, ActionSchema


class TestTradeAdjustmentLifecycle(unittest.TestCase):
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

        # Set default test environment variables
        os.environ["MAX_ADJUSTMENTS_PER_TRADE"] = "1"
        os.environ["ADJUSTMENT_DEDUPLICATION_WINDOW_MINUTES"] = "30"
        os.environ["ADJUSTMENT_MAX_LOTS"] = "1"
        os.environ["TARGET_INVESTMENT_BUDGET"] = "100000"

    def tearDown(self):
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()
        self.session.close()

    def test_is_adjustment_reminder_text_detection(self):
        """Test keyword detection for conversational averaging commentary, planning and status reminders."""
        self.assertTrue(is_adjustment_planning_text("We are planning to average at 241"))
        self.assertTrue(is_adjustment_planning_text("Plan to average around 240"))
        self.assertTrue(is_adjustment_reminder_text("Average around 235-241"))
        self.assertTrue(is_adjustment_reminder_text("If you missed it, add in 220s"))
        self.assertTrue(is_adjustment_reminder_text("add that lot now"))
        self.assertTrue(is_adjustment_reminder_text("We have averaged earlier"))
        self.assertTrue(is_adjustment_reminder_text("Keep holding average position"))
        self.assertTrue(is_adjustment_reminder_text("Average done, now wait"))

        self.assertFalse(is_adjustment_reminder_text("SELL NIFTY 24000 PE @ 183"))
        self.assertFalse(is_adjustment_reminder_text("SL hit exit full position"))
        self.assertFalse(is_adjustment_reminder_text(None))

    def test_get_open_trades_context_includes_adjustment_state(self):
        """Verify open trades context serializes adjustment lifecycle state for Gemini."""
        msg = Message(id=1, text="SELL NIFTY 24000 PE", date=datetime.utcnow())
        self.session.add(msg)
        self.session.commit()

        trade = Trade(
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY PE SPREAD",
            max_adjustments=2,
            adjustment_count=1,
            last_adjustment_at=datetime(2026, 8, 18, 10, 30, 0),
            last_adjustment_price=240.0
        )
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        act = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            is_adjustment=True,
            adjustment_number=1,
            underlying="NIFTY",
            strike=24000.0,
            tradingsymbol="NIFTY26AUG24000PE",
            quantity=65,
            lots=1
        )
        self.session.add(act)
        self.session.commit()

        context = get_open_trades_context(self.session)
        self.assertEqual(len(context), 1)
        t_data = context[0]
        self.assertEqual(t_data["id"], trade.id)
        self.assertEqual(t_data["adjustment_count"], 1)
        self.assertEqual(t_data["max_adjustments"], 2)
        self.assertEqual(t_data["last_adjustment_price"], 240.0)
        self.assertIsNotNone(t_data["last_adjustment_at"])
        self.assertEqual(len(t_data["existing_orders"]), 1)
        self.assertTrue(t_data["existing_orders"][0]["is_adjustment"])

    def test_multi_message_narrative_averaging_flow(self):
        """
        Simulate the exact channel sequence (Messages 69, 71, 72, 75):
        - Trade already open (1 lot initial).
        - Msg 69: "We are planning to average at 241" -> Reminder commentary (zero orders).
        - Msg 71: "Start averaging" -> Approved Adjustment #1 (exactly 1 lot).
        - Msg 72: "Average around 235-241" (1 min later) -> Deduplicated within window (zero orders).
        - Msg 75: "If you missed it, add in 220s" (5 mins later) -> Max adjustments limit & window deduplicated (zero orders).
        Result: Exactly 1 adjustment lot added instead of runaway 31 lots.
        """
        now = datetime(2026, 8, 18, 10, 0, 0)

        # 1. Initial Trade Setup
        init_msg = Message(id=60, telegram_message_id=60, text="SELL NIFTY 24000 PE @ 183", date=now)
        self.session.add(init_msg)
        self.session.commit()

        trade = Trade(
            id=10,
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY PE SELL",
            opened_at=now,
            max_adjustments=1,
            adjustment_count=0
        )
        self.session.add(trade)
        self.session.commit()

        initial_action = Action(
            trade_id=trade.id,
            message_id=init_msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            is_adjustment=False,
            underlying="NIFTY",
            strike=24000.0,
            tradingsymbol="NIFTY26AUG24000PE",
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add(initial_action)
        self.session.commit()

        # -------------------------------------------------------------
        # Message 69 (T+10 min): "We are planning to average at 241"
        # -------------------------------------------------------------
        msg_69 = Message(id=69, telegram_message_id=69, text="We are planning to average at 241", date=now + timedelta(minutes=10))
        self.session.add(msg_69)
        self.session.commit()

        analysis_69 = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            is_adjustment=True,
            is_adjustment_reminder=True,  # Classified as reminder
            related_open_trade_id=trade.id,
            underlying="NIFTY",
            actions=[
                ActionSchema(action_type="SELL", option_type="PE", strike=24000.0, price="241", underlying="NIFTY")
            ]
        )

        is_adj_69 = evaluate_and_deduplicate_adjustments(self.session, trade, msg_69, analysis_69)
        self.assertTrue(is_adj_69)
        self.assertEqual(analysis_69.actions[0].action_type, "INFO")
        self.assertIn("planning commentary", analysis_69.actions[0].details)
        self.assertEqual(trade.adjustment_count, 0)

        traces_69 = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.message_id == msg_69.id,
            MessageStageTrace.stage == "ADJUSTMENT_REMINDER_DETECTED"
        ).all()
        self.assertEqual(len(traces_69), 1)
        self.assertEqual(traces_69[0].status, "INFO")

        # -------------------------------------------------------------
        # Message 71 (T+12 min): "Start averaging" (First execution instruction)
        # -------------------------------------------------------------
        msg_71 = Message(id=71, telegram_message_id=71, text="Start averaging", date=now + timedelta(minutes=12))
        self.session.add(msg_71)
        self.session.commit()

        analysis_71 = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            is_adjustment=True,
            is_adjustment_reminder=False,  # Explicit command
            related_open_trade_id=trade.id,
            underlying="NIFTY",
            actions=[
                ActionSchema(action_type="SELL", option_type="PE", strike=24000.0, price="240", underlying="NIFTY", lots=1)
            ]
        )

        is_adj_71 = evaluate_and_deduplicate_adjustments(self.session, trade, msg_71, analysis_71)
        self.assertTrue(is_adj_71)
        self.assertEqual(analysis_71.actions[0].action_type, "SELL")
        self.assertTrue(analysis_71.actions[0].is_adjustment)
        self.assertEqual(trade.adjustment_count, 1)
        self.assertEqual(trade.last_adjustment_price, 240.0)

        # Process trade actions and sizing
        actions_71 = process_trade_actions_and_sizing(trade, msg_71.id, analysis_71.actions, target_budget=100000.0)
        self.assertEqual(len(actions_71), 1)
        self.assertEqual(actions_71[0].lots, 1)  # Capped at ADJUSTMENT_MAX_LOTS = 1
        self.assertTrue(actions_71[0].is_adjustment)
        self.assertEqual(actions_71[0].adjustment_number, 1)

        for act in actions_71:
            act.order_status = "PLACED"
            self.session.add(act)
        self.session.commit()

        traces_71 = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.message_id == msg_71.id,
            MessageStageTrace.stage == "ADJUSTMENT_APPROVED"
        ).all()
        self.assertEqual(len(traces_71), 1)
        self.assertEqual(traces_71[0].status, "SUCCESS")

        # -------------------------------------------------------------
        # Message 72 (T+13 min): "Average around 235-241" (1 minute later)
        # -------------------------------------------------------------
        msg_72 = Message(id=72, telegram_message_id=72, text="Average around 235-241", date=now + timedelta(minutes=13))
        self.session.add(msg_72)
        self.session.commit()

        analysis_72 = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            is_adjustment=True,
            is_adjustment_reminder=False,
            related_open_trade_id=trade.id,
            underlying="NIFTY",
            actions=[
                ActionSchema(action_type="SELL", option_type="PE", strike=24000.0, price="235-241", underlying="NIFTY")
            ]
        )

        is_adj_72 = evaluate_and_deduplicate_adjustments(self.session, trade, msg_72, analysis_72)
        self.assertTrue(is_adj_72)
        self.assertEqual(analysis_72.actions[0].action_type, "INFO")
        self.assertEqual(trade.adjustment_count, 1)  # Count did not increase

        traces_72 = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.message_id == msg_72.id
        ).all()
        # Triggered ADJUSTMENT_LIMIT_REACHED or ADJUSTMENT_DEDUPLICATED
        stages_72 = [t.stage for t in traces_72]
        self.assertTrue("ADJUSTMENT_LIMIT_REACHED" in stages_72 or "ADJUSTMENT_DEDUPLICATED" in stages_72 or "ADJUSTMENT_REMINDER_DETECTED" in stages_72)

        # -------------------------------------------------------------
        # Message 75 (T+17 min): "If you missed it, add in 220s" (5 mins later)
        # -------------------------------------------------------------
        msg_75 = Message(id=75, telegram_message_id=75, text="If you missed it, add in 220s", date=now + timedelta(minutes=17))
        self.session.add(msg_75)
        self.session.commit()

        analysis_75 = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            is_adjustment=True,
            is_adjustment_reminder=True,
            related_open_trade_id=trade.id,
            underlying="NIFTY",
            actions=[
                ActionSchema(action_type="SELL", option_type="PE", strike=24000.0, price="220s", underlying="NIFTY")
            ]
        )

        is_adj_75 = evaluate_and_deduplicate_adjustments(self.session, trade, msg_75, analysis_75)
        self.assertTrue(is_adj_75)
        self.assertEqual(analysis_75.actions[0].action_type, "INFO")
        self.assertEqual(trade.adjustment_count, 1)

        # -------------------------------------------------------------
        # FINAL VERIFICATION: Check total entry actions and lots for Trade
        # -------------------------------------------------------------
        trade_entry_actions = self.session.query(Action).filter(
            Action.trade_id == trade.id,
            Action.action_type.in_(["BUY", "SELL"])
        ).all()

        self.assertEqual(len(trade_entry_actions), 2)  # 1 initial + 1 averaging
        total_lots = sum(a.lots or 1 for a in trade_entry_actions)
        self.assertEqual(total_lots, 2)  # Exactly 2 lots, NOT 31 lots!

    def test_max_adjustments_cap_blocks_excess_averaging(self):
        """Verify that trade.max_adjustments strictly caps total averaging events."""
        trade = Trade(
            status="OPEN",
            underlying="TATASTEEL",
            max_adjustments=1,
            adjustment_count=1,  # Already at max 1
            last_adjustment_at=datetime.utcnow() - timedelta(hours=2)  # Outside rolling window
        )
        self.session.add(trade)
        self.session.commit()

        msg = Message(id=201, text="Sell 1 more lot of Tata Steel 190 PE", date=datetime.utcnow())
        self.session.add(msg)
        self.session.commit()

        analysis = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            is_adjustment=True,
            related_open_trade_id=trade.id,
            actions=[ActionSchema(action_type="SELL", strike=190.0, option_type="PE", underlying="TATASTEEL")]
        )

        is_adj = evaluate_and_deduplicate_adjustments(self.session, trade, msg, analysis)
        self.assertTrue(is_adj)
        self.assertEqual(analysis.actions[0].action_type, "INFO")
        self.assertIn("Averaging blocked", analysis.actions[0].details)

        trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.message_id == msg.id,
            MessageStageTrace.stage == "ADJUSTMENT_LIMIT_REACHED"
        ).first()
        self.assertIsNotNone(trace)
        self.assertEqual(trace.status, "WARNING")

    def test_rolling_window_deduplication(self):
        """Verify deduplication when two averaging instructions arrive within rolling window."""
        trade = Trade(
            status="OPEN",
            underlying="NIFTY",
            max_adjustments=3,  # Allows up to 3 adjustments
            adjustment_count=1,
            last_adjustment_at=datetime.utcnow() - timedelta(minutes=5)  # 5 minutes ago (within 30-min window)
        )
        self.session.add(trade)
        self.session.commit()

        msg = Message(id=301, text="Add 1 more lot 24000 PE", date=datetime.utcnow())
        self.session.add(msg)
        self.session.commit()

        analysis = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            is_adjustment=True,
            is_adjustment_reminder=False,
            related_open_trade_id=trade.id,
            actions=[ActionSchema(action_type="SELL", strike=24000.0, option_type="PE", underlying="NIFTY")]
        )

        is_adj = evaluate_and_deduplicate_adjustments(self.session, trade, msg, analysis)
        self.assertTrue(is_adj)
        self.assertEqual(analysis.actions[0].action_type, "INFO")
        self.assertIn("Averaging deduplicated", analysis.actions[0].details)

        trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.message_id == msg.id,
            MessageStageTrace.stage == "ADJUSTMENT_DEDUPLICATED"
        ).first()
        self.assertIsNotNone(trace)
        self.assertEqual(trace.status, "INFO")

    def test_net_quantity_square_off_on_averaged_trade(self):
        """
        Verify that when an averaged trade is closed/exited,
        ensure_square_off_actions computes the NET AGGREGATE entry quantity (initial + averaged)
        and squares off 100% of open lots (e.g. 130 qty = 2 lots) without leaving orphaned legs.
        """
        msg_1 = Message(id=1, text="SELL NIFTY 24000 PE @ 183", date=datetime.utcnow())
        msg_2 = Message(id=2, text="Start averaging NIFTY 24000 PE @ 240", date=datetime.utcnow())
        self.session.add(msg_1)
        self.session.add(msg_2)
        self.session.commit()

        trade = Trade(
            id=55,
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY PE SPREAD",
            adjustment_count=1
        )
        self.session.add(trade)
        self.session.commit()

        # Initial entry: 1 lot (65 qty)
        entry_1 = Action(
            trade_id=trade.id,
            message_id=msg_1.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            is_adjustment=False,
            tradingsymbol="NIFTY26AUG24000PE",
            strike=24000.0,
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        # Averaged entry: 1 lot (65 qty)
        entry_2 = Action(
            trade_id=trade.id,
            message_id=msg_2.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            is_adjustment=True,
            adjustment_number=1,
            tradingsymbol="NIFTY26AUG24000PE",
            strike=24000.0,
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add(entry_1)
        self.session.add(entry_2)
        self.session.commit()

        exit_msg = Message(id=3, text="SL hit Exit full position", date=datetime.utcnow())
        self.session.add(exit_msg)
        self.session.commit()

        sq_actions = ensure_square_off_actions(self.session, trade, exit_msg)

        # There should be exactly 1 square-off action covering the full 130 qty (2 lots)
        self.assertEqual(len(sq_actions), 1)
        sq_act = sq_actions[0]
        self.assertEqual(sq_act.action_type, "EXIT")
        self.assertEqual(sq_act.transaction_type, "BUY")
        self.assertEqual(sq_act.tradingsymbol, "NIFTY26AUG24000PE")
        self.assertEqual(sq_act.quantity, 130)  # 65 + 65
        self.assertEqual(sq_act.lots, 2)        # 1 + 1

    def test_adjustment_sizing_capped_at_adjustment_max_lots(self):
        """
        Verify that an adjustment leg is sized using ADJUSTMENT_MAX_LOTS (default 1)
        even if the account budget is 10 Lakhs.
        """
        os.environ["ADJUSTMENT_MAX_LOTS"] = "1"
        os.environ["TARGET_INVESTMENT_BUDGET"] = "1000000"  # 10 Lakhs

        trade = Trade(id=77, underlying="NIFTY", status="OPEN", adjustment_count=1)
        parsed_actions = [
            ActionSchema(
                action_type="SELL",
                option_type="PE",
                strike=24000.0,
                underlying="NIFTY",
                price="240.0",
                is_adjustment=True,
                lots=1
            )
        ]

        actions = process_trade_actions_and_sizing(
            trade=trade,
            db_message_id=999,
            parsed_actions=parsed_actions,
            target_budget=1000000.0
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].lots, 1)  # Capped at 1 lot, not sized to 4 lots from 10L budget
        self.assertTrue(actions[0].is_adjustment)
        self.assertEqual(actions[0].adjustment_number, 1)


if __name__ == "__main__":
    unittest.main()

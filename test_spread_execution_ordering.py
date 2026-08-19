import os
import unittest
from unittest.mock import patch, MagicMock, call
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from worker import (
    execute_trade_actions, process_trade_actions_and_sizing, ensure_square_off_actions,
    format_important_notice_telegram_html, process_single_message
)
from zerodha_client import (
    get_zerodha_order_status,
    verify_zerodha_order_confirmation,
    place_zerodha_order
)
from stage_tracker import record_stage


class MockActionSchema:
    def __init__(self, action_type="BUY", option_type="PE", strike=None, price=None, underlying=None, lots=1, is_main=None, product="NRML"):
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


class TestSpreadExecutionOrdering(unittest.TestCase):
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

        # Create base message and trade
        self.msg = Message(
            telegram_message_id=5001,
            channel_id="test_spread_channel",
            text="SELL NIFTY 24600 PE @150, BUY NIFTY 24300 PE @45",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False,
            revision=0
        )
        self.session.add(self.msg)
        self.session.commit()
        self.session.refresh(self.msg)

        self.trade = Trade(
            status="OPEN",
            structure_type="NIFTY BULL PUT SPREAD",
            underlying="NIFTY",
            opened_at=datetime.utcnow()
        )
        self.session.add(self.trade)
        self.session.commit()
        self.session.refresh(self.trade)

    def tearDown(self):
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()
        self.session.close()

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_credit_spread_executes_buy_hedge_before_sell_short(self, mock_dedup, mock_place):
        """
        Verify that for a credit spread with SELL and BUY legs,
        the execution engine ALWAYS places the BUY (hedge) leg first,
        waits for confirmation, and places the SELL (short) leg second.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        mock_place.side_effect = [
            {"success": True, "order_id": "ORD_BUY_101", "status": "COMPLETE", "message": "BUY order placed"},
            {"success": True, "order_id": "ORD_SELL_102", "status": "COMPLETE", "message": "SELL order placed"}
        ]

        # Intentionally create SELL action first in DB (lower ID) to test sorting
        sell_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            option_type="PE",
            strike=24600.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824600PE",
            order_type="MARKET",
            order_status="PENDING"
        )
        buy_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            option_type="PE",
            strike=24300.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824300PE",
            order_type="MARKET",
            order_status="PENDING"
        )
        self.session.add(sell_act)
        self.session.add(buy_act)
        self.session.commit()

        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False)

        self.assertEqual(len(results), 2)
        # Verify call ordering on mock_place
        self.assertEqual(mock_place.call_count, 2)
        first_call = mock_place.call_args_list[0]
        second_call = mock_place.call_args_list[1]

        # First call MUST be the BUY (hedge) leg
        self.assertEqual(first_call.kwargs["tradingsymbol"], "NIFTY2681824300PE")
        self.assertEqual(first_call.kwargs["transaction_type"], "BUY")

        # Second call MUST be the SELL (short) leg
        self.assertEqual(second_call.kwargs["tradingsymbol"], "NIFTY2681824600PE")
        self.assertEqual(second_call.kwargs["transaction_type"], "SELL")

        # Check DB states
        self.session.refresh(buy_act)
        self.session.refresh(sell_act)
        self.assertIn(buy_act.order_status, ["FILLED", "COMPLETE", "PLACED"])
        self.assertEqual(buy_act.zerodha_order_id, "ORD_BUY_101")
        self.assertIn(sell_act.order_status, ["FILLED", "COMPLETE", "PLACED"])
        self.assertEqual(sell_act.zerodha_order_id, "ORD_SELL_102")

        # Verify HEDGE_LEGS_CONFIRMED stage was recorded
        traces = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == self.trade.id,
            MessageStageTrace.stage == "HEDGE_LEGS_CONFIRMED"
        ).all()
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].status, "SUCCESS")

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_two_phase_verification_aborts_sell_when_buy_fails(self, mock_dedup, mock_place):
        """
        Verify that if the BUY (hedge) leg fails or is rejected,
        the short (SELL) leg placement is IMMEDIATELY ABORTED to protect
        against naked short margin rejection and unhedged market risk.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        # BUY order fails
        mock_place.return_value = {
            "success": False,
            "order_id": None,
            "status": "FAILED",
            "message": "RMS: Margin Insufficient. Required: 45000, Available: 10000"
        }

        sell_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            option_type="PE",
            strike=24600.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824600PE",
            order_type="MARKET",
            order_status="PENDING"
        )
        buy_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            option_type="PE",
            strike=24300.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824300PE",
            order_type="MARKET",
            order_status="PENDING"
        )
        self.session.add(sell_act)
        self.session.add(buy_act)
        self.session.commit()

        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False)

        self.assertEqual(len(results), 2)
        # mock_place should ONLY be called ONCE (for the BUY leg)
        self.assertEqual(mock_place.call_count, 1)
        self.assertEqual(mock_place.call_args.kwargs["tradingsymbol"], "NIFTY2681824300PE")
        self.assertEqual(mock_place.call_args.kwargs["transaction_type"], "BUY")

        # Check DB states
        self.session.refresh(buy_act)
        self.session.refresh(sell_act)

        # BUY act is FAILED
        self.assertEqual(buy_act.order_status, "FAILED")
        # SELL act is FAILED due to abortion
        self.assertEqual(sell_act.order_status, "FAILED")
        self.assertIn("Two-phase verification failed", sell_act.zerodha_response)
        self.assertIn("Long hedge leg", sell_act.zerodha_response)

        # Verify ORDER_EXECUTION_BLOCKED stage trace was recorded
        traces = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == self.trade.id,
            MessageStageTrace.stage == "ORDER_EXECUTION_BLOCKED"
        ).all()
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].status, "ERROR")

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_two_phase_verification_proceeds_when_buy_deduplicated(self, mock_dedup, mock_place):
        """
        Verify that if the BUY (hedge) leg is already open / deduplicated in Zerodha,
        it is recognized as confirmed, and Phase 2 safely proceeds to place the SELL leg.
        """
        def dedup_side_effect(tradingsymbol, transaction_type, quantity, is_exit=False):
            if transaction_type == "BUY":
                return {
                    "duplicate": True,
                    "reason": "position_already_open",
                    "order_id": "DEDUP_BUY_EXISTING",
                    "message": "Net position already 65 on Zerodha"
                }
            return {"duplicate": False, "reason": None, "order_id": None, "message": ""}

        mock_dedup.side_effect = dedup_side_effect
        mock_place.return_value = {
            "success": True,
            "order_id": "ORD_SELL_201",
            "status": "COMPLETE",
            "message": "SELL order placed successfully"
        }

        sell_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            option_type="PE",
            strike=24600.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824600PE",
            order_type="MARKET",
            order_status="PENDING"
        )
        buy_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            option_type="PE",
            strike=24300.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824300PE",
            order_type="MARKET",
            order_status="PENDING"
        )
        self.session.add(sell_act)
        self.session.add(buy_act)
        self.session.commit()

        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False)

        self.assertEqual(len(results), 2)
        # place_zerodha_order was called only for SELL leg (since BUY was deduplicated)
        self.assertEqual(mock_place.call_count, 1)
        self.assertEqual(mock_place.call_args.kwargs["tradingsymbol"], "NIFTY2681824600PE")
        self.assertEqual(mock_place.call_args.kwargs["transaction_type"], "SELL")

        self.session.refresh(buy_act)
        self.session.refresh(sell_act)
        self.assertIn(buy_act.order_status, ["FILLED", "PLACED"])
        self.assertIn(sell_act.order_status, ["FILLED", "COMPLETE", "PLACED"])

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_square_off_exits_short_with_buy_before_selling_hedge(self, mock_dedup, mock_place):
        """
        Verify that on square-off / exit:
        The short leg is closed with a BUY order FIRST (covering short exposure),
        and the hedge leg is closed with a SELL order SECOND.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        mock_place.side_effect = [
            {"success": True, "order_id": "EXIT_BUY_301", "status": "COMPLETE", "message": "Exit BUY order placed"},
            {"success": True, "order_id": "EXIT_SELL_302", "status": "COMPLETE", "message": "Exit SELL order placed"}
        ]

        # Prior entry actions: Short 24600 PE and Long 24300 PE
        entry_sell = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            option_type="PE",
            strike=24600.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824600PE",
            order_type="MARKET",
            order_status="PLACED"
        )
        entry_buy = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            option_type="PE",
            strike=24300.0,
            quantity=65,
            lots=1,
            tradingsymbol="NIFTY2681824300PE",
            order_type="MARKET",
            order_status="PLACED"
        )
        self.session.add(entry_sell)
        self.session.add(entry_buy)
        self.session.commit()

        # Generate square-off actions
        sq_actions = ensure_square_off_actions(self.session, self.trade, self.msg)
        self.assertEqual(len(sq_actions), 2)

        # In square-off list, BUY (closing short) MUST be first, SELL (closing hedge) MUST be second
        self.assertEqual(sq_actions[0].transaction_type, "BUY")
        self.assertEqual(sq_actions[0].tradingsymbol, "NIFTY2681824600PE")
        self.assertEqual(sq_actions[1].transaction_type, "SELL")
        self.assertEqual(sq_actions[1].tradingsymbol, "NIFTY2681824300PE")

        # Execute square off
        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False)
        self.assertEqual(len(results), 2)

        first_call = mock_place.call_args_list[0]
        second_call = mock_place.call_args_list[1]
        self.assertEqual(first_call.kwargs["transaction_type"], "BUY")
        self.assertEqual(first_call.kwargs["tradingsymbol"], "NIFTY2681824600PE")
        self.assertEqual(second_call.kwargs["transaction_type"], "SELL")
        self.assertEqual(second_call.kwargs["tradingsymbol"], "NIFTY2681824300PE")

    @patch("zerodha_client.get_zerodha_client")
    def test_get_zerodha_order_status_order_history_success(self, mock_get_kite):
        """Test get_zerodha_order_status retrieves status from order_history."""
        mock_kite = MagicMock()
        mock_kite.order_history.return_value = [
            {"status": "VALIDATION PENDING"},
            {"status": "COMPLETE", "status_message": None, "filled_quantity": 65, "pending_quantity": 0, "average_price": 45.5}
        ]
        mock_get_kite.return_value = mock_kite

        status_info = get_zerodha_order_status("ORD_12345")
        self.assertTrue(status_info["success"])
        self.assertTrue(status_info["confirmed"])
        self.assertIn(status_info["status"], ["FILLED", "COMPLETE"])
        self.assertEqual(status_info["raw_status"], "COMPLETE")
        self.assertEqual(status_info["filled_quantity"], 65)

    @patch("zerodha_client.get_zerodha_client")
    def test_get_zerodha_order_status_rejection_detected(self, mock_get_kite):
        """Test get_zerodha_order_status detects RMS rejection."""
        mock_kite = MagicMock()
        mock_kite.order_history.return_value = [
            {"status": "REJECTED", "status_message": "RMS: Margin Insufficient", "filled_quantity": 0}
        ]
        mock_get_kite.return_value = mock_kite

        status_info = get_zerodha_order_status("ORD_REJ_999")
        self.assertFalse(status_info["success"])
        self.assertFalse(status_info["confirmed"])
        self.assertEqual(status_info["status"], "REJECTED")
        self.assertEqual(status_info["status_message"], "RMS: Margin Insufficient")

    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_catches_immediate_rms_rejection(self, mock_get_kite):
        """Test place_zerodha_order catches immediate RMS rejection during verification."""
        mock_kite = MagicMock()
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_MARKET = "MARKET"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.place_order.return_value = "230816000001"
        mock_kite.order_history.return_value = [
            {"status": "REJECTED", "status_message": "RMS: Margin Insufficient"}
        ]
        mock_get_kite.return_value = mock_kite

        res = place_zerodha_order(
            tradingsymbol="NIFTY2681824600PE",
            transaction_type="SELL",
            quantity=65,
            verify_confirmation=True
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("rejected by Zerodha RMS", res["message"])

    def test_format_important_notice_telegram_html(self):
        """Test format_important_notice_telegram_html generates formatted alert with failure reasons."""
        failed_act = Action(
            id=99,
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="FAILED",
            zerodha_response="Two-phase verification failed: Long hedge leg failed confirmation."
        )

        html_text = format_important_notice_telegram_html(self.trade, [failed_act])
        self.assertIn("IMPORTANT NOTICE: ACTION(S) NOT EXECUTED", html_text)
        self.assertIn(f"#{self.trade.id}", html_text)
        self.assertIn("NIFTY2681824600PE", html_text)
        self.assertIn("Two-phase verification failed", html_text)
        self.assertIn("Manual Action Required", html_text)

    @patch("worker.client.send_message")
    @patch("worker.analyze_message_with_ai")
    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_important_notice_sent_to_telegram_when_action_unexecuted(self, mock_dedup, mock_place, mock_ai, mock_send):
        """
        Verify that when an action cannot be executed (e.g. order fails),
        an immediate 'IMPORTANT NOTICE' is sent to the Telegram actions channel.
        """
        import asyncio
        from gemini_client import TradeAnalysisSchema, ActionSchema

        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        # Order placement fails
        mock_place.return_value = {
            "success": False,
            "order_id": None,
            "status": "FAILED",
            "message": "RMS: Margin Insufficient"
        }

        mock_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            underlying="NIFTY",
            structure_type="NIFTY PE SPREAD",
            actions=[
                ActionSchema(action_type="SELL", option_type="PE", strike=24600.0, price="150.0", underlying="NIFTY", lots=1, is_main=True),
                ActionSchema(action_type="BUY", option_type="PE", strike=24300.0, price="45.0", underlying="NIFTY", lots=1, is_main=False)
            ]
        )

        mock_send.return_value = MagicMock()

        # Enable auto placement for test
        os.environ["AUTO_PLACE_ORDERS"] = "true"

        try:
            msg_to_process = Message(
                telegram_message_id=6001,
                channel_id="test_channel",
                text="SELL NIFTY 24600 PE @150, BUY NIFTY 24300 PE @45",
                date=datetime.utcnow(),
                processed=False,
                analysed_by_ai=False,
                revision=0
            )
            self.session.add(msg_to_process)
            self.session.commit()
            self.session.refresh(msg_to_process)

            fake_actions_entity = MagicMock()
            asyncio.run(process_single_message(self.session, msg_to_process, actions_entity=fake_actions_entity))

            # Verify send_message was called at least twice:
            # 1. Main Action summary message
            # 2. IMPORTANT NOTICE alert message
            self.assertGreaterEqual(mock_send.call_count, 2)

            # Check that one of the sent messages is the IMPORTANT NOTICE
            sent_texts = [call_args[0][1] for call_args in mock_send.call_args_list]
            has_notice = any("IMPORTANT NOTICE: ACTION(S) NOT EXECUTED" in text for text in sent_texts)
            self.assertTrue(has_notice, "Important Notice was not sent to Telegram actions channel")

            # Check TELEGRAM_IMPORTANT_NOTICE_SENT stage trace was recorded
            traces = self.session.query(MessageStageTrace).filter(
                MessageStageTrace.message_id == msg_to_process.id,
                MessageStageTrace.stage == "TELEGRAM_IMPORTANT_NOTICE_SENT"
            ).all()
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].status, "WARNING")

        finally:
            os.environ["AUTO_PLACE_ORDERS"] = "false"

    @patch("worker.client.send_message")
    @patch("worker.analyze_message_with_ai")
    @patch("worker.verify_zerodha_positions_zero")
    def test_trade_position_closure_verification_stage_recorded(self, mock_verif_zero, mock_ai, mock_send):
        """
        Verify that when a trade is closed by an exit alert,
        the TRADE_POSITION_CLOSURE_VERIFICATION stage trace is recorded.
        """
        import asyncio
        from gemini_client import TradeAnalysisSchema

        # Existing open trade in database with 1 leg
        trade = Trade(underlying="NIFTY", status="OPEN", structure_type="NIFTY PE SPREAD")
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        entry_act = Action(
            trade_id=trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add(entry_act)
        self.session.commit()

        mock_verif_zero.return_value = {
            "all_zero": True,
            "open_positions": {},
            "positions": {"NIFTY2681824600PE": 0},
            "verified": True,
            "message": "All 1 associated position(s) confirmed ZERO on Zerodha."
        }

        mock_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            related_open_trade_id=trade.id,
            underlying="NIFTY",
            trade_status_update="CLOSED",
            actions=[]
        )
        mock_send.return_value = MagicMock()

        exit_msg = Message(
            telegram_message_id=7001,
            channel_id="test_channel",
            text="SL hit close full position",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False,
            revision=0
        )
        self.session.add(exit_msg)
        self.session.commit()

        asyncio.run(process_single_message(self.session, exit_msg, actions_entity=None))

        # Check TRADE_POSITION_CLOSURE_VERIFICATION stage trace
        trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.message_id == exit_msg.id,
            MessageStageTrace.stage == "TRADE_POSITION_CLOSURE_VERIFICATION"
        ).first()

        self.assertIsNotNone(trace)
        self.assertEqual(trace.status, "SUCCESS")
        self.assertIn("all_zero", trace.details or "")

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_failed_action_not_retried_automatically_by_default(self, mock_dedup, mock_place):
        """
        Verify that execute_trade_actions with allow_failed_retry=False (default)
        NEVER queries or retries actions marked with terminal FAILED status.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        failed_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=True,
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="FAILED",
            zerodha_response="RMS: Margin Insufficient"
        )
        self.session.add(failed_act)
        self.session.commit()

        # Automated execution should NOT pick up FAILED actions
        results = execute_trade_actions(self.session, self.trade.id, auto_mode=True, allow_failed_retry=False)
        self.assertEqual(len(results), 0)
        mock_place.assert_not_called()

    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_failed_action_retried_when_allow_failed_retry_explicitly_true(self, mock_dedup, mock_place):
        """
        Verify that execute_trade_actions with allow_failed_retry=True (dedicated manual retry workflow)
        successfully retries actions marked with FAILED status.
        """
        mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}
        mock_place.return_value = {
            "success": True,
            "order_id": "RETRY_ORD_888",
            "status": "COMPLETE",
            "message": "Order placed successfully"
        }

        failed_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=True,
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="FAILED",
            zerodha_response="RMS: Margin Insufficient"
        )
        self.session.add(failed_act)
        self.session.commit()

        # Manual retry workflow should execute the failed action
        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False, allow_failed_retry=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["order_id"], "RETRY_ORD_888")
        mock_place.assert_called_once()

    @patch("worker.client.send_message")
    @patch("worker.analyze_message_with_ai")
    @patch("worker.place_zerodha_order")
    @patch("worker.check_existing_zerodha_order_or_position")
    def test_subsequent_chat_update_does_not_retry_failed_trade_action(self, mock_dedup, mock_place, mock_ai, mock_send):
        """
        Verify that when an entry order previously failed, subsequent chat updates
        (e.g., SL commentary or trade plan update) DO NOT re-submit the failed entry order to Zerodha.
        """
        import asyncio
        from gemini_client import TradeAnalysisSchema

        os.environ["AUTO_PLACE_ORDERS"] = "true"
        try:
            mock_dedup.return_value = {"duplicate": False, "reason": None, "order_id": None, "message": ""}

            trade = Trade(underlying="TATASTEEL", status="OPEN", structure_type="STOCK PE BUY")
            self.session.add(trade)
            self.session.commit()
            self.session.refresh(trade)

            # Historical failed entry action from earlier message
            failed_entry_act = Action(
                trade_id=trade.id,
                message_id=self.msg.id,
                action_type="BUY",
                transaction_type="BUY",
                is_main=True,
                tradingsymbol="TATASTEEL26AUG192.5PE",
                quantity=5500,
                lots=1,
                order_type="MARKET",
                order_status="FAILED",
                zerodha_response="Price outside circuit limits"
            )
            self.session.add(failed_entry_act)
            self.session.commit()

            # Subsequent informational / plan update message (e.g. Message 254: "Tata steel Trade Plan: SL when stock price hits 186")
            mock_ai.return_value = TradeAnalysisSchema(
                is_valid_trade_msg=True,
                is_continuation=True,
                related_open_trade_id=trade.id,
                underlying="TATASTEEL",
                structure_type="STOCK PE BUY",
                actions=[]  # No new order actions in this update message
            )
            mock_send.return_value = MagicMock()

            update_msg = Message(
                telegram_message_id=8001,
                channel_id="test_channel",
                text="Tata steel Trade Plan: SL when stock price hits 186",
                date=datetime.utcnow(),
                processed=False,
                analysed_by_ai=False,
                revision=0
            )
            self.session.add(update_msg)
            self.session.commit()

            asyncio.run(process_single_message(self.session, update_msg, actions_entity=None))

            # Broker place_order MUST NOT be called for the historical failed action
            mock_place.assert_not_called()

            # Verify action status remains FAILED
            self.session.refresh(failed_entry_act)
            self.assertEqual(failed_entry_act.order_status, "FAILED")
        finally:
            os.environ["AUTO_PLACE_ORDERS"] = "false"


if __name__ == "__main__":
    unittest.main()

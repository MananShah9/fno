import os
import unittest
from unittest.mock import patch, MagicMock, call
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from worker import (
    compute_trade_net_positions,
    ensure_square_off_actions,
    reconcile_active_orders,
    cancel_unfilled_entry_orders,
    process_zerodha_postback,
    execute_trade_actions,
    get_open_trades_context
)
from zerodha_client import (
    map_zerodha_status_to_action_status,
    get_zerodha_order_status,
    verify_zerodha_order_confirmation,
    place_zerodha_order,
    cancel_zerodha_order,
    reconcile_zerodha_orders
)
from stage_tracker import record_stage


class TestOrderFillReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.session = db.SessionLocal()
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()

        self.msg = Message(
            telegram_message_id=9001,
            channel_id="test_reconciliation_channel",
            text="BUY NIFTY 24000 PE @100",
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
            structure_type="NIFTY PE BUY",
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

    # =========================================================================
    # 1. State Mapping & Normalization Tests
    # =========================================================================
    def test_map_zerodha_status_to_action_status(self):
        """Test normalization of Zerodha statuses to distinct domain order states."""
        # Complete execution
        self.assertEqual(map_zerodha_status_to_action_status("COMPLETE"), "FILLED")
        
        # Open in order book
        self.assertEqual(map_zerodha_status_to_action_status("OPEN", order_type="LIMIT"), "OPEN_LIMIT")
        self.assertEqual(map_zerodha_status_to_action_status("OPEN"), "OPEN_LIMIT")
        
        # Trigger pending
        self.assertEqual(map_zerodha_status_to_action_status("TRIGGER PENDING"), "TRIGGER_PENDING")
        
        # Terminal error states
        self.assertEqual(map_zerodha_status_to_action_status("REJECTED"), "REJECTED")
        self.assertEqual(map_zerodha_status_to_action_status("CANCELLED"), "CANCELLED")
        
        # Intermediate submitted / pending states
        self.assertEqual(map_zerodha_status_to_action_status("AMO REQ RECEIVED"), "SUBMITTED")
        self.assertEqual(map_zerodha_status_to_action_status("PUT ORDER REQ RECEIVED"), "SUBMITTED")
        self.assertEqual(map_zerodha_status_to_action_status("MODIFY PENDING"), "SUBMITTED")
        self.assertEqual(map_zerodha_status_to_action_status("VALIDATION PENDING"), "SUBMITTED")

        # Partial fills
        self.assertEqual(
            map_zerodha_status_to_action_status("OPEN", filled_qty=30, total_qty=65),
            "PARTIAL_FILL"
        )
        self.assertEqual(
            map_zerodha_status_to_action_status("TRIGGER PENDING", filled_qty=30, total_qty=65),
            "PARTIAL_FILL"
        )

    # =========================================================================
    # 2. Broker Order Status Retrieval Tests
    # =========================================================================
    @patch("zerodha_client.get_zerodha_client")
    def test_get_zerodha_order_status_mappings(self, mock_get_kite):
        """Test get_zerodha_order_status returns mapped status, filled quantities, and average price."""
        mock_kite = MagicMock()
        mock_kite.order_history.return_value = [
            {"status": "OPEN", "filled_quantity": 0, "pending_quantity": 65, "average_price": 0.0, "status_message": None}
        ]
        mock_get_kite.return_value = mock_kite

        # Test OPEN limit order
        st = get_zerodha_order_status("ORD_OPEN_1")
        self.assertTrue(st["success"])
        self.assertTrue(st["confirmed"])
        self.assertEqual(st["status"], "OPEN_LIMIT")
        self.assertEqual(st["raw_status"], "OPEN")
        self.assertEqual(st["filled_quantity"], 0)
        self.assertEqual(st["pending_quantity"], 65)

        # Test COMPLETE filled order
        mock_kite.order_history.return_value = [
            {"status": "COMPLETE", "filled_quantity": 65, "pending_quantity": 0, "average_price": 105.25, "status_message": None}
        ]
        st2 = get_zerodha_order_status("ORD_FILL_2")
        self.assertTrue(st2["success"])
        self.assertEqual(st2["status"], "FILLED")
        self.assertEqual(st2["raw_status"], "COMPLETE")
        self.assertEqual(st2["filled_quantity"], 65)
        self.assertEqual(st2["average_price"], 105.25)

        # Test REJECTED order
        mock_kite.order_history.return_value = [
            {"status": "REJECTED", "filled_quantity": 0, "pending_quantity": 65, "average_price": 0.0, "status_message": "RMS: Margin Insufficient"}
        ]
        st3 = get_zerodha_order_status("ORD_REJ_3")
        self.assertFalse(st3["success"])
        self.assertEqual(st3["status"], "REJECTED")
        self.assertEqual(st3["raw_status"], "REJECTED")
        self.assertEqual(st3["status_message"], "RMS: Margin Insufficient")

    # =========================================================================
    # 3. Active Order Reconciliation Mechanism Tests
    # =========================================================================
    @patch("worker.reconcile_zerodha_orders")
    def test_reconcile_active_orders_updates_open_limit_to_filled(self, mock_reconcile):
        """
        Verify that active order polling detects when an OPEN_LIMIT order transitions
        to FILLED on the exchange, updating quantities, prices, and recording ORDER_RECONCILED.
        """
        action = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            price="100.0",
            order_status="OPEN_LIMIT",
            filled_quantity=0,
            pending_quantity=65,
            average_price=0.0,
            zerodha_order_id="260819_ORD_100"
        )
        self.session.add(action)
        self.session.commit()

        # Mock Zerodha reporting that the order is now filled at 99.80
        mock_reconcile.return_value = {
            "260819_ORD_100": {
                "order_id": "260819_ORD_100",
                "status": "FILLED",
                "raw_status": "COMPLETE",
                "filled_quantity": 65,
                "pending_quantity": 0,
                "average_price": 99.80,
                "status_message": "Order executed"
            }
        }

        results = reconcile_active_orders(self.session, trade_id=self.trade.id)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["new_status"], "FILLED")
        self.assertEqual(results[0]["filled_quantity"], 65)
        self.assertEqual(results[0]["average_price"], 99.80)

        self.session.refresh(action)
        self.assertEqual(action.order_status, "FILLED")
        self.assertEqual(action.filled_quantity, 65)
        self.assertEqual(action.pending_quantity, 0)
        self.assertEqual(action.average_price, 99.80)
        self.assertIsNotNone(action.last_reconciled_at)

        # Check ORDER_RECONCILED and TRADE_LEGS_FILLED stage traces
        traces = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == self.trade.id,
            MessageStageTrace.stage == "ORDER_RECONCILED"
        ).all()
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].status, "SUCCESS")

        fill_traces = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == self.trade.id,
            MessageStageTrace.stage == "TRADE_LEGS_FILLED"
        ).all()
        self.assertEqual(len(fill_traces), 1)

    @patch("worker.reconcile_zerodha_orders")
    def test_reconcile_active_orders_detects_partial_fill(self, mock_reconcile):
        """
        Verify that active order polling detects partial execution (e.g. 30 of 65 shares),
        setting status to PARTIAL_FILL and updating filled_quantity.
        """
        action = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            price="100.0",
            order_status="OPEN_LIMIT",
            filled_quantity=0,
            pending_quantity=65,
            zerodha_order_id="260819_ORD_PARTIAL"
        )
        self.session.add(action)
        self.session.commit()

        mock_reconcile.return_value = {
            "260819_ORD_PARTIAL": {
                "order_id": "260819_ORD_PARTIAL",
                "status": "PARTIAL_FILL",
                "raw_status": "OPEN",
                "filled_quantity": 30,
                "pending_quantity": 35,
                "average_price": 100.0,
                "status_message": "Partial fill"
            }
        }

        reconcile_active_orders(self.session, trade_id=self.trade.id)

        self.session.refresh(action)
        self.assertEqual(action.order_status, "PARTIAL_FILL")
        self.assertEqual(action.filled_quantity, 30)
        self.assertEqual(action.pending_quantity, 35)

    # =========================================================================
    # 4. Zerodha Postback Webhook Handler Tests
    # =========================================================================
    def test_process_zerodha_postback_realtime_fill(self):
        """Test process_zerodha_postback updates Action in real-time when broker webhook arrives."""
        action = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="SELL",
            transaction_type="SELL",
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            price="150.0",
            order_status="OPEN_LIMIT",
            filled_quantity=0,
            pending_quantity=65,
            zerodha_order_id="POSTBACK_ORD_777"
        )
        self.session.add(action)
        self.session.commit()

        payload = {
            "order_id": "POSTBACK_ORD_777",
            "status": "COMPLETE",
            "tradingsymbol": "NIFTY2681824600PE",
            "transaction_type": "SELL",
            "quantity": 65,
            "filled_quantity": 65,
            "pending_quantity": 0,
            "average_price": 150.50,
            "status_message": "Traded 65 @ 150.50"
        }

        res = process_zerodha_postback(payload, self.session)

        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "FILLED")

        self.session.refresh(action)
        self.assertEqual(action.order_status, "FILLED")
        self.assertEqual(action.filled_quantity, 65)
        self.assertEqual(action.pending_quantity, 0)
        self.assertEqual(action.average_price, 150.50)

        # Check ORDER_POSTBACK_RECEIVED stage trace
        traces = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == self.trade.id,
            MessageStageTrace.stage == "ORDER_POSTBACK_RECEIVED"
        ).all()
        self.assertEqual(len(traces), 1)

    # =========================================================================
    # 5. Position Calculation with Fill States
    # =========================================================================
    def test_compute_trade_net_positions_ignores_unfilled_open_limit_orders(self):
        """
        Verify that compute_trade_net_positions evaluates position as FLAT (0)
        when limit entry orders are OPEN_LIMIT with filled_quantity = 0.
        """
        act_open_limit = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            order_status="OPEN_LIMIT",
            filled_quantity=0,
            pending_quantity=65
        )
        self.session.add(act_open_limit)
        self.session.commit()

        pos_map = compute_trade_net_positions(self.session, self.trade)
        
        self.assertIn("NIFTY2681824000PE", pos_map)
        info = pos_map["NIFTY2681824000PE"]
        self.assertEqual(info["net_quantity"], 0)
        self.assertEqual(info["abs_quantity"], 0)
        self.assertEqual(info["position_side"], "FLAT")
        self.assertIsNone(info["required_exit_side"])

    def test_compute_trade_net_positions_handles_partial_fills(self):
        """
        Verify that compute_trade_net_positions uses strictly the filled quantity
        when an order is PARTIAL_FILL.
        """
        act_partial = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            order_status="PARTIAL_FILL",
            filled_quantity=30,
            pending_quantity=35
        )
        self.session.add(act_partial)
        self.session.commit()

        pos_map = compute_trade_net_positions(self.session, self.trade)
        
        info = pos_map["NIFTY2681824000PE"]
        self.assertEqual(info["net_quantity"], 30)
        self.assertEqual(info["abs_quantity"], 30)
        self.assertEqual(info["position_side"], "LONG")
        self.assertEqual(info["required_exit_side"], "SELL")

    # =========================================================================
    # 6. Core Business Defect Prevention: Square-Off Protection on Unfilled Limits
    # =========================================================================
    @patch("worker.cancel_zerodha_order")
    @patch("worker.reconcile_zerodha_orders")
    def test_unfilled_limit_order_skips_square_off_and_cancels_limit_on_exit(self, mock_reconcile, mock_cancel):
        """
        CRITICAL TEST FOR SECTION 4.2:
        Scenario:
          1. System placed a LIMIT entry order (BUY 65 x 24000 PE).
          2. Price moved away, order remained OPEN_LIMIT on exchange (0 filled).
          3. Exit message arrives.
        Expected Behavior:
          1. Unfilled limit entry order is cancelled at Zerodha (preventing orphan fill).
          2. Net open position is recognized as FLAT (0).
          3. NO square-off SELL order is generated (preventing initiating a reverse short position!).
        """
        mock_cancel.return_value = {"success": True, "order_id": "ORD_UNFILLED_101", "message": "Order cancelled"}
        mock_reconcile.return_value = {
            "ORD_UNFILLED_101": {
                "order_id": "ORD_UNFILLED_101",
                "status": "OPEN_LIMIT",
                "raw_status": "OPEN",
                "filled_quantity": 0,
                "pending_quantity": 65,
                "average_price": 0.0,
                "status_message": None
            }
        }

        entry_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=True,
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            price="100.0",
            order_status="OPEN_LIMIT",
            filled_quantity=0,
            pending_quantity=65,
            zerodha_order_id="ORD_UNFILLED_101"
        )
        self.session.add(entry_act)
        self.session.commit()

        exit_msg = Message(
            telegram_message_id=9002,
            channel_id="test_reconciliation_channel",
            text="EXIT FULL POSITION NOW",
            date=datetime.utcnow()
        )
        self.session.add(exit_msg)
        self.session.commit()

        # Execute square off generation
        sq_actions = ensure_square_off_actions(self.session, self.trade, exit_msg)

        # 1. Verify NO square-off action was generated!
        self.assertEqual(len(sq_actions), 0)

        # 2. Verify cancel_zerodha_order was called for the unfilled limit order
        mock_cancel.assert_called_once_with("ORD_UNFILLED_101")

        # 3. Verify entry action was transitioned to CANCELLED
        self.session.refresh(entry_act)
        self.assertEqual(entry_act.order_status, "CANCELLED")

        # 4. Verify ORDER_CANCELLED_ON_EXIT trace was recorded
        traces = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.trade_id == self.trade.id,
            MessageStageTrace.stage == "ORDER_CANCELLED_ON_EXIT"
        ).all()
        self.assertEqual(len(traces), 1)

    @patch("worker.cancel_zerodha_order")
    @patch("worker.reconcile_zerodha_orders")
    def test_partial_fill_squares_off_only_acquired_quantity(self, mock_reconcile, mock_cancel):
        """
        Scenario:
          1. System placed a LIMIT entry order for 65 qty (BUY 24000 PE).
          2. Only 30 shares were filled on exchange; 35 remained open (PARTIAL_FILL).
          3. Exit message arrives.
        Expected Behavior:
          1. Remaining 35 open shares are cancelled on exchange.
          2. Square-off SELL order is generated for EXACTLY 30 shares (matching acquired qty).
        """
        mock_cancel.return_value = {"success": True, "order_id": "ORD_PART_555", "message": "Remaining cancelled"}
        mock_reconcile.return_value = {
            "ORD_PART_555": {
                "order_id": "ORD_PART_555",
                "status": "PARTIAL_FILL",
                "raw_status": "OPEN",
                "filled_quantity": 30,
                "pending_quantity": 35,
                "average_price": 100.0,
                "status_message": None
            }
        }

        entry_act = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=True,
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            lots=1,
            order_type="LIMIT",
            price="100.0",
            order_status="PARTIAL_FILL",
            filled_quantity=30,
            pending_quantity=35,
            zerodha_order_id="ORD_PART_555"
        )
        self.session.add(entry_act)
        self.session.commit()

        exit_msg = Message(
            telegram_message_id=9003,
            channel_id="test_reconciliation_channel",
            text="Exit 24000 PE",
            date=datetime.utcnow()
        )
        self.session.add(exit_msg)
        self.session.commit()

        sq_actions = ensure_square_off_actions(self.session, self.trade, exit_msg)

        # Verify square off was generated for exactly 30 shares
        self.assertEqual(len(sq_actions), 1)
        sq = sq_actions[0]
        self.assertEqual(sq.tradingsymbol, "NIFTY2681824000PE")
        self.assertEqual(sq.transaction_type, "SELL")
        self.assertEqual(sq.quantity, 30)

    # =========================================================================
    # 7. Cancel Unfilled Entry Orders Helper
    # =========================================================================
    @patch("worker.cancel_zerodha_order")
    def test_cancel_unfilled_entry_orders(self, mock_cancel):
        """Test cancel_unfilled_entry_orders cancels all open limit/trigger entry actions."""
        mock_cancel.return_value = {"success": True, "order_id": "ORD_CANCEL_1", "message": "OK"}

        act1 = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            order_status="OPEN_LIMIT",
            zerodha_order_id="ORD_CANCEL_1"
        )
        act2 = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            tradingsymbol="NIFTY2681824000PE",
            quantity=65,
            order_status="FILLED",
            filled_quantity=65,
            zerodha_order_id="ORD_FILLED_2"
        )
        self.session.add_all([act1, act2])
        self.session.commit()

        results = cancel_unfilled_entry_orders(self.session, self.trade)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["order_id"], "ORD_CANCEL_1")
        mock_cancel.assert_called_once_with("ORD_CANCEL_1")

        self.session.refresh(act1)
        self.session.refresh(act2)
        self.assertEqual(act1.order_status, "CANCELLED")
        self.assertEqual(act2.order_status, "FILLED")


if __name__ == "__main__":
    unittest.main()

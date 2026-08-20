import os
import unittest
from unittest.mock import patch, MagicMock, call
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from instruments_manager import (
    get_freeze_quantity,
    get_instrument_freeze_limit,
    slice_order_quantity,
    format_instrument_result,
    resolve_nfo_instrument,
    KNOWN_FREEZE_LIMITS
)
from zerodha_client import (
    place_zerodha_order,
    cancel_zerodha_order,
    get_zerodha_order_status,
    verify_zerodha_order_confirmation,
    reconcile_zerodha_orders,
    BrokerErrorCategory
)
from worker import (
    execute_trade_actions,
    reconcile_active_orders,
    process_trade_actions_and_sizing
)


class TestOrderSlicingFreezeLimits(unittest.TestCase):
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
            telegram_message_id=9500,
            channel_id="test_channel",
            text="BUY NIFTY 24500 CE @ 100",
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
            structure_type="NIFTY CE BUY",
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
    # 1. Freeze Quantity Limits Retrieval & Precedence Tests
    # =========================================================================
    def test_known_freeze_limits_for_major_indices(self):
        """Test that get_freeze_quantity returns exact NSE freeze limits for major indices."""
        self.assertEqual(get_freeze_quantity(underlying="NIFTY"), 1800)
        self.assertEqual(get_freeze_quantity(underlying="BANKNIFTY"), 900)
        self.assertEqual(get_freeze_quantity(underlying="FINNIFTY"), 1800)
        self.assertEqual(get_freeze_quantity(underlying="MIDCPNIFTY"), 4200)
        self.assertEqual(get_freeze_quantity(underlying="SENSEX"), 1000)
        self.assertEqual(get_freeze_quantity(underlying="BANKEX"), 900)

        # Lookup from tradingsymbol
        self.assertEqual(get_freeze_quantity(tradingsymbol="NIFTY26AUG24500CE"), 1800)
        self.assertEqual(get_freeze_quantity(tradingsymbol="BANKNIFTY26AUG52000PE"), 900)
        self.assertEqual(get_freeze_quantity(tradingsymbol="FINNIFTY26AUG23000CE"), 1800)

    def test_known_freeze_limits_for_stocks(self):
        """Test stock-specific freeze limits and lot-size fallbacks."""
        self.assertEqual(get_freeze_quantity(underlying="RELIANCE"), 4500)
        self.assertEqual(get_freeze_quantity(underlying="TATASTEEL"), 55000)
        self.assertEqual(get_freeze_quantity(underlying="HDFCBANK"), 5500)
        self.assertEqual(get_freeze_quantity(underlying="INFY"), 2400)
        self.assertEqual(get_freeze_quantity(underlying="TCS"), 1750)

        # Lookup from stock tradingsymbol
        self.assertEqual(get_freeze_quantity(tradingsymbol="TATASTEEL26AUG185CE"), 55000)
        self.assertEqual(get_freeze_quantity(tradingsymbol="RELIANCE26AUG3000PE"), 4500)

    def test_freeze_limit_precedence_from_csv_row(self):
        """Test that explicit freeze quantity in instrument CSV row takes highest precedence."""
        mock_row = {
            "tradingsymbol": "CUSTOM26AUG100CE",
            "name": "CUSTOM",
            "freeze_qty": "750",
            "lot_size": "25"
        }
        res = format_instrument_result(mock_row)
        self.assertEqual(res["freeze_qty"], 750)
        self.assertEqual(get_freeze_quantity(instrument_row=mock_row), 750)

        # Also supports 'freeze_quantity' or 'max_quantity' column names
        mock_row2 = {"tradingsymbol": "TEST1", "name": "TEST", "freeze_quantity": "1200", "lot_size": "50"}
        self.assertEqual(get_freeze_quantity(instrument_row=mock_row2), 1200)

        mock_row3 = {"tradingsymbol": "TEST2", "name": "TEST", "max_quantity": "600", "lot_size": "30"}
        self.assertEqual(get_freeze_quantity(instrument_row=mock_row3), 600)

    def test_freeze_limit_environment_override(self):
        """Test environment variable overrides (e.g. FREEZE_LIMIT_NIFTY)."""
        with patch.dict(os.environ, {"FREEZE_LIMIT_NIFTY": "900", "FREEZE_LIMIT_CUSTOMSTOCK": "300"}):
            self.assertEqual(get_freeze_quantity(underlying="NIFTY"), 900)
            self.assertEqual(get_freeze_quantity(underlying="CUSTOMSTOCK"), 300)

    def test_stock_fallback_lot_size_multiplier(self):
        """Test unknown stock falls back to lot_size * DEFAULT_STOCK_FREEZE_LOT_MULTIPLIER."""
        # UNKNOWNSYMBOL not in KNOWN_FREEZE_LIMITS, lot_size=250 -> 250 * 20 = 5000
        qty = get_freeze_quantity(underlying="UNKNOWNSYMBOL", lot_size=250)
        self.assertEqual(qty, 5000)

    # =========================================================================
    # 2. Automated Order Slicing Algorithm Tests
    # =========================================================================
    def test_slice_order_quantity_within_limit_returns_single_slice(self):
        """When total quantity <= freeze limit, returns single slice."""
        self.assertEqual(slice_order_quantity(65, 1800, 65), [65])
        self.assertEqual(slice_order_quantity(1800, 1800, 65), [1800])
        self.assertEqual(slice_order_quantity(900, 900, 30), [900])
        self.assertEqual(slice_order_quantity(30, 900, 30), [30])

    def test_slice_order_quantity_exceeding_freeze_limit_nifty(self):
        """
        NIFTY: lot_size=65, freeze_limit=1800.
        Max lots per slice = 1800 // 65 = 27 lots = 1755 quantity.
        For 3900 total quantity (60 lots):
          Slice 1: 1755 (27 lots)
          Slice 2: 1755 (27 lots)
          Slice 3: 390 (6 lots)
          Sum = 3900.
        """
        slices = slice_order_quantity(total_quantity=3900, freeze_limit=1800, lot_size=65)
        self.assertEqual(slices, [1755, 1755, 390])
        self.assertEqual(sum(slices), 3900)
        for s in slices:
            self.assertLessEqual(s, 1800)
            self.assertEqual(s % 65, 0)

    def test_slice_order_quantity_exceeding_freeze_limit_banknifty(self):
        """
        BANKNIFTY: lot_size=30, freeze_limit=900.
        Max lots per slice = 900 // 30 = 30 lots = 900 quantity.
        For 2700 total quantity (90 lots):
          Slice 1: 900 (30 lots)
          Slice 2: 900 (30 lots)
          Slice 3: 900 (30 lots)
        """
        slices = slice_order_quantity(total_quantity=2700, freeze_limit=900, lot_size=30)
        self.assertEqual(slices, [900, 900, 900])
        self.assertEqual(sum(slices), 2700)
        for s in slices:
            self.assertLessEqual(s, 900)
            self.assertEqual(s % 30, 0)

    def test_slice_order_quantity_stock(self):
        """
        Stock: lot_size=500, freeze_limit=2000.
        Total quantity = 5500 (11 lots).
        Max slice = (2000 // 500) * 500 = 2000.
        Slices = [2000, 2000, 1500].
        """
        slices = slice_order_quantity(total_quantity=5500, freeze_limit=2000, lot_size=500)
        self.assertEqual(slices, [2000, 2000, 1500])
        self.assertEqual(sum(slices), 5500)
        for s in slices:
            self.assertLessEqual(s, 2000)
            self.assertEqual(s % 500, 0)

    def test_slice_order_quantity_edge_cases(self):
        """Test edge cases: zero, negative, lot_size=1, lot_size > freeze_limit."""
        self.assertEqual(slice_order_quantity(0, 1800, 65), [])
        self.assertEqual(slice_order_quantity(-100, 1800, 65), [])
        self.assertEqual(slice_order_quantity(500, 0, 65), [500])
        self.assertEqual(slice_order_quantity(500, -10, 65), [500])
        # lot_size=1
        self.assertEqual(slice_order_quantity(2500, 1000, 1), [1000, 1000, 500])

    # =========================================================================
    # 3. Zerodha Client Order Slicing & Multi-Order Execution Tests
    # =========================================================================
    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_slices_large_nifty_order(self, mock_get_kite):
        """
        Verify place_zerodha_order automatically slices 3900 NIFTY quantity
        into 3 orders ([1755, 1755, 390]) and returns composite order tracking.
        """
        mock_kite = MagicMock()
        mock_get_kite.return_value = mock_kite
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_LIMIT = "LIMIT"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"

        # Mock 3 successive order IDs for the 3 slices
        mock_kite.place_order.side_effect = ["ORD_SLICE_1", "ORD_SLICE_2", "ORD_SLICE_3"]
        mock_kite.order_history.side_effect = [
            [{"order_id": "ORD_SLICE_1", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 1755, "order_type": "LIMIT"}],
            [{"order_id": "ORD_SLICE_2", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 1755, "order_type": "LIMIT"}],
            [{"order_id": "ORD_SLICE_3", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 390, "order_type": "LIMIT"}]
        ]

        res = place_zerodha_order(
            tradingsymbol="NIFTY26AUG24500CE",
            transaction_type="BUY",
            quantity=3900,
            exchange="NFO",
            order_type="LIMIT",
            product="NRML",
            price=150.0,
            freeze_limit=1800,
            lot_size=65
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["is_sliced"])
        self.assertEqual(res["slice_count"], 3)
        self.assertEqual(res["slices"], [1755, 1755, 390])
        self.assertEqual(res["order_id"], "ORD_SLICE_1,ORD_SLICE_2,ORD_SLICE_3")
        self.assertEqual(res["order_ids"], ["ORD_SLICE_1", "ORD_SLICE_2", "ORD_SLICE_3"])
        self.assertEqual(res["status"], "OPEN_LIMIT")
        self.assertEqual(res["pending_quantity"], 3900)
        self.assertEqual(res["filled_quantity"], 0)

        # Verify kite.place_order was called exactly 3 times with slice quantities
        self.assertEqual(mock_kite.place_order.call_count, 3)
        calls = mock_kite.place_order.call_args_list
        self.assertEqual(calls[0][1]["quantity"], 1755)
        self.assertEqual(calls[1][1]["quantity"], 1755)
        self.assertEqual(calls[2][1]["quantity"], 390)

    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_slices_and_aggregates_market_fills(self, mock_get_kite):
        """
        Verify place_zerodha_order slices 2700 BankNifty quantity into 3 slices of 900
        and aggregates filled quantities and weighted average price.
        """
        mock_kite = MagicMock()
        mock_get_kite.return_value = mock_kite
        mock_kite.TRANSACTION_TYPE_SELL = "SELL"
        mock_kite.ORDER_TYPE_MARKET = "MARKET"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"

        mock_kite.place_order.side_effect = ["ORD_BN_1", "ORD_BN_2", "ORD_BN_3"]
        mock_kite.order_history.side_effect = [
            [{"order_id": "ORD_BN_1", "status": "COMPLETE", "filled_quantity": 900, "pending_quantity": 0, "average_price": 200.0}],
            [{"order_id": "ORD_BN_2", "status": "COMPLETE", "filled_quantity": 900, "pending_quantity": 0, "average_price": 202.0}],
            [{"order_id": "ORD_BN_3", "status": "COMPLETE", "filled_quantity": 900, "pending_quantity": 0, "average_price": 204.0}]
        ]

        res = place_zerodha_order(
            tradingsymbol="BANKNIFTY26AUG52000PE",
            transaction_type="SELL",
            quantity=2700,
            exchange="NFO",
            order_type="MARKET",
            product="NRML",
            freeze_limit=900,
            lot_size=30
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["is_sliced"])
        self.assertEqual(res["slice_count"], 3)
        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(res["filled_quantity"], 2700)
        self.assertEqual(res["pending_quantity"], 0)
        # Weighted average = (900*200 + 900*202 + 900*204) / 2700 = 202.0
        self.assertAlmostEqual(res["average_price"], 202.0, places=2)

    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_first_slice_failure_aborts_cleanly(self, mock_get_kite):
        """
        Verify that if the very first slice fails (e.g. margin rejection),
        placement aborts immediately without attempting subsequent slices.
        """
        mock_kite = MagicMock()
        mock_get_kite.return_value = mock_kite
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_MARKET = "MARKET"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"

        mock_kite.place_order.side_effect = Exception("RMS: Margin Insufficient")

        res = place_zerodha_order(
            tradingsymbol="NIFTY26AUG24500CE",
            transaction_type="BUY",
            quantity=3900,
            exchange="NFO",
            order_type="MARKET",
            freeze_limit=1800,
            lot_size=65
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["error_category"], BrokerErrorCategory.MARGIN_EXHAUSTION)
        self.assertEqual(mock_kite.place_order.call_count, 1)

    @patch("zerodha_client.get_zerodha_client")
    def test_cancel_zerodha_order_cancels_all_slices_for_composite_id(self, mock_get_kite):
        """
        Verify cancel_zerodha_order splits comma-separated order IDs
        and cancels each individual child slice on Zerodha.
        """
        mock_kite = MagicMock()
        mock_get_kite.return_value = mock_kite
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.cancel_order.return_value = {"order_id": "OK"}

        composite_id = "ORD_SLICE_1,ORD_SLICE_2,ORD_SLICE_3"
        res = cancel_zerodha_order(composite_id)

        self.assertTrue(res["success"])
        self.assertEqual(mock_kite.cancel_order.call_count, 3)
        mock_kite.cancel_order.assert_any_call(variety="regular", order_id="ORD_SLICE_1")
        mock_kite.cancel_order.assert_any_call(variety="regular", order_id="ORD_SLICE_2")
        mock_kite.cancel_order.assert_any_call(variety="regular", order_id="ORD_SLICE_3")

    # =========================================================================
    # 4. Reconciliation of Sliced Orders Tests
    # =========================================================================
    @patch("zerodha_client.get_zerodha_client")
    def test_reconcile_zerodha_orders_aggregates_composite_sliced_orders(self, mock_get_kite):
        """
        Verify reconcile_zerodha_orders queries bulk kite.orders() and
        synthesizes composite entries for sliced multi-order actions.
        """
        mock_kite = MagicMock()
        mock_get_kite.return_value = mock_kite
        mock_kite.orders.return_value = [
            {"order_id": "SLICE_A", "status": "COMPLETE", "filled_quantity": 1755, "pending_quantity": 0, "quantity": 1755, "average_price": 100.0, "order_type": "LIMIT", "tradingsymbol": "NIFTY26AUG24500CE", "transaction_type": "BUY"},
            {"order_id": "SLICE_B", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 1755, "quantity": 1755, "average_price": 0.0, "order_type": "LIMIT", "tradingsymbol": "NIFTY26AUG24500CE", "transaction_type": "BUY"},
            {"order_id": "SLICE_C", "status": "OPEN", "filled_quantity": 0, "pending_quantity": 390, "quantity": 390, "average_price": 0.0, "order_type": "LIMIT", "tradingsymbol": "NIFTY26AUG24500CE", "transaction_type": "BUY"},
        ]

        composite_id = "SLICE_A,SLICE_B,SLICE_C"
        results = reconcile_zerodha_orders([composite_id])

        self.assertIn(composite_id, results)
        comp_info = results[composite_id]
        self.assertEqual(comp_info["filled_quantity"], 1755)
        self.assertEqual(comp_info["pending_quantity"], 2145)
        self.assertEqual(comp_info["total_quantity"], 3900)
        self.assertEqual(comp_info["status"], "PARTIAL_FILL")
        self.assertEqual(comp_info["average_price"], 100.0)

    # =========================================================================
    # 5. Worker End-to-End Execution with Slicing & DB Persistence Tests
    # =========================================================================
    @patch("worker.place_zerodha_order")
    def test_worker_executes_large_order_with_slicing_and_persists_action_metadata(self, mock_place_order):
        """
        Verify worker.execute_trade_actions handles sliced orders,
        updates action.is_sliced, action.slice_count, action.freeze_limit,
        and records ORDER_SLICED and ORDER_PLACED stage traces.
        """
        mock_place_order.return_value = {
            "success": True,
            "order_id": "ORD_S1,ORD_S2",
            "order_ids": ["ORD_S1", "ORD_S2"],
            "is_sliced": True,
            "slice_count": 2,
            "slices": [1755, 1755],
            "freeze_limit": 1800,
            "status": "OPEN_LIMIT",
            "raw_status": "OPEN",
            "message": "Placed 2 sliced orders: ORD_S1,ORD_S2",
            "status_message": None,
            "filled_quantity": 0,
            "pending_quantity": 3510,
            "average_price": 0.0
        }

        action = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY26AUG24500CE",
            quantity=3510,
            lots=54,
            order_type="LIMIT",
            price="120.0",
            order_status="PENDING",
            is_main=True,
            freeze_limit=1800
        )
        self.session.add(action)
        self.session.commit()
        self.session.refresh(action)

        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["order_id"], "ORD_S1,ORD_S2")

        # Verify Action model fields updated in database
        self.session.refresh(action)
        self.assertEqual(action.order_status, "OPEN_LIMIT")
        self.assertEqual(action.zerodha_order_id, "ORD_S1,ORD_S2")
        self.assertTrue(action.is_sliced)
        self.assertEqual(action.slice_count, 2)
        self.assertEqual(action.freeze_limit, 1800)

        # Verify Stage Traces
        sliced_trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.stage == "ORDER_SLICED",
            MessageStageTrace.trade_id == self.trade.id
        ).first()
        self.assertIsNotNone(sliced_trace)
        self.assertIn("1800", sliced_trace.details)

        placed_trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.stage == "ORDER_PLACED",
            MessageStageTrace.trade_id == self.trade.id
        ).first()
        self.assertIsNotNone(placed_trace)
        self.assertIn("ORD_S1", placed_trace.details)
        self.assertIn("ORD_S2", placed_trace.details)


if __name__ == "__main__":
    unittest.main()

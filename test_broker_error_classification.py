import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from zerodha_client import (
    BrokerErrorCategory,
    BROKER_ERROR_CLASS_NAMES,
    classify_broker_error,
    place_zerodha_order,
    cancel_zerodha_order,
    get_zerodha_order_status
)
from worker import execute_trade_actions, format_important_notice_telegram_html
import kiteconnect.exceptions as kite_exc


class TestBrokerErrorClassification(unittest.TestCase):
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
            telegram_message_id=9876,
            channel_id="test_channel",
            text="BUY NIFTY 24500 CE @120",
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
    # 1. Operational Error Classification Tests: classify_broker_error
    # =========================================================================
    def test_classify_market_closed_errors(self):
        """Test classification of various closed market and AMO error strings."""
        test_strings = [
            "Markets are closed right now. Use GTT for placing long standing orders instead.",
            "RMS:Rule : Option Strike not allowed in AMO",
            "Can't place order outside market hours. Place an After Market Order (AMO) instead.",
            "After Market Order (AMO) is not allowed for this segment",
            "Market is closed right now",
            "Exchange is closed",
            "Trading is closed for the day",
            "Orders cannot be placed during market close",
            "AMO orders can only be placed between 15:45 and 08:59",
            "Market order is not allowed during post close session"
        ]

        for err_str in test_strings:
            res = classify_broker_error(err_str)
            self.assertEqual(
                res["category"],
                BrokerErrorCategory.MARKET_CLOSED,
                f"Failed for string: {err_str}"
            )
            self.assertEqual(res["error_class"], "Market Closed")
            self.assertTrue(res["is_market_closed"])
            self.assertFalse(res["is_margin_exhaustion"])
            self.assertFalse(res["is_auth_failure"])

    def test_classify_margin_exhaustion_errors(self):
        """Test classification of margin exhaustion error strings."""
        test_strings = [
            "RMS: Margin Insufficient",
            "Insufficient funds to place order. Available: 500, Required: 150000",
            "RMS:Rule: Margin balance is insufficient",
            "RMS: Margin Exceeded",
            "Fund shortage for order placement",
            "Span margin and exposure margin shortfall: Rs. 45000",
            "Account does not have enough margin"
        ]

        for err_str in test_strings:
            res = classify_broker_error(err_str)
            self.assertEqual(
                res["category"],
                BrokerErrorCategory.MARGIN_EXHAUSTION,
                f"Failed for string: {err_str}"
            )
            self.assertEqual(res["error_class"], "Margin Exhaustion")
            self.assertTrue(res["is_margin_exhaustion"])
            self.assertFalse(res["is_market_closed"])

    def test_classify_circuit_limit_errors(self):
        """Test classification of circuit limit and DPR violation error strings."""
        test_strings = [
            "Price is outside the daily circuit range",
            "RMS:Rule: DPR violation. Order price is outside the permitted operating range",
            "Daily price range exceeded [10.0 - 200.0]",
            "Price out of bounds for limit order",
            "Order price 500.00 is out of permissible range (100.00 - 350.00)"
        ]

        for err_str in test_strings:
            res = classify_broker_error(err_str)
            self.assertEqual(
                res["category"],
                BrokerErrorCategory.CIRCUIT_LIMIT,
                f"Failed for string: {err_str}"
            )
            self.assertEqual(res["error_class"], "Circuit Limit")
            self.assertTrue(res["is_circuit_limit"])

    def test_classify_invalid_instrument_errors(self):
        """Test classification of invalid, expired, or blocked instrument errors."""
        test_strings = [
            "RMS:Rule: Option strike 24600 CE is blocked for trading",
            "Instrument is disabled for trading",
            "Contract has expired",
            "Expired instrument: NIFTY26JUL24000PE",
            "Invalid trading symbol NIFTY_INVALID_SYM",
            "Instrument does not exist",
            "Quantity exceeds the maximum allowed order quantity (freeze limit)",
            "Freeze limit of 1800 exceeded for NIFTY"
        ]

        for err_str in test_strings:
            res = classify_broker_error(err_str)
            self.assertEqual(
                res["category"],
                BrokerErrorCategory.INVALID_INSTRUMENT,
                f"Failed for string: {err_str}"
            )
            self.assertEqual(res["error_class"], "Invalid Instrument")
            self.assertTrue(res["is_invalid_instrument"])

    def test_classify_authorization_failure_errors(self):
        """Test classification of authentication and token expiration errors."""
        # Test KiteConnect exception instances
        token_exc = kite_exc.TokenException("Token is invalid or has expired")
        perm_exc = kite_exc.PermissionException("User does not have permission")
        
        res1 = classify_broker_error(token_exc)
        self.assertEqual(res1["category"], BrokerErrorCategory.AUTH_FAILURE)
        self.assertEqual(res1["error_class"], "Authorization Failure")
        self.assertTrue(res1["is_auth_failure"])

        res2 = classify_broker_error(perm_exc)
        self.assertEqual(res2["category"], BrokerErrorCategory.AUTH_FAILURE)
        self.assertEqual(res2["error_class"], "Authorization Failure")

        # Test string representations
        test_strings = [
            "Token is invalid or session has expired",
            "User not logged in. Please re-authenticate.",
            "403 Forbidden: Invalid api_key or access_token",
            "401 Unauthorized request",
            "Access denied: Invalid session"
        ]
        for err_str in test_strings:
            res = classify_broker_error(err_str)
            self.assertEqual(
                res["category"],
                BrokerErrorCategory.AUTH_FAILURE,
                f"Failed for string: {err_str}"
            )
            self.assertEqual(res["error_class"], "Authorization Failure")

    def test_classify_network_and_general_errors(self):
        """Test classification of network exceptions and fallback general errors."""
        net_exc = kite_exc.NetworkException("Connection reset by peer")
        res_net = classify_broker_error(net_exc)
        self.assertEqual(res_net["category"], BrokerErrorCategory.NETWORK_ERROR)
        self.assertEqual(res_net["error_class"], "Network Error")
        self.assertTrue(res_net["is_retryable"])

        res_gen = classify_broker_error("Some unexpected internal error")
        self.assertEqual(res_gen["category"], BrokerErrorCategory.GENERAL_ERROR)
        self.assertEqual(res_gen["error_class"], "General Error")
        self.assertFalse(res_gen["is_retryable"])

    # =========================================================================
    # 2. place_zerodha_order AMO Retry & Error Handling Tests
    # =========================================================================
    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_retries_amo_on_closed_market_string(self, mock_get_kite):
        """
        Verify that place_zerodha_order detects closed market string
        ('Markets are closed right now. Use GTT...') and retries with VARIETY_AMO.
        """
        mock_kite = MagicMock()
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_MARKET = "MARKET"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.VARIETY_AMO = "amo"

        # 1st call (regular) raises market closed error, 2nd call (AMO) succeeds
        mock_kite.place_order.side_effect = [
            Exception("Markets are closed right now. Use GTT for placing long standing orders instead."),
            "260820000001"
        ]
        mock_kite.order_history.return_value = [{"status": "AMO REQ RECEIVED", "filled_quantity": 0}]
        mock_get_kite.return_value = mock_kite

        res = place_zerodha_order(
            tradingsymbol="NIFTY26AUG24500CE",
            transaction_type="BUY",
            quantity=65,
            verify_confirmation=True
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["order_id"], "260820000001")
        self.assertEqual(res["status"], "SUBMITTED")
        # Verify kite.place_order was called twice: 1st regular, 2nd AMO
        self.assertEqual(mock_kite.place_order.call_count, 2)
        mock_kite.place_order.assert_called_with(
            variety="amo",
            exchange="NFO",
            tradingsymbol="NIFTY26AUG24500CE",
            transaction_type="BUY",
            quantity=65,
            product="NRML",
            order_type="MARKET",
            price=None,
            trigger_price=None,
            validity="DAY"
        )

    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_handles_amo_rejection_cleanly(self, mock_get_kite):
        """
        Verify that when AMO placement also fails (e.g. Option Strike not allowed in AMO),
        place_zerodha_order returns a structured failure with error_category='MARKET_CLOSED'
        instead of raising an unhandled exception.
        """
        mock_kite = MagicMock()
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_MARKET = "MARKET"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.VARIETY_AMO = "amo"

        # 1st regular fails, 2nd AMO fails
        mock_kite.place_order.side_effect = [
            Exception("Markets are closed right now. Use GTT for placing long standing orders instead."),
            Exception("RMS:Rule : Option Strike not allowed in AMO")
        ]
        mock_get_kite.return_value = mock_kite

        res = place_zerodha_order(
            tradingsymbol="NIFTY26AUG24500CE",
            transaction_type="BUY",
            quantity=65,
            verify_confirmation=False
        )

        self.assertFalse(res["success"])
        self.assertIsNone(res["order_id"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(res["error_category"], BrokerErrorCategory.MARKET_CLOSED)
        self.assertEqual(res["error_class"], "Market Closed")
        self.assertIn("RMS:Rule : Option Strike not allowed in AMO", res["message"])

    @patch("zerodha_client.get_zerodha_client")
    def test_place_zerodha_order_does_not_retry_amo_for_margin_or_auth_errors(self, mock_get_kite):
        """
        Verify that non-market-closed errors (e.g. Margin Insufficient) do NOT retry AMO
        and are immediately classified and returned.
        """
        mock_kite = MagicMock()
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.ORDER_TYPE_MARKET = "MARKET"
        mock_kite.PRODUCT_NRML = "NRML"
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.VARIETY_AMO = "amo"

        mock_kite.place_order.side_effect = Exception("RMS: Margin Insufficient")
        mock_get_kite.return_value = mock_kite

        res = place_zerodha_order(
            tradingsymbol="NIFTY26AUG24500CE",
            transaction_type="BUY",
            quantity=65,
            verify_confirmation=False
        )

        self.assertFalse(res["success"])
        self.assertEqual(mock_kite.place_order.call_count, 1)  # Only 1 call, no AMO retry
        self.assertEqual(res["error_category"], BrokerErrorCategory.MARGIN_EXHAUSTION)
        self.assertEqual(res["error_class"], "Margin Exhaustion")

    # =========================================================================
    # 3. Action DB Model & Stage Tracker Persistence Tests
    # =========================================================================
    @patch("worker.place_zerodha_order")
    def test_worker_persists_error_category_on_order_failure(self, mock_place_order):
        """
        Test that worker.execute_trade_actions sets action.error_category
        and includes it in MessageStageTrace details.
        """
        mock_place_order.return_value = {
            "success": False,
            "order_id": None,
            "status": "REJECTED",
            "raw_status": "REJECTED",
            "message": "Order placement failed: Markets are closed right now.",
            "status_message": "Markets are closed right now.",
            "error_category": BrokerErrorCategory.MARKET_CLOSED,
            "error_class": "Market Closed"
        }

        action = Action(
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY26AUG24500CE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="PENDING",
            is_main=True
        )
        self.session.add(action)
        self.session.commit()
        self.session.refresh(action)

        results = execute_trade_actions(self.session, self.trade.id, auto_mode=False)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertEqual(results[0]["error_category"], BrokerErrorCategory.MARKET_CLOSED)

        # Refresh action from DB
        self.session.refresh(action)
        self.assertEqual(action.order_status, "REJECTED")
        self.assertEqual(action.error_category, BrokerErrorCategory.MARKET_CLOSED)

        # Verify MessageStageTrace recorded error_category
        trace = self.session.query(MessageStageTrace).filter(
            MessageStageTrace.stage == "ORDER_REJECTED",
            MessageStageTrace.trade_id == self.trade.id
        ).first()
        self.assertIsNotNone(trace)
        self.assertIn("MARKET_CLOSED", trace.details)
        self.assertIn("Market Closed", trace.details)

    def test_format_important_notice_telegram_html_includes_category(self):
        """Test format_important_notice_telegram_html displays error category tag."""
        failed_act = Action(
            id=101,
            trade_id=self.trade.id,
            message_id=self.msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY26AUG24500CE",
            quantity=65,
            lots=1,
            order_type="MARKET",
            order_status="REJECTED",
            error_category=BrokerErrorCategory.MARKET_CLOSED,
            zerodha_response="Markets are closed right now. Use GTT for placing long standing orders instead."
        )

        html = format_important_notice_telegram_html(self.trade, [failed_act])
        self.assertIn("IMPORTANT NOTICE: ACTION(S) NOT EXECUTED", html)
        self.assertIn("MARKET_CLOSED", html)
        self.assertIn("Markets are closed right now", html)


if __name__ == "__main__":
    unittest.main()

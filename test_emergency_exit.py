import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action, MessageStageTrace
from worker import process_single_message, ensure_square_off_actions, compute_trade_net_positions
from gemini_client import (
    is_emergency_exit_phrase,
    extract_exit_strikes_and_prices,
    analyze_message_with_ai,
    TradeAnalysisSchema,
    ActionSchema
)
from stage_tracker import get_message_history


class TestEmergencyExit(unittest.TestCase):
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

        os.environ["AUTO_PLACE_ORDERS"] = "false"
        os.environ["AUTO_PLACE_EXIT_ORDERS"] = "false"

    def tearDown(self):
        self.session.query(MessageStageTrace).delete()
        self.session.query(Action).delete()
        self.session.query(Message).delete()
        self.session.query(Trade).delete()
        self.session.commit()
        self.session.close()

    def test_emergency_exit_keyword_regex_matching(self):
        """Test regex pattern matching across various emergency and ticker-omitted exit instructions."""
        # Unconditional exit phrases
        self.assertTrue(is_emergency_exit_phrase("EXIT FULL POSITION NOW"))
        self.assertTrue(is_emergency_exit_phrase("exit full position now"))
        self.assertTrue(is_emergency_exit_phrase("EXIT FULL POSITION"))
        self.assertTrue(is_emergency_exit_phrase("Close full position"))
        self.assertTrue(is_emergency_exit_phrase("Close full position now"))
        self.assertTrue(is_emergency_exit_phrase("Exit full"))
        self.assertTrue(is_emergency_exit_phrase("Close full"))
        self.assertTrue(is_emergency_exit_phrase("Exit now"))
        self.assertTrue(is_emergency_exit_phrase("Close now"))
        self.assertTrue(is_emergency_exit_phrase("Exit all positions"))
        self.assertTrue(is_emergency_exit_phrase("Close all positions"))
        self.assertTrue(is_emergency_exit_phrase("Exit the full position"))
        self.assertTrue(is_emergency_exit_phrase("Close the full position"))
        self.assertTrue(is_emergency_exit_phrase("Exit entire position"))
        self.assertTrue(is_emergency_exit_phrase("Close entire position"))
        self.assertTrue(is_emergency_exit_phrase("Square off full position"))
        self.assertTrue(is_emergency_exit_phrase("Square off all positions"))
        self.assertTrue(is_emergency_exit_phrase("Square off now"))
        self.assertTrue(is_emergency_exit_phrase("Close the trade"))
        self.assertTrue(is_emergency_exit_phrase("Close trade now"))
        self.assertTrue(is_emergency_exit_phrase("We are closing the trade"))

        # SL and target triggers
        self.assertTrue(is_emergency_exit_phrase("SL hit Exit full position"))
        self.assertTrue(is_emergency_exit_phrase("SL hit, close all positions"))
        self.assertTrue(is_emergency_exit_phrase("Stoploss hit"))
        self.assertTrue(is_emergency_exit_phrase("Target hit close full"))

        # Profit booking phrases
        self.assertTrue(is_emergency_exit_phrase("PROFIT BOOKING IN THIS TRADE Close 24600 Sell leg Close 24300 Hedge leg"))
        self.assertTrue(is_emergency_exit_phrase("Book profit in this trade"))
        self.assertTrue(is_emergency_exit_phrase("Book full profit"))

        # Strike-specific exits
        self.assertTrue(is_emergency_exit_phrase("Exit 24600 at 93, 24300 at 26"))
        self.assertTrue(is_emergency_exit_phrase("Close 24600 at 93, 24300 at 26"))
        self.assertTrue(is_emergency_exit_phrase("Exit 24000 PE at 90"))
        self.assertTrue(is_emergency_exit_phrase("Book 24150 PE at 35-36"))

        # Negative checks (non-exit messages)
        self.assertFalse(is_emergency_exit_phrase("Good morning traders"))
        self.assertFalse(is_emergency_exit_phrase("Trade incoming..."))
        self.assertFalse(is_emergency_exit_phrase("Deploy Bear Call Spread"))
        self.assertFalse(is_emergency_exit_phrase("SELL NIFTY 24000 PE @ 183 BUY NIFTY 23600 PE @ 81"))
        self.assertFalse(is_emergency_exit_phrase("Nifty looking bullish today"))
        self.assertFalse(is_emergency_exit_phrase(None))
        self.assertFalse(is_emergency_exit_phrase(""))

    def test_extract_exit_strikes_and_prices(self):
        """Test extraction of strike, price, and leg details from ticker-omitted exit messages."""
        # 1. Dual-strike exit with prices
        res1 = extract_exit_strikes_and_prices("Exit 24600 at 93, 24300 at 26")
        self.assertEqual(len(res1), 2)
        self.assertEqual(res1[0]["strike"], 24600.0)
        self.assertEqual(res1[0]["price"], "93")
        self.assertEqual(res1[1]["strike"], 24300.0)
        self.assertEqual(res1[1]["price"], "26")

        # 2. Strike with option type and price
        res2 = extract_exit_strikes_and_prices("Exit 24000 PE at 90")
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["strike"], 24000.0)
        self.assertEqual(res2[0]["option_type"], "PE")
        self.assertEqual(res2[0]["price"], "90")

        # 3. Explicit sell/hedge leg closure
        res3 = extract_exit_strikes_and_prices("PROFIT BOOKING IN THIS TRADE Close 24600 Sell leg Close 24300 Hedge leg")
        self.assertEqual(len(res3), 2)
        self.assertEqual(res3[0]["strike"], 24600.0)
        self.assertTrue(res3[0]["is_main"])
        self.assertEqual(res3[1]["strike"], 24300.0)
        self.assertFalse(res3[1]["is_main"])

        # 4. Pure unconditional exit without strikes
        res4 = extract_exit_strikes_and_prices("EXIT FULL POSITION NOW")
        self.assertEqual(len(res4), 0)

    @patch("gemini_client.get_genai_client")
    def test_ai_post_process_recovery_for_ticker_omitted_emergency_exit(self, mock_get_client):
        """
        Verify that analyze_message_with_ai post-processing repairs a Gemini response
        when Gemini erroneously returns is_valid_trade_msg = False for 'EXIT FULL POSITION NOW'.
        """
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Gemini falsely returned is_valid_trade_msg = False because ticker was omitted in text
        mock_parsed = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            is_continuation=False,
            related_open_trade_id=None,
            underlying=None,
            actions=[],
            trade_status_update="OPEN",
            context_summary="Informational commentary"
        )
        mock_response = MagicMock()
        mock_response.candidates = [MagicMock()]
        mock_response.parsed = mock_parsed
        mock_response.text = None
        mock_client.models.generate_content.return_value = mock_response

        open_trades_context = [
            {
                "id": 10,
                "status": "OPEN",
                "underlying": "NIFTY",
                "structure_type": "NIFTY BULL PUT SPREAD",
                "existing_orders": [
                    {"strike": 24600.0, "transaction_type": "SELL"},
                    {"strike": 24300.0, "transaction_type": "BUY"}
                ]
            }
        ]

        result = analyze_message_with_ai("EXIT FULL POSITION NOW", open_trades_context)
        self.assertIsNotNone(result)
        self.assertTrue(result.is_valid_trade_msg)
        self.assertTrue(result.is_continuation)
        self.assertEqual(result.related_open_trade_id, 10)
        self.assertEqual(result.underlying, "NIFTY")
        self.assertEqual(result.trade_status_update, "CLOSED")

    @patch("worker.analyze_message_with_ai")
    def test_message_79_scenario_deterministic_routing_override(self, mock_analyze_ai):
        """
        Simulates the critical production failure from Channel Message 79:
        - Open Trade #10 exists (NIFTY Bull Put Spread with 2 legs).
        - Message 79 arrives with text 'EXIT FULL POSITION NOW'.
        - AI model returns is_valid_trade_msg = False (no ticker found).
        - Deterministic routing layer MUST catch the emergency exit phrase,
          override the non-trade classification, map to Trade #10,
          update Trade #10 to CLOSED, and generate square-off actions.
        """
        # Simulate AI returning False directly to worker
        mock_analyze_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            is_continuation=False,
            related_open_trade_id=None,
            underlying=None,
            actions=[],
            trade_status_update="OPEN",
            context_summary="Chat message skipped"
        )

        # Setup active open trade in DB
        entry_msg = Message(id=1, telegram_message_id=50, text="DEPLOY NIFTY BULL PUT SPREAD", date=datetime.utcnow())
        self.session.add(entry_msg)
        self.session.commit()

        trade = Trade(
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY BULL PUT SPREAD",
            opened_at=datetime.utcnow()
        )
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        sell_act = Action(
            trade_id=trade.id,
            message_id=entry_msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            strike=24600.0,
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        buy_act = Action(
            trade_id=trade.id,
            message_id=entry_msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            strike=24300.0,
            tradingsymbol="NIFTY2681824300PE",
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add_all([sell_act, buy_act])
        self.session.commit()

        # Incoming urgent exit message without ticker
        msg79 = Message(
            id=79,
            telegram_message_id=79,
            text="EXIT FULL POSITION NOW",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(msg79)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, msg79))
        self.assertTrue(success)

        # Refresh trade and message
        self.session.refresh(trade)
        self.session.refresh(msg79)

        # Verify trade status is CLOSED
        self.assertEqual(trade.status, "CLOSED")
        self.assertIsNotNone(trade.closed_at)

        # Verify square-off actions were created for both legs
        sq_actions = self.session.query(Action).filter(
            Action.message_id == msg79.id,
            Action.action_type == "EXIT"
        ).all()
        self.assertEqual(len(sq_actions), 2)

        # Check square-off order sides and ordering (BUY to close short leg must be first)
        sq_actions.sort(key=lambda a: 0 if a.transaction_type == "BUY" else 1)
        self.assertEqual(sq_actions[0].tradingsymbol, "NIFTY2681824600PE")
        self.assertEqual(sq_actions[0].transaction_type, "BUY")
        self.assertEqual(sq_actions[0].quantity, 65)

        self.assertEqual(sq_actions[1].tradingsymbol, "NIFTY2681824300PE")
        self.assertEqual(sq_actions[1].transaction_type, "SELL")
        self.assertEqual(sq_actions[1].quantity, 65)

        # Verify diagnostic traces recorded the deterministic override
        traces = get_message_history(msg79.id, session=self.session)
        stage_names = [t["stage"] for t in traces]
        self.assertIn("DETERMINISTIC_EMERGENCY_EXIT_OVERRIDE", stage_names)
        self.assertIn("SQUARE_OFF_GENERATION", stage_names)

    @patch("gemini_client.get_genai_client")
    def test_strike_omitted_ticker_exit_with_prices(self, mock_get_client):
        """
        Tests exit message: 'Exit 24600 at 93, 24300 at 26'
        - No ticker name in message text.
        - Contains specific strikes and exit limit prices.
        - Resolves to open NIFTY trade with matching strikes.
        - Generates LIMIT exit orders with specified prices.
        """
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_parsed = TradeAnalysisSchema(
            is_valid_trade_msg=False,  # Simulate AI failing to identify ticker
            actions=[],
            trade_status_update="OPEN"
        )
        mock_response = MagicMock()
        mock_response.candidates = [MagicMock()]
        mock_response.parsed = mock_parsed
        mock_response.text = None
        mock_client.models.generate_content.return_value = mock_response

        trade = Trade(
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY PE SPREAD",
            opened_at=datetime.utcnow()
        )
        self.session.add(trade)
        self.session.commit()

        entry_msg = Message(id=2, telegram_message_id=60, text="NIFTY SPREAD", date=datetime.utcnow())
        self.session.add(entry_msg)
        self.session.commit()

        sell_act = Action(
            trade_id=trade.id,
            message_id=entry_msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            strike=24600.0,
            tradingsymbol="NIFTY2681824600PE",
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        buy_act = Action(
            trade_id=trade.id,
            message_id=entry_msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            strike=24300.0,
            tradingsymbol="NIFTY2681824300PE",
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add_all([sell_act, buy_act])
        self.session.commit()

        msg = Message(
            id=80,
            telegram_message_id=80,
            text="Exit 24600 at 93, 24300 at 26",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, msg))
        self.assertTrue(success)

        self.session.refresh(trade)
        self.assertEqual(trade.status, "CLOSED")

        # Verify square off actions with limit prices
        exit_actions = self.session.query(Action).filter(
            Action.message_id == msg.id,
            Action.action_type == "EXIT"
        ).all()
        self.assertEqual(len(exit_actions), 2)

        leg_24600 = next(a for a in exit_actions if a.strike == 24600.0)
        self.assertEqual(leg_24600.price, "93")
        self.assertEqual(leg_24600.order_type, "LIMIT")
        self.assertEqual(leg_24600.transaction_type, "BUY")

        leg_24300 = next(a for a in exit_actions if a.strike == 24300.0)
        self.assertEqual(leg_24300.price, "26")
        self.assertEqual(leg_24300.order_type, "LIMIT")
        self.assertEqual(leg_24300.transaction_type, "SELL")

    @patch("gemini_client.get_genai_client")
    def test_multi_trade_strike_matching_deterministic_routing(self, mock_get_client):
        """
        When multiple open trades exist in DB (e.g. NIFTY and BANKNIFTY):
        An exit message specifying strikes 'Close 24600 at 93' correctly matches Trade #1 (NIFTY)
        and leaves Trade #2 (BANKNIFTY) OPEN.
        """
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_parsed = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            actions=[],
            trade_status_update="OPEN"
        )
        mock_response = MagicMock()
        mock_response.candidates = [MagicMock()]
        mock_response.parsed = mock_parsed
        mock_response.text = None
        mock_client.models.generate_content.return_value = mock_response

        # Trade 1: NIFTY
        trade1 = Trade(status="OPEN", underlying="NIFTY", structure_type="NIFTY SPREAD")
        # Trade 2: BANKNIFTY
        trade2 = Trade(status="OPEN", underlying="BANKNIFTY", structure_type="BANKNIFTY SPREAD")
        self.session.add_all([trade1, trade2])
        self.session.commit()

        msg_ref = Message(id=3, telegram_message_id=3, text="Init trades", date=datetime.utcnow())
        self.session.add(msg_ref)
        self.session.commit()

        act1 = Action(trade_id=trade1.id, message_id=msg_ref.id, action_type="SELL", strike=24600.0, tradingsymbol="NIFTY2681824600PE", quantity=65, order_status="PLACED")
        act2 = Action(trade_id=trade2.id, message_id=msg_ref.id, action_type="SELL", strike=52000.0, tradingsymbol="BANKNIFTY2681852000PE", quantity=30, order_status="PLACED")
        self.session.add_all([act1, act2])
        self.session.commit()

        exit_msg = Message(
            id=81,
            telegram_message_id=81,
            text="Close 24600 at 93",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(exit_msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, exit_msg))
        self.assertTrue(success)

        self.session.refresh(trade1)
        self.session.refresh(trade2)

        # Trade 1 should be CLOSED, Trade 2 should remain OPEN
        self.assertEqual(trade1.status, "CLOSED")
        self.assertEqual(trade2.status, "OPEN")

    @patch("worker.analyze_message_with_ai")
    def test_close_full_position_unconditional_exit(self, mock_analyze_ai):
        """Test 'Close full position' phrase closes open trade and generates square-off actions."""
        mock_analyze_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            actions=[],
            trade_status_update="OPEN"
        )

        trade = Trade(status="OPEN", underlying="BANKNIFTY", structure_type="BANKNIFTY CE SPREAD")
        self.session.add(trade)
        self.session.commit()

        msg_ref = Message(id=4, telegram_message_id=4, text="BANKNIFTY SPREAD", date=datetime.utcnow())
        self.session.add(msg_ref)
        self.session.commit()

        act1 = Action(trade_id=trade.id, message_id=msg_ref.id, action_type="SELL", is_main=True, underlying="BANKNIFTY", strike=52000.0, tradingsymbol="BANKNIFTY2681852000CE", quantity=30, lots=1, order_status="PLACED")
        act2 = Action(trade_id=trade.id, message_id=msg_ref.id, action_type="BUY", is_main=False, underlying="BANKNIFTY", strike=52500.0, tradingsymbol="BANKNIFTY2681852500CE", quantity=30, lots=1, order_status="PLACED")
        self.session.add_all([act1, act2])
        self.session.commit()

        exit_msg = Message(
            id=82,
            telegram_message_id=82,
            text="Close full position",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(exit_msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, exit_msg))
        self.assertTrue(success)

        self.session.refresh(trade)
        self.assertEqual(trade.status, "CLOSED")

        sq_actions = self.session.query(Action).filter(Action.message_id == exit_msg.id, Action.action_type == "EXIT").all()
        self.assertEqual(len(sq_actions), 2)

    @patch("worker.analyze_message_with_ai")
    def test_sl_hit_exit_full_position(self, mock_analyze_ai):
        """Test 'SL hit Exit full position' triggers unconditional emergency close."""
        mock_analyze_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            actions=[],
            trade_status_update="OPEN"
        )

        trade = Trade(status="OPEN", underlying="TATASTEEL", structure_type="TATASTEEL SPREAD")
        self.session.add(trade)
        self.session.commit()

        msg_ref = Message(id=5, telegram_message_id=5, text="TATASTEEL", date=datetime.utcnow())
        self.session.add(msg_ref)
        self.session.commit()

        act1 = Action(trade_id=trade.id, message_id=msg_ref.id, action_type="SELL", is_main=True, underlying="TATASTEEL", strike=192.5, tradingsymbol="TATASTEEL26AUG192.5PE", quantity=5500, lots=1, order_status="PLACED")
        self.session.add(act1)
        self.session.commit()

        exit_msg = Message(
            id=83,
            telegram_message_id=83,
            text="SL hit Exit full position",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(exit_msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, exit_msg))
        self.assertTrue(success)

        self.session.refresh(trade)
        self.assertEqual(trade.status, "CLOSED")

    @patch("worker.analyze_message_with_ai")
    def test_non_exit_messages_not_intercepted_as_emergency_exit(self, mock_analyze_ai):
        """Verify informational or regular chat messages are not mistakenly identified as emergency exits."""
        mock_analyze_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            actions=[],
            trade_status_update="OPEN",
            context_summary="Chat message"
        )

        trade = Trade(status="OPEN", underlying="NIFTY", structure_type="NIFTY SPREAD")
        self.session.add(trade)
        self.session.commit()

        chat_msg = Message(
            id=84,
            telegram_message_id=84,
            text="Market is moving sideways today, holding our position",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(chat_msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, chat_msg))
        self.assertTrue(success)

        self.session.refresh(trade)
        # Trade should remain OPEN!
        self.assertEqual(trade.status, "OPEN")

        # No square-off actions should be generated
        sq_actions = self.session.query(Action).filter(Action.message_id == chat_msg.id).all()
        self.assertEqual(len(sq_actions), 0)

        traces = get_message_history(chat_msg.id, session=self.session)
        stage_names = [t["stage"] for t in traces]
        self.assertIn("AI_NON_TRADE_MESSAGE", stage_names)
        self.assertNotIn("DETERMINISTIC_EMERGENCY_EXIT_OVERRIDE", stage_names)

    @patch("worker.get_zerodha_net_positions")
    @patch("worker.analyze_message_with_ai")
    def test_single_open_broker_position_fallback(self, mock_analyze_ai, mock_zerodha_pos):
        """
        When 2 trades are marked OPEN in DB, but only 1 trade has active non-zero position on Zerodha:
        'EXIT FULL POSITION NOW' resolves via broker portfolio to the active trade.
        """
        mock_analyze_ai.return_value = TradeAnalysisSchema(
            is_valid_trade_msg=False,
            actions=[],
            trade_status_update="OPEN"
        )

        # Trade 1: NIFTY (Has active broker position: 65)
        trade1 = Trade(status="OPEN", underlying="NIFTY", structure_type="NIFTY SPREAD")
        # Trade 2: BANKNIFTY (Flat on broker: 0)
        trade2 = Trade(status="OPEN", underlying="BANKNIFTY", structure_type="BANKNIFTY SPREAD")
        self.session.add_all([trade1, trade2])
        self.session.commit()

        msg_ref = Message(id=6, telegram_message_id=6, text="Init", date=datetime.utcnow())
        self.session.add(msg_ref)
        self.session.commit()

        act1 = Action(trade_id=trade1.id, message_id=msg_ref.id, action_type="SELL", tradingsymbol="NIFTY2681824600PE", quantity=65, order_status="PLACED")
        act2 = Action(trade_id=trade2.id, message_id=msg_ref.id, action_type="SELL", tradingsymbol="BANKNIFTY2681852000PE", quantity=30, order_status="CANCELLED")
        self.session.add_all([act1, act2])
        self.session.commit()

        # Mock Zerodha broker positions: only NIFTY is active
        mock_zerodha_pos.return_value = {
            "success": True,
            "positions": {
                "NIFTY2681824600PE": -65,
                "BANKNIFTY2681852000PE": 0
            }
        }

        exit_msg = Message(
            id=85,
            telegram_message_id=85,
            text="EXIT FULL POSITION NOW",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(exit_msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, exit_msg))
        self.assertTrue(success)

        self.session.refresh(trade1)
        self.session.refresh(trade2)

        self.assertEqual(trade1.status, "CLOSED")
        self.assertEqual(trade2.status, "OPEN")

    @patch("gemini_client.get_genai_client")
    def test_trade_30_exact_closing_message_inherits_pe_and_closes_put_spread(self, mock_get_client):
        """
        Simulate the exact Trade 30 production scenario:
        - Open trade #30: NIFTY Put Spread (24600 PE SELL, 24300 PE BUY).
        - Telegram message: '24600 close at 93, 24300 close at 26' (omits 'PE').
        - Pipeline processes message:
          1. AI analysis / post-processing extracts strikes 24600 & 24300 with prices 93 & 26.
          2. Maps message to Trade #30.
          3. process_trade_actions_and_sizing cross-references open positions and inherits PE.
          4. Resolves NIFTY...24600PE (BUY) and NIFTY...24300PE (SELL).
          5. Strictly forbids defaulting to CE (no Call options bought).
          6. Updates Trade #30 to CLOSED.
        """
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_parsed = TradeAnalysisSchema(
            is_valid_trade_msg=False,  # Simulate AI failing to identify ticker in text
            actions=[],
            trade_status_update="OPEN"
        )
        mock_response = MagicMock()
        mock_response.candidates = [MagicMock()]
        mock_response.parsed = mock_parsed
        mock_response.text = None
        mock_client.models.generate_content.return_value = mock_response

        # Setup Open Trade 30
        trade = Trade(
            id=30,
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY BULL PUT SPREAD",
            opened_at=datetime.utcnow()
        )
        self.session.add(trade)
        self.session.commit()

        entry_msg = Message(id=30, telegram_message_id=30, text="DEPLOY NIFTY PUT SPREAD", date=datetime.utcnow())
        self.session.add(entry_msg)
        self.session.commit()

        sell_pe = Action(
            trade_id=trade.id,
            message_id=entry_msg.id,
            action_type="SELL",
            transaction_type="SELL",
            is_main=True,
            underlying="NIFTY",
            strike=24600.0,
            option_type="PE",
            tradingsymbol="NIFTY2681824600PE",
            instrument_token=1001,
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        buy_pe = Action(
            trade_id=trade.id,
            message_id=entry_msg.id,
            action_type="BUY",
            transaction_type="BUY",
            is_main=False,
            underlying="NIFTY",
            strike=24300.0,
            option_type="PE",
            tradingsymbol="NIFTY2681824300PE",
            instrument_token=1002,
            quantity=65,
            lots=1,
            order_status="PLACED"
        )
        self.session.add_all([sell_pe, buy_pe])
        self.session.commit()

        # Incoming exit message: "24600 close at 93, 24300 close at 26"
        exit_msg = Message(
            id=31,
            telegram_message_id=31,
            text="24600 close at 93, 24300 close at 26",
            date=datetime.utcnow(),
            processed=False,
            analysed_by_ai=False
        )
        self.session.add(exit_msg)
        self.session.commit()

        import asyncio
        success = asyncio.run(process_single_message(self.session, exit_msg))
        self.assertTrue(success)

        self.session.refresh(trade)
        self.assertEqual(trade.status, "CLOSED")

        # Verify exit actions
        exit_actions = self.session.query(Action).filter(
            Action.message_id == exit_msg.id,
            Action.action_type == "EXIT"
        ).all()
        self.assertEqual(len(exit_actions), 2)

        # Verify leg 24600: PE, BUY, NIFTY2681824600PE (NOT CE)
        act_24600 = next(a for a in exit_actions if a.strike == 24600.0)
        self.assertEqual(act_24600.option_type, "PE")
        self.assertEqual(act_24600.tradingsymbol, "NIFTY2681824600PE")
        self.assertEqual(act_24600.instrument_token, 1001)
        self.assertEqual(act_24600.transaction_type, "BUY")
        self.assertEqual(act_24600.price, "93")
        self.assertEqual(act_24600.order_type, "LIMIT")
        self.assertFalse(act_24600.tradingsymbol.endswith("CE"))

        # Verify leg 24300: PE, SELL, NIFTY2681824300PE (NOT CE)
        act_24300 = next(a for a in exit_actions if a.strike == 24300.0)
        self.assertEqual(act_24300.option_type, "PE")
        self.assertEqual(act_24300.tradingsymbol, "NIFTY2681824300PE")
        self.assertEqual(act_24300.instrument_token, 1002)
        self.assertEqual(act_24300.transaction_type, "SELL")
        self.assertEqual(act_24300.price, "26")
        self.assertEqual(act_24300.order_type, "LIMIT")
        self.assertFalse(act_24300.tradingsymbol.endswith("CE"))


if __name__ == "__main__":
    unittest.main()

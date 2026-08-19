import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import db
from models import Message, Trade, Action
from worker import get_open_trades_context
from gemini_client import (
    format_open_trades_context,
    _format_strike_str,
    analyze_message_with_ai,
    TradeAnalysisSchema,
    ActionSchema
)


class TestOpenTradesContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.session = db.SessionLocal()

    def tearDown(self):
        self.session.query(Action).delete()
        self.session.query(Trade).delete()
        self.session.query(Message).delete()
        self.session.commit()
        self.session.close()

    def test_format_strike_str(self):
        self.assertEqual(_format_strike_str(24600.0), "24600")
        self.assertEqual(_format_strike_str(192.5), "192.5")
        self.assertEqual(_format_strike_str(52000), "52000")
        self.assertEqual(_format_strike_str(None), "N/A")
        self.assertEqual(_format_strike_str("invalid"), "invalid")

    def test_format_open_trades_context_empty(self):
        self.assertEqual(format_open_trades_context([]), "No active open positions.")
        self.assertEqual(format_open_trades_context(None), "No active open positions.")

    def test_format_open_trades_context_tabular_markdown(self):
        open_trades = [
            {
                "id": 10,
                "underlying": "NIFTY",
                "structure_type": "NIFTY BULL PUT SPREAD",
                "active_legs": [
                    {
                        "tradingsymbol": "NIFTY2681824600PE",
                        "strike": 24600.0,
                        "option_type": "PE",
                        "transaction_type": "SELL",
                        "is_main": True
                    },
                    {
                        "tradingsymbol": "NIFTY2681824300PE",
                        "strike": 24300.0,
                        "option_type": "PE",
                        "transaction_type": "BUY",
                        "is_main": False
                    }
                ]
            },
            {
                "id": 12,
                "underlying": "BANKNIFTY",
                "structure_type": "BANKNIFTY BEAR CALL SPREAD",
                "active_legs": [
                    {
                        "tradingsymbol": "BANKNIFTY2681852000CE",
                        "strike": 52000.0,
                        "option_type": "CE",
                        "transaction_type": "SELL",
                        "is_main": True
                    }
                ]
            }
        ]

        table_str = format_open_trades_context(open_trades)
        
        # Verify table structure and columns
        self.assertIn("| Trade ID | Underlying | Strategy Type | Tradingsymbol | Strike | Option Type | Side | Role |", table_str)
        self.assertIn("| 10 | NIFTY | NIFTY BULL PUT SPREAD | NIFTY2681824600PE | 24600 | PE | SELL | Main |", table_str)
        self.assertIn("| 10 | NIFTY | NIFTY BULL PUT SPREAD | NIFTY2681824300PE | 24300 | PE | BUY | Hedge |", table_str)
        self.assertIn("| 12 | BANKNIFTY | BANKNIFTY BEAR CALL SPREAD | BANKNIFTY2681852000CE | 52000 | CE | SELL | Main |", table_str)

        # Verify extraneous metadata and timestamps are NOT in the prompt table
        self.assertNotIn("opened_at", table_str)
        self.assertNotIn("context_summary", table_str)
        self.assertNotIn("instrument_token", table_str)
        self.assertNotIn("sl_trigger", table_str)

    def test_get_open_trades_context_filters_closed_failed_and_metadata_actions(self):
        """
        Verify that get_open_trades_context:
        1. Includes only active open legs.
        2. Excludes historical closed/cancelled/failed actions.
        3. Excludes metadata actions like UPDATE_SL and INFO.
        4. Excludes full timestamps and extraneous metadata.
        """
        msg = Message(id=888, text="BUY NIFTY SPREAD", date=datetime.utcnow())
        self.session.add(msg)
        self.session.commit()

        trade = Trade(
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY BULL PUT SPREAD",
            context_summary="Old multi-day holding summary with lots of commentary that should not be dumped to LLM."
        )
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        # 1. Active Open Leg 1 (Sell 24600 PE)
        act1 = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            tradingsymbol="NIFTY2681824600PE",
            strike=24600.0,
            option_type="PE",
            quantity=65,
            lots=1,
            is_main=True,
            order_status="PLACED",
            instrument_token=12345,
            details="Active short leg"
        )
        # 2. Active Open Leg 2 (Buy 24300 PE)
        act2 = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824300PE",
            strike=24300.0,
            option_type="PE",
            quantity=65,
            lots=1,
            is_main=False,
            order_status="PLACED",
            instrument_token=12346,
            details="Active hedge leg"
        )
        # 3. Failed entry action (should be excluded)
        act_failed = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="SELL",
            transaction_type="SELL",
            tradingsymbol="NIFTY2681824800PE",
            strike=24800.0,
            option_type="PE",
            quantity=65,
            lots=1,
            order_status="FAILED"
        )
        # 4. Cancelled entry action (should be excluded)
        act_cancelled = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824100PE",
            strike=24100.0,
            option_type="PE",
            quantity=65,
            lots=1,
            order_status="CANCELLED"
        )
        # 5. Metadata UPDATE_SL action (should be excluded)
        act_update_sl = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="UPDATE_SL",
            order_status="PLACED",
            stoploss="24250"
        )
        # 6. INFO action (should be excluded)
        act_info = Action(
            trade_id=trade.id,
            message_id=msg.id,
            action_type="INFO",
            order_status="PLACED",
            details="Planning commentary"
        )

        self.session.add_all([act1, act2, act_failed, act_cancelled, act_update_sl, act_info])
        self.session.commit()

        context = get_open_trades_context(self.session)
        matching = [t for t in context if t["id"] == trade.id]
        self.assertEqual(len(matching), 1)
        trade_data = matching[0]

        # Verify only 2 active legs are returned
        active_legs = trade_data["active_legs"]
        self.assertEqual(len(active_legs), 2)
        
        symbols = [leg["tradingsymbol"] for leg in active_legs]
        self.assertIn("NIFTY2681824600PE", symbols)
        self.assertIn("NIFTY2681824300PE", symbols)
        self.assertNotIn("NIFTY2681824800PE", symbols)
        self.assertNotIn("NIFTY2681824100PE", symbols)

        # Verify extraneous fields are not in active leg dicts
        for leg in active_legs:
            self.assertNotIn("instrument_token", leg)
            self.assertNotIn("details", leg)
            self.assertNotIn("sl_trigger_type", leg)
            self.assertNotIn("stoploss", leg)
            self.assertNotIn("target", leg)

    @patch("gemini_client.get_genai_client")
    def test_analyze_message_with_ai_passes_tabular_prompt_context(self, mock_get_client):
        """Verify analyze_message_with_ai sends the minimal tabular format to Gemini."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_candidate = MagicMock()
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.parsed = TradeAnalysisSchema(
            is_valid_trade_msg=True,
            is_continuation=True,
            trade_status_update="CLOSED",
            related_open_trade_id=10,
            underlying="NIFTY",
            structure_type="NIFTY BULL PUT SPREAD",
            actions=[]
        )
        mock_response.text = None
        mock_client.models.generate_content.return_value = mock_response

        open_trades_ctx = [
            {
                "id": 10,
                "underlying": "NIFTY",
                "structure_type": "NIFTY BULL PUT SPREAD",
                "active_legs": [
                    {
                        "tradingsymbol": "NIFTY2681824600PE",
                        "strike": 24600.0,
                        "option_type": "PE",
                        "transaction_type": "SELL",
                        "is_main": True
                    },
                    {
                        "tradingsymbol": "NIFTY2681824300PE",
                        "strike": 24300.0,
                        "option_type": "PE",
                        "transaction_type": "BUY",
                        "is_main": False
                    }
                ]
            }
        ]

        result = analyze_message_with_ai("EXIT FULL POSITION NOW", open_trades_ctx)
        self.assertIsNotNone(result)

        # Inspect prompt passed to generate_content
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        contents = call_kwargs.get("contents") or mock_client.models.generate_content.call_args[1].get("contents")
        
        # Verify the tabular format is present in the prompt
        self.assertIn("| Trade ID | Underlying | Strategy Type | Tradingsymbol | Strike | Option Type | Side | Role |", contents)
        self.assertIn("| 10 | NIFTY | NIFTY BULL PUT SPREAD | NIFTY2681824600PE | 24600 | PE | SELL | Main |", contents)
        self.assertIn("| 10 | NIFTY | NIFTY BULL PUT SPREAD | NIFTY2681824300PE | 24300 | PE | BUY | Hedge |", contents)
        
        # Verify raw JSON dump syntax is NOT in the prompt
        self.assertNotIn('"existing_orders":', contents)
        self.assertNotIn('"instrument_token":', contents)

    def test_partially_closed_trade_returns_only_remaining_open_leg(self):
        """
        When a spread leg is squared off/closed (net qty = 0) while the other leg remains open,
        only the remaining open leg should be present in get_open_trades_context.
        """
        msg_entry = Message(id=991, text="BUY NIFTY SPREAD", date=datetime.utcnow())
        msg_exit = Message(id=992, text="CLOSE HEDGE LEG", date=datetime.utcnow())
        self.session.add_all([msg_entry, msg_exit])
        self.session.commit()

        trade = Trade(
            status="OPEN",
            underlying="NIFTY",
            structure_type="NIFTY BULL PUT SPREAD"
        )
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)

        # Main short leg: SELL 65 (remains OPEN)
        act_main = Action(
            trade_id=trade.id,
            message_id=msg_entry.id,
            action_type="SELL",
            transaction_type="SELL",
            tradingsymbol="NIFTY2681824600PE",
            strike=24600.0,
            option_type="PE",
            quantity=65,
            lots=1,
            is_main=True,
            order_status="PLACED"
        )
        # Hedge long leg: BUY 65
        act_hedge_entry = Action(
            trade_id=trade.id,
            message_id=msg_entry.id,
            action_type="BUY",
            transaction_type="BUY",
            tradingsymbol="NIFTY2681824300PE",
            strike=24300.0,
            option_type="PE",
            quantity=65,
            lots=1,
            is_main=False,
            order_status="PLACED"
        )
        # Hedge exit: SELL 65 (closes hedge leg, net qty = 0)
        act_hedge_exit = Action(
            trade_id=trade.id,
            message_id=msg_exit.id,
            action_type="EXIT",
            transaction_type="SELL",
            tradingsymbol="NIFTY2681824300PE",
            strike=24300.0,
            option_type="PE",
            quantity=65,
            lots=1,
            is_main=False,
            order_status="PLACED"
        )

        self.session.add_all([act_main, act_hedge_entry, act_hedge_exit])
        self.session.commit()

        context = get_open_trades_context(self.session)
        matching = [t for t in context if t["id"] == trade.id]
        self.assertEqual(len(matching), 1)
        trade_data = matching[0]

        # Only the main leg (24600 PE) should be active; hedge leg (24300 PE) is FLAT (0 qty)
        active_legs = trade_data["active_legs"]
        self.assertEqual(len(active_legs), 1)
        self.assertEqual(active_legs[0]["tradingsymbol"], "NIFTY2681824600PE")
        self.assertEqual(active_legs[0]["strike"], 24600.0)

        # Tabular formatting should contain only the remaining leg
        table_str = format_open_trades_context(context)
        self.assertIn("NIFTY2681824600PE", table_str)
        self.assertNotIn("NIFTY2681824300PE", table_str)

    def test_token_and_context_size_reduction(self):
        """
        Verify that the tabular format drastically reduces token footprint
        compared to legacy JSON dumping of raw trade/action models.
        """
        open_trades_data = [
            {
                "id": 1,
                "status": "OPEN",
                "structure_type": "NIFTY BULL PUT SPREAD",
                "underlying": "NIFTY",
                "active_legs": [
                    {
                        "tradingsymbol": "NIFTY2681824600PE",
                        "strike": 24600.0,
                        "option_type": "PE",
                        "transaction_type": "SELL",
                        "is_main": True
                    },
                    {
                        "tradingsymbol": "NIFTY2681824300PE",
                        "strike": 24300.0,
                        "option_type": "PE",
                        "transaction_type": "BUY",
                        "is_main": False
                    }
                ]
            },
            {
                "id": 2,
                "status": "OPEN",
                "structure_type": "BANKNIFTY BEAR CALL SPREAD",
                "underlying": "BANKNIFTY",
                "active_legs": [
                    {
                        "tradingsymbol": "BANKNIFTY2681852000CE",
                        "strike": 52000.0,
                        "option_type": "CE",
                        "transaction_type": "SELL",
                        "is_main": True
                    },
                    {
                        "tradingsymbol": "BANKNIFTY2681852500CE",
                        "strike": 52500.0,
                        "option_type": "CE",
                        "transaction_type": "BUY",
                        "is_main": False
                    }
                ]
            }
        ]

        table_str = format_open_trades_context(open_trades_data)
        self.assertTrue(len(table_str) < 500)
        self.assertIn("NIFTY", table_str)
        self.assertIn("BANKNIFTY", table_str)


if __name__ == "__main__":
    unittest.main()

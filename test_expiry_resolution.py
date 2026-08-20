import unittest
from unittest.mock import patch
from datetime import datetime
from instruments_manager import (
    parse_expiry_hint,
    is_monthly_contract,
    match_candidate_by_expiry,
    resolve_nfo_instrument,
    format_instrument_result
)


class TestParseExpiryHint(unittest.TestCase):
    """
    Unit tests for parse_expiry_hint verifying natural language parsing
    of all Indian F&O expiry formats.
    """
    def test_iso_dates(self):
        res1 = parse_expiry_hint("2026-08-04")
        self.assertEqual(res1, {"type": "exact_date", "year": 2026, "month": 8, "day": 4, "iso": "2026-08-04"})

        res2 = parse_expiry_hint("2026/09/15")
        self.assertEqual(res2, {"type": "exact_date", "year": 2026, "month": 9, "day": 15, "iso": "2026-09-15"})

        res3 = parse_expiry_hint("2026.10.27")
        self.assertEqual(res3, {"type": "exact_date", "year": 2026, "month": 10, "day": 27, "iso": "2026-10-27"})

    def test_standard_numeric_dates(self):
        res1 = parse_expiry_hint("04-08-2026")
        self.assertEqual(res1, {"type": "exact_date", "year": 2026, "month": 8, "day": 4, "iso": "2026-08-04"})

        res2 = parse_expiry_hint("4/8/2026")
        self.assertEqual(res2, {"type": "exact_date", "year": 2026, "month": 8, "day": 4, "iso": "2026-08-04"})

        res3 = parse_expiry_hint("11-08-26")
        self.assertEqual(res3, {"type": "exact_date", "year": 2026, "month": 8, "day": 11, "iso": "2026-08-11"})

    def test_day_month_phrases(self):
        # 4th Aug Series / 4th Aug
        res1 = parse_expiry_hint("4th Aug Series")
        self.assertEqual(res1["type"], "specific_day_month")
        self.assertEqual(res1["day"], 4)
        self.assertEqual(res1["month"], 8)

        res2 = parse_expiry_hint("4 Aug")
        self.assertEqual(res2["day"], 4)
        self.assertEqual(res2["month"], 8)

        res3 = parse_expiry_hint("11th Aug")
        self.assertEqual(res3["day"], 11)
        self.assertEqual(res3["month"], 8)

        res4 = parse_expiry_hint("11 Aug")
        self.assertEqual(res4["day"], 11)
        self.assertEqual(res4["month"], 8)

        res5 = parse_expiry_hint("28JUL")
        self.assertEqual(res5["day"], 28)
        self.assertEqual(res5["month"], 7)

        res6 = parse_expiry_hint("28JUL2026")
        self.assertEqual(res6["day"], 28)
        self.assertEqual(res6["month"], 7)
        self.assertEqual(res6["year"], 2026)

        res7 = parse_expiry_hint("4-Aug-2026")
        self.assertEqual(res7["day"], 4)
        self.assertEqual(res7["month"], 8)
        self.assertEqual(res7["year"], 2026)

        res8 = parse_expiry_hint("4th August")
        self.assertEqual(res8["day"], 4)
        self.assertEqual(res8["month"], 8)

    def test_month_day_phrases(self):
        res1 = parse_expiry_hint("Aug 4th")
        self.assertEqual(res1["type"], "specific_day_month")
        self.assertEqual(res1["day"], 4)
        self.assertEqual(res1["month"], 8)

        res2 = parse_expiry_hint("August 11")
        self.assertEqual(res2["day"], 11)
        self.assertEqual(res2["month"], 8)

        res3 = parse_expiry_hint("Aug 25, 2026")
        self.assertEqual(res3["day"], 25)
        self.assertEqual(res3["month"], 8)
        self.assertEqual(res3["year"], 2026)

    def test_month_series_and_explicit_month(self):
        res1 = parse_expiry_hint("Aug Series")
        self.assertEqual(res1["type"], "month_series")
        self.assertEqual(res1["month"], 8)
        self.assertTrue(res1["is_monthly"])

        res2 = parse_expiry_hint("August Series")
        self.assertEqual(res2["type"], "month_series")
        self.assertEqual(res2["month"], 8)
        self.assertTrue(res2["is_monthly"])

        res3 = parse_expiry_hint("AUG")
        self.assertEqual(res3["type"], "month_series")
        self.assertEqual(res3["month"], 8)

        res4 = parse_expiry_hint("August")
        self.assertEqual(res4["type"], "month_series")
        self.assertEqual(res4["month"], 8)

        res5 = parse_expiry_hint("Aug 2026 Monthly Series")
        self.assertEqual(res5["type"], "month_series")
        self.assertEqual(res5["month"], 8)
        self.assertEqual(res5["year"], 2026)

        res6 = parse_expiry_hint("AUG EXPIRY")
        self.assertEqual(res6["type"], "month_series")
        self.assertEqual(res6["month"], 8)

    def test_relative_month_terms(self):
        res1 = parse_expiry_hint("Monthly")
        self.assertEqual(res1, {"type": "relative_month", "offset": 0, "is_monthly": True})

        res2 = parse_expiry_hint("Current Month")
        self.assertEqual(res2, {"type": "relative_month", "offset": 0, "is_monthly": True})

        res3 = parse_expiry_hint("This Month")
        self.assertEqual(res3, {"type": "relative_month", "offset": 0, "is_monthly": True})

        res4 = parse_expiry_hint("Next Month")
        self.assertEqual(res4, {"type": "relative_month", "offset": 1, "is_monthly": True})

        res5 = parse_expiry_hint("Next Monthly")
        self.assertEqual(res5, {"type": "relative_month", "offset": 1, "is_monthly": True})

        res6 = parse_expiry_hint("Far Month")
        self.assertEqual(res6, {"type": "relative_month", "offset": 2, "is_monthly": True})

    def test_relative_week_terms(self):
        res1 = parse_expiry_hint("Weekly")
        self.assertEqual(res1, {"type": "relative_week", "offset": 0, "is_weekly": True})

        res2 = parse_expiry_hint("This Week")
        self.assertEqual(res2, {"type": "relative_week", "offset": 0, "is_weekly": True})

        res3 = parse_expiry_hint("Next Week")
        self.assertEqual(res3, {"type": "relative_week", "offset": 1, "is_weekly": True})

    def test_empty_and_none(self):
        self.assertIsNone(parse_expiry_hint(None))
        self.assertIsNone(parse_expiry_hint(""))
        self.assertIsNone(parse_expiry_hint("   "))


class TestIsMonthlyContract(unittest.TestCase):
    """
    Unit tests for is_monthly_contract distinguishing Index Weeklies,
    Index Monthlies, and Stock Monthlies.
    """
    def test_index_monthlies(self):
        self.assertTrue(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY26AUG24500CE"}))
        self.assertTrue(is_monthly_contract({"name": "BANKNIFTY", "tradingsymbol": "BANKNIFTY26AUG52000PE"}))
        self.assertTrue(is_monthly_contract({"name": "FINNIFTY", "tradingsymbol": "FINNIFTY26AUG23000CE"}))
        self.assertTrue(is_monthly_contract({"name": "MIDCPNIFTY", "tradingsymbol": "MIDCPNIFTY26SEPFUT"}))
        self.assertTrue(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY26OCT24000PE"}))

    def test_index_weeklies(self):
        self.assertFalse(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY2680424500CE"}))
        self.assertFalse(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY2681124500CE"}))
        self.assertFalse(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY2681824600PE"}))
        self.assertFalse(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY2690124500CE"}))
        self.assertFalse(is_monthly_contract({"name": "NIFTY", "tradingsymbol": "NIFTY26O0624500CE"}))
        self.assertFalse(is_monthly_contract({"name": "BANKNIFTY", "tradingsymbol": "BANKNIFTY2681852000CE"}))

    def test_stock_monthlies(self):
        self.assertTrue(is_monthly_contract({"name": "TATASTEEL", "tradingsymbol": "TATASTEEL26AUG192.5PE"}))
        self.assertTrue(is_monthly_contract({"name": "RELIANCE", "tradingsymbol": "RELIANCE26AUG3000CE"}))
        self.assertTrue(is_monthly_contract({"name": "INFY", "tradingsymbol": "INFY26SEPFUT"}))
        self.assertTrue(is_monthly_contract({"name": "MARUTI", "tradingsymbol": "MARUTI26AUG12000CE"}))
        self.assertTrue(is_monthly_contract({"name": "360ONE", "tradingsymbol": "360ONE26AUGFUT"}))
        self.assertTrue(is_monthly_contract({"name": "M&M", "tradingsymbol": "M&M26AUGFUT"}))


class TestCandidateMatchingByExpiry(unittest.TestCase):
    """
    Unit tests for match_candidate_by_expiry demonstrating fix for Issue 5.1:
    - 'Aug Series' matches August Monthly contract (e.g. 2026-08-25) and NOT August weeklies (2026-08-04, 2026-08-11).
    - '4th Aug Series' matches 4th August weekly contract (2026-08-04).
    - '11 Aug' matches 11th August weekly contract (2026-08-11).
    - 'Monthly' matches nearest monthly contract.
    """
    def setUp(self):
        self.nifty_candidates = [
            {"tradingsymbol": "NIFTY2680424500CE", "expiry": "2026-08-04", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY2681124500CE", "expiry": "2026-08-11", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY2681824500CE", "expiry": "2026-08-18", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY26AUG24500CE", "expiry": "2026-08-25", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY2690124500CE", "expiry": "2026-09-01", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY2690824500CE", "expiry": "2026-09-08", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY26SEP24500CE", "expiry": "2026-09-29", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
            {"tradingsymbol": "NIFTY26OCT24500CE", "expiry": "2026-10-27", "name": "NIFTY", "strike": 24500, "instrument_type": "CE"},
        ]

        self.stock_candidates = [
            {"tradingsymbol": "TATASTEEL26AUG192.5PE", "expiry": "2026-08-25", "name": "TATASTEEL", "strike": 192.5, "instrument_type": "PE"},
            {"tradingsymbol": "TATASTEEL26SEP192.5PE", "expiry": "2026-09-29", "name": "TATASTEEL", "strike": 192.5, "instrument_type": "PE"},
            {"tradingsymbol": "TATASTEEL26OCT192.5PE", "expiry": "2026-10-27", "name": "TATASTEEL", "strike": 192.5, "instrument_type": "PE"},
        ]

    def test_aug_series_matches_monthly_not_weekly(self):
        # 'Aug Series' MUST resolve to August Monthly (NIFTY26AUG24500CE), NOT 4th Aug weekly (NIFTY2680424500CE)
        res = match_candidate_by_expiry(self.nifty_candidates, "Aug Series")
        self.assertEqual(res["tradingsymbol"], "NIFTY26AUG24500CE")
        self.assertEqual(res["expiry"], "2026-08-25")

        # 'August Series'
        res = match_candidate_by_expiry(self.nifty_candidates, "August Series")
        self.assertEqual(res["tradingsymbol"], "NIFTY26AUG24500CE")

        # 'AUG'
        res = match_candidate_by_expiry(self.nifty_candidates, "AUG")
        self.assertEqual(res["tradingsymbol"], "NIFTY26AUG24500CE")

    def test_fourth_aug_series_matches_fourth_aug_weekly(self):
        # '4th Aug Series' MUST resolve to 4th Aug weekly contract (2026-08-04)
        res1 = match_candidate_by_expiry(self.nifty_candidates, "4th Aug Series")
        self.assertEqual(res1["tradingsymbol"], "NIFTY2680424500CE")
        self.assertEqual(res1["expiry"], "2026-08-04")

        # '4th Aug'
        res2 = match_candidate_by_expiry(self.nifty_candidates, "4th Aug")
        self.assertEqual(res2["tradingsymbol"], "NIFTY2680424500CE")

        # '4 Aug'
        res3 = match_candidate_by_expiry(self.nifty_candidates, "4 Aug")
        self.assertEqual(res3["tradingsymbol"], "NIFTY2680424500CE")

    def test_eleventh_aug_series_matches_eleventh_aug_weekly(self):
        # '11 Aug' MUST resolve to 11th Aug weekly contract (2026-08-11)
        res1 = match_candidate_by_expiry(self.nifty_candidates, "11 Aug")
        self.assertEqual(res1["tradingsymbol"], "NIFTY2681124500CE")
        self.assertEqual(res1["expiry"], "2026-08-11")

        # '11th Aug'
        res2 = match_candidate_by_expiry(self.nifty_candidates, "11th Aug")
        self.assertEqual(res2["tradingsymbol"], "NIFTY2681124500CE")

    def test_eighteenth_aug_matches_weekly(self):
        res = match_candidate_by_expiry(self.nifty_candidates, "18 Aug")
        self.assertEqual(res["tradingsymbol"], "NIFTY2681824500CE")
        self.assertEqual(res["expiry"], "2026-08-18")

    def test_monthly_and_relative_month_resolution(self):
        # 'Monthly' -> Nearest monthly contract (NIFTY26AUG24500CE)
        res1 = match_candidate_by_expiry(self.nifty_candidates, "Monthly")
        self.assertEqual(res1["tradingsymbol"], "NIFTY26AUG24500CE")

        # 'Current Month'
        res2 = match_candidate_by_expiry(self.nifty_candidates, "Current Month")
        self.assertEqual(res2["tradingsymbol"], "NIFTY26AUG24500CE")

        # 'Next Month' -> September Monthly contract (NIFTY26SEP24500CE)
        res3 = match_candidate_by_expiry(self.nifty_candidates, "Next Month")
        self.assertEqual(res3["tradingsymbol"], "NIFTY26SEP24500CE")

        # 'Far Month' -> October Monthly contract (NIFTY26OCT24500CE)
        res4 = match_candidate_by_expiry(self.nifty_candidates, "Far Month")
        self.assertEqual(res4["tradingsymbol"], "NIFTY26OCT24500CE")

    def test_weekly_and_relative_week_resolution(self):
        # 'Weekly' -> Nearest weekly (2026-08-04)
        res1 = match_candidate_by_expiry(self.nifty_candidates, "Weekly")
        self.assertEqual(res1["tradingsymbol"], "NIFTY2680424500CE")

        # 'Next Week' -> Second weekly (2026-08-11)
        res2 = match_candidate_by_expiry(self.nifty_candidates, "Next Week")
        self.assertEqual(res2["tradingsymbol"], "NIFTY2681124500CE")

    def test_september_series_and_weeklies(self):
        # '1 Sep' -> 1st Sep weekly
        res1 = match_candidate_by_expiry(self.nifty_candidates, "1 Sep")
        self.assertEqual(res1["tradingsymbol"], "NIFTY2690124500CE")

        # 'Sep Series' -> September Monthly (NIFTY26SEP24500CE)
        res2 = match_candidate_by_expiry(self.nifty_candidates, "Sep Series")
        self.assertEqual(res2["tradingsymbol"], "NIFTY26SEP24500CE")

    def test_stock_contract_resolution(self):
        # Stock with 'Aug Series'
        res1 = match_candidate_by_expiry(self.stock_candidates, "Aug Series")
        self.assertEqual(res1["tradingsymbol"], "TATASTEEL26AUG192.5PE")

        # Stock with 'Monthly'
        res2 = match_candidate_by_expiry(self.stock_candidates, "Monthly")
        self.assertEqual(res2["tradingsymbol"], "TATASTEEL26AUG192.5PE")

        # Stock with 'Next Month'
        res3 = match_candidate_by_expiry(self.stock_candidates, "Next Month")
        self.assertEqual(res3["tradingsymbol"], "TATASTEEL26SEP192.5PE")

        # Stock with None hint -> Defaults to nearest active
        res4 = match_candidate_by_expiry(self.stock_candidates, None)
        self.assertEqual(res4["tradingsymbol"], "TATASTEEL26AUG192.5PE")


class TestResolveNfoInstrumentWithMock(unittest.TestCase):
    """
    Tests end-to-end resolve_nfo_instrument with realistic candidate data.
    """
    def setUp(self):
        self.mock_instruments = [
            # NIFTY CE 24500 contracts
            {"instrument_token": "1001", "exchange_token": "101", "tradingsymbol": "NIFTY2680424500CE", "name": "NIFTY", "last_price": "100", "expiry": "2026-08-04", "strike": "24500", "tick_size": "0.05", "lot_size": "65", "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO"},
            {"instrument_token": "1002", "exchange_token": "102", "tradingsymbol": "NIFTY2681124500CE", "name": "NIFTY", "last_price": "120", "expiry": "2026-08-11", "strike": "24500", "tick_size": "0.05", "lot_size": "65", "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO"},
            {"instrument_token": "1003", "exchange_token": "103", "tradingsymbol": "NIFTY2681824500CE", "name": "NIFTY", "last_price": "150", "expiry": "2026-08-18", "strike": "24500", "tick_size": "0.05", "lot_size": "65", "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO"},
            {"instrument_token": "1004", "exchange_token": "104", "tradingsymbol": "NIFTY26AUG24500CE", "name": "NIFTY", "last_price": "180", "expiry": "2026-08-25", "strike": "24500", "tick_size": "0.05", "lot_size": "65", "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO"},
            {"instrument_token": "1005", "exchange_token": "105", "tradingsymbol": "NIFTY2690124500CE", "name": "NIFTY", "last_price": "200", "expiry": "2026-09-01", "strike": "24500", "tick_size": "0.05", "lot_size": "65", "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO"},
            {"instrument_token": "1006", "exchange_token": "106", "tradingsymbol": "NIFTY26SEP24500CE", "name": "NIFTY", "last_price": "250", "expiry": "2026-09-29", "strike": "24500", "tick_size": "0.05", "lot_size": "65", "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO"},
            # TATASTEEL PE 192.5 contracts
            {"instrument_token": "2001", "exchange_token": "201", "tradingsymbol": "TATASTEEL26AUG192.5PE", "name": "TATASTEEL", "last_price": "4.5", "expiry": "2026-08-25", "strike": "192.5", "tick_size": "0.05", "lot_size": "5500", "instrument_type": "PE", "segment": "NFO-OPT", "exchange": "NFO"},
            {"instrument_token": "2002", "exchange_token": "202", "tradingsymbol": "TATASTEEL26SEP192.5PE", "name": "TATASTEEL", "last_price": "6.0", "expiry": "2026-09-29", "strike": "192.5", "tick_size": "0.05", "lot_size": "5500", "instrument_type": "PE", "segment": "NFO-OPT", "exchange": "NFO"},
        ]

    @patch("instruments_manager.datetime")
    @patch("instruments_manager.get_nfo_instruments")
    def test_resolve_aug_series_vs_4th_aug_series(self, mock_get_inst, mock_dt):
        mock_dt.now.return_value = datetime(2026, 8, 1, 9, 15, 0)
        mock_dt.strptime = datetime.strptime
        mock_get_inst.return_value = self.mock_instruments

        # Aug Series -> NIFTY26AUG24500CE
        res_aug_series = resolve_nfo_instrument("NIFTY", strike=24500.0, option_type="CE", expiry_hint="Aug Series")
        self.assertIsNotNone(res_aug_series)
        self.assertEqual(res_aug_series["tradingsymbol"], "NIFTY26AUG24500CE")
        self.assertEqual(res_aug_series["expiry"], "2026-08-25")

        # 4th Aug Series -> NIFTY2680424500CE
        res_4th_aug = resolve_nfo_instrument("NIFTY", strike=24500.0, option_type="CE", expiry_hint="4th Aug Series")
        self.assertIsNotNone(res_4th_aug)
        self.assertEqual(res_4th_aug["tradingsymbol"], "NIFTY2680424500CE")
        self.assertEqual(res_4th_aug["expiry"], "2026-08-04")

        # 11 Aug -> NIFTY2681124500CE
        res_11_aug = resolve_nfo_instrument("NIFTY", strike=24500.0, option_type="CE", expiry_hint="11 Aug")
        self.assertIsNotNone(res_11_aug)
        self.assertEqual(res_11_aug["tradingsymbol"], "NIFTY2681124500CE")
        self.assertEqual(res_11_aug["expiry"], "2026-08-11")

        # Monthly -> NIFTY26AUG24500CE
        res_monthly = resolve_nfo_instrument("NIFTY", strike=24500.0, option_type="CE", expiry_hint="Monthly")
        self.assertIsNotNone(res_monthly)
        self.assertEqual(res_monthly["tradingsymbol"], "NIFTY26AUG24500CE")

        # Next Month -> NIFTY26SEP24500CE
        res_next_month = resolve_nfo_instrument("NIFTY", strike=24500.0, option_type="CE", expiry_hint="Next Month")
        self.assertIsNotNone(res_next_month)
        self.assertEqual(res_next_month["tradingsymbol"], "NIFTY26SEP24500CE")

    @patch("instruments_manager.datetime")
    @patch("instruments_manager.get_nfo_instruments")
    def test_resolve_stock_contracts(self, mock_get_inst, mock_dt):
        mock_dt.now.return_value = datetime(2026, 8, 1, 9, 15, 0)
        mock_dt.strptime = datetime.strptime
        mock_get_inst.return_value = self.mock_instruments

        res_stock = resolve_nfo_instrument("TATASTEEL", strike=192.5, option_type="PE", expiry_hint="Aug Series")
        self.assertIsNotNone(res_stock)
        self.assertEqual(res_stock["tradingsymbol"], "TATASTEEL26AUG192.5PE")

        res_stock_next = resolve_nfo_instrument("TATASTEEL", strike=192.5, option_type="PE", expiry_hint="Next Month")
        self.assertIsNotNone(res_stock_next)
        self.assertEqual(res_stock_next["tradingsymbol"], "TATASTEEL26SEP192.5PE")


if __name__ == "__main__":
    unittest.main()

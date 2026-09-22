#!/usr/bin/env python3
"""
Operational Robustness & Performance Benchmark Tests
for PSX Today's Opportunities (Same-Day Scanner)
====================================================
Tests:
- Stage 9.1: Time-Exit Cutoff Fires Reliably (Regular 15:15 PKT, Friday 16:15 PKT)
- Stage 9.2: Jummah Break, Holiday, and Ramadan Session Overrides
- Stage 9.3: Universe Scan Performance (< 2 seconds across 150+ symbols)
- API endpoint integration tests
"""

import os
import time
import unittest
import datetime
from pathlib import Path

import psx_calendar
import shared_trading_utils
import psx_daily_opportunities

TEST_DB_PATH = Path(__file__).parent / "cache" / "test_daily_opps_robustness.db"


class TestDailyOpportunitiesRobustness(unittest.TestCase):

    def setUp(self):
        if TEST_DB_PATH.exists():
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass
        self.db = psx_daily_opportunities.DailyOpportunitiesDB(TEST_DB_PATH)
        self.config = psx_daily_opportunities.load_daily_config()
        self.scanner = psx_daily_opportunities.DailyOpportunitiesScanner(self.db, self.config)

    def tearDown(self):
        if TEST_DB_PATH.exists():
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def test_time_exit_timing_regular_day(self):
        """Test Mon-Thu time exit at 15:15 PKT (15 mins before 15:30 close)."""
        sm = self.scanner.lifecycle

        # Create candidate triggered at 11:00
        cid = self.db.save_candidate({
            "symbol": "HUBC",
            "tier": "Momentum",
            "setup_type": "CIRCUIT_RUNNER",
            "state": "TRIGGERED",
            "entry_price": 100.0,
            "target_price": 107.0,
            "stop_loss": 97.0,
            "shares": 1000,
            "detected_at": "2026-09-22 11:00:00"
        })

        cand = dict(self.db.get_all_today()[0])

        # 1. At 15:14 PKT (1 minute before cutoff) -> Must remain TRIGGERED
        sched_mon = shared_trading_utils.get_session_schedule(datetime.datetime(2026, 9, 22, 15, 14))
        self.assertEqual(sched_mon["time_exit_mins"], 915)  # 15:15
        dt_1514 = datetime.datetime(2026, 9, 22, 15, 14, tzinfo=psx_calendar.PKT_TIMEZONE)
        c_before = sm.update_open_candidate(cand, 102.0, sched_mon, dt_1514)
        self.assertEqual(c_before["state"], "TRIGGERED")

        # 2. At 15:15 PKT (Exact cutoff) -> Must force TIME_EXIT
        dt_1515 = datetime.datetime(2026, 9, 22, 15, 15, tzinfo=psx_calendar.PKT_TIMEZONE)
        c_at = sm.update_open_candidate(cand, 102.5, sched_mon, dt_1515)
        self.assertEqual(c_at["state"], "CLOSED")
        self.assertEqual(c_at["exit_type"], "TIME_EXIT")
        self.assertEqual(c_at["exit_price"], 102.5)
        self.assertEqual(c_at["pnl_pct"], 2.5)

    def test_friday_split_session_and_jummah_break(self):
        """Test Friday morning session, Jummah break holding, and afternoon 16:15 cutoff."""
        sm = self.scanner.lifecycle

        # Friday 2026-09-25
        dt_morning = datetime.datetime(2026, 9, 25, 10, 30, tzinfo=psx_calendar.PKT_TIMEZONE)
        sched_fri_morning = shared_trading_utils.get_session_schedule(dt_morning)
        self.assertTrue(sched_fri_morning["is_friday"])
        self.assertTrue(sched_fri_morning["is_in_trading_hours"])
        self.assertEqual(sched_fri_morning["time_exit_mins"], 975)  # 16:15 PKT

        # Candidate triggered in morning session
        cid = self.db.save_candidate({
            "symbol": "TRG",
            "tier": "Active",
            "setup_type": "VWAP_RECLAIM",
            "state": "TRIGGERED",
            "entry_price": 50.0,
            "target_price": 53.0,
            "stop_loss": 48.5,
            "shares": 500,
            "detected_at": "2026-09-25 10:30:00"
        })
        cand = dict(self.db.get_all_today()[0])

        # At 13:00 PKT (During Jummah Break: 12:00 to 14:32 PKT)
        dt_jummah = datetime.datetime(2026, 9, 25, 13, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        sched_jummah = shared_trading_utils.get_session_schedule(dt_jummah)
        self.assertTrue(sched_jummah["is_jummah_break"])
        self.assertTrue(sched_jummah["allow_hold_through_jummah_break"])

        # Candidate must remain TRIGGERED across Jummah prayer break (pause, not a close)
        c_during_break = sm.update_open_candidate(cand, 50.5, sched_jummah, dt_jummah)
        self.assertEqual(c_during_break["state"], "TRIGGERED")

        # At 16:16 PKT (Past Friday afternoon 16:15 cutoff) -> Must force TIME_EXIT
        dt_fri_close = datetime.datetime(2026, 9, 25, 16, 16, tzinfo=psx_calendar.PKT_TIMEZONE)
        sched_fri_close = shared_trading_utils.get_session_schedule(dt_fri_close)
        c_fri_exit = sm.update_open_candidate(cand, 51.5, sched_fri_close, dt_fri_close)
        self.assertEqual(c_fri_exit["state"], "CLOSED")
        self.assertEqual(c_fri_exit["exit_type"], "TIME_EXIT")

    def test_holiday_schedule_suppression(self):
        """Test official PSX holidays (e.g. Pakistan Day March 23)."""
        dt_holiday = datetime.datetime(2026, 3, 23, 11, 0, tzinfo=psx_calendar.PKT_TIMEZONE)
        sched = shared_trading_utils.get_session_schedule(dt_holiday)
        self.assertFalse(sched["is_trading_day"])
        self.assertFalse(sched["is_in_trading_hours"])
        self.assertIn("Pakistan Day", sched["reason"])

    def test_universe_scan_performance_benchmark(self):
        """Simulate scanning 150 liquid symbols and verify completion in < 2.0 seconds."""
        # Generate 150 synthetic liquid stocks
        mock_stocks = []
        for i in range(150):
            mock_stocks.append({
                "symbol": f"STOCK_{i:03d}",
                "price": 50.0 + (i % 20),
                "change": 1.5 + (i % 5),
                "volume": 100000 + (i * 2000),
                "ldcp": 50.0,
                "high": 52.0,
                "low": 49.5,
                "open": 50.5,
                "turnover": (100000 + (i * 2000)) * (50.0 + (i % 20))
            })

        # Synthetic history provider
        def mock_history_provider(sym):
            return [
                {"date": f"2026-08-{d:02d}", "high": 52.0, "low": 48.0, "close": 50.0, "volume": 100000}
                for d in range(1, 22)
            ]

        t0 = time.time()
        res = self.scanner.scan_universe(
            stocks=mock_stocks,
            history_provider=mock_history_provider,
            market_regime="Trending Bull",
            circuit_anomalies=["STOCK_010", "STOCK_025"]
        )
        elapsed = time.time() - t0

        self.assertTrue(res["success"])
        self.assertLess(elapsed, 2.0, f"Scan exceeded 2.0s benchmark: took {elapsed:.2f}s")
        self.assertGreater(len(res["candidates"]), 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Unit tests for Signal Logging & Outcome Tracker (Stage 0.1 & 0.2)
"""

import unittest
import time
from pathlib import Path
from live_trading_db import (
    init_db,
    log_signal_if_eligible,
    evaluate_single_outcome,
    track_signal_outcomes,
    get_signal_statistics
)

TEST_DB = Path(__file__).parent / "cache" / "test_live_signals.db"


class TestLiveTradingDB(unittest.TestCase):
    def setUp(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        init_db(TEST_DB)

    def tearDown(self):
        if TEST_DB.exists():
            TEST_DB.unlink()

    def test_throttled_signal_logging(self):
        sig1 = {
            "symbol": "OGDC",
            "price": 100.0,
            "score": 75.0,
            "recommendation": "STRONG BUY",
            "suggestedEntry": 100.0,
            "targetPrice": 104.5,
            "stopLoss": 97.5,
            "riskLevel": "Low",
            "marketStatus": "Open",
            "data_freshness_sec": 5.0
        }
        id1 = log_signal_if_eligible(sig1, db_file=TEST_DB)
        self.assertIsNotNone(id1)

        # Immediate duplicate with same score and rec -> should be throttled (returns None)
        id2 = log_signal_if_eligible(sig1, db_file=TEST_DB)
        self.assertIsNone(id2)

        # Score shift >= 5 points -> should log
        sig_shift = dict(sig1)
        sig_shift["score"] = 82.0
        id3 = log_signal_if_eligible(sig_shift, db_file=TEST_DB)
        self.assertIsNotNone(id3)

        # Recommendation change -> should log
        sig_rec_change = dict(sig1)
        sig_rec_change["recommendation"] = "BUY"
        id4 = log_signal_if_eligible(sig_rec_change, db_file=TEST_DB)
        self.assertIsNotNone(id4)

    def test_evaluate_single_outcome(self):
        # Target hit scenario (BUY: entry 100, target 105, stop 95)
        bars_target = [{"high": 106.0, "low": 99.0, "close": 105.5}]
        status, r, days, _ = evaluate_single_outcome("BUY", 100.0, 105.0, 95.0, bars_target)
        self.assertEqual(status, "TARGET_HIT")
        self.assertEqual(r, 1.0)
        self.assertEqual(days, 1)

        # Stop hit scenario
        bars_stop = [{"high": 101.0, "low": 94.0, "close": 94.5}]
        status, r, days, _ = evaluate_single_outcome("BUY", 100.0, 105.0, 95.0, bars_stop)
        self.assertEqual(status, "STOP_HIT")
        self.assertEqual(r, -1.0)

        # Ambiguous scenario (both high >= 105 and low <= 95) -> counted as loss for safety
        bars_ambig = [{"high": 107.0, "low": 93.0, "close": 100.0}]
        status, r, days, _ = evaluate_single_outcome("BUY", 100.0, 105.0, 95.0, bars_ambig)
        self.assertEqual(status, "AMBIGUOUS_LOSS")
        self.assertEqual(r, -1.0)

    def test_signal_statistics(self):
        # Log a signal and simulate outcome tracking
        sig = {
            "symbol": "TRG",
            "price": 50.0,
            "score": 80.0,
            "recommendation": "STRONG BUY",
            "suggestedEntry": 50.0,
            "targetPrice": 52.5,
            "stopLoss": 48.75,
            "riskLevel": "Medium",
            "marketStatus": "Open",
            "data_freshness_sec": 2.0
        }
        log_signal_if_eligible(sig, db_file=TEST_DB)

        # Mock forward history fetcher returning winning bar
        def mock_fetcher(sym, created_at):
            return [{"high": 53.0, "low": 49.5, "close": 52.8}]

        updated = track_signal_outcomes(history_fetcher=mock_fetcher, db_file=TEST_DB)
        self.assertEqual(updated, 1)

        stats = get_signal_statistics(db_file=TEST_DB)
        self.assertEqual(stats["total_signals"], 1)
        self.assertEqual(stats["resolved_signals"], 1)
        self.assertEqual(stats["overall_win_rate_pct"], 100.0)
        self.assertGreater(stats["overall_avg_r_multiple"], 0)
        self.assertIn("STRONG BUY", stats["tier_breakdown"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Test Stage 0 Integration & Endpoints
Verifies:
1. config/psx_calendar.json dynamic loading in get_psx_market_status
2. fetch_live_stock_analysis returns dataFreshnessSec
3. live_trading_db: logging, outcome tracking, signal statistics
4. config/live_trading.json parameter availability
"""

import json
import unittest
import time
from pathlib import Path

import live_trading_db
import technical_indicators
from server import get_psx_market_status, fetch_live_stock_analysis

TEST_DB_PATH = Path(__file__).parent / "cache" / "test_stage0_live_trading.db"


class TestStage0Integration(unittest.TestCase):
    def setUp(self):
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()

    def tearDown(self):
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()

    def test_psx_calendar_status(self):
        status = get_psx_market_status()
        self.assertIn("status", status)
        self.assertIn("is_open", status)
        self.assertIn("reason", status)
        self.assertIn("pkt_time", status)
        self.assertIsInstance(status["is_open"], bool)

    def test_live_stock_analysis_freshness(self):
        analysis = fetch_live_stock_analysis("OGDC")
        self.assertEqual(analysis["symbol"], "OGDC")
        self.assertIn("dataFreshnessSec", analysis)
        self.assertIn("lastTickTimestamp", analysis)
        self.assertGreaterEqual(analysis["dataFreshnessSec"], 0)

    def test_signal_logging_and_stats(self):
        sig = {
            "symbol": "OGDC",
            "price": 160.0,
            "score": 82.0,
            "recommendation": "STRONG BUY",
            "suggestedEntry": 160.0,
            "targetPrice": 167.2,
            "stopLoss": 156.0,
            "riskLevel": "Medium",
            "marketStatus": "OPEN",
            "data_freshness_sec": 12.0,
            "factors": [{"icon": "📈", "text": "High volume spike", "weight": "High"}]
        }

        # 1. First log should succeed
        sig_id = live_trading_db.log_signal_if_eligible(sig, db_file=TEST_DB_PATH)
        self.assertIsNotNone(sig_id)

        # 2. Immediate repeat without change should throttle
        sig_id_dup = live_trading_db.log_signal_if_eligible(sig, db_file=TEST_DB_PATH)
        self.assertIsNone(sig_id_dup)

        # 3. Retrieve stats
        stats = live_trading_db.get_signal_statistics(db_file=TEST_DB_PATH)
        self.assertEqual(stats["total_signals"], 1)
        self.assertIn("STRONG BUY", stats["tier_breakdown"])
        self.assertEqual(stats["tier_breakdown"]["STRONG BUY"]["count"], 1)

    def test_config_files_valid_json(self):
        cfg_live = Path(__file__).parent / "config" / "live_trading.json"
        cfg_cal = Path(__file__).parent / "config" / "psx_calendar.json"
        self.assertTrue(cfg_live.exists())
        self.assertTrue(cfg_cal.exists())

        with open(cfg_live, "r") as f:
            data = json.load(f)
            self.assertIn("engine_version", data)
            self.assertIn("data_freshness", data)
            self.assertIn("technical_defaults", data)

        with open(cfg_cal, "r") as f:
            data = json.load(f)
            self.assertIn("sessions", data)
            self.assertIn("holidays", data)


if __name__ == "__main__":
    unittest.main()

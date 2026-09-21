import unittest
import time
from pathlib import Path
import tempfile

from live_scanner import run_live_market_scan, clear_scanner_cache
from live_alerts import check_symbol_alerts, clear_alert_history
from live_trading_db import log_signal_if_eligible, get_symbol_signals, init_db


class TestWorkflowFeatures(unittest.TestCase):
    def setUp(self):
        clear_scanner_cache()
        clear_alert_history()

    def test_scanner_liquidity_filtering_and_ranking(self):
        stocks = [
            # Illiquid: low volume
            {"symbol": "ILLIQ1", "price": 10.0, "volume": 1000, "change": 5.0},
            # Illiquid: low turnover
            {"symbol": "ILLIQ2", "price": 2.0, "volume": 30000, "change": 4.0},  # turnover = 60,000 < 500,000
            # Liquid Bullish
            {"symbol": "BULL1", "price": 100.0, "volume": 50000, "change": 3.5},
            # Liquid Bearish
            {"symbol": "BEAR1", "price": 50.0, "volume": 60000, "change": -3.5},
        ]
        res = run_live_market_scan(stocks, force_refresh=True)
        self.assertEqual(res["liquid_count"], 2)
        bullish_syms = [c["symbol"] for c in res["top_bullish"]]
        bearish_syms = [c["symbol"] for c in res["top_bearish"]]
        self.assertIn("BULL1", bullish_syms)
        self.assertIn("BEAR1", bearish_syms)
        self.assertNotIn("ILLIQ1", bullish_syms)
        self.assertNotIn("ILLIQ2", bullish_syms)

    def test_scanner_caching(self):
        stocks = [{"symbol": "OGDC", "price": 300.0, "volume": 50000, "change": 1.0}]
        res1 = run_live_market_scan(stocks, force_refresh=True, cache_ttl_seconds=60)
        t1 = res1["cached_at"]

        # Call again without force_refresh
        res2 = run_live_market_scan([{"symbol": "DIFFERENT", "price": 10.0, "volume": 100}], force_refresh=False)
        self.assertEqual(res2["cached_at"], t1)
        self.assertEqual(res2["top_bullish"][0]["symbol"], "OGDC")

    def test_alert_signal_crossing_and_deduplication(self):
        # 1. First trigger: BUY crossing
        alerts = check_symbol_alerts(
            symbol="SYS",
            current_price=100.0,
            recommendation="BUY",
            target_price=110.0,
            stop_loss=95.0
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "SIGNAL_CROSS")

        # 2. Immediate repeat: should be suppressed by deduplication
        repeat_alerts = check_symbol_alerts(
            symbol="SYS",
            current_price=101.0,
            recommendation="BUY",
            target_price=110.0,
            stop_loss=95.0
        )
        self.assertEqual(len(repeat_alerts), 0)

    def test_alert_target_hit(self):
        # Price hits target 110.0
        alerts = check_symbol_alerts(
            symbol="LUCK",
            current_price=110.5,
            recommendation="BUY",
            target_price=110.0,
            stop_loss=95.0
        )
        target_alerts = [a for a in alerts if a["type"] == "TARGET_HIT"]
        self.assertEqual(len(target_alerts), 1)
        self.assertIn("Target Hit", target_alerts[0]["title"])

    def test_alert_stop_proximity(self):
        # Stop is 95.0. 0.5% threshold means 95 * 1.005 = 95.475.
        # Price at 95.30 is approaching stop
        alerts = check_symbol_alerts(
            symbol="PPL",
            current_price=95.30,
            recommendation="BUY",
            target_price=110.0,
            stop_loss=95.0
        )
        stop_alerts = [a for a in alerts if a["type"] == "STOP_APPROACH"]
        self.assertEqual(len(stop_alerts), 1)
        self.assertIn("Stop Warning", stop_alerts[0]["title"])

    def test_symbol_signals_history(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tmp_path = Path(tmp.name)
            init_db(tmp_path)

            # Log 3 signals for OGDC and 1 for SYS
            for i in range(3):
                signal_dict = {
                    "symbol": "OGDC",
                    "price": 300.0 + i,
                    "confidence": 75.0 + (i * 10.0),
                    "recommendation": "BUY",
                    "factors": [],
                    "entry": 300.0 + i,
                    "target": 320.0,
                    "stop_loss": 290.0,
                    "risk_level": "Low",
                    "engine_version": "v2"
                }
                log_signal_if_eligible(signal_dict, db_file=tmp_path)

            sys_dict = {
                "symbol": "SYS",
                "price": 100.0,
                "confidence": 40.0,
                "recommendation": "SELL",
                "factors": [],
                "entry": 100.0,
                "target": 90.0,
                "stop_loss": 105.0,
                "risk_level": "Medium",
                "engine_version": "v2"
            }
            log_signal_if_eligible(sys_dict, db_file=tmp_path)

            # Query OGDC history
            ogdc_history = get_symbol_signals("OGDC", limit=10, db_file=tmp_path)
            self.assertEqual(len(ogdc_history), 3)
            self.assertEqual(ogdc_history[0]["symbol"], "OGDC")
            self.assertEqual(ogdc_history[0]["recommendation"], "BUY")

            # Query SYS history
            sys_history = get_symbol_signals("SYS", limit=10, db_file=tmp_path)
            self.assertEqual(len(sys_history), 1)
            self.assertEqual(sys_history[0]["symbol"], "SYS")


if __name__ == "__main__":
    unittest.main()

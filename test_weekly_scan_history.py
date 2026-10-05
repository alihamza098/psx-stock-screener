#!/usr/bin/env python3
"""Weekly scan must evaluate real daily history and never synthesise a price series."""

import random
import unittest
from unittest import mock

import weekly_scan_engine as w


class TestWeeklyScanRealHistory(unittest.TestCase):
    stock = {"symbol": "TST", "price": 120.0, "volume": 2_000_000, "mcap": 5e10, "sector": "Cement",
             "change": 2.0, "yearChange": 30}

    def test_no_history_means_no_candidate(self):
        cfg = w.get_current_config()
        self.assertEqual(w.evaluate_stock_candidate(dict(self.stock), config=cfg, history=[]),
                         (None, "insufficientHistory"))
        with mock.patch.object(w, "_history_provider", None):
            self.assertEqual(w.evaluate_stock_candidate(dict(self.stock), config=cfg), (None, "insufficientHistory"))

    def test_uses_provided_history(self):
        rng = random.Random(1)
        px, bars = 80.0, []
        for i in range(100):
            o = px
            px *= 1 + rng.gauss(0.004, 0.015)
            bars.append({"date": f"d{i}", "open": o, "high": max(o, px) * 1.01, "low": min(o, px) * 0.99,
                         "close": px, "volume": 2e6 * (1 + rng.random())})
        cand, info = w.evaluate_stock_candidate(dict(self.stock, price=round(px, 2)),
                                                config=w.get_current_config(), history=bars)
        self.assertIsInstance(cand, dict, info)


if __name__ == "__main__":
    unittest.main()

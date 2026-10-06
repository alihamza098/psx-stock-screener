#!/usr/bin/env python3
"""Screener "volume" is a 30-day average; only "todayVolume" may drive relative-volume signals."""

import unittest

from shared_trading_utils import today_volume_of


class TestVolumeSemantics(unittest.TestCase):

    def test_today_volume_of(self):
        self.assertIsNone(today_volume_of({"volume": 5e5, "todayVolume": None}))   # live, unknown today
        self.assertEqual(today_volume_of({"volume": 5e5, "todayVolume": 9e5}), 9e5)
        self.assertEqual(today_volume_of({"volume": 7e5}), 7e5)                    # historical bar

    def test_v2_engine_does_not_read_average_as_a_surge(self):
        from scoring_engine import compute_v2_recommendation
        hist = [{"date": f"d{i}", "open": 100, "high": 101, "low": 99, "close": 100 + (i % 3) * 0.1,
                 "volume": 1e5} for i in range(80)]
        base = {"symbol": "X", "price": 100.2, "open": 100, "high": 101, "low": 99, "change": 0.2,
                "avgVolume30d": 1e5}
        unknown = compute_v2_recommendation(stock=dict(base, volume=1e5, todayVolume=None), history=hist)
        normal = compute_v2_recommendation(stock=dict(base, volume=1e5, todayVolume=1e5), history=hist)
        surge = compute_v2_recommendation(stock=dict(base, volume=1e5, todayVolume=1e6), history=hist)
        # unknown today's volume scores like an ordinary day, never like a spike
        self.assertEqual(unknown["confidence"], normal["confidence"])
        self.assertGreater(surge["confidence"], unknown["confidence"])


if __name__ == "__main__":
    unittest.main()

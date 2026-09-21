#!/usr/bin/env python3
"""
Unit Tests for Stage 1: Traded Volume History Feature
Covers compute_volume_stats and compute_projected_volume in server.py
"""

import unittest
from server import compute_volume_stats, compute_projected_volume


class TestVolumeHistory(unittest.TestCase):
    def setUp(self):
        # 25 synthetic bars with known volumes
        # Bars 0-14: 1,000,000 each
        # Bars 15-19: 1,000,000 each (prev 5)
        # Bars 20-24: 2,000,000 each (recent 5, +100% surge -> Rising)
        self.bars = []
        for i in range(25):
            vol = 2000000 if i >= 20 else 1000000
            day_num = i + 1
            self.bars.append({
                "date": f"2026-08-{day_num:02d}",
                "volume": vol,
                "close": 100.0 + i,
                "changePct": 1.5 if i % 2 == 0 else -0.5
            })

    def test_standard_windows_calculation(self):
        # 25 bars: all windows (1D, 2D, 1W, 15D, 1M) should be 'ok'
        today_vol = 3000000
        stats = compute_volume_stats(self.bars, today_volume=today_vol)

        windows = stats["windows"]
        self.assertEqual(windows["1D"]["status"], "ok")
        self.assertEqual(windows["1D"]["total_volume"], 2000000)
        self.assertEqual(windows["1D"]["avg_daily_volume"], 2000000.0)
        # today_ratio: 3M / 2M = 1.5
        self.assertEqual(windows["1D"]["today_ratio"], 1.5)

        self.assertEqual(windows["2D"]["status"], "ok")
        self.assertEqual(windows["2D"]["total_volume"], 4000000)
        self.assertEqual(windows["2D"]["avg_daily_volume"], 2000000.0)

        # 1W = 5 sessions (all 2M)
        self.assertEqual(windows["1W"]["status"], "ok")
        self.assertEqual(windows["1W"]["total_volume"], 10000000)
        self.assertEqual(windows["1W"]["avg_daily_volume"], 2000000.0)
        self.assertEqual(windows["1W"]["today_ratio"], 1.5)

        # 15D = 15 sessions (10 at 1M + 5 at 2M = 20M)
        self.assertEqual(windows["15D"]["status"], "ok")
        self.assertEqual(windows["15D"]["total_volume"], 20000000)
        self.assertAlmostEqual(windows["15D"]["avg_daily_volume"], 20000000 / 15, places=1)

        # 1M = 21 sessions (16 at 1M + 5 at 2M = 26M)
        self.assertEqual(windows["1M"]["status"], "ok")
        self.assertEqual(windows["1M"]["total_volume"], 26000000)
        self.assertAlmostEqual(windows["1M"]["avg_daily_volume"], 26000000 / 21, places=1)

    def test_fewer_bars_than_window(self):
        # Only 3 bars provided
        short_bars = self.bars[:3]
        stats = compute_volume_stats(short_bars, today_volume=1500000)
        windows = stats["windows"]

        # 1D and 2D should be 'ok'
        self.assertEqual(windows["1D"]["status"], "ok")
        self.assertEqual(windows["2D"]["status"], "ok")

        # 1W (5), 15D (15), 1M (21) should be 'n/a'
        self.assertEqual(windows["1W"]["status"], "n/a")
        self.assertIsNone(windows["1W"]["total_volume"])
        self.assertIsNone(windows["1W"]["today_ratio"])

        self.assertEqual(windows["15D"]["status"], "n/a")
        self.assertEqual(windows["1M"]["status"], "n/a")

    def test_empty_bars(self):
        stats = compute_volume_stats([], today_volume=500000)
        self.assertEqual(stats["bars_count"], 0)
        self.assertEqual(stats["trend_label"], "n/a")
        self.assertEqual(stats["recent_bars"], [])
        for w in stats["windows"].values():
            self.assertEqual(w["status"], "n/a")

    def test_zero_volume_days_halted(self):
        # Zero volume day should be marked is_halted and not cause division by zero
        bars_with_halt = [
            {"date": "2026-08-01", "volume": 100000, "close": 50, "changePct": 0.0},
            {"date": "2026-08-02", "volume": 0, "close": 50, "changePct": 0.0},  # halted
            {"date": "2026-08-03", "volume": 200000, "close": 52, "changePct": 4.0},
        ]
        stats = compute_volume_stats(bars_with_halt, today_volume=100000)
        self.assertTrue(stats["recent_bars"][1]["is_halted"])
        self.assertEqual(stats["recent_bars"][1]["volume"], 0)
        self.assertEqual(stats["windows"]["1D"]["lowest_day"]["volume"], 200000)
        self.assertEqual(stats["windows"]["2D"]["lowest_day"]["volume"], 0)

    def test_volume_trend_labels(self):
        # 1. Rising: recent 5 > prev 5 by > 10%
        stats_rising = compute_volume_stats(self.bars, today_volume=1000000)
        self.assertEqual(stats_rising["trend_label"], "Rising")

        # 2. Falling: recent 5 < prev 5 by > 10%
        falling_bars = []
        for i in range(25):
            vol = 500000 if i >= 20 else 2000000
            falling_bars.append({"date": f"2026-07-{i+1:02d}", "volume": vol, "close": 10.0, "changePct": 0.0})
        stats_falling = compute_volume_stats(falling_bars, today_volume=1000000)
        self.assertEqual(stats_falling["trend_label"], "Falling")

        # 3. Flat: within +/-10%
        flat_bars = []
        for i in range(25):
            flat_bars.append({"date": f"2026-06-{i+1:02d}", "volume": 1000000, "close": 10.0, "changePct": 0.0})
        stats_flat = compute_volume_stats(flat_bars, today_volume=1000000)
        self.assertEqual(stats_flat["trend_label"], "Flat")

        # 4. Under 10 bars -> n/a
        stats_short = compute_volume_stats(self.bars[:8], today_volume=1000000)
        self.assertEqual(stats_short["trend_label"], "n/a")

    def test_duplicate_dates_deduplication(self):
        bars_with_dup = [
            {"date": "2026-08-01", "volume": 100000, "close": 50, "changePct": 0.0},
            {"date": "2026-08-01", "volume": 150000, "close": 50, "changePct": 0.0}, # duplicate
            {"date": "2026-08-02", "volume": 200000, "close": 52, "changePct": 4.0},
        ]
        stats = compute_volume_stats(bars_with_dup, today_volume=100000)
        self.assertEqual(stats["bars_count"], 2)

    def test_projected_volume(self):
        # Closed market: returns exact volume as final
        closed_res = compute_projected_volume(500000, {"is_open": False, "reason": "Closed"})
        self.assertEqual(closed_res["projected_volume"], 500000)
        self.assertFalse(closed_res["is_estimate"])

        # None input
        self.assertIsNone(compute_projected_volume(None))


if __name__ == "__main__":
    unittest.main()

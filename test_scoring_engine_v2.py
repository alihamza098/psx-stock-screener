#!/usr/bin/env python3
"""
Unit Tests for Stage 2: Deterministic Scoring Engine v2
Tests unified flow group capping, regime detection, volume tags, and liquidity gating.
"""

import unittest
from scoring_engine import compute_v2_recommendation, evaluate_regime, compute_order_pressure


class TestScoringEngineV2(unittest.TestCase):
    def setUp(self):
        # 30 standard bars for testing
        self.history = []
        for i in range(30):
            self.history.append({
                "date": f"2026-08-{i+1:02d}",
                "open": 100.0 + i,
                "high": 102.0 + i,
                "low": 99.0 + i,
                "close": 101.0 + i,
                "volume": 200000,
                "changePct": 1.0
            })

    def test_flow_group_capping(self):
        # Massive price surge +5.0%, huge volume spike, heavy buying pressure
        # In v1 this would add 15 + 10 + 10 = +35 points!
        # In v2, raw points are +28, but MUST BE CAPPED at +20.0 pts.
        stock = {"price": 135.0, "change": 5.0, "volume": 1000000, "avgVolume": 200000}
        vh = {
            "windows": {
                "1M": {"status": "ok", "avg_daily_volume": 200000},
                "1W": {"status": "ok", "avg_daily_volume": 200000}
            }
        }
        res = compute_v2_recommendation(stock, self.history, volume_history=vh)
        flow_factor = next(f for f in res["factors"] if "Unified Flow" in f["text"])
        self.assertEqual(flow_factor["points"], 20.0) # Strictly capped at +20.0

    def test_regime_classification(self):
        # ADX >= 25.0 and EMA20 > EMA50 by > 0.75% -> Trending Bullish
        reg, trend = evaluate_regime(adx=28.0, ema20=105.0, ema50=100.0)
        self.assertEqual(reg, "Trending")
        self.assertEqual(trend, "Bullish")

        # ADX < 25.0 -> Ranging
        reg_rng, _ = evaluate_regime(adx=18.0, ema20=105.0, ema50=100.0)
        self.assertEqual(reg_rng, "Ranging")

    def test_liquidity_gate(self):
        # Stock with only 5,000 average daily volume (< 25,000 threshold)
        illiquid_stock = {"price": 10.0, "change": 4.5, "volume": 5000, "avgVolume": 5000}
        vh = {
            "windows": {
                "1M": {"status": "ok", "avg_daily_volume": 5000},
                "1W": {"status": "ok", "avg_daily_volume": 5000}
            }
        }
        res = compute_v2_recommendation(illiquid_stock, self.history, volume_history=vh)
        self.assertFalse(res["is_liquid"])
        self.assertEqual(res["recommendation"], "HOLD")
        self.assertIn("signal-illiquid", res["signalClass"])
        self.assertIn("Liquidity Gate", res["factors"][0]["name"])

    def test_volume_tags_weak_participation(self):
        # Price rises +2.0%, but volume is only 50k vs 200k avg (ratio 0.25 < 0.70)
        stock = {"price": 130.0, "change": 2.0, "volume": 50000, "avgVolume": 200000}
        vh = {
            "windows": {
                "1M": {"status": "ok", "avg_daily_volume": 200000},
                "1W": {"status": "ok", "avg_daily_volume": 200000}
            }
        }
        res = compute_v2_recommendation(stock, self.history, volume_history=vh)
        self.assertIn("Weak participation", res["volume_tags"])

    def test_score_clamping(self):
        # Clamped to [5, 95]
        stock_super_bearish = {"price": 50.0, "change": -8.0, "volume": 1000000, "avgVolume": 100000}
        res_bearish = compute_v2_recommendation(stock_super_bearish, self.history)
        self.assertGreaterEqual(res_bearish["confidence"], 5.0)
        self.assertLessEqual(res_bearish["confidence"], 95.0)

    def test_factor_alignment_counter(self):
        stock = {"price": 130.0, "change": 2.0, "volume": 300000, "avgVolume": 200000}
        res = compute_v2_recommendation(stock, self.history)
        self.assertIn("aligned", res["aligned_factors_text"])
        self.assertGreater(res["total_active_factors"], 0)


if __name__ == "__main__":
    unittest.main()

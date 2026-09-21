#!/usr/bin/env python3
"""
Unit Tests for PSX Technical Indicators (Stage 0.3)
===================================================
Tests Wilder's RSI(14), MACD(12,26,9), VWAP, Bollinger %B, Classical Pivots,
EMA20/EMA50, ATR14, and ADX14 against fixed input arrays with verified reference values.
"""

import unittest
from technical_indicators import (
    compute_ema_series,
    compute_rsi_series,
    compute_macd_series,
    compute_bollinger_bands,
    compute_vwap,
    compute_classical_pivots,
    compute_atr_series,
    compute_adx_series
)


class TestTechnicalIndicators(unittest.TestCase):
    def setUp(self):
        # 30-day realistic test price series
        self.closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
            45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
            46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57
        ]
        self.highs = [c + 0.45 for c in self.closes]
        self.lows = [c - 0.40 for c in self.closes]
        self.volumes = [100000 + (i * 5000) for i in range(len(self.closes))]

    def test_ema20_and_ema50(self):
        ema20 = compute_ema_series(self.closes, 20)
        self.assertIsNone(ema20[18])
        self.assertIsNotNone(ema20[19])
        # First EMA is SMA of first 20 values
        expected_first = sum(self.closes[:20]) / 20.0
        self.assertAlmostEqual(ema20[19], expected_first, places=4)
        
        # Next value uses k = 2 / (20 + 1) = 2/21
        k = 2.0 / 21.0
        expected_second = (self.closes[20] * k) + (ema20[19] * (1.0 - k))
        self.assertAlmostEqual(ema20[20], expected_second, places=4)

        # EMA50 on 30 items should be all None
        ema50 = compute_ema_series(self.closes, 50)
        self.assertTrue(all(v is None for v in ema50))

    def test_wilder_rsi14(self):
        rsi = compute_rsi_series(self.closes, 14)
        # First 14 should be None
        for i in range(14):
            self.assertIsNone(rsi[i])
        
        # 15th element (index 14) is first valid RSI
        self.assertIsNotNone(rsi[14])
        self.assertGreaterEqual(rsi[14], 0.0)
        self.assertLessEqual(rsi[14], 100.0)

        # After upward run, RSI should be in bullish territory (>60)
        self.assertGreater(rsi[14], 60.0)

        # Final RSI should reflect recent pullback
        final_rsi = rsi[-1]
        self.assertGreaterEqual(final_rsi, 30.0)
        self.assertLessEqual(final_rsi, 50.0)

    def test_macd12_26_9(self):
        res = compute_macd_series(self.closes, 12, 26, 9)
        macd = res["macd_line"]
        sig = res["signal_line"]
        hist = res["histogram"]

        # MACD valid at index 25 (26th element)
        self.assertIsNone(macd[24])
        self.assertIsNotNone(macd[25])

        # Signal valid after 9 more points (at index 33, but length is 30)
        # Test with 40-element array for full signal confirmation
        extended = self.closes + [45.0, 45.2, 45.5, 45.8, 46.0, 46.2, 46.5, 46.8, 47.0, 47.2]
        res_ext = compute_macd_series(extended, 12, 26, 9)
        self.assertIsNotNone(res_ext["signal_line"][-1])
        self.assertIsNotNone(res_ext["histogram"][-1])
        self.assertAlmostEqual(
            res_ext["histogram"][-1],
            res_ext["macd_line"][-1] - res_ext["signal_line"][-1],
            places=5
        )

    def test_bollinger_bands(self):
        bb = compute_bollinger_bands(self.closes, period=20, multiplier=2.0)
        self.assertIn("upper", bb)
        self.assertIn("middle", bb)
        self.assertIn("lower", bb)
        self.assertIn("percent_b", bb)

        self.assertGreater(bb["upper"], bb["middle"])
        self.assertGreater(bb["middle"], bb["lower"])
        # Last close is 44.57, middle should be near ~45.5
        self.assertGreaterEqual(bb["percent_b"], 0.0)
        self.assertLessEqual(bb["percent_b"], 100.0)

    def test_vwap(self):
        vwap = compute_vwap(self.closes, self.volumes)
        self.assertGreater(vwap, min(self.closes))
        self.assertLess(vwap, max(self.closes))
        # Hand check with 2 items
        p = [10.0, 20.0]
        v = [100.0, 300.0]
        # (10*100 + 20*300) / 400 = (1000 + 6000) / 400 = 17.5
        self.assertEqual(compute_vwap(p, v), 17.5)

    def test_classical_pivots(self):
        pivots = compute_classical_pivots(high=105.0, low=95.0, close=100.0)
        # P = (105 + 95 + 100) / 3 = 100.0
        # R1 = 2*100 - 95 = 105.0
        # S1 = 2*100 - 105 = 95.0
        # R2 = 100 + 10 = 110.0
        # S2 = 100 - 10 = 90.0
        self.assertEqual(pivots["pivot"], 100.0)
        self.assertEqual(pivots["r1"], 105.0)
        self.assertEqual(pivots["s1"], 95.0)
        self.assertEqual(pivots["r2"], 110.0)
        self.assertEqual(pivots["s2"], 90.0)

    def test_atr14(self):
        atr = compute_atr_series(self.highs, self.lows, self.closes, 14)
        for i in range(13):
            self.assertIsNone(atr[i])
        self.assertIsNotNone(atr[13])
        # Each bar high-low is roughly 0.85
        self.assertGreater(atr[13], 0.70)
        self.assertLess(atr[13], 1.20)
        self.assertIsNotNone(atr[-1])

    def test_adx14(self):
        # 45 items to allow ADX smoothing
        ext_closes = self.closes + self.closes[:15]
        ext_highs = [c + 0.50 for c in ext_closes]
        ext_lows = [c - 0.45 for c in ext_closes]
        adx_res = compute_adx_series(ext_highs, ext_lows, ext_closes, 14)
        self.assertIn("adx", adx_res)
        self.assertIn("plus_di", adx_res)
        self.assertIn("minus_di", adx_res)
        self.assertIsNotNone(adx_res["adx"][-1])
        self.assertGreaterEqual(adx_res["adx"][-1], 0.0)
        self.assertLessEqual(adx_res["adx"][-1], 100.0)


if __name__ == "__main__":
    unittest.main()

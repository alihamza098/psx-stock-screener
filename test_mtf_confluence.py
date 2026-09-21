#!/usr/bin/env python3
"""
Unit Tests for Stage 4: Multi-Timeframe Confluence (MTF)
- Direction computation (up / down / neutral) from EMA alignment and MACD state
- Higher timeframe agreement rule (STRONG BUY/SELL only if 1D & 4H agree)
- Counter-trend capping at HOLD (e.g. 15M trigger against 4H bias)
- Graceful handling of missing or partial timeframes
"""

import unittest
from scoring_engine import analyze_timeframe_candles, evaluate_multi_timeframe_confluence


class TestMultiTimeframeConfluence(unittest.TestCase):

    def test_analyze_timeframe_candles_directions(self):
        # 1. Bullish candles series (ascending prices)
        bullish_candles = []
        base = 100.0
        for i in range(60):
            p = base + i * 0.5
            bullish_candles.append({
                "open": p - 0.2,
                "high": p + 0.3,
                "low": p - 0.3,
                "close": p,
                "volume": 10000
            })
        res_up = analyze_timeframe_candles(bullish_candles)
        self.assertEqual(res_up["direction"], "up")
        self.assertEqual(res_up["status"], "ok")
        self.assertGreater(res_up["last_price"], res_up["ema20"])

        # 2. Bearish candles series (descending prices)
        bearish_candles = []
        base = 150.0
        for i in range(60):
            p = base - i * 0.5
            bearish_candles.append({
                "open": p + 0.2,
                "high": p + 0.3,
                "low": p - 0.3,
                "close": p,
                "volume": 10000
            })
        res_down = analyze_timeframe_candles(bearish_candles)
        self.assertEqual(res_down["direction"], "down")
        self.assertEqual(res_down["status"], "ok")
        self.assertLess(res_down["last_price"], res_down["ema20"])

        # 3. Insufficient data (<5 candles)
        res_insufficient = analyze_timeframe_candles([{"close": 100}, {"close": 101}])
        self.assertEqual(res_insufficient["direction"], "n/a")
        self.assertEqual(res_insufficient["status"], "insufficient_data")

    def test_higher_timeframe_agreement_rule(self):
        # Full agreement for STRONG BUY (1D=up, 4H=up)
        tf_full_agree = {
            "1D": {"direction": "up"},
            "4H": {"direction": "up"},
            "1H": {"direction": "up"},
            "15M": {"direction": "up"}
        }
        res1 = evaluate_multi_timeframe_confluence(tf_full_agree, "STRONG BUY")
        self.assertEqual(res1["adjusted_recommendation"], "STRONG BUY")
        self.assertIsNone(res1["downgrade_reason"])

        # 1D is neutral -> Downgraded from STRONG BUY to BUY
        tf_partial = {
            "1D": {"direction": "neutral"},
            "4H": {"direction": "up"},
            "1H": {"direction": "up"},
            "15M": {"direction": "up"}
        }
        res2 = evaluate_multi_timeframe_confluence(tf_partial, "STRONG BUY")
        self.assertEqual(res2["adjusted_recommendation"], "BUY")
        self.assertIsNotNone(res2["downgrade_reason"])

        # Full agreement for STRONG SELL (1D=down, 4H=down)
        tf_sell_agree = {
            "1D": {"direction": "down"},
            "4H": {"direction": "down"},
            "1H": {"direction": "down"},
            "15M": {"direction": "down"}
        }
        res3 = evaluate_multi_timeframe_confluence(tf_sell_agree, "STRONG SELL")
        self.assertEqual(res3["adjusted_recommendation"], "STRONG SELL")

        # 4H is neutral for STRONG SELL -> Downgraded to SELL
        tf_sell_disagree = {
            "1D": {"direction": "down"},
            "4H": {"direction": "neutral"},
            "1H": {"direction": "down"},
            "15M": {"direction": "down"}
        }
        res4 = evaluate_multi_timeframe_confluence(tf_sell_disagree, "STRONG SELL")
        self.assertEqual(res4["adjusted_recommendation"], "SELL")

    def test_counter_trend_capping_at_hold(self):
        # 15M trigger wants to BUY, but 4H bias is DOWN -> Capped at HOLD with Counter-trend tag
        tf_counter_buy = {
            "1D": {"direction": "down"},
            "4H": {"direction": "down"},
            "1H": {"direction": "neutral"},
            "15M": {"direction": "up"}
        }
        res = evaluate_multi_timeframe_confluence(tf_counter_buy, "BUY")
        self.assertEqual(res["adjusted_recommendation"], "HOLD")
        self.assertTrue(res["counter_trend"])
        self.assertEqual(res["counter_trend_tag"], "Counter-trend (4H Bearish Bias)")

        # 15M trigger wants to SELL, but 4H bias is UP -> Capped at HOLD
        tf_counter_sell = {
            "1D": {"direction": "up"},
            "4H": {"direction": "up"},
            "1H": {"direction": "neutral"},
            "15M": {"direction": "down"}
        }
        res_s = evaluate_multi_timeframe_confluence(tf_counter_sell, "SELL")
        self.assertEqual(res_s["adjusted_recommendation"], "HOLD")
        self.assertTrue(res_s["counter_trend"])
        self.assertEqual(res_s["counter_trend_tag"], "Counter-trend (4H Bullish Bias)")

    def test_graceful_missing_timeframes(self):
        # Missing 4H and 1H timeframes
        tf_sparse = {
            "1D": {"direction": "up"}
        }
        res = evaluate_multi_timeframe_confluence(tf_sparse, "BUY")
        self.assertIsNotNone(res)
        self.assertEqual(res["adjusted_recommendation"], "BUY")
        self.assertFalse(res["counter_trend"])


if __name__ == "__main__":
    unittest.main()

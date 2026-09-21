#!/usr/bin/env python3
"""
Unit Tests for Stage 3: Smarter Trade Brackets
- ATR14 dynamic brackets tuned by regime
- PSX circuit limits clamping & near-circuit liquidity risk tag
- Pivot anchoring (pulling target inside R1/R2 and stop inside S1/S2)
- Risk/Reward ratio calculation, downgrade on rr < 1.5, Net R:R
- Position sizing calculation & edge cases
- Volatility level classification (ATR-based)
"""

import unittest
from scoring_engine import compute_trade_brackets, calculate_position_size, compute_v2_recommendation


class TestTradeBracketsV2(unittest.TestCase):

    def test_atr_brackets_by_regime(self):
        # Entry = 100, ATR = 2.0
        # Trending: k_stop = 1.5, k_target = 3.0
        res_trending = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Trending",
            atr=2.0,
            stock={"price": 100.0, "change": 0.0}
        )
        self.assertEqual(res_trending["k_stop"], 1.5)
        self.assertEqual(res_trending["k_target"], 3.0)
        self.assertAlmostEqual(res_trending["stop"], 97.0, places=2)
        self.assertAlmostEqual(res_trending["target"], 106.0, places=2)

        # Range-Bound: k_stop = 1.2, k_target = 2.0
        res_ranging = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Range-Bound",
            atr=2.0,
            stock={"price": 100.0, "change": 0.0}
        )
        self.assertEqual(res_ranging["k_stop"], 1.2)
        self.assertEqual(res_ranging["k_target"], 2.0)
        self.assertAlmostEqual(res_ranging["stop"], 97.6, places=2)
        self.assertAlmostEqual(res_ranging["target"], 104.0, places=2)

    def test_psx_circuit_clamping_and_warning(self):
        # Entry = 100, Previous Close = 100, 7.5% circuit -> Upper = 107.50, Lower = 92.50
        # If ATR is huge (e.g. 5.0), unadjusted target would be 100 + 3*5 = 115.0
        res = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Trending",
            atr=5.0,
            stock={"price": 100.0, "change": 0.0, "ldcp": 100.0}
        )
        # Target must be clamped to upper circuit (107.50)
        self.assertEqual(res["circuit_upper"], 107.50)
        self.assertEqual(res["circuit_lower"], 92.50)
        self.assertLessEqual(res["target"], 107.50)

        # Near circuit check: stock at 106.80 is within 1% of 107.50
        res_near = compute_trade_brackets(
            entry=106.80,
            recommendation="BUY",
            regime="Trending",
            atr=1.0,
            stock={"price": 106.80, "ldcp": 100.0}
        )
        self.assertTrue(res_near["is_near_circuit"])
        self.assertEqual(res_near["circuit_warning"], "Near circuit: exit liquidity risk")

    def test_pivot_anchoring(self):
        # Entry = 100.0, unadjusted target would be 106.0 (100 + 3*2)
        # Pivot R1 = 104.00 lies between entry and target -> target pulled back inside R1 (~103.48)
        pivots = {"r1": 104.00, "r2": 108.00, "s1": 96.00, "s2": 92.00, "pivot": 100.0}
        res = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Trending",
            atr=2.0,
            pivots=pivots,
            stock={"price": 100.0, "ldcp": 100.0}
        )
        self.assertTrue(res["pivot_anchored_target"])
        self.assertLess(res["target"], 104.00)
        self.assertGreater(res["target"], 100.00)

        # Stop anchoring: unadjusted stop = 97.0. If S1 = 98.0, stop pulled back inside S1 (~98.49)
        pivots_stop = {"r1": 108.00, "r2": 112.00, "s1": 98.00, "s2": 95.00, "pivot": 100.0}
        res_stop = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Trending",
            atr=2.0,
            pivots=pivots_stop,
            stock={"price": 100.0, "ldcp": 100.0}
        )
        self.assertTrue(res_stop["pivot_anchored_stop"])
        self.assertGreater(res_stop["stop"], 98.00)
        self.assertLess(res_stop["stop"], 100.00)

    def test_risk_reward_and_signal_downgrade(self):
        # When target is pulled back tightly so reward < 1.5 * risk:
        # e.g., Entry = 100, Target = 102 (R1 pullback), Stop = 97 -> R:R = 2 / 3 = 0.67 < 1.5
        pivots = {"r1": 102.10, "r2": 105.00, "s1": 95.00, "s2": 92.00, "pivot": 99.0}
        res = compute_trade_brackets(
            entry=100.0,
            recommendation="STRONG BUY",
            regime="Trending",
            atr=2.0,
            pivots=pivots,
            stock={"price": 100.0, "ldcp": 100.0}
        )
        self.assertTrue(res["is_poor_rr"])
        self.assertLess(res["gross_rr"], 1.5)
        self.assertEqual(res["adjusted_recommendation"], "BUY")  # Downgraded from STRONG BUY to BUY
        # Verify Net R:R is positive and less than gross R:R
        self.assertLess(res["net_rr"], res["gross_rr"])

    def test_position_sizing_calculations(self):
        # 1,000,000 PKR capital, 1.0% risk -> 10,000 PKR risk
        # Entry = 100, Stop = 95 -> 5 PKR per share risk -> 2,000 shares
        pos = calculate_position_size(capital=1000000.0, risk_pct=1.0, entry=100.0, stop=95.0)
        self.assertEqual(pos["shares"], 2000)
        self.assertEqual(pos["pkr_at_risk"], 10000.0)
        self.assertEqual(pos["total_outlay"], 200000.0)

        # Edge case: stop == entry
        pos_zero_risk = calculate_position_size(capital=1000000.0, risk_pct=1.0, entry=100.0, stop=100.0)
        self.assertEqual(pos_zero_risk["shares"], 0)
        self.assertEqual(pos_zero_risk["pkr_at_risk"], 0.0)

        # Edge case: negative capital or risk
        pos_neg = calculate_position_size(capital=-500.0, risk_pct=1.0, entry=100.0, stop=95.0)
        self.assertEqual(pos_neg["shares"], 0)

    def test_volatility_classification(self):
        # ATR = 4.5% of price -> High Volatility
        res_high_vol = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Trending",
            atr=4.5,
            stock={"price": 100.0, "ldcp": 100.0}
        )
        self.assertEqual(res_high_vol["volatility_level"], "High")

        # ATR = 1.0% of price -> Low Volatility
        res_low_vol = compute_trade_brackets(
            entry=100.0,
            recommendation="BUY",
            regime="Trending",
            atr=1.0,
            stock={"price": 100.0, "ldcp": 100.0}
        )
        self.assertEqual(res_low_vol["volatility_level"], "Low")


if __name__ == "__main__":
    unittest.main()

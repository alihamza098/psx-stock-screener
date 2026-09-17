#!/usr/bin/env python3
"""
Unit Tests for Multibagger Pattern Finder Scoring Engine
=========================================================
Covers all factor components, scaling, clamping, edge cases, and flags.
"""

import unittest
from multibagger.scoring import (
    calculate_volume_zscore,
    calculate_sector_momentum,
    calculate_multibagger_score
)


class TestMultibaggerScoring(unittest.TestCase):

    def test_perfect_score_100(self):
        # 90 days baseline volume of 100k, last 20 days surged to 800k (high z-score)
        vols = [100_000.0] * 70 + [800_000.0] * 20
        res = calculate_multibagger_score(
            price=12.50,
            has_name_or_sector_change=True,
            has_capital_increase=True,
            volumes=vols,
            consecutive_qoq_growth=3,
            sector_return_30d=15.0,
            all_sector_returns_30d=[0.0, 5.0, 10.0, 15.0],
            price_ceiling=20.0,
            free_float_shares=45_000_000
        )
        self.assertEqual(res["score"], 100)
        self.assertEqual(res["breakdown"]["name_change_pts"], 25.0)
        self.assertEqual(res["breakdown"]["capital_increase_pts"], 20.0)
        self.assertEqual(res["breakdown"]["volume_zscore_pts"], 20.0)
        self.assertEqual(res["breakdown"]["turnaround_pts"], 15.0)
        self.assertEqual(res["breakdown"]["price_ceiling_pts"], 10.0)
        self.assertEqual(res["breakdown"]["sector_momentum_pts"], 10.0)
        self.assertIn("low_float", res["flags"])

    def test_zero_score_0(self):
        # Above price ceiling, no triggers, no volume surge, declining revenue, lowest sector
        vols = [100_000.0] * 90  # zero z-score
        res = calculate_multibagger_score(
            price=85.0,  # exceeds 20 PKR ceiling
            has_name_or_sector_change=False,
            has_capital_increase=False,
            volumes=vols,
            consecutive_qoq_growth=0,
            sector_return_30d=-10.0,
            all_sector_returns_30d=[-10.0, 0.0, 10.0],
            price_ceiling=20.0
        )
        self.assertEqual(res["score"], 0)
        self.assertIn("no_turnaround_confirmed_yet", res["flags"])
        self.assertIn("no_capital_increase_yet", res["flags"])

    def test_volume_zscore_capping_at_3_std_dev(self):
        # Extremely huge volume surge: 10M vs 1k
        vols = [1_000.0] * 70 + [10_000_000.0] * 20
        norm_z, raw_z, ratio = calculate_volume_zscore(vols)
        self.assertLessEqual(norm_z, 1.0)
        self.assertGreaterEqual(raw_z, 3.0)
        self.assertGreater(ratio, 10.0)

    def test_empty_and_short_volume_edge_cases(self):
        # Empty volume list
        norm_z, raw_z, ratio = calculate_volume_zscore([])
        self.assertEqual(norm_z, 0.0)
        self.assertEqual(raw_z, 0.0)

        # Fewer than 20 days
        norm_z, raw_z, ratio = calculate_volume_zscore([100.0] * 10)
        self.assertEqual(norm_z, 0.0)

    def test_qoq_turnaround_quarter_scaling(self):
        # 0 quarters -> 0 pts
        res0 = calculate_multibagger_score(price=10.0, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=0)
        self.assertEqual(res0["breakdown"]["turnaround_pts"], 0.0)

        # 1 quarter -> 5 pts
        res1 = calculate_multibagger_score(price=10.0, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=1)
        self.assertEqual(res1["breakdown"]["turnaround_pts"], 5.0)

        # 2 quarters -> 10 pts
        res2 = calculate_multibagger_score(price=10.0, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=2)
        self.assertEqual(res2["breakdown"]["turnaround_pts"], 10.0)

        # 3 quarters -> 15 pts
        res3 = calculate_multibagger_score(price=10.0, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=3)
        self.assertEqual(res3["breakdown"]["turnaround_pts"], 15.0)

        # 5 quarters -> capped at 15 pts
        res5 = calculate_multibagger_score(price=10.0, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=5)
        self.assertEqual(res5["breakdown"]["turnaround_pts"], 15.0)

    def test_price_ceiling_boundary(self):
        # Exactly on 20.0 PKR ceiling
        res_at_20 = calculate_multibagger_score(price=20.0, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=0, price_ceiling=20.0)
        self.assertEqual(res_at_20["breakdown"]["price_ceiling_pts"], 10.0)

        # Just above 20.0 PKR ceiling
        res_above = calculate_multibagger_score(price=20.01, has_name_or_sector_change=False, has_capital_increase=False, volumes=[], consecutive_qoq_growth=0, price_ceiling=20.0)
        self.assertEqual(res_above["breakdown"]["price_ceiling_pts"], 0.0)

    def test_flags_and_reasons_presence(self):
        res = calculate_multibagger_score(
            price=3.50,  # under 5 -> penny_stock_caution
            has_name_or_sector_change=True,
            name_change_details="Zahur Cotton to ITANZ",
            has_capital_increase=False,
            volumes=[10_000.0] * 30,  # thin liquidity
            consecutive_qoq_growth=1,
            free_float_shares=20_000_000  # low float
        )
        self.assertIn("penny_stock_caution", res["flags"])
        self.assertIn("thin_liquidity", res["flags"])
        self.assertIn("low_float", res["flags"])
        self.assertIn("no_capital_increase_yet", res["flags"])
        self.assertTrue(any("Zahur Cotton to ITANZ" in r for r in res["reasons"]))


if __name__ == "__main__":
    unittest.main()

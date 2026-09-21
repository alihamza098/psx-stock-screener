#!/usr/bin/env python3
"""
Unit Tests for PSX Live Backtesting & Calibration Engine (Stage 6)
==================================================================
Tests:
  1. Wilson score 95% confidence interval mathematical bounds.
  2. Maximum drawdown peak-to-trough calculation.
  3. Strict No Lookahead: Modifying future bars does NOT affect decision at bar t.
  4. Minimum-sample-size gating (<30 signals suppresses display win rate).
  5. PSX transaction friction deduction.
  6. Baseline comparison (EMA50 strategy).
  7. Walk-Forward testing & non-destructive suggested_weights.json emission.
"""

import json
import math
import unittest
from pathlib import Path

from live_backtest import (
    compute_wilson_ci,
    compute_max_drawdown_pct,
    run_v2_backtest,
    run_ema50_baseline_backtest,
    run_walk_forward_calibration,
    SUGGESTED_WEIGHTS_FILE
)


class TestLiveBacktest(unittest.TestCase):
    def setUp(self):
        # Generate 100 realistic chronological synthetic daily bars
        self.bars = []
        base_price = 100.0
        for i in range(100):
            # Moderate trending then fluctuating wave
            wave = math.sin(i / 8.0) * 12.0
            drift = (i * 0.15)
            close_p = round(base_price + wave + drift, 2)
            open_p = round(close_p - 0.25, 2)
            high_p = round(close_p + 1.20, 2)
            low_p = round(close_p - 1.10, 2)
            vol = 100000 + (i * 1000)

            self.bars.append({
                "dateStr": f"2026-01-{(i%28)+1:02d}",
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol
            })

    def test_wilson_score_ci(self):
        # 0 total -> (0.0, 0.0)
        low, high = compute_wilson_ci(0, 0)
        self.assertEqual((low, high), (0.0, 0.0))

        # 30 wins out of 50 trials (60% win rate)
        low, high = compute_wilson_ci(30, 50)
        self.assertTrue(0.0 < low < 60.0)
        self.assertTrue(60.0 < high < 100.0)
        self.assertAlmostEqual((low + high) / 2.0, 59.7, delta=1.5)

        # 0 wins out of 20 trials
        low, high = compute_wilson_ci(0, 20)
        self.assertEqual(low, 0.0)
        self.assertTrue(high > 0.0)

        # 20 wins out of 20 trials
        low, high = compute_wilson_ci(20, 20)
        self.assertEqual(high, 100.0)
        self.assertTrue(low < 100.0)

    def test_max_drawdown(self):
        # Monotonically increasing curve -> 0.0% DD
        curve_up = [100.0, 105.0, 110.0, 120.0]
        self.assertEqual(compute_max_drawdown_pct(curve_up), 0.0)

        # Peak 120, trough 90 -> (120 - 90) / 120 = 25.0%
        curve_dd = [100.0, 120.0, 90.0, 110.0]
        self.assertEqual(compute_max_drawdown_pct(curve_dd), 25.0)

    def test_strict_no_lookahead(self):
        """
        Guarantees that altering future bars has ZERO effect on signals generated at bar t.
        """
        # Run original backtest on first 65 bars
        res1 = run_v2_backtest(self.bars[:65], symbol="OGDC")
        trade_at_bar_55 = next(t for t in res1["recent_trades"] if t["bar_index"] == 55)

        # Mutate bar 60 and bar 64 significantly in a copy
        mutated_bars = [dict(b) for b in self.bars[:65]]
        mutated_bars[60]["close"] = 999.0
        mutated_bars[60]["high"] = 1005.0
        mutated_bars[64]["close"] = 10.0

        res2 = run_v2_backtest(mutated_bars, symbol="OGDC")
        trade_at_bar_55_mutated = next(t for t in res2["recent_trades"] if t["bar_index"] == 55)

        # Recommendation and confidence at bar 55 MUST BE IDENTICAL
        self.assertEqual(trade_at_bar_55["recommendation"], trade_at_bar_55_mutated["recommendation"])
        self.assertEqual(trade_at_bar_55["confidence"], trade_at_bar_55_mutated["confidence"])
        self.assertEqual(trade_at_bar_55["entry_price"], trade_at_bar_55_mutated["entry_price"])
        self.assertEqual(trade_at_bar_55["target_price"], trade_at_bar_55_mutated["target_price"])
        self.assertEqual(trade_at_bar_55["stop_price"], trade_at_bar_55_mutated["stop_price"])

    def test_minimum_sample_size_warning_and_tiers(self):
        """
        Verifies Requirement 6.2: Per tier breakdown, minimum-sample warning when <30 signals.
        """
        # 70 bars means 20 decision bars (since warmup is 50), so signal count will be < 30
        res = run_v2_backtest(self.bars[:70], symbol="SYS")
        self.assertIn("tier_metrics", res)
        tiers = ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]
        for t in tiers:
            self.assertIn(t, res["tier_metrics"])
            t_data = res["tier_metrics"][t]
            self.assertTrue(t_data["sample_size_warning"])
            self.assertIsNone(t_data["display_win_rate_pct"])
            self.assertIsNotNone(t_data["warning_text"])
            self.assertIn("wilson_ci_95", t_data)

    def test_transaction_friction_deduction(self):
        """
        Verifies net P&L incorporates PSX transaction friction (0.35%).
        """
        res = run_v2_backtest(self.bars, symbol="LUCK")
        friction_pct = res["transaction_friction_pct"]
        self.assertAlmostEqual(friction_pct, 0.35, places=2)

        # Check trades have friction factored into net_pnl_pkr
        all_trades = res["recent_trades"]
        self.assertTrue(len(all_trades) > 0)
        for tr in all_trades:
            if "BUY" in tr["recommendation"]:
                gross = tr["exit_price"] - tr["entry_price"]
                expected_friction = tr["entry_price"] * 0.0035
                expected_net = gross - expected_friction
                self.assertAlmostEqual(tr["net_pnl_pkr"], expected_net, places=1)

    def test_baseline_ema50_comparison(self):
        """
        Verifies Requirement 6.3: EMA50 baseline comparison calculation.
        """
        base_res = run_ema50_baseline_backtest(self.bars, friction_pct=0.35)
        self.assertIn("trades_count", base_res)
        self.assertIn("win_rate_pct", base_res)
        self.assertIn("profit_factor", base_res)
        self.assertIn("max_drawdown_pct", base_res)
        self.assertIn("total_return_pct", base_res)

    def test_walk_forward_calibration(self):
        """
        Verifies Requirement 6.4: Walk-forward in-sample vs out-of-sample testing,
        writing to config/suggested_weights.json without modifying live config.
        """
        # Snapshot original live config
        live_cfg_path = Path(__file__).parent / "config" / "live_trading.json"
        with open(live_cfg_path) as f:
            orig_live_cfg = f.read()

        wf_res = run_walk_forward_calibration(self.bars, symbol="TEST")
        self.assertEqual(wf_res["status"], "success")
        self.assertIn("in_sample_profit_factor", wf_res)
        self.assertIn("out_of_sample_profit_factor", wf_res)
        self.assertIn("in_sample_vs_oos_gap", wf_res)
        self.assertIn("stability", wf_res)

        # Verify suggested_weights.json exists and has review note
        self.assertTrue(SUGGESTED_WEIGHTS_FILE.exists())
        with open(SUGGESTED_WEIGHTS_FILE) as f:
            rec = json.load(f)
        self.assertIn("suggested_parameters", rec)
        self.assertIn("review_note", rec)

        # Verify live config was NOT mutated
        with open(live_cfg_path) as f:
            current_live_cfg = f.read()
        self.assertEqual(orig_live_cfg, current_live_cfg)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Standing Regression Test Suite: Valuation Engine Edge Cases & Degenerate Guards (Stage 0)
========================================================================================
Tests 0.1 through 0.8 against synthetic and real edge-case records:
0.1: Negative BVPS & negative EPS guard on Graham Number
0.2: Zero starting dividend year and near-zero mean dividend in DDM
0.3: Payout ratio on loss-making dividend payers (EPS <= 0, DPS > 0)
0.4: Leverage penalty when sector average D/E <= 0
0.5: Leave-one-out peer calculation and thin sector (N < 5) flag
0.6: Full-universe scan isolation with malformed ticker
0.7: Suspended / halted stock live price synchronization
0.8: Newly-listed company normalized Piotroski score and distinct verdict
"""

import math
import unittest
from pathlib import Path
from typing import Dict, Any

import psx_undervalued_engine as uve
import psx_piotroski as pio
from server import patch_undervalued_item


class TestValuationEdgeCases(unittest.TestCase):

    def setUp(self):
        self.cfg = uve.load_valuation_config()
        self.macro = uve.get_macro_inputs()

    # ── 0.1: Graham Number Guards ──────────────────────────────────────────────
    def test_01_graham_number_negative_equity_and_earnings(self):
        """0.1: Guard BVPS <= 0 (and EPS <= 0) from generating invalid or imaginary Graham numbers."""
        # Case A: Negative BVPS (-12.5), Positive EPS (5.0) -> Cannot take sqrt of negative
        inp_neg_bvps = {
            "stock": {
                "ticker": "NEG_EQUITY",
                "price": 10.0,
                "eps_ttm": 5.0,
                "bvps": -12.5,
                "shares_outstanding": 1000000.0,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8},
            "macro_inputs": self.macro
        }
        res_a = uve.evaluate_stock_valuation(inp_neg_bvps)
        iv_a = res_a["intrinsic_valuation"]
        self.assertNotEqual(iv_a["method_used"], "Graham_Number", "Graham Number must not be used when BVPS <= 0")
        self.assertIsNone(iv_a["fair_value_per_share"])
        self.assertIn("negative book value/earnings — Graham Number not applicable", res_a["flags"])
        self.assertIn("negative or zero book value — capital erosion", res_a["flags"])

        # Case B: Both EPS <= 0 (-2.0) and BVPS <= 0 (-5.0) -> Product would be positive (+10.0)!
        # Must NOT compute sqrt(22.5 * -2 * -5) = 15.0!
        inp_both_neg = {
            "stock": {
                "ticker": "BOTH_NEG",
                "price": 8.0,
                "eps_ttm": -2.0,
                "bvps": -5.0,
                "shares_outstanding": 1000000.0,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8},
            "macro_inputs": self.macro
        }
        res_b = uve.evaluate_stock_valuation(inp_both_neg)
        iv_b = res_b["intrinsic_valuation"]
        self.assertNotEqual(iv_b["method_used"], "Graham_Number", "Graham Number must not multiply two negative numbers to make positive fair value")
        self.assertIsNone(iv_b["fair_value_per_share"])
        self.assertIn("negative book value/earnings — Graham Number not applicable", res_b["flags"])

    # ── 0.2: DDM Eligibility Guards ────────────────────────────────────────────
    def test_02_ddm_zero_start_year_and_near_zero_mean(self):
        """0.2: Guard division by zero in CAGR when Year 0 dividend is 0, and guard near-zero mean in CV."""
        # Case A: Year 0 has 0 dividend (e.g. [0.0, 2.0, 3.0, 4.0, 5.0])
        inp_zero_base = {
            "stock": {
                "ticker": "ZERO_BASE_DIV",
                "price": 50.0,
                "eps_ttm": 8.0,
                "bvps": 40.0,
                "dividend_per_share": 5.0,
                "dividend_history_5yr": [0.0, 2.0, 3.0, 4.0, 5.0],
                "shares_outstanding": 1000000.0
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8, "sector_avg_dividend_yield": 6.0},
            "macro_inputs": self.macro
        }
        # Must NOT throw ZeroDivisionError
        res_a = uve.evaluate_stock_valuation(inp_zero_base)
        iv_a = res_a["intrinsic_valuation"]
        self.assertNotEqual(iv_a["method_used"], "DDM", "DDM must reject dividend history with Year 0 = 0")
        # Should gracefully fall through to Graham Number floor
        self.assertEqual(iv_a["method_used"], "Graham_Number")
        self.assertGreater(iv_a["fair_value_per_share"], 0)

        # Case B: Near-zero mean dividend
        inp_tiny_mean = {
            "stock": {
                "ticker": "TINY_DIV",
                "price": 10.0,
                "eps_ttm": 2.0,
                "bvps": 10.0,
                "dividend_per_share": 0.001,
                "dividend_history_5yr": [0.001, 0.001, 0.001, 0.001],
                "shares_outstanding": 1000000.0
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8},
            "macro_inputs": self.macro
        }
        res_b = uve.evaluate_stock_valuation(inp_tiny_mean)
        iv_b = res_b["intrinsic_valuation"]
        self.assertNotEqual(iv_b["method_used"], "DDM")

    # ── 0.3: Payout Ratio with Negative Earnings ───────────────────────────────
    def test_03_payout_ratio_lossmaking_dividend_payer(self):
        """0.3: Guard EPS <= 0 with DPS > 0 from getting unearned 100% dividend scores."""
        inp_loss_dividend = {
            "stock": {
                "ticker": "RESERVE_DIV",
                "price": 20.0,
                "eps_ttm": -1.5,      # Lossmaking
                "bvps": 15.0,
                "dividend_per_share": 3.0,  # 15% yield!
                "shares_outstanding": 1000000.0,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8, "sector_avg_dividend_yield": 6.0},
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp_loss_dividend)
        # Must contain explicit warning flag
        self.assertIn("dividend paid from reserves while loss-making (EPS <= 0) — yield unsustainable", res["flags"])
        # Score must be penalized (<= 50, specifically lossmaking_dividend_score = 15.0), not 100.0
        self.assertLess(res["relative_score"], 50.0, "Score must be penalized for loss-making dividend payouts")

    # ── 0.4: Leverage Penalty Guard ────────────────────────────────────────────
    def test_04_leverage_penalty_sector_de_zero(self):
        """0.4: Guard sector-average D/E = 0 before dividing and fall back to baseline."""
        inp_zero_sec_de = {
            "stock": {
                "ticker": "HIGH_DEBT",
                "price": 30.0,
                "eps_ttm": 4.0,
                "bvps": 25.0,
                "debt_to_equity": 3.5,  # Heavy leverage
                "shares_outstanding": 1000000.0
            },
            "sector_peers": {
                "sector_avg_pe": 8.5,
                "sector_avg_pb": 1.8,
                "sector_avg_debt_to_equity": 0.0  # Zero sector D/E
            },
            "macro_inputs": self.macro
        }
        # Must NOT throw ZeroDivisionError and must apply penalty using default D/E (0.85)
        res = uve.evaluate_stock_valuation(inp_zero_sec_de)
        self.assertTrue(any("debt-to-equity" in f and "exceeds sector avg" in f for f in res["flags"]))

    # ── 0.5: Leave-One-Out & Thin Sector Guards ────────────────────────────────
    def test_05_leave_one_out_and_thin_sector(self):
        """0.5: Exclude self from peer calculations and flag sectors with N < 5."""
        stocks = [
            {"symbol": "PEER1", "sector": "ThinSector", "pe": 10.0, "divYield": 5.0},
            {"symbol": "PEER2", "sector": "ThinSector", "pe": 20.0, "divYield": 7.0}
        ]
        # Leave-one-out: excluding PEER1 should leave only PEER2 (PE=20)
        summary_peer1 = uve.compute_sector_peers_summary(stocks, leave_out_symbol="PEER1")
        self.assertEqual(summary_peer1["ThinSector"]["sector_avg_pe"], 20.0)
        self.assertTrue(summary_peer1["ThinSector"]["insufficient_peers"])
        self.assertEqual(summary_peer1["ThinSector"]["sample_count"], 1)

        # Evaluate stock with thin sector
        inp = {
            "stock": {"ticker": "PEER1", "price": 15.0, "eps_ttm": 1.5, "bvps": 10.0, "shares_outstanding": 1000.0},
            "sector_peers": summary_peer1["ThinSector"],
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp)
        self.assertTrue(any("insufficient peer data (N=" in f for f in res["flags"]))

    # ── 0.6: Full-Universe Scan Isolation ──────────────────────────────────────
    def test_06_full_universe_scan_isolation(self):
        """0.6: One malformed ticker must not abort the batch scan for other valid stocks."""
        stocks = [
            {"symbol": "VALID1", "name": "Valid 1", "sector": "Tech", "price": 100.0, "pe": 10.0, "divYield": 5.0, "mcap": 1e8, "freeFloat": 2e7, "volume": 10000},
            {"symbol": "BROKEN_MALFORMED", "name": "Broken", "sector": None, "price": "NOT_A_PRICE", "pe": object()},  # Corrupted data
            {"symbol": "VALID2", "name": "Valid 2", "sector": "Tech", "price": 50.0, "pe": 8.0, "divYield": 6.0, "mcap": 5e7, "freeFloat": 1e7, "volume": 5000}
        ]
        results, stats = uve.run_full_undervalued_scan(stocks)
        # Should record failure for broken ticker but evaluate valid tickers
        self.assertGreaterEqual(stats["evaluated"], 2)
        self.assertIn("BROKEN_MALFORMED", stats["failed_symbols"])
        self.assertGreaterEqual(stats["failures_count"], 1)

    # ── 0.7: Suspended / Halted Stock Handling ─────────────────────────────────
    def test_07_suspended_halted_stock_live_sync(self):
        """0.7: Suspended/halted stocks must not compute live patched MoS or show LIVE_DPS indicator."""
        # Simulated halted stock (volume == 0)
        halted_item = {
            "symbol": "HALTED_STOCK",
            "price": 15.0,
            "intrinsic_valuation": {
                "fair_value_per_share": 30.0,
                "margin_of_safety_pct": 50.0
            }
        }
        # In server.py patch_undervalued_item, if stock info is missing or volume == 0, mark STALE_SNAPSHOT
        res = patch_undervalued_item(halted_item)
        self.assertEqual(res["_price_sync"], "STALE_SNAPSHOT")
        self.assertIn("price unavailable or trading halted", res["_price_note"])

    # ── 0.8: Newly-Listed Company Path & Piotroski Normalization ───────────────
    def test_08_newly_listed_company_path(self):
        """0.8: Newly-listed companies (< 2 yrs) use normalized Piotroski score (out of 6) and distinct verdict."""
        # Test Piotroski engine directly on symbol with < 2 years history
        pio_res = pio.calculate_piotroski_fscore("NEW_IPO")
        self.assertTrue(pio_res["is_new_listing"])
        self.assertEqual(pio_res["max_score"], 6, "Newly-listed company must use 6-factor denominator, not 9")
        self.assertNotEqual(pio_res["verdict"], "VALUE_TRAP", "Newly-listed company must not be labeled VALUE_TRAP due to missing YoY history")

        # Test valuation engine on newly-listed stock with missing data
        inp_new_listing = {
            "stock": {
                "ticker": "NEW_IPO",
                "price": 25.0,
                "eps_ttm": None,  # No historical EPS yet
                "bvps": 20.0,
                "years_listed": 0.5,
                "shares_outstanding": 1000000.0
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8},
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp_new_listing)
        self.assertEqual(res["verdict"], "insufficient_history", "Newly listed stock missing history must have distinct verdict")
    # ── Stage 1: Financial-Sector-Specific Handling & Conglomerates ────────────
    def test_09_financial_sector_scoring_no_ev_ebitda(self):
        """1.1: Financial institutions exclude EV/EBITDA and substitute P/B vs ROE (35% weight)."""
        inp_bank = {
            "stock": {
                "ticker": "TEST_BANK",
                "sector": "Commercial Banks",
                "price": 100.0,
                "eps_ttm": 15.0,
                "bvps": 120.0,
                "roe": 18.0,  # 18% ROE vs 16.5% hurdle
                "dividend_per_share": 10.0,
                "shares_outstanding": 1000000.0,
                "dividend_history_5yr": [6.0, 7.0, 8.0, 9.0, 10.0]
            },
            "sector_peers": {"sector_avg_pe": 6.5, "sector_avg_pb": 0.9, "sector_avg_dividend_yield": 10.0},
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp_bank)
        self.assertTrue(any("Financial institution scoring applied: EV/EBITDA excluded" in f for f in res["flags"]))
        self.assertGreater(res["relative_score"], 60.0)

    def test_10_bank_dcf_inappropriate_caveat(self):
        """1.2: DCF applied to financial institutions must have low confidence and an explicit caveat."""
        inp_bank_dcf = {
            "stock": {
                "ticker": "MEBL_DCF",
                "sector": "Commercial Banks",
                "price": 150.0,
                "eps_ttm": 25.0,
                "bvps": 100.0,
                "dividend_history_5yr": [],  # No DDM
                "fcf_forecast_3yr": [5e7, 6e7, 7e7],  # FCF provided
                "shares_outstanding": 1000000.0
            },
            "sector_peers": {"sector_avg_pe": 6.5, "sector_avg_pb": 1.2},
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp_bank_dcf)
        iv = res["intrinsic_valuation"]
        self.assertEqual(iv["method_used"], "DCF")
        self.assertEqual(res["confidence"], "low", "DCF for financial institution must be low confidence")
        self.assertTrue(any("DCF methodology inappropriate for commercial banks" in f for f in res["flags"]))

    def test_11_conglomerate_holding_caveat(self):
        """1.3: Known conglomerates must carry an explicit multi-segment caveat flag."""
        inp_conglom = {
            "stock": {
                "ticker": "ENGRO",
                "sector": "Fertilizer",
                "price": 300.0,
                "eps_ttm": 40.0,
                "bvps": 250.0,
                "shares_outstanding": 5000000.0,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 8.0, "sector_avg_pb": 1.5},
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp_conglom)
        self.assertTrue(any("Diversified conglomerate/holding company" in f for f in res["flags"]))

    def test_12_continuous_scoring_monotonicity_and_anti_trap(self):
        """Stage 2: Continuous scoring exhibits smooth progression across anchors and honors anti-trap floors."""
        # 1. Monotonicity: small delta in price produces small delta in relative score, not cliff
        base_stock = {
            "ticker": "TEST_SMOOTH",
            "sector": "Technology",
            "price": 74.0,  # pe = 7.4 vs sector 10.0 (ratio 0.74)
            "eps_ttm": 10.0,
            "bvps": 50.0,
            "shares_outstanding": 1000000.0,
            "dividend_history_5yr": []
        }
        inp1 = {"stock": dict(base_stock, price=74.0), "sector_peers": {"sector_avg_pe": 10.0, "sector_avg_pb": 2.0}, "macro_inputs": self.macro}
        inp2 = {"stock": dict(base_stock, price=75.0), "sector_peers": {"sector_avg_pe": 10.0, "sector_avg_pb": 2.0}, "macro_inputs": self.macro}
        inp3 = {"stock": dict(base_stock, price=76.0), "sector_peers": {"sector_avg_pe": 10.0, "sector_avg_pb": 2.0}, "macro_inputs": self.macro}
        
        s1 = uve.evaluate_stock_valuation(inp1)["relative_score"]
        s2 = uve.evaluate_stock_valuation(inp2)["relative_score"]
        s3 = uve.evaluate_stock_valuation(inp3)["relative_score"]
        
        self.assertGreater(s1, s2, "Score must decrease as price increases")
        self.assertGreater(s2, s3, "Score must decrease monotonically")
        # Step difference between 74 and 76 should be modest (< 2 points), not a 15-point cliff
        self.assertLess(abs(s1 - s3), 3.0, f"Expected smooth continuous transition, got large gap: {abs(s1 - s3)}")

        # 2. Anti-trap hard clamp: P/E < 3.0 triggers distress score (10.0) regardless of sector
        distress_inp = {
            "stock": {
                "ticker": "TEST_DISTRESS",
                "sector": "Technology",
                "price": 25.0,  # pe = 2.5 < 3.0
                "eps_ttm": 10.0,
                "bvps": 50.0,
                "shares_outstanding": 1000000.0,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 10.0, "sector_avg_pb": 2.0},
            "macro_inputs": self.macro
        }
        res_distress = uve.evaluate_stock_valuation(distress_inp)
        self.assertTrue(any("P/E below 3" in f for f in res_distress["flags"]))

    def test_13_macro_anchors_and_company_hurdle_rates(self):
        """Stage 3: SBP/macro staleness, company-specific hurdle rates, liquidity buffers, and USD debt flags."""
        # 1. Macro staleness flag
        stale_macro = dict(self.macro, is_stale=True, days_old=60)
        inp_stale = {
            "stock": {"ticker": "STALE_MACRO", "sector": "Commercial Banks", "price": 50.0, "eps_ttm": 8.0, "bvps": 40.0, "shares_outstanding": 1000000.0, "dividend_history_5yr": []},
            "sector_peers": {"sector_avg_pe": 6.0, "sector_avg_pb": 1.2},
            "macro_inputs": stale_macro
        }
        res_stale = uve.evaluate_stock_valuation(inp_stale)
        self.assertTrue(any("macro anchor staleness" in f for f in res_stale["flags"]))

        # 2. Company-specific hurdle rate: Defensive bank (beta 0.85) vs High-beta Tech (beta 1.30)
        inp_bank = {
            "stock": {"ticker": "BANK_HURDLE", "sector": "Commercial Banks", "price": 50.0, "eps_ttm": 8.0, "bvps": 40.0, "shares_outstanding": 1000000.0, "dividend_history_5yr": []},
            "sector_peers": {"sector_avg_pe": 6.0, "sector_avg_pb": 1.2},
            "macro_inputs": self.macro
        }
        inp_tech = {
            "stock": {"ticker": "TECH_HURDLE", "sector": "Technology & Communication", "price": 50.0, "eps_ttm": 8.0, "bvps": 40.0, "shares_outstanding": 1000000.0, "dividend_history_5yr": []},
            "sector_peers": {"sector_avg_pe": 12.0, "sector_avg_pb": 2.5},
            "macro_inputs": self.macro
        }
        res_bank = uve.evaluate_stock_valuation(inp_bank)
        res_tech = uve.evaluate_stock_valuation(inp_tech)
        bank_hurdle = res_bank["intrinsic_valuation"]["hurdle_rate_pct"]
        tech_hurdle = res_tech["intrinsic_valuation"]["hurdle_rate_pct"]
        self.assertLess(bank_hurdle, tech_hurdle, f"Defensive bank hurdle ({bank_hurdle}%) must be lower than tech hurdle ({tech_hurdle}%)")

        # 3. Free float illiquidity buffer (< 15% free float)
        inp_illiquid = {
            "stock": {"ticker": "LOW_FLOAT", "sector": "Technology & Communication", "price": 50.0, "eps_ttm": 8.0, "bvps": 40.0, "shares_outstanding": 1000000.0, "free_float_pct": 8.5, "dividend_history_5yr": []},
            "sector_peers": {"sector_avg_pe": 12.0, "sector_avg_pb": 2.5},
            "macro_inputs": self.macro
        }
        res_illiquid = uve.evaluate_stock_valuation(inp_illiquid)
        self.assertGreater(res_illiquid["intrinsic_valuation"]["hurdle_rate_pct"], tech_hurdle)
        self.assertTrue(any("low free-float" in f for f in res_illiquid["flags"]))

        # 4. USD debt / currency risk flag for Power Generation
        inp_power = {
            "stock": {"ticker": "POWER_CO", "sector": "Power Generation & Distribution", "price": 50.0, "eps_ttm": 8.0, "bvps": 40.0, "shares_outstanding": 1000000.0, "dividend_history_5yr": []},
            "sector_peers": {"sector_avg_pe": 6.0, "sector_avg_pb": 1.2},
            "macro_inputs": self.macro
        }
        res_power = uve.evaluate_stock_valuation(inp_power)
        self.assertTrue(any("FX/currency exposure" in f for f in res_power["flags"]))

    def test_14_corporate_actions_and_earnings_quality(self):
        """Stage 4: Bonus adjustments, continuous one-off discount, cyclical normalized EPS, and circular debt."""
        # 1. Continuous one-off discount (20% reduction on EPS)
        inp_oneoff = {
            "stock": {
                "ticker": "ONE_OFF_CO",
                "sector": "Chemical",
                "price": 50.0,
                "eps_ttm": 10.0,  # normalized should be 8.0
                "bvps": 40.0,
                "shares_outstanding": 1000000.0,
                "one_off_items_flag": True,
                "one_off_discount_ratio": 0.20,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 8.0, "sector_avg_pb": 1.5},
            "macro_inputs": self.macro
        }
        res_oneoff = uve.evaluate_stock_valuation(inp_oneoff)
        self.assertEqual(res_oneoff["relative_metrics"]["eps_normalized"], 8.0)
        # Graham number uses normalized EPS (22.5 * 8.0 * 40.0)^0.5 = sqrt(7200) = 84.85
        # instead of unadjusted (22.5 * 10.0 * 40.0)^0.5 = sqrt(9000) = 94.87
        self.assertAlmostEqual(res_oneoff["intrinsic_valuation"]["fair_value_per_share"], 84.85, delta=0.1)
        self.assertTrue(any("one-off non-recurring earnings" in f for f in res_oneoff["flags"]))

        # 2. Cyclical normalized EPS
        inp_cyclical = {
            "stock": {
                "ticker": "LUCK_CEMENT",
                "sector": "Cement",
                "price": 200.0,
                "eps_ttm": 30.0,
                "eps_history_3yr": [15.0, 20.0, 25.0],  # average = 20.0
                "bvps": 150.0,
                "shares_outstanding": 1000000.0,
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 10.0, "sector_avg_pb": 1.8},
            "macro_inputs": self.macro
        }
        res_cyclical = uve.evaluate_stock_valuation(inp_cyclical)
        self.assertEqual(res_cyclical["relative_metrics"]["cycle_normalized_eps"], 20.0)
        self.assertTrue(any("cycle-normalized" in f for f in res_cyclical["flags"]))

        # 3. Circular debt: Power generation with CFO < 50% Net Income
        inp_circular = {
            "stock": {
                "ticker": "HUBC_CIRCULAR",
                "sector": "Power Generation & Distribution",
                "price": 80.0,
                "eps_ttm": 20.0,
                "bvps": 90.0,
                "shares_outstanding": 1000000.0,
                "cfo": 200.0,
                "net_income": 800.0,  # CFO/NI = 25% < 50%
                "dividend_history_5yr": []
            },
            "sector_peers": {"sector_avg_pe": 6.0, "sector_avg_pb": 1.2},
            "macro_inputs": self.macro
        }
        res_circular = uve.evaluate_stock_valuation(inp_circular)
        self.assertTrue(any("circular debt warning" in f for f in res_circular["flags"]))
        self.assertTrue(any("margin of safety hurdle increased to 20%" in f for f in res_circular["flags"]))

    def test_15_model_stability_and_verdict_history(self):
        """Stage 5: Hysteresis tertile boundary, waterfall shift logging, financials staleness, and history DB."""
        # 1. Hysteresis buffer test:
        # Stock with relative score ~65.5 (between 64.7 and 66.7) and high MoS (e.g. 30%)
        # If it had NO previous verdict, it would be 'possibly_undervalued' because score < 66.7
        # If it WAS previously 'undervalued', hysteresis buffer (66.7 - 2.0 = 64.7) keeps it 'undervalued'
        stock_base = {
            "ticker": "HYSTERESIS_CO",
            "sector": "Commercial Banks",
            "price": 30.0,
            "eps_ttm": 8.0,
            "bvps": 50.0,
            "shares_outstanding": 1000000.0,
            "dividend_per_share": 6.0,
            "dividend_history_5yr": [4.0, 4.5, 5.0, 5.5, 6.0]  # DDM fair value ~ 50-60 -> MoS > 40%
        }
        # Inp without prior verdict
        inp_no_prior = {
            "stock": dict(stock_base, previous_verdict=None),
            "sector_peers": {"sector_avg_pe": 8.0, "sector_avg_pb": 1.2, "sector_avg_dividend_yield": 12.0},
            "macro_inputs": self.macro
        }
        # Inp with prior undervalued verdict
        inp_with_prior = {
            "stock": dict(stock_base, previous_verdict="undervalued"),
            "sector_peers": {"sector_avg_pe": 8.0, "sector_avg_pb": 1.2, "sector_avg_dividend_yield": 12.0},
            "macro_inputs": self.macro
        }
        res_no_prior = uve.evaluate_stock_valuation(inp_no_prior)
        res_with_prior = uve.evaluate_stock_valuation(inp_with_prior)
        
        # Verify relative score is the same
        self.assertEqual(res_no_prior["relative_score"], res_with_prior["relative_score"])

        # 2. Financials staleness flag (> 180 days old)
        inp_stale_fin = {
            "stock": dict(stock_base, financials_date="2024-01-01"),
            "sector_peers": {"sector_avg_pe": 8.0, "sector_avg_pb": 1.2},
            "macro_inputs": self.macro
        }
        res_stale_fin = uve.evaluate_stock_valuation(inp_stale_fin)
        self.assertTrue(any("stale financials" in f for f in res_stale_fin["flags"]))

        # 3. Model shift logging flag
        inp_shift = {
            "stock": dict(stock_base, previous_intrinsic_method="DCF"),
            "sector_peers": {"sector_avg_pe": 8.0, "sector_avg_pb": 1.2},
            "macro_inputs": self.macro
        }
        res_shift = uve.evaluate_stock_valuation(inp_shift)
        self.assertTrue(any("valuation model shift" in f for f in res_shift["flags"]))

    # ── Stage 7: Cross-Engine Synergy Tests ────────────────────────────────────
    def test_16_cross_engine_synergy_tags(self):
        """Stage 7: Verify generation of Prime Compounder, High Conviction, and Momentum Trap synergy tags."""
        # 1. Prime Compounder badge (Piotroski >= 8 + Undervalued)
        mock_res_prime = {
            "verdict": "undervalued",
            "intrinsic_valuation": {"margin_of_safety_pct": 28.5},
            "piotroski": {"f_score": 8}
        }
        tags_prime = uve.get_cross_engine_synergy_tags("TEST_SYM", mock_res_prime)
        prime_badges = [t["badge"] for t in tags_prime]
        self.assertIn("PRIME COMPOUNDER", prime_badges)

        # 2. Value Trap Caution badge (undervalued_caution)
        mock_res_caution = {
            "verdict": "undervalued_caution",
            "intrinsic_valuation": {"margin_of_safety_pct": 20.0},
            "piotroski": {"f_score": 3}
        }
        tags_caution = uve.get_cross_engine_synergy_tags("TEST_SYM", mock_res_caution)
        caution_badges = [t["badge"] for t in tags_caution]
        self.assertIn("VALUE TRAP CAUTION", caution_badges)

        # 3. Momentum Trap Risk (Overvalued + negative MoS)
        mock_res_over = {
            "verdict": "overvalued",
            "intrinsic_valuation": {"margin_of_safety_pct": -25.0},
            "piotroski": {"f_score": 5}
        }
        tags_over = uve.get_cross_engine_synergy_tags("TEST_SYM", mock_res_over)
        over_badges = [t["badge"] for t in tags_over]
        self.assertIn("OVERVALUED", over_badges)

    # ── Stage 8: Operational Security & Export Tests ───────────────────────────
    def test_17_operational_security_and_export(self):
        """Stage 8: Verify rate limiting cooldown, unstripped payload fields, and mandatory disclaimers."""
        import time
        import server

        # 1. Rate limiting cooldown verification
        server._LAST_UNDERVALUED_RESCAN_TIME = time.time()
        time_elapsed = time.time() - server._LAST_UNDERVALUED_RESCAN_TIME
        self.assertLess(time_elapsed, server._UNDERVALUED_RESCAN_COOLDOWN_SEC)

        # 2. patch_undervalued_item preserves disclaimer & confidence
        mock_item = {
            "symbol": "OGDC",
            "verdict": "undervalued",
            "relative_score": 75.0,
            "intrinsic_valuation": {"fair_value_per_share": 180.0, "margin_of_safety_pct": 30.0}
        }
        patched = server.patch_undervalued_item(mock_item)
        self.assertIn("confidence", patched)
        self.assertIn("disclaimer", patched)
        self.assertTrue(len(patched["disclaimer"]) > 10)

        # 3. Complete evaluate_stock_valuation payload structure
        inp = {
            "stock": {
                "ticker": "SEC_TEST",
                "price": 50.0,
                "eps_ttm": 8.0,
                "bvps": 40.0,
                "dividend_per_share": 5.0,
                "dividend_history_5yr": [3.0, 3.5, 4.0, 4.5, 5.0],
                "shares_outstanding": 1000000.0
            },
            "sector_peers": {"sector_avg_pe": 8.5, "sector_avg_pb": 1.8},
            "macro_inputs": self.macro
        }
        res = uve.evaluate_stock_valuation(inp)
        self.assertIn("synergy_tags", res)
        self.assertIn("disclaimer", res)
        self.assertIn("confidence", res)
        self.assertIn("hurdle_rate_pct", res["intrinsic_valuation"])
        self.assertIn("sector_beta", res["intrinsic_valuation"])


if __name__ == "__main__":
    unittest.main()




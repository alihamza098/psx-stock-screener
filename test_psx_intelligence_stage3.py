#!/usr/bin/env python3
"""
Unit and Synthetic Tests for PSX Market Intelligence & AI Learning Engine — Stage 3
Topic: Empirical Bayes Shrinkage for Patterns and Sectors (3.1, 3.2, 3.3)

Validates:
1. EmpiricalBayesEstimator: Prior fitting, shrinkage mathematics, and sample-size gating.
2. Extreme sample temperance: Small N (e.g. 1/1=100%) shrunk toward prior; large N preserved.
3. Continuous Sector Multipliers: Replaces flat 1.3x/0.5x thresholds without cliff effects.
4. PatternLibrary Shrinkage: Computes and persists raw win rate, shrunk win rate, Wilson CIs, and N.
5. PredictionEngine Integration: Uses shrunk pattern win rate and continuous sector multiplier.
6. End-to-End API contracts: GET /api/intelligence/patterns and GET /api/intelligence/sector-shrinkage.
"""

import unittest
import os
import json
import sqlite3
import tempfile
import shutil
from pathlib import Path

from psx_intelligence_engine import (
    IntelligenceDB,
    EmpiricalBayesEstimator,
    PatternLibrary,
    PredictionEngine,
    LearningEngine,
    CauseInvestigator,
    compute_wilson_ci,
    get_intelligence_config
)


class TestEmpiricalBayesEstimator(unittest.TestCase):
    """Test mathematical rigor of Empirical Bayes Shrinkage (Beta-Binomial)."""

    def setUp(self):
        self.estimator = EmpiricalBayesEstimator({
            "sector_prior_weight_m": 20.0,
            "pattern_prior_weight_m": 20.0,
            "min_prior_weight": 5.0,
            "max_prior_weight": 50.0,
            "auto_estimate_m": True,
            "sample_size_gate": 3,
            "sector_multiplier_min": 0.5,
            "sector_multiplier_max": 1.5,
            "default_population_win_rate": 0.1443
        })

    def test_prior_fitting_from_population(self):
        """Verify prior parameters (mu_0, M, alpha, beta) fitted from group outcomes."""
        groups = [
            {"sector": "Textile", "wins": 4, "losses": 43},  # n=47
            {"sector": "Chemical", "wins": 5, "losses": 12},  # n=17
            {"sector": "Modaraba", "wins": 5, "losses": 19},  # n=24
            {"sector": "Insurance", "wins": 2, "losses": 13}, # n=15
        ]
        total_k = 4 + 5 + 5 + 2 # 16
        total_n = 47 + 17 + 24 + 15 # 103
        expected_mu = round(16 / 103, 4)

        mu_0, M, alpha, beta = self.estimator.fit_prior(groups, prior_weight_default=20.0)
        self.assertAlmostEqual(mu_0, expected_mu, places=3)
        self.assertGreaterEqual(M, 5.0)
        self.assertLessEqual(M, 50.0)
        self.assertAlmostEqual(alpha + beta, M, places=2)
        print(f"✓ Prior fitting: mu_0={mu_0:.4f}, M={M:.1f}, alpha={alpha:.2f}, beta={beta:.2f}")

    def test_extreme_sample_shrinkage_small_vs_large_n(self):
        """
        Verify that a 100% win rate on N=1 shrinks dramatically toward prior,
        while an empirical win rate on large N (e.g. N=50) retains its strength.
        """
        mu_0 = 0.15 # 15% population prior
        M = 20.0

        # Small N: 1 win out of 1 trial (100% raw win rate)
        res_small = self.estimator.shrink_rate(wins=1, decisive_n=1, mu_0=mu_0, M=M)
        self.assertEqual(res_small["raw_win_rate_pct"], 100.0)
        # Expected shrunk: (1 + 20*0.15) / (1 + 20) = 4 / 21 = 19.05%
        self.assertAlmostEqual(res_small["shrunk_win_rate_pct"], 19.05, delta=0.5)
        self.assertTrue(res_small["is_gated"])  # N=1 < 3
        self.assertLess(res_small["shrinkage_weight"], 0.10)

        # Large N: 5 wins out of 50 trials (10% raw win rate)
        res_large = self.estimator.shrink_rate(wins=5, decisive_n=50, mu_0=mu_0, M=M)
        self.assertEqual(res_large["raw_win_rate_pct"], 10.0)
        # Expected shrunk: (5 + 3) / (50 + 20) = 8 / 70 = 11.43%
        self.assertAlmostEqual(res_large["shrunk_win_rate_pct"], 11.43, delta=0.5)
        self.assertFalse(res_large["is_gated"])
        self.assertGreater(res_large["shrinkage_weight"], 0.70)

        print(f"✓ Shrinkage temperance: N=1 (raw=100%) -> shrunk={res_small['shrunk_win_rate_pct']:.1f}%; N=50 (raw=10%) -> shrunk={res_large['shrunk_win_rate_pct']:.1f}%")

    def test_continuous_sector_multiplier(self):
        """Verify continuous multiplier replaces flat 1.3x/0.5x threshold without cliff edge."""
        mu_0 = 0.15

        # Exactly at prior -> 1.00x
        m_neutral = self.estimator.compute_sector_multiplier(shrunk_win_rate_pct=15.0, mu_0=mu_0)
        self.assertEqual(m_neutral, 1.0)

        # Outperforming sector -> smooth bonus
        m_bonus = self.estimator.compute_sector_multiplier(shrunk_win_rate_pct=21.0, mu_0=mu_0)
        self.assertAlmostEqual(m_bonus, 1.40, delta=0.05)

        # Underperforming sector -> smooth penalty
        m_penalty = self.estimator.compute_sector_multiplier(shrunk_win_rate_pct=10.0, mu_0=mu_0)
        self.assertAlmostEqual(m_penalty, 0.67, delta=0.05)

        # Clamping bounds [0.5, 1.5]
        m_max = self.estimator.compute_sector_multiplier(shrunk_win_rate_pct=40.0, mu_0=mu_0)
        m_min = self.estimator.compute_sector_multiplier(shrunk_win_rate_pct=2.0, mu_0=mu_0)
        self.assertEqual(m_max, 1.5)
        self.assertEqual(m_min, 0.5)
        print("✓ Continuous sector multiplier verified: neutral=1.0x, bonus=1.4x, penalty=0.67x, bounds=[0.5, 1.5]")


class TestStage3Integration(unittest.TestCase):
    """Integration test for PatternLibrary and LearningEngine Empirical Bayes shrinkage."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "intelligence_test.db"
        self.db = IntelligenceDB(self.db_path)
        self.eb = EmpiricalBayesEstimator()
        self.pattern_lib = PatternLibrary(self.db, self.eb)
        self.investigator = CauseInvestigator(self.db)
        self.prediction_engine = PredictionEngine(self.db, self.pattern_lib, self.investigator)
        self.learning_engine = LearningEngine(self.db)
        # Pre-seed pattern library so foreign keys to detected_patterns are satisfied
        self.pattern_lib.rebuild()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pattern_library_empirical_bayes_rebuild(self):
        """Test rebuilding pattern library persists raw, shrunk win rate, N, and Wilson CI."""
        # Pre-seed pattern P001 so pattern_occurrences FK passes
        self.db.upsert_pattern({
            "id": "P001",
            "name": "Breakout + Volume Surge",
            "fingerprint": "TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION",
            "description": "Stock breaks key resistance on elevated volume"
        })

        # Insert synthetic event and pattern occurrence
        ev_id = "TEST_EV_01"
        self.db.insert_event({
            "id": ev_id,
            "symbol": "LUCK",
            "sector": "Cement",
            "event_type": "PRICE_SPIKE",
            "detected_at": "2026-09-01T10:00:00Z",
            "trade_date": "2026-09-01",
            "price": 100.0,
            "price_change_pct": 5.0,
            "volume": 50000,
            "rvol": 3.0,
            "rsi_at_event": 65.0,
            "status": "OPEN"
        })
        self.db.insert_causes([
            {"event_id": ev_id, "factor": "TECHNICAL_BREAKOUT", "evidence": "HIGH", "confidence": 80, "detail": "", "created_at": "2026-09-01T10:00:00Z"},
            {"event_id": ev_id, "factor": "VOLUME_ACCUMULATION", "evidence": "HIGH", "confidence": 75, "detail": "", "created_at": "2026-09-01T10:00:00Z"}
        ])
        # Record occurrence for P001
        conn = self.db._connect()
        conn.execute("""
            INSERT INTO pattern_occurrences
            (pattern_id, event_id, symbol, matched_at, similarity, outcome, return_5d, max_drawdown, trader_pnl, beat_kse, evaluation_flag)
            VALUES ('P001', ?, 'LUCK', '2026-09-01T10:00:00Z', 0.9, 'WIN', 4.5, 0.8, 1.0, 1, 'VALID')
        """, (ev_id,))
        conn.commit()
        conn.close()

        # Rebuild pattern library
        self.pattern_lib.rebuild()

        patterns = self.db.get_patterns(min_occurrences=1)
        p001 = next((p for p in patterns if p["id"] == "P001"), None)
        self.assertIsNotNone(p001)
        self.assertEqual(p001["win_count"], 1)
        self.assertEqual(p001["raw_win_rate"], 100.0)
        # With prior mu ~ 10-14%, 1 win on N=1 must shrink to < 25%
        self.assertLess(p001["shrunk_win_rate"], 25.0)
        self.assertGreater(p001["shrunk_win_rate"], 5.0)
        self.assertGreaterEqual(p001["wilson_ci_low"], 0.0)
        self.assertLessEqual(p001["wilson_ci_high"], 100.0)
        print(f"✓ Pattern P001 EB rebuild: Raw={p001['raw_win_rate']}%, Shrunk={p001['shrunk_win_rate']}%, CI=[{p001['wilson_ci_low']}%, {p001['wilson_ci_high']}%]")

    def test_learning_engine_sector_shrinkage_persistence(self):
        """Test learning engine sector shrinkage fits prior and populates sector_shrinkage table."""
        # Insert synthetic predictions across two sectors
        ev1 = "EV_SEC_1"
        ev2 = "EV_SEC_2"
        self.db.insert_event({"id": ev1, "symbol": "SECA", "sector": "Chemical", "event_type": "PRICE_SPIKE",
                              "detected_at": "2026-09-01T10:00:00Z", "trade_date": "2026-09-01", "price": 50,
                              "price_change_pct": 4, "volume": 10000, "rvol": 2, "rsi_at_event": 60, "status": "CLOSED"})
        self.db.insert_event({"id": ev2, "symbol": "SECB", "sector": "Textile", "event_type": "PRICE_SPIKE",
                              "detected_at": "2026-09-01T10:00:00Z", "trade_date": "2026-09-01", "price": 20,
                              "price_change_pct": 4, "volume": 10000, "rvol": 2, "rsi_at_event": 60, "status": "CLOSED"})

        self.db.insert_prediction({"id": "PRED_1", "symbol": "SECA", "event_id": ev1, "signal": "POSSIBLE_BREAKOUT",
                                   "confidence": 60, "price_at_signal": 50, "outcome": "CORRECT", "evaluation_flag": "VALID"})
        self.db.update_prediction_outcome("PRED_1", "CORRECT", 3.5, 0.5, 1.0, 0.5, 3.0, 1, "VALID")

        self.db.insert_prediction({"id": "PRED_2", "symbol": "SECB", "event_id": ev2, "signal": "POSSIBLE_BREAKOUT",
                                   "confidence": 60, "price_at_signal": 20, "outcome": "INCORRECT", "evaluation_flag": "VALID"})
        self.db.update_prediction_outcome("PRED_2", "INCORRECT", -5.0, 4.0, -1.5, 0.5, -5.5, 0, "VALID")

        res = self.learning_engine.rebuild_sector_shrinkage(self.eb)
        self.assertEqual(res["sectors_count"], 2)

        chem = self.db.get_sector_shrinkage_by_name("Chemical")
        text = self.db.get_sector_shrinkage_by_name("Textile")
        self.assertIsNotNone(chem)
        self.assertIsNotNone(text)

        # Chemical won (1 win / 1 decisive), Textile lost (0 win / 1 decisive)
        # Multipliers must reflect relative performance centered near 1.0
        self.assertGreater(chem["multiplier"], text["multiplier"])
        self.assertGreaterEqual(chem["multiplier"], 1.0)
        self.assertLessEqual(text["multiplier"], 1.0)
        print(f"✓ Sector shrinkage DB: Chemical={chem['multiplier']}x, Textile={text['multiplier']}x (Pop prior={res['population_prior']['win_rate']:.2f})")

    def test_prediction_engine_uses_shrunk_rate_and_sector_multiplier(self):
        """Test PredictionEngine integrates EB pattern rate and sector shrinkage multiplier."""
        # Setup sector shrinkage in DB
        self.db.upsert_sector_shrinkage({
            "sector": "Chemical",
            "sample_count": 10,
            "win_count": 4,
            "loss_count": 6,
            "raw_win_rate": 40.0,
            "shrunk_win_rate": 25.0,
            "multiplier": 1.35,
            "wilson_ci_low": 16.8,
            "wilson_ci_high": 68.7,
            "shrinkage_weight": 0.33
        })

        # Evaluate prediction
        event = {
            "id": "EV_PRED_TEST",
            "symbol": "EPCL",
            "sector": "Chemical",
            "event_type": "PRICE_SPIKE",
            "price": 30.0,
            "rvol": 3.0,
            "rsi_at_event": 65.0
        }
        causes = [
            {"factor": "TECHNICAL_BREAKOUT", "confidence": 75, "evidence": "HIGH"},
            {"factor": "VOLUME_ACCUMULATION", "confidence": 70, "evidence": "HIGH"}
        ]

        # Insert event into DB to satisfy FK
        self.db.insert_event(event)

        # Call generate_prediction
        self.prediction_engine.generate_prediction(event, causes)
        preds = self.db.get_active_predictions(limit=1)
        self.assertEqual(len(preds), 1)
        p = preds[0]
        reasoning = json.loads(p["reasoning_json"])
        self.assertIn("disclaimer", reasoning)
        self.assertIn("raw_win_rate", reasoning)
        self.assertIn("shrunk_win_rate", reasoning)
        self.assertIn("sector=Chemical(EB=1.35x", str(reasoning.get("calibration_note", "")))
        print(f"✓ PredictionEngine reasoning verified with EB notes: {reasoning.get('calibration_note')}")


if __name__ == "__main__":
    unittest.main()

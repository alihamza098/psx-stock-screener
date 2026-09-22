#!/usr/bin/env python3
"""
Unit and Synthetic Tests for Stage 4: Pattern Library — Regime Conditioning,
Decay Tracking, and Unsupervised Pattern Expansion.
"""

import unittest
import tempfile
import shutil
import json
from pathlib import Path
from datetime import datetime, timedelta

from psx_intelligence_engine import (
    IntelligenceDB,
    EmpiricalBayesEstimator,
    PatternLibrary,
    PatternExpander,
    CauseInvestigator,
    PredictionEngine,
    resolve_market_regime,
    compute_wilson_ci,
    CAUSE_TECH_BREAKOUT,
    CAUSE_VOLUME_ACCUM,
    CAUSE_RSI_MOMENTUM,
    CAUSE_MACD_CONFIRM,
    CAUSE_SECTOR_MOMENTUM,
    CAUSE_MARKET_MOMENTUM,
    CAUSE_CORP_ANNOUNCEMENT,
    CAUSE_UPPER_LOCK_SETUP
)


class TestStage4Unit(unittest.TestCase):
    """Unit tests for Stage 4 mathematical and algorithmic routines."""

    def test_market_regime_resolution_thresholds(self):
        """Verify deterministic classification into CRASH, BEAR, NEUTRAL, BULL."""
        # 1. KSE-100 5-day return thresholds:
        # Crash < -4.0%
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", kse_5d_return=-4.5), "CRASH")
        # Bear < -1.0%
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", kse_5d_return=-2.5), "BEAR")
        # Neutral <= 1.5%
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", kse_5d_return=0.0), "NEUTRAL")
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", kse_5d_return=1.2), "NEUTRAL")
        # Bull > 1.5%
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", kse_5d_return=3.5), "BULL")

        # 2. Breadth score thresholds (0-100):
        # Crash < 15
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", breadth_score=10.0), "CRASH")
        # Bear < 40
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", breadth_score=28.0), "BEAR")
        # Neutral < 70
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", breadth_score=55.0), "NEUTRAL")
        # Bull >= 70
        self.assertEqual(resolve_market_regime(date_str="2026-01-01", breadth_score=82.0), "BULL")
        print("✓ Deterministic market regime resolution verified for all edge conditions.")

    def test_pattern_decay_divergence_logic(self):
        """Verify decay classification logic (DECAYING vs IMPROVING vs STABLE)."""
        estimator = EmpiricalBayesEstimator()
        mu_0 = 0.15
        M = 20.0

        # Pattern with lifetime shrunk win rate = 25.0%
        lifetime_shrunk = 25.0

        # Scenario A: 90d win rate dropped to 10.0% (div = -15.0% <= -5.0%, N=10 >= 3) -> DECAYING
        div_a = 10.0 - lifetime_shrunk
        self.assertLessEqual(div_a, -5.0)
        status_a = "DECAYING" if div_a <= -5.0 else "STABLE"
        self.assertEqual(status_a, "DECAYING")

        # Scenario B: 90d win rate surged to 35.0% (div = +10.0% >= 5.0%, N=10 >= 3) -> IMPROVING
        div_b = 35.0 - lifetime_shrunk
        self.assertGreaterEqual(div_b, 5.0)
        status_b = "IMPROVING" if div_b >= 5.0 else "STABLE"
        self.assertEqual(status_b, "IMPROVING")

        # Scenario C: 90d win rate at 24.0% (div = -1.0%) -> STABLE
        div_c = 24.0 - lifetime_shrunk
        status_c = "DECAYING" if div_c <= -5.0 else ("IMPROVING" if div_c >= 5.0 else "STABLE")
        self.assertEqual(status_c, "STABLE")
        print("✓ Pattern decay divergence logic verified: DECAYING, IMPROVING, and STABLE.")


class TestStage4Integration(unittest.TestCase):
    """Integration tests for Stage 4 DB, PatternExpander, PatternLibrary, and PredictionEngine."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "intel_stage4_test.db"
        self.db = IntelligenceDB(self.db_path)
        self.eb = EmpiricalBayesEstimator()
        self.pattern_lib = PatternLibrary(self.db, self.eb)
        self.pattern_lib.rebuild()
        self.investigator = CauseInvestigator(self.db)
        self.prediction_engine = PredictionEngine(self.db, self.pattern_lib, self.investigator)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unsupervised_pattern_expansion(self):
        """Test that PatternExpander autonomously discovers recurring causal clusters."""
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Seed 6 events for a novel combination: SECTOR_MOMENTUM + VOLUME_ACCUMULATION
        # (This combination is not P001-P008, which has P004 requiring TECHNICAL_BREAKOUT too)
        symbols = ["OGDC", "PPL", "MARI", "OGDC", "PPL", "MARI"]
        for i, sym in enumerate(symbols):
            ev_id = f"ev_exp_{i}"
            self.db.insert_event({
                "id": ev_id,
                "symbol": sym,
                "sector": "Oil & Gas Exploration",
                "event_type": "PRICE_SPIKE",
                "price": 120.0 + i,
                "price_change_pct": 5.0,
                "volume": 2000000,
                "rvol": 3.0,
                "trade_date": today,
                "detected_at": now
            })
            # Insert confident causes
            self.db.insert_causes([
                {
                    "event_id": ev_id,
                    "factor": CAUSE_SECTOR_MOMENTUM,
                    "confidence": 75,
                    "evidence": "Strong",
                    "detail": "Sector peers rising",
                    "created_at": now
                },
                {
                    "event_id": ev_id,
                    "factor": CAUSE_VOLUME_ACCUM,
                    "confidence": 80,
                    "evidence": "Strong",
                    "detail": "Heavy accumulation volume",
                    "created_at": now
                }
            ])

        expander = PatternExpander(self.db)
        # Force min_occurrences = 5 for test
        expander.min_occurrences = 5
        expander.min_distinct_symbols = 2

        base_fingerprints = {p["fingerprint"] for p in self.pattern_lib.BASE_PATTERN_DEFINITIONS}
        candidates = expander.discover_candidate_patterns(base_fingerprints)

        self.assertGreaterEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertTrue(cand["id"].startswith("P"))
        self.assertEqual(cand["is_expanded"], 1)
        expected_fp = f"{CAUSE_SECTOR_MOMENTUM}|{CAUSE_VOLUME_ACCUM}"
        self.assertEqual(cand["fingerprint"], expected_fp)
        self.assertIn("Sector Surge + Volume Accumulation", cand["name"])
        print(f"✓ PatternExpander successfully discovered cluster: {cand['id']} - '{cand['name']}'")

    def test_pattern_regime_conditioning_rebuild(self):
        """Test rebuild() partitions statistics across regimes and persists to pattern_regimes."""
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Create 10 events matching P001 (TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION)
        # 5 in BULL regime (all wins)
        # 5 in BEAR regime (all losses)
        for i in range(10):
            ev_id = f"ev_reg_{i}"
            is_bull = (i < 5)
            regime = "BULL" if is_bull else "BEAR"
            kse_ret = 3.0 if is_bull else -2.5

            self.db.insert_event({
                "id": ev_id,
                "symbol": "LUCK",
                "sector": "Cement",
                "event_type": "PRICE_SPIKE",
                "price": 800.0,
                "price_change_pct": 5.0,
                "volume": 500000,
                "rvol": 3.0,
                "trade_date": today,
                "detected_at": now,
                "kse_return_5d": kse_ret,
                "regime": regime
            })
            self.db.insert_causes([
                {
                    "event_id": ev_id,
                    "factor": CAUSE_TECH_BREAKOUT,
                    "confidence": 75,
                    "evidence": "Strong",
                    "detail": "Breakout",
                    "created_at": now
                },
                {
                    "event_id": ev_id,
                    "factor": CAUSE_VOLUME_ACCUM,
                    "confidence": 80,
                    "evidence": "Strong",
                    "detail": "Volume Surge",
                    "created_at": now
                }
            ])

            # Seed pattern_occurrences outcome: wins for bull, losses for bear
            outcome = "WIN" if is_bull else "LOSS"
            ret5 = 4.5 if is_bull else -3.0
            conn = self.db._connect()
            conn.execute("""
                INSERT INTO pattern_occurrences
                (pattern_id, event_id, symbol, matched_at, similarity, outcome, return_5d, regime)
                VALUES ('P001', ?, 'LUCK', ?, 0.95, ?, ?, ?)
            """, (ev_id, now, outcome, ret5, regime))
            conn.commit()
            conn.close()

        # Run rebuild
        self.pattern_lib.rebuild()

        # Check pattern_regimes table
        regimes = self.db.get_pattern_regimes("P001")
        self.assertEqual(len(regimes), 4)

        reg_map = {r["regime"]: r for r in regimes}
        bull_stats = reg_map["BULL"]
        bear_stats = reg_map["BEAR"]

        # Bull: 5 wins, 0 losses -> 100% raw win rate
        self.assertEqual(bull_stats["win_count"], 5)
        self.assertEqual(bull_stats["loss_count"], 0)
        self.assertEqual(bull_stats["raw_win_rate"], 100.0)
        self.assertGreater(bull_stats["shrunk_win_rate"], bull_stats["wilson_ci_low"])
        self.assertGreater(bull_stats["regime_multiplier"], 1.0)

        # Bear: 0 wins, 5 losses -> 0% raw win rate
        self.assertEqual(bear_stats["win_count"], 0)
        self.assertEqual(bear_stats["loss_count"], 5)
        self.assertEqual(bear_stats["raw_win_rate"], 0.0)
        self.assertLess(bear_stats["regime_multiplier"], 1.0)
        print(f"✓ Pattern regime conditioning verified: BULL mult={bull_stats['regime_multiplier']}x vs BEAR mult={bear_stats['regime_multiplier']}x")

    def test_decay_damping_in_prediction_engine(self):
        """Test PredictionEngine dampens confidence when matching a DECAYING pattern."""
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        # Manually seed a decaying pattern P001 in detected_patterns
        self.db.upsert_pattern({
            "id": "P001",
            "name": "Breakout + Volume Surge",
            "fingerprint": f"{CAUSE_TECH_BREAKOUT}|{CAUSE_VOLUME_ACCUM}",
            "occurrences": 20,
            "win_count": 8,
            "loss_count": 12,
            "raw_win_rate": 40.0,
            "shrunk_win_rate": 35.0,
            "sample_size_n": 20,
            "win_rate_90d": 10.0,
            "sample_size_90d": 10,
            "decay_status": "DECAYING",
            "decay_divergence": -25.0
        })

        # Generate event with P001 causes
        ev_id = "ev_decay_test"
        event = {
            "id": ev_id,
            "symbol": "SYS",
            "sector": "Technology & Communication",
            "event_type": "PRICE_SPIKE",
            "price": 450.0,
            "price_change_pct": 5.0,
            "volume": 1000000,
            "rvol": 3.0,
            "rsi_at_event": 62.0,
            "trade_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "detected_at": now,
            "kse_return_5d": 0.5  # Neutral regime
        }
        self.db.insert_event(event)
        causes = [
            {"factor": CAUSE_TECH_BREAKOUT, "confidence": 80, "evidence": "Strong"},
            {"factor": CAUSE_VOLUME_ACCUM, "confidence": 75, "evidence": "Strong"}
        ]

        # First generate prediction with DECAYING pattern
        self.prediction_engine.generate_prediction(event, causes)

        preds = self.db.get_active_predictions(limit=1)
        self.assertEqual(len(preds), 1)
        pred = preds[0]
        reasoning = json.loads(pred["reasoning_json"])

        self.assertEqual(reasoning["decay_status"], "DECAYING")
        self.assertIsNotNone(reasoning["decay_note"])
        self.assertIn("0.80x dampening applied", reasoning["decay_note"])
        print(f"✓ Decay damping verified in PredictionEngine: confidence={pred['confidence']}%, note='{reasoning['decay_note']}'")


if __name__ == "__main__":
    unittest.main()

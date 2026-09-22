#!/usr/bin/env python3
"""
Unit and Integration Tests for Stage 6: Self-Learning Loop Guardrails
- 6.1 Weight Movement Cap (10% relative change limit with floor)
- 6.2 Calibration Run Audit Logging with 70/30 Train/Held-Out Split
- 6.3 Version Tracking on Predictions and Suggestions
"""

import os
import sys
import json
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# Ensure workspace root is on sys.path
BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from psx_intelligence_engine import (
    IntelligenceDB,
    EmpiricalBayesEstimator,
    PatternLibrary,
    PredictionEngine,
    SuggestedSharesEngine,
    LearningEngine,
    CausalCalibrator,
    CauseInvestigator,
    clamp_weight_change,
    EVENT_PRICE_SPIKE,
    CAUSE_TECH_BREAKOUT,
    CAUSE_VOLUME_ACCUM
)

_test_counter = 0


def _make_db():
    """Create a fresh isolated DB for each test."""
    global _test_counter
    _test_counter += 1
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / f"s6test_{_test_counter}.db"
    return IntelligenceDB(db_path)


class TestWeightClamping(unittest.TestCase):
    """6.1 Mathematical unit tests for weight movement clamping."""

    def test_weight_change_clamping_upward(self):
        """A +50% jump in raw weight (1.0 -> 1.5) must be clamped to +10% (1.10)."""
        old_val = 1.0
        new_val = 1.5
        clamped, was_clamped = clamp_weight_change(old_val, new_val, max_rel=0.10, min_abs=0.02)
        self.assertTrue(was_clamped)
        self.assertAlmostEqual(clamped, 1.10, places=3)

    def test_weight_change_clamping_downward(self):
        """A -40% drop in raw weight (1.0 -> 0.60) must be clamped to -10% (0.90)."""
        old_val = 1.0
        new_val = 0.60
        clamped, was_clamped = clamp_weight_change(old_val, new_val, max_rel=0.10, min_abs=0.02)
        self.assertTrue(was_clamped)
        self.assertAlmostEqual(clamped, 0.90, places=3)

    def test_weight_change_within_tolerance(self):
        """A +5% change (1.0 -> 1.05) is within 10% limit and should not be clamped."""
        old_val = 1.0
        new_val = 1.05
        clamped, was_clamped = clamp_weight_change(old_val, new_val, max_rel=0.10, min_abs=0.02)
        self.assertFalse(was_clamped)
        self.assertAlmostEqual(clamped, 1.05, places=3)

    def test_weight_change_near_zero(self):
        """Near-zero weights utilize absolute floor tolerance."""
        old_val = 0.0
        new_val = 0.08
        clamped, was_clamped = clamp_weight_change(old_val, new_val, max_rel=0.10, min_abs=0.02)
        self.assertTrue(was_clamped)
        self.assertAlmostEqual(clamped, 0.02, places=3)


class TestSectorAndPatternClampingIntegration(unittest.TestCase):
    """Verify weight clamping during sector shrinkage and pattern rebuilds."""

    def setUp(self):
        self.db = _make_db()
        self.eb = EmpiricalBayesEstimator()
        self.learning_engine = LearningEngine(self.db)
        self.pattern_lib = PatternLibrary(self.db, self.eb)

    def test_sector_multiplier_clamped_on_rebuild(self):
        """Pre-seed sector at multiplier 1.00; verify update is capped at 1.10."""
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # Pre-seed Technology sector in DB with multiplier = 1.00
        self.db.upsert_sector_shrinkage({
            "sector": "Technology",
            "sample_count": 50,
            "win_count": 25,
            "loss_count": 25,
            "raw_win_rate": 50.0,
            "shrunk_win_rate": 50.0,
            "multiplier": 1.00,
            "wilson_ci_low": 35.0,
            "wilson_ci_high": 65.0,
            "shrinkage_weight": 0.5,
            "updated_at": now
        })

        # Insert 10 events and predictions with 100% wins to trigger a massive multiplier increase
        for i in range(10):
            ev_id = f"ev_sec_{i}"
            pred_id = f"pred_sec_{i}"
            self.db.insert_event({
                "id": ev_id,
                "symbol": "SYS",
                "sector": "Technology",
                "event_type": EVENT_PRICE_SPIKE,
                "price": 100.0,
                "price_change_pct": 5.0,
                "volume": 100000,
                "trade_date": "2026-09-20",
                "status": "EVALUATED"
            })
            self.db.insert_prediction({
                "id": pred_id,
                "symbol": "SYS",
                "event_id": ev_id,
                "signal": "POSSIBLE_BREAKOUT",
                "confidence": 85,
                "price_at_signal": 100.0,
                "predicted_at": "2026-09-20T10:00:00Z",
                "outcome": "CORRECT"
            })
            self.db.update_prediction_outcome(
                pred_id=pred_id,
                outcome="CORRECT",
                actual_return_5d=6.0,
                max_drawdown_5d=1.0,
                trader_pnl=1.0,
                kse_return_5d=1.0,
                excess_return_kse_5d=5.0,
                beat_kse=1,
                evaluation_flag="VALID"
            )

        res = self.learning_engine.rebuild_sector_shrinkage(self.eb)
        sec_record = next((s for s in res["sectors"] if s["sector"] == "Technology"), None)
        self.assertIsNotNone(sec_record)
        # Multiplier must be clamped: 1.0 * 1.10 = 1.10
        self.assertLessEqual(sec_record["multiplier"], 1.1001)

    def test_pattern_shrunk_win_rate_clamped_on_rebuild(self):
        """Pre-seed pattern P001 with shrunk win rate 20.0%; verify update is capped to 22.0%."""
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.upsert_pattern({
            "id": "P001",
            "name": "Breakout + Volume Surge",
            "fingerprint": "TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION",
            "description": "Test",
            "shrunk_win_rate": 20.0,
            "raw_win_rate": 20.0,
            "sample_size_n": 10,
            "win_count": 2,
            "loss_count": 8,
            "occurrences": 10
        })

        # Insert 15 winning events
        for i in range(15):
            ev_id = f"ev_pat_{i}"
            self.db.insert_event({
                "id": ev_id,
                "symbol": "LUCK",
                "sector": "Cement",
                "event_type": EVENT_PRICE_SPIKE,
                "price": 500.0,
                "trade_date": "2026-09-20",
                "status": "EVALUATED"
            })
            self.db.insert_causes([
                {
                    "event_id": ev_id,
                    "factor": CAUSE_TECH_BREAKOUT,
                    "confidence": 85,
                    "evidence": "High",
                    "detail": "",
                    "created_at": "2026-09-20T10:00:00Z"
                },
                {
                    "event_id": ev_id,
                    "factor": CAUSE_VOLUME_ACCUM,
                    "confidence": 85,
                    "evidence": "High",
                    "detail": "",
                    "created_at": "2026-09-20T10:00:00Z"
                }
            ])
            self.db.insert_prediction({
                "id": f"pred_pat_{i}",
                "symbol": "LUCK",
                "event_id": ev_id,
                "pattern_id": "P001",
                "signal": "POSSIBLE_BREAKOUT",
                "confidence": 80,
                "price_at_signal": 500.0,
                "predicted_at": "2026-09-20T10:00:00Z",
                "outcome": "CORRECT"
            })
            self.db.update_prediction_outcome(
                pred_id=f"pred_pat_{i}",
                outcome="CORRECT",
                actual_return_5d=5.0,
                max_drawdown_5d=1.0,
                trader_pnl=1.0,
                kse_return_5d=1.0,
                excess_return_kse_5d=4.0,
                beat_kse=1,
                evaluation_flag="VALID"
            )

        self.pattern_lib.rebuild()
        pat = self.db.get_pattern("P001")
        self.assertIsNotNone(pat)
        # Should be clamped to 20.0 * 1.10 = 22.0
        self.assertLessEqual(pat["shrunk_win_rate"], 22.001)


class TestGuardedRecalibrationAndAuditLog(unittest.TestCase):
    """6.2 Verify run_guarded_recalibration, 70/30 split, and audit log persistence."""

    def setUp(self):
        self.db = _make_db()
        self.eb = EmpiricalBayesEstimator()
        self.learning_engine = LearningEngine(self.db)
        self.pattern_lib = PatternLibrary(self.db, self.eb)
        self.causal_calibrator = CausalCalibrator(self.db)

    def test_recalibration_audit_log_and_held_out_split(self):
        """Seed 10 valid predictions, run guarded recalibration, check 70/30 split and DB persistence."""
        for i in range(10):
            ev_id = f"ev_audit_{i}"
            pred_id = f"pred_audit_{i}"
            self.db.insert_event({
                "id": ev_id,
                "symbol": "OGDC",
                "sector": "Oil & Gas",
                "event_type": EVENT_PRICE_SPIKE,
                "price": 120.0,
                "trade_date": f"2026-09-{10+i:02d}",
                "status": "EVALUATED"
            })
            self.db.insert_prediction({
                "id": pred_id,
                "symbol": "OGDC",
                "event_id": ev_id,
                "signal": "POSSIBLE_BREAKOUT",
                "confidence": 75,
                "price_at_signal": 120.0,
                "predicted_at": f"2026-09-{10+i:02d}T10:00:00Z",
                "outcome": "CORRECT" if i % 2 == 0 else "INCORRECT"
            })
            self.db.update_prediction_outcome(
                pred_id=pred_id,
                outcome="CORRECT" if i % 2 == 0 else "INCORRECT",
                actual_return_5d=3.0 if i % 2 == 0 else -2.0,
                max_drawdown_5d=1.0,
                trader_pnl=1.0 if i % 2 == 0 else -1.5,
                kse_return_5d=0.5,
                excess_return_kse_5d=2.5 if i % 2 == 0 else -2.5,
                beat_kse=1 if i % 2 == 0 else 0,
                evaluation_flag="VALID"
            )

        run = self.learning_engine.run_guarded_recalibration(
            estimator=self.eb,
            causal_calibrator=self.causal_calibrator,
            pattern_lib=self.pattern_lib,
            held_out_split=0.30
        )

        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "SUCCESS")
        self.assertTrue(run["weights_version"].startswith("W_v"))
        self.assertTrue(run["pattern_version"].startswith("P_v"))
        # 10 samples -> 7 training, 3 held-out
        self.assertEqual(run["training_sample_count"], 7)
        self.assertEqual(run["held_out_sample_count"], 3)
        self.assertIn("sectors", json.loads(run["before_weights_json"]))
        self.assertIn("sectors", json.loads(run["after_weights_json"]))

        # Verify DB retrieval
        runs = self.db.get_calibration_runs(limit=5)
        self.assertGreaterEqual(len(runs), 1)
        latest = self.db.get_latest_calibration_run()
        self.assertEqual(latest["id"], run["id"])

        active_versions = self.db.get_active_model_versions()
        self.assertEqual(active_versions["weights_version"], run["weights_version"])
        self.assertEqual(active_versions["pattern_version"], run["pattern_version"])


class TestVersionTagging(unittest.TestCase):
    """6.3 Verify predictions and suggestions are stamped with active model versions."""

    def setUp(self):
        self.db = _make_db()
        self.eb = EmpiricalBayesEstimator()
        self.investigator = CauseInvestigator(self.db)
        self.pattern_lib = PatternLibrary(self.db, self.eb)
        self.pred_engine = PredictionEngine(self.db, self.pattern_lib, self.investigator)
        self.learning_engine = LearningEngine(self.db)
        self.suggested_shares_engine = SuggestedSharesEngine(self.db, self.pattern_lib, self.learning_engine)

    def test_prediction_engine_version_tagging(self):
        """Verify generated prediction has weights_version and pattern_version columns populated."""
        # Insert a calibration run record to establish an active version
        self.db.insert_calibration_run({
            "id": "CALIB-TEST-V1",
            "weights_version": "W_v2026.09.22.99",
            "pattern_version": "P_v2026.09.22.99",
            "status": "SUCCESS"
        })

        ev = {
            "id": "ev_ver_1",
            "symbol": "MEBL",
            "sector": "Commercial Banks",
            "event_type": EVENT_PRICE_SPIKE,
            "price": 200.0,
            "change_pct": 3.5,
            "volume": 500000,
            "rvol": 2.5,
            "rsi": 62.0,
            "trade_date": "2026-09-22"
        }
        causes = [{
            "factor": CAUSE_TECH_BREAKOUT,
            "confidence": 80,
            "evidence": "High"
        }]

        self.db.insert_event(ev)
        self.pred_engine.generate_prediction(ev, causes)
        preds = self.db.get_active_predictions(limit=5)
        mebl_pred = next((p for p in preds if p["symbol"] == "MEBL"), None)
        self.assertIsNotNone(mebl_pred)
        self.assertEqual(mebl_pred["weights_version"], "W_v2026.09.22.99")
        self.assertEqual(mebl_pred["pattern_version"], "P_v2026.09.22.99")

    def test_suggested_shares_version_tagging(self):
        """Verify generated suggestions have weights_version and pattern_version."""
        self.db.insert_calibration_run({
            "id": "CALIB-TEST-V2",
            "weights_version": "W_v2026.09.22.100",
            "pattern_version": "P_v2026.09.22.100",
            "status": "SUCCESS"
        })

        # Insert a pattern and prediction to generate suggestions from
        self.db.upsert_pattern({
            "id": "P001",
            "name": "Breakout",
            "fingerprint": "TECHNICAL_BREAKOUT",
            "description": "Test",
            "occurrences": 30,
            "win_count": 20,
            "loss_count": 10,
            "shrunk_win_rate": 65.0,
            "raw_win_rate": 66.7,
            "wilson_ci_low": 48.0,
            "wilson_ci_high": 80.0,
            "sample_size_n": 30,
            "avg_5d_return": 4.5
        })

        ev_id = "ev_ss_ver_1"
        self.db.insert_event({
            "id": ev_id,
            "symbol": "HUBC",
            "sector": "Power",
            "event_type": EVENT_PRICE_SPIKE,
            "price": 110.0,
            "change_pct": 3.0,
            "volume": 200000,
            "trade_date": "2026-09-22"
        })
        self.db.insert_causes([{
            "event_id": ev_id,
            "factor": CAUSE_TECH_BREAKOUT,
            "confidence": 85,
            "evidence": "High",
            "detail": "",
            "created_at": "2026-09-22T10:00:00Z"
        }])

        mock_preds = [{
            "id": "pred_ss_ver_1",
            "symbol": "HUBC",
            "event_id": ev_id,
            "pattern_id": "P001",
            "signal": "POSSIBLE_BREAKOUT",
            "confidence": 80,
            "price_at_signal": 110.0,
            "entry_price": 110.0,
            "target_price": 118.8,
            "stop_loss": 105.6,
            "sector": "Power",
            "matched_pattern_id": "P001"
        }]

        res = self.suggested_shares_engine.generate_suggestions(
            market_stocks=[{"symbol": "HUBC", "price": 110.0, "value": 1000000, "volume": 50000}],
            recent_predictions=mock_preds,
            regime="BULL"
        )

        active = res["active_ideas"]
        spec = res["speculative_ideas"]
        all_ideas = active + spec
        hubc_idea = next((i for i in all_ideas if i["symbol"] == "HUBC"), None)
        self.assertIsNotNone(hubc_idea)
        self.assertEqual(hubc_idea["weights_version"], "W_v2026.09.22.100")
        self.assertEqual(hubc_idea["pattern_version"], "P_v2026.09.22.100")


if __name__ == "__main__":
    unittest.main()

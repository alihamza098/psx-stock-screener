"""
test_psx_intelligence_stage1.py
Unit and synthetic test suite for PSX Intelligence Engine Stage 1:
- Causal Factor Validation & Platt Scaling Calibration
- Brier Score tracking (raw vs. calibrated error reduction)
- Multicollinearity decorrelation of Momentum factors into MOMENTUM_CLUSTER
- Unvalidated heuristic capping (<= 50%) for lenses with N < 100
- Corporate Announcement integration into CauseInvestigator
- PredictionEngine integration with composite calibrated probabilities
"""

import math
import tempfile
import sqlite3
import json
from pathlib import Path
from psx_intelligence_engine import (
    IntelligenceDB,
    CausalCalibrator,
    CauseInvestigator,
    PredictionEngine,
    PatternLibrary,
    EVENT_RESISTANCE_BREAK,
    EVENT_VOLUME_SURGE
)


def test_platt_scaling_fit_and_brier_reduction():
    """Verify Platt scaling fits logistic parameters and significantly lowers Brier score."""
    # Synthetic scenario: Overconfident heuristic
    # Scores are high (70 to 90), but actual positive rate is only 20%
    scores = [75.0, 80.0, 85.0, 90.0, 70.0] * 30  # 150 samples
    labels = [1, 0, 0, 0, 0] * 30                # 20% win rate

    A, B, cal_brier, raw_brier = CausalCalibrator.fit_platt_scaling(scores, labels)

    assert raw_brier > 0.35, f"Raw brier should reflect overconfidence, got {raw_brier}"
    assert cal_brier < raw_brier, f"Calibrated Brier {cal_brier} must be lower than raw {raw_brier}"
    assert cal_brier <= 0.20, f"Calibrated Brier should approach baseline rate variance, got {cal_brier}"

    # Verify probability predictions from fitted curve
    z_80 = A * (80.0 / 100.0) + B
    prob_80 = 1.0 / (1.0 + math.exp(-z_80))
    # True win rate is 20%, so calibrated probability should be close to ~0.20, not 0.80
    assert 0.10 <= prob_80 <= 0.30, f"Expected calibrated prob ~0.20, got {prob_80}"
    print(f"✓ Platt scaling: Raw Brier = {raw_brier:.4f} -> Calibrated Brier = {cal_brier:.4f} (A={A}, B={B})")


def test_momentum_cluster_decorrelation():
    """
    Verify that overlapping momentum lenses (Technical Breakout + RSI + MACD)
    are merged into a single MOMENTUM_CLUSTER with capped secondary bonus.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_intel.db"
        db = IntelligenceDB(db_path=db_path)
        calibrator = CausalCalibrator(db)

        # 3 overlapping momentum causes with high confidences
        causes = [
            {"factor": "TECHNICAL_BREAKOUT", "confidence": 85, "evidence": "Breakout above 30d high"},
            {"factor": "RSI_MOMENTUM", "confidence": 80, "evidence": "RSI at 68"},
            {"factor": "MACD_CONFIRMATION", "confidence": 75, "evidence": "Bullish MACD crossover"}
        ]

        composite = calibrator.compute_composite_severity(causes)
        breakdown = composite["cluster_breakdown"]

        # All 3 factors must be merged into MOMENTUM_CLUSTER
        assert "MOMENTUM_CLUSTER" in breakdown, "MOMENTUM_CLUSTER must exist in breakdown"
        assert "TECHNICAL_BREAKOUT" not in breakdown, "TECHNICAL_BREAKOUT should not be a separate cluster"
        assert "RSI_MOMENTUM" not in breakdown, "RSI_MOMENTUM should not be a separate cluster"

        # Primary = 85. Secondary bonus = 0.10 * 80 + 0.10 * 75 = 8 + 7.5 = 15.5 -> capped at 10.0
        # Expected MOMENTUM_CLUSTER raw score = min(95, 85 + 10) = 95.0
        cluster_score = breakdown["MOMENTUM_CLUSTER"]["raw_score"]
        assert cluster_score == 95.0, f"Expected capped score 95.0, got {cluster_score}"
        print("✓ Momentum cluster decorrelation: 3 overlapping signals merged into 1 cluster with capped bonus")


def test_unvalidated_heuristic_capping():
    """
    Lenses with sample size N < 100 must be marked as 'unvalidated heuristic'
    and cannot produce probability > 0.50 nor drive composite confidence > 50%.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_intel.db"
        db = IntelligenceDB(db_path=db_path)
        calibrator = CausalCalibrator(db)

        # Query uncalibrated lens with extreme score 95
        prob, is_cal, tag = calibrator.predict_cluster_probability("CORPORATE_ANNOUNCEMENT", 95.0)
        assert not is_cal, "Uncalibrated lens must report is_calibrated = False"
        assert tag == "unvalidated heuristic", f"Expected 'unvalidated heuristic', got {tag}"
        assert prob <= 0.50, f"Unvalidated heuristic probability must not exceed 0.50, got {prob}"

        # Test composite score when only unvalidated heuristics are active
        unvalidated_causes = [
            {"factor": "CORPORATE_ANNOUNCEMENT", "confidence": 95, "evidence": "Dividend announced"}
        ]
        composite = calibrator.compute_composite_severity(unvalidated_causes)
        assert composite["label"] == "unvalidated heuristic"
        assert composite["composite_score"] <= 50.0, f"Composite score should be capped at 50, got {composite['composite_score']}"
        assert composite["calibrated_win_probability"] <= 0.50
        print(f"✓ Unvalidated heuristic capping: prob={prob}, composite={composite['composite_score']} (both <= 50%)")


def test_corporate_announcement_detection():
    """Verify CauseInvestigator integrates corporate announcements as first-class causes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_intel.db"
        db = IntelligenceDB(db_path=db_path)
        investigator = CauseInvestigator(db)

        # Mock noticeboard scraper returning an announcement for OGDC
        class MockNoticeBoard:
            def has_announcement_for_symbol(self, symbol, days=15):
                if symbol == "OGDC":
                    return True, "Board meeting scheduled for financial results"
                return False, ""

        investigator.noticeboard = MockNoticeBoard()

        event = {
            "id": "EVT_TEST_ANN_001",
            "symbol": "OGDC",
            "event_type": EVENT_VOLUME_SURGE,
            "price": 120.0,
            "rvol": 3.5,
            "change_pct": 3.0
        }

        causes = investigator.investigate(event)
        ann_cause = next((c for c in causes if c["factor"] == "CORPORATE_ANNOUNCEMENT"), None)
        assert ann_cause is not None, "CORPORATE_ANNOUNCEMENT must be present in investigated causes"
        assert "Board meeting" in ann_cause["detail"]
        assert ann_cause["confidence"] == 75
        print(f"✓ Corporate announcement integration: Found factor={ann_cause['factor']}, detail='{ann_cause['detail']}'")


def test_prediction_engine_calibrated_confidence():
    """Verify PredictionEngine generates prediction with calibrated probabilities and tags."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_intel.db"
        db = IntelligenceDB(db_path=db_path)
        calibrator = CausalCalibrator(db)
        pat_lib = PatternLibrary(db)
        pat_lib.rebuild()
        investigator = CauseInvestigator(db)
        pred_engine = PredictionEngine(db, pat_lib, investigator, calibrator)

        event = {
            "id": "EVT_TEST_001",
            "symbol": "HUBC",
            "event_type": EVENT_RESISTANCE_BREAK,
            "price": 140.0,
            "change_pct": 2.8,
            "volume": 500000.0,
            "rvol": 2.5,
            "resistance_level": 138.0,
            "support_level": 132.0,
            "rsi_at_event": 62.0,
            "severity": "HIGH",
            "detected_at": "2026-09-22T10:00:00Z",
            "is_noisy_liquidity": 0,
            "excess_return_sector": 1.8,
            "excess_return_kse": 2.2
        }
        db.insert_event(event)

        causes = [
            {"factor": "TECHNICAL_BREAKOUT", "confidence": 80, "evidence": "Breakout above 30d high"},
            {"factor": "RSI_MOMENTUM", "confidence": 75, "evidence": "RSI jump +4.5"}
        ]

        pred_engine.generate_prediction(event, causes)

        preds = db.get_active_predictions(limit=5)
        assert len(preds) == 1, "Prediction must be stored in database"
        p = preds[0]
        reasoning = json.loads(p["reasoning_json"])
        assert "causal_calibration" in reasoning
        cal_info = reasoning["causal_calibration"]
        assert "cluster_breakdown" in cal_info
        assert "MOMENTUM_CLUSTER" in cal_info["cluster_breakdown"]
        print(f"✓ PredictionEngine integration: Signal={p['signal']}, Confidence={p['confidence']}, Breakdown={list(cal_info['cluster_breakdown'].keys())}")


if __name__ == "__main__":
    test_platt_scaling_fit_and_brier_reduction()
    test_momentum_cluster_decorrelation()
    test_unvalidated_heuristic_capping()
    test_corporate_announcement_detection()
    test_prediction_engine_calibrated_confidence()
    print("\n✅ All Stage 1 tests passed successfully!")

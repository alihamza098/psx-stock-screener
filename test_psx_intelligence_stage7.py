#!/usr/bin/env python3
"""
test_psx_intelligence_stage7.py
Tests for Stage 7: Transparency in the UI
  7.1 Calibration Curve (reliability diagram, binning, ECE, MCE, Brier score)
  7.2 Strict No-Bare-Percentages & Wilson CI checks
  7.3 Correlation Framing Audit (no causal verbs in patterns, theses, narratives)
  7.4 Dual Metric Display (lifetime vs rolling 90-day Wilson CIs)
"""

import os
import json
import pytest
import sqlite3

from psx_intelligence_engine import (
    IntelligenceDB,
    LearningEngine,
    IntelligenceEngine,
    PatternLibrary,
    SuggestedSharesEngine,
    compute_wilson_ci,
    get_engine,
)


class TestStage7CalibrationCurve:
    """Test 7.1: Calibration reliability curve, bucket binning, and error metrics."""

    def test_synthetic_calibration_curve(self, tmp_path):
        db_path = str(tmp_path / "test_stage7_calib.db")
        db = IntelligenceDB(db_path)
        learning = LearningEngine(db)

        # Create synthetic predictions across various confidence buckets
        # Bucket 30-40%: 10 samples, 4 correct -> win rate = 40.0%
        # Bucket 40-50%: 10 samples, 2 correct -> win rate = 20.0%
        # Bucket 70-80%: 10 samples, 8 correct -> win rate = 80.0%
        synthetic_preds = []
        for i in range(10):
            synthetic_preds.append({
                "confidence": 35.0,
                "outcome": "CORRECT" if i < 4 else "INCORRECT",
                "evaluation_flag": "VALID"
            })
        for i in range(10):
            synthetic_preds.append({
                "confidence": 45.0,
                "outcome": "CORRECT" if i < 2 else "INCORRECT",
                "evaluation_flag": "VALID"
            })
        for i in range(10):
            synthetic_preds.append({
                "confidence": 75.0,
                "outcome": "CORRECT" if i < 8 else "INCORRECT",
                "evaluation_flag": "VALID"
            })

        curve = learning.compute_calibration_curve(synthetic_preds)
        assert "buckets" in curve
        assert "summary" in curve
        assert len(curve["buckets"]) == 10

        summary = curve["summary"]
        assert summary["total_decided_samples"] == 30
        assert summary["total_purged_samples"] == 30
        # Total correct: 4 + 2 + 8 = 14 / 30 = 46.7%
        assert summary["overall_purged_win_rate"] == pytest.approx(46.7, abs=0.5)

        # Check Bucket 30-40%
        b30 = next(b for b in curve["buckets"] if b["bucket_label"] == "30-40%")
        assert b30["total_n"] == 10
        assert b30["purged_n"] == 10
        assert b30["mean_pred"] == 35.0
        assert b30["purged_win_rate"] == 40.0
        assert b30["wilson_ci_low"] > 0
        assert b30["wilson_ci_high"] > b30["wilson_ci_low"]
        assert b30["bias"] == pytest.approx(-5.0, abs=0.1)  # 35 - 40 = -5 (underconfident)

        # Check Bucket 40-50%
        b40 = next(b for b in curve["buckets"] if b["bucket_label"] == "40-50%")
        assert b40["total_n"] == 10
        assert b40["purged_win_rate"] == 20.0
        assert b40["bias"] == pytest.approx(25.0, abs=0.1)  # 45 - 20 = +25 (overconfident)

        # Check Bucket 70-80%
        b70 = next(b for b in curve["buckets"] if b["bucket_label"] == "70-80%")
        assert b70["total_n"] == 10
        assert b70["purged_win_rate"] == 80.0
        assert b70["bias"] == pytest.approx(-5.0, abs=0.1)  # 75 - 80 = -5

        # ECE must be positive and bounded
        assert 0.0 <= summary["ece"] <= 1.0
        assert 0.0 <= summary["overall_brier_purged"] <= 1.0

    def test_purged_vs_embargoed_filtering(self, tmp_path):
        """Ensure embargoed/contaminated predictions are partitioned from purged validation."""
        db_path = str(tmp_path / "test_stage7_purged.db")
        db = IntelligenceDB(db_path)
        learning = LearningEngine(db)

        preds = [
            # 5 Valid in 40-50%
            {"confidence": 42.0, "outcome": "CORRECT", "evaluation_flag": "VALID"},
            {"confidence": 42.0, "outcome": "CORRECT", "evaluation_flag": "VALID"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "VALID"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "VALID"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "VALID"},
            # 5 Embargoed in 40-50% (all INCORRECT)
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "EMBARGOED"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "EMBARGOED"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "EMBARGOED"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "EMBARGOED"},
            {"confidence": 42.0, "outcome": "INCORRECT", "evaluation_flag": "EMBARGOED"},
        ]

        res = learning.compute_calibration_curve(preds)
        b = next(b for b in res["buckets"] if b["bucket_label"] == "40-50%")
        assert b["total_n"] == 10
        assert b["purged_n"] == 5
        assert b["raw_win_rate"] == 20.0    # 2 / 10
        assert b["purged_win_rate"] == 40.0 # 2 / 5


class TestStage7CorrelationFramingAudit:
    """Test 7.3: Strict Correlation Framing across all patterns and narratives."""

    FORBIDDEN_CAUSAL_WORDS = [
        " causes ", " caused by ", " leading to ", " leads to ",
        " highest probability ", " guaranteed ", " sure thing "
    ]

    def test_base_patterns_no_causal_language(self):
        for pat in PatternLibrary.BASE_PATTERN_DEFINITIONS:
            desc = pat.get("description", "").lower()
            name = pat.get("name", "").lower()
            for forbidden in self.FORBIDDEN_CAUSAL_WORDS:
                assert forbidden not in f" {desc} ", f"Pattern {pat['id']} contains causal phrasing '{forbidden}': {desc}"
                assert forbidden not in f" {name} ", f"Pattern {pat['id']} name contains causal phrasing '{forbidden}': {name}"

    def test_thesis_templates_correlation_framing(self):
        for cat, tmpl in SuggestedSharesEngine.THESIS_TEMPLATES_EN.items():
            tmpl_lower = tmpl.lower()
            for forbidden in self.FORBIDDEN_CAUSAL_WORDS:
                assert forbidden not in f" {tmpl_lower} ", f"Thesis template '{cat}' contains causal phrasing '{forbidden}'"
            # Must require sample size N and confidence interval
            assert "{n}" in tmpl, f"Template '{cat}' missing sample size token {{n}}"
            assert "{ci_low" in tmpl and "{ci_high" in tmpl, f"Template '{cat}' missing Wilson CI tokens"

    def test_investigator_narrative_correlation_framing(self, tmp_path):
        db_path = str(tmp_path / "test_stage7_narrative.db")
        db = IntelligenceDB(db_path)
        from psx_intelligence_engine import CauseInvestigator
        investigator = CauseInvestigator(db)

        # Single cause
        n1 = investigator.build_narrative([{"factor": "TECHNICAL_BREAKOUT", "confidence": 85}])
        assert "historically consistent with" in n1 or "historically associated with" in n1
        assert "cause" not in n1.lower()

        # Multi causes
        n3 = investigator.build_narrative([
            {"factor": "TECHNICAL_BREAKOUT", "confidence": 85},
            {"factor": "VOLUME_ACCUMULATION", "confidence": 80},
            {"factor": "RSI_MOMENTUM", "confidence": 75},
        ])
        assert "historically associated with co-occurrence" in n3


class TestStage7DualMetricAndWilsonCI:
    """Test 7.2 & 7.4: Dual metric integrity (Lifetime vs Rolling 90-day) with Wilson CIs."""

    def test_wilson_ci_math(self):
        # 10 wins out of 50 samples
        rate, lo, hi = compute_wilson_ci(10, 50)
        assert rate == 20.0
        assert 10.0 <= lo <= 15.0
        assert 28.0 <= hi <= 34.0
        assert lo < hi

        # Edge cases: 0 samples
        rate, lo, hi = compute_wilson_ci(0, 0)
        assert rate == 0.0 and lo == 0.0 and hi == 0.0

    def test_get_patterns_data_includes_dual_wilson_cis(self):
        engine = get_engine()
        patterns = engine.get_patterns_data()
        assert len(patterns) > 0

        for p in patterns:
            # Lifetime stats
            assert "sample_size_n" in p
            assert "shrunk_win_rate_pct" in p
            assert "wilson_ci_low" in p
            assert "wilson_ci_high" in p
            assert isinstance(p["wilson_ci"], list)

            # Rolling 90-day stats
            assert "sample_size_90d" in p
            assert "win_rate_90d" in p
            assert "wilson_ci_90d" in p
            assert "wilson_ci_90d_low" in p
            assert "wilson_ci_90d_high" in p

            if p["sample_size_90d"] > 0 and p["win_rate_90d"] is not None:
                assert p["wilson_ci_90d_low"] <= p["wilson_ci_90d_high"]

    def test_predictions_reasoning_includes_wilson_ci(self):
        engine = get_engine()
        predictions = engine.get_predictions_data(limit=10)
        # Even if active predictions is empty or non-empty, test the structure
        for pred in predictions:
            assert "reasoning" in pred
            assert "wilson_ci" in pred
            assert "wilson_ci_low" in pred
            assert "wilson_ci_high" in pred

    def test_engine_calibration_curve_endpoint(self):
        engine = get_engine()
        res = engine.get_calibration_curve_data()
        assert "buckets" in res
        assert "summary" in res
        assert len(res["buckets"]) == 10
        assert "ece" in res["summary"]
        assert "overall_brier_purged" in res["summary"]

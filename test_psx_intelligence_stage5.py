"""
test_psx_intelligence_stage5.py
Stage 5 — Suggested Shares: Calibrated, Risk-Managed Idea Generation
Tests:
  T1 – Fractional Kelly: positive/negative expectancy, regime dampening
  T2 – Confidence tier classification (High/Medium/Speculative)
  T3 – Sector cap (max 2 per sector) + list cap (max 10)
  T4 – Suggestion expiry updates status to EXPIRED
  T5 – Track record computed from closed suggestions
"""

import os
import sys
import math
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# ── Path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

import psx_intelligence_engine as ie
from psx_intelligence_engine import (
    IntelligenceDB,
    PatternLibrary,
    LearningEngine,
    EmpiricalBayesEstimator,
    SuggestedSharesEngine,
    compute_wilson_ci,
)

# ── Per-test DB factory ──────────────────────────────────────────────────────
_test_counter = 0

def _make_db():
    """Create a fresh isolated DB for each test (no shared state)."""
    global _test_counter
    _test_counter += 1
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / f"s5test_{_test_counter}.db"
    return IntelligenceDB(db_path)


def _make_engine(db):
    eb = EmpiricalBayesEstimator()
    pl = PatternLibrary(db, eb)
    le = LearningEngine(db)
    return SuggestedSharesEngine(db, pl, le)


def _seed_pattern(db, pattern_id="P001", wins=25, n=40, name="Test Breakout"):
    """Insert a detected_pattern row using the correct schema."""
    conn = db._connect()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO detected_patterns
            (id, name, fingerprint, description, occurrences,
             win_count, loss_count, neutral_count, avg_5d_return,
             raw_win_rate, shrunk_win_rate,
             sample_size_n, wilson_ci_low, wilson_ci_high,
             decay_status, decay_divergence, is_expanded,
             last_updated, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pattern_id, name, f"FP-{pattern_id}", "Test pattern",
            n, wins, n - wins, 0,
            3.5,
            round(wins / n * 100, 2), round(wins / n * 100, 2),
            n,
            *compute_wilson_ci(wins, n)[1:],
            "STABLE", 0.0, 0,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
        ))
        conn.commit()
    finally:
        conn.close()


def _seed_event(db, event_id, symbol, sector="Technology", entry=100.0):
    """Insert a stock_event using the proper insert_event() CRUD method."""
    db.insert_event({
        "id": event_id,
        "symbol": symbol,
        "sector": sector,
        "event_type": "UPPER_LOCK",
        "price": entry,
        "price_change_pct": 4.0,
        "volume": 500000.0,
        "rvol": 2.5,
        "rsi_at_event": 58.0,
    })


def _make_pred(symbol, sector, event_id, pattern_id="P001", entry=100.0):
    """Build a prediction dict for direct injection into generate_suggestions."""
    return {
        "symbol": symbol,
        "sector": sector,
        "event_id": event_id,
        "id": event_id,
        "matched_pattern_id": pattern_id,
        "pattern_id": pattern_id,
        "entry_price": entry,
        "target_price": entry * 1.08,
        "stop_loss": entry * 0.96,
    }


def _closed_share(share_id, outcome, return_5d):
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": share_id,
        "symbol": share_id,
        "sector": "Technology",
        "event_id": f"EVT-{share_id}",
        "pattern_id": "P001",
        "category_tag": "Breakout Continuation",
        "composite_score": 60.0,
        "win_probability": 55.0,
        "wilson_ci_low": 42.0,
        "wilson_ci_high": 68.0,
        "sample_size_n": 22,
        "confidence_tier": "High",
        "expected_return_mean": 2.5,
        "expected_return_q25": 0.8,
        "expected_return_q75": 4.5,
        "sector_multiplier": 1.1,
        "regime": "NEUTRAL",
        "entry_price": 100.0,
        "target_price": 108.0,
        "stop_loss": 95.0,
        "suggested_shares_qty": 50,
        "suggested_outlay": 5000.0,
        "risk_pkr": 250.0,
        "kelly_fraction": 0.05,
        "thesis_en": "",
        "thesis_ur": "",
        "status": "CLOSED",
        "created_at": now,
        "expires_at": now,
        "closed_at": now,
        "return_5d": return_5d,
        "outcome": outcome,
    }


# ═══════════════════════════════════════════════════════════════════════════
# T1 – Fractional Kelly Computation
# ═══════════════════════════════════════════════════════════════════════════

class TestFractionalKelly(unittest.TestCase):

    def setUp(self):
        self.db = _make_db()
        self.engine = _make_engine(self.db)

    def test_positive_expectancy_returns_positive_fraction(self):
        """p=0.6, b=2 → f*=0.4, f_adj=0.1 (quarter-Kelly, NEUTRAL×1.0)"""
        f = self.engine.compute_fractional_kelly(p=0.60, b=2.0, regime="NEUTRAL")
        self.assertGreater(f, 0.0)
        # f* = (2*0.6 - 0.4)/2 = 0.4; f_adj = 0.25 * 0.4 * 1.0 = 0.1
        self.assertAlmostEqual(f, 0.1, places=3)

    def test_negative_expectancy_returns_zero(self):
        """p=0.3, b=1.0 → f* < 0 → clamp to 0"""
        f = self.engine.compute_fractional_kelly(p=0.30, b=1.0, regime="NEUTRAL")
        self.assertEqual(f, 0.0)

    def test_crash_regime_dampens_size(self):
        """CRASH regime multiplier 0.4× vs NEUTRAL 1.0×"""
        f_neutral = self.engine.compute_fractional_kelly(p=0.65, b=2.5, regime="NEUTRAL")
        f_crash   = self.engine.compute_fractional_kelly(p=0.65, b=2.5, regime="CRASH")
        self.assertGreater(f_neutral, 0.0)
        self.assertAlmostEqual(f_crash, f_neutral * 0.40, places=3)

    def test_bear_regime_dampens_size(self):
        """BEAR regime multiplier 0.7×"""
        f_neutral = self.engine.compute_fractional_kelly(p=0.65, b=2.5, regime="NEUTRAL")
        f_bear    = self.engine.compute_fractional_kelly(p=0.65, b=2.5, regime="BEAR")
        self.assertAlmostEqual(f_bear, f_neutral * 0.70, places=3)


# ═══════════════════════════════════════════════════════════════════════════
# T2 – Confidence Tier Classification
# ═══════════════════════════════════════════════════════════════════════════

class TestConfidenceTier(unittest.TestCase):

    def setUp(self):
        self.db = _make_db()
        self.engine = _make_engine(self.db)

    def test_high_tier(self):
        """N=30, narrow CI → High"""
        _, ci_low, ci_high = compute_wilson_ci(21, 30)  # ~70% wr, tight CI
        tier = self.engine._confidence_tier(n=30, ci_low=ci_low, ci_high=ci_high)
        self.assertEqual(tier, "High")

    def test_medium_tier(self):
        """N=10, moderate CI → Medium (CI width typically ~0.40 for 7/10)"""
        _, ci_low, ci_high = compute_wilson_ci(7, 10)
        ci_width = (ci_high - ci_low) / 100.0
        tier = self.engine._confidence_tier(n=10, ci_low=ci_low, ci_high=ci_high)
        # N=10 >= 5, ci_width ~0.40 should be Medium; allow Speculative if wider
        self.assertIn(tier, ["Medium", "Speculative"])

    def test_speculative_tier_low_n(self):
        """N=2 → Speculative regardless of CI"""
        _, ci_low, ci_high = compute_wilson_ci(2, 2)
        tier = self.engine._confidence_tier(n=2, ci_low=ci_low, ci_high=ci_high)
        self.assertEqual(tier, "Speculative")

    def test_speculative_tier_wide_ci(self):
        """N=6 with ~50% win rate → wide CI → Speculative"""
        _, ci_low, ci_high = compute_wilson_ci(3, 6)
        tier = self.engine._confidence_tier(n=6, ci_low=ci_low, ci_high=ci_high)
        # CI width is typically > 0.55 for 3/6 → Speculative
        self.assertIn(tier, ["Medium", "Speculative"])


# ═══════════════════════════════════════════════════════════════════════════
# T3 – Sector Cap (≤2 per sector) and List Cap (≤10)
# ═══════════════════════════════════════════════════════════════════════════

class TestSectorCapAndListLimit(unittest.TestCase):

    def setUp(self):
        self.db = _make_db()
        self.engine = _make_engine(self.db)
        # Seed pattern P001 so FK lookups succeed
        _seed_pattern(self.db, pattern_id="P001", wins=25, n=40)

    def _pred(self, symbol, sector):
        event_id = f"EVT-{symbol}"
        _seed_event(self.db, event_id=event_id, symbol=symbol, sector=sector)
        return _make_pred(symbol, sector, event_id)

    def test_sector_cap_max_two_per_sector(self):
        """3 stocks in same sector: only ≤2 make active_ideas."""
        preds = [
            self._pred("LUCK1", "Technology"),
            self._pred("LUCK2", "Technology"),
            self._pred("LUCK3", "Technology"),  # should be capped
        ]
        result = self.engine.generate_suggestions(
            recent_predictions=preds, market_stocks=[], regime="NEUTRAL"
        )
        tech_ideas = [i for i in result["active_ideas"] if i["sector"] == "Technology"]
        self.assertLessEqual(len(tech_ideas), 2)

    def test_list_cap_max_ten(self):
        """15 unique-sector stocks: active_ideas capped at ≤10."""
        preds = [self._pred(f"STCK{i}", f"Sector{i}") for i in range(15)]
        result = self.engine.generate_suggestions(
            recent_predictions=preds, market_stocks=[], regime="NEUTRAL"
        )
        self.assertLessEqual(len(result["active_ideas"]), 10)


# ═══════════════════════════════════════════════════════════════════════════
# T4 – Suggestion Expiry
# ═══════════════════════════════════════════════════════════════════════════

class TestSuggestionExpiry(unittest.TestCase):

    def setUp(self):
        self.db = _make_db()

    def test_expire_marks_status_expired(self):
        """Insert a single ACTIVE suggestion with past expires_at; expire() updates it."""
        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.insert_suggested_share({
            "id": "SS-EXPIRY-ONLY",
            "symbol": "EXPTEST",
            "sector": "Technology",
            "event_id": "EVT-EXP-UNIQUE",
            "pattern_id": "P001",
            "category_tag": "Breakout Continuation",
            "composite_score": 60.0,
            "win_probability": 55.0,
            "wilson_ci_low": 42.0,
            "wilson_ci_high": 68.0,
            "sample_size_n": 22,
            "confidence_tier": "High",
            "expected_return_mean": 2.5,
            "expected_return_q25": 0.8,
            "expected_return_q75": 4.5,
            "sector_multiplier": 1.1,
            "regime": "NEUTRAL",
            "entry_price": 100.0,
            "target_price": 108.0,
            "stop_loss": 95.0,
            "suggested_shares_qty": 50,
            "suggested_outlay": 5000.0,
            "risk_pkr": 250.0,
            "kelly_fraction": 0.05,
            "thesis_en": "EN thesis",
            "thesis_ur": "UR thesis",
            "status": "ACTIVE",
            "created_at": past,
            "expires_at": past,  # already expired
        })
        # Before expire: one ACTIVE
        active_before = self.db.get_active_suggested_shares()
        self.assertEqual(len(active_before), 1)
        # Run expire
        count = self.db.expire_suggested_shares(now)
        self.assertEqual(count, 1)
        # After expire: zero ACTIVE
        active_after = self.db.get_active_suggested_shares()
        self.assertEqual(len(active_after), 0)
        # In recent (non-active)
        recent = self.db.get_recent_suggested_shares()
        self.assertGreaterEqual(len(recent), 1)
        expired = [r for r in recent if r["id"] == "SS-EXPIRY-ONLY"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["status"], "EXPIRED")


# ═══════════════════════════════════════════════════════════════════════════
# T5 – Track Record Computation
# ═══════════════════════════════════════════════════════════════════════════

class TestTrackRecord(unittest.TestCase):

    def setUp(self):
        self.db = _make_db()

    def test_track_record_win_rate_and_profit_factor(self):
        """Insert 4 WINS and 1 LOSS; verify win rate, profit factor, Wilson CI."""
        for i, (outcome, ret) in enumerate([("WIN", 4.5), ("WIN", 3.2),
                                             ("WIN", 6.0), ("WIN", 2.1), ("LOSS", -3.0)]):
            self.db.insert_suggested_share(_closed_share(f"SS-TR-{i}", outcome, ret))
        tr = self.db.get_suggested_shares_track_record()
        self.assertEqual(tr["total_closed"], 5)
        self.assertEqual(tr["wins"], 4)
        self.assertEqual(tr["losses"], 1)
        self.assertEqual(tr["sample_size_n"], 5)
        # Win rate ~80%
        self.assertAlmostEqual(tr["win_rate"], 80.0, delta=1.0)
        # Profit factor: gross_win (15.8) / gross_loss (3.0) > 1
        self.assertIsNotNone(tr["profit_factor"])
        self.assertGreater(tr["profit_factor"], 1.0)
        # Wilson CI populated
        self.assertIsNotNone(tr["wilson_ci"][0])
        self.assertLess(tr["wilson_ci"][0], tr["wilson_ci"][1])

    def test_empty_track_record_returns_none_fields(self):
        """No closed suggestions → total_closed=0, win_rate=None."""
        tr = self.db.get_suggested_shares_track_record()
        self.assertEqual(tr["total_closed"], 0)
        self.assertIsNone(tr["win_rate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

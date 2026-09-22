"""
test_psx_intelligence_stage2.py
Unit and synthetic test suite for PSX Intelligence Engine Stage 2:
- 2.1 Purging of overlapping 5-day evaluation windows
- 2.1 Embargo of 2-day cooldown period for same pattern
- 2.2 Asymmetric Trader PnL (+1.0 win, -1.5 drawdown stopout, 0.0 scratched)
- 2.3 Benchmark comparison vs. KSE-100 (excess return > 0, win rate vs. KSE)
- 2.4 End-to-end evaluation & audit integration
"""

import math
import tempfile
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from psx_intelligence_engine import (
    IntelligenceDB,
    LearningEngine,
    PatternLibrary,
    compute_wilson_ci,
    EVENT_RESISTANCE_BREAK,
    EVENT_VOLUME_SURGE,
    _now
)


def test_purging_overlapping_windows():
    """Verify that predictions sharing trading days with an active 5-day window are PURGED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = IntelligenceDB(db_path=Path(tmpdir) / "test_intel.db")
        learning_engine = LearningEngine(db)

        # Base timestamp
        t0 = "2026-09-01T10:00:00Z"
        t0_ts = datetime.strptime(t0.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
        prior_window_end = t0_ts + 5 * 86400
        prior_embargo_end = prior_window_end + 2 * 86400

        # Prediction 2 days after t0 (within active 5-day window)
        t_overlapping = "2026-09-03T10:00:00Z"
        flag, reason = learning_engine.check_purging_and_embargo(
            symbol="OGDC",
            pattern_id="P001",
            predicted_at=t_overlapping,
            prior_window_end_ts=prior_window_end,
            prior_embargo_end_ts=prior_embargo_end,
            prior_pattern_id="P001"
        )
        assert flag == "PURGED", f"Expected PURGED for overlapping window, got {flag}"
        print(f"✓ Purging filter: Overlapping window correctly flagged as {flag} ('{reason}')")


def test_embargo_cooldown_period():
    """Verify that signals within 2 days post-evaluation for same ticker+pattern are EMBARGOED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = IntelligenceDB(db_path=Path(tmpdir) / "test_intel.db")
        learning_engine = LearningEngine(db)

        # Prior window: Sep 1 to Sep 6. Embargo: Sep 6 to Sep 8.
        t0 = "2026-09-01T10:00:00Z"
        t0_ts = datetime.strptime(t0.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
        prior_window_end = t0_ts + 5 * 86400
        prior_embargo_end = prior_window_end + 2 * 86400

        # Signal at Sep 7 (Day 6, inside 2-day embargo) with SAME pattern P001
        t_embargoed = "2026-09-07T10:00:00Z"
        flag, reason = learning_engine.check_purging_and_embargo(
            symbol="OGDC",
            pattern_id="P001",
            predicted_at=t_embargoed,
            prior_window_end_ts=prior_window_end,
            prior_embargo_end_ts=prior_embargo_end,
            prior_pattern_id="P001"
        )
        assert flag == "EMBARGOED", f"Expected EMBARGOED, got {flag}"

        # Signal at Sep 7 with DIFFERENT pattern P002 (should be VALID)
        flag_diff, _ = learning_engine.check_purging_and_embargo(
            symbol="OGDC",
            pattern_id="P002",
            predicted_at=t_embargoed,
            prior_window_end_ts=prior_window_end,
            prior_embargo_end_ts=prior_embargo_end,
            prior_pattern_id="P001"
        )
        assert flag_diff == "VALID", f"Expected VALID for different pattern, got {flag_diff}"

        # Signal at Sep 9 (Day 8, after embargo period expired) -> VALID
        t_valid = "2026-09-09T10:00:00Z"
        flag_clean, _ = learning_engine.check_purging_and_embargo(
            symbol="OGDC",
            pattern_id="P001",
            predicted_at=t_valid,
            prior_window_end_ts=prior_window_end,
            prior_embargo_end_ts=prior_embargo_end,
            prior_pattern_id="P001"
        )
        assert flag_clean == "VALID", f"Expected VALID after embargo expiry, got {flag_clean}"
        print(f"✓ Embargo filter: Cooldown period correctly enforces EMBARGOED vs VALID")


def test_asymmetric_trader_pnl():
    """Verify asymmetric reward/penalty (+1.0 win, -1.5 drawdown stopout, 0.0 scratched)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = IntelligenceDB(db_path=Path(tmpdir) / "test_intel.db")
        learning_engine = LearningEngine(db)

        # Case 1: Standard Win (ret > 2%, max dd < 1.5%)
        out, pnl = learning_engine.compute_asymmetric_pnl(actual_return_5d=3.5, max_drawdown_5d=1.0)
        assert out == "CORRECT" and pnl == 1.0, f"Expected CORRECT (+1.0), got {out} ({pnl})"

        # Case 2: Drawdown Stopout (ret > 2%, BUT max dd >= 3.0%)
        # Real trader would have been stopped out even if price recovered to +5.0%
        out_stopped, pnl_stopped = learning_engine.compute_asymmetric_pnl(actual_return_5d=5.0, max_drawdown_5d=3.2)
        assert out_stopped == "INCORRECT" and pnl_stopped == -1.5, f"Drawdown stopout must be INCORRECT (-1.5), got {out_stopped} ({pnl_stopped})"

        # Case 3: Flat / In-between (ret 1.2%, max dd 1.0%)
        out_scratch, pnl_scratch = learning_engine.compute_asymmetric_pnl(actual_return_5d=1.2, max_drawdown_5d=1.0)
        assert out_scratch == "NEUTRAL" and pnl_scratch == 0.0, f"Expected NEUTRAL (0.0), got {out_scratch} ({pnl_scratch})"

        # Case 4: Severe Loss (ret -4.0%, max dd 4.5%)
        out_loss, pnl_loss = learning_engine.compute_asymmetric_pnl(actual_return_5d=-4.0, max_drawdown_5d=4.5)
        assert out_loss == "INCORRECT" and pnl_loss == -1.5, f"Expected INCORRECT (-1.5), got {out_loss} ({pnl_loss})"

        print(f"✓ Asymmetric Trader PnL: Win (+1.0), Stopout Loss (-1.5), Scratch (0.0) verified")


def test_benchmark_kse_comparison():
    """Verify comparison against KSE-100 index return over 5-day window."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = IntelligenceDB(db_path=Path(tmpdir) / "test_intel.db")
        learning_engine = LearningEngine(db)

        # Mock candles for stock and KSE100
        stock_candles = [
            {"date": "2026-09-01", "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.0},
            {"date": "2026-09-02", "open": 101.0, "high": 103.0, "low": 100.5, "close": 102.5},
            {"date": "2026-09-03", "open": 102.5, "high": 104.0, "low": 102.0, "close": 103.0},
            {"date": "2026-09-04", "open": 103.0, "high": 105.0, "low": 102.8, "close": 104.0},
            {"date": "2026-09-05", "open": 104.0, "high": 106.0, "low": 103.5, "close": 105.0},
        ]
        kse_candles = [
            {"date": "2026-09-01", "open": 80000.0, "high": 80200.0, "low": 79800.0, "close": 80100.0},
            {"date": "2026-09-05", "open": 80500.0, "high": 81000.0, "low": 80400.0, "close": 80800.0}
        ]

        def mock_history(sym):
            if sym == "TEST": return stock_candles
            if sym == "KSE100": return kse_candles
            return []

        exit_p, ret_5d, dd_5d, w_candles = learning_engine._extract_5d_window(
            stock_candles, "2026-09-01T10:00:00Z", entry_price=100.0, current_price=105.0
        )
        assert exit_p == 105.0
        assert ret_5d == 5.0
        assert dd_5d == 0.5  # lowest low 99.5 on day 1 -> 0.5% drawdown

        kse_ret = learning_engine._get_kse_window_return(
            "2026-09-01", "2026-09-05", history_fn=mock_history
        )
        assert kse_ret == 1.0, f"Expected KSE return 1.0%, got {kse_ret}"

        excess_return = round(ret_5d - kse_ret, 2)
        assert excess_return == 4.0, f"Expected excess return 4.0%, got {excess_return}"
        beat_kse = 1 if excess_return > 0 else 0
        assert beat_kse == 1, "Stock beat KSE100"

        # Check Wilson interval calculation
        rate, lo, hi = compute_wilson_ci(k=65, n=100)
        assert rate == 65.0
        assert 55.0 <= lo <= 60.0
        assert 70.0 <= hi <= 75.0
        print(f"✓ Benchmark vs KSE-100: Excess return = +{excess_return}%, Wilson CI = [{lo}%, {hi}%]")


def test_end_to_end_audit_and_purge():
    """Verify full audit sweep accurately purges overlapping predictions and populates metrics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = IntelligenceDB(db_path=Path(tmpdir) / "test_intel.db")
        learning_engine = LearningEngine(db)
        pat_lib = PatternLibrary(db)
        pat_lib.rebuild()

        # Insert 3 events & predictions for same symbol:
        # Event 1: Sep 1 -> Valid
        # Event 2: Sep 3 -> Overlapping (Purged)
        # Event 3: Sep 10 -> Valid
        events = [
            {"id": "EVT_1", "symbol": "TEST", "event_type": EVENT_RESISTANCE_BREAK, "price": 100.0,
             "detected_at": "2026-09-01T10:00:00Z", "change_pct": 2.5},
            {"id": "EVT_2", "symbol": "TEST", "event_type": EVENT_VOLUME_SURGE, "price": 102.0,
             "detected_at": "2026-09-03T10:00:00Z", "change_pct": 1.5},
            {"id": "EVT_3", "symbol": "TEST", "event_type": EVENT_RESISTANCE_BREAK, "price": 106.0,
             "detected_at": "2026-09-10T10:00:00Z", "change_pct": 3.0}
        ]
        for e in events:
            db.insert_event(e)

        preds = [
            {"id": "PRED_1", "symbol": "TEST", "event_id": "EVT_1", "pattern_id": "P001",
             "signal": "POSSIBLE_BREAKOUT", "confidence": 75, "price_at_signal": 100.0,
             "predicted_at": "2026-09-01T10:00:00Z", "outcome": "PENDING"},
            {"id": "PRED_2", "symbol": "TEST", "event_id": "EVT_2", "pattern_id": "P001",
             "signal": "POSSIBLE_BREAKOUT", "confidence": 70, "price_at_signal": 102.0,
             "predicted_at": "2026-09-03T10:00:00Z", "outcome": "PENDING"},
            {"id": "PRED_3", "symbol": "TEST", "event_id": "EVT_3", "pattern_id": "P001",
             "signal": "POSSIBLE_BREAKOUT", "confidence": 80, "price_at_signal": 106.0,
             "predicted_at": "2026-09-10T10:00:00Z", "outcome": "PENDING"}
        ]
        for p in preds:
            db.insert_prediction(p)

        audit_res = learning_engine.audit_and_purge_all()
        assert audit_res["total_processed"] == 3
        assert audit_res["valid_count"] == 2, f"Expected 2 valid samples, got {audit_res['valid_count']}"
        assert audit_res["purged_count"] == 1, f"Expected 1 purged sample, got {audit_res['purged_count']}"

        stats = db.get_learning_stats()
        assert stats["purged_predictions_count"] == 1
        assert stats["valid_predictions_count"] == 2
        print(f"✓ End-to-end audit: Valid={stats['valid_predictions_count']}, Purged={stats['purged_predictions_count']}")


if __name__ == "__main__":
    test_purging_overlapping_windows()
    test_embargo_cooldown_period()
    test_asymmetric_trader_pnl()
    test_benchmark_kse_comparison()
    test_end_to_end_audit_and_purge()
    print("\n✅ All Stage 2 tests passed successfully!")

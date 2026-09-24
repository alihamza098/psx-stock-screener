#!/usr/bin/env python3
"""
Unit Tests for PSX Today's Opportunities (Same-Day Trade Scanner)
=================================================================
Tests:
- Stage 1: Tier Classifier (Steady, Active, Momentum, transitions, circuit room)
- Stage 2: Setup Detectors (ORB, Gap-and-Go, VWAP Reclaim, Circuit Runner, Oversold Bounce)
- Stage 3: Circuit Achievability Gate (clamping, dropping unreachable targets)
- Stage 4: Lifecycle State Machine (transitions, watch expiry, forced time-exit, startup reconciliation)
- Stage 5: Position Sizing & Regime Risk Multiplier
- Stage 7: Outcome Tracking & Wilson 95% Confidence Intervals
"""

import os
import shutil
import unittest
import datetime
from pathlib import Path

import psx_calendar
import shared_trading_utils
import psx_daily_opportunities
from psx_daily_opportunities import (
    DailyOpportunitiesDB,
    TierClassifier,
    SetupDetectors,
    CircuitAchievabilityGate,
    LifecycleStateMachine,
    PositionRiskManager,
    OutcomeTracker,
    DailyOpportunitiesScanner
)

TEST_DB_PATH = Path(__file__).parent / "cache" / "test_daily_opportunities.db"


class TestDailyOpportunities(unittest.TestCase):

    def setUp(self):
        if TEST_DB_PATH.exists():
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass
        self.db = DailyOpportunitiesDB(TEST_DB_PATH)
        self.config = psx_daily_opportunities.load_daily_config()

    def tearDown(self):
        if TEST_DB_PATH.exists():
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    # ── Stage 1: Tier Classifier Tests ─────────────────────────────────────────

    def test_tier_classifier_boundaries(self):
        classifier = TierClassifier(self.db, self.config)
        sched = {"elapsed_minutes": 120, "total_session_minutes": 358}

        # 1. Steady: Low RVOL (1.1x), normal ATR (2.0%)
        t_steady = classifier.classify_tier(
            symbol="TEST_STEADY",
            current_price=100.0,
            change_pct=1.0,
            today_volume=55000,
            avg_volume_21d=100000,  # 55k / 50k expected = 1.1x RVOL
            atr14=2.0,  # ATR% = 2.0%
            has_circuit_anomaly=False,
            schedule=sched
        )
        self.assertEqual(t_steady["tier"], "Steady")
        self.assertEqual(t_steady["target_pct"], 3.0)

        # 2. Active: Medium RVOL (2.0x)
        t_active = classifier.classify_tier(
            symbol="TEST_ACTIVE",
            current_price=100.0,
            change_pct=2.0,
            today_volume=100000,
            avg_volume_21d=100000,  # 100k / 50k = 2.0x RVOL
            atr14=2.0,
            has_circuit_anomaly=False,
            schedule=sched
        )
        self.assertEqual(t_active["tier"], "Active")
        self.assertEqual(t_active["target_pct"], 5.0)

        # 3. Momentum: High RVOL (3.5x) and good circuit room
        t_mom = classifier.classify_tier(
            symbol="TEST_MOM",
            current_price=100.0,
            change_pct=3.0,
            today_volume=175000,
            avg_volume_21d=100000,  # 175k / 50k = 3.5x RVOL
            atr14=4.2,
            has_circuit_anomaly=False,
            schedule=sched
        )
        self.assertEqual(t_mom["tier"], "Momentum")
        self.assertIsNotNone(t_mom["execution_risk_note"])

        # 4. Momentum Edge Case: High RVOL but circuit room < 1.5% (change = +6.5%, room = 1.0%)
        t_edge = classifier.classify_tier(
            symbol="TEST_EDGE",
            current_price=100.0,
            change_pct=6.5,  # room = 7.5 - 6.5 = 1.0% < 1.5%
            today_volume=175000,
            avg_volume_21d=100000,
            atr14=4.2,
            has_circuit_anomaly=False,
            schedule=sched
        )
        # Cannot be Momentum because room < 1.5%
        self.assertNotEqual(t_edge["tier"], "Momentum")

    def test_tier_transition_logging(self):
        classifier = TierClassifier(self.db, self.config)
        sched = {"elapsed_minutes": 120, "total_session_minutes": 358}

        # Step 1: Stock starts Steady
        classifier.classify_tier("TRG", 100.0, 0.5, 50000, 100000, 2.0, False, sched)
        # Step 2: Surges to Momentum
        classifier.classify_tier("TRG", 104.0, 4.0, 180000, 100000, 4.5, False, sched)

        with self.db._get_conn() as conn:
            row = conn.execute("SELECT * FROM tier_transitions WHERE symbol = 'TRG'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["from_tier"], "Steady")
            self.assertEqual(row["to_tier"], "Momentum")

    # ── Stage 2: Setup Detectors Tests ─────────────────────────────────────────

    def test_setup_detectors(self):
        detectors = SetupDetectors(self.db, self.config)
        tier_active = {"tier": "Active", "rvol_today": 2.0}
        tier_mom = {"tier": "Momentum", "rvol_today": 3.5, "has_circuit_anomaly": True}
        sched = {"elapsed_minutes": 30, "total_session_minutes": 358}

        # 1. ORB Detector
        orb_candles = [{"high": 102.0, "low": 99.0, "price": 101.0}]
        cand_orb = detectors.detect_orb("LUCK", tier_active, 103.0, 3.0, orb_candles, 2.5, sched)
        self.assertIsNotNone(cand_orb)
        self.assertEqual(cand_orb["setup_type"], "ORB")
        self.assertGreater(cand_orb["entry_price"], 102.0)

        # 2. Gap-and-Go Detector
        cand_gap = detectors.detect_gap_and_go("OGDC", tier_active, 102.5, 102.0, 100.0, 2.0, sched)
        self.assertIsNotNone(cand_gap)
        self.assertEqual(cand_gap["setup_type"], "GAP_AND_GO")

        # 3. VWAP Reclaim Detector
        cand_vwap = detectors.detect_vwap_reclaim("PPL", tier_active, 101.5, 101.0, 100.9, 2.0)
        self.assertIsNotNone(cand_vwap)
        self.assertEqual(cand_vwap["setup_type"], "VWAP_RECLAIM")

        # 4. Circuit Runner Detector
        cand_cr = detectors.detect_circuit_runner("UNITY", tier_mom, 106.0, 6.0, 3.0)
        self.assertIsNotNone(cand_cr)
        self.assertEqual(cand_cr["setup_type"], "CIRCUIT_RUNNER")
        self.assertIn("Execution Risk", cand_cr["execution_risk_note"])

        # 5. Oversold Bounce Detector
        pivots = {"pivot": 102.0, "s1": 98.0, "s2": 96.0}
        cand_ob = detectors.detect_oversold_bounce("SYS", tier_active, 98.2, 28.0, pivots, 2.0)
        self.assertIsNotNone(cand_ob)
        self.assertEqual(cand_ob["setup_type"], "OVERSOLD_BOUNCE")

    # ── Stage 3: Circuit Achievability Gate Tests ───────────────────────────────

    def test_circuit_achievability_gate(self):
        gate = CircuitAchievabilityGate(self.config)

        # Candidate with raw target 110.0, but upper circuit is at 107.5 (7.5%)
        cand = {"entry_price": 100.0, "raw_target": 110.0, "stop_loss": 98.0}
        clamped = gate.process_candidate(cand, current_price=100.0, change_pct=0.0, circuit_limit_pct=7.5)
        self.assertIsNotNone(clamped)
        # Target must be clamped to upper circuit minus 0.3% buffer (~107.18)
        self.assertLessEqual(clamped["target_price"], 107.5 * 0.998)

        # Candidate with only 0.8% room to circuit (change = +6.7%) -> Must be DROPPED
        dropped = gate.process_candidate(cand, current_price=106.7, change_pct=6.7, circuit_limit_pct=7.5)
        self.assertIsNone(dropped)

    # ── Stage 4: Lifecycle State Machine & Time-Exit Tests ─────────────────────

    def test_lifecycle_transitions_and_time_exit(self):
        sm = LifecycleStateMachine(self.db, self.config)
        sched = {
            "time_exit_mins": 915,  # 15:15 PKT
            "time_exit_cutoff_str": "15:15 PKT",
            "is_in_trading_hours": True
        }

        # Save a WATCHING candidate
        cid = self.db.save_candidate({
            "symbol": "HUBC",
            "tier": "Active",
            "setup_type": "VWAP_RECLAIM",
            "state": "WATCHING",
            "entry_price": 100.0,
            "target_price": 105.0,
            "stop_loss": 98.0,
            "shares": 500,
            "detected_at": "2026-09-22 10:00:00"
        })

        cand = dict(self.db.get_all_today()[0])

        # 1. Triggers when price crosses entry
        dt_10am = datetime.datetime(2026, 9, 22, 10, 30, tzinfo=psx_calendar.PKT_TIMEZONE)
        c_trig = sm.update_open_candidate(cand, 100.5, sched, dt_10am)
        self.assertEqual(c_trig["state"], "TRIGGERED")

        # 2. Target Hit
        c_target = sm.update_open_candidate(dict(c_trig), 105.5, sched, dt_10am)
        self.assertEqual(c_target["state"], "CLOSED")
        self.assertEqual(c_target["exit_type"], "TARGET_HIT")
        self.assertGreater(c_target["pnl_pct"], 0)

        # 3. Test Forced Time-Exit at 15:16 PKT (past 15:15 cutoff)
        cid2 = self.db.save_candidate({
            "symbol": "FFC",
            "tier": "Active",
            "setup_type": "ORB",
            "state": "TRIGGERED",
            "entry_price": 100.0,
            "target_price": 106.0,
            "stop_loss": 97.0,
            "shares": 300,
            "detected_at": "2026-09-22 11:00:00"
        })
        cand2 = next(c for c in self.db.get_all_today() if c["symbol"] == "FFC")
        dt_late = datetime.datetime(2026, 9, 22, 15, 16, tzinfo=psx_calendar.PKT_TIMEZONE)
        c_late = sm.update_open_candidate(cand2, 101.0, sched, dt_late)
        self.assertEqual(c_late["state"], "CLOSED")
        self.assertEqual(c_late["exit_type"], "TIME_EXIT")
        self.assertIn("Forced end-of-day time exit", c_late["exit_notes"])

    def test_startup_reconciliation(self):
        sm = LifecycleStateMachine(self.db, self.config)

        # Candidate left TRIGGERED during server downtime
        self.db.save_candidate({
            "date": "2026-09-22",
            "symbol": "EFERT",
            "tier": "Steady",
            "setup_type": "OVERSOLD_BOUNCE",
            "state": "TRIGGERED",
            "entry_price": 100.0,
            "target_price": 103.0,
            "stop_loss": 98.0,
            "shares": 200,
            "detected_at": "2026-09-22 10:00:00"
        })

        # Run startup reconciliation after market close / downtime
        sm.reconcile_on_startup(stocks_cache={"EFERT": {"price": 101.5}})

        cands = self.db.get_all_today("2026-09-22")
        efert = next(c for c in cands if c["symbol"] == "EFERT")
        self.assertEqual(efert["state"], "CLOSED")
        self.assertEqual(efert["exit_type"], "TIME_EXIT")
        self.assertIn("downtime", efert["exit_notes"])

    # ── Stage 5: Position Sizing & Regime Risk Multiplier Tests ─────────────────

    def test_position_sizing_and_regime_scaling(self):
        prm = PositionRiskManager(self.config)

        # Standard Neutral market regime: 1.0% risk on 500k = 5,000 PKR
        s_neutral = prm.calculate_sizing(entry=100.0, stop=98.0, market_regime="Neutral")
        # Per share risk = 2.0 PKR. Shares = 5,000 / 2.0 = 2,500 shares
        self.assertEqual(s_neutral["shares"], 2500)
        self.assertEqual(s_neutral["risk_pct_used"], 1.0)

        # Bearish / Crash regime: risk halved to 0.5% (2,500 PKR) -> 1,250 shares
        s_bear = prm.calculate_sizing(entry=100.0, stop=98.0, market_regime="Bearish Trend")
        self.assertEqual(s_bear["shares"], 1250)
        self.assertEqual(s_bear["risk_pct_used"], 0.5)

    # ── Stage 7: Outcome Tracking & Wilson Score Interval Tests ─────────────────

    def test_outcome_tracking_wilson_ci(self):
        # Insert test closed candidates
        for i in range(6):
            self.db.save_candidate({
                "symbol": f"WIN_{i}",
                "tier": "Active",
                "setup_type": "ORB",
                "state": "CLOSED",
                "exit_type": "TARGET_HIT",
                "entry_price": 100.0,
                "target_price": 105.0,
                "exit_price": 105.0,
                "pnl_pct": 5.0,
                "duration_minutes": 45,
                "detected_at": "2026-09-22 10:00:00"
            })
        for i in range(2):
            self.db.save_candidate({
                "symbol": f"LOSS_{i}",
                "tier": "Active",
                "setup_type": "ORB",
                "state": "CLOSED",
                "exit_type": "STOPPED_OUT",
                "entry_price": 100.0,
                "target_price": 105.0,
                "exit_price": 98.0,
                "pnl_pct": -2.0,
                "duration_minutes": 30,
                "detected_at": "2026-09-22 10:00:00"
            })

        tracker = OutcomeTracker(self.db, self.config)
        res = tracker.get_track_record()
        self.assertEqual(res["total_closed_trades"], 8)
        self.assertTrue(len(res["breakdown"]) >= 1)

        orb_stat = next(b for b in res["breakdown"] if b["tier"] == "Active" and b["setup_type"] == "ORB")
        self.assertEqual(orb_stat["total_trades"], 8)
        self.assertEqual(orb_stat["wins"], 6)
        self.assertEqual(orb_stat["losses"], 2)
        self.assertEqual(orb_stat["win_rate_pct"], 75.0)
        self.assertTrue(orb_stat["is_statistically_valid"])  # 8 >= 5
        # Wilson interval must be strictly between 0 and 100 and surround 75%
        self.assertLess(orb_stat["ci_95_lower"], 75.0)
        self.assertGreater(orb_stat["ci_95_upper"], 75.0)


if __name__ == "__main__":
    unittest.main()

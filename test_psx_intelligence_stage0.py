#!/usr/bin/env python3
"""
Unit Tests for Stage 0: PSX Market Intelligence Engine Hygiene & Robust Foundations
- 0.2: Robust Statistics (Median and MAD) vs Outlier-skewed Mean/Stdev
- 0.3: Persistence Filter across 5-minute ticks (noise reduction)
- 0.4: False-Discovery / Excess-Return Control over Sector and KSE-100
- 0.5: As-of Data Discipline (intraday tick appended to historical candles)
- 0.6: Liquidity Awareness Tagging (suppress thin scrip noise)
"""

import unittest
import math
import json
import sqlite3
from pathlib import Path
from psx_intelligence_engine import (
    compute_median_mad,
    get_intelligence_config,
    IntelligenceDB,
    StockMemoryBuilder,
    AnomalyDetector,
    CauseInvestigator,
    PredictionEngine,
    EVENT_PRICE_SPIKE,
    EVENT_VOLUME_SURGE,
    EVENT_UPPER_LOCK,
    EVENT_ACCUMULATION
)

class TestStage0Hygiene(unittest.TestCase):

    def setUp(self):
        self.test_db_path = Path("cache/test_intelligence_stage0.db")
        if self.test_db_path.exists():
            self.test_db_path.unlink()
        self.db = IntelligenceDB(db_path=self.test_db_path)
        self.investigator = CauseInvestigator(self.db)
        self.prediction_engine = PredictionEngine(self.db, None, self.investigator)
        self.detector = AnomalyDetector(self.db, self.investigator, self.prediction_engine)

    def tearDown(self):
        if self.test_db_path.exists():
            try:
                self.test_db_path.unlink()
            except Exception:
                pass

    def test_compute_median_mad_known_synthetic(self):
        # Odd-length sequence: [1, 2, 3, 4, 5, 6, 7, 8, 9]
        # median = 5.0
        # deviations: [4, 3, 2, 1, 0, 1, 2, 3, 4] -> sorted: [0, 1, 1, 2, 2, 3, 3, 4, 4]
        # raw MAD = 2.0
        # normalized MAD = 2.0 * 1.4826 = 2.9652
        data_odd = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        med, mad = compute_median_mad(data_odd, normalizer=1.4826)
        self.assertEqual(med, 5.0)
        self.assertAlmostEqual(mad, 2.9652, places=4)

        # Even-length sequence: [2, 4, 6, 8]
        # median = (4 + 6) / 2 = 5.0
        # deviations: [3, 1, 1, 3] -> sorted: [1, 1, 3, 3] -> median = 2.0
        data_even = [2.0, 4.0, 6.0, 8.0]
        med_e, mad_e = compute_median_mad(data_even, normalizer=1.0)
        self.assertEqual(med_e, 5.0)
        self.assertEqual(mad_e, 2.0)

    def test_robustness_against_extreme_outlier_spike(self):
        # 19 days of 0 volume, 1 day of 5,000,000 shares
        thin_history = [0.0] * 19 + [5000000.0]
        arithmetic_mean = sum(thin_history) / len(thin_history)
        med_vol, mad_vol = compute_median_mad(thin_history)

        self.assertEqual(arithmetic_mean, 250000.0)
        # Median is correctly 0.0 and MAD is 0.0
        self.assertEqual(med_vol, 0.0)
        self.assertEqual(mad_vol, 0.0)

    def test_config_loading_and_externalized_values(self):
        cfg = get_intelligence_config()
        self.assertIn("anomaly_detector", cfg)
        self.assertIn("baseline_memory", cfg)
        self.assertIn("false_discovery", cfg)
        self.assertEqual(cfg["baseline_memory"]["rolling_window_sessions"], 90)
        self.assertEqual(cfg["anomaly_detector"]["persistence_ticks"]["PRICE_SPIKE"], 2)
        self.assertEqual(cfg["anomaly_detector"]["persistence_ticks"]["UPPER_LOCK"], 1)

    def test_persistence_filter_two_ticks_required_for_price_spike(self):
        # Setup stock memory with baseline
        self.db.upsert_stock_memory({
            "symbol": "HUBC",
            "sector": "Power Generation",
            "avg_daily_volume": 500000,
            "median_daily_volume": 500000,
            "mad_daily_volume": 100000,
            "is_liquid": 1
        })

        stock_snapshot = {
            "symbol": "HUBC",
            "price": 120.0,
            "change": 5.0,  # 5% price spike (> 4.5%)
            "volume": 600000.0,
            "sector": "Power Generation"
        }

        # Tick 1: Candidate price spike should be held in persistence buffer, NOT fired
        current_candidates = set()
        ev1 = self.detector._check_stock(
            stock=stock_snapshot,
            kse_change=0.5,
            sector_avg={"Power Generation": 0.5},
            history_fn=None,
            current_tick_candidates=current_candidates
        )
        self.assertIsNone(ev1, "Tick 1 must not trigger event due to 2-tick persistence filter")
        self.assertEqual(self.detector._candidate_ticks.get("HUBC", {}).get(EVENT_PRICE_SPIKE), 1)

        # Tick 2: Condition continues -> Confirmed!
        ev2 = self.detector._check_stock(
            stock=stock_snapshot,
            kse_change=0.5,
            sector_avg={"Power Generation": 0.5},
            history_fn=None,
            current_tick_candidates=current_candidates
        )
        self.assertIsNotNone(ev2, "Tick 2 must confirm and trigger event after 2 consecutive ticks")

    def test_circuit_locks_bypass_persistence_filter(self):
        # Upper locks are unambiguous events that fire on single tick (persistence = 1)
        self.db.upsert_stock_memory({
            "symbol": "PRL",
            "sector": "Refinery",
            "avg_daily_volume": 1000000,
            "median_daily_volume": 1000000,
            "is_liquid": 1
        })
        lock_stock = {
            "symbol": "PRL",
            "price": 30.0,
            "change": 9.9, # Upper circuit lock
            "volume": 2000000.0,
            "sector": "Refinery"
        }
        current_candidates = set()
        ev = self.detector._check_stock(
            stock=lock_stock,
            kse_change=1.0,
            sector_avg={"Refinery": 1.0},
            history_fn=None,
            current_tick_candidates=current_candidates
        )
        self.assertIsNotNone(ev, "Upper lock must fire immediately on single tick")

    def test_false_discovery_control_filters_broad_beta_moves(self):
        # Stock up 4.8%, but entire sector is up 4.2% and KSE is up 3.8%
        # Excess over sector = 0.6% (< 1.5%), Excess over KSE = 1.0% (< 2.0%)
        # Move is purely sector/market beta, not idiosyncratic alpha
        self.db.upsert_stock_memory({
            "symbol": "OGDC",
            "sector": "Oil & Gas Exploration",
            "avg_daily_volume": 2000000,
            "median_daily_volume": 2000000,
            "is_liquid": 1
        })
        beta_stock = {
            "symbol": "OGDC",
            "price": 160.0,
            "change": 4.8,
            "volume": 2500000.0,
            "sector": "Oil & Gas Exploration"
        }
        # Pre-seed candidate tick so persistence filter passes
        self.detector._candidate_ticks = {"OGDC": {EVENT_PRICE_SPIKE: 1}}

        ev = self.detector._check_stock(
            stock=beta_stock,
            kse_change=3.8,
            sector_avg={"Oil & Gas Exploration": 4.2},
            history_fn=None,
            current_tick_candidates=set()
        )
        self.assertIsNone(ev, "False discovery control should suppress move driven purely by sector/market tide")
        self.assertEqual(self.detector._false_discovery_rejected_today, 1)

    def test_liquidity_awareness_tagging(self):
        # Thin penny stock with tiny daily turnover
        self.db.upsert_stock_memory({
            "symbol": "THIN",
            "sector": "Misc",
            "avg_daily_volume": 500,
            "median_daily_volume": 200,
            "mad_daily_volume": 50,
            "is_liquid": 0 # Flagged as illiquid
        })
        stock = {
            "symbol": "THIN",
            "price": 2.0,
            "change": 6.0,
            "volume": 5000.0,
            "sector": "Misc"
        }
        # Pre-seed persistence for VOLUME_SURGE (since rvol = 25.0 >= 2.8 and change = 6.0 >= 2.0)
        self.detector._candidate_ticks = {"THIN": {EVENT_VOLUME_SURGE: 1}}

        ev_id = self.detector._check_stock(
            stock=stock,
            kse_change=0.2,
            sector_avg={"Misc": 0.2},
            history_fn=None,
            current_tick_candidates=set()
        )
        self.assertIsNotNone(ev_id)
        ev_detail = self.db.get_event_detail(ev_id)
        self.assertEqual(ev_detail["is_noisy_liquidity"], 1, "Must tag illiquid scrip as noisy liquidity")


if __name__ == "__main__":
    unittest.main()

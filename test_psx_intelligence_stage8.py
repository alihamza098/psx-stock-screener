#!/usr/bin/env python3
"""
Unit and Integration Tests for PSX Market Intelligence Engine — Stage 8 (Operational Robustness)
================================================================================================
Covers:
  - 8.1 Alert Deduplication: Simultaneous anomaly consolidation into a single event,
    recording of all tripped types, prevention of duplicate alerts later in the session.
  - 8.2 Circuit-Lock Window Analysis & High-Frequency Capture: 20-second live buffer capturing
    transient circuit locks that trigger and resolve within the 5-minute cycle.
  - 8.3 Internal Data Quality Monitoring: Scraper health tracking, baseline gap audits,
    degraded stock suppression of false-discovery volume surges, and /api/intelligence/data-quality endpoint.
"""

import os
import json
import time
import pytest
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import psx_intelligence_engine as pie


@pytest.fixture
def tmp_intel_db(tmp_path):
    """Provide an isolated, freshly initialized IntelligenceDB for Stage 8 tests."""
    db_file = tmp_path / "stage8_intel.db"
    orig_path = pie.DB_PATH
    pie.DB_PATH = db_file
    db = pie.IntelligenceDB()
    yield db
    pie.DB_PATH = orig_path


# ─── 8.1 Alert Deduplication Tests ───────────────────────────────────────────

def test_alert_deduplication_consolidates_simultaneous_anomalies(tmp_intel_db):
    """
    Test that when a stock trips both VOLUME_SURGE and PRICE_SPIKE simultaneously,
    only ONE consolidated event is created, recording both types in snapshot_json.
    """
    investigator = pie.CauseInvestigator(tmp_intel_db)
    pred_engine = pie.PredictionEngine(tmp_intel_db, None, investigator)
    pred_engine.generate_prediction = MagicMock()
    detector = pie.AnomalyDetector(tmp_intel_db, investigator, pred_engine)

    # Establish baseline in stock memory for TEST1
    tmp_intel_db.upsert_stock_memory({
        "symbol": "TEST1",
        "sector": "Technology",
        "median_daily_volume": 100000.0,
        "avg_daily_volume": 100000.0,
        "is_liquid": 1,
        "is_degraded": 0
    })

    # Snapshot: price change +5.5% (trips PRICE_SPIKE >= 4.5%), volume 500,000 (RVOL 5.0 >= 2.8, trips VOLUME_SURGE)
    stock = {
        "symbol": "TEST1",
        "price": 105.5,
        "change": 5.5,
        "volume": 500000.0,
        "sector": "Technology"
    }
    sector_avg = {"Technology": 0.5}  # Excess return = +5.0%

    # Tick 1: persistence filter requires 2 consecutive ticks for PRICE_SPIKE & VOLUME_SURGE
    ev_id_1 = detector._check_stock(stock, kse_change=0.2, sector_avg=sector_avg)
    assert ev_id_1 is None, "First tick awaits persistence confirmation"

    # Tick 2: confirmed consecutive observation -> generates consolidated event
    ev_id_2 = detector._check_stock(stock, kse_change=0.2, sector_avg=sector_avg)
    assert ev_id_2 is not None, "Consolidated event should be generated on tick 2"

    # Verify event record
    events = tmp_intel_db.get_recent_events(limit=5, symbol="TEST1")
    assert len(events) == 1, "Exactly one consolidated event must exist"

    ev = events[0]
    # Priority hierarchy: VOLUME_SURGE > PRICE_SPIKE
    assert ev["event_type"] == pie.EVENT_VOLUME_SURGE
    snapshot = json.loads(ev["snapshot_json"])

    assert "tripped_anomalies" in snapshot
    assert pie.EVENT_VOLUME_SURGE in snapshot["tripped_anomalies"]
    assert pie.EVENT_PRICE_SPIKE in snapshot["tripped_anomalies"]
    assert snapshot["is_consolidated"] is True

    # Check deduplication: third tick with same conditions must NOT create another event
    ev_id_3 = detector._check_stock(stock, kse_change=0.2, sector_avg=sector_avg)
    assert ev_id_3 is None, "Subsequent tick on same day must be deduplicated"

    # Check that individual types were also marked seen
    today = date.today().isoformat()
    assert f"TEST1_{pie.EVENT_VOLUME_SURGE}_{today}" in detector._seen_today
    assert f"TEST1_{pie.EVENT_PRICE_SPIKE}_{today}" in detector._seen_today


def test_priority_hierarchy_upper_lock_over_volume_surge(tmp_intel_db):
    """
    Test that UPPER_LOCK takes precedence over VOLUME_SURGE when both are tripped.
    """
    investigator = pie.CauseInvestigator(tmp_intel_db)
    pred_engine = pie.PredictionEngine(tmp_intel_db, None, investigator)
    pred_engine.generate_prediction = MagicMock()
    detector = pie.AnomalyDetector(tmp_intel_db, investigator, pred_engine)

    tmp_intel_db.upsert_stock_memory({
        "symbol": "LOCK1",
        "sector": "Cement",
        "median_daily_volume": 50000.0,
        "is_liquid": 1,
        "is_degraded": 0
    })

    # Pre-seed 1 tick of VOLUME_SURGE so both UPPER_LOCK (req: 1) and VOLUME_SURGE (req: 2) confirm on this tick
    detector._candidate_ticks["LOCK1"] = {"VOLUME_SURGE": 1}

    stock = {
        "symbol": "LOCK1",
        "price": 50.0,
        "change": 9.9,      # Hits UPPER_LOCK (>= 9.5%)
        "volume": 300000.0,  # Hits VOLUME_SURGE (RVOL 6.0 >= 2.8)
        "sector": "Cement"
    }

    ev_id = detector._check_stock(stock, kse_change=0.0, sector_avg={"Cement": 0.0})
    assert ev_id is not None

    events = tmp_intel_db.get_recent_events(limit=1, symbol="LOCK1")
    assert len(events) == 1
    assert events[0]["event_type"] == pie.EVENT_UPPER_LOCK
    snap = json.loads(events[0]["snapshot_json"])
    assert pie.EVENT_UPPER_LOCK in snap["tripped_anomalies"]
    assert pie.EVENT_VOLUME_SURGE in snap["tripped_anomalies"]
    assert snap["is_consolidated"] is True


# ─── 8.2 Circuit-Lock Window Analysis & High-Frequency Capture Tests ─────────

def test_transient_circuit_lock_captured_via_buffer(tmp_intel_db):
    """
    Test that a transient circuit lock observed during a 20-second tick is captured
    in the 5-minute intelligence cycle even if the price pulled back.
    """
    investigator = pie.CauseInvestigator(tmp_intel_db)
    pred_engine = pie.PredictionEngine(tmp_intel_db, None, investigator)
    pred_engine.generate_prediction = MagicMock()
    detector = pie.AnomalyDetector(tmp_intel_db, investigator, pred_engine)

    tmp_intel_db.upsert_stock_memory({
        "symbol": "TRANSIENT1",
        "sector": "Banks",
        "median_daily_volume": 200000.0,
        "is_liquid": 1,
        "is_degraded": 0
    })

    # Step 1: High-frequency observation at 10:02 AM - stock touched upper lock at +8.0%
    intermediate_stocks = [{
        "symbol": "TRANSIENT1",
        "price": 108.0,
        "change": 8.0,
        "volume": 50000.0,
        "sector": "Banks"
    }]
    detector.record_intraday_tick_observations(intermediate_stocks)

    # Verify buffered
    assert "TRANSIENT1" in detector._intraday_circuit_buffer
    assert detector._intraday_circuit_buffer["TRANSIENT1"]["type"] == pie.EVENT_UPPER_LOCK

    # Step 2: 5-minute cycle tick runs at 10:05 AM.
    # Current stock price has pulled back to +4.0% (normally not an upper lock)
    current_stock = {
        "symbol": "TRANSIENT1",
        "price": 104.0,
        "change": 4.0,
        "volume": 60000.0,
        "sector": "Banks"
    }

    ev_id = detector._check_stock(current_stock, kse_change=0.5, sector_avg={"Banks": 0.5})
    assert ev_id is not None, "Transient circuit lock should be captured"

    events = tmp_intel_db.get_recent_events(limit=1, symbol="TRANSIENT1")
    assert len(events) == 1
    assert events[0]["event_type"] == pie.EVENT_UPPER_LOCK
    snap = json.loads(events[0]["snapshot_json"])
    assert snap.get("transient_lock_resolved") is True, "Must be tagged as resolved transient lock"


# ─── 8.3 Data Quality Monitoring Tests ────────────────────────────────────────

def test_data_quality_baseline_gaps_and_suppression(tmp_intel_db):
    """
    Test that truncated stock history (<30 sessions) is flagged as degraded,
    and downstream false-positive volume surges are suppressed.
    """
    dq_monitor = pie.DataQualityMonitor(tmp_intel_db)

    # 1. Audit stock with only 12 sessions (<30 min required)
    sparse_history = [{"close": 10.0, "volume": 100, "date": f"2026-08-{i+1:02d}"} for i in range(12)]
    audit = dq_monitor.audit_stock_history("SPARSE1", sparse_history)

    assert audit["is_degraded"] == 1
    assert audit["issue_type"] == "BASELINE_GAPS"
    assert audit["severity"] == "DEGRADED"

    # Verify active issue logged in DB
    issues = tmp_intel_db.get_active_data_quality_issues(severity="DEGRADED")
    assert any(i["symbol"] == "SPARSE1" for i in issues)

    # 2. Build stock memory with degraded status
    mem_builder = pie.StockMemoryBuilder(tmp_intel_db, dq_monitor)
    mem_builder._rebuild_stock({"symbol": "SPARSE1", "price": 10.0, "volume": 100, "sector": "Other"},
                               history_fn=lambda sym: sparse_history)

    mem = tmp_intel_db.get_stock_memory("SPARSE1")
    assert mem["is_degraded"] == 1
    assert "minimum 30 required" in mem["degraded_reason"]

    # 3. Anomaly detection: sparse stock has 1,000 volume (10x baseline artifact)
    investigator = pie.CauseInvestigator(tmp_intel_db)
    pred_engine = pie.PredictionEngine(tmp_intel_db, None, investigator)
    detector = pie.AnomalyDetector(tmp_intel_db, investigator, pred_engine)

    stock_snap = {
        "symbol": "SPARSE1",
        "price": 10.2,
        "change": 2.0,
        "volume": 2000.0,
        "sector": "Other"
    }
    # Should be suppressed because baseline is degraded (protecting from false-discovery volume surge)
    ev_id = detector._check_stock(stock_snap, kse_change=0.0, sector_avg={"Other": 0.0})
    assert ev_id is None, "Volume surge on degraded baseline stock must be suppressed"


def test_data_quality_suspension_gap_and_flatline(tmp_intel_db):
    """
    Test detection of prolonged suspension gaps (>14 days) and flatline zero-volume data.
    """
    dq_monitor = pie.DataQualityMonitor(tmp_intel_db)

    # History with a 25-day gap between session 15 and session 16
    history = []
    base_date = datetime(2026, 5, 1)
    for i in range(35):
        if i < 15:
            d = base_date + timedelta(days=i)
        else:
            d = base_date + timedelta(days=i + 25)  # 25-day gap
        history.append({
            "close": 25.0 + (i * 0.1),
            "volume": 50000,
            "date": d.strftime("%Y-%m-%d")
        })

    audit = dq_monitor.audit_stock_history("GAP1", history)
    assert audit["issue_type"] == "SUSPENDED_GAP"
    assert audit["severity"] == "WARNING"

    # History with 10 identical flatline closes and 0 volume
    flat_history = [{"close": 15.0, "volume": 0, "date": f"2026-06-{i+1:02d}"} for i in range(32)]
    audit_flat = dq_monitor.audit_stock_history("FLAT1", flat_history)
    assert audit_flat["issue_type"] == "FLATLINE_DATA"
    assert audit_flat["severity"] == "WARNING"


def test_data_quality_scraper_health_and_report(tmp_intel_db):
    """
    Test scraper failure logging, consecutive failure tracking, and health report compilation.
    """
    dq_monitor = pie.DataQualityMonitor(tmp_intel_db)

    # Initial state
    dq_monitor.record_scrape_status(True)
    report1 = dq_monitor.get_health_report()
    assert report1["scraper_health"]["uptime_pct"] == 100.0
    assert report1["scraper_health"]["consecutive_failures"] == 0

    # Simulate 3 consecutive scraper failures
    dq_monitor.record_scrape_status(False, "Connection timeout to dps.psx.com.pk")
    dq_monitor.record_scrape_status(False, "503 Service Unavailable")
    dq_monitor.record_scrape_status(False, "0 stocks parsed")

    report2 = dq_monitor.get_health_report()
    assert report2["scraper_health"]["consecutive_failures"] == 3
    assert report2["issues_summary"]["critical"] >= 1
    assert "Not investment advice" in report2["disclaimer"]

    # Recovery
    dq_monitor.record_scrape_status(True)
    report3 = dq_monitor.get_health_report()
    assert report3["scraper_health"]["consecutive_failures"] == 0


def test_data_quality_api_integration(tmp_intel_db):
    """
    Test that IntelligenceEngine.get_data_quality_report() produces complete internal report.
    """
    eng = pie.IntelligenceEngine(db=tmp_intel_db)
    eng.dq_monitor = pie.DataQualityMonitor(tmp_intel_db)

    # Populate dummy memory
    tmp_intel_db.upsert_stock_memory({
        "symbol": "SYS1",
        "sector": "Tech",
        "is_degraded": 0
    })
    tmp_intel_db.upsert_stock_memory({
        "symbol": "SYS2",
        "sector": "Tech",
        "is_degraded": 1,
        "degraded_reason": "Baseline gap"
    })

    report = eng.get_data_quality_report()
    assert "baseline_health" in report
    assert "scraper_health" in report
    assert "issues_summary" in report
    assert report["baseline_health"]["total_stocks_tracked"] == 2
    assert report["baseline_health"]["degraded_stocks_count"] == 1
    assert report["baseline_health"]["coverage_pct"] == 50.0
    assert "SYS2" in report["baseline_health"]["degraded_symbols"]
    assert "disclaimer" in report

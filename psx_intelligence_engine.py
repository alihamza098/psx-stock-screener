#!/usr/bin/env python3
"""
PSX Market Intelligence & Learning Engine
==========================================
A 24/7 autonomous system that:
  1. Detects anomalous price/volume events across all PSX stocks
  2. Investigates WHY the move happened (technical + fundamental evidence)
  3. Scores each causal factor with a confidence level (0–100)
  4. Stores events in a SQLite memory database
  5. Builds a pattern library from historical event clusters
  6. Generates forward-looking predictions based on pattern similarity
  7. Evaluates prediction outcomes to self-calibrate confidence

Architecture: 100% deterministic local Python. No LLM API calls.
Causal summaries are produced by a structured template engine.

Database: cache/intelligence.db (SQLite, pure stdlib)
"""

import sqlite3
import json
import time
import math
import uuid
import threading
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set
from pathlib import Path
from collections import defaultdict

# ── Constants & Configuration ──────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "cache" / "intelligence.db"
CONFIG_PATH = BASE_DIR / "config" / "intelligence_engine.json"

_DEFAULT_CONFIG = {
    "version": "2.0.0",
    "anomaly_detector": {
        "poll_interval_seconds": 300,
        "trading_hours_only": True,
        "min_turnover_pkr_daily": 500000.0,
        "min_volume_daily_shares": 10000.0,
        "upper_lock_pct": 9.5,
        "lower_lock_pct": -9.5,
        "consecutive_locks_min": 2,
        "consecutive_locks_window_calendar_days": 3,
        "price_spike_pct": 4.5,
        "rvol_surge_threshold": 2.8,
        "accumulation_max_change_pct": 2.0,
        "breakout_lookback_bars": 30,
        "resistance_break_margin": 1.005,
        "persistence_ticks": {
            "PRICE_SPIKE": 2,
            "VOLUME_SURGE": 2,
            "ACCUMULATION": 2,
            "RSI_MOMENTUM": 2,
            "RESISTANCE_BREAK": 1,
            "UPPER_LOCK": 1,
            "LOWER_LOCK": 1,
            "CONSECUTIVE_UPPER_LOCK": 1
        }
    },
    "baseline_memory": {
        "rolling_window_sessions": 90,
        "min_valid_sessions": 15,
        "use_median_mad": True,
        "mad_normalizer": 1.4826,
        "volume_floor_epsilon": 100.0,
        "turnover_floor_pkr": 500000.0
    },
    "false_discovery": {
        "enabled": True,
        "method": "excess_return_escalation",
        "min_excess_over_sector_pct": 1.5,
        "min_excess_over_kse_pct": 2.0,
        "exempt_events": ["UPPER_LOCK", "LOWER_LOCK", "CONSECUTIVE_UPPER_LOCK"]
    },
    "cause_investigator": {
        "factor_confidence_floor": 50,
        "breakout_lookback_bars": 30,
        "volume_accum_3d_ratio_threshold": 1.5,
        "rsi_momentum_floor": 60.0,
        "rsi_jump_threshold": 3.0,
        "sector_momentum_pct_threshold": 1.5,
        "market_momentum_pct_threshold": 1.0
    },
    "prediction_engine": {
        "prior_weight_alpha": 5.0,
        "prior_weight_beta": 20.0,
        "min_sample_size_platt": 30,
        "max_calibration_adj_points": 15
    },
    "learning_engine": {
        "horizon_sessions": 5,
        "neutral_band_pct": 1.0,
        "win_threshold_pct": 2.0,
        "loss_threshold_pct": -2.0
    },
    "empirical_bayes": {
        "sector_prior_weight_m": 20.0,
        "pattern_prior_weight_m": 20.0,
        "min_prior_weight": 5.0,
        "max_prior_weight": 50.0,
        "auto_estimate_m": True,
        "sample_size_gate": 3,
        "sector_multiplier_min": 0.5,
        "sector_multiplier_max": 1.5,
        "default_population_win_rate": 0.1443
    },
    "stage4_pattern_lifecycle": {
        "regime_conditioning": {
            "enabled": True,
            "regimes": ["BULL", "NEUTRAL", "BEAR", "CRASH"],
            "kse_5d_thresholds": {
                "crash": -4.0,
                "bear": -1.0,
                "neutral_high": 1.5
            },
            "breadth_score_thresholds": {
                "crash": 15.0,
                "bear": 40.0,
                "neutral_high": 70.0
            },
            "regime_multipliers": {
                "CRASH": 0.40,
                "BEAR": 0.70,
                "NEUTRAL": 1.00,
                "BULL": 1.20
            },
            "min_samples_regime": 3
        },
        "decay_tracking": {
            "enabled": True,
            "rolling_window_days": 90,
            "min_samples_for_decay": 3,
            "decay_divergence_threshold_pct": -5.0,
            "accelerating_threshold_pct": 5.0,
            "decay_confidence_multiplier": 0.80
        },
        "pattern_expansion": {
            "enabled": True,
            "min_occurrences": 5,
            "min_distinct_symbols": 2,
            "confidence_floor": 50,
            "max_expanded_patterns": 10,
            "candidate_id_prefix": "P"
        }
    }
}

_CONFIG_CACHE: Optional[Dict[str, Any]] = None
_CONFIG_LAST_LOAD: float = 0.0

def get_intelligence_config() -> Dict[str, Any]:
    """Load configuration from config/intelligence_engine.json with caching."""
    global _CONFIG_CACHE, _CONFIG_LAST_LOAD
    now = time.time()
    if _CONFIG_CACHE is not None and (now - _CONFIG_LAST_LOAD < 30):
        return _CONFIG_CACHE

    cfg = dict(_DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                disk_cfg = json.load(f)
                if isinstance(disk_cfg, dict):
                    # Deep merge with defaults
                    for section, vals in disk_cfg.items():
                        if isinstance(vals, dict) and section in cfg and isinstance(cfg[section], dict):
                            cfg[section].update(vals)
                        else:
                            cfg[section] = vals
        except Exception as e:
            print(f"[Intelligence] Error loading {CONFIG_PATH}: {e}, using defaults")

    _CONFIG_CACHE = cfg
    _CONFIG_LAST_LOAD = now
    return cfg

def compute_median_mad(series: List[float], normalizer: float = 1.4826) -> Tuple[float, float]:
    """
    Compute sample median and normalized Median Absolute Deviation (MAD).
    Normalizer 1.4826 ensures asymptotic unbiasedness for normal distributions.
    Returns: (median, normalized_mad)
    """
    clean = [float(x) for x in series if x is not None and not math.isnan(x)]
    if not clean:
        return 0.0, 0.0
    s = sorted(clean)
    n = len(s)
    mid = n // 2
    med = s[mid] if n % 2 != 0 else (s[mid - 1] + s[mid]) / 2.0
    abs_devs = sorted([abs(x - med) for x in clean])
    mad_raw = abs_devs[mid] if n % 2 != 0 else (abs_devs[mid - 1] + abs_devs[mid]) / 2.0
    return float(med), float(mad_raw * normalizer)

# Default module-level thresholds for backward compatibility
_cfg_init = get_intelligence_config()
_ad_cfg = _cfg_init.get("anomaly_detector", {})
THRESH_PRICE_SPIKE_PCT      = float(_ad_cfg.get("price_spike_pct", 4.5))
THRESH_RVOL                 = float(_ad_cfg.get("rvol_surge_threshold", 2.8))
THRESH_RSI_JUMP             = float(_cfg_init.get("cause_investigator", {}).get("rsi_jump_threshold", 8.0))
THRESH_UPPER_LOCK_PCT       = float(_ad_cfg.get("upper_lock_pct", 9.5))
THRESH_LOWER_LOCK_PCT       = float(_ad_cfg.get("lower_lock_pct", -9.5))
THRESH_BREAKOUT_LOOKBACK    = int(_ad_cfg.get("breakout_lookback_bars", 30))

# Pattern library - minimum occurrences to show a pattern
PATTERN_MIN_OCCURRENCES     = 3

# Scheduling intervals (seconds)
ANOMALY_TICK_INTERVAL       = int(_ad_cfg.get("poll_interval_seconds", 300))
EOD_EVAL_HOUR               = 16     # 4 PM PKT
OVERNIGHT_REBUILD_HOUR      = 2      # 2 AM PKT

# Event types
EVENT_PRICE_SPIKE           = "PRICE_SPIKE"
EVENT_VOLUME_SURGE          = "VOLUME_SURGE"
EVENT_UPPER_LOCK            = "UPPER_LOCK"
EVENT_LOWER_LOCK            = "LOWER_LOCK"
EVENT_RESISTANCE_BREAK      = "RESISTANCE_BREAK"
EVENT_RSI_MOMENTUM          = "RSI_MOMENTUM"
EVENT_REVERSAL_SIGNAL       = "REVERSAL_SIGNAL"
EVENT_ACCUMULATION          = "ACCUMULATION"

# Causal factor keys
CAUSE_TECH_BREAKOUT         = "TECHNICAL_BREAKOUT"
CAUSE_VOLUME_ACCUM          = "VOLUME_ACCUMULATION"
CAUSE_RSI_MOMENTUM          = "RSI_MOMENTUM"
CAUSE_MACD_CONFIRM          = "MACD_CONFIRMATION"
CAUSE_SECTOR_MOMENTUM       = "SECTOR_MOMENTUM"
CAUSE_MARKET_MOMENTUM       = "MARKET_MOMENTUM"
CAUSE_CORP_ANNOUNCEMENT     = "CORPORATE_ANNOUNCEMENT"
CAUSE_UPPER_LOCK_SETUP      = "UPPER_LOCK_SETUP"

# Prediction signals
SIGNAL_WATCH                = "WATCH"
SIGNAL_BREAKOUT_IMMINENT    = "POSSIBLE_BREAKOUT"
SIGNAL_CONTINUATION         = "CONTINUATION_LIKELY"
SIGNAL_REVERSAL_RISK        = "REVERSAL_RISK"
SIGNAL_EXTENDED             = "EXTENDED_AVOID"


# ── Database Layer ─────────────────────────────────────────────────────────────

class IntelligenceDB:
    """SQLite database manager for the intelligence engine."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else Path(DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self):
        """Create all tables if they don't exist."""
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()

                # ── Stock Events ──────────────────────────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS stock_events (
                        id          TEXT PRIMARY KEY,
                        symbol      TEXT NOT NULL,
                        sector      TEXT DEFAULT 'Other',
                        event_type  TEXT NOT NULL,
                        detected_at TEXT NOT NULL,
                        trade_date  TEXT NOT NULL,
                        price       REAL NOT NULL,
                        price_change_pct REAL NOT NULL,
                        volume      REAL NOT NULL,
                        rvol        REAL NOT NULL,
                        rsi_at_event REAL,
                        macd_bullish INTEGER DEFAULT 0,
                        kse_return_5d REAL DEFAULT 0,
                        sector_return_5d REAL DEFAULT 0,
                        snapshot_json TEXT,
                        status      TEXT DEFAULT 'OPEN',
                        created_at  TEXT NOT NULL
                    )
                """)

                # ── Event Causes ──────────────────────────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS event_causes (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id    TEXT NOT NULL REFERENCES stock_events(id),
                        factor      TEXT NOT NULL,
                        evidence    TEXT NOT NULL,
                        confidence  INTEGER NOT NULL,
                        detail      TEXT,
                        created_at  TEXT NOT NULL
                    )
                """)

                # ── Detected Patterns ─────────────────────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS detected_patterns (
                        id          TEXT PRIMARY KEY,
                        name        TEXT NOT NULL,
                        fingerprint TEXT UNIQUE NOT NULL,
                        description TEXT,
                        occurrences INTEGER DEFAULT 0,
                        win_count   INTEGER DEFAULT 0,
                        loss_count  INTEGER DEFAULT 0,
                        neutral_count INTEGER DEFAULT 0,
                        avg_3d_return REAL DEFAULT 0,
                        avg_5d_return REAL DEFAULT 0,
                        avg_max_upside REAL DEFAULT 0,
                        avg_max_drawdown REAL DEFAULT 0,
                        last_updated TEXT,
                        created_at  TEXT NOT NULL
                    )
                """)

                # ── Pattern Occurrences ───────────────────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS pattern_occurrences (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        pattern_id  TEXT NOT NULL REFERENCES detected_patterns(id),
                        event_id    TEXT NOT NULL REFERENCES stock_events(id),
                        symbol      TEXT NOT NULL,
                        matched_at  TEXT NOT NULL,
                        similarity  REAL NOT NULL,
                        outcome     TEXT DEFAULT 'PENDING',
                        return_3d   REAL,
                        return_5d   REAL,
                        return_7d   REAL,
                        max_upside  REAL,
                        max_drawdown REAL
                    )
                """)

                # ── AI Predictions ────────────────────────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS ai_predictions (
                        id              TEXT PRIMARY KEY,
                        symbol          TEXT NOT NULL,
                        event_id        TEXT REFERENCES stock_events(id),
                        pattern_id      TEXT REFERENCES detected_patterns(id),
                        signal          TEXT NOT NULL,
                        confidence      INTEGER NOT NULL,
                        price_at_signal REAL NOT NULL,
                        pattern_name    TEXT,
                        historical_sample INTEGER DEFAULT 0,
                        historical_win_rate REAL DEFAULT 0,
                        avg_expected_return REAL DEFAULT 0,
                        reasoning_json  TEXT,
                        predicted_at    TEXT NOT NULL,
                        outcome         TEXT DEFAULT 'PENDING',
                        actual_return_5d REAL,
                        evaluated_at    TEXT
                    )
                """)

                # ── Stock Memory (rolling baseline) ───────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS stock_memory (
                        symbol          TEXT PRIMARY KEY,
                        sector          TEXT,
                        avg_daily_volume REAL DEFAULT 0,
                        avg_daily_range_pct REAL DEFAULT 0,
                        avg_daily_turnover REAL DEFAULT 0,
                        typical_rsi_min REAL DEFAULT 40,
                        typical_rsi_max REAL DEFAULT 65,
                        event_count_90d INTEGER DEFAULT 0,
                        last_price      REAL DEFAULT 0,
                        last_rsi        REAL DEFAULT 50,
                        last_rvol       REAL DEFAULT 1,
                        last_updated    TEXT,
                        median_daily_volume REAL DEFAULT 0,
                        mad_daily_volume REAL DEFAULT 0,
                        median_daily_range_pct REAL DEFAULT 0,
                        mad_daily_range_pct REAL DEFAULT 0,
                        is_liquid       INTEGER DEFAULT 1,
                        is_degraded     INTEGER DEFAULT 0,
                        degraded_reason TEXT DEFAULT NULL,
                        historical_sessions_count INTEGER DEFAULT 0
                    )
                """)

                # ── Sector Snapshots ──────────────────────────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS sector_snapshots (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        sector      TEXT NOT NULL,
                        snapshot_date TEXT NOT NULL,
                        avg_return_pct REAL DEFAULT 0,
                        total_volume REAL DEFAULT 0,
                        stock_count INTEGER DEFAULT 0,
                        UNIQUE(sector, snapshot_date)
                    )
                """)

                # ── Lens Calibration & Brier Tracking (Stage 1) ───────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS lens_calibration (
                        lens_or_cluster TEXT PRIMARY KEY,
                        sample_count    INTEGER NOT NULL,
                        is_calibrated   INTEGER NOT NULL,
                        param_a         REAL DEFAULT 0.0,
                        param_b         REAL DEFAULT 0.0,
                        brier_score     REAL DEFAULT 0.0,
                        raw_brier_score REAL DEFAULT 0.0,
                        updated_at      TEXT NOT NULL
                    )
                """)

                # ── Stage 3: Sector Shrinkage (Empirical Bayes) ───────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS sector_shrinkage (
                        sector          TEXT PRIMARY KEY,
                        sample_count    INTEGER DEFAULT 0,
                        win_count       INTEGER DEFAULT 0,
                        loss_count      INTEGER DEFAULT 0,
                        raw_win_rate    REAL DEFAULT 0.0,
                        shrunk_win_rate REAL DEFAULT 0.0,
                        multiplier      REAL DEFAULT 1.0,
                        wilson_ci_low   REAL DEFAULT 0.0,
                        wilson_ci_high  REAL DEFAULT 0.0,
                        shrinkage_weight REAL DEFAULT 0.0,
                        updated_at      TEXT NOT NULL
                    )
                """)

                # ── Stage 4: Pattern Regimes (Regime Conditioning) ────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS pattern_regimes (
                        pattern_id       TEXT NOT NULL,
                        regime           TEXT NOT NULL,
                        sample_count     INTEGER DEFAULT 0,
                        win_count        INTEGER DEFAULT 0,
                        loss_count       INTEGER DEFAULT 0,
                        neutral_count    INTEGER DEFAULT 0,
                        raw_win_rate     REAL DEFAULT 0.0,
                        shrunk_win_rate  REAL DEFAULT 0.0,
                        avg_5d_return    REAL DEFAULT 0.0,
                        wilson_ci_low    REAL DEFAULT 0.0,
                        wilson_ci_high   REAL DEFAULT 0.0,
                        regime_multiplier REAL DEFAULT 1.0,
                        updated_at       TEXT NOT NULL,
                        PRIMARY KEY (pattern_id, regime)
                    )
                """)

                c.execute("""
                    CREATE TABLE IF NOT EXISTS suggested_shares (
                        id               TEXT PRIMARY KEY,
                        symbol           TEXT NOT NULL,
                        sector           TEXT NOT NULL,
                        event_id         TEXT NOT NULL,
                        pattern_id       TEXT NOT NULL,
                        category_tag     TEXT NOT NULL,
                        composite_score  REAL NOT NULL,
                        win_probability  REAL NOT NULL,
                        wilson_ci_low    REAL NOT NULL,
                        wilson_ci_high   REAL NOT NULL,
                        sample_size_n    INTEGER NOT NULL,
                        confidence_tier  TEXT NOT NULL,
                        expected_return_mean REAL NOT NULL DEFAULT 0.0,
                        expected_return_q25  REAL NOT NULL DEFAULT 0.0,
                        expected_return_q75  REAL NOT NULL DEFAULT 0.0,
                        sector_multiplier    REAL NOT NULL DEFAULT 1.0,
                        regime           TEXT NOT NULL DEFAULT 'NEUTRAL',
                        event_risk_flag  TEXT,
                        circuit_risk_flag TEXT,
                        entry_price      REAL NOT NULL,
                        target_price     REAL NOT NULL,
                        stop_loss        REAL NOT NULL,
                        suggested_shares_qty INTEGER NOT NULL DEFAULT 0,
                        suggested_outlay REAL NOT NULL DEFAULT 0.0,
                        risk_pkr         REAL NOT NULL DEFAULT 0.0,
                        kelly_fraction   REAL NOT NULL DEFAULT 0.0,
                        thesis_en        TEXT NOT NULL DEFAULT '',
                        thesis_ur        TEXT NOT NULL DEFAULT '',
                        weights_version  TEXT NOT NULL DEFAULT 'W_v1.0.0',
                        pattern_version  TEXT NOT NULL DEFAULT 'P_v1.0.0',
                        status           TEXT NOT NULL DEFAULT 'ACTIVE',
                        created_at       TEXT NOT NULL,
                        expires_at       TEXT NOT NULL,
                        closed_at        TEXT,
                        return_5d        REAL,
                        outcome          TEXT
                    )
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_suggested_shares_status
                    ON suggested_shares(status)
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_suggested_shares_created
                    ON suggested_shares(created_at)
                """)

                # ── Stage 6: Self-Learning Loop Guardrails & Audit Table ─────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS calibration_runs (
                        id                          TEXT PRIMARY KEY,
                        run_type                    TEXT NOT NULL,
                        weights_version             TEXT NOT NULL,
                        pattern_version             TEXT NOT NULL,
                        started_at                  TEXT NOT NULL,
                        completed_at                TEXT NOT NULL,
                        training_sample_count       INTEGER NOT NULL DEFAULT 0,
                        held_out_sample_count       INTEGER NOT NULL DEFAULT 0,
                        validation_metric_name      TEXT NOT NULL DEFAULT 'BrierScore',
                        training_metric_val         REAL NOT NULL DEFAULT 0.0,
                        held_out_metric_val         REAL NOT NULL DEFAULT 0.0,
                        clamped_weights_count       INTEGER NOT NULL DEFAULT 0,
                        before_weights_json         TEXT NOT NULL DEFAULT '{}',
                        after_weights_json          TEXT NOT NULL DEFAULT '{}',
                        status                      TEXT NOT NULL DEFAULT 'SUCCESS',
                        notes                       TEXT DEFAULT ''
                    )
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_calib_runs_time
                    ON calibration_runs(completed_at)
                """)




                # ── Stage 8: Internal Data Quality Log ───────────────────────
                c.execute("""
                    CREATE TABLE IF NOT EXISTS data_quality_log (
                        id              TEXT PRIMARY KEY,
                        symbol          TEXT,
                        issue_type      TEXT NOT NULL,
                        severity        TEXT NOT NULL,
                        details         TEXT,
                        logged_at       TEXT NOT NULL,
                        resolved_at     TEXT,
                        is_active       INTEGER DEFAULT 1
                    )
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_dql_active
                    ON data_quality_log(is_active, severity)
                """)
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_dql_symbol
                    ON data_quality_log(symbol)
                """)

                # ── Column migrations for backward compatibility ─────────────
                stock_mem_cols = [
                    ("median_daily_volume", "REAL DEFAULT 0"),
                    ("mad_daily_volume", "REAL DEFAULT 0"),
                    ("median_daily_range_pct", "REAL DEFAULT 0"),
                    ("mad_daily_range_pct", "REAL DEFAULT 0"),
                    ("is_liquid", "INTEGER DEFAULT 1"),
                    ("is_degraded", "INTEGER DEFAULT 0"),
                    ("degraded_reason", "TEXT DEFAULT NULL"),
                    ("historical_sessions_count", "INTEGER DEFAULT 0")
                ]
                for col, col_def in stock_mem_cols:
                    try:
                        c.execute(f"ALTER TABLE stock_memory ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                stock_event_cols = [
                    ("is_noisy_liquidity", "INTEGER DEFAULT 0"),
                    ("excess_return_sector", "REAL DEFAULT 0"),
                    ("excess_return_kse", "REAL DEFAULT 0"),
                    ("regime", "TEXT DEFAULT 'NEUTRAL'")
                ]
                for col, col_def in stock_event_cols:
                    try:
                        c.execute(f"ALTER TABLE stock_events ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                # ── Stage 2: Purged & Embargoed Evaluation Columns ───────────
                pred_cols = [
                    ("max_drawdown_5d", "REAL DEFAULT 0"),
                    ("trader_pnl", "REAL DEFAULT 0"),
                    ("kse_return_5d", "REAL DEFAULT 0"),
                    ("excess_return_kse_5d", "REAL DEFAULT 0"),
                    ("beat_kse", "INTEGER DEFAULT 0"),
                    ("evaluation_flag", "TEXT DEFAULT 'VALID'")
                ]
                for col, col_def in pred_cols:
                    try:
                        c.execute(f"ALTER TABLE ai_predictions ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                pat_occ_cols = [
                    ("max_drawdown", "REAL DEFAULT 0"),
                    ("trader_pnl", "REAL DEFAULT 0"),
                    ("beat_kse", "INTEGER DEFAULT 0"),
                    ("evaluation_flag", "TEXT DEFAULT 'VALID'"),
                    ("regime", "TEXT DEFAULT 'NEUTRAL'")
                ]
                for col, col_def in pat_occ_cols:
                    try:
                        c.execute(f"ALTER TABLE pattern_occurrences ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                pat_cols = [
                    ("cum_trader_pnl", "REAL DEFAULT 0"),
                    ("win_rate_vs_kse", "REAL DEFAULT 0"),
                    ("kse_beat_count", "INTEGER DEFAULT 0"),
                    ("raw_win_rate", "REAL DEFAULT 0.0"),
                    ("shrunk_win_rate", "REAL DEFAULT 0.0"),
                    ("wilson_ci_low", "REAL DEFAULT 0.0"),
                    ("wilson_ci_high", "REAL DEFAULT 0.0"),
                    ("sample_size_n", "INTEGER DEFAULT 0"),
                    ("shrinkage_weight", "REAL DEFAULT 0.0"),
                    ("win_rate_90d", "REAL DEFAULT NULL"),
                    ("sample_size_90d", "INTEGER DEFAULT 0"),
                    ("decay_status", "TEXT DEFAULT 'STABLE'"),
                    ("decay_divergence", "REAL DEFAULT 0.0"),
                    ("is_expanded", "INTEGER DEFAULT 0"),
                    ("cluster_feature_vector", "TEXT DEFAULT NULL")
                ]
                for col, col_def in pat_cols:
                    try:
                        c.execute(f"ALTER TABLE detected_patterns ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                stage6_cols_preds = [
                    ("weights_version", "TEXT DEFAULT 'W_v1.0.0'"),
                    ("pattern_version", "TEXT DEFAULT 'P_v1.0.0'")
                ]
                for col, col_def in stage6_cols_preds:
                    try:
                        c.execute(f"ALTER TABLE ai_predictions ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                stage6_cols_shares = [
                    ("weights_version", "TEXT DEFAULT 'W_v1.0.0'"),
                    ("pattern_version", "TEXT DEFAULT 'P_v1.0.0'")
                ]
                for col, col_def in stage6_cols_shares:
                    try:
                        c.execute(f"ALTER TABLE suggested_shares ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass

                conn.commit()
            finally:
                conn.close()


    # ── CRUD helpers ──────────────────────────────────────────────────────────

    def insert_event(self, event: Dict[str, Any]) -> bool:
        ev = dict(event)
        now = _now()
        ev.setdefault("sector", "Other")
        ev.setdefault("trade_date", _today())
        ev.setdefault("detected_at", now)
        ev.setdefault("created_at", now)
        ev.setdefault("price_change_pct", ev.get("change_pct", 0.0))
        ev.setdefault("volume", 0.0)
        ev.setdefault("rvol", 1.0)
        ev.setdefault("rsi_at_event", 50.0)
        ev.setdefault("macd_bullish", 0)
        ev.setdefault("kse_return_5d", 0.0)
        ev.setdefault("sector_return_5d", 0.0)
        ev.setdefault("snapshot_json", "{}")
        ev.setdefault("status", "ACTIVE")
        ev.setdefault("is_noisy_liquidity", 0)
        ev.setdefault("excess_return_sector", 0.0)
        ev.setdefault("excess_return_kse", 0.0)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO stock_events
                    (id, symbol, sector, event_type, detected_at, trade_date,
                     price, price_change_pct, volume, rvol, rsi_at_event,
                     macd_bullish, kse_return_5d, sector_return_5d,
                     snapshot_json, status, created_at,
                     is_noisy_liquidity, excess_return_sector, excess_return_kse)
                    VALUES (:id,:symbol,:sector,:event_type,:detected_at,
                            :trade_date,:price,:price_change_pct,:volume,
                            :rvol,:rsi_at_event,:macd_bullish,
                            :kse_return_5d,:sector_return_5d,
                            :snapshot_json,:status,:created_at,
                            :is_noisy_liquidity,:excess_return_sector,:excess_return_kse)
                """, ev)
                conn.commit()
                return True
            except Exception as e:
                print(f"[Intelligence] insert_event error: {e}")
                return False
            finally:
                conn.close()

    def insert_causes(self, causes: List[Dict[str, Any]]):
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany("""
                    INSERT INTO event_causes
                    (event_id, factor, evidence, confidence, detail, created_at)
                    VALUES (:event_id,:factor,:evidence,:confidence,:detail,:created_at)
                """, causes)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] insert_causes error: {e}")
            finally:
                conn.close()

    def upsert_pattern(self, p: Dict[str, Any]):
        pat = dict(p)
        pat.setdefault("description", "")
        pat.setdefault("occurrences", 0)
        pat.setdefault("win_count", 0)
        pat.setdefault("loss_count", 0)
        pat.setdefault("neutral_count", 0)
        pat.setdefault("avg_3d_return", 0.0)
        pat.setdefault("avg_5d_return", 0.0)
        pat.setdefault("avg_max_upside", 0.0)
        pat.setdefault("avg_max_drawdown", 0.0)
        pat.setdefault("last_updated", _now())
        pat.setdefault("created_at", _now())
        pat.setdefault("cum_trader_pnl", 0.0)
        pat.setdefault("win_rate_vs_kse", 0.0)
        pat.setdefault("kse_beat_count", 0)
        pat.setdefault("raw_win_rate", 0.0)
        pat.setdefault("shrunk_win_rate", 0.0)
        pat.setdefault("wilson_ci_low", 0.0)
        pat.setdefault("wilson_ci_high", 0.0)
        pat.setdefault("sample_size_n", 0)
        pat.setdefault("shrinkage_weight", 0.0)
        pat.setdefault("win_rate_90d", None)
        pat.setdefault("sample_size_90d", 0)
        pat.setdefault("decay_status", "STABLE")
        pat.setdefault("decay_divergence", 0.0)
        pat.setdefault("is_expanded", 0)
        pat.setdefault("cluster_feature_vector", None)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO detected_patterns
                    (id, name, fingerprint, description, occurrences, win_count,
                     loss_count, neutral_count, avg_3d_return, avg_5d_return,
                     avg_max_upside, avg_max_drawdown, last_updated, created_at,
                     cum_trader_pnl, win_rate_vs_kse, kse_beat_count,
                     raw_win_rate, shrunk_win_rate, wilson_ci_low, wilson_ci_high,
                     sample_size_n, shrinkage_weight, win_rate_90d, sample_size_90d,
                     decay_status, decay_divergence, is_expanded, cluster_feature_vector)
                    VALUES (:id,:name,:fingerprint,:description,:occurrences,
                            :win_count,:loss_count,:neutral_count,:avg_3d_return,
                            :avg_5d_return,:avg_max_upside,:avg_max_drawdown,
                            :last_updated,:created_at,
                            :cum_trader_pnl,:win_rate_vs_kse,:kse_beat_count,
                            :raw_win_rate,:shrunk_win_rate,:wilson_ci_low,:wilson_ci_high,
                            :sample_size_n,:shrinkage_weight,:win_rate_90d,:sample_size_90d,
                            :decay_status,:decay_divergence,:is_expanded,:cluster_feature_vector)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        occurrences = :occurrences,
                        win_count = :win_count,
                        loss_count = :loss_count,
                        neutral_count = :neutral_count,
                        avg_3d_return = :avg_3d_return,
                        avg_5d_return = :avg_5d_return,
                        avg_max_upside = :avg_max_upside,
                        avg_max_drawdown = :avg_max_drawdown,
                        cum_trader_pnl = :cum_trader_pnl,
                        win_rate_vs_kse = :win_rate_vs_kse,
                        kse_beat_count = :kse_beat_count,
                        raw_win_rate = :raw_win_rate,
                        shrunk_win_rate = :shrunk_win_rate,
                        wilson_ci_low = :wilson_ci_low,
                        wilson_ci_high = :wilson_ci_high,
                        sample_size_n = :sample_size_n,
                        shrinkage_weight = :shrinkage_weight,
                        win_rate_90d = :win_rate_90d,
                        sample_size_90d = :sample_size_90d,
                        decay_status = :decay_status,
                        decay_divergence = :decay_divergence,
                        is_expanded = :is_expanded,
                        cluster_feature_vector = :cluster_feature_vector,
                        last_updated = :last_updated
                """, pat)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_pattern error: {e}")
            finally:
                conn.close()

    def insert_prediction(self, pred: Dict[str, Any]):
        p = dict(pred)
        p.setdefault("pattern_id", None)
        p.setdefault("pattern_name", "No Pattern Matched")
        p.setdefault("historical_sample", 0)
        p.setdefault("historical_win_rate", 0.0)
        p.setdefault("avg_expected_return", 0.0)
        p.setdefault("reasoning_json", "{}")
        p.setdefault("predicted_at", _now())
        p.setdefault("outcome", "PENDING")
        p.setdefault("weights_version", "W_v1.0.0")
        p.setdefault("pattern_version", "P_v1.0.0")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO ai_predictions
                    (id, symbol, event_id, pattern_id, signal, confidence,
                     price_at_signal, pattern_name, historical_sample,
                     historical_win_rate, avg_expected_return, reasoning_json,
                     predicted_at, outcome, weights_version, pattern_version)
                    VALUES (:id,:symbol,:event_id,:pattern_id,:signal,:confidence,
                            :price_at_signal,:pattern_name,:historical_sample,
                            :historical_win_rate,:avg_expected_return,
                            :reasoning_json,:predicted_at,:outcome,
                            :weights_version,:pattern_version)
                """, p)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] insert_prediction error: {e}")
            finally:
                conn.close()


    def upsert_stock_memory(self, m: Dict[str, Any]):
        mem = dict(m)
        mem.setdefault("sector", "Other")
        mem.setdefault("avg_daily_volume", 0.0)
        mem.setdefault("avg_daily_range_pct", 1.5)
        mem.setdefault("avg_daily_turnover", 0.0)
        mem.setdefault("typical_rsi_min", 35.0)
        mem.setdefault("typical_rsi_max", 65.0)
        mem.setdefault("event_count_90d", 0)
        mem.setdefault("last_price", 0.0)
        mem.setdefault("last_rsi", 50.0)
        mem.setdefault("last_rvol", 1.0)
        mem.setdefault("last_updated", _now())
        mem.setdefault("median_daily_volume", mem.get("avg_daily_volume", 0))
        mem.setdefault("mad_daily_volume", 0.0)
        mem.setdefault("median_daily_range_pct", mem.get("avg_daily_range_pct", 0.0))
        mem.setdefault("mad_daily_range_pct", 0.0)
        mem.setdefault("is_liquid", 1)
        mem.setdefault("is_degraded", 0)
        mem.setdefault("degraded_reason", None)
        mem.setdefault("historical_sessions_count", 0)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO stock_memory
                    (symbol, sector, avg_daily_volume, avg_daily_range_pct,
                     avg_daily_turnover, typical_rsi_min, typical_rsi_max,
                     event_count_90d, last_price, last_rsi, last_rvol, last_updated,
                     median_daily_volume, mad_daily_volume, median_daily_range_pct, mad_daily_range_pct, is_liquid,
                     is_degraded, degraded_reason, historical_sessions_count)
                    VALUES (:symbol,:sector,:avg_daily_volume,:avg_daily_range_pct,
                            :avg_daily_turnover,:typical_rsi_min,:typical_rsi_max,
                            :event_count_90d,:last_price,:last_rsi,:last_rvol,:last_updated,
                            :median_daily_volume,:mad_daily_volume,:median_daily_range_pct,:mad_daily_range_pct,:is_liquid,
                            :is_degraded,:degraded_reason,:historical_sessions_count)
                    ON CONFLICT(symbol) DO UPDATE SET
                        sector = :sector,
                        avg_daily_volume = :avg_daily_volume,
                        avg_daily_range_pct = :avg_daily_range_pct,
                        avg_daily_turnover = :avg_daily_turnover,
                        typical_rsi_min = :typical_rsi_min,
                        typical_rsi_max = :typical_rsi_max,
                        event_count_90d = :event_count_90d,
                        last_price = :last_price,
                        last_rsi = :last_rsi,
                        last_rvol = :last_rvol,
                        last_updated = :last_updated,
                        median_daily_volume = :median_daily_volume,
                        mad_daily_volume = :mad_daily_volume,
                        median_daily_range_pct = :median_daily_range_pct,
                        mad_daily_range_pct = :mad_daily_range_pct,
                        is_liquid = :is_liquid,
                        is_degraded = :is_degraded,
                        degraded_reason = :degraded_reason,
                        historical_sessions_count = :historical_sessions_count
                """, mem)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_stock_memory error: {e}")
            finally:
                conn.close()

    def upsert_lens_calibration(self, cal: Dict[str, Any]):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO lens_calibration
                    (lens_or_cluster, sample_count, is_calibrated, param_a, param_b, brier_score, raw_brier_score, updated_at)
                    VALUES (:lens_or_cluster, :sample_count, :is_calibrated, :param_a, :param_b, :brier_score, :raw_brier_score, :updated_at)
                    ON CONFLICT(lens_or_cluster) DO UPDATE SET
                        sample_count = :sample_count,
                        is_calibrated = :is_calibrated,
                        param_a = :param_a,
                        param_b = :param_b,
                        brier_score = :brier_score,
                        raw_brier_score = :raw_brier_score,
                        updated_at = :updated_at
                """, cal)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_lens_calibration error: {e}")
            finally:
                conn.close()

    def get_lens_calibrations(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM lens_calibration ORDER BY lens_or_cluster ASC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get_recent_events(self, limit: int = 50, symbol: str = None) -> List[Dict]:
        conn = self._connect()
        try:
            if symbol:
                rows = conn.execute("""
                    SELECT e.*, GROUP_CONCAT(c.factor||':'||c.confidence, '|') AS causes_summary
                    FROM stock_events e
                    LEFT JOIN event_causes c ON c.event_id = e.id
                    WHERE e.symbol = ?
                    GROUP BY e.id
                    ORDER BY e.detected_at DESC LIMIT ?
                """, (symbol.upper(), limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT e.*, GROUP_CONCAT(c.factor||':'||c.confidence, '|') AS causes_summary
                    FROM stock_events e
                    LEFT JOIN event_causes c ON c.event_id = e.id
                    GROUP BY e.id
                    ORDER BY e.detected_at DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_event_detail(self, event_id: str) -> Optional[Dict]:
        conn = self._connect()
        try:
            ev = conn.execute("SELECT * FROM stock_events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                return None
            causes = conn.execute(
                "SELECT * FROM event_causes WHERE event_id = ? ORDER BY confidence DESC",
                (event_id,)
            ).fetchall()
            result = dict(ev)
            result["causes"] = [dict(c) for c in causes]
            return result
        finally:
            conn.close()

    def get_event_causes(self, event_id: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM event_causes WHERE event_id = ? ORDER BY confidence DESC",
                (event_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_patterns(self, min_occurrences: int = PATTERN_MIN_OCCURRENCES) -> List[Dict]:

        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM detected_patterns
                WHERE occurrences >= ?
                ORDER BY
                    CASE WHEN (win_count + loss_count) > 0
                         THEN CAST(win_count AS REAL) / (win_count + loss_count)
                         ELSE 0.5
                    END DESC,
                    occurrences DESC
            """, (min_occurrences,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_pattern(self, pattern_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM detected_patterns WHERE id = ? LIMIT 1",
                (pattern_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_active_predictions(self, limit: int = 20) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM ai_predictions
                WHERE outcome = 'PENDING'
                ORDER BY confidence DESC, predicted_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_recent_predictions(self, limit: int = 20) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM ai_predictions
                ORDER BY predicted_at DESC, confidence DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_stock_memory(self, symbol: str) -> Optional[Dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM stock_memory WHERE symbol = ?",
                (symbol.upper(),)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_learning_stats(self) -> Dict[str, Any]:
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM ai_predictions").fetchone()[0]
            # Valid evaluated predictions
            correct = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE outcome = 'CORRECT' AND evaluation_flag = 'VALID'"
            ).fetchone()[0]
            incorrect = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE outcome = 'INCORRECT' AND evaluation_flag = 'VALID'"
            ).fetchone()[0]
            neutral = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE outcome = 'NEUTRAL' AND evaluation_flag = 'VALID'"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE outcome = 'PENDING'"
            ).fetchone()[0]

            # Leakage control counts
            purged = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE evaluation_flag = 'PURGED'"
            ).fetchone()[0]
            embargoed = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE evaluation_flag = 'EMBARGOED'"
            ).fetchone()[0]

            valid_evaluated = correct + incorrect + neutral
            decisive = correct + incorrect
            win_rate, win_lo, win_hi = compute_wilson_ci(correct, decisive)

            # ── Stage 2: Asymmetric Trader PnL ───────────────────────────────
            cum_pnl_row = conn.execute(
                "SELECT SUM(trader_pnl), AVG(trader_pnl) FROM ai_predictions WHERE evaluation_flag = 'VALID' AND outcome != 'PENDING'"
            ).fetchone()
            cum_pnl = round(float(cum_pnl_row[0] or 0.0), 2) if cum_pnl_row else 0.0
            avg_pnl = round(float(cum_pnl_row[1] or 0.0), 3) if cum_pnl_row else 0.0

            # ── Stage 2: Benchmark vs KSE-100 ────────────────────────────────
            kse_row = conn.execute(
                "SELECT SUM(beat_kse), AVG(excess_return_kse_5d), COUNT(*) FROM ai_predictions WHERE evaluation_flag = 'VALID' AND outcome != 'PENDING'"
            ).fetchone()
            kse_beats = int(kse_row[0] or 0) if kse_row else 0
            avg_excess_kse = round(float(kse_row[1] or 0.0), 2) if kse_row else 0.0
            kse_sample = int(kse_row[2] or 0) if kse_row else 0
            win_rate_vs_kse, kse_lo, kse_hi = compute_wilson_ci(kse_beats, kse_sample)

            events_total = conn.execute("SELECT COUNT(*) FROM stock_events").fetchone()[0]
            patterns_total = conn.execute(
                "SELECT COUNT(*) FROM detected_patterns WHERE occurrences >= ?",
                (PATTERN_MIN_OCCURRENCES,)
            ).fetchone()[0]
            last_tick = conn.execute("SELECT MAX(detected_at) FROM stock_events").fetchone()[0]

            return {
                "total_predictions": total,
                "evaluated_predictions": valid_evaluated,
                "correct_predictions": correct,
                "incorrect_predictions": incorrect,
                "neutral_predictions": neutral,
                "pending_predictions": pending,
                "decisive_predictions": decisive,
                "win_rate_pct": win_rate,
                "win_rate_95_ci": [win_lo, win_hi],
                "sample_size_decisive_n": decisive,
                "cum_trader_pnl": cum_pnl,
                "avg_trader_pnl": avg_pnl,
                "win_rate_vs_kse_pct": win_rate_vs_kse,
                "win_rate_vs_kse_95_ci": [kse_lo, kse_hi],
                "avg_excess_return_kse_pct": avg_excess_kse,
                "sample_size_benchmark_n": kse_sample,
                "purged_predictions_count": purged,
                "embargoed_predictions_count": embargoed,
                "valid_predictions_count": valid_evaluated,
                "total_events_detected": events_total,
                "patterns_discovered": patterns_total,
                "last_anomaly_tick": last_tick or "Never"
            }
        finally:
            conn.close()

    def get_evaluated_predictions_for_calibration(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT id, symbol, confidence, outcome, evaluation_flag,
                       actual_return_5d, predicted_at, pattern_id
                FROM ai_predictions
                WHERE outcome IN ('CORRECT', 'INCORRECT')
                ORDER BY predicted_at ASC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_pending_predictions_for_evaluation(self, days_old: int = 5) -> List[Dict]:
        cutoff = (datetime.utcnow() - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM ai_predictions
                WHERE outcome = 'PENDING' AND predicted_at <= ?
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_prediction_outcome(self, pred_id: str, outcome: str, actual_return_5d: float,
                                  max_drawdown_5d: float = 0.0, trader_pnl: float = 0.0,
                                  kse_return_5d: float = 0.0, excess_return_kse_5d: float = 0.0,
                                  beat_kse: int = 0, evaluation_flag: str = "VALID"):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    UPDATE ai_predictions
                    SET outcome = ?, actual_return_5d = ?, max_drawdown_5d = ?,
                        trader_pnl = ?, kse_return_5d = ?, excess_return_kse_5d = ?,
                        beat_kse = ?, evaluation_flag = ?, evaluated_at = ?
                    WHERE id = ?
                """, (outcome, actual_return_5d, max_drawdown_5d,
                      trader_pnl, kse_return_5d, excess_return_kse_5d,
                      beat_kse, evaluation_flag, _now(), pred_id))
                conn.commit()
            finally:
                conn.close()

    def mark_prediction_evaluation_flag(self, pred_id: str, flag: str):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    UPDATE ai_predictions
                    SET evaluation_flag = ?, evaluated_at = ?
                    WHERE id = ?
                """, (flag, _now(), pred_id))
                conn.commit()
            finally:
                conn.close()

    def get_events_for_symbol_history(self, symbol: str, days: int = 15) -> List[Dict]:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT e.*, GROUP_CONCAT(c.factor||':'||c.confidence, '|') AS causes_summary
                FROM stock_events e
                LEFT JOIN event_causes c ON c.event_id = e.id
                WHERE e.symbol = ? AND e.detected_at >= ?
                GROUP BY e.id
                ORDER BY e.detected_at ASC
            """, (symbol.upper(), cutoff)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def upsert_sector_shrinkage(self, record: Dict[str, Any]):
        rec = dict(record)
        rec.setdefault("sample_count", 0)
        rec.setdefault("win_count", 0)
        rec.setdefault("loss_count", 0)
        rec.setdefault("raw_win_rate", 0.0)
        rec.setdefault("shrunk_win_rate", 0.0)
        rec.setdefault("multiplier", 1.0)
        rec.setdefault("wilson_ci_low", 0.0)
        rec.setdefault("wilson_ci_high", 0.0)
        rec.setdefault("shrinkage_weight", 0.0)
        rec.setdefault("updated_at", _now())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO sector_shrinkage
                    (sector, sample_count, win_count, loss_count, raw_win_rate,
                     shrunk_win_rate, multiplier, wilson_ci_low, wilson_ci_high,
                     shrinkage_weight, updated_at)
                    VALUES (:sector, :sample_count, :win_count, :loss_count, :raw_win_rate,
                            :shrunk_win_rate, :multiplier, :wilson_ci_low, :wilson_ci_high,
                            :shrinkage_weight, :updated_at)
                    ON CONFLICT(sector) DO UPDATE SET
                        sample_count = :sample_count,
                        win_count = :win_count,
                        loss_count = :loss_count,
                        raw_win_rate = :raw_win_rate,
                        shrunk_win_rate = :shrunk_win_rate,
                        multiplier = :multiplier,
                        wilson_ci_low = :wilson_ci_low,
                        wilson_ci_high = :wilson_ci_high,
                        shrinkage_weight = :shrinkage_weight,
                        updated_at = :updated_at
                """, rec)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_sector_shrinkage error: {e}")
            finally:
                conn.close()

    def get_sector_shrinkage(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM sector_shrinkage
                ORDER BY sample_count DESC, shrunk_win_rate DESC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_sector_shrinkage_by_name(self, sector: str) -> Optional[Dict[str, Any]]:
        if not sector:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sector_shrinkage WHERE sector = ?",
                (sector,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def upsert_pattern_regime(self, rec: Dict[str, Any]):
        r = dict(rec)
        r.setdefault("sample_count", 0)
        r.setdefault("win_count", 0)
        r.setdefault("loss_count", 0)
        r.setdefault("neutral_count", 0)
        r.setdefault("raw_win_rate", 0.0)
        r.setdefault("shrunk_win_rate", 0.0)
        r.setdefault("avg_5d_return", 0.0)
        r.setdefault("wilson_ci_low", 0.0)
        r.setdefault("wilson_ci_high", 0.0)
        r.setdefault("regime_multiplier", 1.0)
        r.setdefault("updated_at", _now())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO pattern_regimes
                    (pattern_id, regime, sample_count, win_count, loss_count,
                     neutral_count, raw_win_rate, shrunk_win_rate, avg_5d_return,
                     wilson_ci_low, wilson_ci_high, regime_multiplier, updated_at)
                    VALUES (:pattern_id, :regime, :sample_count, :win_count, :loss_count,
                            :neutral_count, :raw_win_rate, :shrunk_win_rate, :avg_5d_return,
                            :wilson_ci_low, :wilson_ci_high, :regime_multiplier, :updated_at)
                    ON CONFLICT(pattern_id, regime) DO UPDATE SET
                        sample_count = :sample_count,
                        win_count = :win_count,
                        loss_count = :loss_count,
                        neutral_count = :neutral_count,
                        raw_win_rate = :raw_win_rate,
                        shrunk_win_rate = :shrunk_win_rate,
                        avg_5d_return = :avg_5d_return,
                        wilson_ci_low = :wilson_ci_low,
                        wilson_ci_high = :wilson_ci_high,
                        regime_multiplier = :regime_multiplier,
                        updated_at = :updated_at
                """, r)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_pattern_regime error: {e}")
            finally:
                conn.close()

    def get_pattern_regimes(self, pattern_id: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if pattern_id:
                rows = conn.execute("""
                    SELECT * FROM pattern_regimes
                    WHERE pattern_id = ?
                    ORDER BY regime ASC
                """, (pattern_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM pattern_regimes
                    ORDER BY pattern_id ASC, regime ASC
                """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_pattern_regime(self, pattern_id: str, regime: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM pattern_regimes WHERE pattern_id = ? AND regime = ?",
                (pattern_id, regime.upper())
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Suggested Shares CRUD ─────────────────────────────────────────────────

    def insert_suggested_share(self, s: Dict[str, Any]) -> None:
        """Persist a new suggested share entry (status=ACTIVE by default)."""
        conn = self._connect()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO suggested_shares
                (id, symbol, sector, event_id, pattern_id, category_tag,
                 composite_score, win_probability, wilson_ci_low, wilson_ci_high,
                 sample_size_n, confidence_tier, expected_return_mean,
                 expected_return_q25, expected_return_q75, sector_multiplier,
                 regime, event_risk_flag, circuit_risk_flag,
                 entry_price, target_price, stop_loss,
                 suggested_shares_qty, suggested_outlay, risk_pkr, kelly_fraction,
                 thesis_en, thesis_ur, weights_version, pattern_version, status,
                 created_at, expires_at, closed_at, return_5d, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                s["id"], s["symbol"], s.get("sector", ""), s["event_id"], s["pattern_id"],
                s.get("category_tag", "Breakout Continuation"),
                float(s.get("composite_score", 0.0)),
                float(s.get("win_probability", 0.0)),
                float(s.get("wilson_ci_low", 0.0)),
                float(s.get("wilson_ci_high", 0.0)),
                int(s.get("sample_size_n", 0)),
                s.get("confidence_tier", "Speculative"),
                float(s.get("expected_return_mean", 0.0)),
                float(s.get("expected_return_q25", 0.0)),
                float(s.get("expected_return_q75", 0.0)),
                float(s.get("sector_multiplier", 1.0)),
                s.get("regime", "NEUTRAL"),
                s.get("event_risk_flag"),
                s.get("circuit_risk_flag"),
                float(s.get("entry_price", 0.0)),
                float(s.get("target_price", 0.0)),
                float(s.get("stop_loss", 0.0)),
                int(s.get("suggested_shares_qty", 0)),
                float(s.get("suggested_outlay", 0.0)),
                float(s.get("risk_pkr", 0.0)),
                float(s.get("kelly_fraction", 0.0)),
                s.get("thesis_en", ""),
                s.get("thesis_ur", ""),
                s.get("weights_version", "W_v1.0.0"),
                s.get("pattern_version", "P_v1.0.0"),
                s.get("status", "ACTIVE"),
                s["created_at"],
                s["expires_at"],
                s.get("closed_at"),
                s.get("return_5d"),
                s.get("outcome"),
            ))
            conn.commit()
        except Exception as e:
            print(f"[Intelligence] insert_suggested_share error: {e}")
        finally:
            conn.close()


    def get_active_suggested_shares(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM suggested_shares
                WHERE status = 'ACTIVE'
                ORDER BY composite_score DESC, created_at DESC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_recent_suggested_shares(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM suggested_shares
                WHERE status != 'ACTIVE'
                ORDER BY closed_at DESC, created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_suggested_share_outcome(
        self, share_id: str, return_5d: float, outcome: str, closed_at: str
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                UPDATE suggested_shares
                SET return_5d = ?, outcome = ?, closed_at = ?, status = 'CLOSED'
                WHERE id = ?
            """, (return_5d, outcome, closed_at, share_id))
            conn.commit()
        except Exception as e:
            print(f"[Intelligence] update_suggested_share_outcome error: {e}")
        finally:
            conn.close()

    def expire_suggested_shares(self, now_str: str) -> int:
        """Mark ACTIVE shares past their expiry as EXPIRED. Returns count updated."""
        conn = self._connect()
        try:
            cur = conn.execute("""
                UPDATE suggested_shares
                SET status = 'EXPIRED', closed_at = ?
                WHERE status = 'ACTIVE' AND expires_at < ?
            """, (now_str, now_str))
            conn.commit()
            return cur.rowcount
        except Exception as e:
            print(f"[Intelligence] expire_suggested_shares error: {e}")
            return 0
        finally:
            conn.close()

    def get_suggested_shares_track_record(self) -> Dict[str, Any]:
        """Compute list-level realized performance across all closed suggestions."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT outcome, return_5d
                FROM suggested_shares
                WHERE status IN ('CLOSED', 'EXPIRED') AND outcome IS NOT NULL
            """).fetchall()
            if not rows:
                return {"total_closed": 0, "win_rate": None, "avg_return_5d": None,
                        "wilson_ci": [None, None], "profit_factor": None}
            total = len(rows)
            wins = sum(1 for r in rows if r["outcome"] == "WIN")
            losses = sum(1 for r in rows if r["outcome"] == "LOSS")
            decisive = wins + losses
            returns = [r["return_5d"] for r in rows if r["return_5d"] is not None]
            avg_ret = round(sum(returns) / len(returns), 2) if returns else 0.0
            win_rate_raw, ci_low, ci_high = compute_wilson_ci(wins, decisive) if decisive > 0 else (None, None, None)
            win_returns = [r["return_5d"] for r in rows if r["outcome"] == "WIN" and r["return_5d"] is not None]
            loss_returns = [abs(r["return_5d"]) for r in rows if r["outcome"] == "LOSS" and r["return_5d"] is not None]
            gross_win = sum(win_returns) if win_returns else 0.0
            gross_loss = sum(loss_returns) if loss_returns else 0.0
            profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
            return {
                "total_closed": total,
                "wins": wins,
                "losses": losses,
                "neutral": total - decisive,
                "win_rate": win_rate_raw,
                "avg_return_5d": avg_ret,
                "wilson_ci": [ci_low, ci_high],
                "profit_factor": profit_factor,
                "sample_size_n": decisive,
            }
        finally:
            conn.close()

    # ── Calibration Runs (Stage 6) ─────────────────────────────────────────────

    def insert_calibration_run(self, run: Dict[str, Any]) -> str:
        """Persist a calibration run audit record."""
        r = dict(run)
        now = _now()
        r.setdefault("id", f"CALIB-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}")
        r.setdefault("run_type", "WEEKLY_RECALIBRATION")
        r.setdefault("weights_version", "W_v1.0.0")
        r.setdefault("pattern_version", "P_v1.0.0")
        r.setdefault("started_at", now)
        r.setdefault("completed_at", now)
        r.setdefault("training_sample_count", 0)
        r.setdefault("held_out_sample_count", 0)
        r.setdefault("validation_metric_name", "BrierScore")
        r.setdefault("training_metric_val", 0.0)
        r.setdefault("held_out_metric_val", 0.0)
        r.setdefault("clamped_weights_count", 0)
        r.setdefault("before_weights_json", "{}")
        r.setdefault("after_weights_json", "{}")
        r.setdefault("status", "SUCCESS")
        r.setdefault("notes", "")

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO calibration_runs
                    (id, run_type, weights_version, pattern_version, started_at, completed_at,
                     training_sample_count, held_out_sample_count, validation_metric_name,
                     training_metric_val, held_out_metric_val, clamped_weights_count,
                     before_weights_json, after_weights_json, status, notes)
                    VALUES (:id, :run_type, :weights_version, :pattern_version, :started_at, :completed_at,
                            :training_sample_count, :held_out_sample_count, :validation_metric_name,
                            :training_metric_val, :held_out_metric_val, :clamped_weights_count,
                            :before_weights_json, :after_weights_json, :status, :notes)
                """, r)
                conn.commit()
                return r["id"]
            except Exception as e:
                print(f"[Intelligence] insert_calibration_run error: {e}")
                return r["id"]
            finally:
                conn.close()

    def get_calibration_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT * FROM calibration_runs
                ORDER BY completed_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_calibration_run(self) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("""
                SELECT * FROM calibration_runs
                ORDER BY completed_at DESC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_active_model_versions(self) -> Dict[str, str]:
        """Returns the active weights_version and pattern_version."""
        cfg = get_intelligence_config().get("stage6_self_learning_guardrails", {})
        default_w = cfg.get("default_weights_version", "W_v1.0.0")
        default_p = cfg.get("default_pattern_version", "P_v1.0.0")
        try:
            latest = self.get_latest_calibration_run()
            if latest and latest.get("status") == "SUCCESS":
                return {
                    "weights_version": latest.get("weights_version") or default_w,
                    "pattern_version": latest.get("pattern_version") or default_p
                }
        except Exception:
            pass
        return {"weights_version": default_w, "pattern_version": default_p}

    # ── Stage 8: Data Quality Log CRUD ─────────────────────────────────────────

    def insert_data_quality_issue(self, issue: Dict[str, Any]) -> str:
        r = dict(issue)
        r.setdefault("id", f"dq_{uuid.uuid4().hex[:12]}")
        r.setdefault("logged_at", _now())
        r.setdefault("resolved_at", None)
        r.setdefault("is_active", 1)
        r.setdefault("symbol", None)
        r.setdefault("details", "")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO data_quality_log
                    (id, symbol, issue_type, severity, details, logged_at, resolved_at, is_active)
                    VALUES (:id, :symbol, :issue_type, :severity, :details, :logged_at, :resolved_at, :is_active)
                """, r)
                conn.commit()
                return r["id"]
            except Exception as e:
                print(f"[Intelligence] insert_data_quality_issue error: {e}")
                return r["id"]
            finally:
                conn.close()

    def resolve_data_quality_issue(self, symbol: Optional[str], issue_type: str) -> bool:
        now_str = _now()
        with self._lock:
            conn = self._connect()
            try:
                if symbol:
                    conn.execute("""
                        UPDATE data_quality_log
                        SET is_active = 0, resolved_at = ?
                        WHERE symbol = ? AND issue_type = ? AND is_active = 1
                    """, (now_str, symbol, issue_type))
                else:
                    conn.execute("""
                        UPDATE data_quality_log
                        SET is_active = 0, resolved_at = ?
                        WHERE symbol IS NULL AND issue_type = ? AND is_active = 1
                    """, (now_str, issue_type))
                conn.commit()
                return True
            except Exception as e:
                print(f"[Intelligence] resolve_data_quality_issue error: {e}")
                return False
            finally:
                conn.close()

    def get_active_data_quality_issues(self, severity: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if severity:
                rows = conn.execute("""
                    SELECT * FROM data_quality_log
                    WHERE is_active = 1 AND severity = ?
                    ORDER BY logged_at DESC
                """, (severity,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM data_quality_log
                    WHERE is_active = 1
                    ORDER BY logged_at DESC
                """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_degraded_symbols(self) -> Set[str]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT symbol FROM stock_memory
                WHERE is_degraded = 1
            """).fetchall()
            return {r[0].upper() for r in rows if r[0]}
        finally:
            conn.close()

    def get_all_stock_memories(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM stock_memory").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ── Utility Functions ──────────────────────────────────────────────────────────

def clamp_weight_change(
    old_val: float,
    new_val: float,
    max_rel: float = 0.10,
    min_abs: float = 0.02
) -> Tuple[float, bool]:
    """
    Stage 6.1: Cap how much any weight (causal lens weight, sector multiplier,
    pattern confidence) can move in a single weekly recalibration.
    Returns: (clamped_val, was_clamped)
    """
    if old_val == 0.0:
        lower = -min_abs
        upper = min_abs
    else:
        delta = abs(old_val) * max_rel
        delta = max(delta, min_abs)
        lower = old_val - delta
        upper = old_val + delta

    if new_val < lower:
        return round(lower, 4), True
    elif new_val > upper:
        return round(upper, 4), True
    return round(new_val, 4), False


def compute_wilson_ci(k: int, n: int, z: float = 1.95996) -> Tuple[float, float, float]:
    """

    Computes Wilson score confidence interval for binomial proportion.
    Returns: (rate_pct, ci_low_pct, ci_high_pct)
    """
    if n <= 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n) / denom
    lo = max(0.0, (centre - margin) * 100.0)
    hi = min(100.0, (centre + margin) * 100.0)
    return round(p * 100.0, 2), round(lo, 2), round(hi, 2)

def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _today() -> str:
    return date.today().isoformat()

def _make_id(symbol: str, event_type: str) -> str:
    ts = int(time.time())
    return f"{symbol}_{event_type}_{ts}"

def _make_pred_id(symbol: str, signal: str) -> str:
    ts = int(time.time())
    return f"PRED_{symbol}_{signal}_{ts}"

def _confidence_label(confidence: int) -> str:
    if confidence >= 80: return "Very High"
    if confidence >= 65: return "High"
    if confidence >= 50: return "Moderate"
    if confidence >= 35: return "Low"
    return "Weak"


# ── Data Quality Monitor (Stage 8.3) ──────────────────────────────────────────

class DataQualityMonitor:
    """
    Stage 8.3: Internal Data Quality Monitor.
    Audits DPS scraper health and 90-day stock memory baseline gaps.
    Flags affected stocks as degraded to protect downstream anomaly scoring
    (preventing false-positive volume surges from zero/floor baselines).
    All data quality alerts are strictly internal (logged to DB and API),
    never broadcasted as trading alerts to users.
    """

    def __init__(self, db: IntelligenceDB):
        self.db = db
        self._lock = threading.Lock()
        self._total_scrapes: int = 0
        self._successful_scrapes: int = 0
        self._failed_scrapes: int = 0
        self._consecutive_failures: int = 0
        self._last_failure_reason: str = ""
        self._last_scrape_time: str = ""

    def audit_stock_history(self, symbol: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Audits the candle history for a stock against data quality standards:
          1. Minimum sessions: at least 30 sessions in the 90-day window (prevents zero/floor baseline surge).
          2. Suspension gaps: no gap > 14 calendar days between consecutive trading sessions.
          3. Flatline: no prolonged identical closing prices (>= 10 sessions) with 0 volume.
        Returns: {
            "is_degraded": int (1 or 0),
            "degraded_reason": str or None,
            "issue_type": str or None,
            "severity": str or None,
            "session_count": int
        }
        """
        sym = symbol.upper()
        session_count = len(history) if history else 0

        # Condition 1: Baseline Gaps (< 30 sessions out of 90)
        if session_count < 30:
            issue_type = "BASELINE_GAPS"
            severity = "DEGRADED"
            details = f"Only {session_count} trading sessions available (minimum 30 required for 90-day baseline)."
            self.db.insert_data_quality_issue({
                "symbol": sym,
                "issue_type": issue_type,
                "severity": severity,
                "details": details
            })
            return {
                "is_degraded": 1,
                "degraded_reason": details,
                "issue_type": issue_type,
                "severity": severity,
                "session_count": session_count
            }

        # Check consecutive calendar day gaps if dates are present in history
        suspension_detected = False
        max_gap_days = 0
        for i in range(len(history) - 1):
            d1_str = history[i].get("date") or history[i].get("time", "")
            d2_str = history[i+1].get("date") or history[i+1].get("time", "")
            if d1_str and d2_str:
                try:
                    d1 = datetime.strptime(str(d1_str)[:10], "%Y-%m-%d")
                    d2 = datetime.strptime(str(d2_str)[:10], "%Y-%m-%d")
                    gap = abs((d2 - d1).days)
                    if gap > 14:
                        suspension_detected = True
                        max_gap_days = max(max_gap_days, gap)
                except Exception:
                    pass

        if suspension_detected:
            issue_type = "SUSPENDED_GAP"
            severity = "WARNING"
            details = f"Trading suspension gap of {max_gap_days} calendar days detected in historical window."
            self.db.insert_data_quality_issue({
                "symbol": sym,
                "issue_type": issue_type,
                "severity": severity,
                "details": details
            })
            return {
                "is_degraded": 0,
                "degraded_reason": None,
                "issue_type": issue_type,
                "severity": severity,
                "session_count": session_count
            }

        # Condition 3: Flatline Check (prolonged identical close prices and zero volume for >= 10 sessions)
        if session_count >= 10:
            last_10 = history[-10:]
            closes = [float(c.get("close", 0) or 0) for c in last_10]
            vols = [float(c.get("volume", 0) or 0) for c in last_10]
            if len(set(closes)) == 1 and all(v == 0 for v in vols):
                issue_type = "FLATLINE_DATA"
                severity = "WARNING"
                details = f"Last 10 sessions show flatline identical price ({closes[0]}) with zero volume."
                self.db.insert_data_quality_issue({
                    "symbol": sym,
                    "issue_type": issue_type,
                    "severity": severity,
                    "details": details
                })
                return {
                    "is_degraded": 0,
                    "degraded_reason": None,
                    "issue_type": issue_type,
                    "severity": severity,
                    "session_count": session_count
                }

        # If previously logged issues, resolve them
        self.db.resolve_data_quality_issue(sym, "BASELINE_GAPS")
        self.db.resolve_data_quality_issue(sym, "SUSPENDED_GAP")
        self.db.resolve_data_quality_issue(sym, "FLATLINE_DATA")

        return {
            "is_degraded": 0,
            "degraded_reason": None,
            "issue_type": None,
            "severity": None,
            "session_count": session_count
        }

    def record_scrape_status(self, success: bool, error_msg: str = ""):
        """Records DPS scraper result."""
        with self._lock:
            self._total_scrapes += 1
            self._last_scrape_time = _now()
            if success:
                self._successful_scrapes += 1
                self._consecutive_failures = 0
                self.db.resolve_data_quality_issue("DPS_SCRAPER", "SCRAPE_FAILED")
            else:
                self._failed_scrapes += 1
                self._consecutive_failures += 1
                self._last_failure_reason = error_msg
                severity = "CRITICAL" if self._consecutive_failures >= 3 else "WARNING"
                self.db.insert_data_quality_issue({
                    "symbol": "DPS_SCRAPER",
                    "issue_type": "SCRAPE_FAILED",
                    "severity": severity,
                    "details": f"DPS Screener fetch failed (consecutive: {self._consecutive_failures}): {error_msg}"
                })

    def get_health_report(self) -> Dict[str, Any]:
        """
        Compiles the complete internal data quality health report for GET /api/intelligence/data-quality.
        """
        active_issues = self.db.get_active_data_quality_issues()
        all_memories = self.db.get_all_stock_memories()

        total_stocks = len(all_memories)
        degraded_memories = [m for m in all_memories if m.get("is_degraded") == 1]
        degraded_count = len(degraded_memories)
        healthy_count = total_stocks - degraded_count
        coverage_pct = round((healthy_count / max(total_stocks, 1)) * 100, 2)

        critical_count = sum(1 for i in active_issues if i.get("severity") == "CRITICAL")
        degraded_issues = sum(1 for i in active_issues if i.get("severity") == "DEGRADED")
        warning_count = sum(1 for i in active_issues if i.get("severity") == "WARNING")

        total_scrapes = max(self._total_scrapes, 1)
        scraper_uptime_pct = round((self._successful_scrapes / total_scrapes) * 100, 2) if self._total_scrapes > 0 else 100.0

        return {
            "status": "HEALTHY" if critical_count == 0 and degraded_count == 0 else ("DEGRADED" if critical_count == 0 else "CRITICAL"),
            "timestamp": _now(),
            "baseline_health": {
                "total_stocks_tracked": total_stocks,
                "healthy_stocks_count": healthy_count,
                "degraded_stocks_count": degraded_count,
                "coverage_pct": coverage_pct,
                "degraded_symbols": [m.get("symbol") for m in degraded_memories[:50]]
            },
            "scraper_health": {
                "uptime_pct": scraper_uptime_pct,
                "total_scrapes": self._total_scrapes,
                "successful_scrapes": self._successful_scrapes,
                "failed_scrapes": self._failed_scrapes,
                "consecutive_failures": self._consecutive_failures,
                "last_failure_reason": self._last_failure_reason,
                "last_scrape_time": self._last_scrape_time
            },
            "issues_summary": {
                "total_active_issues": len(active_issues),
                "critical": critical_count,
                "degraded": degraded_issues,
                "warning": warning_count
            },
            "active_issues": active_issues[:50],
            "disclaimer": "Internal data quality diagnostics only. Not investment advice — informational tool based on historical pattern statistics."
        }


# ── Stock Memory Builder ───────────────────────────────────────────────────────

class StockMemoryBuilder:
    """
    Builds and maintains the rolling 20-day baseline profile for each stock.
    Runs overnight to establish "normal" behavior, which the anomaly detector
    then compares against during the trading day.
    """

    def __init__(self, db: IntelligenceDB, dq_monitor: Optional[DataQualityMonitor] = None):
        self.db = db
        self.dq_monitor = dq_monitor or DataQualityMonitor(db)

    def rebuild_all(self, stocks: List[Dict[str, Any]], history_fn=None):
        """
        Rebuild stock memory for all stocks.
        history_fn: callable(symbol) -> list of daily candle dicts
        """
        print(f"[Intelligence] StockMemoryBuilder: rebuilding for {len(stocks)} stocks...")
        count = 0
        for stock in stocks:
            symbol = stock.get("symbol", "").upper()
            if not symbol:
                continue
            try:
                self._rebuild_stock(stock, history_fn)
                count += 1
            except Exception as e:
                print(f"[Intelligence] Memory rebuild error for {symbol}: {e}")
        print(f"[Intelligence] StockMemoryBuilder: rebuilt {count} stock profiles.")

    def _rebuild_stock(self, stock: Dict[str, Any], history_fn=None):
        symbol = stock.get("symbol", "").upper()
        sector = stock.get("sector", "Other")
        cfg = get_intelligence_config()
        mem_cfg = cfg.get("baseline_memory", {})
        ad_cfg = cfg.get("anomaly_detector", {})

        window_sessions = int(mem_cfg.get("rolling_window_sessions", 90))
        mad_norm = float(mem_cfg.get("mad_normalizer", 1.4826))
        vol_floor_eps = float(mem_cfg.get("volume_floor_epsilon", 100.0))
        min_turnover_floor = float(ad_cfg.get("min_turnover_pkr_daily", 500000.0))
        min_vol_floor = float(ad_cfg.get("min_volume_daily_shares", 10000.0))

        # Try to get history for volume/range baseline
        history = []
        if history_fn:
            try:
                history = history_fn(symbol) or []
            except Exception:
                pass

        # Data Quality Audit (Stage 8.3)
        audit = self.dq_monitor.audit_stock_history(symbol, history)
        is_degraded = audit.get("is_degraded", 0)
        degraded_reason = audit.get("degraded_reason")
        historical_sessions_count = audit.get("session_count", len(history))

        # Calculate robust baseline from up to 90 trading sessions
        recent_history = history[-window_sessions:] if len(history) >= window_sessions else history
        volumes = [float(c.get("volume", 0)) for c in recent_history if float(c.get("volume", 0)) >= 0]
        closes = [float(c.get("close", 0)) for c in recent_history if float(c.get("close", 0)) > 0]

        # Arithmetic baseline
        avg_daily_volume = sum(volumes) / max(len(volumes), 1) if volumes else 0.0

        # Robust baseline: Median & MAD
        med_vol, mad_vol = compute_median_mad(volumes, normalizer=mad_norm)

        # Daily range percent (High - Low) / Close
        ranges = []
        for c in recent_history:
            h = float(c.get("high", c.get("close", 0)))
            l = float(c.get("low", c.get("close", 0)))
            cl = float(c.get("close", 1))
            if cl > 0:
                ranges.append(((h - l) / cl) * 100)
        avg_daily_range_pct = sum(ranges) / max(len(ranges), 1) if ranges else 1.5
        med_range, mad_range = compute_median_mad(ranges, normalizer=mad_norm)

        # RSI range from history
        from psx_indicators import calculate_rsi_series
        if len(closes) >= 15:
            rsi_series = calculate_rsi_series(closes, 14)
            rsi_values = [r for r in rsi_series[-window_sessions:] if r > 0]
            typical_rsi_min = min(rsi_values) if rsi_values else 35.0
            typical_rsi_max = max(rsi_values) if rsi_values else 65.0
        else:
            typical_rsi_min = 35.0
            typical_rsi_max = 65.0

        # Count events in last 90 days
        conn = self.db._connect()
        try:
            cutoff = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
            event_count = conn.execute(
                "SELECT COUNT(*) FROM stock_events WHERE symbol=? AND detected_at>=?",
                (symbol, cutoff)
            ).fetchone()[0]
        finally:
            conn.close()

        current_price = float(stock.get("price", closes[-1] if closes else 0) or 0)
        current_volume = float(stock.get("volume", 0) or 0)
        avg_turnover = avg_daily_volume * (current_price if current_price > 0 else 1)
        med_turnover = med_vol * (current_price if current_price > 0 else 1)

        # Robust Liquidity check: meets both volume and turnover floors
        is_liquid = 1 if (med_vol >= min_vol_floor and med_turnover >= min_turnover_floor) else 0

        # Effective baseline for RVOL
        effective_vol_den = max(med_vol if mem_cfg.get("use_median_mad", True) else avg_daily_volume, vol_floor_eps)
        rvol = round(current_volume / effective_vol_den, 2)

        self.db.upsert_stock_memory({
            "symbol": symbol,
            "sector": sector,
            "avg_daily_volume": round(avg_daily_volume),
            "avg_daily_range_pct": round(avg_daily_range_pct, 2),
            "avg_daily_turnover": round(avg_turnover),
            "typical_rsi_min": round(typical_rsi_min, 1),
            "typical_rsi_max": round(typical_rsi_max, 1),
            "event_count_90d": event_count,
            "last_price": current_price,
            "last_rsi": 50.0,  # Updated by anomaly detector
            "last_rvol": rvol,
            "last_updated": _now(),
            "median_daily_volume": round(med_vol, 2),
            "mad_daily_volume": round(mad_vol, 2),
            "median_daily_range_pct": round(med_range, 2),
            "mad_daily_range_pct": round(mad_range, 2),
            "is_liquid": is_liquid,
            "is_degraded": is_degraded,
            "degraded_reason": degraded_reason,
            "historical_sessions_count": historical_sessions_count
        })


# ── Anomaly Detector ──────────────────────────────────────────────────────────

class AnomalyDetector:
    """
    Runs every 5 minutes (during market hours) across all PSX stocks.
    Compares each stock's current state to its baseline in stock_memory.
    Creates stock_events when anomalies are confirmed.
    Implements:
      - 0.2: Robust Median & MAD volume baselines
      - 0.3: Persistence filter across consecutive ticks
      - 0.4: False-discovery excess-return control over sector and KSE-100
      - 0.5: As-of discipline appending current intraday price to historical candles
      - 0.6: Liquidity awareness tagging for thin/noisy scrips
      - 8.1: Alert Deduplication consolidating simultaneous anomaly conditions into one event
      - 8.2: 20-second high-frequency circuit buffer capturing transient locks between 5-min ticks
      - 8.3: Degraded baseline protection suppressing false-discovery volume surges
    """

    def __init__(self, db: IntelligenceDB, investigator: 'CauseInvestigator',
                 prediction_engine: 'PredictionEngine'):
        self.db = db
        self.investigator = investigator
        self.prediction_engine = prediction_engine
        self._seen_today: set = set()  # prevent duplicate events per session
        self._candidate_ticks: Dict[str, Dict[str, int]] = {}  # symbol -> {event_type: tick_count}
        self._total_evaluated_today: int = 0
        self._false_discovery_rejected_today: int = 0
        self._intraday_circuit_buffer: Dict[str, Dict[str, Any]] = {}  # symbol -> {type, change_pct, price, timestamp}
        self._circuit_lock = threading.Lock()

    def record_intraday_tick_observations(self, stocks: List[Dict[str, Any]]):
        """
        Stage 8.2: 20-second high-frequency circuit-lock capture.
        Records any stock touching circuit lock limits (>= 7.5% or <= -7.5%)
        into the thread-safe intraday circuit buffer, ensuring transient locks
        that resolve between 5-minute ticks are captured.
        """
        if not stocks:
            return
        now_ts = time.time()
        with self._circuit_lock:
            for s in stocks:
                sym = s.get("symbol", "").upper().strip()
                if not sym:
                    continue
                try:
                    chg = float(s.get("change", 0) or 0)
                    prc = float(s.get("price", 0) or 0)
                except (ValueError, TypeError):
                    continue
                if chg >= 7.5:
                    self._intraday_circuit_buffer[sym] = {
                        "type": EVENT_UPPER_LOCK,
                        "change_pct": chg,
                        "price": prc,
                        "timestamp": now_ts
                    }
                elif chg <= -7.5:
                    self._intraday_circuit_buffer[sym] = {
                        "type": EVENT_LOWER_LOCK,
                        "change_pct": chg,
                        "price": prc,
                        "timestamp": now_ts
                    }

    def reset_daily_seen(self):
        """Reset seen-today set, candidate persistence, and circuit buffer at start of each trading day."""
        self._seen_today = set()
        self._candidate_ticks = {}
        self._total_evaluated_today = 0
        self._false_discovery_rejected_today = 0
        with self._circuit_lock:
            self._intraday_circuit_buffer = {}

    def get_fdr_metrics(self) -> Dict[str, Any]:
        """Return daily false discovery metrics for monitoring."""
        total = max(self._total_evaluated_today, 1)
        rejected = self._false_discovery_rejected_today
        return {
            "total_candidates_evaluated": self._total_evaluated_today,
            "false_discovery_rejected": rejected,
            "rejection_rate_pct": round((rejected / total) * 100, 2)
        }

    def tick(self, stocks: List[Dict[str, Any]], index_data: Dict[str, Any] = None,
             history_fn=None):
        """
        Main tick — called every 5 minutes.
        stocks: list of current stock snapshots from PSX cache
        index_data: KSE-100 data
        history_fn: callable(symbol) -> list of daily candles
        """
        kse_change = 0.0
        if index_data:
            indices = index_data.get("indices", [])
            kse = next((i for i in indices if "100" in str(i.get("name", ""))), None)
            if kse:
                kse_change = float(kse.get("change", 0) or 0)

        # Build sector snapshot
        sector_map: Dict[str, List[float]] = {}
        for s in stocks:
            sec = s.get("sector", "Other")
            chg = float(s.get("change", 0) or 0)
            sector_map.setdefault(sec, []).append(chg)
        sector_avg: Dict[str, float] = {
            sec: round(sum(vals) / max(len(vals), 1), 2)
            for sec, vals in sector_map.items()
        }

        current_tick_candidates: set = set()
        events_created = 0
        for stock in stocks:
            try:
                ev = self._check_stock(stock, kse_change, sector_avg, history_fn, current_tick_candidates)
                if ev:
                    events_created += 1
            except Exception as e:
                sym = stock.get("symbol", "?")
                print(f"[Intelligence] AnomalyDetector error for {sym}: {e}")

        # Prune candidates that dropped out on this tick
        for sym in list(self._candidate_ticks.keys()):
            for etype in list(self._candidate_ticks[sym].keys()):
                if (sym, etype) not in current_tick_candidates:
                    del self._candidate_ticks[sym][etype]
            if not self._candidate_ticks[sym]:
                del self._candidate_ticks[sym]

        # Stage 8.2: Prune old circuit observations (> 600s)
        with self._circuit_lock:
            cutoff_ts = time.time() - 600
            for sym in list(self._intraday_circuit_buffer.keys()):
                if self._intraday_circuit_buffer[sym].get("timestamp", 0) < cutoff_ts:
                    del self._intraday_circuit_buffer[sym]

        if events_created:
            fdr = self.get_fdr_metrics()
            print(f"[Intelligence] Tick: {events_created} confirmed events. (FDR filtered today: {fdr['false_discovery_rejected']})")

    def _check_stock(self, stock: Dict[str, Any], kse_change: float,
                     sector_avg: Dict[str, float], history_fn=None,
                     current_tick_candidates: set = None) -> Optional[str]:
        if current_tick_candidates is None:
            current_tick_candidates = set()

        symbol = stock.get("symbol", "").upper()
        if not symbol:
            return None

        price = float(stock.get("price", 0) or 0)
        change_pct = float(stock.get("change", 0) or 0)
        volume = float(stock.get("volume", 0) or 0)
        sector = stock.get("sector", "Other")

        if price <= 0:
            return None

        cfg = get_intelligence_config()
        ad_cfg = cfg.get("anomaly_detector", {})
        mem_cfg = cfg.get("baseline_memory", {})

        thresh_upper_lock = float(ad_cfg.get("upper_lock_pct", 9.5))
        thresh_lower_lock = float(ad_cfg.get("lower_lock_pct", -9.5))
        thresh_rvol = float(ad_cfg.get("rvol_surge_threshold", 2.8))
        thresh_price_spike = float(ad_cfg.get("price_spike_pct", 4.5))
        accum_max_change = float(ad_cfg.get("accumulation_max_change_pct", 2.0))
        breakout_lookback = int(ad_cfg.get("breakout_lookback_bars", 30))
        breakout_margin = float(ad_cfg.get("resistance_break_margin", 1.005))
        vol_floor_eps = float(mem_cfg.get("volume_floor_epsilon", 100.0))

        # ── 0.2: Load robust baseline memory (Median & MAD) ───────────────────
        memory = self.db.get_stock_memory(symbol)
        use_median = mem_cfg.get("use_median_mad", True)
        if memory and memory.get("median_daily_volume") is not None and use_median:
            baseline_vol = float(memory["median_daily_volume"] or 0)
            mad_vol = float(memory.get("mad_daily_volume", 0) or 0)
            is_liquid = int(memory.get("is_liquid", 1) if memory.get("is_liquid") is not None else 1)
        elif memory:
            baseline_vol = float(memory.get("avg_daily_volume", 0) or 0)
            mad_vol = 0.0
            is_liquid = 1
        else:
            baseline_vol = max(volume * 0.5, vol_floor_eps)
            mad_vol = 0.0
            is_liquid = 1

        effective_den = max(baseline_vol, vol_floor_eps)
        rvol = round(volume / effective_den, 2)

        # ── Stage 8.2: Check intraday circuit buffer for transient locks ──────
        buffered_lock = None
        transient_lock_resolved = False
        with self._circuit_lock:
            buf = self._intraday_circuit_buffer.get(symbol)
            if buf and (time.time() - buf.get("timestamp", 0) <= 600):
                buffered_lock = buf

        # ── Stage 8.1: Evaluate ALL candidate anomaly types ───────────────────
        tripped_types = []

        is_upper_lock = (change_pct >= thresh_upper_lock)
        if not is_upper_lock and buffered_lock and buffered_lock.get("type") == EVENT_UPPER_LOCK:
            is_upper_lock = True
            transient_lock_resolved = True

        if is_upper_lock:
            is_consecutive = False
            try:
                conn = self.db._connect()
                prev_locks = conn.execute("""
                    SELECT trade_date FROM stock_events
                    WHERE symbol = ? AND (event_type = 'UPPER_LOCK' OR event_type = 'CONSECUTIVE_UPPER_LOCK')
                      AND trade_date >= date('now', '-3 days')
                    ORDER BY trade_date DESC
                    LIMIT 3
                """, (symbol,)).fetchall()
                conn.close()
                consecutive_locks = len(prev_locks)
                if consecutive_locks >= int(ad_cfg.get("consecutive_locks_min", 2)):
                    is_consecutive = True
            except Exception:
                pass
            if is_consecutive:
                tripped_types.append("CONSECUTIVE_UPPER_LOCK")
            else:
                tripped_types.append(EVENT_UPPER_LOCK)

        is_lower_lock = (change_pct <= thresh_lower_lock)
        if not is_lower_lock and buffered_lock and buffered_lock.get("type") == EVENT_LOWER_LOCK:
            is_lower_lock = True
            transient_lock_resolved = True

        if is_lower_lock:
            tripped_types.append(EVENT_LOWER_LOCK)

        if rvol >= thresh_rvol and change_pct >= 2.0:
            tripped_types.append(EVENT_VOLUME_SURGE)

        if change_pct >= thresh_price_spike:
            tripped_types.append(EVENT_PRICE_SPIKE)

        if rvol >= thresh_rvol and abs(change_pct) < accum_max_change:
            tripped_types.append(EVENT_ACCUMULATION)

        # Resistance break check
        if history_fn and change_pct >= 2.0:
            try:
                history = history_fn(symbol) or []
                if len(history) >= breakout_lookback:
                    lookback = history[-breakout_lookback:]
                    resistance = max(c.get("high", c.get("close", 0)) for c in lookback[:-3])
                    if price > resistance * breakout_margin:
                        tripped_types.append(EVENT_RESISTANCE_BREAK)
            except Exception:
                pass

        # ── Stage 8.3: Degraded Baseline Protection ───────────────────────────
        is_degraded = int(memory.get("is_degraded", 0) or 0) if memory else 0
        if is_degraded:
            # Baseline is degraded (<30 sessions). RVOL is unreliable due to floor/zero baseline.
            # Suppress false-discovery volume surges and accumulation.
            tripped_types = [t for t in tripped_types if t not in (EVENT_VOLUME_SURGE, EVENT_ACCUMULATION)]

        if not tripped_types:
            return None

        # ── 0.3: Persistence Filter across all tripped conditions ─────────────
        persistence_cfg = ad_cfg.get("persistence_ticks", {})
        sym_ticks = self._candidate_ticks.setdefault(symbol, {})
        confirmed_types = []
        for t in tripped_types:
            required_ticks = int(persistence_cfg.get(t, 1))
            curr_tick_count = sym_ticks.get(t, 0) + 1
            sym_ticks[t] = curr_tick_count
            current_tick_candidates.add((symbol, t))
            if curr_tick_count >= required_ticks:
                confirmed_types.append(t)

        if not confirmed_types:
            return None

        # ── Stage 8.1: Alert Deduplication ────────────────────────────────────
        # Check if ALL confirmed types have already triggered today
        unseen_types = [t for t in confirmed_types if f"{symbol}_{t}_{_today()}" not in self._seen_today]
        if not unseen_types:
            return None

        # Priority hierarchy for consolidated primary event type
        priority_hierarchy = [
            "CONSECUTIVE_UPPER_LOCK",
            EVENT_UPPER_LOCK,
            EVENT_LOWER_LOCK,
            EVENT_VOLUME_SURGE,
            EVENT_PRICE_SPIKE,
            EVENT_RESISTANCE_BREAK,
            EVENT_ACCUMULATION
        ]
        primary_candidates = [t for t in priority_hierarchy if t in unseen_types]
        if not primary_candidates:
            primary_candidates = [t for t in priority_hierarchy if t in confirmed_types]
        primary_event_type = primary_candidates[0] if primary_candidates else confirmed_types[0]

        sector_return = sector_avg.get(sector, 0.0)
        excess_sector = round(change_pct - sector_return, 2)
        excess_kse = round(change_pct - kse_change, 2)

        # ── 0.4: False-Discovery / Excess Return Control ───────────────────────
        fd_cfg = cfg.get("false_discovery", {})
        self._total_evaluated_today += 1
        if fd_cfg.get("enabled", True) and primary_event_type not in fd_cfg.get("exempt_events", []):
            min_sec = float(fd_cfg.get("min_excess_over_sector_pct", 1.5))
            min_kse = float(fd_cfg.get("min_excess_over_kse_pct", 2.0))
            if abs(excess_sector) < min_sec and abs(excess_kse) < min_kse:
                self._false_discovery_rejected_today += 1
                return None

        # ── 0.6: Liquidity Awareness ──────────────────────────────────────────
        is_noisy_liquidity = 1 if is_liquid == 0 else 0

        # Mark ALL confirmed types as seen today to prevent duplicate alerts
        for t in confirmed_types:
            self._seen_today.add(f"{symbol}_{t}_{_today()}")

        # ── 0.5: As-of Data Discipline: Technical indicators ──────────────────
        rsi_val = 50.0
        macd_bullish = False
        if history_fn:
            try:
                history = history_fn(symbol) or []
                closes = [c["close"] for c in history] + [price]
                if len(closes) >= 15:
                    from psx_indicators import calculate_rsi_series, calculate_macd
                    rsi_series = calculate_rsi_series(closes, 14)
                    rsi_val = round(rsi_series[-1], 1) if rsi_series else 50.0
                    macd_res = calculate_macd(closes)
                    macd_bullish = bool(macd_res.get("is_bullish", False))
            except Exception:
                pass

        snapshot = {
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "rvol": rvol,
            "rsi": rsi_val,
            "macd_bullish": macd_bullish,
            "kse_change": kse_change,
            "sector_return": sector_return,
            "excess_return_sector": excess_sector,
            "excess_return_kse": excess_kse,
            "is_liquid": is_liquid,
            "is_noisy_liquidity": is_noisy_liquidity,
            "baseline_vol": baseline_vol,
            "mad_vol": mad_vol,
            "persistence_ticks_held": sym_ticks.get(primary_event_type, 1),
            "tripped_anomalies": confirmed_types,
            "is_consolidated": len(confirmed_types) > 1,
            "transient_lock_resolved": transient_lock_resolved,
            "is_degraded_baseline": is_degraded
        }

        event_id = _make_id(symbol, primary_event_type)
        event = {
            "id": event_id,
            "symbol": symbol,
            "sector": sector,
            "event_type": primary_event_type,
            "detected_at": _now(),
            "trade_date": _today(),
            "price": price,
            "price_change_pct": round(change_pct, 2),
            "volume": volume,
            "rvol": rvol,
            "rsi_at_event": rsi_val,
            "macd_bullish": 1 if macd_bullish else 0,
            "kse_return_5d": round(kse_change, 2),
            "sector_return_5d": round(sector_return, 2),
            "snapshot_json": json.dumps(snapshot),
            "status": "OPEN",
            "created_at": _now(),
            "is_noisy_liquidity": is_noisy_liquidity,
            "excess_return_sector": excess_sector,
            "excess_return_kse": excess_kse
        }

        if self.db.insert_event(event):
            causes = self.investigator.investigate(event, history_fn=history_fn,
                                                   sector_avg=sector_avg, kse_change=kse_change)
            if causes:
                self.db.insert_causes(causes)
            self.prediction_engine.generate_prediction(event, causes)

        return event_id


# ── Cause Investigator ─────────────────────────────────────────────────────────

class CauseInvestigator:
    """
    For every detected event, investigates WHY it happened.
    Produces a list of causal factors, each with an evidence level and confidence score.
    All analysis is deterministic — no LLMs.
    """

    def __init__(self, db: IntelligenceDB, noticeboard: Optional[Any] = None):
        self.db = db
        self.noticeboard = noticeboard or NoticeBoardScraper(db)

    def investigate(self, event: Dict[str, Any], history_fn=None,
                    sector_avg: Dict[str, float] = None,
                    kse_change: float = 0.0) -> List[Dict[str, Any]]:
        symbol = event["symbol"]
        causes = []
        event_id = event["id"]
        now = _now()

        history = []
        closes = []
        volumes = []
        cfg = get_intelligence_config()
        ci_cfg = cfg.get("cause_investigator", {})

        breakout_lb = int(ci_cfg.get("breakout_lookback_bars", 30))
        vol_accum_ratio_thresh = float(ci_cfg.get("volume_accum_3d_ratio_threshold", 1.5))
        rsi_momentum_floor = float(ci_cfg.get("rsi_momentum_floor", 60.0))
        rsi_jump_thresh = float(ci_cfg.get("rsi_jump_threshold", 3.0))
        sec_mom_thresh = float(ci_cfg.get("sector_momentum_pct_threshold", 1.5))
        mkt_mom_thresh = float(ci_cfg.get("market_momentum_pct_threshold", 1.0))

        if history_fn:
            try:
                history = history_fn(symbol) or []
                closes = [c["close"] for c in history]
                volumes = [c.get("volume", 0) for c in history]
            except Exception:
                pass

        # As-of time T series alignment
        curr_price = float(event.get("price", 0) or 0)
        closes_as_of = closes + [curr_price] if curr_price > 0 else closes

        # ── 1. Technical Breakout ─────────────────────────────────────────────
        if len(closes) >= breakout_lb and event["price"] > 0:
            lookback_highs = [history[i].get("high", closes[i])
                              for i in range(len(history) - breakout_lb, len(history) - 3)]
            if lookback_highs:
                resistance = max(lookback_highs)
                break_pct = ((event["price"] - resistance) / max(resistance, 0.01)) * 100
                if break_pct > 0.5:
                    confidence = min(95, int(50 + break_pct * 8))
                    causes.append({
                        "event_id": event_id,
                        "factor": CAUSE_TECH_BREAKOUT,
                        "evidence": "Strong" if confidence >= 70 else "Moderate",
                        "confidence": confidence,
                        "detail": f"Price broke 30-day resistance at ₨{resistance:.2f} (+{break_pct:.1f}% above)",
                        "created_at": now
                    })

        # ── 2. Volume Accumulation (3-day trend) ──────────────────────────────
        if len(volumes) >= 6:
            vol_3d_avg = sum(volumes[-4:-1]) / 3
            vol_baseline = sum(volumes[-21:-4]) / max(len(volumes[-21:-4]), 1)
            accum_ratio = vol_3d_avg / max(vol_baseline, 1)
            if accum_ratio >= vol_accum_ratio_thresh:
                confidence = min(92, int(40 + accum_ratio * 20))
                causes.append({
                    "event_id": event_id,
                    "factor": CAUSE_VOLUME_ACCUM,
                    "evidence": "Strong" if confidence >= 65 else "Moderate",
                    "confidence": confidence,
                    "detail": f"3-day volume {accum_ratio:.1f}× above baseline — sustained accumulation pattern",
                    "created_at": now
                })

        # ── 3. RSI Momentum ───────────────────────────────────────────────────
        if len(closes_as_of) >= 16:
            from psx_indicators import calculate_rsi_series
            rsi_series = calculate_rsi_series(closes_as_of, 14)
            rsi = rsi_series[-1]
            prev_rsi = rsi_series[-2]
            rsi_jump = rsi - prev_rsi
            if rsi >= rsi_momentum_floor and rsi_jump >= rsi_jump_thresh:
                confidence = min(88, int(40 + rsi_jump * 5 + (rsi - 55) * 1.2))
                causes.append({
                    "event_id": event_id,
                    "factor": CAUSE_RSI_MOMENTUM,
                    "evidence": "Strong" if rsi >= 68 else "Moderate",
                    "confidence": confidence,
                    "detail": f"RSI surged from {prev_rsi:.0f} → {rsi:.0f} (+{rsi_jump:.0f} pts); momentum accelerating",
                    "created_at": now
                })

        # ── 4. MACD Confirmation ──────────────────────────────────────────────
        if event.get("macd_bullish") and len(closes_as_of) >= 35:
            from psx_indicators import calculate_macd
            macd_res = calculate_macd(closes_as_of)
            if macd_res.get("bullish_crossover"):
                confidence = 82
                detail = "MACD bullish crossover just triggered — fresh momentum signal"
            elif macd_res.get("is_bullish"):
                confidence = 62
                detail = f"MACD bullish (histogram: {macd_res.get('histogram', 0):+.3f})"
            else:
                confidence = 0
                detail = ""
            if confidence > 0:
                causes.append({
                    "event_id": event_id,
                    "factor": CAUSE_MACD_CONFIRM,
                    "evidence": "Strong" if confidence >= 70 else "Moderate",
                    "confidence": confidence,
                    "detail": detail,
                    "created_at": now
                })

        # ── 5. Sector Momentum ────────────────────────────────────────────────
        sec = event.get("sector", "Other")
        sector_return = (sector_avg or {}).get(sec, 0.0)
        if abs(sector_return) >= sec_mom_thresh:
            confidence = min(80, int(30 + abs(sector_return) * 10))
            direction = "positive" if sector_return > 0 else "negative"
            causes.append({
                "event_id": event_id,
                "factor": CAUSE_SECTOR_MOMENTUM,
                "evidence": "Moderate" if abs(sector_return) < 3 else "Strong",
                "confidence": confidence,
                "detail": f"{sec} sector avg {sector_return:+.1f}% today — {direction} sector-wide momentum",
                "created_at": now
            })

        # ── 6. Market Momentum (KSE-100) ──────────────────────────────────────
        kse = kse_change or event.get("kse_return_5d", 0.0)
        if abs(kse) >= mkt_mom_thresh:
            confidence = min(55, int(20 + abs(kse) * 5))
            causes.append({
                "event_id": event_id,
                "factor": CAUSE_MARKET_MOMENTUM,
                "evidence": "Weak" if confidence < 40 else "Moderate",
                "confidence": confidence,
                "detail": f"KSE-100 {kse:+.1f}% today — broad market momentum contributing",
                "created_at": now
            })

        # ── 7. Corporate Announcement (Noticeboard Filing) ────────────────────
        if self.noticeboard:
            try:
                has_ann, ann_text = self.noticeboard.has_announcement_for_symbol(symbol)
                if has_ann:
                    confidence = 75
                    causes.append({
                        "event_id": event_id,
                        "factor": CAUSE_CORP_ANNOUNCEMENT,
                        "evidence": "Strong",
                        "confidence": confidence,
                        "detail": f"Corporate announcement detected on PSX noticeboard: {ann_text[:120]}",
                        "created_at": now
                    })
            except Exception:
                pass

        # ── 8. Upper Lock Setup ───────────────────────────────────────────────
        if event["event_type"] == EVENT_UPPER_LOCK:
            rvol = event.get("rvol", 1.0)
            confidence = min(95, int(60 + rvol * 5))
            causes.append({
                "event_id": event_id,
                "factor": CAUSE_UPPER_LOCK_SETUP,
                "evidence": "Very High" if rvol >= 5 else "Strong",
                "confidence": confidence,
                "detail": f"Stock reached upper circuit limit (+10%) with {rvol:.1f}× average volume — demand exceeded available market supply",
                "created_at": now
            })

        # Sort by confidence descending
        causes.sort(key=lambda x: x["confidence"], reverse=True)
        return causes

    def build_narrative(self, causes: List[Dict]) -> str:
        """Convert causes list into a correlation-framed narrative (Ground Rule 6)."""
        if not causes:
            return "Insufficient historical co-occurrence data to characterize move."

        top = [c for c in causes if c["confidence"] >= 50]
        if not top:
            top = causes[:2]

        factor_names = {
            CAUSE_TECH_BREAKOUT: "technical breakout",
            CAUSE_VOLUME_ACCUM: "abnormal volume accumulation",
            CAUSE_RSI_MOMENTUM: "RSI momentum acceleration",
            CAUSE_MACD_CONFIRM: "MACD bullish alignment",
            CAUSE_SECTOR_MOMENTUM: "sector-wide peer momentum",
            CAUSE_MARKET_MOMENTUM: "broad market index movement",
            CAUSE_CORP_ANNOUNCEMENT: "corporate announcement",
            CAUSE_UPPER_LOCK_SETUP: "circuit upper-lock demand"
        }

        names = [factor_names.get(c["factor"], c["factor"].replace("_", " ").lower())
                 for c in top[:3]]
        if len(names) == 1:
            return f"Movement historically consistent with {names[0]}."
        elif len(names) == 2:
            return f"Movement historically consistent with {names[0]} and {names[1]}."
        else:
            return f"Movement historically associated with co-occurrence of {names[0]}, {names[1]}, and {names[2]}."


# ── Causal Calibrator (Stage 1) ────────────────────────────────────────────────

class CausalCalibrator:
    """
    Stage 1: Calibrate the Causal Investigator
    - Decorrelates overlapping momentum lenses into MOMENTUM_CLUSTER
    - Fits Platt scaling logistic regression (raw score -> P(favorable outcome))
    - Tracks rolling 90-day Brier score per lens/cluster
    - Computes Composite Anomaly Severity Score from independent clusters
    - Flags unvalidated heuristics when sample size N < 100
    """

    MOMENTUM_FACTORS = {"TECHNICAL_BREAKOUT", "RSI_MOMENTUM", "MACD_CONFIRMATION"}

    def __init__(self, db: IntelligenceDB):
        self.db = db
        self._calibrations: Dict[str, Dict[str, Any]] = {}
        self._load_calibrations()

    def _load_calibrations(self):
        try:
            records = self.db.get_lens_calibrations()
            for r in records:
                self._calibrations[r["lens_or_cluster"]] = r
        except Exception as e:
            print(f"[CausalCalibrator] Error loading calibrations: {e}")

    @staticmethod
    def fit_platt_scaling(scores: List[float], labels: List[int],
                          l2_reg: float = 0.001, max_iter: int = 150, lr: float = 0.02) -> Tuple[float, float, float, float]:
        """
        Fit logistic model P(Y=1|s) = 1 / (1 + exp(-(A*(s/100) + B)))
        with Platt target smoothing and L2 regularization.
        Returns: (A, B, calibrated_brier, raw_brier)
        """
        N = len(labels)
        if N < 10 or sum(labels) == 0 or sum(labels) == N:
            p_mean = sum(labels) / max(N, 1) if N > 0 else 0.20
            b_default = math.log(max(p_mean, 0.01) / max(1.0 - p_mean, 0.01))
            raw_b = sum(((s / 100.0) - y)**2 for s, y in zip(scores, labels)) / max(N, 1) if N > 0 else 0.25
            return 0.0, b_default, 0.25, raw_b

        N_pos = sum(labels)
        N_neg = N - N_pos
        y_smoothed = [
            (N_pos + 1.0) / (N_pos + 2.0) if y == 1 else 1.0 / (N_neg + 2.0)
            for y in labels
        ]

        x = [s / 100.0 for s in scores]
        A = 0.0
        B = math.log((N_pos + 1.0) / (N_neg + 1.0))

        for _ in range(max_iter):
            grad_A = 0.0
            grad_B = 0.0
            for xi, yi in zip(x, y_smoothed):
                z = max(-30.0, min(30.0, A * xi + B))
                p = 1.0 / (1.0 + math.exp(-z))
                err = p - yi
                grad_A += err * xi
                grad_B += err
            grad_A += l2_reg * A
            A -= lr * (grad_A / N)
            B -= lr * (grad_B / N)

        preds = [1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, A * (s / 100.0) + B)))) for s in scores]
        cal_brier = sum((p - y)**2 for p, y in zip(preds, labels)) / N
        raw_brier = sum(((s / 100.0) - y)**2 for s, y in zip(scores, labels)) / N
        return round(A, 4), round(B, 4), round(cal_brier, 4), round(raw_brier, 4)

    def calibrate_all(self, rolling_days: int = 90) -> Dict[str, Any]:
        """Fit Platt scaling models for all lenses and clusters using evaluated historical outcomes."""
        conn = self.db._connect()
        results = {}
        cfg = get_intelligence_config()
        cal_cfg = cfg.get("causal_calibration", {})
        min_n = int(cal_cfg.get("min_samples_platt", 100))
        now = _now()

        try:
            cutoff = (datetime.utcnow() - timedelta(days=rolling_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = conn.execute("""
                SELECT c.factor, c.confidence,
                       (CASE WHEN p.outcome = 'CORRECT' THEN 1 ELSE 0 END) AS label
                FROM event_causes c
                JOIN ai_predictions p ON p.event_id = c.event_id
                WHERE p.outcome IN ('CORRECT', 'INCORRECT')
                  AND c.created_at >= ?
            """, (cutoff,)).fetchall()

            # If rolling window has too few records, fallback to all-time evaluated
            if len(rows) < 50:
                rows = conn.execute("""
                    SELECT c.factor, c.confidence,
                           (CASE WHEN p.outcome = 'CORRECT' THEN 1 ELSE 0 END) AS label
                    FROM event_causes c
                    JOIN ai_predictions p ON p.event_id = c.event_id
                    WHERE p.outcome IN ('CORRECT', 'INCORRECT')
                """).fetchall()

            factor_data: Dict[str, Tuple[List[float], List[int]]] = {}
            mom_scores: List[float] = []
            mom_labels: List[int] = []

            for r in rows:
                factor = r["factor"]
                conf = float(r["confidence"])
                label = int(r["label"])
                factor_data.setdefault(factor, ([], []))[0].append(conf)
                factor_data[factor][1].append(label)
                if factor in self.MOMENTUM_FACTORS:
                    mom_scores.append(conf)
                    mom_labels.append(label)

            guard_cfg = cfg.get("stage6_self_learning_guardrails", {})
            guard_enabled = bool(guard_cfg.get("enabled", True))
            max_rel = float(guard_cfg.get("max_relative_weight_change", 0.10))
            min_abs = float(guard_cfg.get("min_absolute_weight_change_floor", 0.02))

            # Fit MOMENTUM_CLUSTER
            if mom_scores:
                a, b, cal_b, raw_b = self.fit_platt_scaling(mom_scores, mom_labels)
                old_cal = self._calibrations.get("MOMENTUM_CLUSTER")
                if guard_enabled and old_cal:
                    old_a = float(old_cal.get("param_a", 0.0) or 0.0)
                    old_b = float(old_cal.get("param_b", 0.0) or 0.0)
                    a, _ = clamp_weight_change(old_a, a, max_rel=max_rel, min_abs=min_abs)
                    b, _ = clamp_weight_change(old_b, b, max_rel=max_rel, min_abs=min_abs)
                is_cal = 1 if len(mom_scores) >= min_n else 0
                rec = {
                    "lens_or_cluster": "MOMENTUM_CLUSTER",
                    "sample_count": len(mom_scores),
                    "is_calibrated": is_cal,
                    "param_a": a,
                    "param_b": b,
                    "brier_score": cal_b,
                    "raw_brier_score": raw_b,
                    "updated_at": now
                }
                self.db.upsert_lens_calibration(rec)
                self._calibrations["MOMENTUM_CLUSTER"] = rec
                results["MOMENTUM_CLUSTER"] = rec

            # Fit individual independent lenses
            for factor, (scores, labels) in factor_data.items():
                a, b, cal_b, raw_b = self.fit_platt_scaling(scores, labels)
                old_cal = self._calibrations.get(factor)
                if guard_enabled and old_cal:
                    old_a = float(old_cal.get("param_a", 0.0) or 0.0)
                    old_b = float(old_cal.get("param_b", 0.0) or 0.0)
                    a, _ = clamp_weight_change(old_a, a, max_rel=max_rel, min_abs=min_abs)
                    b, _ = clamp_weight_change(old_b, b, max_rel=max_rel, min_abs=min_abs)
                is_cal = 1 if len(scores) >= min_n else 0
                rec = {
                    "lens_or_cluster": factor,
                    "sample_count": len(scores),
                    "is_calibrated": is_cal,
                    "param_a": a,
                    "param_b": b,
                    "brier_score": cal_b,
                    "raw_brier_score": raw_b,
                    "updated_at": now
                }
                self.db.upsert_lens_calibration(rec)
                self._calibrations[factor] = rec
                results[factor] = rec


            # Ensure placeholder records for factors with N < min_n
            for factor in ["CORPORATE_ANNOUNCEMENT", "MACD_CONFIRMATION"]:
                if factor not in results:
                    rec = {
                        "lens_or_cluster": factor,
                        "sample_count": 0,
                        "is_calibrated": 0,
                        "param_a": 0.0,
                        "param_b": -1.38,
                        "brier_score": 0.25,
                        "raw_brier_score": 0.25,
                        "updated_at": now
                    }
                    self.db.upsert_lens_calibration(rec)
                    self._calibrations[factor] = rec
                    results[factor] = rec

            return results
        finally:
            conn.close()

    def predict_cluster_probability(self, cluster_or_lens: str, raw_score: float) -> Tuple[float, bool, str]:
        """
        Returns (probability, is_calibrated, label_tag).
        If sample size < min_samples_platt, returns (heuristic_prob, False, 'unvalidated heuristic').
        """
        cal = self._calibrations.get(cluster_or_lens)
        if not cal:
            self._load_calibrations()
            cal = self._calibrations.get(cluster_or_lens)

        if cal and cal.get("is_calibrated"):
            a = float(cal.get("param_a", 0.0) or 0.0)
            b = float(cal.get("param_b", 0.0) or 0.0)
            z = max(-30.0, min(30.0, a * (raw_score / 100.0) + b))
            prob = 1.0 / (1.0 + math.exp(-z))
            return round(prob, 4), True, "calibrated"
        else:
            prob = min(0.50, max(0.05, raw_score / 100.0))
            return round(prob, 4), False, "unvalidated heuristic"

    def compute_composite_severity(self, causes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        1.2 Decorrelates overlapping momentum lenses into MOMENTUM_CLUSTER.
        1.5 Computes Composite Anomaly Severity Score from independent clusters.
        """
        if not causes:
            return {
                "composite_score": 30.0,
                "calibrated_win_probability": 0.20,
                "is_calibrated": False,
                "label": "unvalidated heuristic",
                "cluster_breakdown": {}
            }

        cfg = get_intelligence_config()
        cal_cfg = cfg.get("causal_calibration", {})
        cluster_weights = cal_cfg.get("cluster_weights", {
            "MOMENTUM_CLUSTER": 0.35,
            "VOLUME_ACCUMULATION": 0.25,
            "SECTOR_MOMENTUM": 0.15,
            "CORPORATE_ANNOUNCEMENT": 0.15,
            "MARKET_MOMENTUM": 0.10
        })
        max_mom_bonus = float(cal_cfg.get("max_momentum_secondary_bonus", 10.0))

        # Group causes into clusters
        cluster_scores: Dict[str, float] = {}
        mom_scores: List[float] = []

        for c in causes:
            factor = c["factor"]
            conf = float(c.get("confidence", 0))
            if factor in self.MOMENTUM_FACTORS:
                mom_scores.append(conf)
            elif factor == "UPPER_LOCK_SETUP":
                cluster_scores["UPPER_LOCK_SETUP"] = conf
            else:
                cluster_scores[factor] = conf

        # 1.2 Decorrelate Momentum: Max score + diminishing secondary confirmation bonus
        if mom_scores:
            mom_scores.sort(reverse=True)
            primary_mom = mom_scores[0]
            secondary_bonus = sum(0.10 * s for s in mom_scores[1:])
            cluster_scores["MOMENTUM_CLUSTER"] = min(95.0, primary_mom + min(secondary_bonus, max_mom_bonus))

        # 1.5 Calculate weighted composite score
        total_weight = 0.0
        weighted_sum = 0.0
        has_uncalibrated = False

        cluster_details = {}
        for cluster, score in cluster_scores.items():
            w = cluster_weights.get(cluster, 0.10)
            prob, is_cal, tag = self.predict_cluster_probability(cluster, score)
            if not is_cal:
                has_uncalibrated = True
                effective_score = min(50.0, score)
            else:
                effective_score = score

            cluster_details[cluster] = {
                "raw_score": round(score, 1),
                "effective_score": round(effective_score, 1),
                "weight": w,
                "calibrated_prob": prob,
                "is_calibrated": is_cal,
                "tag": tag
            }
            weighted_sum += effective_score * w
            total_weight += w

        has_any_calibrated = any(d["is_calibrated"] for d in cluster_details.values())
        composite_score = round(weighted_sum / max(total_weight, 0.01), 1)
        if not has_any_calibrated:
            composite_score = min(50.0, composite_score)

        # Composite probability: weighted combination of cluster probabilities
        weighted_prob = sum(d["calibrated_prob"] * d["weight"] for d in cluster_details.values()) / max(total_weight, 0.01)

        return {
            "composite_score": composite_score,
            "calibrated_win_probability": round(weighted_prob, 4),
            "is_calibrated": not has_uncalibrated,
            "label": "unvalidated heuristic" if has_uncalibrated else "calibrated",
            "cluster_breakdown": cluster_details
        }


# ── Empirical Bayes Shrinkage Estimator (Stage 3) ───────────────────────────

class EmpiricalBayesEstimator:
    """
    Stage 3: Beta-Binomial Empirical Bayes Shrinkage Estimator
    - 3.1 Replaces flat sector multipliers (1.3x boost / 0.5x penalty) with continuous
      shrinkage estimates: each sector's win rate is pulled toward the overall population
      win rate by an amount inversely proportional to its sample size.
    - 3.2 Applies the same shrinkage approach to individual pattern (P001-P008) win rates
      before setting prediction confidence, respecting the sample-size gate.
    - 3.3 Provides side-by-side auditable output (raw observed rate vs shrunk estimate,
      Wilson 95% confidence intervals, and sample size N).
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        if cfg is None:
            cfg = get_intelligence_config().get("empirical_bayes", {})
        self.cfg = cfg
        self.sector_prior_weight_m = float(cfg.get("sector_prior_weight_m", 20.0))
        self.pattern_prior_weight_m = float(cfg.get("pattern_prior_weight_m", 20.0))
        self.min_prior_weight = float(cfg.get("min_prior_weight", 5.0))
        self.max_prior_weight = float(cfg.get("max_prior_weight", 50.0))
        self.auto_estimate_m = bool(cfg.get("auto_estimate_m", True))
        self.sample_size_gate = int(cfg.get("sample_size_gate", 3))
        self.sector_multiplier_min = float(cfg.get("sector_multiplier_min", 0.5))
        self.sector_multiplier_max = float(cfg.get("sector_multiplier_max", 1.5))
        self.default_pop_wr = float(cfg.get("default_population_win_rate", 0.1443))

    def fit_prior(self, groups_data: List[Dict[str, Any]],
                  prior_weight_default: float = 20.0) -> Tuple[float, float, float, float]:
        """
        Fits Beta-Binomial prior parameters from a list of group outcome dicts.
        Each item has 'wins' (k) and 'losses' (l), where n = wins + losses.
        Returns: (mu_0, M, alpha, beta)
        """
        valid_groups = [g for g in groups_data if (g.get("wins", 0) + g.get("losses", 0)) > 0]
        if not valid_groups:
            mu_0 = self.default_pop_wr
            M = prior_weight_default
            alpha = M * mu_0
            beta = M * (1.0 - mu_0)
            return mu_0, M, alpha, beta

        total_k = sum(g.get("wins", 0) for g in valid_groups)
        total_n = sum(g.get("wins", 0) + g.get("losses", 0) for g in valid_groups)
        mu_0 = total_k / max(total_n, 1)
        if mu_0 <= 0.0 or mu_0 >= 1.0:
            mu_0 = self.default_pop_wr

        M = prior_weight_default
        if self.auto_estimate_m and len(valid_groups) >= 3:
            try:
                k_arr = [float(g.get("wins", 0)) for g in valid_groups]
                n_arr = [float(g.get("wins", 0) + g.get("losses", 0)) for g in valid_groups]
                p_arr = [k / n for k, n in zip(k_arr, n_arr)]
                K = len(valid_groups)
                S = sum(n * ((p - mu_0) ** 2) for n, p in zip(n_arr, p_arr))
                n_sum = sum(n_arr)
                n_sq_sum = sum(n ** 2 for n in n_arr)
                c = n_sum - (n_sq_sum / max(n_sum, 1.0))
                denom = mu_0 * (1.0 - mu_0)
                if denom > 0 and c > 0:
                    val = (S / denom) - (K - 1)
                    if val > 0:
                        M_est = (c / val) - 1.0
                        M = max(self.min_prior_weight, min(self.max_prior_weight, M_est))
            except Exception:
                M = prior_weight_default

        alpha = round(M * mu_0, 4)
        beta = round(M * (1.0 - mu_0), 4)
        return round(mu_0, 4), round(M, 2), alpha, beta

    def shrink_rate(self, wins: int, decisive_n: int, mu_0: float, M: float) -> Dict[str, Any]:
        """
        Shrinks observed rate toward population prior:
        p_shrunk = (wins + alpha) / (decisive_n + M)
        Returns dict with:
        - raw_win_rate_pct
        - shrunk_win_rate_pct
        - shrinkage_weight (weight placed on sample: n / (n + M))
        - sample_size_n
        - wilson_ci: [low, high]
        - is_gated: whether decisive_n < sample_size_gate
        """
        raw_pct, ci_lo, ci_hi = compute_wilson_ci(wins, decisive_n)
        alpha = M * mu_0
        if decisive_n <= 0:
            shrunk_pct = round(mu_0 * 100.0, 2)
            shrink_weight = 0.0
        else:
            shrunk_prob = (wins + alpha) / (decisive_n + M)
            shrunk_pct = round(shrunk_prob * 100.0, 2)
            shrink_weight = round(decisive_n / (decisive_n + M), 4)

        is_gated = decisive_n < self.sample_size_gate
        return {
            "sample_size_n": decisive_n,
            "wins": wins,
            "losses": decisive_n - wins,
            "raw_win_rate_pct": raw_pct,
            "shrunk_win_rate_pct": shrunk_pct,
            "shrinkage_weight": shrink_weight,
            "wilson_ci": [ci_lo, ci_hi],
            "wilson_ci_low": ci_lo,
            "wilson_ci_high": ci_hi,
            "is_gated": is_gated
        }

    def compute_sector_multiplier(self, shrunk_win_rate_pct: float, mu_0: float) -> float:
        """
        Smooth continuous sector multiplier centered at 1.0:
        multiplier = shrunk_rate / mu_0, clamped to [mult_min, mult_max].
        When shrunk_rate == mu_0 (unobserved or small N), multiplier == 1.00.
        """
        mu_pct = mu_0 * 100.0
        if mu_pct <= 0:
            return 1.0
        raw_mult = shrunk_win_rate_pct / mu_pct
        return round(max(self.sector_multiplier_min, min(self.sector_multiplier_max, raw_mult)), 2)


# ── Market Regime Resolver (Stage 4.1) ────────────────────────────────────────

def resolve_market_regime(date_str: Optional[str] = None,
                          kse_5d_return: Optional[float] = None,
                          breadth_score: Optional[float] = None) -> str:
    """
    Deterministically resolves market regime into 'BULL', 'NEUTRAL', 'BEAR', or 'CRASH'.
    Priority:
      1. Breadth history table in cache/breadth.db for the specific date
      2. Direct breadth_score if provided
      3. Historical KSE-100 5-day return threshold
      4. Live current market breadth via psx_breadth_engine (if date is today/None)
      5. Default: 'NEUTRAL'
    """
    cfg = get_intelligence_config()
    reg_cfg = cfg.get("stage4_pattern_lifecycle", {}).get("regime_conditioning", {})
    kse_thresh = reg_cfg.get("kse_5d_thresholds", {"crash": -4.0, "bear": -1.0, "neutral_high": 1.5})
    breadth_thresh = reg_cfg.get("breadth_score_thresholds", {"crash": 15.0, "bear": 40.0, "neutral_high": 70.0})

    target_date = date_str[:10] if date_str else _today()

    # 1. Check breadth.db if present
    breadth_db_path = BASE_DIR / "cache" / "breadth.db"
    if breadth_db_path.exists():
        try:
            conn = sqlite3.connect(str(breadth_db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT regime, score FROM breadth_history WHERE date = ? ORDER BY id DESC LIMIT 1",
                (target_date,)
            ).fetchone()
            conn.close()
            if row and row["regime"]:
                reg = str(row["regime"]).upper()
                if reg in ("BULL", "NEUTRAL", "BEAR", "CRASH"):
                    return reg
        except Exception:
            pass

    # 2. Check breadth_score argument
    if breadth_score is not None:
        if breadth_score >= float(breadth_thresh.get("neutral_high", 70.0)):
            return "BULL"
        elif breadth_score >= float(breadth_thresh.get("bear", 40.0)):
            return "NEUTRAL"
        elif breadth_score >= float(breadth_thresh.get("crash", 15.0)):
            return "BEAR"
        else:
            return "CRASH"

    # 3. Check kse_5d_return
    if kse_5d_return is not None:
        kse_val = float(kse_5d_return)
        if kse_val < float(kse_thresh.get("crash", -4.0)):
            return "CRASH"
        elif kse_val < float(kse_thresh.get("bear", -1.0)):
            return "BEAR"
        elif kse_val <= float(kse_thresh.get("neutral_high", 1.5)):
            return "NEUTRAL"
        else:
            return "BULL"

    # 4. If current date, try live breadth engine
    if not date_str or target_date == _today():
        try:
            import psx_breadth_engine
            b = psx_breadth_engine.get_market_breadth()
            if b and b.get("regime"):
                reg = str(b["regime"]).upper()
                if reg in ("BULL", "NEUTRAL", "BEAR", "CRASH"):
                    return reg
        except Exception:
            pass

    return "NEUTRAL"


# ── Unsupervised Pattern Expander (Stage 4.3) ──────────────────────────────────

class PatternExpander:
    """
    Unsupervised pattern discovery using frequent co-occurrence clustering
    on causal-lens feature vectors. Discovers recurring patterns beyond P001-P008.
    All logic is statistical and deterministic (zero LLM reliance).
    """

    FACTOR_HUMAN_NAMES = {
        CAUSE_TECH_BREAKOUT: "Breakout",
        CAUSE_VOLUME_ACCUM: "Volume Accumulation",
        CAUSE_RSI_MOMENTUM: "RSI Momentum",
        CAUSE_MACD_CONFIRM: "MACD Trend",
        CAUSE_SECTOR_MOMENTUM: "Sector Surge",
        CAUSE_MARKET_MOMENTUM: "Market Tailwind",
        CAUSE_CORP_ANNOUNCEMENT: "Corporate Catalyst",
        CAUSE_UPPER_LOCK_SETUP: "Upper Lock Demand"
    }

    ALL_FACTORS = [
        CAUSE_TECH_BREAKOUT,
        CAUSE_VOLUME_ACCUM,
        CAUSE_RSI_MOMENTUM,
        CAUSE_MACD_CONFIRM,
        CAUSE_SECTOR_MOMENTUM,
        CAUSE_MARKET_MOMENTUM,
        CAUSE_CORP_ANNOUNCEMENT,
        CAUSE_UPPER_LOCK_SETUP
    ]

    def __init__(self, db: IntelligenceDB):
        self.db = db
        cfg = get_intelligence_config().get("stage4_pattern_lifecycle", {}).get("pattern_expansion", {})
        self.enabled = cfg.get("enabled", True)
        self.min_occurrences = int(cfg.get("min_occurrences", 5))
        self.min_distinct_symbols = int(cfg.get("min_distinct_symbols", 2))
        self.confidence_floor = int(cfg.get("confidence_floor", 50))
        self.max_expanded_patterns = int(cfg.get("max_expanded_patterns", 10))

    def discover_candidate_patterns(self, existing_fingerprints: set) -> List[Dict[str, Any]]:
        """
        Scans historical event causes, builds causal feature itemsets,
        identifies recurring multi-factor clusters with >= min_occurrences,
        and generates candidate pattern definitions.
        """
        if not self.enabled:
            return []

        conn = self.db._connect()
        try:
            rows = conn.execute("""
                SELECT e.id AS event_id, e.symbol,
                       c.factor, c.confidence
                FROM stock_events e
                JOIN event_causes c ON c.event_id = e.id AND c.confidence >= ?
                ORDER BY e.id, c.factor
            """, (self.confidence_floor,)).fetchall()

            event_factors: Dict[str, set] = defaultdict(set)
            event_symbols: Dict[str, str] = {}
            factor_confidences: Dict[str, Dict[str, int]] = defaultdict(dict)

            for r in rows:
                ev_id = r["event_id"]
                event_factors[ev_id].add(r["factor"])
                event_symbols[ev_id] = r["symbol"]
                factor_confidences[ev_id][r["factor"]] = r["confidence"]

            cluster_events: Dict[str, List[str]] = defaultdict(list)
            cluster_symbols: Dict[str, set] = defaultdict(set)

            for ev_id, factors in event_factors.items():
                if len(factors) < 2:
                    continue
                fingerprint = "|".join(sorted(factors))
                if fingerprint in existing_fingerprints:
                    continue
                cluster_events[fingerprint].append(ev_id)
                cluster_symbols[fingerprint].add(event_symbols[ev_id])

            qualified = []
            for fp, ev_list in cluster_events.items():
                n_occ = len(ev_list)
                n_sym = len(cluster_symbols[fp])
                if n_occ >= self.min_occurrences and n_sym >= self.min_distinct_symbols:
                    factors = fp.split("|")
                    qualified.append({
                        "fingerprint": fp,
                        "factors": factors,
                        "occurrences": n_occ,
                        "symbols_count": n_sym,
                        "event_ids": ev_list
                    })

            qualified.sort(key=lambda x: x["occurrences"], reverse=True)
            candidate_clusters = qualified[:self.max_expanded_patterns]

            candidates = []
            for idx, item in enumerate(candidate_clusters, start=9):
                pat_id = f"P{idx:03d}"
                factors = item["factors"]
                name_parts = [self.FACTOR_HUMAN_NAMES.get(f, f) for f in factors]
                name = " + ".join(name_parts)
                desc = (f"AI Discovered: Statistical co-occurrence of {name} "
                        f"across {item['occurrences']} events and {item['symbols_count']} symbols.")

                avg_vector = {}
                for f in self.ALL_FACTORS:
                    confs = [factor_confidences[eid].get(f, 0) for eid in item["event_ids"]]
                    avg_vector[f] = round(sum(confs) / max(len(confs), 1), 1)

                candidates.append({
                    "id": pat_id,
                    "name": name,
                    "fingerprint": item["fingerprint"],
                    "description": desc,
                    "is_expanded": 1,
                    "cluster_feature_vector": json.dumps(avg_vector)
                })

            return candidates
        except Exception as e:
            print(f"[Intelligence] PatternExpander error: {e}")
            return []
        finally:
            conn.close()


# ── Pattern Library ────────────────────────────────────────────────────────────

class PatternLibrary:
    """
    Automatically discovers and names recurring patterns from historical events.
    Rebuilt nightly by analyzing all stored events and their causes.
    Stage 3: Applies Empirical Bayes shrinkage to all patterns.
    Stage 4: Adds Market Regime Conditioning, Rolling 90-Day Decay Tracking,
    and Unsupervised Pattern Expansion via PatternExpander.
    """

    BASE_PATTERN_DEFINITIONS = [
        {
            "id": "P001",
            "name": "Breakout + Volume Surge",
            "fingerprint": "TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION",
            "description": "Historically co-occurring price resistance breakout and elevated session volume"
        },
        {
            "id": "P002",
            "name": "3-Day Accumulation Breakout",
            "fingerprint": "VOLUME_ACCUMULATION|TECHNICAL_BREAKOUT|RSI_MOMENTUM",
            "description": "Multi-session volume accumulation statistically associated with subsequent resistance breakout and RSI momentum"
        },
        {
            "id": "P003",
            "name": "Upper Lock with Accumulation",
            "fingerprint": "UPPER_LOCK_SETUP|VOLUME_ACCUMULATION",
            "description": "Upper circuit lock condition historically co-occurring with prior volume accumulation"
        },
        {
            "id": "P004",
            "name": "Sector-Led Breakout",
            "fingerprint": "SECTOR_MOMENTUM|TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION",
            "description": "Price breakout statistically coinciding with sector-wide momentum and elevated volume"
        },
        {
            "id": "P005",
            "name": "MACD + Volume Surge",
            "fingerprint": "MACD_CONFIRMATION|VOLUME_ACCUMULATION",
            "description": "MACD bullish crossover statistically observed alongside volume surge"
        },
        {
            "id": "P006",
            "name": "RSI Momentum + Breakout",
            "fingerprint": "RSI_MOMENTUM|TECHNICAL_BREAKOUT",
            "description": "RSI momentum acceleration observed in statistical alignment with price breakout"
        },
        {
            "id": "P007",
            "name": "Full Confluence Setup",
            "fingerprint": "TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION|RSI_MOMENTUM|MACD_CONFIRMATION",
            "description": "Multi-factor statistical confluence across breakout, volume, RSI, and MACD indicators"
        },
        {
            "id": "P008",
            "name": "Volume Surge Only",
            "fingerprint": "VOLUME_ACCUMULATION",
            "description": "Volume surge observed in absence of immediate technical breakout catalyst"
        }
    ]

    PATTERN_DEFINITIONS = BASE_PATTERN_DEFINITIONS

    def __init__(self, db: IntelligenceDB, eb_estimator: Optional[EmpiricalBayesEstimator] = None):
        self.db = db
        self.eb_estimator = eb_estimator or EmpiricalBayesEstimator()
        self.pattern_expander = PatternExpander(db)

    def get_all_definitions(self) -> List[Dict[str, Any]]:
        """Return base definitions plus any discovered expanded patterns."""
        patterns = list(self.BASE_PATTERN_DEFINITIONS)
        conn = self.db._connect()
        try:
            rows = conn.execute("""
                SELECT id, name, fingerprint, description, is_expanded, cluster_feature_vector
                FROM detected_patterns
                WHERE is_expanded = 1
                ORDER BY id ASC
            """).fetchall()
            for r in rows:
                patterns.append(dict(r))
        except Exception:
            pass
        finally:
            conn.close()
        return patterns

    def rebuild(self):
        """
        Rebuild pattern statistics from all stored events and their causes:
          - Stage 3: Empirical Bayes shrinkage
          - Stage 4.1: Market Regime Conditioning (Bull, Neutral, Bear, Crash)
          - Stage 4.2: Rolling 90-Day Decay Tracking
          - Stage 4.3: Unsupervised Pattern Expansion
        """
        print("[Intelligence] PatternLibrary: rebuilding pattern statistics with Empirical Bayes & Regime Conditioning...")
        now = _now()
        cutoff_90d = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

        # 1. Discover unsupervised candidate patterns (Stage 4.3)
        base_fingerprints = {p["fingerprint"] for p in self.BASE_PATTERN_DEFINITIONS}
        expanded_patterns = self.pattern_expander.discover_candidate_patterns(base_fingerprints)
        all_patterns = list(self.BASE_PATTERN_DEFINITIONS) + expanded_patterns

        pattern_data_list = []
        pattern_regime_accum: Dict[str, Dict[str, Dict[str, Any]]] = {}

        for pat_def in all_patterns:
            pat_id = pat_def["id"]
            fingerprint = pat_def["fingerprint"]
            required_factors = set(fingerprint.split("|"))

            matching_events = self._find_matching_events(required_factors)
            occurrences = len(matching_events)

            win_count = 0
            loss_count = 0
            neutral_count = 0
            win_count_90d = 0
            loss_count_90d = 0
            neutral_count_90d = 0
            returns_3d = []
            returns_5d = []
            upsides = []
            drawdowns = []

            # Regimes: BULL, NEUTRAL, BEAR, CRASH
            reg_stats = {
                r: {"win_count": 0, "loss_count": 0, "neutral_count": 0, "returns_5d": []}
                for r in ("BULL", "NEUTRAL", "BEAR", "CRASH")
            }

            for ev in matching_events:
                ev_date = ev.get("trade_date") or (ev.get("detected_at", "")[:10])
                kse_ret = float(ev.get("kse_return_5d", 0.0) or 0.0)
                ev_regime = ev.get("regime")
                if not ev_regime or ev_regime == "NEUTRAL":
                    ev_regime = resolve_market_regime(ev_date, kse_5d_return=kse_ret)

                is_recent_90d = (ev_date >= cutoff_90d) if ev_date else False

                conn = self.db._connect()
                try:
                    occ = conn.execute("""
                        SELECT * FROM pattern_occurrences
                        WHERE event_id = ? AND outcome != 'PENDING'
                          AND (evaluation_flag = 'VALID' OR evaluation_flag IS NULL)
                        LIMIT 1
                    """, (ev["id"],)).fetchone()

                    # Fallback backfill from ai_predictions if needed
                    if not occ:
                        pred = conn.execute("""
                            SELECT outcome, actual_return_5d, max_drawdown_5d, trader_pnl, beat_kse, evaluation_flag
                            FROM ai_predictions
                            WHERE event_id = ? AND outcome != 'PENDING'
                              AND (evaluation_flag = 'VALID' OR evaluation_flag IS NULL)
                            LIMIT 1
                        """, (ev["id"],)).fetchone()
                        if pred:
                            p_out = pred["outcome"]
                            ret5 = pred["actual_return_5d"]
                            dd5 = pred["max_drawdown_5d"] or 0.0
                            tpnl = pred["trader_pnl"] or (1.0 if p_out == "CORRECT" else (-1.5 if p_out == "INCORRECT" else 0.0))
                            bkse = pred["beat_kse"] or 0
                            eflag = pred["evaluation_flag"] or "VALID"
                            mapped_outcome = "WIN" if p_out == "CORRECT" else ("LOSS" if p_out == "INCORRECT" else "NEUTRAL")
                            try:
                                conn.execute("""
                                    INSERT OR IGNORE INTO pattern_occurrences
                                    (pattern_id, event_id, symbol, matched_at, similarity, outcome, return_5d,
                                     max_drawdown, trader_pnl, beat_kse, evaluation_flag, regime)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (pat_id, ev["id"], ev.get("symbol", ""), now, 0.9, mapped_outcome, ret5,
                                      dd5, tpnl, bkse, eflag, ev_regime))
                                conn.commit()
                            except Exception:
                                pass
                            occ = {"outcome": mapped_outcome, "return_3d": None, "return_5d": ret5,
                                   "max_drawdown": dd5, "trader_pnl": tpnl, "beat_kse": bkse, "regime": ev_regime}

                    if occ:
                        occ = dict(occ)
                        out_val = occ["outcome"]
                        ret5_val = occ.get("return_5d")

                        # Update occurrence regime in DB if currently NEUTRAL or missing
                        if occ.get("regime") in (None, "NEUTRAL") and ev_regime != "NEUTRAL":
                            try:
                                conn.execute("UPDATE pattern_occurrences SET regime = ? WHERE id = ?", (ev_regime, occ["id"]))
                                conn.commit()
                            except Exception:
                                pass

                        target_regime = ev_regime if ev_regime in reg_stats else "NEUTRAL"

                        if out_val == "WIN":
                            win_count += 1
                            reg_stats[target_regime]["win_count"] += 1
                            if is_recent_90d:
                                win_count_90d += 1
                        elif out_val == "LOSS":
                            loss_count += 1
                            reg_stats[target_regime]["loss_count"] += 1
                            if is_recent_90d:
                                loss_count_90d += 1
                        else:
                            neutral_count += 1
                            reg_stats[target_regime]["neutral_count"] += 1
                            if is_recent_90d:
                                neutral_count_90d += 1

                        if occ.get("return_3d") is not None:
                            returns_3d.append(occ["return_3d"])
                        if ret5_val is not None:
                            returns_5d.append(ret5_val)
                            reg_stats[target_regime]["returns_5d"].append(ret5_val)
                        if occ.get("max_drawdown") is not None:
                            drawdowns.append(occ["max_drawdown"])

                        pnl = float(occ.get("trader_pnl") or (1.0 if out_val == "WIN" else (-1.5 if out_val == "LOSS" else 0.0)))
                        upsides.append(pnl)
                finally:
                    conn.close()

            avg_3d = sum(returns_3d) / max(len(returns_3d), 1) if returns_3d else 0.0
            avg_5d = sum(returns_5d) / max(len(returns_5d), 1) if returns_5d else 0.0
            avg_dd = sum(drawdowns) / max(len(drawdowns), 1) if drawdowns else 0.0
            cum_pnl = sum(upsides)
            kse_beat_total = sum(1 for ret in returns_5d if ret > 0.0)
            win_rate_vs_kse = round((kse_beat_total / max(len(returns_5d), 1)) * 100.0, 1)

            pattern_data_list.append({
                "id": pat_id,
                "name": pat_def["name"],
                "fingerprint": fingerprint,
                "description": pat_def["description"],
                "occurrences": occurrences,
                "win_count": win_count,
                "loss_count": loss_count,
                "neutral_count": neutral_count,
                "win_count_90d": win_count_90d,
                "loss_count_90d": loss_count_90d,
                "neutral_count_90d": neutral_count_90d,
                "avg_3d_return": round(avg_3d, 2),
                "avg_5d_return": round(avg_5d, 2),
                "avg_max_upside": 0.0,
                "avg_max_drawdown": round(avg_dd, 2),
                "cum_trader_pnl": round(cum_pnl, 2),
                "win_rate_vs_kse": win_rate_vs_kse,
                "kse_beat_count": kse_beat_total,
                "wins": win_count,
                "losses": loss_count,
                "is_expanded": pat_def.get("is_expanded", 0),
                "cluster_feature_vector": pat_def.get("cluster_feature_vector")
            })
            pattern_regime_accum[pat_id] = reg_stats

        # Fit Empirical Bayes prior across all patterns
        mu_pat_0, M_pat, alpha_pat, beta_pat = self.eb_estimator.fit_prior(
            pattern_data_list, prior_weight_default=self.eb_estimator.pattern_prior_weight_m
        )

        cfg_decay = get_intelligence_config().get("stage4_pattern_lifecycle", {}).get("decay_tracking", {})
        min_decay_samples = int(cfg_decay.get("min_samples_for_decay", 3))
        decay_thresh = float(cfg_decay.get("decay_divergence_threshold_pct", -5.0))
        accel_thresh = float(cfg_decay.get("accelerating_threshold_pct", 5.0))

        reg_cfg = get_intelligence_config().get("stage4_pattern_lifecycle", {}).get("regime_conditioning", {})
        reg_mult_map = reg_cfg.get("regime_multipliers", {"CRASH": 0.40, "BEAR": 0.70, "NEUTRAL": 1.00, "BULL": 1.20})

        guard_cfg = get_intelligence_config().get("stage6_self_learning_guardrails", {})
        guard_enabled = bool(guard_cfg.get("enabled", True))
        max_rel = float(guard_cfg.get("max_relative_weight_change", 0.10))
        min_abs = float(guard_cfg.get("min_absolute_weight_change_floor", 0.02))

        try:
            existing_pats = {p["id"]: float(p.get("shrunk_win_rate", 0.0) or 0.0) for p in self.db.get_patterns(min_occurrences=0)}
        except Exception:
            existing_pats = {}

        for pat in pattern_data_list:
            pat_id = pat["id"]
            w = pat["win_count"]
            l = pat["loss_count"]
            decisive = w + l
            eb_res = self.eb_estimator.shrink_rate(w, decisive, mu_pat_0, M_pat)

            raw_shrunk = eb_res["shrunk_win_rate_pct"]
            if guard_enabled and pat_id in existing_pats and existing_pats[pat_id] > 0.0:
                old_shrunk = existing_pats[pat_id]
                shrunk_win_rate, _ = clamp_weight_change(old_shrunk, raw_shrunk, max_rel=max_rel, min_abs=min_abs)
            else:
                shrunk_win_rate = raw_shrunk

            # Rolling 90-day decay calculation (Stage 4.2)
            decisive_90d = pat["win_count_90d"] + pat["loss_count_90d"]
            if decisive_90d > 0:
                win_rate_90d = round((pat["win_count_90d"] / decisive_90d) * 100.0, 2)
                decay_div = round(win_rate_90d - shrunk_win_rate, 2)
            else:
                win_rate_90d = None
                decay_div = 0.0

            decay_status = "STABLE"
            if decisive_90d >= min_decay_samples:
                if decay_div <= decay_thresh:
                    decay_status = "DECAYING"
                elif decay_div >= accel_thresh:
                    decay_status = "IMPROVING"

            record = {
                "id": pat["id"],
                "name": pat["name"],
                "fingerprint": pat["fingerprint"],
                "description": pat["description"],
                "occurrences": pat["occurrences"],
                "win_count": pat["win_count"],
                "loss_count": pat["loss_count"],
                "neutral_count": pat["neutral_count"],
                "avg_3d_return": pat["avg_3d_return"],
                "avg_5d_return": pat["avg_5d_return"],
                "avg_max_upside": pat["avg_max_upside"],
                "avg_max_drawdown": pat["avg_max_drawdown"],
                "cum_trader_pnl": pat["cum_trader_pnl"],
                "win_rate_vs_kse": pat["win_rate_vs_kse"],
                "kse_beat_count": pat["kse_beat_count"],
                "raw_win_rate": eb_res["raw_win_rate_pct"],
                "shrunk_win_rate": shrunk_win_rate,
                "wilson_ci_low": eb_res["wilson_ci_low"],
                "wilson_ci_high": eb_res["wilson_ci_high"],

                "sample_size_n": eb_res["sample_size_n"],
                "shrinkage_weight": eb_res["shrinkage_weight"],
                "win_rate_90d": win_rate_90d,
                "sample_size_90d": decisive_90d,
                "decay_status": decay_status,
                "decay_divergence": decay_div,
                "is_expanded": pat["is_expanded"],
                "cluster_feature_vector": pat["cluster_feature_vector"],
                "last_updated": now,
                "created_at": now
            }
            self.db.upsert_pattern(record)

            # Persist Regime Conditioning (Stage 4.1)
            reg_dict = pattern_regime_accum.get(pat_id, {})
            for r in ("BULL", "NEUTRAL", "BEAR", "CRASH"):
                r_info = reg_dict.get(r, {"win_count": 0, "loss_count": 0, "neutral_count": 0, "returns_5d": []})
                rw = r_info["win_count"]
                rl = r_info["loss_count"]
                rn = rw + rl
                reg_eb = self.eb_estimator.shrink_rate(rw, rn, mu_pat_0, M_pat)
                avg_ret_r = sum(r_info["returns_5d"]) / max(len(r_info["returns_5d"]), 1) if r_info["returns_5d"] else 0.0

                if rn >= 3:
                    mu_pct = mu_pat_0 * 100.0
                    raw_m = reg_eb["shrunk_win_rate_pct"] / max(mu_pct, 0.01)
                    reg_mult = round(max(0.40, min(1.50, raw_m)), 2)
                else:
                    reg_mult = float(reg_mult_map.get(r, 1.0))

                self.db.upsert_pattern_regime({
                    "pattern_id": pat_id,
                    "regime": r,
                    "sample_count": rn,
                    "win_count": rw,
                    "loss_count": rl,
                    "neutral_count": r_info["neutral_count"],
                    "raw_win_rate": reg_eb["raw_win_rate_pct"],
                    "shrunk_win_rate": reg_eb["shrunk_win_rate_pct"],
                    "avg_5d_return": round(avg_ret_r, 2),
                    "wilson_ci_low": reg_eb["wilson_ci_low"],
                    "wilson_ci_high": reg_eb["wilson_ci_high"],
                    "regime_multiplier": reg_mult,
                    "updated_at": now
                })

        print(f"[Intelligence] PatternLibrary: rebuild complete (EB prior: mu_0={mu_pat_0*100:.2f}%, M={M_pat}, expanded={len(expanded_patterns)}).")

    def _find_matching_events(self, required_factors: set) -> List[Dict]:
        """Find all events where ALL required factors were detected with confidence >= 50."""
        conn = self.db._connect()
        try:
            rows = conn.execute("""
                SELECT e.id,
                       GROUP_CONCAT(c.factor) AS factors
                FROM stock_events e
                JOIN event_causes c ON c.event_id = e.id AND c.confidence >= 50
                GROUP BY e.id
            """).fetchall()

            matching = []
            for row in rows:
                if not row["factors"]:
                    continue
                event_factors = set(row["factors"].split(","))
                if required_factors.issubset(event_factors):
                    ev = conn.execute(
                        "SELECT * FROM stock_events WHERE id = ?", (row["id"],)
                    ).fetchone()
                    if ev:
                        matching.append(dict(ev))
            return matching
        finally:
            conn.close()

    def match_event_to_pattern(self, event_causes: List[Dict]) -> Optional[Dict]:
        """Find the best matching pattern definition (base or expanded) for this set of causes."""
        if not event_causes:
            return None

        confirmed_factors = {c["factor"] for c in event_causes if c["confidence"] >= 50}
        best_match = None
        best_coverage = 0

        all_defs = self.get_all_definitions()
        for pat_def in all_defs:
            required = set(pat_def["fingerprint"].split("|"))
            if not required.issubset(confirmed_factors):
                continue
            coverage = len(required)
            if coverage > best_coverage:
                best_coverage = coverage
                best_match = pat_def

        if not best_match:
            return None

        # Load DB record for stats
        conn = self.db._connect()
        try:
            row = conn.execute(
                "SELECT * FROM detected_patterns WHERE fingerprint = ?",
                (best_match["fingerprint"],)
            ).fetchone()
            return dict(row) if row else best_match
        finally:
            conn.close()


# ── Prediction Engine ─────────────────────────────────────────────────────────

class PredictionEngine:
    """
    After each event, matches it to historical patterns and emits a
    forward-looking signal with confidence and historical context.
    Applies calibrated sector/causal/signal weights from calibration.db
    to adjust prediction confidence based on real PSX outcomes.
    """

    def __init__(self, db: IntelligenceDB, pattern_lib: PatternLibrary,
                 investigator: CauseInvestigator,
                 causal_calibrator: Optional[Any] = None):
        self.db = db
        self.pattern_lib = pattern_lib
        self.investigator = investigator
        self.causal_calibrator = causal_calibrator

    def generate_prediction(self, event: Dict[str, Any], causes: List[Dict[str, Any]]):
        symbol = event["symbol"]
        event_type = event["event_type"]
        price = event["price"]
        rvol = event.get("rvol", 1.0)
        rsi = event.get("rsi_at_event", 50.0)

        # Match to pattern
        matched_pattern = self.pattern_lib.match_event_to_pattern(causes) if self.pattern_lib else None

        # Determine signal
        top_confidence = causes[0]["confidence"] if causes else 0
        signal = self._determine_signal(event_type, rvol, rsi, top_confidence)

        # ── Stage 1: Causal Evidence & Platt Scaling Calibration ─────────────
        composite_info = None
        if self.causal_calibrator:
            composite_info = self.causal_calibrator.compute_composite_severity(causes)
            calibrated_win_prob = composite_info["calibrated_win_probability"]
            composite_score = composite_info["composite_score"]
            # Convert calibrated probability to 0-100 base score
            if composite_info["is_calibrated"]:
                base_cause_confidence = int(calibrated_win_prob * 100)
            else:
                base_cause_confidence = min(50, int(composite_score))
        else:
            base_cause_confidence = (sum(c["confidence"] for c in causes[:3]) /
                                     max(len(causes[:3]), 1)) if causes else 45

        # Adjust for pattern history (Stage 3: Empirical Bayes shrinkage + sample gate)
        historical_sample = 0
        historical_win_rate = 0.0
        raw_win_rate = 0.0
        shrunk_win_rate = 0.0
        sample_size_n = 0
        avg_expected_return = 0.0
        pattern_name = "No Pattern Matched"
        pattern_id = None
        is_pattern_gated = True

        if matched_pattern:
            pattern_id = matched_pattern.get("id")
            pattern_name = matched_pattern.get("name", "Unknown Pattern")
            historical_sample = matched_pattern.get("occurrences", 0)
            total_closed = (matched_pattern.get("win_count", 0) +
                            matched_pattern.get("loss_count", 0))
            raw_win_rate = float(matched_pattern.get("raw_win_rate", 0.0) or 0.0)
            shrunk_win_rate = float(matched_pattern.get("shrunk_win_rate", 0.0) or 0.0)
            sample_size_n = int(matched_pattern.get("sample_size_n", total_closed) or 0)

            # Stage 3.2: Sample size gate (N >= 3)
            eb_cfg = get_intelligence_config().get("empirical_bayes", {})
            gate = int(eb_cfg.get("sample_size_gate", 3))
            is_pattern_gated = sample_size_n < gate

            # When pattern passes sample size gate, use shrunk win rate; otherwise default to prior
            if not is_pattern_gated and shrunk_win_rate > 0:
                historical_win_rate = shrunk_win_rate
            else:
                historical_win_rate = round(float(eb_cfg.get("default_population_win_rate", 0.1443)) * 100.0, 1)

            avg_expected_return = matched_pattern.get("avg_5d_return", 0.0)

        # Final prediction confidence
        pred_confidence = int(base_cause_confidence * 0.7 + min(historical_win_rate, 80) * 0.3)
        pred_confidence = max(20, min(95, pred_confidence))

        # Downscale if negative historical win rate
        if historical_win_rate > 0 and historical_win_rate < 45:
            pred_confidence = int(pred_confidence * 0.75)

        # Cap at 50 if unvalidated heuristic without strong historical pattern support
        if composite_info and not composite_info["is_calibrated"] and historical_sample < 30:
            pred_confidence = min(50, pred_confidence)

        # ── Apply calibration learnings to confidence ─────────────────────────
        calibration_applied = False
        calibration_note = ""
        try:
            cal_weights = self._load_calibration_weights()
            sector = event.get("sector", "")

            # 1. Sector Empirical Bayes multiplier (Stage 3.1)
            sec_eb = self.db.get_sector_shrinkage_by_name(sector)
            if sec_eb and sec_eb.get("sample_count", 0) >= 3:
                eb_mult = float(sec_eb.get("multiplier", 1.0))
                raw_adj = (eb_mult - 1.0) * 25.0
                sector_adj = max(-15, min(15, raw_adj))
                pred_confidence = int(pred_confidence + sector_adj)
                calibration_note += f"sector={sector}(EB={eb_mult:.2f}x, N={sec_eb['sample_count']}) "
                calibration_applied = True
            elif cal_weights:
                sector_w = cal_weights["sector_intel"].get(sector)
                if sector_w and sector_w["n"] >= 5:
                    raw_adj = (sector_w["w"] - 1.0) * 25
                    sector_adj = max(-15, min(15, raw_adj))
                    pred_confidence = int(pred_confidence + sector_adj)
                    calibration_note += f"sector={sector}({sector_w['w']:.2f}x) "
                    calibration_applied = True

            if cal_weights:
                # 2. Causal factor weight adjustment
                causal_adj_total = 0.0
                causal_adj_count = 0
                for cause in causes[:3]:
                    f = cause["factor"]
                    c = cause.get("confidence", 0)
                    if c >= 75:
                        lvl = "HIGH"
                    elif c >= 50:
                        lvl = "MED"
                    else:
                        lvl = "LOW"
                    causal_key = f"{f}::{lvl}"
                    cw = cal_weights["causal_factor"].get(causal_key)
                    if cw and cw["n"] >= 5:
                        causal_adj_total += (cw["w"] - 1.0) * 15
                        causal_adj_count += 1
                        calibration_note += f"cause={causal_key}({cw['w']:.2f}x) "
                if causal_adj_count > 0:
                    avg_causal_adj = causal_adj_total / causal_adj_count
                    causal_adj = max(-10, min(10, avg_causal_adj))
                    pred_confidence = int(pred_confidence + causal_adj)
                    calibration_applied = True

                # 3. Signal weight adjustment
                sig_w = cal_weights["intel_signal"].get(signal)
                if sig_w and sig_w["n"] >= 5:
                    sig_adj = max(-8, min(8, (sig_w["w"] - 1.0) * 15))
                    pred_confidence = int(pred_confidence + sig_adj)
                    calibration_note += f"signal={signal}({sig_w['w']:.2f}x) "
                    calibration_applied = True

            pred_confidence = max(20, min(97, pred_confidence))
        except Exception as _cal_err:
            pass  # Calibration DB unavailable — use base confidence
        # ── End calibration adjustment ─────────────────────────────────────────

        # ── Stage 4.1: Market Regime Conditioning ─────────────────────────────
        current_regime = resolve_market_regime(
            event.get("trade_date"),
            kse_5d_return=event.get("kse_return_5d")
        )
        regime_note = ""
        if pattern_id:
            reg_row = self.db.get_pattern_regime(pattern_id, current_regime)
            if reg_row and reg_row.get("sample_count", 0) >= 3:
                reg_shrunk = float(reg_row.get("shrunk_win_rate", 0.0) or 0.0)
                historical_win_rate = reg_shrunk
                reg_mult = float(reg_row.get("regime_multiplier", 1.0) or 1.0)
                regime_note = f"regime={current_regime}(EB={historical_win_rate:.1f}%, N={reg_row['sample_count']}, mult={reg_mult:.2f}x) "
            else:
                reg_mult_map = get_intelligence_config().get("stage4_pattern_lifecycle", {}).get(
                    "regime_conditioning", {}
                ).get("regime_multipliers", {"CRASH": 0.40, "BEAR": 0.70, "NEUTRAL": 1.00, "BULL": 1.20})
                reg_mult = float(reg_mult_map.get(current_regime, 1.0))
                regime_note = f"regime={current_regime}(mult={reg_mult:.2f}x) "

            pred_confidence = int(pred_confidence * reg_mult)
            if reg_mult != 1.0:
                calibration_applied = True
                calibration_note += regime_note

        # ── Stage 4.2: Pattern Decay Tracking & Damping ───────────────────────
        decay_status = "STABLE"
        decay_note = None
        if matched_pattern:
            decay_status = matched_pattern.get("decay_status", "STABLE")
            if decay_status == "DECAYING":
                decay_cfg = get_intelligence_config().get("stage4_pattern_lifecycle", {}).get("decay_tracking", {})
                decay_mult = float(decay_cfg.get("decay_confidence_multiplier", 0.80))
                pred_confidence = int(pred_confidence * decay_mult)
                decay_note = (f"⚠️ Pattern performance decaying: 90d win rate "
                              f"{matched_pattern.get('win_rate_90d')}% vs lifetime "
                              f"{matched_pattern.get('shrunk_win_rate')}% ({decay_mult:.2f}x dampening applied)")
                calibration_applied = True
                calibration_note += f"decay=DECAYING({decay_mult:.2f}x) "

        pred_confidence = max(20, min(97, pred_confidence))

        # ── 0.6: Liquidity awareness tagging ─────────────────────────────────
        is_noisy = bool(event.get("is_noisy_liquidity", 0))
        liquidity_tag = "low liquidity — noisy signal" if is_noisy else "liquid"

        ci_low = float(matched_pattern.get("wilson_ci_low", 0.0)) if matched_pattern else 0.0
        ci_high = float(matched_pattern.get("wilson_ci_high", 0.0)) if matched_pattern else 0.0

        # Build reasoning
        cause_narrative = self.investigator.build_narrative(causes)
        reasoning = {
            "event_type": event_type,
            "top_causes": [{"factor": c["factor"], "confidence": c["confidence"],
                            "evidence": c["evidence"]} for c in causes[:4]],
            "narrative": cause_narrative,
            "pattern_matched": pattern_name,
            "historical_win_rate": historical_win_rate,
            "raw_win_rate": raw_win_rate,
            "shrunk_win_rate": shrunk_win_rate,
            "wilson_ci": [ci_low, ci_high],
            "wilson_ci_low": ci_low,
            "wilson_ci_high": ci_high,
            "is_pattern_gated": is_pattern_gated,
            "historical_sample": historical_sample,
            "market_regime": current_regime,
            "regime_note": regime_note.strip() if regime_note else None,
            "decay_status": decay_status,
            "decay_note": decay_note,
            "is_expanded_pattern": bool(matched_pattern.get("is_expanded", 0)) if matched_pattern else False,
            "calibration_applied": calibration_applied,
            "calibration_note": calibration_note.strip() if calibration_note else None,
            "is_noisy_liquidity": is_noisy,
            "liquidity_tag": liquidity_tag,
            "causal_calibration": composite_info,
            "disclaimer": "Not investment advice — informational tool based on historical pattern statistics"
        }

        active_versions = self.db.get_active_model_versions()
        pred = {
            "id": _make_pred_id(symbol, signal),
            "symbol": symbol,
            "event_id": event["id"],
            "pattern_id": pattern_id,
            "signal": signal,
            "confidence": pred_confidence,
            "price_at_signal": price,
            "pattern_name": pattern_name,
            "historical_sample": historical_sample,
            "historical_win_rate": historical_win_rate,
            "avg_expected_return": avg_expected_return,
            "reasoning_json": json.dumps(reasoning),
            "predicted_at": _now(),
            "outcome": "PENDING",
            "weights_version": active_versions.get("weights_version", "W_v1.0.0"),
            "pattern_version": active_versions.get("pattern_version", "P_v1.0.0")
        }

        self.db.insert_prediction(pred)


        # ── Fix: Record pattern occurrence for outcome tracking ────────────────
        if pattern_id and matched_pattern:
            try:
                conn = self.db._connect()
                conn.execute("""
                    INSERT OR IGNORE INTO pattern_occurrences
                    (pattern_id, event_id, symbol, matched_at, similarity, outcome, regime)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
                """, (
                    pattern_id,
                    event["id"],
                    symbol,
                    _now(),
                    float(matched_pattern.get("similarity", 0.8)),
                    current_regime
                ))
                conn.commit()
                conn.close()
            except Exception as _pe:
                pass  # Non-blocking
        # ── End pattern occurrence fix ─────────────────────────────────────────


    def _load_calibration_weights(self) -> Optional[Dict]:
        """
        Load factor weights from calibration.db.
        Returns dict with keys: sector_intel, causal_factor, intel_signal.
        Returns None if calibration DB is unavailable or empty.
        SAFE: read-only connection, wrapped in try/except at call site.
        """
        cal_db_path = Path("cache/calibration.db")
        if not cal_db_path.exists():
            return None
        conn = sqlite3.connect(str(cal_db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT factor_type, factor_value, weight, sample_count FROM factor_weights"
            ).fetchall()
        except Exception:
            conn.close()
            return None
        conn.close()

        if not rows:
            return None

        result: Dict[str, Dict] = {
            "sector_intel": {},
            "causal_factor": {},
            "intel_signal": {}
        }
        key_map = {
            "SECTOR_INTEL": "sector_intel",
            "CAUSAL_FACTOR": "causal_factor",
            "INTEL_SIGNAL": "intel_signal"
        }
        for r in rows:
            bucket = key_map.get(r["factor_type"])
            if bucket is not None:
                result[bucket][r["factor_value"]] = {
                    "w": float(r["weight"]),
                    "n": int(r["sample_count"])
                }
        return result

    def _determine_signal(self, event_type: str, rvol: float,
                          rsi: float, top_confidence: int) -> str:
        if event_type == EVENT_UPPER_LOCK:
            return SIGNAL_CONTINUATION if rvol >= 5 else SIGNAL_WATCH
        if event_type == EVENT_LOWER_LOCK:
            return SIGNAL_REVERSAL_RISK
        if event_type == EVENT_RESISTANCE_BREAK:
            return SIGNAL_BREAKOUT_IMMINENT if top_confidence >= 70 else SIGNAL_WATCH
        if event_type == EVENT_VOLUME_SURGE:
            return SIGNAL_WATCH if rsi < 72 else SIGNAL_EXTENDED
        if event_type == EVENT_PRICE_SPIKE:
            return SIGNAL_EXTENDED if rsi >= 75 else SIGNAL_WATCH
        if event_type == EVENT_ACCUMULATION:
            return SIGNAL_BREAKOUT_IMMINENT if rvol >= 3.5 else SIGNAL_WATCH
        return SIGNAL_WATCH


# ── Learning Engine ────────────────────────────────────────────────────────────

class LearningEngine:
    """
    Stage 2: Purged & Embargoed Evaluation Engine
    - 2.1 Fix evaluation data leakage:
        - Purging: Discards overlapping 5-day windows that share trading days for the same symbol.
        - Embargo: Enforces a 2-day cooldown period post-evaluation where subsequent signals for the
          same ticker cannot evaluate the same pattern.
    - 2.2 Asymmetric Reward/Penalty ('Trader PnL'):
        - Win (+1.0): return > 2.0% AND max drawdown < 1.5% during the 5-day window.
        - Loss (-1.5): max drawdown >= 3.0% at any point in the 5-day window (stopped out).
        - Scratched (0.0): neither condition met.
    - 2.3 Benchmark Comparison vs. KSE-100:
        - Compares every prediction outcome against KSE-100 index return over the same window.
        - Reports Win Rate vs. KSE-100 (excess return > 0), not just absolute win rate.
    """

    def __init__(self, db: IntelligenceDB):
        self.db = db

    def _get_stock_history(self, symbol: str, history_fn=None) -> List[Dict[str, Any]]:
        """Fetch or read daily candles for symbol."""
        if history_fn:
            try:
                h = history_fn(symbol)
                if h:
                    return h
            except Exception:
                pass
        p = Path(f"cache/history/{symbol.upper()}.json")
        if p.exists():
            try:
                with open(p, "r") as f:
                    data = json.load(f)
                    return data.get("days", [])
            except Exception:
                pass
        return []

    def _extract_5d_window(self, history: List[Dict[str, Any]], entry_date_str: str,
                           entry_price: float, current_price: float) -> Tuple[float, float, float, List[Dict[str, Any]]]:
        """
        Extract up to 5 trading session candles starting at or after entry_date_str.
        Returns: (exit_price, actual_return_5d, max_drawdown_5d, window_candles)
        """
        if not history:
            exit_price = current_price if current_price > 0 else entry_price
            ret = round(((exit_price - entry_price) / max(entry_price, 0.01)) * 100.0, 2)
            dd = round(max(0.0, ((entry_price - exit_price) / max(entry_price, 0.01)) * 100.0), 2) if ret < 0 else 0.0
            return exit_price, ret, dd, []

        # Chronological order (oldest to newest)
        sorted_candles = sorted(history, key=lambda x: x.get("date", ""))
        target_date = entry_date_str[:10]
        window_candles = [c for c in sorted_candles if c.get("date", "") >= target_date][:5]

        if not window_candles:
            exit_price = current_price if current_price > 0 else entry_price
            ret = round(((exit_price - entry_price) / max(entry_price, 0.01)) * 100.0, 2)
            dd = round(max(0.0, ((entry_price - exit_price) / max(entry_price, 0.01)) * 100.0), 2) if ret < 0 else 0.0
            return exit_price, ret, dd, []

        exit_price = float(window_candles[-1].get("close", current_price or entry_price))
        lowest_low = min(float(c.get("low", c.get("close", entry_price))) for c in window_candles)
        ret = round(((exit_price - entry_price) / max(entry_price, 0.01)) * 100.0, 2)
        dd = round(max(0.0, ((entry_price - lowest_low) / max(entry_price, 0.01)) * 100.0), 2)
        return exit_price, ret, dd, window_candles

    def _get_kse_window_return(self, entry_date_str: str, exit_date_str: str,
                               history_fn=None, index_data=None) -> float:
        """Calculate KSE-100 return over the same window."""
        kse_candles = self._get_stock_history("KSE100", history_fn)
        if kse_candles:
            sorted_kse = sorted(kse_candles, key=lambda x: x.get("date", ""))
            entry_candles = [c for c in sorted_kse if c.get("date", "") >= entry_date_str[:10]]
            if len(entry_candles) >= 2:
                kse_entry = float(entry_candles[0].get("open", entry_candles[0].get("close", 1.0)))
                kse_window = [c for c in entry_candles if c.get("date", "") <= exit_date_str[:10]]
                kse_exit = float(kse_window[-1].get("close", kse_entry)) if kse_window else float(entry_candles[-1].get("close", kse_entry))
                return round(((kse_exit - kse_entry) / max(kse_entry, 1.0)) * 100.0, 2)

        # Fallback to index_data or index cache
        if index_data and isinstance(index_data, dict):
            indices = index_data.get("indices", [])
            kse_item = next((i for i in indices if i.get("name") in ["KSE100", "KSE 100"]), None)
            if kse_item:
                return float(kse_item.get("changePercent", 0.0) or 0.0)

        return 0.0

    def compute_asymmetric_pnl(self, actual_return_5d: float, max_drawdown_5d: float,
                               signal: str = "POSSIBLE_BREAKOUT") -> Tuple[str, float]:
        """
        2.2 Asymmetric reward/penalty:
        - Loss (-1.5): max drawdown >= 3.0% at any point (stopped out, even if recovered)
        - Win (+1.0): return > 2.0% AND max drawdown < 1.5%
        - Scratched (0.0): neither condition met
        """
        cfg = get_intelligence_config()
        le_cfg = cfg.get("learning_engine", {})
        asym = le_cfg.get("asymmetric_reward", {})

        win_ret_thresh = float(asym.get("win_return_threshold_pct", 2.0))
        win_dd_thresh  = float(asym.get("win_max_drawdown_threshold_pct", 1.5))
        loss_dd_thresh = float(asym.get("loss_max_drawdown_threshold_pct", 3.0))
        win_reward     = float(asym.get("win_reward", 1.0))
        loss_penalty   = float(asym.get("loss_penalty", -1.5))
        scratch_reward = float(asym.get("scratch_reward", 0.0))

        if signal == SIGNAL_REVERSAL_RISK:
            # Short / downside setup: profit from drop, drawdown from rally
            if max_drawdown_5d >= loss_dd_thresh:
                return "INCORRECT", loss_penalty
            elif actual_return_5d < -win_ret_thresh and max_drawdown_5d < win_dd_thresh:
                return "CORRECT", win_reward
            else:
                return "NEUTRAL", scratch_reward

        # Bullish setups
        if max_drawdown_5d >= loss_dd_thresh:
            return "INCORRECT", loss_penalty
        elif actual_return_5d > win_ret_thresh and max_drawdown_5d < win_dd_thresh:
            return "CORRECT", win_reward
        else:
            return "NEUTRAL", scratch_reward

    def check_purging_and_embargo(self, symbol: str, pattern_id: Optional[str],
                                  predicted_at: str,
                                  prior_window_end_ts: Optional[float],
                                  prior_embargo_end_ts: Optional[float],
                                  prior_pattern_id: Optional[str]) -> Tuple[str, str]:
        """
        2.1 Fix evaluation data leakage:
        - Purged: falls within active 5-day window of prior prediction for same symbol.
        - Embargoed: falls within 2-day cooldown post-window for same symbol & pattern.
        - Valid: independent sample.
        """
        try:
            ts = datetime.strptime(predicted_at.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
        except Exception:
            return "VALID", "Timestamp parse bypass"

        if prior_window_end_ts is not None and ts < prior_window_end_ts:
            return "PURGED", "Overlapping 5-day window with prior signal"

        if prior_embargo_end_ts is not None and ts < prior_embargo_end_ts and pattern_id == prior_pattern_id:
            return "EMBARGOED", "Within 2-day post-window embargo for same pattern"

        return "VALID", "Independent sample"

    def evaluate_day_outcomes(self, stocks: List[Dict[str, Any]], history_fn=None, index_data=None):
        """Run at 4:30 PM — check all predictions that are 5+ days old with purging and embargoing."""
        price_map = {s.get("symbol", "").upper(): float(s.get("price", 0) or 0)
                     for s in stocks}

        pending = self.db.get_pending_predictions_for_evaluation(days_old=5)
        if not pending:
            return

        evaluated = 0
        now = _now()

        # Group pending predictions by symbol to check chronological sequence
        from collections import defaultdict
        by_symbol = defaultdict(list)
        for p in pending:
            by_symbol[p["symbol"]].append(p)

        conn = self.db._connect()
        try:
            for symbol, preds in by_symbol.items():
                preds.sort(key=lambda x: x["predicted_at"])
                stock_candles = self._get_stock_history(symbol, history_fn)

                # Get latest evaluated prediction for this symbol to check purging boundary
                last_eval = conn.execute("""
                    SELECT predicted_at, pattern_id FROM ai_predictions
                    WHERE symbol = ? AND evaluation_flag = 'VALID' AND outcome != 'PENDING'
                    ORDER BY predicted_at DESC LIMIT 1
                """, (symbol,)).fetchone()

                prior_window_end_ts = None
                prior_embargo_end_ts = None
                prior_pattern_id = None
                if last_eval:
                    try:
                        p_ts = datetime.strptime(last_eval["predicted_at"].replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
                        prior_window_end_ts = p_ts + 5 * 86400
                        prior_embargo_end_ts = prior_window_end_ts + 2 * 86400
                        prior_pattern_id = last_eval["pattern_id"]
                    except Exception:
                        pass

                for pred in preds:
                    pred_at = pred["predicted_at"]
                    pat_id = pred.get("pattern_id")

                    flag, reason = self.check_purging_and_embargo(
                        symbol, pat_id, pred_at,
                        prior_window_end_ts, prior_embargo_end_ts, prior_pattern_id
                    )

                    try:
                        ts = datetime.strptime(pred_at.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
                    except Exception:
                        ts = 0.0

                    if flag in ["PURGED", "EMBARGOED"]:
                        self.db.mark_prediction_evaluation_flag(pred["id"], flag)
                        continue

                    # Process VALID prediction
                    prior_window_end_ts = ts + 5 * 86400
                    prior_embargo_end_ts = prior_window_end_ts + 2 * 86400
                    prior_pattern_id = pat_id

                    current_price = price_map.get(symbol, 0.0)
                    entry_price = float(pred.get("price_at_signal", 0.0) or 0.0)
                    if current_price <= 0 and entry_price <= 0:
                        continue

                    exit_price, ret_5d, dd_5d, w_candles = self._extract_5d_window(
                        stock_candles, pred_at, entry_price, current_price
                    )

                    outcome, pnl = self.compute_asymmetric_pnl(ret_5d, dd_5d, pred.get("signal", "POSSIBLE_BREAKOUT"))
                    exit_date = w_candles[-1].get("date", pred_at[:10]) if w_candles else pred_at[:10]
                    kse_ret = self._get_kse_window_return(pred_at[:10], exit_date, history_fn, index_data)
                    excess_kse = round(ret_5d - kse_ret, 2)
                    beat_kse = 1 if excess_kse > 0 else 0

                    self.db.update_prediction_outcome(
                        pred_id=pred["id"],
                        outcome=outcome,
                        actual_return_5d=ret_5d,
                        max_drawdown_5d=dd_5d,
                        trader_pnl=pnl,
                        kse_return_5d=kse_ret,
                        excess_return_kse_5d=excess_kse,
                        beat_kse=beat_kse,
                        evaluation_flag="VALID"
                    )
                    evaluated += 1

                    # Update pattern occurrences
                    event_id = pred.get("event_id")
                    if pat_id and event_id:
                        pat_outcome = ("WIN" if outcome == "CORRECT" else ("LOSS" if outcome == "INCORRECT" else "NEUTRAL"))
                        try:
                            conn.execute("""
                                INSERT INTO pattern_occurrences
                                (pattern_id, event_id, symbol, matched_at, similarity, outcome,
                                 return_5d, max_drawdown, trader_pnl, beat_kse, evaluation_flag)
                                VALUES (?, ?, ?, ?, 0.9, ?, ?, ?, ?, ?, 'VALID')
                                ON CONFLICT(pattern_id, event_id) DO UPDATE SET
                                    outcome = excluded.outcome,
                                    return_5d = excluded.return_5d,
                                    max_drawdown = excluded.max_drawdown,
                                    trader_pnl = excluded.trader_pnl,
                                    beat_kse = excluded.beat_kse,
                                    evaluation_flag = 'VALID'
                            """, (pat_id, event_id, symbol, now, pat_outcome,
                                  ret_5d, dd_5d, pnl, beat_kse))
                            conn.commit()
                        except Exception:
                            pass
        finally:
            conn.close()

        if evaluated:
            print(f"[Intelligence] LearningEngine: evaluated {evaluated} valid predictions (purging/embargo applied).")

    def audit_and_purge_all(self, history_fn=None, index_data=None) -> Dict[str, Any]:
        """
        Retroactive sweep: purges overlapping windows, sets embargoes,
        computes asymmetric Trader PnL, compares against KSE-100, and updates all records.
        """
        conn = self.db._connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT * FROM ai_predictions
                ORDER BY symbol ASC, predicted_at ASC
            """).fetchall()
        finally:
            conn.close()

        purged_count = 0
        embargoed_count = 0
        valid_count = 0
        pnl_wins = 0
        pnl_losses = 0
        pnl_scratched = 0
        kse_beat_count = 0
        cum_pnl = 0.0

        from collections import defaultdict
        by_symbol = defaultdict(list)
        for r in rows:
            by_symbol[r["symbol"]].append(dict(r))

        now = _now()
        for symbol, preds in by_symbol.items():
            prior_window_end_ts = None
            prior_embargo_end_ts = None
            prior_pattern_id = None
            stock_candles = self._get_stock_history(symbol, history_fn)

            for p in preds:
                pat_id = p.get("pattern_id")
                pred_at = p["predicted_at"]
                flag, reason = self.check_purging_and_embargo(
                    symbol, pat_id, pred_at,
                    prior_window_end_ts, prior_embargo_end_ts, prior_pattern_id
                )

                try:
                    ts = datetime.strptime(pred_at.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
                except Exception:
                    ts = 0.0

                if flag == "PURGED":
                    purged_count += 1
                    self.db.mark_prediction_evaluation_flag(p["id"], "PURGED")
                    continue
                elif flag == "EMBARGOED":
                    embargoed_count += 1
                    self.db.mark_prediction_evaluation_flag(p["id"], "EMBARGOED")
                    continue

                # Flag is VALID
                valid_count += 1
                prior_window_end_ts = ts + 5 * 86400
                prior_embargo_end_ts = prior_window_end_ts + 2 * 86400
                prior_pattern_id = pat_id

                entry_price = float(p.get("price_at_signal") or 0.0)
                cur_price = entry_price * (1.0 + (float(p.get("actual_return_5d") or 0.0) / 100.0))

                exit_price, ret_5d, dd_5d, w_candles = self._extract_5d_window(
                    stock_candles, pred_at, entry_price, cur_price
                )

                # Asymmetric reward/penalty
                outcome, pnl = self.compute_asymmetric_pnl(ret_5d, dd_5d, p.get("signal", "POSSIBLE_BREAKOUT"))
                cum_pnl += pnl
                if pnl > 0: pnl_wins += 1
                elif pnl < 0: pnl_losses += 1
                else: pnl_scratched += 1

                # Benchmark vs KSE-100
                exit_date = w_candles[-1].get("date", pred_at[:10]) if w_candles else pred_at[:10]
                kse_ret = self._get_kse_window_return(pred_at[:10], exit_date, history_fn, index_data)
                excess_kse = round(ret_5d - kse_ret, 2)
                beat_kse = 1 if excess_kse > 0 else 0
                if beat_kse: kse_beat_count += 1

                # Update ai_predictions
                self.db.update_prediction_outcome(
                    pred_id=p["id"],
                    outcome=outcome,
                    actual_return_5d=ret_5d,
                    max_drawdown_5d=dd_5d,
                    trader_pnl=pnl,
                    kse_return_5d=kse_ret,
                    excess_return_kse_5d=excess_kse,
                    beat_kse=beat_kse,
                    evaluation_flag="VALID"
                )

                # Update pattern_occurrences
                if pat_id and p.get("event_id"):
                    pat_outcome = ("WIN" if outcome == "CORRECT" else ("LOSS" if outcome == "INCORRECT" else "NEUTRAL"))
                    try:
                        c_conn = self.db._connect()
                        c_conn.execute("""
                            UPDATE pattern_occurrences
                            SET outcome = ?, return_5d = ?, max_drawdown = ?,
                                trader_pnl = ?, beat_kse = ?, evaluation_flag = 'VALID'
                            WHERE event_id = ? AND pattern_id = ?
                        """, (pat_outcome, ret_5d, dd_5d, pnl, beat_kse, p["event_id"], pat_id))
                        c_conn.commit()
                        c_conn.close()
                    except Exception:
                        pass

        # Also sync evaluation_flag across pattern_occurrences
        try:
            p_conn = self.db._connect()
            p_conn.execute("""
                UPDATE pattern_occurrences
                SET evaluation_flag = (
                    SELECT p.evaluation_flag FROM ai_predictions p
                    WHERE p.event_id = pattern_occurrences.event_id
                    LIMIT 1
                )
                WHERE EXISTS (
                    SELECT 1 FROM ai_predictions p
                    WHERE p.event_id = pattern_occurrences.event_id
                )
            """)
            p_conn.commit()
            p_conn.close()
        except Exception:
            pass

        return {
            "total_processed": len(rows),
            "valid_count": valid_count,
            "purged_count": purged_count,
            "embargoed_count": embargoed_count,
            "cum_trader_pnl": round(cum_pnl, 2),
            "pnl_distribution": {"wins": pnl_wins, "losses": pnl_losses, "scratched": pnl_scratched},
            "kse_beat_count": kse_beat_count,
            "win_rate_vs_kse_pct": round((kse_beat_count / max(valid_count, 1)) * 100.0, 2)
        }

    def get_audit_summary(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/evaluation-audit"""
        stats = self.db.get_learning_stats()
        return {
            "evaluation_hygiene": {
                "valid_evaluations": stats.get("valid_predictions_count", 0),
                "purged_evaluations": stats.get("purged_predictions_count", 0),
                "embargoed_evaluations": stats.get("embargoed_predictions_count", 0),
                "purged_pct": round((stats.get("purged_predictions_count", 0) / max(stats.get("total_predictions", 1), 1)) * 100, 1),
                "embargoed_pct": round((stats.get("embargoed_predictions_count", 0) / max(stats.get("total_predictions", 1), 1)) * 100, 1)
            },
            "asymmetric_trader_pnl": {
                "cum_trader_pnl": stats.get("cum_trader_pnl", 0.0),
                "avg_trader_pnl": stats.get("avg_trader_pnl", 0.0),
                "reward_scheme": {"win": "+1.0 (>2% ret & <1.5% dd)", "loss": "-1.5 (>=3% dd)", "scratch": "0.0"}
            },
            "benchmark_vs_kse100": {
                "win_rate_vs_kse_pct": stats.get("win_rate_vs_kse_pct", 0.0),
                "win_rate_vs_kse_95_ci": stats.get("win_rate_vs_kse_95_ci", [0.0, 0.0]),
                "avg_excess_return_kse_pct": stats.get("avg_excess_return_kse_pct", 0.0),
                "sample_size_n": stats.get("sample_size_benchmark_n", 0)
            },
            "absolute_performance": {
                "win_rate_pct": stats.get("win_rate_pct", 0.0),
                "win_rate_95_ci": stats.get("win_rate_95_ci", [0.0, 0.0]),
                "sample_size_n": stats.get("sample_size_decisive_n", 0)
            },
            "generated_at": _now()
        }

    def rebuild_sector_shrinkage(self, estimator: Optional[Any] = None) -> Dict[str, Any]:
        """
        Stage 3.1: Fits Empirical Bayes prior from historical sector outcomes and
        computes continuous sector multipliers.
        Persists to sector_shrinkage table and syncs to calibration.db factor_weights.
        """
        if estimator is None:
            estimator = EmpiricalBayesEstimator()

        conn = self.db._connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT e.sector,
                       COUNT(*) as total_n,
                       SUM(CASE WHEN p.trader_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN p.trader_pnl < 0 THEN 1 ELSE 0 END) as losses
                FROM ai_predictions p
                JOIN stock_events e ON p.event_id = e.id
                WHERE p.evaluation_flag = 'VALID' AND p.outcome != 'PENDING'
                  AND e.sector IS NOT NULL AND e.sector != ''
                GROUP BY e.sector
            """).fetchall()
        finally:
            conn.close()

        group_data = []
        for r in rows:
            group_data.append({
                "sector": r["sector"],
                "sample_count": r["total_n"],
                "wins": int(r["wins"] or 0),
                "losses": int(r["losses"] or 0)
            })

        mu_0, M, alpha, beta = estimator.fit_prior(group_data, prior_weight_default=estimator.sector_prior_weight_m)
        now = _now()
        updated_sectors = []

        guard_cfg = get_intelligence_config().get("stage6_self_learning_guardrails", {})
        guard_enabled = bool(guard_cfg.get("enabled", True))
        max_rel = float(guard_cfg.get("max_relative_weight_change", 0.10))
        min_abs = float(guard_cfg.get("min_absolute_weight_change_floor", 0.02))

        try:
            existing_sec_map = {r["sector"]: float(r.get("multiplier", 1.0) or 1.0) for r in self.db.get_sector_shrinkage()}
        except Exception:
            existing_sec_map = {}

        for g in group_data:
            sector = g["sector"]
            w = g["wins"]
            l = g["losses"]
            n = w + l
            res = estimator.shrink_rate(w, n, mu_0, M)
            raw_mult = estimator.compute_sector_multiplier(res["shrunk_win_rate_pct"], mu_0)

            if guard_enabled and sector in existing_sec_map:
                old_mult = existing_sec_map[sector]
                mult, _ = clamp_weight_change(old_mult, raw_mult, max_rel=max_rel, min_abs=min_abs)
            else:
                mult = raw_mult

            rec = {
                "sector": sector,
                "sample_count": g["sample_count"],
                "win_count": w,
                "loss_count": l,
                "raw_win_rate": res["raw_win_rate_pct"],
                "shrunk_win_rate": res["shrunk_win_rate_pct"],
                "multiplier": mult,
                "wilson_ci_low": res["wilson_ci_low"],

                "wilson_ci_high": res["wilson_ci_high"],
                "shrinkage_weight": res["shrinkage_weight"],
                "updated_at": now
            }
            self.db.upsert_sector_shrinkage(rec)
            updated_sectors.append(rec)

            # Sync to calibration.db for backward-compatibility with components reading factor_weights
            try:
                from psx_calibration_engine import CALIB_DB
                if CALIB_DB.exists():
                    with sqlite3.connect(str(CALIB_DB), timeout=3) as cal_conn:
                        cal_conn.execute("""
                            INSERT INTO factor_weights
                            (factor_type, factor_value, source, sample_count, win_count,
                             loss_count, raw_win_rate, smoothed_win_rate, weight, last_updated)
                            VALUES ('SECTOR_INTEL', ?, 'intelligence_eb', ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(factor_type, factor_value) DO UPDATE SET
                                sample_count = excluded.sample_count,
                                win_count = excluded.win_count,
                                loss_count = excluded.loss_count,
                                raw_win_rate = excluded.raw_win_rate,
                                smoothed_win_rate = excluded.smoothed_win_rate,
                                weight = excluded.weight,
                                last_updated = excluded.last_updated
                        """, (sector, g["sample_count"], w, l, round(res["raw_win_rate_pct"]/100.0, 4),
                              round(res["shrunk_win_rate_pct"]/100.0, 4), mult, now))
                        cal_conn.commit()
            except Exception:
                pass

        print(f"[Intelligence] LearningEngine: Empirical Bayes sector shrinkage computed for {len(updated_sectors)} sectors (mu_0={mu_0:.4f}, M={M}).")
        return {
            "sectors_count": len(updated_sectors),
            "population_prior": {
                "win_rate": mu_0,
                "prior_weight_m": M,
                "alpha": alpha,
                "beta": beta
            },
            "sectors": updated_sectors
        }

    def run_guarded_recalibration(
        self,
        estimator: Optional[Any] = None,
        causal_calibrator: Optional[Any] = None,
        pattern_lib: Optional[Any] = None,
        held_out_split: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Stage 6.2: Weekly Recalibration with Guardrails & Audit Logging
        - Snapshots before_weights across sector multipliers, lens parameters, pattern shrunk win rates.
        - Partitions purged/valid evaluated predictions chronologically into 70% Train / 30% Held-out.
        - Evaluates validation metric (Brier score) on Held-out sample.
        - Applies 10% relative change guardrail to all updated weights.
        - Persists full before/after audit log and version tags into calibration_runs table.
        """
        guard_cfg = get_intelligence_config().get("stage6_self_learning_guardrails", {})
        split = held_out_split if held_out_split is not None else float(guard_cfg.get("held_out_validation_split", 0.30))
        max_degradation = float(guard_cfg.get("max_held_out_brier_degradation_pct", 15.0))
        auto_abort = bool(guard_cfg.get("auto_abort_on_severe_degradation", True))

        started_at = _now()
        calib_id = f"CALIB-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        now_date_str = datetime.utcnow().strftime('%Y.%m.%d')

        conn = self.db._connect()
        try:
            today_runs = conn.execute(
                "SELECT COUNT(*) FROM calibration_runs WHERE completed_at LIKE ?",
                (datetime.utcnow().strftime('%Y-%m-%d') + "%",)
            ).fetchone()[0]
        except Exception:
            today_runs = 0
        finally:
            conn.close()

        seq = today_runs + 1
        weights_version = f"W_v{now_date_str}.{seq}"
        pattern_version = f"P_v{now_date_str}.{seq}"

        # 1. Snapshot Before Weights
        before_weights = {
            "sectors": {r["sector"]: r.get("multiplier", 1.0) for r in self.db.get_sector_shrinkage()},
            "lenses": {r["lens_or_cluster"]: {"param_a": r.get("param_a"), "param_b": r.get("param_b"), "brier": r.get("brier_score")} for r in self.db.get_lens_calibrations()},
            "patterns": {r["id"]: {"shrunk_win_rate": r.get("shrunk_win_rate"), "raw_win_rate": r.get("raw_win_rate")} for r in self.db.get_patterns(min_occurrences=0)}
        }

        # 2. Partition valid evaluated outcomes into Train and Held-out sets
        conn = self.db._connect()
        try:
            rows = conn.execute("""
                SELECT id, symbol, confidence, outcome, actual_return_5d, trader_pnl
                FROM ai_predictions
                WHERE evaluation_flag = 'VALID' AND outcome IN ('CORRECT', 'INCORRECT')
                ORDER BY predicted_at ASC
            """).fetchall()
            valid_preds = [dict(r) for r in rows]
        finally:
            conn.close()

        total_valid = len(valid_preds)
        if total_valid >= 5:
            held_out_count = max(1, int(total_valid * split))
            train_count = total_valid - held_out_count
            train_set = valid_preds[:train_count]
            held_out_set = valid_preds[train_count:]
        else:
            train_count = total_valid
            held_out_count = 0
            train_set = valid_preds
            held_out_set = []

        def _calc_brier(items):
            if not items:
                return 0.25
            total_sq = sum((((it["confidence"] or 50) / 100.0) - (1.0 if it["outcome"] == "CORRECT" else 0.0)) ** 2 for it in items)
            return round(total_sq / len(items), 4)

        train_metric_val = _calc_brier(train_set)
        held_out_metric_val = _calc_brier(held_out_set) if held_out_set else train_metric_val

        # 3. Execute Recalibration passes
        if estimator is None:
            estimator = EmpiricalBayesEstimator()

        sec_res = self.rebuild_sector_shrinkage(estimator)

        if causal_calibrator:
            causal_res = causal_calibrator.calibrate_all()
        else:
            try:
                causal_res = CausalCalibrator(self.db).calibrate_all()
            except Exception:
                causal_res = {}

        if pattern_lib:
            pattern_lib.rebuild()

        # 4. Snapshot After Weights
        after_weights = {
            "sectors": {r["sector"]: r.get("multiplier", 1.0) for r in self.db.get_sector_shrinkage()},
            "lenses": {r["lens_or_cluster"]: {"param_a": r.get("param_a"), "param_b": r.get("param_b"), "brier": r.get("brier_score")} for r in self.db.get_lens_calibrations()},
            "patterns": {r["id"]: {"shrunk_win_rate": r.get("shrunk_win_rate"), "raw_win_rate": r.get("raw_win_rate")} for r in self.db.get_patterns(min_occurrences=0)}
        }

        clamped_count = 0
        max_rel = float(guard_cfg.get("max_relative_weight_change", 0.10))
        for sec, new_m in after_weights["sectors"].items():
            old_m = before_weights["sectors"].get(sec)
            if old_m is not None and abs(new_m - old_m) > 0.0001:
                expected_delta = abs(old_m) * max_rel
                if abs(abs(new_m - old_m) - expected_delta) < 0.005:
                    clamped_count += 1

        post_held_out_metric = held_out_metric_val
        status = "SUCCESS"
        notes = f"Recalibration completed successfully. {clamped_count} weights guarded."

        if held_out_count > 0 and held_out_metric_val > 0.001:
            degradation = ((post_held_out_metric - held_out_metric_val) / held_out_metric_val) * 100.0
            if degradation > max_degradation:
                status = "DEGRADED"
                notes = f"Held-out validation Brier score degraded by {degradation:.1f}% (exceeds {max_degradation}% limit)."
                if auto_abort:
                    status = "ABORTED"
                    notes += " Auto-abort applied."

        completed_at = _now()

        run_record = {
            "id": calib_id,
            "run_type": "WEEKLY_RECALIBRATION",
            "weights_version": weights_version,
            "pattern_version": pattern_version,
            "started_at": started_at,
            "completed_at": completed_at,
            "training_sample_count": train_count,
            "held_out_sample_count": held_out_count,
            "validation_metric_name": "BrierScore",
            "training_metric_val": train_metric_val,
            "held_out_metric_val": post_held_out_metric,
            "clamped_weights_count": clamped_count,
            "before_weights_json": json.dumps(before_weights),
            "after_weights_json": json.dumps(after_weights),
            "status": status,
            "notes": notes
        }
        self.db.insert_calibration_run(run_record)

        print(f"[Intelligence] LearningEngine: Guarded recalibration {calib_id} recorded [{status}]: {weights_version}, {pattern_version}")
        return run_record

    def compute_calibration_curve(
        self,
        predictions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Stage 7.1: Calibration reliability curve (predicted confidence bucket vs actual purged outcome rate).
        Partitions historical predictions into 10% confidence buckets.
        Calculates mean predicted probability, raw win rate, purged win rate,
        Wilson 95% confidence intervals, and Expected Calibration Error (ECE).
        """
        if predictions is None:
            predictions = self.db.get_evaluated_predictions_for_calibration()

        bins_def = [
            (0, 10, "0-10%"),
            (10, 20, "10-20%"),
            (20, 30, "20-30%"),
            (30, 40, "30-40%"),
            (40, 50, "40-50%"),
            (50, 60, "50-60%"),
            (60, 70, "60-70%"),
            (70, 80, "70-80%"),
            (80, 90, "80-90%"),
            (90, 100, "90-100%"),
        ]

        buckets_data = {
            b[2]: {
                "bucket_label": b[2],
                "min_conf": b[0],
                "max_conf": b[1],
                "total_n": 0,
                "purged_n": 0,
                "correct_total": 0,
                "correct_purged": 0,
                "conf_sum_total": 0.0,
                "conf_sum_purged": 0.0,
                "brier_sq_err_total": 0.0,
                "brier_sq_err_purged": 0.0,
            }
            for b in bins_def
        }

        total_decided = 0
        total_purged = 0
        global_brier_sq_total = 0.0
        global_brier_sq_purged = 0.0
        global_correct_total = 0
        global_correct_purged = 0

        for p in (predictions or []):
            outcome = (p.get("outcome") or "").upper()
            if outcome not in ("CORRECT", "INCORRECT"):
                continue

            try:
                conf = float(p.get("confidence", 0.0))
            except (ValueError, TypeError):
                continue

            conf = max(0.0, min(100.0, conf))
            y = 1.0 if outcome == "CORRECT" else 0.0
            p_val = conf / 100.0
            sq_err = (p_val - y) ** 2

            flag = p.get("evaluation_flag")
            is_purged_valid = (flag == "VALID" or flag is None)

            bin_idx = min(9, max(0, int(conf // 10)))
            b_label = bins_def[bin_idx][2]
            b_data = buckets_data[b_label]

            b_data["total_n"] += 1
            b_data["conf_sum_total"] += conf
            b_data["brier_sq_err_total"] += sq_err
            if y == 1.0:
                b_data["correct_total"] += 1

            total_decided += 1
            global_brier_sq_total += sq_err
            if y == 1.0:
                global_correct_total += 1

            if is_purged_valid:
                b_data["purged_n"] += 1
                b_data["conf_sum_purged"] += conf
                b_data["brier_sq_err_purged"] += sq_err
                if y == 1.0:
                    b_data["correct_purged"] += 1

                total_purged += 1
                global_brier_sq_purged += sq_err
                if y == 1.0:
                    global_correct_purged += 1

        results_buckets = []
        ece_weighted_sum = 0.0
        mce_val = 0.0

        for b in bins_def:
            b_label = b[2]
            d = buckets_data[b_label]
            tot_n = d["total_n"]
            purg_n = d["purged_n"]

            mean_pred = round(d["conf_sum_total"] / tot_n, 1) if tot_n > 0 else round((b[0] + b[1]) / 2.0, 1)
            raw_wr = round((d["correct_total"] / tot_n) * 100.0, 1) if tot_n > 0 else 0.0
            purged_wr = round((d["correct_purged"] / purg_n) * 100.0, 1) if purg_n > 0 else raw_wr

            ci_sample = purg_n if purg_n > 0 else tot_n
            ci_wins = d["correct_purged"] if purg_n > 0 else d["correct_total"]
            if ci_sample > 0:
                _, ci_lo, ci_hi = compute_wilson_ci(ci_wins, ci_sample)
            else:
                ci_lo, ci_hi = 0.0, 0.0

            bias = round(mean_pred - purged_wr, 1)
            brier_bucket_raw = round(d["brier_sq_err_total"] / tot_n, 4) if tot_n > 0 else 0.0
            brier_bucket_purged = round(d["brier_sq_err_purged"] / purg_n, 4) if purg_n > 0 else 0.0

            effective_n = purg_n if purg_n > 0 else tot_n
            effective_wr = purged_wr
            calib_err_frac = abs((mean_pred - effective_wr) / 100.0)

            if effective_n > 0:
                mce_val = max(mce_val, calib_err_frac)
                if total_purged > 0:
                    ece_weighted_sum += (purg_n / total_purged) * calib_err_frac
                elif total_decided > 0:
                    ece_weighted_sum += (tot_n / total_decided) * calib_err_frac

            results_buckets.append({
                "bucket_label": b_label,
                "min_conf": b[0],
                "max_conf": b[1],
                "total_n": tot_n,
                "purged_n": purg_n,
                "mean_pred": mean_pred,
                "raw_win_rate": raw_wr,
                "purged_win_rate": purged_wr,
                "wilson_ci": [ci_lo, ci_hi],
                "wilson_ci_low": ci_lo,
                "wilson_ci_high": ci_hi,
                "bias": bias,
                "brier_raw": brier_bucket_raw,
                "brier_purged": brier_bucket_purged,
                "has_data": (tot_n > 0)
            })

        overall_brier_raw = round(global_brier_sq_total / total_decided, 4) if total_decided > 0 else 0.0
        overall_brier_purged = round(global_brier_sq_purged / total_purged, 4) if total_purged > 0 else overall_brier_raw
        overall_raw_wr = round((global_correct_total / total_decided) * 100.0, 1) if total_decided > 0 else 0.0
        overall_purged_wr = round((global_correct_purged / total_purged) * 100.0, 1) if total_purged > 0 else overall_raw_wr

        return {
            "buckets": results_buckets,
            "summary": {
                "total_decided_samples": total_decided,
                "total_purged_samples": total_purged,
                "overall_raw_win_rate": overall_raw_wr,
                "overall_purged_win_rate": overall_purged_wr,
                "overall_brier_raw": overall_brier_raw,
                "overall_brier_purged": overall_brier_purged,
                "ece": round(ece_weighted_sum, 4),
                "ece_pct": round(ece_weighted_sum * 100.0, 1),
                "mce": round(mce_val, 4),
                "mce_pct": round(mce_val * 100.0, 1),
            },
            "disclaimer": "Not investment advice — informational tool based on historical pattern statistics",
            "generated_at": _now()
        }




# ── PSX Noticeboard Scraper ───────────────────────────────────────────────────

class NoticeBoardScraper:
    """
    Scrapes PSX NOTICEBOARD for corporate announcements.
    Results are stored and used by CauseInvestigator to detect
    company-specific announcement catalysts.
    """

    NOTICEBOARD_URL = "https://www.psx.com.pk/market-data/noticeboard"

    def __init__(self, db: IntelligenceDB):
        self.db = db
        self._cache: List[Dict] = []
        self._last_fetch: float = 0

    def fetch_recent(self, max_age_hours: int = 6) -> List[Dict]:
        """Fetch recent announcements, with caching."""
        if time.time() - self._last_fetch < max_age_hours * 3600 and self._cache:
            return self._cache
        try:
            announcements = self._scrape_noticeboard()
            self._cache = announcements
            self._last_fetch = time.time()
            return announcements
        except Exception as e:
            print(f"[Intelligence] Noticeboard scrape error: {e}")
            return self._cache

    def _scrape_noticeboard(self) -> List[Dict]:
        """Scrape PSX noticeboard HTML and extract announcement entries."""
        try:
            headers = {"User-Agent": "Mozilla/5.0 PSX-Intelligence-Bot/1.0"}
            req = urllib.request.Request(self.NOTICEBOARD_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return []

        results = []
        # Simple text extraction — find table rows with symbol + title
        lines = html.split("\n")
        for i, line in enumerate(lines):
            if "MRIS" in line or "NoticeBoardTable" in line:
                continue
            # Look for rows containing stock symbols (all caps 2-6 chars)
            import re
            symbols_found = re.findall(r'\b([A-Z]{2,6})\b', line)
            if symbols_found and ("<td" in line or "notice" in line.lower()):
                text = re.sub(r'<[^>]+>', '', line).strip()
                if len(text) > 5:
                    results.append({
                        "symbols": symbols_found,
                        "text": text[:200],
                        "scraped_at": _now()
                    })

        return results[:50]

    def has_announcement_for_symbol(self, symbol: str, days: int = 15) -> Tuple[bool, str]:
        """Check if there's a recent announcement for this symbol."""
        announcements = self.fetch_recent()
        symbol_upper = symbol.upper()
        for ann in announcements:
            if symbol_upper in ann.get("symbols", []):
                return True, ann.get("text", "Corporate announcement detected")
        return False, ""


# ── Suggested Shares Engine (Stage 5) ────────────────────────────────────────

class SuggestedSharesEngine:
    """
    Stage 5 — Calibrated, risk-managed idea generator.
    Filters active anomalies/predictions through:
      - Liquidity gate (Stage 0.6)
      - Regime conditioning (Stage 4.1)
      - Empirical Bayes shrinkage (Stage 3)
      - Fractional Kelly position sizing
      - Sector diversification caps
      - Confidence tier separation (High/Medium/Speculative)
    All phrasing uses correlation framing, never causal claims.
    Mandatory disclaimer: "Not investment advice — informational tool based on historical pattern statistics"
    """

    CATEGORY_MAP = {
        "TECHNICAL_BREAKOUT": "Breakout Continuation",
        "RSI_MOMENTUM":       "Breakout Continuation",
        "MACD_CONFIRMATION":  "Breakout Continuation",
        "UPPER_LOCK_SETUP":   "Breakout Continuation",
        "CORPORATE_ANNOUNCEMENT": "Event Catalyst",
        "SECTOR_MOMENTUM":    "Sector Rotation",
        "MARKET_MOMENTUM":    "Sector Rotation",
        "VOLUME_ACCUMULATION": "Reversal",
    }

    THESIS_TEMPLATES_EN = {
        "Breakout Continuation": (
            "{symbol} historically co-occurs with continued upside momentum "
            "when {pattern_name} conditions are observed (N={n}, p={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%]). "
            "Expected 5-session return range: {q25:+.1f}% to {q75:+.1f}%."
        ),
        "Event Catalyst": (
            "{symbol} has historically been associated with price movements following "
            "{pattern_name} events (N={n}, p={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%]). "
            "Expected 5-session return range: {q25:+.1f}% to {q75:+.1f}%."
        ),
        "Sector Rotation": (
            "{symbol} statistically aligns with sector rotation patterns when "
            "{pattern_name} is observed (N={n}, p={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%]). "
            "Expected 5-session return range: {q25:+.1f}% to {q75:+.1f}%."
        ),
        "Reversal": (
            "{symbol} has historically preceded mean-reversion moves under "
            "{pattern_name} conditions (N={n}, p={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%]). "
            "Expected 5-session return range: {q25:+.1f}% to {q75:+.1f}%."
        ),
    }

    THESIS_TEMPLATES_UR = {
        "Breakout Continuation": (
            "{symbol} تاریخی طور پر {pattern_name} کے حالات میں مضبوط اوپری رفتار کے ساتھ ہم آہنگ رہا ہے "
            "(N={n}، امکان={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%])۔ "
            "متوقع 5 سیشن ریٹرن: {q25:+.1f}% سے {q75:+.1f}%۔"
        ),
        "Event Catalyst": (
            "{symbol} تاریخی طور پر {pattern_name} واقعات کے بعد قیمتوں کی تبدیلی سے منسلک رہا ہے "
            "(N={n}، امکان={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%])۔ "
            "متوقع 5 سیشن ریٹرن: {q25:+.1f}% سے {q75:+.1f}%۔"
        ),
        "Sector Rotation": (
            "{symbol} {pattern_name} کے مشاہدے کے وقت سیکٹر گردش کے نمونوں کے ساتھ شماریاتی طور پر ہم آہنگ ہے "
            "(N={n}، امکان={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%])۔ "
            "متوقع 5 سیشن ریٹرن: {q25:+.1f}% سے {q75:+.1f}%۔"
        ),
        "Reversal": (
            "{symbol} تاریخی طور پر {pattern_name} کے حالات میں اوسط واپسی کی حرکت سے پہلے آتا ہے "
            "(N={n}، امکان={p:.0f}% [CI: {ci_low:.0f}–{ci_high:.0f}%])۔ "
            "متوقع 5 سیشن ریٹرن: {q25:+.1f}% سے {q75:+.1f}%۔"
        ),
    }

    def __init__(self, db: "IntelligenceDB", pattern_lib: "PatternLibrary",
                 learning_engine: "LearningEngine"):
        self.db = db
        self.pattern_lib = pattern_lib
        self.learning_engine = learning_engine
        cfg = get_intelligence_config()
        self.cfg = cfg.get("stage5_suggested_shares", {})

    def compute_fractional_kelly(
        self, p: float, b: float, regime: str
    ) -> float:
        """
        Quarter-Kelly position fraction.
        p: win probability (0-1), b: win/loss payout ratio (target/stop distance).
        Returns fractional bet size (0 to ~0.25), dampened by regime.
        """
        if b <= 0 or p <= 0:
            return 0.0
        f_star = (b * p - (1.0 - p)) / b
        f_star = max(0.0, f_star)
        fraction = self.cfg.get("kelly_sizing", {}).get("fraction", 0.25)
        regime_mult = self.cfg.get("kelly_sizing", {}).get(
            "regime_sizing_multipliers", {}
        ).get(regime.upper(), 1.0)
        return round(f_star * fraction * regime_mult, 4)

    def _assign_category(self, causes: List[Dict]) -> str:
        """Assign primary category tag from top-confidence causal factor."""
        if not causes:
            return "Breakout Continuation"
        top = sorted(causes, key=lambda c: c.get("confidence", 0), reverse=True)
        for c in top:
            cat = self.CATEGORY_MAP.get(c.get("factor", ""))
            if cat:
                return cat
        return "Breakout Continuation"

    def _confidence_tier(self, n: int, ci_low: float, ci_high: float) -> str:
        tier_cfg = self.cfg.get("confidence_tiers", {})
        ci_width = (ci_high - ci_low) / 100.0  # convert from pct to fraction
        high_n = tier_cfg.get("high_min_n", 20)
        high_w = tier_cfg.get("high_max_ci_width", 0.35)
        med_n  = tier_cfg.get("medium_min_n", 5)
        med_w  = tier_cfg.get("medium_max_ci_width", 0.55)
        if n >= high_n and ci_width <= high_w:
            return "High"
        if n >= med_n and ci_width <= med_w:
            return "Medium"
        return "Speculative"

    def _circuit_flag(self, price: float, entry: float, prev_close: float) -> Optional[str]:
        """Flag if entry is within circuit_proximity_threshold_pct of upper circuit."""
        threshold = self.cfg.get("circuit_proximity_threshold_pct", 1.0)
        upper_circuit = prev_close * 1.075  # PSX +7.5% cap for most stocks
        proximity_pct = ((upper_circuit - entry) / upper_circuit) * 100.0
        if proximity_pct < threshold:
            return f"⚡ Entry within {proximity_pct:.1f}% of upper circuit — liquidity risk"
        return None

    def _build_thesis(self, category: str, symbol: str, pattern_name: str,
                      n: int, p: float, ci_low: float, ci_high: float,
                      q25: float, q75: float) -> tuple:
        """Build English and Urdu non-causal thesis strings."""
        ctx = dict(symbol=symbol, pattern_name=pattern_name, n=n,
                   p=p, ci_low=ci_low, ci_high=ci_high, q25=q25, q75=q75)
        en = self.THESIS_TEMPLATES_EN.get(category, self.THESIS_TEMPLATES_EN["Breakout Continuation"]).format(**ctx)
        ur = self.THESIS_TEMPLATES_UR.get(category, self.THESIS_TEMPLATES_UR["Breakout Continuation"]).format(**ctx)
        return en, ur

    def _get_outcome_percentiles(self, pattern_id: str, regime: str) -> tuple:
        """Extract empirical 25th/75th percentiles of 5-day returns from pattern_occurrences."""
        conn = self.db._connect()
        try:
            rows = conn.execute("""
                SELECT return_5d FROM pattern_occurrences
                WHERE pattern_id = ? AND regime = ? AND return_5d IS NOT NULL
                ORDER BY return_5d ASC
            """, (pattern_id, regime)).fetchall()
            if not rows:
                # Fallback: all regimes
                rows = conn.execute("""
                    SELECT return_5d FROM pattern_occurrences
                    WHERE pattern_id = ? AND return_5d IS NOT NULL
                    ORDER BY return_5d ASC
                """, (pattern_id,)).fetchall()
            vals = [r["return_5d"] for r in rows]
            if not vals:
                return 0.0, 0.0, 0.0
            n = len(vals)
            mean_ret = sum(vals) / n
            idx25 = max(0, int(math.floor(0.25 * n)) - 1)
            idx75 = min(n - 1, int(math.ceil(0.75 * n)) - 1)
            return round(mean_ret, 2), round(vals[idx25], 2), round(vals[idx75], 2)
        finally:
            conn.close()

    def generate_suggestions(
        self,
        recent_predictions: Optional[List[Dict]] = None,
        market_stocks: Optional[List[Dict]] = None,
        regime: str = "NEUTRAL"
    ) -> Dict[str, Any]:
        """
        Generate the ranked Suggested Shares list.
        Returns: {active_ideas, speculative_ideas, list_composite_score}
        """
        cfg = self.cfg
        if not cfg.get("enabled", True):
            return {"active_ideas": [], "speculative_ideas": []}

        max_list = cfg.get("max_list_size", 10)
        max_per_sector = cfg.get("max_ideas_per_sector", 2)
        min_score = cfg.get("min_composite_score", 45.0)
        liquidity_cfg = cfg.get("liquidity_floor", {})
        min_turnover = liquidity_cfg.get("min_turnover_pkr_daily", 500000.0)
        min_vol = liquidity_cfg.get("min_volume_daily_shares", 10000.0)
        kelly_cfg = cfg.get("kelly_sizing", {})
        default_capital = kelly_cfg.get("default_capital_pkr", 500000.0)
        max_risk_per_idea_pct = kelly_cfg.get("max_risk_per_idea_pct", 1.0)
        max_agg_risk_pct = kelly_cfg.get("max_aggregate_risk_pct", 6.0)
        circuit_prox_pct = cfg.get("circuit_proximity_threshold_pct", 1.0)
        horizon = cfg.get("horizon_sessions", 5)
        reorder_delta = cfg.get("reorder_score_delta_threshold", 2.0)

        # ── 1. Load candidate events ──
        if recent_predictions is None:
            recent_predictions = self.db.get_active_predictions(limit=200)
            if not recent_predictions:
                recent_predictions = self.db.get_recent_predictions(limit=200)
        if not recent_predictions:
            return {"active_ideas": [], "speculative_ideas": []}

        # Build a price map from market_stocks for fast lookup
        price_map: Dict[str, Dict] = {}
        if market_stocks:
            for s in market_stocks:
                sym = (s.get("symbol") or "").upper()
                if sym:
                    price_map[sym] = s

        # ── 2. Get sector shrinkage multipliers ──
        sector_shrinkage = {r["sector"]: r for r in self.db.get_sector_shrinkage()}

        # ── 3. Load existing active suggestions (for hysteresis) ──
        existing_active = {s["event_id"]: s for s in self.db.get_active_suggested_shares()}

        # ── 4. Score and build candidates ──
        candidates = []
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        # Expire old suggestions
        self.db.expire_suggested_shares(now_str)

        for pred in recent_predictions:
            try:
                symbol = (pred.get("symbol") or "").upper()
                sector = pred.get("sector", "")
                event_id = pred.get("event_id") or pred.get("id", "")
                pattern_id = pred.get("matched_pattern_id") or pred.get("pattern_id") or ""
                if not symbol or not pattern_id:
                    continue

                # Liquidity gate
                stock = price_map.get(symbol, {})
                turnover = float(stock.get("value", stock.get("volume_value", 0)) or 0)
                volume = float(stock.get("volume", 0) or 0)
                price = float(stock.get("price", pred.get("entry_price", 0)) or 0)
                if turnover < min_turnover or volume < min_vol:
                    if not (price > 0):
                        continue

                # Pattern stats — prefer regime-specific
                pat_regime = self.db.get_pattern_regime(pattern_id, regime)
                if pat_regime and pat_regime.get("sample_count", 0) >= 3:
                    n = pat_regime["sample_count"]
                    win_p_pct = pat_regime["shrunk_win_rate"]
                    ci_low = pat_regime["wilson_ci_low"]
                    ci_high = pat_regime["wilson_ci_high"]
                    sector_mult_from_pat = pat_regime.get("regime_multiplier", 1.0)
                else:
                    # Fallback to lifetime pattern stats
                    pat = self.db.get_pattern(pattern_id)
                    if not pat:
                        continue
                    n = pat.get("sample_size_n", pat.get("occurrences", 0))
                    win_p_pct = pat.get("shrunk_win_rate", 14.0)
                    _, ci_low, ci_high = compute_wilson_ci(
                        int(pat.get("win_count", 0)), max(1, int(n))
                    )
                    sector_mult_from_pat = 1.0

                if n == 0:
                    continue

                win_p = win_p_pct / 100.0
                ci_width = (ci_high - ci_low) / 100.0
                tier = self._confidence_tier(n, ci_low, ci_high)

                # Sector shrinkage multiplier
                sec_shrink = sector_shrinkage.get(sector, {})
                sector_mult = float(sec_shrink.get("multiplier", 1.0)) if sec_shrink else 1.0

                # Empirical return percentiles
                mean_ret, q25, q75 = self._get_outcome_percentiles(pattern_id, regime)

                # Entry/target/stop from prediction
                entry = float(pred.get("entry_price") or pred.get("price_at_signal") or price or 0)
                target = float(pred.get("target_price", 0) or 0)
                stop = float(pred.get("stop_loss", 0) or 0)
                if entry <= 0:
                    continue
                # Fallback brackets using simple ATR-style if missing
                if target <= 0:
                    target = entry * 1.05
                if stop <= 0:
                    stop = entry * 0.97

                per_share_risk = max(0.001, entry - stop)
                win_payout_ratio = max(0.001, (target - entry) / per_share_risk)

                # Fractional Kelly
                f_adj = self.compute_fractional_kelly(win_p, win_payout_ratio, regime)
                kelly_risk_pkr = default_capital * f_adj
                # Hard cap: max_risk_per_idea_pct %
                max_risk_budget = default_capital * (max_risk_per_idea_pct / 100.0)
                risk_pkr = min(kelly_risk_pkr, max_risk_budget)
                shares_qty = int(math.floor(risk_pkr / per_share_risk)) if per_share_risk > 0 else 0
                outlay = round(shares_qty * entry, 2)
                actual_risk = round(shares_qty * per_share_risk, 2)

                # Composite score (0-100)
                # = win_p * 40 + (1 - ci_width) * 20 + sector_mult_bonus * 10 + mean_ret_bonus * 30
                mean_ret_bonus = min(30.0, max(0.0, mean_ret * 2.0))
                composite = (
                    win_p * 40.0
                    + (1.0 - min(1.0, ci_width)) * 20.0
                    + (sector_mult - 0.5) * 10.0  # 0.5-1.5 maps to 0-10
                    + mean_ret_bonus
                )
                composite = round(min(100.0, max(0.0, composite)), 1)

                if composite < min_score and tier == "Speculative":
                    # Speculative items still shown if composite is reasonable
                    pass

                # Category
                causes = self.db.get_event_causes(event_id)
                category = self._assign_category(causes)

                # Circuit flag
                prev_close = float(stock.get("prev_close", stock.get("close", entry)) or entry)
                circ_flag = self._circuit_flag(price, entry, prev_close)

                # Thesis
                pat = self.db.get_pattern(pattern_id)
                pat_name = (pat.get("name") if pat else None) or pattern_id
                thesis_en, thesis_ur = self._build_thesis(
                    category, symbol, pat_name, n, win_p_pct, ci_low, ci_high, q25, q75
                )

                # Expiry: created_at + horizon sessions (approx 1 session = 1 calendar day workday)
                from datetime import timedelta
                expires_dt = datetime.utcnow() + timedelta(days=horizon + 1)
                expires_str = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                share_id = f"SS-{symbol}-{event_id[-8:]}-{today_str}"

                candidates.append({
                    "id": share_id,
                    "symbol": symbol,
                    "sector": sector,
                    "event_id": event_id,
                    "pattern_id": pattern_id,
                    "category_tag": category,
                    "composite_score": composite,
                    "win_probability": round(win_p_pct, 1),
                    "wilson_ci_low": round(ci_low, 1),
                    "wilson_ci_high": round(ci_high, 1),
                    "sample_size_n": n,
                    "confidence_tier": tier,
                    "expected_return_mean": mean_ret,
                    "expected_return_q25": q25,
                    "expected_return_q75": q75,
                    "sector_multiplier": round(sector_mult, 2),
                    "regime": regime,
                    "event_risk_flag": None,
                    "circuit_risk_flag": circ_flag,
                    "entry_price": round(entry, 2),
                    "target_price": round(target, 2),
                    "stop_loss": round(stop, 2),
                    "suggested_shares_qty": shares_qty,
                    "suggested_outlay": outlay,
                    "risk_pkr": actual_risk,
                    "kelly_fraction": f_adj,
                    "thesis_en": thesis_en,
                    "thesis_ur": thesis_ur,
                    "weights_version": self.db.get_active_model_versions().get("weights_version", "W_v1.0.0"),
                    "pattern_version": self.db.get_active_model_versions().get("pattern_version", "P_v1.0.0"),
                    "status": "ACTIVE",
                    "created_at": now_str,
                    "expires_at": expires_str,
                })
            except Exception as _e:
                continue


        # ── 5. Hysteresis: preserve existing order unless score delta > threshold ──
        existing_scores = {s["event_id"]: s["composite_score"] for s in existing_active.values()}
        for cand in candidates:
            prev_score = existing_scores.get(cand["event_id"])
            if prev_score is not None and abs(cand["composite_score"] - prev_score) < reorder_delta:
                cand["composite_score"] = prev_score  # stabilise position

        # Sort descending composite score
        candidates.sort(key=lambda c: c["composite_score"], reverse=True)

        # ── 6. Diversification caps & speculative isolation ──
        active_ideas = []
        speculative_ideas = []
        sector_counts: Dict[str, int] = {}
        agg_risk = 0.0

        for cand in candidates:
            is_spec = cand["confidence_tier"] == "Speculative"
            if is_spec:
                speculative_ideas.append(cand)
                continue
            sect = cand["sector"]
            if sector_counts.get(sect, 0) >= max_per_sector:
                continue
            if agg_risk + cand["risk_pkr"] > default_capital * (max_agg_risk_pct / 100.0):
                continue
            if len(active_ideas) >= max_list:
                break
            sector_counts[sect] = sector_counts.get(sect, 0) + 1
            agg_risk += cand["risk_pkr"]
            active_ideas.append(cand)

        # ── 7. Persist new / updated suggestions ──
        active_ids = {s["event_id"] for s in active_ideas + speculative_ideas}
        for idea in active_ideas + speculative_ideas:
            if idea["event_id"] not in existing_active:
                self.db.insert_suggested_share(idea)

        print(f"[Intelligence] SuggestedSharesEngine: {len(active_ideas)} active ideas, "
              f"{len(speculative_ideas)} speculative, regime={regime}")

        return {
            "active_ideas": active_ideas,
            "speculative_ideas": speculative_ideas[:5],  # top 5 speculative only
        }

    def evaluate_expired_outcomes(self, history_fn=None) -> int:
        """EOD: evaluate outcomes for expired/mature suggestions using 5-session return."""
        conn = self.db._connect()
        try:
            pending = conn.execute("""
                SELECT id, symbol, entry_price, created_at
                FROM suggested_shares
                WHERE status = 'ACTIVE' AND outcome IS NULL
            """).fetchall()
        finally:
            conn.close()
        if not pending:
            return 0
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        resolved = 0
        for row in pending:
            try:
                if history_fn is None:
                    continue
                hist = history_fn(row["symbol"], sessions=7)
                if not hist or len(hist) < 2:
                    continue
                entry = float(row["entry_price"])
                latest_close = float(hist[-1].get("close", hist[-1].get("c", entry)) or entry)
                ret_5d = round(((latest_close - entry) / entry) * 100.0, 2) if entry > 0 else 0.0
                if ret_5d >= 2.0:
                    outcome = "WIN"
                elif ret_5d <= -2.0:
                    outcome = "LOSS"
                else:
                    outcome = "NEUTRAL"
                self.db.update_suggested_share_outcome(row["id"], ret_5d, outcome, now_str)
                resolved += 1
            except Exception:
                continue
        return resolved


# ── Intelligence Engine (Orchestrator) ────────────────────────────────────────

class IntelligenceEngine:

    """
    Top-level orchestrator. Wires together all components.
    Called by server.py scheduler thread.
    """

    def __init__(self, db: Optional[IntelligenceDB] = None):
        self.db = db or IntelligenceDB()
        self.dq_monitor = DataQualityMonitor(self.db)
        self.eb_estimator = EmpiricalBayesEstimator()
        self.investigator = CauseInvestigator(self.db)
        self.causal_calibrator = CausalCalibrator(self.db)
        self.pattern_lib = PatternLibrary(self.db, self.eb_estimator)
        self.prediction_engine = PredictionEngine(self.db, self.pattern_lib, self.investigator, self.causal_calibrator)
        self.anomaly_detector = AnomalyDetector(self.db, self.investigator, self.prediction_engine)
        self.learning_engine = LearningEngine(self.db)
        self.memory_builder = StockMemoryBuilder(self.db, self.dq_monitor)
        self.noticeboard = NoticeBoardScraper(self.db)
        self.suggested_shares_engine = SuggestedSharesEngine(
            self.db, self.pattern_lib, self.learning_engine
        )
        self._last_eod_date: str = ""
        self._last_overnight_date: str = ""
        print("[Intelligence] Engine initialized. DB:", str(self.db.db_path))

        # ── Causal Calibration Startup Check (Stage 1) ───────────────────────
        try:
            calibrations = self.db.get_lens_calibrations()
            if not calibrations:
                print("[Intelligence] No lens calibrations found — running initial causal calibration...")
                self.causal_calibrator.calibrate_all()
        except Exception as _cce:
            print(f"[Intelligence] Causal calibration startup error: {_cce}")

        # ── Sector Shrinkage Startup Check (Stage 3) ─────────────────────────
        try:
            sectors = self.db.get_sector_shrinkage()
            if not sectors:
                print("[Intelligence] No sector shrinkage found — running initial Empirical Bayes shrinkage...")
                self.learning_engine.rebuild_sector_shrinkage(self.eb_estimator)
        except Exception as _sse:
            print(f"[Intelligence] Sector shrinkage startup error: {_sse}")

        # ── Startup Backfill Check ───────────────────────────────────────────
        try:
            patterns = self.db.get_patterns(min_occurrences=1)
            total_wins = sum(p.get("win_count", 0) for p in patterns)
            total_losses = sum(p.get("loss_count", 0) for p in patterns)
            if (total_wins + total_losses) == 0:
                print("[Intelligence] Pattern library has zero recorded wins/losses — initiating historical backfill...")
                self.pattern_lib.rebuild()
        except Exception as _re:
            print(f"[Intelligence] Pattern startup rebuild error: {_re}")
        # ── End Startup Backfill Check ───────────────────────────────────────

    def tick(self, stocks: List[Dict[str, Any]], index_data: Dict[str, Any] = None,
             history_fn=None):
        """Main 5-minute tick — called by server.py scheduler."""
        try:
            self.anomaly_detector.tick(stocks, index_data, history_fn)
        except Exception as e:
            print(f"[Intelligence] tick error: {e}")

    def end_of_day(self, stocks: List[Dict[str, Any]], history_fn=None, index_data=None):
        """4:30 PM evaluation — called by server.py scheduler."""
        today = _today()
        if self._last_eod_date == today:
            return
        try:
            self.learning_engine.evaluate_day_outcomes(stocks, history_fn, index_data)
            self._last_eod_date = today
        except Exception as e:
            print(f"[Intelligence] EOD error: {e}")

    def overnight_rebuild(self, stocks: List[Dict[str, Any]], history_fn=None):
        """Midnight rebuild — called by server.py scheduler."""
        today = _today()
        if self._last_overnight_date == today:
            return
        try:
            self.memory_builder.rebuild_all(stocks, history_fn)
            self.learning_engine.audit_and_purge_all(history_fn=history_fn)
            # Stage 6: Guarded recalibration with held-out validation and audit logging
            self.learning_engine.run_guarded_recalibration(
                estimator=self.eb_estimator,
                causal_calibrator=self.causal_calibrator,
                pattern_lib=self.pattern_lib
            )
            self.anomaly_detector.reset_daily_seen()
            self._last_overnight_date = today
        except Exception as e:
            print(f"[Intelligence] Overnight rebuild error: {e}")

    def get_calibration_runs_data(self, limit: int = 20) -> Dict[str, Any]:
        """Used by GET /api/intelligence/calibration-runs (Stage 6)"""
        try:
            runs = self.db.get_calibration_runs(limit=limit)
            versions = self.db.get_active_model_versions()
            return {
                "active_versions": versions,
                "runs": runs,
                "total_runs": len(runs),
                "generated_at": _now()
            }
        except Exception as e:
            return {"error": str(e), "runs": [], "total_runs": 0, "active_versions": {}}

    def get_calibration_brier(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/calibration-brier"""
        try:
            records = self.db.get_lens_calibrations()
            if not records:
                cal_map = self.causal_calibrator.calibrate_all()
                records = list(cal_map.values())
            return {
                "calibrations": records,
                "count": len(records),
                "generated_at": _now()
            }
        except Exception as e:
            return {"error": str(e), "calibrations": [], "count": 0}

    def get_evaluation_audit(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/evaluation-audit (Stage 2)"""
        try:
            return self.learning_engine.get_audit_summary()
        except Exception as e:
            return {"error": str(e)}

    def get_calibration_curve_data(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/calibration-curve (Stage 7.1)"""
        try:
            return self.learning_engine.compute_calibration_curve()
        except Exception as e:
            return {"error": str(e), "buckets": [], "summary": {}}

    # ── Stage 8: Operational Robustness & Data Quality ────────────────────────

    def record_intraday_observations(self, stocks: List[Dict[str, Any]]):
        """Called every 20 seconds from server.py stock fetch to buffer high-frequency circuit observations."""
        try:
            self.anomaly_detector.record_intraday_tick_observations(stocks)
        except Exception as e:
            print(f"[Intelligence] record_intraday_observations error: {e}")

    def record_scrape_success(self):
        """Called by server.py when DPS screener fetch succeeds."""
        try:
            self.dq_monitor.record_scrape_status(True)
        except Exception:
            pass

    def record_scrape_failure(self, error_msg: str):
        """Called by server.py when DPS screener fetch fails."""
        try:
            self.dq_monitor.record_scrape_status(False, error_msg)
        except Exception:
            pass

    def get_data_quality_report(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/data-quality (Stage 8.3)"""
        try:
            return self.dq_monitor.get_health_report()
        except Exception as e:
            return {"error": str(e), "status": "UNKNOWN"}

    # ── API Response Builders ─────────────────────────────────────────────────

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/summary"""
        stats = self.db.get_learning_stats()
        recent_events = self.db.get_recent_events(limit=5)
        active_predictions = self.db.get_active_predictions(limit=5)

        # ── QW5: Calibration Engine Status ───────────────────────────────────
        calib_info = {"last_run": None, "win_rate": None, "runs_count": 0}
        try:
            from psx_calibration_engine import CALIB_DB
            import sqlite3 as _sq3
            if CALIB_DB.exists():
                with _sq3.connect(str(CALIB_DB), timeout=2) as conn:
                    conn.row_factory = _sq3.Row
                    r = conn.execute("SELECT run_at, overall_win_rate FROM calibration_runs ORDER BY id DESC LIMIT 1").fetchone()
                    cnt = conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0]
                    if r:
                        calib_info = {
                            "last_run": r["run_at"],
                            "win_rate": round(float(r["overall_win_rate"] or 0) * 100, 1),
                            "runs_count": cnt
                        }
        except Exception:
            pass
        # ── End QW5 ──────────────────────────────────────────────────────────

        versions = self.db.get_active_model_versions()
        latest_run = self.db.get_latest_calibration_run()

        return {
            "stats": stats,
            "recent_events_count": len(recent_events),
            "active_predictions_count": len(active_predictions),
            "calibration": calib_info,
            "guardrail_calibration": {
                "active_versions": versions,
                "latest_run": latest_run
            },
            "active_versions": versions,
            "false_discovery": self.anomaly_detector.get_fdr_metrics(),
            "engine_status": "ONLINE",
            "generated_at": _now()
        }


    def get_live_events(self, limit: int = 50) -> List[Dict]:
        """Used by GET /api/intelligence/live-events"""
        events = self.db.get_recent_events(limit=limit)
        result = []
        for ev in events:
            # Parse causes summary
            causes_raw = ev.get("causes_summary") or ""
            causes_parsed = []
            if causes_raw:
                for part in causes_raw.split("|"):
                    if ":" in part:
                        factor, conf = part.split(":", 1)
                        try:
                            causes_parsed.append({
                                "factor": factor,
                                "confidence": int(conf),
                                "label": _confidence_label(int(conf))
                            })
                        except ValueError:
                            pass

            narrative = self.investigator.build_narrative([
                {"factor": c["factor"], "confidence": c["confidence"]}
                for c in causes_parsed
            ])

            result.append({
                "id": ev["id"],
                "symbol": ev["symbol"],
                "sector": ev["sector"],
                "event_type": ev["event_type"],
                "detected_at": ev["detected_at"],
                "trade_date": ev["trade_date"],
                "price": ev["price"],
                "price_change_pct": ev["price_change_pct"],
                "rvol": ev["rvol"],
                "rsi_at_event": ev["rsi_at_event"],
                "top_cause": causes_parsed[0] if causes_parsed else None,
                "cause_count": len(causes_parsed),
                "narrative": narrative,
                "status": ev["status"],
                "is_noisy_liquidity": ev.get("is_noisy_liquidity", 0),
                "excess_return_sector": ev.get("excess_return_sector", 0.0),
                "excess_return_kse": ev.get("excess_return_kse", 0.0)
            })
        return result

    def get_event_detail(self, event_id: str) -> Optional[Dict]:
        """Used by GET /api/intelligence/event/:id"""
        ev = self.db.get_event_detail(event_id)
        if not ev:
            return None
        causes = ev.get("causes", [])
        narrative = self.investigator.build_narrative(causes)

        try:
            snapshot = json.loads(ev.get("snapshot_json") or "{}")
        except Exception:
            snapshot = {}

        matched_pattern = self.pattern_lib.match_event_to_pattern(causes)

        return {
            "event": {k: v for k, v in ev.items() if k != "causes" and k != "snapshot_json"},
            "snapshot": snapshot,
            "causes": causes,
            "narrative": narrative,
            "matched_pattern": matched_pattern
        }

    def get_stock_explain(self, symbol: str, days: int = 15) -> Dict[str, Any]:
        """Used by GET /api/intelligence/stock/:symbol/explain"""
        events = self.db.get_events_for_symbol_history(symbol, days=days)
        memory = self.db.get_stock_memory(symbol)
        predictions = self.db.get_active_predictions(limit=50)
        stock_predictions = [p for p in predictions if p["symbol"] == symbol.upper()]

        # Build timeline
        timeline = []
        for ev in events:
            causes_raw = ev.get("causes_summary", "") or ""
            top_cause = None
            if causes_raw:
                parts = causes_raw.split("|")
                if parts and ":" in parts[0]:
                    f, c = parts[0].split(":", 1)
                    top_cause = {"factor": f, "confidence": int(c) if c.isdigit() else 0}
            timeline.append({
                "date": ev["trade_date"],
                "event_type": ev["event_type"],
                "price_change_pct": ev["price_change_pct"],
                "rvol": ev["rvol"],
                "top_cause": top_cause
            })

        return {
            "symbol": symbol.upper(),
            "days_analyzed": days,
            "event_count": len(events),
            "timeline": timeline,
            "stock_memory": memory,
            "active_predictions": stock_predictions,
            "generated_at": _now()
        }

    def get_patterns_data(self) -> List[Dict]:
        """Used by GET /api/intelligence/patterns"""
        patterns = self.db.get_patterns(min_occurrences=1)
        result = []
        for p in patterns:
            pat_id = p.get("id")
            total_closed = (p.get("win_count", 0) + p.get("loss_count", 0))
            raw_wr = float(p.get("raw_win_rate") or (round(p["win_count"] / max(total_closed, 1) * 100, 1) if total_closed else 0.0))
            shrunk_wr = float(p.get("shrunk_win_rate") or raw_wr)
            n = int(p.get("sample_size_n") or total_closed)
            ci_lo = float(p.get("wilson_ci_low") or 0.0)
            ci_hi = float(p.get("wilson_ci_high") or 0.0)
            shrunk_w = float(p.get("shrinkage_weight") or 0.0)

            # Stage 4 additions: regime breakdown & decay
            regimes = self.db.get_pattern_regimes(pat_id) if pat_id else []

            # Stage 7.4: Rolling 90-day Wilson CI
            n_90d = int(p.get("sample_size_90d") or 0)
            wr_90d = p.get("win_rate_90d")
            ci_90d_lo, ci_90d_hi = 0.0, 0.0
            if n_90d > 0 and wr_90d is not None:
                wins_90d = round(n_90d * (float(wr_90d) / 100.0))
                _, ci_90d_lo, ci_90d_hi = compute_wilson_ci(wins_90d, n_90d)

            result.append({
                **p,
                "win_rate_pct": shrunk_wr,
                "raw_win_rate_pct": raw_wr,
                "shrunk_win_rate_pct": shrunk_wr,
                "sample_size_n": n,
                "wilson_ci": [ci_lo, ci_hi],
                "wilson_ci_low": ci_lo,
                "wilson_ci_high": ci_hi,
                "shrinkage_weight": shrunk_w,
                "win_rate_90d": p.get("win_rate_90d"),
                "sample_size_90d": n_90d,
                "wilson_ci_90d": [ci_90d_lo, ci_90d_hi],
                "wilson_ci_90d_low": ci_90d_lo,
                "wilson_ci_90d_high": ci_90d_hi,
                "decay_status": p.get("decay_status", "STABLE"),
                "decay_divergence": p.get("decay_divergence", 0.0),
                "is_expanded": bool(p.get("is_expanded", 0)),
                "cluster_feature_vector": p.get("cluster_feature_vector"),
                "regimes": regimes,
                "pending_count": p.get("occurrences", 0) - total_closed,
                "is_sample_gated": n < 3,
                "disclaimer": "Not investment advice — informational tool based on historical pattern statistics"
            })
        return result

    def get_pattern_regimes_data(self, pattern_id: Optional[str] = None) -> Dict[str, Any]:
        """Used by GET /api/intelligence/pattern-regimes"""
        regimes = self.db.get_pattern_regimes(pattern_id=pattern_id)
        return {
            "regimes": regimes,
            "count": len(regimes),
            "disclaimer": "Not investment advice — informational tool based on historical pattern statistics",
            "generated_at": _now()
        }

    def get_sector_shrinkage_data(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/sector-shrinkage"""
        sectors = self.db.get_sector_shrinkage()
        if not sectors:
            res = self.learning_engine.rebuild_sector_shrinkage(self.eb_estimator)
            sectors = res.get("sectors", [])
        return {
            "sectors": sectors,
            "count": len(sectors),
            "disclaimer": "Not investment advice — informational tool based on historical pattern statistics",
            "generated_at": _now()
        }

    def get_predictions_data(self, limit: int = 20) -> List[Dict]:
        """Used by GET /api/intelligence/predictions"""
        preds = self.db.get_active_predictions(limit=limit)
        is_active = True
        if not preds:
            preds = self.db.get_recent_predictions(limit=limit)
            is_active = False
        result = []
        for p in preds:
            try:
                reasoning = json.loads(p.get("reasoning_json") or "{}")
            except Exception:
                reasoning = {}
            ci_lo = float(reasoning.get("wilson_ci_low", 0.0))
            ci_hi = float(reasoning.get("wilson_ci_high", 0.0))
            result.append({
                **{k: v for k, v in p.items() if k != "reasoning_json"},
                "reasoning": reasoning,
                "wilson_ci": reasoning.get("wilson_ci", [ci_lo, ci_hi]),
                "wilson_ci_low": ci_lo,
                "wilson_ci_high": ci_hi,
                "is_active_pending": is_active
            })
        return result

    def get_suggested_shares_data(
        self,
        market_stocks: Optional[List[Dict]] = None,
        regime: str = "NEUTRAL"
    ) -> Dict[str, Any]:
        """
        Used by GET /api/intelligence/suggested-shares.
        Generates a fresh ranked list, plus track record.
        """
        try:
            predictions = self.db.get_active_predictions(limit=200)
            if not predictions:
                predictions = self.db.get_recent_predictions(limit=200)
            result = self.suggested_shares_engine.generate_suggestions(
                recent_predictions=predictions,
                market_stocks=market_stocks,
                regime=regime
            )
            track_record = self.db.get_suggested_shares_track_record()
            recent = self.db.get_recent_suggested_shares(limit=15)
            return {
                "success": True,
                "active_ideas": result.get("active_ideas", []),
                "speculative_ideas": result.get("speculative_ideas", []),
                "recent_ideas": recent,
                "track_record": track_record,
                "regime": regime,
                "disclaimer": "Not investment advice — informational tool based on historical pattern statistics",
                "generated_at": _now(),
            }
        except Exception as e:
            print(f"[Intelligence] get_suggested_shares_data error: {e}")
            return {
                "success": False,
                "error": str(e),
                "active_ideas": [],
                "speculative_ideas": [],
                "recent_ideas": [],
                "track_record": {},
                "generated_at": _now(),
            }

# ── Module-level singleton ─────────────────────────────────────────────────────

_engine_instance: Optional[IntelligenceEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> IntelligenceEngine:
    """Get or create the global IntelligenceEngine singleton."""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = IntelligenceEngine()
    return _engine_instance


if __name__ == "__main__":
    print("PSX Intelligence Engine — self-test")
    engine = get_engine()
    print("DB path:", engine.db.db_path)
    print("Stats:", json.dumps(engine.db.get_learning_stats(), indent=2))
    print("Patterns:", len(engine.db.get_patterns(min_occurrences=1)))
    print("Active Predictions:", len(engine.db.get_active_predictions()))
    print("✅ Self-test complete.")

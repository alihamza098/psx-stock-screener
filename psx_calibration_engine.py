#!/usr/bin/env python3
"""
PSX Self-Learning Calibration Engine
======================================
Reads outcome data from both weekly_scan.db (prediction_audits) and
intelligence.db (ai_predictions, pattern_occurrences) to continuously:

  1. Aggregate closed trade outcomes grouped by trigger type, grade,
     sector, market regime, and causal factor
  2. Apply Bayesian smoothing to derive reliable per-factor win rates
     even with small sample sizes
  3. Sweep config parameters to find PSX-optimal thresholds
  4. Backtest proposed changes against stored history before applying
  5. Auto-update weekly scan config (1 change per cycle, with safeguards)
  6. Maintain a transparent log of every algorithm adjustment

Runs every Sunday 11 PM PKT via server.py scheduler.
All computation is local, deterministic, zero-cost.

Database: cache/calibration.db
"""

import sqlite3
import json
import time
import threading
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR       = Path(__file__).parent
CALIB_DB       = BASE_DIR / "cache" / "calibration.db"
WEEKLY_DB      = BASE_DIR / "cache" / "weekly_scan.db"
INTEL_DB       = BASE_DIR / "cache" / "intelligence.db"

# ── Constants ─────────────────────────────────────────────────────────────────

# Bayesian prior — equivalent to N "neutral" (50%) samples
BAYESIAN_PRIOR_STRENGTH = 5

# Minimum closed outcomes before any config change is allowed
MIN_SAMPLES_FOR_CHANGE  = 10

# Recency decay windows (days)
RECENCY_FULL_DAYS   = 90    # weight = 1.0
RECENCY_HALF_DAYS   = 180   # weight = 0.7
RECENCY_MIN_WEIGHT  = 0.4   # older than 180 days

# Config sweep ranges
SWEEP_SCORE_THRESHOLDS = [3, 4, 5, 6]
SWEEP_MIN_RR = [1.2, 1.4, 1.5, 1.6, 1.8, 2.0, 2.2, 2.5]
SWEEP_ATR_MULT = [1.0, 1.2, 1.5, 1.8, 2.0, 2.2, 2.5]
SWEEP_BREAKOUT_LOOKBACK = [10, 15, 20, 25, 30]

# Minimum improvement (profit factor delta) to apply config change
MIN_BACKTEST_IMPROVEMENT = 0.05   # 5% better profit factor

# Max weight ceiling / floor
WEIGHT_MAX = 2.0
WEIGHT_MIN = 0.3


# ── Utility ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _today() -> str:
    return datetime.date.today().isoformat()

def _days_ago(iso_str: str) -> int:
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - dt
        return max(0, delta.days)
    except Exception:
        return 999

def _recency_weight(iso_str: str) -> float:
    days = _days_ago(iso_str)
    if days <= RECENCY_FULL_DAYS:
        return 1.0
    if days <= RECENCY_HALF_DAYS:
        return 0.7
    return RECENCY_MIN_WEIGHT


# ── Calibration Database ──────────────────────────────────────────────────────

class CalibrationDB:
    """SQLite database for calibration metrics and history."""

    def __init__(self, db_path: Path = CALIB_DB):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript("""
                -- Per-factor / trigger-type performance stats
                CREATE TABLE IF NOT EXISTS factor_weights (
                    id              TEXT PRIMARY KEY,
                    factor_type     TEXT NOT NULL,
                    factor_value    TEXT NOT NULL,
                    source          TEXT NOT NULL DEFAULT 'weekly',
                    sample_count    INTEGER DEFAULT 0,
                    win_count       INTEGER DEFAULT 0,
                    loss_count      INTEGER DEFAULT 0,
                    raw_win_rate    REAL DEFAULT 0.5,
                    smoothed_win_rate REAL DEFAULT 0.5,
                    weight          REAL DEFAULT 1.0,
                    last_updated    TEXT,
                    UNIQUE(factor_type, factor_value, source)
                );

                -- Per-intelligence-pattern edge stats
                CREATE TABLE IF NOT EXISTS pattern_edge (
                    pattern_id      TEXT PRIMARY KEY,
                    pattern_name    TEXT NOT NULL,
                    sample_count    INTEGER DEFAULT 0,
                    win_count       INTEGER DEFAULT 0,
                    loss_count      INTEGER DEFAULT 0,
                    raw_win_rate    REAL DEFAULT 0.0,
                    psxEdge         REAL DEFAULT 0.0,
                    recommended_confidence_floor INTEGER DEFAULT 50,
                    last_updated    TEXT
                );

                -- Per-sector predictability stats
                CREATE TABLE IF NOT EXISTS sector_stats (
                    sector          TEXT PRIMARY KEY,
                    sample_count    INTEGER DEFAULT 0,
                    win_count       INTEGER DEFAULT 0,
                    loss_count      INTEGER DEFAULT 0,
                    win_rate        REAL DEFAULT 0.0,
                    avg_days_to_outcome REAL DEFAULT 0.0,
                    avg_winner_gain REAL DEFAULT 0.0,
                    avg_loser_loss  REAL DEFAULT 0.0,
                    profit_factor   REAL DEFAULT 1.0,
                    last_updated    TEXT
                );

                -- Per market-regime stats (BULL / BEAR / SIDEWAYS)
                CREATE TABLE IF NOT EXISTS regime_stats (
                    regime          TEXT PRIMARY KEY,
                    sample_count    INTEGER DEFAULT 0,
                    win_count       INTEGER DEFAULT 0,
                    loss_count      INTEGER DEFAULT 0,
                    win_rate        REAL DEFAULT 0.0,
                    best_trigger    TEXT,
                    last_updated    TEXT
                );

                -- Chronological log of every calibration run
                CREATE TABLE IF NOT EXISTS calibration_runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at          TEXT NOT NULL,
                    total_samples   INTEGER DEFAULT 0,
                    closed_samples  INTEGER DEFAULT 0,
                    overall_win_rate REAL DEFAULT 0.0,
                    profit_factor   REAL DEFAULT 1.0,
                    changes_applied INTEGER DEFAULT 0,
                    summary         TEXT,
                    details_json    TEXT
                );

                -- Config change recommendations (applied or rejected)
                CREATE TABLE IF NOT EXISTS config_recommendations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommended_at  TEXT NOT NULL,
                    param_name      TEXT NOT NULL,
                    old_value       TEXT NOT NULL,
                    new_value       TEXT NOT NULL,
                    reason          TEXT NOT NULL,
                    sample_count    INTEGER DEFAULT 0,
                    backtest_pf_before REAL DEFAULT 0.0,
                    backtest_pf_after  REAL DEFAULT 0.0,
                    applied         INTEGER DEFAULT 0,
                    applied_at      TEXT,
                    reverted        INTEGER DEFAULT 0,
                    reverted_at     TEXT,
                    revert_reason   TEXT
                );
                """)
                conn.commit()
            finally:
                conn.close()

    def upsert_factor_weight(self, fw: Dict[str, Any]):
        key = f"{fw['factor_type']}::{fw['factor_value']}::{fw.get('source','weekly')}"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO factor_weights
                    (id, factor_type, factor_value, source, sample_count, win_count,
                     loss_count, raw_win_rate, smoothed_win_rate, weight, last_updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(factor_type, factor_value, source) DO UPDATE SET
                        sample_count = excluded.sample_count,
                        win_count    = excluded.win_count,
                        loss_count   = excluded.loss_count,
                        raw_win_rate = excluded.raw_win_rate,
                        smoothed_win_rate = excluded.smoothed_win_rate,
                        weight       = excluded.weight,
                        last_updated = excluded.last_updated
                """, (key, fw['factor_type'], fw['factor_value'], fw.get('source','weekly'),
                      fw['sample_count'], fw['win_count'], fw['loss_count'],
                      fw['raw_win_rate'], fw['smoothed_win_rate'], fw['weight'],
                      fw.get('last_updated', _now())))
                conn.commit()
            finally:
                conn.close()

    def upsert_pattern_edge(self, pe: Dict[str, Any]):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO pattern_edge
                    (pattern_id, pattern_name, sample_count, win_count, loss_count,
                     raw_win_rate, psxEdge, recommended_confidence_floor, last_updated)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(pattern_id) DO UPDATE SET
                        sample_count = excluded.sample_count,
                        win_count    = excluded.win_count,
                        loss_count   = excluded.loss_count,
                        raw_win_rate = excluded.raw_win_rate,
                        psxEdge      = excluded.psxEdge,
                        recommended_confidence_floor = excluded.recommended_confidence_floor,
                        last_updated = excluded.last_updated
                """, (pe['pattern_id'], pe['pattern_name'], pe['sample_count'],
                      pe['win_count'], pe['loss_count'], pe['raw_win_rate'],
                      pe['psxEdge'], pe['recommended_confidence_floor'], _now()))
                conn.commit()
            finally:
                conn.close()

    def upsert_sector_stat(self, s: Dict[str, Any]):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO sector_stats
                    (sector, sample_count, win_count, loss_count, win_rate,
                     avg_days_to_outcome, avg_winner_gain, avg_loser_loss,
                     profit_factor, last_updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(sector) DO UPDATE SET
                        sample_count = excluded.sample_count,
                        win_count    = excluded.win_count,
                        loss_count   = excluded.loss_count,
                        win_rate     = excluded.win_rate,
                        avg_days_to_outcome = excluded.avg_days_to_outcome,
                        avg_winner_gain = excluded.avg_winner_gain,
                        avg_loser_loss  = excluded.avg_loser_loss,
                        profit_factor   = excluded.profit_factor,
                        last_updated    = excluded.last_updated
                """, (s['sector'], s['sample_count'], s['win_count'], s['loss_count'],
                      s['win_rate'], s['avg_days_to_outcome'], s['avg_winner_gain'],
                      s['avg_loser_loss'], s['profit_factor'], _now()))
                conn.commit()
            finally:
                conn.close()

    def log_calibration_run(self, run: Dict[str, Any]) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("""
                    INSERT INTO calibration_runs
                    (run_at, total_samples, closed_samples, overall_win_rate,
                     profit_factor, changes_applied, summary, details_json)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (run['run_at'], run['total_samples'], run['closed_samples'],
                      run['overall_win_rate'], run['profit_factor'],
                      run['changes_applied'], run['summary'],
                      json.dumps(run.get('details', {}))))
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def log_recommendation(self, rec: Dict[str, Any]) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("""
                    INSERT INTO config_recommendations
                    (recommended_at, param_name, old_value, new_value, reason,
                     sample_count, backtest_pf_before, backtest_pf_after, applied)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (_now(), rec['param_name'], str(rec['old_value']), str(rec['new_value']),
                      rec['reason'], rec['sample_count'],
                      rec['backtest_pf_before'], rec['backtest_pf_after'],
                      1 if rec.get('applied') else 0))
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def get_all_factor_weights(self) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM factor_weights ORDER BY weight DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_sector_stats(self) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sector_stats ORDER BY win_rate DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_pattern_edge(self) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM pattern_edge ORDER BY psxEdge DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_calibration_history(self, limit: int = 20) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM calibration_runs ORDER BY run_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_recommendations_history(self, limit: int = 30) -> List[Dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM config_recommendations ORDER BY recommended_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_stats_summary(self) -> Dict[str, Any]:
        conn = self._connect()
        try:
            run_count = conn.execute(
                "SELECT COUNT(*) FROM calibration_runs"
            ).fetchone()[0]
            changes  = conn.execute(
                "SELECT COUNT(*) FROM config_recommendations WHERE applied = 1"
            ).fetchone()[0]
            factor_count = conn.execute(
                "SELECT COUNT(*) FROM factor_weights WHERE sample_count >= 3"
            ).fetchone()[0]
            last_run = conn.execute(
                "SELECT run_at FROM calibration_runs ORDER BY run_at DESC LIMIT 1"
            ).fetchone()
            return {
                "calibration_runs_total": run_count,
                "config_changes_applied": changes,
                "factor_profiles": factor_count,
                "last_calibration_at": last_run["run_at"] if last_run else "Never"
            }
        finally:
            conn.close()


# ── Outcome Aggregator ────────────────────────────────────────────────────────

class OutcomeAggregator:
    """
    Reads closed outcomes from both weekly_scan.db and intelligence.db,
    normalises them into a unified list for statistical analysis.
    """

    def read_weekly_outcomes(self) -> List[Dict]:
        """
        Reads all CLOSED prediction_audits from weekly_scan.db.
        Returns list of normalised outcome records.
        """
        if not WEEKLY_DB.exists():
            return []
        outcomes = []
        try:
            conn = sqlite3.connect(str(WEEKLY_DB), timeout=10)
            conn.row_factory = sqlite3.Row
            # Join audits with candidates to get trigger_type
            rows = conn.execute("""
                SELECT pa.*,
                       sc.trend_score, sc.trigger_score, sc.volume_score,
                       sc.stock_trend_direction, sc.index_trend_direction
                FROM prediction_audits pa
                LEFT JOIN scan_candidates sc ON sc.id = pa.candidate_id
                WHERE pa.outcome IN ('SUCCESSFUL', 'STOPPED_OUT')
            """).fetchall()
            # Get trigger types per candidate
            trigger_rows = conn.execute(
                "SELECT candidate_id, trigger_type, volume_ratio_to_avg20d FROM scan_candidate_triggers"
            ).fetchall()
            trigger_map: Dict[str, List[str]] = {}
            for t in trigger_rows:
                trigger_map.setdefault(t["candidate_id"], []).append(t["trigger_type"])

            conn.close()
            for r in rows:
                d = dict(r)
                d["trigger_types"] = trigger_map.get(d["candidate_id"], [])
                d["is_win"] = (d["outcome"] == "SUCCESSFUL")
                outcomes.append(d)
        except Exception as e:
            print(f"[Calibration] Weekly DB read error: {e}")
        return outcomes

    def read_intelligence_outcomes(self) -> List[Dict]:
        """
        Reads all evaluated (non-PENDING) ai_predictions from intelligence.db.
        """
        if not INTEL_DB.exists():
            return []
        outcomes = []
        try:
            conn = sqlite3.connect(str(INTEL_DB), timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT ap.*, se.sector, se.event_type, se.rsi_at_event,
                       se.macd_bullish, se.sector_return_5d
                FROM ai_predictions ap
                LEFT JOIN stock_events se ON se.id = ap.event_id
                WHERE ap.outcome IN ('CORRECT', 'INCORRECT')
            """).fetchall()
            conn.close()
            for r in rows:
                d = dict(r)
                d["is_win"] = (d["outcome"] == "CORRECT")
                outcomes.append(d)
        except Exception as e:
            print(f"[Calibration] Intel DB read error: {e}")
        return outcomes

    def read_event_causes(self) -> List[Dict]:
        """Read all event causes with their linked prediction outcomes."""
        if not INTEL_DB.exists():
            return []
        results = []
        try:
            conn = sqlite3.connect(str(INTEL_DB), timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT ec.factor, ec.confidence, ec.evidence,
                       ap.outcome, ap.actual_return_5d, ap.predicted_at
                FROM event_causes ec
                JOIN stock_events se ON se.id = ec.event_id
                JOIN ai_predictions ap ON ap.event_id = se.id
                WHERE ap.outcome IN ('CORRECT', 'INCORRECT')
            """).fetchall()
            conn.close()
            for r in rows:
                results.append(dict(r))
        except Exception as e:
            print(f"[Calibration] Event causes read error: {e}")
        return results


# ── Factor Weight Engine ──────────────────────────────────────────────────────

class FactorWeightEngine:
    """
    Computes Bayesian-smoothed win rates and normalised weights
    for every signal dimension: trigger type, grade, sector, stop_basis,
    and intelligence causal factors.
    """

    def __init__(self, db: CalibrationDB):
        self.db = db

    def _bayesian_smooth(self, wins: float, losses: float) -> float:
        """Bayesian smoothing: pulls toward 50% when sample is small."""
        prior = BAYESIAN_PRIOR_STRENGTH
        return (wins + prior * 0.5) / (wins + losses + prior)

    def _to_weight(self, smoothed_rate: float) -> float:
        """Convert win rate [0,1] to multiplicative weight [WEIGHT_MIN, WEIGHT_MAX]."""
        weight = WEIGHT_MIN + smoothed_rate * (WEIGHT_MAX - WEIGHT_MIN)
        return round(max(WEIGHT_MIN, min(WEIGHT_MAX, weight)), 3)

    def calculate_all(self, weekly_outcomes: List[Dict],
                      intel_outcomes: List[Dict],
                      event_causes: List[Dict]):
        """Run all factor weight calculations and save to DB."""
        self._calc_trigger_types(weekly_outcomes)
        self._calc_grades(weekly_outcomes)
        self._calc_stop_basis(weekly_outcomes)
        self._calc_rr_buckets(weekly_outcomes)
        self._calc_score_levels(weekly_outcomes)
        self._calc_intel_signals(intel_outcomes)
        self._calc_causal_factors(event_causes)

    def _group_and_save(self, outcomes: List[Dict], key_fn,
                        factor_type: str, source: str = 'weekly'):
        """Generic grouping helper."""
        groups: Dict[str, Dict] = {}
        for o in outcomes:
            key = key_fn(o)
            if not key:
                continue
            if key not in groups:
                groups[key] = {"wins": 0.0, "losses": 0.0, "total": 0, "dates": []}
            w = _recency_weight(o.get("predicted_at") or o.get("last_evaluated_at") or "")
            if o.get("is_win"):
                groups[key]["wins"] += w
            else:
                groups[key]["losses"] += w
            groups[key]["total"] += 1

        for key, g in groups.items():
            wins, losses = g["wins"], g["losses"]
            total = g["total"]
            raw_wr = wins / max(wins + losses, 1)
            smoothed = self._bayesian_smooth(wins, losses)
            weight = self._to_weight(smoothed)
            self.db.upsert_factor_weight({
                "factor_type": factor_type,
                "factor_value": key,
                "source": source,
                "sample_count": total,
                "win_count": round(wins),
                "loss_count": round(losses),
                "raw_win_rate": round(raw_wr, 4),
                "smoothed_win_rate": round(smoothed, 4),
                "weight": weight,
                "last_updated": _now()
            })

    def _calc_trigger_types(self, outcomes: List[Dict]):
        def key(o):
            types = o.get("trigger_types") or []
            return types[0] if types else None
        self._group_and_save(outcomes, key, "TRIGGER_TYPE", "weekly")

        # Also by individual trigger
        flat = []
        for o in outcomes:
            for t in (o.get("trigger_types") or []):
                flat.append({**o, "_trigger": t})
        self._group_and_save(flat, lambda o: o.get("_trigger"), "TRIGGER_INDIVIDUAL", "weekly")

    def _calc_grades(self, outcomes: List[Dict]):
        self._group_and_save(outcomes, lambda o: o.get("grade"), "GRADE", "weekly")

    def _calc_stop_basis(self, outcomes: List[Dict]):
        self._group_and_save(outcomes, lambda o: o.get("stop_basis"), "STOP_BASIS", "weekly")

    def _calc_rr_buckets(self, outcomes: List[Dict]):
        def rr_bucket(o):
            rr = float(o.get("reward_risk_ratio") or 0)
            if rr < 1.5: return "RR_BELOW_1.5"
            if rr < 2.0: return "RR_1.5_2.0"
            if rr < 2.5: return "RR_2.0_2.5"
            return "RR_ABOVE_2.5"
        self._group_and_save(outcomes, rr_bucket, "RR_BUCKET", "weekly")

    def _calc_score_levels(self, outcomes: List[Dict]):
        def score_key(o):
            s = o.get("raw_score")
            return f"SCORE_{s}" if s is not None else None
        self._group_and_save(outcomes, score_key, "RAW_SCORE", "weekly")

    def _calc_intel_signals(self, intel_outcomes: List[Dict]):
        self._group_and_save(intel_outcomes,
                             lambda o: o.get("signal"), "INTEL_SIGNAL", "intelligence")
        self._group_and_save(intel_outcomes,
                             lambda o: o.get("sector"), "SECTOR_INTEL", "intelligence")

    def _calc_causal_factors(self, event_causes: List[Dict]):
        def conf_bucket(o):
            factor = o.get("factor")
            conf = int(o.get("confidence") or 0)
            bucket = "HIGH" if conf >= 70 else ("MED" if conf >= 50 else "LOW")
            return f"{factor}::{bucket}" if factor else None
        self._group_and_save(event_causes, conf_bucket, "CAUSAL_FACTOR", "intelligence")


# ── Sector Stats Builder ───────────────────────────────────────────────────────

class SectorStatsBuilder:
    """Computes per-sector prediction reliability statistics."""

    def __init__(self, db: CalibrationDB):
        self.db = db

    def build(self, weekly_outcomes: List[Dict]):
        sectors: Dict[str, Dict] = {}
        for o in weekly_outcomes:
            sec = o.get("sector") or "Other"
            if sec not in sectors:
                sectors[sec] = {"wins": [], "losses": [], "days": [], "gains": [], "losses_pct": []}
            if o.get("is_win"):
                sectors[sec]["wins"].append(o)
                g = float(o.get("max_gain_pct") or 0)
                if g > 0:
                    sectors[sec]["gains"].append(g)
            else:
                sectors[sec]["losses"].append(o)
                l = abs(float(o.get("max_loss_pct") or 0))
                if l > 0:
                    sectors[sec]["losses_pct"].append(l)
            days = int(o.get("days_elapsed") or 0)
            sectors[sec]["days"].append(days)

        for sec, data in sectors.items():
            wins = len(data["wins"])
            losses = len(data["losses"])
            total = wins + losses
            if total == 0:
                continue
            win_rate = round(wins / total, 4)
            avg_days = round(sum(data["days"]) / max(len(data["days"]), 1), 1)
            avg_gain = round(sum(data["gains"]) / max(len(data["gains"]), 1), 2)
            avg_loss = round(sum(data["losses_pct"]) / max(len(data["losses_pct"]), 1), 2)
            gross_gains = sum(data["gains"])
            gross_losses = sum(data["losses_pct"])
            pf = round(gross_gains / max(gross_losses, 0.01), 2)
            self.db.upsert_sector_stat({
                "sector": sec,
                "sample_count": total,
                "win_count": wins,
                "loss_count": losses,
                "win_rate": win_rate,
                "avg_days_to_outcome": avg_days,
                "avg_winner_gain": avg_gain,
                "avg_loser_loss": avg_loss,
                "profit_factor": pf
            })


# ── Pattern Edge Builder ──────────────────────────────────────────────────────

class PatternEdgeBuilder:
    """Computes PSX-specific edge for each Intelligence Engine pattern."""

    # Pattern definitions (same IDs as psx_intelligence_engine)
    PATTERNS = {
        "P001": "Breakout + Volume Surge",
        "P002": "3-Day Accumulation Breakout",
        "P003": "Upper Lock with Accumulation",
        "P004": "Sector-Led Breakout",
        "P005": "MACD + Volume Surge",
        "P006": "RSI Momentum + Breakout",
        "P007": "Full Confluence Setup",
        "P008": "Volume Surge Only"
    }

    def __init__(self, db: CalibrationDB):
        self.db = db

    def build(self):
        if not INTEL_DB.exists():
            return
        try:
            conn = sqlite3.connect(str(INTEL_DB), timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT dp.id as pattern_id, dp.name, dp.occurrences,
                       dp.win_count, dp.loss_count, dp.avg_5d_return
                FROM detected_patterns dp
            """).fetchall()
            conn.close()

            for r in rows:
                total = (r["win_count"] or 0) + (r["loss_count"] or 0)
                raw_wr = (r["win_count"] or 0) / max(total, 1) if total > 0 else 0.5
                edge = round(raw_wr - 0.5, 4)  # edge above 50% random
                # Recommended confidence floor: if edge > 0.15 → use 65, else use 50
                conf_floor = 65 if edge >= 0.15 else (55 if edge >= 0.05 else 45)
                self.db.upsert_pattern_edge({
                    "pattern_id": r["pattern_id"],
                    "pattern_name": r["name"],
                    "sample_count": r["occurrences"] or 0,
                    "win_count": r["win_count"] or 0,
                    "loss_count": r["loss_count"] or 0,
                    "raw_win_rate": round(raw_wr, 4),
                    "psxEdge": edge,
                    "recommended_confidence_floor": conf_floor
                })
        except Exception as e:
            print(f"[Calibration] PatternEdgeBuilder error: {e}")


# ── Threshold Optimizer ───────────────────────────────────────────────────────

class ThresholdOptimizer:
    """
    Sweeps config parameter ranges to find PSX-optimal values.
    Tests each candidate threshold against historical outcome data
    and computes the resulting profit factor.
    """

    def find_optimal_score_threshold(self, outcomes: List[Dict]) -> Tuple[int, float]:
        """Find rawScore threshold that maximises profit factor."""
        best_threshold, best_pf = 3, 1.0
        for thresh in SWEEP_SCORE_THRESHOLDS:
            filtered = [o for o in outcomes if int(o.get("raw_score") or 0) >= thresh]
            pf = self._profit_factor(filtered)
            if pf > best_pf and len(filtered) >= MIN_SAMPLES_FOR_CHANGE:
                best_pf = pf
                best_threshold = thresh
        return best_threshold, round(best_pf, 3)

    def find_optimal_min_rr(self, outcomes: List[Dict]) -> Tuple[float, float]:
        """Find minRewardRiskRatio that maximises profit factor."""
        best_rr, best_pf = 1.5, 1.0
        for rr in SWEEP_MIN_RR:
            filtered = [o for o in outcomes if float(o.get("reward_risk_ratio") or 0) >= rr]
            pf = self._profit_factor(filtered)
            if pf > best_pf and len(filtered) >= MIN_SAMPLES_FOR_CHANGE:
                best_pf = pf
                best_rr = rr
        return best_rr, round(best_pf, 3)

    def find_optimal_atr_multiple(self, outcomes: List[Dict]) -> Tuple[float, float]:
        """
        Proxy: compare STOPPED_OUT rate vs ATR multiple band.
        We don't have exact ATR at time of outcome, so we use stop_distance_pct
        from entry vs the standardised ATR multiple bucket.
        """
        # Group by stop_basis and find which stop_basis has lowest stopped rate
        basis_outcomes: Dict[str, List] = {}
        for o in outcomes:
            basis = o.get("stop_basis") or "UNKNOWN"
            basis_outcomes.setdefault(basis, []).append(o)

        best_basis = "ATR_1.5"
        best_pf = 1.0
        for basis, group in basis_outcomes.items():
            pf = self._profit_factor(group)
            if pf > best_pf and len(group) >= 5:
                best_pf = pf
                best_basis = basis

        # Map stop_basis back to ATR multiple (heuristic)
        atr_map = {
            "SWING_STRUCTURE": 1.5,
            "ATR_1.0": 1.0, "ATR_1.5": 1.5, "ATR_2.0": 2.0
        }
        return atr_map.get(best_basis, 1.5), round(best_pf, 3)

    def _profit_factor(self, outcomes: List[Dict]) -> float:
        wins  = [o for o in outcomes if o.get("is_win")]
        losses = [o for o in outcomes if not o.get("is_win")]
        if not wins or not losses:
            return 1.0
        gross_gain = sum(abs(float(o.get("max_gain_pct") or 0)) for o in wins)
        gross_loss = sum(abs(float(o.get("max_loss_pct") or 0)) for o in losses)
        return round(gross_gain / max(gross_loss, 0.01), 3)


# ── Backtest Engine ───────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Validates proposed parameter changes by simulating them against
    all stored historical outcomes. Answers: "If we had used these
    parameters from the start, would our results have been better?"
    """

    def backtest_score_threshold(self, outcomes: List[Dict],
                                  old_thresh: int, new_thresh: int) -> Tuple[float, float]:
        """Compare profit factor at old vs new score threshold."""
        old_set = [o for o in outcomes if int(o.get("raw_score") or 0) >= old_thresh]
        new_set = [o for o in outcomes if int(o.get("raw_score") or 0) >= new_thresh]
        pf_old = self._pf(old_set)
        pf_new = self._pf(new_set)
        return round(pf_old, 3), round(pf_new, 3)

    def backtest_min_rr(self, outcomes: List[Dict],
                         old_rr: float, new_rr: float) -> Tuple[float, float]:
        old_set = [o for o in outcomes if float(o.get("reward_risk_ratio") or 0) >= old_rr]
        new_set = [o for o in outcomes if float(o.get("reward_risk_ratio") or 0) >= new_rr]
        return round(self._pf(old_set), 3), round(self._pf(new_set), 3)

    def _pf(self, outcomes: List[Dict]) -> float:
        if not outcomes:
            return 1.0
        wins   = [o for o in outcomes if o.get("is_win")]
        losses = [o for o in outcomes if not o.get("is_win")]
        if not losses:
            return 2.0
        gross_gain = sum(abs(float(o.get("max_gain_pct") or 0)) for o in wins)
        gross_loss = sum(abs(float(o.get("max_loss_pct") or 0)) for o in losses)
        return round(gross_gain / max(gross_loss, 0.01), 3)


# ── Auto Config Updater ───────────────────────────────────────────────────────

class AutoConfigUpdater:
    """
    Applies validated config changes to weekly_scan.db via weekly_scan_engine.
    Hard gates: only 1 change per calibration run, min 10 samples, backtest improvement required.
    """

    def __init__(self, db: CalibrationDB):
        self.db = db

    def propose_and_apply(self, weekly_outcomes: List[Dict],
                           optimizer: ThresholdOptimizer,
                           backtester: BacktestEngine) -> List[Dict]:
        """
        Returns list of changes that were applied.
        """
        if len(weekly_outcomes) < MIN_SAMPLES_FOR_CHANGE:
            print(f"[Calibration] Only {len(weekly_outcomes)} closed outcomes — need {MIN_SAMPLES_FOR_CHANGE}. Skipping config update.")
            return []

        try:
            import weekly_scan_engine as weekly_engine
            current_config = weekly_engine.get_current_config()
        except Exception as e:
            print(f"[Calibration] Could not load weekly config: {e}")
            return []

        changes_applied = []

        # ── Evaluate minRR ──────────────────────────────────────────────────
        new_rr, new_rr_pf = optimizer.find_optimal_min_rr(weekly_outcomes)
        old_rr = float(current_config.get("risk", {}).get("minRewardRiskRatio", 1.5))
        if abs(new_rr - old_rr) >= 0.15:
            pf_before, pf_after = backtester.backtest_min_rr(weekly_outcomes, old_rr, new_rr)
            improvement = pf_after - pf_before
            reason = (f"Optimal minRR from {len(weekly_outcomes)}-sample analysis: "
                      f"PF improves {pf_before:.2f}→{pf_after:.2f} "
                      f"(+{improvement:.2f}) using minRR={new_rr}")
            rec = {
                "param_name": "risk.minRewardRiskRatio",
                "old_value": old_rr,
                "new_value": new_rr,
                "reason": reason,
                "sample_count": len(weekly_outcomes),
                "backtest_pf_before": pf_before,
                "backtest_pf_after": pf_after,
                "applied": False
            }
            if improvement >= MIN_BACKTEST_IMPROVEMENT:
                try:
                    new_config = dict(current_config)
                    new_config.setdefault("risk", {})["minRewardRiskRatio"] = new_rr
                    weekly_engine.save_config(new_config)
                    rec["applied"] = True
                    changes_applied.append(rec)
                    print(f"[Calibration] ✅ Applied: minRR {old_rr}→{new_rr} (PF: {pf_before:.2f}→{pf_after:.2f})")
                except Exception as e:
                    print(f"[Calibration] Failed to apply config: {e}")
            else:
                print(f"[Calibration] ❌ Rejected: minRR {old_rr}→{new_rr} (improvement {improvement:.3f} < {MIN_BACKTEST_IMPROVEMENT})")
            self.db.log_recommendation(rec)
            # Only 1 change per cycle
            if changes_applied:
                return changes_applied

        # ── Evaluate rawScore threshold ────────────────────────────────────
        new_score, _ = optimizer.find_optimal_score_threshold(weekly_outcomes)
        old_min_grade = current_config.get("output", {}).get("minGradeToDisplay", "B")
        # Derive current effective min score from grade
        grade_to_score = {"A_PLUS": 5, "A": 4, "B": 3}
        old_score = grade_to_score.get(old_min_grade, 3)
        if new_score != old_score:
            pf_before, pf_after = backtester.backtest_score_threshold(weekly_outcomes, old_score, new_score)
            improvement = pf_after - pf_before
            score_to_grade = {5: "A_PLUS", 4: "A", 3: "B"}
            new_grade = score_to_grade.get(new_score, "B")
            reason = (f"Optimal min score from {len(weekly_outcomes)}-sample analysis: "
                      f"PF improves {pf_before:.2f}→{pf_after:.2f} "
                      f"using minGrade={new_grade} (score≥{new_score})")
            rec = {
                "param_name": "output.minGradeToDisplay",
                "old_value": old_min_grade,
                "new_value": new_grade,
                "reason": reason,
                "sample_count": len(weekly_outcomes),
                "backtest_pf_before": pf_before,
                "backtest_pf_after": pf_after,
                "applied": False
            }
            if improvement >= MIN_BACKTEST_IMPROVEMENT:
                try:
                    new_config = dict(current_config)
                    new_config.setdefault("output", {})["minGradeToDisplay"] = new_grade
                    weekly_engine.save_config(new_config)
                    rec["applied"] = True
                    changes_applied.append(rec)
                    print(f"[Calibration] ✅ Applied: minGrade {old_min_grade}→{new_grade}")
                except Exception as e:
                    print(f"[Calibration] Failed to apply grade config: {e}")
            else:
                print(f"[Calibration] ❌ Rejected: grade change (improvement {improvement:.3f} < required)")
            self.db.log_recommendation(rec)

        return changes_applied


# ── Main Calibration Engine ───────────────────────────────────────────────────

class CalibrationEngine:
    """
    Top-level orchestrator — called weekly by server.py scheduler.
    """

    def __init__(self):
        self.db            = CalibrationDB()
        self.aggregator    = OutcomeAggregator()
        self.weight_engine = FactorWeightEngine(self.db)
        self.sector_builder = SectorStatsBuilder(self.db)
        self.pattern_builder = PatternEdgeBuilder(self.db)
        self.optimizer     = ThresholdOptimizer()
        self.backtester    = BacktestEngine()
        self.updater       = AutoConfigUpdater(self.db)
        print("[Calibration] Engine initialized. DB:", str(self.db.db_path))

    def run_weekly_calibration(self) -> Dict[str, Any]:
        """
        Full weekly calibration cycle. Called every Sunday 11 PM PKT.
        Returns a summary dict describing what was analysed and changed.
        """
        print("[Calibration] ─── Starting weekly calibration cycle ───")
        start_time = time.time()

        # 1. Aggregate outcomes
        weekly_outcomes = self.aggregator.read_weekly_outcomes()
        intel_outcomes  = self.aggregator.read_intelligence_outcomes()
        event_causes    = self.aggregator.read_event_causes()

        closed_weekly = len(weekly_outcomes)
        closed_intel  = len(intel_outcomes)
        all_closed    = closed_weekly + closed_intel

        print(f"[Calibration] Outcomes loaded: {closed_weekly} weekly, {closed_intel} intelligence")

        # 2. Compute factor weights
        self.weight_engine.calculate_all(weekly_outcomes, intel_outcomes, event_causes)

        # 3. Compute sector stats
        self.sector_builder.build(weekly_outcomes)

        # 4. Compute pattern edge
        self.pattern_builder.build()

        # 5. Overall performance metrics
        overall_wr = 0.0
        pf = 1.0
        if weekly_outcomes:
            wins   = [o for o in weekly_outcomes if o.get("is_win")]
            losses = [o for o in weekly_outcomes if not o.get("is_win")]
            if wins or losses:
                overall_wr = round(len(wins) / max(len(wins) + len(losses), 1), 4)
            gross_gain = sum(abs(float(o.get("max_gain_pct") or 0)) for o in wins)
            gross_loss = sum(abs(float(o.get("max_loss_pct") or 0)) for o in losses)
            pf = round(gross_gain / max(gross_loss, 0.01), 3)

        # 6. Threshold optimisation + config update
        changes = self.updater.propose_and_apply(
            weekly_outcomes, self.optimizer, self.backtester
        )

        elapsed = round(time.time() - start_time, 2)
        summary = (f"Calibration complete in {elapsed}s. "
                   f"Analysed {all_closed} closed outcomes. "
                   f"Config changes applied: {len(changes)}.")
        if changes:
            summary += " Changes: " + "; ".join(
                f"{c['param_name']} {c['old_value']}→{c['new_value']}" for c in changes
            )

        details = {
            "weekly_outcomes": closed_weekly,
            "intel_outcomes": closed_intel,
            "event_causes_analysed": len(event_causes),
            "changes": changes,
            "elapsed_seconds": elapsed
        }

        # 7. Log the run
        self.db.log_calibration_run({
            "run_at": _now(),
            "total_samples": all_closed,
            "closed_samples": closed_weekly,
            "overall_win_rate": overall_wr,
            "profit_factor": pf,
            "changes_applied": len(changes),
            "summary": summary,
            "details": details
        })

        print(f"[Calibration] ─── {summary} ───")
        return {"success": True, "summary": summary, "details": details}

    # ── API Response Builders ─────────────────────────────────────────────────

    def generate_report(self) -> Dict[str, Any]:
        """Full calibration report for GET /api/calibration/report"""
        stats          = self.db.get_stats_summary()
        factor_weights = self.db.get_all_factor_weights()
        sector_stats   = self.db.get_sector_stats()
        pattern_edge   = self.db.get_pattern_edge()
        history        = self.db.get_calibration_history(limit=5)
        recs           = self.db.get_recommendations_history(limit=10)

        # Group factor weights by factor_type for frontend
        grouped: Dict[str, List] = {}
        for fw in factor_weights:
            ft = fw["factor_type"]
            grouped.setdefault(ft, []).append(fw)

        # Compute overall weekly outcomes snapshot (from weekly DB)
        weekly_outcomes = self.aggregator.read_weekly_outcomes()
        wins   = [o for o in weekly_outcomes if o.get("is_win")]
        losses = [o for o in weekly_outcomes if not o.get("is_win")]
        closed = len(wins) + len(losses)
        overall_wr = round(len(wins) / max(closed, 1) * 100, 1) if closed else 0.0
        gross_gain = sum(abs(float(o.get("max_gain_pct") or 0)) for o in wins)
        gross_loss = sum(abs(float(o.get("max_loss_pct") or 0)) for o in losses)
        pf = round(gross_gain / max(gross_loss, 0.01), 2) if gross_loss > 0 else 0.0

        return {
            "meta": stats,
            "performance": {
                "closed_outcomes": closed,
                "wins": len(wins),
                "losses": len(losses),
                "overall_win_rate_pct": overall_wr,
                "profit_factor": pf
            },
            "factor_weights": grouped,
            "sector_stats": sector_stats,
            "pattern_edge": pattern_edge,
            "recent_runs": history,
            "config_changes": recs,
            "generated_at": _now()
        }


# ── Module singleton ──────────────────────────────────────────────────────────

_engine_instance: Optional[CalibrationEngine] = None
_engine_lock = threading.Lock()


def get_calibration_engine() -> CalibrationEngine:
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = CalibrationEngine()
    return _engine_instance


if __name__ == "__main__":
    print("PSX Calibration Engine — self-test")
    engine = get_calibration_engine()
    print("DB path:", engine.db.db_path)
    stats = engine.db.get_stats_summary()
    print("Stats:", json.dumps(stats, indent=2))
    print("Running calibration cycle...")
    result = engine.run_weekly_calibration()
    print("Result:", json.dumps(result, indent=2))
    print("✅ Self-test complete.")

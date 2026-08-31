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
import threading
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

DB_PATH = Path("cache/intelligence.db")

# Anomaly detection thresholds
THRESH_PRICE_SPIKE_PCT      = 4.5    # price move > 4.5% in one session
THRESH_RVOL                 = 2.8    # volume > 2.8× 20-day average
THRESH_RSI_JUMP             = 8.0    # RSI rises > 8 points in one session
THRESH_UPPER_LOCK_PCT       = 9.5    # price at or above upper circuit limit
THRESH_LOWER_LOCK_PCT       = -9.5   # price at or below lower circuit limit
THRESH_BREAKOUT_LOOKBACK    = 30     # days to look back for resistance

# Pattern library - minimum occurrences to show a pattern
PATTERN_MIN_OCCURRENCES     = 3

# Scheduling intervals (seconds)
ANOMALY_TICK_INTERVAL       = 300    # 5 minutes
EOD_EVAL_HOUR               = 16     # 4 PM PKT (market closes 3:30 PM, buffer)
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

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
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
                        last_updated    TEXT
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

                conn.commit()
            finally:
                conn.close()

    # ── CRUD helpers ──────────────────────────────────────────────────────────

    def insert_event(self, event: Dict[str, Any]) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO stock_events
                    (id, symbol, sector, event_type, detected_at, trade_date,
                     price, price_change_pct, volume, rvol, rsi_at_event,
                     macd_bullish, kse_return_5d, sector_return_5d,
                     snapshot_json, status, created_at)
                    VALUES (:id,:symbol,:sector,:event_type,:detected_at,
                            :trade_date,:price,:price_change_pct,:volume,
                            :rvol,:rsi_at_event,:macd_bullish,
                            :kse_return_5d,:sector_return_5d,
                            :snapshot_json,:status,:created_at)
                """, event)
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
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO detected_patterns
                    (id, name, fingerprint, description, occurrences, win_count,
                     loss_count, neutral_count, avg_3d_return, avg_5d_return,
                     avg_max_upside, avg_max_drawdown, last_updated, created_at)
                    VALUES (:id,:name,:fingerprint,:description,:occurrences,
                            :win_count,:loss_count,:neutral_count,:avg_3d_return,
                            :avg_5d_return,:avg_max_upside,:avg_max_drawdown,
                            :last_updated,:created_at)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        occurrences = :occurrences,
                        win_count = :win_count,
                        loss_count = :loss_count,
                        neutral_count = :neutral_count,
                        avg_3d_return = :avg_3d_return,
                        avg_5d_return = :avg_5d_return,
                        avg_max_upside = :avg_max_upside,
                        avg_max_drawdown = :avg_max_drawdown,
                        last_updated = :last_updated
                """, p)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_pattern error: {e}")
            finally:
                conn.close()

    def insert_prediction(self, pred: Dict[str, Any]):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO ai_predictions
                    (id, symbol, event_id, pattern_id, signal, confidence,
                     price_at_signal, pattern_name, historical_sample,
                     historical_win_rate, avg_expected_return, reasoning_json,
                     predicted_at, outcome)
                    VALUES (:id,:symbol,:event_id,:pattern_id,:signal,:confidence,
                            :price_at_signal,:pattern_name,:historical_sample,
                            :historical_win_rate,:avg_expected_return,
                            :reasoning_json,:predicted_at,:outcome)
                """, pred)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] insert_prediction error: {e}")
            finally:
                conn.close()

    def upsert_stock_memory(self, m: Dict[str, Any]):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO stock_memory
                    (symbol, sector, avg_daily_volume, avg_daily_range_pct,
                     avg_daily_turnover, typical_rsi_min, typical_rsi_max,
                     event_count_90d, last_price, last_rsi, last_rvol, last_updated)
                    VALUES (:symbol,:sector,:avg_daily_volume,:avg_daily_range_pct,
                            :avg_daily_turnover,:typical_rsi_min,:typical_rsi_max,
                            :event_count_90d,:last_price,:last_rsi,:last_rvol,:last_updated)
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
                        last_updated = :last_updated
                """, m)
                conn.commit()
            except Exception as e:
                print(f"[Intelligence] upsert_stock_memory error: {e}")
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
            total = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions"
            ).fetchone()[0]
            evaluated = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE outcome != 'PENDING'"
            ).fetchone()[0]
            correct = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE outcome = 'CORRECT'"
            ).fetchone()[0]
            events_total = conn.execute(
                "SELECT COUNT(*) FROM stock_events"
            ).fetchone()[0]
            patterns_total = conn.execute(
                "SELECT COUNT(*) FROM detected_patterns WHERE occurrences >= ?",
                (PATTERN_MIN_OCCURRENCES,)
            ).fetchone()[0]
            last_tick = conn.execute(
                "SELECT MAX(detected_at) FROM stock_events"
            ).fetchone()[0]
            win_rate = round((correct / max(evaluated, 1)) * 100, 1)
            return {
                "total_predictions": total,
                "evaluated_predictions": evaluated,
                "correct_predictions": correct,
                "win_rate_pct": win_rate,
                "total_events_detected": events_total,
                "patterns_discovered": patterns_total,
                "last_anomaly_tick": last_tick or "Never"
            }
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

    def update_prediction_outcome(self, pred_id: str, outcome: str, actual_return_5d: float):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    UPDATE ai_predictions
                    SET outcome = ?, actual_return_5d = ?, evaluated_at = ?
                    WHERE id = ?
                """, (outcome, actual_return_5d, _now(), pred_id))
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


# ── Utility Functions ──────────────────────────────────────────────────────────

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


# ── Stock Memory Builder ───────────────────────────────────────────────────────

class StockMemoryBuilder:
    """
    Builds and maintains the rolling 20-day baseline profile for each stock.
    Runs overnight to establish "normal" behavior, which the anomaly detector
    then compares against during the trading day.
    """

    def __init__(self, db: IntelligenceDB):
        self.db = db

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

        # Try to get history for volume/range baseline
        history = []
        if history_fn:
            try:
                history = history_fn(symbol) or []
            except Exception:
                pass

        # Calculate baseline from history
        volumes = [c.get("volume", 0) for c in history[-20:] if c.get("volume", 0) > 0]
        closes = [c.get("close", 0) for c in history[-20:] if c.get("close", 0) > 0]
        opens = [c.get("open", c.get("close", 0)) for c in history[-20:]]

        avg_daily_volume = sum(volumes) / max(len(volumes), 1)

        # Daily range percent
        ranges = []
        for c in history[-20:]:
            h = c.get("high", c.get("close", 0))
            l = c.get("low", c.get("close", 0))
            cl = c.get("close", 1)
            if cl > 0:
                ranges.append(((h - l) / cl) * 100)
        avg_daily_range_pct = sum(ranges) / max(len(ranges), 1) if ranges else 1.5

        # RSI range from history
        from psx_indicators import calculate_rsi_series
        if len(closes) >= 15:
            rsi_series = calculate_rsi_series(closes, 14)
            rsi_values = [r for r in rsi_series[-20:] if r > 0]
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

        current_price = stock.get("price", closes[-1] if closes else 0)
        current_volume = stock.get("volume", 0)
        avg_turnover = avg_daily_volume * (current_price if current_price > 0 else 1)

        # Current RVOL
        rvol = round(current_volume / max(avg_daily_volume, 1), 2) if avg_daily_volume > 0 else 1.0

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
            "last_updated": _now()
        })


# ── Anomaly Detector ──────────────────────────────────────────────────────────

class AnomalyDetector:
    """
    Runs every 5 minutes (during market hours) across all PSX stocks.
    Compares each stock's current state to its baseline in stock_memory.
    Creates stock_events when anomalies are detected.
    """

    def __init__(self, db: IntelligenceDB, investigator: 'CauseInvestigator',
                 prediction_engine: 'PredictionEngine'):
        self.db = db
        self.investigator = investigator
        self.prediction_engine = prediction_engine
        self._seen_today: set = set()  # prevent duplicate events per session

    def reset_daily_seen(self):
        """Reset seen-today set at start of each trading day."""
        self._seen_today = set()

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

        events_created = 0
        for stock in stocks:
            try:
                ev = self._check_stock(stock, kse_change, sector_avg, history_fn)
                if ev:
                    events_created += 1
            except Exception as e:
                sym = stock.get("symbol", "?")
                print(f"[Intelligence] AnomalyDetector error for {sym}: {e}")

        if events_created:
            print(f"[Intelligence] Tick: {events_created} new events detected.")

    def _check_stock(self, stock: Dict[str, Any], kse_change: float,
                     sector_avg: Dict[str, float], history_fn=None) -> Optional[str]:
        symbol = stock.get("symbol", "").upper()
        if not symbol:
            return None

        price = float(stock.get("price", 0) or 0)
        change_pct = float(stock.get("change", 0) or 0)
        volume = float(stock.get("volume", 0) or 0)
        sector = stock.get("sector", "Other")

        if price <= 0:
            return None

        # Load baseline memory
        memory = self.db.get_stock_memory(symbol)
        avg_vol = memory["avg_daily_volume"] if memory else max(volume * 0.5, 1)
        rvol = round(volume / max(avg_vol, 1), 2)

        # Determine event type(s)
        event_type = None
        if change_pct >= THRESH_UPPER_LOCK_PCT:
            event_type = EVENT_UPPER_LOCK
        elif change_pct <= THRESH_LOWER_LOCK_PCT:
            event_type = EVENT_LOWER_LOCK
        elif rvol >= THRESH_RVOL and change_pct >= 2.0:
            event_type = EVENT_VOLUME_SURGE
        elif change_pct >= THRESH_PRICE_SPIKE_PCT:
            event_type = EVENT_PRICE_SPIKE
        elif rvol >= THRESH_RVOL and abs(change_pct) < 2.0:
            event_type = EVENT_ACCUMULATION

        # Resistance break check (requires history)
        if event_type is None and history_fn and change_pct >= 2.0:
            try:
                history = history_fn(symbol) or []
                if len(history) >= THRESH_BREAKOUT_LOOKBACK:
                    lookback = history[-THRESH_BREAKOUT_LOOKBACK:]
                    resistance = max(c.get("high", c.get("close", 0)) for c in lookback[:-3])
                    if price > resistance * 1.005:
                        event_type = EVENT_RESISTANCE_BREAK
            except Exception:
                pass

        if event_type is None:
            return None

        # Prevent duplicate events for same symbol+type on same day
        dedup_key = f"{symbol}_{event_type}_{_today()}"
        if dedup_key in self._seen_today:
            return None
        self._seen_today.add(dedup_key)

        # Calculate technical indicators
        rsi_val = 50.0
        macd_bullish = False
        if history_fn:
            try:
                history = history_fn(symbol) or []
                if len(history) >= 15:
                    from psx_indicators import calculate_rsi_series, calculate_macd
                    closes = [c["close"] for c in history]
                    rsi_series = calculate_rsi_series(closes, 14)
                    rsi_val = round(rsi_series[-1], 1) if rsi_series else 50.0
                    macd_res = calculate_macd(closes)
                    macd_bullish = bool(macd_res.get("is_bullish", False))
            except Exception:
                pass

        sector_return = sector_avg.get(sector, 0.0)

        # Build snapshot
        snapshot = {
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "rvol": rvol,
            "rsi": rsi_val,
            "macd_bullish": macd_bullish,
            "kse_change": kse_change,
            "sector_return": sector_return,
            "avg_vol_baseline": avg_vol
        }

        event_id = _make_id(symbol, event_type)
        event = {
            "id": event_id,
            "symbol": symbol,
            "sector": sector,
            "event_type": event_type,
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
            "created_at": _now()
        }

        if self.db.insert_event(event):
            # Trigger investigation
            causes = self.investigator.investigate(event, history_fn=history_fn,
                                                   sector_avg=sector_avg, kse_change=kse_change)
            if causes:
                self.db.insert_causes(causes)
            # Generate prediction
            self.prediction_engine.generate_prediction(event, causes)

        return event_id


# ── Cause Investigator ─────────────────────────────────────────────────────────

class CauseInvestigator:
    """
    For every detected event, investigates WHY it happened.
    Produces a list of causal factors, each with an evidence level and confidence score.
    All analysis is deterministic — no LLMs.
    """

    def __init__(self, db: IntelligenceDB):
        self.db = db

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
        if history_fn:
            try:
                history = history_fn(symbol) or []
                closes = [c["close"] for c in history]
                volumes = [c.get("volume", 0) for c in history]
            except Exception:
                pass

        # ── 1. Technical Breakout ─────────────────────────────────────────────
        if len(closes) >= 30 and event["price"] > 0:
            lookback_highs = [history[i].get("high", closes[i])
                              for i in range(len(history) - 30, len(history) - 3)]
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
            if accum_ratio >= 1.5:
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
        rsi = event.get("rsi_at_event", 50.0)
        if len(closes) >= 16:
            from psx_indicators import calculate_rsi_series
            rsi_series = calculate_rsi_series(closes, 14)
            prev_rsi = rsi_series[-2] if len(rsi_series) >= 2 else rsi
            rsi_jump = rsi - prev_rsi
            if rsi >= 60 and rsi_jump >= 3:
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
        if event.get("macd_bullish") and len(closes) >= 35:
            from psx_indicators import calculate_macd
            macd_res = calculate_macd(closes)
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
        if abs(sector_return) >= 1.5:
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
        if abs(kse) >= 1.0:
            confidence = min(55, int(20 + abs(kse) * 5))
            causes.append({
                "event_id": event_id,
                "factor": CAUSE_MARKET_MOMENTUM,
                "evidence": "Weak" if confidence < 40 else "Moderate",
                "confidence": confidence,
                "detail": f"KSE-100 {kse:+.1f}% today — broad market momentum contributing",
                "created_at": now
            })

        # ── 7. Upper Lock Setup ───────────────────────────────────────────────
        if event["event_type"] == EVENT_UPPER_LOCK:
            rvol = event.get("rvol", 1.0)
            confidence = min(95, int(60 + rvol * 5))
            causes.append({
                "event_id": event_id,
                "factor": CAUSE_UPPER_LOCK_SETUP,
                "evidence": "Very High" if rvol >= 5 else "Strong",
                "confidence": confidence,
                "detail": f"Stock hit upper circuit limit (+10%) with {rvol:.1f}× average volume — strong demand exceeded available supply",
                "created_at": now
            })

        # Sort by confidence descending
        causes.sort(key=lambda x: x["confidence"], reverse=True)
        return causes

    def build_narrative(self, causes: List[Dict]) -> str:
        """Convert causes list into a human-readable narrative."""
        if not causes:
            return "Insufficient data to determine cause."

        top = [c for c in causes if c["confidence"] >= 50]
        if not top:
            top = causes[:2]

        factor_names = {
            CAUSE_TECH_BREAKOUT: "technical breakout",
            CAUSE_VOLUME_ACCUM: "abnormal volume accumulation",
            CAUSE_RSI_MOMENTUM: "RSI momentum surge",
            CAUSE_MACD_CONFIRM: "MACD bullish confirmation",
            CAUSE_SECTOR_MOMENTUM: "sector-wide momentum",
            CAUSE_MARKET_MOMENTUM: "broad market movement",
            CAUSE_CORP_ANNOUNCEMENT: "corporate announcement",
            CAUSE_UPPER_LOCK_SETUP: "extreme demand (upper lock)"
        }

        names = [factor_names.get(c["factor"], c["factor"].replace("_", " ").lower())
                 for c in top[:3]]
        if len(names) == 1:
            return f"Most likely driven by {names[0]}."
        elif len(names) == 2:
            return f"Most likely driven by {names[0]} + {names[1]}."
        else:
            return f"Most likely combination: {names[0]} + {names[1]} + {names[2]}."


# ── Pattern Library ────────────────────────────────────────────────────────────

class PatternLibrary:
    """
    Automatically discovers and names recurring patterns from historical events.
    Rebuilt nightly by analyzing all stored events and their causes.
    """

    PATTERN_DEFINITIONS = [
        {
            "id": "P001",
            "name": "Breakout + Volume Surge",
            "fingerprint": "TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION",
            "description": "Stock breaks key resistance on elevated volume — classic institutional breakout setup"
        },
        {
            "id": "P002",
            "name": "3-Day Accumulation Breakout",
            "fingerprint": "VOLUME_ACCUMULATION|TECHNICAL_BREAKOUT|RSI_MOMENTUM",
            "description": "Multi-day volume accumulation followed by resistance breakout with RSI momentum"
        },
        {
            "id": "P003",
            "name": "Upper Lock with Accumulation",
            "fingerprint": "UPPER_LOCK_SETUP|VOLUME_ACCUMULATION",
            "description": "Stock hits upper circuit with prior accumulation — extreme demand setup"
        },
        {
            "id": "P004",
            "name": "Sector-Led Breakout",
            "fingerprint": "SECTOR_MOMENTUM|TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION",
            "description": "Breakout coincides with strong sector-wide momentum — likely institutional rotation"
        },
        {
            "id": "P005",
            "name": "MACD + Volume Surge",
            "fingerprint": "MACD_CONFIRMATION|VOLUME_ACCUMULATION",
            "description": "MACD bullish crossover confirmed by volume surge"
        },
        {
            "id": "P006",
            "name": "RSI Momentum + Breakout",
            "fingerprint": "RSI_MOMENTUM|TECHNICAL_BREAKOUT",
            "description": "RSI momentum acceleration coincides with price breakout"
        },
        {
            "id": "P007",
            "name": "Full Confluence Setup",
            "fingerprint": "TECHNICAL_BREAKOUT|VOLUME_ACCUMULATION|RSI_MOMENTUM|MACD_CONFIRMATION",
            "description": "All technical factors aligned — highest probability continuation setup"
        },
        {
            "id": "P008",
            "name": "Volume Surge Only",
            "fingerprint": "VOLUME_ACCUMULATION",
            "description": "Significant volume surge without clear price catalyst — possible early accumulation phase"
        }
    ]

    def __init__(self, db: IntelligenceDB):
        self.db = db

    def rebuild(self):
        """Rebuild pattern statistics from all stored events and their causes."""
        print("[Intelligence] PatternLibrary: rebuilding pattern statistics...")
        now = _now()

        for pat_def in self.PATTERN_DEFINITIONS:
            fingerprint = pat_def["fingerprint"]
            required_factors = set(fingerprint.split("|"))

            # Find all events that had ALL required factors with confidence >= 50
            matching_events = self._find_matching_events(required_factors)

            occurrences = len(matching_events)
            win_count = 0
            loss_count = 0
            neutral_count = 0
            returns_3d = []
            returns_5d = []
            upsides = []
            drawdowns = []

            for ev in matching_events:
                # Check pattern_occurrences for outcomes
                conn = self.db._connect()
                try:
                    occ = conn.execute("""
                        SELECT * FROM pattern_occurrences
                        WHERE event_id = ? AND outcome != 'PENDING'
                        LIMIT 1
                    """, (ev["id"],)).fetchone()
                    if occ:
                        occ = dict(occ)
                        if occ["outcome"] == "WIN":
                            win_count += 1
                        elif occ["outcome"] == "LOSS":
                            loss_count += 1
                        else:
                            neutral_count += 1
                        if occ.get("return_3d") is not None:
                            returns_3d.append(occ["return_3d"])
                        if occ.get("return_5d") is not None:
                            returns_5d.append(occ["return_5d"])
                finally:
                    conn.close()

            avg_3d = sum(returns_3d) / max(len(returns_3d), 1) if returns_3d else 0.0
            avg_5d = sum(returns_5d) / max(len(returns_5d), 1) if returns_5d else 0.0

            self.db.upsert_pattern({
                "id": pat_def["id"],
                "name": pat_def["name"],
                "fingerprint": fingerprint,
                "description": pat_def["description"],
                "occurrences": occurrences,
                "win_count": win_count,
                "loss_count": loss_count,
                "neutral_count": neutral_count,
                "avg_3d_return": round(avg_3d, 2),
                "avg_5d_return": round(avg_5d, 2),
                "avg_max_upside": 0.0,
                "avg_max_drawdown": 0.0,
                "last_updated": now,
                "created_at": now
            })

        print("[Intelligence] PatternLibrary: rebuild complete.")

    def _find_matching_events(self, required_factors: set) -> List[Dict]:
        """Find all events where ALL required factors were detected with confidence >= 50."""
        conn = self.db._connect()
        try:
            # Get events with their confirmed factors
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
        """Find the best matching pattern definition for this set of causes."""
        if not event_causes:
            return None

        confirmed_factors = {c["factor"] for c in event_causes if c["confidence"] >= 50}
        best_match = None
        best_coverage = 0

        for pat_def in self.PATTERN_DEFINITIONS:
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
                 investigator: CauseInvestigator):
        self.db = db
        self.pattern_lib = pattern_lib
        self.investigator = investigator

    def generate_prediction(self, event: Dict[str, Any], causes: List[Dict[str, Any]]):
        symbol = event["symbol"]
        event_type = event["event_type"]
        price = event["price"]
        rvol = event.get("rvol", 1.0)
        rsi = event.get("rsi_at_event", 50.0)

        # Match to pattern
        matched_pattern = self.pattern_lib.match_event_to_pattern(causes)

        # Determine signal
        top_confidence = causes[0]["confidence"] if causes else 0
        signal = self._determine_signal(event_type, rvol, rsi, top_confidence)

        # Base confidence on top causal evidence
        avg_cause_confidence = (sum(c["confidence"] for c in causes[:3]) /
                                max(len(causes[:3]), 1)) if causes else 45

        # Adjust for pattern history
        historical_sample = 0
        historical_win_rate = 0.0
        avg_expected_return = 0.0
        pattern_name = "No Pattern Matched"
        pattern_id = None

        if matched_pattern:
            pattern_id = matched_pattern.get("id")
            pattern_name = matched_pattern.get("name", "Unknown Pattern")
            historical_sample = matched_pattern.get("occurrences", 0)
            total_closed = (matched_pattern.get("win_count", 0) +
                            matched_pattern.get("loss_count", 0))
            if total_closed > 0:
                historical_win_rate = round(
                    matched_pattern["win_count"] / total_closed * 100, 1
                )
            avg_expected_return = matched_pattern.get("avg_5d_return", 0.0)

        # Final prediction confidence
        pred_confidence = int(avg_cause_confidence * 0.7 + min(historical_win_rate, 80) * 0.3)
        pred_confidence = max(30, min(95, pred_confidence))

        # Downscale if negative historical win rate
        if historical_win_rate > 0 and historical_win_rate < 45:
            pred_confidence = int(pred_confidence * 0.75)

        # ── Apply calibration learnings to confidence ─────────────────────────
        calibration_applied = False
        calibration_note = ""
        try:
            cal_weights = self._load_calibration_weights()
            if cal_weights:
                sector = event.get("sector", "")
                # 1. Sector weight adjustment (only if sample_count >= 5)
                sector_w = cal_weights["sector_intel"].get(sector)
                if sector_w and sector_w["n"] >= 5:
                    # Cap adjustment to ±15 points
                    raw_adj = (sector_w["w"] - 1.0) * 25  # 1.2w → +5pts, 0.7w → -7.5pts
                    sector_adj = max(-15, min(15, raw_adj))
                    pred_confidence = int(pred_confidence + sector_adj)
                    calibration_note += f"sector={sector}({sector_w['w']:.2f}x) "
                    calibration_applied = True

                # 2. Causal factor weight adjustment
                causal_adj_total = 0.0
                causal_adj_count = 0
                for cause in causes[:3]:
                    f = cause["factor"]
                    c = cause.get("confidence", 0)
                    # Map evidence level to key suffix
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

        # Build reasoning
        cause_narrative = self.investigator.build_narrative(causes)
        reasoning = {
            "event_type": event_type,
            "top_causes": [{"factor": c["factor"], "confidence": c["confidence"],
                            "evidence": c["evidence"]} for c in causes[:4]],
            "narrative": cause_narrative,
            "pattern_matched": pattern_name,
            "historical_win_rate": historical_win_rate,
            "historical_sample": historical_sample,
            "calibration_applied": calibration_applied,
            "calibration_note": calibration_note.strip() if calibration_note else None
        }


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
            "outcome": "PENDING"
        }

        self.db.insert_prediction(pred)

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
    Evaluates prediction outcomes at end of day.
    Updates pattern win/loss counts to self-calibrate confidence.
    """

    def __init__(self, db: IntelligenceDB):
        self.db = db

    def evaluate_day_outcomes(self, stocks: List[Dict[str, Any]]):
        """Run at 4:30 PM — check all predictions that are 5+ days old."""
        price_map = {s.get("symbol", "").upper(): float(s.get("price", 0) or 0)
                     for s in stocks}

        pending = self.db.get_pending_predictions_for_evaluation(days_old=5)
        evaluated = 0

        for pred in pending:
            symbol = pred["symbol"]
            current_price = price_map.get(symbol, 0)
            entry_price = pred.get("price_at_signal", 0)

            if current_price <= 0 or entry_price <= 0:
                continue

            actual_return_5d = round(((current_price - entry_price) / entry_price) * 100, 2)

            # Determine outcome
            signal = pred["signal"]
            if signal in [SIGNAL_BREAKOUT_IMMINENT, SIGNAL_CONTINUATION, SIGNAL_WATCH]:
                outcome = "CORRECT" if actual_return_5d >= 2.0 else "INCORRECT"
            elif signal == SIGNAL_REVERSAL_RISK:
                outcome = "CORRECT" if actual_return_5d <= -2.0 else "INCORRECT"
            elif signal == SIGNAL_EXTENDED:
                outcome = "CORRECT" if actual_return_5d < 0 else "INCORRECT"
            else:
                outcome = "NEUTRAL"

            self.db.update_prediction_outcome(pred["id"], outcome, actual_return_5d)
            evaluated += 1

        if evaluated:
            print(f"[Intelligence] LearningEngine: evaluated {evaluated} predictions.")


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


# ── Intelligence Engine (Orchestrator) ────────────────────────────────────────

class IntelligenceEngine:
    """
    Top-level orchestrator. Wires together all components.
    Called by server.py scheduler thread.
    """

    def __init__(self):
        self.db = IntelligenceDB()
        self.investigator = CauseInvestigator(self.db)
        self.pattern_lib = PatternLibrary(self.db)
        self.prediction_engine = PredictionEngine(self.db, self.pattern_lib, self.investigator)
        self.anomaly_detector = AnomalyDetector(self.db, self.investigator, self.prediction_engine)
        self.learning_engine = LearningEngine(self.db)
        self.memory_builder = StockMemoryBuilder(self.db)
        self.noticeboard = NoticeBoardScraper(self.db)
        self._last_eod_date: str = ""
        self._last_overnight_date: str = ""
        print("[Intelligence] Engine initialized. DB:", str(self.db.db_path))

    def tick(self, stocks: List[Dict[str, Any]], index_data: Dict[str, Any] = None,
             history_fn=None):
        """Main 5-minute tick — called by server.py scheduler."""
        try:
            self.anomaly_detector.tick(stocks, index_data, history_fn)
        except Exception as e:
            print(f"[Intelligence] tick error: {e}")

    def end_of_day(self, stocks: List[Dict[str, Any]]):
        """4:30 PM evaluation — called by server.py scheduler."""
        today = _today()
        if self._last_eod_date == today:
            return
        try:
            self.learning_engine.evaluate_day_outcomes(stocks)
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
            self.pattern_lib.rebuild()
            self.anomaly_detector.reset_daily_seen()
            self._last_overnight_date = today
        except Exception as e:
            print(f"[Intelligence] Overnight rebuild error: {e}")

    # ── API Response Builders ─────────────────────────────────────────────────

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Used by GET /api/intelligence/summary"""
        stats = self.db.get_learning_stats()
        recent_events = self.db.get_recent_events(limit=5)
        active_predictions = self.db.get_active_predictions(limit=5)

        return {
            "stats": stats,
            "recent_events_count": len(recent_events),
            "active_predictions_count": len(active_predictions),
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
                "status": ev["status"]
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
            total_closed = (p.get("win_count", 0) + p.get("loss_count", 0))
            win_rate = round(p["win_count"] / max(total_closed, 1) * 100, 1) if total_closed else 0
            result.append({
                **p,
                "win_rate_pct": win_rate,
                "pending_count": p.get("occurrences", 0) - total_closed
            })
        return result

    def get_predictions_data(self, limit: int = 20) -> List[Dict]:
        """Used by GET /api/intelligence/predictions"""
        preds = self.db.get_active_predictions(limit=limit)
        result = []
        for p in preds:
            try:
                reasoning = json.loads(p.get("reasoning_json") or "{}")
            except Exception:
                reasoning = {}
            result.append({
                **{k: v for k, v in p.items() if k != "reasoning_json"},
                "reasoning": reasoning
            })
        return result


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

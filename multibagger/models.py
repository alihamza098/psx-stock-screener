#!/usr/bin/env python3
"""
Multibagger Pattern Finder — Data Models & SQLite Persistence
==============================================================
Pure SQLite storage for DPS announcements, quarterly financials,
float profiles, historical backtested reference table, and daily candidate rankings.

Pure Python stdlib — zero external dependencies.
"""

import sqlite3
import json
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

DB_PATH = Path("cache/multibagger.db")
_SCHEMA_DONE = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_multibaggers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    run_start_date    TEXT NOT NULL,
    run_peak_date     TEXT NOT NULL,
    start_price       REAL NOT NULL,
    peak_price        REAL NOT NULL,
    multiple_achieved REAL NOT NULL,
    trigger_tags      TEXT NOT NULL,      -- JSON list: ["NAME_OR_BUSINESS_CHANGE", "CAPITAL_INCREASE", ...]
    float_at_breakout INTEGER,
    sector            TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE(ticker, run_start_date)
);

CREATE TABLE IF NOT EXISTS dps_announcements_cache (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,          -- YYYY-MM-DD or MMM DD, YYYY
    title         TEXT NOT NULL,
    link          TEXT,
    triggers_json TEXT,                   -- JSON list of detected triggers
    scraped_at    TEXT NOT NULL,
    UNIQUE(symbol, date, title)
);

CREATE TABLE IF NOT EXISTS dps_financials_cache (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    period        TEXT NOT NULL,          -- e.g. "Q3 2026", "2025"
    sales         REAL,                   -- in thousands PKR or as reported
    eps           REAL,
    is_quarterly  INTEGER DEFAULT 1,
    scraped_at    TEXT NOT NULL,
    UNIQUE(symbol, period)
);

CREATE TABLE IF NOT EXISTS company_profile_cache (
    symbol            TEXT PRIMARY KEY,
    shares            INTEGER,
    free_float_shares INTEGER,
    free_float_pct    REAL,
    sector            TEXT,
    scraped_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_multibagger_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date     TEXT NOT NULL,          -- YYYY-MM-DD
    ticker        TEXT NOT NULL,
    score         INTEGER NOT NULL,
    price         REAL NOT NULL,
    reasons_json  TEXT NOT NULL,          -- JSON list
    sector        TEXT,
    float_shares  INTEGER,
    flags_json    TEXT NOT NULL,          -- JSON list
    scanned_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_cache (
    query_key         TEXT PRIMARY KEY,   -- TICKER or hash(query)
    ticker            TEXT NOT NULL,
    report_json       TEXT NOT NULL,      -- synthesized report object
    source_links_json TEXT NOT NULL,      -- list of source dicts
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hist_ticker ON historical_multibaggers(ticker);
CREATE INDEX IF NOT EXISTS idx_ann_sym ON dps_announcements_cache(symbol);
CREATE INDEX IF NOT EXISTS idx_fin_sym ON dps_financials_cache(symbol);
CREATE INDEX IF NOT EXISTS idx_cand_date ON daily_multibagger_candidates(scan_date);
"""


def get_conn() -> sqlite3.Connection:
    """Get SQLite connection with automatic schema creation."""
    global _SCHEMA_DONE
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    if not _SCHEMA_DONE:
        try:
            conn.executescript(SCHEMA)
            conn.commit()
            _SCHEMA_DONE = True
        except Exception as e:
            print(f"[MultibaggerDB] Schema error: {e}")
    return conn


def save_announcements(symbol: str, announcements: List[Dict[str, Any]]) -> int:
    """Cache announcements for a symbol. Returns count of newly inserted rows."""
    if not announcements:
        return 0
    symbol = symbol.upper()
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    with get_conn() as conn:
        for ann in announcements:
            date_str = ann.get("date", "").strip()
            title = ann.get("title", "").strip()
            link = ann.get("link", "").strip()
            triggers = json.dumps(ann.get("triggers", []))
            try:
                conn.execute("""
                    INSERT INTO dps_announcements_cache (symbol, date, title, link, triggers_json, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, date, title) DO UPDATE SET
                        link = excluded.link,
                        triggers_json = excluded.triggers_json,
                        scraped_at = excluded.scraped_at
                """, (symbol, date_str, title, link, triggers, now_str))
                inserted += 1
            except Exception:
                pass
        conn.commit()
    return inserted


def get_cached_announcements(symbol: str) -> List[Dict[str, Any]]:
    """Retrieve cached announcements for a symbol."""
    symbol = symbol.upper()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date, title, link, triggers_json, scraped_at
            FROM dps_announcements_cache
            WHERE symbol = ?
            ORDER BY id DESC
        """, (symbol,)).fetchall()
        result = []
        for r in rows:
            triggers = []
            try:
                triggers = json.loads(r["triggers_json"] or "[]")
            except Exception:
                pass
            result.append({
                "date": r["date"],
                "title": r["title"],
                "link": r["link"],
                "triggers": triggers,
                "scraped_at": r["scraped_at"]
            })
        return result


def save_company_profile(symbol: str, profile: Dict[str, Any]) -> None:
    """Save shares, float, and sector info."""
    symbol = symbol.upper()
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO company_profile_cache (symbol, shares, free_float_shares, free_float_pct, sector, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                shares = excluded.shares,
                free_float_shares = excluded.free_float_shares,
                free_float_pct = excluded.free_float_pct,
                sector = excluded.sector,
                scraped_at = excluded.scraped_at
        """, (
            symbol,
            profile.get("shares"),
            profile.get("free_float_shares"),
            profile.get("free_float_pct"),
            profile.get("sector"),
            now_str
        ))
        conn.commit()


def get_company_profile(symbol: str) -> Optional[Dict[str, Any]]:
    """Get cached company profile."""
    symbol = symbol.upper()
    with get_conn() as conn:
        row = conn.execute("""
            SELECT symbol, shares, free_float_shares, free_float_pct, sector, scraped_at
            FROM company_profile_cache WHERE symbol = ?
        """, (symbol,)).fetchone()
        if row:
            return dict(row)
        return None


def save_financials(symbol: str, financials: List[Dict[str, Any]]) -> int:
    """Save parsed quarterly/annual financials."""
    symbol = symbol.upper()
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    with get_conn() as conn:
        for f in financials:
            period = f.get("period", "").strip()
            sales = f.get("sales")
            eps = f.get("eps")
            is_q = 1 if f.get("is_quarterly", True) else 0
            try:
                conn.execute("""
                    INSERT INTO dps_financials_cache (symbol, period, sales, eps, is_quarterly, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, period) DO UPDATE SET
                        sales = excluded.sales,
                        eps = excluded.eps,
                        is_quarterly = excluded.is_quarterly,
                        scraped_at = excluded.scraped_at
                """, (symbol, period, sales, eps, is_q, now_str))
                inserted += 1
            except Exception:
                pass
        conn.commit()
    return inserted


def get_cached_financials(symbol: str, quarterly_only: bool = True) -> List[Dict[str, Any]]:
    """Get cached financials for a symbol ordered chronologically/by id."""
    symbol = symbol.upper()
    with get_conn() as conn:
        q = "SELECT period, sales, eps, is_quarterly, scraped_at FROM dps_financials_cache WHERE symbol = ?"
        params = [symbol]
        if quarterly_only:
            q += " AND is_quarterly = 1"
        q += " ORDER BY id DESC"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

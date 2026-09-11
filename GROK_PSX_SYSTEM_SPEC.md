# 🚀 PSX Intelligent Stock Screener & AI Engine — Master Specification for Grok

> **Usage Instructions for Grok**:  
> Feed this document to Grok with the following instruction:  
> *"You are an expert quantitative financial software engineer. Use the complete architecture, algorithms, database schemas, scoring formulas, and Python source code in this document to implement or replicate the Pakistan Stock Exchange (PSX) Screener, Intraday AI Alert Engine, Self-Learning Calibration Engine, and Undervalued Stock Valuation System exactly as specified."*

---

## 📑 Table of Contents
1. [System Architecture & Overview](#1-system-architecture--overview)
2. [PSX Market Microstructure Rules](#2-psx-market-microstructure-rules)
3. [Intraday Trade Alert Engine (`psx_intraday_engine.py`)](#3-intraday-trade-alert-engine-psx_intraday_enginepy)
4. [Intraday Self-Learning Engine (`psx_intraday_learner.py`)](#4-intraday-self-learning-engine-psx_intraday_learnerpy)
5. [PSX Undervalued Stock Analyzer (`psx_undervalued_engine.py`)](#5-psx-undervalued-stock-analyzer-psx_undervalued_enginepy)
6. [Telegram Bot & Alert Delivery (`psx_telegram_bot.py`)](#6-telegram-bot--alert-delivery-psx_telegram_botpy)
7. [Database Schemas & Data Models](#7-database-schemas--data-models)
8. [Deployment & Environment Configuration](#8-deployment--environment-configuration)

---

## 1. System Architecture & Overview

The PSX Intelligent Stock Screener is a full-stack automated financial intelligence and trade alerting platform for the Pakistan Stock Exchange (PSX).

```
┌────────────────────────────────────────────────────────────────────────┐
│                        PSX Live Market Scraper                         │
│             (Scrapes 742+ listed stocks every 5 min from PSX)          │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
       ┌───────────────────────────┼───────────────────────────┐
       ▼                           ▼                           ▼
┌──────────────┐          ┌─────────────────┐         ┌─────────────────┐
│ Intraday AI  │          │  Self-Learning  │         │   Undervalued   │
│ Alert Engine │◄────────►│   Calibration   │         │ Stock Analyzer  │
│  (5-min tick)│          │  (Bayesian DB)  │         │ (DDM, DCF, PE)  │
└──────┬───────┘          └─────────────────┘         └────────┬────────┘
       │                                                       │
       ▼                                                       ▼
┌──────────────┐                                      ┌─────────────────┐
│ Telegram Bot │                                      │ Web Dashboard   │
│ Instant/Sched│                                      │ PWA / UI View   │
└──────────────┘                                      └─────────────────┘
```

- **Backend**: Python 3.10+ Standard Library HTTP Server (`server.py`), 0 heavy dependencies (`sqlite3`, `urllib`, `threading`, `json`).
- **Database**: Pure SQLite databases stored in `cache/`:
  - `cache/weekly_scan.db`: Long-term value setups, audits, and grades (A+, A, B).
  - `cache/intelligence.db`: Causal event tracking, patterns (P001, P003), stock price memory (742 stocks).
  - `cache/calibration.db`: Bayesian win-rate calibration, 55 factor weights, sector win rates.
  - `cache/intraday_learning.db`: Daily intraday trade outcomes, daily EOD evaluation, sector edge weights.
  - `cache/undervalued.db`: Relative and absolute intrinsic valuations (DDM, DCF, Graham Number).

---

## 2. PSX Market Microstructure Rules

To avoid false signals, every trade engine must respect these PSX rules:

1. **Daily Circuit Breakers**:
   - Maximum daily move is capped at **$\pm 10.0\%$** from previous day's close (or $\pm 7.5\%$ for smaller caps).
   - **Never buy near the upper circuit**: Any stock with `change > 7.0%` is disqualified from intraday breakouts. Chasing above +7% carries severe downside dump risk and leaves zero upside headroom.
   - **Circuit-Aware Target**: Target price must never exceed `prev_close * 1.090` (leaving a 1% safety buffer before the circuit freeze).

2. **Institutional Liquidity Floor**:
   - Traded Value $\ge$ **PKR 10,000,000** (10M PKR minimum).
   - Share Volume $\ge$ **100,000 shares** (or **25,000 shares** for high-priced stocks $\ge$ PKR 150).
   - Price $\ge$ **PKR 5.0** (penny stocks excluded).

3. **Session Timing (PKT - Pakistan Standard Time, UTC+5)**:
   - Market Trading Hours: **09:15 AM to 03:30 PM PKT** (375 minutes).
   - Friday Prayer Break: **12:00 PM to 02:32 PM PKT** (alerts suspended).
   - Morning Brief: **09:15 AM PKT**.
   - Morning Pick: **10:30 AM PKT**.
   - Afternoon Pick: **01:00 PM PKT**.
   - EOD Outcome Evaluation & Market Wrap: **03:30 PM PKT**.

4. **Dynamic Risk Buffers**:
   - Minimum stop buffer: **2.5%** below entry.
   - Maximum stop buffer: **3.8%** below entry.
   - Reward-to-Risk ratio: $\ge 1.1:1$ to $1.4:1$.

---

## 3. Intraday Trade Alert Engine (`psx_intraday_engine.py`)

This engine runs every 5 minutes during market hours. It filters illiquid traps, computes time-of-day volume pace, enforces circuit boundaries, and outputs actionable trade setups.

```python
#!/usr/bin/env python3
"""
PSX Intraday Trade Alert Engine
=================================
Scans live PSX stocks every 5 minutes during market hours and identifies
short-term trade setups with automatic target/close monitoring.
"""

import json
import time
import datetime
import threading
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_INSTANT_PER_DAY   = 2        # Instant high-conviction quota
MAX_SCHEDULED_PER_DAY = 2        # Scheduled quota: 10:30 AM + 1:00 PM
INSTANT_SCORE_THRESHOLD  = 70    # Score to fire instantly
SCHEDULED_SCORE_MIN      = 55    # Min score for scheduled picks

# Institutional liquidity floors
MIN_LIQUIDITY_PKR        = 10_000_000  # PKR 10M minimum traded value today
MIN_VOLUME_NORMAL        = 100_000     # 100k shares min for stocks under PKR 150
MIN_VOLUME_HIGH_PRICE    = 25_000      # 25k shares min for stocks >= PKR 150
MIN_PRICE_PKR            = 5.0         # Exclude penny junk stocks

# Momentum bounds — sweet spot vs overextended trap
MIN_ALLOWED_CHANGE       = 1.0         # Positive upward momentum
MAX_ALLOWED_CHANGE       = 7.0         # PSX daily limit is +10%; never buy above +7%

# Alert window (PKT)
ALERT_START_HOUR   = 9
ALERT_START_MIN    = 45
ALERT_END_HOUR     = 15
ALERT_END_MIN      = 0

# Scheduled times (PKT)
MORNING_HOUR,   MORNING_MIN   = 10, 30
AFTERNOON_HOUR, AFTERNOON_MIN = 13, 0

# Friday prayer break
FRIDAY_BREAK_START = (12, 0)
FRIDAY_BREAK_END   = (14, 32)

_state_lock = threading.Lock()

_daily: Dict[str, Any] = {
    "date":               "",
    "instant_sent":       0,
    "instant_symbols":    [],
    "morning_sent":       False,
    "afternoon_sent":     False,
    "scheduled_symbols":  [],
    "open_positions":     {},
    "candidates":         [],
}

_memory_cache: Dict[str, Dict] = {}
_memory_cache_time: float = 0.0

_sector_weights_cache: Dict[str, float] = {}
_sector_weights_cache_time: float = 0.0


def _pkt_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)


def _today_str() -> str:
    return _pkt_now().strftime("%Y-%m-%d")


def _reset_if_new_day() -> None:
    today = _today_str()
    with _state_lock:
        if _daily["date"] != today:
            _daily.update({
                "date":               today,
                "instant_sent":       0,
                "instant_symbols":    [],
                "morning_sent":       False,
                "afternoon_sent":     False,
                "scheduled_symbols":  [],
                "open_positions":     {},
                "candidates":         [],
            })


def _is_market_window() -> bool:
    now = _pkt_now()
    wd  = now.weekday()
    if wd >= 5:
        return False
    total = now.hour * 60 + now.minute
    start = ALERT_START_HOUR * 60 + ALERT_START_MIN
    end   = ALERT_END_HOUR   * 60 + ALERT_END_MIN
    if not (start <= total < end):
        return False
    if wd == 4:
        bs = FRIDAY_BREAK_START[0] * 60 + FRIDAY_BREAK_START[1]
        be = FRIDAY_BREAK_END[0]   * 60 + FRIDAY_BREAK_END[1]
        if bs <= total < be:
            return False
    return True


def _get_time_of_day_fraction() -> float:
    """PSX market session: 09:15 to 15:30 PKT (375 minutes)."""
    now = _pkt_now()
    total_mins = now.hour * 60 + now.minute
    open_mins  = 9 * 60 + 15
    close_mins = 15 * 60 + 30
    if total_mins <= open_mins:
        return 0.15
    if total_mins >= close_mins:
        return 1.0
    elapsed = total_mins - open_mins
    return max(0.15, min(1.0, elapsed / 375.0))


def _get_stock_memory(sym: str, memory_db_fn=None) -> Dict:
    """Load baseline average volume and technical indicators with in-memory caching."""
    global _memory_cache, _memory_cache_time
    now = time.time()
    if now - _memory_cache_time > 600 or not _memory_cache:
        _memory_cache = {}
        try:
            db_path = Path("cache/intelligence.db")
            if db_path.exists():
                with sqlite3.connect(str(db_path), timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("SELECT symbol, avg_daily_volume, last_rsi FROM stock_memory").fetchall()
                    for r in rows:
                        _memory_cache[r["symbol"].upper()] = {
                            "avg_daily_volume": float(r["avg_daily_volume"] or 0),
                            "last_rsi": float(r["last_rsi"] or 50.0),
                        }
        except Exception:
            pass
        _memory_cache_time = now

    sym_u = sym.upper()
    if sym_u in _memory_cache:
        return _memory_cache[sym_u]
    if memory_db_fn:
        try:
            mem = memory_db_fn(sym_u)
            if mem:
                return {
                    "avg_daily_volume": float(mem.get("avg_daily_volume", 0) or 0),
                    "last_rsi": float(mem.get("last_rsi", 50.0) or 50.0),
                }
        except Exception:
            pass
    return {}


def _get_learned_sector_weights() -> Dict[str, float]:
    """Load combined sector weights from calibration and intraday learning engines."""
    global _sector_weights_cache, _sector_weights_cache_time
    now = time.time()
    if now - _sector_weights_cache_time > 600 or not _sector_weights_cache:
        try:
            import psx_intraday_learner as _learner
            _sector_weights_cache = _learner.get_sector_weights()
        except Exception:
            _sector_weights_cache = {}
        _sector_weights_cache_time = now
    return _sector_weights_cache


def _sector_avgs(stocks: List[Dict]) -> Dict[str, float]:
    data: Dict[str, List[float]] = {}
    for s in stocks:
        sec = s.get("sector", "Other")
        chg = float(s.get("change", 0) or 0)
        data.setdefault(sec, []).append(chg)
    return {k: sum(v)/len(v) for k, v in data.items() if v}


def _score(stock: Dict, kse_chg: float,
           sec_avgs: Dict[str, float],
           avg_vol: float = 0,
           last_rsi: float = 50.0,
           sector_weight: float = 1.0) -> int:
    """Institutional 0–100 intraday scoring."""
    price  = float(stock.get("price", 0) or 0)
    change = float(stock.get("change", 0) or 0)
    volume = float(stock.get("volume", 0) or 0)
    sector = stock.get("sector", "Other")

    if price < MIN_PRICE_PKR or change < MIN_ALLOWED_CHANGE or change > MAX_ALLOWED_CHANGE:
        return 0

    # True Relative Volume Pace based on Time of Day
    if avg_vol > 0:
        expected_vol = max(avg_vol * _get_time_of_day_fraction(), 1000.0)
        rvol = volume / expected_vol
    else:
        rvol = 1.0

    # 1. Volume surge pace (0–25 pts)
    if   rvol >= 3.0: vs = 25
    elif rvol >= 2.0: vs = 20
    elif rvol >= 1.5: vs = 14
    elif rvol >= 1.2: vs = 8
    else:             vs = 0

    # 2. Price Momentum (0–25 pts)
    if   1.5 <= change <= 4.8: ms = 25
    elif 1.0 <= change < 1.5:  ms = 15
    elif 4.8 < change <= 6.0:  ms = 10
    elif 6.0 < change <= 7.0:  ms = 3
    else:                      ms = 0

    # 3. Sector Tailwind + AI Calibrated Weight (0–20 pts)
    sa = sec_avgs.get(sector, 0)
    sec_pts = 8 if sa >= 1.5 else (5 if sa >= 0.8 else (3 if sa >= 0.2 else 0))
    if sector_weight >= 1.10:   cal_pts = 12
    elif sector_weight >= 0.90: cal_pts = 7
    elif sector_weight >= 0.70: cal_pts = 3
    else:                       cal_pts = -8
    ss = max(0, min(20, sec_pts + cal_pts))

    # 4. RSI zone / Structure (0–15 pts)
    if 45.0 <= last_rsi <= 65.0:  rs = 15
    elif 65.0 < last_rsi <= 72.0: rs = 10
    elif last_rsi > 75.0:         rs = 0
    else:                         rs = 7

    # 5. Market regime (0–15 pts)
    if   kse_chg >= 0.8:  mkt = 15
    elif kse_chg >= 0.3:  mkt = 10
    elif kse_chg >= 0.0:  mkt = 5
    elif kse_chg < -0.5:  mkt = -5
    else:                 mkt = 2

    return max(0, min(vs + ms + ss + rs + mkt, 100))


def _build_levels(stock: Dict) -> Optional[Dict]:
    """Compute actionable entry, dynamic stop loss, and circuit-aware target."""
    price  = float(stock.get("price", 0) or 0)
    change = float(stock.get("change", 0) or 0)
    low    = float(stock.get("low", 0) or 0)

    if price <= 0:
        return None

    prev_close = price / (1.0 + (change / 100.0))
    circuit_upper = prev_close * 1.10
    max_safe_target = prev_close * 1.090

    desired_target = price * 1.042
    target = round(min(desired_target, max_safe_target), 2)
    reward_pct = round((target - price) / price * 100, 1)

    if reward_pct < 2.2:
        return None

    if low > 0 and low < price:
        low_stop = round(low * 0.992, 2)
    else:
        low_stop = round(price * 0.970, 2)

    max_stop = round(price * 0.975, 2) # -2.5%
    min_stop = round(price * 0.962, 2) # -3.8%
    stop = max(min_stop, min(max_stop, low_stop))
    risk_pct = round((price - stop) / price * 100, 1)

    rr = round(reward_pct / max(risk_pct, 0.1), 1)
    if rr < 1.1:
        return None

    return {
        "entry_min": round(price * 0.998, 2),
        "entry_max": round(price * 1.003, 2),
        "stop": stop, "target": target,
        "risk_pct": risk_pct, "reward_pct": reward_pct,
        "rr": rr,
        "session_low": round(low, 2) if low > 0 else round(price * 0.970, 2),
        "circuit_headroom": round((circuit_upper - price) / price * 100, 1),
    }


def _liquidity_ok(stock: Dict) -> bool:
    price  = float(stock.get("price", 0) or 0)
    volume = float(stock.get("volume", 0) or 0)
    if price < MIN_PRICE_PKR:
        return False
    turnover = price * volume
    if turnover < MIN_LIQUIDITY_PKR:
        return False
    if price >= 150.0:
        return volume >= MIN_VOLUME_HIGH_PRICE
    return volume >= MIN_VOLUME_NORMAL


def scan_for_opportunities(
        stocks: List[Dict],
        index_data: Optional[Dict] = None,
        memory_db_fn=None,
        force: bool = False
) -> List[Dict]:
    """Score all live stocks for intraday potential. Called every 5 min."""
    _reset_if_new_day()
    if not stocks or (not force and not _is_market_window()):
        return []

    kse_chg = 0.0
    if index_data:
        for idx in index_data.get("indices", []):
            if "100" in idx.get("name", ""):
                kse_chg = float(idx.get("changePercent", idx.get("percentChange", 0)) or 0)
                break

    sec_avgs     = _sector_avgs(stocks)
    sec_weights  = _get_learned_sector_weights()
    tod_fraction = _get_time_of_day_fraction()
    candidates   = []

    for stock in stocks:
        sym    = stock.get("symbol", "").upper()
        change = float(stock.get("change", 0) or 0)
        price  = float(stock.get("price", 0) or 0)
        volume = float(stock.get("volume", 0) or 0)
        sector = stock.get("sector", "Other")

        if not sym or price < MIN_PRICE_PKR:
            continue
        if change < MIN_ALLOWED_CHANGE or change > MAX_ALLOWED_CHANGE:
            continue
        if not _liquidity_ok(stock):
            continue

        mem = _get_stock_memory(sym, memory_db_fn)
        avg_vol  = mem.get("avg_daily_volume", 0.0)
        last_rsi = mem.get("last_rsi", 50.0)
        sec_w    = sec_weights.get(sector, 0.6)

        sc = _score(stock, kse_chg, sec_avgs, avg_vol, last_rsi, sec_w)
        if sc < SCHEDULED_SCORE_MIN:
            continue

        lvl = _build_levels(stock)
        if not lvl:
            continue

        if avg_vol > 0:
            exp_vol = max(avg_vol * tod_fraction, 1000.0)
            rvol = round(volume / exp_vol, 1)
        else:
            rvol = 1.0

        turnover_m = round((price * volume) / 1_000_000.0, 1)

        candidates.append({
            "symbol":        sym,
            "name":          stock.get("name", sym),
            "sector":        sector,
            "price":         price,
            "change":        round(change, 2),
            "volume":        int(volume),
            "turnover_m":    turnover_m,
            "rvol":          rvol,
            "score":         sc,
            "levels":        lvl,
            "sector_weight": sec_w,
            "scanned_at":    _pkt_now().strftime("%H:%M PKT"),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    with _state_lock:
        _daily["candidates"] = candidates
    return candidates
```

---

## 4. Intraday Self-Learning Engine (`psx_intraday_learner.py`)

This engine closes the feedback loop. When a trade is alerted, it records it in SQLite. At 3:30 PM PKT, it evaluates whether the stock hit its target or stopped out, updates Bayesian sector weights, and dispatches a morning brief the next day.

```python
#!/usr/bin/env python3
"""
PSX Intraday Learning Engine
==============================
Records every intraday alert sent, evaluates its end-of-day outcome,
and uses the results to improve tomorrow's stock scoring.
"""

import sqlite3
import datetime
import threading
from pathlib import Path
from typing import Dict, Any, List

DB_PATH = Path("cache/intraday_learning.db")
_db_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_picks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    sector        TEXT,
    score         INTEGER,
    rvol          REAL,
    entry_price   REAL,
    stop_price    REAL,
    target_price  REAL,
    risk_pct      REAL,
    reward_pct    REAL,
    rr            REAL,
    mode          TEXT,
    alerted_at    TEXT,
    eod_price     REAL,
    max_price     REAL,
    outcome       TEXT,
    actual_return_pct REAL,
    target_reached    INTEGER DEFAULT 0,
    stop_reached      INTEGER DEFAULT 0,
    evaluated_at  TEXT
);

CREATE TABLE IF NOT EXISTS sector_weights (
    sector        TEXT PRIMARY KEY,
    win_count     INTEGER DEFAULT 0,
    loss_count    INTEGER DEFAULT 0,
    total_return  REAL    DEFAULT 0.0,
    avg_return    REAL    DEFAULT 0.0,
    weight        REAL    DEFAULT 1.0,
    last_updated  TEXT
);
"""

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def _pkt_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)

def get_sector_weights() -> Dict[str, float]:
    """Combines baseline calibration weights with live intraday weights."""
    weights: Dict[str, float] = {}
    try:
        cal_path = Path("cache/calibration.db")
        if cal_path.exists():
            with sqlite3.connect(str(cal_path), timeout=5) as c_conn:
                c_rows = c_conn.execute(
                    "SELECT factor_value, weight FROM factor_weights WHERE factor_type='SECTOR_INTEL'"
                ).fetchall()
                for r in c_rows:
                    if r[0]:
                        weights[r[0]] = float(r[1] or 1.0)
    except Exception:
        pass

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute("SELECT sector, weight FROM sector_weights").fetchall()
            for r in rows:
                if r["sector"]:
                    weights[r["sector"]] = float(r["weight"] or 1.0)
        finally:
            conn.close()
    return weights

def record_alert(candidate: Dict[str, Any], mode: str) -> None:
    lvl = candidate.get("levels", {})
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO intraday_picks
                (date, symbol, sector, score, rvol, entry_price, stop_price,
                 target_price, risk_pct, reward_pct, rr, mode, alerted_at, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                _pkt_now().strftime("%Y-%m-%d"),
                candidate.get("symbol", "").upper(),
                candidate.get("sector", "Other"),
                candidate.get("score", 0),
                candidate.get("rvol", 0),
                lvl.get("entry_min", candidate.get("price", 0)),
                lvl.get("stop", 0),
                lvl.get("target", 0),
                lvl.get("risk_pct", 0),
                lvl.get("reward_pct", 0),
                lvl.get("rr", 0),
                mode,
                _pkt_now().strftime("%H:%M PKT"),
                "PENDING"
            ))
            conn.commit()
        finally:
            conn.close()

def evaluate_eod(stocks: List[Dict[str, Any]]) -> List[Dict]:
    today = _pkt_now().strftime("%Y-%m-%d")
    price_map = {
        s.get("symbol", "").upper(): {
            "price": float(s.get("price", 0) or 0),
            "high":  float(s.get("high", 0) or 0),
        }
        for s in stocks if s.get("symbol")
    }
    evaluated = []
    with _db_lock:
        conn = _get_conn()
        try:
            picks = conn.execute(
                "SELECT * FROM intraday_picks WHERE date = ? AND outcome = 'PENDING'", (today,)
            ).fetchall()
            for row in picks:
                sym    = row["symbol"]
                entry  = row["entry_price"] or 0
                target = row["target_price"] or 0
                stop   = row["stop_price"] or 0
                live   = price_map.get(sym, {})
                eod_p  = live.get("price", 0)
                max_p  = live.get("high", eod_p)

                if eod_p <= 0 or entry <= 0:
                    continue

                actual_ret = round((eod_p - entry) / entry * 100, 2)
                target_hit = max_p >= target if target > 0 else False
                stop_hit   = eod_p <= stop   if stop   > 0 else False

                if target_hit:    outcome = "TARGET_HIT"
                elif stop_hit:    outcome = "STOP_HIT"
                elif actual_ret > 0: outcome = "PARTIAL_GAIN"
                else:             outcome = "PARTIAL_LOSS"

                conn.execute("""
                    UPDATE intraday_picks
                    SET eod_price=?, max_price=?, outcome=?,
                        actual_return_pct=?, target_reached=?, stop_reached=?,
                        evaluated_at=?
                    WHERE id=?
                """, (eod_p, max_p, outcome, actual_ret, int(target_hit), int(stop_hit), _pkt_now().isoformat(), row["id"]))
                evaluated.append({
                    "symbol": sym, "sector": row["sector"], "score": row["score"],
                    "entry": entry, "eod_price": eod_p, "outcome": outcome, "return_pct": actual_ret
                })
            conn.commit()
        finally:
            conn.close()

    # Recompute sector weights if outcomes evaluated
    if evaluated:
        _update_sector_weights()
    return evaluated

def _update_sector_weights() -> None:
    MIN_SAMPLES = 3
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute("""
                SELECT sector, COUNT(*) as total,
                       SUM(CASE WHEN outcome IN ('TARGET_HIT','PARTIAL_GAIN') THEN 1 ELSE 0 END) as wins,
                       AVG(actual_return_pct) as avg_ret
                FROM intraday_picks WHERE outcome != 'PENDING' AND sector IS NOT NULL
                GROUP BY sector
            """).fetchall()
            for r in rows:
                if r["total"] < MIN_SAMPLES:
                    continue
                win_rate = r["wins"] / r["total"]
                weight = round(max(0.5, min(1.5, 1.0 + (win_rate - 0.5) * 1.0)), 3)
                conn.execute("""
                    INSERT INTO sector_weights (sector, win_count, loss_count, total_return, avg_return, weight, last_updated)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(sector) DO UPDATE SET
                        win_count = excluded.win_count, loss_count = excluded.loss_count,
                        total_return = excluded.total_return, avg_return = excluded.avg_return,
                        weight = excluded.weight, last_updated = excluded.last_updated
                """, (r["sector"], r["wins"], r["total"] - r["wins"], r["total"] * (r["avg_ret"] or 0),
                      round(r["avg_ret"] or 0, 2), weight, _pkt_now().isoformat()))
            conn.commit()
        finally:
            conn.close()
```

---

## 5. PSX Undervalued Stock Analyzer (`psx_undervalued_engine.py`)

This engine implements the comprehensive valuation framework that powers the **UnderValue stocks** tab.

### The System Prompt (Verbatim Specification)
```text
You are the valuation engine inside a Pakistan Stock Exchange (PSX) portfolio app.
Your only job: given structured financial data for one PSX-listed stock, its
sector peers, and current macro inputs, compute whether the stock is
undervalued, fairly valued, or overvalued — and return a structured verdict.

You are NOT a chat assistant in this context. Do not add conversational
preamble, do not ask clarifying questions, do not offer opinions outside the
schema. If required data is missing, say so inside the schema — never guess,
interpolate, or fabricate a number to fill a gap.

DEFINITIONS:
A stock is UNDERVALUED when its current market price sits below a defensible
estimate of intrinsic value, with a margin of safety wide enough to absorb
estimation error — evaluated two ways:

1. RELATIVE UNDERVALUATION: cheaper than sector peers on standard multiples
   (P/E, P/B, EV/EBITDA, dividend yield) without a fundamental reason
   (deteriorating earnings, governance risk, structural decline).

2. ABSOLUTE UNDERVALUATION: market price below intrinsic value computed via
   Dividend Discount Model (DDM) for stable dividend payers, or Discounted
   Cash Flow (DCF) for growth/reinvestment-heavy firms.

Never issue a verdict from relative multiples alone. Always attempt the intrinsic cross-check.
```

### Valuation Rules & Methods:
1. **Relative Valuation (4 Multiples)**:
   - $P/E$ Weight: 35%
   - $P/B$ Weight: 25%
   - $EV/EBITDA$ Weight: 20%
   - Dividend Yield Weight: 20%
   - Relative Score: $0 \text{ to } 100$. Score $\ge 60$ indicates relative undervaluation.
2. **Absolute Valuation (Intrinsic Models)**:
   - **DDM (Gordon Growth)**: For dividend payers ($Payout \ge 25\%$, stable earnings):
     $$P_0 = \frac{D_1}{K_e - g}$$
     Where $K_e = R_f + (\beta \times ERP)$, $R_f = \text{10Y PIB rate}$ ($\sim 13.5\%$), $ERP = 6.0\%$, $g = \min(ROE \times (1 - \text{Payout}), 7.0\%)$.
   - **Graham Number**:
     $$\text{Fair Value} = \sqrt{22.5 \times EPS \times BVPS}$$
   - **Discounted Cash Flow (DCF)**: 2-stage model for growth firms using free cash flow to equity.
3. **Margin of Safety (MoS)**:
   $$\text{MoS \%} = \frac{\text{Fair Value} - \text{Current Price}}{\text{Fair Value}} \times 100$$
   - Heavy Undervalued: $\text{MoS} \ge 25\%$
   - Undervalued: $12\% \le \text{MoS} < 25\%$
   - Fairly Valued: $-10\% \le \text{MoS} < 12\%$
   - Overvalued: $\text{MoS} < -10\%$

---

## 6. Telegram Bot & Alert Delivery (`psx_telegram_bot.py`)

Robust Telegram alert dispatcher with SSL unverified context fallback, 3-attempt exponential retries, and rich institutional formatting:

```python
def alert_intraday_setup(candidate: Dict[str, Any], mode: str = "INSTANT", force: bool = False) -> bool:
    if not is_enabled():
        return False

    sym    = candidate.get("symbol", "?")
    sector = candidate.get("sector", "?")
    score  = candidate.get("score", 0)
    rvol   = candidate.get("rvol", 0)
    change = candidate.get("change", 0)
    price  = candidate.get("price", 0)
    lvl    = candidate.get("levels", {})
    at     = candidate.get("scanned_at", "")

    entry_min  = lvl.get("entry_min", price)
    entry_max  = lvl.get("entry_max", price)
    stop       = lvl.get("stop", 0)
    target     = lvl.get("target", 0)
    risk_pct   = lvl.get("risk_pct", 0)
    reward_pct = lvl.get("reward_pct", 0)
    rr         = lvl.get("rr", 0)

    mode_badge = {
        "INSTANT":        "⚡ INSTANT — High Conviction Setup",
        "MORNING_PICK":   "🌅 MORNING PICK — Best Setup (10:30 AM)",
        "AFTERNOON_PICK": "🌆 AFTERNOON PICK — Best Setup (1:00 PM)",
        "TODAY_SETUP":    "⭐ TODAY'S TOP SETUP — Live Review",
    }.get(mode, "⚡ INTRADAY ALERT")

    turnover_m = candidate.get("turnover_m", 0)
    sec_w      = candidate.get("sector_weight", 1.0)
    headroom   = lvl.get("circuit_headroom", 0)

    catalysts = []
    if turnover_m >= 10.0:
        catalysts.append(f"Turnover: ₨{turnover_m}M traded today 🔥")
    if rvol >= 2.0:
        catalysts.append(f"Volume surge: {rvol}x expected session pace")
    if 1.5 <= change <= 4.8:
        catalysts.append(f"+{change}% sweet-spot breakout momentum")
    if sec_w >= 1.10:
        catalysts.append(f"Sector edge: {sector} ({sec_w}x AI calibration) 🧠")
    if headroom > 0:
        catalysts.append(f"Ceiling: +{headroom}% headroom to +10% circuit limit")

    catalyst_str = "\n".join(f"  • {c}" for c in catalysts[:4])

    text = (
        f"⚡ <b>PSX INTRADAY SETUP</b>\n"
        f"<i>{mode_badge}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Symbol:</b>  {sym} 📈 LONG\n"
        f"<b>Sector:</b>  {sector}\n"
        f"<b>Score:</b>   {score}/100  |  <b>RVol:</b> {rvol}x  |  <b>Move:</b> +{change}%\n\n"
        f"📍 <b>TRADE LEVELS</b>\n"
        f"  • <b>Entry:</b>   ₨{entry_min:.2f} – ₨{entry_max:.2f}\n"
        f"  • <b>Stop:</b>    ₨{stop:.2f} (-{risk_pct}%) [Dynamic Risk Buffer] 🛡\n"
        f"  • <b>Target:</b>  ₨{target:.2f} (+{reward_pct}%) 🎯\n"
        f"  • <b>R:R:</b>     {rr}x\n\n"
        f"⚡ <b>SETUP CATALYSTS:</b>\n"
        f"{catalyst_str}\n\n"
        f"⏱ <i>Intraday only — close by 3:00 PM PKT · Scanned at {at}</i>\n"
        f"<i>PSX Alert · psx.up.railway.app</i>"
    )

    _send_async(text)
    return True
```

---

## 7. Database Schemas & Data Models

### A. `cache/calibration.db`
- `factor_weights`: `(factor_type, factor_value, sample_count, win_count, raw_win_rate, smoothed_win_rate, weight)`
- `sector_stats`: `(sector, sample_count, win_count, win_rate, profit_factor)`

### B. `cache/intelligence.db`
- `stock_memory`: `(symbol, sector, avg_daily_volume, avg_daily_range_pct, last_rsi, last_rvol, last_updated)`
- `ai_predictions`: `(symbol, signal, confidence, pattern_name, outcome)`

### C. `cache/intraday_learning.db`
- `intraday_picks`: `(date, symbol, sector, score, rvol, entry_price, stop_price, target_price, outcome, actual_return_pct)`
- `sector_weights`: `(sector, win_count, loss_count, total_return, avg_return, weight)`

### D. `cache/undervalued.db`
- `undervalued_stocks`: `(symbol, name, sector, price, verdict, relative_score, pe, pb, div_yield_pct, fair_value, margin_of_safety_pct, confidence)`

---

## 8. Deployment & Environment Configuration

- **Railway Deployment**: Root `Procfile` executes `python server.py`.
- **Port**: Bound to `$PORT` (default 8000).
- **Auto-Syncing Background Daemon**: Scrapes every 300s, executes intraday scans every 300s, tracks positions, and executes EOD audits at 3:30 PM PKT.
- **Admin Authentication**: Header or JSON body key `secret` matching `PSX#SuperAdmin@2026!kse100`.

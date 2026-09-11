#!/usr/bin/env python3
"""
PSX Intraday Learning Engine
==============================
Records every intraday alert sent, evaluates its end-of-day outcome,
and uses the results to improve tomorrow's stock scoring.

Workflow:
  1. record_alert()     — called when intraday alert fires (stores pick + levels)
  2. evaluate_eod()     — called at 3:30 PM daily (checks final price vs levels)
  3. build_learned_weights() — derives sector/score adjustments from past outcomes
  4. send_morning_brief()    — 9:15 AM: yesterday's results + what to expect today

DB: cache/intraday_learning.db (pure SQLite, zero pip-deps)
"""

import sqlite3
import json
import datetime
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional

DB_PATH = Path("cache/intraday_learning.db")
_db_lock = threading.Lock()


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_picks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,          -- YYYY-MM-DD
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
    mode          TEXT,                   -- INSTANT | MORNING_PICK | AFTERNOON_PICK
    alerted_at    TEXT,                   -- HH:MM PKT
    -- Outcome (filled at EOD)
    eod_price     REAL,
    max_price     REAL,                   -- highest price seen during day after alert
    outcome       TEXT,                   -- TARGET_HIT | STOP_HIT | PARTIAL_GAIN | PARTIAL_LOSS | PENDING
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
    weight        REAL    DEFAULT 1.0,   -- multiplier applied to scoring (0.5–1.5)
    last_updated  TEXT
);

CREATE TABLE IF NOT EXISTS score_thresholds (
    key           TEXT PRIMARY KEY,
    value         REAL,
    last_updated  TEXT
);

CREATE TABLE IF NOT EXISTS stock_reputation (
    symbol        TEXT PRIMARY KEY,
    reputation    REAL    DEFAULT 0.0,   -- starts 0; +10 per win, -15 per loss
    win_count     INTEGER DEFAULT 0,
    loss_count    INTEGER DEFAULT 0,
    last_outcome  TEXT,
    last_updated  TEXT
);

CREATE TABLE IF NOT EXISTS learning_state (
    key           TEXT PRIMARY KEY,
    value         TEXT,
    updated_at    TEXT
);
"""


# ── Learning Mode Helpers ─────────────────────────────────────────────────────
# LEARNING MODE: active when < 200 intraday picks have been evaluated.
# During learning mode, thresholds are stricter and alerts display a warning.

LEARNING_MODE_THRESHOLD = 200   # picks needed to exit learning mode
REPUTATION_WIN_BONUS    = 10.0  # reputation gained per win
REPUTATION_LOSS_PENALTY = 15.0  # reputation lost per loss
REPUTATION_DECAY_WEEKLY = 0.05  # 5% decay per week (no activity = slowly forgotten)
REPUTATION_BLACKLIST_THRESHOLD = -20.0   # below this → excluded from alerts


def is_learning_mode() -> bool:
    """Returns True if we have fewer than LEARNING_MODE_THRESHOLD evaluated picks."""
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM intraday_picks WHERE outcome != 'PENDING'"
            ).fetchone()
            return (row["n"] if row else 0) < LEARNING_MODE_THRESHOLD
        finally:
            conn.close()


def get_learning_progress() -> dict:
    """Returns learning phase progress stats for dashboard and Telegram."""
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome != 'PENDING' THEN 1 ELSE 0 END) as evaluated,
                    SUM(CASE WHEN outcome = 'TARGET_HIT' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'STOP_HIT'   THEN 1 ELSE 0 END) as losses,
                    AVG(CASE WHEN outcome != 'PENDING' THEN actual_return_pct END) as avg_ret
                FROM intraday_picks
            """).fetchone()
            evaluated = row["evaluated"] or 0
            wins      = row["wins"] or 0
            losses    = row["losses"] or 0
            avg_ret   = round(row["avg_ret"] or 0, 2)
            win_rate  = round(wins / max(evaluated, 1) * 100, 1)
            in_learning = evaluated < LEARNING_MODE_THRESHOLD
            return {
                "evaluated":     evaluated,
                "wins":          wins,
                "losses":        losses,
                "avg_return":    avg_ret,
                "win_rate_pct":  win_rate,
                "learning_mode": in_learning,
                "samples_needed": max(0, LEARNING_MODE_THRESHOLD - evaluated),
                "progress_pct":  round(min(evaluated / LEARNING_MODE_THRESHOLD * 100, 100), 1)
            }
        finally:
            conn.close()


def compute_credibility_score() -> dict:
    """
    Credibility Score 0–100 for the dashboard gauge.
    Formula:
      accuracy_30d  (40% weight) — recent prediction accuracy
      profit_factor (20% weight) — are wins bigger than losses?
      sample_conf   (20% weight) — how many samples backing weights?
      consistency   (20% weight) — is accuracy stable week-over-week?
    """
    with _db_lock:
        conn = _get_conn()
        try:
            # Last 30 days
            cutoff_30d = (_pkt_now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
            row = conn.execute("""
                SELECT
                    COUNT(*) as n,
                    SUM(CASE WHEN outcome='TARGET_HIT' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome='STOP_HIT'   THEN 1 ELSE 0 END) as losses,
                    AVG(CASE WHEN outcome='TARGET_HIT' THEN actual_return_pct ELSE NULL END) as avg_win,
                    AVG(CASE WHEN outcome='STOP_HIT'   THEN ABS(actual_return_pct) ELSE NULL END) as avg_loss
                FROM intraday_picks WHERE date >= ? AND outcome != 'PENDING'
            """, (cutoff_30d,)).fetchone()

            n       = row["n"] or 0
            wins    = row["wins"] or 0
            losses  = row["losses"] or 0
            avg_win  = row["avg_win"] or 0
            avg_loss = row["avg_loss"] or 1  # avoid div-by-zero

            # Accuracy score (0–40)
            accuracy_pct = wins / max(n, 1)
            accuracy_score = round(accuracy_pct * 40, 1)

            # Profit factor score (0–20)
            pf = (avg_win * wins) / max(avg_loss * losses, 0.01)
            pf_score = round(min(pf / 2.0, 1.0) * 20, 1)  # PF=2.0 → full 20 pts

            # Sample confidence score (0–20)
            sample_score = round(min(n / LEARNING_MODE_THRESHOLD, 1.0) * 20, 1)

            # Consistency: compare last 2 weeks
            midpoint = (_pkt_now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
            r1 = conn.execute("""
                SELECT SUM(CASE WHEN outcome='TARGET_HIT' THEN 1 ELSE 0 END)*1.0 / MAX(COUNT(*),1) as wr
                FROM intraday_picks WHERE date >= ? AND outcome != 'PENDING'
            """, (midpoint,)).fetchone()
            r2 = conn.execute("""
                SELECT SUM(CASE WHEN outcome='TARGET_HIT' THEN 1 ELSE 0 END)*1.0 / MAX(COUNT(*),1) as wr
                FROM intraday_picks WHERE date >= ? AND date < ? AND outcome != 'PENDING'
            """, (cutoff_30d, midpoint)).fetchone()
            wr1 = r1["wr"] if r1 else 0
            wr2 = r2["wr"] if r2 else 0
            consistency = 1.0 - min(abs(wr1 - wr2) * 2, 1.0)
            consistency_score = round(consistency * 20, 1)

            total_score = round(accuracy_score + pf_score + sample_score + consistency_score, 1)

            if total_score >= 81:
                label = "💎 High Confidence — Trade-Ready"
                color = "green"
            elif total_score >= 61:
                label = "🟢 Reliable for Guidance"
                color = "green"
            elif total_score >= 31:
                label = "🟡 Building Confidence"
                color = "yellow"
            else:
                label = "🔴 Learning Phase — Do Not Trade Blindly"
                color = "red"

            return {
                "total": total_score,
                "components": {
                    "accuracy": accuracy_score,
                    "profit_factor": pf_score,
                    "sample_confidence": sample_score,
                    "consistency": consistency_score
                },
                "label": label,
                "color": color,
                "samples_evaluated": n
            }
        finally:
            conn.close()


# ── Stock Reputation ──────────────────────────────────────────────────────────

def update_stock_reputation(symbol: str, outcome: str, return_pct: float) -> None:
    """
    Update a stock's reputation after an intraday outcome.
    Wins: +10 pts. Losses: -15 pts. Neutral: no change.
    Stocks below -20 are automatically excluded from future alerts.
    """
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT reputation, win_count, loss_count FROM stock_reputation WHERE symbol = ?",
                (symbol.upper(),)
            ).fetchone()
            rep   = row["reputation"] if row else 0.0
            wins  = row["win_count"]  if row else 0
            losses = row["loss_count"] if row else 0

            if outcome == "TARGET_HIT":
                rep += REPUTATION_WIN_BONUS
                wins += 1
            elif outcome == "STOP_HIT":
                rep -= REPUTATION_LOSS_PENALTY
                losses += 1

            now_str = _pkt_now().isoformat()
            conn.execute("""
                INSERT INTO stock_reputation (symbol, reputation, win_count, loss_count, last_outcome, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    reputation=excluded.reputation, win_count=excluded.win_count,
                    loss_count=excluded.loss_count, last_outcome=excluded.last_outcome,
                    last_updated=excluded.last_updated
            """, (symbol.upper(), round(rep, 2), wins, losses, outcome, now_str))
            conn.commit()
        finally:
            conn.close()


def get_blacklisted_stocks_by_reputation() -> set:
    """Returns set of symbols with reputation below threshold — excluded from alerts."""
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT symbol FROM stock_reputation WHERE reputation <= ?",
                (REPUTATION_BLACKLIST_THRESHOLD,)
            ).fetchall()
            return {r["symbol"] for r in rows}
        finally:
            conn.close()


def get_reputation(symbol: str) -> float:
    """Returns a stock's current reputation score (default 0.0)."""
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT reputation FROM stock_reputation WHERE symbol = ?",
                (symbol.upper(),)
            ).fetchone()
            return float(row["reputation"]) if row else 0.0
        finally:
            conn.close()





def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _pkt_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)


def _today() -> str:
    return _pkt_now().strftime("%Y-%m-%d")


# ── Record Alert ──────────────────────────────────────────────────────────────

def record_alert(candidate: Dict[str, Any], mode: str) -> None:
    """
    Call immediately after an intraday Telegram alert fires.
    Stores the pick so we can evaluate it at EOD.
    """
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
                _today(),
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


# ── End-of-Day Evaluation ─────────────────────────────────────────────────────

def evaluate_eod(stocks: List[Dict[str, Any]]) -> List[Dict]:
    """
    Called at 3:30 PM PKT. Checks live prices against stored targets/stops.
    Updates outcome for all PENDING picks from today.
    Returns list of evaluated picks for reporting.
    """
    today = _today()
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
            picks = conn.execute("""
                SELECT * FROM intraday_picks
                WHERE date = ? AND outcome = 'PENDING'
            """, (today,)).fetchall()

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

                if target_hit:
                    outcome = "TARGET_HIT"
                elif stop_hit:
                    outcome = "STOP_HIT"
                elif actual_ret > 0:
                    outcome = "PARTIAL_GAIN"
                else:
                    outcome = "PARTIAL_LOSS"

                conn.execute("""
                    UPDATE intraday_picks
                    SET eod_price=?, max_price=?, outcome=?,
                        actual_return_pct=?, target_reached=?, stop_reached=?,
                        evaluated_at=?
                    WHERE id=?
                """, (
                    eod_p, max_p, outcome, actual_ret,
                    1 if target_hit else 0,
                    1 if stop_hit   else 0,
                    _pkt_now().isoformat(),
                    row["id"]
                ))

                evaluated.append({
                    "symbol":      sym,
                    "sector":      row["sector"],
                    "score":       row["score"],
                    "mode":        row["mode"],
                    "entry":       entry,
                    "eod_price":   eod_p,
                    "target":      target,
                    "stop":        stop,
                    "outcome":     outcome,
                    "return_pct":  actual_ret,
                })

                # Update stock reputation (self-learning per stock)
                update_stock_reputation(sym, outcome, actual_ret)

            conn.commit()
        finally:
            conn.close()

    # After evaluation, update sector weights
    if evaluated:
        _update_sector_weights()

    return evaluated



# ── Sector Weight Learner ─────────────────────────────────────────────────────

def _update_sector_weights() -> None:
    """
    Recompute sector win-rate weights from all historical picks.
    Weight = 0.5 (avoid) to 1.5 (favor). Neutral = 1.0.
    Requires minimum 3 samples before adjusting.
    """
    MIN_SAMPLES = 3
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute("""
                SELECT sector,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome IN ('TARGET_HIT','PARTIAL_GAIN') THEN 1 ELSE 0 END) as wins,
                       AVG(actual_return_pct) as avg_ret
                FROM intraday_picks
                WHERE outcome != 'PENDING' AND sector IS NOT NULL
                GROUP BY sector
            """).fetchall()

            for r in rows:
                if r["total"] < MIN_SAMPLES:
                    continue
                win_rate = r["wins"] / r["total"]
                avg_ret  = r["avg_ret"] or 0

                # Weight formula: base 1.0, ±0.5 based on win rate vs 50% neutral
                weight = 1.0 + (win_rate - 0.5) * 1.0   # range 0.5–1.5
                weight = round(max(0.5, min(1.5, weight)), 3)

                conn.execute("""
                    INSERT INTO sector_weights
                        (sector, win_count, loss_count, total_return, avg_return, weight, last_updated)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(sector) DO UPDATE SET
                        win_count    = excluded.win_count,
                        loss_count   = excluded.loss_count,
                        total_return = excluded.total_return,
                        avg_return   = excluded.avg_return,
                        weight       = excluded.weight,
                        last_updated = excluded.last_updated
                """, (
                    r["sector"],
                    r["wins"],
                    r["total"] - r["wins"],
                    r["total"] * (r["avg_ret"] or 0),
                    round(avg_ret, 2),
                    weight,
                    _pkt_now().isoformat()
                ))
            conn.commit()
        finally:
            conn.close()


def get_sector_weights() -> Dict[str, float]:
    """
    Returns {sector: weight_multiplier} for use in intraday scoring.
    Combines:
      1. Calibrated baseline weights from cache/calibration.db (factor_type='SECTOR_INTEL')
      2. Live intraday learned weights from cache/intraday_learning.db (sector_weights)
    """
    weights: Dict[str, float] = {}
    # 1. Baseline from calibration.db
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

    # 2. Live overrides from intraday_learning.db
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT sector, weight FROM sector_weights"
            ).fetchall()
            for r in rows:
                if r["sector"]:
                    weights[r["sector"]] = float(r["weight"] or 1.0)
        finally:
            conn.close()

    return weights


# ── Morning Brief ─────────────────────────────────────────────────────────────

def send_morning_brief(stocks: List[Dict[str, Any]]) -> bool:
    """
    Called at 9:15 AM PKT every trading day.
    1. Shows yesterday's pick outcomes
    2. Shows learned sector edge
    3. Shows today's pre-market top candidates (from current stock data)
    """
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        yesterday = (_pkt_now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        # Skip weekends for yesterday
        wd = _pkt_now().weekday()
        if wd == 0:   # Monday — yesterday was Friday
            yesterday = (_pkt_now() - datetime.timedelta(days=3)).strftime("%Y-%m-%d")

        lines = ["📊 <b>PSX INTRADAY — MORNING BRIEF</b>",
                 f"<i>{_pkt_now().strftime('%a %d %b %Y · %H:%M PKT')}</i>",
                 "━━━━━━━━━━━━━━━━━━━━"]

        # Yesterday's results
        with _db_lock:
            conn = _get_conn()
            try:
                picks = conn.execute("""
                    SELECT * FROM intraday_picks
                    WHERE date = ? AND outcome != 'PENDING'
                    ORDER BY id
                """, (yesterday,)).fetchall()
            finally:
                conn.close()

        if picks:
            wins    = sum(1 for p in picks if p["outcome"] in ("TARGET_HIT", "PARTIAL_GAIN"))
            losses  = len(picks) - wins
            avg_ret = sum(p["actual_return_pct"] or 0 for p in picks) / len(picks)
            lines.append(f"\n📅 <b>YESTERDAY'S RESULTS ({yesterday})</b>")
            for p in picks:
                out_emoji = {
                    "TARGET_HIT":    "✅",
                    "PARTIAL_GAIN":  "🟢",
                    "PARTIAL_LOSS":  "🟡",
                    "STOP_HIT":      "🛑",
                }.get(p["outcome"], "⚪")
                ret = p["actual_return_pct"] or 0
                ret_str = f"+{ret:.1f}%" if ret >= 0 else f"{ret:.1f}%"
                lines.append(
                    f"  {out_emoji} <b>{p['symbol']}</b> [{p['mode'].replace('_',' ')}] "
                    f"→ {ret_str} ({p['outcome'].replace('_',' ')})"
                )
            ret_str = f"+{avg_ret:.1f}%" if avg_ret >= 0 else f"{avg_ret:.1f}%"
            lines.append(f"\n  📈 Avg return: <b>{ret_str}</b> | {wins}W / {losses}L")
        else:
            lines.append("\n📅 <b>YESTERDAY</b>: No intraday picks recorded.")

        # Market health from breadth engine
        try:
            import psx_breadth_engine as _bre
            breadth_snap = _bre.compute_breadth(stocks)
            lines.append(f"\n{_bre.get_breadth_brief_line()}")
        except Exception:
            pass  # Breadth engine optional — morning brief continues without it

        # Learned sector edges
        weights = get_sector_weights()
        favored  = [(s, w) for s, w in weights.items() if w >= 1.2]
        avoid    = [(s, w) for s, w in weights.items() if w <= 0.7]
        favored.sort(key=lambda x: x[1], reverse=True)
        avoid.sort(key=lambda x: x[1])

        if favored or avoid:
            lines.append("\n🧠 <b>LEARNED SECTOR EDGE</b>")
            for s, w in favored[:3]:
                lines.append(f"  ✅ {s} ({w:.2f}x — favour)")
            for s, w in avoid[:3]:
                lines.append(f"  ⚠️ {s} ({w:.2f}x — avoid)")

        lines.append("\n⏱ <i>Market opens 9:30 AM · Alerts active from 9:45 AM PKT</i>")
        lines.append("<i>PSX Intraday Engine · psxai.up.railway.app</i>")

        text = "\n".join(lines)
        ok, _ = _tg._send_message(text)
        if ok:
            print("[IntradayLearner] Morning brief sent.")
        return ok

    except Exception as e:
        print(f"[IntradayLearner] Morning brief error: {e}")
        return False


# ── EOD Summary Alert ─────────────────────────────────────────────────────────

def send_eod_summary(evaluated: List[Dict]) -> bool:
    """Send end-of-day outcome summary after 3:30 PM evaluation."""
    if not evaluated:
        return False
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        wins   = sum(1 for p in evaluated if p["outcome"] in ("TARGET_HIT", "PARTIAL_GAIN"))
        losses = len(evaluated) - wins
        avg_r  = sum(p["return_pct"] for p in evaluated) / len(evaluated)

        lines = [
            "📋 <b>PSX INTRADAY — END OF DAY</b>",
            f"<i>{_pkt_now().strftime('%a %d %b %Y')}</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        for p in evaluated:
            emoji = {"TARGET_HIT": "✅", "PARTIAL_GAIN": "🟢",
                     "PARTIAL_LOSS": "🟡", "STOP_HIT": "🛑"}.get(p["outcome"], "⚪")
            ret = p["return_pct"]
            ret_str = f"+{ret:.1f}%" if ret >= 0 else f"{ret:.1f}%"
            lines.append(
                f"  {emoji} <b>{p['symbol']}</b> — Entry ₨{p['entry']:.2f} → "
                f"EOD ₨{p['eod_price']:.2f}  <b>{ret_str}</b>"
            )

        avg_str = f"+{avg_r:.1f}%" if avg_r >= 0 else f"{avg_r:.1f}%"
        lines += [
            f"\n📊 <b>{wins}W / {losses}L · Avg: {avg_str}</b>",
            "<i>Sector weights updated from today's results.</i>",
            "<i>Tomorrow's morning brief at 9:15 AM PKT.</i>",
        ]

        ok, _ = _tg._send_message("\n".join(lines))
        if ok:
            print("[IntradayLearner] EOD summary sent.")
        return ok
    except Exception as e:
        print(f"[IntradayLearner] EOD summary error: {e}")
        return False


# ── Query Helpers ─────────────────────────────────────────────────────────────

def get_recent_picks(days: int = 7) -> List[Dict]:
    """Return last N days of picks for API display."""
    cutoff = (_pkt_now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute("""
                SELECT * FROM intraday_picks
                WHERE date >= ? ORDER BY date DESC, id DESC
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_performance_stats() -> Dict:
    """Summary stats for API."""
    with _db_lock:
        conn = _get_conn()
        try:
            stats = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome='TARGET_HIT'   THEN 1 ELSE 0 END) as targets_hit,
                    SUM(CASE WHEN outcome='STOP_HIT'     THEN 1 ELSE 0 END) as stops_hit,
                    SUM(CASE WHEN outcome='PARTIAL_GAIN' THEN 1 ELSE 0 END) as partial_gains,
                    SUM(CASE WHEN outcome='PARTIAL_LOSS' THEN 1 ELSE 0 END) as partial_losses,
                    AVG(CASE WHEN outcome!='PENDING' THEN actual_return_pct END) as avg_return,
                    MAX(actual_return_pct) as best_return,
                    MIN(actual_return_pct) as worst_return
                FROM intraday_picks
                WHERE outcome != 'PENDING'
            """).fetchone()
            sector_rows = conn.execute(
                "SELECT * FROM sector_weights ORDER BY weight DESC"
            ).fetchall()
            return {
                "total_evaluated": stats["total"] or 0,
                "target_hit":      stats["targets_hit"] or 0,
                "stop_hit":        stats["stops_hit"] or 0,
                "partial_gain":    stats["partial_gains"] or 0,
                "partial_loss":    stats["partial_losses"] or 0,
                "avg_return_pct":  round(stats["avg_return"] or 0, 2),
                "best_return_pct": round(stats["best_return"] or 0, 2),
                "worst_return_pct":round(stats["worst_return"] or 0, 2),
                "sector_weights":  [dict(r) for r in sector_rows],
            }
        finally:
            conn.close()


# ── Market Wrap (3:30 PM PKT) ─────────────────────────────────────────────────

def send_market_wrap(stocks: List[Dict[str, Any]],
                     index_data: Optional[Dict] = None) -> bool:
    """
    End-of-day market wrap fired at 3:30 PM PKT.
    Covers:
      • KSE-100 close (change, volume, value)
      • Advance / Decline / Unchanged breadth
      • Top 3 sector winners and losers
      • Short AI narrative explaining why market moved
      • Next-day bias and outlook
    """
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        now   = _pkt_now()
        today = now.strftime("%a %d %b %Y")

        # ── KSE-100 data ──────────────────────────────────────────────────────
        kse_change  = 0.0
        kse_value   = 0.0
        kse_name    = "KSE-100"
        market_vol  = ""
        market_val  = ""

        if index_data:
            for idx in (index_data.get("indices") or []):
                n = idx.get("name", "")
                if "100" in n:
                    kse_change = float(idx.get("percentChange",
                                       idx.get("changePercent", 0)) or 0)
                    kse_value  = float(idx.get("value", 0) or 0)
                    kse_name   = n
                    break
            market_vol = index_data.get("market", {}).get("volume", "")
            market_val = index_data.get("market", {}).get("value",  "")

        # ── Breadth ───────────────────────────────────────────────────────────
        advances  = sum(1 for s in stocks if float(s.get("change", 0) or 0) > 0)
        declines  = sum(1 for s in stocks if float(s.get("change", 0) or 0) < 0)
        unchanged = len(stocks) - advances - declines
        breadth_ratio = advances / max(declines, 1)

        # ── Sector performance ────────────────────────────────────────────────
        sector_perf: Dict[str, List[float]] = {}
        for s in stocks:
            sec = s.get("sector", "Other")
            chg = float(s.get("change", 0) or 0)
            sector_perf.setdefault(sec, []).append(chg)

        sector_avgs = {
            sec: round(sum(vals) / len(vals), 2)
            for sec, vals in sector_perf.items()
            if len(vals) >= 2
        }
        sorted_sectors = sorted(sector_avgs.items(), key=lambda x: x[1], reverse=True)
        top_sectors    = sorted_sectors[:3]
        bot_sectors    = sorted_sectors[-3:]

        # ── Narrative builder ─────────────────────────────────────────────────
        def _market_narrative(chg: float, adv: int, dec: int,
                              top: list, bot: list) -> str:
            """Rule-based narrative about today's session."""
            direction = "rallied" if chg > 0 else ("declined" if chg < 0 else "closed flat")
            strength  = "sharply " if abs(chg) > 1.5 else ("modestly " if abs(chg) > 0.5 else "")
            breadth_desc = (
                "broad-based buying" if adv > dec * 1.5 else
                "broad-based selling" if dec > adv * 1.5 else
                "mixed breadth"
            )
            top_str = ", ".join(f"{s} ({v:+.1f}%)" for s, v in top[:2]) if top else "—"
            bot_str = ", ".join(f"{s} ({v:+.1f}%)" for s, v in bot[:2]) if bot else "—"

            return (
                f"Market {strength}{direction} amid {breadth_desc}. "
                f"Leaders: {top_str}. "
                f"Laggards: {bot_str}."
            )

        # ── Next-day bias ─────────────────────────────────────────────────────
        def _next_day_bias(chg: float, ratio: float) -> tuple:
            """Returns (bias_label, outlook_text)."""
            if chg > 1.0 and ratio > 2.0:
                return "📈 BULLISH", "Strong close + broad buying — momentum likely continues. Watch for gap-up open. Take breakouts early."
            elif chg > 0.3 and ratio > 1.3:
                return "🟢 MILDLY BULLISH", "Positive close with decent breadth. Cautiously bullish — wait for 9:45 AM confirmation before entering."
            elif abs(chg) <= 0.3:
                return "⚪ NEUTRAL", "Flat session — no directional edge for tomorrow. Stick to high-volume setups only."
            elif chg < -0.3 and ratio < 0.8:
                return "🔴 MILDLY BEARISH", "Negative breadth — be selective tomorrow. Prefer defensive sectors. Tighten stop losses."
            elif chg < -1.0 and ratio < 0.5:
                return "📉 BEARISH", "Heavy selling today — risk of follow-through tomorrow. Consider sitting out or going only for exceptional score ≥ 80 setups."
            else:
                return "⚪ NEUTRAL", "Mixed signals — trade carefully, wait for clear direction after market open."

        narrative = _market_narrative(kse_change, advances, declines,
                                      top_sectors, bot_sectors)
        bias_label, outlook = _next_day_bias(kse_change, breadth_ratio)

        # ── Build message ─────────────────────────────────────────────────────
        chg_sign  = "+" if kse_change >= 0 else ""
        chg_emoji = "📈" if kse_change > 0 else ("📉" if kse_change < 0 else "➡️")

        top_sec_str = "\n".join(
            f"  ✅ {s}: {v:+.2f}%" for s, v in top_sectors
        ) or "  —"
        bot_sec_str = "\n".join(
            f"  🔻 {s}: {v:+.2f}%" for s, v in bot_sectors
        ) or "  —"

        vol_line = f"  Vol: {market_vol} | Value: ₨{market_val}B\n" if market_vol else ""

        text = (
            f"🔔 <b>PSX MARKET WRAP — {today}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{chg_emoji} <b>{kse_name}:  {chg_sign}{kse_change:.2f}%</b>  "
            f"({'Close: ₨' + str(round(kse_value)) if kse_value else ''})\n"
            f"{vol_line}"
            f"\n📊 <b>MARKET BREADTH</b>\n"
            f"  🟢 Advances:  {advances}\n"
            f"  🔴 Declines:  {declines}\n"
            f"  ⚪ Unchanged: {unchanged}\n"
            f"\n🏆 <b>TOP SECTORS</b>\n{top_sec_str}\n"
            f"\n⬇️ <b>WORST SECTORS</b>\n{bot_sec_str}\n"
            f"\n📝 <b>TODAY'S TAKE</b>\n"
            f"  {narrative}\n"
            f"\n🔭 <b>TOMORROW'S OUTLOOK</b>  {bias_label}\n"
            f"  {outlook}\n"
            f"\n<i>Next alerts: 9:15 AM morning brief · 9:45 AM scan starts</i>\n"
            f"<i>PSX Engine · psxai.up.railway.app</i>"
        )

        ok, _ = _tg._send_message(text)
        if ok:
            print("[IntradayLearner] Market wrap sent.")
        return ok

    except Exception as e:
        print(f"[IntradayLearner] Market wrap error: {e}")
        return False


# ── Daily Scorecard (4:15 PM PKT — SC-1) ──────────────────────────────────────

def send_daily_scorecard(today_evaluated: List[Dict] = None) -> bool:
    """
    Fires at 4:15 PM PKT every market day.
    Self-accountability report: what was alerted, what happened, system accuracy,
    learning progress, and current credibility score.
    """
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        now    = _pkt_now()
        today  = now.strftime("%a %d %b %Y")
        today_evaluated = today_evaluated or []

        # ── Today's intraday results ──────────────────────────────────────────
        wins_today   = sum(1 for p in today_evaluated if p["outcome"] == "TARGET_HIT")
        losses_today = sum(1 for p in today_evaluated if p["outcome"] == "STOP_HIT")
        partial_today = len(today_evaluated) - wins_today - losses_today
        avg_ret_today = (sum(p["return_pct"] for p in today_evaluated) / len(today_evaluated)
                         if today_evaluated else 0)
        ret_str = f"+{avg_ret_today:.1f}%" if avg_ret_today >= 0 else f"{avg_ret_today:.1f}%"

        # ── Build today's picks detail ────────────────────────────────────────
        pick_lines = []
        for p in today_evaluated:
            emoji = "✅" if p["outcome"] == "TARGET_HIT" else ("🛑" if p["outcome"] == "STOP_HIT" else "⚪")
            ret_s = f"+{p['return_pct']:.1f}%" if p['return_pct'] >= 0 else f"{p['return_pct']:.1f}%"
            pick_lines.append(f"  {emoji} {p['symbol']} — {p['outcome'].replace('_',' ')} {ret_s}")
        if not pick_lines:
            pick_lines = ["  (No alerts evaluated today)"]

        # ── All-time learning stats ───────────────────────────────────────────
        progress   = get_learning_progress()
        credibility = compute_credibility_score()
        mode_str   = "🔬 Learning Phase" if progress["learning_mode"] else "🎓 Calibrated Mode"
        cred_score = credibility["total"]
        cred_label = credibility["label"]

        # ── Weekly scan all-time stats (from weekly_scan.db) ─────────────────
        try:
            import sqlite3 as _sqlite3
            from pathlib import Path as _Path
            _wdb = _sqlite3.connect(str(_Path("cache/weekly_scan.db")), timeout=5)
            _wdb.row_factory = _sqlite3.Row
            _wrow = _wdb.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN target_reached=1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN stop_hit=1 THEN 1 ELSE 0 END) as stops,
                    SUM(CASE WHEN outcome='IN_PROGRESS' THEN 1 ELSE 0 END) as active
                FROM prediction_audits
            """).fetchone()
            _wdb.close()
            weekly_total = _wrow["total"] or 0
            weekly_wins  = _wrow["wins"]  or 0
            weekly_stops = _wrow["stops"] or 0
            weekly_active= _wrow["active"] or 0
            weekly_wr    = round(weekly_wins / max(weekly_total, 1) * 100, 1)
            weekly_line  = f"  Weekly Scan: {weekly_wins}✅ / {weekly_stops}🛑 / {weekly_active}🔄 active ({weekly_wr}% win rate)"
        except Exception:
            weekly_line  = "  Weekly Scan: data unavailable"

        # ── Compose message ───────────────────────────────────────────────────
        samples_str = f"{progress['evaluated']}/{200}"
        lines = [
            f"📊 <b>PSX AI — DAILY SCORECARD</b>",
            f"<i>{today}</i>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"\n⚡ <b>TODAY'S INTRADAY</b>  {ret_str}",
        ] + pick_lines + [
            f"\n📈 <b>SYSTEM PERFORMANCE</b>",
            f"  Intraday (all-time): {progress['wins']}W / {progress['losses']}L "
            f"({progress['win_rate_pct']}% win rate)",
            weekly_line,
            f"\n🔬 <b>LEARNING PROGRESS</b>",
            f"  Mode: {mode_str}",
            f"  Samples: {samples_str} ({progress['progress_pct']}% to calibration)",
            f"\n🏅 <b>CREDIBILITY SCORE: {cred_score}/100</b>",
            f"  {cred_label}",
            f"\n<i>Next: 9:15 AM morning brief | psxai.up.railway.app</i>",
        ]

        ok, _ = _tg._send_message("\n".join(lines))
        if ok:
            print("[IntradayLearner] Daily scorecard sent.")
        return ok

    except Exception as e:
        print(f"[IntradayLearner] Daily scorecard error: {e}")
        return False


# ── Sunday Pre-Week Intelligence Report (8 PM PKT) ───────────────────────────

def send_preweek_report(stocks: List[Dict[str, Any]], rotation: Dict = None) -> bool:
    """
    Every Sunday at 8 PM PKT — pre-week strategy briefing.
    Covers: last week performance, top sectors for next week,
    stocks to watch, AI learning progress, credibility score.
    """
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        rotation = rotation or {}
        now = _pkt_now()

        # ── Last week performance ─────────────────────────────────────────────
        cutoff_7d = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        progress  = get_learning_progress()
        cred      = compute_credibility_score()

        with _db_lock:
            conn = _get_conn()
            week_row = conn.execute("""
                SELECT COUNT(*) as n,
                    SUM(CASE WHEN outcome='TARGET_HIT' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome='STOP_HIT'   THEN 1 ELSE 0 END) as losses,
                    AVG(actual_return_pct) as avg_ret
                FROM intraday_picks WHERE date >= ? AND outcome != 'PENDING'
            """, (cutoff_7d,)).fetchone()
            conn.close()

        n_week  = week_row["n"] or 0
        w_week  = week_row["wins"] or 0
        l_week  = week_row["losses"] or 0
        r_week  = round(week_row["avg_ret"] or 0, 1)

        # ── Sector bets from rotation ─────────────────────────────────────────
        hot_secs  = rotation.get("hot", [])
        dump_secs = rotation.get("dump", [])

        # ── Stocks near breakout from live data ───────────────────────────────
        watchlist = []
        for s in stocks:
            chg  = float(s.get("change", 0) or 0)
            vol  = float(s.get("volume", 0) or 0)
            sym  = s.get("symbol", "")
            sec  = s.get("sector", "")
            # Check reputation — skip blacklisted
            rep = get_reputation(sym)
            if rep <= -20:
                continue
            # Near breakout: positive change 0.5–3%, high volume, hot sector
            hot_names = {x["sector"] for x in hot_secs}
            if 0.5 <= chg <= 3.5 and vol > 50_000 and sec in hot_names:
                watchlist.append({
                    "symbol": sym, "sector": sec,
                    "change": chg, "volume": int(vol)
                })
        watchlist.sort(key=lambda x: x["change"], reverse=True)
        watchlist = watchlist[:5]

        # ── Compose message ───────────────────────────────────────────────────
        next_week_str = (now + datetime.timedelta(days=1)).strftime("%d %b")
        lines = [
            f"📋 <b>PSX PRE-WEEK INTELLIGENCE</b>",
            f"<i>Week of {next_week_str} · Sent Sunday {now.strftime('%H:%M PKT')}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # Last week recap
        lines.append(f"\n📊 <b>LAST WEEK RECAP</b>")
        if n_week > 0:
            ret_sign = "+" if r_week >= 0 else ""
            lines.append(
                f"  Intraday: {w_week}✅ wins / {l_week}🛑 stops / "
                f"{n_week - w_week - l_week}⚪ partial"
            )
            lines.append(f"  Avg return: {ret_sign}{r_week}%")
        else:
            lines.append("  No evaluated picks last week (learning phase)")

        # Top sector bets
        lines.append(f"\n🎯 <b>TOP SECTOR BETS THIS WEEK</b>")
        if hot_secs:
            for i, s in enumerate(hot_secs[:3], 1):
                lines.append(
                    f"  #{i} ✅ {s['sector']}: {s['today_chg']:+.1f}% avg | "
                    f"vol {s['vol_ratio']:.1f}x | {s['breadth_pct']:.0f}% breadth"
                )
        else:
            lines.append("  Accumulating data — focus on Insurance + Food (historical leaders)")

        if dump_secs:
            lines.append(f"\n🚫 <b>AVOID THIS WEEK:</b>")
            for s in dump_secs[:3]:
                lines.append(f"  ❌ {s['sector']}: {s['today_chg']:+.1f}% avg (dump zone)")

        # Watchlist
        if watchlist:
            lines.append(f"\n🔍 <b>STOCKS TO WATCH MONDAY:</b>")
            for w in watchlist:
                lines.append(
                    f"  👁 {w['symbol']} ({w['sector']}): "
                    f"+{w['change']:.1f}% Fri · in hot sector"
                )
        else:
            lines.append(f"\n🔍 <b>STOCKS TO WATCH:</b> Scan Monday 9:45 AM onward")

        # AI status
        mode = "🔬 Learning Phase" if progress["learning_mode"] else "🎓 Calibrated"
        lines.append(f"\n📈 <b>AI STATUS</b>")
        lines.append(f"  Mode: {mode} ({progress['evaluated']}/200 samples)")
        lines.append(f"  Credibility: {cred['total']}/100 — {cred['label']}")

        if progress["learning_mode"]:
            lines.append(
                f"\n⚠️ <i>System still learning. Verify setups manually. "
                f"Best sectors historically: Insurance (71% win rate), "
                f"Food & FMCG (40% win rate).</i>"
            )

        lines.append(f"\n⏰ <i>Alerts start Mon 9:45 AM · Morning brief 9:15 AM</i>")
        lines.append(f"<i>psxai.up.railway.app</i>")

        ok, _ = _tg._send_message("\n".join(lines))
        if ok:
            print("[IntradayLearner] Sunday pre-week report sent.")
        return ok

    except Exception as e:
        print(f"[IntradayLearner] Pre-week report error: {e}")
        return False

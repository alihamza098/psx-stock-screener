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
    """Returns {sector: weight_multiplier} for use in intraday scoring."""
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT sector, weight FROM sector_weights"
            ).fetchall()
            return {r["sector"]: r["weight"] for r in rows}
        finally:
            conn.close()


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
        lines.append("<i>PSX Intraday Engine · psx.up.railway.app</i>")

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
            f"<i>PSX Engine · psx.up.railway.app</i>"
        )

        ok, _ = _tg._send_message(text)
        if ok:
            print("[IntradayLearner] Market wrap sent.")
        return ok

    except Exception as e:
        print(f"[IntradayLearner] Market wrap error: {e}")
        return False

#!/usr/bin/env python3
"""
PSX Market Breadth Engine
==========================
Computes real-time PSX market health from all 742 listed stocks.
Used to block intraday alerts during weak/bear market conditions.

REGIME LEVELS:
  BULL   (score 70-100): All systems go — full alert quota
  NEUTRAL(score 40-69):  Mixed — only score >= 80 alerts fire
  BEAR   (score 15-39):  Weak market — no new longs, warn user
  CRASH  (score 0-14):   Extreme selling — emergency alert, stay cash

No pip dependencies — pure stdlib (sqlite3, json, datetime, math).
"""

import sqlite3
import json
import datetime
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import defaultdict

_lock = threading.Lock()
_DB_PATH = Path("cache/breadth.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS breadth_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   TEXT NOT NULL,
    date          TEXT NOT NULL,
    time_pkt      TEXT NOT NULL,
    total_stocks  INTEGER DEFAULT 0,
    advances      INTEGER DEFAULT 0,
    declines      INTEGER DEFAULT 0,
    unchanged     INTEGER DEFAULT 0,
    ad_ratio      REAL DEFAULT 1.0,
    ad_pct        REAL DEFAULT 50.0,
    up_volume     REAL DEFAULT 0,
    down_volume   REAL DEFAULT 0,
    vol_ratio     REAL DEFAULT 1.0,
    kse100_adv    INTEGER DEFAULT 0,
    kse100_dec    INTEGER DEFAULT 0,
    kse100_ad_pct REAL DEFAULT 50.0,
    new_highs_20d INTEGER DEFAULT 0,
    new_lows_20d  INTEGER DEFAULT 0,
    regime        TEXT DEFAULT 'NEUTRAL',
    score         REAL DEFAULT 50.0
);

CREATE TABLE IF NOT EXISTS sector_daily (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    sector        TEXT NOT NULL,
    avg_change    REAL DEFAULT 0.0,
    total_volume  REAL DEFAULT 0.0,
    advances      INTEGER DEFAULT 0,
    declines      INTEGER DEFAULT 0,
    stock_count   INTEGER DEFAULT 0,
    rotation_score REAL DEFAULT 0.0,
    UNIQUE(date, sector)
);
"""


def _pkt_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)

def _get_conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Core Breadth Calculation ──────────────────────────────────────────────────

def compute_breadth(stocks):
    """
    Compute market breadth from stock list (from stocks_cache).
    Returns a full breadth report dict and persists to breadth.db.
    """
    if not stocks:
        return _empty_breadth()

    now = _pkt_now()
    advances = declines = unchanged = 0
    up_volume = down_volume = 0.0
    kse100_adv = kse100_dec = 0
    new_highs = new_lows = 0
    total = 0
    sector_data = {}

    for s in stocks:
        chg = float(s.get("change", 0) or 0)
        vol = float(s.get("volume", 0) or 0)
        is_kse100 = bool(s.get("isKSE100", False))
        sector = s.get("sector", "Other") or "Other"

        total += 1

        if chg > 0.05:
            advances += 1
            up_volume += vol
            if is_kse100:
                kse100_adv += 1
            if chg >= 2.0:
                new_highs += 1
        elif chg < -0.05:
            declines += 1
            down_volume += vol
            if is_kse100:
                kse100_dec += 1
            if chg <= -2.0:
                new_lows += 1
        else:
            unchanged += 1

        sd = sector_data.setdefault(sector, {
            "total_volume": 0.0, "changes": [],
            "advances": 0, "declines": 0, "count": 0
        })
        sd["total_volume"] += vol
        sd["changes"].append(chg)
        sd["count"] += 1
        if chg > 0.05:
            sd["advances"] += 1
        elif chg < -0.05:
            sd["declines"] += 1

    if total == 0:
        return _empty_breadth()

    ad_ratio  = round(advances / max(declines, 1), 3)
    ad_pct    = round(advances / total * 100, 1)
    vol_ratio = round(up_volume / max(down_volume, 1), 3)

    kse_total = kse100_adv + kse100_dec
    kse100_ad_pct = round(kse100_adv / max(kse_total, 1) * 100, 1)

    # Score: 0-100
    ad_score  = min(40.0, max(0.0, (ad_ratio - 0.5) / 1.5 * 40))
    vol_score = min(30.0, max(0.0, (vol_ratio - 0.5) / 1.5 * 30))
    kse_score = round(kse100_ad_pct / 100 * 20, 1)
    net_highs = new_highs - new_lows
    nh_score  = min(10.0, max(0.0, 5.0 + net_highs * 0.5))
    score = round(ad_score + vol_score + kse_score + nh_score, 1)

    if score >= 70:
        regime, emoji, label = "BULL", "📈", "Healthy — Full Alert Mode"
    elif score >= 40:
        regime, emoji, label = "NEUTRAL", "⚪", "Mixed — High-Quality Alerts Only"
    elif score >= 15:
        regime, emoji, label = "BEAR", "📉", "Weak Market — No New Longs Today"
    else:
        regime, emoji, label = "CRASH", "🚨", "Extreme Selling — Stay Cash"

    result = {
        "score": score, "regime": regime, "emoji": emoji, "label": label,
        "total_stocks": total, "advances": advances, "declines": declines,
        "unchanged": unchanged, "ad_ratio": ad_ratio, "ad_pct": ad_pct,
        "up_volume": round(up_volume), "down_volume": round(down_volume),
        "vol_ratio": vol_ratio, "kse100_adv": kse100_adv, "kse100_dec": kse100_dec,
        "kse100_ad_pct": kse100_ad_pct, "new_highs_20d": new_highs, "new_lows_20d": new_lows,
        "components": {
            "ad_score": round(ad_score, 1), "vol_score": round(vol_score, 1),
            "kse_score": kse_score, "nh_score": round(nh_score, 1)
        },
        "sector_summary": _build_sector_summary(sector_data),
        "computed_at": now.strftime("%Y-%m-%d %H:%M PKT")
    }

    _persist_breadth(result, now)
    _persist_sector_daily(sector_data, now.strftime("%Y-%m-%d"))

    print(f"[Breadth] {regime} {score}/100 | {advances}A/{declines}D/{unchanged}U of {total} stocks")
    return result


def _build_sector_summary(sector_data):
    summary = []
    for sector, d in sector_data.items():
        changes = d["changes"]
        if not changes:
            continue
        avg_chg = round(sum(changes) / len(changes), 2)
        ad_pct  = round(d["advances"] / max(d["count"], 1) * 100, 1)
        summary.append({
            "sector": sector, "avg_change": avg_chg,
            "stock_count": d["count"], "advances": d["advances"],
            "declines": d["declines"], "ad_pct": ad_pct,
            "total_volume": round(d["total_volume"])
        })
    summary.sort(key=lambda x: x["avg_change"], reverse=True)
    return summary


def _persist_breadth(r, now):
    try:
        with _lock:
            conn = _get_conn()
            conn.execute("""
                INSERT INTO breadth_history
                (recorded_at, date, time_pkt, total_stocks, advances, declines,
                 unchanged, ad_ratio, ad_pct, up_volume, down_volume, vol_ratio,
                 kse100_adv, kse100_dec, kse100_ad_pct, new_highs_20d,
                 new_lows_20d, regime, score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                now.isoformat(), now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
                r["total_stocks"], r["advances"], r["declines"], r["unchanged"],
                r["ad_ratio"], r["ad_pct"], r["up_volume"], r["down_volume"],
                r["vol_ratio"], r["kse100_adv"], r["kse100_dec"], r["kse100_ad_pct"],
                r["new_highs_20d"], r["new_lows_20d"], r["regime"], r["score"]
            ))
            conn.commit()
            conn.close()
    except Exception:
        pass


def _persist_sector_daily(sector_data, date_str):
    try:
        with _lock:
            conn = _get_conn()
            for sector, d in sector_data.items():
                changes = d["changes"]
                if not changes:
                    continue
                avg_chg = round(sum(changes) / len(changes), 3)
                conn.execute("""
                    INSERT INTO sector_daily
                    (date, sector, avg_change, total_volume, advances, declines, stock_count)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(date, sector) DO UPDATE SET
                        avg_change=excluded.avg_change, total_volume=excluded.total_volume,
                        advances=excluded.advances, declines=excluded.declines,
                        stock_count=excluded.stock_count
                """, (date_str, sector, avg_chg, round(d["total_volume"]),
                      d["advances"], d["declines"], d["count"]))
            conn.commit()
            conn.close()
    except Exception:
        pass


def _empty_breadth():
    return {
        "score": 50.0, "regime": "NEUTRAL", "emoji": "⚪",
        "label": "No data", "total_stocks": 0,
        "advances": 0, "declines": 0, "unchanged": 0,
        "ad_ratio": 1.0, "ad_pct": 50.0,
        "up_volume": 0, "down_volume": 0, "vol_ratio": 1.0,
        "kse100_adv": 0, "kse100_dec": 0, "kse100_ad_pct": 50.0,
        "new_highs_20d": 0, "new_lows_20d": 0,
        "components": {"ad_score": 0, "vol_score": 0, "kse_score": 0, "nh_score": 0},
        "sector_summary": [], "computed_at": "N/A"
    }


# ── Query Helpers ─────────────────────────────────────────────────────────────

def get_latest_breadth():
    try:
        with _lock:
            conn = _get_conn()
            row = conn.execute(
                "SELECT * FROM breadth_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            return dict(row) if row else None
    except Exception:
        return None


def get_regime():
    b = get_latest_breadth()
    return b["regime"] if b else "NEUTRAL"


def get_score():
    b = get_latest_breadth()
    return float(b["score"]) if b else 50.0


def get_breadth_brief_line():
    """Single formatted line for morning brief inclusion."""
    b = get_latest_breadth()
    if not b:
        return "🌡️ Market Health: Calculating..."
    regime = b["regime"]
    score  = b["score"]
    adv    = b["advances"]
    total  = b["total_stocks"]
    label  = {
        "BULL":    "✅ Full alerts active",
        "NEUTRAL": "⚠️ High-quality only (score ≥80)",
        "BEAR":    "⛔ Intraday alerts suspended",
        "CRASH":   "🚨 STAY CASH — no trades"
    }.get(regime, "")
    emoji = {"BULL": "📈", "NEUTRAL": "⚪", "BEAR": "📉", "CRASH": "🚨"}.get(regime, "⚪")
    return (
        f"🌡️ <b>Market Health: {score:.0f}/100 ({regime})</b>  {emoji}\n"
        f"  {adv}/{total} stocks advancing · {label}"
    )


# ── Sector Rotation Engine ────────────────────────────────────────────────────

def compute_sector_rotation():
    """
    Detects which sectors have hot money flowing in vs dumping out.
    Compares today vs 5-day rolling average for each sector.
    """
    try:
        with _lock:
            conn = _get_conn()
            rows = conn.execute("""
                SELECT sector, date, avg_change, total_volume, advances, declines, stock_count
                FROM sector_daily ORDER BY date DESC LIMIT 200
            """).fetchall()
            conn.close()
    except Exception:
        return {"hot": [], "neutral": [], "cold": [], "dump": [], "date": "N/A"}

    if not rows:
        return {"hot": [], "neutral": [], "cold": [], "dump": [], "date": "N/A"}

    sector_history = defaultdict(list)
    for r in rows:
        sector_history[r["sector"]].append({
            "date": r["date"], "avg_change": r["avg_change"],
            "vol": r["total_volume"], "advances": r["advances"],
            "declines": r["declines"], "count": r["stock_count"]
        })

    hot = []
    cold = []
    dump = []
    neutral = []

    for sector, history in sector_history.items():
        if len(history) < 2:
            continue
        history.sort(key=lambda x: x["date"], reverse=True)
        today = history[0]
        prior_5 = history[1:6]
        if not prior_5:
            continue

        avg_chg_5d = sum(p["avg_change"] for p in prior_5) / len(prior_5)
        avg_vol_5d = max(sum(p["vol"] for p in prior_5) / len(prior_5), 1)

        chg_delta = today["avg_change"] - avg_chg_5d
        vol_ratio = today["vol"] / avg_vol_5d
        tot       = max(today["advances"] + today["declines"], 1)
        breadth   = today["advances"] / tot * 100

        rotation_score = round(
            (chg_delta * 2.0) +
            (vol_ratio - 1.0) * 3.0 +
            (breadth - 50) * 0.1,
            2
        )

        entry = {
            "sector": sector, "rotation_score": rotation_score,
            "today_chg": round(today["avg_change"], 2),
            "avg_chg_5d": round(avg_chg_5d, 2),
            "vol_ratio": round(vol_ratio, 2),
            "breadth_pct": round(breadth, 1),
            "stock_count": today["count"],
            "advances": today["advances"], "declines": today["declines"]
        }

        if rotation_score >= 3.0:
            hot.append(entry)
        elif rotation_score <= -4.0:
            dump.append(entry)
        elif rotation_score <= -2.0:
            cold.append(entry)
        else:
            neutral.append(entry)

    hot.sort(key=lambda x: x["rotation_score"], reverse=True)
    cold.sort(key=lambda x: x["rotation_score"])
    dump.sort(key=lambda x: x["rotation_score"])

    return {
        "hot": hot[:5], "neutral": neutral[:5],
        "cold": cold[:5], "dump": dump[:5],
        "date": _pkt_now().strftime("%Y-%m-%d")
    }


def get_hot_sectors():
    rot = compute_sector_rotation()
    return {s["sector"] for s in rot.get("hot", [])}


def get_dump_sectors():
    rot = compute_sector_rotation()
    return {s["sector"] for s in rot.get("dump", [])}


def get_sector_score_bonus(sector):
    """
    Returns score adjustment for a sector:
      +12 if HOT, -20 if DUMP ZONE, 0 otherwise.
    Used in weekly scan and intraday scoring.
    """
    hot  = get_hot_sectors()
    dump = get_dump_sectors()
    if sector in hot:
        return 12
    if sector in dump:
        return -20
    return 0


# ── Telegram Alerts ───────────────────────────────────────────────────────────

def send_breadth_emergency_alert(breadth):
    """Fire emergency Telegram when market is BEAR or CRASH."""
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        score   = breadth["score"]
        regime  = breadth["regime"]
        adv     = breadth["advances"]
        total   = breadth["total_stocks"]
        ad_pct  = breadth["ad_pct"]
        vol_rat = breadth["vol_ratio"]

        if regime == "CRASH":
            header = "🚨 MARKET CRASH SIGNAL — STAY CASH"
            advice = ("Extreme broad-based selling across PSX. "
                      "Do NOT take new positions. Tighten all stops.")
        else:
            header = "📉 BEAR MARKET ALERT — NO NEW LONGS"
            advice = ("Market breadth deeply negative. "
                      "Intraday alerts suspended today. "
                      "Wait for breadth to recover before re-entering.")

        now_str = _pkt_now().strftime("%a %d %b · %H:%M PKT")
        text = (
            f"🌡️ <b>{header}</b>\n"
            f"<i>{now_str}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Market Breadth: {score}/100 ({regime})</b>\n\n"
            f"🔴 Only {adv}/{total} stocks advancing ({ad_pct}%)\n"
            f"📊 Selling volume: {vol_rat:.1f}x buying volume\n\n"
            f"⛔ <b>INTRADAY ALERTS SUSPENDED</b>\n"
            f"<i>{advice}</i>\n\n"
            f"<i>psxai.up.railway.app</i>"
        )
        ok, _ = _tg._send_message(text)
        return ok
    except Exception as e:
        print(f"[Breadth] Emergency alert error: {e}")
        return False


def send_weekly_rotation_report(rotation):
    """Friday sector rotation Telegram report."""
    try:
        import psx_telegram_bot as _tg
        if not _tg.is_enabled():
            return False

        hot  = rotation.get("hot", [])
        cold = rotation.get("cold", [])
        dump = rotation.get("dump", [])
        now_str = _pkt_now().strftime("%a %d %b %Y")

        lines = [
            f"🔄 <b>PSX SECTOR ROTATION REPORT</b>",
            f"<i>Week ending {now_str}</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        if hot:
            lines.append("\n🚀 <b>HOT — Money flowing IN:</b>")
            for s in hot:
                lines.append(
                    f"  ✅ {s['sector']}: {s['today_chg']:+.1f}% | "
                    f"vol {s['vol_ratio']:.1f}x | "
                    f"{s['advances']}/{s['stock_count']} stocks up"
                )
        else:
            lines.append("\n🚀 <b>HOT sectors:</b> None identified this week")

        if cold:
            lines.append("\n⚠️ <b>COOLING DOWN — reduce exposure:</b>")
            for s in cold[:3]:
                lines.append(f"  🔶 {s['sector']}: {s['today_chg']:+.1f}% avg")

        if dump:
            lines.append("\n🚫 <b>DUMP ZONE — no new picks:</b>")
            for s in dump:
                lines.append(f"  ❌ {s['sector']}: {s['today_chg']:+.1f}% avg (avoid)")

        if hot:
            hot_names = " + ".join(s["sector"].split()[0] for s in hot[:3])
            lines.append(f"\n🎯 <b>Next week focus: {hot_names}</b>")

        lines.append(f"\n<i>Scanner auto-prioritizes hot sectors | psxai.up.railway.app</i>")

        ok, _ = _tg._send_message("\n".join(lines))
        if ok:
            print("[Breadth] Weekly rotation report sent.")
        return ok
    except Exception as e:
        print(f"[Breadth] Rotation report error: {e}")
        return False

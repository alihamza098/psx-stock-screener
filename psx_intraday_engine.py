#!/usr/bin/env python3
"""
PSX Intraday Trade Alert Engine
=================================
Scans live PSX stocks every 5 minutes during market hours and identifies
short-term trade setups with automatic target/close monitoring.

Alert Strategy:
  INSTANT   (max 2/day): Score >= 75 fires immediately — exceptional setups
  SCHEDULED (max 2/day): 10:30 AM best morning pick + 1:00 PM best afternoon pick
  CLOSE     (per trade): When live price hits target — "Close Trade Now" alert

Total: up to 4 trade alerts + N close alerts per day.

Zero pip-dependencies — pure Python stdlib.
"""

import json
import time
import datetime
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_INSTANT_PER_DAY   = 2        # Separate quota — instant high-conviction
MAX_SCHEDULED_PER_DAY = 2        # Separate quota — 10:30 AM + 1:00 PM
INSTANT_SCORE_THRESHOLD  = 75    # Score to fire instantly
SCHEDULED_SCORE_MIN      = 55    # Min score for scheduled picks
MIN_LIQUIDITY_PKR        = 5_000_000   # PKR 5M traded value today

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

# ── Daily State ───────────────────────────────────────────────────────────────

_state_lock = threading.Lock()

_daily: Dict[str, Any] = {
    "date":               "",
    # Instant quota
    "instant_sent":       0,
    "instant_symbols":    [],
    # Scheduled quota
    "morning_sent":       False,
    "afternoon_sent":     False,
    "scheduled_symbols":  [],
    # Open positions (for close monitoring)
    # { symbol: { target, stop, entry, alerted_at, close_alerted } }
    "open_positions":     {},
    # All scored candidates from last tick (for scheduled picks)
    "candidates":         [],
}


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


def _is_trading_day() -> bool:
    return _pkt_now().weekday() < 5


# ── Scoring Engine ────────────────────────────────────────────────────────────

def _sector_avgs(stocks: List[Dict]) -> Dict[str, float]:
    data: Dict[str, List[float]] = {}
    for s in stocks:
        sec = s.get("sector", "Other")
        chg = float(s.get("change", 0) or 0)
        data.setdefault(sec, []).append(chg)
    return {k: sum(v)/len(v) for k, v in data.items() if v}


def _score(stock: Dict, kse_chg: float,
           sec_avgs: Dict[str, float], avg_vol: float = 0) -> int:
    """
    0–100 intraday score.
    Volume surge    0–30 pts
    Price momentum  0–25 pts
    Sector tailwind 0–15 pts
    RSI zone proxy  0–15 pts
    Market regime   0–15 pts
    """
    price  = float(stock.get("price", 0) or 0)
    change = float(stock.get("change", 0) or 0)
    volume = float(stock.get("volume", 0) or 0)
    sector = stock.get("sector", "Other")

    if price <= 0:
        return 0

    baseline = avg_vol if avg_vol > 0 else max(volume * 0.4, 1)
    rvol = volume / max(baseline, 1)

    # 1. Volume surge
    if   rvol >= 4.0: vs = 30
    elif rvol >= 3.0: vs = 25
    elif rvol >= 2.5: vs = 20
    elif rvol >= 2.0: vs = 14
    elif rvol >= 1.5: vs = 8
    else:             vs = 0

    # 2. Momentum
    if   change >= 4.5: ms = 25
    elif change >= 3.0: ms = 20
    elif change >= 2.0: ms = 15
    elif change >= 1.0: ms = 8
    elif change >= 0.3: ms = 3
    else:               ms = 0

    # 3. Sector
    sa = sec_avgs.get(sector, 0)
    if   sa >= 1.5: ss = 15
    elif sa >= 0.8: ss = 10
    elif sa >= 0.2: ss = 5
    else:           ss = 0

    # 4. RSI zone proxy
    if 1.5 <= change <= 5.0 and rvol >= 2.0: rs = 15
    elif change > 5.0:                        rs = 5
    elif change >= 0.5:                       rs = 8
    else:                                     rs = 0

    # 5. Market regime
    if   kse_chg >= 0.8: mkt = 15
    elif kse_chg >= 0.3: mkt = 10
    elif kse_chg >= 0:   mkt = 5
    else:                mkt = 0

    return min(vs + ms + ss + rs + mkt, 100)


def _build_levels(stock: Dict) -> Dict:
    price  = float(stock.get("price", 0) or 0)
    low    = float(stock.get("low", 0) or 0)

    entry_min = round(price * 0.998, 2)
    entry_max = round(price * 1.003, 2)

    stop_low  = round(low * 0.995, 2) if low > 0 else round(price * 0.980, 2)
    stop_pct  = round(price * 0.980, 2)
    stop      = max(stop_low, stop_pct)
    risk_pct  = round((price - stop) / price * 100, 1)

    raw_tgt   = max(3.0, min(6.0, risk_pct * 2))
    target    = round(price * (1 + raw_tgt / 100), 2)
    reward    = round((target - price) / price * 100, 1)
    rr        = round(reward / max(risk_pct, 0.1), 1)

    return {
        "entry_min": entry_min, "entry_max": entry_max,
        "stop": stop,           "target": target,
        "risk_pct": risk_pct,   "reward_pct": reward,
        "rr": rr,
        "session_low": round(low, 2),
    }


def _liquidity_ok(stock: Dict) -> bool:
    price  = float(stock.get("price", 0) or 0)
    volume = float(stock.get("volume", 0) or 0)
    return (price * volume) >= MIN_LIQUIDITY_PKR


# ── Main Scanner ──────────────────────────────────────────────────────────────

def scan_for_opportunities(
        stocks: List[Dict],
        index_data: Optional[Dict] = None,
        memory_db_fn=None
) -> List[Dict]:
    """
    Score all live stocks for intraday potential.
    Returns sorted candidate list. Called every 5 min from server.py.
    """
    _reset_if_new_day()

    if not stocks or not _is_market_window():
        return []

    # KSE-100 change
    kse_chg = 0.0
    if index_data:
        for idx in index_data.get("indices", []):
            if "100" in idx.get("name", ""):
                kse_chg = float(idx.get("changePercent",
                                        idx.get("percentChange", 0)) or 0)
                break

    sec_avgs  = _sector_avgs(stocks)
    candidates = []

    for stock in stocks:
        sym    = stock.get("symbol", "").upper()
        change = float(stock.get("change", 0) or 0)
        price  = float(stock.get("price", 0) or 0)

        if not sym or price <= 0 or change <= 0:
            continue
        if not _liquidity_ok(stock):
            continue

        avg_vol = 0
        if memory_db_fn:
            try:
                mem = memory_db_fn(sym)
                if mem:
                    avg_vol = float(mem.get("avg_daily_volume", 0) or 0)
            except Exception:
                pass

        sc = _score(stock, kse_chg, sec_avgs, avg_vol)
        if sc < SCHEDULED_SCORE_MIN:
            continue

        lvl = _build_levels(stock)
        if lvl["rr"] < 1.5:
            continue

        volume   = float(stock.get("volume", 0) or 0)
        baseline = avg_vol if avg_vol > 0 else max(volume * 0.4, 1)
        rvol     = round(volume / max(baseline, 1), 1)

        candidates.append({
            "symbol":     sym,
            "name":       stock.get("name", sym),
            "sector":     stock.get("sector", "Other"),
            "price":      price,
            "change":     round(change, 2),
            "volume":     int(volume),
            "rvol":       rvol,
            "score":      sc,
            "levels":     lvl,
            "scanned_at": _pkt_now().strftime("%H:%M PKT"),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    with _state_lock:
        _daily["candidates"] = candidates

    return candidates


# ── Instant Alert (Option A — up to 2/day, score >= 75) ──────────────────────

def check_instant_alerts(candidates: List[Dict]) -> int:
    """Fire immediately for any candidate scoring >= INSTANT_SCORE_THRESHOLD."""
    _reset_if_new_day()
    sent = 0
    try:
        import psx_telegram_bot as _tg
        with _state_lock:
            instant_sent    = _daily["instant_sent"]
            instant_symbols = list(_daily["instant_symbols"])

        for cand in candidates:
            if cand["score"] < INSTANT_SCORE_THRESHOLD:
                continue
            if instant_sent >= MAX_INSTANT_PER_DAY:
                break
            sym = cand["symbol"]
            if sym in instant_symbols:
                continue
            ok = _tg.alert_intraday_setup(cand, mode="INSTANT")
            if ok:
                with _state_lock:
                    _daily["instant_sent"] += 1
                    _daily["instant_symbols"].append(sym)
                    _daily["open_positions"][sym] = {
                        "target":       cand["levels"]["target"],
                        "stop":         cand["levels"]["stop"],
                        "entry":        cand["price"],
                        "mode":         "INSTANT",
                        "close_alerted": False,
                    }
                instant_sent += 1
                instant_symbols.append(sym)
                sent += 1
                print(f"[Intraday] INSTANT → {sym} score={cand['score']}")
    except Exception as e:
        print(f"[Intraday] Instant alert error: {e}")
    return sent


# ── Scheduled Morning — 10:30 AM PKT ─────────────────────────────────────────

def check_scheduled_morning(candidates: List[Dict]) -> bool:
    """10:30 AM — send best pick from scheduled quota (independent of instant)."""
    _reset_if_new_day()
    with _state_lock:
        if _daily["morning_sent"]:
            return False
        scheduled_symbols = list(_daily["scheduled_symbols"])
        scheduled_sent    = len(scheduled_symbols)

    if scheduled_sent >= MAX_SCHEDULED_PER_DAY:
        with _state_lock:
            _daily["morning_sent"] = True
        return False

    try:
        import psx_telegram_bot as _tg
        for cand in candidates:
            if cand["score"] < SCHEDULED_SCORE_MIN:
                continue
            sym = cand["symbol"]
            if sym in scheduled_symbols:
                continue
            ok = _tg.alert_intraday_setup(cand, mode="MORNING_PICK")
            if ok:
                with _state_lock:
                    _daily["morning_sent"]    = True
                    _daily["scheduled_symbols"].append(sym)
                    _daily["open_positions"][sym] = {
                        "target":        cand["levels"]["target"],
                        "stop":          cand["levels"]["stop"],
                        "entry":         cand["price"],
                        "mode":          "MORNING_PICK",
                        "close_alerted": False,
                    }
                print(f"[Intraday] MORNING → {sym} score={cand['score']}")
                return True
    except Exception as e:
        print(f"[Intraday] Morning alert error: {e}")

    with _state_lock:
        _daily["morning_sent"] = True
    return False


# ── Scheduled Afternoon — 1:00 PM PKT ────────────────────────────────────────

def check_scheduled_afternoon(candidates: List[Dict]) -> bool:
    """1:00 PM — send second scheduled pick (must be different from morning pick)."""
    _reset_if_new_day()
    with _state_lock:
        if _daily["afternoon_sent"]:
            return False
        scheduled_symbols = list(_daily["scheduled_symbols"])
        scheduled_sent    = len(scheduled_symbols)

    if scheduled_sent >= MAX_SCHEDULED_PER_DAY:
        with _state_lock:
            _daily["afternoon_sent"] = True
        return False

    try:
        import psx_telegram_bot as _tg
        for cand in candidates:
            if cand["score"] < SCHEDULED_SCORE_MIN:
                continue
            sym = cand["symbol"]
            if sym in scheduled_symbols:
                continue
            ok = _tg.alert_intraday_setup(cand, mode="AFTERNOON_PICK")
            if ok:
                with _state_lock:
                    _daily["afternoon_sent"] = True
                    _daily["scheduled_symbols"].append(sym)
                    _daily["open_positions"][sym] = {
                        "target":        cand["levels"]["target"],
                        "stop":          cand["levels"]["stop"],
                        "entry":         cand["price"],
                        "mode":          "AFTERNOON_PICK",
                        "close_alerted": False,
                    }
                print(f"[Intraday] AFTERNOON → {sym} score={cand['score']}")
                return True
    except Exception as e:
        print(f"[Intraday] Afternoon alert error: {e}")

    with _state_lock:
        _daily["afternoon_sent"] = True
    return False


# ── Target / Stop Monitor — runs every 5 min tick ────────────────────────────

def check_target_hits(stocks: List[Dict]) -> int:
    """
    For every open intraday position, check if target has been reached.
    If yes → fire "Close Trade Now" Telegram alert.
    Called every 5-min tick alongside scan_for_opportunities.
    Returns number of close alerts sent.
    """
    _reset_if_new_day()

    with _state_lock:
        positions = dict(_daily["open_positions"])

    if not positions:
        return 0

    # Build live price lookup
    prices = {s.get("symbol", "").upper(): float(s.get("price", 0) or 0)
              for s in stocks if s.get("symbol")}

    sent = 0
    try:
        import psx_telegram_bot as _tg
        for sym, pos in positions.items():
            if pos["close_alerted"]:
                continue

            live_price = prices.get(sym, 0)
            if live_price <= 0:
                continue

            target     = pos["target"]
            stop       = pos["stop"]
            entry      = pos["entry"]
            hit_target = live_price >= target
            hit_stop   = live_price <= stop

            if hit_target or hit_stop:
                ok = _tg.alert_intraday_close(
                    symbol=sym,
                    entry=entry,
                    live_price=live_price,
                    target=target,
                    stop=stop,
                    hit_target=hit_target,
                    mode=pos.get("mode", "INTRADAY"),
                )
                if ok:
                    with _state_lock:
                        _daily["open_positions"][sym]["close_alerted"] = True
                    sent += 1
                    result = "TARGET ✅" if hit_target else "STOP ❌"
                    print(f"[Intraday] CLOSE alert → {sym} {result} @ ₨{live_price}")
    except Exception as e:
        print(f"[Intraday] Close monitor error: {e}")

    return sent


# ── Status API ────────────────────────────────────────────────────────────────

def get_daily_status() -> Dict:
    _reset_if_new_day()
    with _state_lock:
        top = _daily["candidates"][:5]
        return {
            "date":               _daily["date"],
            "instant_sent":       _daily["instant_sent"],
            "instant_remaining":  max(0, MAX_INSTANT_PER_DAY - _daily["instant_sent"]),
            "instant_symbols":    list(_daily["instant_symbols"]),
            "morning_sent":       _daily["morning_sent"],
            "afternoon_sent":     _daily["afternoon_sent"],
            "scheduled_symbols":  list(_daily["scheduled_symbols"]),
            "open_positions":     {k: {**v} for k, v in _daily["open_positions"].items()},
            "total_alerts_today": (
                _daily["instant_sent"] +
                (1 if _daily["morning_sent"] and _daily["scheduled_symbols"] else 0) +
                (1 if _daily["afternoon_sent"] and len(_daily["scheduled_symbols"]) >= 2 else 0)
            ),
            "top_candidates":     top,
            "market_window":      _is_market_window(),
            "pkt_time":           _pkt_now().strftime("%Y-%m-%d %H:%M:%S PKT"),
        }

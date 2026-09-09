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
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_INSTANT_PER_DAY   = 2        # Separate quota — instant high-conviction
MAX_SCHEDULED_PER_DAY = 2        # Separate quota — 10:30 AM + 1:00 PM
INSTANT_SCORE_THRESHOLD  = 70    # Score to fire instantly
SCHEDULED_SCORE_MIN      = 55    # Min score for scheduled picks

# Institutional liquidity floors — stops illiquid traps like PMPK (3,475 shares)
MIN_LIQUIDITY_PKR        = 10_000_000  # PKR 10M minimum traded value today
MIN_VOLUME_NORMAL        = 100_000     # 100k shares min for stocks under PKR 150
MIN_VOLUME_HIGH_PRICE    = 25_000      # 25k shares min for stocks >= PKR 150
MIN_PRICE_PKR            = 5.0         # Exclude penny junk stocks

# Momentum bounds — sweet spot vs overextended trap
MIN_ALLOWED_CHANGE       = 1.0         # Must have positive upward momentum
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


def _is_trading_day() -> bool:
    return _pkt_now().weekday() < 5


def _get_time_of_day_fraction() -> float:
    """
    PSX market session: 09:15 to 15:30 PKT (375 minutes).
    Returns fraction of session elapsed so we can compute expected volume pace.
    """
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


# ── Scoring Engine ────────────────────────────────────────────────────────────

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
    """
    Institutional 0–100 intraday scoring:
    Volume surge pace    0–25 pts (time-of-day RVol vs true 20-30d baseline)
    Price momentum       0–25 pts (sweet spot +1.5% to +4.8%; disqualified > +7.0%)
    Sector & AI edge     0–20 pts (intraday flow + Bayesian calibrated weight)
    RSI & Structure      0–15 pts (prime markup zone 45-65; penalty for extreme overbought)
    Market regime        0–15 pts (KSE-100 tailwind vs headwind)
    """
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

    # 2. Price Momentum (0–25 pts) — reward sweet spot, penalize late runners
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
    """
    Compute actionable entry, dynamic stop loss, and circuit-aware target.
    Prevents unrealistic targets exceeding PSX's +10% daily upper price band.
    Enforces minimum 2.5% dynamic stop buffer to avoid false stopouts.
    """
    price  = float(stock.get("price", 0) or 0)
    change = float(stock.get("change", 0) or 0)
    low    = float(stock.get("low", 0) or 0)

    if price <= 0:
        return None

    # Calculate previous close & upper circuit ceiling (+10%)
    prev_close = price / (1.0 + (change / 100.0))
    circuit_upper = prev_close * 1.10
    # Safe ceiling: 1% below upper circuit to guarantee fills before lock
    max_safe_target = prev_close * 1.090

    # Desired intraday target: +4.2% gain
    desired_target = price * 1.042
    target = round(min(desired_target, max_safe_target), 2)
    reward_pct = round((target - price) / price * 100, 1)

    # Disqualify if headroom to circuit limit is less than 2.2%
    if reward_pct < 2.2:
        return None

    # Dynamic stop loss:
    # 1. Based on session low if available and below price
    if low > 0 and low < price:
        low_stop = round(low * 0.992, 2)
    else:
        low_stop = round(price * 0.970, 2) # -3.0% default risk

    # Clamp stop loss between -2.5% (minimum buffer to avoid noise) and -3.8% (maximum risk allowed)
    max_stop = round(price * 0.975, 2) # -2.5%
    min_stop = round(price * 0.962, 2) # -3.8%
    stop = max(min_stop, min(max_stop, low_stop))
    risk_pct = round((price - stop) / price * 100, 1)

    rr = round(reward_pct / max(risk_pct, 0.1), 1)
    if rr < 1.1: # Must offer positive expectancy
        return None

    entry_min = round(price * 0.998, 2)
    entry_max = round(price * 1.003, 2)

    return {
        "entry_min": entry_min, "entry_max": entry_max,
        "stop": stop,           "target": target,
        "risk_pct": risk_pct,   "reward_pct": reward_pct,
        "rr": rr,
        "session_low": round(low, 2) if low > 0 else round(price * 0.970, 2),
        "circuit_headroom": round((circuit_upper - price) / price * 100, 1),
    }


def _liquidity_ok(stock: Dict) -> bool:
    """Enforces traded value floor AND traded share count floor."""
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


# ── Main Scanner ──────────────────────────────────────────────────────────────

def scan_for_opportunities(
        stocks: List[Dict],
        index_data: Optional[Dict] = None,
        memory_db_fn=None,
        force: bool = False
) -> List[Dict]:
    """
    Score all live stocks for intraday potential.
    Returns sorted candidate list. Called every 5 min from server.py.
    """
    _reset_if_new_day()

    if not stocks or (not force and not _is_market_window()):
        return []

    # KSE-100 change
    kse_chg = 0.0
    if index_data:
        for idx in index_data.get("indices", []):
            if "100" in idx.get("name", ""):
                kse_chg = float(idx.get("changePercent",
                                        idx.get("percentChange", 0)) or 0)
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
                try:
                    import psx_intraday_learner as _learner
                    _learner.record_alert(cand, mode="INSTANT")
                except Exception:
                    pass

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
                try:
                    import psx_intraday_learner as _learner
                    _learner.record_alert(cand, mode="MORNING_PICK")
                except Exception:
                    pass
                return True

    except Exception as e:
        print(f"[Intraday] Morning alert error: {e}")

    # Keep morning_sent as False if no candidate was dispatched so next tick retries
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
                try:
                    import psx_intraday_learner as _learner
                    _learner.record_alert(cand, mode="AFTERNOON_PICK")
                except Exception:
                    pass
                return True

    except Exception as e:
        print(f"[Intraday] Afternoon alert error: {e}")

    # Keep afternoon_sent as False if no candidate was dispatched so next tick retries
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

#!/usr/bin/env python3
"""
Shared PSX Trading Utilities
============================
Shared module providing deterministic, standardized math for:
1. ATR-based dynamic stop/target brackets
2. Position sizing with capital & risk management
3. Liquidity gate evaluation
4. Time-of-day cumulative volume curve & RVOL computation
5. Market session hours & forced time-exit cutoffs (Mon-Thu vs Friday split)
6. PSX Circuit room calculation & limit validation
"""

import math
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import psx_calendar

CONFIG_DIR = Path(__file__).parent / "config"


def calculate_position_size(
    capital: float,
    risk_pct: float,
    entry: float,
    stop: float
) -> Dict[str, Any]:
    """
    Computes position size based on capital and account risk percentage.
    Handles zero/negative risk, stop equal to entry, and rounds down to whole shares.
    """
    try:
        cap = float(capital)
        rp = float(risk_pct)
        ent = float(entry)
        stp = float(stop)
    except (ValueError, TypeError):
        return {
            "shares": 0,
            "pkr_at_risk": 0.0,
            "total_outlay": 0.0,
            "risk_amount": 0.0,
            "per_share_risk": 0.0
        }

    if cap <= 0 or rp <= 0 or ent <= 0:
        return {
            "shares": 0,
            "pkr_at_risk": 0.0,
            "total_outlay": 0.0,
            "risk_amount": 0.0,
            "per_share_risk": 0.0
        }

    risk_amount = cap * (rp / 100.0)
    per_share_risk = abs(ent - stp)

    if per_share_risk <= 0.0001:
        return {
            "shares": 0,
            "pkr_at_risk": 0.0,
            "total_outlay": 0.0,
            "risk_amount": round(risk_amount, 2),
            "per_share_risk": 0.0
        }

    shares = int(math.floor(risk_amount / per_share_risk))
    pkr_at_risk = round(shares * per_share_risk, 2)
    total_outlay = round(shares * ent, 2)

    return {
        "shares": shares,
        "pkr_at_risk": pkr_at_risk,
        "total_outlay": total_outlay,
        "risk_amount": round(risk_amount, 2),
        "per_share_risk": round(per_share_risk, 2)
    }


def compute_trade_brackets(
    entry: float,
    recommendation: str,
    regime: str,
    atr: float,
    pivots: Optional[Dict[str, float]] = None,
    stock: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Computes dynamic ATR stop & target brackets with circuit limit clamping, pivot anchoring,
    and net friction R:R calculation.
    """
    if stock is None:
        stock = {}

    tb_cfg = (config or {}).get("trade_brackets", {})
    regime_mults = tb_cfg.get("regime_multipliers", {
        "Trending": {"k_stop": 1.5, "k_target": 3.0},
        "Range-Bound": {"k_stop": 1.2, "k_target": 2.0}
    })
    mult = regime_mults.get(regime, {"k_stop": 1.5, "k_target": 3.0} if regime == "Trending" else {"k_stop": 1.2, "k_target": 2.0})
    k_stop = float(mult.get("k_stop", 1.5))
    k_target = float(mult.get("k_target", 3.0))

    if atr <= 0.0001:
        atr = entry * 0.025  # Fallback 2.5%

    is_buy = "BUY" in recommendation
    is_sell = "SELL" in recommendation

    # 1. Base ATR brackets
    if is_buy:
        raw_stop = entry - (k_stop * atr)
        raw_target = entry + (k_target * atr)
    elif is_sell:
        raw_stop = entry + (k_stop * atr)
        raw_target = entry - (k_target * atr)
    else:  # HOLD
        raw_stop = entry - (1.0 * atr)
        raw_target = entry + (1.5 * atr)

    # 2. PSX Circuit limits
    circuit_pct = float(stock.get("circuit_limit_pct") or tb_cfg.get("circuit_limit_default_pct", 7.5))
    circuit_assumed = stock.get("circuit_limit_pct") is None

    # Reference price (LDCP or implied from change)
    if stock.get("ldcp"):
        ref_price = float(stock["ldcp"])
    elif stock.get("change") is not None:
        chg = float(stock.get("change", 0.0))
        ref_price = entry / (1.0 + (chg / 100.0)) if (1.0 + (chg / 100.0)) != 0 else entry
    else:
        ref_price = entry

    band_spread = max(1.00, ref_price * (circuit_pct / 100.0))
    circuit_upper = round(ref_price + band_spread, 2)
    circuit_lower = round(max(0.01, ref_price - band_spread), 2)

    # Near circuit check (~1%)
    near_thresh = float(tb_cfg.get("near_circuit_threshold_pct", 1.0)) / 100.0
    is_near_upper = entry >= (circuit_upper * (1.0 - near_thresh))
    is_near_lower = entry <= (circuit_lower * (1.0 + near_thresh))
    is_near_circuit = is_near_upper or is_near_lower
    circuit_warning = "Near circuit: exit liquidity risk" if is_near_circuit else None

    # Clamp target and stop to circuit limits
    if is_buy:
        clamped_target = min(raw_target, circuit_upper)
        clamped_stop = max(raw_stop, circuit_lower)
    elif is_sell:
        clamped_target = max(raw_target, circuit_lower)
        clamped_stop = min(raw_stop, circuit_upper)
    else:
        clamped_target = min(raw_target, circuit_upper)
        clamped_stop = max(raw_stop, circuit_lower)

    # 3. Pivot Anchoring
    target = clamped_target
    stop = clamped_stop
    pivot_anchored_target = False
    pivot_anchored_stop = False

    if pivots:
        r1 = pivots.get("r1")
        r2 = pivots.get("r2")
        s1 = pivots.get("s1")
        s2 = pivots.get("s2")

        if is_buy:
            if r1 and entry < r1 and target > r1:
                target = round(r1 * 0.995, 2)
                pivot_anchored_target = True
            elif r2 and entry < r2 and target > r2:
                target = round(r2 * 0.995, 2)
                pivot_anchored_target = True

            if s1 and entry > s1 and stop < s1:
                stop = round(s1 * 1.005, 2)
                pivot_anchored_stop = True
            elif s2 and entry > s2 and stop < s2:
                stop = round(s2 * 1.005, 2)
                pivot_anchored_stop = True

        elif is_sell:
            if s1 and entry > s1 and target < s1:
                target = round(s1 * 1.005, 2)
                pivot_anchored_target = True
            elif s2 and entry > s2 and target < s2:
                target = round(s2 * 1.005, 2)
                pivot_anchored_target = True

            if r1 and entry < r1 and stop > r1:
                stop = round(r1 * 0.995, 2)
                pivot_anchored_stop = True

    # Safety bounds to ensure valid non-crossing brackets
    if is_buy:
        if target <= entry:
            target = round(entry + max(0.10, atr * 0.5), 2)
        if stop >= entry:
            stop = round(entry - max(0.10, atr * 0.5), 2)
    elif is_sell:
        if target >= entry:
            target = round(entry - max(0.10, atr * 0.5), 2)
        if stop <= entry:
            stop = round(entry + max(0.10, atr * 0.5), 2)

    # 4. Risk / Reward Calculation
    if is_buy:
        gross_risk = max(0.01, entry - stop)
        gross_reward = max(0.0, target - entry)
    elif is_sell:
        gross_risk = max(0.01, stop - entry)
        gross_reward = max(0.0, entry - target)
    else:
        gross_risk = max(0.01, entry - stop)
        gross_reward = max(0.0, target - entry)

    gross_rr = round(gross_reward / gross_risk, 2) if gross_risk > 0 else 0.0

    costs_cfg = tb_cfg.get("costs", {})
    friction_pct = float(costs_cfg.get("total_round_trip_pct", 0.35)) / 100.0
    friction = entry * friction_pct

    net_reward = max(0.0, gross_reward - friction)
    net_risk = gross_risk + friction
    net_rr = round(net_reward / max(0.01, net_risk), 2)

    min_rr = float(tb_cfg.get("min_rr_threshold", 1.5))
    adjusted_rec = recommendation
    is_poor_rr = gross_rr < min_rr

    if is_poor_rr and recommendation != "HOLD":
        if recommendation == "STRONG BUY":
            adjusted_rec = "BUY"
        elif recommendation == "BUY":
            adjusted_rec = "HOLD"
        elif recommendation == "STRONG SELL":
            adjusted_rec = "SELL"
        elif recommendation == "SELL":
            adjusted_rec = "HOLD"

    atr_pct = (atr / entry) * 100.0 if entry > 0 else 0.0
    vol_cfg = tb_cfg.get("volatility_thresholds", {"high_atr_pct": 4.0, "normal_atr_pct": 2.5})
    high_vol_thresh = float(vol_cfg.get("high_atr_pct", 4.0))

    if atr_pct >= high_vol_thresh:
        volatility_level = "High"
    elif atr_pct >= float(vol_cfg.get("normal_atr_pct", 2.5)):
        volatility_level = "Normal"
    else:
        volatility_level = "Low"

    risk_distance = abs(entry - stop)
    reward_distance = abs(target - entry)

    return {
        "entry": round(entry, 2),
        "target": round(target, 2),
        "stop": round(stop, 2),
        "suggested_entry": round(entry, 2),
        "target_price": round(target, 2),
        "stop_loss": round(stop, 2),
        "raw_target": round(raw_target, 2),
        "raw_stop": round(raw_stop, 2),
        "k_stop": k_stop,
        "k_target": k_target,
        "atr": round(atr, 2),
        "atr_used": round(atr, 2),
        "atr_pct": round(atr_pct, 2),
        "volatility_level": volatility_level,
        "circuit_upper": circuit_upper,
        "circuit_lower": circuit_lower,
        "circuit_pct": circuit_pct,
        "circuit_assumed": circuit_assumed,
        "is_near_circuit": is_near_circuit,
        "circuit_warning": circuit_warning,
        "pivot_anchored_target": pivot_anchored_target,
        "pivot_anchored_stop": pivot_anchored_stop,
        "rr_ratio": gross_rr,
        "gross_rr": gross_rr,
        "net_rr": net_rr,
        "is_poor_rr": is_poor_rr,
        "original_recommendation": recommendation,
        "adjusted_recommendation": adjusted_rec,
        "friction_pct": round(friction_pct * 100.0, 2),
        "risk_distance": round(risk_distance, 2),
        "reward_distance": round(reward_distance, 2)
    }



def check_liquidity_gate(
    stock: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
    min_vol: int = 25000,
    min_turnover_pkr: float = 500000.0
) -> Dict[str, Any]:
    """
    Evaluates whether a stock meets minimum liquidity thresholds.
    Uses 21-day average volume and turnover when history is provided,
    or today's live volume/turnover as fallback.
    """
    avg_vol = 0.0
    avg_turnover = 0.0

    if history and len(history) >= 5:
        recent_bars = history[-21:]
        vols = []
        turnovers = []
        for b in recent_bars:
            v = float(b.get("volume", 0) or 0)
            c = float(b.get("close", 0) or 0)
            vols.append(v)
            turnovers.append(v * c)
        if vols:
            avg_vol = sum(vols) / len(vols)
            avg_turnover = sum(turnovers) / len(turnovers)
    else:
        live_vol = float(stock.get("volume", 0) or 0)
        live_price = float(stock.get("price", 0) or stock.get("current", 0) or 0)
        avg_vol = live_vol
        avg_turnover = live_vol * live_price

    passes_vol = avg_vol >= min_vol
    passes_turnover = avg_turnover >= min_turnover_pkr
    passes = passes_vol and passes_turnover

    reason = []
    if not passes_vol:
        reason.append(f"Avg Vol {int(avg_vol):,} < {min_vol:,}")
    if not passes_turnover:
        reason.append(f"Avg Turnover PKR {int(avg_turnover):,} < {int(min_turnover_pkr):,}")

    return {
        "passes": passes,
        "avg_volume_21d": round(avg_vol, 1),
        "avg_turnover_21d_pkr": round(avg_turnover, 2),
        "failure_reason": "; ".join(reason) if reason else None
    }


# Standard intraday cumulative volume progression profile (U-shaped trading volume in PSX)
DEFAULT_TIME_OF_DAY_CURVE = [
    {"minute": 15, "expected_fraction": 0.08},
    {"minute": 30, "expected_fraction": 0.16},
    {"minute": 60, "expected_fraction": 0.30},
    {"minute": 120, "expected_fraction": 0.50},
    {"minute": 180, "expected_fraction": 0.65},
    {"minute": 240, "expected_fraction": 0.78},
    {"minute": 300, "expected_fraction": 0.90},
    {"minute": 360, "expected_fraction": 1.00}
]


def get_time_of_day_volume_fraction(
    elapsed_minutes: int,
    total_session_minutes: int,
    custom_curve: Optional[List[Dict[str, float]]] = None
) -> float:
    """
    Interpolates expected cumulative volume fraction at a given minute into the trading day.
    Respects PSX's U-shape volume distribution (high at open, lower midday, accelerating toward close).
    """
    if elapsed_minutes <= 0:
        return 0.02
    if elapsed_minutes >= total_session_minutes:
        return 1.0

    curve = custom_curve or DEFAULT_TIME_OF_DAY_CURVE
    # Scale curve minute points to current session duration
    base_duration = curve[-1]["minute"] if curve else 360
    scale_factor = total_session_minutes / float(base_duration)

    scaled_points = []
    for pt in curve:
        scaled_points.append({
            "minute": pt["minute"] * scale_factor,
            "fraction": pt["expected_fraction"]
        })

    # Linear interpolation between points
    if elapsed_minutes <= scaled_points[0]["minute"]:
        f = (elapsed_minutes / max(1.0, scaled_points[0]["minute"])) * scaled_points[0]["fraction"]
        return max(0.02, min(1.0, f))

    for i in range(len(scaled_points) - 1):
        p1 = scaled_points[i]
        p2 = scaled_points[i + 1]
        if p1["minute"] <= elapsed_minutes <= p2["minute"]:
            t = (elapsed_minutes - p1["minute"]) / max(1.0, (p2["minute"] - p1["minute"]))
            f = p1["fraction"] + t * (p2["fraction"] - p1["fraction"])
            return max(0.02, min(1.0, f))

    return 1.0


def compute_rvol_today(
    today_volume: float,
    avg_volume_21d: float,
    elapsed_minutes: int,
    total_session_minutes: int,
    custom_curve: Optional[List[Dict[str, float]]] = None
) -> float:
    """
    Computes Relative Volume (RVOL) so-far-today compared to time-of-day expectation:
    RVOL_today = today_volume / (avg_volume_21d * expected_fraction_at_time)
    """
    try:
        vol = float(today_volume)
        avg = float(avg_volume_21d)
    except (ValueError, TypeError):
        return 1.0

    if avg <= 10.0:
        return 1.0 if vol <= 10.0 else 2.5

    exp_frac = get_time_of_day_volume_fraction(elapsed_minutes, total_session_minutes, custom_curve)
    expected_vol_now = max(100.0, avg * exp_frac)
    rvol = vol / expected_vol_now
    return round(max(0.01, rvol), 2)


def compute_circuit_room(
    current_price: float,
    change_pct: float,
    circuit_limit_pct: float = 7.5
) -> Dict[str, Any]:
    """
    Calculates remaining headroom to upper circuit and distance to lower circuit.
    circuit_room = circuit_limit_pct - abs(change_pct)
    """
    try:
        prc = float(current_price)
        chg = float(change_pct)
        cl = float(circuit_limit_pct)
    except (ValueError, TypeError):
        return {
            "room_to_upper_pct": 7.5,
            "room_to_lower_pct": 7.5,
            "circuit_room_pct": 7.5,
            "upper_circuit_price": round(current_price * 1.075, 2),
            "lower_circuit_price": round(current_price * 0.925, 2)
        }

    # Upper headroom: how much further can it go up before +CL%
    room_to_upper = max(0.0, cl - chg)
    # Lower room: how much further down before -CL%
    room_to_lower = max(0.0, cl + chg)

    # Reference baseline price
    ref_price = prc / (1.0 + (chg / 100.0)) if (1.0 + (chg / 100.0)) > 0 else prc
    upper_prc = round(ref_price * (1.0 + (cl / 100.0)), 2)
    lower_prc = round(max(0.01, ref_price * (1.0 - (cl / 100.0))), 2)

    return {
        "room_to_upper_pct": round(room_to_upper, 2),
        "room_to_lower_pct": round(room_to_lower, 2),
        "circuit_room_pct": round(room_to_upper, 2),  # for long trades
        "upper_circuit_price": upper_prc,
        "lower_circuit_price": lower_prc
    }


def get_session_schedule(
    dt: Optional[datetime.datetime] = None,
    time_exit_buffer_minutes: int = 15,
    allow_hold_through_jummah_break: bool = True
) -> Dict[str, Any]:
    """
    Calculates session times, phase, elapsed minutes, and forced time-exit cutoffs
    for Mon-Thu vs Friday split session in PKT.
    """
    if dt is None:
        dt = psx_calendar.get_current_pkt_datetime()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=psx_calendar.PKT_TIMEZONE)
    else:
        dt = dt.astimezone(psx_calendar.PKT_TIMEZONE)

    weekday = dt.weekday()
    cur_mins = dt.hour * 60 + dt.minute
    is_friday = (weekday == 4)
    holiday_name = psx_calendar.is_psx_holiday(dt.date())

    if weekday in (5, 6) or holiday_name:
        return {
            "is_trading_day": False,
            "reason": f"Closed ({holiday_name or 'Weekend'})",
            "session_open_mins": 572,  # 09:32
            "session_close_mins": 930,  # 15:30
            "time_exit_mins": 915,      # 15:15
            "elapsed_minutes": 0,
            "total_session_minutes": 358,
            "is_in_trading_hours": False,
            "is_jummah_break": False,
            "time_exit_cutoff_str": "15:15 PKT"
        }

    if is_friday:
        # Friday Split Session:
        # Session 1: 09:17 (557) - 12:00 (720) [163 mins]
        # Jummah Break: 12:00 (720) - 14:32 (872)
        # Session 2: 14:32 (872) - 16:30 (990) [118 mins]
        # Total active trading minutes: 163 + 118 = 281 mins
        total_active_mins = 281
        is_jummah = (720 <= cur_mins < 872)
        time_exit_mins = 990 - time_exit_buffer_minutes  # 16:15 PKT

        if cur_mins < 557:
            elapsed = 0
            in_trading = False
        elif 557 <= cur_mins < 720:
            elapsed = cur_mins - 557
            in_trading = True
        elif 720 <= cur_mins < 872:
            elapsed = 163  # paused at end of session 1
            in_trading = False
        elif 872 <= cur_mins <= 990:
            elapsed = 163 + (cur_mins - 872)
            in_trading = True
        else:
            elapsed = total_active_mins
            in_trading = False

        return {
            "is_trading_day": True,
            "is_friday": True,
            "session_open_mins": 557,     # 09:17
            "session_close_mins": 990,    # 16:30
            "time_exit_mins": time_exit_mins,  # 16:15
            "elapsed_minutes": elapsed,
            "total_session_minutes": total_active_mins,
            "is_in_trading_hours": in_trading,
            "is_jummah_break": is_jummah,
            "allow_hold_through_jummah_break": allow_hold_through_jummah_break,
            "time_exit_cutoff_str": "16:15 PKT",
            "session_name": "Friday Split Session"
        }
    else:
        # Mon-Thu Regular: 09:32 (572) - 15:30 (930) [358 mins]
        open_mins = 572
        close_mins = 930
        time_exit_mins = close_mins - time_exit_buffer_minutes  # 15:15 PKT
        total_mins = close_mins - open_mins

        if cur_mins < open_mins:
            elapsed = 0
            in_trading = False
        elif open_mins <= cur_mins <= close_mins:
            elapsed = cur_mins - open_mins
            in_trading = True
        else:
            elapsed = total_mins
            in_trading = False

        return {
            "is_trading_day": True,
            "is_friday": False,
            "session_open_mins": open_mins,
            "session_close_mins": close_mins,
            "time_exit_mins": time_exit_mins,
            "elapsed_minutes": elapsed,
            "total_session_minutes": total_mins,
            "is_in_trading_hours": in_trading,
            "is_jummah_break": False,
            "allow_hold_through_jummah_break": False,
            "time_exit_cutoff_str": "15:15 PKT",
            "session_name": "Regular Trading Session"
        }

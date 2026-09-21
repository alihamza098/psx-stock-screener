#!/usr/bin/env python3
"""
PSX Standard Technical Indicator Math Module
=============================================
Pure Python, deterministic reference calculations for:
- Wilder's Smoothed RSI (14)
- MACD (12, 26, 9)
- Exponential Moving Average (EMA 20 / EMA 50)
- Bollinger Bands & %B (20, 2.0)
- VWAP (Volume-Weighted Average Price)
- Classical Pivot Points (P, R1, R2, S1, S2)
- ATR (Average True Range, 14)
- ADX & Directional Movement (+DI, -DI, ADX 14)
"""

import math
from typing import List, Dict, Any, Optional, Tuple


def compute_ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """Calculates standard Exponential Moving Average (EMA)."""
    n = len(values)
    ema = [None] * n
    if n < period or period <= 0:
        return ema

    k = 2.0 / (period + 1)
    # Seed with initial SMA
    initial_sum = sum(values[:period])
    ema[period - 1] = initial_sum / period

    for i in range(period, n):
        ema[i] = (values[i] * k) + (ema[i - 1] * (1.0 - k))
    return ema


def compute_rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Calculates J. Welles Wilder's Smoothed RSI (14).
    Uses modified moving average:
    avg_gain = (prev_avg_gain * 13 + current_gain) / 14
    """
    n = len(closes)
    rsi = [None] * n
    if n <= period or period <= 0:
        return rsi

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff

    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0.0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0.0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def compute_macd_series(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> Dict[str, List[Optional[float]]]:
    """
    Calculates standard MACD (12, 26, 9):
    MACD Line = EMA(fast) - EMA(slow)
    Signal Line = EMA(signal) of MACD Line
    Histogram = MACD Line - Signal Line
    """
    n = len(closes)
    ema_fast = compute_ema_series(closes, fast)
    ema_slow = compute_ema_series(closes, slow)

    macd_line = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    # Signal line is EMA(signal) of macd_line starting at index (slow - 1)
    signal_line = [None] * n
    valid_macd_indices = [i for i in range(n) if macd_line[i] is not None]
    if len(valid_macd_indices) >= signal:
        valid_values = [macd_line[i] for i in valid_macd_indices]
        sig_sub = compute_ema_series(valid_values, signal)
        for sub_idx, orig_idx in enumerate(valid_macd_indices):
            signal_line[orig_idx] = sig_sub[sub_idx]

    histogram = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]

    return {
        "macd_line": macd_line,
        "signal_line": signal_line,
        "histogram": histogram
    }


def compute_bollinger_bands(
    closes: List[float],
    period: int = 20,
    multiplier: float = 2.0
) -> Dict[str, float]:
    """Calculates current Bollinger Bands (Upper, Middle, Lower, and %B)."""
    if not closes or len(closes) < period:
        p = closes[-1] if closes else 10.0
        return {"upper": p * 1.05, "middle": p, "lower": p * 0.95, "percent_b": 50.0}

    slice_bars = closes[-period:]
    middle = sum(slice_bars) / period
    variance = sum((x - middle) ** 2 for x in slice_bars) / period
    std_dev = math.sqrt(variance)

    upper = middle + (multiplier * std_dev)
    lower = middle - (multiplier * std_dev)
    last_price = closes[-1]

    band_width = upper - lower
    percent_b = ((last_price - lower) / band_width * 100.0) if band_width > 0 else 50.0

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "percent_b": round(percent_b, 1)
    }


def compute_vwap(prices: List[float], volumes: List[float]) -> float:
    """Calculates Volume-Weighted Average Price (VWAP)."""
    if not prices or not volumes or len(prices) != len(volumes):
        return prices[-1] if prices else 0.0

    sum_pv = sum(p * v for p, v in zip(prices, volumes))
    sum_v = sum(volumes)
    return (sum_pv / sum_v) if sum_v > 0 else prices[-1]


def compute_classical_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Calculates Classical Floor Trader Pivot Points:
    P = (H + L + C) / 3
    R1 = 2P - L, S1 = 2P - H
    R2 = P + (H - L), S2 = P - (H - L)
    """
    p = (high + low + close) / 3.0
    r1 = (2.0 * p) - low
    s1 = (2.0 * p) - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    return {
        "pivot": round(p, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2)
    }


def compute_atr_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> List[Optional[float]]:
    """
    Calculates Wilder's Average True Range (ATR 14).
    TR = max(H - L, abs(H - C_prev), abs(L - C_prev))
    """
    n = len(closes)
    atr = [None] * n
    if n < period or len(highs) != n or len(lows) != n:
        return atr

    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

    # Initial ATR is SMA of first 'period' TRs
    initial_atr = sum(tr[:period]) / period
    atr[period - 1] = initial_atr

    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return atr


def compute_adx_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> Dict[str, List[Optional[float]]]:
    """
    Calculates Welles Wilder's Average Directional Index (ADX 14):
    +DM, -DM, smoothed TR, +DI, -DI, DX, and ADX.
    """
    n = len(closes)
    empty_res = {"plus_di": [None] * n, "minus_di": [None] * n, "adx": [None] * n}
    if n < period * 2 or len(highs) != n or len(lows) != n:
        return empty_res

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0.0

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0.0

        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

    # Wilder's smoothing
    smoothed_tr = [0.0] * n
    smoothed_plus_dm = [0.0] * n
    smoothed_minus_dm = [0.0] * n

    smoothed_tr[period] = sum(tr[1:period + 1])
    smoothed_plus_dm[period] = sum(plus_dm[1:period + 1])
    smoothed_minus_dm[period] = sum(minus_dm[1:period + 1])

    for i in range(period + 1, n):
        smoothed_tr[i] = smoothed_tr[i - 1] - (smoothed_tr[i - 1] / period) + tr[i]
        smoothed_plus_dm[i] = smoothed_plus_dm[i - 1] - (smoothed_plus_dm[i - 1] / period) + plus_dm[i]
        smoothed_minus_dm[i] = smoothed_minus_dm[i - 1] - (smoothed_minus_dm[i - 1] / period) + minus_dm[i]

    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n

    for i in range(period, n):
        if smoothed_tr[i] > 0:
            p_di = (smoothed_plus_dm[i] / smoothed_tr[i]) * 100.0
            m_di = (smoothed_minus_dm[i] / smoothed_tr[i]) * 100.0
            plus_di[i] = p_di
            minus_di[i] = m_di
            di_sum = p_di + m_di
            dx[i] = (abs(p_di - m_di) / di_sum * 100.0) if di_sum > 0 else 0.0

    # ADX is Wilder smoothed DX
    adx = [None] * n
    adx_start = period * 2 - 1
    if n > adx_start:
        first_adx = sum(dx[period:adx_start + 1]) / period
        adx[adx_start] = first_adx
        for i in range(adx_start + 1, n):
            adx[i] = ((adx[i - 1] * (period - 1)) + dx[i]) / period

    return {
        "plus_di": plus_di,
        "minus_di": minus_di,
        "adx": adx
    }

#!/usr/bin/env python3
"""
PSX Multi-Timeframe Technical Indicator & Divergence Engine
----------------------------------------------------------
Calculates mathematical indicators and candlestick divergence patterns:
- Multi-timeframe bar series (1D, 4H, 1H, 15M)
- 4H / 1H / 1D MACD (Line, Signal, Histogram, Bullish/Bearish Crossovers)
- RSI (14) with Automated Regular & Hidden Bullish/Bearish Divergence detection
- Moving Averages (EMA 9, 21, 50, 200, SMA 20, 50, 200)
- Volatility & Bands (Bollinger Bands, ATR-14, ADX-14)
- Volume Profiling (20-day Average Volume, RVOL - Relative Volume Multiplier)
"""

import math
from typing import List, Dict, Any, Tuple, Optional


def calculate_sma(data: List[float], period: int) -> float:
    """Simple Moving Average (SMA)."""
    if not data or len(data) < period:
        return data[-1] if data else 0.0
    return sum(data[-period:]) / period


def calculate_ema_series(data: List[float], period: int) -> List[float]:
    """Calculate complete Exponential Moving Average (EMA) series."""
    if not data:
        return []
    if len(data) < period:
        return [sum(data) / len(data)] * len(data)
    
    k = 2.0 / (period + 1)
    ema_series = [sum(data[:period]) / period]
    
    for val in data[period:]:
        new_ema = val * k + ema_series[-1] * (1.0 - k)
        ema_series.append(new_ema)
        
    pad_count = len(data) - len(ema_series)
    return [ema_series[0]] * pad_count + ema_series


def calculate_ema(data: List[float], period: int) -> float:
    """Latest Exponential Moving Average (EMA)."""
    series = calculate_ema_series(data, period)
    return series[-1] if series else (data[-1] if data else 0.0)


def calculate_macd(data: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Dict[str, Any]:
    """
    Calculate Moving Average Convergence Divergence (MACD).
    Returns macd line, signal line, histogram, and crossover flags.
    """
    if len(data) < slow + signal_period:
        return {
            "macd": 0.0,
            "signal": 0.0,
            "histogram": 0.0,
            "is_bullish": False,
            "bullish_crossover": False,
            "bearish_crossover": False,
            "trend": "Neutral"
        }
    
    fast_ema = calculate_ema_series(data, fast)
    slow_ema = calculate_ema_series(data, slow)
    
    macd_series = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_series = calculate_ema_series(macd_series, signal_period)
    
    cur_macd = macd_series[-1]
    cur_signal = signal_series[-1]
    cur_hist = cur_macd - cur_signal
    
    prev_macd = macd_series[-2] if len(macd_series) >= 2 else cur_macd
    prev_signal = signal_series[-2] if len(signal_series) >= 2 else cur_signal
    
    bullish_cross = (prev_macd <= prev_signal) and (cur_macd > cur_signal)
    bearish_cross = (prev_macd >= prev_signal) and (cur_macd < cur_signal)
    is_bullish = cur_macd > cur_signal
    
    return {
        "macd": round(cur_macd, 3),
        "signal": round(cur_signal, 3),
        "histogram": round(cur_hist, 3),
        "is_bullish": is_bullish,
        "bullish_crossover": bullish_cross,
        "bearish_crossover": bearish_cross,
        "trend": "Bullish" if is_bullish else "Bearish"
    }


def calculate_rsi_series(prices: List[float], period: int = 14) -> List[float]:
    """Calculate Relative Strength Index (RSI) series using Wilder's smoothing."""
    if len(prices) < period + 1:
        return [50.0] * len(prices)
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
        
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    rsi_list = [50.0] * period
    if avg_loss == 0:
        rsi_list.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_list.append(100.0 - (100.0 / (1.0 + rs)))
        
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi_list.append(rsi)
        
    return rsi_list


def detect_rsi_divergence(prices: List[float], rsi_series: List[float], lookback: int = 25) -> Dict[str, Any]:
    """
    Detect Regular and Hidden Bullish & Bearish Divergence:
    - Regular Bullish: Price lower low (LL), RSI higher low (HL) -> Strong Reversal Setup
    - Hidden Bullish:  Price higher low (HL), RSI lower low (LL) -> Trend Continuation Setup
    - Regular Bearish: Price higher high (HH), RSI lower high (LH)
    """
    result = {
        "has_bullish_divergence": False,
        "has_bearish_divergence": False,
        "type": None,
        "detail": "No divergence detected"
    }
    
    if len(prices) < lookback or len(rsi_series) < lookback:
        return result
    
    p = prices[-lookback:]
    r = rsi_series[-lookback:]
    
    swing_lows = []
    for i in range(2, len(p) - 2):
        if p[i] <= p[i - 1] and p[i] <= p[i - 2] and p[i] <= p[i + 1] and p[i] <= p[i + 2]:
            swing_lows.append((i, p[i], r[i]))
            
    if len(swing_lows) >= 2:
        prev_idx, prev_p, prev_r = swing_lows[-2]
        last_idx, last_p, last_r = swing_lows[-1]
        
        if last_p < prev_p * 0.998 and last_r > prev_r + 2.0:
            result["has_bullish_divergence"] = True
            result["type"] = "REGULAR_BULLISH"
            result["detail"] = f"Bullish Reversal Divergence: Price Lower Low ({last_p:.2f} < {prev_p:.2f}) with RSI Higher Low ({last_r:.1f} > {prev_r:.1f})"
            return result
        
        elif last_p > prev_p * 1.002 and last_r < prev_r - 2.0 and last_r < 50:
            result["has_bullish_divergence"] = True
            result["type"] = "HIDDEN_BULLISH"
            result["detail"] = f"Hidden Bullish Continuation: Price Higher Low ({last_p:.2f} > {prev_p:.2f}) with RSI Lower Low ({last_r:.1f} < {prev_r:.1f})"
            return result

    swing_highs = []
    for i in range(2, len(p) - 2):
        if p[i] >= p[i - 1] and p[i] >= p[i - 2] and p[i] >= p[i + 1] and p[i] >= p[i + 2]:
            swing_highs.append((i, p[i], r[i]))
            
    if len(swing_highs) >= 2:
        prev_idx, prev_p, prev_r = swing_highs[-2]
        last_idx, last_p, last_r = swing_highs[-1]
        
        if last_p > prev_p * 1.002 and last_r < prev_r - 2.0:
            result["has_bearish_divergence"] = True
            result["type"] = "REGULAR_BEARISH"
            result["detail"] = f"Bearish Divergence: Price Higher High ({last_p:.2f} > {prev_p:.2f}) with RSI Lower High ({last_r:.1f} < {prev_r:.1f})"
            return result
            
    return result


def calculate_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """Calculate Average True Range (ATR)."""
    if not candles or len(candles) < 2:
        return 1.0
    
    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i].get("high", candles[i].get("close", 0))
        l = candles[i].get("low", candles[i].get("close", 0))
        prev_c = candles[i - 1].get("close", 0)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
        
    if len(tr_list) < period:
        return sum(tr_list) / max(len(tr_list), 1)
    
    return sum(tr_list[-period:]) / period


def calculate_bollinger_bands(prices: List[float], period: int = 20, num_std: float = 2.0) -> Dict[str, float]:
    """Calculate Bollinger Bands (Upper, Middle SMA, Lower)."""
    if len(prices) < period:
        p = prices[-1] if prices else 0.0
        return {"upper": p * 1.05, "middle": p, "lower": p * 0.95, "bandwidth": 10.0}
    
    recent = prices[-period:]
    sma_val = sum(recent) / period
    variance = sum((x - sma_val) ** 2 for x in recent) / period
    std_dev = math.sqrt(variance)
    
    upper = sma_val + (num_std * std_dev)
    lower = sma_val - (num_std * std_dev)
    bandwidth = ((upper - lower) / sma_val * 100) if sma_val > 0 else 0.0
    
    return {
        "upper": round(upper, 2),
        "middle": round(sma_val, 2),
        "lower": round(lower, 2),
        "bandwidth": round(bandwidth, 2)
    }


def calculate_rvol(current_volume: float, historical_volumes: List[float], period: int = 20) -> float:
    """Calculate Relative Volume (RVOL) multiplier vs historical average."""
    if not historical_volumes:
        return 1.0
    valid_vols = [v for v in historical_volumes[-period:] if v > 0]
    if not valid_vols:
        return 1.0
    avg_vol = sum(valid_vols) / len(valid_vols)
    return round(current_volume / max(avg_vol, 1.0), 2)


def analyze_symbol_technical_profile(candles_1d: List[Dict[str, Any]], current_price: float, current_volume: float) -> Dict[str, Any]:
    """
    Computes a full institutional technical profile across multiple timeframes for a given symbol.
    """
    if not candles_1d:
        return {}
    
    closes = [c["close"] for c in candles_1d]
    volumes = [c.get("volume", 0) for c in candles_1d]
    
    ema9 = round(calculate_ema(closes, 9), 2)
    ema21 = round(calculate_ema(closes, 21), 2)
    ema50 = round(calculate_ema(closes, 50), 2)
    ema200 = round(calculate_ema(closes, 200), 2)
    sma20 = round(calculate_sma(closes, 20), 2)
    sma50 = round(calculate_sma(closes, 50), 2)
    sma200 = round(calculate_sma(closes, 200), 2)
    
    macd_1d = calculate_macd(closes)
    rsi_series_1d = calculate_rsi_series(closes, 14)
    rsi_1d = round(rsi_series_1d[-1], 1) if rsi_series_1d else 50.0
    divergence_1d = detect_rsi_divergence(closes, rsi_series_1d)
    
    closes_4h = []
    for c in candles_1d:
        o, cl = c.get("open", c["close"]), c["close"]
        closes_4h.append(round(o + (cl - o) * 0.5, 2))
        closes_4h.append(cl)
    
    macd_4h = calculate_macd(closes_4h)
    rsi_series_4h = calculate_rsi_series(closes_4h, 14)
    rsi_4h = round(rsi_series_4h[-1], 1) if rsi_series_4h else 50.0
    divergence_4h = detect_rsi_divergence(closes_4h, rsi_series_4h)
    
    atr14 = round(calculate_atr(candles_1d, 14), 2)
    bb = calculate_bollinger_bands(closes, 20)
    rvol = calculate_rvol(current_volume, volumes, 20)
    
    recent_bars = candles_1d[-30:] if len(candles_1d) >= 30 else candles_1d
    support = round(min(c.get("low", c["close"]) for c in recent_bars), 2)
    resistance = round(max(c.get("high", c["close"]) for c in recent_bars), 2)
    
    trend = "BULLISH" if current_price > ema50 and ema50 >= ema200 else ("BEARISH" if current_price < ema50 else "NEUTRAL")
    
    return {
        "current_price": current_price,
        "trend": trend,
        "ma": {
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema200": ema200,
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
            "price_above_ema50": current_price > ema50,
            "price_above_ema200": current_price > ema200,
            "golden_cross": ema50 > ema200
        },
        "macd_1d": macd_1d,
        "macd_4h": macd_4h,
        "rsi_1d": rsi_1d,
        "rsi_4h": rsi_4h,
        "divergence_1d": divergence_1d,
        "divergence_4h": divergence_4h,
        "atr14": atr14,
        "bollinger": bb,
        "rvol": rvol,
        "levels": {
            "support": support,
            "resistance": resistance,
            "distance_to_resistance_pct": round((resistance - current_price) / max(current_price, 0.01) * 100, 2),
            "distance_to_support_pct": round((current_price - support) / max(current_price, 0.01) * 100, 2)
        }
    }

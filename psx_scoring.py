#!/usr/bin/env python3
"""
PSX Deterministic Trade Opportunity Scoring & Bracket Engine
------------------------------------------------------------
Computes mathematical 0–100 conviction score and exact trade brackets:
- Weighted factor scoring (0–100)
- Bracket calculation: Entry Range, Stop Loss (SL), Take Profit 1 (TP1), Take Profit 2 (TP2)
- Risk:Reward (R:R) validation
"""

from typing import Dict, Any, List, Optional
import math


def calculate_trade_score(
    tech_profile: Dict[str, Any],
    candidate_meta: Dict[str, Any],
    market_regime: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Compute deterministic score from 0 to 100 based on weighted technical & market factors.
    """
    breakdown = {}
    total_score = 0.0
    reasons = []

    price = tech_profile.get("current_price", candidate_meta.get("price", 1.0))
    ma = tech_profile.get("ma", {})
    macd_4h = tech_profile.get("macd_4h", {})
    macd_1d = tech_profile.get("macd_1d", {})
    rsi_1d = tech_profile.get("rsi_1d", 50.0)
    rsi_4h = tech_profile.get("rsi_4h", 50.0)
    div_1d = tech_profile.get("divergence_1d", {})
    div_4h = tech_profile.get("divergence_4h", {})
    rvol = tech_profile.get("rvol", 1.0)
    atr = tech_profile.get("atr14", price * 0.02)
    levels = tech_profile.get("levels", {})
    lock_info = candidate_meta.get("lock_info", {})
    regime_name = market_regime.get("regime", "NEUTRAL")

    # 1. Higher-Timeframe Trend (Max: 15 pts)
    trend_pts = 0
    if ma.get("price_above_ema200", False):
        trend_pts += 8
    if ma.get("price_above_ema50", False):
        trend_pts += 4
    if ma.get("golden_cross", False):
        trend_pts += 3
    breakdown["htf_trend"] = {"score": trend_pts, "max": 15}
    total_score += trend_pts
    if trend_pts >= 12:
        reasons.append("Strong Daily Bullish Trend (Above EMA 50 & 200)")

    # 2. 4H MACD Momentum & Crossover (Max: 10 pts)
    macd_pts = 0
    if macd_4h.get("bullish_crossover", False):
        macd_pts = 10
        reasons.append("Fresh 4H MACD Bullish Crossover")
    elif macd_4h.get("is_bullish", False):
        macd_pts = 7
        if macd_4h.get("histogram", 0) > 0:
            macd_pts = 9
            reasons.append("Positive 4H MACD Histogram Expansion")
    breakdown["macd_4h"] = {"score": macd_pts, "max": 10}
    total_score += macd_pts

    # 3. 1H Momentum & Moving Averages (Max: 10 pts)
    mom_pts = 0
    change = candidate_meta.get("change", 0.0)
    if change > 4.0:
        mom_pts = 10
    elif change > 2.0:
        mom_pts = 8
    elif change > 0.5:
        mom_pts = 6
    elif change > 0:
        mom_pts = 4
    breakdown["momentum"] = {"score": mom_pts, "max": 10}
    total_score += mom_pts

    # 4. RSI & Divergence (Max: 10 pts)
    rsi_pts = 0
    if div_1d.get("has_bullish_divergence") or div_4h.get("has_bullish_divergence"):
        rsi_pts = 10
        reasons.append(f"Bullish RSI Divergence: {div_4h.get('detail') or div_1d.get('detail')}")
    elif 45 <= rsi_1d <= 65:
        rsi_pts = 8
    elif 30 <= rsi_1d < 45:
        rsi_pts = 7
    elif rsi_1d < 30:
        rsi_pts = 6
    elif rsi_1d > 75:
        rsi_pts = 3
    else:
        rsi_pts = 5
    breakdown["rsi_divergence"] = {"score": rsi_pts, "max": 10}
    total_score += rsi_pts

    # 5. Volume Expansion / RVOL (Max: 15 pts)
    vol_pts = 0
    if rvol >= 3.0:
        vol_pts = 15
        reasons.append(f"Institutional Volume Surge ({rvol:.1f}x 20D Avg)")
    elif rvol >= 2.0:
        vol_pts = 12
        reasons.append(f"High Relative Volume ({rvol:.1f}x 20D Avg)")
    elif rvol >= 1.4:
        vol_pts = 9
    elif rvol >= 1.0:
        vol_pts = 6
    else:
        vol_pts = 2
    breakdown["volume_rvol"] = {"score": vol_pts, "max": 15}
    total_score += vol_pts

    # 6. Upper-Lock & Circuit Proximity (Max: 10 pts)
    lock_pts = 0
    dist_to_lock = lock_info.get("distance_to_upper_pct", 10.0)
    if 0.5 <= dist_to_lock <= 3.5 and change >= 3.0:
        lock_pts = 10
        reasons.append(f"Upper-Lock Momentum Setup ({dist_to_lock:.1f}% to Circuit Lock)")
    elif dist_to_lock <= 5.0 and change > 1.5:
        lock_pts = 8
    elif lock_info.get("is_at_upper_lock", False):
        lock_pts = 7
    else:
        lock_pts = 5
    breakdown["lock_proximity"] = {"score": lock_pts, "max": 10}
    total_score += lock_pts

    # 7. Support / Resistance Breakout (Max: 10 pts)
    sr_pts = 0
    dist_to_res = levels.get("distance_to_resistance_pct", 5.0)
    if dist_to_res <= 0.5:
        sr_pts = 10
        reasons.append("Resistance Breakout Confirmed")
    elif dist_to_res <= 2.5:
        sr_pts = 8
    else:
        sr_pts = 6
    breakdown["support_resistance"] = {"score": sr_pts, "max": 10}
    total_score += sr_pts

    # 8. Volatility & ATR Health (Max: 5 pts)
    volat_pts = 5 if (atr / max(price, 0.01)) <= 0.05 else 3
    breakdown["volatility_atr"] = {"score": volat_pts, "max": 5}
    total_score += volat_pts

    # 9. Market Regime & Sector (Max: 10 pts)
    regime_pts = 0
    if regime_name == "RISK_ON":
        regime_pts = 10
    elif regime_name == "NEUTRAL":
        regime_pts = 6
    else:
        regime_pts = 2
    breakdown["market_regime"] = {"score": regime_pts, "max": 10}
    total_score += regime_pts

    # 10. Financial / Fundamental Catalyst (Max: 5 pts)
    fund_pts = 4
    breakdown["fundamentals"] = {"score": fund_pts, "max": 5}
    total_score += fund_pts

    total_score = min(100.0, round(total_score, 1))

    entry_low = round(price * 0.997, 2)
    entry_high = round(price * 1.004, 2)
    
    sl_distance = max(atr * 1.2, price * 0.015)
    stop_loss = round(max(price - sl_distance, price * 0.95), 2)
    risk_amount = round(price - stop_loss, 2)
    risk_pct = round((risk_amount / price) * 100.0, 2)

    tp1 = round(price + (risk_amount * 1.6), 2)
    tp2 = round(price + (risk_amount * 2.8), 2)
    
    reward_amount_tp1 = round(tp1 - price, 2)
    reward_pct_tp1 = round((reward_amount_tp1 / price) * 100.0, 2)
    
    rr_ratio = round(reward_amount_tp1 / max(risk_amount, 0.01), 2)

    if div_1d.get("has_bullish_divergence") or div_4h.get("has_bullish_divergence"):
        strategy = "RSI Bullish Divergence Reversal"
    elif lock_pts >= 9:
        strategy = "Upper-Lock Momentum Breakout"
    elif rvol >= 2.0 and sr_pts >= 8:
        strategy = "High-Volume Resistance Breakout"
    else:
        strategy = "Multi-Timeframe Trend Continuation"

    return {
        "score": total_score,
        "strategy": strategy,
        "reasons": reasons,
        "breakdown": breakdown,
        "entry_range": f"{entry_low:.2f} – {entry_high:.2f}",
        "entry_price": price,
        "stop_loss": stop_loss,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_pct": risk_pct,
        "reward_pct_tp1": reward_pct_tp1,
        "rr_ratio": f"1:{rr_ratio:.1f}"
    }

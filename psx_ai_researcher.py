#!/usr/bin/env python3
"""
PSX AI Researcher & Trade Card Generator
----------------------------------------
Takes quantitative scoring data and technical profiles to generate
institutional, human-readable Trade Cards for user approval.
"""

from typing import Dict, Any, List


def generate_trade_card(
    symbol: str,
    name: str,
    sector: str,
    score_data: Dict[str, Any],
    tech_profile: Dict[str, Any],
    market_regime: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Format a complete, institutional Trade Card for human approval.
    """
    score = score_data.get("score", 50.0)
    
    if score >= 80:
        conviction = "HIGH CONVICTION"
        action = "BUY"
        status_color = "green"
    elif score >= 68:
        conviction = "MODERATE CONVICTION"
        action = "WATCH / ACCUMULATE"
        status_color = "blue"
    elif score >= 55:
        conviction = "LOW CONVICTION"
        action = "WATCHLIST"
        status_color = "yellow"
    else:
        conviction = "WEAK SETUP"
        action = "IGNORE"
        status_color = "red"

    ma = tech_profile.get("ma", {})
    macd_4h = tech_profile.get("macd_4h", {})
    rsi_1d = tech_profile.get("rsi_1d", 50.0)
    rvol = tech_profile.get("rvol", 1.0)
    levels = tech_profile.get("levels", {})

    technical_checklist = [
        {"item": "Daily Trend Above EMA 50 & 200", "passed": ma.get("price_above_ema50", False) and ma.get("price_above_ema200", False)},
        {"item": f"4H MACD Bullish ({macd_4h.get('histogram', 0):+.2f})", "passed": macd_4h.get("is_bullish", False)},
        {"item": f"RSI In Sweet Spot ({rsi_1d:.1f})", "passed": 40 <= rsi_1d <= 70},
        {"item": f"Relative Volume Expansion ({rvol:.1f}x 20D)", "passed": rvol >= 1.4},
        {"item": f"Near Key Support/Resistance Breakout", "passed": levels.get("distance_to_resistance_pct", 10.0) <= 3.0},
        {"item": f"Market Regime Support ({market_regime.get('regime', 'NEUTRAL')})", "passed": market_regime.get("regime") == "RISK_ON"}
    ]

    card = {
        "symbol": symbol.upper(),
        "name": name,
        "sector": sector,
        "conviction": conviction,
        "recommendation": action,
        "status_color": status_color,
        "score": score,
        "strategy": score_data.get("strategy", "Momentum Breakout"),
        "brackets": {
            "entry_range": score_data.get("entry_range", "Market"),
            "entry_price": score_data.get("entry_price", 0.0),
            "stop_loss": score_data.get("stop_loss", 0.0),
            "take_profit_1": score_data.get("take_profit_1", 0.0),
            "take_profit_2": score_data.get("take_profit_2", 0.0),
            "risk_pct": score_data.get("risk_pct", 0.0),
            "reward_pct_tp1": score_data.get("reward_pct_tp1", 0.0),
            "rr_ratio": score_data.get("rr_ratio", "1:2.0")
        },
        "technical_checklist": technical_checklist,
        "key_reasons": score_data.get("reasons", []),
        "score_breakdown": score_data.get("breakdown", {}),
        "approval_state": "PENDING_HUMAN_APPROVAL",
        "market_regime": market_regime.get("regime", "NEUTRAL")
    }

    return card

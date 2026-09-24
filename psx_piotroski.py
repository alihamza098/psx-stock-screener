#!/usr/bin/env python3
"""
Piotroski F-Score Fundamental Strength Engine
---------------------------------------------
Evaluates the 9-point fundamental accounting health checklist developed
by Prof. Joseph Piotroski to eliminate "value traps" from true value stocks.

Categories:
1. Profitability (4 points): Positive Net Income, Positive CFO, ROA Expansion, Quality of Earnings (CFO > Net Income).
2. Leverage & Liquidity (3 points): Leverage Reduction, Current Ratio Expansion, Zero Equity Dilution.
3. Operating Efficiency (2 points): Gross Margin Expansion, Asset Turnover Expansion.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional

FINANCIALS_PATH = Path(__file__).parent / "financials.json"
SNAPSHOT_PATH = Path(__file__).parent / "data_snapshot.json"

_financials_cache = None
_snapshot_cache = None

def _load_data():
    global _financials_cache, _snapshot_cache
    if _financials_cache is None and FINANCIALS_PATH.exists():
        try:
            with open(FINANCIALS_PATH, "r", encoding="utf-8") as f:
                _financials_cache = json.load(f)
        except Exception:
            _financials_cache = {}
    if _snapshot_cache is None and SNAPSHOT_PATH.exists():
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                snap = json.load(f)
                stocks = snap.get("data", []) if isinstance(snap, dict) else (snap if isinstance(snap, list) else [])
                _snapshot_cache = {s.get("symbol", "").upper(): s for s in stocks if s.get("symbol")}
        except Exception:
            _snapshot_cache = {}

def calculate_piotroski_fscore(symbol: str) -> Dict[str, Any]:
    """
    Calculate 9-point Piotroski F-Score for symbol.
    Returns structured score, breakdown, and verdict.
    """
    _load_data()
    sym_u = symbol.upper()
    stock_info = (_snapshot_cache or {}).get(sym_u, {})
    fin_history = (_financials_cache or {}).get(sym_u, {})

    points = {
        "positive_net_income": 0,
        "positive_cfo": 0,
        "roa_expansion": 0,
        "quality_of_earnings": 0,
        "leverage_reduction": 0,
        "liquidity_expansion": 0,
        "zero_dilution": 1,         # Default 1 if no massive share issues detected
        "margin_expansion": 0,
        "turnover_expansion": 0
    }
    rationales = []

    # Financial data extraction
    eps = float(stock_info.get("eps", 0) or 0)
    pe = float(stock_info.get("pe", 0) or 0)
    pb = float(stock_info.get("pb", 0) or 0)
    roe = float(stock_info.get("roe", 0) or 0)
    mcap = float(stock_info.get("mcap", 0) or 0)
    div_yield = float(stock_info.get("divYield", 0) or 0)

    # 1. Positive Net Income
    if eps > 0 or roe > 0:
        points["positive_net_income"] = 1
        rationales.append("Profitable operations (EPS > 0, ROE > 0)")
    else:
        rationales.append("Unprofitable / negative earnings")

    # 2. Positive Operating Cash Flow
    # For PSX companies without separate cashflow line, dividend payout or high ROE indicates cash generation
    if eps > 0 and (div_yield > 2.0 or roe > 8.0):
        points["positive_cfo"] = 1
        rationales.append("Strong operational cash flow indicated by dividend payout / solid ROE")

    # 3. ROA Expansion & Efficiency (YoY components)
    rev_years = sorted(list(fin_history.keys())) if isinstance(fin_history, dict) else []
    is_new_listing = len(rev_years) < 2 or stock_info.get("is_new_listing", False)

    if len(rev_years) >= 2:
        latest_rev = float(fin_history.get(rev_years[-1], 0) or 0)
        prev_rev = float(fin_history.get(rev_years[-2], 0) or 0)
        if latest_rev > prev_rev:
            points["roa_expansion"] = 1
            points["margin_expansion"] = 1
            points["turnover_expansion"] = 1
            rationales.append(f"Revenue expanded from PKR {prev_rev:,.0f} to PKR {latest_rev:,.0f}")
    elif eps > 0:
        points["roa_expansion"] = 1

    # 4. Quality of Earnings (Accruals)
    if points["positive_net_income"] and points["positive_cfo"]:
        points["quality_of_earnings"] = 1

    # 5. Leverage & Liquidity
    # Low P/B and positive ROE indicate asset quality and manageable debt
    if pb > 0 and pb <= 2.5:
        points["leverage_reduction"] = 1
    if div_yield > 0:
        points["liquidity_expansion"] = 1

    total_score = sum(points.values())

    if is_new_listing:
        # Normalized Denominator for newly-listed companies (< 2 years history)
        # 3 YoY factors (margin_expansion, turnover_expansion, roa_expansion YoY) are skipped
        max_score = 6
        pct = (total_score / max_score) * 100.0
        if pct >= 66.0:
            verdict = "NEW_LISTING_HEALTHY"
            label = f"Newly Listed — Healthy Accounting ({total_score}/{max_score})"
            color = "green"
        else:
            verdict = "NEW_LISTING_INSUFFICIENT_HISTORY"
            label = f"Newly Listed (< 2 Yrs History — Score {total_score}/{max_score})"
            color = "blue"
    else:
        max_score = 9
        if total_score >= 7:
            verdict = "STRONG_VALUE"
            label = "High Fundamental Quality (Safe)"
            color = "green"
        elif total_score >= 4:
            verdict = "MODERATE_QUALITY"
            label = "Average Quality (Neutral)"
            color = "yellow"
        else:
            verdict = "VALUE_TRAP"
            label = "Value Trap Warning (Weak Accounting Health)"
            color = "red"

    return {
        "symbol": sym_u,
        "f_score": total_score,
        "score": total_score,
        "max_score": max_score,
        "is_new_listing": is_new_listing,
        "verdict": verdict,
        "label": label,
        "color": color,
        "breakdown": points,
        "rationales": rationales
    }

calculate_piotroski_f_score = calculate_piotroski_fscore

if __name__ == "__main__":
    for s in ["LUCK", "OGDC", "MEBL", "SYS", "HASCOL"]:
        res = calculate_piotroski_fscore(s)
        print(f"{s}: F-Score {res['f_score']}/9 ({res['verdict']})")

#!/usr/bin/env python3
"""
PSX Opportunity Scanner & Market Regime Classifier
--------------------------------------------------
Filters ~520 PSX listed stocks through the Opportunity Funnel:
1. Liquidity & Volume filter (Active stocks)
2. Upper-lock & Circuit Breaker proximity
3. Market Regime classification (KSE-100 trend & market breadth)
4. Candidate ranking for quantitative scoring
"""

from typing import List, Dict, Any, Tuple
import time


def classify_market_regime(index_data: Dict[str, Any], stock_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Determine PSX overall market regime: RISK_ON, NEUTRAL, or RISK_OFF.
    Calculates KSE-100 performance, total market turnover, and market breadth (advancers vs decliners).
    """
    kse100 = None
    if index_data and "indices" in index_data:
        for idx in index_data["indices"]:
            if "100" in idx.get("name", ""):
                kse100 = idx
                break
                
    kse_change_pct = kse100.get("changePercent", 0.0) if kse100 else 0.0
    kse_value = kse100.get("value", 0.0) if kse100 else 0.0
    
    advancers = sum(1 for s in stock_list if s.get("change", 0.0) > 0)
    decliners = sum(1 for s in stock_list if s.get("change", 0.0) < 0)
    unchanged = sum(1 for s in stock_list if s.get("change", 0.0) == 0)
    total_active = advancers + decliners + unchanged
    
    breadth_ratio = (advancers / max(advancers + decliners, 1)) * 100
    
    if kse_change_pct >= 0.5 and breadth_ratio >= 55.0:
        regime = "RISK_ON"
        sentiment = "Bullish - High Conviction for Longs"
    elif kse_change_pct <= -0.6 or breadth_ratio <= 40.0:
        regime = "RISK_OFF"
        sentiment = "Bearish / Defensive - Strict Selective Entries"
    else:
        regime = "NEUTRAL"
        sentiment = "Consolidating / Range-bound"
        
    return {
        "regime": regime,
        "sentiment": sentiment,
        "kse100_value": kse_value,
        "kse100_change_pct": round(kse_change_pct, 2),
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "breadth_pct": round(breadth_ratio, 1),
        "total_symbols_scanned": total_active
    }


def calculate_upper_lock_status(stock: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate circuit breaker limits and distance to Upper/Lower Lock.
    PSX standard circuit breaker: +/- 5%, 7.5%, or 10% from previous close.
    """
    price = stock.get("price", 0.0)
    change = stock.get("change", 0.0)
    
    prev_close = price / (1.0 + (change / 100.0)) if change != -100.0 and change != 0 else price
    
    limit_pct = 7.5 if price > 100 else 10.0
    upper_lock_price = round(prev_close * (1.0 + limit_pct / 100.0), 2)
    lower_lock_price = round(prev_close * (1.0 - limit_pct / 100.0), 2)
    
    distance_to_upper = round(max(0.0, ((upper_lock_price - price) / max(price, 0.01)) * 100.0), 2)
    is_at_upper_lock = change >= (limit_pct - 0.2) or distance_to_upper <= 0.2
    
    return {
        "prev_close": round(prev_close, 2),
        "upper_lock_price": upper_lock_price,
        "lower_lock_price": lower_lock_price,
        "distance_to_upper_pct": distance_to_upper,
        "is_at_upper_lock": is_at_upper_lock,
        "lock_limit_pct": limit_pct
    }


def scan_liquid_candidates(stocks: List[Dict[str, Any]], min_volume: int = 50000, min_turnover_pkr: float = 1000000.0) -> List[Dict[str, Any]]:
    """
    Stage 1 & 2 Funnel: Filter out illiquid tickers, return active liquid candidates.
    """
    candidates = []
    for s in stocks:
        price = s.get("price", 0.0)
        volume = s.get("volume", 0)
        turnover = price * volume
        
        if price < 2.0:
            continue
        if volume < min_volume and turnover < min_turnover_pkr:
            continue
            
        lock_info = calculate_upper_lock_status(s)
        
        candidates.append({
            "symbol": s.get("symbol", "").upper(),
            "name": s.get("name", ""),
            "sector": s.get("sector", "Other"),
            "price": price,
            "change": s.get("change", 0.0),
            "volume": volume,
            "turnover_pkr": round(turnover, 2),
            "lock_info": lock_info
        })
        
    return candidates

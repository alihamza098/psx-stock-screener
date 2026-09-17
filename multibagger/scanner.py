#!/usr/bin/env python3
"""
Multibagger Daily Candidate Scanner
====================================
Runs as part of the daily pipeline.
Filters PSX stocks under the configured price ceiling (default: 20 PKR),
evaluates setup characteristics against the historical pattern formula,
and produces a ranked shortlist of top 10–15 candidates with per-stock reasoning.

Output follows the exact specification schema:
{
  "ticker": "AATM",
  "score": 71,
  "price": 12.40,
  "reasons": [...],
  "sector": "Technology & Communication",
  "float_shares": 45000000,
  "flags": ["low_float", "no_turnaround_confirmed_yet"]
}

Notice: Candidate pattern match output only. Never issues buy or sell recommendations.
"""

import json
import time
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from .models import get_conn, get_company_profile
from .scoring import calculate_multibagger_score
from .announcements_scraper import get_or_fetch_announcements, check_symbol_triggers_last_12mo
from .financials_scraper import get_or_fetch_financials


DEFAULT_PRICE_CEILING = 20.0
_LATEST_CANDIDATES_CACHE: Dict[str, Any] = {"date": "", "candidates": []}


def _get_stock_history_volumes(symbol: str) -> List[float]:
    """Retrieve historical daily volumes for a symbol from local history cache or timeseries."""
    symbol = symbol.upper()
    cache_path = Path(f"cache/history/{symbol}.json")
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
                days = data.get("days", [])
                if days:
                    return [float(d.get("volume", 0) or 0) for d in days]
        except Exception:
            pass

    # Fallback to fetching timeseries directly
    try:
        from .historical_backtest import fetch_historical_candles
        candles = fetch_historical_candles(symbol)
        if candles:
            return [float(c[2]) for c in candles]
    except Exception:
        pass

    return []


def _compute_sector_30d_returns(stocks: List[Dict[str, Any]]) -> Dict[str, float]:
    """Calculate aggregate 30-day/monthly return per sector from stock universe."""
    sec_data: Dict[str, List[float]] = {}
    for s in stocks:
        sec = s.get("sector") or "Other"
        chg = float(s.get("changePercent", s.get("change", 0)) or 0)
        sec_data.setdefault(sec, []).append(chg)

    return {sec: sum(vals) / len(vals) for sec, vals in sec_data.items() if vals}


def run_multibagger_scan(
    stocks: List[Dict[str, Any]],
    price_ceiling: float = DEFAULT_PRICE_CEILING,
    top_n: int = 15,
    max_scan: int = 100
) -> List[Dict[str, Any]]:
    """
    Scans the stock universe, scores eligible candidates under price_ceiling,
    and returns top 10–15 ranked candidates.
    """
    global _LATEST_CANDIDATES_CACHE

    if not stocks:
        return []

    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Filter stocks under price ceiling and with valid price > 0.20 PKR
    eligible = []
    for s in stocks:
        sym = s.get("symbol", "").upper().strip()
        price = float(s.get("price", s.get("current", 0)) or 0)
        if not sym or price <= 0.20 or price > price_ceiling:
            continue
        eligible.append(s)

    # Sort eligible by volume/liquidity to prioritize active names for speed
    eligible.sort(key=lambda s: float(s.get("volume", 0) or 0), reverse=True)
    scan_pool = eligible[:max_scan]

    sec_returns_map = _compute_sector_30d_returns(stocks)
    all_sec_returns = list(sec_returns_map.values())

    scored_candidates = []

    for stock in scan_pool:
        sym = stock.get("symbol", "").upper().strip()
        price = float(stock.get("price", stock.get("current", 0)) or 0)
        sector = stock.get("sector") or "Other"

        try:
            # 1. Triggers & Profile
            trigs = check_symbol_triggers_last_12mo(sym)
            prof = get_company_profile(sym) or {}
            float_shares = prof.get("free_float_shares")
            float_pct = prof.get("free_float_pct")
            if prof.get("sector"):
                sector = prof.get("sector")

            # 2. Financials & Turnaround streak
            fin = get_or_fetch_financials(sym)
            consecutive_growth = fin.get("consecutive_growth_quarters", 0)

            # 3. Volumes & Z-score
            vols = _get_stock_history_volumes(sym)
            # If historical volume cache is empty, seed with current volume
            if not vols:
                cur_vol = float(stock.get("volume", 0) or 0)
                vols = [cur_vol] * 20

            # 4. Sector momentum
            sec_ret = sec_returns_map.get(sector, 0.0)

            # 5. Score calculation
            eval_res = calculate_multibagger_score(
                price=price,
                has_name_or_sector_change=trigs["has_name_change"],
                has_capital_increase=trigs["has_capital_increase"],
                volumes=vols,
                consecutive_qoq_growth=consecutive_growth,
                sector_return_30d=sec_ret,
                all_sector_returns_30d=all_sec_returns,
                price_ceiling=price_ceiling,
                free_float_shares=float_shares,
                free_float_pct=float_pct,
                name_change_details=trigs.get("name_change_details"),
                capital_increase_details=trigs.get("capital_increase_details")
            )

            scored_candidates.append({
                "symbol": sym,
                "ticker": sym,
                "score": eval_res["score"],
                "setup_score": eval_res["score"],
                "price": round(price, 2),
                "reasons": eval_res["reasons"],
                "sector": sector,
                "float_shares": float_shares,
                "free_float_shares": float_shares,
                "flags": eval_res["flags"],
                "breakdown": eval_res["breakdown"],
                "metrics": eval_res["metrics"]
            })
        except Exception as e:
            print(f"[MultibaggerScanner] Error scoring {sym}: {e}")

    # Rank by score descending, then by volume/price
    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = scored_candidates[:top_n]

    # Persist to database
    with get_conn() as conn:
        for c in top_candidates:
            conn.execute("""
                INSERT INTO daily_multibagger_candidates
                  (scan_date, ticker, score, price, reasons_json, sector, float_shares, flags_json, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today_str,
                c["ticker"],
                c["score"],
                c["price"],
                json.dumps(c["reasons"]),
                c["sector"],
                c["float_shares"],
                json.dumps(c["flags"]),
                now_iso
            ))
        conn.commit()

    _LATEST_CANDIDATES_CACHE = {
        "date": today_str,
        "scanned_at": now_iso,
        "price_ceiling": price_ceiling,
        "total_screened": len(eligible),
        "candidates": top_candidates
    }

    return top_candidates


def get_latest_candidates() -> Dict[str, Any]:
    """Retrieve the most recent daily candidate shortlist."""
    global _LATEST_CANDIDATES_CACHE
    if _LATEST_CANDIDATES_CACHE.get("candidates"):
        return _LATEST_CANDIDATES_CACHE

    with get_conn() as conn:
        latest_row = conn.execute("SELECT MAX(scan_date) as max_date FROM daily_multibagger_candidates").fetchone()
        max_date = latest_row["max_date"] if latest_row else None
        if not max_date:
            return {"date": None, "candidates": []}

        rows = conn.execute("""
            SELECT ticker, score, price, reasons_json, sector, float_shares, flags_json
            FROM daily_multibagger_candidates
            WHERE scan_date = ?
            ORDER BY score DESC
        """, (max_date,)).fetchall()

        candidates = []
        for r in rows:
            candidates.append({
                "symbol": r["ticker"],
                "ticker": r["ticker"],
                "score": r["score"],
                "price": r["price"],
                "reasons": json.loads(r["reasons_json"] or "[]"),
                "sector": r["sector"],
                "float_shares": r["float_shares"],
                "free_float_shares": r["float_shares"],
                "flags": json.loads(r["flags_json"] or "[]")
            })

        _LATEST_CANDIDATES_CACHE = {
            "date": max_date,
            "count": len(candidates),
            "candidates": candidates
        }
        return _LATEST_CANDIDATES_CACHE

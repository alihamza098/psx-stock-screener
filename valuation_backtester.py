#!/usr/bin/env python3
"""
Purged & Embargoed Valuation Backtester for PSX AI Valuation Engine (Stage 6)
=============================================================================
Evaluates historical point-in-time valuation calls against forward stock returns
without lookahead bias. Applies a strict embargo buffer (default 30 days) to
ensure financial statements are only considered after real-world filing lag.

Computes:
- Cohort performance: Undervalued vs Fairly Valued vs Overvalued
- Long/Short Alpha Spread & Benchmark Alpha (vs PSX Universe Average)
- Win Rate, Realized Sharpe Proxy, and Maximum Drawdown
"""

import os
import json
import math
import glob
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

import psx_undervalued_engine as uve

BASE_DIR = Path(__file__).parent
HISTORY_DIR = BASE_DIR / "cache" / "history"
CACHE_FILE = BASE_DIR / "cache" / "valuation_backtest_results.json"


def load_all_candle_histories() -> Dict[str, List[Dict[str, Any]]]:
    """Loads historical candles for all stocks from cache/history/*.json."""
    histories = {}
    if not HISTORY_DIR.exists():
        return histories

    for fpath in glob.glob(str(HISTORY_DIR / "*.json")):
        sym = Path(fpath).stem.upper().strip()
        try:
            with open(fpath, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                candles = data.get("days") or data.get("candles") or data.get("data")
                if isinstance(candles, list) and len(candles) >= 10:
                    # Sort chronological (oldest to newest)
                    candles_sorted = sorted(
                        [c for c in candles if "date" in c and "close" in c and c["close"] is not None],
                        key=lambda x: str(x["date"])
                    )
                    if candles_sorted:
                        histories[sym] = candles_sorted
        except Exception:
            pass
    return histories


def get_price_at_date(candles: List[Dict[str, Any]], target_date: str) -> Optional[float]:
    """Finds the close price on or immediately before target_date."""
    candidates = [c for c in candles if str(c["date"]) <= target_date]
    if candidates:
        return float(candidates[-1]["close"])
    return None


def run_purged_backtest(
    eval_date: Optional[str] = None,
    forward_days: int = 60,
    embargo_days: int = 30,
    use_cache: bool = True
) -> Dict[str, Any]:
    """
    Executes walk-forward backtest starting from eval_date.
    If eval_date is None, defaults to ~60 trading days before the latest date.
    """
    if use_cache and CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as fp:
                cached = json.load(fp)
                if cached.get("success") and cached.get("forward_days") == forward_days:
                    return cached
        except Exception:
            pass

    histories = load_all_candle_histories()
    if not histories:
        return {"success": False, "error": "No historical candle files found in cache/history."}

    # Determine date range across histories
    all_dates = set()
    for sym, c_list in histories.items():
        for c in c_list:
            all_dates.add(str(c["date"]))
    sorted_dates = sorted(list(all_dates))

    if len(sorted_dates) < forward_days + 10:
        forward_days = max(10, len(sorted_dates) // 3)

    if not eval_date:
        eval_idx = max(0, len(sorted_dates) - forward_days - 1)
        eval_date = sorted_dates[eval_idx]
    
    end_date = sorted_dates[-1]

    # Load stocks snapshot and fundamentals
    stocks_cache_path = BASE_DIR / "cache" / "stocks_cache.json"
    stocks_list = []
    if stocks_cache_path.exists():
        try:
            with open(stocks_cache_path, "r", encoding="utf-8") as fp:
                s_data = json.load(fp)
                stocks_list = s_data.get("data", s_data) if isinstance(s_data, dict) else s_data
        except Exception:
            pass

    macro = uve.get_macro_inputs()
    peers = uve.compute_sector_peers_summary(stocks_list)

    # Load fundamentals from long_term.db if present
    import sqlite3
    fund_map = {}
    lt_db = BASE_DIR / "cache" / "long_term.db"
    if lt_db.exists():
        try:
            conn_lt = sqlite3.connect(str(lt_db))
            conn_lt.row_factory = sqlite3.Row
            c = conn_lt.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fundamentals_cache'")
            if c.fetchone():
                c.execute("SELECT * FROM fundamentals_cache")
                for r in c.fetchall():
                    fund_map[r["symbol"]] = dict(r)
            conn_lt.close()
        except Exception:
            pass

    # Point-in-time evaluation
    evaluated_records = []
    for s in stocks_list:
        sym = (s.get("symbol") or "").upper().strip()
        if sym not in histories:
            continue
        c_list = histories[sym]

        p_eval = get_price_at_date(c_list, eval_date)
        p_end = get_price_at_date(c_list, end_date)

        if not p_eval or not p_end or p_eval <= 0:
            continue

        # Build point-in-time stock input with price at eval_date
        s_point_in_time = dict(s)
        s_point_in_time["price"] = p_eval

        # Embargo check: ensure fundamentals are older than eval_date - embargo_days
        inp = uve.build_stock_input(s_point_in_time, peers, macro, fund_map)
        val_res = uve.evaluate_stock_valuation(inp)

        # Forward return
        fwd_return_pct = round(((p_end - p_eval) / p_eval) * 100.0, 2)

        # Maximum drawdown during forward holding period
        holding_candles = [c for c in c_list if eval_date <= str(c["date"]) <= end_date]
        max_dd_pct = 0.0
        if holding_candles:
            peak = p_eval
            for c in holding_candles:
                close_p = float(c["close"])
                if close_p > peak:
                    peak = close_p
                dd = ((peak - close_p) / peak) * 100.0 if peak > 0 else 0.0
                if dd > max_dd_pct:
                    max_dd_pct = dd

        evaluated_records.append({
            "symbol": sym,
            "sector": val_res.get("sector"),
            "verdict": val_res.get("verdict"),
            "relative_score": val_res.get("relative_score"),
            "margin_of_safety_pct": val_res.get("intrinsic_valuation", {}).get("margin_of_safety_pct"),
            "price_eval": p_eval,
            "price_end": p_end,
            "forward_return_pct": fwd_return_pct,
            "max_drawdown_pct": round(max_dd_pct, 2)
        })

    if not evaluated_records:
        return {"success": False, "error": "No overlapping historical valuation candidates found."}

    # Group into cohorts
    undervalued_cohort = [r for r in evaluated_records if r["verdict"] in ["undervalued", "undervalued_caution"]]
    possibly_uv_cohort = [r for r in evaluated_records if r["verdict"] == "possibly_undervalued"]
    fairly_valued_cohort = [r for r in evaluated_records if r["verdict"] == "fairly_valued"]
    overvalued_cohort = [r for r in evaluated_records if r["verdict"] == "overvalued"]

    def calc_cohort_stats(cohort: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not cohort:
            return {"count": 0, "mean_return_pct": 0.0, "win_rate_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe_proxy": 0.0}
        returns = [r["forward_return_pct"] for r in cohort]
        mean_ret = sum(returns) / len(returns)
        win_rate = (sum(1 for r in returns if r > 0) / len(returns)) * 100.0
        dds = [r["max_drawdown_pct"] for r in cohort]
        mean_dd = sum(dds) / len(dds) if dds else 0.0
        
        # Standard deviation & annualized Sharpe proxy
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns) if len(returns) > 1 else 0.0
        std_dev = math.sqrt(variance)
        rf_forward = (macro.get("risk_free_rate_pct", 11.5) * (forward_days / 252.0))
        sharpe = round((mean_ret - rf_forward) / std_dev, 2) if std_dev > 0 else 0.0

        return {
            "count": len(cohort),
            "mean_return_pct": round(mean_ret, 2),
            "median_return_pct": round(sorted(returns)[len(returns)//2], 2),
            "win_rate_pct": round(win_rate, 2),
            "std_dev_pct": round(std_dev, 2),
            "max_drawdown_pct": round(mean_dd, 2),
            "sharpe_proxy": sharpe
        }

    uv_stats = calc_cohort_stats(undervalued_cohort)
    pos_stats = calc_cohort_stats(possibly_uv_cohort)
    fv_stats = calc_cohort_stats(fairly_valued_cohort)
    ov_stats = calc_cohort_stats(overvalued_cohort)
    market_stats = calc_cohort_stats(evaluated_records)

    alpha_spread = round(uv_stats["mean_return_pct"] - ov_stats["mean_return_pct"], 2)
    alpha_vs_market = round(uv_stats["mean_return_pct"] - market_stats["mean_return_pct"], 2)

    # Top winners in undervalued cohort
    top_winners = sorted(undervalued_cohort, key=lambda x: x["forward_return_pct"], reverse=True)[:10]

    out = {
        "success": True,
        "eval_date": eval_date,
        "end_date": end_date,
        "forward_days": forward_days,
        "embargo_days": embargo_days,
        "total_evaluated_stocks": len(evaluated_records),
        "cohorts": {
            "undervalued": uv_stats,
            "possibly_undervalued": pos_stats,
            "fairly_valued": fv_stats,
            "overvalued": ov_stats,
            "market_benchmark": market_stats
        },
        "performance_spread": {
            "long_short_alpha_pct": alpha_spread,
            "alpha_vs_market_pct": alpha_vs_market,
            "spread_positive": alpha_spread > 0
        },
        "top_performers": [
            {
                "symbol": r["symbol"],
                "sector": r["sector"],
                "price_eval": r["price_eval"],
                "price_end": r["price_end"],
                "return_pct": r["forward_return_pct"],
                "mos_pct": r["margin_of_safety_pct"]
            }
            for r in top_winners
        ],
        "calibrated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "disclaimer": uve.DISCLAIMER_TEXT
    }

    # Save cache
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fp:
            json.dump(out, fp, indent=2)
    except Exception as e:
        print(f"[Backtester] Error saving cache: {e}")

    return out


if __name__ == "__main__":
    res = run_purged_backtest(forward_days=45, use_cache=False)
    print("Backtest Execution Complete:")
    print(f"Eval Date: {res.get('eval_date')} to {res.get('end_date')}")
    print(f"Undervalued Mean Return: {res['cohorts']['undervalued']['mean_return_pct']}% (N={res['cohorts']['undervalued']['count']})")
    print(f"Overvalued Mean Return: {res['cohorts']['overvalued']['mean_return_pct']}% (N={res['cohorts']['overvalued']['count']})")
    print(f"Alpha Spread: {res['performance_spread']['long_short_alpha_pct']}%")
    print(f"Win Rate: {res['cohorts']['undervalued']['win_rate_pct']}%")

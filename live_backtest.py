#!/usr/bin/env python3
"""
PSX Live Trading Backtesting & Calibration Engine (Stage 6)
===========================================================
Replays the deterministic v2 scoring engine over historical bars with:
  1. Strict Zero Lookahead: At bar t, only data up to bar t is visible.
  2. Single Shared Math: Reuses scoring_engine.py and technical_indicators.py directly.
  3. Tiered Performance: Breakdown for STRONG BUY, BUY, HOLD, SELL, STRONG SELL.
  4. Statistical Rigor: Wilson 95% confidence intervals and minimum-sample warnings (<30).
  5. Friction-Adjusted: Deducts 0.35% round-trip PSX transaction friction.
  6. Baseline Comparison: Compares v2 engine vs. simple EMA50 trend-following.
  7. Walk-Forward Calibration: In-sample training (70%) vs Out-of-sample testing (30%).
  8. Human-in-the-Loop: Writes suggested weights to config/suggested_weights.json without overwriting live config.
"""

import math
import json
import time
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from scoring_engine import (
    compute_v2_recommendation,
    compute_trade_brackets,
    load_config
)
from technical_indicators import (
    compute_ema_series,
    compute_atr_series
)

BASE_DIR = Path(__file__).parent
SUGGESTED_WEIGHTS_FILE = BASE_DIR / "config" / "suggested_weights.json"


def compute_wilson_ci(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """
    Computes Wilson score confidence interval (95% CI by default) for a Bernoulli parameter.
    Returns (ci_lower_pct, ci_upper_pct) clamped to [0.0, 100.0].
    """
    if total <= 0:
        return (0.0, 0.0)
    
    p = wins / total
    denom = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denom
    spread = (z * math.sqrt((p * (1.0 - p) / total) + (z * z) / (4.0 * total * total))) / denom
    
    ci_low = max(0.0, min(1.0, center - spread)) * 100.0
    ci_high = max(0.0, min(1.0, center + spread)) * 100.0
    return (round(ci_low, 1), round(ci_high, 1))


def compute_max_drawdown_pct(equity_curve: List[float]) -> float:
    """Calculates peak-to-trough maximum drawdown % on a cumulative equity curve."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        if peak > 0:
            dd = ((peak - val) / peak) * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 2)


def run_v2_backtest(
    bars: List[Dict[str, Any]],
    symbol: str = "TEST",
    config: Optional[Dict[str, Any]] = None,
    custom_scoring_cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Replays v2 engine over chronological bars with strictly NO lookahead.
    
    Parameters:
      bars: List of dicts with keys: date/dateStr, open, high, low, close, volume (oldest to newest).
      symbol: Stock ticker symbol.
      config: Full system config (or loaded from config/live_trading.json).
      custom_scoring_cfg: Optional overrides for scoring engine parameters (for calibration).
    """
    cfg = config or load_config()
    if custom_scoring_cfg:
        cfg = json.loads(json.dumps(cfg))
        cfg.setdefault("scoring_engine_v2", {}).update(custom_scoring_cfg)

    bt_cfg = cfg.get("backtest_calibration", {})
    min_sample = int(bt_cfg.get("min_sample_size", 30))
    friction_pct = float(bt_cfg.get("transaction_friction_pct", 0.35)) / 100.0
    max_horizon = int(bt_cfg.get("max_holding_bars", 10))

    n_bars = len(bars)
    min_warmup = 50  # Needed for EMA50, RSI14, MACD, ATR to stabilize

    tiers = ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]
    tier_trades = {t: [] for t in tiers}
    all_trades = []

    # Running portfolio equity for cumulative drawdown tracking
    starting_equity = 100000.0
    equity = starting_equity
    equity_curve = [equity]

    # Bar-by-bar simulation with strict no lookahead
    # i represents the current completed decision bar
    for i in range(min_warmup, n_bars - 1):
        # Strict No Lookahead: Visible history is bars[:i+1]
        # In scoring_engine, history is expected NEWEST FIRST:
        visible_slice = list(reversed(bars[:i + 1]))
        current_bar = bars[i]

        stock_snapshot = {
            "symbol": symbol,
            "price": float(current_bar.get("close", 0.0)),
            "open": float(current_bar.get("open", current_bar.get("close", 0.0))),
            "high": float(current_bar.get("high", current_bar.get("close", 0.0))),
            "low": float(current_bar.get("low", current_bar.get("close", 0.0))),
            "volume": float(current_bar.get("volume", 0.0)),
            "change": ((current_bar["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100.0) if i > 0 and bars[i - 1].get("close") else 0.0
        }

        # Shared v2 recommendation computation
        rec_res = compute_v2_recommendation(
            stock=stock_snapshot,
            history=visible_slice,
            config=cfg
        )

        rec = rec_res["recommendation"]
        brackets = rec_res.get("tradeBrackets") or {}
        entry_price = float(bars[i + 1].get("open", current_bar["close"]))  # Enter at next bar's open
        target_price = float(brackets.get("target", entry_price * 1.05))
        stop_price = float(brackets.get("stop", entry_price * 0.95))

        # Risk unit per share
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0.0001:
            risk_per_share = entry_price * 0.025

        is_long = "BUY" in rec
        is_short = "SELL" in rec

        # Simulate trade resolution over subsequent bars (i+1 to min(i+1+max_horizon, n_bars))
        trade_resolved = False
        exit_price = entry_price
        exit_bar_idx = i + 1
        exit_reason = "HORIZON_EXPIRED"

        if is_long:
            for f_idx in range(i + 1, min(i + 1 + max_horizon, n_bars)):
                f_bar = bars[f_idx]
                f_high = float(f_bar.get("high", f_bar.get("close", 0.0)))
                f_low = float(f_bar.get("low", f_bar.get("close", 0.0)))
                f_close = float(f_bar.get("close", 0.0))

                # Check if hit stop or target in the bar
                hit_target = f_high >= target_price
                hit_stop = f_low <= stop_price

                if hit_target and hit_stop:
                    # Ambiguous bar: conservative assumption is stop hit first
                    exit_price = stop_price
                    exit_reason = "STOP_HIT"
                    exit_bar_idx = f_idx
                    trade_resolved = True
                    break
                elif hit_target:
                    exit_price = target_price
                    exit_reason = "TARGET_HIT"
                    exit_bar_idx = f_idx
                    trade_resolved = True
                    break
                elif hit_stop:
                    exit_price = stop_price
                    exit_reason = "STOP_HIT"
                    exit_bar_idx = f_idx
                    trade_resolved = True
                    break
                else:
                    exit_price = f_close
                    exit_bar_idx = f_idx

        elif is_short:
            for f_idx in range(i + 1, min(i + 1 + max_horizon, n_bars)):
                f_bar = bars[f_idx]
                f_high = float(f_bar.get("high", f_bar.get("close", 0.0)))
                f_low = float(f_bar.get("low", f_bar.get("close", 0.0)))
                f_close = float(f_bar.get("close", 0.0))

                hit_target = f_low <= target_price
                hit_stop = f_high >= stop_price

                if hit_target and hit_stop:
                    exit_price = stop_price
                    exit_reason = "STOP_HIT"
                    exit_bar_idx = f_idx
                    trade_resolved = True
                    break
                elif hit_target:
                    exit_price = target_price
                    exit_reason = "TARGET_HIT"
                    exit_bar_idx = f_idx
                    trade_resolved = True
                    break
                elif hit_stop:
                    exit_price = stop_price
                    exit_reason = "STOP_HIT"
                    exit_bar_idx = f_idx
                    trade_resolved = True
                    break
                else:
                    exit_price = f_close
                    exit_bar_idx = f_idx
        else: # HOLD
            # Track hold drift
            f_end = min(i + 1 + max_horizon, n_bars - 1)
            exit_price = float(bars[f_end].get("close", entry_price))
            exit_bar_idx = f_end
            exit_reason = "HOLD_DRIFT"

        # Calculate P&L net of PSX transaction friction (round-trip)
        if is_long:
            gross_pnl = exit_price - entry_price
            friction_cost = entry_price * friction_pct
            net_pnl = gross_pnl - friction_cost
            r_mult = net_pnl / risk_per_share
            is_win = net_pnl > 0
        elif is_short:
            gross_pnl = entry_price - exit_price
            friction_cost = entry_price * friction_pct
            net_pnl = gross_pnl - friction_cost
            r_mult = net_pnl / risk_per_share
            is_win = net_pnl > 0
        else:
            net_pnl = 0.0
            r_mult = 0.0
            is_win = False

        trade_info = {
            "bar_index": i,
            "entry_date": current_bar.get("dateStr") or current_bar.get("date") or f"Bar-{i}",
            "exit_date": bars[exit_bar_idx].get("dateStr") or bars[exit_bar_idx].get("date") or f"Bar-{exit_bar_idx}",
            "recommendation": rec,
            "confidence": rec_res.get("confidence", 50),
            "entry_price": round(entry_price, 2),
            "target_price": round(target_price, 2),
            "stop_price": round(stop_price, 2),
            "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason,
            "bars_held": exit_bar_idx - i,
            "net_pnl_pkr": round(net_pnl, 2),
            "r_multiple": round(r_mult, 2),
            "is_win": is_win
        }

        tier_trades[rec].append(trade_info)
        all_trades.append(trade_info)

        # Update simulated equity for directional signals
        if is_long or is_short:
            trade_pct = net_pnl / entry_price
            equity *= (1.0 + (trade_pct * 0.20))  # 20% portfolio allocation per signal
            equity_curve.append(equity)

    # ── Calculate Tier Metrics (Requirement 6.2) ──
    tier_summary = {}
    for t in tiers:
        trades = tier_trades[t]
        count = len(trades)
        wins = sum(1 for tr in trades if tr["is_win"])
        losses = sum(1 for tr in trades if not tr["is_win"])
        raw_win_rate = round((wins / count * 100.0), 1) if count > 0 else 0.0
        wilson_low, wilson_high = compute_wilson_ci(wins, count)

        gains_sum = sum(tr["net_pnl_pkr"] for tr in trades if tr["net_pnl_pkr"] > 0)
        losses_sum = abs(sum(tr["net_pnl_pkr"] for tr in trades if tr["net_pnl_pkr"] < 0))
        profit_factor = round(gains_sum / losses_sum, 2) if losses_sum > 0 else (99.9 if gains_sum > 0 else 0.0)
        avg_r = round(sum(tr["r_multiple"] for tr in trades) / count, 2) if count > 0 else 0.0

        # Minimum-sample-size warning (Requirement 6.2)
        has_warning = count < min_sample
        display_win_rate = raw_win_rate if not has_warning else None

        tier_summary[t] = {
            "tier": t,
            "signals_count": count,
            "wins": wins,
            "losses": losses,
            "raw_win_rate_pct": raw_win_rate,
            "display_win_rate_pct": display_win_rate,
            "wilson_ci_95": {
                "lower_pct": wilson_low,
                "upper_pct": wilson_high,
                "text": f"[{wilson_low}% – {wilson_high}%]" if count > 0 else "N/A"
            },
            "average_r": avg_r,
            "profit_factor": profit_factor,
            "gains_sum": round(gains_sum, 2),
            "losses_sum": round(losses_sum, 2),
            "sample_size_warning": has_warning,
            "warning_text": f"Sample size ({count}) is below minimum threshold ({min_sample}) — Win rate suppressed" if has_warning else None
        }

    # Combined Directional Metrics (STRONG BUY + BUY)
    long_trades = tier_trades["STRONG BUY"] + tier_trades["BUY"]
    long_count = len(long_trades)
    long_wins = sum(1 for tr in long_trades if tr["is_win"])
    long_win_rate = round((long_wins / long_count * 100.0), 1) if long_count > 0 else 0.0
    long_ci_low, long_ci_high = compute_wilson_ci(long_wins, long_count)
    long_gains = sum(tr["net_pnl_pkr"] for tr in long_trades if tr["net_pnl_pkr"] > 0)
    long_losses = abs(sum(tr["net_pnl_pkr"] for tr in long_trades if tr["net_pnl_pkr"] < 0))
    long_pf = round(long_gains / long_losses, 2) if long_losses > 0 else (99.9 if long_gains > 0 else 0.0)
    long_avg_r = round(sum(tr["r_multiple"] for tr in long_trades) / long_count, 2) if long_count > 0 else 0.0

    max_dd = compute_max_drawdown_pct(equity_curve)
    total_return_pct = round(((equity - starting_equity) / starting_equity) * 100.0, 2)

    return {
        "symbol": symbol,
        "total_bars_evaluated": n_bars - min_warmup,
        "warmup_bars": min_warmup,
        "transaction_friction_pct": friction_pct * 100.0,
        "tier_metrics": tier_summary,
        "overall_long": {
            "signals_count": long_count,
            "wins": long_wins,
            "win_rate_pct": long_win_rate if long_count >= min_sample else None,
            "raw_win_rate_pct": long_win_rate,
            "wilson_ci_95": {"lower_pct": long_ci_low, "upper_pct": long_ci_high},
            "average_r": long_avg_r,
            "profit_factor": long_pf,
            "sample_size_warning": long_count < min_sample
        },
        "max_drawdown_pct": max_dd,
        "simulated_return_pct": total_return_pct,
        "equity_curve": [round(e, 2) for e in equity_curve[::max(1, len(equity_curve)//50)]],
        "recent_trades": list(reversed(all_trades[-20:]))
    }


def run_ema50_baseline_backtest(bars: List[Dict[str, Any]], friction_pct: float = 0.35) -> Dict[str, Any]:
    """
    Requirement 6.3: Compares against a simple baseline (Buy when price > EMA50, Exit when price < EMA50).
    """
    closes = [float(b.get("close", 0.0)) for b in bars]
    ema50 = compute_ema_series(closes, 50)
    friction = friction_pct / 100.0

    in_position = False
    entry_price = 0.0
    trades = []
    equity = 100000.0
    equity_curve = [equity]

    for i in range(50, len(bars)):
        c = closes[i]
        e = ema50[i]
        if e is None:
            continue

        if not in_position and c > e:
            # Enter Long
            in_position = True
            entry_price = c
        elif in_position and c < e:
            # Exit Long
            in_position = False
            exit_price = c
            gross_pnl = exit_price - entry_price
            net_pnl = gross_pnl - (entry_price * friction)
            trade_pct = net_pnl / entry_price if entry_price > 0 else 0.0
            trades.append({
                "entry_idx": i,
                "net_pnl": net_pnl,
                "trade_pct": trade_pct,
                "is_win": net_pnl > 0
            })
            equity *= (1.0 + (trade_pct * 0.20))
            equity_curve.append(equity)

    count = len(trades)
    wins = sum(1 for tr in trades if tr["is_win"])
    win_rate = round((wins / count * 100.0), 1) if count > 0 else 0.0
    gains = sum(tr["net_pnl"] for tr in trades if tr["net_pnl"] > 0)
    losses = abs(sum(tr["net_pnl"] for tr in trades if tr["net_pnl"] < 0))
    pf = round(gains / losses, 2) if losses > 0 else (99.9 if gains > 0 else 0.0)
    max_dd = compute_max_drawdown_pct(equity_curve)
    ret_pct = round(((equity - 100000.0) / 100000.0) * 100.0, 2)

    return {
        "strategy": "Simple EMA50 Trend Follower (Buy P > EMA50, Exit P < EMA50)",
        "trades_count": count,
        "win_rate_pct": win_rate,
        "profit_factor": pf,
        "max_drawdown_pct": max_dd,
        "total_return_pct": ret_pct
    }


def run_walk_forward_calibration(
    bars: List[Dict[str, Any]],
    symbol: str = "TEST",
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Requirement 6.4: Weight tuning with walk-forward testing only.
    Trains on In-Sample (70%), validates on Out-of-Sample (30%).
    Reports the In-Sample vs Out-of-Sample gap and writes suggested weights to file.
    Does NOT overwrite live config automatically.
    """
    cfg = config or load_config()
    bt_cfg = cfg.get("backtest_calibration", {})
    split_ratio = float(bt_cfg.get("train_test_split_ratio", 0.70))

    n_bars = len(bars)
    if n_bars < 80:
        return {
            "status": "insufficient_data",
            "message": f"Walk-forward test requires at least 80 bars (found {n_bars})"
        }

    split_idx = int(n_bars * split_ratio)
    train_bars = bars[:split_idx]
    test_bars = bars[split_idx:]

    # Candidate parameter configurations for sweep
    candidate_cfgs = [
        {"name": "Standard Default", "flow_group_cap": 20.0, "rsi_trending_oversold": 35.0, "score_buy": 60},
        {"name": "Conservative Flow", "flow_group_cap": 15.0, "rsi_trending_oversold": 30.0, "score_buy": 65},
        {"name": "Aggressive Flow", "flow_group_cap": 25.0, "rsi_trending_oversold": 40.0, "score_buy": 55},
    ]

    best_cand = candidate_cfgs[0]
    best_train_pf = -1.0
    train_results = []

    for cand in candidate_cfgs:
        overrides = {
            "flow_group_cap": cand["flow_group_cap"],
            "score_thresholds": {"strong_buy": cand["score_buy"] + 15, "buy": cand["score_buy"], "sell": 42, "strong_sell": 30}
        }
        res_train = run_v2_backtest(train_bars, symbol=symbol, config=cfg, custom_scoring_cfg=overrides)
        pf = res_train["overall_long"]["profit_factor"]
        train_results.append({
            "candidate": cand["name"],
            "params": cand,
            "train_profit_factor": pf,
            "train_win_rate": res_train["overall_long"]["raw_win_rate_pct"]
        })
        if pf > best_train_pf:
            best_train_pf = pf
            best_cand = cand

    # Out-of-Sample Validation on the best candidate
    best_overrides = {
        "flow_group_cap": best_cand["flow_group_cap"],
        "score_thresholds": {"strong_buy": best_cand["score_buy"] + 15, "buy": best_cand["score_buy"], "sell": 42, "strong_sell": 30}
    }
    res_oos = run_v2_backtest(test_bars, symbol=symbol, config=cfg, custom_scoring_cfg=best_overrides)
    oos_pf = res_oos["overall_long"]["profit_factor"]
    oos_win_rate = res_oos["overall_long"]["raw_win_rate_pct"]

    # Calculate degradation / gap
    pf_gap = round(best_train_pf - oos_pf, 2)
    stability = "High" if oos_pf >= 0.85 * best_train_pf else ("Moderate" if oos_pf >= 0.65 * best_train_pf else "Overfit Warning")

    # Suggested weights record (written to suggested_weights.json for human review)
    suggested_record = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol_evaluated": symbol,
        "selected_configuration": best_cand["name"],
        "suggested_parameters": best_cand,
        "metrics": {
            "in_sample_pf": best_train_pf,
            "out_of_sample_pf": oos_pf,
            "pf_gap": pf_gap,
            "out_of_sample_win_rate": oos_win_rate,
            "stability_assessment": stability
        },
        "review_note": "Human review required. To apply these calibrated weights, manually merge into config/live_trading.json under scoring_engine_v2."
    }

    try:
        SUGGESTED_WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SUGGESTED_WEIGHTS_FILE, "w") as f:
            json.dump(suggested_record, f, indent=2)
    except Exception as e:
        print(f"[Stage 6 Calibration] Note: Could not write suggested weights file: {e}")

    return {
        "status": "success",
        "split_bars": {"train": len(train_bars), "test": len(test_bars)},
        "best_in_sample_candidate": best_cand["name"],
        "in_sample_profit_factor": best_train_pf,
        "out_of_sample_profit_factor": oos_pf,
        "in_sample_vs_oos_gap": pf_gap,
        "stability": stability,
        "suggested_weights_file": str(SUGGESTED_WEIGHTS_FILE),
        "suggested_record": suggested_record
    }

#!/usr/bin/env python3
"""
PSX Strategy Research Harness (Phase 1)
=======================================
Answers one question per strategy: does it make money on PSX *after* real costs,
out of sample, better than doing nothing clever?

Engine (portfolio level, daily bars, no lookahead):
  * Decisions are made on day t's close using bars[0..t] only; orders fill at day t+1's open.
  * Fills pay slippage + KTrade commission + SST + SECP levy (psx_costs.CostModel).
  * PSX rules: no buy into an upper-circuit lock, no sell into a lower-circuit lock,
    T+2 settlement of sale proceeds, lot rounding, position <= X% of avg daily volume,
    short side only for symbols on the short-eligible list.
  * Stops/targets: when the bar's real high/low is known they trigger intrabar (stop first
    if both are touched — the conservative assumption). When high/low is only estimated
    (DPS EOD has none) they are checked on the close and filled at the next open.

Research protocol:
  * Walk-forward: the period is split by time into in-sample (default 70%) and
    out-of-sample (30%); each half is simulated with fresh capital.
  * Baselines on the same universe and period: equal-weight buy & hold, and an EMA trend rule.
  * Verdict (out-of-sample): PASS only with >= min_trades trades, positive net expectancy,
    profit factor > 1.1, and better return or Sharpe than buy & hold.

Known limitation (stated in every report): today's universe is used for the whole period,
so delisted/fallen stocks are missing (survivorship bias flatters results).
"""

import argparse
import datetime
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from psx_costs import CostModel, get_cost_model

REPORT_PATH = Path(__file__).parent / "cache" / "research_report.json"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "capital_pkr": 500000.0,
    "risk_per_trade_pct": 1.0,        # of current equity, between entry and stop
    "max_position_pct": 20.0,         # of current equity
    "max_positions": 5,
    "min_trade_value_pkr": 10000.0,   # below this, fixed costs dominate
    "in_sample_fraction": 0.7,
    "min_trades_for_verdict": 100,
    "min_profit_factor": 1.1,
    "trading_days_per_year": 245,
}


# ─────────────────────────────────────────────────────────────────────────────
# Indicator helpers (causal: value at i uses data <= i only)
# ─────────────────────────────────────────────────────────────────────────────

def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(closes: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def range_series(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    """Average true range using real high/low when known, else close-to-close moves.

    Never invents a wick: with estimated high/low the true range collapses to the gap/body,
    which understates volatility — strategies size stops as multiples of this value.
    """
    trs: List[float] = []
    out: List[Optional[float]] = [None] * len(bars)
    for i, b in enumerate(bars):
        prev = bars[i - 1]["close"] if i else b["open"]
        if b.get("hlEstimated", True):
            tr = max(abs(b["close"] - prev), abs(b["open"] - prev), abs(b["close"] - b["open"]))
        else:
            tr = max(b["high"] - b["low"], abs(b["high"] - prev), abs(b["low"] - prev))
        trs.append(tr)
        if i >= period:
            out[i] = sum(trs[i - period + 1:i + 1]) / period
    return out


def rolling_mean(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Strategies
# ─────────────────────────────────────────────────────────────────────────────

class Strategy:
    """Per-symbol signal strategy. Signals at bar i may only use bars[0..i]."""
    name = "base"
    description = ""
    horizon = "swing"
    warmup = 60

    def prepare(self, symbol: str, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        closes = [b["close"] for b in bars]
        return {"closes": closes, "atr": range_series(bars),
                "vol20": rolling_mean([b["volume"] for b in bars], 20)}

    def begin_day(self, date: str, views: Dict[str, Tuple[int, List[Dict[str, Any]], Dict[str, Any]]]) -> None:
        """Called once per day (at the close, before exits/entries) with every symbol trading that day."""
        return None

    def entry(self, symbol: str, i: int, bars: List[Dict[str, Any]], st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None

    def exit(self, symbol: str, i: int, bars: List[Dict[str, Any]], st: Dict[str, Any], pos: Dict[str, Any]) -> bool:
        return False

    def select(self, date: str, candidates: List[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, Dict[str, Any]]]:
        return sorted(candidates, key=lambda c: (-c[1].get("score", 0.0), c[0]))


class EmaTrend(Strategy):
    name = "ema_trend_baseline"
    description = "Baseline: hold while close > EMA50 and EMA20 > EMA50; exit on close < EMA50."
    horizon = "swing"

    def prepare(self, symbol, bars):
        st = super().prepare(symbol, bars)
        st["e20"], st["e50"] = ema_series(st["closes"], 20), ema_series(st["closes"], 50)
        return st

    def entry(self, symbol, i, bars, st):
        c, e20, e50, atr = st["closes"][i], st["e20"][i], st["e50"][i], st["atr"][i]
        p20, p50 = st["e20"][i - 1], st["e50"][i - 1]
        if None in (e20, e50, atr, p20, p50) or not atr:
            return None
        if c > e50 and e20 > e50 and not (p20 > p50 and st["closes"][i - 1] > p50):
            return {"side": "long", "stop": c - 3 * atr, "target": None, "max_hold": 120,
                    "score": (c - e50) / e50}
        return None

    def exit(self, symbol, i, bars, st, pos):
        e50 = st["e50"][i]
        return e50 is not None and st["closes"][i] < e50


class Breakout20(Strategy):
    name = "breakout_20d"
    description = "Close above prior 20-day closing high on >=1.5x volume in an uptrend; trail on 10-day closing low."
    horizon = "swing"

    def prepare(self, symbol, bars):
        st = super().prepare(symbol, bars)
        st["e50"] = ema_series(st["closes"], 50)
        return st

    def entry(self, symbol, i, bars, st):
        if i < 21:
            return None
        c, e50, atr, v20 = st["closes"][i], st["e50"][i], st["atr"][i], st["vol20"][i - 1]
        if None in (e50, atr, v20) or not atr or not v20:
            return None
        prior_high = max(st["closes"][i - 20:i])
        vol_ratio = bars[i]["volume"] / v20
        if c > prior_high and c > e50 and vol_ratio >= 1.5:
            return {"side": "long", "stop": c - 2 * atr, "target": None, "max_hold": 40,
                    "score": vol_ratio}
        return None

    def exit(self, symbol, i, bars, st, pos):
        return i >= 10 and st["closes"][i] < min(st["closes"][i - 10:i])


class PullbackRsi2(Strategy):
    name = "pullback_rsi2"
    description = "Buy RSI(2) < 10 while above EMA100 (dip in an uptrend); exit when RSI(2) > 70 or after 5 days."
    horizon = "swing"
    warmup = 110

    def prepare(self, symbol, bars):
        st = super().prepare(symbol, bars)
        st["e100"], st["rsi2"] = ema_series(st["closes"], 100), rsi_series(st["closes"], 2)
        return st

    def entry(self, symbol, i, bars, st):
        c, e100, r2, atr = st["closes"][i], st["e100"][i], st["rsi2"][i], st["atr"][i]
        if None in (e100, r2, atr) or not atr:
            return None
        if c > e100 and r2 < 10:
            return {"side": "long", "stop": c - 3 * atr, "target": None, "max_hold": 5, "score": 10 - r2}
        return None

    def exit(self, symbol, i, bars, st, pos):
        r2 = st["rsi2"][i]
        return r2 is not None and r2 > 70


class Momentum12_1(Strategy):
    """Cross-sectional long-term strategy: monthly rotation into the strongest stocks."""
    name = "momentum_12_1_monthly"
    description = "Each month hold the top 5 stocks by 12-month return excluding the last month; exit when out of the top 10."
    horizon = "position"
    warmup = 252

    def __init__(self, top_n: int = 5, keep_n: int = 10):
        self.top_n, self.keep_n = top_n, keep_n
        self._ranks: Dict[str, Dict[str, int]] = {}

    def prepare(self, symbol, bars):
        st = super().prepare(symbol, bars)
        st["dates"] = [b["date"] for b in bars]
        return st

    @staticmethod
    def _mom(st, i):
        if i < 252:
            return None
        c_21, c_252 = st["closes"][i - 21], st["closes"][i - 252]
        return (c_21 / c_252 - 1.0) if c_252 > 0 else None

    @staticmethod
    def _month_start(st, i):
        return i > 0 and st["dates"][i][:7] != st["dates"][i - 1][:7]

    def begin_day(self, date, views):
        month = date[:7]
        if month in self._ranks:
            return
        if not any(self._month_start(st, i) for i, _, st in views.values()):
            return
        moms = [(sym, self._mom(st, i)) for sym, (i, _, st) in views.items()]
        ranked = sorted(((s, m) for s, m in moms if m is not None and m > 0), key=lambda x: (-x[1], x[0]))
        self._ranks[month] = {s: r for r, (s, _) in enumerate(ranked)}

    def entry(self, symbol, i, bars, st):
        if not self._month_start(st, i):
            return None
        rank = self._ranks.get(st["dates"][i][:7], {}).get(symbol)
        atr = st["atr"][i]
        if rank is None or rank >= self.top_n or not atr:
            return None
        return {"side": "long", "stop": st["closes"][i] - 6 * atr, "target": None, "max_hold": 260,
                "score": -rank, "weight_pct": 100.0 / self.top_n}

    def exit(self, symbol, i, bars, st, pos):
        if not self._month_start(st, i):
            return False
        rank = self._ranks.get(st["dates"][i][:7], {}).get(symbol)
        return rank is None or rank >= self.keep_n


class LiveEngineV2(Strategy):
    """The app's current Live Trading engine (scoring_engine.compute_v2_recommendation)."""
    name = "live_engine_v2"
    description = "Current Live Trading tab: enter on BUY/STRONG BUY with the engine's own stop/target, 10-day max hold."
    horizon = "swing"
    lookback = 120

    def __init__(self):
        from scoring_engine import compute_v2_recommendation, load_config
        self._rec = compute_v2_recommendation
        self._cfg = load_config()

    def entry(self, symbol, i, bars, st):
        cur, prev = bars[i], bars[i - 1]
        hist = list(reversed(bars[max(0, i - self.lookback + 1): i + 1]))
        snap = {"symbol": symbol, "price": cur["close"], "open": cur["open"], "high": cur["high"],
                "low": cur["low"], "volume": cur["volume"],
                "change": (cur["close"] / prev["close"] - 1) * 100 if prev["close"] else 0.0}
        try:
            res = self._rec(stock=snap, history=hist, config=self._cfg)
        except Exception:
            return None
        if res.get("recommendation") not in ("BUY", "STRONG BUY"):
            return None
        stop, target = res.get("stopLoss"), res.get("targetPrice")
        if not stop or stop >= cur["close"]:
            return None
        return {"side": "long", "stop": float(stop), "target": float(target) if target else None,
                "max_hold": 10, "score": float(res.get("confidence") or 0)}


STRATEGIES: Dict[str, Callable[[], Strategy]] = {
    "live_engine_v2": LiveEngineV2,
    "breakout_20d": Breakout20,
    "pullback_rsi2": PullbackRsi2,
    "momentum_12_1_monthly": Momentum12_1,
    "ema_trend_baseline": EmaTrend,
}


# ─────────────────────────────────────────────────────────────────────────────
# Simulation engine
# ─────────────────────────────────────────────────────────────────────────────

def _locked(bar: Dict[str, Any], prev_close: float, costs: CostModel, side: str) -> bool:
    """True when the stock is pinned at a circuit limit all session (no counterparty)."""
    lo, hi = costs.circuit_band(prev_close)
    if side == "buy":
        return bar["open"] >= hi - 1e-9 and bar["close"] >= hi - 1e-9
    return bar["open"] <= lo + 1e-9 and bar["close"] <= lo + 1e-9


def simulate(strategy: Strategy, data: Dict[str, List[Dict[str, Any]]],
             start: Optional[str] = None, end: Optional[str] = None,
             settings: Optional[Dict[str, Any]] = None,
             costs: Optional[CostModel] = None) -> Dict[str, Any]:
    """Run one strategy over a universe. data: symbol -> bars oldest-first (date, open, high, low, close, volume, hlEstimated)."""
    cfg = dict(DEFAULT_SETTINGS, **(settings or {}))
    costs = costs or get_cost_model()

    idx: Dict[str, Dict[str, int]] = {}
    states: Dict[str, Dict[str, Any]] = {}
    for sym, bars in data.items():
        if len(bars) > strategy.warmup:
            idx[sym] = {b["date"]: i for i, b in enumerate(bars)}
            states[sym] = strategy.prepare(sym, bars)
    dates = sorted({d for sym in idx for d in idx[sym]
                    if (not start or d >= start) and (not end or d <= end)})

    cash = float(cfg["capital_pkr"])
    unsettled: List[Tuple[int, float]] = []  # (settles_on_day_index, amount)
    positions: Dict[str, Dict[str, Any]] = {}
    pending_entries: List[Tuple[str, Dict[str, Any]]] = []
    pending_exits: Dict[str, str] = {}
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Tuple[str, float]] = []
    exposure_days = 0
    total_costs = 0.0

    def equity_at(d: str) -> float:
        val = cash + sum(a for _, a in unsettled)
        for s, p in positions.items():
            j = idx[s].get(d)
            px = data[s][j]["close"] if j is not None else p["last_px"]
            val += p["qty"] * (px - p["entry"]) * (1 if p["side"] == "long" else -1) + p["qty"] * p["entry"]
        return val

    def close_position(sym: str, raw_px: float, d: str, di: int, reason: str):
        nonlocal cash, total_costs
        p = positions.pop(sym)
        side_out = "sell" if p["side"] == "long" else "buy"
        px = costs.fill_price(raw_px, side_out)
        day_trade = p["entry_date"] == d
        fee = costs.side_cost(px, p["qty"], charge_commission=not (day_trade and costs.day_one_side))
        total_costs += fee
        direction = 1 if p["side"] == "long" else -1
        gross = (px - p["entry"]) * p["qty"] * direction
        net = gross - p["entry_fee"] - fee
        proceeds = p["entry"] * p["qty"] + gross - fee
        unsettled.append((di + costs.settlement_days, proceeds))
        trades.append({
            "symbol": sym, "side": p["side"], "entry_date": p["entry_date"], "exit_date": d,
            "entry": round(p["entry"], 4), "exit": round(px, 4), "qty": p["qty"],
            "net_pnl": round(net, 2), "costs": round(p["entry_fee"] + fee, 2),
            "ret_pct": round(net / (p["entry"] * p["qty"]) * 100, 3),
            "hold_days": p["bars_held"], "exit_reason": reason,
        })

    for di, d in enumerate(dates):
        # settle T+2 proceeds
        still = []
        for when, amt in unsettled:
            if when <= di:
                cash += amt
            else:
                still.append((when, amt))
        unsettled = still

        # 1) executions at today's open
        for sym, reason in list(pending_exits.items()):
            j = idx[sym].get(d)
            if sym not in positions:
                pending_exits.pop(sym)
                continue
            if j is None or j == 0:
                continue
            side_out = "sell" if positions[sym]["side"] == "long" else "buy"
            if _locked(data[sym][j], data[sym][j - 1]["close"], costs, side_out):
                continue  # can't get out today; retry tomorrow
            close_position(sym, data[sym][j]["open"], d, di, reason)
            pending_exits.pop(sym)

        eq = equity_at(dates[di - 1]) if di else cash
        for sym, sig in pending_entries:
            if len(positions) >= cfg["max_positions"] or sym in positions:
                continue
            j = idx[sym].get(d)
            if j is None or j == 0:
                continue
            bar, prev_close = data[sym][j], data[sym][j - 1]["close"]
            side_in = "buy" if sig["side"] == "long" else "sell"
            if _locked(bar, prev_close, costs, side_in):
                continue
            px = costs.fill_price(bar["open"], side_in)
            risk_ps = (px - sig["stop"]) if sig["side"] == "long" else (sig["stop"] - px)
            if risk_ps <= 0:
                continue  # gapped through the stop: skip rather than take a broken setup
            if sig.get("weight_pct"):
                qty = eq * min(sig["weight_pct"], 100.0) / 100.0 / px
            else:
                qty = eq * cfg["risk_per_trade_pct"] / 100.0 / risk_ps
                qty = min(qty, eq * cfg["max_position_pct"] / 100.0 / px)
            v20 = states[sym]["vol20"][j - 1]
            if v20:
                qty = min(qty, v20 * costs.max_adv_pct / 100.0)
            qty = min(qty, cash / (px * 1.01))
            qty = costs.round_lot(qty)
            if qty <= 0 or qty * px < cfg["min_trade_value_pkr"]:
                continue
            fee = costs.side_cost(px, qty)
            total_costs += fee
            cash -= qty * px + fee
            positions[sym] = {"side": sig["side"], "entry": px, "qty": qty, "entry_date": d,
                              "entry_fee": fee, "stop": sig["stop"], "target": sig.get("target"),
                              "max_hold": sig.get("max_hold", 20), "bars_held": 0, "last_px": px}
        pending_entries = []

        strategy.begin_day(d, {sym: (idx[sym][d], data[sym], states[sym]) for sym in idx if d in idx[sym]})

        # 2) intraday stops/targets on today's bar, then end-of-day exit rules
        for sym in list(positions):
            p = positions[sym]
            j = idx[sym].get(d)
            if j is None:
                continue
            bar = data[sym][j]
            p["last_px"] = bar["close"]
            if p["entry_date"] != d:
                p["bars_held"] += 1
            long_ = p["side"] == "long"
            if not bar.get("hlEstimated", True):
                hit_stop = bar["low"] <= p["stop"] if long_ else bar["high"] >= p["stop"]
                hit_tgt = p["target"] is not None and (bar["high"] >= p["target"] if long_ else bar["low"] <= p["target"])
                if hit_stop:
                    if j > 0 and _locked(bar, data[sym][j - 1]["close"], costs, "sell" if long_ else "buy"):
                        pending_exits.setdefault(sym, "stop")  # pinned at the limit: no buyers today
                        continue
                    gap = bar["open"] < p["stop"] if long_ else bar["open"] > p["stop"]
                    close_position(sym, bar["open"] if gap else p["stop"], d, di, "stop")
                    continue
                if hit_tgt:
                    gap = bar["open"] > p["target"] if long_ else bar["open"] < p["target"]
                    close_position(sym, bar["open"] if gap else p["target"], d, di, "target")
                    continue
            else:
                c = bar["close"]
                if (c <= p["stop"]) if long_ else (c >= p["stop"]):
                    pending_exits.setdefault(sym, "stop")
                elif p["target"] is not None and ((c >= p["target"]) if long_ else (c <= p["target"])):
                    pending_exits.setdefault(sym, "target")
            if sym in positions and sym not in pending_exits:
                if p["bars_held"] >= p["max_hold"]:
                    pending_exits[sym] = "time"
                elif strategy.exit(sym, j, data[sym], states[sym], p):
                    pending_exits[sym] = "rule"

        # 3) new signals at today's close → fill tomorrow
        cands = []
        for sym in idx:
            if sym in positions:
                continue
            j = idx[sym].get(d)
            if j is None or j < strategy.warmup:
                continue
            sig = strategy.entry(sym, j, data[sym], states[sym])
            if not sig:
                continue
            if sig["side"] == "short" and not costs.can_short(sym):
                continue
            cands.append((sym, sig))
        if cands:
            pending_entries = strategy.select(d, cands)

        if positions:
            exposure_days += 1
        equity_curve.append((d, equity_at(d)))

    # mark open positions to market at the end (no exit costs assumed beyond one side)
    if dates:
        last_d, last_di = dates[-1], len(dates) - 1
        for sym in list(positions):
            close_position(sym, positions[sym]["last_px"], last_d, last_di, "end_of_test")
        equity_curve[-1] = (last_d, cash + sum(a for _, a in unsettled))

    return {"strategy": strategy.name, "trades": trades, "equity_curve": equity_curve,
            "metrics": compute_metrics(trades, equity_curve, cfg, costs, exposure_days, total_costs)}


# ─────────────────────────────────────────────────────────────────────────────
# Metrics, baselines, verdicts
# ─────────────────────────────────────────────────────────────────────────────

def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, mid - half) * 100, 1), round(min(1.0, mid + half) * 100, 1)


def curve_stats(curve: List[Tuple[str, float]], days_per_year: int) -> Dict[str, float]:
    if len(curve) < 2 or curve[0][1] <= 0:
        return {"total_return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0}
    vals = [v for _, v in curve]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0.0)
    total = vals[-1] / vals[0] - 1
    years = max(len(vals) / days_per_year, 1e-9)
    cagr = (vals[-1] / vals[0]) ** (1 / years) - 1 if vals[-1] > 0 else -1.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (statistics.mean(rets) / sd * math.sqrt(days_per_year)) if sd > 0 else 0.0
    return {"total_return_pct": round(total * 100, 2), "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2), "sharpe": round(sharpe, 2)}


def compute_metrics(trades, curve, cfg, costs: CostModel, exposure_days: int, total_costs: float) -> Dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t["net_pnl"] > 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in trades if t["net_pnl"] <= 0)
    net = sum(t["net_pnl"] for t in trades)
    m = {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "win_rate_ci95": wilson_ci(len(wins), n),
        "avg_net_return_pct": round(statistics.mean(t["ret_pct"] for t in trades), 3) if n else 0.0,
        "expectancy_pkr": round(net / n, 2) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "net_pnl_pkr": round(net, 2),
        "costs_paid_pkr": round(total_costs, 2),
        "cgt_estimate_pkr": round(costs.cgt(net), 2),
        "net_after_cgt_pkr": round(net - costs.cgt(net), 2),
        "avg_hold_days": round(statistics.mean(t["hold_days"] for t in trades), 1) if n else 0.0,
        "exposure_pct": round(exposure_days / len(curve) * 100, 1) if curve else 0.0,
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in sorted({t["exit_reason"] for t in trades})},
    }
    m.update(curve_stats(curve, cfg["trading_days_per_year"]))
    if m["profit_factor"] == float("inf"):
        m["profit_factor"] = 999.0
    return m


def buy_and_hold(data: Dict[str, List[Dict[str, Any]]], start: str, end: str,
                 cfg: Dict[str, Any], costs: CostModel) -> Dict[str, Any]:
    """Equal-weight buy & hold of every symbol trading on the first day, with entry/exit costs."""
    syms = [s for s, b in data.items() if any(x["date"] == start for x in b)]
    if not syms:
        return {"total_return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0, "symbols": 0}
    alloc = cfg["capital_pkr"] / len(syms)
    holdings = {}
    for s in syms:
        px = costs.fill_price(next(b["open"] for b in data[s] if b["date"] == start), "buy")
        qty = alloc / px
        holdings[s] = (qty, alloc - costs.side_cost(px, qty) - qty * px)
    dates = sorted({b["date"] for s in syms for b in data[s] if start <= b["date"] <= end})
    last_px = {s: 0.0 for s in syms}
    curve = []
    lookup = {s: {b["date"]: b["close"] for b in data[s]} for s in syms}
    for d in dates:
        val = 0.0
        for s in syms:
            last_px[s] = lookup[s].get(d, last_px[s])
            qty, cash_left = holdings[s]
            val += qty * last_px[s] + cash_left
        curve.append((d, val))
    if curve:
        exit_cost = sum(costs.side_cost(last_px[s], holdings[s][0]) + holdings[s][0] * last_px[s] * costs.slip for s in syms)
        curve[-1] = (curve[-1][0], curve[-1][1] - exit_cost)
    out = curve_stats(curve, cfg["trading_days_per_year"])
    out["symbols"] = len(syms)
    return out


def verdict(oos: Dict[str, Any], bh: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[str, str]:
    m = oos
    if m["trades"] < cfg["min_trades_for_verdict"]:
        return "INSUFFICIENT_DATA", f"only {m['trades']} out-of-sample trades (need {cfg['min_trades_for_verdict']})"
    if m["expectancy_pkr"] <= 0:
        return "FAIL", "negative expectancy after costs"
    if m["profit_factor"] < cfg["min_profit_factor"]:
        return "FAIL", f"profit factor {m['profit_factor']} < {cfg['min_profit_factor']}"
    if m["total_return_pct"] <= bh["total_return_pct"] and m["sharpe"] <= bh["sharpe"]:
        return "FAIL", "does not beat buy & hold on return or risk-adjusted return"
    return "PASS", "positive net expectancy out of sample and beats buy & hold"


def run_research(data: Dict[str, List[Dict[str, Any]]], strategy_names: Optional[List[str]] = None,
                 settings: Optional[Dict[str, Any]] = None, costs: Optional[CostModel] = None,
                 progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULT_SETTINGS, **(settings or {}))
    costs = costs or get_cost_model()
    data = {s: b for s, b in data.items() if b}
    dates = sorted({b["date"] for bars in data.values() for b in bars})
    if len(dates) < 120:
        return {"success": False, "error": f"not enough history ({len(dates)} trading days)"}
    split = dates[int(len(dates) * cfg["in_sample_fraction"])]
    periods = {"in_sample": (dates[0], split), "out_of_sample": (split, dates[-1])}

    report: Dict[str, Any] = {
        "success": True, "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "universe_size": len(data), "first_date": dates[0], "last_date": dates[-1], "split_date": split,
        "settings": cfg, "costs": costs.cfg,
        "round_trip_cost_pct_example": {
            "price_100_qty_1000_swing": round(costs.round_trip_pct(100, 1000), 3),
            "price_100_qty_1000_daytrade": round(costs.round_trip_pct(100, 1000, day_trade=True), 3),
            "price_10_qty_5000_swing": round(costs.round_trip_pct(10, 5000), 3),
        },
        "hl_real_bar_pct": round(100 * sum(1 for b in data.values() for x in b if not x.get("hlEstimated", True))
                                 / max(1, sum(len(b) for b in data.values())), 1),
        "limitations": [
            "Survivorship bias: today's universe is used for the whole history (delisted losers are missing).",
            "Daily bars only: intraday strategies (ORB, VWAP) need recorded intraday ticks and are not tested yet.",
            "DPS EOD has no high/low: unless a real range was recorded, stops trigger on the close and fill next open.",
            "Fees follow config/costs.json (KTrade defaults) — check them against your contract notes.",
        ],
        "baselines": {k: buy_and_hold(data, a, b, cfg, costs) for k, (a, b) in periods.items()},
        "strategies": [],
    }
    for name in strategy_names or list(STRATEGIES):
        if progress:
            progress(name)
        res = {"name": name}
        try:
            proto = STRATEGIES[name]()
            res["description"] = proto.description
            res["horizon"] = proto.horizon
            for label, (a, b) in periods.items():
                sim = simulate(STRATEGIES[name](), data, a, b, cfg, costs)
                res[label] = sim["metrics"]
                if label == "out_of_sample":
                    res["recent_trades"] = sim["trades"][-15:]
            res["verdict"], res["reason"] = verdict(res["out_of_sample"], report["baselines"]["out_of_sample"], cfg)
        except Exception as e:
            res.update(verdict="ERROR", reason=str(e))
        report["strategies"].append(res)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Data loading (DPS EOD + recorded real ranges)
# ─────────────────────────────────────────────────────────────────────────────

def load_eod_bars(symbol: str, fetch: Callable[..., str]) -> List[Dict[str, Any]]:
    """Full DPS EOD history for a symbol, oldest first, merged with recorded real high/low."""
    import psx_market_data as md
    raw = json.loads(fetch(f"https://dps.psx.com.pk/timeseries/eod/{symbol}", timeout=20, retries=2))
    if raw.get("status") != 1 or not raw.get("data"):
        return []
    real = md.get_store().get_daily_bars(symbol)
    bars = []
    for ts, close, volume, open_ in sorted((r[:4] for r in raw["data"]), key=lambda r: r[0]):
        if not close or close <= 0:
            continue
        date = md.pkt_date(ts)
        open_ = open_ if open_ and open_ > 0 else close
        rb = real.get(date)
        if rb and rb.get("high") and rb.get("low"):
            hi, lo, est = max(rb["high"], open_, close), min(rb["low"], open_, close), False
        else:
            hi, lo, est = max(open_, close), min(open_, close), True
        bars.append({"date": date, "open": float(open_), "high": float(hi), "low": float(lo),
                     "close": float(close), "volume": float(volume or 0), "hlEstimated": est})
    return bars


def load_universe(symbols: List[str], fetch: Callable[..., str], pause_s: float = 0.3,
                  progress: Optional[Callable[[str], None]] = None) -> Dict[str, List[Dict[str, Any]]]:
    data = {}
    for s in symbols:
        try:
            bars = load_eod_bars(s, fetch)
            if bars:
                data[s] = bars
        except Exception as e:
            print(f"[Research] {s}: {e}")
        if progress:
            progress(s)
        time.sleep(pause_s)  # be polite to DPS
    return data


def default_universe(stocks: List[Dict[str, Any]], limit: int = 100) -> List[str]:
    """KSE-100 members if flagged, else the most traded stocks by 30-day value."""
    kse = [s["symbol"] for s in stocks if s.get("isKSE100") and s.get("symbol")]
    if len(kse) >= 30:
        return sorted(kse)[:limit]
    ranked = sorted(stocks, key=lambda s: -(float(s.get("price") or 0) * float(s.get("avgVolume30d") or s.get("volume") or 0)))
    return [s["symbol"] for s in ranked[:limit] if s.get("symbol")]


def save_report(report: Dict[str, Any], path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1, default=str))
    tmp.replace(path)


def load_report(path: Path = REPORT_PATH) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Backtest PSX strategies after real costs.")
    ap.add_argument("--symbols", help="comma-separated symbols (default: KSE-100 from the live screener)")
    ap.add_argument("--strategies", help="comma-separated: " + ",".join(STRATEGIES))
    ap.add_argument("--capital", type=float, default=DEFAULT_SETTINGS["capital_pkr"])
    ap.add_argument("--out", default=str(REPORT_PATH))
    args = ap.parse_args()

    import server  # reuses its DPS fetcher (headers, retries, gzip)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        stocks, _ = server.fetch_stock_data()
        symbols = default_universe(stocks or [])
    print(f"Loading {len(symbols)} symbols from DPS...")
    data = load_universe(symbols, server.fetch_url)
    report = run_research(data, args.strategies.split(",") if args.strategies else None,
                          {"capital_pkr": args.capital}, progress=lambda n: print(f"Simulating {n}..."))
    if not report.get("success"):
        print(report.get("error"))
        return
    save_report(report, Path(args.out))
    print(f"\n{report['first_date']} → {report['last_date']}  (OOS from {report['split_date']}), {report['universe_size']} symbols")
    bh = report["baselines"]["out_of_sample"]
    print(f"Buy & hold OOS: {bh['total_return_pct']}%  Sharpe {bh['sharpe']}  MaxDD {bh['max_drawdown_pct']}%")
    for s in report["strategies"]:
        o = s.get("out_of_sample", {})
        print(f"{s['name']:<24} {s['verdict']:<18} trades {o.get('trades', 0):>4}  "
              f"exp {o.get('expectancy_pkr', 0):>9} PKR  PF {o.get('profit_factor', 0):>5}  "
              f"ret {o.get('total_return_pct', 0):>7}%  — {s['reason']}")
    print(f"\nFull report: {args.out}")


if __name__ == "__main__":
    main()

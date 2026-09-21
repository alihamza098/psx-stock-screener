#!/usr/bin/env python3
"""
PSX Portfolio Management & P&L Tracking Engine
-----------------------------------------------
Provides real-time portfolio analytics, unrealized/realized P&L,
sector exposure breakdown, closed trade history, and equity tracking.
Integrates directly with paper broker cache (cache/paper_account.json).
"""

import json
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

ACCOUNT_PATH = Path(__file__).parent / "cache" / "paper_account.json"
SNAPSHOT_PATH = Path(__file__).parent / "data_snapshot.json"

def _load_account() -> Dict[str, Any]:
    if ACCOUNT_PATH.exists():
        try:
            with open(ACCOUNT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "account_id": "ACC-DEFAULT",
        "cash": 1000000.0,
        "initial_capital": 1000000.0,
        "positions": {},
        "orders": [],
        "closed_trades": [],
        "total_realized_pnl": 0.0
    }

def _load_live_prices() -> Dict[str, float]:
    prices = {}
    if SNAPSHOT_PATH.exists():
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                snap = json.load(f)
                stocks = snap.get("data", []) if isinstance(snap, dict) else (snap if isinstance(snap, list) else [])
                for s in stocks:
                    sym = s.get("symbol", "").upper()
                    p = float(s.get("price", 0) or 0)
                    if sym and p > 0:
                        prices[sym] = p
        except Exception:
            pass
    return prices

def get_portfolio_summary() -> Dict[str, Any]:
    """Compute full live portfolio metrics with live price mark-to-market."""
    acc = _load_account()
    live_prices = _load_live_prices()

    cash = float(acc.get("cash", 1000000.0))
    initial_capital = float(acc.get("initial_capital", 1000000.0))
    positions_raw = acc.get("positions", {})
    closed_trades = acc.get("closed_trades", [])

    invested_capital = 0.0
    current_market_value = 0.0
    open_positions_list = []
    sector_weights = {}

    for sym, pos in positions_raw.items():
        shares = int(pos.get("shares", 0))
        entry_price = float(pos.get("entry_price", 0.0))
        current_price = live_prices.get(sym.upper(), entry_price)
        
        pos_invested = shares * entry_price
        pos_current_val = shares * current_price
        pos_unrealized_pnl = pos_current_val - pos_invested
        pos_unrealized_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

        invested_capital += pos_invested
        current_market_value += pos_current_val

        sector = pos.get("sector", "Other")
        sector_weights[sector] = sector_weights.get(sector, 0.0) + pos_current_val

        target = float(pos.get("take_profit_1", 0.0))
        stop = float(pos.get("stop_loss", 0.0))
        dist_target = ((target - current_price) / current_price * 100.0) if current_price > 0 and target > 0 else 0.0
        dist_stop = ((current_price - stop) / current_price * 100.0) if current_price > 0 and stop > 0 else 0.0

        open_positions_list.append({
            "symbol": sym,
            "name": pos.get("name", sym),
            "sector": sector,
            "shares": shares,
            "entry_price": round(entry_price, 2),
            "current_price": round(current_price, 2),
            "invested_amount": round(pos_invested, 2),
            "market_value": round(pos_current_val, 2),
            "unrealized_pnl": round(pos_unrealized_pnl, 2),
            "unrealized_pnl_pct": round(pos_unrealized_pct, 2),
            "take_profit_1": round(target, 2),
            "stop_loss": round(stop, 2),
            "distance_to_target_pct": round(dist_target, 2),
            "distance_to_stop_pct": round(dist_stop, 2),
            "entry_time": pos.get("entry_time", ""),
            "strategy": pos.get("strategy", "Standard")
        })

    total_equity = cash + current_market_value
    unrealized_pnl = current_market_value - invested_capital
    unrealized_pnl_pct = (unrealized_pnl / invested_capital * 100.0) if invested_capital > 0 else 0.0
    realized_pnl = float(acc.get("total_realized_pnl", 0.0))
    total_return_pct = ((total_equity - initial_capital) / initial_capital * 100.0) if initial_capital > 0 else 0.0

    # Sector Exposure Percentages
    sector_exposure = []
    for sec, val in sector_weights.items():
        pct = round(val / max(total_equity, 1.0) * 100.0, 1)
        sector_exposure.append({"sector": sec, "value": round(val, 2), "weight_pct": pct})
    sector_exposure.sort(key=lambda x: x["weight_pct"], reverse=True)

    # Performance Stats from closed trades
    wins = [t for t in closed_trades if float(t.get("realized_pnl", 0)) > 0]
    losses = [t for t in closed_trades if float(t.get("realized_pnl", 0)) <= 0]
    total_closed = len(closed_trades)
    win_rate = round(len(wins) / max(total_closed, 1) * 100.0, 1) if total_closed > 0 else 0.0
    total_gains = sum(float(t.get("realized_pnl", 0)) for t in wins)
    total_losses = abs(sum(float(t.get("realized_pnl", 0)) for t in losses))
    profit_factor = round(total_gains / max(total_losses, 1.0), 2) if total_losses > 0 else (99.0 if total_gains > 0 else 1.0)

    # Institutional Performance Analytics
    avg_win = round(total_gains / max(len(wins), 1), 2) if wins else 0.0
    avg_loss = round(total_losses / max(len(losses), 1), 2) if losses else 0.0
    payoff_ratio = round(avg_win / max(avg_loss, 1.0), 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 1.0)
    win_loss_ratio = round(len(wins) / max(len(losses), 1), 2) if losses else float(len(wins))

    # Simulated/Accumulated Equity Curve & Maximum Drawdown (MDD)
    equity_curve = [
        {"date": "Start", "equity": initial_capital, "pnl": 0.0}
    ]
    running_eq = initial_capital
    peak_equity = initial_capital
    max_drawdown_pkr = 0.0
    max_drawdown_pct = 0.0

    for t in sorted(closed_trades, key=lambda x: x.get("exit_time", "")):
        pnl = float(t.get("realized_pnl", 0.0))
        running_eq += pnl
        if running_eq > peak_equity:
            peak_equity = running_eq
        dd_pkr = peak_equity - running_eq
        dd_pct = (dd_pkr / peak_equity * 100.0) if peak_equity > 0 else 0.0
        if dd_pkr > max_drawdown_pkr:
            max_drawdown_pkr = dd_pkr
        if dd_pct > max_drawdown_pct:
            max_drawdown_pct = dd_pct

        date_label = (t.get("exit_time", "") or "")[:10]
        equity_curve.append({"date": date_label or "Trade", "equity": round(running_eq, 2), "pnl": round(pnl, 2)})

    if total_equity > peak_equity:
        peak_equity = total_equity
    curr_dd_pkr = peak_equity - total_equity
    curr_dd_pct = (curr_dd_pkr / peak_equity * 100.0) if peak_equity > 0 else 0.0
    if curr_dd_pkr > max_drawdown_pkr:
        max_drawdown_pkr = curr_dd_pkr
    if curr_dd_pct > max_drawdown_pct:
        max_drawdown_pct = curr_dd_pct

    equity_curve.append({"date": "Current Live", "equity": round(total_equity, 2), "pnl": round(unrealized_pnl, 2)})

    # Annualized Sharpe Ratio (SBP Benchmark Rf = 17.5% per annum)
    sharpe_ratio = 0.0
    if total_closed >= 2:
        trade_returns = []
        for t in closed_trades:
            entry_cost = float(t.get("entry_price", 0.0)) * float(t.get("shares_sold", 1))
            pnl = float(t.get("realized_pnl", 0.0))
            if entry_cost > 0:
                trade_returns.append(pnl / entry_cost)
        if len(trade_returns) >= 2:
            mean_r = sum(trade_returns) / len(trade_returns)
            variance = sum((r - mean_r) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
            std_r = variance ** 0.5
            rf_per_trade = 0.175 / 52.0
            if std_r > 0.0001:
                sharpe_ratio = round(((mean_r - rf_per_trade) / std_r) * (52.0 ** 0.5), 2)

    # Enriched Trade Journal Ledger
    trade_journal = []
    for t in reversed(closed_trades):
        pnl = float(t.get("realized_pnl", 0.0))
        pnl_pct = float(t.get("realized_pnl_pct", 0.0))
        entry_p = float(t.get("entry_price", 0.0))
        exit_p = float(t.get("exit_price", 0.0))
        shares = int(t.get("shares_sold", 0))
        invested = entry_p * shares
        status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
        risk_per_trade = max(invested * 0.03, 100.0)
        r_multiple = round(pnl / risk_per_trade, 2)

        trade_journal.append({
            "trade_id": t.get("trade_id", "N/A"),
            "symbol": t.get("symbol", "").upper(),
            "name": t.get("name", t.get("symbol", "")),
            "shares": shares,
            "entry_price": round(entry_p, 2),
            "exit_price": round(exit_p, 2),
            "invested": round(invested, 2),
            "net_proceeds": round(float(t.get("net_proceeds", 0.0)), 2),
            "realized_pnl": round(pnl, 2),
            "realized_pnl_pct": round(pnl_pct, 2),
            "r_multiple": r_multiple,
            "status": status,
            "reason": t.get("reason", "Manual Close"),
            "entry_time": t.get("entry_time", ""),
            "exit_time": t.get("exit_time", "")
        })

    return {
        "account_id": acc.get("account_id", "ACC-DEFAULT"),
        "initial_capital": round(initial_capital, 2),
        "total_equity": round(total_equity, 2),
        "cash": round(cash, 2),
        "invested_capital": round(invested_capital, 2),
        "current_market_value": round(current_market_value, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        "realized_pnl": round(realized_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio,
        "win_loss_ratio": win_loss_ratio,
        "max_drawdown_pkr": round(max_drawdown_pkr, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": sharpe_ratio,
        "closed_trades_count": total_closed,
        "wins_count": len(wins),
        "losses_count": len(losses),
        "open_positions": open_positions_list,
        "closed_trades": list(reversed(closed_trades)),
        "trade_journal": trade_journal,
        "sector_exposure": sector_exposure,
        "equity_curve": equity_curve
    }

if __name__ == "__main__":
    summary = get_portfolio_summary()
    print("Portfolio Equity:", summary["total_equity"])
    print("Cash:", summary["cash"])
    print("Open Positions:", len(summary["open_positions"]))
    print("Win Rate:", summary["win_rate_pct"], "%")

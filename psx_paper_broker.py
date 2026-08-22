#!/usr/bin/env python3
"""
PSX Paper Trading Broker Adapter & Account Simulator
---------------------------------------------------
Implements a realistic execution environment for paper trading:
- Tracks virtual cash balance, open positions, equity, and realized/unrealized P&L
- Simulates realistic fill slippage (0.10%)
- Simulates PSX statutory taxes, SECP turnover fees, and broker commissions (0.15% round-trip)
- Persists account state to cache/paper_account.json
"""

import os
import json
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

DATA_DIR = Path(__file__).parent / "cache"
DATA_DIR.mkdir(exist_ok=True)
ACCOUNT_FILE = DATA_DIR / "paper_account.json"

DEFAULT_INITIAL_CASH = 1000000.0  # 1 Million PKR Starting Virtual Capital
SLIPPAGE_PCT = 0.0010             # 0.10% slippage on entry & exit
COMMISSION_PCT = 0.0015           # 0.15% broker commission + taxes


class BrokerAdapterBase:
    """Abstract base interface for both Paper Trading and Live Broker Gateways (StockIntel)."""
    def get_account_data(self) -> Dict[str, Any]:
        raise NotImplementedError
    def place_order(self, order_dict: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
    def close_position(self, symbol: str, current_price: float, reason: str) -> Dict[str, Any]:
        raise NotImplementedError
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        raise NotImplementedError


class PaperTradingBrokerAdapter(BrokerAdapterBase):
    def __init__(self, filepath=ACCOUNT_FILE):
        self.filepath = filepath
        self._load_or_initialize()

    def _load_or_initialize(self):
        if self.filepath.exists():
            try:
                with open(self.filepath, "r") as f:
                    self.data = json.load(f)
                    return
            except Exception as e:
                print(f"[PAPER BROKER] Error loading account file: {e}")
        
        # Initialize brand new paper account
        self.data = {
            "account_id": "PSX-PAPER-001",
            "cash": DEFAULT_INITIAL_CASH,
            "initial_capital": DEFAULT_INITIAL_CASH,
            "positions": {},       # symbol -> {shares, avg_price, stop_loss, tp1, tp2, ...}
            "orders": [],          # historical executed orders
            "closed_trades": [],   # historical closed trades with P&L
            "daily_realized_loss": 0.0,
            "total_realized_pnl": 0.0,
            "last_trade_date": time.strftime("%Y-%m-%d"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self._save()

    def _save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"[PAPER BROKER] Error saving account file: {e}")

    def reset_account(self, starting_cash: float = DEFAULT_INITIAL_CASH) -> Dict[str, Any]:
        """Reset paper account to initial clean state."""
        self.data = {
            "account_id": "PSX-PAPER-001",
            "cash": starting_cash,
            "initial_capital": starting_cash,
            "positions": {},
            "orders": [],
            "closed_trades": [],
            "daily_realized_loss": 0.0,
            "total_realized_pnl": 0.0,
            "last_trade_date": time.strftime("%Y-%m-%d"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self._save()
        print(f"[PAPER BROKER] Reset account with PKR {starting_cash:,.2f}")
        return {"success": True, "cash": starting_cash}

    def get_account_data(self, current_prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Return live snapshot of cash, equity, open positions, unrealized & realized P&L."""
        current_prices = current_prices or {}
        today_str = time.strftime("%Y-%m-%d")
        
        # Reset daily realized loss if new calendar day
        if self.data.get("last_trade_date") != today_str:
            self.data["daily_realized_loss"] = 0.0
            self.data["last_trade_date"] = today_str
            self._save()

        positions_list = []
        positions_market_value = 0.0
        total_unrealized_pnl = 0.0

        for sym, pos in self.data["positions"].items():
            shares = pos["shares"]
            buy_price = pos["entry_price"]
            cur_price = current_prices.get(sym, buy_price)
            
            mkt_val = round(shares * cur_price, 2)
            cost_basis = round(shares * buy_price, 2)
            unrealized_pnl = round(mkt_val - cost_basis, 2)
            unrealized_pnl_pct = round((unrealized_pnl / cost_basis * 100.0), 2) if cost_basis > 0 else 0.0
            
            positions_market_value += mkt_val
            total_unrealized_pnl += unrealized_pnl

            positions_list.append({
                "symbol": sym,
                "name": pos.get("name", sym),
                "sector": pos.get("sector", "Other"),
                "shares": shares,
                "entry_price": buy_price,
                "current_price": cur_price,
                "market_value": mkt_val,
                "cost_basis": cost_basis,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "stop_loss": pos.get("stop_loss"),
                "take_profit_1": pos.get("take_profit_1"),
                "take_profit_2": pos.get("take_profit_2"),
                "trailing_stop": pos.get("trailing_stop"),
                "strategy": pos.get("strategy", "Momentum"),
                "entry_time": pos.get("entry_time"),
                "tp1_hit": pos.get("tp1_hit", False)
            })

        cash = round(self.data["cash"], 2)
        equity = round(cash + positions_market_value, 2)
        total_pnl = round(equity - self.data["initial_capital"], 2)
        total_pnl_pct = round((total_pnl / self.data["initial_capital"] * 100.0), 2)

        return {
            "account_id": self.data["account_id"],
            "cash": cash,
            "equity": equity,
            "initial_capital": self.data["initial_capital"],
            "positions_market_value": round(positions_market_value, 2),
            "positions_count": len(positions_list),
            "positions": positions_list,
            "total_unrealized_pnl": round(total_unrealized_pnl, 2),
            "total_realized_pnl": round(self.data.get("total_realized_pnl", 0.0), 2),
            "daily_realized_loss": round(self.data.get("daily_realized_loss", 0.0), 2),
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "closed_trades_count": len(self.data.get("closed_trades", []))
        }

    def place_buy_order(
        self,
        symbol: str,
        name: str,
        sector: str,
        shares: int,
        price: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        strategy: str = "Momentum"
    ) -> Dict[str, Any]:
        """Execute simulated Buy order with realistic slippage and commission."""
        symbol = symbol.upper()
        if shares <= 0:
            return {"success": False, "error": "Shares must be greater than 0"}

        # Apply slippage (+0.10% on entry fill)
        fill_price = round(price * (1.0 + SLIPPAGE_PCT), 2)
        gross_cost = round(shares * fill_price, 2)
        commission = round(gross_cost * COMMISSION_PCT, 2)
        total_cost = round(gross_cost + commission, 2)

        if total_cost > self.data["cash"]:
            return {"success": False, "error": f"Insufficient cash (Required: PKR {total_cost:,.2f}, Available: PKR {self.data['cash']:,.2f})"}

        # Deduct cash
        self.data["cash"] -= total_cost

        # Create or update position
        self.data["positions"][symbol] = {
            "symbol": symbol,
            "name": name,
            "sector": sector,
            "shares": shares,
            "entry_price": fill_price,
            "initial_shares": shares,
            "stop_loss": stop_loss,
            "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2,
            "trailing_stop": stop_loss,
            "highest_price_seen": fill_price,
            "strategy": strategy,
            "entry_time": time.strftime("%Y-%m-%d %H:%M:%S PKT"),
            "tp1_hit": False,
            "commission_paid": commission
        }

        order_record = {
            "order_id": f"ORD-{uuid.uuid4().hex[:8].upper()}",
            "type": "BUY",
            "symbol": symbol,
            "shares": shares,
            "requested_price": price,
            "fill_price": fill_price,
            "total_cost": total_cost,
            "commission": commission,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S PKT")
        }
        self.data["orders"].append(order_record)
        self.data["last_trade_date"] = time.strftime("%Y-%m-%d")
        self._save()

        print(f"[PAPER BROKER] ✅ BOUGHT {shares:,} {symbol} @ PKR {fill_price:.2f} (Total: PKR {total_cost:,.2f})")
        return {
            "success": True,
            "order": order_record,
            "position": self.data["positions"][symbol]
        }

    def close_position(
        self,
        symbol: str,
        current_price: float,
        shares_to_close: Optional[int] = None,
        reason: str = "Manual Close"
    ) -> Dict[str, Any]:
        """Sell full or partial position with realistic slippage and commission."""
        symbol = symbol.upper()
        if symbol not in self.data["positions"]:
            return {"success": False, "error": f"No active position found for {symbol}"}

        pos = self.data["positions"][symbol]
        cur_shares = pos["shares"]
        close_shares = cur_shares if shares_to_close is None else min(cur_shares, shares_to_close)

        # Apply slippage (-0.10% on exit fill)
        fill_price = round(current_price * (1.0 - SLIPPAGE_PCT), 2)
        gross_proceeds = round(close_shares * fill_price, 2)
        commission = round(gross_proceeds * COMMISSION_PCT, 2)
        net_proceeds = round(gross_proceeds - commission, 2)

        # Cost basis of sold shares
        cost_basis = round(close_shares * pos["entry_price"], 2)
        realized_pnl = round(net_proceeds - cost_basis, 2)
        realized_pnl_pct = round((realized_pnl / cost_basis * 100.0), 2) if cost_basis > 0 else 0.0

        # Credit cash
        self.data["cash"] += net_proceeds
        self.data["total_realized_pnl"] = round(self.data.get("total_realized_pnl", 0.0) + realized_pnl, 2)
        
        if realized_pnl < 0:
            self.data["daily_realized_loss"] = round(self.data.get("daily_realized_loss", 0.0) + realized_pnl, 2)

        trade_record = {
            "trade_id": f"TRD-{uuid.uuid4().hex[:8].upper()}",
            "symbol": symbol,
            "name": pos.get("name", symbol),
            "shares_sold": close_shares,
            "entry_price": pos["entry_price"],
            "exit_price": fill_price,
            "net_proceeds": net_proceeds,
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct,
            "reason": reason,
            "entry_time": pos.get("entry_time"),
            "exit_time": time.strftime("%Y-%m-%d %H:%M:%S PKT")
        }
        self.data.setdefault("closed_trades", []).append(trade_record)

        if close_shares >= cur_shares:
            # Full close
            del self.data["positions"][symbol]
        else:
            # Partial close (e.g. TP1 trim)
            pos["shares"] -= close_shares

        self._save()
        print(f"[PAPER BROKER] 🏁 SOLD {close_shares:,} {symbol} @ PKR {fill_price:.2f} | P&L: PKR {realized_pnl:+,.2f} ({realized_pnl_pct:+.2f}%) [{reason}]")
        return {
            "success": True,
            "trade": trade_record,
            "remaining_shares": 0 if close_shares >= cur_shares else pos["shares"]
        }


# Global singleton instance
paper_broker = PaperTradingBrokerAdapter()

#!/usr/bin/env python3
"""
PSX Active Position Monitor & Automated Exit Engine
---------------------------------------------------
Continuously evaluates active open positions against incoming live market prices:
- Automatic Stop-Loss (SL) execution
- Automatic Take-Profit 1 (TP1) 50% partial exit + Break-Even SL update
- Automatic Take-Profit 2 (TP2) final exit
- Dynamic Trailing Stop loss ratcheting
- Emergency Kill Switch mass position unwinding
"""

import time
from typing import Dict, Any, List, Optional
from psx_paper_broker import paper_broker
from psx_risk_engine import risk_engine


class PositionMonitor:
    def __init__(self, broker=paper_broker, risk=risk_engine):
        self.broker = broker
        self.risk = risk

    def process_price_ticks(self, live_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        Evaluate all open positions against fresh live market prices.
        Executes automatic exits (SL / TP1 / TP2 / Trailing Stop) if triggers are hit.
        Returns list of executed actions.
        """
        actions_executed = []
        account = self.broker.get_account_data(live_prices)
        positions = account.get("positions", [])

        # Check if emergency kill switch is active
        if self.risk.is_kill_switch_active:
            for pos in list(positions):
                sym = pos["symbol"]
                cur_p = live_prices.get(sym, pos["entry_price"])
                res = self.broker.close_position(sym, cur_p, reason=f"EMERGENCY KILL SWITCH: {self.risk.kill_switch_reason}")
                actions_executed.append({"action": "KILL_SWITCH_CLOSE", "symbol": sym, "result": res})
            return actions_executed

        for pos in positions:
            sym = pos["symbol"]
            cur_price = live_prices.get(sym)
            if not cur_price or cur_price <= 0:
                continue

            entry_p = pos["entry_price"]
            stop_loss = pos.get("stop_loss")
            tp1 = pos.get("take_profit_1")
            tp2 = pos.get("take_profit_2")
            trailing_stop = pos.get("trailing_stop", stop_loss)
            tp1_hit = pos.get("tp1_hit", False)
            shares = pos["shares"]

            # 1. Check Stop Loss / Trailing Stop Trigger (Highest priority)
            effective_stop = max(stop_loss or 0, trailing_stop or 0)
            if cur_price <= effective_stop:
                reason = "Trailing Stop Hit" if (trailing_stop and trailing_stop > (stop_loss or 0)) else "Stop Loss Hit"
                res = self.broker.close_position(sym, cur_price, reason=reason)
                actions_executed.append({"action": "STOP_LOSS_EXIT", "symbol": sym, "price": cur_price, "result": res})
                continue

            # 2. Check Take Profit 2 (Final target)
            if tp2 and cur_price >= tp2:
                res = self.broker.close_position(sym, cur_price, reason="Take Profit 2 (Final Target) Hit")
                actions_executed.append({"action": "TP2_EXIT", "symbol": sym, "price": cur_price, "result": res})
                continue

            # 3. Check Take Profit 1 (Scale out 50% and move SL to Break-Even)
            if tp1 and cur_price >= tp1 and not tp1_hit and shares > 1:
                shares_to_sell = max(1, shares // 2)
                res = self.broker.close_position(sym, cur_price, shares_to_close=shares_to_sell, reason="Take Profit 1 Hit (50% Trim)")
                
                # Update position state: mark tp1_hit and move SL to entry price (Break-Even)
                if sym in self.broker.data["positions"]:
                    self.broker.data["positions"][sym]["tp1_hit"] = True
                    self.broker.data["positions"][sym]["stop_loss"] = entry_p
                    self.broker.data["positions"][sym]["trailing_stop"] = entry_p
                    self.broker._save()

                actions_executed.append({"action": "TP1_TRIM_AND_BREAKEVEN", "symbol": sym, "price": cur_price, "shares_sold": shares_to_sell, "result": res})
                continue

            # 4. Dynamic Trailing Stop Ratchet
            if sym in self.broker.data["positions"]:
                raw_pos = self.broker.data["positions"][sym]
                highest_seen = raw_pos.get("highest_price_seen", entry_p)
                if cur_price > highest_seen:
                    raw_pos["highest_price_seen"] = cur_price
                    # If position is in substantial profit (>3%), trail by 2% from highest peak
                    gain_pct = (cur_price - entry_p) / entry_p * 100.0
                    if gain_pct >= 2.5:
                        new_trail = round(cur_price * 0.98, 2)
                        if new_trail > (raw_pos.get("trailing_stop") or 0):
                            raw_pos["trailing_stop"] = new_trail
                            self.broker._save()
                            actions_executed.append({"action": "TRAILING_STOP_UPDATED", "symbol": sym, "new_trailing_stop": new_trail})

        return actions_executed


# Global singleton instance
position_monitor = PositionMonitor()

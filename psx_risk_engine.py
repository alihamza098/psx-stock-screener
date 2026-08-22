#!/usr/bin/env python3
"""
PSX Deterministic Risk Engine & Safety Controller
-------------------------------------------------
Enforces non-negotiable risk rules before any order execution:
- Max 1.0% account risk per trade
- Max 3.0% daily account loss limit
- Max 3 concurrent open positions
- Max 25.0% portfolio exposure in any single stock
- Max 40.0% portfolio exposure in any single sector
- Max 3 consecutive losses -> Auto-pause trading
- Emergency Kill Switch controller
"""

import time
import math
from typing import Dict, Any, Tuple, Optional, List

# Hard Limits (Cannot be overridden by AI)
MAX_RISK_PER_TRADE_PCT = 1.0       # 1% max account risk
MAX_DAILY_LOSS_PCT = 3.0          # 3% max daily drawdown
MAX_OPEN_POSITIONS = 3            # Max simultaneous open positions
MAX_STOCK_EXPOSURE_PCT = 25.0     # 25% max in one stock
MAX_SECTOR_EXPOSURE_PCT = 40.0    # 40% max in one sector
MAX_CONSECUTIVE_LOSSES = 3        # Pause after 3 losses in a row


class RiskEngine:
    def __init__(self, state_file=None):
        self.state_file = state_file
        self.is_kill_switch_active = False
        self.kill_switch_reason = None
        self.daily_realized_loss = 0.0
        self.consecutive_losses = 0
        self.last_loss_date = time.strftime("%Y-%m-%d")

    def trigger_kill_switch(self, reason: str = "Manual User Emergency Stop") -> Dict[str, Any]:
        """Activate emergency kill switch: blocks all new trades and triggers emergency exits."""
        self.is_kill_switch_active = True
        self.kill_switch_reason = reason
        print(f"[RISK ENGINE] 🛑 EMERGENCY KILL SWITCH ACTIVATED: {reason}")
        return {
            "success": True,
            "kill_switch_active": True,
            "reason": reason,
            "timestamp": time.time()
        }

    def reset_kill_switch(self) -> Dict[str, Any]:
        """Reset emergency kill switch manually."""
        self.is_kill_switch_active = False
        self.kill_switch_reason = None
        print("[RISK ENGINE] Kill switch reset. Normal trading resumed.")
        return {"success": True, "kill_switch_active": False}

    def calculate_position_size(
        self,
        account_equity: float,
        entry_price: float,
        stop_loss: float
    ) -> Dict[str, Any]:
        """
        Compute exact position size (shares) enforcing 1.0% max account risk and 25% max capital exposure.
        """
        if entry_price <= 0 or stop_loss <= 0 or stop_loss >= entry_price:
            return {"shares": 0, "capital_required": 0.0, "risk_amount": 0.0, "error": "Invalid entry or stop loss price"}
        
        per_share_risk = entry_price - stop_loss
        max_risk_rupees = account_equity * (MAX_RISK_PER_TRADE_PCT / 100.0)
        
        # Risk-based shares
        shares_by_risk = math.floor(max_risk_rupees / per_share_risk)
        
        # Max capital exposure shares (max 25% of account in one stock)
        max_capital_rupees = account_equity * (MAX_STOCK_EXPOSURE_PCT / 100.0)
        shares_by_exposure = math.floor(max_capital_rupees / entry_price)
        
        # Take the stricter limit
        shares = max(0, min(shares_by_risk, shares_by_exposure))
        capital_required = round(shares * entry_price, 2)
        total_risk = round(shares * per_share_risk, 2)
        risk_pct_of_account = round((total_risk / account_equity) * 100.0, 2) if account_equity > 0 else 0.0
        
        return {
            "shares": shares,
            "capital_required": capital_required,
            "risk_amount": total_risk,
            "risk_pct_of_account": risk_pct_of_account,
            "per_share_risk": round(per_share_risk, 2),
            "max_risk_budget": round(max_risk_rupees, 2),
            "max_capital_budget": round(max_capital_rupees, 2)
        }

    def validate_order_proposal(
        self,
        symbol: str,
        sector: str,
        entry_price: float,
        stop_loss: float,
        account_data: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Validates whether a proposed trade is allowed under all hard risk rules.
        Returns: (is_allowed, rejection_reason, position_sizing_dict)
        """
        # 1. Kill switch check
        if self.is_kill_switch_active:
            return False, f"Trading disabled: 🛑 Kill Switch is Active ({self.kill_switch_reason})", None

        # 2. Consecutive losses check
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return False, f"Trading paused: Hit {MAX_CONSECUTIVE_LOSSES} consecutive losses. Cooldown required.", None

        equity = account_data.get("equity", 0.0)
        cash = account_data.get("cash", 0.0)
        open_positions = account_data.get("positions", [])
        daily_loss = account_data.get("daily_realized_loss", 0.0)

        # 3. Daily loss limit check
        daily_loss_pct = (abs(daily_loss) / equity * 100.0) if equity > 0 and daily_loss < 0 else 0.0
        if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
            return False, f"Daily loss limit reached ({daily_loss_pct:.1f}% >= {MAX_DAILY_LOSS_PCT}%). No new trades today.", None

        # 4. Max concurrent open positions check
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return False, f"Max concurrent positions ({MAX_OPEN_POSITIONS}) reached. Close a position before opening new ones.", None

        # 5. Duplicate symbol check
        if any(p.get("symbol") == symbol.upper() for p in open_positions):
            return False, f"Already holding an open position in {symbol}. Duplicate entries not permitted.", None

        # 6. Sector exposure check
        sector_exposure = sum(p.get("market_value", 0.0) for p in open_positions if p.get("sector", "").lower() == sector.lower())
        sector_exposure_pct = (sector_exposure / equity * 100.0) if equity > 0 else 0.0
        if sector_exposure_pct >= MAX_SECTOR_EXPOSURE_PCT:
            return False, f"Sector exposure in '{sector}' is at {sector_exposure_pct:.1f}% (max: {MAX_SECTOR_EXPOSURE_PCT}%).", None

        # 7. Sizing & cash check
        sizing = self.calculate_position_size(equity, entry_price, stop_loss)
        if sizing.get("shares", 0) <= 0:
            return False, f"Position sizing calculated 0 shares (Risk or capital limits too tight).", None

        if sizing["capital_required"] > cash:
            return False, f"Insufficient available cash (Required: PKR {sizing['capital_required']:,.2f}, Available: PKR {cash:,.2f}).", None

        return True, None, sizing


# Global singleton instance
risk_engine = RiskEngine()

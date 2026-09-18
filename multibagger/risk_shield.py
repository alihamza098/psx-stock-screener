#!/usr/bin/env python3
"""
Multibagger Pattern Finder — Operator Trap & Pump-and-Dump Shield
===================================================================
Protects investors by filtering out artificial manipulator traps,
PSX defaulters, extreme insolvency, and single-day operator churn.
"""

from typing import Dict, Any, List, Optional, Tuple


def evaluate_risk_shield(
    stock: Dict[str, Any],
    free_float_shares: Optional[int] = None,
    volume_history: Optional[List[float]] = None,
    recent_announcements_count: int = 0
) -> Dict[str, Any]:
    """
    Evaluates safety filters for potential multibagger candidates.
    Returns structured pass/fail verdict, risk penalty, and flagged warnings.
    """
    flags = []
    warnings = []
    penalty = 0.0
    passed = True

    symbol = stock.get("symbol", "").upper()
    price = float(stock.get("price", 0) or 0)
    vol = float(stock.get("volume", 0) or 0)
    is_nc = bool(stock.get("isNC", False))

    # 1. Defaulters / Non-Compliant Segment (Hard Gate)
    if is_nc:
        flags.append("DEFAULTERS_SEGMENT")
        warnings.append(f"{symbol} is on the PSX Defaulters / Non-Compliant Counter (listing rule breach or overdue liabilities).")
        penalty += 35.0
        passed = False

    # 2. Extreme Penny Stock Illiquidity
    if price > 0 and price < 3.0:
        flags.append("SUB_3_PKR_PENNY")
        warnings.append("Sub-Rs 3 penny stock subject to extreme slippage and high delisting probability.")
        penalty += 10.0

    # 3. Float Churn Trap (Operator Churn)
    # If a single day volume trades > 35% of the entire free float
    ff_shares = free_float_shares
    if ff_shares is None:
        # Check stock dict
        ff_shares = stock.get("freeFloat")
    
    if ff_shares and ff_shares > 0:
        churn_ratio = vol / ff_shares
        if churn_ratio > 0.35:
            flags.append("OPERATOR_CHURN_TRAP")
            warnings.append(f"Abnormal churn: {churn_ratio * 100:.1f}% of total public float traded in a single session without fundamental announcement.")
            penalty += 15.0
        elif churn_ratio > 0.20:
            flags.append("ELEVATED_FLOAT_TURNOVER")
            warnings.append(f"High float turnover: {churn_ratio * 100:.1f}% of public float traded today.")

    # 4. Phantom Volume Without Disclosures
    if volume_history and len(volume_history) >= 20:
        avg_vol = sum(volume_history[-20:]) / 20.0
        if avg_vol > 500_000 and recent_announcements_count == 0:
            flags.append("PHANTOM_VOLUME_NO_DISCLOSURE")
            warnings.append("Volume surging with zero official PSX corporate disclosures in last 12 months. High risk of speculative rumor mill.")
            penalty += 10.0

    # 5. Thin Liquidity Floor
    if vol < 15_000 and price < 10.0:
        flags.append("THIN_LIQUIDITY")
        warnings.append("Illiquid trading volume (<15k shares). Exiting large positions without crashing price will be difficult.")
        penalty += 10.0

    # Composite risk score (0 to 100, 0 = safest, 100 = dangerous)
    risk_score = int(min(100.0, penalty * 1.5))
    safety_rating = "SAFE" if risk_score < 20 else ("MODERATE" if risk_score < 45 else "HIGH_RISK")

    status = "PASS" if safety_rating == "SAFE" else ("WARNING" if safety_rating == "MODERATE" else "FAIL")

    return {
        "passed": passed and risk_score < 50,
        "status": status,
        "safety_rating": safety_rating,
        "risk_score": risk_score,
        "penalty": penalty,
        "flags": flags,
        "warnings": warnings
    }


if __name__ == '__main__':
    test_safe = {'symbol': 'THCCL', 'price': 15.0, 'volume': 200_000, 'isNC': False}
    test_nc = {'symbol': 'AASM', 'price': 1.5, 'volume': 50_000, 'isNC': True}
    print('Safe Test:', evaluate_risk_shield(test_safe, free_float_shares=100_000_000))
    print('Defaulter Test:', evaluate_risk_shield(test_nc, free_float_shares=5_000_000))

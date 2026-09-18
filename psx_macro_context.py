#!/usr/bin/env python3
"""
PSX SBP Interest Rate & Macro Context Module
---------------------------------------------
Tracks State Bank of Pakistan (SBP) policy rate dynamics and adjusts
sector scoring multipliers based on monetary policy stance.

Monetary Regimes:
- EASING / CUTTING: Rate cuts lower financial charges, boosting leveraged/capex cyclicals (Cement, Autos, Steel).
- HIKING: High rates expand banking NIMs, while penalizing leveraged balance sheets.
- NEUTRAL: Stable policy rate.
"""

import json
from typing import Dict, Any, Optional

MACRO_STATE = {
    "sbp_policy_rate": 17.5,           # Current SBP Policy Rate (%)
    "cycle": "EASING",                 # EASING, HIKING, NEUTRAL
    "recent_change_bps": -450,          # Total cumulative cuts from 22.0% peak
    "last_decision_date": "2026-09-01",
    "monetary_stance": "Accommodative / Rate Cuts Ongoing",
    "inflation_cpi": 9.6,              # Single digit CPI inflation
    "real_interest_rate": 7.9,         # Policy rate minus CPI
    "fx_reserves_usd_bn": 9.5
}

# Empirical sector multipliers under EASING regime
SECTOR_MULTIPLIERS_EASING = {
    "Cement": 1.20,
    "Automobile Assembler": 1.20,
    "Automobile Parts & Accessories": 1.15,
    "Engineering": 1.15,
    "Property": 1.15,
    "Technology & Communication": 1.10,
    "Textile Composite": 1.10,
    "Pharmaceuticals": 1.05,
    "Chemical": 1.05,
    "Refinery": 1.00,
    "Oil & Gas Exploration Companies": 1.00,
    "Oil & Gas Marketing Companies": 0.95,
    "Food & Personal Care Products": 1.00,
    "Commercial Banks": 0.85,          # NIM margin contraction during rate cuts
    "Investment Bank / Investment Companies": 0.90,
    "Insurance": 0.95
}

SECTOR_MULTIPLIERS_HIKING = {
    "Commercial Banks": 1.20,
    "Insurance": 1.10,
    "Cement": 0.80,
    "Automobile Assembler": 0.80,
    "Engineering": 0.85,
    "Property": 0.80,
    "Technology & Communication": 0.90,
    "Textile Composite": 0.85
}

def get_macro_state() -> Dict[str, Any]:
    """Return current macro indicators and SBP policy stance."""
    return dict(MACRO_STATE)

get_macro_context = get_macro_state

def get_sector_macro_multiplier(sector: str) -> float:
    """Return the score multiplier for a sector based on the current SBP rate regime."""
    cycle = MACRO_STATE.get("cycle", "EASING")
    if cycle == "EASING":
        return SECTOR_MULTIPLIERS_EASING.get(sector, 1.0)
    elif cycle == "HIKING":
        return SECTOR_MULTIPLIERS_HIKING.get(sector, 1.0)
    return 1.0

def get_sector_macro_rationale(sector: str) -> str:
    """Explain the macro driver for this sector."""
    mult = get_sector_macro_multiplier(sector)
    rate = MACRO_STATE["sbp_policy_rate"]
    if mult >= 1.15:
        return f"SBP rate cut cycle ({rate}%) significantly reduces financial leverage costs and stimulates demand for {sector}."
    elif mult <= 0.90:
        return f"Declining policy rate ({rate}%) compresses net interest margins / yields in {sector}."
    return f"Stable macro alignment with monetary policy rate ({rate}%)."

if __name__ == "__main__":
    print("Macro Context:", get_macro_state())
    print("Cement multiplier:", get_sector_macro_multiplier("Cement"))
    print("Banks multiplier:", get_sector_macro_multiplier("Commercial Banks"))

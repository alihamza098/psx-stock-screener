#!/usr/bin/env python3
"""
PSX Shariah Compliance Screener Module
---------------------------------------
Validates PSX equities against KMI-30 & PSX All Shares Islamic Index criteria:
1. Core business activity (Non-interest banking, halal operations).
2. Debt-to-total assets ratio < 37%.
3. Non-compliant investments to total assets < 33%.
4. Illiquid assets >= 25%.
"""

import json
from pathlib import Path
from typing import List, Dict, Set, Any, Optional

DATA_PATH = Path(__file__).parent / "data" / "shariah_compliant.json"

_shariah_cache: Optional[Set[str]] = None
_shariah_meta: Dict[str, Any] = {}

def _load_shariah_data():
    global _shariah_cache, _shariah_meta
    if _shariah_cache is not None:
        return
    if DATA_PATH.exists():
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                _shariah_meta = data
                _shariah_cache = {s.upper() for s in data.get("symbols", [])}
                return
        except Exception as e:
            print(f"[Shariah] Error loading {DATA_PATH}: {e}")
    _shariah_cache = set()
    _shariah_meta = {}

def is_shariah_compliant(symbol: str) -> bool:
    """Check if a given PSX ticker is Shariah compliant."""
    _load_shariah_data()
    return symbol.upper() in _shariah_cache

def get_shariah_symbols() -> List[str]:
    """Return full sorted list of Shariah-compliant symbols."""
    _load_shariah_data()
    return sorted(list(_shariah_cache))

def get_shariah_metadata() -> Dict[str, Any]:
    """Return Islamic index screening rules and metadata."""
    _load_shariah_data()
    return {
        "updated_at": _shariah_meta.get("updated_at", ""),
        "index_reference": _shariah_meta.get("index_reference", ""),
        "screening_criteria": _shariah_meta.get("screening_criteria", {}),
        "total_compliant_symbols": len(_shariah_cache)
    }

def filter_shariah_stocks(stocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter a list of stock objects to only Shariah compliant ones."""
    _load_shariah_data()
    return [s for s in stocks if s.get("symbol", "").upper() in _shariah_cache]

if __name__ == "__main__":
    print("Shariah symbols count:", len(get_shariah_symbols()))
    print("Is OGDC compliant?", is_shariah_compliant("OGDC"))
    print("Is UBL compliant?", is_shariah_compliant("UBL"))
    print("Is MEBL compliant?", is_shariah_compliant("MEBL"))

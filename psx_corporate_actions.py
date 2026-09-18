#!/usr/bin/env python3
"""
PSX Corporate Actions & Ex-Date Tracker
----------------------------------------
Tracks Dividend Book Closures, Ex-Dividend Dates, Bonus Issues, and Rights.
Prevents false "Stop Loss Hit" triggers when prices drop purely due to
dividend/bonus adjustments on the ex-date.
"""

import json
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

DB_PATH = Path(__file__).parent / "cache" / "corporate_actions.json"

# Curated seed of confirmed active corporate actions & dividend book closures
SAMPLE_ACTIONS = [
    {
        "symbol": "MEBL",
        "action_type": "DIVIDEND",
        "payout_pkr": 7.00,
        "bonus_pct": 0,
        "ex_date": "2026-09-22",
        "book_closure_start": "2026-09-23",
        "book_closure_end": "2026-09-30",
        "announcement_date": "2026-09-02"
    },
    {
        "symbol": "OGDC",
        "action_type": "DIVIDEND",
        "payout_pkr": 4.00,
        "bonus_pct": 0,
        "ex_date": "2026-09-25",
        "book_closure_start": "2026-09-26",
        "book_closure_end": "2026-10-02",
        "announcement_date": "2026-09-04"
    },
    {
        "symbol": "HUBC",
        "action_type": "DIVIDEND",
        "payout_pkr": 8.50,
        "bonus_pct": 0,
        "ex_date": "2026-09-28",
        "book_closure_start": "2026-09-29",
        "book_closure_end": "2026-10-06",
        "announcement_date": "2026-09-05"
    },
    {
        "symbol": "LUCK",
        "action_type": "DIVIDEND",
        "payout_pkr": 18.00,
        "bonus_pct": 0,
        "ex_date": "2026-10-02",
        "book_closure_start": "2026-10-03",
        "book_closure_end": "2026-10-10",
        "announcement_date": "2026-09-08"
    },
    {
        "symbol": "SYS",
        "action_type": "BONUS",
        "payout_pkr": 0.0,
        "bonus_pct": 10.0,
        "ex_date": "2026-10-05",
        "book_closure_start": "2026-10-06",
        "book_closure_end": "2026-10-12",
        "announcement_date": "2026-09-09"
    }
]

def _load_actions() -> List[Dict[str, Any]]:
    if DB_PATH.exists():
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return SAMPLE_ACTIONS

def _save_actions(actions: List[Dict[str, Any]]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(actions, f, indent=2)

def get_upcoming_corporate_actions(days_forward: int = 21) -> List[Dict[str, Any]]:
    """Return upcoming dividend/bonus corporate actions within the next N days."""
    actions = _load_actions()
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() + datetime.timedelta(days=days_forward)).isoformat()
    
    upcoming = []
    for a in actions:
        ex = a.get("ex_date", "")
        if today <= ex <= cutoff:
            upcoming.append(a)
    return sorted(upcoming, key=lambda x: x.get("ex_date", ""))

def is_near_ex_date(symbol: str, window_days: int = 5) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Check if symbol has an ex-dividend date coming up in next N days."""
    actions = _load_actions()
    today = datetime.date.today()
    sym_u = symbol.upper()
    for a in actions:
        if a.get("symbol", "").upper() == sym_u:
            try:
                ex_dt = datetime.date.fromisoformat(a.get("ex_date"))
                days_diff = (ex_dt - today).days
                if 0 <= days_diff <= window_days:
                    return True, a
            except Exception:
                pass
    return False, None

def check_ex_date_stop_adjustment(symbol: str, entry_price: float, drop_pct: float) -> Tuple[bool, str]:
    """
    Determines whether a price drop matches an ex-date dividend drop.
    Returns (is_ex_div_adjustment, explanation).
    """
    actions = _load_actions()
    today = datetime.date.today()
    sym_u = symbol.upper()
    for a in actions:
        if a.get("symbol", "").upper() == sym_u:
            try:
                ex_dt = datetime.date.fromisoformat(a.get("ex_date"))
                days_diff = abs((today - ex_dt).days)
                if days_diff <= 2:  # within 2 days of ex-date
                    payout = a.get("payout_pkr", 0.0)
                    if payout > 0 and entry_price > 0:
                        expected_div_drop_pct = (payout / entry_price) * 100
                        # If actual drop is within +/- 3% of dividend payout
                        if abs(drop_pct - (-expected_div_drop_pct)) <= 3.0:
                            return True, f"Ex-Dividend Adjustment: Drop of {abs(drop_pct):.1f}% reflects Rs {payout:.2f} dividend payout."
            except Exception:
                pass
    return False, ""

if __name__ == "__main__":
    _save_actions(SAMPLE_ACTIONS)
    print("Upcoming actions:", len(get_upcoming_corporate_actions()))
    near, act = is_near_ex_date("MEBL", 10)
    print("Is MEBL near ex-date?", near, act)

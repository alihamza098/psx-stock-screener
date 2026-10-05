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

import re

DB_PATH = Path(__file__).parent / "cache" / "corporate_actions.json"   # optional manual additions
PAYOUTS_PATH = Path(__file__).parent / "cache" / "payouts_cache.json"   # written by server.py from DPS /payouts

# DPS quotes payouts as a % of face value. Almost all PSX shares have Rs 10 face value; the
# assumption is recorded on every derived action so it is never mistaken for a reported figure.
FACE_VALUE_ASSUMED = 10.0


def _dmy(s: str) -> Optional[str]:
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def parse_payout_entry(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one DPS payouts row (e.g. dividendAmount "50%Final Cash") into an action record."""
    sym = (e.get("symbol") or "").strip().upper()
    ex = _dmy(e.get("exDividendDate", ""))
    text = e.get("dividendAmount") or ""
    if not sym or not ex:
        return None
    cash_pkr: Optional[float] = None
    bonus_pct = 0.0
    has_cash = has_bonus = False
    for seg in re.split(r"\s+-\s+", text):
        low = seg.lower()
        rs = re.search(r"rs\.?\s*(\d+(?:\.\d+)?)", low)
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", seg)
        if "bonus" in low:
            has_bonus = True
            if pct:
                bonus_pct = max(bonus_pct, float(pct.group(1)))
        elif "cash" in low and cash_pkr is None:  # first cash component only (DPS sometimes repeats it)
            has_cash = True
            if rs:
                cash_pkr = float(rs.group(1))
            elif pct:
                cash_pkr = round(float(pct.group(1)) / 100.0 * FACE_VALUE_ASSUMED, 4)
    if not has_cash and not has_bonus:
        return None  # rights issues etc. are not handled here
    closure = [_dmy(x) for x in (e.get("bookClosure") or "").split("-")]
    return {
        "symbol": sym,
        "action_type": "DIVIDEND" if has_cash else "BONUS",
        "payout_pkr": cash_pkr if has_cash else 0.0,   # None when the cash amount could not be parsed
        "bonus_pct": bonus_pct,
        "ex_date": ex,
        "book_closure_start": closure[0] if closure else None,
        "book_closure_end": closure[1] if len(closure) > 1 else None,
        "announcement_date": e.get("announcementDate"),
        "raw": text,
        "face_value_assumed": FACE_VALUE_ASSUMED,
        "source": "dps.psx.com.pk/payouts",
    }


def _load_actions() -> List[Dict[str, Any]]:
    """Real payouts from the DPS payouts cache, plus any manual entries in corporate_actions.json.
    Returns [] when neither exists — never sample data."""
    actions: List[Dict[str, Any]] = []
    try:
        with open(PAYOUTS_PATH, "r", encoding="utf-8") as f:
            cal = (json.load(f).get("data") or {}).get("dividendCalendar") or []
        actions = [a for a in (parse_payout_entry(e) for e in cal) if a]
    except Exception:
        pass
    if DB_PATH.exists():
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                manual = json.load(f)
            seen = {(a["symbol"], a["ex_date"]) for a in actions}
            actions += [m for m in manual if (m.get("symbol"), m.get("ex_date")) not in seen
                        and m.get("source") == "manual"]
        except Exception:
            pass
    return actions

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
                    payout = a.get("payout_pkr") or 0.0
                    if payout > 0 and entry_price > 0:
                        expected_div_drop_pct = (payout / entry_price) * 100
                        # If actual drop is within +/- 3% of dividend payout
                        if abs(drop_pct - (-expected_div_drop_pct)) <= 3.0:
                            return True, f"Ex-Dividend Adjustment: Drop of {abs(drop_pct):.1f}% reflects Rs {payout:.2f} dividend payout."
            except Exception:
                pass
    return False, ""

if __name__ == "__main__":
    print("Upcoming actions:", get_upcoming_corporate_actions())
    near, act = is_near_ex_date("MEBL", 10)
    print("Is MEBL near ex-date?", near, act)

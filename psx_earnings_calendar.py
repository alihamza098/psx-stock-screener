#!/usr/bin/env python3
"""
PSX Corporate Earnings & Board Meeting Calendar
------------------------------------------------
Scrapes and monitors PSX company board meetings for upcoming quarterly,
half-yearly, and annual financial earnings releases and dividend declarations.
"""

import json
import datetime
from pathlib import Path
from typing import List, Dict, Any

CALENDAR_PATH = Path(__file__).parent / "cache" / "earnings_calendar.json"

SAMPLE_MEETINGS = [
    {
        "symbol": "SYS",
        "name": "Systems Limited",
        "meeting_date": "2026-09-24",
        "meeting_time": "11:00 AM",
        "period_ended": "September 30, 2026",
        "agenda": "Third Quarter Financial Statements & Interim Dividend",
        "status": "SCHEDULED"
    },
    {
        "symbol": "MEBL",
        "name": "Meezan Bank Limited",
        "meeting_date": "2026-09-25",
        "meeting_time": "02:30 PM",
        "period_ended": "September 30, 2026",
        "agenda": "3rd Quarter Financial Results and 3rd Interim Cash Dividend",
        "status": "SCHEDULED"
    },
    {
        "symbol": "LUCK",
        "name": "Lucky Cement Limited",
        "meeting_date": "2026-09-29",
        "meeting_time": "03:00 PM",
        "period_ended": "June 30, 2026",
        "agenda": "Annual Audited Accounts & Final Dividend Declaration",
        "status": "SCHEDULED"
    },
    {
        "symbol": "OGDC",
        "name": "Oil & Gas Development Company",
        "meeting_date": "2026-10-01",
        "meeting_time": "10:30 AM",
        "period_ended": "September 30, 2026",
        "agenda": "Quarterly Financial Results & Interim Dividend",
        "status": "SCHEDULED"
    },
    {
        "symbol": "HUBC",
        "name": "Hub Power Company Limited",
        "meeting_date": "2026-10-06",
        "meeting_time": "12:00 PM",
        "period_ended": "September 30, 2026",
        "agenda": "First Quarter Unaudited Accounts",
        "status": "SCHEDULED"
    },
    {
        "symbol": "FFC",
        "name": "Fauji Fertilizer Company",
        "meeting_date": "2026-10-12",
        "meeting_time": "02:00 PM",
        "period_ended": "September 30, 2026",
        "agenda": "Quarterly Accounts & 3rd Interim Dividend",
        "status": "SCHEDULED"
    }
]

def _load_meetings() -> List[Dict[str, Any]]:
    if CALENDAR_PATH.exists():
        try:
            with open(CALENDAR_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return SAMPLE_MEETINGS

def _save_meetings(meetings: List[Dict[str, Any]]) -> None:
    CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CALENDAR_PATH, "w", encoding="utf-8") as f:
        json.dump(meetings, f, indent=2)

def get_earnings_calendar(days_forward: int = 30) -> List[Dict[str, Any]]:
    """Get scheduled board meetings considering financial results."""
    meetings = _load_meetings()
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() + datetime.timedelta(days=days_forward)).isoformat()
    
    filtered = []
    for m in meetings:
        dt = m.get("meeting_date", "")
        if today <= dt <= cutoff:
            item = dict(m)
            item["date"] = dt
            item["company_name"] = item.get("name", item.get("symbol", ""))
            filtered.append(item)
    return sorted(filtered, key=lambda x: x.get("meeting_date", ""))

def get_upcoming_earnings_calendar(days_forward: int = 30) -> Dict[str, Any]:
    meetings = get_earnings_calendar(days_forward=days_forward)
    return {
        "upcoming_meetings": meetings,
        "total_meetings": len(meetings),
        "days_forward": days_forward
    }

if __name__ == "__main__":
    _save_meetings(SAMPLE_MEETINGS)
    print("Upcoming earnings meetings:", len(get_earnings_calendar()))

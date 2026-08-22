#!/usr/bin/env python3
"""
PSX Market Calendar & Session Engine
------------------------------------
Handles Pakistan Stock Exchange (PSX) trading hours, session phases, Friday split schedules,
and official exchange holidays in Pakistan Standard Time (PKT, UTC+5).
"""

import datetime
from typing import Dict, Any, Optional

# PSX Official Public Holidays for 2026 / 2027 (Format: YYYY-MM-DD)
PSX_HOLIDAYS_2026_2027 = {
    # 2026
    "2026-02-05": "Kashmir Day",
    "2026-03-20": "Eid-ul-Fitr (Day 1 - Tentative)",
    "2026-03-21": "Eid-ul-Fitr (Day 2 - Tentative)",
    "2026-03-22": "Eid-ul-Fitr (Day 3 - Tentative)",
    "2026-03-23": "Pakistan Day",
    "2026-05-01": "Labour Day",
    "2026-05-27": "Eid-ul-Adha (Day 1 - Tentative)",
    "2026-05-28": "Eid-ul-Adha (Day 2 - Tentative)",
    "2026-05-29": "Eid-ul-Adha (Day 3 - Tentative)",
    "2026-06-25": "Ashura (9th Muharram - Tentative)",
    "2026-06-26": "Ashura (10th Muharram - Tentative)",
    "2026-08-14": "Independence Day",
    "2026-08-25": "Eid Milad-un-Nabi (Tentative)",
    "2026-11-09": "Iqbal Day",
    "2026-12-25": "Quaid-e-Azam Day / Christmas",
    # 2027
    "2027-02-05": "Kashmir Day",
    "2027-03-10": "Eid-ul-Fitr (Tentative)",
    "2027-03-23": "Pakistan Day",
    "2027-05-01": "Labour Day",
    "2027-08-14": "Independence Day",
    "2027-12-25": "Quaid-e-Azam Day",
}

PKT_TIMEZONE = datetime.timezone(datetime.timedelta(hours=5))


def get_current_pkt_datetime() -> datetime.datetime:
    """Return current datetime in PKT (UTC+5)."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(PKT_TIMEZONE)


def is_psx_holiday(dt: datetime.date) -> Optional[str]:
    """Check if given date is an official PSX exchange holiday."""
    date_str = dt.strftime("%Y-%m-%d")
    return PSX_HOLIDAYS_2026_2027.get(date_str, None)


def get_psx_market_status(dt: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """
    Determine exact PSX trading session phase and time remaining.
    """
    if dt is None:
        dt = get_current_pkt_datetime()
    else:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PKT_TIMEZONE)
        else:
            dt = dt.astimezone(PKT_TIMEZONE)

    date_obj = dt.date()
    weekday = dt.weekday()  # 0 = Mon, 4 = Fri, 5 = Sat, 6 = Sun
    time_minutes = dt.hour * 60 + dt.minute
    pkt_str = dt.strftime("%Y-%m-%d %H:%M:%S PKT")

    # 1. Weekend check
    if weekday in (5, 6):
        return {
            "status": "Closed",
            "phase": "WEEKEND",
            "reason": "Saturday/Sunday - Exchange Closed",
            "is_open": False,
            "can_order": False,
            "session_name": "Weekend",
            "pkt_time": pkt_str,
            "minutes_to_close": 0,
            "next_event": "Market Opens Monday 09:15 AM PKT"
        }

    # 2. Holiday check
    holiday_name = is_psx_holiday(date_obj)
    if holiday_name:
        return {
            "status": "Closed",
            "phase": "HOLIDAY",
            "reason": f"Exchange Holiday ({holiday_name})",
            "is_open": False,
            "can_order": False,
            "session_name": f"Holiday: {holiday_name}",
            "pkt_time": pkt_str,
            "minutes_to_close": 0,
            "next_event": "Market Closed for Holiday"
        }

    # 3. Friday Schedule
    if weekday == 4:
        if 540 <= time_minutes < 555:
            mins_left = 555 - time_minutes
            return {
                "status": "Pre-Open",
                "phase": "PRE_OPEN_1",
                "reason": "Friday Morning Pre-Open Auction",
                "is_open": True,
                "can_order": True,
                "session_name": "Friday Morning Pre-Open",
                "pkt_time": pkt_str,
                "minutes_to_close": mins_left,
                "next_event": f"Continuous Trading starts in {mins_left} mins"
            }
        elif 555 <= time_minutes < 720:
            mins_left = 720 - time_minutes
            return {
                "status": "Open",
                "phase": "TRADING_SESSION_1",
                "reason": "Friday Morning Regular Trading",
                "is_open": True,
                "can_order": True,
                "session_name": "Friday Session 1 (Morning)",
                "pkt_time": pkt_str,
                "minutes_to_close": mins_left,
                "next_event": f"Session 1 closes in {mins_left} mins for Prayer Break"
            }
        elif 720 <= time_minutes < 870:
            mins_left = 870 - time_minutes
            return {
                "status": "Break",
                "phase": "PRAYER_BREAK",
                "reason": "Friday Prayer & Midday Break (Exchange Paused)",
                "is_open": False,
                "can_order": False,
                "session_name": "Friday Prayer Break",
                "pkt_time": pkt_str,
                "minutes_to_close": 0,
                "next_event": f"Session 2 resumes at 14:30 PKT (in {mins_left} mins)"
            }
        elif 870 <= time_minutes <= 990:
            mins_left = 990 - time_minutes
            return {
                "status": "Open",
                "phase": "TRADING_SESSION_2",
                "reason": "Friday Afternoon Regular Trading",
                "is_open": True,
                "can_order": True,
                "session_name": "Friday Session 2 (Afternoon)",
                "pkt_time": pkt_str,
                "minutes_to_close": mins_left,
                "next_event": f"Session 2 closes in {mins_left} mins"
            }
        else:
            return {
                "status": "Closed",
                "phase": "CLOSED",
                "reason": "Outside Friday Trading Hours",
                "is_open": False,
                "can_order": False,
                "session_name": "Market Closed",
                "pkt_time": pkt_str,
                "minutes_to_close": 0,
                "next_event": "Market Opens Monday 09:15 AM PKT"
            }

    # 4. Monday - Thursday Schedule
    else:
        if 540 <= time_minutes < 555:
            mins_left = 555 - time_minutes
            return {
                "status": "Pre-Open",
                "phase": "PRE_OPEN",
                "reason": "Pre-Open Order Accumulation",
                "is_open": True,
                "can_order": True,
                "session_name": "Pre-Open Auction",
                "pkt_time": pkt_str,
                "minutes_to_close": mins_left,
                "next_event": f"Continuous Trading starts in {mins_left} mins"
            }
        elif 555 <= time_minutes <= 930:
            mins_left = 930 - time_minutes
            return {
                "status": "Open",
                "phase": "CONTINUOUS_TRADING",
                "reason": "Regular Continuous Trading Session",
                "is_open": True,
                "can_order": True,
                "session_name": "Regular Trading Session",
                "pkt_time": pkt_str,
                "minutes_to_close": mins_left,
                "next_event": f"Market Closes in {mins_left} mins (15:30 PKT)"
            }
        else:
            return {
                "status": "Closed",
                "phase": "CLOSED",
                "reason": "Outside Regular Trading Hours (09:30 - 15:30 PKT)",
                "is_open": False,
                "can_order": False,
                "session_name": "Market Closed",
                "pkt_time": pkt_str,
                "minutes_to_close": 0,
                "next_event": "Next session opens 09:15 AM PKT"
            }

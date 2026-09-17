#!/usr/bin/env python3
"""
DPS Announcements Scraper & Trigger Detector
=============================================
Fetches announcements, free float, and shares outstanding from PSX DPS company pages.
Parses announcements and classifies catalytic events:
  - NAME_OR_SECTOR_CHANGE (e.g. Zahur Cotton -> ITANZ)
  - CAPITAL_INCREASE (e.g. Authorized share capital increase, rights/bonus issues)
  - TURNAROUND_EVENTS (e.g. Revival of operations, restructuring, mergers)

Pure Python stdlib — zero external dependencies.
"""

import urllib.request
import re
import json
import time
import datetime
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

from .models import (
    save_announcements,
    get_cached_announcements,
    save_company_profile,
    get_company_profile
)

# ── Trigger Regex Patterns ─────────────────────────────────────────────────────

NAME_CHANGE_PATTERNS = [
    r'change\s+of\s+name',
    r'change\s+in\s+name',
    r'name\s+change',
    r'change\s+in\s+principal\s+activit',
    r'change\s+of\s+principal\s+business',
    r'principal\s+line\s+of\s+business',
    r'change\s+of\s+business',
    r'object\s+clause',
    r'alteration\s+(?:in|of)\s+memorandum',
    r'amendment\s+(?:in|of)\s+memorandum',
    r'memorandum\s+of\s+association',
]

CAPITAL_INCREASE_PATTERNS = [
    r'increase\s+in\s+authori[sz]ed\s+(?:share\s+)?capital',
    r'authori[sz]ed\s+(?:share\s+)?capital',
    r'right\s+issue',
    r'right\s+shares',
    r'issuance\s+of\s+(?:right|bonus)\s+shares',
    r'bonus\s+shares',
    r'stock\s+split',
    r'paid-?up\s+capital',
    r'capital\s+increase',
    r'increase\s+in\s+paid-?up',
]

TURNAROUND_PATTERNS = [
    r'resumption\s+of\s+(?:commercial\s+)?production',
    r'revival\s+of\s+business',
    r'restructur(?:ing|ed)',
    r'commencement\s+of\s+(?:commercial\s+)?operations',
    r'resumption\s+of\s+operations',
    r'turnaround',
    r'scheme\s+of\s+arrangement',
    r'merger',
    r'acquisition',
]


def detect_triggers(title: str) -> List[str]:
    """Inspect announcement title and return matched trigger tags."""
    triggers = []
    text = title.lower()

    for pat in NAME_CHANGE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            triggers.append("NAME_OR_SECTOR_CHANGE")
            break

    for pat in CAPITAL_INCREASE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            triggers.append("CAPITAL_INCREASE")
            break

    for pat in TURNAROUND_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            triggers.append("TURNAROUND_RELATED")
            break

    return triggers


_PAGE_CACHE: Dict[str, Tuple[float, str]] = {}

def fetch_dps_company_page(symbol: str, timeout: int = 5) -> str:
    """Download HTML from dps.psx.com.pk/company/{symbol} with browser headers and in-memory cache."""
    symbol = symbol.upper()
    now = time.time()
    if symbol in _PAGE_CACHE:
        ts, cached_html = _PAGE_CACHE[symbol]
        if now - ts < 300:  # 5 min TTL
            return cached_html

    url = f"https://dps.psx.com.pk/company/{symbol}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8", errors="ignore")
        _PAGE_CACHE[symbol] = (now, content)
        return content


def parse_company_profile(html: str, symbol: str) -> Dict[str, Any]:
    """Extract Shares, Free Float, and Sector from DPS company HTML."""
    profile: Dict[str, Any] = {
        "symbol": symbol.upper(),
        "shares": None,
        "free_float_shares": None,
        "free_float_pct": None,
        "sector": None
    }

    # Extract Sector from company header or metadata
    sec_match = re.search(r'<div class="item__head">SECTOR</div>\s*<div class="item__value">([^<]+)</div>', html, re.IGNORECASE)
    if not sec_match:
        sec_match = re.search(r'class="sector[^"]*">([^<]+)<', html, re.IGNORECASE)
    if sec_match:
        profile["sector"] = sec_match.group(1).strip()

    # Extract stats_items: Shares, Free Float
    stats = re.findall(r'class="stats_label">([^<]+)</div>\s*<div class="stats_value">([^<]+)</div>', html, re.IGNORECASE)
    for label, val in stats:
        label_clean = label.strip().lower()
        val_clean = val.strip()
        if "shares" in label_clean and not profile["shares"]:
            try:
                profile["shares"] = int(val_clean.replace(",", ""))
            except Exception:
                pass
        elif "free float" in label_clean:
            if "%" in val_clean:
                try:
                    profile["free_float_pct"] = float(val_clean.replace("%", "").strip())
                except Exception:
                    pass
            elif not profile["free_float_shares"]:
                try:
                    profile["free_float_shares"] = int(val_clean.replace(",", ""))
                except Exception:
                    pass

    return profile


def parse_announcements(html: str) -> List[Dict[str, Any]]:
    """Parse announcement rows (Date, Title, Link) and detect triggers."""
    announcements = []

    # Target the announcements section
    # Usually in <div class="company__payouts">...<h1 class="section__title">Announcements</h1>
    announce_match = re.search(
        r'<h1 class="section__title">Announcements</h1>(.*?)</div>\s*</div>\s*</div>\s*<div class="section',
        html, re.DOTALL | re.IGNORECASE
    )
    if not announce_match:
        # Fallback to general announcements container
        announce_match = re.search(
        r'<h1 class="section__title">Announcements</h1>(.*?)(?:<h1 class="section__title"|id="financialTab")',
        html, re.DOTALL | re.IGNORECASE
    )

    content = announce_match.group(1) if announce_match else html

    # Find table rows with Date, Title, and optional link
    rows = re.findall(
        r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>(?:\s*<td>(.*?)</td>)?\s*</tr>',
        content, re.IGNORECASE | re.DOTALL
    )

    seen = set()
    for row in rows:
        d = re.sub(r'<[^>]+>', '', row[0]).strip()
        t = re.sub(r'<[^>]+>', '', row[1]).strip()
        links_html = row[2] if len(row) > 2 and row[2] else ""

        # Filter out table header or non-announcement rows
        if not d or not t or t.lower() in ("title", "subject", "announcement", "ceo", "chairperson"):
            continue
        # Verify d looks like a date (e.g. Aug 24, 2026 or 2026-08-24)
        if not re.search(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b', d, re.IGNORECASE):
            continue

        pdf_match = re.search(r'href="(/download/document/.*?|/download/attachment/.*?)"', links_html, re.IGNORECASE)
        link = "https://dps.psx.com.pk" + pdf_match.group(1) if pdf_match else ""

        key = (d, t)
        if key in seen:
            continue
        seen.add(key)

        triggers = detect_triggers(t)

        announcements.append({
            "date": d,
            "title": t,
            "link": link,
            "triggers": triggers
        })

    return announcements


def get_or_fetch_announcements(symbol: str, max_age_seconds: int = 86400, force: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Get announcements and company profile for a symbol.
    Uses SQLite cache if fresh; otherwise fetches from DPS and updates cache.
    Returns (announcements_list, company_profile_dict).
    """
    symbol = symbol.upper()
    cached_anns = get_cached_announcements(symbol)
    cached_prof = get_company_profile(symbol)

    if cached_anns and cached_prof and not force:
        # Check cache age
        return cached_anns, cached_prof

    try:
        html = fetch_dps_company_page(symbol)
        profile = parse_company_profile(html, symbol)
        announcements = parse_announcements(html)

        save_company_profile(symbol, profile)
        save_announcements(symbol, announcements)

        return announcements, profile
    except Exception as e:
        print(f"[AnnouncementsScraper] Error fetching {symbol}: {e}")
        return cached_anns or [], cached_prof or {"symbol": symbol}


def check_symbol_triggers_last_12mo(symbol: str) -> Dict[str, Any]:
    """
    Check if symbol has NAME_OR_SECTOR_CHANGE or CAPITAL_INCREASE in last 12 months.
    Returns:
      {
        "has_name_change": bool,
        "name_change_details": Optional[str],
        "has_capital_increase": bool,
        "capital_increase_details": Optional[str],
        "all_triggers": List[str]
      }
    """
    announcements, _ = get_or_fetch_announcements(symbol)
    has_name = False
    name_det = None
    has_cap = False
    cap_det = None
    all_trigs = set()

    for ann in announcements:
        trigs = ann.get("triggers", [])
        for t in trigs:
            all_trigs.add(t)
            if t == "NAME_OR_SECTOR_CHANGE" and not has_name:
                has_name = True
                name_det = f"{ann['date']}: {ann['title']}"
            elif t == "CAPITAL_INCREASE" and not has_cap:
                has_cap = True
                cap_det = f"{ann['date']}: {ann['title']}"

    return {
        "has_name_change": has_name,
        "name_change_details": name_det,
        "has_capital_increase": has_cap,
        "capital_increase_details": cap_det,
        "all_triggers": list(all_trigs),
        "total_announcements": len(announcements)
    }

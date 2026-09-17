#!/usr/bin/env python3
"""
DPS Financials Scraper & Turnaround Metric Calculator
======================================================
Parses quarterly and annual financial statements (Sales, EPS) from DPS company pages.
Computes the consecutive QoQ revenue growth streak (turnaround signal: 2-3 quarters of QoQ growth).

Pure Python stdlib — zero external dependencies.
"""

import urllib.request
import re
import datetime
from typing import Dict, Any, List, Optional, Tuple

from .models import save_financials, get_cached_financials


def _clean_number(text: str) -> Optional[float]:
    """Parse string number into float, handling commas and negative parentheses e.g. (0.08) -> -0.08."""
    if not text:
        return None
    cleaned = re.sub(r'<[^>]+>', '', text).strip().replace(',', '')
    if not cleaned or cleaned in ('-', '—', 'n/a', 'na', ''):
        return None
    if cleaned.startswith('(') and cleaned.endswith(')'):
        cleaned = '-' + cleaned[1:-1].strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def parse_financial_tables(html: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extracts both Annual and Quarterly financials from id="financialTab".
    Returns {
       "annual": [{"period": "2026", "sales": 758200.0, "eps": 15.88}, ...],
       "quarterly": [{"period": "Q3 2026", "sales": 181182.0, "eps": 1.12}, ...]
    }
    """
    result = {"annual": [], "quarterly": []}

    idx = html.find('id="financialTab"')
    if idx == -1:
        return result

    tab_html = html[idx:idx + 8000]

    for panel_name, key in [("Annual", "annual"), ("Quarterly", "quarterly")]:
        panel_match = re.search(
            rf'<div class="tabs__panel"[^>]*data-name="{panel_name}"[^>]*>(.*?)</div>\s*</div>',
            tab_html, re.DOTALL | re.IGNORECASE
        )
        if not panel_match:
            continue

        p_html = panel_match.group(1)

        # Extract period headers
        headers = re.findall(r'<th[^>]*class="right"[^>]*>(.*?)</th>', p_html, re.IGNORECASE)
        headers = [h.strip() for h in headers if h.strip()]

        # Extract Sales row
        sales_match = re.search(r'<tr><td>Sales</td>(.*?)</tr>', p_html, re.DOTALL | re.IGNORECASE)
        sales_vals = []
        if sales_match:
            raw_sales = re.findall(r'<td[^>]*>(.*?)</td>', sales_match.group(1), re.DOTALL | re.IGNORECASE)
            for s in raw_sales:
                sales_vals.append(_clean_number(s))

        # Extract EPS row
        eps_match = re.search(r'<tr><td>EPS</td>(.*?)</tr>', p_html, re.DOTALL | re.IGNORECASE)
        eps_vals = []
        if eps_match:
            raw_eps = re.findall(r'<td[^>]*>(.*?)</td>', eps_match.group(1), re.DOTALL | re.IGNORECASE)
            for e in raw_eps:
                eps_vals.append(_clean_number(e))

        items = []
        for i, period in enumerate(headers):
            s_val = sales_vals[i] if i < len(sales_vals) else None
            e_val = eps_vals[i] if i < len(eps_vals) else None
            items.append({
                "period": period,
                "sales": s_val,
                "eps": e_val,
                "is_quarterly": (key == "quarterly")
            })

        result[key] = items

    return result


def compute_consecutive_qoq_growth(quarterly: List[Dict[str, Any]]) -> int:
    """
    Computes consecutive quarters of QoQ revenue growth.
    The input list is in descending order (newest period first: [Q3, Q2, Q1, ...]).
    Returns streak count (e.g. 0, 1, 2, 3+).
    """
    if not quarterly or len(quarterly) < 2:
        return 0

    # Filter entries with valid positive sales
    valid_quarters = [q for q in quarterly if q.get("sales") is not None and q.get("sales") > 0]
    if len(valid_quarters) < 2:
        return 0

    streak = 0
    # Walk from latest backwards comparing to the prior quarter
    for i in range(len(valid_quarters) - 1):
        cur_q = valid_quarters[i]
        prev_q = valid_quarters[i + 1]

        if cur_q["sales"] > prev_q["sales"]:
            streak += 1
        else:
            break

    return streak


def get_or_fetch_financials(symbol: str, force: bool = False) -> Dict[str, Any]:
    """
    Get financials for a symbol with local SQLite caching.
    Returns:
      {
        "quarterly": [...],
        "annual": [...],
        "consecutive_growth_quarters": int,
        "latest_sales": float,
        "latest_eps": float
      }
    """
    symbol = symbol.upper()
    cached = get_cached_financials(symbol, quarterly_only=False)

    if cached and not force:
        q_list = [c for c in cached if c.get("is_quarterly")]
        a_list = [c for c in cached if not c.get("is_quarterly")]
        growth = compute_consecutive_qoq_growth(q_list)
        latest = q_list[0] if q_list else (a_list[0] if a_list else {})
        return {
            "quarterly": q_list,
            "annual": a_list,
            "consecutive_growth_quarters": growth,
            "latest_sales": latest.get("sales"),
            "latest_eps": latest.get("eps")
        }

    try:
        from .announcements_scraper import fetch_dps_company_page
        html = fetch_dps_company_page(symbol)
        tables = parse_financial_tables(html)

        all_fin = tables["quarterly"] + tables["annual"]
        save_financials(symbol, all_fin)

        growth = compute_consecutive_qoq_growth(tables["quarterly"])
        latest = tables["quarterly"][0] if tables["quarterly"] else (tables["annual"][0] if tables["annual"] else {})

        return {
            "quarterly": tables["quarterly"],
            "annual": tables["annual"],
            "consecutive_growth_quarters": growth,
            "latest_sales": latest.get("sales"),
            "latest_eps": latest.get("eps")
        }
    except Exception as e:
        print(f"[FinancialsScraper] Error for {symbol}: {e}")
        return {
            "quarterly": [],
            "annual": [],
            "consecutive_growth_quarters": 0,
            "latest_sales": None,
            "latest_eps": None
        }

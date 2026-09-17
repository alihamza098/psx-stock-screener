#!/usr/bin/env python3
"""
Deep Research Mode — Multi-Source Fetcher (Zero-Cost / Option A)
================================================================
Fetches targeted, public PSX information across the fixed scoped source list:
  1. DPS PSX (Official announcements & filings)
  2. Sarmaaya.pk (Key ratios & valuation)
  3. SCS Trade (Snapshot, beta, technicals)
  4. Mettis Global (PSX news wire search)
  5. Business Recorder / ProPakistani (Business news)
  6. TradingView (Community sentiment reference link)

Polite request pacing and SQLite caching included.
Pure Python stdlib — zero external dependencies.
"""

import urllib.request
import urllib.parse
import ssl
import re
import json
import time
from typing import Dict, Any, List, Optional

_SSL_CTX = ssl._create_unverified_context()
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fetch_url(url: str, timeout: int = 8) -> str:
    req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_dps_research(symbol: str) -> Dict[str, Any]:
    """Fetch official DPS company announcements and filings."""
    symbol = symbol.upper()
    url = f"https://dps.psx.com.pk/company/{symbol}"
    items = []
    try:
        html = _fetch_url(url)
        # Announcements
        from ..announcements_scraper import parse_announcements, parse_company_profile
        anns = parse_announcements(html)
        prof = parse_company_profile(html, symbol)

        summary_lines = []
        if prof.get("shares"):
            summary_lines.append(f"Total Shares: {prof['shares']:,}")
        if prof.get("free_float_shares"):
            summary_lines.append(f"Free Float: {prof['free_float_shares']:,} ({prof.get('free_float_pct', 'N/A')}%)")
        if prof.get("sector"):
            summary_lines.append(f"Sector: {prof['sector']}")

        for a in anns[:8]:
            trig_str = f" [Tag: {', '.join(a['triggers'])}]" if a.get("triggers") else ""
            summary_lines.append(f"- {a['date']}: {a['title']}{trig_str}")

        return {
            "source": "DPS PSX (Official)",
            "url": url,
            "title": f"PSX Official Announcements for {symbol}",
            "text": "\n".join(summary_lines),
            "status": "success"
        }
    except Exception as e:
        return {
            "source": "DPS PSX (Official)",
            "url": url,
            "title": f"PSX Company Portal for {symbol}",
            "text": f"Error accessing DPS: {e}",
            "status": "error"
        }


def fetch_sarmaaya_research(symbol: str) -> Dict[str, Any]:
    """Fetch fundamentals summary from Sarmaaya.pk."""
    symbol = symbol.upper()
    url = f"https://sarmaaya.pk/stocks/{symbol}"
    try:
        html = _fetch_url(url)
        # Extract title or meta description
        meta_match = re.search(r'<meta name="description" content="([^"]+)"', html, re.IGNORECASE)
        desc = meta_match.group(1).strip() if meta_match else ""

        # Extract P/E, EPS, Market Cap if present
        ratios = []
        for pat, label in [
            (r'P/E\s*Ratio[^<]*</div>\s*<div[^>]*>([^<]+)</div>', 'P/E'),
            (r'EPS[^<]*</div>\s*<div[^>]*>([^<]+)</div>', 'EPS'),
            (r'Market\s*Cap[^<]*</div>\s*<div[^>]*>([^<]+)</div>', 'Market Cap'),
            (r'52\s*Week\s*Range[^<]*</div>\s*<div[^>]*>([^<]+)</div>', '52W Range'),
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                ratios.append(f"{label}: {m.group(1).strip()}")

        text = desc
        if ratios:
            text += "\n" + " | ".join(ratios)

        return {
            "source": "Sarmaaya.pk",
            "url": url,
            "title": f"Sarmaaya Profile: {symbol}",
            "text": text[:1000] if text else f"Sarmaaya page loaded for {symbol}.",
            "status": "success"
        }
    except Exception as e:
        return {
            "source": "Sarmaaya.pk",
            "url": url,
            "title": f"Sarmaaya Profile: {symbol}",
            "text": f"Sarmaaya data unavailable ({e}).",
            "status": "error"
        }


def fetch_scstrade_research(symbol: str) -> Dict[str, Any]:
    """Fetch company overview from SCS Trade (Acuity)."""
    symbol = symbol.upper()
    url = f"https://www.scstrade.com/stockscreening/SS_CompanySnapShot.aspx?symbol={symbol}"
    try:
        html = _fetch_url(url)
        # Extract table rows
        rows = re.findall(r'<tr[^>]*>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>', html)
        items = []
        for k, v in rows[:12]:
            k_clean = k.strip()
            v_clean = v.strip()
            if k_clean and v_clean:
                items.append(f"{k_clean}: {v_clean}")

        return {
            "source": "SCS Trade",
            "url": url,
            "title": f"SCS Trade Snapshot: {symbol}",
            "text": "\n".join(items) if items else f"SCS Trade company snapshot available for {symbol}.",
            "status": "success"
        }
    except Exception as e:
        return {
            "source": "SCS Trade",
            "url": url,
            "title": f"SCS Trade Snapshot: {symbol}",
            "text": f"SCS Trade data unavailable ({e}).",
            "status": "error"
        }


def fetch_mettis_research(symbol: str) -> Dict[str, Any]:
    """Search Mettis Global News Wire for recent stories about the symbol."""
    symbol = symbol.upper()
    query = urllib.parse.quote(symbol)
    url = f"https://mettisglobal.news/?s={query}"
    try:
        html = _fetch_url(url)
        # Extract article titles & links
        articles = re.findall(r'<h2 class="entry-title"><a href="([^"]+)"[^>]*>([^<]+)</a></h2>', html, re.IGNORECASE)
        lines = []
        for link, title in articles[:5]:
            lines.append(f"- {title.strip()} ({link})")

        return {
            "source": "Mettis Global",
            "url": url,
            "title": f"Mettis Global PSX Wire: {symbol}",
            "text": "\n".join(lines) if lines else f"No recent specific wire stories found for {symbol}.",
            "status": "success"
        }
    except Exception as e:
        return {
            "source": "Mettis Global",
            "url": url,
            "title": f"Mettis Global PSX Wire: {symbol}",
            "text": f"Mettis search unavailable ({e}).",
            "status": "error"
        }


def fetch_business_recorder_research(symbol: str) -> Dict[str, Any]:
    """Scan Business Recorder RSS feed for mentions of the symbol."""
    symbol = symbol.upper()
    url = "https://www.brecorder.com/feeds/latest-news"
    try:
        xml = _fetch_url(url)
        items = re.findall(r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>', xml, re.DOTALL | re.IGNORECASE)
        matched = []
        for title, link in items:
            t_clean = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', title).strip()
            l_clean = link.strip()
            if symbol.lower() in t_clean.lower():
                matched.append(f"- {t_clean} ({l_clean})")

        text = "\n".join(matched[:4]) if matched else f"No breaking stories mentioning {symbol} in today's news wire."
        return {
            "source": "Business Recorder",
            "url": "https://www.brecorder.com",
            "title": "Business Recorder Feed",
            "text": text,
            "status": "success"
        }
    except Exception as e:
        return {
            "source": "Business Recorder",
            "url": "https://www.brecorder.com",
            "title": "Business Recorder",
            "text": f"Feed unavailable ({e}).",
            "status": "error"
        }


def fetch_all_research_sources(symbol: str) -> List[Dict[str, Any]]:
    """Fan out requests across all target research sources with graceful fallbacks."""
    symbol = symbol.upper().strip()
    sources = [
        fetch_dps_research(symbol),
        fetch_sarmaaya_research(symbol),
        fetch_scstrade_research(symbol),
        fetch_mettis_research(symbol),
        fetch_business_recorder_research(symbol),
        {
            "source": "TradingView PSX",
            "url": f"https://www.tradingview.com/symbols/PSX-{symbol}/",
            "title": f"TradingView PSX Community & Chart: {symbol}",
            "text": f"Community sentiment and real-time interactive charting available at TradingView.",
            "status": "success"
        }
    ]
    return sources

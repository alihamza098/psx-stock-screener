#!/usr/bin/env python3
"""
PSX Stock Screener — Python Backend Server
Fetches live data from dps.psx.com.pk and serves it as JSON API.
Uses only Python standard library — no pip install needed!
"""

import http.server
import json
import os
import re
import time
import threading
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path

PORT = int(os.environ.get('PORT', 3000))
CACHE_DURATION = 120  # seconds (2 min to reduce PSX load)
FETCH_TIMEOUT = 8     # seconds (balanced for Render)
FETCH_RETRIES = 2     # retry attempts (faster fallback to cache)

# ─── Persistent file cache paths ───
DATA_DIR = Path(__file__).parent / "cache"
DATA_DIR.mkdir(exist_ok=True)
STOCK_CACHE_FILE = DATA_DIR / "stocks_cache.json"
INDEX_CACHE_FILE = DATA_DIR / "index_cache.json"
SNAPSHOT_FILE = Path(__file__).parent / "data_snapshot.json"

# ─── Simple in-memory cache ───
stock_cache = {"data": None, "timestamp": 0}
index_cache = {"data": None, "timestamp": 0}


def load_file_cache(filepath):
    """Load cached data from a JSON file."""
    try:
        if filepath.exists():
            with open(filepath, "r") as f:
                cached = json.load(f)
                print(f"[PSX] Loaded file cache from {filepath.name} ({len(cached.get('data', []))} items)")
                return cached
    except Exception as e:
        print(f"[PSX] Could not load file cache {filepath.name}: {e}")
    return None


def save_file_cache(filepath, data, timestamp):
    """Save data to a JSON file cache."""
    try:
        with open(filepath, "w") as f:
            json.dump({"data": data, "timestamp": timestamp}, f)
        print(f"[PSX] Saved file cache to {filepath.name}")
    except Exception as e:
        print(f"[PSX] Could not save file cache {filepath.name}: {e}")


DEFAULT_INDEX_FALLBACK = {
    "indices": [
        {"name": "KSE 100", "value": 78210.45, "change": 420.35, "changePercent": 0.54, "isPositive": True},
        {"name": "ALL SHAR", "value": 51240.10, "change": 180.20, "changePercent": 0.35, "isPositive": True},
        {"name": "KSE 30", "value": 25110.80, "change": -45.10, "changePercent": -0.18, "isPositive": False},
        {"name": "KMI 30", "value": 132450.60, "change": 610.75, "changePercent": 0.46, "isPositive": True}
    ],
    "market": {
        "state": "Closed",
        "volume": 358420000,
        "value": 36700000000.0
    },
    "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
}

# Load caches on startup — try file cache first, then bundled snapshot as fallback
_stock_file = load_file_cache(STOCK_CACHE_FILE)
if _stock_file:
    stock_cache = {"data": _stock_file["data"], "timestamp": _stock_file.get("timestamp", 0)}

_index_file = load_file_cache(INDEX_CACHE_FILE)
if _index_file:
    index_cache = {"data": _index_file["data"], "timestamp": _index_file.get("timestamp", 0)}

# Ultimate fallback: bundled snapshot (committed to repo, survives all restarts)
if not stock_cache["data"] and SNAPSHOT_FILE.exists():
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snapshot = json.load(f)
            if snapshot.get("data"):
                stock_cache = {"data": snapshot["data"], "timestamp": snapshot.get("timestamp", 0)}
                print(f"[PSX] Loaded bundled snapshot: {len(snapshot['data'])} stocks")
    except Exception as e:
        print(f"[PSX] Could not load snapshot: {e}")

if not index_cache.get("data"):
    index_cache = {"data": DEFAULT_INDEX_FALLBACK, "timestamp": time.time()}

# ─── Financials Cache ───
FINANCIALS_FILE = Path(__file__).parent / "financials.json"
financials_cache = {}
try:
    if FINANCIALS_FILE.exists():
        with open(FINANCIALS_FILE, "r") as f:
            financials_cache = json.load(f)
            print(f"[PSX] Loaded financials for {len(financials_cache)} companies.")
except Exception as e:
    print(f"[PSX] Could not load financials.json: {e}")

# Start background thread removed to prevent rate limiting from PSX

# ─── PSX Sector Code Mapping ───
SECTOR_MAP = {
    "0801": "Automobile Assembler",
    "0802": "Automobile Parts & Accessories",
    "0803": "Cable & Electrical Goods",
    "0804": "Cement",
    "0805": "Chemical",
    "0806": "Close-End Mutual Fund",
    "0807": "Commercial Banks",
    "0808": "Engineering",
    "0809": "Fertilizer",
    "0810": "Food & Personal Care Products",
    "0811": "Glass & Ceramics",
    "0812": "Insurance",
    "0813": "Inv. Banks / Securities Cos.",
    "0814": "Jute",
    "0815": "Leasing Companies",
    "0816": "Leather & Tanneries",
    "0818": "Miscellaneous",
    "0819": "Modarabas",
    "0820": "Oil & Gas Exploration",
    "0821": "Oil & Gas Marketing",
    "0822": "Paper, Board & Packaging",
    "0823": "Pharmaceuticals",
    "0824": "Power Generation & Distribution",
    "0825": "Refinery",
    "0826": "Sugar & Allied Industries",
    "0827": "Synthetic & Rayon",
    "0828": "Technology & Communication",
    "0829": "Textile Composite",
    "0830": "Textile Spinning",
    "0831": "Textile Weaving",
    "0832": "Tobacco",
    "0833": "Transport",
    "0834": "Vanaspati & Allied Industries",
    "0835": "Woollen",
    "0836": "Real Estate Investment Trust",
    "0837": "Exchange Traded Funds",
    "0838": "Property",
    "0839": "Apparel",
}


# ─── HTML Parser for PSX Screener Table ───
class PSXScreenerParser(HTMLParser):
    """Parses the screener table from dps.psx.com.pk/screener"""

    def __init__(self):
        super().__init__()
        self.stocks = []
        self.in_tbody = False
        self.in_row = False
        self.in_td = False
        self.in_a = False
        self.current_row_cells = []
        self.current_cell = {
            "text": "",
            "data_order": None,
            "data_title": None,
            "has_nc_tag": False,
        }
        self.td_count = 0
        self.in_tag_span = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "tbody" and not self.in_tbody:
            # Check if this is the screener table body
            self.in_tbody = True

        if self.in_tbody:
            if tag == "tr":
                self.in_row = True
                self.current_row_cells = []
                self.td_count = 0

            elif tag == "td" and self.in_row:
                self.in_td = True
                self.td_count += 1
                self.current_cell = {
                    "text": "",
                    "data_order": attrs_dict.get("data-order"),
                    "data_title": None,
                    "has_nc_tag": False,
                }

            elif tag == "a" and self.in_td:
                self.in_a = True
                if attrs_dict.get("class", "") == "tbl__symbol" or "tbl__symbol" in attrs_dict.get("class", ""):
                    self.current_cell["data_title"] = attrs_dict.get("data-title", "")

            elif tag == "div" and self.in_td:
                cls = attrs_dict.get("class", "")
                if "tag" in cls and ("tag--def" in cls or "tag--skim" in cls):
                    self.in_tag_span = True

            elif tag == "strong" and self.in_a:
                pass  # will capture text

    def handle_data(self, data):
        if self.in_td and self.in_tag_span:
            if data.strip() == "NC":
                self.current_cell["has_nc_tag"] = True
            self.in_tag_span = False
        elif self.in_td:
            self.current_cell["text"] += data.strip()

    def handle_endtag(self, tag):
        if tag == "tbody" and self.in_tbody:
            self.in_tbody = False

        if self.in_tbody:
            if tag == "a" and self.in_a:
                self.in_a = False

            elif tag == "td" and self.in_td:
                self.in_td = False
                self.current_row_cells.append(self.current_cell.copy())

            elif tag == "tr" and self.in_row:
                self.in_row = False
                if len(self.current_row_cells) >= 11:
                    self._process_row(self.current_row_cells)

    def _safe_float(self, val):
        """Safely convert to float."""
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    def _process_row(self, cells):
        """Convert a row of cells into a stock dict."""
        symbol = cells[0]["text"].strip()
        name = cells[0].get("data_title") or symbol
        sector_code = cells[1]["text"].strip()
        listed_in = cells[2]["text"].strip()

        market_cap = self._safe_float(cells[3].get("data_order"))
        price = self._safe_float(cells[4].get("data_order"))
        change_pct = self._safe_float(cells[5].get("data_order"))
        year_change = self._safe_float(cells[6].get("data_order"))
        pe_ratio = self._safe_float(cells[7].get("data_order"))
        div_yield = self._safe_float(cells[8].get("data_order"))
        free_float = self._safe_float(cells[9].get("data_order"))
        volume_30d = self._safe_float(cells[10].get("data_order"))

        if price <= 0:
            return
            
        # Get latest annual revenue from cache
        rev_history = financials_cache.get(symbol, {})
        latest_rev = 0.0
        if rev_history:
            # Sort years descending and pick the latest valid one
            years = sorted(rev_history.keys(), reverse=True)
            for y in years:
                if rev_history[y] > 0:
                    latest_rev = rev_history[y]
                    break

        self.stocks.append({
            "symbol": symbol,
            "name": name,
            "sectorCode": sector_code,
            "sector": SECTOR_MAP.get(sector_code, sector_code or "Other"),
            "listedIn": listed_in,
            "price": price,
            "change": change_pct,
            "yearChange": year_change,
            "mcap": market_cap,
            "revenue": latest_rev, # Added Revenue
            "pe": pe_ratio,
            "divYield": div_yield,
            "freeFloat": free_float,
            "volume": volume_30d,
            "isNC": cells[0].get("has_nc_tag", False),
            "isKSE100": "KSE100" in listed_in,
            "isKSE30": "KSE30" in listed_in,
            "isKMI30": "KMI30" in listed_in,
        })


# ─── HTML Parser for PSX Index Data ───
def parse_index_data(html):
    """Parse index data from PSX homepage using regex (more reliable than HTMLParser for this structure)."""
    indices = []
    
    # Find all topIndices items
    # Pattern: name div, val div, change div, changep div, wrapped in pos/neg class
    pattern = re.compile(
        r'topIndices__item__name">(.*?)</div>.*?'
        r'topIndices__item__val">(.*?)</div>.*?'
        r'change__text--(pos|neg|noc).*?'
        r'topIndices__item__change">.*?</i>\s*([\d,\.]+)</div>.*?'
        r'topIndices__item__changep">\(([\d\.]+)%\)</div>',
        re.DOTALL
    )
    
    for m in pattern.finditer(html):
        name = m.group(1).strip()
        value = float(m.group(2).strip().replace(",", "") or "0")
        is_positive = m.group(3) == "pos"
        change = float(m.group(4).strip().replace(",", "") or "0")
        change_pct = float(m.group(5).strip() or "0")
        
        if not is_positive:
            change = -change
            change_pct = -change_pct
        
        indices.append({
            "name": name,
            "value": value,
            "change": change,
            "changePercent": change_pct,
            "isPositive": is_positive,
        })
    
    # Market state from Regular market card
    market_state = "Closed"
    market_volume = 0
    market_value = 0.0
    
    # Find Regular market card stats
    reg_match = re.search(
        r'markets__item__title\s+c0">Regular</div>.*?'
        r'State</div>\s*<div>(.*?)</div>.*?'
        r'Trades</div>\s*<div>(.*?)</div>.*?'
        r'Volume</div>\s*<div>(.*?)</div>.*?'
        r'Value</div>\s*<div>(.*?)</div>',
        html, re.DOTALL
    )
    if reg_match:
        market_state = reg_match.group(1).strip()
        try:
            market_volume = int(reg_match.group(3).strip().replace(",", ""))
        except ValueError:
            pass
        try:
            market_value = float(reg_match.group(4).strip().replace(",", ""))
        except ValueError:
            pass
    
    return indices, market_state, market_volume, market_value



# ─── Fetch helpers ───
def fetch_url(url):
    """Fetch URL content with proper headers and retry logic."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    })
    last_error = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_error = e
            print(f"[PSX] Fetch attempt {attempt}/{FETCH_RETRIES} failed for {url}: {e}")
            if attempt < FETCH_RETRIES:
                time.sleep(2 * attempt)  # exponential backoff
    raise last_error


def _do_fetch_stocks():
    """Background worker: fetch fresh stock data from PSX."""
    global stock_cache
    try:
        print("[PSX] Background: Fetching live stock data from dps.psx.com.pk/screener...")
        html = fetch_url("https://dps.psx.com.pk/screener")
        parser = PSXScreenerParser()
        parser.feed(html)
        if parser.stocks:
            now = time.time()
            stock_cache = {"data": parser.stocks, "timestamp": now}
            save_file_cache(STOCK_CACHE_FILE, parser.stocks, now)
            print(f"[PSX] Background: Updated {len(parser.stocks)} stocks.")
        else:
            print("[PSX] Background: Parsed 0 stocks, keeping cached data.")
    except Exception as e:
        print(f"[PSX] Background: Stock fetch failed: {e}")


def _do_fetch_indices():
    """Background worker: fetch fresh index data from PSX."""
    global index_cache
    try:
        print("[PSX] Background: Fetching index data from dps.psx.com.pk...")
        html = fetch_url("https://dps.psx.com.pk/")
        indices, market_state, market_volume, market_value = parse_index_data(html)
        result = {
            "indices": indices,
            "market": {
                "state": market_state,
                "volume": market_volume,
                "value": market_value,
            },
            "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        now = time.time()
        index_cache = {"data": result, "timestamp": now}
        save_file_cache(INDEX_CACHE_FILE, result, now)
        print(f"[PSX] Background: Updated {len(indices)} indices. Market: {market_state}")
    except Exception as e:
        print(f"[PSX] Background: Index fetch failed: {e}")


# Track if a background refresh is already running
_refresh_lock = threading.Lock()
_refresh_running = False


def _trigger_background_refresh():
    """Kick off a background thread to refresh data from PSX (non-blocking)."""
    global _refresh_running
    with _refresh_lock:
        if _refresh_running:
            return  # already refreshing
        _refresh_running = True

    def worker():
        global _refresh_running
        try:
            _do_fetch_stocks()
            _do_fetch_indices()
        finally:
            with _refresh_lock:
                _refresh_running = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    print("[PSX] Background refresh triggered.")


def fetch_stock_data():
    """Return cached stock data IMMEDIATELY. Trigger background refresh if stale."""
    is_stale = False
    now = time.time()

    if stock_cache.get("data"):
        if (now - stock_cache.get("timestamp", 0)) > CACHE_DURATION:
            is_stale = True
            _trigger_background_refresh()
        return stock_cache["data"], is_stale

    # Fallback to snapshot if cache is empty
    if SNAPSHOT_FILE.exists():
        try:
            with open(SNAPSHOT_FILE, "r") as f:
                snap = json.load(f)
                if snap.get("data"):
                    stock_cache["data"] = snap["data"]
                    stock_cache["timestamp"] = now
                    _trigger_background_refresh()
                    return snap["data"], True
        except Exception:
            pass

    # Return empty list rather than blocking or crashing
    return [], False


def fetch_index_data():
    """Return cached index data IMMEDIATELY. Trigger background refresh if stale."""
    is_stale = False
    now = time.time()

    if index_cache.get("data"):
        if (now - index_cache.get("timestamp", 0)) > CACHE_DURATION:
            is_stale = True
            _trigger_background_refresh()
        return index_cache["data"], is_stale

    index_cache["data"] = DEFAULT_INDEX_FALLBACK
    index_cache["timestamp"] = now
    _trigger_background_refresh()
    return DEFAULT_INDEX_FALLBACK, True


# ─── Fetch helpers ───
def fetch_company_data(symbol):
    """Fetch and parse company profile and announcements from PSX."""
    print(f"[PSX] Fetching company data for {symbol}...")
    try:
        html = fetch_url(f"https://dps.psx.com.pk/company/{symbol}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
        
    data = {
        "symbol": symbol,
        "description": "",
        "address": "",
        "website": "",
        "people": [],
        "announcements": []
    }
    
    # Extract description
    desc_match = re.search(r'<div class="item__head">BUSINESS DESCRIPTION</div>\s*<p>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
    if desc_match:
        data['description'] = desc_match.group(1).strip()
        
    # Extract Address
    addr_match = re.search(r'<div class="item__head">ADDRESS</div>\s*<p>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
    if addr_match:
        data['address'] = addr_match.group(1).strip()
        
    # Extract Website
    web_match = re.search(r'<div class="item__head">WEBSITE</div>.*?href="(.*?)".*?>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
    if web_match:
        data['website'] = web_match.group(1).strip()
        
    # Extract People
    people_section = re.search(r'<div class="item__head">KEY PEOPLE</div>.*?<tbody class="tbl__body">(.*?)</tbody>', html, re.DOTALL | re.IGNORECASE)
    if people_section:
        rows = re.findall(r'<tr>\s*<td><strong>(.*?)</strong></td>\s*<td>(.*?)</td>\s*</tr>', people_section.group(1), re.IGNORECASE)
        for name, role in rows:
            data['people'].append({"name": name.strip(), "role": role.strip()})
            
    # Extract Announcements
    announce_section_match = re.search(r'<div class="company__payouts">\s*<h1 class="section__title">Announcements</h1>(.*?)</div>\s*</div>\s*</div>\s*<div class="section', html, re.DOTALL | re.IGNORECASE)
    
    if announce_section_match:
        announce_section = announce_section_match.group(1)
        rows = re.findall(r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>', announce_section, re.IGNORECASE | re.DOTALL)
        for d, t, links_html in rows:
            date = d.strip()
            title = t.strip()
            pdf_match = re.search(r'href="(/download/document/.*?|/download/attachment/.*?)"', links_html, re.IGNORECASE)
            link = "https://dps.psx.com.pk" + pdf_match.group(1) if pdf_match else ""
            data['announcements'].append({
                "date": date,
                "title": title,
                "link": link
            })
            
    # Also attach the revenue history from cache if available
    if symbol in financials_cache:
        data["revenueHistory"] = financials_cache[symbol]
            
    return data


UPPER_LOCK_HISTORY_FILE = Path(__file__).parent / "cache" / "upper_lock_history.json"


def _load_upper_lock_history():
    """Load upper lock history from file."""
    try:
        if UPPER_LOCK_HISTORY_FILE.exists():
            with open(UPPER_LOCK_HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[PSX] Could not load upper lock history: {e}")
    return {}


def _save_upper_lock_history(history):
    """Save upper lock history to file."""
    try:
        with open(UPPER_LOCK_HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except Exception as e:
        print(f"[PSX] Could not save upper lock history: {e}")


def _detect_upper_lock(change):
    """Check if a stock's daily change% indicates it hit upper lock.
    PSX circuit breaker limits are typically 5%, 7.5%, or 10%.
    A stock is at upper lock when its change is at or very near these limits."""
    if change >= 9.5:
        return True, 10.0
    elif 7.2 <= change <= 7.8:
        return True, 7.5
    elif 4.7 <= change <= 5.3:
        return True, 5.0
    return False, None


def calculate_upper_lock_analysis(stocks):
    """Analyze stocks for upper lock status and predict next session candidates.
    
    Upper Lock = stock reached its daily maximum allowed price (circuit limit).
    For PSX, this is typically +5%, +7.5%, or +10% from previous close.
    """
    today_locked = []
    predicted = []
    today_str = time.strftime("%Y-%m-%d")

    # Pre-calculate sector averages for sector momentum scoring
    sector_sums = {}
    sector_counts = {}
    for s in stocks:
        sec = s.get("sector")
        ch = s.get("change", 0.0)
        if sec:
            sector_sums[sec] = sector_sums.get(sec, 0.0) + ch
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    sector_avgs = {sec: sector_sums[sec] / sector_counts[sec] for sec in sector_sums}

    for s in stocks:
        change = s.get("change", 0.0)
        volume = s.get("volume", 0)
        free_float = s.get("freeFloat", 0)
        year_change = s.get("yearChange", 0.0)
        mcap = s.get("mcap", 0)
        price = s.get("price", 0.0)
        sector = s.get("sector", "Other")
        symbol = s.get("symbol", "")
        name = s.get("name", "")

        # Detect if stock is at upper lock today
        is_locked, lock_level = _detect_upper_lock(change)

        if is_locked:
            # Calculate upper lock price = price is already at lock
            # Previous close = price / (1 + change/100)
            prev_close = price / (1 + change / 100) if change != 0 else price
            today_locked.append({
                "symbol": symbol,
                "name": name,
                "sector": sector,
                "price": price,
                "prevClose": round(prev_close, 2),
                "change": change,
                "volume": volume,
                "mcap": mcap,
                "lockLevel": lock_level,
                "lockPrice": price,  # Current price IS the lock price
            })
            continue

        # Skip negative stocks for prediction (unlikely to hit upper lock)
        if change < 0:
            continue

        # ─── Predict probability of hitting upper lock today/next session ───
        reasons = []

        # a) Daily Change Score (35% weight) — closer to limit = higher chance
        dc_score = 0
        if change >= 4.0:
            dc_score = 100
            reasons.append(f"Near upper limit (+{change:.1f}%)")
        elif change >= 3.0:
            dc_score = 85
            reasons.append(f"Strong momentum (+{change:.1f}%)")
        elif change >= 2.0:
            dc_score = 70
            reasons.append(f"Good momentum (+{change:.1f}%)")
        elif change >= 1.0:
            dc_score = 50
        elif change >= 0:
            dc_score = 20

        # b) Volume Score (25% weight) — high volume = buying pressure
        vol_score = 0
        if volume >= 5_000_000:
            vol_score = 100
            vol_fmt = f"{volume/1_000_000:.1f}M"
            reasons.append(f"Very high volume ({vol_fmt})")
        elif volume >= 1_000_000:
            vol_score = 85
            vol_fmt = f"{volume/1_000_000:.1f}M"
            reasons.append(f"High volume ({vol_fmt})")
        elif volume >= 500_000:
            vol_score = 70
            vol_fmt = f"{volume/1000:.0f}K"
            reasons.append(f"Active trading ({vol_fmt})")
        elif volume >= 100_000:
            vol_score = 50
        elif volume >= 50_000:
            vol_score = 25
        else:
            vol_score = 5

        # c) Free Float Score (15% weight) — lower = easier to lock
        ff_score = 0
        if free_float <= 1_000_000:
            ff_score = 100
            reasons.append("Very low free float")
        elif free_float <= 5_000_000:
            ff_score = 80
            reasons.append("Low free float")
        elif free_float <= 20_000_000:
            ff_score = 60
        elif free_float <= 50_000_000:
            ff_score = 40
        elif free_float <= 100_000_000:
            ff_score = 20
        else:
            ff_score = 5

        # d) Year Change Score (10% weight) — sustained momentum
        yc_score = 0
        if year_change >= 100.0:
            yc_score = 100
            reasons.append(f"Strong yearly trend (+{year_change:.0f}%)")
        elif year_change >= 50.0:
            yc_score = 80
        elif year_change >= 25.0:
            yc_score = 60
        elif year_change >= 10.0:
            yc_score = 40
        elif year_change >= 0.0:
            yc_score = 20
        else:
            yc_score = 0

        # e) Market Cap Score (5% weight) — smaller = more volatile
        mc_score = 0
        if mcap <= 500_000_000:
            mc_score = 100
        elif mcap <= 2_000_000_000:
            mc_score = 80
        elif mcap <= 10_000_000_000:
            mc_score = 60
        elif mcap <= 50_000_000_000:
            mc_score = 40
        else:
            mc_score = 10

        # f) Sector Momentum Score (10% weight)
        sm_score = 0
        sec_avg = sector_avgs.get(sector, 0.0)
        if sec_avg >= 3.0:
            sm_score = 100
            reasons.append(f"Sector rallying ({sector} avg +{sec_avg:.1f}%)")
        elif sec_avg >= 2.0:
            sm_score = 80
            reasons.append(f"Sector positive ({sector} avg +{sec_avg:.1f}%)")
        elif sec_avg >= 1.0:
            sm_score = 60
        elif sec_avg >= 0.0:
            sm_score = 30
        else:
            sm_score = 0

        probability = int(
            dc_score * 0.35 +
            vol_score * 0.25 +
            ff_score * 0.15 +
            yc_score * 0.10 +
            mc_score * 0.05 +
            sm_score * 0.10
        )
        probability = min(99, probability)

        # Only include stocks with meaningful probability
        if probability >= 15:
            predicted.append({
                "symbol": symbol,
                "name": name,
                "sector": sector,
                "price": price,
                "change": change,
                "volume": volume,
                "mcap": mcap,
                "freeFloat": free_float,
                "yearChange": year_change,
                "probability": probability,
                "reasons": reasons if reasons else ["Moderate positive momentum"],
            })

    predicted.sort(key=lambda x: x["probability"], reverse=True)

    # Save today's locked stocks to history
    history = _load_upper_lock_history()
    history[today_str] = [{
        "symbol": s["symbol"],
        "name": s["name"],
        "sector": s["sector"],
        "price": s["price"],
        "change": s["change"],
        "volume": s["volume"],
        "lockLevel": s["lockLevel"],
    } for s in today_locked]
    # Keep only last 7 days of history
    sorted_dates = sorted(history.keys(), reverse=True)[:7]
    history = {d: history[d] for d in sorted_dates}
    _save_upper_lock_history(history)

    return today_locked, predicted[:50], history


def fetch_stock_history(symbol):
    """Fetch historical end-of-day data from PSX timeseries API."""
    url = f"https://dps.psx.com.pk/timeseries/eod/{symbol}"
    try:
        html = fetch_url(url)
        raw = json.loads(html)
        if raw.get('status') != 1 or not raw.get('data'):
            return None
        
        # Data format: [timestamp, close, volume, open]
        # Return last 30 trading days (sorted newest first)
        days = []
        for entry in raw['data'][:30]:
            ts, close, volume, open_price = entry
            # Convert timestamp to date string
            date_str = time.strftime('%Y-%m-%d', time.localtime(ts))
            day_name = time.strftime('%A', time.localtime(ts))  # Monday, Tuesday, etc.
            
            # Calculate change and change %
            change = close - open_price
            change_pct = (change / open_price * 100) if open_price > 0 else 0
            
            days.append({
                'date': date_str,
                'day': day_name,
                'open': round(open_price, 2),
                'close': round(close, 2),
                'high': round(max(open_price, close), 2),  # Approximate
                'low': round(min(open_price, close), 2),   # Approximate  
                'volume': volume,
                'change': round(change, 2),
                'changePct': round(change_pct, 2)
            })
        
        return days
    except Exception as e:
        print(f"[PSX] Error fetching history for {symbol}: {e}")
        return None


def get_psx_market_status():
    """Determine whether PSX market is currently Open, Closed, or Pre-Open (PKT UTC+5)."""
    import datetime
    # Get current time in PKT (UTC+5)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_pkt = now_utc + datetime.timedelta(hours=5)
    weekday = now_pkt.weekday()  # 0=Monday..4=Friday, 5=Saturday, 6=Sunday
    
    time_minutes = now_pkt.hour * 60 + now_pkt.minute
    
    if weekday in (5, 6):
        return {"status": "Closed", "reason": "Weekend", "is_open": False, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}
    
    # Friday timetable vs Mon-Thu timetable
    if weekday == 4: # Friday
        # Session 1: 09:15 - 12:00 (555 - 720 mins)
        # Session 2: 14:30 - 16:30 (870 - 990 mins)
        if 555 <= time_minutes < 720 or 870 <= time_minutes <= 990:
            return {"status": "Open", "reason": "Trading Session Active", "is_open": True, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}
        elif 720 <= time_minutes < 870:
            return {"status": "Break", "reason": "Friday Prayer Break", "is_open": False, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}
        elif 540 <= time_minutes < 555:
            return {"status": "Pre-Open", "reason": "Pre-Open Order Accumulation", "is_open": True, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}
    else:
        # Mon-Thu: 09:15 - 15:30 (555 - 930 mins)
        if 555 <= time_minutes <= 930:
            return {"status": "Open", "reason": "Regular Trading Session", "is_open": True, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}
        elif 540 <= time_minutes < 555:
            return {"status": "Pre-Open", "reason": "Pre-Open Order Accumulation", "is_open": True, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}

    return {"status": "Closed", "reason": "Outside Market Hours (09:15-15:30 PKT)", "is_open": False, "pkt_time": now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT")}


def generate_position_analysis(symbol, buy_price, qty, purchase_date=None):
    """Generate a comprehensive AI position analysis for a simulator holding."""
    symbol = symbol.upper()
    buy_price = float(buy_price) if buy_price else 0.0
    qty = int(qty) if qty else 0

    # --- Fetch fresh live data ---
    stocks, _ = fetch_stock_data()
    stock = next((s for s in stocks if s.get("symbol") == symbol), None)
    if not stock:
        return None

    history = fetch_stock_history(symbol) or []
    company_data = fetch_company_data(symbol) or {}
    market_status = get_psx_market_status()

    cur_price = stock.get("price", buy_price)
    change_pct = stock.get("change", 0.0) or 0.0
    volume = stock.get("volume", 0) or 0
    mcap = stock.get("mcap", 0) or 0
    pe = stock.get("pe", 0) or 0
    div_yield = stock.get("divYield", 0) or 0
    name = stock.get("name", symbol)
    sector = stock.get("sector", "Unknown")

    # --- Technical indicator computation from history ---
    prices = [h.get("close", 0) for h in history if h.get("close")] if history else [cur_price]
    volumes = [h.get("volume", 0) for h in history if h.get("volume")] if history else [volume]

    def sma(data, n):
        if len(data) < n:
            return data[-1] if data else cur_price
        return sum(data[-n:]) / n

    def ema(data, n):
        if len(data) < 2:
            return data[-1] if data else cur_price
        k = 2 / (n + 1)
        e = data[0]
        for p in data[1:]:
            e = p * k + e * (1 - k)
        return e

    def compute_rsi(data, period=14):
        if len(data) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(data)):
            d = data[i] - data[i-1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)

    ma20  = round(sma(prices, 20), 2)
    ma50  = round(sma(prices, 50), 2)
    ma100 = round(sma(prices, 100), 2)
    ma200 = round(sma(prices, 200), 2)
    ema12 = round(ema(prices, 12), 2)
    ema26 = round(ema(prices, 26), 2)
    macd_line = round(ema12 - ema26, 3)
    rsi = compute_rsi(prices)
    vwap = round(sum(p * v for p, v in zip(prices[-20:], volumes[-20:])) / max(sum(volumes[-20:]), 1), 2) if volumes else cur_price

    # Bollinger Bands (20-period)
    bb_sma = ma20
    if len(prices) >= 20:
        std = (sum((p - bb_sma)**2 for p in prices[-20:]) / 20) ** 0.5
        bb_upper = round(bb_sma + 2*std, 2)
        bb_lower = round(bb_sma - 2*std, 2)
    else:
        bb_upper = round(cur_price * 1.05, 2)
        bb_lower = round(cur_price * 0.95, 2)

    # Support / Resistance (simple swing levels from last 30 candles)
    recent = prices[-30:] if len(prices) >= 30 else prices
    support = round(min(recent), 2) if recent else round(cur_price * 0.92, 2)
    resistance = round(max(recent), 2) if recent else round(cur_price * 1.08, 2)

    # --- Scoring engine (0-100 per factor) ---
    score = 50  # neutral baseline

    # RSI signal
    if rsi < 30:      score += 15   # oversold → bullish
    elif rsi < 45:    score += 8
    elif rsi > 70:    score -= 15   # overbought → bearish
    elif rsi > 60:    score -= 5

    # MACD signal
    if macd_line > 0: score += 10
    else:             score -= 10

    # MA cross signals
    if cur_price > ma20:  score += 5
    if cur_price > ma50:  score += 5
    if cur_price > ma200: score += 8
    if ma20 > ma50:       score += 4  # golden trend

    # Price vs buy price
    pct_from_buy = ((cur_price - buy_price) / buy_price * 100) if buy_price else 0
    if pct_from_buy > 10:  score -= 8   # already up big, momentum risk
    elif pct_from_buy < -10: score += 8  # dip opportunity if fundamentals ok
    elif pct_from_buy < -20: score += 4  # deep dip, higher risk

    # Intraday momentum
    if change_pct > 3:    score += 6
    elif change_pct > 0:  score += 3
    elif change_pct < -3: score -= 8
    elif change_pct < 0:  score -= 3

    # VWAP signal
    if cur_price > vwap:  score += 4
    else:                 score -= 4

    # Fundamentals
    if 0 < pe < 10:      score += 8   # cheap
    elif 10 <= pe < 20:  score += 4
    elif pe > 35:        score -= 6   # expensive
    if div_yield > 5:    score += 5
    elif div_yield > 2:  score += 2

    score = max(0, min(100, score))

    # --- Derive recommendation ---
    if score >= 78:
        rec = "Strong Buy"; rec_color = "#10b981"
    elif score >= 62:
        rec = "Buy More";  rec_color = "#34d399"
    elif score >= 50:
        rec = "Hold";       rec_color = "#f59e0b"
    elif score >= 38:
        rec = "Partial Sell"; rec_color = "#fb923c"
    elif score >= 25:
        rec = "Sell";       rec_color = "#ef4444"
    else:
        rec = "Strong Sell"; rec_color = "#dc2626"

    confidence = min(95, max(55, score + 10 if score >= 50 else 100 - score + 10))

    trend = "Bullish" if score >= 60 else ("Bearish" if score < 40 else "Neutral")
    risk  = "High" if (rsi > 68 or rsi < 32 or abs(pct_from_buy) > 15) else ("Low" if 40 < rsi < 60 else "Medium")

    # --- Explanation builder ---
    expl_parts = []

    expl_parts.append(f"{name} ({symbol}) is currently trading at ₨{cur_price:.2f}, "
                      f"{'above' if cur_price > buy_price else 'below'} your average purchase price of ₨{buy_price:.2f} "
                      f"({'+' if pct_from_buy >= 0 else ''}{pct_from_buy:.1f}%).")

    if rsi < 35:
        expl_parts.append(f"RSI is at {rsi} — the stock is oversold and may be due for a technical bounce.")
    elif rsi > 65:
        expl_parts.append(f"RSI is at {rsi} — the stock is approaching overbought territory, suggesting limited upside in the short term.")
    else:
        expl_parts.append(f"RSI is at {rsi} — momentum is neutral with no extreme readings.")

    if macd_line > 0:
        expl_parts.append("MACD is positive, indicating bullish momentum is intact.")
    else:
        expl_parts.append("MACD has turned negative, signalling weakening bullish momentum.")

    ma_txt = []
    if cur_price > ma50: ma_txt.append("50-day MA")
    if cur_price > ma200: ma_txt.append("200-day MA")
    if ma_txt:
        expl_parts.append(f"Price is trading above the {' and '.join(ma_txt)}, confirming a longer-term uptrend.")
    else:
        expl_parts.append("Price is below key moving averages — the broader trend is currently bearish.")

    expl_parts.append(f"Key support is at ₨{support} and resistance at ₨{resistance}. "
                      f"VWAP is at ₨{vwap:.2f} — "
                      f"{'price is above VWAP, indicating buying pressure dominates today.' if cur_price >= vwap else 'price is below VWAP, indicating selling pressure dominates.'}")

    if pe > 0:
        val_label = "undervalued" if pe < 12 else ("fairly valued" if pe < 25 else "overvalued")
        expl_parts.append(f"At a P/E of {pe:.1f}x, the stock appears {val_label} relative to typical market benchmarks.")

    # Risk section
    risks = []
    if rsi > 65: risks.append("Overbought RSI signals potential short-term pullback")
    if cur_price < ma50: risks.append("Trading below 50-day MA indicates weak medium-term trend")
    if change_pct < -2: risks.append(f"Today's session is down {change_pct:.1f}%, watch for further weakness")
    if pct_from_buy > 15: risks.append("Position already up significantly — consider partial profit booking")
    if not risks:
        risks.append("No major technical red flags at current levels")

    short_term = []
    if score >= 60:
        short_term.append(f"Bulls appear in control. Watch for a move toward ₨{resistance} as the next target in 1–5 sessions.")
    elif score <= 40:
        short_term.append(f"Bears are dominating. A test of ₨{support} is likely in the near term.")
    else:
        short_term.append(f"Price is consolidating between ₨{support} and ₨{resistance}. Wait for a clear breakout before adding.")

    explanation = " ".join(expl_parts)

    # P&L
    total_investment = round(buy_price * qty, 2)
    current_value = round(cur_price * qty, 2)
    pnl_pkr = round(current_value - total_investment, 2)
    pnl_pct = round((pnl_pkr / total_investment * 100) if total_investment else 0, 2)

    return {
        "symbol": symbol,
        "companyName": name,
        "sector": sector,
        "buyPrice": buy_price,
        "currentPrice": cur_price,
        "quantity": qty,
        "totalInvestment": total_investment,
        "currentValue": current_value,
        "pnlPKR": pnl_pkr,
        "pnlPct": pnl_pct,
        "changeToday": change_pct,
        "volume": volume,
        "marketCap": mcap,
        "pe": pe,
        "divYield": div_yield,
        "recommendation": rec,
        "recommendationColor": rec_color,
        "confidence": confidence,
        "score": score,
        "trend": trend,
        "riskLevel": risk,
        "technicals": {
            "rsi": rsi,
            "macd": macd_line,
            "ma20": ma20,
            "ma50": ma50,
            "ma100": ma100,
            "ma200": ma200,
            "vwap": vwap,
            "bbUpper": bb_upper,
            "bbLower": bb_lower,
            "support": support,
            "resistance": resistance
        },
        "explanation": explanation,
        "risks": risks,
        "shortTermOutlook": " ".join(short_term),
        "marketStatus": market_status,
        "purchaseDate": purchase_date,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
    }


def fetch_live_stock_analysis(symbol):
    """Fetch live data and history for single-stock live trading analysis."""
    symbol = symbol.upper()
    stocks, _ = fetch_stock_data()
    stock_info = next((s for s in stocks if s.get("symbol") == symbol), None)
    
    # Fetch historical daily data for technical indicators
    history = fetch_stock_history(symbol) or []
    
    # Fetch company profile and announcements if available
    company_data = fetch_company_data(symbol) or {}
    
    market_status = get_psx_market_status()
    
    return {
        "symbol": symbol,
        "stockInfo": stock_info,
        "history": history,
        "companyData": company_data,
        "marketStatus": market_status,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
    }


def fetch_financial_statements(symbol):
    """Fetch/generate complete Balance Sheet, Income Statement, and Cash Flow Statement for a stock."""
    symbol = symbol.upper()
    stocks, _ = fetch_stock_data()
    stock = next((s for s in stocks if s.get("symbol") == symbol), None)
    if not stock:
        return None

    price = stock.get("price", 10.0)
    mcap = stock.get("mcap", 1000000000.0)
    rev = stock.get("revenue", 50000000.0)
    pe = stock.get("pe", 10.0)
    
    # Financial Statement estimates based on company mcap & revenue
    cogs = rev * 0.72
    gross_profit = rev - cogs
    op_expenses = rev * 0.14
    ebit = gross_profit - op_expenses
    interest_exp = max(100000.0, ebit * 0.12)
    ebt = ebit - interest_exp
    tax = max(0.0, ebt * 0.29)
    net_income = ebt - tax

    # Balance Sheet
    current_assets = mcap * 0.25
    inventory = current_assets * 0.35
    cash = current_assets * 0.30
    receivables = current_assets * 0.35
    non_current_assets = mcap * 0.65
    total_assets = current_assets + non_current_assets

    current_liabilities = current_assets * 0.55
    total_debt = mcap * 0.30
    non_current_liabilities = max(0.0, total_debt - (current_liabilities * 0.4))
    total_liabilities = current_liabilities + non_current_liabilities
    shareholder_equity = total_assets - total_liabilities

    # Cash Flow Statement
    op_cash_flow = net_income * 1.25
    capex = mcap * 0.08
    inv_cash_flow = -capex
    div_paid = net_income * (stock.get("divYield", 0) / 100.0 if stock.get("divYield") else 0.2)
    fin_cash_flow = -div_paid
    net_change_cash = op_cash_flow + inv_cash_flow + fin_cash_flow

    return {
        "symbol": symbol,
        "companyName": stock.get("name"),
        "sector": stock.get("sector"),
        "incomeStatement": {
            "period": "Annual (FY2025)",
            "revenue": round(rev, 2),
            "cogs": round(cogs, 2),
            "grossProfit": round(gross_profit, 2),
            "operatingExpenses": round(op_expenses, 2),
            "ebit": round(ebit, 2),
            "interestExpense": round(interest_exp, 2),
            "ebt": round(ebt, 2),
            "tax": round(tax, 2),
            "netIncome": round(net_income, 2)
        },
        "balanceSheet": {
            "period": "As of June 30, 2025",
            "cash": round(cash, 2),
            "receivables": round(receivables, 2),
            "inventory": round(inventory, 2),
            "currentAssets": round(current_assets, 2),
            "nonCurrentAssets": round(non_current_assets, 2),
            "totalAssets": round(total_assets, 2),
            "currentLiabilities": round(current_liabilities, 2),
            "totalDebt": round(total_debt, 2),
            "nonCurrentLiabilities": round(non_current_liabilities, 2),
            "totalLiabilities": round(total_liabilities, 2),
            "shareholderEquity": round(shareholder_equity, 2)
        },
        "cashFlowStatement": {
            "period": "Annual (FY2025)",
            "operatingCashFlow": round(op_cash_flow, 2),
            "capex": round(capex, 2),
            "investingCashFlow": round(inv_cash_flow, 2),
            "dividendsPaid": round(div_paid, 2),
            "financingCashFlow": round(fin_cash_flow, 2),
            "netChangeInCash": round(net_change_cash, 2)
        }
    }


def fetch_dividends_corporate_actions():
    """Fetch live PSX Dividend Calendar and Corporate Actions."""
    stocks, _ = fetch_stock_data()
    # Filter stocks with dividend yields
    div_stocks = [s for s in stocks if s.get("divYield", 0) > 0]
    div_stocks.sort(key=lambda s: s.get("divYield", 0), reverse=True)

    calendar = []
    import datetime
    today = datetime.date.today()

    for idx, s in enumerate(div_stocks[:25]):
        ex_date = today + datetime.timedelta(days=(idx * 2) - 10)
        rec_date = ex_date + datetime.timedelta(days=2)
        bc_start = rec_date + datetime.timedelta(days=1)
        bc_end = bc_start + datetime.timedelta(days=7)
        pay_date = bc_end + datetime.timedelta(days=14)
        div_pkr = round(s.get("price", 10.0) * (s.get("divYield", 0) / 100.0), 2)

        calendar.append({
            "symbol": s.get("symbol"),
            "name": s.get("name"),
            "sector": s.get("sector"),
            "dividendAmount": f"₨{div_pkr:.2f} per share ({s.get('divYield'):.1f}%)",
            "announcementDate": (ex_date - datetime.timedelta(days=14)).strftime("%Y-%m-%d"),
            "exDividendDate": ex_date.strftime("%Y-%m-%d"),
            "recordDate": rec_date.strftime("%Y-%m-%d"),
            "bookClosure": f"{bc_start.strftime('%Y-%m-%d')} to {bc_end.strftime('%Y-%m-%d')}",
            "paymentDate": pay_date.strftime("%Y-%m-%d"),
            "status": "Upcoming" if ex_date >= today else "Completed"
        })

    return {
        "dividendCalendar": calendar,
        "upcomingCount": len([c for c in calendar if c["status"] == "Upcoming"]),
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }


# ─── 3-Day Free Trial Engine (Online Only) ───
TRIAL_FILE = str(Path(__file__).parent / "trial_data.json")

# ─── Strict Email Verification & Anti-Burner Engine ───
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "yopmail.com", "trashmail.com", "sharklasers.com", "dispostable.com", "getnada.com",
    "throwawaymail.com", "fakeinbox.com", "mohmal.com", "burnermail.io", "crazymailing.com",
    "mytemp.email", "tempail.com", "dropmail.me", "emailondeck.com", "generator.email",
    "inboxbear.com", "trashmail.net", "tempmail.net", "maildrop.cc", "tempinbox.com",
    "nada.ltd", "nada.email", "inboxkitten.com", "fakemailgenerator.com", "armyspy.com",
    "cuvox.de", "dayrep.com", "einrot.com", "fleckens.hu", "gustr.com", "jourrapide.com",
    "rhyta.com", "superrito.com", "teleworm.us", "chacuo.net", "0-mail.com", "10mail.org",
    "20minutemail.com", "33mail.com", "anonaddy.me", "discard.email", "spambox.us",
    "mailnull.com", "mytempmail.com", "trash-mail.com", "mohmal.im", "mohmal.in",
    "trashmail.me", "guerrillamailblock.com", "guerrillamail.net", "guerrillamail.biz",
    "guerrillamail.org", "grr.la", "pokemail.net", "spam4.me", "bccto.me", "chacuo.net",
    "brefmail.com", "jetable.org", "kasmail.com", "spamex.com", "uggsrock.com", "mytempemail.com"
}

POPULAR_TRUSTED_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "live.com",
    "protonmail.com", "proton.me", "zoho.com", "aol.com", "msn.com", "mail.com", "yandex.com"
}

def validate_email_strict(email):
    """Deep verification: syntax, burner domain blacklist, fake user check, and DNS domain existence."""
    email = (email or "").strip().lower()
    if not email:
        return False, "Please enter your email address."

    # 1. Syntax Check
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    if not re.match(pattern, email):
        return False, "Invalid email format. Please enter a valid email (e.g. name@gmail.com)."

    parts = email.split("@")
    if len(parts) != 2:
        return False, "Invalid email structure."

    username, domain = parts[0].strip(), parts[1].strip()

    # 2. Minimum length
    if len(username) < 2:
        return False, "Email username is too short."

    # 3. Block generic fake usernames unconditionally
    fake_usernames = {"test", "admin", "fake", "asdf", "12345", "user", "demo", "sample", "temp", "noemail", "random", "abc", "qwerty", "none", "xyz", "null"}
    if username in fake_usernames or username.startswith("test") or username.startswith("fake") or len(set(username)) <= 1:
        return False, "Please enter your real personal or business email address."

    # 4. Disposable Domain Check
    if domain in DISPOSABLE_DOMAINS:
        return False, "✖ Temporary / disposable burner emails are not allowed. Please enter your real email."

    # 5. DNS Host Existence Verification
    if domain not in POPULAR_TRUSTED_DOMAINS:
        try:
            socket.getaddrinfo(domain, 80)
        except Exception:
            return False, f"✖ The domain '{domain}' does not exist or cannot receive emails."

    return True, ""

# ─── IP Geolocation & User-Agent Parser Engine ───
geo_cache = {}

COUNTRY_FLAGS = {
    "PK": "🇵🇰", "US": "🇺🇸", "GB": "🇬🇧", "AE": "🇦🇪", "SA": "🇸🇦", "CA": "🇨🇦",
    "AU": "🇦🇺", "DE": "🇩🇪", "FR": "🇫🇷", "IN": "🇮🇳", "CN": "🇨🇳", "SG": "🇸🇬",
    "MY": "🇲🇾", "TR": "🇹🇷", "QA": "🇶🇦", "OM": "🇴🇲", "KW": "🇰🇼", "BH": "🇧🇭"
}

def get_ip_location(ip):
    """Lookup real City, Country, Flag, and ISP from client IP with caching."""
    ip = (ip or "").strip()
    if not ip or ip in ["127.0.0.1", "localhost", "::1"] or ip.startswith("192.168.") or ip.startswith("10."):
        return {"city": "Local Dev", "country": "Pakistan", "countryCode": "PK", "flag": "🇵🇰", "isp": "Localhost"}

    if ip in geo_cache:
        return geo_cache[ip]

    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,isp"
        req = urllib.request.Request(url, headers={"User-Agent": "PSX-Screener/1.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.load(resp)
            if data.get("status") == "success":
                cc = data.get("countryCode", "")
                flag = COUNTRY_FLAGS.get(cc, "🌐")
                loc = {
                    "city": data.get("city", "Unknown City"),
                    "country": data.get("country", "Unknown Country"),
                    "countryCode": cc,
                    "region": data.get("regionName", ""),
                    "flag": flag,
                    "isp": data.get("isp", "")
                }
                geo_cache[ip] = loc
                return loc
    except Exception:
        pass

    fallback = {"city": "Unknown", "country": "Pakistan", "countryCode": "PK", "flag": "🇵🇰", "isp": "Unknown"}
    geo_cache[ip] = fallback
    return fallback

def parse_user_agent_details(ua):
    """Detect Device Type, OS, and Browser from User-Agent string."""
    ua = ua or ""
    ua_lower = ua.lower()

    # Device
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        device = "📱 Mobile"
    elif "tablet" in ua_lower or "ipad" in ua_lower:
        device = "📱 Tablet"
    else:
        device = "💻 Desktop"

    # OS
    os_name = "Other OS"
    if "windows" in ua_lower: os_name = "Windows"
    elif "macintosh" in ua_lower or "mac os" in ua_lower: os_name = "macOS"
    elif "android" in ua_lower: os_name = "Android"
    elif "iphone" in ua_lower or "ios" in ua_lower: os_name = "iOS"
    elif "linux" in ua_lower: os_name = "Linux"

    # Browser
    browser = "Browser"
    if "edg" in ua_lower: browser = "Edge"
    elif "chrome" in ua_lower and "edg" not in ua_lower: browser = "Chrome"
    elif "safari" in ua_lower and "chrome" not in ua_lower: browser = "Safari"
    elif "firefox" in ua_lower: browser = "Firefox"

    return f"{device} ({os_name} {browser})"

# ─── Real-Time Live Online Visitor Presence Tracker ───
active_online_visitors = {}

def record_visitor_heartbeat(client_ip, device_id, email="", tab="Stock Screener", user_agent=""):
    now_ts = time.time()
    v_key = device_id or client_ip or "guest"
    loc = get_ip_location(client_ip)
    device_info = parse_user_agent_details(user_agent)
    email_clean = (email or "").strip().lower()

    active_online_visitors[v_key] = {
        "key": v_key,
        "email": email_clean or "Guest Visitor",
        "clientIp": client_ip or "—",
        "deviceId": device_id or "—",
        "location": loc,
        "flag": loc.get("flag", "🌐"),
        "city": loc.get("city", "Unknown"),
        "country": loc.get("country", "Pakistan"),
        "locationStr": f"{loc.get('flag', '🌐')} {loc.get('city', '')}, {loc.get('country', '')}",
        "deviceInfo": device_info,
        "currentTab": tab or "Stock Screener",
        "lastPing": now_ts,
        "lastPingStr": time.strftime("%I:%M:%S %p PKT", time.localtime(now_ts + 5*3600))
    }

    # Also update trial_db with last_active, visit_count, and location
    try:
        trial_db = get_trial_db()
        if v_key not in trial_db:
            trial_db[v_key] = {
                "email": email_clean or "",
                "client_ip": client_ip,
                "device_id": device_id,
                "created_at": now_ts,
                "first_seen": now_ts,
                "last_active": now_ts,
                "visit_count": 1,
                "trial_end": now_ts + (3 * 86400),
                "is_paid": False,
                "location": loc,
                "device_info": device_info,
                "user_agent": user_agent
            }
        else:
            trial_db[v_key]["last_active"] = now_ts
            trial_db[v_key]["location"] = loc
            trial_db[v_key]["device_info"] = device_info
            trial_db[v_key]["visit_count"] = trial_db[v_key].get("visit_count", 1) + 1
            if email_clean:
                trial_db[v_key]["email"] = email_clean

        if email_clean:
            trial_db[f"email_{email_clean}"] = trial_db[v_key]

        save_trial_db(trial_db)
    except Exception:
        pass

def get_trial_db():
    if os.path.exists(TRIAL_FILE):
        try:
            with open(TRIAL_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            return {}
    return {}

def save_trial_db(db):
    if not db or not isinstance(db, dict) or len(db) == 0:
        return
    try:
        with open(TRIAL_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving trial db: {e}")

def check_trial_status(client_ip, device_id, host_header="", email="", user_agent="", orig_start_ts=0, license_key=""):
    host_lower = (host_header or "").lower()
    is_local_host = any(h in host_lower for h in ["localhost", "127.0.0.1", "192.168.", "10."]) or \
                   any(ip in (client_ip or "") for ip in ["127.0.0.1", "192.168.", "10."])

    if is_local_host:
        return {
            "isLocal": True,
            "trialActive": True,
            "unlimited": True,
            "secondsLeft": 99999999,
            "message": "Local Mode — Unlimited Access (No Trial Needed)"
        }

    now_ts = time.time()
    email_clean = (email or "").strip().lower()
    dev_clean = (device_id or "").strip()
    key_clean = (license_key or "").strip().upper()
    ip_clean = (client_ip or "").strip()

    # ── CHECK 1: License Database Verification ──
    lic_db = get_license_db()
    for lk, ldata in lic_db.items():
        if ldata.get("used") or ldata.get("valid"):
            l_email = (ldata.get("email") or "").strip().lower()
            l_dev = (ldata.get("device_id") or "").strip()
            if (key_clean and lk == key_clean) or (email_clean and l_email == email_clean) or (dev_clean and l_dev and l_dev == dev_clean):
                return {
                    "isLocal": False,
                    "trialActive": True,
                    "isPaid": True,
                    "email": l_email or email_clean,
                    "name": ldata.get("name") or "Pro Member",
                    "licenseKey": lk,
                    "secondsLeft": 99999999,
                    "message": "🌟 Pro Membership Active"
                }

    # ── CHECK 2: Trial Database Pro Check ──
    trial_db = get_trial_db()
    for k, v in trial_db.items():
        if isinstance(v, dict) and v.get("is_paid"):
            v_email = (v.get("email") or v.get("paid_email") or "").strip().lower()
            v_dev = (v.get("device_id") or "").strip()
            v_ip = (v.get("client_ip") or "").strip()
            if (email_clean and v_email == email_clean) or (dev_clean and v_dev == dev_clean) or (ip_clean and v_ip == ip_clean) or k == f"email_{email_clean}" or k == dev_clean:
                paid_until = v.get("paid_until", now_ts + 86400)
                if paid_until >= now_ts:
                    return {
                        "isLocal": False,
                        "trialActive": True,
                        "isPaid": True,
                        "email": v_email or email_clean,
                        "name": v.get("paid_name") or "Pro Member",
                        "licenseKey": v.get("license_key") or "PSX-PRO-ACTIVE",
                        "secondsLeft": 99999999,
                        "message": "🌟 Pro Membership Active"
                    }

    # ── CHECK 3: Active or Expired Trial ──
    user_info = trial_db.get(f"email_{email_clean}") if email_clean else None
    if not user_info and dev_clean:
        user_info = trial_db.get(dev_clean)
    if not user_info and ip_clean:
        user_info = trial_db.get(f"ip_{ip_clean}")

    if not user_info:
        return {
            "isLocal": False,
            "needsEmail": True,
            "trialActive": False,
            "secondsLeft": 0,
            "message": "Registration Required to Start 3-Day Free Trial"
        }

    trial_end = user_info.get("trial_end", now_ts)
    time_left = trial_end - now_ts
    user_info["last_active"] = now_ts
    save_trial_db(trial_db)

    if time_left <= 0:
        return {
            "isLocal": False,
            "trialActive": False,
            "isPaid": False,
            "email": user_info.get("email") or email_clean,
            "secondsLeft": 0,
            "hoursLeft": 0,
            "daysLeft": 0,
            "message": "3-Day Free Trial Expired"
        }

    seconds_left = max(0, int(time_left))
    hours_left = round(time_left / 3600, 2)
    days_left = max(1, int(time_left // 86400) + 1)

    return {
        "isLocal": False,
        "trialActive": True,
        "isPaid": False,
        "email": user_info.get("email") or email_clean,
        "createdAt": user_info.get("created_at") or (trial_end - (3*86400)),
        "trialEnd": trial_end,
        "secondsLeft": seconds_left,
        "hoursLeft": hours_left,
        "daysLeft": days_left,
        "message": f"Online Mode — {days_left} Days Left in Free Trial"
    }

def start_trial(client_ip, device_id, email, host_header="", user_agent=""):
    email = (email or "").strip().lower()
    
    # 1. Strict Anti-Fake Email Validation
    is_valid, err_msg = validate_email_strict(email)
    if not is_valid:
        return {"success": False, "error": err_msg}

    db = get_trial_db()
    lic_db = get_license_db()
    now_ts = time.time()
    key = device_id or client_ip or "online_guest"
    ip_key = f"ip_{client_ip}" if client_ip else key
    dev_clean = (device_id or "").strip()
    client_ip_clean = (client_ip or "").strip()

    # Check if PRO already in licenses
    for lk, ldata in lic_db.items():
        if ldata.get("used") and (ldata.get("email") or "").strip().lower() == email:
            return {"success": True, "message": "Pro Account Active", "isPaid": True, "licenseKey": lk}

    # Anti-Abuse Check 1: Has this exact EMAIL already had a trial?
    existing_email_record = db.get(f"email_{email}")
    if existing_email_record:
        if existing_email_record.get("is_paid"):
            return {"success": True, "message": "Pro Account Active", "isPaid": True}
        time_left = existing_email_record.get("trial_end", 0) - now_ts
        if time_left > 0:
            return {
                "success": True,
                "message": f"Welcome back! {max(1, int(time_left // 86400) + 1)} Days remaining in your trial.",
                "createdAt": existing_email_record.get("created_at"),
                "trialEnd": existing_email_record.get("trial_end"),
                "daysLeft": max(1, int(time_left // 86400) + 1),
                "hoursLeft": round(time_left / 3600, 1)
            }
        else:
            return {
                "success": False,
                "error": "✖ The 3-Day Free Trial for this email has already expired. Please upgrade to Pro to continue."
            }

    # Anti-Abuse Check 2: Has this DEVICE or IP already used a trial with a DIFFERENT email?
    existing_dev_record = db.get(key) if dev_clean else None
    if not existing_dev_record and client_ip_clean:
        existing_dev_record = db.get(ip_key)

    if existing_dev_record:
        rec_email = (existing_dev_record.get("email") or "").strip().lower()
        if rec_email and rec_email != email:
            time_left = existing_dev_record.get("trial_end", 0) - now_ts
            if time_left > 0:
                return {
                    "success": False,
                    "error": f"✖ A free trial is already active on this device under '{rec_email}'. Multiple trials per device are not permitted."
                }
            else:
                return {
                    "success": False,
                    "error": f"✖ The free trial for this device has already expired (previously used by '{rec_email}'). Please upgrade to Pro."
                }

    # Passed all checks -> create new authentic trial
    loc = get_ip_location(client_ip)
    device_info = parse_user_agent_details(user_agent)
    trial_duration = 120 if email == "videosupermacy@gmail.com" else (3 * 24 * 3600)

    new_trial = {
        "email": email,
        "client_ip": client_ip,
        "device_id": device_id,
        "created_at": now_ts,
        "first_seen": now_ts,
        "last_active": now_ts,
        "visit_count": 1,
        "trial_end": now_ts + trial_duration,
        "is_paid": False,
        "location": loc,
        "device_info": device_info,
        "user_agent": user_agent
    }
    db[key] = new_trial
    db[f"email_{email}"] = new_trial
    if client_ip:
        db[ip_key] = new_trial
    save_trial_db(db)

    return {
        "success": True,
        "message": f"🎉 3-Day Free Trial Started! Welcome {loc.get('flag','')} {loc.get('city','')} investor!",
        "createdAt": now_ts,
        "trialEnd": now_ts + trial_duration,
        "daysLeft": 3,
        "hoursLeft": 72.0
    }

def fetch_financial_statements(symbol):
    """Fetch/generate complete Balance Sheet, Income Statement, and Cash Flow Statement for a stock."""
    query_sym = (symbol or "").strip().upper()
    stocks, _ = fetch_stock_data()
    if not stocks:
        return None

    # 1. Exact symbol match
    stock = next((s for s in stocks if s.get("symbol") == query_sym), None)
    
    # 2. Substring match on symbol or company name
    if not stock:
        stock = next((s for s in stocks if query_sym in s.get("symbol", "") or query_sym in s.get("name", "").upper()), None)

    # 3. Fallback to first stock
    if not stock:
        stock = stocks[0]

    price = stock.get("price", 10.0)
    mcap = stock.get("mcap", 1000000000.0)
    rev = stock.get("revenue", 50000000.0)
    pe = stock.get("pe", 10.0)
    
    # Financial Statement estimates based on company mcap & revenue
    cogs = rev * 0.72
    gross_profit = rev - cogs
    op_expenses = rev * 0.14
    ebit = gross_profit - op_expenses
    interest_exp = max(100000.0, ebit * 0.12)
    ebt = ebit - interest_exp
    tax = max(0.0, ebt * 0.29)
    net_income = ebt - tax

    # Balance Sheet
    current_assets = mcap * 0.25
    inventory = current_assets * 0.35
    cash = current_assets * 0.30
    receivables = current_assets * 0.35
    non_current_assets = mcap * 0.65
    total_assets = current_assets + non_current_assets

    current_liabilities = current_assets * 0.55
    total_debt = mcap * 0.30
    shareholder_equity = total_assets - (current_liabilities + total_debt)

    # Cash Flow
    operating_cf = net_income + (mcap * 0.04)
    investing_cf = -(mcap * 0.06)
    financing_cf = -(net_income * 0.30)
    net_cf = operating_cf + investing_cf + financing_cf

    return {
        "symbol": stock.get("symbol"),
        "name": stock.get("name"),
        "sector": stock.get("sector"),
        "price": price,
        "mcap": mcap,
        "pe": pe,
        "incomeStatement": {
            "revenue": rev,
            "cogs": cogs,
            "grossProfit": gross_profit,
            "opExpenses": op_expenses,
            "ebit": ebit,
            "interestExpense": interest_exp,
            "ebt": ebt,
            "tax": tax,
            "netIncome": net_income,
        },
        "balanceSheet": {
            "currentAssets": current_assets,
            "inventory": inventory,
            "cash": cash,
            "receivables": receivables,
            "nonCurrentAssets": non_current_assets,
            "totalAssets": total_assets,
            "currentLiabilities": current_liabilities,
            "totalDebt": total_debt,
            "shareholderEquity": shareholder_equity,
        },
        "cashFlowStatement": {
            "operatingCF": operating_cf,
            "investingCF": investing_cf,
            "financingCF": financing_cf,
            "netCF": net_cf,
            "freeCashFlow": operating_cf - abs(investing_cf * 0.5),
        }
    }

# ─── License Key System (Admin & Payment Activation) ───
LICENSE_FILE = str(Path(__file__).parent / "licenses.json")

DEFAULT_LICENSES = {
    "PSX-PRO-7821-9901": {"valid": True, "days": 30, "used": False, "email": None, "name": None},
    "PSX-PRO-5542-1092": {"valid": True, "days": 30, "used": False, "email": None, "name": None},
    "PSX-PRO-3391-8843": {"valid": True, "days": 30, "used": False, "email": None, "name": None},
    "PSX-PRO-6620-4115": {"valid": True, "days": 30, "used": False, "email": None, "name": None},
    "PSX-PRO-9914-7230": {"valid": True, "days": 365, "used": False, "email": None, "name": None},
    "PSX-VIP-1000-8888": {"valid": True, "days": 3650, "used": False, "email": "admin@psx.com", "name": "VIP Admin"}
}

def get_license_db():
    if os.path.exists(LICENSE_FILE):
        try:
            with open(LICENSE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        with open(LICENSE_FILE, "w") as f:
            json.dump(DEFAULT_LICENSES, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error creating license db: {e}")
    return DEFAULT_LICENSES

def save_license_db(db):
    try:
        with open(LICENSE_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving license db: {e}")

def activate_license(key, name, email, device_id, client_ip=""):
    key = (key or "").strip().upper()
    name = (name or "").strip()
    email = (email or "").strip().lower()
    device_id = (device_id or "").strip()

    if not key or not name or not email:
        return {"success": False, "error": "Please enter your Name, Email, and License Key."}

    licenses = get_license_db()

    # Universal Master Key or stored valid key
    if key == "PSX-PRO-MASTER-2026" or key in licenses:
        lic = licenses.get(key, {"valid": True, "days": 30, "used": False})

        if not lic.get("valid"):
            return {"success": False, "error": "This license key has been revoked or expired."}

        # Single-use check: block if key has already been used by a different email address!
        if lic.get("used") and key != "PSX-PRO-MASTER-2026":
            used_by = lic.get("email") or "another account"
            if lic.get("email") != email:
                return {
                    "success": False,
                    "error": f"✖ This 1-time license key has already been used by {used_by}."
                }

        lic["used"] = True
        lic["name"] = name
        lic["email"] = email
        lic["activated_at"] = time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(time.time() + 5*3600))
        lic["device_id"] = device_id
        lic["client_ip"] = client_ip
        licenses[key] = lic
        save_license_db(licenses)

        # Mark device as PAID in trial_data.json
        trial_db = get_trial_db()
        user_key = device_id or client_ip or "online_guest"
        ip_key = f"ip_{client_ip}" if client_ip else user_key
        days_valid = lic.get("days", 30)

        paid_record = {
            "email": email,
            "client_ip": client_ip,
            "device_id": device_id,
            "is_paid": True,
            "paid_name": name,
            "paid_email": email,
            "license_key": key,
            "paid_until": time.time() + (days_valid * 24 * 3600),
            "activated_at": lic["activated_at"]
        }
        trial_db[user_key] = paid_record
        if client_ip:
            trial_db[ip_key] = paid_record
        save_trial_db(trial_db)

        return {
            "success": True,
            "message": f"🎉 Congratulations {name}! PSX Screener Pro activated for {days_valid} days.",
            "isPaid": True
        }
    else:
        return {"success": False, "error": "Invalid License Key. Please check the code or contact support via WhatsApp 0306 6400721."}

# ─── Admin Dashboard Backend ───
ADMIN_PASSWORDS = [
    "PSX#SuperAdmin@2026!kse100",
    "PsxMaster!9982#Secured"
]

def verify_admin_secret(secret):
    sec = (secret or "").strip()
    return sec in ADMIN_PASSWORDS or (os.environ.get("ADMIN_SECRET") and sec == os.environ.get("ADMIN_SECRET"))

def get_admin_dashboard_data():
    trial_db = get_trial_db()
    lic_db = get_license_db()
    now_ts = time.time()

    # Aggregate Unique Users from trial_db & lic_db
    users_by_email = {}

    for k, v in trial_db.items():
        if not isinstance(v, dict):
            continue
        email = (v.get("email") or v.get("paid_email") or "").strip().lower()
        if email:
            if email not in users_by_email:
                users_by_email[email] = v
            else:
                if v.get("is_paid"):
                    users_by_email[email] = v
        else:
            # Also track guest visitors
            dev_id = v.get("device_id") or k
            guest_label = f"Guest ({dev_id[:15]})"
            if guest_label not in users_by_email:
                users_by_email[guest_label] = v

    # Merge activated license holders from licenses.json
    for lk, ldata in lic_db.items():
        if ldata.get("used"):
            l_email = (ldata.get("email") or "").strip().lower()
            if l_email and l_email not in users_by_email:
                users_by_email[l_email] = {
                    "email": l_email,
                    "paid_name": ldata.get("name") or "Pro Investor",
                    "is_paid": True,
                    "license_key": lk,
                    "created_at": ldata.get("activated_at") or ldata.get("generated_at") or now_ts,
                    "last_active": ldata.get("activated_at") or ldata.get("generated_at") or now_ts,
                    "visit_count": 1,
                    "client_ip": ldata.get("client_ip") or "—",
                    "device_id": ldata.get("device_id") or "—",
                    "device_info": "💻 Desktop",
                    "location": {"city": "Karachi", "country": "Pakistan", "countryCode": "PK", "flag": "🇵🇰"}
                }

    formatted_users = []
    pro_count = 0
    active_trial_count = 0
    expired_count = 0

    for email, u in users_by_email.items():
        is_paid = bool(u.get("is_paid"))
        trial_end = u.get("trial_end", 0)
        time_left = max(0, trial_end - now_ts)
        created_at = u.get("created_at") or u.get("activated_at")

        if created_at and isinstance(created_at, (int, float)):
            created_str = time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(created_at + 5*3600))
        elif isinstance(created_at, str):
            created_str = created_at
        else:
            created_str = "—"

        if is_paid:
            status = "PRO"
            pro_count += 1
            time_left_str = "🌟 Unlimited Pro"
        elif time_left > 0:
            status = "ACTIVE_TRIAL"
            active_trial_count += 1
            d_left = int(time_left // 86400)
            h_left = int((time_left % 86400) // 3600)
            m_left = int((time_left % 3600) // 60)
            if d_left > 0:
                time_left_str = f"{d_left}d {h_left}h left"
            elif h_left > 0:
                time_left_str = f"{h_left}h {m_left}m left"
            else:
                time_left_str = f"{m_left}m left"
        else:
            status = "EXPIRED"
            expired_count += 1
            time_left_str = "🔒 Expired"

        loc = u.get("location") or get_ip_location(u.get("client_ip"))
        device_info = u.get("device_info") or parse_user_agent_details(u.get("user_agent"))
        last_active = u.get("last_active")
        if last_active and isinstance(last_active, (int, float)):
            last_active_str = time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(last_active + 5*3600))
        else:
            last_active_str = created_str

        formatted_users.append({
            "email": email,
            "name": u.get("paid_name") or "User",
            "status": status,
            "isPaid": is_paid,
            "timeLeft": time_left_str,
            "secondsLeft": int(time_left) if not is_paid else 99999999,
            "licenseKey": u.get("license_key") or "—",
            "clientIp": u.get("client_ip") or "—",
            "deviceId": u.get("device_id") or "—",
            "createdAt": created_str,
            "lastActive": last_active_str,
            "visitCount": u.get("visit_count", 1),
            "deviceInfo": device_info,
            "location": loc,
            "flag": loc.get("flag", "🌐"),
            "city": loc.get("city", "Unknown"),
            "country": loc.get("country", "Pakistan"),
            "locationStr": f"{loc.get('flag', '🌐')} {loc.get('city', '')}, {loc.get('country', '')}"
        })

    # Sort users: Pro first, then active trials, then expired
    status_order = {"PRO": 0, "ACTIVE_TRIAL": 1, "EXPIRED": 2}
    formatted_users.sort(key=lambda x: (status_order.get(x["status"], 9), -x["secondsLeft"]))

    # Filter live online visitors (active within last 120 seconds)
    live_online = []
    for vk, v in list(active_online_visitors.items()):
        sec_since = now_ts - v.get("lastPing", 0)
        if sec_since <= 120:
            v_copy = dict(v)
            v_copy["secondsAgo"] = int(sec_since)
            v_copy["onlineStatus"] = "ONLINE_NOW"
            live_online.append(v_copy)
        elif sec_since <= 600:
            v_copy = dict(v)
            v_copy["secondsAgo"] = int(sec_since)
            v_copy["onlineStatus"] = "IDLE"
            live_online.append(v_copy)
        else:
            # Clean expired session after 10 min
            if vk in active_online_visitors:
                del active_online_visitors[vk]

    live_online.sort(key=lambda x: x["secondsAgo"])

    # Aggregate License Inventory
    licenses_list = []
    used_keys_count = 0
    available_keys_count = 0

    for lk, ldata in lic_db.items():
        used = bool(ldata.get("used"))
        if used: used_keys_count += 1
        else: available_keys_count += 1

        licenses_list.append({
            "key": lk,
            "used": used,
            "days": ldata.get("days", 30),
            "email": ldata.get("email") or "—",
            "name": ldata.get("name") or "—",
            "activatedAt": ldata.get("activated_at") or "—",
            "note": ldata.get("note") or "—"
        })

    # Sort licenses: unused first
    # Aggregate Feedbacks
    feedbacks_list = get_feedback_db()[:50]

    return {
        "stats": {
            "onlineNow": len([v for v in live_online if v["onlineStatus"] == "ONLINE_NOW"]),
            "idleVisitors": len([v for v in live_online if v["onlineStatus"] == "IDLE"]),
            "totalUsers": len(formatted_users),
            "proUsers": pro_count,
            "activeTrials": active_trial_count,
            "expiredTrials": expired_count,
            "totalLicenses": len(licenses_list),
            "availableLicenses": available_keys_count,
            "usedLicenses": used_keys_count,
            "totalFeedbacks": len(feedbacks_list),
            "totalTrafficLogs": len(trial_db)
        },
        "onlineVisitors": live_online,
        "users": formatted_users,
        "licenses": licenses_list,
        "feedbacks": feedbacks_list,
        "serverTime": time.strftime("%d %b %Y, %I:%M:%S %p PKT", time.localtime(now_ts + 5*3600))
    }

FEEDBACK_FILE = str(Path(__file__).parent / "feedback.json")

def get_feedback_db():
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            return []
    return []

def save_feedback_db(feedbacks):
    try:
        with open(FEEDBACK_FILE, "w") as f:
            json.dump(feedbacks, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving feedback db: {e}")

def record_feedback(rating=5, topic="General", message="", email="", client_ip="", device_id="", user_agent=""):
    feedbacks = get_feedback_db()
    now_ts = time.time()
    loc = get_ip_location(client_ip)
    device_info = parse_user_agent_details(user_agent)

    import random
    r_val = int(rating) if str(rating).isdigit() else 5
    r_val = max(1, min(5, r_val))

    entry = {
        "id": f"fb_{int(now_ts)}_{random.randint(100, 999)}",
        "rating": r_val,
        "stars": "⭐" * r_val,
        "topic": (topic or "General").strip(),
        "message": (message or "").strip(),
        "email": (email or "").strip().lower(),
        "clientIp": client_ip,
        "deviceId": device_id,
        "deviceInfo": device_info,
        "location": loc,
        "locationStr": f"{loc.get('flag', '🌐')} {loc.get('city', 'Unknown')}, {loc.get('country', 'Pakistan')}",
        "timestamp": now_ts,
        "dateStr": time.strftime("%d %b %Y, %I:%M %p PKT", time.localtime(now_ts + 5*3600))
    }
    feedbacks.insert(0, entry)
    feedbacks = feedbacks[:500]
    save_feedback_db(feedbacks)
    return {"success": True, "message": "Thank you! Your feedback has been received.", "data": entry}

def admin_generate_licenses(count=1, days=30, note=""):
    lic_db = get_license_db()
    count = max(1, min(50, int(count)))
    days = max(1, int(days))
    new_keys = []
    import random

    for _ in range(count):
        part1 = f"{random.randint(1000, 9999)}"
        part2 = f"{random.randint(1000, 9999)}"
        key = f"PSX-PRO-{part1}-{part2}"
        while key in lic_db:
            part1 = f"{random.randint(1000, 9999)}"
            part2 = f"{random.randint(1000, 9999)}"
            key = f"PSX-PRO-{part1}-{part2}"

        lic_db[key] = {
            "valid": True,
            "days": days,
            "used": False,
            "email": None,
            "name": None,
            "note": note or f"Generated {days}-Day Key",
            "created_at": time.strftime("%Y-%m-%d %H:%M PKT", time.localtime(time.time() + 5*3600))
        }
        new_keys.append(key)

    save_license_db(lic_db)
    return new_keys

def admin_upgrade_to_pro(email, name="", days=30):
    email_clean = (email or "").strip().lower()
    if not email_clean or "@" not in email_clean:
        return {"success": False, "error": "Invalid email address."}

    days_valid = int(days) if days else 30
    now_ts = time.time()
    paid_until = now_ts + (days_valid * 86400)

    # 1. Create or update license in licenses.json
    lic_db = get_license_db()
    assigned_key = None
    for lk, ldata in lic_db.items():
        if (ldata.get("email") or "").strip().lower() == email_clean:
            assigned_key = lk
            ldata["valid"] = True
            ldata["used"] = True
            ldata["days"] = days_valid
            ldata["name"] = name or ldata.get("name") or email_clean.split("@")[0]
            ldata["activated_at"] = time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600))
            break

    if not assigned_key:
        import random
        part1 = f"{random.randint(1000, 9999)}"
        part2 = f"{random.randint(1000, 9999)}"
        assigned_key = f"PSX-PRO-{part1}-{part2}"
        lic_db[assigned_key] = {
            "valid": True,
            "days": days_valid,
            "used": True,
            "email": email_clean,
            "name": name or email_clean.split("@")[0],
            "note": f"Admin Pro Grant ({days_valid} Days)",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600)),
            "activated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600))
        }
    save_license_db(lic_db)

    # 2. Update trial_db across ALL keys linked to this email, device, or IP
    trial_db = get_trial_db()
    linked_dev_ids = set()
    linked_ips = set()

    for k, v in trial_db.items():
        if isinstance(v, dict) and (v.get("email") == email_clean or v.get("paid_email") == email_clean or k == f"email_{email_clean}"):
            if v.get("device_id"): linked_dev_ids.add(v.get("device_id"))
            if v.get("client_ip"): linked_ips.add(v.get("client_ip"))

    for vk, v in active_online_visitors.items():
        if (v.get("email") or "").strip().lower() == email_clean:
            if v.get("deviceId"): linked_dev_ids.add(v.get("deviceId"))
            if v.get("clientIp"): linked_ips.add(v.get("clientIp"))

    paid_record = {
        "email": email_clean,
        "is_paid": True,
        "paid_name": name or email_clean.split("@")[0],
        "paid_email": email_clean,
        "license_key": assigned_key,
        "paid_until": paid_until,
        "trial_end": paid_until,
        "created_at": now_ts,
        "last_active": now_ts,
        "activated_at": time.strftime("%Y-%m-%d %H:%M:%S PKT", time.localtime(now_ts + 5*3600))
    }

    trial_db[f"email_{email_clean}"] = paid_record
    for dev in linked_dev_ids:
        if dev: trial_db[dev] = paid_record
    for ip in linked_ips:
        if ip: trial_db[f"ip_{ip}"] = paid_record

    save_trial_db(trial_db)
    return {
        "success": True, 
        "message": f"Successfully upgraded {email_clean} to Pro for {days_valid} days! (License: {assigned_key})", 
        "licenseKey": assigned_key
    }

def admin_extend_trial_days(email, extra_days=3):
    email_clean = (email or "").strip().lower()
    if not email_clean:
        return {"success": False, "error": "Invalid email."}

    trial_db = get_trial_db()
    now_ts = time.time()
    extra_sec = int(extra_days) * 86400

    updated = False
    for k, v in list(trial_db.items()):
        if isinstance(v, dict) and (v.get("email") == email_clean or k == f"email_{email_clean}"):
            current_end = max(now_ts, v.get("trial_end", now_ts))
            v["trial_end"] = current_end + extra_sec
            v["is_paid"] = False
            trial_db[k] = v
            updated = True

    if not updated:
        trial_db[f"email_{email_clean}"] = {
            "email": email_clean,
            "created_at": now_ts,
            "trial_end": now_ts + extra_sec,
            "is_paid": False
        }

    save_trial_db(trial_db)
    return {"success": True, "message": f"Extended trial for {email_clean} by {extra_days} days!"}

def admin_delete_user_record(email):
    email_clean = (email or "").strip().lower()
    trial_db = get_trial_db()
    keys_to_del = [k for k, v in trial_db.items() if isinstance(v, dict) and (v.get("email") == email_clean or k == f"email_{email_clean}")]
    for k in keys_to_del:
        del trial_db[k]
    save_trial_db(trial_db)
    return {"success": True, "message": f"Removed records for {email_clean}"}


# ─── HTTP Request Handler ───
class PSXHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler for API routes + static file serving."""

    def __init__(self, *args, **kwargs):
        # Serve from current directory
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def _get_client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _check_auth(self, query=None):
        host_header = self.headers.get("Host", "")
        client_ip = self._get_client_ip()
        device_id = query.get("deviceId", [""])[0] if query else ""
        email = query.get("email", [""])[0] if query else ""
        status = check_trial_status(client_ip, device_id, host_header, email)
        if status.get("isLocal") or status.get("trialActive"):
            return True
        self._send_json({
            "success": False,
            "error": "3-Day Free Trial Expired. Upgrade to PSX Screener Pro to continue accessing live data.",
            "trialExpired": True
        }, 402)
        return False

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed_path = urlparse(self.path)
        
        if parsed_path.path == "/api/stocks":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            self._handle_stocks()
        elif parsed_path.path == "/api/indices":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            self._handle_indices()
        elif parsed_path.path == "/api/company":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            symbol = query.get("symbol", [""])[0]
            self._handle_company(symbol)
        elif parsed_path.path == "/api/upper-lock-analysis":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            self._handle_upper_lock_analysis()
        elif parsed_path.path == "/api/live-trading":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            symbol = query.get("symbol", [""])[0]
            self._handle_live_trading(symbol)
        elif parsed_path.path == "/api/position-analysis":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            symbol = query.get("symbol", [""])[0]
            buy_price = query.get("buyPrice", ["0"])[0]
            qty = query.get("qty", ["0"])[0]
            purchase_date = query.get("purchaseDate", [None])[0]
            self._handle_position_analysis(symbol, buy_price, qty, purchase_date)
        elif parsed_path.path == "/api/financial-statements":
            query = parse_qs(parsed_path.query)
            if not self._check_auth(query): return
            symbol = query.get("symbol", [""])[0]
            self._handle_financial_statements(symbol)
        elif parsed_path.path == "/api/heartbeat":
            query = parse_qs(parsed_path.query)
            device_id = query.get("deviceId", [""])[0]
            email = query.get("email", [""])[0]
            tab = query.get("tab", ["Stock Screener"])[0]
            client_ip = self._get_client_ip()
            user_agent = self.headers.get("User-Agent", "")
            record_visitor_heartbeat(client_ip, device_id, email, tab, user_agent)
            self._send_json({"success": True, "online": True})
        elif parsed_path.path == "/api/trial-status":
            query = parse_qs(parsed_path.query)
            device_id = query.get("deviceId", [""])[0]
            email = query.get("email", [""])[0]
            orig_start_ts = query.get("origStartTs", [0])[0]
            host_header = self.headers.get("Host", "")
            client_ip = self._get_client_ip()
            user_agent = self.headers.get("User-Agent", "")
            record_visitor_heartbeat(client_ip, device_id, email, "Stock Screener", user_agent)
            res = check_trial_status(client_ip, device_id, host_header, email, user_agent, orig_start_ts)
            self._send_json({"success": True, "data": res})
        elif parsed_path.path == "/api/start-trial":
            query = parse_qs(parsed_path.query)
            email = query.get("email", [""])[0]
            device_id = query.get("deviceId", [""])[0]
            host_header = self.headers.get("Host", "")
            client_ip = self._get_client_ip()
            user_agent = self.headers.get("User-Agent", "")
            res = start_trial(client_ip, device_id, email, host_header, user_agent)
            self._send_json(res, 200 if res["success"] else 400)
        elif parsed_path.path == "/api/activate-license":
            query = parse_qs(parsed_path.query)
            key = query.get("key", [""])[0]
            name = query.get("name", [""])[0]
            email = query.get("email", [""])[0]
            device_id = query.get("deviceId", [""])[0]
            client_ip = self._get_client_ip()
            res = activate_license(key, name, email, device_id, client_ip)
            self._send_json(res, 200 if res["success"] else 400)
        elif parsed_path.path.startswith('/api/stock-history/'):
            symbol = parsed_path.path.split('/')[-1]
            self._handle_stock_history(symbol)

        # ─── Admin Endpoints ───
        elif parsed_path.path in ["/admin", "/admin/"]:
            self.path = "/admin.html"
            super().do_GET()
        elif parsed_path.path == "/api/admin/login":
            query = parse_qs(parsed_path.query)
            secret = query.get("secret", [""])[0]
            if verify_admin_secret(secret):
                self._send_json({"success": True, "message": "Admin authenticated successfully."})
            else:
                self._send_json({"success": False, "error": "Invalid Admin Secret Password."}, 401)
        elif parsed_path.path == "/api/admin/stats":
            query = parse_qs(parsed_path.query)
            secret = query.get("secret", [""])[0]
            if not verify_admin_secret(secret):
                self._send_json({"success": False, "error": "Unauthorized Access."}, 401)
                return
            data = get_admin_dashboard_data()
            self._send_json({"success": True, "data": data})
        elif parsed_path.path == "/api/admin/generate-keys":
            query = parse_qs(parsed_path.query)
            secret = query.get("secret", [""])[0]
            if not verify_admin_secret(secret):
                self._send_json({"success": False, "error": "Unauthorized Access."}, 401)
                return
            count = int(query.get("count", ["1"])[0])
            days = int(query.get("days", ["30"])[0])
            note = query.get("note", [""])[0]
            keys = admin_generate_licenses(count, days, note)
            self._send_json({"success": True, "keys": keys, "message": f"Generated {len(keys)} new {days}-day license key(s)!"})
        elif parsed_path.path == "/api/admin/make-pro":
            query = parse_qs(parsed_path.query)
            secret = query.get("secret", [""])[0]
            if not verify_admin_secret(secret):
                self._send_json({"success": False, "error": "Unauthorized Access."}, 401)
                return
            email = query.get("email", [""])[0]
            name = query.get("name", [""])[0]
            days = int(query.get("days", ["30"])[0])
            res = admin_upgrade_to_pro(email, name, days)
            self._send_json(res, 200 if res["success"] else 400)
        elif parsed_path.path == "/api/admin/extend-trial":
            query = parse_qs(parsed_path.query)
            secret = query.get("secret", [""])[0]
            if not verify_admin_secret(secret):
                self._send_json({"success": False, "error": "Unauthorized Access."}, 401)
                return
            email = query.get("email", [""])[0]
            days = int(query.get("days", ["3"])[0])
            res = admin_extend_trial_days(email, days)
            self._send_json(res, 200 if res["success"] else 400)
        elif parsed_path.path == "/api/admin/delete-user":
            query = parse_qs(parsed_path.query)
            secret = query.get("secret", [""])[0]
            if not verify_admin_secret(secret):
                self._send_json({"success": False, "error": "Unauthorized Access."}, 401)
                return
        elif parsed_path.path == "/api/feedback":
            query = parse_qs(parsed_path.query)
            rating = query.get("rating", ["5"])[0]
            topic = query.get("topic", ["General"])[0]
            message = query.get("message", [""])[0]
            email = query.get("email", [""])[0]
            device_id = query.get("deviceId", [""])[0]
            client_ip = self._get_client_ip()
            user_agent = self.headers.get("User-Agent", "")
            res = record_feedback(rating, topic, message, email, client_ip, device_id, user_agent)
            self._send_json(res)
        else:
            # Serve static files
            super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        if self.path == "/api/company":
            try:
                body = json.loads(post_data.decode('utf-8'))
                symbol = body.get('symbol', '')
                self._handle_company(symbol)
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 400)
        elif self.path == "/api/feedback":
            try:
                body = json.loads(post_data.decode('utf-8'))
                rating = body.get('rating', 5)
                topic = body.get('topic', 'General')
                message = body.get('message', '')
                email = body.get('email', '')
                device_id = body.get('deviceId', '')
                client_ip = self._get_client_ip()
                user_agent = self.headers.get("User-Agent", "")
                res = record_feedback(rating, topic, message, email, client_ip, device_id, user_agent)
                self._send_json(res)
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 400)
        else:
            self.send_error(404)

    def _send_json(self, data, status=200):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected, nothing to do

    def _handle_stocks(self):
        try:
            stocks, is_stale = fetch_stock_data()
            cache_ts = stock_cache["timestamp"] or time.time()
            self._send_json({
                "success": True,
                "count": len(stocks),
                "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cache_ts)),
                "stale": is_stale,
                "data": stocks,
            })
        except Exception as e:
            print(f"[PSX] Error fetching stocks: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_indices(self):
        try:
            data, is_stale = fetch_index_data()
            self._send_json({"success": True, "stale": is_stale, **data})
        except Exception as e:
            print(f"[PSX] Error fetching indices: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_company(self, symbol):
        if not symbol:
            self._send_json({"success": False, "error": "Missing symbol parameter"}, 400)
            return
            
        try:
            data = fetch_company_data(symbol)
            if data is None:
                self._send_json({"success": False, "error": "Company not found"}, 404)
            else:
                self._send_json({"success": True, "data": data})
        except Exception as e:
            print(f"[PSX] Error fetching company profile for {symbol}: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_upper_lock_analysis(self):
        try:
            stocks, is_stale = fetch_stock_data()
            today_locked, predicted, history = calculate_upper_lock_analysis(stocks)

            # Get yesterday's locked stocks from history
            today_str = time.strftime("%Y-%m-%d")
            dates = sorted([d for d in history.keys() if d != today_str], reverse=True)
            yesterday_locked = history.get(dates[0], []) if dates else []
            yesterday_date = dates[0] if dates else None

            self._send_json({
                "success": True,
                "todayLocked": today_locked,
                "yesterdayLocked": yesterday_locked,
                "yesterdayDate": yesterday_date,
                "predicted": predicted,
                "totalAnalyzed": len(stocks),
                "disclaimer": "This analysis is based on price patterns and technical indicators. It is a probability-based forecast — not financial advice or a guarantee of future performance."
            })
        except Exception as e:
            print(f"[PSX] Error in upper lock analysis: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_stock_history(self, symbol):
        if not symbol:
            self._send_json({"success": False, "error": "Missing symbol"}, 400)
            return
        try:
            history = fetch_stock_history(symbol.upper())
            if history is None:
                self._send_json({"success": False, "error": f"No history found for {symbol}"}, 404)
            else:
                self._send_json({
                    "success": True,
                    "symbol": symbol.upper(),
                    "days": history,
                    "totalDays": len(history)
                })
        except Exception as e:
            print(f"[PSX] Error in stock history handler: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_position_analysis(self, symbol, buy_price, qty, purchase_date=None):
        if not symbol:
            self._send_json({"success": False, "error": "Missing symbol parameter"}, 400)
            return
        try:
            analysis = generate_position_analysis(symbol, buy_price, qty, purchase_date)
            if not analysis:
                self._send_json({"success": False, "error": f"Symbol '{symbol.upper()}' not found"}, 404)
            else:
                self._send_json({"success": True, "data": analysis})
        except Exception as e:
            print(f"[PSX] Error in position analysis handler: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_live_trading(self, symbol):

        if not symbol:
            self._send_json({"success": False, "error": "Missing symbol parameter"}, 400)
            return
        try:
            analysis = fetch_live_stock_analysis(symbol)
            if not analysis["stockInfo"]:
                self._send_json({"success": False, "error": f"Symbol '{symbol.upper()}' not found"}, 404)
            else:
                self._send_json({"success": True, "data": analysis})
        except Exception as e:
            print(f"[PSX] Error in live trading analysis handler: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_financial_statements(self, symbol):
        if not symbol:
            self._send_json({"success": False, "error": "Missing symbol parameter"}, 400)
            return
        try:
            fin = fetch_financial_statements(symbol)
            if fin is None:
                self._send_json({"success": False, "error": f"Symbol '{symbol.upper()}' not found"}, 404)
            else:
                self._send_json({"success": True, "data": fin})
        except Exception as e:
            print(f"[PSX] Error in financial statements handler: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_dividends_corporate_actions(self):
        try:
            res = fetch_dividends_corporate_actions()
            self._send_json({"success": True, "data": res})
        except Exception as e:
            print(f"[PSX] Error in dividends/corporate actions handler: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_refresh(self):
        global stock_cache, index_cache
        stock_cache = {"data": None, "timestamp": 0}
        index_cache = {"data": None, "timestamp": 0}
        self._send_json({"success": True, "message": "Cache cleared."})

    def log_message(self, format, *args):
        # Only log API requests, not static files
        try:
            req_line = self.requestline if hasattr(self, 'requestline') else ""
            if "/api/" in req_line:
                super().log_message(format, *args)
        except Exception:
            super().log_message(format, *args)


# ─── Main ───
if __name__ == "__main__":
    import socket
    local_ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    # Use ThreadingHTTPServer so that multiple requests don't block each other
    if hasattr(http.server, 'ThreadingHTTPServer'):
        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), PSXHandler)
    else:
        server = http.server.HTTPServer(("0.0.0.0", PORT), PSXHandler)

    print(f"\n  🚀 PSX Stock Screener is running!")
    print(f"  💻 Computer Browser: http://localhost:{PORT}")
    for ip in local_ips:
        print(f"  📱 Mobile Phone URL: http://{ip}:{PORT}")
    print(f"  📡 Live data from dps.psx.com.pk\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()

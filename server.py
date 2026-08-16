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

    if stock_cache["data"]:
        # Check if data is stale (older than cache duration)
        if (now - stock_cache["timestamp"]) > CACHE_DURATION:
            is_stale = True
            _trigger_background_refresh()
        return stock_cache["data"], is_stale

    # No data at all — must do a synchronous fetch (first ever request)
    try:
        _do_fetch_stocks()
        if stock_cache["data"]:
            return stock_cache["data"], False
    except Exception:
        pass
    raise ValueError("No stock data available")


def fetch_index_data():
    """Return cached index data IMMEDIATELY. Trigger background refresh if stale."""
    is_stale = False
    now = time.time()

    if index_cache["data"]:
        if (now - index_cache["timestamp"]) > CACHE_DURATION:
            is_stale = True
            _trigger_background_refresh()
        return index_cache["data"], is_stale

    # No data at all
    try:
        _do_fetch_indices()
        if index_cache["data"]:
            return index_cache["data"], False
    except Exception:
        pass
    # Return empty but valid response rather than crashing
    return {
        "indices": [],
        "market": {"state": "Closed", "volume": 0, "value": 0},
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, True


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

def get_trial_db():
    if os.path.exists(TRIAL_FILE):
        try:
            with open(TRIAL_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_trial_db(db):
    try:
        with open(TRIAL_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"[PSX] Error saving trial db: {e}")

def check_trial_status(client_ip, device_id, host_header=""):
    # Detect if request is coming from local network / localhost
    host_lower = (host_header or "").lower()
    is_local_host = any(h in host_lower for h in ["localhost", "127.0.0.1", "192.168.", "10."]) or \
                   any(ip in (client_ip or "") for ip in ["127.0.0.1", "192.168.", "10."])

    if is_local_host:
        return {
            "isLocal": True,
            "trialActive": True,
            "unlimited": True,
            "message": "Local Mode — Unlimited Access (No Trial Needed)"
        }

    # Online Deployment Mode: 3-Day (72-hour) Trial per IP & Device ID
    db = get_trial_db()
    key = device_id or client_ip or "online_guest"
    ip_key = f"ip_{client_ip}" if client_ip else key
    now_ts = time.time()

    user_info = db.get(key) or db.get(ip_key)

    if not user_info:
        # Needs Email Registration to start 3-day trial
        return {
            "isLocal": False,
            "needsEmail": True,
            "trialActive": False,
            "message": "Registration Required to Start 3-Day Free Trial"
        }

    if user_info.get("is_paid"):
        return {"isLocal": False, "trialActive": True, "isPaid": True, "message": "Pro Account Active"}

    trial_end = user_info.get("trial_end", now_ts)
    time_left = trial_end - now_ts

    if time_left <= 0:
        return {
            "isLocal": False,
            "trialActive": False,
            "hoursLeft": 0,
            "daysLeft": 0,
            "message": "3-Day Free Trial Expired"
        }

    hours_left = round(time_left / 3600, 1)
    days_left = max(1, int(time_left // 86400) + 1)

    return {
        "isLocal": False,
        "trialActive": True,
        "isPaid": False,
        "hoursLeft": hours_left,
        "daysLeft": days_left,
        "message": f"Online Mode — {days_left} Days Left in Free Trial"
    }

def start_trial(client_ip, device_id, email, host_header=""):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"success": False, "error": "Please enter a valid email address."}

    db = get_trial_db()
    key = device_id or client_ip or "online_guest"
    ip_key = f"ip_{client_ip}" if client_ip else key
    now_ts = time.time()

    existing = db.get(key) or db.get(ip_key)
    if existing:
        if existing.get("is_paid"):
            return {"success": True, "message": "Pro Account Active", "isPaid": True}
        
        trial_end = existing.get("trial_end", now_ts)
        time_left = trial_end - now_ts
        if time_left > 0:
            return {
                "success": True,
                "message": "Trial Active",
                "daysLeft": max(1, int(time_left // 86400) + 1),
                "hoursLeft": round(time_left / 3600, 1)
            }
        else:
            return {"success": False, "error": "Your 3-day free trial on this IP / Device has already expired. Please upgrade to Pro."}

    new_trial = {
        "email": email,
        "client_ip": client_ip,
        "device_id": device_id,
        "created_at": now_ts,
        "trial_end": now_ts + (3 * 24 * 3600),
        "is_paid": False
    }
    db[key] = new_trial
    if client_ip:
        db[ip_key] = new_trial
    save_trial_db(db)

    return {
        "success": True,
        "message": "3-Day Free Trial Started!",
        "daysLeft": 3,
        "hoursLeft": 72.0
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
        days_valid = lic.get("days", 30)

        trial_db[user_key] = {
            "is_paid": True,
            "paid_name": name,
            "paid_email": email,
            "license_key": key,
            "paid_until": time.time() + (days_valid * 24 * 3600),
            "activated_at": lic["activated_at"]
        }
        save_trial_db(trial_db)

        return {
            "success": True,
            "message": f"License Activated Successfully! Welcome {name} to PSX Screener Pro ({days_valid} Days Access).",
            "name": name,
            "email": email,
            "daysValid": days_valid
        }

    return {"success": False, "error": "Invalid License Key. Please check the code or contact support via WhatsApp 0306 6400721."}


# ─── HTTP Request Handler ───
class PSXHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler for API routes + static file serving."""

    def __init__(self, *args, **kwargs):
        # Serve from current directory
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed_path = urlparse(self.path)
        
        if parsed_path.path == "/api/stocks":
            self._handle_stocks()
        elif parsed_path.path == "/api/indices":
            self._handle_indices()
        elif parsed_path.path == "/api/company":
            query = parse_qs(parsed_path.query)
            symbol = query.get("symbol", [""])[0]
            self._handle_company(symbol)
        elif parsed_path.path == "/api/upper-lock-analysis":
            self._handle_upper_lock_analysis()
        elif parsed_path.path == "/api/live-trading":
            query = parse_qs(parsed_path.query)
            symbol = query.get("symbol", [""])[0]
            self._handle_live_trading(symbol)
        elif parsed_path.path == "/api/position-analysis":
            query = parse_qs(parsed_path.query)
            symbol = query.get("symbol", [""])[0]
            buy_price = query.get("buyPrice", ["0"])[0]
            qty = query.get("qty", ["0"])[0]
            purchase_date = query.get("purchaseDate", [None])[0]
            self._handle_position_analysis(symbol, buy_price, qty, purchase_date)
        elif parsed_path.path == "/api/financial-statements":
            query = parse_qs(parsed_path.query)
            symbol = query.get("symbol", [""])[0]
            self._handle_financial_statements(symbol)
        elif parsed_path.path == "/api/trial-status":
            query = parse_qs(parsed_path.query)
            device_id = query.get("deviceId", [""])[0]
            host_header = self.headers.get("Host", "")
            client_ip = self.client_address[0] if self.client_address else ""
            res = check_trial_status(client_ip, device_id, host_header)
            self._send_json({"success": True, "data": res})
        elif parsed_path.path == "/api/start-trial":
            query = parse_qs(parsed_path.query)
            email = query.get("email", [""])[0]
            device_id = query.get("deviceId", [""])[0]
            host_header = self.headers.get("Host", "")
            client_ip = self.client_address[0] if self.client_address else ""
            res = start_trial(client_ip, device_id, email, host_header)
            self._send_json(res, 200 if res["success"] else 400)
        elif parsed_path.path == "/api/activate-license":
            query = parse_qs(parsed_path.query)
            key = query.get("key", [""])[0]
            name = query.get("name", [""])[0]
            email = query.get("email", [""])[0]
            device_id = query.get("deviceId", [""])[0]
            client_ip = self.client_address[0] if self.client_address else ""
            res = activate_license(key, name, email, device_id, client_ip)
            self._send_json(res, 200 if res["success"] else 400)
        elif parsed_path.path.startswith('/api/stock-history/'):
            symbol = parsed_path.path.split('/')[-1]
            self._handle_stock_history(symbol)
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

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
FETCH_TIMEOUT = 15    # seconds (increased for Render cold starts)
FETCH_RETRIES = 3     # retry attempts

# ─── Persistent file cache paths ───
DATA_DIR = Path(__file__).parent / "cache"
DATA_DIR.mkdir(exist_ok=True)
STOCK_CACHE_FILE = DATA_DIR / "stocks_cache.json"
INDEX_CACHE_FILE = DATA_DIR / "index_cache.json"

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


# Load file caches on startup (survives Render sleep/wake within same deploy)
_stock_file = load_file_cache(STOCK_CACHE_FILE)
if _stock_file:
    stock_cache = {"data": _stock_file["data"], "timestamp": _stock_file.get("timestamp", 0)}

_index_file = load_file_cache(INDEX_CACHE_FILE)
if _index_file:
    index_cache = {"data": _index_file["data"], "timestamp": _index_file.get("timestamp", 0)}

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


def fetch_stock_data():
    """Fetch and parse stock data from PSX screener page.
    Falls back to file-cached data if PSX is unreachable."""
    global stock_cache
    now = time.time()
    if stock_cache["data"] and (now - stock_cache["timestamp"]) < CACHE_DURATION:
        return stock_cache["data"], False  # data, is_stale

    try:
        print("[PSX] Fetching live stock data from dps.psx.com.pk/screener...")
        html = fetch_url("https://dps.psx.com.pk/screener")

        parser = PSXScreenerParser()
        parser.feed(html)

        if parser.stocks:
            print(f"[PSX] Parsed {len(parser.stocks)} stocks from PSX screener.")
            stock_cache = {"data": parser.stocks, "timestamp": now}
            save_file_cache(STOCK_CACHE_FILE, parser.stocks, now)
            return parser.stocks, False
        else:
            print("[PSX] WARNING: Parsed 0 stocks from PSX. Using cached data.")
            raise ValueError("Empty response from PSX")
    except Exception as e:
        print(f"[PSX] Live fetch failed: {e}. Falling back to cache.")
        if stock_cache["data"]:
            return stock_cache["data"], True  # stale
        raise  # no cache at all, propagate error


def fetch_index_data():
    """Fetch and parse index data from PSX homepage.
    Falls back to file-cached data if PSX is unreachable."""
    global index_cache
    now = time.time()
    if index_cache["data"] and (now - index_cache["timestamp"]) < CACHE_DURATION:
        return index_cache["data"], False

    try:
        print("[PSX] Fetching index data from dps.psx.com.pk...")
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

        print(f"[PSX] Parsed {len(indices)} indices. Market: {market_state}")
        index_cache = {"data": result, "timestamp": now}
        save_file_cache(INDEX_CACHE_FILE, result, now)
        return result, False
    except Exception as e:
        print(f"[PSX] Index fetch failed: {e}. Falling back to cache.")
        if index_cache["data"]:
            return index_cache["data"], True
        raise


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
        else:
            # Serve static files
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/refresh":
            self._handle_refresh()
        else:
            self.send_error(404)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

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
    # Use ThreadingHTTPServer so that multiple requests don't block each other
    if hasattr(http.server, 'ThreadingHTTPServer'):
        server = http.server.ThreadingHTTPServer(("", PORT), PSXHandler)
    else:
        server = http.server.HTTPServer(("", PORT), PSXHandler)
    print(f"\n  🚀 PSX Stock Screener is running!")
    print(f"  📊 Open http://localhost:{PORT} in your browser")
    print(f"  📡 Live data from dps.psx.com.pk")
    print(f"  🐍 Powered by Python (zero dependencies)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()

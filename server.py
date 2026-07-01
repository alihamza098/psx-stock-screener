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
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path

PORT = int(os.environ.get('PORT', 3000))
CACHE_DURATION = 60  # seconds

# ─── Simple in-memory cache ───
stock_cache = {"data": None, "timestamp": 0}
index_cache = {"data": None, "timestamp": 0}

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
    """Fetch URL content with proper headers."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_stock_data():
    """Fetch and parse stock data from PSX screener page."""
    global stock_cache
    now = time.time()
    if stock_cache["data"] and (now - stock_cache["timestamp"]) < CACHE_DURATION:
        return stock_cache["data"]

    print("[PSX] Fetching live stock data from dps.psx.com.pk/screener...")
    html = fetch_url("https://dps.psx.com.pk/screener")

    parser = PSXScreenerParser()
    parser.feed(html)

    print(f"[PSX] Parsed {len(parser.stocks)} stocks from PSX screener.")
    stock_cache = {"data": parser.stocks, "timestamp": now}
    return parser.stocks


def fetch_index_data():
    """Fetch and parse index data from PSX homepage."""
    global index_cache
    now = time.time()
    if index_cache["data"] and (now - index_cache["timestamp"]) < CACHE_DURATION:
        return index_cache["data"]

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
    return result


# ─── HTTP Request Handler ───
class PSXHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler for API routes + static file serving."""

    def __init__(self, *args, **kwargs):
        # Serve from current directory
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_GET(self):
        if self.path == "/api/stocks":
            self._handle_stocks()
        elif self.path == "/api/indices":
            self._handle_indices()
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
            stocks = fetch_stock_data()
            self._send_json({
                "success": True,
                "count": len(stocks),
                "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stock_cache["timestamp"])),
                "data": stocks,
            })
        except Exception as e:
            print(f"[PSX] Error fetching stocks: {e}")
            if stock_cache["data"]:
                self._send_json({
                    "success": True,
                    "count": len(stock_cache["data"]),
                    "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stock_cache["timestamp"])),
                    "stale": True,
                    "data": stock_cache["data"],
                })
            else:
                self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_indices(self):
        try:
            data = fetch_index_data()
            self._send_json({"success": True, **data})
        except Exception as e:
            print(f"[PSX] Error fetching indices: {e}")
            if index_cache["data"]:
                self._send_json({"success": True, "stale": True, **index_cache["data"]})
            else:
                self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_refresh(self):
        global stock_cache, index_cache
        stock_cache = {"data": None, "timestamp": 0}
        index_cache = {"data": None, "timestamp": 0}
        self._send_json({"success": True, "message": "Cache cleared."})

    def log_message(self, format, *args):
        # Only log API requests, not static files
        if "/api/" in (args[0] if args else ""):
            super().log_message(format, *args)


# ─── Main ───
if __name__ == "__main__":
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

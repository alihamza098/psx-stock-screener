import asyncio
import aiohttp
import json
import time
from bs4 import BeautifulSoup

# We need the list of symbols. Let's fetch the screener first.
async def get_symbols():
    try:
        import urllib.request
        from html.parser import HTMLParser
        
        class ScreenerParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.symbols = []
                self.in_tbody = False
                self.in_row = False
                self.in_td = False
            def handle_starttag(self, tag, attrs):
                if tag == "tbody": self.in_tbody = True
                if self.in_tbody and tag == "tr": self.in_row = True
                if self.in_row and tag == "td": self.in_td = True
            def handle_data(self, data):
                if self.in_td and data.strip() and not hasattr(self, 'current_symbol'):
                    self.current_symbol = data.strip()
            def handle_endtag(self, tag):
                if tag == "td" and self.in_td: 
                    self.in_td = False
                    if hasattr(self, 'current_symbol'):
                        self.symbols.append(self.current_symbol)
                        del self.current_symbol
                if tag == "tr" and self.in_row: self.in_row = False
                if tag == "tbody": self.in_tbody = False

        req = urllib.request.Request("https://dps.psx.com.pk/screener", headers={'User-Agent': 'Mozilla'})
        html = urllib.request.urlopen(req).read().decode('utf-8')
        parser = ScreenerParser()
        parser.feed(html)
        return list(dict.fromkeys(parser.symbols)) # Unique symbols
    except Exception as e:
        print(e)
        return []

async def fetch_financials(session, symbol):
    try:
        async with session.get(f"https://dps.psx.com.pk/company/{symbol}", timeout=10) as response:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            tables = soup.find_all('table')
            
            revenue_data = {}
            for t in tables:
                text = t.get_text()
                if 'Sales' in text or 'Turnover' in text or 'Revenue' in text:
                    headers = [th.get_text(strip=True) for th in t.find_all('th') if th.get_text(strip=True).isdigit()]
                    if headers:
                        rows = t.find('tbody').find_all('tr')
                        for row in rows:
                            cols = row.find_all('td')
                            label = cols[0].get_text(strip=True).lower()
                            if 'sales' in label or 'revenue' in label or 'turnover' in label:
                                for i, h in enumerate(headers):
                                    if i + 1 < len(cols):
                                        val_str = cols[i+1].get_text(strip=True).replace(',', '')
                                        try:
                                            revenue_data[h] = float(val_str)
                                        except ValueError:
                                            pass
                                break
                    if revenue_data:
                        break # Found annual revenue
            return symbol, revenue_data
    except Exception as e:
        return symbol, {}

async def main():
    symbols = await get_symbols()
    print(f"Fetching financials for {len(symbols)} symbols...")
    start = time.time()
    
    financials = {}
    async with aiohttp.ClientSession(headers={'User-Agent': 'Mozilla/5.0'}) as session:
        # We will do chunks of 50 to not overload PSX
        chunk_size = 50
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i+chunk_size]
            tasks = [fetch_financials(session, s) for s in chunk]
            results = await asyncio.gather(*tasks)
            for symbol, data in results:
                if data:
                    financials[symbol] = data
            print(f"Processed {i+len(chunk)}/{len(symbols)}...")
            await asyncio.sleep(0.5)
        
    print(f"Finished in {time.time() - start:.2f}s. Saving to financials.json")
    with open("financials.json", "w") as f:
        json.dump(financials, f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())

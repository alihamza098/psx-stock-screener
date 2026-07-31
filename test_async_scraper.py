import asyncio
import aiohttp
import time
from bs4 import BeautifulSoup
import re

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
                    # Look for headers like 2025, 2024, 2023
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
    symbols = ["OGDC", "HUBC", "ENGRO", "PPL", "LUCK", "SYS", "MEBL", "POL", "UBL", "MCB"]
    print(f"Fetching financials for {len(symbols)} symbols...")
    start = time.time()
    
    async with aiohttp.ClientSession(headers={'User-Agent': 'Mozilla/5.0'}) as session:
        tasks = [fetch_financials(session, s) for s in symbols]
        results = await asyncio.gather(*tasks)
        
    print(f"Finished in {time.time() - start:.2f}s")
    for symbol, data in results:
        print(f"{symbol}: {data}")

if __name__ == "__main__":
    asyncio.run(main())

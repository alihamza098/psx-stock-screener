import urllib.request
import re
from bs4 import BeautifulSoup

def find_financials(symbol):
    req = urllib.request.Request(
        f"https://dps.psx.com.pk/company/{symbol}",
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html = urllib.request.urlopen(req).read().decode('utf-8')
    
    # Try to find Financials section
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.find_all('table')
    
    print(f"Found {len(tables)} tables on {symbol} page.")
    for i, t in enumerate(tables):
        text = t.get_text(strip=True)
        if 'Revenue' in text or 'Sales' in text or 'Turnover' in text:
            print(f"\n--- Table {i} might contain Revenue ---")
            print(t.prettify()[:1000])

find_financials("OGDC")

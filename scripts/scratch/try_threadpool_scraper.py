import urllib.request
from bs4 import BeautifulSoup
import concurrent.futures
import time

def fetch_revenue(symbol):
    try:
        req = urllib.request.Request(f"https://dps.psx.com.pk/company/{symbol}", headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        tables = soup.find_all('table')
        
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
                            rev_data = {}
                            for i, h in enumerate(headers):
                                if i + 1 < len(cols):
                                    val_str = cols[i+1].get_text(strip=True).replace(',', '')
                                    try:
                                        rev_data[h] = float(val_str)
                                    except ValueError:
                                        pass
                            return symbol, rev_data
    except Exception as e:
        return symbol, {}
    return symbol, {}

symbols = ["OGDC", "HUBC", "ENGRO", "PPL", "LUCK", "SYS", "MEBL", "POL", "UBL", "MCB"]
start = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    results = list(executor.map(fetch_revenue, symbols))

print(f"Finished in {time.time() - start:.2f}s")
for s, d in results:
    print(f"{s}: {d}")

import urllib.request
import re
import json
from html.parser import HTMLParser

def fetch_company(symbol):
    req = urllib.request.Request(
        f"https://dps.psx.com.pk/company/{symbol}",
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    html = urllib.request.urlopen(req).read().decode('utf-8')
    
    data = {
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
    # There are multiple tabs: Financial Results, Board Meetings, Others. Let's extract all rows from tables inside the announcements section
    announce_section_match = re.search(r'<div class="company__payouts">\s*<h1 class="section__title">Announcements</h1>(.*?)</div>\s*</div>\s*</div>\s*<div class="section', html, re.DOTALL | re.IGNORECASE)
    
    if announce_section_match:
        announce_section = announce_section_match.group(1)
        # Find all <tr> tags with 3 <td>s: Date, Title, Links
        rows = re.findall(r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>', announce_section, re.IGNORECASE | re.DOTALL)
        for d, t, links_html in rows:
            date = d.strip()
            title = t.strip()
            # Extract PDF link if exists
            pdf_match = re.search(r'href="(/download/document/.*?|/download/attachment/.*?)"', links_html, re.IGNORECASE)
            link = "https://dps.psx.com.pk" + pdf_match.group(1) if pdf_match else ""
            data['announcements'].append({
                "date": date,
                "title": title,
                "link": link
            })
            
    print(json.dumps(data, indent=2))

fetch_company("OGDC")

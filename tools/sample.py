"""What does a channel actually post? Prints, per channel, the link destinations and the first line
of its latest posts (text only, never media). Usage: python tools/sample.py <channel> [<channel> ...]"""
import json
import re
import sys
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
SHORT = re.compile(r"^(bit\.ly|tinyurl\.com|s\.click\.aliexpress\.com|a\.aliexpress\.com|amzn\.to|amzn\.eu|temu\.to|did\.li|katzr\.net|cutt\.ly|rb\.gy)$")


def resolve(url):
    try:
        r = requests.head(url, headers=UA, timeout=12, allow_redirects=True)
        return urlparse(r.url).netloc.lower().replace("www.", "")
    except Exception:
        return "?"


for u in sys.argv[1:]:
    soup = BeautifulSoup(requests.get("https://t.me/s/" + u, headers=UA, timeout=25).text, "html.parser")
    posts, hosts, resolved = [], Counter(), Counter()
    for m in soup.select(".tgme_widget_message"):
        body = m.select_one(".tgme_widget_message_text")
        if not body:
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = body.get_text(" ").strip()
        first = next((l.strip() for l in text.split("\n") if len(l.strip()) > 3), "")[:110]
        links = [a["href"] for a in body.select("a[href]") if a["href"].startswith("http") and "t.me/" not in a["href"]]
        for h in {urlparse(x).netloc.lower().replace("www.", "") for x in links}:
            hosts[h] += 1
        for x in links[:1]:
            h = urlparse(x).netloc.lower().replace("www.", "")
            if SHORT.match(h) and sum(resolved.values()) < 8:
                resolved[resolve(x)] += 1
        price = re.findall(r"(?:₪\s?\d[\d,.]*|\d[\d,.]*\s?(?:₪|ש\"ח|שח)|\$\s?\d[\d,.]*)", text)
        posts.append({"first": first, "len": len(text), "links": len(links), "price": price[:2]})
    print("SAMPLE " + json.dumps({"u": u, "posts": len(posts), "hosts": dict(hosts.most_common(8)),
                                  "short_links_lead_to": dict(resolved), "latest": posts[-8:]}, ensure_ascii=True), flush=True)

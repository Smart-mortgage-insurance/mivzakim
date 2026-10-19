"""Fetches public web pages (logged-out) and prints ONLY contact details found in their text:
e-mails, phone numbers, WhatsApp links, web sites. HTML only - never downloads images or video.
Usage: python tools/fetchtext.py <url> [<url> ...]   -> one `PAGE {json}` line per url."""
import json
import re
import sys
from urllib.parse import unquote

import requests

UAS = {
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "crawler": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "bot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
}
PHONE = re.compile(r"(?<![\d.])(?:\+?972[-\s]?|0)(?:5\d|[2-4]|7\d|[89])[-\s]?\d{3}[-\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[A-Za-z]{2,})+")
WA = re.compile(r"wa\.me/\+?(\d{9,15})|whatsapp\.com/send/?\?[^\s\"']*?phone=\+?(\d{9,15})", re.I)
JUNK = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|js|css)$|sentry|wixpress|example\.|@2x|\d+\.\d+\.\d+|facebook\.com|fb\.com|fbcdn|paypal\.com", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
META = re.compile(r'<meta[^>]+(?:property|name)="(og:title|og:description|description)"[^>]+content="([^"]*)"', re.I)


def clean(html):
    """Decode the escapes Facebook / JSON blobs use, drop tags."""
    t = html.replace("\\/", "/").replace("\\u0040", "@").replace("&#064;", "@").replace("&amp;", "&")
    t = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), t)
    return unquote(t)


def scan(url):
    best = {"url": url, "status": "no answer"}
    for name, ua in UAS.items():
        try:
            r = requests.get(url, headers={"User-Agent": ua, "Accept-Language": "he-IL,he;q=0.9,en;q=0.6",
                                           "Accept": "text/html,application/xhtml+xml"}, timeout=25, allow_redirects=True)
        except Exception as exc:
            best.setdefault("errors", []).append("%s: %s" % (name, type(exc).__name__))
            continue
        html = r.text or ""
        txt = clean(html)
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        t = TITLE.search(html)
        info = {
            "url": url, "ua": name, "status": r.status_code, "final": r.url[:120], "bytes": len(html),
            "login_wall": bool(re.search(r"/login|log in to facebook|you must log in|התחבר", r.url + html[:4000], re.I)),
            "title": re.sub(r"\s+", " ", clean(t.group(1)))[:120] if t else "",
            "meta": {k: clean(v)[:300] for k, v in META.findall(html)},
            "mails": sorted({m.lower().rstrip(".") for m in EMAIL.findall(txt) if not JUNK.search(m)})[:12],
            "phones": sorted({re.sub(r"\D", "", m) for m in PHONE.findall(body)})[:12],
            "wa": sorted({a or b for a, b in WA.findall(txt)})[:6],
            "sites": sorted(set(re.findall(r'"(?:website|websites?_url|url)"\s*:\s*"(https?://(?!(?:www\.|m\.|l\.)?facebook)[^"]{4,80})"', txt)))[:6],
        }
        # contact-looking fields inside the page's embedded data (Facebook keeps "about" details there)
        kv = set()
        for k, v in re.findall(r'"([a-z_]*(?:email|phone|whatsapp|contact|website|address|category_name|owner|admin)[a-z_]*)"\s*:\s*"([^"]{3,90})"', txt, re.I):
            if not re.search(r"fbcdn|static\.|\.js|\.php|^https?://(?:www\.)?facebook", v):
                kv.add(k + "=" + v)
        info["fields"] = sorted(kv)[:25]
        near = set()
        for m in re.finditer(r"(?:מייל|אימייל|טלפון|וואטסאפ|ווטסאפ|WhatsApp|Email|Phone)[^<>{}\"]{0,70}", body):
            frag = re.sub(r"\s+", " ", m.group(0)).strip()
            if re.search(r"\d{6,}|@", frag):
                near.add(frag[:90])
        info["near"] = sorted(near)[:10]
        if re.match(r"https?://(?:bit\.ly|did\.li|tinyurl\.com|katzr\.net)/", url):
            info["redirects_to"] = r.url[:200]
        score = len(info["mails"]) + len(info["phones"]) + len(info["wa"]) + (0 if info["login_wall"] else 1)
        if score > best.get("_score", -1):
            best = dict(info, _score=score)
    best.pop("_score", None)
    return best


for u in sys.argv[1:]:
    print("PAGE " + json.dumps(scan(u), ensure_ascii=True), flush=True)

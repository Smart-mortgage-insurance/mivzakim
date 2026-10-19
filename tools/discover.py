"""Hunts for more public job channels. Three sources, all from the web (no Telegram login):
  1. forwards / mentions / links found deep in the history of channels we already carry
  2. web search engines restricted to t.me
  3. the channels found in 1+2 that turn out to be job channels are crawled the same way (BFS)
Prints one `FOUND {json}` line per candidate with a public feed. No post text is printed."""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, quote

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "he-IL,he;q=0.9,en;q=0.6"}
CFG = json.loads((Path(__file__).resolve().parent.parent / "scraper" / "config.json").read_text(encoding="utf-8"))
KNOWN = {c["username"].lower() for c in CFG["channels"]}
RE_MODE = "--re" in sys.argv                 # hunt real-estate channels instead of job channels
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
if RE_MODE:                                  # every channel we know may point at a flats channel; args are direct candidates
    SEEDS = [c["username"] for c in CFG["channels"]] + ["Recommended_channels", "RSHIMAE"]
    if "--only" in sys.argv:                 # check just the given candidates (and whatever they point at)
        SEEDS = []
else:
    SEEDS = [c["username"] for c in CFG["channels"] if c.get("enabled", True) and c.get("kind") == "jobs"] + ARGS

TME = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][\w]{3,31})(?![\w])", re.I)
MENTION = re.compile(r"(?<![\w@.])@([A-Za-z]\w{4,31})")
SKIP = {"s", "share", "joinchat", "addstickers", "iv", "proxy", "socks", "login", "addlist", "boost", "c", "telegram"}
JOB = re.compile(r"דרוש|משר[הות]|עבוד[הות]|קו\"ח|קורות חיים|שכר|תפקיד|למשרד|גיוס|job|hiring|vacanc", re.I)
if RE_MODE:
    JOB = re.compile(r"דיר[הת]|דירות|להשכרה|למכירה|חדרים|חד'|נדל\"ן|נדלן|מ\"ר|תיווך|שכירות|פנטהאוז|יחידת דיור|קומה", re.I)
HEB = re.compile(r"[֐-׿]")
PHONE = re.compile(r"(?<!\d)(?:\+?972[-\s]?|0)(?:5\d|[2-4]|7\d|[89])[-\s]?\d{3}[-\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
WA = re.compile(r"wa\.me/\+?(\d{9,15})|whatsapp\.com/send/?\?[^\s\"]*?phone=\+?(\d{9,15})", re.I)

QUERIES = """דרושים|משרות|דרושה|לוח דרושים|משרות חמות|דרושים חרדים|משרות לחרדים|עבודה לחרדים|עבודה מהבית|משרות הייטק
דרושים ירושלים|דרושים בני ברק|דרושים תל אביב|דרושים חיפה|דרושים באר שבע|דרושים אשדוד|דרושים בית שמש|דרושים מודיעין עילית
דרושים אלעד|דרושים פתח תקווה|דרושים צפון|דרושים דרום|דרושים שרון|משרות סטודנטים|משרות ג'וניור|עבודות מזדמנות|משרות הוראה
דרושים נהגים|משרות אדמיניסטרציה|משרות שיווק|משרות כספים|משרות חינוך|ערוץ דרושים|ערוץ משרות|עבודות לנוער|משרות חלקיות
דרושות מזכירות|משרות לנשים|משרות אמהות|דרושים מהיום להיום|עבודה מועדפת|משרות ממשלתיות|משרות עמותות|דרושים רמת גן
דרושים נתניה|דרושים ראשון לציון|דרושים חולון|דרושים רחובות|דרושים אשקלון|דרושים טבריה|דרושים צפת|דרושים ביתר""".replace("\n", "|").split("|")

S = requests.Session()
S.headers.update(UA)
BUDGET = [1700 if RE_MODE else 900]          # max HTTP requests for the whole run


def get(url, **kw):
    if BUDGET[0] <= 0:
        return ""
    BUDGET[0] -= 1
    try:
        r = S.get(url, timeout=20, **kw)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def names_in(text):
    text = unquote(unquote(text))
    out = {m for m in TME.findall(text)} | set()
    return {n for n in out if n.lower() not in SKIP and not n.lower().endswith("bot")}


def page(u, before=None):
    return get("https://t.me/s/" + u + ("?before=%d" % before if before else ""))


def crawl(u, pages):
    """Everything a channel points at, over `pages` pages of history."""
    found, before = set(), None
    for _ in range(pages):
        html = page(u, before)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        msgs = soup.select(".tgme_widget_message")
        if not msgs:
            break
        for a in soup.select(".tgme_widget_message_forwarded_from_name[href], .tgme_widget_message_text a[href], .tgme_channel_info_description a[href]"):
            found |= names_in(a["href"])
        for t in soup.select(".tgme_widget_message_text, .tgme_channel_info_description"):
            txt = t.get_text(" ")
            found |= set(MENTION.findall(txt)) | names_in(txt)
        ids = [int(m["data-post"].split("/")[-1]) for m in msgs if m.get("data-post")]
        if not ids or min(ids) <= 1:
            break
        before = min(ids)
        time.sleep(0.25)
    return {f for f in found if f.lower() != u.lower() and not f.lower().endswith("bot")}


def info(u):
    html = page(u)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    texts = [t.get_text(" ") for t in soup.select(".tgme_widget_message_text")]
    if not soup.select(".tgme_widget_message"):
        return None
    title = soup.select_one(".tgme_channel_info_header_title")
    subs = soup.select_one(".tgme_channel_info_counter .counter_value")
    desc = soup.select_one(".tgme_channel_info_description")
    dtxt = desc.get_text(" ", strip=True) if desc else ""
    dblob = dtxt + " " + (" ".join(a.get("href", "") for a in desc.select("a[href]")) if desc else "")
    times = [t["datetime"] for t in soup.select(".tgme_widget_message_date time[datetime]")]
    now = datetime.now(timezone.utc)
    ages = sorted((now - datetime.fromisoformat(t)).total_seconds() / 3600 for t in times)
    ttl = title.get_text(strip=True) if title else ""
    contacts = sorted({"mail:" + m.lower() for m in EMAIL.findall(dblob)} |
                      {"tel:" + re.sub(r"\D", "", m) for m in PHONE.findall(dtxt)} |
                      {"wa:" + (a or b) for a, b in WA.findall(dblob)} |
                      {"tg:@" + m for m in MENTION.findall(dtxt) if m.lower() != u.lower()})
    return {
        "u": u, "title": ttl, "subs": subs.get_text() if subs else "",
        "posts": len(texts), "newest_h": round(ages[0]) if ages else -1, "oldest_h": round(ages[-1]) if ages else -1,
        "job": sum(1 for t in texts if JOB.search(t)), "heb": sum(1 for t in texts if HEB.search(t)),
        "title_job": bool(JOB.search(ttl + " " + dtxt)), "desc": dtxt[:300], "contacts": contacts,
    }


def search_engines():
    found = set()
    for q in QUERIES:
        for url in ("https://html.duckduckgo.com/html/?q=" + quote("site:t.me " + q),
                    "https://www.bing.com/search?count=50&q=" + quote("site:t.me " + q),
                    "https://search.brave.com/search?q=" + quote("site:t.me " + q)):
            html = get(url)
            got = names_in(html)
            found |= got
            time.sleep(0.8)
        print("SEARCH", len(found), "after", repr(q.encode("ascii", "backslashreplace").decode()))
    return found


def is_jobs(d):
    return d and d["posts"] >= 5 and d["heb"] >= d["posts"] * 0.5 and (d["job"] >= d["posts"] * 0.5 or d["title_job"])


checked, results = set(KNOWN), {}
queue = set(ARGS) if RE_MODE else set()
for s in SEEDS:
    queue |= crawl(s, 8 if RE_MODE else 12)
print("SEED-LINKS", len(queue))
if "--no-search" not in sys.argv and not RE_MODE:
    queue |= search_engines()
print("QUEUE", len(queue), "budget", BUDGET[0])

for depth in range(3):
    nxt = set()
    for u in sorted(queue):
        if u.lower() in checked or BUDGET[0] <= 0:
            continue
        checked.add(u.lower())
        d = info(u)
        time.sleep(0.2)
        if not d:
            continue
        d["depth"] = depth
        results[u.lower()] = d
        if is_jobs(d):
            print("FOUND " + json.dumps(d, ensure_ascii=True), flush=True)
            if d["newest_h"] < 24 * 30:
                nxt |= crawl(u, 8 if RE_MODE else 4)
    queue = nxt
    print("ROUND", depth, "checked", len(checked), "next", len(queue), "budget", BUDGET[0], flush=True)

print("OTHER " + json.dumps([[d["u"], d["subs"], d["newest_h"], d["job"], d["posts"]] for d in results.values() if not is_jobs(d)]))

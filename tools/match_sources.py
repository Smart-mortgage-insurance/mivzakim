"""Which Telegram channels does another feed copy from? Compares the latest messages of a feed API
(default: hagizra.news) with the latest posts of candidate channels: our news channels, every channel
they forward/mention/link, and any usernames given as arguments. Prints counts only, never post text."""
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
CFG = json.loads((Path(__file__).resolve().parent.parent / "scraper" / "config.json").read_text(encoding="utf-8"))
NEWS = [c["username"] for c in CFG["channels"] if not c.get("kind")]
EXTRA = [a for a in sys.argv[1:] if not a.startswith("--")]
TME = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][\w]{3,31})", re.I)
MENTION = re.compile(r"(?<![\w@.])@([A-Za-z]\w{4,31})")
SKIP = {"share", "joinchat", "addlist", "boost", "iv", "proxy", "telegram"}


def norm(t):
    t = re.sub(r"https?://\S+", " ", t or "")
    t = re.sub(r"[^֐-׿A-Za-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def shingles(t):
    w = [x for x in norm(t).split(" ") if len(x) > 1]
    return {" ".join(w[i:i + 4]) for i in range(len(w) - 3)}


def feed():
    msgs = []
    for off in range(0, 600, 50):
        try:
            j = requests.get("https://hagizra.news/api/v2/messages?offset=%d&limit=50&direction=desc" % off, headers=UA, timeout=25).json()
        except Exception as exc:
            print("FEED ERROR", type(exc).__name__)
            break
        m = j.get("messages") or []
        msgs += m
        if len(m) < 50:
            break
    out = []
    for m in msgs:
        if m.get("deleted") or m.get("is_ads") or not m.get("text"):
            continue
        sh = shingles(m["text"])
        if len(sh) >= 3:
            out.append({"ts": m["timestamp"][:19], "sh": sh})
    return out


def channel_posts(u, pages=2):
    """Shingles of the channel's latest posts, its title, and every channel it points at."""
    posts, refs, title, before = [], set(), "", None
    for _ in range(pages):
        html = requests.get("https://t.me/s/" + u + ("?before=%d" % before if before else ""), headers=UA, timeout=25).text
        soup = BeautifulSoup(html, "html.parser")
        if not title:
            t = soup.select_one(".tgme_channel_info_header_title")
            title = t.get_text(strip=True) if t else ""
        msgs = soup.select(".tgme_widget_message")
        if not msgs:
            break
        for m in msgs:
            for a in m.select("a[href]"):
                refs |= {x for x in TME.findall(a["href"])}
            body = m.select_one(".tgme_widget_message_text")
            if body:
                txt = body.get_text(" ")
                refs |= set(MENTION.findall(txt))
                posts.append(shingles(txt))
        ids = [int(m["data-post"].split("/")[-1]) for m in msgs if m.get("data-post")]
        if not ids:
            break
        before = min(ids)
        time.sleep(0.2)
    return title, [p for p in posts if len(p) >= 3], {r for r in refs if r.lower() not in SKIP and r.lower() != u.lower()}


items = feed()
print("FEED %d usable messages" % len(items))
done, queue, titles, matched = set(), list(dict.fromkeys(NEWS + EXTRA)), {}, Counter()
unmatched = list(range(len(items)))
for depth in range(2):
    nxt = set()
    for u in queue:
        if u.lower() in done:
            continue
        done.add(u.lower())
        try:
            title, posts, refs = channel_posts(u, 3 if depth == 0 else 2)
        except Exception:
            continue
        titles[u] = title
        nxt |= refs
        still = []
        for i in unmatched:
            sh = items[i]["sh"]
            if any(len(sh & p) / len(sh) >= 0.5 for p in posts):
                matched[u] += 1
            else:
                still.append(i)
        unmatched = still
        time.sleep(0.2)
    queue = sorted(nxt)[:250]
    print("ROUND %d checked %d, unmatched now %d" % (depth, len(done), len(unmatched)), flush=True)

for u, n in matched.most_common():
    print("SOURCE " + json.dumps({"u": u, "title": titles.get(u, ""), "matches": n, "ours": u in NEWS}, ensure_ascii=True))
print("RESULT " + json.dumps({"feed": len(items), "matched": len(items) - len(unmatched), "unmatched": len(unmatched)}))

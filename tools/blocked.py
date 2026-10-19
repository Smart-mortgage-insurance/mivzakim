"""Collects the posts the person filter blocked into blocked.html, for a manual check of the filter.
Runs on Actions (t.me is NetFree-blocked); the page goes out only as a short-lived artifact, never into the repo."""
import html, json, os, sys, time
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def fetch(post):
    ch, mid = post.split("/")
    r = requests.get("https://t.me/s/{}/{}".format(ch, mid), headers=UA, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    box = soup.find(attrs={"data-post": post})
    if not box:
        return None, None
    t = box.select_one(".tgme_widget_message_text")
    when = box.select_one("time")
    return (t.get_text("\n").strip() if t else ""), (when.get("datetime") if when else "")


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    blocked = json.load(open(os.path.join(ROOT, "data", "person_filter.json"), encoding="utf-8")).get("blocked", {})
    titles = {c["username"]: c.get("title") or c["username"]
              for c in json.load(open(os.path.join(ROOT, "scraper", "config.json"), encoding="utf-8"))["channels"]}
    posts = sorted(blocked, key=lambda p: -blocked[p])[:limit]
    cards, ok = [], 0
    for p in posts:
        text, ts = fetch(p)
        time.sleep(0.7)
        if text is None:
            continue
        ok += 1
        cards.append('<div class="c"><div class="h">{} &middot; {} &middot; {}</div><div class="t">{}</div></div>'.format(
            html.escape(titles.get(p.split("/")[0], p)), html.escape(p), html.escape((ts or "")[:16].replace("T", " ")),
            html.escape(text)))
    page = ('<!doctype html><html dir="rtl" lang="he"><meta charset="utf-8"><title>blocked</title><style>'
            'body{font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:16px;max-width:760px;margin:auto}'
            '.c{background:#fff;border-radius:10px;padding:12px 14px;margin:10px 0;box-shadow:0 1px 3px #0002}'
            '.h{color:#777;font-size:13px;margin-bottom:6px}.t{white-space:pre-wrap;line-height:1.6}</style>'
            '<h2>@N</h2>@C</html>').replace("@N", "{} / {}".format(ok, len(blocked))).replace("@C", "".join(cards))
    open(os.path.join(ROOT, "blocked.html"), "w", encoding="utf-8").write(page)
    print("found {} of {} requested ({} blocked total)".format(ok, len(posts), len(blocked)))


if __name__ == "__main__":
    main()

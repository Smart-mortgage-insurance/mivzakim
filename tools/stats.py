"""Collects the site's visit/view counters (abacus) into data/stats.json for the stats page.
Past days are kept from the previous run; only the last 2 days are re-read (they can still grow)."""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "stats.json")
PROMO = os.path.join(ROOT, "data", "sponsored.json")
BASE, NS = "https://abacus.jasoncameron.dev", "mivzakim-site"
START = date(2026, 10, 9)          # the day counting started
SERIES = ("u", "pv", "iv")         # unique visitors / page views / item views, per day


def get(key):
    time.sleep(0.4)                # abacus allows 30 requests per 10 seconds
    for attempt in range(3):
        try:
            r = requests.get("%s/get/%s/%s" % (BASE, NS, key), timeout=20)
            if r.status_code == 404:
                return 0
            if r.status_code == 429:
                time.sleep(10)
                continue
            r.raise_for_status()
            return int(r.json().get("value") or 0)
        except requests.RequestException:
            time.sleep(3)
    return None                    # unknown: keep the previous value


def main():
    try:
        old = json.load(open(OUT, encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    days = old.get("days", {})
    today = datetime.now(timezone(timedelta(hours=3))).date()   # Israel (close enough for a daily bucket)
    d = START
    while d <= today:
        k = d.strftime("%Y%m%d")
        if k not in days or (today - d).days <= 1:
            row = dict(days.get(k, {}))
            for s in SERIES:
                v = get("%s-%s" % (s, k))
                if v is not None:
                    row[s] = v
            days[k] = row
        d += timedelta(days=1)

    # total impressions of all ads (no names: the page is shown to other businesses)
    try:
        items = json.load(open(PROMO, encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        items = []
    bases = sorted({it["id"].rsplit("-side", 1)[0].rsplit("-feed", 1)[0] for it in items if it.get("id")})
    ads = dict(old.get("ads", {}))
    for b in bases:
        v = get("a-" + "".join(c if c.isalnum() or c in "_.-" else "_" for c in b)[:60])
        if v is not None:
            ads[b] = v

    out = {"start": START.isoformat(), "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
           "days": dict(sorted(days.items())), "ads": ads}
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("days:", len(days), "ads:", len(ads))


if __name__ == "__main__":
    main()

"""Checks candidate channel usernames on t.me/s: is it a public channel, how big, how fresh.
Prints numbers only (no post text)."""
import sys
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

CANDIDATES = sys.argv[1:] or """
holysale11 LametayelDigital hulmeudar AviationNewsIL isroteldeals SecretFlights
miluim_deals behatsdaa hist_org amorclubhotel
""".split()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}


def info(u):
    r = requests.get("https://t.me/s/" + u, headers=UA, timeout=25)
    soup = BeautifulSoup(r.text, "html.parser")
    msgs = soup.select(".tgme_widget_message")
    if not msgs:
        return "NO PUBLIC FEED"
    title = soup.select_one(".tgme_channel_info_header_title")
    subs = soup.select_one(".tgme_channel_info_counter .counter_value")
    times = [t["datetime"] for t in soup.select(".tgme_widget_message_date time[datetime]")]
    now = datetime.now(timezone.utc)
    ages = sorted((now - datetime.fromisoformat(t)).total_seconds() / 3600 for t in times)
    texts = [m.select_one(".tgme_widget_message_text") for m in msgs]
    lens = [len(t.get_text()) for t in texts if t]
    links = sum(1 for t in texts if t and t.select_one("a[href^=http]"))
    vids = sum(1 for m in msgs if m.select_one(".tgme_widget_message_video_player,.tgme_widget_message_roundvideo_player"))
    verified = "VERIFIED " if soup.select_one(".tgme_channel_info_header_labels .verified-icon,.tgme_header_title .verified-icon,i.verified-icon") else ""
    return verified + "videos={} ".format(vids) + "subs={} posts={} newest={:.0f}h oldest={:.0f}h with_text={} avg_len={} with_link={} photos={} | {}".format(
        subs.get_text() if subs else "?", len(msgs), ages[0] if ages else -1, ages[-1] if ages else -1,
        len(lens), sum(lens) // max(1, len(lens)), links,
        sum(1 for m in msgs if m.select_one(".tgme_widget_message_photo_wrap")),
        title.get_text(strip=True) if title else "?")


for u in CANDIDATES:
    try:
        print(u, "->", info(u))
    except Exception as exc:
        print(u, "-> ERROR", type(exc).__name__)

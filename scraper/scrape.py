# -*- coding: utf-8 -*-
"""
מוזיק – haredi music-news aggregator.

Pulls RSS feeds from haredi music sites, extracts title / summary / cover image /
source link / (free) download link, re-hosts cover images into the repo so they
load for NetFree users, runs images through an optional Gemini modesty filter,
and writes data/news.json for the site.

Copyright note: we only surface a DOWNLOAD button when the source itself offers a
free/official media file (an RSS <enclosure> or a direct audio/video link in the
post). We never download or re-host copyright-protected songs or clips for
redistribution — playback/downloads of protected media stay at the original site.

Runs on GitHub Actions. Optional GEMINI_API_KEY (repo secret) enables image
filtering; without it, cover images are kept as-is (haredi music covers are
typically modest graphics/male artists).
"""

import os
import re
import io
import json
import time
import base64
import hashlib
import mimetypes
from datetime import datetime, timezone
from html import unescape

import requests
import feedparser
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
NEWS_PATH = os.path.join(DATA_DIR, "news.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
os.makedirs(MEDIA_DIR, exist_ok=True)

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CFG = json.load(fh)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"})

AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg")
VIDEO_EXT = (".mp4", ".webm", ".mov")


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #
BOILERPLATE = [
    re.compile(r"\s*הפוסט\b.*?הופיע לראשונה ב.*$", re.S),
    re.compile(r"\s*The post\b.*?appeared first on .*$", re.I | re.S),
]


def html_to_text(html, limit):
    if not html:
        return ""
    txt = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    txt = unescape(re.sub(r"\s+", " ", txt)).strip()
    for rx in BOILERPLATE:            # strip "appeared first on <site>" source stamps
        txt = rx.sub("", txt).strip()
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt


IMG_TAIL = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# never treat these as a "download" (static assets)
ASSET_TAIL = (".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
              ".ico", ".woff", ".woff2", ".ttf")
DL_PATTERNS = [
    r'<source[^>]*src=["\']([^"\']+)',
    r'<audio[^>]*src=["\']([^"\']+)',
    r'href=["\']([^"\']+\.(?:mp3|m4a|mp4|wav))(?:["\'?])',
]
# reject ad/tracker URLs mistaken for downloads
BAD_DL_RE = re.compile(r'(?:[._/-]ads?[._/-]|/ad[_/]|advert|workers\.dev|doubleclick|googlesyndication|/track)', re.I)


def is_bad_dl(url):
    u = (url or "").strip()
    if not u.lower().startswith(("http", "//", "/")):
        return True
    base = u.lower().split("?")[0]
    if base.endswith(ASSET_TAIL) or base.endswith(IMG_TAIL):
        return True
    return bool(BAD_DL_RE.search(u))


def fetch_article(url):
    """Fetch the article once; return (og_image, download_url).

    download_url mirrors whatever the SOURCE itself offers (an <audio>/<source>,
    a direct media file, or a /download link). We link to it — never re-host it.
    """
    if not url:
        return "", None
    og, dl = "", None
    try:
        r = SESSION.get(url, timeout=25)
        if r.status_code != 200:
            return "", None
        h = r.text
        m = (re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)', h, re.I)
             or re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image', h, re.I))
        if m:
            og = m.group(1)
        for pat in DL_PATTERNS:
            for cand in re.findall(pat, h, re.I):
                if not is_bad_dl(cand):
                    dl = cand
                    break
            if dl:
                break
    except Exception:
        pass
    return og, dl


def upgrade_blogger_img(url):
    # blogger thumbnails come as /s72-c/ etc.; bump to a large size
    return re.sub(r"/s\d+(-c)?/", "/s1600/", url)


def pick_image(entry):
    # media:thumbnail / media:content
    for key in ("media_thumbnail", "media_content"):
        arr = entry.get(key)
        if arr:
            for m in arr:
                u = m.get("url")
                if u and not u.lower().endswith(VIDEO_EXT):
                    return upgrade_blogger_img(u)
    # enclosure image
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("image/") and enc.get("href"):
            return enc["href"]
    # first <img> in content/summary
    html = ""
    if entry.get("content"):
        html = entry["content"][0].get("value", "")
    html = html or entry.get("summary", "")
    m = re.search(r'<img[^>]*src=[\'"]([^\'"]+)', html)
    if m:
        return m.group(1)
    return ""


def pick_download(entry):
    """Only a source-offered free media file (enclosure or direct link)."""
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href", "")
        typ = (enc.get("type") or "").lower()
        if (typ.startswith("audio/") or typ.startswith("video/")
                or href.lower().endswith(AUDIO_EXT + VIDEO_EXT)) and not is_bad_dl(href):
            return href
    html = ""
    if entry.get("content"):
        html = entry["content"][0].get("value", "")
    html = html or entry.get("summary", "")
    for cand in re.findall(r'href=[\'"]([^\'"]+\.(?:mp3|m4a|mp4)[^\'"]*)', html, re.I):
        if not is_bad_dl(cand):
            return cand
    return None


def entry_ts(entry):
    for k in ("published_parsed", "updated_parsed"):
        t = entry.get(k)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return None


# --------------------------------------------------------------------------- #
# Media download + Gemini modesty filter
# --------------------------------------------------------------------------- #
EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
}


def download(url, max_bytes):
    try:
        r = SESSION.get(url, timeout=45, stream=True)
        if r.status_code != 200:
            return None, None
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        buf = io.BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > max_bytes:
                return None, None
        return buf.getvalue(), ctype
    except Exception as exc:
        log("    ! download failed:", exc)
        return None, None


def gemini_image_ok(img_bytes, mime):
    if not GEMINI_KEY:
        return None
    model = CFG["media_filter"].get("gemini_model", "gemini-2.0-flash")
    endpoint = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{}:generateContent?key={}".format(model, GEMINI_KEY))
    prompt = (
        "אתה מסנן תמונות לאתר מוזיקה חרדי לגולשי אינטרנט מסונן (נטפרי). "
        "החזר JSON בלבד {\"ok\": true/false}. ok=false אם מופיעים נשים/נערות בצורה בולטת, "
        "לבוש לא צנוע, או תוכן לא ראוי לציבור חרדי. ok=true עבור עטיפות אלבום, גרפיקת טקסט, "
        "כלי נגינה, גברים/זמרים, נופים, לוגואים. בספק – false."
    )
    body = {"contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": mime or "image/jpeg",
                         "data": base64.b64encode(img_bytes).decode()}}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 40}}
    try:
        r = requests.post(endpoint, json=body, timeout=45)
        if r.status_code != 200:
            return False
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r'"ok"\s*:\s*(true|false)', txt, re.I)
        return m.group(1).lower() == "true" if m else False
    except Exception:
        return False


def process_image(item_id, url):
    """Download, filter, and re-host a cover image. Returns local path or ""."""
    mf = CFG["media_filter"]
    data, ctype = download(url, CFG["limits"]["max_media_bytes"])
    if not data or not (ctype or "").startswith("image/"):
        return ""
    if mf.get("use_gemini", True):
        ok = gemini_image_ok(data, ctype)
        if ok is False:
            log("    - cover rejected by filter")
            return ""
        # ok is None (no key) -> keep_image fallback
    ext = EXT_BY_MIME.get(ctype) or mimetypes.guess_extension(ctype or "") or ".jpg"
    fname = hashlib.sha1((item_id + url).encode()).hexdigest()[:16] + ext
    fpath = os.path.join(MEDIA_DIR, fname)
    if not os.path.exists(fpath):
        with open(fpath, "wb") as out:
            out.write(data)
    return "data/media/{}".format(fname)


# --------------------------------------------------------------------------- #
# Persist
# --------------------------------------------------------------------------- #
def load_news():
    if os.path.exists(NEWS_PATH):
        try:
            with open(NEWS_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"items": []}


def prune_media(items):
    referenced = {os.path.basename(it["image"]) for it in items if it.get("image")}
    for fn in os.listdir(MEDIA_DIR):
        if fn == ".gitkeep" or fn in referenced:
            continue
        try:
            os.remove(os.path.join(MEDIA_DIR, fn))
        except OSError:
            pass


AUDIO_WORDS = ("סינגל", "האזינו", "האזנה", "מאזינים", "שיר חדש", "אלבום", "מחרוזת",
               "ניגון", "ניגונים", "דיסק", "להורדה", "לשיר")
CLIP_WORDS = ("קליפ", "וידאו", "צפו", "צפייה", "clip", "video", "הופעה")


def classify(title, summary, download):
    t = (title or "") + " " + (summary or "")
    if any(w in t for w in CLIP_WORDS):
        return "clip"
    if download and download.lower().split("?")[0].endswith(AUDIO_EXT):
        return "audio"
    if any(w in t for w in AUDIO_WORDS):
        return "audio"
    return "news"


def reconcile_media(items):
    """Self-heal: if an item points to an image file that isn't on disk,
    re-download it (using the stored remote URL). Guarantees news.json never
    references a missing file. Fixes covers vanishing across runs."""
    healed = 0
    for it in items:
        img = it.get("image")
        if not img:
            continue
        if os.path.exists(os.path.join(ROOT, img)):
            continue
        src = it.get("image_src")
        if not src:
            og, _dl = fetch_article(it.get("link", ""))
            src = og
            it["image_src"] = src
        it["image"] = process_image(it["id"], src) if src else ""
        if it["image"]:
            healed += 1
    if healed:
        log("reconcile: re-downloaded {} missing covers".format(healed))


def main():
    log("== מוזיק scraper ==")
    log("Gemini image filter:", "ON" if GEMINI_KEY else "OFF (keep images)")

    news = load_news()
    existing = {it["id"]: it for it in news.get("items", [])}
    tcfg = CFG["text"]
    new_count = 0

    # repair existing items: drop bogus (static-asset) download links, backfill type
    for it in existing.values():
        d = (it.get("download") or "").lower().split("?")[0]
        if d.endswith(ASSET_TAIL):
            it["download"] = None
        if "type" not in it:
            it["type"] = classify(it.get("title"), it.get("text"), it.get("download"))

    for src in CFG["sources"]:
        if not src.get("enabled", True):
            continue
        name, feed = src["name"], src["feed"]
        log("-> {}".format(name))
        try:
            parsed = feedparser.parse(feed, agent=UA)
        except Exception as exc:
            log("   ! feed error:", exc)
            continue
        entries = parsed.entries[: CFG["limits"]["max_per_source"]]
        log("   {} entries".format(len(entries)))

        for e in entries:
            link = e.get("link") or ""
            title = html_to_text(e.get("title", ""), 200)
            if len(title) < tcfg.get("min_title", 4):
                continue
            hid = "s" + hashlib.sha1((link or title).encode()).hexdigest()[:14]
            if hid in existing:
                continue

            summary = html_to_text(
                (e.get("content") and e["content"][0].get("value")) or e.get("summary", ""),
                tcfg.get("summary_chars", 320))
            dl_on = CFG.get("downloads", {}).get("enabled")
            feed_img = pick_image(e)
            feed_dl = pick_download(e) if dl_on else None
            og_img, art_dl = ("", None)
            if (not feed_img) or (dl_on and not feed_dl):
                og_img, art_dl = fetch_article(link)
            img_url = feed_img or og_img
            local_img = process_image(hid, img_url) if img_url else ""
            download_url = feed_dl or art_dl

            existing[hid] = {
                "id": hid,
                "title": title,
                "text": summary,
                "image": local_img,
                "image_src": img_url,
                "type": classify(title, summary, download_url),
                "source": name,
                "link": link,
                "download": download_url,
                "ts": entry_ts(e),
            }
            new_count += 1
        time.sleep(1)

    items = list(existing.values())
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    items = items[: CFG["limits"]["max_items"]]
    reconcile_media(items)
    prune_media(items)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "brand": CFG["brand"],
        "sources": [{"name": s["name"], "site": s.get("site", "")}
                    for s in CFG["sources"] if s.get("enabled", True)],
        "count": len(items),
        "items": items,
    }
    with open(NEWS_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    log("== done: {} new, {} total ==".format(new_count, len(items)))


if __name__ == "__main__":
    main()

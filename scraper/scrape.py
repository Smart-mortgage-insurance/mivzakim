# -*- coding: utf-8 -*-
"""
מבזקים – Telegram → news.json scraper with content filtering for a NetFree audience.

Runs on GitHub Actions (unfiltered egress). NetFree blocks Telegram, so this MUST
run off a filtered connection. It:

  1. Fetches the public web preview of each channel (https://t.me/s/<username>).
  2. Parses messages: text, timestamp, permalink, photos, videos.
  3. Filters text (strips links/@handles, drops promo/spam, dedupes).
  4. Downloads approved media INTO the repo (data/media/) so it loads for
     NetFree users (Telegram's own CDN is blocked by NetFree).
  5. Runs every image through Gemini vision; anything not kosher / not
     NetFree-safe is dropped.
  6. Merges into data/news.json (newest first, capped) and prunes old media.

No API keys are required except an optional GEMINI_API_KEY (repo secret) for the
image filter. Without it, the system falls back to text-only for safety.
"""

import os
import re
import io
import sys
import json
import time
import base64
import hashlib
import mimetypes
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Paths & config
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

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# Telegram parsing
# --------------------------------------------------------------------------- #
BG_URL_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)", re.I)


def fetch_channel(username):
    """Return the parsed list of messages (dicts) for one channel."""
    url = "https://t.me/s/{}".format(username)
    try:
        resp = SESSION.get(url, timeout=30)
    except Exception as exc:
        log("  ! network error for @{}: {}".format(username, exc))
        return []
    if resp.status_code != 200:
        log("  ! @{} returned HTTP {} (blocked/unavailable)".format(username, resp.status_code))
        return []
    return parse_messages(resp.text)


def parse_messages(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for wrap in soup.select("div.tgme_widget_message_wrap"):
        msg = wrap.select_one("div.tgme_widget_message")
        if not msg:
            continue
        post_id = msg.get("data-post")  # e.g. "channelkikar/1234"
        if not post_id:
            continue

        # text (preserve line breaks)
        text = ""
        tnode = msg.select_one("div.tgme_widget_message_text")
        if tnode:
            for br in tnode.find_all("br"):
                br.replace_with("\n")
            text = tnode.get_text().strip()

        # timestamp
        ts = None
        tm = msg.select_one("a.tgme_widget_message_date time")
        if tm and tm.get("datetime"):
            ts = tm["datetime"]

        # media: photos (incl. albums) + link-preview image
        media = []
        for ph in msg.select("a.tgme_widget_message_photo_wrap, "
                             "a.tgme_widget_message_link_preview_image, "
                             "i.link_preview_image"):
            style = ph.get("style", "")
            m = BG_URL_RE.search(style)
            if m:
                media.append({"type": "image", "src": m.group(1)})

        # video / gif — grab direct src if present, else the poster thumbnail
        for vid in msg.select("video.tgme_widget_message_video"):
            src = vid.get("src")
            poster = vid.get("poster")
            if src:
                media.append({"type": "video", "src": src, "poster": poster})
            elif poster:
                media.append({"type": "image", "src": poster})
        for vthumb in msg.select("i.tgme_widget_message_video_thumb"):
            m = BG_URL_RE.search(vthumb.get("style", ""))
            if m and not any(x.get("type") == "video" for x in media):
                media.append({"type": "image", "src": m.group(1)})

        # skip service / empty messages
        if not text and not media:
            continue

        out.append({
            "id": post_id,   # raw id, used only internally for dedupe (never emitted)
            "text": text,
            "ts": ts,
            "media": media,
        })
    return out


# --------------------------------------------------------------------------- #
# Text filtering
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.I)
HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}")
MULTISPACE_RE = re.compile(r"[ \t]{2,}")
MULTILINE_RE = re.compile(r"\n{3,}")


LEAD_PUNCT_RE = re.compile(r"^[\s\-–—|:•·.,]+")
TRAIL_PUNCT_RE = re.compile(r"[\s\-–—|:•·]+$")


def clean_text(text):
    tf = CFG["text_filter"]
    if tf.get("strip_urls", True):
        text = URL_RE.sub("", text)
    if tf.get("strip_handles", True):
        text = HANDLE_RE.sub("", text)
    # remove source signatures / channel branding (longest first)
    for phrase in sorted(tf.get("strip_phrases", []), key=len, reverse=True):
        if phrase:
            text = text.replace(phrase, "")
    text = MULTISPACE_RE.sub(" ", text)
    text = MULTILINE_RE.sub("\n\n", text)
    # trim per-line, drop lines left empty or as bare punctuation after stripping
    out_lines = []
    for ln in text.split("\n"):
        ln = TRAIL_PUNCT_RE.sub("", LEAD_PUNCT_RE.sub("", ln.strip())).strip()
        out_lines.append(ln)
    text = "\n".join(out_lines)
    text = MULTILINE_RE.sub("\n\n", text).strip()
    return text


def text_allowed(text):
    tf = CFG["text_filter"]
    low = text.lower()
    for bad in tf.get("drop_if_contains", []):
        if bad.lower() in low:
            return False
    return True


# --------------------------------------------------------------------------- #
# Media download + Gemini vision filter
# --------------------------------------------------------------------------- #
EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm",
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
                return None, None  # too big
        return buf.getvalue(), ctype
    except Exception as exc:
        log("    ! download failed: {}".format(exc))
        return None, None


def gemini_image_ok(img_bytes, mime):
    """Ask Gemini whether the image is appropriate for a haredi/NetFree audience."""
    if not GEMINI_KEY:
        return None  # unknown
    model = CFG["media_filter"].get("gemini_model", "gemini-2.0-flash")
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{}:generateContent?key={}".format(model, GEMINI_KEY)
    )
    prompt = (
        "אתה מסנן תוכן לאתר חדשות חרדי המיועד לגולשים עם אינטרנט מסונן (נטפרי). "
        "בדוק את התמונה והחזר JSON בלבד בפורמט {\"ok\": true/false}. "
        "החזר ok=false אם מופיעים: נשים או נערות בצורה בולטת, לבוש לא צנוע, "
        "תוכן פרובוקטיבי/אלים/דוחה, פרסומות לא צנועות, או כל דבר לא ראוי לציבור חרדי. "
        "החזר ok=true עבור: גברים, רבנים, נופים, בניינים, מסמכים, מפות, גרפיקת טקסט, "
        "חפצים, רכבים, אירועים גבריים. בספק – החזר false."
    )
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime or "image/jpeg",
                                 "data": base64.b64encode(img_bytes).decode()}},
            ]
        }],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 40},
    }
    try:
        r = requests.post(endpoint, json=body, timeout=45)
        if r.status_code != 200:
            log("    ! gemini HTTP {}: {}".format(r.status_code, r.text[:160]))
            return False  # fail safe -> reject
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r'"ok"\s*:\s*(true|false)', txt, re.I)
        if m:
            return m.group(1).lower() == "true"
        return False
    except Exception as exc:
        log("    ! gemini error: {}".format(exc))
        return False  # fail safe


def process_media(item):
    """Download + filter media; rewrite each entry to a local repo path or drop it."""
    mf = CFG["media_filter"]
    max_bytes = CFG["limits"]["max_media_bytes"]
    kept = []

    # No key and policy is text_only -> strip all media
    if not GEMINI_KEY and mf.get("fallback_without_key") == "text_only":
        item["media"] = []
        return item

    for m in item.get("media", []):
        mtype = m["type"]
        if mtype == "image" and not mf.get("allow_images", True):
            continue
        if mtype == "video" and not mf.get("allow_videos", True):
            continue

        data, ctype = download(m["src"], max_bytes)
        if not data:
            continue

        # decide on the visual: for video we judge its poster frame if available
        judge_bytes, judge_mime = data, ctype
        if mtype == "video" and m.get("poster"):
            p_data, p_ctype = download(m["poster"], max_bytes)
            if p_data:
                judge_bytes, judge_mime = p_data, p_ctype

        if mf.get("use_gemini", True):
            ok = gemini_image_ok(judge_bytes if mtype == "image" else judge_bytes,
                                 judge_mime)
            if ok is False:
                log("    - media rejected by filter")
                if mf.get("on_reject") == "drop_item":
                    return None
                continue  # drop_media
            if ok is None and not GEMINI_KEY:
                continue  # unknown + no key -> skip media to stay safe

        # save into repo
        ext = EXT_BY_MIME.get(ctype) or mimetypes.guess_extension(ctype or "") or (
            ".mp4" if mtype == "video" else ".jpg")
        fname = hashlib.sha1((item["id"] + m["src"]).encode()).hexdigest()[:16] + ext
        fpath = os.path.join(MEDIA_DIR, fname)
        if not os.path.exists(fpath):
            with open(fpath, "wb") as out:
                out.write(data)

        entry = {"type": mtype, "file": "data/media/{}".format(fname)}
        # keep a poster image for videos too
        if mtype == "video" and m.get("poster"):
            pd, pc = download(m["poster"], max_bytes)
            if pd:
                pext = EXT_BY_MIME.get(pc, ".jpg")
                pfname = hashlib.sha1((item["id"] + m["poster"]).encode()).hexdigest()[:16] + pext
                with open(os.path.join(MEDIA_DIR, pfname), "wb") as out:
                    out.write(pd)
                entry["poster"] = "data/media/{}".format(pfname)
        kept.append(entry)

    item["media"] = kept
    return item


# --------------------------------------------------------------------------- #
# Merge + persist
# --------------------------------------------------------------------------- #
def load_news():
    if os.path.exists(NEWS_PATH):
        try:
            with open(NEWS_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"updated": None, "brand": CFG["brand"], "items": []}


def prune_media(items):
    """Delete media files no longer referenced by any item."""
    referenced = set()
    for it in items:
        for m in it.get("media", []):
            for key in ("file", "poster"):
                if m.get(key):
                    referenced.add(os.path.basename(m[key]))
    for fn in os.listdir(MEDIA_DIR):
        if fn == ".gitkeep":
            continue
        if fn not in referenced:
            try:
                os.remove(os.path.join(MEDIA_DIR, fn))
            except OSError:
                pass


def main():
    log("== מבזקים scraper ==")
    log("Gemini image filter:", "ON" if GEMINI_KEY else "OFF (text-only fallback)")

    news = load_news()
    # retroactively re-clean already-published items so filter changes apply to them too
    existing = {}
    recleaned = 0
    for it in news.get("items", []):
        original = it.get("text", "")
        it["text"] = clean_text(original)
        if it["text"] != original:
            recleaned += 1
        if not it["text"] and not it.get("media"):
            continue  # nothing left after cleaning -> drop
        if it["text"] and not text_allowed(it["text"]):
            continue
        existing[it["id"]] = it
    if recleaned:
        log("re-cleaned {} existing items".format(recleaned))
    new_count = 0

    sources = CFG.get("sources", CFG.get("channels", []))
    for ch in sources:
        if not ch.get("enabled", True):
            continue
        uname = ch["username"]
        log("-> source #{}".format(hashlib.sha1(uname.encode()).hexdigest()[:6]))
        msgs = fetch_channel(uname)
        log("   {} messages".format(len(msgs)))
        msgs = msgs[-CFG["limits"]["max_messages_per_channel"]:]

        for msg in msgs:
            # hashed id: dedupe key that never reveals the source in the public feed
            hid = "m" + hashlib.sha1(msg["id"].encode()).hexdigest()[:14]
            if hid in existing:
                continue  # already have it
            text = clean_text(msg["text"])
            if len(text) < CFG["text_filter"]["min_length"] and not msg["media"]:
                continue
            if text and not text_allowed(text):
                log("   - dropped (promo/spam)")
                continue

            msg["text"] = text
            processed = process_media(msg)
            if processed is None:
                continue
            if not processed["text"] and not processed["media"]:
                continue

            existing[hid] = {
                "id": hid,
                "text": text,
                "ts": msg["ts"],
                "media": processed["media"],
            }
            new_count += 1
        time.sleep(1)  # be polite between sources

    # sort newest first, cap
    items = list(existing.values())
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    items = items[: CFG["limits"]["max_items"]]

    prune_media(items)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "brand": CFG["brand"],
        "count": len(items),
        "items": items,
    }
    with open(NEWS_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    log("== done: {} new, {} total ==".format(new_count, len(items)))


if __name__ == "__main__":
    main()

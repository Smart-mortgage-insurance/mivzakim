# -*- coding: utf-8 -*-
"""
נטגרם – mirrors public Telegram channels into a NetFree-reachable feed.

Transparent aggregator: every post is shown AS-IS, attributed to its source
channel, with a link to the original. We do not edit, rewrite or re-word posts
(that is what keeps the site a quoting platform rather than a publisher).

Runs on GitHub Actions (unfiltered egress) because NetFree blocks t.me and the
Telegram CDN. Media is downloaded into the repo (data/media/) so it loads for
NetFree users; every image passes a Gemini modesty filter first.
"""

import os
import re
import copy
import io
import json
import time
import base64
import hashlib
import mimetypes
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

from shabbat import is_holy, now_is_holy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
NEWS_PATH = os.path.join(DATA_DIR, "news.json")
REMOVED_PATH = os.path.join(DATA_DIR, "removed.json")
PF_PATH = os.path.join(DATA_DIR, "person_filter.json")
CONFIG_PATH = os.path.join(HERE, "config.json")

os.makedirs(MEDIA_DIR, exist_ok=True)

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CFG = json.load(fh)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# without a Gemini key, keep images anyway (NetFree filters images on the user's side)
UNFILTERED_OK = CFG["media_filter"].get("allow_without_gemini", False)
# Photos/videos in the channels mostly belong to press photographers and agencies.
# Re-hosting them needs a licence, so post media is off unless explicitly enabled.
MEDIA_ON = CFG["media_filter"].get("enabled", True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
})


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# Telegram parsing
# --------------------------------------------------------------------------- #
BG_URL_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)", re.I)


def parse_views(s):
    s = (s or "").strip().upper().replace(",", "")
    try:
        if s.endswith("K"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("M"):
            return int(float(s[:-1]) * 1000000)
        return int(s)
    except ValueError:
        return None


def fetch_channel(username):
    """Return (channel_info, messages) for one public channel."""
    try:
        resp = SESSION.get("https://t.me/s/{}".format(username), timeout=30)
    except Exception as exc:
        log("  ! network error for @{}: {}".format(username, exc))
        return None, []
    if resp.status_code != 200:
        log("  ! @{} HTTP {}".format(username, resp.status_code))
        return None, []
    soup = BeautifulSoup(resp.text, "html.parser")

    info = {"username": username, "title": username, "avatar_src": None}
    t = soup.select_one(".tgme_channel_info_header_title")
    if t:
        info["title"] = t.get_text(" ", strip=True)
    av = soup.select_one(".tgme_page_photo_image img, .tgme_channel_info_header img")
    if av and av.get("src"):
        info["avatar_src"] = av["src"]
    return info, parse_messages(soup)


def parse_messages(soup):
    out = []
    for msg in soup.select("div.tgme_widget_message"):
        post_id = msg.get("data-post")  # "channel/1234"
        if not post_id:
            continue

        text = rich = ""
        tnode = msg.select_one("div.tgme_widget_message_text")
        if tnode:
            for br in tnode.find_all("br"):
                br.replace_with("\n")
            text = tnode.get_text().strip()
            # same text with link targets kept visible (used by job/deal boards)
            rnode = copy.copy(tnode)
            for a in rnode.find_all("a"):
                href, label = a.get("href", ""), a.get_text().strip()
                if not href.startswith("http") or "//t.me/" in href or "telegram.me/" in href:
                    a.replace_with("" if label.startswith(("http", "@", "t.me")) else label)
                elif label.startswith("http") or label.rstrip(".\u2026") in href:
                    a.replace_with(" {} ".format(href))
                else:
                    a.replace_with("{} {} ".format(label, href))
            rich = rnode.get_text().strip()

        ts = None
        tm = msg.select_one("a.tgme_widget_message_date time")
        if tm and tm.get("datetime"):
            ts = tm["datetime"]

        views = None
        vn = msg.select_one(".tgme_widget_message_views")
        if vn:
            views = parse_views(vn.get_text())

        fwd = None
        fn = msg.select_one(".tgme_widget_message_forwarded_from_name")
        if fn:
            fwd = fn.get_text(" ", strip=True)

        media = []
        for ph in msg.select("a.tgme_widget_message_photo_wrap, "
                             "a.tgme_widget_message_link_preview_image, "
                             "i.link_preview_image"):
            m = BG_URL_RE.search(ph.get("style", ""))
            if m:
                media.append({"type": "image", "src": m.group(1)})
        for vid in msg.select("video.tgme_widget_message_video"):
            if vid.get("src"):
                media.append({"type": "video", "src": vid["src"], "poster": vid.get("poster")})
            elif vid.get("poster"):
                media.append({"type": "image", "src": vid["poster"], "from_video": True})
        for vthumb in msg.select("i.tgme_widget_message_video_thumb"):
            m = BG_URL_RE.search(vthumb.get("style", ""))
            if m and not any(x["type"] == "video" for x in media):
                media.append({"type": "image", "src": m.group(1), "from_video": True})

        if not text and not media:
            continue
        out.append({"post": post_id, "text": text, "ts": ts, "views": views,
                    "forwarded_from": fwd, "media": media, "rich": rich})
    return out


# --------------------------------------------------------------------------- #
# Filtering (spam/ads only — the post itself is never rewritten)
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.I)
DROP_REGEX = [re.compile(p) for p in CFG["text_filter"].get("drop_regex", [])]


HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}")
FOOTER_RE = [re.compile(p) for p in CFG["text_filter"].get("strip_line_regex", [])]
# per-line leftovers: "| source >>", arrows pointing at a removed link, bare domains
TRAIL_RE = [re.compile(p) for p in CFG["text_filter"].get("strip_regex", [])]
# camera / "see above" emojis pointing at media we don't show: swapped for a space
SPACE_RE = [re.compile(p) for p in CFG["text_filter"].get("replace_with_space", [])]


def clean_text(text):
    # removed: raw links (Telegram is blocked in NetFree) and each channel's own
    # "join us / follow us" footer. The news text itself is never changed.
    for marker in CFG["text_filter"].get("cut_from", []):
        pos = text.find(marker)
        if pos != -1:
            text = text[:pos]
    text = URL_RE.sub("", text)
    text = HANDLE_RE.sub("", text)
    for rx in SPACE_RE:
        text = rx.sub(" ", text)
    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        for _ in range(3):  # repeat: stripping one tail can expose another
            for rx in TRAIL_RE:
                ln = rx.sub("", ln).strip()
        if not any(rx.search(ln) for rx in FOOTER_RE):
            lines.append(ln)
    text = "\n".join(lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_board(text):
    # job/deal boards: links are the whole point, so only Telegram-internal ones go
    text = re.sub(r"(https?://)?(t|telegram)\.me/\S+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def allowed(kind, text):
    if kind:  # boards: the ad is the content, only empty fragments are dropped
        if kind == "deals" and CFG["text_filter"].get("deals_drop_regex") and \
                re.search(CFG["text_filter"]["deals_drop_regex"], text, re.I):
            return False  # deals that don't suit the audience (food that isn't kosher, immodest clothing, gambling)
        return len(re.sub(r"[\W\d_]", "", text)) >= CFG["text_filter"].get("min_letters", 12)
    return text_allowed(text)


def text_allowed(text):
    if len(re.sub(r"\W", "", text)) < CFG["text_filter"].get("min_letters", 12):
        return False  # "#", "שר החוץ" and similar fragments
    low = text.lower()
    if any(b.lower() in low for b in CFG["text_filter"].get("drop_if_contains", [])):
        return False
    return not any(rx.search(text) for rx in DROP_REGEX)


# --------------------------------------------------------------------------- #
# Media download + Gemini modesty filter
# --------------------------------------------------------------------------- #
EXT_BY_MIME = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
               "image/webp": ".webp", "image/gif": ".gif",
               "video/mp4": ".mp4", "video/webm": ".webm"}


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
        log("    ! download failed: {}".format(exc))
        return None, None


def gemini_image_ok(img_bytes, mime):
    model = CFG["media_filter"].get("gemini_model", "gemini-3.5-flash")
    endpoint = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{}:generateContent?key={}".format(model, GEMINI_KEY))
    prompt = (
        "אתה מסנן תמונות לאתר המיועד לציבור חרדי עם אינטרנט מסונן (נטפרי). "
        "החזר JSON בלבד: {\"ok\": true/false}. "
        "ok=false אם מופיעות נשים או נערות, לבוש לא צנוע, תוכן אלים/מזעזע/דוחה, "
        "או כל דבר לא ראוי לציבור חרדי. ok=true לגברים, רבנים, נופים, מבנים, מסמכים, "
        "מפות, גרפיקת טקסט, חפצים ורכבים. בספק – false."
    )
    body = {"contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime or "image/jpeg",
                                 "data": base64.b64encode(img_bytes).decode()}}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 40,
                                 "responseMimeType": "application/json"}}
    try:
        r = requests.post(endpoint, json=body, timeout=45)
        if r.status_code != 200:
            log("    ! gemini HTTP {}: {}".format(r.status_code, r.text[:160]))
            return False
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r'"ok"\s*:\s*(true|false)', txt, re.I)
        return bool(m) and m.group(1).lower() == "true"
    except Exception as exc:
        log("    ! gemini error: {}".format(exc))
        return False  # fail safe


def save_media(key, data, ctype, default_ext):
    ext = EXT_BY_MIME.get(ctype) or mimetypes.guess_extension(ctype or "") or default_ext
    fname = hashlib.sha1(key.encode()).hexdigest()[:16] + ext
    fpath = os.path.join(MEDIA_DIR, fname)
    if not os.path.exists(fpath):
        with open(fpath, "wb") as out:
            out.write(data)
    return "data/media/" + fname


def process_media(post, media):
    """Download + filter; returns local media entries (images/videos that passed)."""
    if not MEDIA_ON or (not GEMINI_KEY and not UNFILTERED_OK):
        return []  # media switched off, or no filter available -> text only
    max_bytes = CFG["limits"]["max_media_bytes"]
    kept = []
    for m in media:
        if m["type"] == "video" and not CFG["media_filter"].get("allow_videos", True):
            continue
        data, ctype = download(m["src"], max_bytes)
        if not data:
            continue
        judge, jmime, pdata, pctype = data, ctype, None, None
        if m["type"] == "video":
            if not m.get("poster"):
                continue  # can't judge a video with no frame
            pdata, pctype = download(m["poster"], max_bytes)
            if not pdata:
                continue
            judge, jmime = pdata, pctype
        if GEMINI_KEY and not gemini_image_ok(judge, jmime):
            log("    - media rejected by filter")
            continue
        entry = {"type": m["type"],
                 "file": save_media(post + m["src"], data, ctype,
                                    ".mp4" if m["type"] == "video" else ".jpg")}
        if pdata:
            entry["poster"] = save_media(post + m["poster"], pdata, pctype, ".jpg")
        kept.append(entry)
    return kept


def missing_media(orig, shown):
    """What the original post had that we don't show (so a caption isn't left hanging)."""
    if shown or not orig:
        return None
    return "video" if any(m["type"] == "video" or m.get("from_video") for m in orig) else "image"


def process_avatar(info):
    """Channel logos are shown for attribution; still pass the modesty filter."""
    src = info.pop("avatar_src", None)
    if not src or not (GEMINI_KEY or UNFILTERED_OK):
        return None
    data, ctype = download(src, 2000000)
    if not data or (GEMINI_KEY and not gemini_image_ok(data, ctype)):
        return None
    return save_media("avatar:" + info["username"], data, ctype, ".jpg")


# --------------------------------------------------------------------------- #
# Person filter: posts that speak badly about specific people never go up
# --------------------------------------------------------------------------- #
PERSON_PROMPT = (
    "You review Hebrew Telegram news posts for a Haredi news site that avoids lashon hara and "
    "defamation of individuals - but it is a NEWS site, so ordinary news must be published.\n"
    "For each numbered post set block=true ONLY if the post itself attributes a crime, a scandal, "
    "a moral failing, an embarrassing personal matter, or an insult or mockery to a SPECIFIC, "
    "IDENTIFIABLE person (named, or recognizable from the details), or spreads gossip or rumors "
    "about a person.\n"
    "Set block=false for everything else, including:\n"
    "- security, military, terror and war reports, including about terrorists, enemy leaders and enemy states;\n"
    "- accidents, fires, weather, health, traffic, economy and consumer news;\n"
    "- arrests, investigations and court cases where the suspect is not named or identifiable;\n"
    "- statements, decisions, votes and quotes of politicians, ministers, rabbis, officials and public "
    "bodies, and plain reports of political disagreements between them;\n"
    "- criticism of policies, laws, organizations, parties or governments (not of a private person);\n"
    "- deaths, funerals, condolences, praise, events, Torah and community news.\n"
    "When unsure, block=false.\n"
)

# Hagizra-style headline: 2-6 bold words shown before the post text, followed by " |"
HEADLINE_PROMPT = (
    "\n\nבנוסף, לכל הודעה כתוב בשדה t כותרת קצרה בסגנון אתרי מבזקים חרדיים: "
    "2 עד 6 מילים בעברית, עניינית ומדויקת לפי תוכן ההודעה בלבד, בלי סימן פיסוק בסוף, "
    "בלי אימוג'י, בלי מירכאות ובלי שם הערוץ. במודעת דרושים - התפקיד (ואם כתוב, המקום). "
    "במבצע - המוצר והמחיר אם מופיע. אם אין להודעה תוכן ברור - t ריק.\n\n"
)
RESULT_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "i": {"type": "INTEGER"}, "block": {"type": "BOOLEAN"}, "t": {"type": "STRING"}},
    "required": ["i", "block", "t"]}}


def gemini_person_check(texts):
    """Returns a list of booleans (True = block) for the given posts, or None on any failure."""
    pf = CFG.get("person_filter", {})
    endpoint = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{}:generateContent?key={}".format(pf.get("gemini_model", "gemini-3.5-flash-lite"), GEMINI_KEY))
    listing = "\n\n".join("מבזק {}:\n{}".format(n + 1, t[:1500]) for n, t in enumerate(texts))
    body = {"contents": [{"parts": [{"text": PERSON_PROMPT + HEADLINE_PROMPT + listing}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseSchema": RESULT_SCHEMA}}
    try:
        r = requests.post(endpoint, json=body, timeout=90)
        if r.status_code != 200:
            log("   ! person filter HTTP {}: {}".format(r.status_code, r.text[:200].replace(GEMINI_KEY, "***")))
            return None
        rows = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        verdict = {int(row["i"]): (bool(row["block"]), clean_title(row.get("t", ""))) for row in rows}
        if set(verdict) != set(range(1, len(texts) + 1)):
            log("   ! person filter: answer does not cover every post")
            return None
        return [verdict[n + 1] for n in range(len(texts))]
    except Exception as exc:
        log("   ! person filter error: {}".format(str(exc).replace(GEMINI_KEY, "***")[:200]))
        return None


def clean_title(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip("|:.-– \"'״")
    words = t.split()
    return " ".join(words[:8]) if len(words) >= 2 else ""


def person_filter(existing, blocked, state):
    """Checks every unchecked post. New posts that could not be checked are held back
    (not published) and tried again on a later cycle; blocked ones are remembered."""
    pf = CFG.get("person_filter", {})
    pending = [it for it in existing.values()
               if it.get("text") and (not it.get("checked") or "title" not in it)]
    if not pf.get("enabled") or not pending:
        return
    if not GEMINI_KEY:
        log("person filter: OFF - no GEMINI_API_KEY, posts are published unchecked")
        return

    def hold_new(items):
        for it in items:
            if it.get("_new"):
                existing.pop(it["post"], None)

    now = time.time()
    if now - state.get("pf_last", 0) < pf.get("min_interval_sec", 180):
        hold_new(pending)  # stay inside the free quota: wait for the next window
        return
    state["pf_last"] = now
    size = pf.get("batch", 20)
    n_blocked = n_checked = 0
    for start in range(0, len(pending), size):
        batch = pending[start:start + size]
        verdicts = gemini_person_check([it["text"] for it in batch])
        if verdicts is None:
            hold_new(pending[start:])
            break
        for it, (block, title) in zip(batch, verdicts):
            it["title"] = title
            n_checked += 1
            if block:
                existing.pop(it["post"], None)
                blocked.add(it["post"])
                n_blocked += 1
            else:
                it["checked"] = True
        time.sleep(4)
    log("person filter: {} of {} checked, {} blocked, {} titled".format(
        n_checked, len(pending), n_blocked, sum(1 for it in pending if it.get("title"))))


# --------------------------------------------------------------------------- #
# Merge + persist
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def prune_media(items, channels):
    referenced = {os.path.basename(c["avatar"]) for c in channels.values() if c.get("avatar")}
    for it in items:
        for m in it.get("media", []):
            for key in ("file", "poster"):
                if m.get(key):
                    referenced.add(os.path.basename(m[key]))
    for fn in os.listdir(MEDIA_DIR):
        if fn != ".gitkeep" and fn not in referenced:
            try:
                os.remove(os.path.join(MEDIA_DIR, fn))
            except OSError:
                pass


def main():
    if now_is_holy():
        log("== שבת / יום טוב - no scraping, nothing published ==")
        return
    log("== נטגרם scraper ==  image filter:", "ON" if GEMINI_KEY else ("OFF (images kept, NetFree filters)" if UNFILTERED_OK else "OFF (text-only)"))
    news = load_json(NEWS_PATH, {"items": [], "channels": {}})
    removed = set(load_json(REMOVED_PATH, {"posts": []}).get("posts", []))
    pf_state = load_json(PF_PATH, {})
    blocked = set(pf_state.get("blocked", {}))
    enabled = [c for c in CFG["channels"] if c.get("enabled", True)]
    enabled_names = {c["username"] for c in enabled}
    # some channels mark every real news item with a fixed prefix ("הפרגוד:");
    # their sponsored posts come without it, so anything unprefixed is dropped
    prefixes = {c["username"]: c["require_prefix"] for c in enabled if c.get("require_prefix")}

    def channel_ok(post, raw):
        pre = prefixes.get(post.split("/")[0])
        return not pre or re.sub(r"^[\s‎‏*]+", "", raw or "").startswith(pre)

    def is_caption(post, text):
        """A few words that came attached to a photo/video: meaningless without it."""
        pre = prefixes.get(post.split("/")[0], "")
        body = text[len(pre):] if pre and text.startswith(pre) else text
        return len(body.split()) <= CFG["text_filter"].get("caption_max_words", 0)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=CFG["limits"].get("max_age_days", 5))).isoformat()
    existing = {}
    for it in news.get("items", []):
        if (it.get("post") in removed or it.get("post", "").split("/")[0] not in enabled_names
                or (it.get("ts") or "") < cutoff or is_holy(it.get("ts"))):
            continue
        if it.get("post") in blocked:
            continue
        # re-apply the current filters, so filter changes also clean older posts
        if not it.get("kind"):
            it["text"] = clean_text(it.get("text", ""))
        if not MEDIA_ON:
            it["media"] = []  # also strips photos/videos already published
        if not channel_ok(it["post"], it["text"]):
            continue
        if it["text"] and not allowed(it.get("kind"), it["text"]):
            continue
        if not it["text"] and not it.get("media"):
            continue
        existing[it["post"]] = it
    channels = {k: v for k, v in news.get("channels", {}).items() if k in enabled_names}
    new_count = 0

    for ch in enabled:
        uname = ch["username"]
        kind = ch.get("kind")
        log("-> @" + uname)
        info, msgs = fetch_channel(uname)
        if not msgs:
            log("   ! no public posts (wrong username / private / empty) - skipped")
            channels.pop(uname, None)
            continue
        if info:
            prev = channels.get(uname, {})
            avatar = prev.get("avatar") or process_avatar(info)
            info.pop("avatar_src", None)
            channels[uname] = {"title": ch.get("title") or info["title"],
                               "avatar": avatar}
            if kind:
                channels[uname]["kind"] = kind
        log("   {} messages".format(len(msgs)))

        for msg in msgs[-CFG["limits"]["max_messages_per_channel"]:]:
            post = msg["post"]
            if post in existing:
                if msg["views"] is not None:
                    existing[post]["views"] = msg["views"]  # keep view counts fresh
                miss = missing_media(msg["media"], existing[post].get("media"))
                if miss:
                    existing[post]["missing"] = miss
                else:
                    existing[post].pop("missing", None)
                continue
            if post in removed or post in blocked or (msg["ts"] or "") < cutoff:
                continue  # removed on request, or an old post (dead/moved channel)
            if is_holy(msg["ts"]):
                log("   - dropped (posted on Shabbat / Yom Tov)")
                continue
            if not channel_ok(post, msg["text"]):
                log("   - dropped (sponsored: no channel prefix)")
                continue
            if msg["text"] and not allowed(kind, msg["text"]):
                log("   - dropped (ad/spam)")
                continue
            text = clean_board(msg["rich"] or msg["text"]) if kind else clean_text(msg["text"])
            if not text and not msg["media"]:
                continue
            if text and not allowed(kind, text):
                log("   - dropped (ad/spam)")
                continue
            media = process_media(post, msg["media"])   # site media is reviewed by NetFree (images held until reviewed, videos blocked - checked 2026-10-19)
            if not text and not media:
                continue
            if msg["media"] and not media and is_caption(post, text):
                log("   - dropped (caption of a photo/video we don't show)")
                continue
            existing[post] = {
                "id": "p" + hashlib.sha1(post.encode()).hexdigest()[:12],
                "post": post,
                "ts": msg["ts"],
                "text": text,
                "views": msg["views"],
                "forwarded_from": msg["forwarded_from"],
                "media": media,
                "_new": True,
            }
            if missing_media(msg["media"], media):
                existing[post]["missing"] = missing_media(msg["media"], media)
            if kind:
                existing[post]["kind"] = kind
            new_count += 1
        time.sleep(1)

    person_filter(existing, blocked, pf_state)
    for it in existing.values():
        it.pop("_new", None)
    # remember blocked posts only while the channel page can still serve them
    now, keep = time.time(), (CFG["limits"].get("max_age_days", 5) + 2) * 86400
    seen = pf_state.get("blocked", {})
    pf_state["blocked"] = {p: seen.get(p, now) for p in sorted(blocked) if now - seen.get(p, now) < keep}
    with open(PF_PATH, "w", encoding="utf-8") as fh:
        json.dump(pf_state, fh, ensure_ascii=False, indent=1)

    items = sorted(existing.values(), key=lambda x: x.get("ts") or "", reverse=True)
    # per-channel cap, so busy channels never push the quieter ones off the site
    per_ch, kept = CFG["limits"].get("max_items_per_channel", 20), {}
    capped = []
    for it in items:
        ch = it["post"].split("/")[0]
        if kept.get(ch, 0) < per_ch:
            kept[ch] = kept.get(ch, 0) + 1
            capped.append(it)
    items = capped[: CFG["limits"]["max_items"]]
    prune_media(items, channels)

    out = {"updated": datetime.now(timezone.utc).isoformat(),
           "channels": channels, "count": len(items), "items": items}
    with open(NEWS_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    log("== done: {} new, {} total ==".format(new_count, len(items)))


if __name__ == "__main__":
    main()

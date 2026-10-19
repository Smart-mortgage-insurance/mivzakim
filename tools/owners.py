"""Digs for a way to reach the OWNER of each channel (to ask permission), not the advertisers inside it.
Sources per channel: the channel description, lines in the post history that talk about advertising /
contacting the admin, details that recur in most posts, the public profile page of every @user the
channel points at, and the web sites it links to (home + contact pages).
Prints one `OWNER {json}` line per channel."""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "he-IL,he;q=0.9,en;q=0.6"}
CFG = json.loads((Path(__file__).resolve().parent.parent / "scraper" / "config.json").read_text(encoding="utf-8"))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
CHANNELS = ARGS or [c["username"] for c in CFG["channels"] if c.get("enabled", True) and c.get("kind") == "jobs"]
PAGES = 40 if "--deep" in sys.argv else 15

PHONE = re.compile(r"(?<!\d)(?:\+?972[-\s]?|0)(?:5\d|[2-4]|7\d|[89])[-\s]?\d{3}[-\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MENTION = re.compile(r"(?<![\w@.])@([A-Za-z]\w{4,31})")
TME = re.compile(r"(?:t\.me|telegram\.me)/(\+?[\w-]+)(?:/\d+)?", re.I)
WA = re.compile(r"wa\.me/\+?(\d{9,15})|whatsapp\.com/send/?\?[^\s\"']*?phone=\+?(\d{9,15})", re.I)
WAGROUP = re.compile(r"chat\.whatsapp\.com/\w+", re.I)
URL = re.compile(r"https?://[^\s\"'<>)]+", re.I)
RULES = re.compile(r"כללי|חוקי|תקנון|ברוכים הבאים|הנהלת|מנהלי|rules|welcome", re.I)
EXTRA_POSTS = {"jobs_in_jerusalem": [1528]}          # rules posts referenced from the linked groups' bios
ADMIN = re.compile(r"לפרסום|פרסום בערוץ|לפרסם|ליצירת קשר|צור קשר|צרו קשר|מנהל|אדמין|לפניות|פניות|contact|admin|advertis", re.I)
SOCIAL = re.compile(r"t\.me|telegram\.|whatsapp\.|wa\.me|facebook\.|fb\.|instagram\.|linkedin\.|youtube\.|youtu\.be|twitter\.|x\.com|bit\.ly|tiktok\.|paypal\.|forms\.gle|google\.", re.I)
JUNK_MAIL = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg)$|sentry|wixpress|example\.|@2x|u003e", re.I)

S = requests.Session()
S.headers.update(UA)


def get(url, timeout=20):
    try:
        r = S.get(url, timeout=timeout)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def norm_phone(p):
    d = re.sub(r"\D", "", p)
    return "0" + d[3:] if d.startswith("972") else d


def tokens(text, hrefs=""):
    blob = text + " " + hrefs
    out = set()
    out |= {"mail:" + m.lower().rstrip(".") for m in EMAIL.findall(blob) if not JUNK_MAIL.search(m)}
    out |= {"tel:" + norm_phone(m) for m in PHONE.findall(text)}
    out |= {"wa:" + norm_phone(a or b) for a, b in WA.findall(blob)}
    out |= {"wagroup:" + m for m in WAGROUP.findall(blob)}
    out |= {"tg:" + m for m in MENTION.findall(text)}
    out |= {"tg:" + m for m in TME.findall(blob) if m.lower() not in ("s", "share", "joinchat", "addlist") and not m.startswith("+")}
    return out


def channel(u):
    own = u.lower()
    desc_txt = desc_hrefs = title = subs = ""
    posts, seen, admin_lines, admin_tok = 0, Counter(), [], Counter()
    before = None
    for page in range(PAGES):
        html = get("https://t.me/s/" + u + ("?before=%d" % before if before else ""))
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        if page == 0:
            t = soup.select_one(".tgme_channel_info_header_title")
            c = soup.select_one(".tgme_channel_info_counter .counter_value")
            d = soup.select_one(".tgme_channel_info_description")
            title, subs = (t.get_text(strip=True) if t else ""), (c.get_text() if c else "")
            if d:
                for br in d.find_all("br"):
                    br.replace_with("\n")
                desc_txt = d.get_text(" ").strip()
                desc_hrefs = " ".join(a.get("href", "") for a in d.select("a[href]"))
        msgs = soup.select(".tgme_widget_message")
        if not msgs:
            break
        for m in msgs:
            body = m.select_one(".tgme_widget_message_text")
            if not body:
                continue
            posts += 1
            for br in body.find_all("br"):
                br.replace_with("\n")
            text = body.get_text(" ")
            hrefs = " ".join(a.get("href", "") for a in body.select("a[href]"))
            seen.update(t for t in tokens(text, hrefs) if t.lower() not in ("tg:" + own, "tg:@" + own))
            for line in text.split("\n"):
                if ADMIN.search(line):
                    tk = tokens(line) - {"tg:" + u}
                    if tk:
                        admin_tok.update(tk)
                        if len(admin_lines) < 4 and line.strip()[:110] not in admin_lines:
                            admin_lines.append(line.strip()[:110])
        ids = [int(m["data-post"].split("/")[-1]) for m in msgs if m.get("data-post")]
        if not ids or min(ids) <= 1:
            break
        before = min(ids)
        time.sleep(0.25)

    # the first posts of the channel: welcome / rules / "how to advertise here" usually live there
    old_lines, old_tok, after = [], Counter(), 0
    for _ in range(4):
        html = get("https://t.me/s/%s?after=%d" % (u, after))
        soup = BeautifulSoup(html, "html.parser") if html else None
        msgs = soup.select(".tgme_widget_message") if soup else []
        if not msgs:
            break
        for m in msgs:
            body = m.select_one(".tgme_widget_message_text")
            if not body:
                continue
            for br in body.find_all("br"):
                br.replace_with("\n")
            text = body.get_text(" ")
            hrefs = " ".join(a.get("href", "") for a in body.select("a[href]"))
            if ADMIN.search(text) or RULES.search(text):
                old_tok.update(tokens(text, hrefs) - {"tg:" + u})
                if len(old_lines) < 5:
                    old_lines.append(re.sub(r"\s+", " ", text)[:260])
        ids = [int(m["data-post"].split("/")[-1]) for m in msgs if m.get("data-post")]
        if not ids or max(ids) <= after:
            break
        after = max(ids)
        time.sleep(0.25)

    # posts the owner points at as "the rules" (t.me/<channel>/<id>)
    rules = {}
    linked = set(re.findall(r"t\.me/(?:s/)?(%s)/(\d+)" % re.escape(u), desc_txt + " " + desc_hrefs, re.I))
    for extra in EXTRA_POSTS.get(own, []):
        linked.add((u, str(extra)))
    for _, pid in sorted(linked)[:4]:
        html = get("https://t.me/%s/%s?embed=1&mode=tme" % (u, pid))
        sp = BeautifulSoup(html, "html.parser") if html else None
        body = sp.select_one(".tgme_widget_message_text") if sp else None
        if body:
            for br in body.find_all("br"):
                br.replace_with("\n")
            text = body.get_text(" ")
            hrefs = " ".join(a.get("href", "") for a in body.select("a[href]"))
            rules[pid] = {"text": re.sub(r"\s+", " ", text)[:500], "contacts": sorted(tokens(text, hrefs) - {"tg:" + u})}

    desc_tok = {t for t in tokens(desc_txt, desc_hrefs) if t.lower() != "tg:" + own}
    need = max(4, posts // 4)
    footer = {k: v for k, v in seen.most_common(15) if v >= need}
    sites = sorted({urlparse(x).scheme + "://" + urlparse(x).netloc for x in URL.findall(desc_txt + " " + desc_hrefs)
                    if not SOCIAL.search(urlparse(x).netloc)} |
                   {"https://" + w for w in re.findall(r"(?<![\w@./])((?:www\.)?[\w-]+\.(?:co\.il|org\.il|com|net|io))(?![\w@])", desc_txt, re.I)
                    if not SOCIAL.search(w)})

    # public profile page of every telegram user / group the owner points at
    profiles = {}
    users = {t[3:] for t in desc_tok | set(admin_tok) | set(footer) if t.startswith("tg:")}
    for name in sorted(users)[:8]:
        html = get("https://t.me/" + name)
        if not html:
            continue
        sp = BeautifulSoup(html, "html.parser")
        tt = sp.select_one(".tgme_page_title")
        bio = sp.select_one(".tgme_page_description")
        extra = sp.select_one(".tgme_page_extra")
        btxt = bio.get_text(" ") if bio else ""
        bhref = " ".join(a.get("href", "") for a in bio.select("a[href]")) if bio else ""
        profiles[name] = {"name": tt.get_text(strip=True) if tt else "", "kind": extra.get_text(strip=True)[:40] if extra else "",
                          "bio": btxt.strip()[:160], "contacts": sorted(tokens(btxt, bhref) - {"tg:" + name})}
        time.sleep(0.2)

    # the owner's web site: home page + contact pages
    web = {}
    for site in sites[:3]:
        found, pages = set(), [site]
        home = get(site, 15)
        if home:
            sp = BeautifulSoup(home, "html.parser")
            for a in sp.select("a[href]"):
                label = (a.get_text(" ") + " " + a["href"]).lower()
                if re.search(r"contact|צור|קשר|about|אודות", label):
                    pages.append(urljoin(site, a["href"]))
            for pg in list(dict.fromkeys(pages))[:5]:
                html = home if pg == site else get(pg, 15)
                if not html:
                    continue
                sp2 = BeautifulSoup(html, "html.parser")
                hrefs = " ".join(a.get("href", "") for a in sp2.select("a[href]"))
                found |= {t for t in tokens(sp2.get_text(" "), hrefs) if not t.startswith("tg:")}
        web[site] = sorted(found)[:14] if home else ["(site did not answer)"]

    return {"u": u, "title": title, "subs": subs, "posts_scanned": posts, "desc": desc_txt[:1500],
            "first_posts": old_lines, "first_posts_contacts": dict(old_tok.most_common(10)), "rules_posts": rules,
            "desc_contacts": sorted(desc_tok), "admin_lines": admin_lines,
            "admin_contacts": dict(admin_tok.most_common(10)), "footer_contacts": footer,
            "profiles": profiles, "web": web}


for u in CHANNELS:
    try:
        print("OWNER " + json.dumps(channel(u), ensure_ascii=True), flush=True)
    except Exception as exc:
        print("OWNER " + json.dumps({"u": u, "error": "%s: %s" % (type(exc).__name__, exc)}), flush=True)

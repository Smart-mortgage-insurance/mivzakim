"""Re-runs the current person filter over the posts it blocked before. Posts that now pass are
dropped from the blocked list, so the next feed cycle picks them up again and publishes them.
Runs on Actions (t.me is NetFree-blocked); prints counts only, the released posts go out only
as a short-lived artifact for a manual look, never into the repo."""
import html, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scraper"))
import scrape                      # noqa: E402
from blocked import fetch          # noqa: E402


def main():
    if not scrape.GEMINI_KEY:
        sys.exit("no GEMINI_API_KEY")
    state = scrape.load_json(scrape.PF_PATH, {})
    blocked = state.get("blocked", {})
    posts, texts = [], []
    for p in sorted(blocked):
        text, _ = fetch(p)
        time.sleep(0.7)
        if text:
            posts.append(p)
            texts.append(text)
    released, still, failed = [], 0, 0
    for start in range(0, len(texts), 20):
        verdicts = scrape.gemini_person_check(texts[start:start + 20])
        if verdicts is None:
            failed += len(texts[start:start + 20])
            continue
        for p, t, (block, _) in zip(posts[start:start + 20], texts[start:start + 20], verdicts):
            if block:
                still += 1
            else:
                released.append((p, t))
        time.sleep(4)
    for p, _ in released:
        blocked.pop(p, None)
    with open(scrape.PF_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    cards = "".join('<div class="c"><div class="h">{}</div>{}</div>'.format(
        html.escape(p), html.escape(t).replace("\n", "<br>")) for p, t in released)
    page = ('<!doctype html><html dir="rtl" lang="he"><meta charset="utf-8"><style>'
            'body{font-family:Arial,sans-serif;background:#f3f3f3;max-width:760px;margin:auto;padding:16px}'
            '.c{background:#fff;border-radius:10px;padding:12px;margin:10px 0;line-height:1.5}'
            '.h{color:#777;font-size:13px;margin-bottom:6px}</style>'
            '<h2>@N</h2>@C</html>').replace("@N", str(len(released))).replace("@C", cards)
    open(os.path.join(os.path.dirname(HERE), "released.html"), "w", encoding="utf-8").write(page)
    json.dump([p for p, _ in released], open(os.path.join(os.path.dirname(HERE), "released.json"), "w"))
    print("blocked before: {}, fetched: {}, released: {}, still blocked: {}, not checked: {}".format(
        len(blocked) + len(released), len(posts), len(released), still, failed))


if __name__ == "__main__":
    main()

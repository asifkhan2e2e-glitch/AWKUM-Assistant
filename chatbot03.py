"""
Phase 2: AWKUM data collector.

Reads the public WordPress REST API of awkum.edu.pk (pages + posts) and the
Blogger feed of news.awkum.edu.pk, turns every item into clean text, and saves
one JSON file per item in data/docs/.  Re-running only rewrites items that changed.

Run:   python collector/collect.py
Options:
   --news-days 548   how far back to read the news blog (default ~18 months)
   --skip-news / --skip-wp
Output: data/docs/*.json, data/state.json, data/collect_report.md
"""
import argparse, base64, hashlib, html, json, re, time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString

HERE = Path(__file__).resolve().parent
# If this file sits inside a folder called "collector", the project root is one level up; otherwise it is this folder.
ROOT = HERE.parent if HERE.name == "collector" else HERE
VERSION = "v8 (v7 + program-wise fees + button-title fix)"   # printed at start, so you can see the new file is the one running
DOCS = ROOT / "data" / "docs"
STATE = ROOT / "data" / "state.json"
REPORT = ROOT / "data" / "collect_report.md"

BASE = "https://awkum.edu.pk"
NEWS = "https://news.awkum.edu.pk"
UA = {"User-Agent": "AWKUM-Chatbot-Collector/1.0 (student project; read-only)"}
DELAY = 1.0

# Theme demo / test pages that are not real university content (matched on URL path).
SKIP_PATHS = {"test", "test-2", "sample-page", "home-testing", "home-new", "home-new-cloned-22074",
              "home-02-cloned-16501", "home-2", "homepages", "marketing", "services",
              "testimonials", "projects", "qa", "team"}

# Order matters: first match wins.
NEWS_RULES = [
    ("jobs", r"fixed pay|lecturer|assistant professor|vacanc|advertisement no|walk-?in|recruit|appellate|selection board"),
    ("hostel", r"hostel"),
    ("merit_list", r"merit list"),
    ("admission", r"admission|admitted"),
    ("scholarship", r"scholarship|stipend|financial aid"),
    ("exam_result", r"date ?sheet|examination|\bexams?\b|result|roll no"),
    ("tender", r"tender|quotation|\bbid\b|procurement"),
    ("phd_defense", r"defen[cs]e|viva voce|dissertation"),
]
PAGE_RULES = [  # (category, path prefix)
    ("fee", "admissions/fee-structure"),
    ("scholarship", "admissions/scholarships"),
    ("admission", "admissions"),
    ("department", "faculties-colleges"),
    ("administration", "administration"),
    ("about", "about-us"),
    ("academics", "academics"),
    ("contact", "contact"),
]

s = requests.Session()
s.headers.update(UA)
_last = [0.0]


TRIES = 5


def get(url, **kw):
    """GET with patience: the university server is sometimes slow, so wait longer and retry before giving up."""
    for attempt in range(TRIES):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = s.get(url, timeout=(15, 120), **kw)      # 15 s to connect, 120 s to receive the page
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == TRIES - 1:
                raise
            pause = 5 * (attempt + 1)
            print(f"  (slow or failed: {type(e).__name__}; retrying in {pause}s, try {attempt + 2} of {TRIES})")
            time.sleep(pause)


# ---------- parsing ----------
VC_RAW = re.compile(r"\[vc_raw_html[^\]]*\](.*?)\[/vc_raw_html\]", re.S)
B64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/=])")


def _b64_html(blob):
    """WPBakery stores 'Raw HTML' blocks as base64(urlencoded html). Return the html, or None."""
    blob = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", blob))
    try:
        txt = unquote(base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8"))
    except Exception:
        return None
    return txt if "<" in txt else None


FEE_DATA = re.compile(r"var\s+DATA\s*=\s*(\[.*?\])\s*;", re.S)


def fee_widget(decoded):
    """The AWKUM fee page keeps every program's fee in a JavaScript list inside a 'fee calculator' widget.
    Return {"rows": [...], "title": ..., "note": ...} or None if this block is not that widget."""
    m = FEE_DATA.search(decoded or "")
    if not m:
        return None
    try:
        rows = json.loads(m.group(1))
    except ValueError:
        return None
    need = ("cat", "name", "tuition", "other", "total")
    out, seen = [], set()
    for r in rows:
        if not (isinstance(r, dict) and all(k in r for k in need)):
            return None
        key = tuple(r[k] for k in need)
        if key not in seen:                                   # the page lists a few programs twice
            seen.add(key)
            out.append({k: r[k] for k in need})
    soup = BeautifulSoup(decoded, "html.parser")
    h = soup.find("h2")
    note = soup.find(class_="awkum-fee-footnote")
    return {"rows": out, "title": h.get_text(" ", strip=True) if h else "Official Fee Structure",
            "note": note.get_text(" ", strip=True) if note else ""}


def find_fee_widget(body):
    for m in VC_RAW.finditer(body or ""):
        d = _b64_html(m.group(1))
        w = fee_widget(d) if d else None
        if w:
            return w
    return None


def _raw_block(m):
    d = _b64_html(m.group(1))
    if not d:
        return " "
    w = fee_widget(d)
    if w:      # the widget itself is empty HTML + script; its numbers are saved separately as structured rows
        return f"<h3>{html.escape(w['title'])}</h3><p>{html.escape(w['note'])}</p>"
    return d


def expand_shortcodes(body):
    body = VC_RAW.sub(_raw_block, body or "")
    body = B64_RUN.sub(lambda m: _b64_html(m.group(0)) or m.group(0), body)
    # The API sends the quotes inside shortcodes as HTML entities (&#8221; etc). Decode them inside [...] only.
    body = re.sub(r"\[/?vc_[a-z_0-9]+[^\]]*\]", lambda m: html.unescape(m.group(0)), body)
    # WordPress "smart quotes" the same tag inconsistently: it can open with " and close with the double-prime (U+2033).
    Q = "\u201c\u201d\u201e\u201f\u2033\u2036\""
    # accordion / tab titles become headings, so an embedded file keeps the title of the tab it sits in
    body = re.sub(r"\[(?:vc_accordion_tab|vc_tta_section|vc_tab|vc_toggle|vc_tour_section)\b[^\]]*?(?<![A-Za-z_])title=[" + Q + r"]([^" + Q + r"]*)[" + Q + r"][^\]]*\]",
                  lambda m: f"<h3>{html.escape(m.group(1))}</h3>", body)
    # buttons ("BA / BSc Part-I Form") become normal links
    def _btn(m):
        tag = m.group(0)
        t = re.search(r"(?<![A-Za-z_])title=[" + Q + r"]([^" + Q + r"]*)[" + Q + r"]", tag)
        u = re.search(r"link=[" + Q + r"]url:([^|" + Q + r"]*)", tag)
        if not (t and u):
            return " "
        return f'<a href="{html.escape(unquote(u.group(1)))}">{html.escape(t.group(1))}</a> '
    body = re.sub(r"\[vc_btn\b[^\]]*\]", _btn, body)
    return re.sub(r"\[/?vc_[a-z_0-9]+[^\]]*\]", " ", body)        # leftover [vc_row] [vc_column] ... tags


def html_to_text(body, base_url):
    soup = BeautifulSoup(expand_shortcodes(body), "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "form"]):
        t.decompose()
    links = []
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = urljoin(base_url, (tag.get("src") or tag.get("data") or "").strip())
        if src.startswith("http"):
            # Drive "preview" links are for embedding; give users the normal "view" link
            src = re.sub(r"(drive\.google\.com/file/d/[^/?]+)/preview", r"\1/view", src)
            links.append({"label": "Embedded document", "href": src})
            tag.replace_with(NavigableString(f"\n[Embedded document: {src}]\n"))
        else:
            tag.decompose()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        label = a.get_text(" ", strip=True)
        if not href.startswith(("http", "mailto:", "tel:")) or href.endswith("#"):
            a.unwrap()
        elif not label:
            a.decompose()  # image-only link (banners, "new" gifs)
        else:
            links.append({"label": label[:120], "href": href})
            # keep the URL next to its text, so a search chunk always carries its own link
            a.replace_with(NavigableString(label if label == href else f"{label} ({href})"))
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(" | ".join(cells))
        table.replace_with(NavigableString("\n" + "\n".join(rows) + "\n"))
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    seen, uniq = set(), []
    for l in links:
        if l["href"] not in seen:
            seen.add(l["href"]); uniq.append(l)
    return text, uniq


def classify(source, path, title, labels):
    if source == "news":
        hay = f"{title} {' '.join(labels)}".lower()
        for cat, rx in NEWS_RULES:
            if re.search(rx, hay):
                return cat
        return "news"
    for cat, prefix in PAGE_RULES:
        if path.startswith(prefix):
            return cat
    return "other" if path else "home"


def path_meta(path):
    parts = path.split("/")
    if parts[0] != "faculties-colleges":
        return {}
    return {"faculty": parts[1] if len(parts) > 1 else "",
            "department": parts[2] if len(parts) > 2 else "",
            "section": parts[3] if len(parts) > 3 else ""}


def parse_merit_entries(body):
    """Turn a merit-list post (one <li> per program, each with a link) into structured entries."""
    soup = BeautifulSoup(body or "", "html.parser")
    entries, section, group = [], "", ""
    for el in soup.find_all(["h1", "h2", "h3", "h4", "li"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if el.name != "li":
            section, group = text, ""
            continue
        a = next((x for x in el.find_all("a", href=True) if x.get_text(strip=True)), None)
        if a is None:
            if text and len(text) < 60:          # e.g. "Timergara Campus." / "IBL." / "Diploma."
                group = text.strip(" .")
            continue
        label = a.get_text(" ", strip=True)
        title = text.replace(label, "").strip(" -\u2013:")
        if not title:                            # link-only item (e.g. WhatsApp channel)
            continue
        m = re.search(r"merit\s*list\s*[-\u2013]?\s*([IVX]+)\b", title, re.I)
        program = re.sub(r"\s*[-\u2013]?\s*merit\s*list\s*[-\u2013]?\s*[IVX]*", "", title, flags=re.I).strip(" -\u2013")
        entries.append({"section": section, "group": group, "program": program,
                        "list_no": m.group(1).upper() if m else "", "title": title, "url": a["href"]})
    return entries


def make_doc(source, uid, url, title, body, published, modified, labels):
    text, links = html_to_text(body, url)
    path = urlparse(url).path.strip("/")
    title = html.unescape(re.sub(r"<[^>]+>", "", title or "")).strip()
    doc = {"id": uid, "source": source, "url": url, "title": title,
           "category": classify(source, path, title, labels),
           "labels": labels, "published": published, "modified": modified,
           "text": text, "links": links}
    if source == "wp":
        doc.update(path_meta(path))
    if source == "news" and doc["category"] == "merit_list":
        doc["entries"] = parse_merit_entries(body)
    fee = find_fee_widget(body) if source == "wp" else None
    if fee:
        doc["fee_rows"], doc["fee_title"], doc["fee_note"] = fee["rows"], fee["title"], fee["note"]
    key = title + "\n" + text + "\n" + "\n".join(l["href"] for l in links) + "\n" + json.dumps(doc.get("fee_rows", []))
    doc["hash"] = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return doc


# ---------- sources ----------
def wp_fetch(kind):
    fields = "id,date,modified,link,title,content" + (",categories" if kind == "posts" else "")
    page = 1
    while True:
        r = get(f"{BASE}/wp-json/wp/v2/{kind}", params={"per_page": 50, "page": page, "_fields": fields})
        items = r.json()
        yield from items                      # hand over each batch at once, so it is saved even if a later batch fails
        if not items or page >= int(r.headers.get("X-WP-TotalPages", 1)):
            return
        page += 1


def wp_docs():
    cats = {c["id"]: html.unescape(c["name"]) for c in
            get(f"{BASE}/wp-json/wp/v2/categories", params={"per_page": 100, "_fields": "id,name"}).json()}
    for kind in ("pages", "posts"):
        for it in wp_fetch(kind):
            path = urlparse(it["link"]).path.strip("/")
            if path in SKIP_PATHS:
                continue
            labels = [cats.get(c, str(c)) for c in it.get("categories", [])]
            yield make_doc("wp", f"wp-{kind[:-1]}-{it['id']}", it["link"], it["title"]["rendered"],
                           it["content"]["rendered"], it["date"], it["modified"], labels)


def news_docs(days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    start = 1
    while True:
        r = get(f"{NEWS}/feeds/posts/default", params={"alt": "json", "start-index": start, "max-results": 100})
        entries = r.json()["feed"].get("entry", [])
        if not entries:
            return
        for e in entries:
            pub = e["published"]["$t"]
            if pub[:10] < cutoff:
                return
            m = re.search(r"post-(\d+)", e["id"]["$t"])
            link = next((l["href"] for l in e["link"] if l["rel"] == "alternate"), "")
            labels = [c["term"] for c in e.get("category", [])]
            yield make_doc("news", f"news-{m.group(1) if m else hashlib.md5(link.encode()).hexdigest()[:10]}",
                           link, e["title"]["$t"], e.get("content", {}).get("$t", ""), pub, e["updated"]["$t"], labels)
        start += len(entries)


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--news-days", type=int, default=548)
    ap.add_argument("--skip-news", action="store_true")
    ap.add_argument("--skip-wp", action="store_true")
    ap.add_argument("--force", action="store_true", help="rewrite every file even if the page did not change")
    args = ap.parse_args()

    print(f"collect.py {VERSION}")
    print(f"Saving into: {DOCS}")
    DOCS.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    new_state = dict(state)
    stats, by_cat, thin, seen_wp, removed = Counter(), Counter(), [], set(), []
    merit_docs, embed_only, fee_docs = [], [], []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def handle(doc):
        status = "new" if doc["id"] not in state else ("changed" if state[doc["id"]] != doc["hash"] else "same")
        if status != "same" or args.force:
            doc["collected_at"] = now
            (DOCS / f"{doc['id']}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        new_state[doc["id"]] = doc["hash"]
        stats[status] += 1
        by_cat[doc["category"]] += 1
        if doc.get("entries"):
            merit_docs.append((doc["title"], len(doc["entries"])))
        if doc.get("fee_rows"):
            fee_docs.append((doc["url"], len(doc["fee_rows"])))
        if len(doc["text"]) < 200:
            is_embed = any(l["label"] == "Embedded document" for l in doc["links"])
            (embed_only if is_embed else thin).append((doc["url"], len(doc["text"])))

    problems = []
    if not args.skip_wp:
        print("Reading website pages and posts...")
        try:
            for d in wp_docs():
                seen_wp.add(d["id"]); handle(d)
            # Only when the whole list was read is it safe to say a page was removed from the website.
            for uid in [k for k in state if k.startswith("wp-") and k not in seen_wp]:
                (DOCS / f"{uid}.json").unlink(missing_ok=True)
                new_state.pop(uid); removed.append(uid)
        except requests.RequestException as e:
            problems.append(f"website pages: {type(e).__name__}")
            print(f"\n!! The website did not answer ({type(e).__name__}). Nothing already saved was deleted.")
    if not args.skip_news:
        print("Reading news blog...")
        try:
            for d in news_docs(args.news_days):
                handle(d)
        except requests.RequestException as e:
            problems.append(f"news blog: {type(e).__name__}")
            print(f"\n!! The news site did not answer ({type(e).__name__}). Nothing already saved was deleted.")

    STATE.write_text(json.dumps(new_state, indent=0), encoding="utf-8")
    md = ["# Collector report", f"\nRun: {now}\n",
          f"- New: {stats['new']}  |  Changed: {stats['changed']}  |  Unchanged: {stats['same']}  |  Removed: {len(removed)}\n",
          "## Items by category\n"] + [f"- {c}: {n}" for c, n in by_cat.most_common()]
    md += ["\n## Fee table (program-wise fees read from the fee calculator)\n"] + [f"- {u}: {n} programs" for u, n in fee_docs]
    md += ["\n## Merit list posts (entries parsed)\n"] + [f"- {t[:90]}: {n} entries" for t, n in merit_docs]
    md += [f"\n## Items that are only an embedded file (title + link, content not readable): {len(embed_only)}\n"]
    md += [f"- {u}" for u, n in embed_only[:15]]
    md += [f"\n## Items with very little text ({len(thin)}) - probably image-based, need a look\n"]
    md += [f"- {u} ({n} chars)" for u, n in thin[:80]]
    REPORT.write_text("\n".join(md), encoding="utf-8")
    print(f"Done. new={stats['new']} changed={stats['changed']} same={stats['same']} removed={len(removed)}")
    if problems:
        print("NOT COMPLETE (" + "; ".join(problems) + "). Everything saved so far is kept. Wait a few minutes and run this command again.")
    print("Send data/collect_report.md to Claude.")


if __name__ == "__main__":
    main()

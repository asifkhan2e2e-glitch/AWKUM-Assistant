"""
Phase 3: build the search index.

Reads data/docs/*.json (made by the collector) and writes data/index.sqlite,
a single-file full-text search index (SQLite FTS5, BM25 ranking).  No extra packages needed.

Run:   python indexer/build_index.py
Re-run it after every collector run (takes a few seconds; the index is rebuilt from scratch).
"""
import json, re, sqlite3, sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find_root():
    for p in (HERE, HERE.parent):          # works whether the script sits in the project folder or in indexer/
        if (p / "data" / "docs").exists():
            return p
    sys.exit("Could not find data/docs. Run the collector first.")


ROOT = find_root()
DOCS, CURATED, DB = ROOT / "data" / "docs", ROOT / "data" / "curated", ROOT / "data" / "index.sqlite"
MAX_CHARS = 900

# Official links the bot may always give. Add more in data/curated/*.json (same fields).
DEFAULT_LINKS = [
    {"title": "MIS (student portal: result, attendance, fee status)", "url": "https://eservices.awkum.edu.pk/",
     "keywords": "mis eservices student portal login result attendance fee status roll number",
     "note": "Students log in here themselves. The chatbot cannot see anyone's MIS data."},
    {"title": "AWKUM official website", "url": "https://awkum.edu.pk/", "keywords": "website home main site"},
    {"title": "AWKUM news and notices", "url": "https://news.awkum.edu.pk/", "keywords": "news notices announcements merit lists jobs tenders"},
    {"title": "Admissions", "url": "https://awkum.edu.pk/admissions/", "keywords": "admission admissions apply programs"},
    {"title": "How to apply", "url": "https://awkum.edu.pk/admissions/how-to-apply/", "keywords": "how to apply application process"},
    {"title": "Eligibility criteria", "url": "https://awkum.edu.pk/admissions/eligibility-criteria/", "keywords": "eligibility criteria requirements"},
    {"title": "Fee structure", "url": "https://awkum.edu.pk/admissions/fee-structure/", "keywords": "fee fees structure charges dues bank slip"},
    {"title": "Scholarships", "url": "https://awkum.edu.pk/admissions/scholarships/", "keywords": "scholarship scholarships financial aid"},
    {"title": "Academic calendar", "url": "https://awkum.edu.pk/academics/academic-calendar/", "keywords": "academic calendar semester dates schedule"},
    {"title": "Prospectus", "url": "https://awkum.edu.pk/prospectus/", "keywords": "prospectus"},
    {"title": "Map and directions to AWKUM", "url": "https://awkum.edu.pk/about-us/map-directions/", "keywords": "map directions location address route reach"},
    {"title": "Contact AWKUM", "url": "https://awkum.edu.pk/contact/", "keywords": "contact phone email address"},
    {"title": "Admission portal (admissions.awkum.edu.pk)", "url": "https://admissions.awkum.edu.pk/", "keywords": "admission portal online apply",
     "note": "Reported closed / not reachable on 2 October 2026. Do not send users here; point them to the Admissions page and the news site instead."},
    {"title": "WhatsApp channel for Admissions Fall 2026 updates", "url": "https://whatsapp.com/channel/0029VbDAcdB1t90inGeXET2q",
     "keywords": "whatsapp channel updates admissions fall 2026"},
]


def split_text(text, limit=MAX_CHARS):
    out, cur, new = [], [], 0
    size = 0

    def flush():
        nonlocal cur, new, size
        out.append("\n".join(cur))
        keep = cur[-1] if len(cur[-1]) < 200 else None     # 1 line of overlap between chunks
        cur, new, size = ([keep] if keep else []), 0, (len(keep) + 1 if keep else 0)

    for raw in text.split("\n"):
        line = raw.strip()
        if not line or re.fullmatch(r"[A-Za-z0-9+/=%]{300,}", line):    # empty, or an undecoded base64 blob
            continue
        pieces = []
        while len(line) > limit:                            # very long line: cut at a space
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(line[:cut]); line = line[cut:].strip()
        pieces.append(line)
        for pc in pieces:
            if new and size + len(pc) + 1 > limit:
                flush()
            cur.append(pc); new += 1; size += len(pc) + 1
    if new:
        out.append("\n".join(cur))
    return out


def words(slug):
    return re.sub(r"[-_]+", " ", slug or "")


def meta_for(d):
    parts = [d.get("category", ""), words(d.get("faculty")), words(d.get("department")), words(d.get("section")),
             " ".join(d.get("labels", []))]
    return " ".join(p for p in parts if p)


# This chatbot serves ONLY the Garden Campus, Mardan. Anything that belongs to another campus is left out of the index.
OTHER_CAMPUS = re.compile(r"\b(timergara|pabbi|srh|swat|buner|chitral|shangla|lower[- ]dir|upper[- ]dir|ucw|university[- ]college[- ]for[- ]women)\b", re.I)


def other_campus(*texts):
    return any(t and OTHER_CAMPUS.search(str(t)) for t in texts)


CAT_WORDS = {"Undergraduate Studies": "BS undergraduate bachelor bachelors",
             "MS / MPhil Programs": "MS MPhil master masters graduate postgraduate",
             "Doctorate / PhD Programs": "PhD doctorate doctoral",
             "Post Graduate Diploma": "diploma PGD postgraduate"}
# Misspellings that appear in the university's own fee list. Added to the searchable text only (users type the right spelling).
TYPOS = {"buisness": "business", "developmnetal": "developmental", "mangement": "management", "interantional": "international",
         "enterprenurship": "entrepreneurship", "veterniry": "veterinary", "pharmaceutial": "pharmaceutical",
         "anaesthesia": "anesthesia", "cardiology": "cardiac"}


# What students type  ->  what the fee list calls it (added to the searchable text only)
ALIASES = {"bcs": "BSCS computer science", "law": "LLB law", "dvm": "veterinary doctor", "pharm": "pharmacy PharmD",
           "bba": "business administration", "mba": "business administration master", "llm": "master of laws law",
           "bed": "education teaching", "gis": "geographic information system", "ir": "international relations"}


def fee_rows(d):
    """One searchable entry per program from the fee calculator (program, level, tuition, other fees, total)."""
    url, pub = d["url"], (d.get("published") or "")[:10]
    note = d.get("fee_note") or "Official fee structure page"
    for r in d["fee_rows"]:
        name, cat = r["name"], r["cat"]
        if other_campus(name):
            continue
        toks = re.findall(r"[a-z]+", name.lower())
        fixed = " ".join([TYPOS[w] for w in toks if w in TYPOS] + [ALIASES[w] for w in toks if w in ALIASES])
        body = (f"{name} | {cat} | Tuition fee: Rs. {r['tuition']:,} | Other fees: Rs. {r['other']:,} | "
                f"Total fee: Rs. {r['total']:,} per semester. {note} ({url})")
        yield (f"Fee: {name} ({cat})", f"fee fees tuition semester {cat} {CAT_WORDS.get(cat, '')} {fixed} fall 2026",
               body, url, "fee", pub, d["id"], json.dumps({"row": f"{cat}|{name}", "post_url": url, "post_title": d["title"]}))


def rows_for(d):
    title, url, cat = d["title"], d["url"], d.get("category", "")
    pub = (d.get("published") or "")[:10]
    meta = meta_for(d)
    base = {"doc_id": d["id"], "post_url": url, "post_title": title}
    if d.get("fee_rows"):
        yield from fee_rows(d)                              # the rest of the page is still indexed normally below
    if d.get("entries"):                                    # merit list post: one row per program/list
        for e in d["entries"]:
            if other_campus(e["title"], e["program"], e["section"], e["group"]):
                continue
            body = (f"{e['title']} | {e['section']} | {e['group']} | Link to the list: {e['url']} | "
                    f"Posted on the page: {title} ({url})")
            yield (f"Merit list: {e['program']}", f"{meta} merit list fall 2026 list {e['list_no']} {e['section']} {e['group']}",
                   body, e["url"], "merit_list", pub, d["id"], json.dumps({**base, "list_no": e["list_no"]}))
        yield (title, meta, "This page lists the merit list of every program (BS, MS, MPhil, PhD). "
               "New lists are added regularly, so check the page for the latest one.", url, cat, pub, d["id"], json.dumps(base))
        return
    chunks = split_text(d.get("text", ""))
    if not chunks:
        chunks = ["(No readable text. The notice is an image or a file; open the link to read it.)"]
    for i, ch in enumerate(chunks):
        yield (title, meta, ch, url, cat, pub, d["id"], json.dumps({**base, "chunk": i}))


def curated_rows():
    items = list(DEFAULT_LINKS)
    if CURATED.exists():
        for f in sorted(CURATED.glob("*.json")):
            items += json.loads(f.read_text(encoding="utf-8"))
    for it in items:
        body = f"{it['title']}. {it.get('note', '')} Link: {it['url']}".replace("  ", " ")
        yield (it["title"], "official link " + it.get("keywords", ""), body, it["url"], "link", "", "link-" + it["url"], "{}")


# Checked by hand against the official website on 9 October 2026 (the pages do not state a total number of departments).
FACTS = [
    ("AWKUM faculties and constituent colleges",
     "faculties colleges how many number count total faculty list university structure pca pakhtunkhwa college of arts cahvs veterinary",
     "Abdul Wali Khan University Mardan has 5 faculties: Faculty of Arts & Humanities; Faculty of Business & Economics; "
     "Faculty of Chemical & Life Sciences; Faculty of Physical & Numerical Sciences; Faculty of Social Sciences. "
     "It also has two constituent colleges: the College of Animal Husbandry & Veterinary Sciences (CAHVS) and Pakhtunkhwa College of Arts (PCA). "
     "The website does not publish one official total for the number of departments; the departments are listed faculty by faculty. "
     "(https://awkum.edu.pk/faculties-colleges/)", "https://awkum.edu.pk/faculties-colleges/"),
    ("Departments of the Faculty of Physical & Numerical Sciences",
     "departments faculty physical numerical sciences computer science geology mathematics physics statistics campus garden",
     "The Faculty of Physical & Numerical Sciences has 5 departments: Computer Science, Geology, Mathematics, Physics, Statistics. "
     "The Department of Computer Science at the Garden Campus, Mardan has its own page "
     "(https://awkum.edu.pk/faculties-colleges/faculty-of-physical-numerical-sciences/cs-garden/). "
     "Each department site has a 'Chairperson Message' page and a 'People' page, which is where the chairperson is named. "
     "(https://awkum.edu.pk/faculties-colleges/faculty-of-physical-numerical-sciences/)",
     "https://awkum.edu.pk/faculties-colleges/faculty-of-physical-numerical-sciences/"),
]


def fact_rows():
    for title, meta, body, url in FACTS:
        yield (title, meta, body, url, "department", "", "fact-" + url + title, "{}")


def directory_rows(docs):
    """'How many departments?' / 'which departments are in the faculty of X?' are not answered by any single web page,
    so build one overview from the website's own folder structure: faculties-colleges/<faculty>/<department>/..."""
    from urllib.parse import urlparse
    found = {}                                                  # (faculty, department) -> (path depth, title, url)
    for d in docs:
        fac, dep = d.get("faculty"), d.get("department")
        if not (fac and dep):
            continue
        depth = len([p for p in urlparse(d["url"]).path.split("/") if p])
        title = re.sub(r"\s+", " ", d.get("title", "")).strip()
        if (fac, dep) not in found or depth < found[(fac, dep)][0]:
            found[(fac, dep)] = (depth, title, d["url"])
    if not found:
        return
    by_fac = {}
    for (fac, dep), (_, title, url) in sorted(found.items()):
        name = title if re.search(r"department|institute|centre|center|college|school|faculty", title, re.I) else words(dep).title()
        by_fac.setdefault(fac, []).append((name, url))
    meta = "departments list all how many number count total faculties colleges institutes which departments does the university have"
    summary = ["Pages found on the website under each faculty or college (names can be departments, campuses, programs, labs or sections, "
               "so this is a guide to what exists, not an official count):"]
    summary += [f"{words(f).title()}: " + "; ".join(n for n, _ in v) for f, v in by_fac.items()]
    text = "\n".join(summary)
    for i, ch in enumerate(split_text(text)):
        yield ("List of AWKUM faculties and departments (overview)", meta, ch, "https://awkum.edu.pk/faculties-colleges/", "department",
               "", "directory-summary", json.dumps({"chunk": i}))
    for f, v in by_fac.items():
        body = f"Departments and units of {words(f).title()}:\n" + "\n".join(f"{n} ({u})" for n, u in v)
        for i, ch in enumerate(split_text(body)):
            yield (f"Departments of {words(f).title()}", meta + " " + words(f), ch, v[0][1], "department", "", "directory-" + f, json.dumps({"chunk": i}))


def main():
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    try:
        con.execute("""CREATE VIRTUAL TABLE chunks USING fts5(
            title, meta, body, url UNINDEXED, category UNINDEXED, published UNINDEXED, doc_id UNINDEXED, extra UNINDEXED, raw UNINDEXED,
            tokenize = 'porter unicode61 remove_diacritics 2')""")
    except sqlite3.OperationalError as e:
        sys.exit(f"This Python build has no SQLite FTS5 support ({e}). Install Python from python.org and try again.")
    n, by_cat = 0, Counter()
    ins = "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)"

    def add(row):
        # Search runs on the text WITHOUT web addresses (otherwise "BA" matches any link that contains "-BA-");
        # the original text, with its links, is kept in `raw` and is what the chatbot reads.
        searchable = re.sub(r"\s+", " ", re.sub(r"https?://\S+|mailto:\S+", " ", row[2]))
        con.execute(ins, (row[0], row[1], searchable, *row[3:], row[2]))
    all_docs = []
    for f in sorted(DOCS.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if other_campus(d.get("url"), d.get("title"), d.get("department")):
            continue                                      # page of another campus
        all_docs.append(d)
        for row in rows_for(d):
            add(row); n += 1; by_cat[row[4]] += 1
    for row in list(fact_rows()) + list(directory_rows(all_docs)):
        add(row); n += 1; by_cat["department"] += 1
    for row in curated_rows():
        add(row); n += 1; by_cat["link"] += 1
    con.commit(); con.close()
    print(f"Index built: {n} chunks from {len(list(DOCS.glob('*.json')))} documents -> {DB} ({DB.stat().st_size // 1024} KB)")
    for c, k in by_cat.most_common():
        print(f"  {c}: {k}")


if __name__ == "__main__":
    main()

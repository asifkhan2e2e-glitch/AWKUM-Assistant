"""
Search the index.  Used by the chatbot, and handy for testing retrieval by hand:

    python indexer/search.py "BSCS software merit list"
    python indexer/search.py "hostel" --cat hostel -k 5
"""
import argparse, json, re, sqlite3, sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = next((p / "data" / "index.sqlite" for p in (HERE, HERE.parent) if (p / "data" / "index.sqlite").exists()),
          HERE.parent / "data" / "index.sqlite")

STOP = set("""a an the of to in on for and or is are was were be been being what which who whom whose how when where why do does did
can could should would will shall may might i me my we our you your it its this that these those about at by from with as tell
please give show have has had any there their them kindly want need know get""".split())
# Things that go out of date: newer items should rank higher. Departments, fees pages etc. are evergreen.
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
# Low-value for student questions: pushed down unless the question is clearly about them.
NOISY = {"jobs": {"job", "jobs", "vacancy", "vacancies", "hiring", "recruitment", "interview", "visiting", "position", "positions",
                  "career", "careers", "lecturer", "advertisement", "walk"},
         "tender": {"tender", "tenders", "bid", "quotation", "procurement", "rfp"},
         "phd_defense": {"defense", "defence", "viva", "dissertation", "public"}}
TIME_BOUND = {"news", "admission", "merit_list", "scholarship", "jobs", "hostel", "exam_result", "tender", "phd_defense"}


def to_fts(query):
    toks = [t for t in re.findall(r"\w+", query.lower()) if t not in STOP and len(t) > 1]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))     # quoted: user text can never break FTS syntax


def _age_days(published):
    try:
        return max(0, (date.today() - date.fromisoformat(published)).days)
    except (TypeError, ValueError):
        return None


def search(query, k=8, category=None, per_doc=2, db=DB):
    q = to_fts(query)
    if not q:
        return []
    words = set(re.findall(r"\w+", query.lower()))
    sql = ("SELECT title, url, category, published, doc_id, extra, raw, bm25(chunks, 5.0, 2.0, 1.0) AS score "
           "FROM chunks WHERE chunks MATCH ? " + ("AND category = ? " if category else "") + "ORDER BY score LIMIT 80")
    con = sqlite3.connect(db)
    rows = con.execute(sql, (q, category) if category else (q,)).fetchall()
    con.close()
    scored = []
    for title, url, cat, pub, doc_id, extra, body, score in rows:
        bonus = 0.0
        if cat in TIME_BOUND and (age := _age_days(pub)) is not None:
            bonus = 0.6 / (1 + age / 120)                              # recent items get up to +60%
        ex = json.loads(extra or "{}")
        tie = 0.002 * ROMAN.get(ex.get("list_no", ""), 0)               # of equal merit lists, the higher number (newer) first
        penalty = 0.4 if cat in NOISY and not (words & NOISY[cat]) else 1.0
        scored.append((-score * (1 + bonus + tie) * penalty, dict(title=title, url=url, category=cat, published=pub,
                                                  doc_id=doc_id, body=body, extra=ex)))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], {}
    for s, r in scored:
        row = r["extra"].get("row")                                       # fee rows: each program is its own result
        key = (r["doc_id"], row) if row else (r["url"] if r["category"] == "merit_list" else r["doc_id"])   # every merit list (I, II, III...) is wanted
        if seen.get(key, 0) >= per_doc:
            continue
        seen[key] = seen.get(key, 0) + 1
        r["score"] = round(s, 2)
        out.append(r)
        if len(out) == k:
            break
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--cat")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--full", action="store_true", help="print the whole text of each result")
    a = ap.parse_args()
    if not Path(DB).exists():
        sys.exit("No index yet. Run: python indexer/build_index.py")
    for i, r in enumerate(search(a.query, a.k, a.cat), 1):
        print(f"{i}. [{r['category']}] {r['published']}  {r['title']}  (score {r['score']})\n   {r['url']}\n   {(r['body'] if a.full else r['body'][:160])!r}\n")

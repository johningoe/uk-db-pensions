"""Automated review of flagged sponsor matches.

Decisions live in review_decisions (one per scheme) and are re-applied after rematching, as long as the
scheme still points at the company the decision was made about. Sources, strongest first:
  manual   - imported from the reviewer_decision column of review_queue.csv
  ai       - adjudicated row by row from ai_queue.csv (see export_ai_queue / import_decisions)
  rule:*   - the corroboration and plausibility rules below
"""
import csv
import sqlite3

from . import config as C
from .match import Matcher, _words_agree, upsert_sponsor
from .names import core, norm

# A scheme with 100+ members belongs to a business that trades. These categories suggest a name-holder.
WEAK_ACCOUNTS = {"DORMANT", "NO ACCOUNTS FILED", "ACCOUNTS TYPE NOT AVAILABLE", "NO ACCOUNTS TYPE AVAILABLE", "NULL", ""}
REGISTER_DATE = "2020-03-16"
SUBSTANTIAL = {"FULL", "GROUP", "MEDIUM", "SMALL", "AUDIT EXEMPTION SUBSIDIARY"}    # a new one may be a successor
AI_QUEUE = C.DATA / "ai_queue.csv"
PRIORITY = {"manual": 3, "ai": 2}

DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_decisions (
    psr                 TEXT PRIMARY KEY,
    reviewed_company    TEXT,       -- the sponsor_company_number the decision is about
    decision            TEXT,       -- accept / switch / reject
    new_company_number  TEXT,       -- for switch
    method              TEXT,       -- manual / ai / rule:<name>
    reason              TEXT,
    decided_at          TEXT DEFAULT (datetime('now'))
);
"""


def init(con):
    con.executescript(DECISIONS_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(schemes)")}
    for c in ("review_method", "review_reason"):
        if c not in cols:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {c} TEXT")
    con.commit()


def _acc(cat):
    return (cat or "").upper().replace("-", " ").strip()


def _iso(d):
    if d and len(d) == 10 and d[2] == "/":
        return f"{d[6:]}-{d[3:5]}-{d[:2]}"
    return d


def _outward(pc):
    pc = (pc or "").replace(" ", "").upper()
    return pc[:-3] if len(pc) > 4 else None


def _profile(m, sponsors, num):
    """Company facts from the live snapshot, else from sponsors (dissolved companies only exist there)."""
    r = m.company(num) or sponsors.get(num)
    if not r:
        return None
    r = dict(r)
    r["acc"] = _acc(r.get("accounts_category"))
    r["inc"] = _iso(r.get("incorporation_date"))
    r["trading"] = r["acc"] not in WEAK_ACCOUNTS and not (r["inc"] and r["inc"] > REGISTER_DATE)
    r["db_period_end"] = sponsors[num]["db_period_end"] if num in sponsors else None
    return r


def _alternatives(m, cand, exclude):
    """Live companies whose core name is the sponsor name or a standard variant of it."""
    cc = core(norm(cand or ""))
    if not cc:
        return []
    nums = []
    for v in [cc, *m._variants(cc)]:
        nums += [r["company_number"] for r in m._rows("name_core", v) if not r["is_previous"]]
    return [n for n in dict.fromkeys(nums) if n != exclude]


def _evidence(s, co, ppf_co):
    ev = []
    if s["post_code"] and _outward(s["post_code"]) and co.get("reg_post_code"):
        if s["post_code"].replace(" ", "").upper() == co["reg_post_code"].replace(" ", "").upper():
            ev.append("postcode")
        elif _outward(s["post_code"]) == _outward(co["reg_post_code"]):
            ev.append("district")
    if co.get("db_period_end"):
        ev.append("db_figures")
    if ppf_co:
        a, b = core(norm(ppf_co)), core(norm(co.get("company_name") or ""))
        if a and b and (a == b or _words_agree(a, b)):
            ev.append("ppf_name")
    return ev


def auto_review(con):
    """Rule decisions for flagged matches that have no decision yet. Returns {rule: count}."""
    init(con)
    m = Matcher()
    con.row_factory = sqlite3.Row
    sponsors = {r["company_number"]: r for r in con.execute("SELECT * FROM sponsors")}
    rows = con.execute("""
        SELECT s.psr, s.scheme_name, s.post_code, s.sponsor_name_derived, s.sponsor_company_number, s.match_method,
               (SELECT company_name FROM ppf_schemes p WHERE p.scheme_name = s.ppf_scheme_name) AS ppf_co
        FROM schemes s
        WHERE s.needs_review = 1 AND s.sponsor_company_number IS NOT NULL
          AND s.psr NOT IN (SELECT psr FROM review_decisions)""").fetchall()
    con.row_factory = None
    out, counts = [], {}
    for s in rows:
        cur = _profile(m, sponsors, s["sponsor_company_number"])
        if not cur:
            continue
        ev = _evidence(s, cur, s["ppf_co"])
        if s["match_method"] == "postcode":
            ev = [e for e in ev if e not in ("postcode", "district")]      # that is how it was found
        single = len(core(norm(s["sponsor_name_derived"] or "")).split()) <= 1
        alts = [a for a in (_profile(m, sponsors, n) for n in
                            _alternatives(m, s["sponsor_name_derived"], cur["company_number"])) if a]
        for a in alts:
            a["ev"] = _evidence(s, a, s["ppf_co"])
        strong = {"postcode", "db_figures", "ppf_name"}
        decision = None
        if strong & set(ev):
            decision = ("accept", None, "rule:corroborated", f"{', '.join(ev)} agree")
        else:
            better = [a for a in alts if strong & set(a["ev"])]
            local = [a for a in alts if a["trading"] and "district" in a["ev"]]
            if len(better) == 1:
                b = better[0]
                decision = ("switch", b["company_number"], "rule:better_candidate",
                            f"{b['company_name']} has {', '.join(b['ev'])}; matched company has none")
            elif not cur["trading"] and len(local) == 1 and "district" not in ev:
                b = local[0]
                decision = ("switch", b["company_number"], "rule:trading_sibling",
                            f"matched company is {cur['acc'].lower() or 'not filing'}; {b['company_name']} "
                            f"trades in the scheme's postcode district")
            elif (cur["inc"] and cur["inc"] > REGISTER_DATE and cur["acc"] not in SUBSTANTIAL
                  and not any(a["trading"] for a in alts)):
                decision = ("reject", None, "rule:too_new",
                            f"incorporated {cur['inc']}, after the register date, and no older trading company "
                            "of that name")
            elif cur["trading"] and "district" in ev and not any("district" in a["ev"] for a in alts):
                decision = ("accept", None, "rule:local_trading",
                            f"trading ({cur['acc'].lower()}) and registered in the scheme's postcode district")
            elif (cur["trading"] and not single and s["match_method"] in ("exact", "exact_core")
                  and not any(a["trading"] for a in alts)):
                decision = ("accept", None, "rule:unique_trading",
                            "multi-word exact name; the only trading company of that name")
        if decision:
            out.append((s["psr"], s["sponsor_company_number"], *decision))
            counts[decision[2]] = counts.get(decision[2], 0) + 1
    con.executemany("""INSERT INTO review_decisions (psr, reviewed_company, decision, new_company_number, method, reason)
                       VALUES (?,?,?,?,?,?)""", out)
    con.commit()
    return counts


def apply_decisions(con):
    """Write decisions onto schemes. Only applies where the scheme still points at the reviewed company."""
    init(con)
    m = Matcher()
    rows = con.execute("""SELECT d.psr, d.decision, d.new_company_number, d.method, d.reason
                          FROM review_decisions d JOIN schemes s ON s.psr = d.psr
                          WHERE s.sponsor_company_number = d.reviewed_company""").fetchall()
    n = {"accept": 0, "switch": 0, "reject": 0}
    for psr, decision, new, method, reason in rows:
        if decision == "accept":
            con.execute("""UPDATE schemes SET needs_review=0, match_confidence=max(match_confidence, 0.85),
                           review_method=?, review_reason=? WHERE psr=?""", (method, reason, psr))
        elif decision == "switch":
            co = m.company(new)
            if not co:
                continue
            upsert_sponsor(con, co)
            con.execute("""UPDATE schemes SET sponsor_company_number=?, needs_review=0, match_confidence=0.85,
                           review_method=?, review_reason=? WHERE psr=?""", (new, method, reason, psr))
        elif decision == "reject":
            con.execute("""UPDATE schemes SET sponsor_company_number=NULL, match_method='rejected', match_confidence=0,
                           needs_review=0, review_method=?, review_reason=? WHERE psr=?""", (method, reason, psr))
        n[decision] += 1
    con.execute("DELETE FROM sponsors WHERE company_number NOT IN "
                "(SELECT sponsor_company_number FROM schemes WHERE sponsor_company_number IS NOT NULL)")
    con.commit()
    return n


def export_ai_queue(con, path=AI_QUEUE, priority_only=True):
    """Flagged matches the rules could not settle, with the facts needed to judge them and up to 5 alternatives."""
    init(con)
    m = Matcher()
    con.row_factory = sqlite3.Row
    sponsors = {r["company_number"]: r for r in con.execute("SELECT * FROM sponsors")}
    extra = """AND s.likely_live = 1 AND s.name_type <> 'DC_likely'""" if priority_only else ""
    rows = con.execute(f"""
        SELECT s.psr, s.scheme_name, s.post_code, s.city, s.sponsor_name_derived, s.sponsor_company_number,
               s.match_method FROM schemes s
        WHERE s.needs_review = 1 AND s.sponsor_company_number IS NOT NULL {extra}
          AND s.psr NOT IN (SELECT psr FROM review_decisions) ORDER BY s.psr""").fetchall()
    con.row_factory = None

    def desc(p):
        return (f"{p['company_number']} {p['company_name']} [{(p.get('company_status') or '').lower()}; "
                f"{p['acc'].lower() or '?'}; inc {(p['inc'] or '?')[:4]}; {p.get('reg_post_code') or '?'}; "
                f"SIC {(p.get('sic_1') or '?')[:40]}]")

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["psr", "scheme", "scheme_postcode", "derived_sponsor", "method", "matched", "alternatives",
                    "decision", "new_company_number", "reason"])
        for s in rows:
            cur = _profile(m, sponsors, s["sponsor_company_number"])
            alts = [a for a in (_profile(m, sponsors, n) for n in
                                _alternatives(m, s["sponsor_name_derived"], s["sponsor_company_number"])) if a][:5]
            w.writerow([s["psr"], s["scheme_name"], f"{s['post_code'] or ''} {s['city'] or ''}".strip(),
                        s["sponsor_name_derived"], s["match_method"], desc(cur) if cur else s["sponsor_company_number"],
                        " | ".join(desc(a) for a in alts), "", "", ""])
    return len(rows)


def import_decisions(con, path, method):
    """Read decision / new_company_number / reason columns (ai_queue.csv) or reviewer_decision (review_queue.csv:
    accept, reject, or a company number to switch to). Higher-priority sources are never overwritten."""
    init(con)
    n = 0
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            d = (r.get("decision") or r.get("reviewer_decision") or "").strip().lower()
            if not d:
                continue
            new = (r.get("new_company_number") or "").strip().upper() or None
            if d not in ("accept", "reject", "switch"):
                d, new = "switch", d.upper()                # a bare company number
            if d == "switch" and not new:
                continue
            cur = con.execute("SELECT sponsor_company_number FROM schemes WHERE psr=?", (r["psr"],)).fetchone()
            old = con.execute("SELECT method FROM review_decisions WHERE psr=?", (r["psr"],)).fetchone()
            if not cur or (old and PRIORITY.get(old[0], 0) > PRIORITY.get(method, 0)):
                continue
            con.execute("""INSERT OR REPLACE INTO review_decisions
                           (psr, reviewed_company, decision, new_company_number, method, reason)
                           VALUES (?,?,?,?,?,?)""", (r["psr"], cur[0], d, new, method, (r.get("reason") or "").strip()))
            n += 1
    con.commit()
    return n


def run(con):
    counts = auto_review(con)
    for path, method in ((C.DATA / "review_queue.csv", "manual"), (AI_QUEUE, "ai")):
        if path.exists():
            print(f"Imported {import_decisions(con, path, method):,} {method} decisions from {path.name}")
    applied = apply_decisions(con)
    print("Review rules: " + ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items())))
    print(f"Applied: {applied}")

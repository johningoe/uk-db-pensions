"""Second look at the sponsor names derived from unmatched schemes' names.

The name passes take the sponsor from the scheme name, which often leaves scheme wording in ("Direct Line Group
Hybrid", "Network Rail CARE", "Ford Salaried Contributory"), policy references ("... 93/M01/6012"), office tags
("Lazard London") or nothing usable at all ("Pension And Life Assurance Plan Of Furmanite International Limited").

  clean(name)   rule-based trim of that wording
  apply(con)    for live schemes still without a sponsor: the hand verdict from sponsor_name_review.csv if there is
                one, else the rule-based trim. Sets schemes.sponsor_name_review (corrected / ok / a reason it has no
                company sponsor) and puts a corrected name in sponsor_name_derived (the original goes in
                sponsor_name_original)
  rematch(con)  looks the corrected names up in the Companies House snapshot and writes the candidates to
                data/sponsor_rematch_queue.csv for review: a brand on its own often hits a dormant or brand-new
                namesake ("Ford" -> FORD LIMITED, dormant, not Ford Motor Company), so nothing is accepted here.
                Verdicts go in review_decisions and are applied by review.apply_decisions.

sponsor_name_review.csv (hand verdicts, by PSR): verdict 'name' gives the right sponsor name; otherwise a reason the
scheme has no company sponsor to find: product (a personal or insurer product, not an employer's scheme),
public_sector, non_company_body (union, livery company, church, chartered, statutory or mutual body, foreign
government), multi_employer, no_usable_name (initials, a generic word or a test record).
"""
import csv
import re
from pathlib import Path

from . import config as C

REVIEW_CSV = Path(__file__).with_name("sponsor_name_review.csv")
QUEUE_CSV = C.DATA / "sponsor_rematch_queue.csv"

# scheme wording left at the end of a derived name ("Life" and "Security" only in scheme phrases: Canada Life and
# Chubb Security are companies)
TRAIL = (r"(?:private|personal|executive|executives|directors?|staff|officers?|senior|supplementary|contributory|"
         r"non-contributory|basic|combined|cash|closed|\(closed\)|\(db\)|\(dc\)|db|dc|hybrid|care|lifestyle|"
         r"money purchase|final salary|defined benefits?|contracted[- ]out|retirements?|retirement saver|"
         r"benefit builder|income and protection|group life|managed|ex|employees?|works|operatives|ancillary|"
         r"vice-presidents|staff security|employee financial security|financial security|provident|equitable life|"
         r"number one|no\.? ?\d+|scheme [a-z]|secton|section|r ?b ?s|rdbs|ops|uk division|london office|london|"
         r"division|\(staff\)|\(uk\)|uk|1?\d{3,4}|\(\d{4}\)|-)")


def clean(name):
    s = re.sub(r"\s+group of companies\b", " Group", (name or "").strip(), flags=re.I)
    s = re.sub(r"^(?:the\s+)?pension (?:and (?:life )?assurance )?(?:plan|scheme) (?:of|for) (?:the\s+)?", "", s,
               flags=re.I)
    s = re.sub(r"\s+[A-Z]{0,4}\d[\w/\-]*\d\w*$", "", s)                      # policy refs: 93/M01/6012, PM33052
    s = re.sub(r"\s+(?:pol(?:icy)?\.? ?no\.?|policy)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s+c/o\b.*$", "", s, flags=re.I)
    prev = None
    while prev != s:
        prev = s
        s = re.sub(rf"\s+{TRAIL}$", "", s, flags=re.I).strip(" -,.")
    s = re.sub(r"\(\s*formerly [^)]*\)", "", s, flags=re.I).strip()
    s = re.sub(r"\s+\d{4}\b", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -,.")


def _review():
    with open(REVIEW_CSV, newline="") as f:
        return {r["psr"]: r for r in csv.DictReader(f)}


def _columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(schemes)")}
    for col in ("sponsor_name_review", "sponsor_name_original"):
        if col not in have:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {col} TEXT")


def apply(con):
    _columns(con)
    review = _review()
    counts = {}
    for psr, name, derived, original in con.execute(
            """SELECT psr, scheme_name, sponsor_name_derived, sponsor_name_original FROM schemes
               WHERE likely_live = 1 AND name_type <> 'DC_likely' AND sponsor_company_number IS NULL
                 AND charity_number IS NULL""").fetchall():
        base = original or derived or name
        r = review.get(psr)
        if r and r["verdict"] != "name":
            verdict, new = r["verdict"], base
        else:
            new = r["sponsor_name"] if r else clean(base)
            verdict = "ok" if new == base else "corrected"
        con.execute("""UPDATE schemes SET sponsor_name_review = ?, sponsor_name_derived = ?,
                           sponsor_name_original = ? WHERE psr = ?""",
                    (verdict, new or base, base if new != base else original, psr))
        counts[verdict] = counts.get(verdict, 0) + 1
    con.commit()
    return counts


def rematch(con, out=QUEUE_CSV):
    """Candidates for schemes whose sponsor name was corrected, for review. Returns the number written."""
    from .match import Matcher, _pc
    m = Matcher()
    rejected = {(p, c) for p, c in con.execute(
        "SELECT psr, reviewed_company FROM review_decisions WHERE decision = 'reject'")}
    rows = []
    for psr, name, new, pc in con.execute(
            """SELECT psr, scheme_name, sponsor_name_derived, post_code FROM schemes
               WHERE sponsor_name_review = 'corrected' AND sponsor_company_number IS NULL""").fetchall():
        log, spc = [], _pc(pc)
        hit = m._exact_pass([new], spc, log, variants=False) or m._exact_pass([new], spc, log, variants=True)
        if not hit:
            hit, _ = m._fuzzy_pass([new], spc, log)
        if not hit or (psr, hit["company_number"]) in rejected:
            continue
        co = m.company(hit["company_number"])
        rows.append([psr, name, new, hit["company_number"], co["company_name"], co["company_status"],
                     co["incorporation_date"], co["accounts_category"], co["reg_post_code"], pc, hit["method"],
                     hit["confidence"], ""])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["psr", "scheme_name", "sponsor_name", "company_number", "company_name", "company_status",
                    "incorporated", "accounts_category", "company_post_code", "scheme_post_code", "method",
                    "confidence", "reviewer_decision"])
        w.writerows(rows)
    return len(rows)

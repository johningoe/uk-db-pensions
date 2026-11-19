"""A review page for the gaps and doubtful figures, and the import of what reviewers decide on it.

    python run.py review-page              # writes data/review.html (the queue, data embedded)
    python run.py review-import FILE       # applies decisions exported from the page's database

The page lists one item per thing a person can settle, in five kinds:

  check    a figure the quality rules flag (one figure only, net only, old, odd ratio, under £1m)
  verify   a figure read by machine of £100m or more, not yet checked by hand
  unread   the sponsor's accounts mention a DB scheme but no figure was read (or the figure failed the checks)
  match    the scheme names a company that was not found (with the rejected candidates, if any)
  live     the sponsor is dissolved, in liquidation or in administration: is the scheme still live?

Reviewers' decisions are saved in the published page's own database (collection `decisions`, one document per
item key). Claude reads them back into a JSON file, and review-import turns each into the record the pipeline
already uses:

  confirm / figures / misread / no_db   rows in hand_checked.csv (register bodies as reg:<source>:<number>)
  sponsor                               review_decisions rows (switch to the company number given)
  reject                                review_decisions rows (the matched company is a namesake: unmatch)
  not_company                           rows in sponsor_name_review.csv (public sector, product, ...)
  not_live                              rows in liveness_overrides.csv, applied by match.flag_status
  skip                                  nothing (looked at, not settled)

Then `python run.py status` and `python run.py web` rebuild the figures and the explorer.
"""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from . import config as C
from . import gaps, handcheck, pdfaccounts, web

TEMPLATE = Path(__file__).with_name("review_template.html")
OUT = C.DATA / "review.html"
LIVENESS_CSV = Path(__file__).with_name("liveness_overrides.csv")
NAME_REVIEW_CSV = Path(__file__).with_name("sponsor_name_review.csv")
DECISIONS_LOG = Path(__file__).with_name("review_page_decisions.json")
CH = "https://find-and-update.company-information.service.gov.uk"
VERIFY_FROM = 100e6


def _doc(num, page=None):
    """Link to the company's latest accounts on Companies House (opening at the page where possible)."""
    if not num or num.startswith("reg:"):
        return None
    item = pdfaccounts.latest_accounts(num) or {}
    tid = item.get("transaction_id")
    if not tid:
        return {"url": f"{CH}/company/{num}/filing-history", "page": None, "filed": None, "made_up": None}
    url = f"{CH}/company/{num}/filing-history/{tid}/document?format=pdf&download=0"
    if page is not None:
        url += f"#page={page + 1}"
    return {"url": url, "page": None if page is None else page + 1, "filed": item.get("date"),
            "made_up": item.get("action_date") or (item.get("description_values") or {}).get("made_up_date")}


def _period(v):
    v = (v or "").strip()
    return (v[:10] if len(v) >= 10 and v[4] == "-" else v) or None


def _m(v):
    return None if v is None else round(v / 1e6, 3)


def queue(con):
    rows = web._rows(con)
    i = {c: n for n, c in enumerate(web.COLS)}
    pdf = {r[0]: r[1:] for r in con.execute("SELECT company_number, page, evidence, filing_date FROM pdf_accounts")}
    items, bodies = {}, {}

    # figures: one item per reporting body (own company, parent group, or register body)
    for r in rows:
        if r[i["group"]] != "with_figures" or r[i["quality"]] in (None, "hand", "tagged"):
            continue
        o, a = r[i["obligation"]], r[i["assets"]]
        big = max(abs(o or 0), abs(a or 0))
        kind = "check" if r[i["quality"]] == "check" else "verify" if big >= VERIFY_FROM else None
        if not kind:
            continue
        src, no = r[i["source"]], r[i["fig_body_no"]]
        ident = f"reg:{r[i['reg']]}:{no}" if src == "register" else no
        key = f"fig:{ident}"
        it = items.get(key)
        if not it:
            where = r[i["fig_where"]] or ""
            doc = None
            if src == "register":
                url = next((w for w in where.split() if w.startswith("http")), None)
                doc = {"url": url, "page": None, "filed": None, "made_up": None} if url else None
            else:
                p = pdf.get(no)
                doc = _doc(no, p[0] if p and p[0] is not None and "pdf" in (where or "pdf") else None)
            it = items[key] = {
                "key": key, "kind": kind, "name": r[i["fig_body"]], "id": ident, "source": src,
                "o": _m(o), "a": _m(a), "period": _period(r[i["period_end"]]),
                "why": r[i["quality_why"]], "where": where, "doc": doc, "val": _m(big), "schemes": []}
        it["schemes"].append([r[i["psr"]], r[i["name"]]])

    # sponsors with no usable figure: unreadable notes, failed checks, dissolved or insolvent
    for r in rows:
        g, reason, co = r[i["group"]], r[i["reason"]], r[i["co"]]
        if g not in ("no_figures", "no_figure_due", "not_public") or not co:
            continue
        if reason.startswith(("Accounts mention", "Figure found")):
            kind, key = "unread", f"unread:{co}"
        elif reason.startswith("Sponsor dissolved"):
            kind, key = "live", f"live:{co}"
        else:
            continue
        it = items.get(key)
        if not it:
            p = pdf.get(co)
            sp = con.execute("""SELECT company_status, accounts_category, db_obligation, db_plan_assets, db_check_note,
                                       dissolution_date FROM sponsors WHERE company_number=?""", (co,)).fetchone() or [None] * 6
            it = items[key] = {
                "key": key, "kind": kind, "name": r[i["co_name"]], "id": co, "source": "own",
                "status": sp[0], "accounts": sp[1], "dissolved": sp[5], "why": reason,
                "evidence": (p[1] or "")[:400] if p else None,
                "flagged": [_m(sp[2]), _m(sp[3]), sp[4]] if reason.startswith("Figure found") else None,
                "doc": _doc(co, p[0] if p else None) if kind == "unread" else
                       {"url": f"{CH}/company/{co}", "page": None, "filed": None, "made_up": None},
                "val": None, "schemes": []}
        it["schemes"].append([r[i["psr"]], r[i["name"]]])

    # schemes naming a company that wasn't found
    from .match import Matcher
    m = Matcher()
    for r in rows:
        if r[i["group"]] != "not_identified" or not r[i["reason"]].startswith("Company or brand"):
            continue
        psr = r[i["psr"]]
        cands = []
        for num, name, score, method in con.execute(
                """SELECT company_number, company_name, max(score), group_concat(DISTINCT method) FROM match_log
                   WHERE psr=? AND company_number IS NOT NULL GROUP BY company_number ORDER BY max(score) DESC LIMIT 4""",
                (psr,)):
            c = m.company(num)
            cands.append({"no": num, "name": name, "score": score, "method": method,
                          "status": c["company_status"] if c else None,
                          "postcode": c["reg_post_code"] if c else None})
        derived = r[i["derived"]] or ""
        items[f"match:{psr}"] = {
            "key": f"match:{psr}", "kind": "match", "name": r[i["name"]], "id": psr, "source": None,
            "derived": derived, "postcode": r[i["postcode"]], "city": r[i["city"]], "cands": cands,
            "search": f"{CH}/search/companies?q={quote_plus(derived or r[i['name']])}",
            "val": None, "schemes": [[psr, r[i["name"]]]]}

    out = list(items.values())
    order = {"check": 0, "verify": 1, "unread": 2, "live": 3, "match": 4}
    out.sort(key=lambda it: (it["val"] is None, -(it["val"] or 0), order[it["kind"]], -len(it["schemes"]), it["name"] or ""))
    return out


def export(con, out=OUT):
    items = queue(con)
    data = {"built": date.today().isoformat(), "items": items}
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    out.write_text(html)
    return len(items), out


# ---------------------------------------------------------------- import decisions

def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _rewrite(path, fields, keyf, new_rows):
    """Replace rows with the same key, append the rest."""
    rows, eol = [], "\n"
    if path.exists():
        eol = "\r\n" if b"\r\n" in path.read_bytes()[:4096] else "\n"     # keep the file's own line endings
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    keys = {keyf(r) for r in new_rows}
    rows = [r for r in rows if keyf(r) not in keys] + new_rows
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator=eol, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def apply(con, path):
    """Apply decisions exported from the page (a JSON list of decision documents). Returns counts by action."""
    from . import review
    docs = json.loads(Path(path).read_text())
    docs = [d.get("data", d) for d in docs]
    items = {it["key"]: it for it in queue(con)}
    hand, names, live, n = [], [], [], {}
    with open(handcheck.CSV, newline="") as f:
        had = {r["company_number"]: r for r in csv.DictReader(f)}     # keeps filing date and page on a re-import
    review.init(con)
    for d in docs:
        key, act = d.get("key"), d.get("action")
        it = items.get(key) or {}
        ident = d.get("id") or it.get("id")
        psrs = d.get("schemes") or [s[0] for s in it.get("schemes", [])]
        note = (d.get("note") or "").strip()
        tag = f"review page {(d.get('at') or '')[:10]}".strip()
        if act == "confirm" and not it:
            n["already applied"] = n.get("already applied", 0) + 1   # confirmed earlier: the item has left the queue
            continue
        if act in ("confirm", "figures", "misread", "no_db") and ident:
            o, a = (_num(d.get("obligation_m")), _num(d.get("assets_m"))) if act == "figures" else \
                   (it.get("o"), it.get("a")) if act == "confirm" else (None, None)
            if act == "figures" and o is None and a is None:
                continue
            what = {"confirm": "Figures confirmed", "figures": "Figures entered", "misread": "Figure misread",
                    "no_db": "Accounts show no DB scheme"}[act]
            doc = it.get("doc") or {}
            prev = had.get(ident) or {}
            hand.append({"company_number": ident, "company_name": it.get("name") or d.get("name") or "",
                         "period_end": d.get("period_end") or (it.get("period") if act == "confirm" else "") or "",
                         "currency": "GBP", "obligation_m": "" if o is None else o, "assets_m": "" if a is None else a,
                         "basis": "misread" if act in ("misread", "no_db") else d.get("basis") or "group",
                         "filing_date": doc.get("filed") or prev.get("filing_date") or "",
                         "page": d.get("page") or doc.get("page") or prev.get("page") or "",
                         "note": f"{what} on the {tag}" + (f": {note}" if note else "")})
        elif act == "sponsor" and d.get("company_number"):
            new = d["company_number"].strip().upper().zfill(8)
            for psr in psrs:
                cur = con.execute("SELECT sponsor_company_number FROM schemes WHERE psr=?", (psr,)).fetchone()
                con.execute("""INSERT OR REPLACE INTO review_decisions (psr, reviewed_company, decision,
                               new_company_number, method, reason, decided_at) VALUES (?,?,?,?,?,?,?)""",
                            (psr, cur[0] if cur else None, "switch", new, "review_page", note or tag,
                             d.get("at") or datetime.now(timezone.utc).isoformat()))
        elif act == "reject":
            for psr in psrs:
                cur = con.execute("SELECT sponsor_company_number FROM schemes WHERE psr=?", (psr,)).fetchone()
                if cur and cur[0]:
                    con.execute("""INSERT OR REPLACE INTO review_decisions (psr, reviewed_company, decision,
                                   new_company_number, method, reason, decided_at) VALUES (?,?,?,?,?,?,?)""",
                                (psr, cur[0], "reject", None, "review_page", note or tag,
                                 d.get("at") or datetime.now(timezone.utc).isoformat()))
        elif act == "not_company" and d.get("verdict") in gaps.REVIEW:
            for psr, name in it.get("schemes", [[p, ""] for p in psrs]):
                names.append({"psr": psr, "scheme_name": name, "verdict": d["verdict"], "sponsor_name": ""})
        elif act == "not_live":
            for psr in psrs:
                live.append({"psr": psr, "status": d.get("reason") or "not_live", "note": note, "decided": tag})
        else:
            continue
        n[act] = n.get(act, 0) + 1
    con.commit()
    if hand:
        _rewrite(handcheck.CSV, ["company_number", "company_name", "period_end", "currency", "obligation_m",
                                 "assets_m", "basis", "filing_date", "page", "note"], lambda r: r["company_number"], hand)
    if names:
        _rewrite(NAME_REVIEW_CSV, ["psr", "scheme_name", "verdict", "sponsor_name"], lambda r: r["psr"], names)
    if live:
        _rewrite(LIVENESS_CSV, ["psr", "status", "note", "decided"], lambda r: r["psr"], live)
    # keep the decisions as a record (merged by item key, newest wins)
    log = {}
    if DECISIONS_LOG.exists():
        log = {d["key"]: d for d in json.loads(DECISIONS_LOG.read_text())}
    log.update({d["key"]: {k: v for k, v in d.items() if k != "by"} for d in docs if d.get("key")})
    DECISIONS_LOG.write_text(json.dumps(sorted(log.values(), key=lambda d: d["key"]), indent=1, ensure_ascii=False) + "\n")
    return n


def liveness_overrides():
    if not LIVENESS_CSV.exists():
        return {}
    with open(LIVENESS_CSV, newline="") as f:
        return {r["psr"]: r["status"] for r in csv.DictReader(f)}

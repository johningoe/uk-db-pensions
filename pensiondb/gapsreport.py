"""The gaps: every live DB scheme with no usable figure, and every scheme still a candidate, with why and what next.

One row per sponsor where the employer is identified (a company or a register body), one row per scheme where it
isn't. Each row says which group it is in (gaps.classify), its band and reason (band.build), what has been tried so
far, and the next step. Rows are either open (candidates: work could settle them) or assumed (out of the band on an
assumption that a sample can test, such as "small accounts mean a small scheme").

    reports/gaps.csv     the rows
    data/gaps.html       the same rows as a page, with counts and filters (published as an artifact)

    python run.py gaps          # also part of run.py status
"""
import csv
import json
import random
from collections import Counter
from datetime import date
from pathlib import Path

from . import config as C
from . import gaps

ROOT = Path(__file__).resolve().parent.parent
CSV_OUT = ROOT / "reports" / "gaps.csv"
HTML_OUT = C.DATA / "gaps.html"
TEMPLATE = Path(__file__).with_name("gaps_template.html")
DECISIONS = Path(__file__).with_name("review_page_decisions.json")
SAMPLE = 40            # rows per assumption picked (fixed seed) to test it
FIELDS = ["status", "group", "band", "reason", "sponsor_id", "sponsor", "schemes", "scheme_names", "psrs",
          "accounts", "tried", "next_step", "detail"]

NO_SIZE = {"No figure yet, employer files full accounts", "Charity or other body, no figure yet"}
GROUP_TEXT = {"with_figures": "Figures, not settled", "no_figures": "Identified, no figures",
              "not_identified": "Not identified"}

# reasons that are out of the band on an assumption, and the assumption
ASSUMED = {
    "Employer files small, micro or no accounts": "Small, micro or no accounts carry no pension note; assumed too small "
                                                  "for £50m of DB assets",
    "No employer found, no DB evidence": "Nothing says the scheme is DB (name only, no PPF/FAS listing); assumed "
                                         "DC or gone",
    "Small charity or body": "Small charity or body; assumed too small",
    "Member of a multi-employer scheme": "Figures sit with the multi-employer scheme, not the employer",
}


def _decisions():
    """Latest review-page note per company or scheme id."""
    if not DECISIONS.exists():
        return {}
    out = {}
    for d in sorted(json.load(open(DECISIONS)), key=lambda d: d.get("at") or ""):
        if d.get("id") and (d.get("note") or d.get("action")):
            out[d["id"]] = f"{d.get('action', '')}: {d.get('note') or ''}".strip(": ")
    return out


def _next_step(group, kind, pdf, chk, method, review):
    """(tried, next step) for a row."""
    pdf = pdf or ""
    tried = []
    if group == "not_identified":
        tried.append({"rejected": "a Companies House match was found and rejected on review",
                      "unmatched": "no Companies House or register match by name, core name or postcode",
                      "non_company": "set aside as a public-sector or non-company body",
                      "multi_employer": "matched only to an umbrella scheme"}.get(method, f"match method: {method}"))
        if kind == "Public sector (scope to decide)":
            return tried, "Decide whether public-sector schemes are in scope"
        if kind == "Employer not found, DB evidence":
            if method == "rejected":
                return tried, "Find the right employer: former name, brand or parent of the rejected company"
            return tried, "Find the employer: search Companies House for the brand or former name, or the trustee company"
        return tried, "None while the assumption holds; check the sample to test it"
    if pdf:
        tried.append({"mentioned_no_figures": "accounts read; DB scheme mentioned but no figure could be read",
                      "figures": "figures read from the accounts PDF",
                      "no_filing": "no accounts PDF available to read",
                      "group_plan": "accounts say the scheme is reported by another group company",
                      "dc_only": "accounts read; only DC schemes found",
                      "no_pension_note": "accounts read; no pension note found",
                      "multi_employer": "accounts say it is a multi-employer scheme"}.get(pdf, f"PDF status: {pdf}"))
    if chk and chk.startswith("flag"):
        tried.append(f"figure rejected by the checks ({chk.split(':', 1)[1]})")
    if review:
        tried.append(f"review page: {review}")
    if kind == "No figure yet, employer files full accounts":
        step = {"mentioned_no_figures": "Read the pension note from the page images (scanned or unreadable table)",
                "figures": "Re-read the pension note by eye: the figures read were rejected",
                "group_plan": "Find the group company that reports the scheme and re-point it",
                "dc_only": "Check whether the DB scheme moved to another group company",
                "no_pension_note": "Check whether the DB scheme moved to another group company",
                "no_filing": "Fetch the latest full accounts and read the pension note"}.get(
            pdf, "Fetch the latest full accounts and read the pension note")
        return tried, step
    return tried, {
        "Charity or other body, no figure yet": "Read the body's accounts (register filing or own website)",
        "Figure failed the checks": "Check the flagged figure against the accounts",
        "Parent group's figure only": "Read the sponsor's own accounts for its share",
        "Accounts read, no gross figure": "Find gross figures (scheme's own report and accounts, or the accounts PDF)",
        "Employer files small, micro or no accounts": "None while the assumption holds; check whether a parent "
                                                      "group reports the scheme",
        "Small charity or body": "None while the assumption holds",
        "Member of a multi-employer scheme": "None: size the umbrella scheme instead",
    }.get(kind, "None")


def build(con):
    status = gaps.classify(con)
    rows = con.execute("""
        SELECT s.psr, s.scheme_name, s.band, s.band_kind, s.band_reason, s.sponsor_company_number, sp.company_name,
               sp.accounts_category, sp.db_pdf_status, sp.db_check, sp.db_check_note, s.match_method,
               s.charity_source, s.charity_number, s.charity_name, s.sponsor_name_derived
        FROM schemes s LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number""").fetchall()
    dec = _decisions()
    units = {}
    for (psr, sname, band, kind, breason, co, coname, acc, pdf, chk, chknote, method, rsrc, rno, rname,
         derived) in rows:
        group = status[psr][0]
        if group in ("not_live", "dc"):
            continue
        if group == "with_figures" and band != "candidate":
            continue
        if co:
            key, sid, sponsor = ("co", co), co, coname
        elif rno:
            key, sid, sponsor = ("reg", rsrc, rno), f"{rsrc}:{rno}", rname
        else:
            key, sid, sponsor = ("scheme", psr), "", derived or ""
        u = units.setdefault(key, {"group": group, "band": band, "reason": kind, "detail": breason, "sponsor_id": sid,
                                   "sponsor": sponsor or "", "psrs": [], "names": [], "accounts": acc or "",
                                   "pdf": pdf, "chk": chk, "chknote": chknote, "method": method})
        u["psrs"].append(psr)
        u["names"].append(sname)
        if band == "candidate" and u["band"] != "candidate":       # a sponsor's open scheme makes the row open
            u.update(band=band, reason=kind, detail=breason, group=group)
    out = []
    for key, u in units.items():
        review = dec.get(u["sponsor_id"]) or next((dec[p] for p in u["psrs"] if p in dec), "")
        tried, step = _next_step(u["group"], u["reason"], u["pdf"], u["chk"], u["method"], review)
        if u["chknote"] and u["reason"] == "Figure failed the checks":
            tried.append(u["chknote"])
        open_ = u["band"] == "candidate"
        # gaps.classify counts a named parent or a charity's net-only annual return as figures; the band found no size
        group = "no_figures" if u["reason"] in NO_SIZE else u["group"]
        out.append({"status": "open" if open_ else ("assumed" if u["reason"] in ASSUMED else "explained"),
                    "group": GROUP_TEXT[group], "band": u["band"], "reason": u["reason"],
                    "sponsor_id": u["sponsor_id"], "sponsor": u["sponsor"], "schemes": len(u["psrs"]),
                    "scheme_names": " | ".join(u["names"]), "psrs": " ".join(u["psrs"]), "accounts": u["accounts"],
                    "tried": "; ".join(t for t in tried if t), "next_step": step,
                    "detail": u["detail"] if u["reason"] not in ASSUMED else ASSUMED[u["reason"]]})
    # a fixed sample of each assumption, to test it by hand
    rnd = random.Random(20261117)
    for reason in ASSUMED:
        pool = [r for r in out if r["status"] == "assumed" and r["reason"] == reason]
        for r in rnd.sample(pool, min(SAMPLE, len(pool))):
            r["status"] = "sample"
            r["next_step"] = "Check this one to test the assumption: " + r["next_step"][0].lower() + r["next_step"][1:]
    order = {"open": 0, "sample": 1, "assumed": 2, "explained": 3}
    out.sort(key=lambda r: (order[r["status"]], r["reason"], -r["schemes"], r["sponsor"] or r["scheme_names"]))
    CSV_OUT.parent.mkdir(exist_ok=True)
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(out)
    _page(out)
    n = Counter(r["status"] for r in out)
    return {"rows": len(out), **n}


def _page(rows):
    data = json.dumps({"built": date.today().isoformat(), "rows": rows}, ensure_ascii=False)
    HTML_OUT.write_text(TEMPLATE.read_text().replace("/*DATA*/null", data.replace("</", "<\\/")))

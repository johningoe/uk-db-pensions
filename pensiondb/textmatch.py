"""Match schemes to the company whose saved accounts name them.

Accounts usually name the schemes a company sponsors, so an unmatched scheme whose full name appears in exactly one
company's accounts text (PDF/OCR text or latest iXBRL filing) is very likely that company's: often a scheme that
passed to a new owner, e.g. "Babtie Group Limited Superannuation and Life Assurance Scheme" in Jacobs UK's accounts.
  1 company                      -> accounts_text, confidence 0.85
  2-3 companies, and exactly one shares the scheme's distinctive word -> that one, 0.8
  4+ companies                   -> an industry-wide multi-employer scheme: match_method 'multi_employer', no sponsor
Only distinctive names are searched (3+ words, at least one beyond pension boilerplate), whole-phrase.
"""
import collections
import glob
import html
import os
import re

from . import config as C
from .names import norm

GENERIC = set("THE AND OF FOR STAFF PENSION PENSIONS SCHEME PLAN FUND RETIREMENT BENEFITS BENEFIT LIFE ASSURANCE DEATH "
              "SUPERANNUATION GROUP SECTION EMPLOYEES EXECUTIVE WORKS COMPANY LIMITED PLC UK TRUST SALARIED OFFICERS "
              "NO DEFINED CONTRIBUTION FINAL SALARY SAVINGS SENIOR MANAGEMENT".split())
MULTI = 4


def _targets(con):
    return con.execute("""SELECT psr, scheme_name FROM schemes WHERE sponsor_company_number IS NULL
        AND charity_number IS NULL AND match_method IN ('unmatched', 'rejected') AND likely_live = 1
        AND name_type <> 'DC_likely'""").fetchall()


def _keys(rows):
    keys = {}
    for psr, name in rows:
        k = norm(re.sub(r"\s*\b[A-Z]{0,3}\d{4,}[A-Za-z]?\b\s*$", "", name))     # trailing reference numbers
        toks = k.split()
        dist = [t for t in toks if t not in GENERIC and not t.isdigit()]
        if len(toks) >= 3 and dist and len(" ".join(dist)) >= 5:
            keys[psr] = (k, max(dist, key=len), dist)
    return keys


def _texts():
    """{company_number: [paths]}: saved PDF/OCR texts and each company's latest iXBRL filing."""
    files = collections.defaultdict(list)
    for p in glob.glob(str(C.RAW / "companies_house" / "pdf_accounts" / "*.txt")):
        files[os.path.basename(p).split("_")[0]].append(p)
    latest = {}
    for p in glob.glob(str(C.RAW / "companies_house" / "accounts" / "*" / "*.htm*")):
        b = os.path.basename(p).split("_")
        if len(b) >= 4 and (b[2] not in latest or b[3] > latest[b[2]][0]):
            latest[b[2]] = (b[3], p)
    for num, (_, p) in latest.items():
        files[num].append(p)
    return files


def find(con):
    """{psr: set(company numbers)} for unmatched schemes named in some company's accounts."""
    keys = _keys(_targets(con))
    hits = collections.defaultdict(set)
    for num, paths in _texts().items():
        t = " ".join(open(p, errors="ignore").read() for p in paths)
        if any(p.endswith((".html", ".htm")) for p in paths):
            t = html.unescape(re.sub(r"<[^>]+>", " ", t))
        t = " " + norm(t) + " "
        toks = set(t.split())
        for psr, (k, rare, _) in keys.items():
            if rare in toks and f" {k} " in t:
                hits[psr].add(num)
    return hits, keys


def run(con):
    from .chapi import _get, _profile_row
    from .match import upsert_sponsor
    hits, keys = find(con)
    names = dict(_targets(con))
    counts = collections.Counter()
    for psr, nums in hits.items():
        if len(nums) >= MULTI:
            con.execute("UPDATE schemes SET match_method = 'multi_employer', match_confidence = NULL, needs_review = 0 "
                        "WHERE psr = ?", (psr,))
            counts["multi_employer"] += 1
            continue
        profiles = {n: _get(f"/company/{n}") for n in nums}
        profiles = {n: p for n, p in profiles.items() if p}
        if len(profiles) == 1:
            num, conf = next(iter(profiles)), 0.85
        else:
            dist = set(keys[psr][2])
            share = [n for n, p in profiles.items() if dist & set(norm(p.get("company_name")).split())]
            if len(share) != 1:
                counts["ambiguous"] += 1
                continue
            num, conf = share[0], 0.8
        p = profiles[num]
        upsert_sponsor(con, _profile_row(p), source="api")
        con.execute("""INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number, company_name,
                           score, confidence, accepted, needs_review, notes) VALUES (?,?,?,?,?,?,?,?,1,0,?)""",
                    (psr, names[psr], keys[psr][0], "accounts_text", num, p.get("company_name"), 100, conf,
                     f"scheme named in the accounts of {len(nums)} compan{'y' if len(nums) == 1 else 'ies'}"))
        con.execute("""UPDATE schemes SET sponsor_company_number = ?, match_method = 'accounts_text',
                           match_confidence = ?, needs_review = 0 WHERE psr = ?""", (num, conf, psr))
        counts["matched"] += 1
        counts["matched_active"] += p.get("company_status") == "active"
    con.commit()
    print(f"Accounts-text match: {dict(counts)}")
    return counts

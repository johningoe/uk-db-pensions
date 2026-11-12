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


# ---------------------------------------------------------------- confirming existing matches

# words dropped on both sides before comparing, so "The XYZ Limited Pension Scheme" matches "XYZ Pension Scheme"
STRIP = set("THE LIMITED PLC COMPANY CO AND OF INCORPORATED LLP".split())
PLURAL = {"PENSIONS": "PENSION", "SCHEMES": "SCHEME", "PLANS": "PLAN", "FUNDS": "FUND"}
PENSION_WORDS = set("PENSION RETIREMENT SUPERANNUATION STAFF EXECUTIVE WORKS SENIOR LIFE EMPLOYEES SALARIED "
                    "DEFINED FINAL".split())


def _clean(s):
    return " ".join(PLURAL.get(t, t) for t in s.split() if t not in STRIP)


def _scheme_keys(name):
    """(full key, core key or None): the cleaned scheme name, and the words before its pension wording."""
    toks = _clean(norm(re.sub(r"\s*\b[A-Z]{0,3}\d{4,}[A-Za-z]?\b\s*$", "", name))).split()
    full = " ".join(toks) if len(toks) >= 2 and any(t not in GENERIC for t in toks) else None
    core = []
    for t in toks:
        if t in PENSION_WORDS or t in ("PLAN", "SCHEME", "FUND"):
            break
        core.append(t)
    core = core if core and any(t not in GENERIC and not t.isdigit() and len(t) > 2 for t in core) else None
    return full, (" ".join(core) if core else None)


def _norm_text(paths):
    t = " ".join(open(p, errors="ignore").read() for p in paths)
    if any(p.endswith((".html", ".htm")) for p in paths):
        t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return " " + _clean(norm(t)) + " "


def confirm(con):
    """For each matched live scheme, does the sponsor's saved accounts text (or its parent's) name the scheme?
    schemes.sponsor_named_in_accounts:
      named                 the full scheme name is in the sponsor's accounts
      named_in_parent       ... in a parent company's accounts (sponsor's own accounts don't name it)
      core_named            the scheme's distinctive words appear just before 'pension', 'retirement' or
                            'superannuation' in the sponsor's (or parent's: core_named_in_parent) accounts
      db_unnamed            the sponsor's accounts show a DB scheme but don't name it ("the defined benefit
                            pension scheme"), usual for a company with one scheme: consistent, not proof
      not_named             the accounts text exists but neither names the scheme nor shows a DB scheme
      no_text               no accounts text saved for the sponsor or its parents
    schemes.named_by_other: when not named by the sponsor, other companies whose accounts name the scheme in full."""
    for col in ("sponsor_named_in_accounts", "named_by_other"):
        if col not in {r[1] for r in con.execute("PRAGMA table_info(schemes)")}:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {col} TEXT")
    con.execute("UPDATE schemes SET sponsor_named_in_accounts = NULL, named_by_other = NULL")
    rows = con.execute("""SELECT psr, scheme_name, sponsor_company_number FROM schemes
        WHERE sponsor_company_number IS NOT NULL AND likely_live = 1 AND name_type <> 'DC_likely'""").fetchall()
    parents = dict(con.execute("SELECT company_number, parent_number FROM company_parents WHERE parent_number IS NOT NULL"))
    group = dict(con.execute("SELECT company_number, group_db_company_number FROM sponsors "
                             "WHERE group_db_company_number IS NOT NULL"))
    # sponsors whose own accounts show a DB scheme (figures, or a DB note without readable figures)
    has_db = {r[0] for r in con.execute("""SELECT company_number FROM sponsors WHERE db_period_end IS NOT NULL
        OR db_pdf_status IN ('figures', 'mentioned_no_figures') OR db_check = 'flag:misread'""")}
    files = _texts()
    cache = {}

    def text(num):
        if num not in cache:
            cache[num] = _norm_text(files[num]) if files.get(num) else None
        return cache[num]

    def chain(num):
        out, seen = [], {num}
        for nxt in (group.get(num), parents.get(num)):
            p = nxt
            while p and p not in seen and len(out) < 6:
                out.append(p)
                seen.add(p)
                p = parents.get(p)
        return out

    def core_hit(core, t):
        return re.search(rf" {re.escape(core)}(?: [A-Z0-9]+){{0,4}} (?:PENSION|RETIREMENT|SUPERANNUATION) ", t)

    result, unconfirmed = {}, {}
    for psr, name, num in rows:
        full, core = _scheme_keys(name)
        own, up = text(num), [t for t in (text(p) for p in chain(num)) if t]
        if own is None and not up:
            result[psr] = "no_text"
        elif full and own and f" {full} " in own:
            result[psr] = "named"
        elif full and any(f" {full} " in t for t in up):
            result[psr] = "named_in_parent"
        elif core and own and core_hit(core, own):
            result[psr] = "core_named"
        elif core and any(core_hit(core, t) for t in up):
            result[psr] = "core_named_in_parent"
        elif num in has_db:
            result[psr] = "db_unnamed"
        else:
            result[psr] = "not_named"
        if result[psr] in ("not_named", "db_unnamed", "core_named", "core_named_in_parent") and full:
            unconfirmed[psr] = (full, num)
    # who else names the unconfirmed schemes? one pass over every saved text
    others = collections.defaultdict(set)
    rare = {psr: max(full.split(), key=len) for psr, (full, _) in unconfirmed.items()}
    for cn in files:
        t = text(cn) if cn in cache else _norm_text(files[cn])
        toks = set(t.split())
        for psr, (full, num) in unconfirmed.items():
            if cn != num and rare[psr] in toks and f" {full} " in t:
                others[psr].add(cn)
    for psr, status in result.items():
        con.execute("UPDATE schemes SET sponsor_named_in_accounts = ?, named_by_other = ? WHERE psr = ?",
                    (status, ",".join(sorted(others[psr]))[:200] or None if psr in others else None, psr))
    con.commit()
    return collections.Counter(result.values()), len(others)

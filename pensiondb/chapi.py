"""Companies House public API: refresh sponsor status and find dissolved sponsors the snapshot can't see.
Needs a free key (https://developer.company-information.service.gov.uk/) in $COMPANIES_HOUSE_API_KEY.
Responses are cached under raw/companies_house/api_cache/ so reruns are free."""
import hashlib
import json
import os
import time

import requests

from . import config as C
from .match import upsert_sponsor
from .names import norm, sponsor_candidates

_MIN_INTERVAL = 0.55            # CH allows 600 requests / 5 minutes
_last = [0.0]


def api_key():
    """From the environment, else from a .env file next to run.py (kept out of shell history and chat)."""
    if os.environ.get(C.CH_API_KEY_ENV):
        return os.environ[C.CH_API_KEY_ENV]
    env = C.ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == C.CH_API_KEY_ENV and v.strip():
                return v.strip().strip('"').strip("'")
    return None


def _get(path, params=None):
    key = hashlib.sha1((path + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    cache = C.CH_API_CACHE / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    wait = _MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(5):
        try:
            r = requests.get(C.CH_API_BASE + path, params=params, auth=(api_key(), ""), timeout=60)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 4:
                raise
            time.sleep(30 * (attempt + 1))
            continue
        _last[0] = time.time()
        if r.status_code in (429, 502, 503, 504):
            time.sleep(60 * (attempt + 1))
            continue
        break
    if r.status_code == 404:
        data = None
    else:
        r.raise_for_status()
        data = r.json()
    C.CH_API_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def _profile_row(p):
    addr = p.get("registered_office_address") or {}
    return {
        "company_number": p["company_number"], "company_name": p.get("company_name"),
        "company_status": p.get("company_status"), "company_category": p.get("type"),
        "incorporation_date": p.get("date_of_creation"), "dissolution_date": p.get("date_of_cessation"),
        "reg_post_code": addr.get("postal_code"), "sic_1": ",".join(p.get("sic_codes") or []) or None,
        "accounts_category": ((p.get("accounts") or {}).get("last_accounts") or {}).get("type"),
    }


def refresh_sponsor_status(con):
    nums = [r[0] for r in con.execute("SELECT company_number FROM sponsors WHERE status_source <> 'api'")]
    for i, num in enumerate(nums, 1):
        p = _get(f"/company/{num}")
        if p:
            upsert_sponsor(con, _profile_row(p), source="api")
        if i % 100 == 0:
            con.commit()
            print(f"  API status {i:,}/{len(nums):,}", end="\r")
    con.commit()
    print(f"API: refreshed {len(nums):,} sponsor statuses")


def search_unmatched(con):
    """Exact-normalised-name search for unmatched schemes. Catches dissolved sponsors absent from the snapshot."""
    rows = con.execute("SELECT psr, scheme_name FROM schemes WHERE match_method='unmatched'").fetchall()
    found = 0
    for i, (psr, name) in enumerate(rows, 1):
        for cand in sponsor_candidates(name):
            res = _get("/search/companies", {"q": cand, "items_per_page": 20}) or {}
            want = norm(cand)
            hits = [it for it in res.get("items", []) if norm(it.get("title")) == want
                    or norm(it.get("title")).removesuffix(" LIMITED").removesuffix(" PLC") == want]
            if not hits:
                continue
            hit = hits[0]
            n = len({h["company_number"] for h in hits})
            conf = 0.9 if n == 1 else 0.6
            p = _get(f"/company/{hit['company_number']}")
            if not p:
                continue
            upsert_sponsor(con, _profile_row(p), source="api")
            con.execute("""INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number,
                               company_name, score, confidence, accepted, needs_review, notes)
                           VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                        (psr, name, cand, "api_exact", hit["company_number"], hit.get("title"), 100, conf,
                         int(n > 1), f"{n} candidate companies; status {p.get('company_status')}"))
            con.execute("""UPDATE schemes SET sponsor_name_derived=?, sponsor_company_number=?, match_method='api_exact',
                               match_confidence=?, needs_review=? WHERE psr=?""",
                        (cand, hit["company_number"], conf, int(n > 1), psr))
            found += 1
            break
        if i % 100 == 0:
            con.commit()
            print(f"  API search {i:,}/{len(rows):,} ({found} found)", end="\r")
    con.commit()
    print(f"API: matched {found:,} of {len(rows):,} previously unmatched schemes")


# ---------------------------------------------------------------- dissolved / renamed / postcode-led search

def _pc(s):
    return (s or "").replace(" ", "").upper()


def _search_items(cand):
    """Companies a candidate name could be: live (current name), dissolved (name at dissolution) and dissolved
    (any previous name). Each as (number, [names], postcode, status, dissolved_on)."""
    out = {}
    live = _get("/search/companies", {"q": cand, "items_per_page": 20}) or {}
    for it in live.get("items", []):
        snippet = it.get("address_snippet") or ""
        pc = ((it.get("address") or {}).get("postal_code")) or snippet.rsplit(",", 1)[-1]
        out[it["company_number"]] = (it["company_number"], [it.get("title") or ""], _pc(pc),
                                     it.get("company_status"), it.get("date_of_cessation"), it.get("date_of_creation"))
    for st in ("best-match", "previous-name-dissolved"):
        try:
            res = _get("/dissolved-search/companies", {"q": cand, "search_type": st, "size": 20}) or {}
        except requests.HTTPError:
            res = {}                            # the dissolved index throws 500s on some queries; skip it
        for it in res.get("items", []):
            names = [it.get("company_name") or ""] + [p.get("name") or "" for p in it.get("previous_company_names") or []]
            pc = (it.get("registered_office_address") or {}).get("postal_code")
            prev = out.get(it["company_number"])
            out[it["company_number"]] = (it["company_number"], sorted(set(names + (prev[1] if prev else [])))
                                         , _pc(pc) or (prev[2] if prev else ""), it.get("company_status") or "dissolved",
                                         it.get("date_of_cessation"),
                                         it.get("date_of_creation") or (prev[5] if prev else None))
    return list(out.values())


def _judge(cand, items, scheme_pc):
    """(item, matched name, method, confidence, needs_review) or None.
    exact: a current or former name equals the candidate (suffix-insensitive); several companies -> postcode decides,
    else review. postcode: same registered postcode and the names agree word for word (fuzzy)."""
    from rapidfuzz import fuzz

    from .match import _words_agree
    from .names import core
    want = core(norm(cand))
    if len(want) < 3:
        return None
    exact = []
    for it in items:
        for nm in it[1]:
            if core(norm(nm)) == want:
                exact.append((it, nm))
                break
    if exact:
        pcs = [e for e in exact if scheme_pc and e[0][2] == scheme_pc]
        if pcs:
            return (*pcs[0], "api_exact_pc", 0.97, 0)
        if len(exact) == 1 and len(want.split()) >= 2:
            return (*exact[0], "api_exact_any", 0.9, 0)
        return (*exact[0], "api_exact_any", 0.6, 1)
    if not scheme_pc:
        return None
    for it in items:
        if it[2] != scheme_pc:
            continue
        for nm in it[1]:
            k = core(norm(nm))
            # token_sort, not token_set: "KUEHNE NAGEL" must not match "KUEHNE NAGEL DRINKFLOW ASSET CONTROL"
            if k and fuzz.token_sort_ratio(want, k) >= 90 and _words_agree(want, k):
                return it, nm, "api_postcode", 0.85, 0
    return None


GENERIC_FIRST = {"UK", "BRITISH", "NATIONAL", "GENERAL", "INTERNATIONAL", "UNITED", "ROYAL", "NORTHERN", "SOUTHERN",
                 "EASTERN", "WESTERN", "NORTH", "SOUTH", "EAST", "WEST", "CENTRAL", "GREAT", "NEW", "FIRST", "GLOBAL",
                 "EUROPEAN", "AMERICAN", "LONDON", "SCOTTISH", "WELSH", "ENGLISH", "STANDARD", "UNIVERSAL", "MODERN",
                 "ASSOCIATED", "ALLIED", "CONSOLIDATED", "INDUSTRIAL", "GROUP", "STAFF", "COMPANY", "TRUST"}


def _judge_subset(cand, items, scheme_pc):
    """A company at the scheme's exact postcode whose name is a shorter or longer form of the candidate, starting
    with the same distinctive word: "FEEDWATER LIMITED" for "Feedwater Treatment Services Ltd"."""
    from .names import core
    want = core(norm(cand)).split()
    if not scheme_pc or not want or want[0] in GENERIC_FIRST or len(want[0]) < 4:
        return None
    hits = []
    for it in items:
        if it[2] != scheme_pc or (len(it) > 5 and (it[5] or "") >= "2020"):
            continue                            # created after the register date: a namesake, not the employer
        for nm in it[1]:
            have = core(norm(nm)).split()
            if {"PENSION", "PENSIONS", "TRUSTEE", "TRUSTEES", "NOMINEES"} & set(have):
                continue                        # the scheme's trustee company, not the employer
            if have and have[0] == want[0] and (set(have) <= set(want) or set(want) <= set(have)):
                hits.append((it, nm))
                break
    if len({h[0][0] for h in hits}) == 1:
        return (*hits[0], "api_pc_subset", 0.8, 0)
    return None


def third_pass(con, limit=None):
    """Third pass over likely-live schemes still without a sponsor, including those whose earlier candidate a
    reviewer rejected: up to three sponsor-name candidates against live, dissolved and former names, with the
    current name cleaning ("LIMITED(THE)", "CO-OPERATIVE"), then the same-postcode name-subset rule. Companies a
    reviewer rejected for that scheme, and companies created after the register (2020 on), are skipped."""
    rows = con.execute("""SELECT s.psr, s.scheme_name, s.post_code FROM schemes s
                          WHERE s.sponsor_company_number IS NULL AND s.charity_number IS NULL
                            AND s.match_method IN ('unmatched', 'rejected') AND s.likely_live = 1
                            AND s.name_type <> 'DC_likely'""").fetchall()
    rejected = {}
    for psr, co in con.execute("SELECT psr, reviewed_company FROM review_decisions WHERE decision = 'reject'"):
        rejected.setdefault(psr, set()).add(co)
    rows = rows[:limit] if limit else rows
    found, live = 0, 0
    for i, (psr, name, pc) in enumerate(rows, 1):
        spc = _pc(pc)
        hit = None
        for cand in sponsor_candidates(name)[:3]:
            items = [it for it in _search_items(cand) if it[0] not in rejected.get(psr, set())
                     and not (len(it) > 5 and (it[5] or "") >= "2020")]
            hit = _judge(cand, items, spc) or _judge_subset(cand, items, spc)
            if hit:
                break
        if hit:
            it, nm, method, conf, review = hit
            p = _get(f"/company/{it[0]}")
            if p:
                upsert_sponsor(con, _profile_row(p), source="api")
                note = f"third pass: matched name {nm!r}; status {p.get('company_status')}"
                con.execute("""INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number,
                                   company_name, score, confidence, accepted, needs_review, notes)
                               VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                            (psr, name, cand, method, it[0], p.get("company_name"), 100, conf, review, note))
                con.execute("""UPDATE schemes SET sponsor_name_derived=?, sponsor_company_number=?, match_method=?,
                                   match_confidence=?, needs_review=? WHERE psr=?""",
                            (cand, it[0], method, conf, review, psr))
                found += 1
                live += p.get("company_status") == "active"
        if i % 100 == 0:
            con.commit()
            print(f"  third pass {i:,}/{len(rows):,}: {found} matched ({live} active)", flush=True)
    con.commit()
    print(f"API third pass: matched {found:,} of {len(rows):,} schemes ({live:,} to active companies)")
    return found


def search_dissolved(con, limit=None):
    """Second API pass for schemes still without a sponsor (and those whose only candidate was rejected as
    incorporated after the scheme): live, dissolved and former names, plus a postcode-led fuzzy match.
    Skips any company a reviewer already rejected for that scheme."""
    rows = con.execute("""SELECT s.psr, s.scheme_name, s.post_code FROM schemes s
                          WHERE s.sponsor_company_number IS NULL AND s.charity_number IS NULL
                            AND (s.match_method = 'unmatched' OR s.review_reason LIKE '%too_new%'
                                 OR s.review_method = 'rule:too_new')""").fetchall()
    rejected = {}
    for psr, co in con.execute("SELECT psr, reviewed_company FROM review_decisions WHERE decision = 'reject'"):
        rejected.setdefault(psr, set()).add(co)
    rows = rows[:limit] if limit else rows
    found, live = 0, 0
    for i, (psr, name, pc) in enumerate(rows, 1):
        spc = _pc(pc)
        hit = None
        for cand in sponsor_candidates(name)[:2]:
            items = [it for it in _search_items(cand) if it[0] not in rejected.get(psr, set())]
            hit = _judge(cand, items, spc)
            if hit:
                break
        if hit:
            it, nm, method, conf, review = hit
            p = _get(f"/company/{it[0]}")
            if p:
                upsert_sponsor(con, _profile_row(p), source="api")
                note = f"matched name {nm!r}; status {p.get('company_status')}"
                con.execute("""INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number,
                                   company_name, score, confidence, accepted, needs_review, notes)
                               VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                            (psr, name, cand, method, it[0], p.get("company_name"), 100, conf, review, note))
                con.execute("""UPDATE schemes SET sponsor_name_derived=?, sponsor_company_number=?, match_method=?,
                                   match_confidence=?, needs_review=? WHERE psr=?""",
                            (cand, it[0], method, conf, review, psr))
                found += 1
                live += p.get("company_status") == "active"
        if i % 50 == 0:
            con.commit()
            print(f"  search {i:,}/{len(rows):,}: {found} matched ({live} active)", flush=True)
    con.commit()
    print(f"API second pass: matched {found:,} of {len(rows):,} schemes ({live:,} to active companies)")
    return found

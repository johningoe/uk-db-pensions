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

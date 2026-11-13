"""Sponsors that aren't Companies House registrants: charities and registered societies.

Many schemes with no Companies House sponsor belong to charities (CIOs, trusts, church bodies), schools, co-ops,
friendly societies and housing associations, which register elsewhere. This loads three free bulk registers:

  ccew  Charity Commission for England and Wales: full register extract (daily), plus Part B of the annual
        return, which has a `defined_benefit_pension_scheme` figure for larger charities (income over ~£1m)
  oscr  Scottish Charity Regulator: register download (daily)
  fca   FCA Mutuals Public Register: society list (co-ops, community benefit societies, friendly societies,
        housing associations, credit unions, working men's clubs)

and matches sponsor-less schemes to them by name (same derivation as the Companies House match), using the scheme
postcode to separate common names. Results go in `charity_matches` and the schemes' charity_* columns; they never
overwrite the Companies House sponsor. A charity that is also a company gives its company number too.

    python run.py charities-download    # into raw/charities/
    python run.py charities             # load, match, write charity_* columns
"""
import csv
from pathlib import Path
import io
import re
import zipfile

import requests
from rapidfuzz import fuzz, process

from . import config as C
from .match import _words_agree
from .names import core, first_token, norm, sponsor_candidates

DIR = C.RAW / "charities"
CCEW = "https://ccewuksprdoneregsadata1.blob.core.windows.net/data/txt/publicextract.{}.zip"
FILES = {
    "ccew_charity.zip": CCEW.format("charity"),
    "ccew_charity_other_names.zip": CCEW.format("charity_other_names"),
    "ccew_charity_annual_return_partb.zip": CCEW.format("charity_annual_return_partb"),
    "oscr_register.download": "https://www.oscr.org.uk/download/charity-register",
    "fca_SocietyList.csv": "https://fcastoragemprprod.blob.core.windows.net/societylist/SocietyList.csv",
}
PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
FUZZY = 92


def download():
    DIR.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        r = requests.get(url, headers={"User-Agent": C.USER_AGENT}, timeout=300)
        r.raise_for_status()
        (DIR / name).write_bytes(r.content)
        print(f"{name}: {len(r.content) / 1e6:.1f} MB")


def _pc(s):
    m = PC_RE.search((s or "").upper())
    return m.group(1) + m.group(2) if m else ""


def _keys(name):
    """Match key for a register name: the normalised core. Charity words ("Trust", "Foundation") are kept:
    dropping them matched companies' grant-making foundations to the companies' own schemes."""
    k = core(norm(name))
    return {k} if k else set()


def _schema(con):
    con.executescript("""
    DROP TABLE IF EXISTS charity_register;
    CREATE TABLE charity_register (
        source TEXT, number TEXT, name TEXT, name_key TEXT, first_tok TEXT, active INTEGER, status TEXT,
        form TEXT, postcode TEXT, company_number TEXT, income REAL, removed TEXT);
    CREATE TABLE IF NOT EXISTS charity_finance (
        source TEXT, number TEXT, fy_end TEXT, income REAL, total_assets REAL, db_pension REAL,
        PRIMARY KEY (source, number));
    CREATE TABLE IF NOT EXISTS charity_matches (
        psr TEXT PRIMARY KEY, candidate TEXT, source TEXT, number TEXT, name TEXT, method TEXT, score REAL,
        postcode_match INTEGER, n_candidates INTEGER, accepted INTEGER, company_number TEXT);
    """)
    have = {r[1] for r in con.execute("PRAGMA table_info(schemes)")}
    for col, typ in {"charity_source": "TEXT", "charity_number": "TEXT", "charity_name": "TEXT",
                     "charity_match_confidence": "REAL", "charity_active": "INTEGER",
                     "charity_db_pension": "REAL", "charity_fy_end": "TEXT"}.items():
        if col not in have:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {col} {typ}")


def _tsv(zname):
    z = zipfile.ZipFile(DIR / zname)
    f = io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig", errors="replace", newline="")
    return csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def _co(s):
    s = (s or "").strip().upper()
    return (s if s[:2].isalpha() else s.zfill(8)) if s and s != "0" else None


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load(con):
    _schema(con)
    rows = []

    def add(source, number, name, active, status, form, pc, co=None, income=None, removed=None):
        for k in _keys(name):
            rows.append((source, number, name, k, first_token(k), int(active), status, form, pc, co, income, removed))

    main = {}
    for r in _tsv("ccew_charity.zip"):
        if r["linked_charity_number"] not in ("0", ""):
            continue                            # linked (subsidiary) charities share the main number
        n = r["registered_charity_number"]
        active = r["charity_registration_status"] == "Registered"
        main[n] = (active, r["charity_registration_status"], r["charity_type"], _pc(r["charity_contact_postcode"]),
                   _co(r["charity_company_registration_number"]),
                   _num(r["latest_income"]), (r["date_of_removal"] or "")[:10] or None)
        add("ccew", n, r["charity_name"], *main[n])
    for r in _tsv("ccew_charity_other_names.zip"):
        n = r["registered_charity_number"]
        if n in main and r["linked_charity_number"] in ("0", ""):
            add("ccew", n, r["charity_name"], *main[n])
    fin = []
    for r in _tsv("ccew_charity_annual_return_partb.zip"):
        if r["latest_fin_period_submitted_ind"] == "True":
            fin.append(("ccew", r["registered_charity_number"], r["fin_period_end_date"][:10],
                        _num(r["income_total_income_and_endowments"]), _num(r["assets_total_assets_and_liabilities"]),
                        _num(r["defined_benefit_pension_scheme"])))
    z = zipfile.ZipFile(DIR / "oscr_register.download")
    for r in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig", errors="replace")):
        form = r["Constitutional Form"]
        add("oscr", r["Charity Number"], r["Charity Name"], r["Charity Status"] != "Removed", r["Charity Status"],
            form, _pc(r["Postcode"]), None, _num(r["Most recent year income"]))
        if r.get("Known As"):
            add("oscr", r["Charity Number"], r["Known As"], r["Charity Status"] != "Removed", r["Charity Status"],
                form, _pc(r["Postcode"]), None, _num(r["Most recent year income"]))
    with open(DIR / "fca_SocietyList.csv", encoding="cp1252", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r["Society Name"]:
                st = r["Society Status"]
                add("fca", r["Full Registation Number"], r["Society Name"], st.startswith("Registered"), st,
                    r["Registered As"], _pc(r["Society Address"]), None, None, r["Deregistration Date"] or None)
    con.executemany("INSERT INTO charity_register VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("DELETE FROM charity_finance")
    con.executemany("INSERT OR REPLACE INTO charity_finance VALUES (?,?,?,?,?,?)", fin)
    con.execute("CREATE INDEX IF NOT EXISTS ix_charity_key ON charity_register(name_key)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_charity_tok ON charity_register(first_tok)")
    con.commit()
    by = dict(con.execute("SELECT source, count(DISTINCT number) FROM charity_register GROUP BY 1").fetchall())
    print(f"Registers loaded: {by}; Part B financial rows {len(fin):,}")


def _pick(rows, scheme_pc):
    """(row, distinct bodies, postcode hit). Postcode match first, then active bodies, then highest income."""
    by = {}
    for r in rows:
        by.setdefault((r[0], r[1]), r)
    cands = list(by.values())
    hits = [r for r in cands if scheme_pc and r[8] == scheme_pc]
    if hits:
        return hits[0], len(cands), True
    active = [r for r in cands if r[5]] or cands
    return max(active, key=lambda r: r[10] or 0), len(cands), False


def _dominant(rows, pick):
    """True when the pick is the only active body of that name, or has 10x the income of every other one."""
    others = {(r[0], r[1]): r for r in rows if (r[0], r[1]) != (pick[0], pick[1])}.values()
    return pick[5] and all(not r[5] or (pick[10] or 0) > 10 * (r[10] or 0) for r in others)


DECISIONS = Path(__file__).with_name("charity_decisions.csv")


def _decisions():
    """Reviewed register candidates (psr, source, number, decision accept/reject), kept with the code so they
    survive a rebuild. Fill `reviewer_decision` in data/charity_review.csv and copy the rows here."""
    if not DECISIONS.exists():
        return {}
    with open(DECISIONS, newline="") as f:
        return {r["psr"]: r for r in csv.DictReader(f)}


def match(con):
    """Match every scheme without a Companies House sponsor to the registers."""
    con.execute("DELETE FROM charity_matches")
    blocks = {}
    for row in con.execute("SELECT * FROM charity_register"):
        blocks.setdefault(row[4], []).append(row)
    exact = {}
    for rows in blocks.values():
        for r in rows:
            exact.setdefault(r[3], []).append(r)
    schemes = con.execute("""SELECT psr, scheme_name, post_code, sponsor_name_derived FROM schemes
                             WHERE sponsor_company_number IS NULL""").fetchall()
    decided = _decisions()
    out = []
    for psr, sname, spc, derived in schemes:
        spc = _pc(spc)
        best = None
        # the corrected sponsor name (sponsornames) last, so it only adds matches: "RNIB Retirement Benefits Scheme"
        cands = list(dict.fromkeys([*sponsor_candidates(sname), *([derived] if derived else [])]))
        for cand in cands:
            for key in sorted(_keys(cand), key=len, reverse=True):
                if len(key) < 4 or key in ("SCHEME", "FUND", "STAFF"):
                    continue
                if key in exact:
                    row, n, pch = _pick(exact[key], spc)
                    if len(key.split()) < 2 and not pch:
                        continue                # "COMMUNITY", "MACMILLAN": too common without a postcode
                    # a unique name, or a postcode that singles one out; else a person should decide
                    dom = n > 1 and len(key.split()) >= 2 and _dominant(exact[key], row)
                    ok = pch or (n == 1 and len(key.split()) >= 2) or dom
                    conf = 0.97 if pch else 0.9 if n == 1 else 0.8 if dom else 0.6
                    best = (cand, row, "exact", 100.0, pch, n, ok, conf)
                    break
            if best:
                break
            key = core(norm(cand))
            rows = blocks.get(first_token(key), [])
            if not rows or len(key.split()) < 2:
                continue
            res = process.extractOne(key, [r[3] for r in rows], scorer=fuzz.token_sort_ratio, score_cutoff=FUZZY)
            if res and _words_agree(key, res[0]):
                row, n, pch = _pick([r for r in rows if r[3] == res[0]], spc)
                best = (cand, row, "fuzzy", res[1], pch, n, pch, 0.85 if pch else 0.6)   # fuzzy needs the postcode
                break
        if best:
            cand, row, method, score, pch, n, ok, conf = best
            d = decided.get(psr)
            if d and d["decision"] == "accept":          # a reviewer's verdict, possibly naming another entry
                alt = con.execute("SELECT * FROM charity_register WHERE source=? AND number=? LIMIT 1",
                                  (d["source"], d["number"])).fetchone()
                row, ok, conf, method = alt or row, True, 0.9, method if not alt or alt[1] == row[1] else "reviewed"
            elif d and (d["source"], d["number"]) == (row[0], row[1]):
                ok = False
            out.append((psr, cand, row[0], row[1], row[2], method, score, int(pch), n, int(ok), row[9], conf, row[5]))
    con.executemany("INSERT INTO charity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)", [o[:11] for o in out])
    con.execute("""UPDATE schemes SET charity_source=NULL, charity_number=NULL, charity_name=NULL,
                   charity_match_confidence=NULL, charity_active=NULL, charity_db_pension=NULL, charity_fy_end=NULL""")
    for o in out:
        if o[9]:
            con.execute("""UPDATE schemes SET charity_source=?, charity_number=?, charity_name=?,
                           charity_match_confidence=?, charity_active=? WHERE psr=?""",
                        (o[2], o[3], o[4], o[11], o[12], o[0]))
    con.execute("""UPDATE schemes SET (charity_db_pension, charity_fy_end) =
                       (SELECT db_pension, fy_end FROM charity_finance f
                        WHERE f.source = schemes.charity_source AND f.number = schemes.charity_number)
                   WHERE charity_number IS NOT NULL""")
    con.commit()
    rev = con.execute("""SELECT m.psr, s.scheme_name, s.post_code, m.source, m.number, m.name, m.method, m.n_candidates
                         FROM charity_matches m JOIN schemes s USING (psr) WHERE m.accepted = 0
                         ORDER BY s.scheme_name""").fetchall()
    with open(C.DATA / "charity_review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["psr", "scheme_name", "scheme_post_code", "register", "number", "register_name", "method",
                    "same_name_bodies", "reviewer_decision"])
        w.writerows([*r, ""] for r in rev)
    acc = sum(o[9] for o in out)
    print(f"Schemes without a CH sponsor: {len(schemes):,}; register candidates found {len(out):,}; "
          f"accepted {acc:,}; left for review {len(out) - acc:,}")
    return acc


def run(con):
    load(con)
    return match(con)


# ---------------------------------------------------------------- accounts of matched bodies

ACC_DIR = DIR / "accounts"
CC_PAGE = ("https://register-of-charities.charitycommission.gov.uk/en/charity-search/-/charity-details/{}"
           "/accounts-and-annual-returns")
OSCR_PAGE = "https://www.oscr.org.uk/about-charities/search-the-register/charity-details?number={}"
FCA = "https://mutuals.fca.org.uk"
PAUSE = 1.0                                     # seconds between requests to the registers' websites
_session = requests.Session()
_session.headers["User-Agent"] = C.USER_AGENT


def _http(url, **kw):
    import time
    time.sleep(PAUSE)
    r = _session.get(url, timeout=180, **kw)
    r.raise_for_status()
    return r


def _latest_ccew(number):
    """(period end, PDF url) of the latest accounts on the Charity Commission register page."""
    import html
    from datetime import datetime
    page = _http(CC_PAGE.format(number)).text
    best = None
    for row in re.findall(r"<tr.*?</tr>", page, re.S):
        link = re.search(r'href="([^"]*accounts-resource[^"]*)"', row)
        if not link:
            continue
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row))
        d = re.search(r"(\d{1,2} [A-Z][a-z]+ \d{4})", txt)
        when = datetime.strptime(d.group(1), "%d %B %Y").date().isoformat() if d else ""
        if best is None or when > best[0]:
            best = (when, html.unescape(link.group(1)))
    return best


def _latest_oscr(number):
    page = _http(OSCR_PAGE.format(number)).text
    docs = re.findall(r"href='(https://oscr-docs[^']+\.pdf)'", page)
    year = re.search(r"Latest Annual Reports and Accounts\s*</[^>]+>\s*(?:<[^>]+>\s*)*(\d{1,2} \w{3} \d{4})",
                     re.sub(r"\s+", " ", page))
    return (year.group(1) if year else "latest", docs[0]) if docs else None


def _latest_fca(number):
    from datetime import datetime
    page = _http(f"{FCA}/Search/Search", params={"SearchTerm": number}).text
    sid = re.search(r'href="/Search/Society/(\d+)"', page)
    if not sid:
        return None
    rows = _http(f"{FCA}/Documents/GetSocietiesDocument", params={"societyId": sid.group(1)}).json()
    rows = rows.get("aaData", rows) if isinstance(rows, dict) else rows
    acc = [r for r in rows if r[2] == "Annual Returns and Accounts"]
    if not acc:
        return None
    r = max(acc, key=lambda r: datetime.strptime(r[0], "%d/%m/%Y"))
    return datetime.strptime(r[0], "%d/%m/%Y").date().isoformat(), f"{FCA}/Documents/Download/{r[4]}"


def _read_body(source, number, company):
    """Same result dict as pdfaccounts.assess, from Companies House if the body is a company, else the register."""
    from . import pdfaccounts as P
    if company and not company.startswith(("RC", "IP")):
        r = P.assess(company)
        r["doc"] = f"companies_house:{company}"
        if r.get("status") != "no_filing":
            return r
    latest = {"ccew": _latest_ccew, "oscr": _latest_oscr, "fca": _latest_fca}[source](number)
    if not latest:
        return dict(status="no_filing", doc=None)
    when, url = latest
    key, item = f"{source}_{number}", {"date": re.sub(r"\W", "", when) or "latest"}
    ACC_DIR.mkdir(parents=True, exist_ok=True)
    cached = (ACC_DIR / f"{key}_{item['date']}.txt").exists()
    data = None if cached else _http(url).content
    if data is not None and not data.startswith(b"%PDF"):
        return dict(status="error", evidence="not a PDF", doc=url)
    pages, src = P.page_texts(key, item, data, "pdf", P.MAX_PAGES, cache_dir=ACC_DIR)
    full = "\n".join(t for _, t in pages)
    base = dict(filing_date=when, text_source=src if full.strip() else "none", doc=url)
    hit = P.extract(pages)
    if hit:
        mult = P.MULT
        f = {k: v * mult[u] for k, (v, u) in hit["found"].items()}
        return dict(base, status="figures", page=hit["page"], evidence=hit["evidence"],
                    unit=",".join(sorted({u for _, u in hit["found"].values()})),
                    obligation=abs(f["obligation"]) if "obligation" in f else None,
                    assets=abs(f["assets"]) if "assets" in f else None, net=f.get("net"))
    if P.DB_MENTION.search(full):
        return dict(base, status="mentioned_no_figures")
    return dict(base, status="dc_only" if P.DC_ONLY.search(full) else "no_pension_note")


def targets(con):
    """Active matched bodies of likely-live, non-DC schemes with no DB figure from the annual return."""
    return con.execute("""
        SELECT DISTINCT s.charity_source, s.charity_number,
               (SELECT max(company_number) FROM charity_register r
                WHERE r.source = s.charity_source AND r.number = s.charity_number)
        FROM schemes s WHERE s.charity_number IS NOT NULL AND s.charity_active = 1 AND s.likely_live = 1
          AND s.name_type <> 'DC_likely'
          AND NOT EXISTS (SELECT 1 FROM charity_finance f WHERE f.source = s.charity_source
                          AND f.number = s.charity_number AND coalesce(f.db_pension, 0) <> 0)
          AND (s.charity_source <> 'ccew' OR (SELECT max(income) FROM charity_register r
               WHERE r.source = s.charity_source AND r.number = s.charity_number) >= 1e6)""").fetchall()


def read_accounts(con, limit=None):
    con.execute("""CREATE TABLE IF NOT EXISTS charity_accounts (
        source TEXT, number TEXT, doc TEXT, filing_date TEXT, text_source TEXT, status TEXT, obligation REAL,
        assets REAL, net REAL, unit TEXT, page INTEGER, evidence TEXT, checked_at TEXT,
        PRIMARY KEY (source, number))""")
    done = {(r[0], r[1]) for r in con.execute("SELECT source, number FROM charity_accounts WHERE status <> 'error'")}
    todo = [t for t in targets(con) if (t[0], t[1]) not in done][:limit]
    for i, (src, num, co) in enumerate(todo, 1):
        try:
            r = _read_body(src, num, co)
        except Exception as e:                  # noqa: BLE001 - record and carry on
            r = dict(status="error", evidence=str(e)[:300])
        con.execute("""INSERT OR REPLACE INTO charity_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (src, num, r.get("doc"), r.get("filing_date"), r.get("text_source"), r["status"],
                     r.get("obligation"), r.get("assets"), r.get("net"), r.get("unit"), r.get("page"),
                     r.get("evidence")))
        con.commit()
        print(f"[{i}/{len(todo)}] {src} {num} {r['status']}", flush=True)
    merge_accounts(con)


def merge_accounts(con):
    """Copy accounts figures onto the schemes (charity_db_*), after the same plausibility check as sponsors."""
    from . import checks
    have = {r[1] for r in con.execute("PRAGMA table_info(schemes)")}
    for col, typ in {"charity_db_obligation": "REAL", "charity_db_assets": "REAL", "charity_db_source": "TEXT",
                     "charity_db_check": "TEXT"}.items():
        if col not in have:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {col} {typ}")
    # start again from the annual-return figure alone, so re-reads replace earlier accounts figures
    con.execute("""UPDATE schemes SET charity_db_obligation=NULL, charity_db_assets=NULL, charity_db_check=NULL,
                   (charity_db_pension, charity_fy_end) = (SELECT nullif(db_pension, 0), fy_end FROM charity_finance f
                        WHERE f.source = schemes.charity_source AND f.number = schemes.charity_number)
                   WHERE charity_number IS NOT NULL""")
    con.execute("""UPDATE schemes SET charity_db_source = CASE WHEN coalesce(charity_db_pension, 0) <> 0
                   THEN 'annual_return' END""")
    n = 0
    for src, num, ob, assets, net, fdate, unit, doc in con.execute(
            """SELECT source, number, obligation, assets, net, filing_date, unit, doc FROM charity_accounts
               WHERE status = 'figures'""").fetchall():
        ob, assets, check, _ = checks.judge(ob, assets, None, unit)
        if ob is not None and assets is not None:
            net = assets - ob
        con.execute("""UPDATE schemes SET charity_db_obligation=?, charity_db_assets=?, charity_db_check=?,
                           charity_db_pension = coalesce(?, charity_db_pension), charity_fy_end = coalesce(?, charity_fy_end),
                           charity_db_source = ? WHERE charity_source=? AND charity_number=?""",
                    (ob, assets, check, net, fdate, "accounts", src, num))
        n += 1
    con.commit()
    print(f"Accounts figures copied for {n:,} bodies")

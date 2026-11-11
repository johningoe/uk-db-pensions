"""Parent companies: DB figures from a sponsor's group accounts when its own accounts have none.

Many named sponsors are dormant, small or audit-exempt subsidiaries, or take part in a group-wide scheme that
they account for as defined contribution. The figures then sit in a parent's (group) accounts.

1. `lookup`: walk up the Companies House register of persons with significant control (PSC), following the
   active corporate controller with the largest stake, while it is a UK company. A 50/50 joint venture or a
   foreign or individual controller ends the chain. API responses are cached like every other CH call.
2. `run`: read the accounts of each sponsor's nearest UK parent that may hold figures (reusing the PDF/iXBRL
   reader in pdfaccounts). If that parent has none, the next one up is queued on the following pass.
3. `merge`: copy the nearest parent's figures onto the sponsor's `group_db_*` columns. They are group-level
   figures (often covering several schemes and subsidiaries), so they are kept apart from the sponsor's own
   `db_*` columns and must not be summed across sponsors.
"""
import json
import re
import time

from rapidfuzz import fuzz

from . import checks, pdfaccounts
from .chapi import _get
from .names import norm

MAX_LEVELS = 8
PARENT_MAX_PAGES = 400                      # group annual reports are long; the pension note is near the end
# Accounts types that never carry a pension note: not worth reading
NO_NOTE = {"dormant", "micro-entity", "no-accounts-type-available", "unaudited-abridged", "total-exemption-small",
           "null", "", None}
UK_WORDS = re.compile(r"united kingdom|england|wales|scotland|northern ireland|companies house|registrar of companies"
                      r"|companies act|\buk\b|great britain|britain|british|english|scottish|welsh", re.I)
STAKE = [("75-to-100", 3), ("50-to-75", 2), ("25-to-50", 1)]

GROUP_COLS = {
    "parent_chain": "TEXT",                 # JSON [[number, name], ...] nearest first, UK companies only
    "parent_top_name": "TEXT",              # the controller that ended the chain (may be foreign or an individual)
    "parent_top_country": "TEXT",
    "parent_chain_end": "TEXT",             # no_psc / foreign / individual / joint_venture / not_found / max_levels
    "group_db_company_number": "TEXT",
    "group_db_company_name": "TEXT",
    "group_db_levels": "INTEGER",           # 1 = immediate parent
    "group_db_period_end": "TEXT",
    "group_db_obligation": "REAL",
    "group_db_plan_assets": "REAL",
    "group_db_surplus_deficit": "REAL",
    "group_db_source": "TEXT",
}


def ensure_columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(sponsors)")}
    for col, typ in {**GROUP_COLS, "db_pdf_status": "TEXT"}.items():
        if col not in have:
            con.execute(f"ALTER TABLE sponsors ADD COLUMN {col} {typ}")


def _schema(con):
    ensure_columns(con)
    con.execute("""CREATE TABLE IF NOT EXISTS company_parents (
        company_number TEXT PRIMARY KEY, parent_number TEXT, parent_name TEXT, parent_country TEXT,
        end_reason TEXT, stake INTEGER, checked_at TEXT)""")


def targets(con):
    """Active sponsors of confirmed, likely-live, non-DC schemes with no DB figures of their own."""
    return [r[0] for r in con.execute("""
        SELECT DISTINCT sp.company_number FROM sponsors sp JOIN schemes s ON s.sponsor_company_number = sp.company_number
        WHERE lower(sp.company_status) = 'active' AND s.likely_live = 1 AND s.name_type <> 'DC_likely'
          AND s.needs_review = 0 AND sp.db_period_end IS NULL ORDER BY 1""")]


# ---------------------------------------------------------------- 1. ownership chain

def _number(ident):
    raw = re.sub(r"[^A-Z0-9]", "", (ident.get("registration_number") or "").upper())
    raw = re.sub(r"^(?:NO|NUMBER|COMPANYNO|REGNO|CRN)", "", raw)
    m = re.fullmatch(r"(SC|NI|OC|SO|NC|R0|IP|SP|LP|SL|NL|FC|GE)?(\d{1,8})", raw)
    if not m:
        return None
    prefix, digits = m.group(1) or "", m.group(2)
    return prefix + digits.zfill(8 - len(prefix))


def _same_name(a, b):
    return fuzz.token_set_ratio(norm(a), norm(b)) >= 85


def _parent(num):
    """(parent_number, parent_name, country, end_reason, stake) for one company, from its PSC register."""
    data = _get(f"/company/{num}/persons-with-significant-control", {"items_per_page": 100}) or {}
    live = [it for it in data.get("items", []) if not it.get("ceased") and not it.get("ceased_on")]
    if not live:
        return None, None, None, "no_psc", None

    def stake(it):
        n = " ".join(it.get("natures_of_control") or [])
        return max([v for k, v in STAKE if k in n] or [0])

    live.sort(key=stake, reverse=True)
    top = live[0]
    s = stake(top)
    if len(live) > 1 and s < 3 and stake(live[1]) == s:
        return None, top.get("name"), None, "joint_venture", s
    kind = top.get("kind", "")
    if "corporate-entity" not in kind and "legal-person" not in kind:
        return None, top.get("name"), None, "individual", s
    ident = top.get("identification") or {}
    country = ident.get("country_registered") or ident.get("legal_authority")
    place = " ".join(str(ident.get(k) or "") for k in ("country_registered", "place_registered", "legal_authority"))
    if not UK_WORDS.search(place):
        return None, top.get("name"), country, "foreign", s
    pnum = _number(ident)
    prof = _get(f"/company/{pnum}") if pnum else None
    if not (prof and _same_name(prof.get("company_name"), top.get("name"))):
        # number missing or mistyped: fall back to an exact-name search
        res = _get("/search/companies", {"q": top.get("name"), "items_per_page": 10}) or {}
        hits = [h for h in res.get("items", []) if norm(h.get("title")) == norm(top.get("name"))
                and h.get("company_status") == "active"]
        if len(hits) != 1:
            return None, top.get("name"), country, "not_found", s
        pnum = hits[0]["company_number"]
    return pnum, top.get("name"), country, None, s


def chain(con, num):
    """[(number, name), ...] nearest parent first, plus (top_name, top_country, end_reason)."""
    out, seen = [], {num}
    cur = num
    for _ in range(MAX_LEVELS):
        row = con.execute("SELECT parent_number, parent_name, parent_country, end_reason FROM company_parents "
                          "WHERE company_number=?", (cur,)).fetchone()
        if not row:
            p = _parent(cur)
            con.execute("INSERT OR REPLACE INTO company_parents VALUES (?,?,?,?,?,?,datetime('now'))", (cur, *p))
            row = p[:4]
        pnum, pname, country, end = row
        if not pnum:
            return out, (pname, country, end)
        if pnum in seen:
            return out, (pname, country, "cycle")
        out.append((pnum, pname))
        seen.add(pnum)
        cur = pnum
    return out, (None, None, "max_levels")


def lookup(con, companies=None):
    _schema(con)
    nums = companies or targets(con)
    t0 = time.time()
    for i, num in enumerate(nums, 1):
        ch, (top, country, end) = chain(con, num)
        con.execute("""UPDATE sponsors SET parent_chain=?, parent_top_name=?, parent_top_country=?, parent_chain_end=?
                       WHERE company_number=?""", (json.dumps(ch), top, country, end, num))
        if i % 50 == 0:
            con.commit()
            print(f"  parents {i:,}/{len(nums):,} ({time.time() - t0:.0f}s)", flush=True)
    con.commit()
    print(f"Parents: looked up {len(nums):,} sponsors")


# ---------------------------------------------------------------- 2. read parents' accounts

def _known_figures(con, num):
    """Figures we already hold for a company: as a sponsor (iXBRL/PDF) or from an earlier accounts read.
    Returns a dict, False (read, no figures) or None (not read yet)."""
    sp = con.execute("""SELECT db_period_end, db_obligation, db_plan_assets, db_surplus_deficit, db_taxonomy, db_check
                        FROM sponsors WHERE company_number=? AND db_period_end IS NOT NULL""", (num,)).fetchone()
    if sp:
        if (sp[5] or "").startswith("flag"):
            return False                        # implausible figures: don't pass them down the group
        return dict(period=sp[0], obligation=sp[1], assets=sp[2], net=sp[3], source=sp[4])
    p = con.execute("SELECT status, obligation, assets, net, text_source, filing_date, page, unit FROM pdf_accounts "
                    "WHERE company_number=?", (num,)).fetchone()
    if not p:
        return None
    if p[0] != "figures":
        return None if p[0] == "error" else False
    item = pdfaccounts.latest_accounts(num) or {}
    period = item.get("action_date") or (item.get("description_values") or {}).get("made_up_date") or p[5]
    ob, assets, check, _ = checks.judge(p[1], p[2], checks._page_text(num, p[6]), p[7])
    if check.startswith("flag"):
        return False
    net = p[3]
    if ob is not None and assets is not None:
        net = assets - ob
    return dict(period=period, obligation=ob, assets=assets, net=net, source=f"pdf_{p[4]}")


def _worth_reading(num):
    prof = _get(f"/company/{num}") or {}
    t = ((prof.get("accounts") or {}).get("last_accounts") or {}).get("type")
    return prof.get("company_status") == "active" and t not in NO_NOTE


def frontier(con, skip=()):
    """Parents to read next: for each target, the nearest parent not yet read, provided every nearer parent
    has been read (or isn't worth reading) and had no figures."""
    todo = []
    for num, ch in con.execute("SELECT company_number, parent_chain FROM sponsors WHERE parent_chain IS NOT NULL "
                               "AND parent_chain <> '[]'"):
        if num in skip:
            continue
        for pnum, _ in json.loads(ch):
            k = _known_figures(con, pnum)
            if k:
                break
            if k is None and _worth_reading(pnum):
                todo.append(pnum)
                break
    return sorted(set(todo))


def pending_own_pdf(con):
    """Sponsors still waiting for their own PDF accounts to be read: don't spend time on their parents yet."""
    done = {r[0] for r in con.execute("SELECT company_number FROM pdf_accounts")}
    return set(pdfaccounts.pilot_sample(con, n=None)) - done


def run(con, shard=None, limit=None, pause=0):
    _schema(con)
    pdfaccounts._schema(con)
    con.commit()
    skip = pending_own_pdf(con)
    t0, n_done, tried = time.time(), 0, set()
    cols = ["filing_date", "description", "paper_filed", "pages", "text_source", "status", "obligation", "assets",
            "net", "unit", "page", "evidence"]
    while True:
        todo = [t for t in frontier(con, skip) if t not in tried]
        if shard:
            i, n = shard
            todo = todo[i::n]
        if not todo:
            break
        for num in todo:
            tried.add(num)
            if con.execute("SELECT 1 FROM pdf_accounts WHERE company_number=?", (num,)).fetchone():
                continue                    # another worker got there first
            try:
                r = pdfaccounts.assess(num, max_pages=PARENT_MAX_PAGES)
            except Exception as e:          # noqa: BLE001 - record and carry on
                r = dict(status="error", evidence=str(e)[:300])
            con.execute(f"INSERT OR REPLACE INTO pdf_accounts (company_number, {', '.join(cols)}, checked_at) "
                        f"VALUES (?, {', '.join('?' * len(cols))}, datetime('now'))", [num, *[r.get(c) for c in cols]])
            con.commit()
            n_done += 1
            print(f"[{n_done}] parent {num} {r['status']} ({time.time() - t0:.0f}s)", flush=True)
            if limit and n_done >= limit:
                return
            if pause:
                time.sleep(pause)


# ---------------------------------------------------------------- 3. merge

def merge(con):
    _schema(con)
    for c in GROUP_COLS:
        if c.startswith("group_db"):
            con.execute(f"UPDATE sponsors SET {c} = NULL")
    n = 0
    for num, ch in con.execute("SELECT company_number, parent_chain FROM sponsors WHERE parent_chain IS NOT NULL "
                               "AND parent_chain <> '[]'").fetchall():
        for level, (pnum, pname) in enumerate(json.loads(ch), 1):
            k = _known_figures(con, pnum)
            if not k:
                continue
            con.execute("""UPDATE sponsors SET group_db_company_number=?, group_db_company_name=?, group_db_levels=?,
                               group_db_period_end=?, group_db_obligation=?, group_db_plan_assets=?,
                               group_db_surplus_deficit=?, group_db_source=? WHERE company_number=?""",
                        (pnum, pname, level, k["period"], k["obligation"], k["assets"], k["net"], k["source"], num))
            n += 1
            break
    con.commit()
    return n

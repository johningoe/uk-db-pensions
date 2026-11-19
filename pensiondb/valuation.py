"""When was each scheme's last funding (Part 3) valuation? Read from the accounts text we already hold.

Trustees must value a DB scheme at least every three years, on a fixed effective date, and finish within 15 months.
Sponsors' pension notes usually say when the last one was: "The last funding valuation of the Plan was carried out
by a qualified actuary as at 5 April 2023 and showed a deficit of £3.3m". This reads the saved accounts text (PDF and
OCR text, iXBRL filings, charity and society accounts) for such sentences, and for "the next valuation is due as at
…", and keeps the sentence as evidence. The accounting (FRS 102 / IAS 19) valuation and its "updated to" date are
not the funding valuation and are ignored, as is a "valuation" dated on the accounts' own balance sheet date.

Per scheme:
  stated_date    the latest funding valuation date stated in the accounts
  level          scheme: the sponsor has one live DB scheme, or the sentence names the scheme
                 sponsor: the sponsor has several schemes and the sentence does not say which
                 group: from the parent group's accounts (where the sponsor's figures come from), not naming it
  latest_date    the most recent effective date that has passed on the three-year cycle from the stated (or next)
                 date: the stated date itself, or later and marked inferred (only from dates in the last nine years). A valuation is finished up to 15 months
                 after its effective date, so the latest one may still be in progress.

    python run.py valuations        # table scheme_valuations, reports/valuation_dates.csv
                                    # plus dates found by hand: pensiondb/valuation_dates_hand.csv
"""
import csv
import re
from datetime import date

from . import config as C
from . import gaps
from .liveness import _ixbrl_text, _names_scheme, _sentences, _texts

HAND = C.ROOT / "pensiondb" / "valuation_dates_hand.csv"   # psr, stated_date, next_date, source, evidence

MON = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
       "december")
MONRX = "|".join(m.capitalize() for m in MON) + "|" + "|".join(MON) + "|" + "|".join(m.upper() for m in MON)
DATE = (rf"(?:(?P<d1>[0-9Il|]{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{MONRX}),?\s+(?P<y1>(?:19|20)\d\d)"
        rf"|(?P<m2>{MONRX})\s+(?P<d2>\d{{1,2}}),?\s+(?P<y2>(?:19|20)\d\d)"
        rf"|(?P<d3>\d{{1,2}})[/.](?P<mn3>\d{{1,2}})[/.](?P<y3>(?:19|20)\d\d))")
KIND = r"(?:actuarial|triennial|funding|statutory|formal|full|technical provisions|scheme funding|ongoing funding|tri-annual)"
# "...last formal actuarial valuation of the defined benefit section was carried out at 1 July 2023"
LAST = re.compile(rf"\b{KIND}\s+(?:actuarial\s+)?valuations?\b(?P<mid>[^.;]{{0,120}}?)\b(?:as\s+at|as\s+of|at|dated|"
                  rf"effective\s+date\s+(?:of|was)?|with\s+an\s+effective\s+date\s+of|on)\s+{DATE}", re.I)
# "...the valuation as at 31 March 2023 ..." with the kind word after: "the 31 March 2023 actuarial valuation"
LAST2 = re.compile(rf"\b{DATE}\s+(?:triennial\s+|funding\s+|formal\s+|full\s+)?(?:actuarial\s+)?valuation\b", re.I)
NEXT = re.compile(rf"\bnext\s+(?:{KIND}\s+)?(?:actuarial\s+)?valuation\b[^.;]{{0,100}}?{DATE}", re.I)
ACCOUNTING = re.compile(r"FRS\s?102|IAS\s?19|section\s+28|FRS\s?17|for\s+(?:the\s+)?(?:purposes?\s+of\s+)?accounting"
                        r"|accounting\s+(?:standard|purposes)|updated\s+to|rolled\s+forward", re.I)
ACCT_AFTER = re.compile(r"\s*,?\s*(?:by\s+[^,.;]{0,40}?\s+)?(?:for\s+(?:the\s+)?(?:purposes?\s+of\s+)?(?:FRS|IAS|accounting)|under\s+(?:FRS|IAS)"
                        r"|in\s+accordance\s+with\s+(?:FRS|IAS))", re.I)
FUNDING = re.compile(r"fund|triennial|trustee|recovery|deficit|contribution|technical provisions|formal|statutory"
                     r"|scheme actuary|every three years", re.I)


def _date(m):
    for d, mo, y in (("d1", "m1", "y1"), ("d2", "m2", "y2"), ("d3", "mn3", "y3")):
        if m.group(y):
            day = re.sub(r"[Il|]", "1", m.group(d))
            mon = m.group(mo)
            month = int(mon) if mon.isdigit() else MON.index(mon.lower()) + 1
            try:
                return date(int(m.group(y)), month, int(day))
            except ValueError:
                return None
    return None


def hits(text, filed=None):
    """[(kind 'last'/'next', date, sentence)] in a text."""
    out = []
    limit = date(int(filed[:4]), int(filed[5:7]), int(filed[8:10])) if filed else date.today()
    for s in _sentences(text):
        if len(s) > 700 or "valuation" not in s.lower():
            continue
        for m in NEXT.finditer(s):
            d = _date(m)
            if d and date(1995, 1, 1) <= d <= date(limit.year + 4, 12, 31):
                out.append(("next", d, s.strip()))
        for rx in (LAST, LAST2):
            for m in rx.finditer(s):
                if rx is LAST and ACCOUNTING.search(m.group("mid") or ""):
                    continue
                if NEXT.search(s[max(0, m.start() - 30):m.end()]):
                    continue
                # "...valuation at 31 December 2025 for the purposes of FRS 102": an accounting valuation
                if ACCT_AFTER.match(s[m.end():m.end() + 80]):
                    continue
                if re.search(r"informal\s*$", s[max(0, m.start() - 12):m.start()], re.I):
                    continue
                d = _date(m)
                if d and date(1995, 1, 1) <= d <= limit:
                    out.append(("last", d, s.strip()))
    return out


def _cycle(d, today):
    """The latest date on d's three-year cycle that has passed by today."""
    while True:
        try:
            nxt = d.replace(year=d.year + 3)
        except ValueError:                      # 29 February
            nxt = d.replace(year=d.year + 3, day=28)
        if nxt > today:
            return d
        d = nxt


def run(con, today=None):
    today = today or date.today()
    con.execute("DROP TABLE IF EXISTS scheme_valuations")
    con.execute("""CREATE TABLE scheme_valuations (psr TEXT PRIMARY KEY, stated_date TEXT, next_date TEXT,
                   level TEXT, latest_date TEXT, latest_basis TEXT, source TEXT, evidence TEXT)""")
    status = gaps.classify(con)
    live = ("with_figures", "no_figures", "no_figure_due", "not_public")
    rows = con.execute("""SELECT s.psr, s.scheme_name, s.sponsor_company_number, s.charity_source, s.charity_number
                          FROM schemes s""").fetchall()
    # a "valuation" dated on the accounts' own balance sheet date is the accounting one, not the funding valuation
    year_ends = {r[0]: r[1][:10] for r in con.execute(
        "SELECT company_number, db_period_end FROM sponsors WHERE db_period_end IS NOT NULL")}
    parent = dict(con.execute("SELECT company_number, group_db_company_number FROM sponsors "
                              "WHERE group_db_company_number IS NOT NULL"))
    rows = [r for r in rows if status[r[0]][0] in live and (r[2] or r[4])]
    per = {}
    for psr, _, co, reg, rno in rows:
        key = co or (reg, rno)
        per[key] = per.get(key, 0) + 1
    files = {p.name: p for p in C.CH_ACCOUNTS_DIR.rglob("Prod*")}
    cache, out = {}, []
    for psr, name, co, reg, rno in rows:
        key = co or (reg, rno)
        if key not in cache:
            found = []
            texts = _texts(co, None, None) if co else []          # saved PDF / OCR text, newest first
            if co and not texts:
                texts = _ixbrl_text(con, co, files)                # else the latest tagged filing
            if not texts and rno:
                texts = _texts(None, reg, rno)                     # a charity or society's accounts
            for label, text in texts:
                filed = re.search(r"(\d{4}-\d\d-\d\d)", label)
                found += [(k, d, s, label) for k, d, s in hits(text, filed.group(1) if filed else None)]
            if not found and co in parent:                        # the parent group's accounts, which hold the figures
                for label, text in _texts(parent[co], None, None):
                    filed = re.search(r"(\d{4}-\d\d-\d\d)", label)
                    found += [(k, d, s, f"parent {parent[co]}: {label}")
                              for k, d, s in hits(text, filed.group(1) if filed else None)]
            cache[key] = found
        found = [h for h in cache[key] if not (h[0] == "last" and co and h[1].isoformat() == year_ends.get(co) if not h[3].startswith('parent ') else year_ends.get(parent.get(co)))]
        if not found:
            continue
        named = [h for h in found if _names_scheme(h[2], name)]
        single = per[key] == 1
        use = named or found
        from_parent = found[0][3].startswith("parent ")
        level = "scheme" if named else "group" if from_parent else "scheme" if single else "sponsor"
        last = [h for h in use if h[0] == "last"]
        nxt = [h for h in use if h[0] == "next"]
        stated = max(last, key=lambda h: h[1]) if last else None
        nx = max(nxt, key=lambda h: h[1]) if nxt else None
        base = max([h[1] for h in (stated, nx) if h and h[1] <= today], default=None)
        if not base:
            continue
        # project forward on the three-year cycle only from a recent date: an old one may be from stale accounts
        latest = _cycle(base, today) if base >= date(today.year - 9, 1, 1) else base
        basis = "stated" if stated and latest == stated[1] else "inferred"
        ev = stated or nx
        out.append((psr, stated[1].isoformat() if stated else None, nx[1].isoformat() if nx else None, level,
                    latest.isoformat(), basis, ev[3], ev[2][:500]))
    # dates found by hand in schemes' own documents (summary funding statements, scheme websites, announcements):
    # used where the accounts gave no date, or an earlier one
    if HAND.exists():
        by = {o[0]: o for o in out}
        with open(HAND, newline="") as f:
            hand = [(r["psr"], r["stated_date"] or None, r["next_date"] or None, r["source"], "", r["evidence"])
                    for r in csv.DictReader(f)]
        for psr, sd, nd, url, src, ev in hand:
            if status.get(psr, ("",))[0] not in live:
                continue
            base = max([date.fromisoformat(x) for x in (sd, nd) if x and date.fromisoformat(x) <= today], default=None)
            if not base:
                continue
            old = by.get(psr)
            if old and old[1] and sd and old[1] >= sd:
                continue
            latest = _cycle(base, today) if base >= date(today.year - 9, 1, 1) else base
            by[psr] = (psr, sd, nd, "scheme", latest.isoformat(), "stated" if sd and latest.isoformat() == sd else
                       "inferred", f"scheme document: {url}", (ev or "")[:500])
        out = list(by.values())
    con.executemany("INSERT INTO scheme_valuations VALUES (?,?,?,?,?,?,?,?)", out)
    con.commit()
    rep = C.ROOT / "reports" / "valuation_dates.csv"
    with open(rep, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["psr", "scheme_name", "sponsor", "stated_valuation_date", "next_valuation_date", "level",
                    "latest_valuation_date", "latest_basis", "source", "evidence"])
        for r in con.execute("""SELECT v.psr, s.scheme_name, coalesce(sp.company_name, s.charity_name), v.stated_date,
                                v.next_date, v.level, v.latest_date, v.latest_basis, v.source, v.evidence
                                FROM scheme_valuations v JOIN schemes s USING (psr)
                                LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
                                ORDER BY s.scheme_name"""):
            w.writerow(r)
    n = len(out)
    return {"schemes": len(rows), "with_date": n, "stated": sum(1 for o in out if o[1]),
            "scheme_level": sum(1 for o in out if o[3] == "scheme"),
            "latest_inferred": sum(1 for o in out if o[5] == "inferred")}

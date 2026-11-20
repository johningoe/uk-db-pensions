"""Senior finance officers of sponsors in the £50m-£500m band, from the public Companies House register.

For each sponsor (company) with a scheme in the band this lists, from the register of officers:
  finance officer    the current director named next to a finance title (CFO, finance director) in the company's
                     own accounts; else the company secretary (often the finance lead in smaller companies). The
                     register no longer publishes directors' occupations, so titles come only from the accounts.
  signing director   the current director who signed the latest accounts we hold, where the text shows it
  published contacts role mailboxes (pensions, finance, investor relations, company secretary, enquiries) that
                     the company itself prints in those accounts; personal addresses are not collected

The output names individuals, so it is written to data/ (gitignored) and never to reports/ or the repository:
data/sponsor_finance_contacts.csv and the table sponsor_finance_contacts.

    python run.py contacts
"""
import csv
import re

from rapidfuzz import fuzz

from . import config as C
from .chapi import _get
from .liveness import _ixbrl_text, _texts

OUT = C.DATA / "sponsor_finance_contacts.csv"
FIELDS = ["company_number", "company_name", "band_schemes", "finance_officer", "finance_role", "finance_occupation",
          "finance_appointed", "finance_basis", "other_finance_officers", "company_secretary", "signing_director",
          "signing_source", "published_contacts", "contacts_source", "register_link"]
RANK = [(1, re.compile(r"\b(?:chief financial|c\.?f\.?o\b|finance director|director of finance|financial director|"
                       r"group finance director|finance & operations director|finance and operations director)", re.I)),
        (2, re.compile(r"\b(?:finance|financial controller|treasurer|treasury|chief accountant|head of finance|"
                       r"vice president,? finance|vp finance|controller)\b", re.I)),
        (3, re.compile(r"\b(?:chartered accountant|accountant|chartered certified|aca\b|acca\b|cima\b|fca\b)", re.I))]
SIGN = re.compile(r"(?:were|was) approved (?:by the board|for issue)[^.]{0,200}?signed on (?:its|their) behalf by"
                  r"|signed on (?:its|their) behalf by", re.I)
TITLE = re.compile(r"chief financial officer|group finance director|finance director|director of finance|\bCFO\b"
                   r"|chief finance officer|financial director", re.I)
EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
ROLE_BOX = re.compile(r"^(?:pensions?|pension\.?trustees?|trustees?|finance|accounts|investor\.?relations|ir|investors|"
                      r"company\.?secretar(?:y|iat)|cosec|secretariat|enquir(?:y|ies)|info|contact|hello|treasury)"
                      r"(?:[._-][a-z0-9]+)?$", re.I)


def _name(raw):
    """ "SMITH, John Andrew" -> "John Andrew Smith"."""
    if "," in raw:
        last, _, first = raw.partition(",")
        return f"{first.strip()} {last.strip().title()}".strip()
    return raw.strip()


def officers(num):
    out, start = [], 0
    while True:
        d = _get(f"/company/{num}/officers", {"items_per_page": 100, "start_index": start}) or {}
        items = d.get("items") or []
        out += items
        start += len(items)
        if not items or start >= (d.get("active_count", 0) + d.get("resigned_count", 0) + d.get("inactive_count", 0)):
            break
    # the register no longer gives directors' occupations, so job titles come from the accounts (see _titled)
    return [o for o in out if not o.get("resigned_on")]


def _rank(o):
    text = " ".join(filter(None, [o.get("occupation"), o.get("officer_role")]))
    for r, rx in RANK:
        if rx.search(text):
            return r
    return 4 if o.get("officer_role") == "secretary" else 9


def _hits(o, window):
    last = o["name"].partition(",")[0].strip()
    first = _name(o["name"]).split()[0] if _name(o["name"]).split() else ""
    return (len(last) >= 3 and re.search(r"\b" + re.escape(last) + r"\b", window, re.I)
            and (not first or re.search(r"\b" + re.escape(first[:3]), window, re.I)))


def _signer(texts, current):
    """Current directors named just after the formal approval of the accounts ("signed on its behalf by")."""
    dirs = [o for o in current if "director" in (o.get("officer_role") or "")]
    for label, text in texts:
        for m in SIGN.finditer(text):
            window = text[m.end(): m.end() + 250]
            names = [_name(o["name"]) for o in dirs if _hits(o, window)]
            if names:
                return "; ".join(names[:3]), label
    return None, None


def _titled(texts, current):
    """A current director named next to a finance title (CFO, finance director) in the company's own accounts."""
    dirs = [o for o in current if "director" in (o.get("officer_role") or "")]
    for label, text in texts:
        for m in TITLE.finditer(text):
            window = text[max(0, m.start() - 90): m.end() + 90]
            hit = [o for o in dirs if _hits(o, window)]
            if len(hit) == 1:
                return hit[0], m.group(0), label
    return None, None, None


def _contacts(texts, company_name):
    found = {}
    for label, text in texts:
        for local, domain in EMAIL.findall(text):
            if ROLE_BOX.match(local) and not re.search(r"companieshouse|gov\.uk|hmrc|thepensionsregulator", domain, re.I):
                found.setdefault(f"{local}@{domain}".lower(), label)
    return found


def run(con):
    rows = con.execute("""SELECT sp.company_number, sp.company_name, count(*)
        FROM schemes s JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
        WHERE s.band = 'in' GROUP BY sp.company_number ORDER BY sp.company_name""").fetchall()
    files = {p.name: p for p in C.CH_ACCOUNTS_DIR.rglob("Prod*")}
    out = []
    for i, (num, name, n) in enumerate(rows, 1):
        current = officers(num)
        ranked = sorted((o for o in current if _rank(o) < 9), key=lambda o: (_rank(o), o.get("appointed_on") or ""))
        fin = ranked[0] if ranked else None
        others = [f"{_name(o['name'])} ({o.get('occupation') or o.get('officer_role')})" for o in ranked[1:4] if _rank(o) <= 3]
        sec = next((_name(o["name"]) for o in current if o.get("officer_role") == "secretary"), "")
        texts = _texts(num, None, None) or _ixbrl_text(con, num, files)
        signer, sign_src = _signer(texts, current)
        if not fin or _rank(fin) > 1:                      # the accounts may name the finance director
            o, title, src = _titled(texts, current)
            if o:
                fin = dict(o, occupation=f"{title} (named in the accounts, {src})")
                fin["_rank"] = 1
        boxes = _contacts(texts, name)
        basis = {1: "senior finance role", 2: "finance role", 3: "accountant", 4: "company secretary"}.get(
            fin.get("_rank", _rank(fin)), "") if fin else ""
        out.append({"company_number": num, "company_name": name, "band_schemes": n,
                    "finance_officer": _name(fin["name"]) if fin else "", "finance_role": (fin or {}).get("officer_role", ""),
                    "finance_occupation": (fin or {}).get("occupation", ""),
                    "finance_appointed": (fin or {}).get("appointed_on", ""), "finance_basis": basis,
                    "other_finance_officers": "; ".join(others), "company_secretary": sec,
                    "signing_director": signer or "", "signing_source": sign_src or "",
                    "published_contacts": "; ".join(boxes), "contacts_source": "; ".join(sorted(set(boxes.values()))),
                    "register_link": f"https://find-and-update.company-information.service.gov.uk/company/{num}/officers"})
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} sponsors", flush=True)
    con.execute("DROP TABLE IF EXISTS sponsor_finance_contacts")
    con.execute(f"CREATE TABLE sponsor_finance_contacts ({', '.join(f + ' TEXT' for f in FIELDS)})")
    con.executemany(f"INSERT INTO sponsor_finance_contacts VALUES ({','.join('?' * len(FIELDS))})",
                    [[r[f] for f in FIELDS] for r in out])
    con.commit()
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(out)
    return {"sponsors": len(out), "finance_officer": sum(1 for r in out if r["finance_officer"]),
            "senior_finance_role": sum(1 for r in out if r["finance_basis"] == "senior finance role"),
            "signing_director": sum(1 for r in out if r["signing_director"]),
            "published_contacts": sum(1 for r in out if r["published_contacts"])}

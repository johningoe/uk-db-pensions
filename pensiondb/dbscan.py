"""Which companies report a DB pension scheme? A scan of every company's iXBRL accounts, to find schemes we lack.

The scheme list comes from EIOPA's March 2020 register and is matched to sponsors from the scheme side. This works
the other way: it streams Companies House's monthly bulk accounts archives (every iXBRL filing) and keeps each
filing that tags defined benefit obligation, plan assets or a net DB position, using the same concepts and
headline rules as accounts.py. Companies with DB figures that are neither a sponsor nor a parent in our database
point to schemes that are unmatched, wrongly matched or missing. Large groups that file PDF accounts are not in
the archives, so this finds mainly mid-sized and smaller filers.

One archive at a time: download, scan with all cores, delete. Results are saved per archive in
data/dbscan/<archive>.json ({company number: {period_end, obligation, assets, net, taxonomy, file}}), so the run
resumes where it stopped.

    python run.py db-scan --limit 12        # the latest 12 months
    python run.py db-scan-report            # reports/db_filers_not_in_db.csv
"""
import csv
import json
import multiprocessing as mp
import os
import re
import time
import zipfile

from . import accounts
from . import config as C
from .bulkscan import _download

OUT = C.DATA / "dbscan"
TMP = C.RAW / "companies_house" / "bulk_tmp"
GATE = re.compile((r":(?:" + "|".join(re.escape(k) for k in accounts.CONCEPTS) + r")\"").encode())
NUM = re.compile(r"_([A-Z]{2}\d{6}|\d{8})_(\d{8})\.")


def _scan(args):
    path, names = args
    out = []
    with zipfile.ZipFile(path) as z:
        for n in names:
            data = z.read(n)
            if not GATE.search(data):
                continue
            try:
                h = accounts.headline(accounts.parse_ixbrl(data))
            except Exception:                       # a malformed filing: skip it, keep the run going
                continue
            if not h or not any(h.get(k) for k in ("obligation", "assets", "net")):
                continue
            m = NUM.search(n)
            out.append((m.group(1) if m else n, {**h, "file": n}))
    return out


def scan_archive(path, workers=None):
    workers = workers or os.cpu_count()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.endswith((".html", ".htm", ".xhtml"))]
    chunks = [(str(path), names[i:i + 2000]) for i in range(0, len(names), 2000)]
    found = {}
    t0 = time.time()
    with mp.get_context("spawn").Pool(workers) as pool:
        for i, hits in enumerate(pool.imap_unordered(_scan, chunks), 1):
            for num, h in hits:
                if num not in found or h["period_end"] > found[num]["period_end"]:
                    found[num] = h
            if i % 20 == 0 or i == len(chunks):
                print(f"    {min(i * 2000, len(names)):,}/{len(names):,} filings, {len(found):,} with DB figures "
                      f"({time.time() - t0:.0f}s)", flush=True)
    return found


def run(limit=12, workers=None):
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    archives = list(accounts.list_monthly_archives().items())[-limit:]
    print(f"DB scan over {len(archives)} archives", flush=True)
    for name, url in reversed(archives):
        out = OUT / (name + ".json")
        if out.exists():
            continue
        local = C.RAW / "companies_house" / "accounts" / name
        path, temp = (local, False) if local.is_file() else (TMP / name, True)
        t0 = time.time()
        if temp:
            print(f"  {name}: downloading", flush=True)
            _download(url, path)
        print(f"  {name}: scanning ({time.time() - t0:.0f}s)", flush=True)
        found = scan_archive(path, workers)
        out.write_text(json.dumps(found))
        if temp:
            path.unlink()
        print(f"  {name}: {len(found):,} companies with DB figures ({time.time() - t0:.0f}s)", flush=True)
    return merged()


def merged():
    """{company number: latest headline} over all scanned archives."""
    best = {}
    for f in OUT.glob("*.json"):
        for num, h in json.loads(f.read_text()).items():
            if num not in best or h["period_end"] > best[num]["period_end"]:
                best[num] = h
    return best


# ---------------------------------------------------------------- what the scan finds that we lack

REPORT = C.ROOT / "reports" / "db_filers_not_in_db.csv"
MIN_SIZE = 50e6
FIELDS = ["company_number", "company_name", "status", "period_end", "obligation_m", "assets_m", "net_m",
          "why_listed", "candidate_psrs", "candidate_schemes"]


def report(con, min_size=MIN_SIZE):
    """Companies whose iXBRL accounts show DB assets or obligation of at least min_size and which are not a
    sponsor, a reporting parent, or any company in a sponsor's ownership chain. For each, live schemes with no
    sponsor whose name contains the company's core name are listed as candidates."""
    import sqlite3
    from .names import core, norm
    found = merged()
    known = {r[0] for r in con.execute("SELECT company_number FROM sponsors")}
    known |= {r[0] for r in con.execute("SELECT group_db_company_number FROM sponsors WHERE group_db_company_number IS NOT NULL")}
    for (chain,) in con.execute("SELECT parent_chain FROM sponsors WHERE parent_chain IS NOT NULL"):
        try:
            known |= {c[0] for c in json.loads(chain)}
        except (ValueError, TypeError, IndexError):
            pass
    known |= {r[0] for r in con.execute("SELECT parent_number FROM company_parents WHERE parent_number IS NOT NULL")}
    snap = sqlite3.connect(C.DATA / "ch_snapshot.sqlite")
    unmatched = [(p, n, f" {norm(n)} ") for p, n in con.execute(
        """SELECT psr, scheme_name FROM schemes WHERE likely_live = 1 AND coalesce(name_type, '') <> 'DC_likely'
           AND sponsor_company_number IS NULL AND charity_number IS NULL""")]
    rows = []
    for num, h in found.items():
        size = max(h.get("assets") or 0, h.get("obligation") or 0)
        if size < min_size or num in known:
            continue
        co = snap.execute("SELECT company_name, company_status FROM companies WHERE company_number=?", (num,)).fetchone()
        name, status = co if co else ("", "")
        key = core(norm(name)) if name else ""
        cands = [(p, n) for p, n, t in unmatched if key and len(key) >= 4 and f" {key} " in t] if key else []
        rows.append({"company_number": num, "company_name": name, "status": status, "period_end": h["period_end"],
                     "obligation_m": round(h["obligation"] / 1e6, 1) if h.get("obligation") else "",
                     "assets_m": round(h["assets"] / 1e6, 1) if h.get("assets") else "",
                     "net_m": round(h["net"] / 1e6, 1) if h.get("net") is not None else "",
                     "why_listed": "DB figures of £50m+ in its accounts; not a sponsor or parent in the database",
                     "candidate_psrs": " ".join(p for p, _ in cands[:10]),
                     "candidate_schemes": " | ".join(n for _, n in cands[:10])})
    rows.sort(key=lambda r: -(r["assets_m"] or r["obligation_m"] or 0))
    REPORT.parent.mkdir(exist_ok=True)
    with open(REPORT, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(rows)
    return {"filers_with_db": len(found), "over_min_size": sum(1 for h in found.values()
            if max(h.get("assets") or 0, h.get("obligation") or 0) >= min_size),
            "not_in_db": len(rows), "with_candidate_scheme": sum(1 for r in rows if r["candidate_psrs"])}

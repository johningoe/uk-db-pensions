"""Who names our unmatched schemes? A scan of every company's iXBRL accounts, not just our sponsors'.

Many schemes keep the name of an employer that no longer exists ("Hewden Hire Plc Cash Benefits Scheme"); the
company that now runs the scheme usually names it in its pension note. textmatch searches only the ~13,000
companies whose accounts we hold; this streams Companies House's monthly bulk accounts archives (every iXBRL
filing, about 380,000 a month, 3.4GB zipped) and looks for the full names of live schemes that still have no
sponsor. Twelve months cover every iXBRL filer's latest accounts. Large groups file PDFs, which are not in the
archives, so this mostly finds small and medium companies.

One archive at a time: download, scan with all cores, delete. Hits are saved per archive in
data/bulkscan/<archive>.json ({psr: [company numbers]}), so the run resumes where it stopped.

    python run.py bulk-scan --limit 12      # the latest 12 months
"""
import collections
import html
import json
import multiprocessing as mp
import os
import re
import time
import zipfile

import requests

from . import accounts
from . import config as C
from .names import norm
from .textmatch import _clean, _scheme_keys

OUT = C.DATA / "bulkscan"
TMP = C.RAW / "companies_house" / "bulk_tmp"
GATE = re.compile(rb"pension|retirement|superannuation", re.I)
TAG = re.compile(r"<[^>]+>")
NUM = re.compile(r"_([A-Z]{2}\d{6}|\d{8})_\d{8}\.")

_keys = None


def targets(con):
    """{psr: full key} for live, not-DC schemes with no sponsor on any register and a distinctive full name."""
    out = {}
    for psr, name in con.execute("""SELECT psr, scheme_name FROM schemes WHERE likely_live = 1
            AND name_type <> 'DC_likely' AND sponsor_company_number IS NULL AND charity_number IS NULL
            AND coalesce(sponsor_name_review, '') NOT IN ('public_sector', 'product')"""):
        full, _ = _scheme_keys(name)
        if full and len(full.split()) >= 3:
            out[psr] = full
    return out


def _init(keys):
    global _keys
    by_rare = collections.defaultdict(list)
    for psr, full in keys.items():
        by_rare[max(full.split(), key=len)].append((psr, f" {full} "))
    _keys = by_rare


def _scan(args):
    path, names = args
    hits = []
    with zipfile.ZipFile(path) as z:
        for n in names:
            data = z.read(n)
            if not GATE.search(data):
                continue
            t = " " + _clean(norm(html.unescape(TAG.sub(" ", data.decode("utf-8", "ignore"))))) + " "
            toks = set(t.split())
            m = NUM.search(n)
            for rare, items in _keys.items():
                if rare in toks:
                    for psr, full in items:
                        if full in t:
                            hits.append((psr, m.group(1) if m else n))
    return hits


def scan_archive(path, keys, workers=None):
    workers = workers or os.cpu_count()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.endswith((".html", ".htm", ".xhtml"))]
    chunks = [(str(path), names[i:i + 2000]) for i in range(0, len(names), 2000)]
    found = collections.defaultdict(set)
    t0 = time.time()
    with mp.get_context("spawn").Pool(workers, initializer=_init, initargs=(keys,)) as pool:
        for i, hits in enumerate(pool.imap_unordered(_scan, chunks), 1):
            for psr, num in hits:
                found[psr].add(num)
            if i % 20 == 0 or i == len(chunks):
                print(f"    {i * 2000:,}/{len(names):,} filings, {len(found):,} schemes named "
                      f"({time.time() - t0:.0f}s)", flush=True)
    return {p: sorted(v) for p, v in found.items()}


def _download(url, dest):
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 22):
                f.write(chunk)


def run(con, limit=12, workers=None):
    keys = targets(con)
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    archives = list(accounts.list_monthly_archives().items())[-limit:]
    print(f"Bulk scan: {len(keys):,} scheme names over {len(archives)} archives", flush=True)
    for name, url in reversed(archives):
        out = OUT / (name + ".json")
        if out.exists():
            continue
        local = C.RAW / "companies_house" / "accounts" / name
        path, temp = (local, False) if local.exists() else (TMP / name, True)
        t0 = time.time()
        if temp:
            print(f"  {name}: downloading", flush=True)
            _download(url, path)
        print(f"  {name}: scanning ({time.time() - t0:.0f}s)", flush=True)
        found = scan_archive(path, keys, workers)
        out.write_text(json.dumps(found))
        if temp:
            path.unlink()
        print(f"  {name}: {len(found):,} schemes named ({time.time() - t0:.0f}s)", flush=True)
    return merged()


def merged():
    """{psr: set(company numbers)} over all scanned archives."""
    allf = collections.defaultdict(set)
    for f in OUT.glob("*.json"):
        for psr, nums in json.loads(f.read_text()).items():
            allf[psr].update(nums)
    return allf

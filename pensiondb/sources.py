"""Download and load the raw sources: EIOPA register, PPF schemes list, FAS list, CH bulk snapshot."""
import csv
import io
import re
import sqlite3
import time
import zipfile

import pandas as pd
import requests
from bs4 import BeautifulSoup

from . import config as C
from .names import first_token, is_section, name_type, norm, core


def _get(url, **kw):
    r = requests.get(url, headers={"User-Agent": C.USER_AGENT}, timeout=120, **kw)
    r.raise_for_status()
    return r


def download(url, dest, chunk=1 << 20):
    """Resumable streaming download. Skips if the file already exists and is complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists():
        return dest
    pos = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": C.USER_AGENT, **({"Range": f"bytes={pos}-"} if pos else {})}
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(part, "ab" if r.status_code == 206 else "wb") as f:
            for b in r.iter_content(chunk):
                f.write(b)
    part.rename(dest)
    return dest


# ---------------------------------------------------------------- EIOPA

def fetch_eiopa():
    dest = C.EIOPA_DIR / C.EIOPA_FILE
    if dest.exists():
        return dest
    for url in C.EIOPA_URLS:
        r = requests.get(url, headers={"User-Agent": C.USER_AGENT}, timeout=120)
        if r.ok and r.content[:2] == b"PK":          # real xlsx, not SharePoint's 'File Not Found' page
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            print(f"EIOPA register from {url}")
            return dest
    raise RuntimeError("EIOPA register not available from any known URL")


def load_eiopa(con):
    path = fetch_eiopa()
    df = pd.read_excel(path, dtype=str)
    uk = df[df["Reporting Country"].isin(["UK", "GB"])].copy()
    uk = uk.drop_duplicates("Identification Code")
    street = (uk["Street number"].fillna("") + " " + uk["Street name"].fillna("")).str.strip()
    rows = [
        (r["Identification Code"].strip(), r["The Official name of the entity"].strip(), st or None,
         r["Post Code"], r["City"], r["Registration start date"], r["Registration end date"],
         int(is_section(r["The Official name of the entity"])), name_type(r["The Official name of the entity"]))
        for (_, r), st in zip(uk.iterrows(), street)
    ]
    rows = [tuple(None if (isinstance(v, float) and pd.isna(v)) else v for v in row) for row in rows]
    con.executemany(
        """INSERT INTO schemes (psr, scheme_name, street, post_code, city, registration_start_date,
               registration_end_date, is_section, name_type)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(psr) DO UPDATE SET scheme_name=excluded.scheme_name, street=excluded.street,
               post_code=excluded.post_code, city=excluded.city, is_section=excluded.is_section,
               name_type=excluded.name_type""", rows)
    con.commit()
    print(f"EIOPA: {len(rows)} UK schemes loaded")
    return len(rows)


# ---------------------------------------------------------------- PPF schemes list (paginated HTML table)

def fetch_ppf(delay=1.0):
    """Saves each results page under raw/ppf/ so the parse is reproducible offline."""
    C.PPF_DIR.mkdir(parents=True, exist_ok=True)
    page, last = 0, None
    while last is None or page <= last:
        dest = C.PPF_DIR / f"schemes_page_{page:03d}.html"
        if not dest.exists():
            dest.write_bytes(_get(C.PPF_SCHEMES_URL, params={"searchViewType": 2, "page": page}).content)
            time.sleep(delay)
        if last is None:
            pages = re.findall(rb"searchViewType=2&(?:amp;)?page=(\d+)", dest.read_bytes())
            last = max(int(p) for p in pages) if pages else 0
        page += 1
    return sorted(C.PPF_DIR.glob("schemes_page_*.html"))


def load_ppf(con):
    rows = []
    for f in fetch_ppf():
        soup = BeautifulSoup(f.read_bytes(), "lxml")
        table = soup.select_one(".schemes-list__table--desktop")
        if table is None:
            raise RuntimeError(f"Unexpected PPF page layout in {f.name} (maintenance page?) - delete it and retry")
        for r in table.select(".schemes-list__table-row:not(.schemes-list__table-row--header)"):
            cols = [c.get_text(" ", strip=True) or None for c in r.select(".schemes-list__table-col")]
            if len(cols) == 5:
                rows.append(tuple(cols))
    rows = list(dict.fromkeys(rows))
    con.execute("DELETE FROM ppf_schemes")
    con.executemany("INSERT INTO ppf_schemes VALUES (?,?,?,?,?)", rows)
    con.commit()
    print(f"PPF: {len(rows)} schemes")
    return len(rows)


# ---------------------------------------------------------------- FAS list (PDF table)

def load_fas(con):
    import pdfplumber

    path = download(C.FAS_PDF_URL, C.FAS_DIR / "list_of_fas_schemes-2.pdf")
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for r in table:
                    if not r or not r[0] or r[0].strip() == "Scheme Name":
                        continue
                    r = [(c or "").replace("\n", " ").strip() or None for c in r] + [None] * 4
                    rows.append(tuple(r[:4]))
    con.execute("DELETE FROM fas_schemes")
    con.executemany("INSERT INTO fas_schemes VALUES (?,?,?,?)", rows)
    con.commit()
    print(f"FAS: {len(rows)} schemes")
    return len(rows)


# ---------------------------------------------------------------- Companies House bulk snapshot

def latest_snapshot_name():
    html = _get(C.CH_DOWNLOAD_BASE + "en_output.html").text
    m = re.search(r'href="(BasicCompanyDataAsOneFile-[\d-]+\.zip)"', html)
    if not m:
        raise RuntimeError("Could not find the CH snapshot link")
    return m.group(1)


def build_ch_index(force=False):
    """Load the CH snapshot into data/ch_snapshot.sqlite with normalised current and previous names.
    The snapshot holds live companies only (incl. those in administration/liquidation); dissolved
    companies need the API."""
    zips = sorted(C.CH_SNAPSHOT_DIR.glob("BasicCompanyDataAsOneFile-*.zip"))
    if not zips:
        name = latest_snapshot_name()
        zips = [download(C.CH_DOWNLOAD_BASE + name, C.CH_SNAPSHOT_DIR / name)]
    src = zips[-1]
    if C.CH_DB.exists() and not force:
        con = sqlite3.connect(C.CH_DB)
        done = con.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        con.close()
        if done and done[0] == src.name:
            return C.CH_DB
        C.CH_DB.unlink()

    con = sqlite3.connect(C.CH_DB)
    con.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE companies (company_number TEXT PRIMARY KEY, company_name TEXT, company_status TEXT,
            company_category TEXT, incorporation_date TEXT, dissolution_date TEXT, reg_post_code TEXT,
            sic_1 TEXT, accounts_category TEXT);
        CREATE TABLE names (company_number TEXT, name TEXT, name_norm TEXT, name_core TEXT,
            first_tok TEXT, is_previous INTEGER);
    """)
    with zipfile.ZipFile(src) as z:
        member = [n for n in z.namelist() if n.endswith(".csv")][0]
        with z.open(member) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
            header = [h.strip() for h in next(reader)]
            ix = {h: i for i, h in enumerate(header)}
            prev_cols = [ix[h] for h in header if re.fullmatch(r"PreviousName_\d+\.CompanyName", h)]
            co_buf, nm_buf, n, bad = [], [], 0, 0
            for row in reader:
                if len(row) < len(header):          # a handful of rows are truncated in CH's file
                    bad += 1
                    continue
                num = row[ix["CompanyNumber"]].strip()
                name = row[ix["CompanyName"]].strip()
                co_buf.append((num, name, row[ix["CompanyStatus"]], row[ix["CompanyCategory"]],
                               row[ix["IncorporationDate"]], row[ix["DissolutionDate"]] or None,
                               row[ix["RegAddress.PostCode"]], row[ix["SICCode.SicText_1"]],
                               row[ix["Accounts.AccountCategory"]]))
                for nm, prev in [(name, 0)] + [(row[i].strip(), 1) for i in prev_cols if row[i].strip()]:
                    nn = norm(nm)
                    nm_buf.append((num, nm, nn, core(nn), first_token(nn), prev))
                n += 1
                if len(co_buf) >= 200_000:
                    con.executemany("INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?,?,?)", co_buf)
                    con.executemany("INSERT INTO names VALUES (?,?,?,?,?,?)", nm_buf)
                    co_buf, nm_buf = [], []
                    print(f"  CH snapshot: {n:,} companies", end="\r")
            con.executemany("INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?,?,?)", co_buf)
            con.executemany("INSERT INTO names VALUES (?,?,?,?,?,?)", nm_buf)
    print("\n  indexing...")
    con.executescript("""
        CREATE INDEX ix_names_norm ON names(name_norm);
        CREATE INDEX ix_names_core ON names(name_core);
        CREATE INDEX ix_names_first ON names(first_tok);
    """)
    con.execute("INSERT INTO meta VALUES ('source', ?)", (src.name,))
    con.commit()
    con.close()
    print(f"CH snapshot index built from {src.name}: {n:,} companies ({bad} malformed rows skipped)")
    return C.CH_DB

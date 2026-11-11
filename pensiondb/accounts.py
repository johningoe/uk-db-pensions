"""Extract DB pension figures from Companies House Free Accounts Data (iXBRL), for matched sponsors only.

Monthly archives (Accounts_Monthly_Data-<Month><Year>.zip) hold one file per filing, named
Prod224_<run>_<company number>_<period end>.html; CIC filings are nested zips. Only files for company
numbers in the sponsors table are opened.

Archives are read remotely with HTTP range requests: the zip's central directory is fetched, then only
the sponsors' members (~0.1% of each archive). The ~300 GB of archives never touch the disk; the
extracted sponsor filings are kept under raw/companies_house/accounts/<archive>/."""
import io
import re
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from lxml import etree

from . import config as C
from .sources import _get

FILE_RE = re.compile(r"_([A-Z0-9]{8})_(\d{8})(?:_CIC)?\.(?:html|xhtml|xml|zip)$", re.I)

# Local concept names (prefixes vary by filing: core:, ns5:, d:, ifrs-full: ...).
# sign = how to turn the tagged value into "surplus positive / deficit negative" for net concepts.
CONCEPTS = {
    # FRS 102 (FRC 'core' taxonomy)
    "LiabilitiesDefinedBenefitPlanPresentValue": ("obligation", "frs102", None),
    "FairValueAssetsDefinedBenefitPlan": ("assets", "frs102", None),
    "DeficitSurplusInDefinedBenefitPlan": ("net", "frs102", -1),
    "RetirementBenefitObligationsSurplus": ("net", "frs102", -1),
    "NetDefinedBenefitLiabilityAsset": ("net", "frs102", -1),
    # older UK GAAP / FRS 17-era taxonomies (2009-2015 filings)
    "DefinedBenefitObligationPresentValue": ("obligation", "uk-gaap", None),
    "DefinedBenefitLiabilitiesFromPlansThatAreWhollyOrPartlyFundedPresentValue": ("obligation_part", "uk-gaap", None),
    "DefinedBenefitLiabilitiesFromPlansThatAreWhollyUnfundedPresentValue": ("obligation_part", "uk-gaap", None),
    "DefinedBenefitObligationFromSchemesThatAreWhollyOrPartlyFundedPresentValue": ("obligation_part", "uk-gaap", None),
    "FairValueAssetsDefinedBenefitScheme": ("assets", "uk-gaap", None),
    "FairValueAssetsInDefinedBenefitPlansLessPresentValueFundedObligations": ("net", "uk-gaap", +1),
    "PensionAssetLiability": ("net", "uk-gaap", +1),
    "DeficitSurplusOnPensionScheme": ("net", "uk-gaap", -1),
    "NetLiabilityAssetArisingFromDefinedBenefitPlanNon-current": ("net", "uk-gaap", -1),
    # IFRS (ifrs-full)
    "DefinedBenefitObligationAtPresentValue": ("obligation", "ifrs", None),
    "PlanAssetsAtFairValue": ("assets", "ifrs", None),
    "SurplusDeficitInPlan": ("net", "ifrs", +1),
    # ESEF filings by listed groups tag only the balance-sheet line, not the pension note
    "NoncurrentRecognisedLiabilitiesDefinedBenefitPlan": ("net", "ifrs", -1),
    "CurrentRecognisedLiabilitiesDefinedBenefitPlan": ("net", "ifrs", -1),
    "NoncurrentRecognisedAssetsDefinedBenefitPlan": ("net", "ifrs", +1),
    "CurrentRecognisedAssetsDefinedBenefitPlan": ("net", "ifrs", +1),
    "NetDefinedBenefitLiabilityAssetAbstract": None,
}
CONCEPTS = {k: v for k, v in CONCEPTS.items() if v}
PLAN_DIM = re.compile(r"Post-?employmentBenefitPlansDimension|DefinedBenefitPlansDimension|RetirementBenefitPlans", re.I)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def list_monthly_archives():
    """{archive name: url} for every monthly (and 2008/2009 yearly) archive CH publishes."""
    out = {}
    for page in ("en_monthlyaccountsdata.html", "historicmonthlyaccountsdata.html"):
        html = _get(C.CH_DOWNLOAD_BASE + page).text
        for href in re.findall(r'href="((?:archive/)?Accounts_Monthly_Data-[A-Za-z]+\d{4}\.zip)"', html):
            out[href.rsplit("/", 1)[-1]] = C.CH_DOWNLOAD_BASE + href
    return dict(sorted(out.items(), key=lambda kv: _month_key(kv[0])))


def _month_key(name):
    m = re.search(r"-([A-Za-z]+)(\d{4})\.zip", name)
    if not m:
        return (0, 0)
    month = m.group(1).capitalize()
    return (int(m.group(2)), MONTHS.index(month) + 1 if month in MONTHS else 0)   # JanuaryToDecember2008 -> 0


class RemoteFile(io.RawIOBase):
    """Seekable read-only view of a URL via HTTP Range requests, so zipfile can read selected members."""

    def __init__(self, url):
        self.url, self.pos, self.s = url, 0, requests.Session()
        self.s.headers["User-Agent"] = C.USER_AGENT
        r = self.s.head(url, timeout=60, allow_redirects=True)
        r.raise_for_status()
        self.size = int(r.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n=-1):
        end = self.size if n is None or n < 0 else min(self.size, self.pos + n)
        if end <= self.pos:
            return b""
        for attempt in range(5):
            try:
                r = self.s.get(self.url, headers={"Range": f"bytes={self.pos}-{end - 1}"}, timeout=300)
                if r.status_code == 206 and len(r.content) == end - self.pos:
                    break
            except requests.RequestException:
                pass
        else:
            raise IOError(f"range read failed: {self.url} {self.pos}-{end}")
        self.pos = end
        return r.content

    def readall(self):
        return self.read(-1)


# ---------------------------------------------------------------- iXBRL parsing

def _num(el):
    txt = "".join(el.itertext()).strip()
    fmt = (el.get("format") or "").lower()
    if fmt.endswith(("zerodash", "fixed-zero", "nocontent")) or txt in {"-", "–", "—", "nil", ""}:
        val = 0.0
    else:
        if fmt.endswith(("numcommadecimal", "num-comma-decimal", "numdotcomma")):     # 1.234,56
            txt = txt.replace(".", "").replace(" ", "").replace(",", ".")
        txt = re.sub(r"[^\d.]", "", txt)
        if not txt:
            return None
        val = float(txt)
    val *= 10 ** int(el.get("scale") or 0)
    return -val if el.get("sign") == "-" else val


def parse_ixbrl(data):
    """Returns list of facts: dict(concept, value, period_end, dims)."""
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(data, parser)
    if root is None:
        return []
    contexts = {}
    for ctx in root.iter("{*}context"):
        inst = ctx.find(".//{*}instant")
        end = ctx.find(".//{*}endDate")
        dims = tuple(sorted((m.get("dimension"), (m.text or "").strip()) for m in ctx.iter("{*}explicitMember")))
        contexts[ctx.get("id")] = ((inst if inst is not None else end).text.strip()
                                   if (inst is not None or end is not None) else None, dims)
    facts = []
    for el in root.iter("{*}nonFraction"):
        local = (el.get("name") or "").split(":")[-1]
        if local not in CONCEPTS:
            continue
        val = _num(el)
        period, dims = contexts.get(el.get("contextRef"), (None, ()))
        if val is None or not period:
            continue
        facts.append(dict(concept=local, value=val, period_end=period, dims=dims))
    return facts


def headline(facts):
    """Collapse facts to one figure per category at the latest balance-sheet date.
    Non-dimensional total wins; otherwise sum across pension-plan members; otherwise the largest value."""
    if not facts:
        return None
    latest = max(f["period_end"] for f in facts)
    out = {"period_end": latest}
    by_cat = defaultdict(list)
    for f in facts:
        if f["period_end"] == latest:
            by_cat[CONCEPTS[f["concept"]][0]].append(f)
    for cat, fs in by_cat.items():
        # one value per (concept, dims): duplicates appear when a figure is tagged in several notes
        uniq = {}
        for f in fs:
            uniq.setdefault((f["concept"], f["dims"]), f)
        fs = list(uniq.values())
        plain = [f for f in fs if not f["dims"]]
        if plain:
            pick = max(plain, key=lambda f: abs(f["value"]))
            v = pick["value"]
        else:
            plan_only = [f for f in fs if all(PLAN_DIM.search(d) for d, _ in f["dims"])]
            concept = (plan_only or fs)[0]["concept"]
            if plan_only:
                v = sum(f["value"] for f in plan_only if f["concept"] == concept)
            else:
                v = max((f for f in fs if f["concept"] == concept), key=lambda f: abs(f["value"]))["value"]
            pick = {"concept": concept}
        tax = CONCEPTS[pick["concept"]][1]
        if cat == "net":
            v *= CONCEPTS[pick["concept"]][2]       # -> surplus positive, deficit negative
        else:
            v = abs(v)
        out[cat] = v
        out.setdefault("taxonomy", tax)
    # funded + unfunded obligations reported separately (no total tagged)
    if "obligation_part" in out:
        parts = {}
        for f in by_cat["obligation_part"]:
            if not f["dims"]:
                parts[f["concept"]] = abs(f["value"])
        if "obligation" not in out and parts:
            out["obligation"] = sum(parts.values())
        del out["obligation_part"]
    return out


# ---------------------------------------------------------------- driver

def fetch_sponsor_members(name, url, wanted):
    """Extract sponsor filings from one archive (local copy if present, else remote) into raw/.
    Returns [(member, company_number, bytes)]."""
    local = C.CH_ACCOUNTS_DIR / name
    out_dir = C.CH_ACCOUNTS_DIR / name.removesuffix(".zip")
    src = local if local.exists() else RemoteFile(url)
    out = []
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            m = FILE_RE.search(info.filename)
            if not m or m.group(1).upper() not in wanted:
                continue
            dest = out_dir / info.filename.rsplit("/", 1)[-1]
            data = dest.read_bytes() if dest.exists() else z.read(info)
            if not dest.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            num = m.group(1).upper()
            if info.filename.lower().endswith(".zip"):          # CIC: nested zip with the accounts inside
                inner = zipfile.ZipFile(io.BytesIO(data))
                for n in inner.namelist():
                    if n.endswith((".html", ".xhtml")) and "accounts" in n:
                        out.append((info.filename + "!" + n, num, inner.read(n)))
            elif data:                                          # CH archives contain empty members
                out.append((info.filename, num, data))
    return out


def process_members(con, name, members):
    done = {r[0] for r in con.execute("SELECT member FROM accounts_files_processed WHERE archive=?", (name,))}
    n_files = n_hits = 0
    for member, num, data in members:
        if member in done:
            continue
        n_files += 1
        try:
            facts = parse_ixbrl(data)
        except Exception as e:                  # noqa: BLE001 - malformed filing: log and move on
            print(f"  parse error {member}: {e}")
            facts = []
        con.executemany(
            "INSERT INTO accounts_facts VALUES (?,?,?,?,?,?,?,?,?)",
            [(num, member, f["period_end"], f["concept"], CONCEPTS[f["concept"]][0], f["value"],
              int(bool(f["dims"])), "; ".join(f"{d}={m}" for d, m in f["dims"]) or None,
              CONCEPTS[f["concept"]][1]) for f in facts])
        con.execute("INSERT OR REPLACE INTO accounts_files_processed VALUES (?,?,?,?)",
                    (name, member, num, len(facts)))
        h = headline(facts)
        if h and any(h.get(k) for k in ("obligation", "assets", "net")):
            n_hits += 1
    con.execute("INSERT OR REPLACE INTO accounts_archives_done VALUES (?, datetime('now'))", (name,))
    con.commit()
    return n_files, n_hits


def reparse(con):
    """Rebuild accounts_facts from the sponsor filings already saved under raw/ (no network)."""
    con.execute("DELETE FROM accounts_facts")
    con.execute("DELETE FROM accounts_files_processed")
    wanted = {r[0] for r in con.execute("SELECT company_number FROM sponsors")}
    for d in sorted((p for p in C.CH_ACCOUNTS_DIR.iterdir() if p.is_dir()), key=lambda p: _month_key(p.name + ".zip")):
        members = []
        for f in sorted(d.iterdir()):
            m = FILE_RE.search(f.name)
            if not m or m.group(1).upper() not in wanted:
                continue
            data = f.read_bytes()
            if f.suffix.lower() == ".zip":
                inner = zipfile.ZipFile(io.BytesIO(data))
                members += [(f.name + "!" + n, m.group(1).upper(), inner.read(n)) for n in inner.namelist()
                            if n.endswith((".html", ".xhtml")) and "accounts" in n]
            elif data:
                members.append((f.name, m.group(1).upper(), data))
        files, hits = process_members(con, d.name + ".zip", members)
        print(f"  {d.name}: {files} filings, {hits} with DB figures", end="\r")
    print(f"\nsponsor DB figures rebuilt: {refresh_sponsor_figures(con)} filings")


def refresh_sponsor_figures(con):
    """Recompute sponsor DB columns from accounts_facts, so figures survive rematching and reruns."""
    con.execute("""UPDATE sponsors SET db_period_end=NULL, db_obligation=NULL, db_plan_assets=NULL,
                   db_surplus_deficit=NULL, db_net_basis=NULL, db_taxonomy=NULL, db_source_file=NULL""")
    by_file = defaultdict(list)
    for num, member, period, concept, dims, value in con.execute(
            """SELECT f.company_number, f.source_file, f.period_end, f.concept, f.dimensions, f.value
               FROM accounts_facts f JOIN sponsors s USING (company_number)"""):
        dim_t = tuple(tuple(x.split("=", 1)) for x in dims.split("; ")) if dims else ()
        by_file[(num, member)].append(dict(concept=concept, value=value, period_end=period, dims=dim_t))
    n = 0
    for (num, member), facts in sorted(by_file.items(), key=lambda kv: kv[0][1]):
        h = headline(facts)
        if h and any(h.get(k) for k in ("obligation", "assets", "net")):
            _store(con, num, member, h)
            n += 1
    con.commit()
    return n


def _store(con, num, member, h):
    cur = con.execute("SELECT db_period_end FROM sponsors WHERE company_number=?", (num,)).fetchone()
    if cur and cur[0] and cur[0] > h["period_end"]:
        return                                      # keep the most recent balance sheet
    ob, assets, net = h.get("obligation"), h.get("assets"), h.get("net")
    if not any((ob, assets, net)):
        return                                      # lines tagged but all zero/blank
    # Filers sign the tagged net line inconsistently (checked against assets - obligation on the
    # September 2026 file), so prefer figures we can derive.
    if ob is not None and assets is not None:
        net, basis = assets - ob, "assets_minus_obligation"
    elif ob and net is not None and abs(abs(net) - ob) < 1:
        net, basis = -ob, "unfunded"
    elif net is not None:
        basis = "tagged_sign_unverified"
    else:
        basis = None
    con.execute("""UPDATE sponsors SET db_period_end=?, db_obligation=?, db_plan_assets=?, db_surplus_deficit=?,
                       db_net_basis=?, db_taxonomy=?, db_source_file=? WHERE company_number=?""",
                (h["period_end"], ob, assets, net, basis, h.get("taxonomy"), member, num))


def run(con, months=None, all_months=False, workers=4):
    """months: list like ['September2026']; default = latest month only. Archives already completed are skipped."""
    con.execute("CREATE TABLE IF NOT EXISTS accounts_archives_done (archive TEXT PRIMARY KEY, done_at TEXT)")
    available = list_monthly_archives()
    if all_months:
        names = list(available)
    elif months:
        names = [f"Accounts_Monthly_Data-{m}.zip" for m in months]
    else:
        names = list(available)[-1:]
    # Sponsors added since the finished archives were read (API and postcode matches) need those archives again.
    con.execute("CREATE TABLE IF NOT EXISTS accounts_sponsors_scanned (company_number TEXT PRIMARY KEY, scanned_at TEXT)")
    done = {r[0] for r in con.execute("SELECT archive FROM accounts_archives_done")}
    wanted = {r[0] for r in con.execute("SELECT company_number FROM sponsors")}
    new = wanted - {r[0] for r in con.execute("SELECT company_number FROM accounts_sponsors_scanned")}
    jobs = [(n, wanted) for n in names if n not in done]
    if new and done:
        jobs += [(n, new) for n in names if n in done]
    print(f"{len(names)} archives requested, {len(names) - len(done & set(names))} new to read; "
          f"{len(new):,} sponsors not yet scanned")
    failed = 0
    # Range reads are network-bound, so fetch several archives at once; parse/write stays on this thread.
    with ThreadPoolExecutor(workers) as pool:
        futures = [(n, pool.submit(fetch_sponsor_members, n, available.get(n, C.CH_DOWNLOAD_BASE + n), w))
                   for n, w in jobs]
        for i, (n, fut) in enumerate(futures, 1):
            try:
                members = fut.result()
            except Exception as e:              # noqa: BLE001 - leave it for the next run
                print(f"[{i}/{len(jobs)}] {n}: FAILED {e}")
                failed += 1
                continue
            files, hits = process_members(con, n, members)
            print(f"[{i}/{len(jobs)}] {n}: {files} sponsor filings, {hits} with DB figures", flush=True)
    if not failed and (all_months or not done):
        con.executemany("INSERT OR IGNORE INTO accounts_sponsors_scanned VALUES (?, datetime('now'))",
                        [(c,) for c in wanted])
    print(f"sponsor DB figures rebuilt from stored facts: {refresh_sponsor_figures(con)} filings")

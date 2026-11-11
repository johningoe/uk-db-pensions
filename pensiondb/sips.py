"""Find published Statements of Investment Principles (SIPs) and implementation statements.

Schemes with 100+ members must publish their SIP on a free public website. There is no central index, so
candidate documents come from three free, account-less channels:
  wikidata   sponsor's official website (Wikidata P856, looked up by Companies House number P2622)
  guess      domains guessed from sponsor / scheme names, kept only if DNS resolves
  admin      administrator / adviser sites that host many schemes' documents
For each site the Wayback Machine URL index lists archived PDFs; SIP-like URLs are fetched (live, else the
archived copy) and accepted only if the text reads as a SIP/implementation statement AND names the scheme.
Documents are saved under raw/sips/<psr>/."""
import hashlib
import io
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from . import config as C
from .names import core, norm, sponsor_candidates

SIP_DIR = C.RAW / "sips"
CDX = "https://web.archive.org/cdx/search/cdx"
DOC_URL_RE = re.compile(
    r"statement[\W_]*of[\W_]*investment|investment[\W_]*principles|(?<![a-z])sip(?![a-z])|implementation[\W_]*statement",
    re.I)
LINK_HINT_RE = re.compile(r"pension|investment[\W_]*principles|(?<![a-z])sip(?![a-z])|implementation|trustee|governance",
                          re.I)
ADMIN_DOMAINS = [   # hosts that publish documents for many client schemes; extend as more are found
    "xpsgroup.com", "isio.com", "barnett-waddingham.co.uk", "hymans.co.uk", "lcp.com", "lcp.uk.com",
    "broadstone.co.uk", "firstactuarial.co.uk", "quantumadvisory.co.uk", "spence-partners.co.uk",
    "hazellcarr.com", "premiercompanies.co.uk", "cartwright.co.uk", "trafalgarhouse.co.uk", "capita.com",
    "barnett-waddingham.com", "aon.com", "mercer.com", "wtwco.com", "buckglobal.com", "punter-southall.com",
    "psgovernance.com", "zedra.com", "dalriadatrustees.co.uk", "ptluk.com", "independenttrustee.co.uk",
    "law-debenture.com", "bestrustees.co.uk", "capcuk.com", "pensions-sip.co.uk",
    # found via the pilot's search-engine spot check
    "seic.com", "apexgroup.com", "gateleyplc.com", "lawdebenture.com", "epa.towerswatson.com", "cwst.co.uk",
    "vidett.com", "hodgebank.co.uk", "myworkpension.co.uk",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sip_pilot (psr TEXT PRIMARY KEY, stratum TEXT);
CREATE TABLE IF NOT EXISTS sip_domains (psr TEXT, domain TEXT, channel TEXT, PRIMARY KEY (psr, domain));
CREATE TABLE IF NOT EXISTS sips (
    psr TEXT, url TEXT, channel TEXT, doc_type TEXT, latest_year INTEGER, mentions_db INTEGER,
    mentions_dc INTEGER, names_sponsor INTEGER, scheme_score REAL, match_level TEXT, content_sha1 TEXT,
    fetched_from TEXT, local_path TEXT,
    PRIMARY KEY (psr, content_sha1));    -- match_level: scheme (names this scheme) / sponsor_only
CREATE TABLE IF NOT EXISTS sip_candidates_checked (url TEXT PRIMARY KEY, ok INTEGER, note TEXT);
"""

_s = requests.Session()
_s.headers["User-Agent"] = C.USER_AGENT


def _get(url, timeout=60, max_bytes=50 << 20, **kw):
    """GET with retries and a wall-clock limit (requests' timeout is per read, so a trickling server
    could otherwise hang forever)."""
    for attempt in range(3):
        try:
            start = time.time()
            r = _s.get(url, timeout=(10, timeout), stream=True, **kw)
            buf = bytearray()
            for chunk in r.iter_content(1 << 16):
                buf += chunk
                if time.time() - start > timeout * 2 or len(buf) > max_bytes:
                    raise requests.Timeout("wall-clock limit")
            r._content = bytes(buf)
            if r.status_code in (429, 503):
                time.sleep(10 * (attempt + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(3)
    return None


# ---------------------------------------------------------------- sample

def make_sample(con, n=200, seed=42):
    """Stratified: half high-confidence matched sponsors, a quarter other matches, a quarter unmatched.
    Live, not DC-named, not public sector."""
    con.executescript(SCHEMA)
    if con.execute("SELECT count(*) FROM sip_pilot").fetchone()[0]:
        return
    base = "FROM schemes WHERE likely_live=1 AND name_type <> 'DC_likely' AND match_method <> 'non_company'"
    strata = [("matched_high", "AND match_confidence >= 0.85 AND needs_review = 0", n // 2),
              ("matched_other", "AND sponsor_company_number IS NOT NULL AND NOT (match_confidence >= 0.85 "
                                "AND needs_review = 0)", n // 4),
              ("unmatched", "AND sponsor_company_number IS NULL", n - n // 2 - n // 4)]
    for name, where, k in strata:
        rows = con.execute(f"SELECT psr {base} {where} ORDER BY (CAST(psr AS INTEGER) * {seed} + 7919) % 10007, psr "
                           "LIMIT ?", (k,)).fetchall()
        con.executemany("INSERT INTO sip_pilot VALUES (?,?)", [(r[0], name) for r in rows])
    con.commit()


# ---------------------------------------------------------------- candidate domains

def _registrable(host):
    host = host.lower().removeprefix("www.")
    parts = host.split(".")
    keep = 3 if len(parts) >= 3 and parts[-2] in ("co", "org", "ac", "gov", "me", "ltd", "plc") else 2
    return ".".join(parts[-keep:])


def wikidata_sites(numbers):
    out = {}
    numbers = list(numbers)
    for i in range(0, len(numbers), 200):
        vals = " ".join(f'"{n}"' for n in numbers[i:i + 200])
        q = f"SELECT ?ch ?w WHERE {{ VALUES ?ch {{ {vals} }} ?c wdt:P2622 ?ch . ?c wdt:P856 ?w }}"
        r = _get("https://query.wikidata.org/sparql", params={"query": q}, headers={"Accept": "application/json"})
        if r is not None and r.ok:
            for b in r.json()["results"]["bindings"]:
                out.setdefault(b["ch"]["value"], set()).add(_registrable(urlparse(b["w"]["value"]).netloc))
        time.sleep(1)
    return out


def _slug_variants(name):
    toks = [t for t in core(norm(name)).lower().split() if t not in {"the", "and", "of", "uk", "group", "holdings"}]
    if not toks:
        return []
    joined, dashed = "".join(toks), "-".join(toks)
    out = [joined, dashed]
    if len(toks) > 2:
        out.append(toks[0] + toks[1])
    if len(toks) > 1:
        out.append("".join(t[0] for t in toks))                  # acronym
    return [s for s in dict.fromkeys(out) if len(s) >= 3]


def guessed_domains(scheme_name, sponsor_name):
    names = [n for n in [sponsor_name, *(sponsor_candidates(scheme_name)[:1])] if n]
    hosts = []
    for n in names:
        for s in _slug_variants(n)[:3]:
            hosts += [f"{s}.co.uk", f"{s}.com", f"{s}pensions.co.uk", f"{s}pensionscheme.co.uk",
                      f"{s}pensionscheme.com", f"{s}pensionplan.co.uk", f"{s}-pensions.co.uk"]
    return list(dict.fromkeys(hosts))


def _resolves(host):
    try:
        socket.setdefaulttimeout(5)
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def collect_domains(con):
    rows = con.execute("""SELECT p.psr, s.scheme_name, sp.company_name, s.sponsor_company_number
                          FROM sip_pilot p JOIN schemes s USING (psr)
                          LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number""").fetchall()
    wd = wikidata_sites({r[3] for r in rows if r[3]})
    pairs = []
    for psr, sname, spname, num in rows:
        pairs += [(psr, d, "wikidata") for d in wd.get(num, ())]
        pairs += [(psr, d, "guess") for d in guessed_domains(sname, spname)]
    with ThreadPoolExecutor(16) as pool:
        ok = list(pool.map(lambda p: p[2] == "wikidata" or _resolves(p[1]), pairs))
    con.executemany("INSERT OR IGNORE INTO sip_domains VALUES (?,?,?)", [p for p, k in zip(pairs, ok) if k])
    con.commit()
    print(f"domains: {sum(ok)} live of {len(pairs)} candidates ({sum(len(v) for v in wd.values())} from Wikidata)")


# ---------------------------------------------------------------- listing documents on a domain

def wayback_pdfs(domain, limit=20000):
    """SIP-like document URLs archived for a domain (PDFs and pages)."""
    cache = SIP_DIR / "_cdx" / f"{domain}.txt"
    if cache.exists():
        lines = cache.read_text().splitlines()
    else:
        r = _get(CDX, timeout=180, params={"url": f"{domain}/*", "fl": "original,mimetype", "collapse": "urlkey",
                                           "filter": "statuscode:200", "limit": limit})
        lines = r.text.splitlines() if r is not None and r.ok else []
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("\n".join(lines))
        time.sleep(1)
    out = []
    for ln in lines:
        url, _, mime = ln.rpartition(" ")
        if url and DOC_URL_RE.search(unquote(url)) and ("pdf" in mime or url.lower().endswith(".pdf")):
            out.append(url)
    return list(dict.fromkeys(out))


def crawl_links(domain, max_pages=15):
    """Homepage + pension-ish pages, two levels: PDF links that look like SIPs (cached per domain)."""
    cache = SIP_DIR / "_crawl" / f"{domain}.txt"
    if cache.exists():
        return [u for u in cache.read_text().splitlines() if u]
    found = _crawl(domain, max_pages)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("\n".join(found))
    return found


def _crawl(domain, max_pages, budget=90):
    seen, queue, found = set(), [f"https://{domain}/", f"https://www.{domain}/"], []
    depth = {q: 0 for q in queue}
    deadline = time.time() + budget                 # a slow site can't hold up the run
    while queue and len(seen) < max_pages and time.time() < deadline:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = _get(url, timeout=20)
        if r is None or not r.ok or "html" not in r.headers.get("Content-Type", ""):
            continue
        soup = BeautifulSoup(r.content, "lxml")
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"])
            text = a.get_text(" ", strip=True)
            if _registrable(urlparse(href).netloc or domain) != _registrable(domain):
                continue
            if href.lower().split("?")[0].endswith(".pdf") and (DOC_URL_RE.search(unquote(href)) or DOC_URL_RE.search(text)):
                found.append(href)
            elif depth[url] < 2 and LINK_HINT_RE.search(href + " " + text) and href not in depth:
                depth[href] = depth[url] + 1
                queue.append(href)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------- fetch + validate

def _pdf_text(data, pages=4):
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:pages])
    except Exception:                               # noqa: BLE001 - not a readable PDF
        return ""


def fetch_doc(url):
    """Live copy first, else the newest archived copy. Returns (bytes, source) or (None, None). Cached."""
    key = hashlib.sha1(url.encode()).hexdigest()
    cache = SIP_DIR / "_docs"
    for src in ("live", "wayback"):
        if (cache / f"{key}.{src}.pdf").exists():
            return (cache / f"{key}.{src}.pdf").read_bytes(), src
    if (cache / f"{key}.none").exists():
        return None, None
    data, src = _fetch_doc(url)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / (f"{key}.{src}.pdf" if data else f"{key}.none")).write_bytes(data or b"")
    return data, src


def _fetch_doc(url):
    r = _get(url, timeout=60)
    if r is not None and r.ok and r.content[:4] == b"%PDF":
        return r.content, "live"
    r = _get(f"https://web.archive.org/web/2026id_/{url}", timeout=90)
    if r is not None and r.ok and r.content[:4] == b"%PDF":
        return r.content, "wayback"
    return None, None


def _distinctive(scheme_name):
    return core(norm((sponsor_candidates(scheme_name) or [scheme_name])[0]))


def _scheme_key(scheme_name):
    """Scheme name without leading THE and trailing policy/reference codes ("96393J", "REF2886", "NO 3565")."""
    toks = norm(scheme_name).split()
    while toks and (re.search(r"\d", toks[-1]) or toks[-1] in {"NO", "REF", "POLICY"}):
        toks.pop()
    return " ".join(toks)


def assess(text, scheme_name, sponsor_name):
    t = norm(text)
    title = t[:1500]
    is_is = "IMPLEMENTATION STATEMENT" in title
    is_sip = "INVESTMENT PRINCIPLES" in title
    if not (is_sip or is_is or "INVESTMENT PRINCIPLES" in t[:6000]):
        return None
    head = t[:6000]
    d = _distinctive(scheme_name)
    key = _scheme_key(scheme_name)
    # boilerplate-proof: the whole scheme name must appear, not just "staff pension and life assurance plan"
    score = fuzz.partial_ratio(key, head) if len(key) >= 12 else 0
    sponsor_hit = len(d) >= 5 and fuzz.partial_ratio(d, head) >= 95
    level = "scheme" if score >= 90 and (sponsor_hit or len(d) < 5) else ("sponsor_only" if sponsor_hit else None)
    if not level:
        return None
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", text) if int(y) <= 2026]
    return dict(doc_type="implementation_statement" if is_is else "SIP", scheme_score=score, match_level=level,
                latest_year=max(years) if years else None,
                mentions_db=int(bool(re.search(r"defined benefit|final salary|\bDB\b", text, re.I))),
                mentions_dc=int(bool(re.search(r"defined contribution|money purchase|\bDC\b", text, re.I))),
                names_sponsor=int(bool(sponsor_name) and fuzz.partial_ratio(core(norm(sponsor_name)), head) >= 90))


def run_pilot(con, workers=6):
    con.executescript(SCHEMA)
    make_sample(con)
    if not con.execute("SELECT count(*) FROM sip_domains").fetchone()[0]:
        collect_domains(con)
    schemes = {r[0]: r[1:] for r in con.execute(
        """SELECT p.psr, s.scheme_name, sp.company_name FROM sip_pilot p JOIN schemes s USING (psr)
           LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number""")}

    # candidate URLs per scheme: own domains (wayback listing + crawl), then admin-site pool matched by name
    cands = {psr: [] for psr in schemes}
    doms = con.execute("SELECT psr, domain, channel FROM sip_domains").fetchall()
    done = [0]

    def list_domain(d):
        out = wayback_pdfs(d[1]) + crawl_links(d[1])
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"  listed {done[0]}/{len(doms)} domains", flush=True)
        return out

    with ThreadPoolExecutor(workers) as pool:
        listed = dict(zip(doms, pool.map(list_domain, doms)))
    for (psr, dom, ch), urls in listed.items():
        cands[psr] += [(u, ch) for u in urls]
    with ThreadPoolExecutor(workers) as pool:
        admin = dict(zip(ADMIN_DOMAINS, pool.map(wayback_pdfs, ADMIN_DOMAINS)))
    print("admin pool:", {d: len(v) for d, v in admin.items() if v})
    for psr, (sname, spname) in schemes.items():
        d = _distinctive(sname).lower()
        if len(d) < 4:
            continue
        for dom, urls in admin.items():
            for u in urls:
                slug = re.sub(r"[\W_]+", " ", unquote(urlparse(u).path).lower())
                if fuzz.partial_ratio(d, slug) >= 90 and all(w in slug for w in d.split()[:2]):
                    cands[psr].append((u, "admin"))

    todo = {u for v in cands.values() for u, _ in v}
    print(f"{len(todo)} candidate documents for {sum(1 for v in cands.values() if v)} schemes")
    with ThreadPoolExecutor(workers) as pool:
        docs = dict(zip(todo, pool.map(fetch_doc, todo)))
    print("  documents fetched", flush=True)
    texts = {u: _pdf_text(b) if b else "" for u, (b, _) in docs.items()}

    found = 0
    for psr, lst in cands.items():
        sname, spname = schemes[psr]
        hit = False
        for u, ch in dict.fromkeys(lst):
            a = assess(texts.get(u, ""), sname, spname)
            if not a:
                continue
            data, src = docs[u]
            sha = hashlib.sha1(data).hexdigest()
            path = SIP_DIR / psr / (sha[:12] + ".pdf")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            con.execute("INSERT OR IGNORE INTO sips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (psr, u, ch, a["doc_type"], a["latest_year"], a["mentions_db"], a["mentions_dc"],
                         a["names_sponsor"], a["scheme_score"], a["match_level"], sha, src,
                         str(path.relative_to(C.ROOT))))
            hit = hit or a["match_level"] == "scheme"
        found += hit
    con.commit()
    print(f"SIP or implementation statement naming the scheme found for {found} of {len(schemes)} pilot schemes")

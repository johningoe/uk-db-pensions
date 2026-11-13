"""DB figures from PDF accounts (for sponsors that never filed iXBRL).

Fetches the latest accounts filing through the Companies House document API, takes the PDF's own text where it
has some and otherwise OCRs it, then looks for the defined benefit note.
OCR engine: macOS Vision on a Mac, Tesseract elsewhere (or set PENSIONDB_OCR=vision|tesseract).
PDFs and their text are cached under raw/companies_house/pdf_accounts/."""
import io
import os
import random
import shutil
import sys
import re
import time
import zipfile

import lxml.html
import pypdfium2 as pdfium
import requests

from . import config as C
from .accounts import headline, parse_ixbrl
from .chapi import _get, api_key

PDF_DIR = C.RAW / "companies_house" / "pdf_accounts"
MAX_PAGES = 200
MAX_BYTES = 60_000_000
KEEP_PDFS = False
OCR_ENGINE = os.environ.get("PENSIONDB_OCR") or ("vision" if sys.platform == "darwin" else "tesseract")
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract") or "tesseract"
TESSERACT_PSM = os.environ.get("TESSERACT_PSM", "6")       # 6 = one block of text: keeps table rows on one line

DB_MENTION = re.compile(r"defined benefit|final salary|retirement benefit (?:obligation|scheme)", re.I)
PENSION_WORD = re.compile(r"pension|retirement benefit|(?:scheme|plan) (?:assets|liabilities)", re.I)
DC_ONLY = re.compile(r"defined contribution|money purchase", re.I)
LINES = {
    "obligation": re.compile(r"present value of (?:the )?(?:defined benefit |funded |scheme |plan )?(?:obligations?|liabilit)"
                             r"|defined benefit obligations?\b(?! at)|(?:scheme|plan)['’]?s? liabilities|liabilities of the"
                             r" (?:scheme|plan)|actuarial liabilit(?:y|ies)\b|total defined benefit (?:liability|obligation)",
                             re.I),
    "assets": re.compile(r"fair value of (?:the )?(?:plan |scheme )?assets|(?:scheme|plan)['’]?s? assets\b"
                         r"|market value of (?:the )?(?:pension )?(?:(?:scheme|plan)['’]?s? )?assets"
                         r"|total defined benefit assets?\b", re.I),
    "net": re.compile(r"net (?:pension|defined benefit|retirement benefit)s? (?:liability|asset|deficit|surplus|obligation)"
                      r"|(?:deficit|surplus) in the (?:scheme|plan)|net (?:deficit|surplus)\b", re.I),
}
NUM = re.compile(r"\(?-?\d{1,3}(?:[,:;]\d{3})+\)?|\(?-?\d+(?:\.\d+)?\)?")     # OCR reads some commas as : or ;


def _schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS pdf_accounts (
        company_number TEXT PRIMARY KEY, filing_date TEXT, description TEXT, paper_filed INTEGER, pages INTEGER,
        text_source TEXT,               -- native / ocr / none
        status TEXT,                    -- figures / mentioned_no_figures / dc_only / no_pension_note / no_filing / error
        obligation REAL, assets REAL, net REAL, unit TEXT, page INTEGER, evidence TEXT, checked_at TEXT)""")


# ---------------------------------------------------------------- fetching

def latest_accounts(num):
    fh = _get(f"/company/{num}/filing-history", {"category": "accounts", "items_per_page": 10}) or {}
    for it in fh.get("items", []):
        # only real accounts: not a change of accounting reference date (type AA01, description
        # "change-account-reference-date-..."), and not interim or initial accounts (filed to justify a
        # dividend), none of which have a pension note
        desc = it.get("description", "")
        if it.get("type", "").startswith("AA") and (it.get("links") or {}).get("document_metadata") \
                and desc.startswith("accounts") and not re.search(r"interim|initial", desc):
            return it
    return None


TYPES = {"application/pdf": "pdf", "application/xhtml+xml": "xhtml", "application/zip": "zip"}


def fetch_doc(num, item):
    """(bytes, kind) of the filing in the best format offered: PDF, else XHTML (iXBRL), else a zip of iXBRL."""
    for kind in TYPES.values():
        dest = PDF_DIR / f"{num}_{item['date']}.{kind}"
        if dest.exists():
            return dest.read_bytes(), kind
    meta = item["links"]["document_metadata"]
    offered = (_doc_get(meta).json() or {}).get("resources", {})
    mime = next((m for m in TYPES if m in offered), None)
    if not mime:
        raise ValueError(f"no usable format: {', '.join(offered) or 'none'}")
    r = _doc_get(meta + "/content", mime)
    data = r.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(data) > MAX_BYTES:
        raise ValueError("document too large")
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    (PDF_DIR / f"{num}_{item['date']}.{TYPES[mime]}").write_bytes(data)
    return data, TYPES[mime]


def _doc_get(url, accept=None):
    for attempt in range(4):
        try:
            r = requests.get(url, auth=(api_key(), ""), headers={"Accept": accept} if accept else {}, timeout=180,
                             stream=bool(accept))
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(60)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("download failed")


def _xhtml(data, kind):
    """The iXBRL document itself, unpacking a zip if need be (largest .xhtml/.html inside)."""
    if kind == "zip":
        z = zipfile.ZipFile(io.BytesIO(data))
        names = [i for i in z.infolist() if i.filename.lower().endswith((".xhtml", ".html", ".htm"))]
        return z.read(max(names, key=lambda i: i.file_size)) if names else b""
    return data


# ---------------------------------------------------------------- text

def _ocr_page(page):
    return _ocr_tesseract(page) if OCR_ENGINE == "tesseract" else _ocr_vision(page)


def _ocr_tesseract(page):
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    img = page.render(scale=300 / 72).to_pil().convert("L")        # 300 dpi greyscale, Tesseract's sweet spot
    return pytesseract.image_to_string(img, lang="eng", config=f"--psm {TESSERACT_PSM}")


def _ocr_vision(page, fast=False):
    import objc
    with objc.autorelease_pool():               # Vision objects pile up otherwise: long runs run out of memory
        return _ocr_vision_inner(page, fast)


def _ocr_vision_inner(page, fast=False):
    import Vision
    from Foundation import NSData
    buf = io.BytesIO()
    page.render(scale=2.5).to_pil().save(buf, "PNG")
    png = buf.getvalue()
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(1 if fast else 0)  # accurate; "fast" misses most of a pension note (fine for spotting one)
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(png, len(png)), None)
    h.performRequests_error_([req], None)
    # Rebuild printed lines: group fragments by height, then read each line left to right
    obs = sorted(req.results() or [], key=lambda o: -o.boundingBox().origin.y)
    lines, cur, y = [], [], None
    for o in obs:
        oy = o.boundingBox().origin.y
        if y is not None and abs(oy - y) > 0.006:
            lines.append(cur)
            cur = []
        cur.append(o)
        y = oy
    lines.append(cur)
    return "\n".join("  ".join(o.topCandidates_(1)[0].string() for o in sorted(ln, key=lambda o: o.boundingBox().origin.x))
                     for ln in lines if ln)


def page_texts(num, item, data, kind="pdf", max_pages=MAX_PAGES, cache_dir=None):
    """[(page_no, text)], text source. Cached as one file per filing with form feeds between pages."""
    cache_dir = cache_dir or PDF_DIR
    cache = cache_dir / f"{num}_{item['date']}.txt"
    if cache.exists():
        raw = cache.read_text()
        src, _, body = raw.partition("\n")
        return list(enumerate(body.split("\f"))), src
    if kind != "pdf":
        root = lxml.html.fromstring(_xhtml(data, kind) or b"<html/>")
        for el in root.iter("style", "script"):
            el.drop_tree()
        # one line per innermost block (rendered PDFs put each printed line in its own div)
        block = {"div", "p", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td"}
        lines = [" ".join(el.text_content().split()) for el in root.iter()
                 if isinstance(el.tag, str) and el.tag.split("}")[-1] in block
                 and not any(isinstance(c.tag, str) and c.tag.split("}")[-1] in block for c in el)]
        text = "\n".join(ln for ln in lines if ln)
        cache.write_text("xhtml\n" + text)
        return [(0, text)], "xhtml"
    doc = pdfium.PdfDocument(data)
    n = min(len(doc), max_pages)
    native = [doc[i].get_textpage().get_text_range() for i in range(n)]
    if sum(len(t.strip()) for t in native) > 200 * n:
        texts, src = native, "native"
    else:
        texts, src = [_ocr_page(doc[i]) for i in range(n)], "ocr"
    cache.write_text(src + "\n" + "\f".join(texts))
    if KEEP_PDFS is False:
        (cache_dir / f"{num}_{item['date']}.pdf").unlink(missing_ok=True)   # the text is all we use; saves ~4 MB each
    return list(enumerate(texts)), src


# ---------------------------------------------------------------- figures

# Movement-table and narrative lines that mention the same words but are not the balance-sheet figure
NOT_BALANCE = re.compile(r"beginning|start of|opening|brought forward|\bat 1(?:st)? [a-z]|return|interest|remeasure"
                         r"|expected|movement|change|cost|income|gain|loss|measured in accordance|hierarchy|discount"
                         r"|sensitiv|assumption|increase|decrease|expense|paid|contribution|transfer"
                         r"|duration|\byears\b|valuation|funding level|experience|adjustment|percent|%|derecogni|arising on|restriction", re.I)
TABULAR = re.compile(r"^[\s.:;,|‘'’)\-–—]{0,6}(?:[£€E]\s?)?\(?-?\d")    # the amount follows the label directly
NOT_AMOUNT_BEFORE = re.compile(r"(?:FRS|IAS|IFRS|note|notes|section|page|para(?:graph)?)\s*$", re.I)
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
DAY_AFTER = re.compile(r"^(?:st|nd|rd|th)?\s*" + MONTH, re.I)        # "31 December", "1st January"
DAY_BEFORE = re.compile(r"\b(?:at|on|to|as at)\s*$", re.I)              # "... as at 31" (month on the next line)
# Column-heading unit tokens. A bare "million" is left out: narrative ("£21.9 million") is not a heading.
UNIT_M = re.compile(r"(?<![\d.,])[£€Ef$]\s?['’]?m\b|£\s?['’]?millions?\b|\bin millions\b|\bEm\b")
UNIT_K = re.compile(r"[£€Ef$]\s?['’‘]?0{3}\b|£\s?k\b|['’‘]000\b|(?<![\d,.:;])000s?\b|\bthousands\b")


UNIT_BN = re.compile(r"(?<![\d.,])[£€Ef$]\s?['’]?bn\b|£\s?billions?\b|\bin billions\b|\bEbn\b")
MULT = {"bn": 1e9, "m": 1e6, "m?": 1e6, "000": 1e3, "1": 1}


def _unit_at(lines, i):
    """Unit of line i: the nearest column heading on or above it; failing that, the page's commoner heading."""
    for ln in reversed(lines[max(0, i - 60):i + 1]):
        k, m, b = UNIT_K.search(ln), UNIT_M.search(ln), UNIT_BN.search(ln)
        if k or m or b:
            first = min((x.start(), u) for x, u in ((k, "000"), (m, "m"), (b, "bn")) if x)
            return first[1]
    text = "\n".join(lines)
    m, k = len(UNIT_M.findall(text)), len(UNIT_K.findall(text))
    if m or k:
        return "m" if m > k else "000"
    return _doc_unit            # no heading on the page: the document's, or whole pounds if it has none


_doc_unit = "000"


def _set_doc_unit(pages):
    """Unit for pages with no heading of their own: £'000 as before, but whole pounds ("1") when the document has
    no £'000 or £m heading anywhere (small companies' accounts: "Defined benefit obligation 855,000")."""
    global _doc_unit
    text = "\n".join(t for _, t in pages)
    m, k = len(UNIT_M.findall(text)), len(UNIT_K.findall(text))
    _doc_unit = "000" if (m or k) else "1"     # the document's commoner heading proved unreliable (prose "£m")


# OCR letter-for-digit slips inside amounts: Vision reads 5 as S, Tesseract as §; O for 0; l or I for 1
_S5 = re.compile(r"(?:(?<=^)|(?<=[\s(\-]))[S§](?=[,:;]?\d)")
_O0 = re.compile(r"(?<=\d)[Oo](?=\d)|(?<=[,:;])[Oo](?=\d\d)|(?<=[,:;]\d)[Oo](?=\d)|(?<=[,:;]\d\d)[Oo](?!\w)")
_L1 = re.compile(r"(?<=\d)[lI](?=\d)|(?<=[,:;])[lI](?=\d\d)")


DOLLAR_PAGE = re.compile(r"\$\s?'?(?:000|m\b|million)|US\$|\bUSD\b|dollar", re.I)
DOLLAR5 = re.compile(r"(?:(?<=^)|(?<=[\s(\-]))\$(?=\d{1,2}[,:;]\d{3})")


def fix_digits(s):
    return _L1.sub("1", _O0.sub("0", _S5.sub("5", s)))


def _first_amount(s, totals=True):
    """(value, unit) of the first amount in s; unit inferred from the token where it can be. With totals, a row
    whose first figures add up to a later one (a multi-scheme table) gives that total."""
    s = fix_digits(s)
    toks = list(NUM.finditer(s))
    for i, m in enumerate(toks):
        tok = m.group(0)
        if NOT_AMOUNT_BEFORE.search(s[max(0, m.start() - 12):m.start()]):
            continue
        bare = tok.strip("()-")
        v = re.sub(r"[,:;]", "", bare)
        if re.fullmatch(r"(19|20)\d\d", bare):
            continue                            # years ("2,012" is an amount, "2012" a year)
        try:
            x = float(v)
        except ValueError:
            continue
        groups = len(re.findall(r"[,:;]\d{3}", bare))
        if not groups and "." not in bare:
            if x < 10:
                continue                        # note numbers
            if x <= 31 and (DAY_AFTER.match(s[m.end():]) or DAY_BEFORE.search(s[:m.start()])):
                continue                        # day of a date
            if x < 100 and any(re.search(r"[,:;]\d{3}", t.group(0)) for t in toks[i + 1:]):
                continue                        # a note reference before the real (comma-grouped) amounts
        after = s[m.end():m.end() + 2].lower()
        unit = ("1" if groups >= 2 else "bn" if after.startswith(("bn", " bn", "b ", " billion"))
                else "m" if after.startswith(("m", " m"))
                else "m~" if "." in bare and not groups           # a decimal: £m unless the heading says £bn
                else "000" if after.startswith(("k", " k"))
                else "1" if groups and re.search(r"[£€]\s?$", s[max(0, m.start() - 2):m.start()]) else None)
                # "£60,400" in narrative is pounds; table columns carry the unit in their heading instead
        plain = not groups and "." not in bare and x >= 1000      # "2397": in a £m table, likely 239.7 misread
        # multi-scheme tables: "4,598  5,449  10,047" = scheme A, scheme B, total -> take the total
        vals = []
        for t in toks[i:i + 6]:
            b = t.group(0).strip("()-")
            try:
                vals.append(float(re.sub(r"[,:;]", "", b)))
            except ValueError:
                break
        for k in (range(2, len(vals)) if totals else ()):
            if all(v > 0 for v in vals[:k + 1]) and abs(sum(vals[:k]) - vals[k]) <= 0.002 * vals[k] + 1:
                x = vals[k]
                break
        return (-x if tok.startswith(("(", "-")) else x), unit, plain
    return None, None, False


def extract(pages):
    """Best page = the one with the most balance-sheet DB lines. Amounts are GBP; the scans read £ as E or €."""
    best, prev_db = None, False
    pages = list(pages)
    _set_doc_unit(pages)
    for no, text in pages:
        here = bool(DB_MENTION.search(text))
        # a note's table often runs onto the next page, which may not repeat "defined benefit"
        if not (here or (prev_db and PENSION_WORD.search(text))):
            prev_db = here
            continue
        prev_db = here
        dollars = DOLLAR_PAGE.search(text)
        found, ev, prose, rests = {}, [], {}, {}
        lines = text.splitlines()
        for li, line in enumerate(lines):
            if not dollars:                     # Tesseract reads a leading 5 as $: "$46,270" for 546,270
                line = DOLLAR5.sub("5", line)
            if NOT_BALANCE.search(line):
                continue
            # a "net ..." line is the net figure even when it also says "obligation" or "assets"
            keys = ["net"] if LINES["net"].search(line) else ["obligation", "assets"]
            for key in keys:
                rx = LINES[key]
                if key not in found and rx.search(line):
                    rest = re.sub(r"^[A-Za-z]+", "", rx.split(line, 1)[-1])   # rest of a word cut short: "liabilit|ies"
                    v, unit, plain = _first_amount(rest)
                    if v is not None and abs(v) >= 1 and not (key == "assets" and abs(v) == 100):  # 100%
                        if unit == "m~":
                            unit = "bn" if _unit_at(lines, li) == "bn" else "m"
                        unit = unit or _unit_at(lines, li)
                        hit = ((v, "m?" if unit == "m" and plain else unit), line.strip()[:160])
                        if TABULAR.match(rest):
                            found[key] = hit[0]
                            rests[key] = (rest, li)
                            ev.append(hit[1])
                        else:                   # a sentence: used only if no table line gives this figure
                            prose.setdefault(key, hit)
        for key, (val, line) in prose.items():
            if key not in found:
                found[key] = val
                ev.append(line)
        # a total for one of the pair but not the other would compare a group total with one scheme: use first columns
        if {"obligation", "assets"} <= set(rests):
            first = {k: _first_amount(rests[k][0], totals=False)[0] for k in ("obligation", "assets")}
            is_total = {k: first[k] is not None and abs(first[k]) != abs(found[k][0]) for k in first}
            plain_row = [k for k in first if not is_total[k]]
            multi_col = plain_row and len([t for t in NUM.finditer(rests[plain_row[0]][0])
                                           if re.search(r"\d[,.:;]?\d", t.group(0))]) >= 2
            if is_total["obligation"] != is_total["assets"] and multi_col:    # a one-figure row is already a total
                for k in first:
                    if first[k] is not None:
                        found[k] = (first[k], found[k][1])
        # a page with both beats one with net only. Between pages that both give obligation and assets (often
        # separate schemes, e.g. a UK scheme and a small German one), the bigger obligation wins; otherwise a page
        # that itself says "defined benefit"
        both = {"obligation", "assets"} <= set(found)
        size = 0
        if both and {"obligation", "assets"} <= set(rests):            # both from table lines, not sentences
            o, a = (abs(found[k][0]) * MULT[found[k][1] or "1"] for k in ("obligation", "assets"))
            if o and a and max(o, a) <= 3 * min(o, a):                  # a consistent pair, not a stray number
                size = o
        rank = (both, len(found), size, here)
        if found and (best is None or rank > best["rank"]):
            best = {"found": found, "page": no, "evidence": " | ".join(ev), "rank": rank}
    if best and "obligation" in best["found"] and "assets" in best["found"]:
        mult = MULT
        o, a = (abs(best["found"][k][0]) * mult[best["found"][k][1]] for k in ("obligation", "assets"))
        if max(o, a) > 20 * min(o, a):          # one of the pair is a stray number; keep the larger
            del best["found"]["obligation" if o < a else "assets"]
    return best


def assess(num, max_pages=MAX_PAGES):
    item = latest_accounts(num)
    if not item:
        return dict(status="no_filing")
    base = dict(filing_date=item.get("date"), description=item.get("description"),
                paper_filed=int(bool(item.get("paper_filed"))), pages=item.get("pages"))
    if (PDF_DIR / f"{num}_{item['date']}.txt").exists() and not any(
            (PDF_DIR / f"{num}_{item['date']}.{k}").exists() for k in ("xhtml", "zip")):
        data, kind = None, "pdf"                # text already extracted; the PDF itself has been deleted
    else:
        data, kind = fetch_doc(num, item)
    tagged = None
    if kind != "pdf":                           # tagged figures beat text scraping
        h = headline(parse_ixbrl(_xhtml(data, kind)))
        if h and any(h.get(k) for k in ("obligation", "assets", "net")):
            tagged = dict(base, status="figures", text_source="ixbrl", unit="1", page=0,
                          evidence=f"iXBRL tags at {h['period_end']}", obligation=h.get("obligation"),
                          assets=h.get("assets"), net=h.get("net"))
            if h.get("obligation") and h.get("assets"):
                return tagged
    pages, src = page_texts(num, item, data, kind, max_pages)
    full = "\n".join(t for _, t in pages)
    base["text_source"] = src if full.strip() else "none"
    hit = extract(pages)
    if tagged and not (hit and {"obligation", "assets"} & set(hit["found"])):
        return tagged                           # listed groups often tag only the net balance-sheet line
    if hit:
        mult = MULT
        f = {k: v * mult[u] for k, (v, u) in hit["found"].items()}
        return dict(base, status="figures", page=hit["page"], evidence=hit["evidence"],
                    unit=",".join(sorted({u for _, u in hit["found"].values()})),
                    obligation=abs(f["obligation"]) if "obligation" in f else None,
                    assets=abs(f["assets"]) if "assets" in f else None,
                    net=f.get("net") if "net" in f else (tagged or {}).get("net"))
    if DB_MENTION.search(full):
        m = DB_MENTION.search(full)
        return dict(base, status="mentioned_no_figures", evidence=full[max(0, m.start() - 120): m.end() + 200])
    return dict(base, status="dc_only" if DC_ONLY.search(full) else "no_pension_note")


LONG_MAX_BYTES = 200_000_000
LONG_PAGES = 600
LONG_HINT = re.compile(r"pen[s5]i[o0]n|defined benefit|benefit ob|plan [a@]ss|scheme [a@]ss|actuar|retirement benefit",
                       re.I)            # loose on purpose: fast OCR reads "Pensions" as "Penslons" and the like


def extend_long(con, companies=None, limit=None):
    """Read the pages beyond MAX_PAGES of long annual reports (big groups' pension notes often sit after page 200).
    Each extra page gets a fast OCR pass; pages that mention pensions, and the page after each, are then read
    accurately. The text is appended to the saved file, so page numbers stay right, and the figures re-extracted."""
    global MAX_BYTES
    rows = con.execute("""SELECT company_number, pages FROM pdf_accounts WHERE pages > ? AND pages <= ?
                          AND text_source IN ('ocr', 'native')
                          AND substr(company_number, 1, 2) NOT IN ('FC', 'SF', 'NF', 'BR')     -- overseas parents' reports
                          AND company_number IN (SELECT sponsor_company_number FROM schemes WHERE likely_live = 1 OR live_doubt IS NOT NULL
                                                 UNION SELECT group_db_company_number FROM sponsors
                                                 UNION SELECT parent_number FROM company_parents)
                          ORDER BY pages""", (MAX_PAGES, LONG_PAGES)).fetchall()
    if companies is not None:
        rows = [r for r in rows if r[0] in set(companies)]
    rows = rows[:limit] if limit else rows
    cols = ["filing_date", "description", "paper_filed", "pages", "text_source", "status", "obligation", "assets",
            "net", "unit", "page", "evidence"]
    for k, (num, pages) in enumerate(rows, 1):
        item = latest_accounts(num)
        cache = item and PDF_DIR / f"{num}_{item['date']}.txt"
        if not cache or not cache.exists():
            continue
        src, _, body = cache.read_text().partition("\n")
        have = body.split("\f")
        if len(have) >= pages:
            continue
        t0 = time.time()
        old_max, MAX_BYTES = MAX_BYTES, LONG_MAX_BYTES
        try:
            data, kind = fetch_doc(num, item)
        except Exception as e:                  # noqa: BLE001
            print(f"[{k}/{len(rows)}] {num} download failed: {e}", flush=True)
            continue
        finally:
            MAX_BYTES = old_max
        doc = pdfium.PdfDocument(data)
        del data
        extra, scanned, hits = {}, set(), set()
        for i in range(len(have), len(doc)):     # pass 1: native text, or a fast OCR to spot the pension pages
            native = doc[i].get_textpage().get_text_range()
            if len(native.strip()) > 200:
                extra[i] = native
                continue
            extra[i] = _ocr_vision(doc[i], fast=True) if OCR_ENGINE == "vision" else _ocr_page(doc[i])
            scanned.add(i)
            if LONG_HINT.search(extra[i]):
                hits.add(i)
        # pass 2: accurate OCR of the pension pages and their neighbours (notes run over; fast OCR garbles headings)
        for i in sorted({j for h in hits for j in range(h - 1, h + 3)} & scanned):
            if OCR_ENGINE == "vision":
                extra[i] = _ocr_vision(doc[i])
        extra = [extra[i] for i in sorted(extra)]
        doc.close()
        cache.write_text(src + "\n" + "\f".join(have + extra))
        (PDF_DIR / f"{num}_{item['date']}.pdf").unlink(missing_ok=True)
        r = assess(num)
        con.execute(f"UPDATE pdf_accounts SET {', '.join(c + '=?' for c in cols)} WHERE company_number=?",
                    [*[r.get(c) for c in cols], num])
        con.commit()
        print(f"[{k}/{len(rows)}] {num} +{len(extra)} pages -> {r['status']} "
              f"{r.get('obligation') or ''} {r.get('assets') or ''} ({time.time() - t0:.0f}s)", flush=True)


# ---------------------------------------------------------------- pilot

def pilot_sample(con, n=100, seed=7):
    """Active, large/medium sponsors of likely-live non-DC schemes that never filed iXBRL and have no DB figures."""
    rows = [r[0] for r in con.execute("""
        SELECT DISTINCT sp.company_number FROM sponsors sp JOIN schemes s ON s.sponsor_company_number = sp.company_number
        WHERE lower(sp.company_status) = 'active' AND s.likely_live = 1 AND s.name_type <> 'DC_likely'
          AND s.needs_review = 0 AND sp.db_period_end IS NULL
          AND upper(replace(sp.accounts_category, '-', ' ')) IN ('FULL', 'GROUP', 'MEDIUM')
          AND sp.company_number NOT IN (SELECT company_number FROM accounts_files_processed)
        ORDER BY sp.company_number""")]
    random.Random(seed).shuffle(rows)
    return rows[:n] if n else rows


def unread_full_filers(con):
    """Active full/group/medium sponsors of likely-live non-DC schemes whose machine-readable filings had no DB tags
    and whose latest accounts haven't been read as a document. Their DB note is often untagged text."""
    return [r[0] for r in con.execute("""
        SELECT DISTINCT sp.company_number FROM sponsors sp JOIN schemes s ON s.sponsor_company_number = sp.company_number
        WHERE lower(sp.company_status) = 'active' AND s.likely_live = 1 AND s.name_type <> 'DC_likely'
          AND sp.db_period_end IS NULL AND sp.group_db_company_number IS NULL
          AND upper(replace(sp.accounts_category, '-', ' ')) IN ('FULL', 'GROUP', 'MEDIUM')
          AND sp.company_number NOT IN (SELECT company_number FROM pdf_accounts)
        ORDER BY sp.company_number""")]


def run(con, companies, shard=None, limit=None, pause=0):
    """shard = (i, n): this worker takes every n-th company, so n workers can OCR side by side.
    limit: stop after this many companies (run in batches). pause: seconds to rest between documents."""
    _schema(con)
    con.commit()
    done = {r[0] for r in con.execute("SELECT company_number FROM pdf_accounts")}
    todo = [c for c in companies if c not in done]
    if shard:
        i, n = shard
        todo = todo[i::n]
    if limit:
        todo = todo[:limit]
    t0 = time.time()
    for i, num in enumerate(todo, 1):
        try:
            r = assess(num)
        except Exception as e:                  # noqa: BLE001 - record and carry on
            r = dict(status="error", evidence=str(e)[:300])
        cols = ["filing_date", "description", "paper_filed", "pages", "text_source", "status", "obligation", "assets",
                "net", "unit", "page", "evidence"]
        con.execute(f"INSERT OR REPLACE INTO pdf_accounts (company_number, {', '.join(cols)}, checked_at) "
                    f"VALUES (?, {', '.join('?' * len(cols))}, datetime('now'))", [num, *[r.get(c) for c in cols]])
        con.commit()
        print(f"[{i}/{len(todo)}] {num} {r['status']} ({time.time() - t0:.0f}s)", flush=True)
        if pause:
            time.sleep(pause)


def reextract(con):
    """Re-run figure extraction for every document already read, from its saved text (no download, no OCR).
    Use after changing the extraction rules."""
    cols = ["filing_date", "description", "paper_filed", "pages", "text_source", "status", "obligation", "assets",
            "net", "unit", "page", "evidence"]
    rows = con.execute("SELECT company_number, status, obligation, assets, net, unit FROM pdf_accounts "
                       "WHERE status NOT IN ('error', 'no_filing')").fetchall()
    changed = 0
    for num, st, ob, assets, net, unit in rows:
        item = latest_accounts(num)
        if not item or not (PDF_DIR / f"{num}_{item['date']}.txt").exists():
            continue
        try:
            r = assess(num)
        except Exception:                       # noqa: BLE001 - keep the old result
            continue
        if (r["status"], r.get("obligation"), r.get("assets"), r.get("net"), r.get("unit")) != (st, ob, assets, net, unit):
            changed += 1
            con.execute(f"UPDATE pdf_accounts SET {', '.join(c + '=?' for c in cols)} WHERE company_number=?",
                        [*[r.get(c) for c in cols], num])
    con.commit()
    print(f"Re-extracted {len(rows):,} documents: {changed:,} changed")


# ---------------------------------------------------------------- merge into sponsors

def merge(con):
    """Copy PDF results onto sponsors. iXBRL figures always win, so this only fills sponsors with none.
    Run after accounts.refresh_sponsor_figures (which clears and rebuilds the iXBRL figures)."""
    _schema(con)
    if "db_pdf_status" not in {r[1] for r in con.execute("PRAGMA table_info(sponsors)")}:
        con.execute("ALTER TABLE sponsors ADD COLUMN db_pdf_status TEXT")   # figures / mentioned_no_figures / dc_only ...
    con.execute("UPDATE sponsors SET db_pdf_status = (SELECT status FROM pdf_accounts p "
                "WHERE p.company_number = sponsors.company_number)")
    # clear earlier PDF-derived figures so a re-read (pdf-reextract) replaces them; iXBRL figures are untouched
    con.execute("""UPDATE sponsors SET db_period_end=NULL, db_obligation=NULL, db_plan_assets=NULL,
                       db_surplus_deficit=NULL, db_net_basis=NULL, db_taxonomy=NULL, db_source_file=NULL
                   WHERE db_taxonomy LIKE 'pdf_%' OR db_taxonomy = 'hand_checked'""")
    n = 0
    for num, ob, assets, net, src, fdate, page in con.execute(
            """SELECT p.company_number, p.obligation, p.assets, p.net, p.text_source, p.filing_date, p.page
               FROM pdf_accounts p JOIN sponsors sp USING (company_number)
               WHERE p.status = 'figures' AND sp.db_period_end IS NULL""").fetchall():
        item = latest_accounts(num) or {}           # cached API response: gives the balance-sheet date
        period = item.get("action_date") or (item.get("description_values") or {}).get("made_up_date") or fdate
        if ob is not None and assets is not None:
            net, basis = assets - ob, "assets_minus_obligation"
        else:
            basis = "reported_net_sign_unverified" if net is not None else None
        con.execute("""UPDATE sponsors SET db_period_end=?, db_obligation=?, db_plan_assets=?, db_surplus_deficit=?,
                           db_net_basis=?, db_taxonomy=?, db_source_file=? WHERE company_number=?""",
                    (period, ob, assets, net, basis, "pdf_ixbrl" if src == "ixbrl" else f"pdf_{src}",
                     f"filing {fdate}, page {page}", num))
        n += 1
    con.commit()
    return n

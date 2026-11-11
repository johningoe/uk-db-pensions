"""Compare Tesseract with macOS Vision on accounts already read with Vision.

    python ocr_compare.py fetch      # pick 30 OCR'd companies, download their PDFs to raw/ocr_test/
    python ocr_compare.py run        # OCR them with Tesseract and compare figures with the Vision results

Needs the tesseract binary on PATH (or TESSERACT_CMD). Writes data/ocr_compare.csv.
"""
import csv
import json
import random
import sys
import time

import pypdfium2 as pdfium

from pensiondb import config as C, db
from pensiondb import pdfaccounts as P

TEST_DIR = C.RAW / "ocr_test"
SAMPLE = TEST_DIR / "sample.json"


def fetch():
    con = db.connect()
    rows = con.execute("""SELECT company_number, status FROM pdf_accounts
                          WHERE text_source = 'ocr' AND status IN
                          ('figures', 'mentioned_no_figures', 'dc_only', 'no_pension_note')""").fetchall()
    rnd = random.Random(11)
    by = {}
    for n, st in rows:
        by.setdefault(st, []).append(n)
    pick = (rnd.sample(by["figures"], 20) + rnd.sample(by["mentioned_no_figures"], 5)
            + rnd.sample(by.get("dc_only", []) + by.get("no_pension_note", []), 5))
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    for n in pick:
        item = P.latest_accounts(n)
        dest = TEST_DIR / f"{n}.pdf"
        if dest.exists():
            continue
        meta = item["links"]["document_metadata"]
        r = P._doc_get(meta + "/content", "application/pdf")
        dest.write_bytes(r.raw.read(P.MAX_BYTES + 1, decode_content=True))
        print("fetched", n, dest.stat().st_size // 1024, "KB", flush=True)
    SAMPLE.write_text(json.dumps(pick))


def _fmt(v):
    return "" if v is None else f"{v / 1e6:,.1f}"


def run():
    P.OCR_ENGINE = "tesseract"
    con = db.connect()
    pick = json.loads(SAMPLE.read_text())
    out, pages_total, t_total = [], 0, 0.0
    for n in pick:
        v = con.execute("SELECT status, obligation, assets, net FROM pdf_accounts WHERE company_number=?",
                        (n,)).fetchone()
        name = con.execute("SELECT company_name FROM sponsors WHERE company_number=?", (n,)).fetchone()
        doc = pdfium.PdfDocument((TEST_DIR / f"{n}.pdf").read_bytes())
        k = min(len(doc), P.MAX_PAGES)
        t0 = time.time()
        pages = list(enumerate(P._ocr_page(doc[i]) for i in range(k)))
        dt = time.time() - t0
        pages_total += k
        t_total += dt
        (TEST_DIR / f"{n}.tess.txt").write_text("\f".join(t for _, t in pages))
        full = "\n".join(t for _, t in pages)
        hit = P.extract(pages)
        if hit:
            mult = {"m": 1e6, "000": 1e3, "1": 1}
            f = {key: val * mult[u] for key, (val, u) in hit["found"].items()}
            t = dict(status="figures", obligation=abs(f["obligation"]) if "obligation" in f else None,
                     assets=abs(f["assets"]) if "assets" in f else None, net=f.get("net"))
        elif P.DB_MENTION.search(full):
            t = dict(status="mentioned_no_figures")
        else:
            t = dict(status="dc_only" if P.DC_ONLY.search(full) else "no_pension_note")

        def same(a, b):
            return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 0.005 * max(abs(a), abs(b), 1))

        agree = t["status"] == v[0] and all(same(t.get(c), x) for c, x in zip(("obligation", "assets", "net"), v[1:]))
        row = dict(company_number=n, company=(name or [""])[0], pages=k, secs=round(dt),
                   vision_status=v[0], tess_status=t["status"],
                   vision_obl=_fmt(v[1]), tess_obl=_fmt(t.get("obligation")),
                   vision_assets=_fmt(v[2]), tess_assets=_fmt(t.get("assets")),
                   vision_net=_fmt(v[3]), tess_net=_fmt(t.get("net")), agree=int(agree))
        out.append(row)
        print(row, flush=True)
    with open(C.DATA / "ocr_compare.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"\nagree {sum(r['agree'] for r in out)}/{len(out)}; "
          f"{pages_total} pages in {t_total:.0f}s ({t_total / max(pages_total, 1):.2f}s/page)")


if __name__ == "__main__":
    {"fetch": fetch, "run": run}[sys.argv[1]]()

"""Plausibility check on DB figures, mainly to catch misread units and currencies in PDF/OCR results.

Each sponsor with figures gets db_check:
  ok           nothing odd
  fixed_unit   obligation and assets were ~1,000x apart (one read in £m, the other in £'000): the larger is
               divided by 1,000, and the original values are kept in db_check_note
  flag:<why>   kept as read but not trusted: excluded from the summary's largest-figures table, and a flagged
               parent's figures are not passed down as group figures. Reasons:
                 too_large          obligation or assets above £50bn (more than any UK scheme)
                 too_small          obligation or assets under £20k (a unit probably missed)
                 assets_vs_obligation  one is more than 3x the other (a stray number on one line)
                 foreign_currency   the source page is mostly in dollars, euros or another currency
                 decimal_lost       a £m figure of 1,000+ printed without a comma, e.g. "2397" for 239.7
"""
import re

from . import pdfaccounts

MAX_GBP = 50e9
MIN_GBP = 20e3
RATIO = 3
UNIT_RATIO = (500, 2000)                        # ~1,000x apart = a £m / £'000 mix-up

# No "$<digit>" or bare "€": OCR reads a 5 as $, and £ as € or E. One stray token is not enough (MIN_FOREIGN).
FOREIGN = re.compile(r"US\$|\bUSD\b|\$\s?['’]?(?:m|000|million|bn)\b|\bEUR\b|\beuros?\b|€\s?million|\bbaht\b"
                     r"|\bTHB\b|\bJPY\b|\byen\b|HK\$|\bCHF\b|\bSEK\b|\bDKK\b|\bNOK\b|\bINR\b|\brupees?\b", re.I)
MIN_FOREIGN = 2
STERLING = re.compile(r"£|\bGBP\b|sterling|\bpence\b")


def ensure_columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(sponsors)")}
    for col in ("db_check", "db_check_note"):
        if col not in have:
            con.execute(f"ALTER TABLE sponsors ADD COLUMN {col} TEXT")


def _page_text(num, page):
    """The page the PDF figures came from, from the saved text (None if not available)."""
    if page is None:
        return None
    item = pdfaccounts.latest_accounts(num)
    f = item and pdfaccounts.PDF_DIR / f"{num}_{item['date']}.txt"
    if not f or not f.exists():
        return None
    pages = f.read_text().partition("\n")[2].split("\f")
    return pages[page] if page < len(pages) else None


def judge(ob, assets, page_text=None, unit=None):
    """(obligation, assets, check, note). Only the unit mix-up is corrected; everything else is flagged."""
    ob, assets = (abs(ob) if ob else ob), (abs(assets) if assets else assets)
    if ob and assets:
        hi, lo = max(ob, assets), min(ob, assets)
        if UNIT_RATIO[0] <= hi / lo <= UNIT_RATIO[1]:
            note = f"read as obligation {ob:,.0f}, assets {assets:,.0f}; larger divided by 1,000"
            ob, assets = (ob / 1000, assets) if ob > assets else (ob, assets / 1000)
            return ob, assets, "fixed_unit", note
    why = []
    vals = [v for v in (ob, assets) if v]
    if any(v > MAX_GBP for v in vals):
        why.append("too_large")
    if vals and max(vals) < MIN_GBP:
        why.append("too_small")
    if ob and assets and max(ob, assets) > RATIO * min(ob, assets):
        why.append("assets_vs_obligation")
    if unit and "m?" in unit:
        why.append("decimal_lost")
    n_foreign = len(FOREIGN.findall(page_text or ""))
    if n_foreign >= MIN_FOREIGN and n_foreign > len(STERLING.findall(page_text)):
        why.append("foreign_currency")
    return ob, assets, ("flag:" + ",".join(why)) if why else "ok", None


def run(con):
    """Check every sponsor's own figures. Run after pdfaccounts.merge (which rewrites them)."""
    ensure_columns(con)
    con.execute("UPDATE sponsors SET db_check = NULL, db_check_note = NULL")
    counts = {}
    for num, ob, assets, net, tax, page, unit in con.execute(
            """SELECT sp.company_number, sp.db_obligation, sp.db_plan_assets, sp.db_surplus_deficit, sp.db_taxonomy,
                      p.page, p.unit FROM sponsors sp LEFT JOIN pdf_accounts p USING (company_number)
               WHERE sp.db_period_end IS NOT NULL""").fetchall():
        text = _page_text(num, page) if (tax or "").startswith("pdf_") else None
        ob2, assets2, check, note = judge(ob, assets, text, unit if (tax or "").startswith("pdf_") else None)
        if check == "fixed_unit":
            net = assets2 - ob2
            con.execute("UPDATE sponsors SET db_obligation=?, db_plan_assets=?, db_surplus_deficit=? "
                        "WHERE company_number=?", (ob2, assets2, net, num))
        con.execute("UPDATE sponsors SET db_check=?, db_check_note=? WHERE company_number=?", (check, note, num))
        counts[check.split(":")[0]] = counts.get(check.split(":")[0], 0) + 1
    con.commit()
    return counts

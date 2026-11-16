"""Whether each sponsor's DB schemes, taken together, hold £50m-£500m of assets; and figures per scheme where they
can be told apart.

The target is sponsors (employers) with £50m-£500m of DB assets across all their schemes, from their own accounts.
build(con) writes, on every scheme row (each scheme carries its sponsor's band):

  schemes.band          in / candidate / out
  schemes.band_kind     the kind of reason, short enough to group by
  schemes.band_reason   the reason, with the figure behind it
  scheme_figures        a scheme's own figures where known: split by hand from the pension note
                        (scheme_figures.csv) or the sponsor's figures when it has only one live DB scheme

  in         the sponsor's own DB assets are £50m-£500m
  candidate  could be in the band and is not settled yet: a parent group's figure only, no figure from a sponsor
             that files full accounts, a figure that failed the checks, or a scheme with DB evidence whose sponsor
             is not yet found
  out        the sponsor's DB assets are outside the band, the parent group's total is under £50m, the sponsor
             files small or micro accounts or is insolvent or dissolved, or the scheme is not live, DC or shows
             no DB evidence

Where assets are missing the obligation stands in for size, and the reason says so.

    python run.py band          # also part of run.py status
"""
import csv
from pathlib import Path

from . import gaps

LOW, HIGH = 50e6, 500e6
GONE = {"liquidation", "administration", "in administration", "dissolved", "closed", "converted-closed", "receivership",
        "insolvency-proceedings", "voluntary-arrangement"}
CSV = Path(__file__).with_name("scheme_figures.csv")
CURRENCY_CSV = Path(__file__).with_name("sponsor_currency.csv")   # company_number, currency, note: for figures flagged foreign_currency
# approximate currency units per £1 in 2025, for placing a sponsor in or out of the band only (figures are not converted)
APPROX_RATE = {"USD": 1.35, "EUR": 1.17, "AUD": 2.05, "GBP": 1.0}
BAND_REVIEW = Path(__file__).with_name("sponsor_band_review.csv")   # company_number, band (in/out), note: a verdict read from the accounts where no sterling figure could be entered
LIVE_CHECKED = Path(__file__).with_name("live_checked.csv")   # psr, verdict (live), note: a liveness lead reviewed and set aside
FIELDS = ["psr", "scheme_name", "period_end", "obligation_m", "assets_m", "source", "note"]
LIVE = ("with_figures", "no_figures")
SMALL = {"total exemption full", "total exemption small", "small", "micro entity", "dormant", "no accounts filed",
         "unaudited abridged", "audited abridged", "filing exemption subsidiary", "partial exemption"}


def _cat(c):
    return (c or "").replace("-", " ").strip().lower()


def _m(x):
    return f"£{x / 1e6:,.0f}m" if x >= 10e6 else f"£{x / 1e6:,.1f}m"


def _size(ob, assets):
    """(size in £, note): assets, else the obligation standing in for them."""
    if assets is not None:
        return assets, ""
    if ob is not None:
        return ob, " (obligation; assets not known)"
    return None, ""


def _hand():
    if not CSV.exists():
        return {}
    with open(CSV, newline="") as f:
        num = lambda v: float(v) * 1e6 if (v or "").strip() else None
        return {r["psr"]: (num(r["obligation_m"]), num(r["assets_m"]), r["period_end"] or None, r["source"], r["note"])
                for r in csv.DictReader(f) if r.get("psr")}


def build(con):
    for col in ("band", "band_kind", "band_reason"):
        try:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {col} TEXT")
        except Exception:
            pass
    con.execute("DROP TABLE IF EXISTS scheme_figures")
    con.execute("""CREATE TABLE scheme_figures (psr TEXT PRIMARY KEY, obligation REAL, assets REAL, period_end TEXT,
                   basis TEXT, source TEXT, note TEXT)""")
    status = gaps.classify(con)
    rows = con.execute("""
        SELECT s.psr, s.sponsor_company_number, s.charity_source, s.charity_number, s.db_evidence,
               sp.accounts_category, sp.company_name,
               sp.db_period_end IS NOT NULL AND coalesce(sp.db_check, 'ok') NOT LIKE 'flag%',
               sp.db_obligation, sp.db_plan_assets, sp.db_period_end, sp.db_check, sp.db_taxonomy, sp.db_source_file,
               sp.group_db_company_name, sp.group_db_obligation, sp.group_db_plan_assets,
               s.charity_db_obligation, s.charity_db_assets, s.charity_fy_end, s.charity_db_source, s.charity_name,
               lower(coalesce(sp.company_status, '')), coalesce(sp.db_pdf_status, '')
        FROM schemes s LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number""").fetchall()
    # live DB schemes per employer and per register body: one scheme means the employer's figure is the scheme's
    per_co, per_reg = {}, {}
    for r in rows:
        if status[r[0]][0] in LIVE:
            if r[1]:
                per_co[r[1]] = per_co.get(r[1], 0) + 1
            elif r[3]:
                per_reg[(r[2], r[3])] = per_reg.get((r[2], r[3]), 0) + 1
    hand = _hand()
    rev = {}
    if BAND_REVIEW.exists():
        with open(BAND_REVIEW, newline="") as f:
            rev = {r["company_number"]: r for r in csv.DictReader(f)}
    cur = {}
    if CURRENCY_CSV.exists():
        with open(CURRENCY_CSV, newline="") as f:
            cur = {r["company_number"]: r for r in csv.DictReader(f)}
    figs, bands = [], []
    for (psr, co, rsrc, rno, evidence, acc, co_name, own, ob, assets, pe, chk, tax, src, gname, gob, gassets,
         cob, cassets, cfy, csrc, cname, costatus, pdfst) in rows:
        group, reason = status[psr]
        fig = None
        if psr in hand:
            hob, has, hpe, hsrc, hnote = hand[psr]
            fig = (hob, has, hpe, "scheme_note", hsrc, hnote)
        elif group in LIVE and co and own and per_co.get(co) == 1:
            fig = (ob, assets, pe, "employer_single", f"{co_name} ({co})", tax or "")
        elif group in LIVE and not co and rno and (cob is not None or cassets is not None) and per_reg.get((rsrc, rno)) == 1:
            fig = (cob, cassets, cfy, "register_single", f"{cname} ({rsrc} {rno})", csrc or "")
        if fig:
            figs.append((psr, *fig))
        bands.append((*_band(group, reason, fig, co, rno, evidence, acc, own, ob, assets, chk, gname, gob, gassets,
                             cob, cassets, per_co.get(co) if co else per_reg.get((rsrc, rno)), costatus, cur.get(co), rev.get(co), pdfst), psr))
    con.executemany("INSERT INTO scheme_figures VALUES (?,?,?,?,?,?,?)", figs)
    con.executemany("UPDATE schemes SET band=?, band_kind=?, band_reason=? WHERE psr=?", bands)
    con.commit()
    out = {}
    for b, *_ in bands:
        out[b] = out.get(b, 0) + 1
    out.update(_live_check(con))
    return {"scheme figures": len(figs), **out}


SIGNAL_TEXT = {"figures": "Recent DB figures in the employer's accounts", "buy_in": "Accounts describe a buy-in (still live)",
               "winding_up": "Accounts say a buy-out or wind-up is under way (live until it completes)"}


def _live_check(con):
    """schemes.live_check for target schemes: live (evidence the scheme still exists), check (a sign it may have been
    bought out or wound up: review it) or unknown. Built from scheme_liveness (run.py liveness)."""
    for col in ("live_check", "live_evidence"):
        try:
            con.execute(f"ALTER TABLE schemes ADD COLUMN {col} TEXT")
        except Exception:
            pass
    con.execute("UPDATE schemes SET live_check=NULL, live_evidence=NULL")
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='scheme_liveness'").fetchone():
        return {}
    cols = {r[1] for r in con.execute("PRAGMA table_info(scheme_liveness)")}
    tr = "l.trustee_status, l.trustee_evidence" if "trustee_status" in cols else "NULL, NULL"
    checked = {}
    if LIVE_CHECKED.exists():
        with open(LIVE_CHECKED, newline="") as f:
            checked = {r["psr"]: r for r in csv.DictReader(f)}
    upd, n = [], {}
    for psr, sig, names, ev, src, tst, tev in con.execute(f"""
            SELECT s.psr, l.signal, l.names, l.evidence, l.source, {tr} FROM schemes s
            LEFT JOIN scheme_liveness l ON l.psr = s.psr WHERE s.band IN ('in', 'candidate')"""):
        if psr in checked:
            chk, why = checked[psr]["verdict"], f"Reviewed: {checked[psr]['note']}"
        elif sig == "bought_out":
            chk, why = "check", f"Accounts may say it was bought out or wound up: {ev} ({src})"
        elif sig in SIGNAL_TEXT and (names or sig == "figures"):
            chk, why = "live", f"{SIGNAL_TEXT[sig]}: {ev}" + (f" ({src})" if src and sig != "figures" else "")
        elif tst == "dissolved":
            chk, why = "check", f"Trustee company dissolved (may only have been replaced): {tev}"
        elif tst == "active":
            chk, why = "live", f"Pension trustee company still active: {tev}"
        else:
            chk, why = "unknown", "No evidence either way in the saved accounts or trustee companies"
        upd.append((chk, why, psr))
        n[f"live_{chk}"] = n.get(f"live_{chk}", 0) + 1
    con.executemany("UPDATE schemes SET live_check=?, live_evidence=? WHERE psr=?", upd)
    con.commit()
    return n


def _band(group, reason, fig, co, rno, evidence, acc, own, ob, assets, chk, gname, gob, gassets, cob, cassets, n, costatus="", currency=None, review=None, pdfst=""):
    if group == "not_live":
        return "out", "Not live", "Not live"
    if group == "dc":
        return "out", "DC scheme", "DC scheme"
    if co and costatus in GONE:
        return "out", "Sponsor insolvent or dissolved", f"Sponsor's status at Companies House: {costatus}"
    if co and review and review["band"] in ("in", "out") and group != "not_identified":
        kind = "Sponsor's DB assets £50m-£500m" if review["band"] == "in" else "Out of band on review of the accounts"
        return review["band"], kind, f"Reviewed: {review['note']}"
    if group == "not_identified":
        if not evidence:
            return "out", "No employer found, no DB evidence", "No employer found and no DB evidence"
        if reason == "Public sector":
            return "candidate", "Public sector (scope to decide)", "Public-sector scheme with DB evidence (scope to decide)"
        return "candidate", "Employer not found, DB evidence", f"Employer not found; scheme has DB evidence ({reason.lower()})"
    # the sponsor's own DB figures, covering all its schemes
    if own or (not co and (cob is not None or cassets is not None)):
        size, how = _size(ob, assets) if own else _size(cob, cassets)
        if size is not None:
            schemes = f" ({n} live DB schemes)" if n and n > 1 else ""
            if size < LOW:
                return "out", "Sponsor's DB assets under £50m", f"Sponsor's DB assets {_m(size)}{how}{schemes}, under £50m"
            if size > HIGH:
                return "out", "Sponsor's DB assets over £500m", f"Sponsor's DB assets {_m(size)}{how}{schemes}, over £500m"
            return "in", "Sponsor's DB assets £50m-£500m", f"Sponsor's DB assets {_m(size)}{how}{schemes}"
    if co and currency and (chk or "").startswith("flag") and "foreign_currency" in chk and (ob is not None or assets is not None):
        size, how = _size(ob, assets)
        rate = APPROX_RATE.get(currency["currency"])
        if rate and size / rate >= 10e6:          # smaller than £10m for a flagged figure: more likely a unit misread
            size /= rate
            conv = "" if currency["currency"] == "GBP" else f" (converted from {currency['currency']} at about {rate}/£)"
            if size < LOW:
                return "out", "Sponsor's DB assets under £50m", f"Sponsor's DB assets about {_m(size)}{how}{conv}, under £50m"
            if size > HIGH:
                return "out", "Sponsor's DB assets over £500m", f"Sponsor's DB assets about {_m(size)}{how}{conv}, over £500m"
            return "in", "Sponsor's DB assets £50m-£500m", f"Sponsor's DB assets about {_m(size)}{how}{conv}; {currency['note']}"
    if co and (gob is not None or gassets is not None):
        size, how = _size(gob, gassets)
        if size < LOW:
            return "out", "Parent group total under £50m", f"Parent group's schemes total {_m(size)}{how}, under £50m"
        if size <= HIGH:
            return "in", "Parent group's DB assets £50m-£500m", f"Parent group {gname}: DB assets {_m(size)}{how} across the group's schemes"
        if pdfst != "figures":
            return "out", "Parent group's DB assets over £500m", f"Parent group {gname}: DB assets {_m(size)}{how} across the group's schemes, over £500m; the sponsor reports none of its own"
        return "candidate", "Parent group's figure only", f"Only the parent group's figure ({gname}, {_m(size)}{how}, over £500m); the sponsor's own accounts give a figure that failed the checks"
    if reason == "Accounts show no DB scheme":
        return "out", "Accounts show no DB scheme", "Employer's accounts show no DB scheme"
    if reason.startswith("Member of a multi-employer scheme"):
        return "out", "Member of a multi-employer scheme", "Member of a multi-employer scheme (no figure of its own)"
    if co and (chk or "").startswith("flag"):
        size, how = _size(ob, assets)
        if size is not None and size >= LOW / 2:
            return "candidate", "Figure failed the checks", f"Figure of {_m(size)}{how} failed the checks"
    if not co:
        if reason.startswith("Not read"):
            return "out", "Small charity or body", "Small charity or body, accounts not read"
        return "candidate", "Charity or other body, no figure yet", "Charity or other body, no figure yet"
    cat = _cat(acc)
    if cat == "no accounts filed":
        return "out", "Employer files small, micro or no accounts", "Employer files no accounts"
    if cat in SMALL:
        return "out", "Employer files small, micro or no accounts", f"Employer files {cat} accounts"
    if cat in ("", "null", "accounts type not available", "no accounts type available"):
        return "candidate", "No figure yet, employer files full accounts", "No figure yet; employer's accounts type not known"
    return "candidate", "No figure yet, employer files full accounts", f"No figure yet; employer files {cat} accounts"

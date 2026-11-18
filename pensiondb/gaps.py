"""Where each scheme stands: employer found or not, figures or not, and why.

classify(con) returns {psr: (group, reason)} for every scheme:

  with_figures     live DB/hybrid scheme, employer identified, DB figures from its accounts (own, group or register)
  no_figures       live DB/hybrid scheme, employer identified, no usable figure (reason says why)
  no_figure_due    live DB/hybrid scheme, employer identified, but no figure of its own is there to find: the sponsor
                   is dissolved or insolvent, its accounts show no DB scheme, or it is one member of a multi-employer
                   scheme
  not_identified   live DB/hybrid scheme with no employer found (reason: the kind of scheme, read from its name)
  dc               live scheme whose name says defined contribution (outside the database's scope)
  not_live         wound up, transferred to the PPF or FAS, or the sponsor is gone

The reasons for unidentified schemes come from rules on the scheme name plus the hand verdicts in
sponsor_name_review.csv, so they size each kind of gap rather than certify it.
"""
import re

GROUPS = {
    "with_figures": "Employer identified, with figures",
    "no_figures": "Employer identified, no figures",
    "no_figure_due": "Employer identified, no figure to find",
    "not_identified": "Employer not identified",
    "dc": "DC scheme (out of scope)",
    "not_live": "Not live",
}

# kinds of unidentified scheme, by name (first match wins)
KINDS = [
    ("Public sector", r"\bLGPS\b|local government|county council|borough|city council|police|fire ?(?:men|fighters)"
     r"|fire authority|public officers|superannuation \(|civil service|\bNHS\b|teachers'? pension|armed forces"
     r"|government of|parliament|judicial|\bact 19\d\d|research council|arts council|audit commission"
     r"|racial equality|reserve forces|greenwich hospital|legal services commission|livestock commission"
     r"|nature conservancy|schools council|royal ordnance|army careers|tec national|foyle fisheries"
     r"|architects registration|general dental"),
    ("Insurer product or master policy", r"\bpol(?:icy)?\.? ?no\b|stanplan|\bSCP\b|centralised|group retirement scheme"
     r"|growth plan|personal retirement|\bFPP\b|federated pension|\bpolicy\b|\bG\d{4,}|\b[A-Z]\d{3,}\b"),
    ("Multi-employer, industry-wide or master trust", r"\bsection\b|industry[- ]wide|mineworkers|railways pension"
     r"|electricity supply|merchant navy|\bESPS\b|\bTPT\b|\bSHPS\b|multi[- ]employer|associated employers"
     r"|participating employers|master ?trust|national employment savings|the pensions trust|complete pension"
     r"|amber pension|premier trust|fenchurch|workplace pension trust|workers pension trust|church workers"
     r"|church of england funded|church administrators|methodist ministers|united reformed church ministers"
     r"|scottish episcopal church pension|scottish housing association pension"
     r"|superannuation arrangements of the university of london|university of oxford staff"),
    ("Overseas employer, branch or foreign government", r"\bbranch\b|\bAG\b|\bS\.?A\.?\b|\bN\.?V\.?\b|\bGmbH\b|\bInc\b"
     r"|\bBank\b|airlines|banca|banque|bancaire|\bSpA\b|embassy|high commission|exchange service|commonwealth"
     r"|european commission|israeli|kuwait|battle monuments|air force|\bI P P F\b"),
    ("Non-company body with no register entry", r"institute|institution|association|society|union|federation"
     r"|council|college|university|school|trust\b|church|diocese|hospital|charity|chartered|royal|club|commission"
     r"|congress|guild|harbour|port of|port authority|conservancy|haven authority|irish lights|duchy|household"
     r"|privy purse|livery|company'?s? (?:staff )?pension|temple|gray's inn|faculty of advocates|assay"),
]
REVIEW = {"public_sector": "Public sector", "product": "Insurer product or master policy",
          "multi_employer": "Multi-employer, industry-wide or master trust",
          "non_company_body": "Non-company body with no register entry", "no_usable_name": "No usable name"}
NOT_FOUND = "Company or brand named, not found"
NONE_DUE = ("Sponsor dissolved, in liquidation or in administration", "Accounts show no DB scheme",
            "Member of a multi-employer scheme (no figure of its own)")


def kind(name, review=None):
    if review in REVIEW:
        return REVIEW[review]
    return next((k for k, rx in KINDS if re.search(rx, name or "", re.I)), NOT_FOUND)


def _no_figures(r):
    if r["co"]:
        if r["pdf"] in ("dc_only", "no_pension_note") and (r["db_check"] or "") == "flag:misread":
            return "Accounts show no DB scheme"         # a reviewer's no-DB verdict outranks the cleared figure
        if (r["db_check"] or "").startswith("flag"):
            return "Figure found but failed the checks"
        if re.search(r"dissolved|liquidation|administration|closed|receiver|insolven", r["co_status"]):
            return "Sponsor dissolved, in liquidation or in administration"
        if r["pdf"] == "mentioned_no_figures":
            return "Accounts mention a DB scheme, figure not readable"
        if r["pdf"] == "multi_employer":
            return "Member of a multi-employer scheme (no figure of its own)"
        if r["pdf"] == "group_plan":
            return "Group scheme: figures are in the parent's accounts"
        if r["pdf"] in ("dc_only", "no_pension_note"):
            return "Accounts show no DB scheme"
        if re.search(r"dormant|no accounts", r["acc_cat"]):
            return "Sponsor dormant or files no accounts"
        if re.search(r"small|micro|exempt|unaudited|filleted", r["acc_cat"]):
            return "Small-company accounts (no pension note)"
        return "Other, or not yet read"
    if r["ch_src"]:
        return "Figure found but failed the checks"
    if r.get("ch_source") == "body":
        return "Accounts read, no gross figure (net only or not published)"
    st = r["ch_status"]
    if st == "mentioned_no_figures":
        return "Accounts mention a DB scheme, figure not readable"
    if st in ("dc_only", "no_pension_note"):
        return "Accounts show no DB scheme"
    if st == "no_filing":
        return "No accounts available"
    return "Not read (small charity, inactive, or return without accounts)"


def _not_live(r):
    if r["ppf"] == "Transferred":
        return "Transferred to the PPF"
    if "Transferred" in (r["fas"] or ""):
        return "Transferred to the FAS"
    if r["doubt"] == "accounts_no_db":
        return "Sponsor's accounts show no DB scheme"
    if r["doubt"] in ("sponsor_dormant", "sponsor_no_accounts"):
        return "Sponsor dormant or files no accounts"
    if (r["doubt"] or "").startswith("reviewed:"):
        return {"reviewed:bought_out": "Bought out (reviewed)", "reviewed:wound_up": "Wound up (reviewed)",
                "reviewed:ppf": "In the PPF (reviewed)"}.get(r["doubt"], "Not live (reviewed)")
    if r["dissolved"]:
        return "Sponsor dissolved"
    return "Other"


def classify(con):
    q = """SELECT s.psr, s.scheme_name, s.likely_live, s.name_type, s.sponsor_company_number co, s.charity_number ch,
               s.charity_db_source ch_src, s.charity_source ch_source, coalesce(s.charity_db_check, 'ok') ch_check, s.sponsor_name_review review,
               s.ppf_status ppf, s.fas_status fas, s.live_doubt doubt, s.sponsor_dissolved dissolved,
               sp.db_period_end IS NOT NULL AND coalesce(sp.db_check, 'ok') NOT LIKE 'flag%' own,
               sp.group_db_company_number IS NOT NULL grp, sp.db_check, lower(coalesce(sp.company_status, '')) co_status,
               coalesce(sp.db_pdf_status, '') pdf, lower(coalesce(sp.accounts_category, '')) acc_cat,
               (SELECT status FROM charity_accounts a WHERE a.source = s.charity_source AND a.number = s.charity_number)
                   ch_status
           FROM schemes s LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number"""
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    out = {}
    for row in cur:
        r = dict(zip(cols, row))
        if not r["likely_live"]:
            out[r["psr"]] = ("not_live", _not_live(r))
        elif r["name_type"] == "DC_likely":
            out[r["psr"]] = ("dc", "Name says defined contribution")
        elif r["co"] and r["own"]:
            out[r["psr"]] = ("with_figures", "Sponsor's own accounts")
        elif r["co"] and r["grp"]:
            out[r["psr"]] = ("with_figures", "Parent group's accounts")
        elif not r["co"] and r["ch_src"] and not r["ch_check"].startswith("flag"):
            out[r["psr"]] = ("with_figures", "Charity, union or mutual accounts")
        elif r["co"] or r["ch"]:
            why = _no_figures(r)
            out[r["psr"]] = ("no_figure_due" if why in NONE_DUE else "no_figures", why)
        else:
            out[r["psr"]] = ("not_identified", kind(r["scheme_name"], r["review"]))
    return out


# ---------------------------------------------------------------- how far to trust a figure

QUALITY = {
    "hand": "Checked by hand",
    "tagged": "Tagged in the accounts",
    "read": "Read from the accounts",
    "check": "Needs checking",
}
TAGGED = {"frs102", "uk-gaap", "pdf_ixbrl", "pdf_xhtml"}


def quality(source, how, obligation, assets, period_end, accounts_category=None, built=None):
    """(label key, [reasons]) for a set of DB figures.

    source             own / group / register
    how                db_taxonomy (own), group_db_source (group), or charity_db_source (register: accounts or
                       annual_return)
    accounts_category  the reporting company's accounts type, to catch a £m report read as £'000

    Hand-checked figures are trusted as they are. Others need checking when only one of obligation and assets (or only the net) was
    found, the year end is more than three years before the build date, a company filing full or group accounts
    reports under £1m (a £m table read as £'000 is the usual cause), or assets are under half or over twice the
    obligation."""
    from datetime import date
    if how == "hand_checked":
        return "hand", []
    why = []
    if how == "annual_return":
        why.append("net figure only (charity annual return)")
    elif obligation is None and assets is None:
        why.append("net figure only (no obligation or assets found)")
    elif obligation is None or assets is None:
        why.append("only one of obligation and assets found")
    built = built or date.today().isoformat()
    if period_end and period_end[:10] < f"{int(built[:4]) - 3}{built[4:10]}":
        why.append(f"old figures (year end {period_end[:10]})")
    big = max(v for v in (obligation, assets, 0) if v is not None)
    if source == "own" and (accounts_category or "").lower() in ("full", "group") and 0 < big < 1e6:
        why.append("under £1m for a company filing full accounts: check the unit")
    if obligation and assets and not 0.5 <= assets / obligation <= 2:
        why.append(f"assets {assets / obligation:.0%} of the obligation")
    if why:
        return "check", why
    return ("tagged" if how in TAGGED else "read"), []

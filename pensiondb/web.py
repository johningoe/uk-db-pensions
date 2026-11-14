"""A single-page explorer of the database: data/explorer.html, a self-contained page (data embedded) to open in a
browser. Search, filter and sort every scheme, see why each has or lacks a sponsor and figures, open a scheme for
its full record and source links, and roll schemes up by sponsor.

    python run.py web            # writes data/explorer.html
"""
import json
from datetime import date
from pathlib import Path

from . import config as C
from . import gaps

TEMPLATE = Path(__file__).with_name("explorer_template.html")
OUT = C.DATA / "explorer.html"

# one row per scheme, as arrays in this column order (keeps the page small)
COLS = ["psr", "name", "postcode", "city", "name_type", "is_section", "group", "reason",
        "co", "co_name", "co_status", "co_accounts", "match_method", "match_conf", "needs_review",
        "derived", "name_review", "named_in_accounts",
        "reg", "reg_number", "reg_name",
        "source", "fig_body", "fig_body_no", "period_end", "obligation", "assets", "fig_check", "fig_where",
        "ppf", "fas", "live_doubt", "parent_top", "parent_country"]


def _rows(con):
    status = gaps.classify(con)
    q = """
    SELECT s.psr, s.scheme_name, s.post_code, s.city, s.name_type, s.is_section,
           s.sponsor_company_number, sp.company_name, sp.company_status, sp.accounts_category,
           s.match_method, s.match_confidence, s.needs_review,
           s.sponsor_name_derived, s.sponsor_name_review,
           s.sponsor_named_in_accounts,
           s.charity_source, s.charity_number, s.charity_name,
           sp.db_period_end IS NOT NULL AND coalesce(sp.db_check, 'ok') NOT LIKE 'flag%',
           sp.db_period_end, sp.db_obligation, sp.db_plan_assets, sp.db_check, sp.db_source_file, sp.db_taxonomy,
           sp.group_db_company_number, sp.group_db_company_name, sp.group_db_period_end, sp.group_db_obligation,
           sp.group_db_plan_assets, sp.group_db_source,
           s.charity_db_source, s.charity_fy_end, s.charity_db_obligation, s.charity_db_assets, s.charity_db_pension,
           s.charity_db_check, a.doc, a.page,
           s.ppf_status, s.fas_status, s.live_doubt, sp.parent_top_name, sp.parent_top_country
    FROM schemes s
    LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
    LEFT JOIN charity_accounts a ON a.source = s.charity_source AND a.number = s.charity_number"""
    out = []
    for (psr, name, pc, city, nt, sec, co, co_name, co_st, co_acc, mm, mc, nr, derived, review, named,
         reg, reg_no, reg_name, own, pe, ob, assets, chk, src_file, tax, gco, gname, gpe, gob, gassets, gsrc,
         csrc, cfy, cob, cassets, cnet, cchk, cdoc, cpage, ppf, fas, doubt, ptop, pctry) in con.execute(q):
        group, reason = status[psr]
        fig = [None] * 8
        if co and own:
            where = src_file or "" if tax == "hand_checked" else f"{tax or ''}: {src_file or ''}".strip(": ")
            fig = ["own", co_name, co, pe, ob, assets, chk or "ok", where]
        elif co and gco:
            fig = ["group", gname, gco, gpe, gob, gassets, "ok", gsrc or ""]
        elif csrc:
            where = f"{cdoc} (page {cpage + 1})" if cdoc and cpage is not None else cdoc or ""
            if cob is None and cassets is None and cnet is not None:
                where = f"Charity Commission annual return: net {cnet:,.0f} only"
            fig = ["register", reg_name, reg_no, cfy, cob, cassets, cchk or "ok", where]
        elif co and chk and chk.startswith("flag"):
            fig = ["own", co_name, co, pe, ob, assets, chk, f"{tax or ''}: {src_file or ''}".strip(": ")]
        out.append([psr, name, pc, city, nt, sec, group, reason,
                    co, co_name, co_st, co_acc, mm, mc, nr,
                    derived, review, named,
                    reg, reg_no, reg_name, *fig, ppf, fas, doubt, ptop, pctry])
    return out


def export(con, out=OUT):
    rows = _rows(con)
    payload = {"cols": COLS, "rows": rows, "groups": gaps.GROUPS, "built": date.today().isoformat()}
    data = json.dumps(payload, separators=(",", ":"), default=str).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", data)
    Path(out).write_text(html)
    print(f"Explorer: {len(rows):,} schemes -> {out} ({len(html) / 1e6:.1f} MB)")
    return out

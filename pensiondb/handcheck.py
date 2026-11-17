"""Hand-checked DB figures for the largest sponsors, read from their annual reports by eye.

The automatic reader struggles most with the biggest groups: long reports, several schemes or countries side by
side, figures in dollars or euros. hand_checked.csv records, for each company checked, the figures and where they
are (filing date and PDF page), so anyone can verify them. These override whatever the reader found.

CSV columns:
  period_end     balance-sheet date (blank = take it from the filing)
  currency       GBP / USD / EUR; converted to sterling at the period-end rate in FX
  obligation_m   present value of the DB obligation, in millions
  assets_m       fair value of plan assets, in millions
  company_number a company number, or reg:<source>:<number> for a charity, mutual or union matched on its register
  basis          uk_plans (the UK column of a multi-country table), main_scheme, group (all plans in the
                 accounts), or misread (the reader's figure is wrong and no reliable figure could be read:
                 the sponsor's figures are cleared)
"""
import csv
from pathlib import Path

from . import pdfaccounts

CSV = Path(__file__).with_name("hand_checked.csv")
# bodies on no register and not at Companies House (University of Oxford, of Cambridge, Newcastle University), with
# the schemes they sponsor and figures read from their published financial statements. Columns: body_id, body_name,
# psrs (space-separated), period_end, obligation_m, assets_m, page, source (URL of the accounts), note
BODIES = Path(__file__).with_name("bodies.csv")

# currency units per £1 at the period end, with the source of each rate
FX = {
    ("USD", "2025-12-31"): 1.3474,      # Rio Tinto plc annual report 2025 (year-end rate £1 = US$1.3474)
    ("USD", "2025-06-30"): 1.3735,      # market rate on 30 June 2025
    ("EUR", "2025-12-31"): 1 / 0.8726,  # ECB reference rate 31 Dec 2025: €1 = £0.8726
    ("USD", "2026-02-20"): 1.3501,      # Bank of England spot rate 20 Feb 2026 (via poundsterlinglive.com)
    ("USD", "2025-03-31"): 1.2910,      # Bank of England spot rate 31 Mar 2025 (via poundsterlinglive.com)
    ("USD", "2025-09-30"): 1.3462,      # Bank of England spot rate 30 Sep 2025 (via poundsterlinglive.com)
    ("USD", "2026-03-31"): 1.3188,      # Bank of England spot rate 31 Mar 2026 (via poundsterlinglive.com)
    ("USD", "2025-04-30"): 1.3356,      # Bank of England spot rate 30 Apr 2025 (via poundsterlinglive.com)
    ("USD", "2025-05-31"): 1.3482,      # Bank of England spot rate 30 May 2025, last working day (via poundsterlinglive.com)
    ("USD", "2025-08-31"): 1.3509,      # Bank of England spot rate 29 Aug 2025, last working day (via poundsterlinglive.com)
    ("USD", "2025-12-28"): 1.3507,      # Bank of England spot rate 24 Dec 2025, last working day (via poundsterlinglive.com)
}


def load():
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def _period(num, row):
    if row["period_end"]:
        return row["period_end"]
    item = pdfaccounts.latest_accounts(num) or {}
    return item.get("action_date") or (item.get("description_values") or {}).get("made_up_date") or row["filing_date"]


def apply(con):
    """Write hand-checked figures onto sponsors. Run after checks.run and before parents.merge."""
    n = 0
    for r in load():
        num = r["company_number"]
        if num.startswith("reg:"):
            n += _apply_register(con, r)
            continue
        if not con.execute("SELECT 1 FROM sponsors WHERE company_number=?", (num,)).fetchone():
            continue
        src = f"hand-checked: filing {r['filing_date'] or '?'}, page {r['page']}; {r['note']}"
        if r["basis"] == "misread":
            con.execute("""UPDATE sponsors SET db_period_end=NULL, db_obligation=NULL, db_plan_assets=NULL,
                               db_surplus_deficit=NULL, db_net_basis=NULL, db_taxonomy=NULL, db_source_file=?,
                               db_check='flag:misread', db_check_note=? WHERE company_number=?""",
                        (src, r["note"], num))
            n += 1
            continue
        period = _period(num, r)
        rate = 1.0
        if r["currency"] != "GBP":
            rate = FX.get((r["currency"], period))
            if not rate:
                raise ValueError(f"{num}: no {r['currency']} rate for {period} in handcheck.FX")
        ob = float(r["obligation_m"]) * 1e6 / rate if r["obligation_m"] else None
        assets = float(r["assets_m"]) * 1e6 / rate if r["assets_m"] else None
        net = assets - ob if ob is not None and assets is not None else None
        note = f"{r['basis']}" + (f"; {r['currency']} at {rate:.4f} per £" if r["currency"] != "GBP" else "")
        con.execute("""UPDATE sponsors SET db_period_end=?, db_obligation=?, db_plan_assets=?, db_surplus_deficit=?,
                           db_net_basis='assets_minus_obligation', db_taxonomy='hand_checked', db_source_file=?,
                           db_check='ok:hand_checked', db_check_note=? WHERE company_number=?""",
                    (period, ob, assets, net, src, note, num))
        n += 1
    n += _apply_bodies(con)
    con.commit()
    return n


def _apply_bodies(con):
    """Attach each body in bodies.csv to its schemes (charity_source 'body') with its figures, where the scheme has
    no Companies House sponsor. A body with no figures still counts as identified, so its schemes stay candidates."""
    if not BODIES.exists():
        return 0
    with open(BODIES, newline="") as f:
        rows = list(csv.DictReader(f))
    n = 0
    for r in rows:
        ob = float(r["obligation_m"]) * 1e6 if r["obligation_m"] else None
        assets = float(r["assets_m"]) * 1e6 if r["assets_m"] else None
        net = assets - ob if ob is not None and assets is not None else None
        src = "hand_checked" if ob is not None or assets is not None else None
        for psr in r["psrs"].split():
            n += con.execute("""UPDATE schemes SET charity_source='body', charity_number=?, charity_name=?,
                                    charity_match_confidence=0.9, charity_active=1, charity_db_obligation=?,
                                    charity_db_assets=?, charity_db_pension=?, charity_fy_end=?, charity_db_source=?,
                                    charity_db_check=NULL
                                WHERE psr=? AND sponsor_company_number IS NULL""",
                             (r["body_id"], r["body_name"], ob, assets, net, r["period_end"] or None, src, psr)).rowcount
    return n


def _apply_register(con, r):
    """A register body's figures (charity, mutual or union; company_number 'reg:<source>:<number>'), written onto
    its schemes' charity_db_* columns. Returns the number of bodies changed (0 or 1)."""
    _, source, number = r["company_number"].split(":", 2)
    where = "WHERE charity_source=? AND charity_number=?"
    if not con.execute(f"SELECT 1 FROM schemes {where}", (source, number)).fetchone():
        return 0
    if r["basis"] == "misread":
        con.execute(f"""UPDATE schemes SET charity_db_obligation=NULL, charity_db_assets=NULL, charity_db_pension=NULL,
                            charity_db_source=NULL, charity_db_check='flag:misread' {where}""", (source, number))
        return 1
    ob = float(r["obligation_m"]) * 1e6 if r["obligation_m"] else None
    assets = float(r["assets_m"]) * 1e6 if r["assets_m"] else None
    net = assets - ob if ob is not None and assets is not None else None
    con.execute(f"""UPDATE schemes SET charity_db_obligation=?, charity_db_assets=?, charity_db_pension=?,
                        charity_fy_end=coalesce(nullif(?, ''), charity_fy_end), charity_db_source='hand_checked',
                        charity_db_check=NULL {where}""", (ob, assets, net, r["period_end"], source, number))
    return 1


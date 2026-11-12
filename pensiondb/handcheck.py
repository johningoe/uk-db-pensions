"""Hand-checked DB figures for the largest sponsors, read from their annual reports by eye.

The automatic reader struggles most with the biggest groups: long reports, several schemes or countries side by
side, figures in dollars or euros. hand_checked.csv records, for each company checked, the figures and where they
are (filing date and PDF page), so anyone can verify them. These override whatever the reader found.

CSV columns:
  period_end     balance-sheet date (blank = take it from the filing)
  currency       GBP / USD / EUR; converted to sterling at the period-end rate in FX
  obligation_m   present value of the DB obligation, in millions
  assets_m       fair value of plan assets, in millions
  basis          uk_plans (the UK column of a multi-country table), main_scheme, group (all plans in the
                 accounts), or misread (the reader's figure is wrong and no reliable figure could be read:
                 the sponsor's figures are cleared)
"""
import csv
from pathlib import Path

from . import pdfaccounts

CSV = Path(__file__).with_name("hand_checked.csv")

# currency units per £1 at the period end, with the source of each rate
FX = {
    ("USD", "2025-12-31"): 1.3474,      # Rio Tinto plc annual report 2025 (year-end rate £1 = US$1.3474)
    ("USD", "2025-06-30"): 1.3735,      # market rate on 30 June 2025
    ("EUR", "2025-12-31"): 1 / 0.8726,  # ECB reference rate 31 Dec 2025: €1 = £0.8726
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
    con.commit()
    return n

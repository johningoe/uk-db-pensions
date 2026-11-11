"""Coverage summary (markdown) and a CSV review queue of matches needing a human look."""
import csv
from datetime import date

from . import config as C


def _one(con, sql):
    return con.execute(sql).fetchone()[0]


def _pct(n, d):
    return f"{n:,} ({100 * n / d:.1f}%)" if d else "0"


def review_queue(con, path=C.DATA / "review_queue.csv"):
    rows = con.execute("""
        SELECT s.psr, s.scheme_name, s.post_code, m.candidate_sponsor, m.method, m.company_number, m.company_name,
               m.score, m.confidence, m.accepted, m.notes
        FROM schemes s JOIN match_log m ON m.psr = s.psr
        WHERE s.needs_review = 1 AND (m.accepted = 1 OR s.match_method = 'unmatched')
        ORDER BY m.accepted DESC, m.confidence DESC""").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["psr", "scheme_name", "scheme_post_code", "candidate_sponsor", "method", "company_number",
                    "company_name", "score", "confidence", "accepted", "notes", "reviewer_decision"])
        w.writerows([*r, ""] for r in rows)
    return len(rows)


def write(con):
    q = lambda sql: _one(con, sql)  # noqa: E731
    total = q("SELECT count(*) FROM schemes")
    matched = q("SELECT count(*) FROM schemes WHERE sponsor_company_number IS NOT NULL")
    high = q("SELECT count(*) FROM schemes WHERE match_confidence >= 0.85 AND needs_review = 0")
    review = q("SELECT count(*) FROM schemes WHERE needs_review = 1")
    sponsors = q("SELECT count(*) FROM sponsors")
    with_db = q("SELECT count(*) FROM sponsors WHERE db_period_end IS NOT NULL")
    schemes_db = q("""SELECT count(*) FROM schemes s JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
                      WHERE sp.db_period_end IS NOT NULL""")
    months = [r[0] for r in con.execute("SELECT DISTINCT archive FROM accounts_files_processed")]
    n_filings = q("SELECT count(DISTINCT member) FROM accounts_files_processed")
    stale = q("SELECT count(*) FROM sponsors WHERE db_period_end < date('now', '-3 years')")
    filings = q("SELECT count(DISTINCT company_number) FROM accounts_files_processed")
    live_view = q("SELECT count(*) FROM v_live_db_hybrid")

    lines = [
        f"# UK DB & hybrid pension schemes - coverage summary ({date.today().isoformat()})", "",
        "## Coverage", "",
        "| Measure | Count |", "|---|---|",
        f"| UK schemes in EIOPA register (16 Mar 2020, 100+ members) | {total:,} |",
        f"| Sponsor matched to a Companies House company | {_pct(matched, total)} |",
        f"| ...of which high confidence (>= 0.85, no review flag) | {_pct(high, total)} |",
        f"| Schemes flagged for manual review | {_pct(review, total)} |",
        f"| Distinct sponsor companies | {sponsors:,} |",
        f"| Sponsors with a filing in the accounts months processed | {_pct(filings, sponsors)} |",
        f"| Sponsors with DB figures extracted | {_pct(with_db, sponsors)} |",
        f"| ...of which read from PDF accounts (OCR or text) | "
        f"{q("SELECT count(*) FROM sponsors WHERE db_taxonomy LIKE 'pdf%'"):,} |",
        f"| Schemes whose sponsor has DB figures | {_pct(schemes_db, total)} |",
        f"| Sponsors without own figures but with a UK parent that has them (group level) | "
        f"{q("SELECT count(*) FROM sponsors WHERE db_period_end IS NULL AND group_db_company_number IS NOT NULL"):,} |",
        f"| Schemes whose sponsor has own or parent-group DB figures | "
        f"{_pct(q("""SELECT count(*) FROM schemes s JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
                     WHERE sp.db_period_end IS NOT NULL OR sp.group_db_company_number IS NOT NULL"""), total)} |",
        f"| Schemes in `v_live_db_hybrid` (likely live, not DC-named) | {live_view:,} |",
        "", f"Accounts archives processed: {len(months)} ({n_filings:,} sponsor filings parsed)", "",
        "## Match methods", "", "| Method | Schemes | Avg confidence | Needs review |", "|---|---|---|---|",
    ]
    for m, n, c, r in con.execute("""SELECT match_method, count(*), avg(match_confidence), sum(needs_review)
                                     FROM schemes GROUP BY 1 ORDER BY 2 DESC"""):
        lines.append(f"| {m} | {n:,} | {c or 0:.2f} | {r:,} |")
    if con.execute("SELECT name FROM sqlite_master WHERE name='review_decisions'").fetchone():
        lines += ["", "## Automated review of flagged matches", "",
                  "| Source | Accept | Switch | Reject |", "|---|---|---|---|"]
        for m, a, s_, r in con.execute("""SELECT method,
                                             sum(decision='accept'), sum(decision='switch'), sum(decision='reject')
                                         FROM review_decisions GROUP BY 1 ORDER BY 1"""):
            lines.append(f"| {m} | {a:,} | {s_:,} | {r:,} |")
    lines += ["", "## Scheme type hints (from scheme name only)", "", "| name_type | Schemes |", "|---|---|"]
    for t, n in con.execute("SELECT name_type, count(*) FROM schemes GROUP BY 1 ORDER BY 2 DESC"):
        lines.append(f"| {t} | {n:,} |")
    lines += ["", "## Status flags", "", "| Flag | Schemes |", "|---|---|"]
    for label, sql in [
        ("On PPF list - In Assessment", "ppf_status = 'In Assessment'"),
        ("On PPF list - Transferred", "ppf_status = 'Transferred'"),
        ("On PPF list - Withdrawn", "ppf_status = 'Withdrawn'"),
        ("On FAS list", "fas_status IS NOT NULL"),
        ("Sponsor dissolved", "sponsor_dissolved = 1"),
        ("Sponsor in administration / liquidation / other insolvency", "sponsor_insolvency = 1"),
        ("Any DB evidence (name, PPF, FAS or sponsor accounts)", "db_evidence IS NOT NULL"),
    ]:
        lines.append(f"| {label} | {q('SELECT count(*) FROM schemes WHERE ' + sql):,} |")
    ppf_total = q("SELECT count(*) FROM ppf_schemes")
    fas_total = q("SELECT count(*) FROM fas_schemes")
    lines += ["", f"PPF list: {ppf_total:,} schemes; FAS list: {fas_total:,} schemes (most predate the 2020 register).",
              "", "## DB figures (sponsors)", "", "| Net basis | Sponsors |", "|---|---|"]
    for b, n in con.execute("SELECT db_net_basis, count(*) FROM sponsors WHERE db_period_end IS NOT NULL GROUP BY 1"):
        lines.append(f"| {b} | {n:,} |")
    lines += ["", "Largest DB obligations found:", "", "| Sponsor | Period end | Obligation £m | Assets £m | Surplus/(deficit) £m |",
              "|---|---|---|---|---|"]
    fm = lambda v: "" if v is None else f"{v / 1e6:,.1f}"  # noqa: E731
    for n, p, o, a, s in con.execute("""SELECT company_name, db_period_end, db_obligation, db_plan_assets, db_surplus_deficit
                                       FROM sponsors WHERE db_obligation IS NOT NULL ORDER BY db_obligation DESC LIMIT 10"""):
        lines.append(f"| {n} | {p} | {fm(o)} | {fm(a)} | {fm(s)} |")

    lines += ["", "## Ownership chain of sponsors without their own DB figures", "",
              "| Chain ends at | Sponsors |", "|---|---|"]
    for e, n in con.execute("""SELECT coalesce(parent_chain_end, 'not looked up'), count(*) FROM sponsors
                               WHERE parent_chain IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""):
        lines.append(f"| {e} | {n:,} |")
    lines += ["", "Group figures (`group_db_*`) are the nearest UK parent's, covering every scheme in that group. "
              "Several sponsors can share one parent, so don't add them up across sponsors."]

    n_review = review_queue(con)
    api = q("SELECT count(*) FROM sponsors WHERE status_source = 'api'")
    lines += ["", "## Main gaps", "",
              "- **Registration date is blank for every UK row** in the EIOPA file, so `registration_start_date` is empty. "
              "TPR's register isn't open data; there is no free source for it.",
              "- **The EIOPA file doesn't say DB or DC.** `name_type` is a guess from the scheme name. Most schemes come out "
              "`unknown`. DB is confirmed only by a PPF or FAS listing, or by DB tags in the sponsor's accounts (`db_evidence`).",
              "- **The register is a March 2020 snapshot.** Schemes set up later are missing. Schemes wound up or bought out "
              "since then show as live unless the PPF list, FAS list or sponsor status says otherwise.",
              f"- **Dissolved sponsors are invisible without the API.** The bulk snapshot lists live companies only. "
              f"{'API status checks ran for ' + format(api, ',') + ' sponsors.' if api else 'No API key was set, so no dissolved-company search or status refresh ran. Many unmatched schemes probably have dissolved sponsors.'}",
              f"- **DB figures depend on machine-readable (iXBRL) filings.** Across {len(months)} archives, "
              f"{_pct(sponsors - filings, sponsors)} of sponsors never filed one. Big groups often file their DB note as "
              "PDF, report it only at a parent company, or have their pension sit in a parent company the scheme name "
              "doesn't mention. Some filings in Companies House's archives are empty files.",
              f"- **Some DB figures are old.** {stale:,} sponsors' latest machine-readable balance sheet is more than "
              "3 years old. Check `db_period_end`.",
              "- **Tagged net figures have inconsistent signs across filers.** `db_surplus_deficit` is assets minus "
              "obligation where both are tagged. Otherwise it's marked `tagged_sign_unverified`.",
              "- **Multi-employer and section schemes** match the participating employer where the name gives one. "
              "Otherwise they match the umbrella scheme's name.",
              "- **Public-sector schemes** (police, teachers, councils, universities, colleges, churches) are set to "
              "`non_company` and not fuzzy-matched.",
              f"- **{n_review:,} rows still need manual review** after the automated pass. They're in "
              "`data/review_queue.csv`; put accept, reject or a company number in `reviewer_decision` and rerun "
              "`run.py review`. Manual decisions override automated ones."]
    C.SUMMARY_MD.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

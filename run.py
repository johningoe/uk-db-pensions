"""UK DB & hybrid pension scheme database.

    python run.py all                         # every step, latest accounts month only
    python run.py seed | flags | match | api | postcode | review | accounts | status | summary
    python run.py accounts --month September2026 --month August2026
    python run.py accounts --all-months       # every archive CH publishes, read remotely (~30 min)
    python run.py reparse                     # re-extract figures from saved filings (no network)
    python run.py sip-pilot                   # find published SIPs for a 200-scheme sample
    python run.py pdf-pilot                   # DB figures from PDF accounts for 100 non-iXBRL sponsors
    python run.py pdf-accounts --limit 100    # next 100 eligible sponsors (one worker; rerun for the next batch)
    python run.py pdf-reextract               # redo figure extraction from saved text after a rules change
    python run.py cloud-export | cloud-import # read PDF accounts on GitHub Actions (see pensiondb/cloud.py)
    python run.py check                       # plausibility check on DB figures (also part of status)
    python run.py parents                     # ownership chain (PSC register) for sponsors without DB figures
    python run.py parent-accounts --limit 50  # read the nearest parent's group accounts (PDF/iXBRL)
"""
import argparse

from pensiondb import accounts, chapi, checks, cloud, db, match, parents, pdfaccounts, review, sips, sources, summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["all", "seed", "flags", "match", "api", "postcode", "review", "accounts", "reparse", "status", "summary",
                                     "sip-pilot", "pdf-pilot", "pdf-accounts", "parents", "parent-accounts", "pdf-reextract", "cloud-export", "cloud-import", "check"])
    ap.add_argument("--month", action="append", help="accounts month, e.g. September2026 (repeatable)")
    ap.add_argument("--all-months", action="store_true")
    ap.add_argument("--shard", help="i/n: this worker's share of the pdf-accounts run")
    ap.add_argument("--limit", type=int, help="pdf-accounts: companies in this batch")
    ap.add_argument("--pause", type=float, default=0, help="pdf-accounts: seconds to rest between documents")
    ap.add_argument("--rematch", action="store_true", help="redo sponsor matching for every scheme")
    a = ap.parse_args()

    con = db.connect()
    db.init(con)
    steps = ["seed", "flags", "match", "api", "postcode", "review", "accounts", "status", "summary"] if a.step == "all" else [a.step]
    for step in steps:
        print(f"--- {step}")
        if step == "seed":
            sources.load_eiopa(con)
            sources.load_ppf(con)
            sources.load_fas(con)
            sources.build_ch_index()
        elif step == "flags":
            match.flag_ppf_fas(con)
        elif step == "match":
            match.match_sponsors(con, rematch=a.rematch or a.step == "all")
        elif step == "api":
            if chapi.api_key():
                chapi.search_unmatched(con)
                chapi.refresh_sponsor_status(con)
            else:
                print("COMPANIES_HOUSE_API_KEY not set - skipping (dissolved sponsors stay unmatched)")
        elif step == "postcode":
            match.match_by_postcode(con)
        elif step == "review":
            review.run(con)
        elif step == "accounts":
            accounts.run(con, months=a.month, all_months=a.all_months)
        elif step == "reparse":
            accounts.reparse(con)
        elif step == "status":
            print(f"PDF accounts: {pdfaccounts.merge(con):,} sponsors given DB figures")
            print(f"Figure check: {checks.run(con)}")
            print(f"Parents: {parents.merge(con):,} sponsors given group DB figures")
            match.flag_status(con)
        elif step == "summary":
            summary.write(con)
        elif step == "sip-pilot":
            sips.run_pilot(con)
        elif step == "pdf-pilot":
            pdfaccounts.run(con, pdfaccounts.pilot_sample(con))
        elif step == "pdf-accounts":
            shard = tuple(int(x) for x in a.shard.split("/")) if a.shard else None
            pdfaccounts.run(con, pdfaccounts.pilot_sample(con, n=None), shard, a.limit, a.pause)
        elif step == "cloud-export":
            cloud.export(con)
        elif step == "cloud-import":
            cloud.import_(con)
        elif step == "check":
            print(f"Figure check: {checks.run(con)}")
        elif step == "pdf-reextract":
            pdfaccounts.reextract(con)
        elif step == "parents":
            parents.lookup(con)
        elif step == "parent-accounts":
            shard = tuple(int(x) for x in a.shard.split("/")) if a.shard else None
            parents.run(con, shard, a.limit, a.pause)


if __name__ == "__main__":
    main()

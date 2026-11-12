# UK DB & hybrid pension schemes database

A free, reproducible SQLite database of UK occupational pension schemes, their likely sponsoring
employers, those sponsors' defined benefit (DB) figures, and PPF/FAS/insolvency flags.

## Layout

```
run.py              CLI entry point
pensiondb/          code (config, sources, names, match, chapi, accounts, summary, db)
raw/                downloads only, never edited
  eiopa/            EIOPA IORP register, 16 March 2020 (from the Wayback Machine; EIOPA's link is dead)
  ppf/              PPF "Schemes we look after" result pages, saved as HTML
  fas/              FAS scheme list (PDF)
  companies_house/  bulk company snapshot, api_cache/, accounts/<archive>/ (sponsor filings only)
data/
  pensions.sqlite   the output
  ch_snapshot.sqlite  indexed copy of the CH snapshot (~2 GB, rebuildable)
  coverage_summary.md
  review_queue.csv  matches to check by hand
```

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export COMPANIES_HOUSE_API_KEY=...      # optional but recommended, free from developer.company-information.service.gov.uk
.venv/bin/python run.py all             # seed, flags, match, api, postcode, review, accounts (latest month), status, summary
.venv/bin/python run.py accounts --month August2026 --month July2026
.venv/bin/python run.py accounts --all-months   # all ~194 archives (2008 on), ~30 min; finished archives are skipped
.venv/bin/python run.py pdf-accounts --limit 100   # DB figures from PDF accounts, in batches
.venv/bin/python run.py full-filers                  # full/group/medium sponsors whose iXBRL had no DB tags
.venv/bin/python run.py parents                      # ownership chains for sponsors still without figures
.venv/bin/python run.py parent-accounts --limit 50   # parents' group accounts
.venv/bin/python run.py status && .venv/bin/python run.py summary
```

Each step can run on its own. Downloads are cached and reruns are incremental.

The accounts archives total about 300 GB, so they aren't downloaded. Each one is read remotely with HTTP range
requests: the step fetches the zip's directory, then only the filings for matched sponsors (about 0.1% of each
archive). Those filings are saved under `raw/companies_house/accounts/<archive>/`. A local copy of an archive, if
present, is used instead.

## Tables

- **schemes**: one row per UK scheme (PSR number). Columns: name, address, derived sponsor, `sponsor_company_number`,
  `match_method`, `match_confidence`, `needs_review`, PPF/FAS status and dates, `sponsor_dissolved`,
  `sponsor_insolvency`, `name_type`, `db_evidence`, `live_doubt` and `likely_live`.
- **sponsors**: one row per matched company. Columns: CH status and DB figures (`db_obligation`, `db_plan_assets`,
  `db_surplus_deficit`, `db_period_end`, `db_net_basis`, `db_taxonomy`, `db_source_file`).
- **match_log**: every match attempt, accepted or not, with candidate, method, score, confidence and notes.
- **accounts_facts**: every DB balance-sheet fact extracted (concept, period, value, dimensions), kept for audit.
- **ppf_schemes**, **fas_schemes**: the raw lists.
- **v_live_db_hybrid**: a view of schemes that are likely live and not DC by name, joined to sponsor figures.

## Matching rules

These are tried in order. The first hit wins.

1. **exact**: the normalised derived sponsor name equals a company's current name. `_prev` means it matched a previous name.
2. **exact_core**: the same, ignoring LIMITED/PLC/LLP.
3. **exact_variant**: the core name plus or minus UK / GROUP / HOLDINGS / INTERNATIONAL.
4. **fuzzy**: RapidFuzz token-sort score of at least 92 within a first-word block, multi-word names only, and each word
   must match a word in the company name. Always flagged for review.
5. **api_exact**: an exact normalised hit from the CH search API. This finds dissolved companies and only runs with a key.
   **Second pass** (`run.py api-second-pass`, also part of `run.py api`): for schemes still unmatched, or held back as
   too new, it also searches CH's dissolved-company index by name and by previous name. `api_exact_pc` is an exact
   name at the scheme's postcode (0.97). `api_exact_any` is an exact name elsewhere (0.9 if unique with 2+ words,
   otherwise 0.6 and flagged for review). `api_postcode` is a near-identical name at the scheme's postcode (0.85).
   Companies a reviewer has rejected for that scheme are skipped.
6. **postcode**: last resort. A live company registered at the scheme's own postcode whose name contains the sponsor's
   leading distinctive word (and at least two of its words when it has two or more). Postcodes shared by 3+ schemes
   (administrators) or 26+ companies (formation agents) are skipped. Trustee and pension-vehicle companies are logged
   as leads only. Always flagged for review.

Confidence goes down for multiple candidate companies and single-word sponsors. It goes up when the scheme postcode
equals the company's registered-office postcode. A PPF-listed scheme's company name is tried first as the sponsor.
Police, teachers, councils, universities and similar bodies are marked `non_company` and not fuzzy-matched.

A dissolved sponsor sets `likely_live = 0`, except when the match is low confidence (under 0.7) and still needs
review: a wrong company with the same name shouldn't close a live scheme.

`live_doubt` also sets `likely_live = 0` when an active sponsor's own records point away from DB:
- `accounts_no_db`: its latest full accounts were read and never mention defined benefit or other DB wording
  (actuarial, IAS 19, scheme liabilities, pensionable salary and so on). A DB sponsor has to disclose its scheme,
  so the scheme has most likely been merged, bought out or wound up, or sits with another group company.
- `sponsor_dormant`: it files dormant accounts.
- `sponsor_no_accounts`: it is over two years old and has never filed accounts.

Only ordinary companies and LLPs formed before 2016 are judged. Trustee and nominee companies, Royal Charter bodies,
societies and overseas companies are left alone. So is any scheme with other DB evidence (PPF or FAS listing, DB
figures or a DB mention in the sponsor's or a parent's accounts, charity DB figures) or a low-confidence match
awaiting review. Texts under 3,000 characters count as failed reads, not as proof.

## Reviewing flagged matches

`run.py review` settles flagged matches and records each decision in `review_decisions`, with its source and reason.
Decisions are applied only while the scheme still points at the company the decision was about, so they survive
reruns. Sources, strongest first:

1. **manual**: `reviewer_decision` in `data/review_queue.csv` (accept, reject, or a company number to switch to).
2. **ai**: `data/ai_queue.csv`, where each row was judged by reading the scheme name, postcode and the candidate
   companies' status, accounts type, incorporation year, address and SIC code. Rows marked "unsure" are left flagged.
3. **rule**:
   - `corroborated`: the scheme postcode, the company's DB figures or the PPF sponsor name agrees.
   - `better_candidate` / `trading_sibling`: a same-name company has that evidence, or trades in the scheme's
     postcode district while the match is dormant.
   - `local_trading` / `unique_trading`: a trading company in the scheme's district, or the only trading company
     with a multi-word exact name.
   - `too_new`: rejects a small company formed after the March 2020 register date.

Rejected schemes get `match_method = 'rejected'`. Insurers' AVC and personal products and master trusts are
classed `DC_likely` by name.

## DB figures

From FRS 102 (FRC `core`) and IFRS tags: `LiabilitiesDefinedBenefitPlanPresentValue` /
`DefinedBenefitObligationAtPresentValue`, `FairValueAssetsDefinedBenefitPlan` / `PlanAssetsAtFairValue`,
`DeficitSurplusInDefinedBenefitPlan`, `RetirementBenefitObligationsSurplus`, `NetDefinedBenefitLiabilityAsset` and
`SurplusDeficitInPlan`.

- A total without dimensions is used when there is one. Otherwise per-plan members are summed.
- The latest balance-sheet date wins.
- Filers sign the net line inconsistently. So `db_surplus_deficit` is assets minus obligation when both exist,
  minus the obligation when the scheme is unfunded, and otherwise the tagged value marked `tagged_sign_unverified`.

### From PDF accounts

Most large sponsors never file iXBRL. `run.py pdf-accounts` takes active full/group/medium-accounts sponsors with
no figures, fetches their latest accounts through the CH document API (PDF, XHTML or ESEF zip), and reads the
pension note. Scanned PDFs are read with macOS Vision text recognition. Each result goes in `pdf_accounts` with a
status (`figures`, `mentioned_no_figures`, `dc_only`, `no_pension_note`, `no_filing`, `error`), the page and the
lines it came from. Only the extracted text is kept, under `raw/companies_house/pdf_accounts/`.

The reader looks at pages that mention defined benefit, plus the page after each (tables often run over). It
knows the common line labels: present value of obligations or liabilities, actuarial liability, fair or market
value of scheme or plan assets, and total defined benefit asset or liability. A figure on a table line beats one
in a sentence, and lines about returns, interest, movements, experience adjustments, durations, percentages and
funding valuations are skipped. The page with both obligation and assets wins. After changing these rules, run
`run.py pdf-reextract`. It re-reads every saved text without downloading anything.

OCR is CPU-heavy. Run it in batches (`--limit 100`, `--pause 15`) or as up to 3 workers (`--shard 0/3`, `1/3`, `2/3`).
`run.py full-filers` runs the same reader over active full/group/medium sponsors of live schemes whose iXBRL
filings had no DB tags (the note is often untagged text). It resumes where it stopped. `run.py status` copies the figures onto sponsors that have no iXBRL figures, with
`db_taxonomy` set to `pdf_ocr`, `pdf_native`, `pdf_xhtml` or `pdf_ixbrl`.

### Reading accounts on GitHub Actions

OCR for ~2,000 documents takes a laptop 12+ hours. The `PDF accounts` workflow does it on parallel Linux runners
with Tesseract instead. On a 30-company test it matched macOS Vision on 25 of 30, about as accurate overall, at about
1 s per page per core.

1. `run.py cloud-export` writes `cloud/todo.json`: own accounts still to read, plus each sponsor's nearest unread
   parent. Commit and push it.
2. Add the Companies House key as the repository secret `COMPANIES_HOUSE_API_KEY`.
3. Run the workflow (Actions → PDF accounts → Run workflow; default 8 runners, 300 minutes). Runners need no database.
4. Download the artifacts into `cloud/results/` (`gh run download <run id> -D cloud/results`), then
   `run.py cloud-import` and `run.py status`.
5. Export again for the next level of parents. Each round is smaller.

While a run is going, `run.py cloud-progress` shows each runner's latest line (documents done, with figures,
errors, minutes), posted every 5 minutes as a commit status on the commit being run. The same lines appear in the
run's live log on GitHub (search for `PROGRESS`).

`raw/`, `data/` and `.env` are git-ignored: the key and the database never leave this machine.

### Charities and registered societies

Some schemes' employers aren't on Companies House at all: charitable incorporated organisations, trusts, church
bodies, colleges, co-ops, friendly and building societies, housing associations. `run.py charities-download` fetches
three free registers into `raw/charities/`:

- the Charity Commission's full register extract (England and Wales), with Part B of the annual return;
- the Scottish Charity Regulator's register download;
- the FCA Mutuals Public Register society list.

`run.py charities` matches every scheme without a Companies House sponsor to them by name. A match is accepted when
the name is unique (two words or more), the scheme postcode agrees, or one body of that name is the only active one
or has 10× the income of the others. Accepted matches fill `schemes.charity_source`, `charity_number`,
`charity_name`, `charity_match_confidence` and `charity_active`. They never replace a Companies House sponsor.
Everything found, accepted or not, is in `charity_matches`; doubtful ones go to `data/charity_review.csv`.

For larger charities in England and Wales, Part B of the annual return includes the net defined benefit pension
balance; it's copied to `charity_db_pension` (negative = deficit) with `charity_fy_end`. It's the net figure only,
not obligation and assets.

`run.py charity-accounts` reads the latest accounts of every active matched body without that figure (charities with
income of £1m or more, Scottish charities, registered societies): from Companies House when the charity is also a
company, otherwise the PDF from the Charity Commission register page, OSCR's published accounts or the FCA Mutuals
register's "Annual Returns and Accounts" documents. One request a second; text is cached in `raw/charities/accounts/`.
Results are in `charity_accounts`; figures go to `charity_db_obligation`, `charity_db_assets` and
`charity_db_pension` (assets minus obligation), checked like sponsors' figures (`charity_db_check`), with
`charity_db_source` = `annual_return` or `accounts`.

### From parent companies (group level)

Many named sponsors are dormant, small or exempt subsidiaries, or take part in a group scheme and report it as DC,
so their own accounts have no figures.

1. `run.py parents` follows each such sponsor's register of persons with significant control up through its UK
   corporate parents (largest active stake; a 50/50 split, an individual or a foreign company ends the chain).
   The chain is saved in `sponsors.parent_chain`, with `parent_top_name`, `parent_top_country` and `parent_chain_end`.
2. `run.py parent-accounts` reads the nearest parent's accounts with the same PDF/iXBRL reader, moving up a level
   when a parent has none. It skips dormant and micro parents, and the parents of sponsors whose own PDF accounts
   haven't been read yet. Same `--limit`, `--pause` and `--shard` options.
3. `run.py status` fills `group_db_*` (parent number, name, levels up, period, obligation, assets, surplus/deficit).

Group figures cover the whole group, often several schemes and subsidiaries. Several sponsors can share a parent,
so don't add `group_db_*` up across sponsors. They are kept apart from the sponsor's own `db_*` columns.

### Figure check

`run.py status` (or `run.py check` on its own) sets `sponsors.db_check` for every sponsor with figures:

- `ok`: nothing odd.
- `fixed_unit`: obligation and assets were about 1,000× apart (one read in £m, the other in £'000). The larger
  is divided by 1,000; the values as read are in `db_check_note`.
- `flag:<reasons>`: kept as read but not trusted. Flagged figures are left out of the summary's largest-figures
  table, and a flagged parent's figures are not passed down as group figures. Reasons:
  - `too_large`: over £50bn, more than any UK scheme.
  - `too_small`: under £20k, so a unit was probably missed.
  - `assets_vs_obligation`: one is more than 3× the other, usually a stray number on one line.
  - `foreign_currency`: the source page is mostly in dollars, euros or another currency.
  - `decimal_lost`: a £m figure of 1,000 or more printed without a comma (OCR read "239.7" as "2397").

The PDF reader takes each figure's unit from the nearest column heading above it ("£'000", "£m"), not from
anywhere on the page, and reads "k" after an amount as thousands. After changing extraction rules, run
`run.py pdf-reextract` then `run.py status`.

The check catches gross errors only. A figure misread within the right order of magnitude (a wrong digit, a
prior-year column) passes, so spot-check figures that matter against the filed accounts.

Not used: the gov.uk Pension Tracing Service.

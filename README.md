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
  `sponsor_insolvency`, `name_type`, `db_evidence` and `likely_live`.
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
6. **postcode**: last resort. A live company registered at the scheme's own postcode whose name contains the sponsor's
   leading distinctive word (and at least two of its words when it has two or more). Postcodes shared by 3+ schemes
   (administrators) or 26+ companies (formation agents) are skipped. Trustee and pension-vehicle companies are logged
   as leads only. Always flagged for review.

Confidence goes down for multiple candidate companies and single-word sponsors. It goes up when the scheme postcode
equals the company's registered-office postcode. A PPF-listed scheme's company name is tried first as the sponsor.
Police, teachers, councils, universities and similar bodies are marked `non_company` and not fuzzy-matched.

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

OCR is CPU-heavy. Run it in batches (`--limit 100`, `--pause 15`) or as up to 3 workers (`--shard 0/3`, `1/3`, `2/3`).
It resumes where it stopped. `run.py status` copies the figures onto sponsors that have no iXBRL figures, with
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

`raw/`, `data/` and `.env` are git-ignored: the key and the database never leave this machine.

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

Not used: the gov.uk Pension Tracing Service.

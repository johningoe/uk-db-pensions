import sqlite3

from .config import DATA, OUT_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS schemes (
    psr                     TEXT PRIMARY KEY,   -- EIOPA 'Identification Code' (TPR Pension Scheme Registry number)
    scheme_name             TEXT NOT NULL,
    street                  TEXT,
    post_code               TEXT,
    city                    TEXT,
    registration_start_date TEXT,               -- as published by EIOPA (empty for every UK row in the 2020 file)
    registration_end_date   TEXT,
    is_section              INTEGER,            -- name looks like a section of a multi-employer scheme
    name_type               TEXT,               -- DB_likely / hybrid_likely / DC_likely / unknown (from scheme name only)
    db_evidence             TEXT,               -- comma list: name, ppf, fas, sponsor_accounts
    sponsor_name_derived    TEXT,
    sponsor_company_number  TEXT REFERENCES sponsors(company_number),
    match_method            TEXT,
    match_confidence        REAL,
    needs_review            INTEGER DEFAULT 0,
    ppf_status              TEXT,               -- In Assessment / Transferred / Withdrawn
    ppf_assessment_date     TEXT,
    ppf_transfer_or_withdrawn_date TEXT,
    ppf_match_score         REAL,
    ppf_scheme_name         TEXT,               -- name as on the PPF list (its company name is used as a sponsor hint)
    fas_status              TEXT,
    fas_transfer_date       TEXT,
    fas_match_score         REAL,
    sponsor_dissolved       INTEGER,
    sponsor_insolvency      INTEGER,            -- administration / liquidation / receivership etc.
    likely_live             INTEGER             -- see v_live_db_hybrid
);

CREATE TABLE IF NOT EXISTS sponsors (
    company_number      TEXT PRIMARY KEY,
    company_name        TEXT,
    company_status      TEXT,
    company_category    TEXT,
    incorporation_date  TEXT,
    dissolution_date    TEXT,
    reg_post_code       TEXT,
    sic_1               TEXT,
    accounts_category   TEXT,
    status_source       TEXT,                   -- snapshot / api
    status_checked_at   TEXT,
    db_period_end       TEXT,
    db_obligation       REAL,                   -- present value of DB obligation (positive number)
    db_plan_assets      REAL,
    db_surplus_deficit  REAL,                   -- assets - obligation (negative = deficit)
    db_net_basis        TEXT,                   -- assets_minus_obligation / unfunded / tagged_sign_unverified
    db_taxonomy         TEXT,                   -- frs102 / ifrs
    db_source_file      TEXT
);

CREATE TABLE IF NOT EXISTS match_log (
    id                  INTEGER PRIMARY KEY,
    psr                 TEXT,
    scheme_name         TEXT,
    candidate_sponsor   TEXT,
    method              TEXT,
    company_number      TEXT,
    company_name        TEXT,
    score               REAL,
    confidence          REAL,
    accepted            INTEGER,
    needs_review        INTEGER,
    notes               TEXT,
    logged_at           TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_match_log_psr ON match_log(psr);

CREATE TABLE IF NOT EXISTS accounts_facts (     -- every pension-related numeric fact found, for audit
    company_number  TEXT,
    source_file     TEXT,
    period_end      TEXT,
    concept         TEXT,
    category        TEXT,                       -- obligation / assets / net / other
    value           REAL,
    has_dimensions  INTEGER,
    dimensions      TEXT,
    taxonomy        TEXT
);
CREATE INDEX IF NOT EXISTS ix_facts_co ON accounts_facts(company_number);

CREATE TABLE IF NOT EXISTS accounts_files_processed (
    archive     TEXT,
    member      TEXT,
    company_number TEXT,
    n_facts     INTEGER,
    PRIMARY KEY (archive, member)
);

CREATE TABLE IF NOT EXISTS ppf_schemes (
    scheme_name TEXT, company_name TEXT, status TEXT, assessment_date TEXT, transfer_or_withdrawn_date TEXT
);
CREATE TABLE IF NOT EXISTS fas_schemes (
    scheme_name TEXT, fas_tranche TEXT, status TEXT, transfer_date TEXT
);

"""

# Group figures come from a parent's accounts and cover the whole group: never sum them across sponsors.
VIEW = """
DROP VIEW IF EXISTS v_live_db_hybrid;
CREATE VIEW v_live_db_hybrid AS
SELECT s.*, sp.company_name AS sponsor_name, sp.company_status AS sponsor_status,
       sp.db_obligation, sp.db_plan_assets, sp.db_surplus_deficit, sp.db_period_end,
       sp.group_db_company_name, sp.group_db_levels, sp.group_db_obligation, sp.group_db_plan_assets,
       sp.group_db_surplus_deficit, sp.group_db_period_end, sp.parent_top_name, sp.parent_top_country
FROM schemes s LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
WHERE s.likely_live = 1 AND s.name_type <> 'DC_likely';
"""


def connect(path=OUT_DB):
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=120)      # parallel workers share the file
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(con):
    from .parents import ensure_columns
    con.executescript(SCHEMA)
    ensure_columns(con)
    con.executescript(VIEW)
    con.commit()

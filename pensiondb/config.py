"""Paths and constants. Raw downloads live under raw/, outputs under data/, code under pensiondb/."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
DATA = ROOT / "data"

EIOPA_DIR = RAW / "eiopa"
CH_SNAPSHOT_DIR = RAW / "companies_house" / "snapshot"
CH_ACCOUNTS_DIR = RAW / "companies_house" / "accounts"
CH_API_CACHE = RAW / "companies_house" / "api_cache"
PPF_DIR = RAW / "ppf"
FAS_DIR = RAW / "fas"

OUT_DB = DATA / "pensions.sqlite"          # schemes, sponsors, match_log, ...
CH_DB = DATA / "ch_snapshot.sqlite"        # indexed copy of the CH bulk snapshot (large)
SUMMARY_MD = DATA / "coverage_summary.md"

# EIOPA's own link has been dead since 2025; the Wayback Machine copy is byte-identical in content.
EIOPA_FILE = "BACKUP_IORPS_20200316_publish.xlsx"
EIOPA_URLS = [
    "https://register.eiopa.europa.eu/SiteAssets/BACKUP_IORPS_20200316_publish.xlsx",
    "https://web.archive.org/web/20200507005239id_/https://register.eiopa.europa.eu/SiteAssets/BACKUP_IORPS_20200316_publish.xlsx",
]

CH_DOWNLOAD_BASE = "https://download.companieshouse.gov.uk/"
CH_API_BASE = "https://api.company-information.service.gov.uk"
CH_API_KEY_ENV = "COMPANIES_HOUSE_API_KEY"

PPF_SCHEMES_URL = "https://www.ppf.co.uk/Schemes"
FAS_PDF_URL = "https://www.ppf.co.uk/-/media/PPF-Website/Public/Files/file-2018-11/L/list_of_fas_schemes-2.pdf"

USER_AGENT = "uk-db-pensions-db/0.1 (research; contact via repo owner)"

# Matching thresholds (rapidfuzz token_sort_ratio, 0-100)
FUZZY_ACCEPT = 92       # >= this: accepted but flagged for review
FUZZY_LOG_ONLY = 75     # >= this: best candidate logged, scheme left unmatched

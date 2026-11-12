"""Sponsor matching (scheme -> Companies House company) and PPF/FAS scheme matching."""
import re
import sqlite3
from functools import lru_cache

from rapidfuzz import fuzz, process

from . import config as C
from .names import core, first_token, non_company, norm, sponsor_candidates

INSOLVENCY_WORDS = ("administration", "liquidation", "receiver", "receivership", "voluntary arrangement",
                    "insolvency")


def _pc(s):
    return (s or "").replace(" ", "").upper()


def _words_agree(a, b, min_ratio=85):
    """Every word of the shorter name must closely match some word of the other (stops GOLFERS ~ BOXERS).
    Spaces are also compared away so GROUND WORKS ~ GROUNDWORKS still passes."""
    if a.replace(" ", "") == b.replace(" ", ""):
        return True
    wa, wb = a.split(), b.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return all(max(fuzz.ratio(w, x) for x in long_) >= min_ratio for w in short if len(w) > 2)


class Matcher:
    def __init__(self, ch_db=C.CH_DB):
        self.ch = sqlite3.connect(ch_db)
        self.ch.row_factory = sqlite3.Row

    def company(self, num):
        return self.ch.execute("SELECT * FROM companies WHERE company_number=?", (num,)).fetchone()

    def _rows(self, col, key):
        return self.ch.execute(
            f"""SELECT n.company_number, n.name, n.is_previous, c.company_status, c.reg_post_code
                FROM names n JOIN companies c USING (company_number) WHERE n.{col}=?""", (key,)).fetchall()

    @lru_cache(maxsize=4096)
    def _block(self, tok):
        rows = self.ch.execute("SELECT company_number, name, name_core FROM names WHERE first_tok=?", (tok,)).fetchall()
        return rows, [r[2] for r in rows]

    @staticmethod
    def _pick(rows, scheme_pc):
        """Choose among candidate rows; returns (row, n_distinct, postcode_hit)."""
        by_co = {}
        for r in rows:
            by_co.setdefault(r["company_number"], r)
        cands = list(by_co.values())
        pc_hits = [r for r in cands if scheme_pc and _pc(r["reg_post_code"]) == scheme_pc]
        if pc_hits:
            return pc_hits[0], len(cands), True
        active = [r for r in cands if (r["company_status"] or "").startswith("Active")]
        current = [r for r in (active or cands) if not r["is_previous"]]
        return (current or active or cands)[0], len(cands), False

    def match_one(self, scheme_name, post_code, extra_candidates=()):
        """Returns (best, log) where best is a dict or None, and log is a list of attempt dicts."""
        scheme_pc = _pc(post_code)
        cands = list(dict.fromkeys([*extra_candidates, *sponsor_candidates(scheme_name)]))
        log = []
        hit = self._exact_pass(cands, scheme_pc, log, variants=False) or \
            (None if non_company(scheme_name) else self._exact_pass(cands, scheme_pc, log, variants=True))
        if hit:
            return hit, log
        if non_company(scheme_name):
            log.append(dict(candidate=" | ".join(cands), method="non_company", company_number=None, company_name=None,
                            score=None, confidence=0, accepted=0, needs_review=0,
                            notes="public-sector / non-company body: fuzzy matching skipped"))
            return None, log
        return self._fuzzy_pass(cands, scheme_pc, log)

    @staticmethod
    def _variants(cc):
        """Deterministic name variants: SKANSKA -> SKANSKA UK, AVERY DENNISON UK -> AVERY DENNISON, ..."""
        out = [cc + s for s in (" UK", " GROUP", " HOLDINGS", " UK HOLDINGS", " GROUP UK", " INTERNATIONAL")]
        for s in (" UK", " GROUP", " HOLDINGS", " INTERNATIONAL"):
            if cc.endswith(s):
                out.append(cc[: -len(s)])
        return [v for v in out if v]

    def _exact_pass(self, cands, scheme_pc, log, variants):
        for cand in cands:
            nn = norm(cand)
            cc = core(nn)
            single_token = len(cc.split()) <= 1
            if variants:
                rows, method = [], "exact_variant"
                for v in self._variants(cc):
                    rows = self._rows("name_core", v)
                    if rows:
                        break
            else:
                # exact on full normalised name (current, then previous names), then suffix-insensitive
                rows = self._rows("name_norm", nn) if nn != cc else []
                method = "exact"
                if not rows:
                    rows = self._rows("name_core", cc) if cc else []
                    method = "exact_core"
            if rows:
                r, n, pc_hit = self._pick(rows, scheme_pc)
                prev = bool(r["is_previous"])
                conf = {"exact": 0.95, "exact_core": 0.85, "exact_variant": 0.75}[method] - (0.1 if prev else 0)
                conf -= 0.25 if (n > 1 and not pc_hit) else 0
                conf -= 0.15 if (single_token and not pc_hit) else 0
                conf = min(1.0, conf + (0.05 if pc_hit else 0))
                review = (n > 1 and not pc_hit) or (single_token and not pc_hit) or conf < 0.7
                notes = "; ".join(filter(None, [
                    "previous name" if prev else "", f"{n} candidate companies" if n > 1 else "",
                    "postcode match" if pc_hit else "", "single-word sponsor" if single_token else ""]))
                hit = dict(candidate=cand, method=method + ("_prev" if prev else ""),
                           company_number=r["company_number"], company_name=r["name"], score=100.0,
                           confidence=round(conf, 2), accepted=1, needs_review=int(review), notes=notes)
                log.append(hit)
                return hit
        return None

    def _fuzzy_pass(self, cands, scheme_pc, log):
        """Best fuzzy match within the first-token block; multi-word sponsors only."""
        best = None
        for cand in cands:
            cc = core(norm(cand))
            tok = first_token(cc)
            if not tok or len(cc.split()) < 2:
                continue
            rows, choices = self._block(tok)
            if not choices:
                continue
            res = process.extractOne(cc, choices, scorer=fuzz.token_sort_ratio, score_cutoff=C.FUZZY_LOG_ONLY)
            if res and res[1] >= C.FUZZY_ACCEPT and not _words_agree(cc, res[0]):
                res = (res[0], min(res[1], C.FUZZY_ACCEPT - 0.1), res[2])     # demote: log only
            if res and (best is None or res[1] > best["score"]):
                r = rows[res[2]]
                best = dict(candidate=cand, method="fuzzy", company_number=r[0], company_name=r[1],
                            score=round(res[1], 1), confidence=round(res[1] / 100 * 0.75, 2),
                            accepted=0, needs_review=1, notes="")
        if best:
            co = self.company(best["company_number"])
            if co and scheme_pc and _pc(co["reg_post_code"]) == scheme_pc:
                best["confidence"] = round(best["confidence"] + 0.1, 2)
                best["notes"] = "postcode match"
            if best["score"] >= C.FUZZY_ACCEPT:
                best["accepted"] = 1
            else:
                best["notes"] = (best["notes"] + "; below accept threshold").strip("; ")
            log.append(best)
            return (best if best["accepted"] else None), log
        log.append(dict(candidate=" | ".join(cands), method="none", company_number=None, company_name=None,
                        score=None, confidence=0, accepted=0, needs_review=0, notes="no candidate found"))
        return None, log


def upsert_sponsor(con, co, source="snapshot"):
    con.execute(
        """INSERT INTO sponsors (company_number, company_name, company_status, company_category, incorporation_date,
               dissolution_date, reg_post_code, sic_1, accounts_category, status_source, status_checked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(company_number) DO UPDATE SET company_name=excluded.company_name,
               company_status=excluded.company_status, dissolution_date=excluded.dissolution_date,
               reg_post_code=excluded.reg_post_code, status_source=excluded.status_source,
               status_checked_at=excluded.status_checked_at""",
        (co["company_number"], co["company_name"], co["company_status"], co["company_category"],
         co["incorporation_date"], co["dissolution_date"], co["reg_post_code"], co["sic_1"],
         co["accounts_category"], source))


def match_sponsors(con, rematch=False):
    m = Matcher()
    where = "" if rematch else "WHERE s.match_method IS NULL"
    schemes = con.execute(f"""SELECT s.psr, s.scheme_name, s.post_code,
                                 (SELECT company_name FROM ppf_schemes p WHERE p.scheme_name = s.ppf_scheme_name)
                              FROM schemes s {where}""").fetchall()
    if rematch:
        con.execute("DELETE FROM match_log WHERE method NOT LIKE 'api%'")
    for i, (psr, name, pc, hint) in enumerate(schemes, 1):
        best, log = m.match_one(name, pc, extra_candidates=[hint] if hint else ())
        con.executemany(
            """INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number, company_name,
                   score, confidence, accepted, needs_review, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(psr, name, l["candidate"], l["method"], l["company_number"], l["company_name"], l["score"],
              l["confidence"], l["accepted"], l["needs_review"], l["notes"]) for l in log])
        derived = (hint or (sponsor_candidates(name) or [None])[0])
        if best:
            upsert_sponsor(con, m.company(best["company_number"]))
            con.execute("""UPDATE schemes SET sponsor_name_derived=?, sponsor_company_number=?, match_method=?,
                               match_confidence=?, needs_review=? WHERE psr=?""",
                        (best["candidate"], best["company_number"], best["method"], best["confidence"],
                         best["needs_review"], psr))
        else:
            nc = any(l["method"] == "non_company" for l in log)
            con.execute("""UPDATE schemes SET sponsor_name_derived=?, sponsor_company_number=NULL,
                               match_method=?, match_confidence=0, needs_review=? WHERE psr=?""",
                        (derived, "non_company" if nc else "unmatched",
                         int(any(l["method"] == "fuzzy" for l in log)), psr))
        if i % 500 == 0:
            con.commit()
            print(f"  matched {i:,}/{len(schemes):,}", end="\r")
    con.execute("DELETE FROM sponsors WHERE company_number NOT IN "
                "(SELECT sponsor_company_number FROM schemes WHERE sponsor_company_number IS NOT NULL)")
    con.commit()
    print()


# Words too common to tie a scheme to a company by themselves (BBVA LONDON ~ ABLE PROJECTS LONDON).
_GENERIC = set("""LONDON ROYAL NATIONAL BRITISH BRITAIN ENGLAND ENGLISH SCOTLAND SCOTTISH WALES WELSH IRELAND IRISH
    NORTHERN NORTH SOUTH EAST WEST CENTRAL INTERNATIONAL EUROPE EUROPEAN HOLDINGS GROUP SERVICES INDUSTRIES
    ENGINEERING ASSOCIATION SOCIETY COUNCIL TRUST CITY COUNTY COMPANY SONS BROTHERS PARTNERS UK LIMITED PLC LLP
    STAFF EMPLOYEES WORKS PENSION PENSIONS SCHEME PLAN FUND RETIREMENT SUPERANNUATION BENEFITS""".split())
_TRUSTEE_RE = re.compile(r"\b(TRUSTEES?|PENSIONS?|NOMINEES?)\b")


def match_by_postcode(con, max_schemes_at_pc=2, max_companies_at_pc=25):
    """Last resort for unmatched schemes: a live company registered at the scheme's own postcode whose name
    shares the sponsor's leading distinctive word. Administrator hubs (many schemes at one postcode) and
    formation-agent addresses (many companies) are skipped. Every hit is flagged for review."""
    m = Matcher()
    m.ch.execute("CREATE INDEX IF NOT EXISTS ix_comp_pc ON companies(reg_post_code)")
    per_pc = dict(con.execute("SELECT upper(trim(post_code)), count(*) FROM schemes GROUP BY 1").fetchall())
    con.execute("DELETE FROM match_log WHERE method='postcode'")       # rerunnable: undo the previous pass
    con.execute("""UPDATE schemes SET sponsor_company_number=NULL, match_method='unmatched', match_confidence=0
                   WHERE match_method='postcode'""")
    rows = con.execute("""SELECT psr, scheme_name, upper(trim(post_code)) FROM schemes
                          WHERE match_method='unmatched' AND post_code IS NOT NULL""").fetchall()
    found = 0
    for psr, name, pc in rows:
        if per_pc.get(pc, 0) > max_schemes_at_pc:
            continue
        cos = m.ch.execute("SELECT * FROM companies WHERE reg_post_code=?", (pc,)).fetchall()
        if not cos or len(cos) > max_companies_at_pc:
            continue
        best = None
        for cand in sponsor_candidates(name):
            words = [w for w in core(norm(cand)).split() if w not in _GENERIC and len(w) >= 3]
            if not words:
                continue
            for co in cos:
                cw = norm(co["company_name"]).split()
                if max(fuzz.ratio(words[0], x) for x in cw) < 90:      # the brand word must be there
                    continue
                shared = sum(max(fuzz.ratio(w, x) for x in cw) >= 85 for w in words)
                if shared < min(2, len(words)):                            # one shared word is chance
                    continue
                key = (not _TRUSTEE_RE.search(norm(co["company_name"])), shared, -len(cw))
                if best is None or key > best[0]:
                    best = (key, cand, co, shared, len(words))
        if not best:
            continue
        (is_employer, _, _), cand, co, shared, nwords = best
        conf = 0.5 + (0.1 if shared == nwords else 0)
        notes = f"postcode match only ({len(cos)} companies at postcode); {shared}/{nwords} sponsor words in name"
        if not is_employer:
            notes += "; trustee/pension company, not the employer - lead only"
        con.execute("""INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number,
                           company_name, score, confidence, accepted, needs_review, notes)
                       VALUES (?,?,?,'postcode',?,?,?,?,?,1,?)""",
                    (psr, name, cand, co["company_number"], co["company_name"], round(100 * shared / nwords, 1),
                     conf, int(is_employer), notes))
        if not is_employer:
            continue
        upsert_sponsor(con, co)
        con.execute("""UPDATE schemes SET sponsor_name_derived=?, sponsor_company_number=?, match_method='postcode',
                           match_confidence=?, needs_review=1 WHERE psr=?""", (cand, co["company_number"], conf, psr))
        found += 1
    con.execute("DELETE FROM sponsors WHERE company_number NOT IN "
                "(SELECT sponsor_company_number FROM schemes WHERE sponsor_company_number IS NOT NULL)")
    con.commit()
    print(f"Postcode: matched {found:,} of {len(rows):,} unmatched schemes (all flagged for review)")


# ---------------------------------------------------------------- PPF / FAS -> schemes

def _match_scheme_names(con, rows, threshold=92):
    """rows: [(name, payload...)] -> {psr: (score, payload)} using exact normalised then fuzzy match."""
    def distinctive(n):              # "Bonas Group Pension Scheme" -> "BONAS GROUP": boilerplate dominates otherwise
        return core(norm((sponsor_candidates(n) or [n])[0]))

    schemes = con.execute("SELECT psr, scheme_name FROM schemes").fetchall()
    keys = [norm(n) for _, n in schemes]
    dkeys = [distinctive(n) for _, n in schemes]
    exact = {}
    for (psr, _), k in zip(schemes, keys):
        exact.setdefault(k, []).append(psr)
    out = {}
    for row in rows:
        k = norm(row[0])
        if k in exact:
            for psr in exact[k]:
                out[psr] = (100.0, row)
            continue
        d = distinctive(row[0])
        if len(d) < 4:
            continue
        res = process.extractOne(d, dkeys, scorer=fuzz.token_sort_ratio, score_cutoff=threshold)
        if res and _words_agree(d, res[0]) and fuzz.token_sort_ratio(k, keys[res[2]]) >= 85:
            psr = schemes[res[2]][0]
            if psr not in out or out[psr][0] < res[1]:
                out[psr] = (round(res[1], 1), row)
    return out


def flag_ppf_fas(con):
    con.execute("""UPDATE schemes SET ppf_status=NULL, ppf_assessment_date=NULL, ppf_transfer_or_withdrawn_date=NULL,
                   ppf_match_score=NULL, ppf_scheme_name=NULL, fas_status=NULL, fas_transfer_date=NULL, fas_match_score=NULL""")
    ppf = _match_scheme_names(con, con.execute("SELECT * FROM ppf_schemes").fetchall())
    for psr, (score, (sname, _co, status, ad, td)) in ppf.items():
        con.execute("""UPDATE schemes SET ppf_status=?, ppf_assessment_date=?, ppf_transfer_or_withdrawn_date=?,
                       ppf_match_score=?, ppf_scheme_name=? WHERE psr=?""", (status, ad, td, score, sname, psr))
    fas = _match_scheme_names(con, con.execute("SELECT * FROM fas_schemes").fetchall())
    for psr, (score, (_n, tranche, status, td)) in fas.items():
        con.execute("UPDATE schemes SET fas_status=?, fas_transfer_date=?, fas_match_score=? WHERE psr=?",
                    (f"{tranche} {status}".strip(), td, score, psr))
    con.commit()
    print(f"PPF matched to {len(ppf)} EIOPA schemes; FAS matched to {len(fas)}")


MIN_TEXT = 3000          # characters: shorter saved accounts text is a failed read, not proof of no pension note
DB_HINT = re.compile(r"actuar|IAS\s?19|section 28|FRS\s?17\b|final pensionable|pensionable (?:salary|pay)|career average"
                     r"|pension (?:scheme )?(?:deficit|surplus)|(?:scheme|plan) (?:assets|liabilities)|multi[- ]employer",
                     re.I)        # DB wording the reader's "defined benefit" test misses: not proof of no DB


def _live_doubt(con):
    """live_doubt: why an on-register scheme is probably no longer a live DB scheme of its matched sponsor.
    Only set when nothing else points to DB (PPF/FAS listing, DB figures or a DB mention in the sponsor's or a
    parent's accounts, charity DB figures) and the match isn't a low-confidence one awaiting review.
      sponsor_dormant      the sponsor files dormant accounts: it employs no one and can't be running a DB scheme
      sponsor_no_accounts  the sponsor is over two years old and has never filed accounts
    Only ordinary companies and LLPs formed before 2016 are judged, and never trustee or nominee companies.
      accounts_no_db       the sponsor's latest full accounts were read and never mention defined benefit; a DB
                           sponsor must disclose its scheme, so it has most likely been merged, bought out or wound
                           up, or sits with another group company"""
    from . import pdfaccounts
    have = {r[1] for r in con.execute("PRAGMA table_info(schemes)")}
    if "live_doubt" not in have:
        con.execute("ALTER TABLE schemes ADD COLUMN live_doubt TEXT")
    con.execute("DROP TABLE IF EXISTS temp.short_text")      # texts too short or too DB-ish to rule DB out
    con.execute("CREATE TEMP TABLE short_text (company_number TEXT PRIMARY KEY)")
    for (num,) in con.execute("SELECT company_number FROM pdf_accounts WHERE status IN ('dc_only', 'no_pension_note')"
                              ).fetchall():
        item = pdfaccounts.latest_accounts(num)
        f = item and pdfaccounts.PDF_DIR / f"{num}_{item['date']}.txt"
        if not f or not f.exists() or f.stat().st_size < MIN_TEXT or DB_HINT.search(f.read_text()):
            con.execute("INSERT OR IGNORE INTO short_text VALUES (?)", (num,))
    has_charity = "charity_db_source" in have
    con.execute(f"""UPDATE schemes SET live_doubt = (
        SELECT CASE
            WHEN upper(sp.accounts_category) = 'DORMANT' THEN 'sponsor_dormant'
            WHEN upper(sp.accounts_category) = 'NO ACCOUNTS FILED'
                 AND substr(sp.incorporation_date, 7, 4) || '-' || substr(sp.incorporation_date, 4, 2) || '-'
                     || substr(sp.incorporation_date, 1, 2) < date('now', '-2 years') THEN 'sponsor_no_accounts'
            WHEN sp.db_pdf_status IN ('dc_only', 'no_pension_note')
                 AND sp.company_number NOT IN (SELECT company_number FROM temp.short_text) THEN 'accounts_no_db'
            END
        FROM sponsors sp WHERE sp.company_number = schemes.sponsor_company_number
          AND lower(sp.company_status) = 'active'
          -- ordinary UK companies and LLPs only: Royal Charter bodies, societies, assurance and overseas
          -- companies don't file (their own) accounts at Companies House
          AND (sp.company_number GLOB '[0-9]*' OR substr(sp.company_number, 1, 2) IN ('SC', 'NI', 'OC', 'SO', 'NC', 'R0'))
          -- trustee and nominee companies are normally dormant
          AND NOT (upper(sp.company_name) GLOB '*TRUSTEE*' OR upper(sp.company_name) GLOB '*NOMINEE*'
                   OR upper(sp.company_name) GLOB '*PENSION*')
          -- a company formed since 2016 is more likely a namesake (a matching question, not a liveness one)
          AND substr(sp.incorporation_date, 7, 4) < '2016')""")
    con.execute(f"""UPDATE schemes SET live_doubt = NULL WHERE live_doubt IS NOT NULL AND (
        coalesce(db_evidence, '') GLOB '*ppf*' OR coalesce(db_evidence, '') GLOB '*fas*'
        OR coalesce(db_evidence, '') GLOB '*accounts*'
        OR (needs_review = 1 AND coalesce(match_confidence, 0) < 0.7)
        {"OR charity_db_source IS NOT NULL" if has_charity else ""})""")


def flag_status(con):
    """Sponsor dissolved / insolvency flags, DB evidence, and the likely_live flag."""
    from .parents import ensure_columns
    ensure_columns(con)
    con.execute("""UPDATE schemes SET
        sponsor_dissolved = (SELECT CASE WHEN sp.company_status LIKE 'Dissolved%' OR sp.dissolution_date IS NOT NULL
                                         OR sp.company_status IN ('closed','converted-closed') THEN 1 ELSE 0 END
                             FROM sponsors sp WHERE sp.company_number = schemes.sponsor_company_number),
        sponsor_insolvency = (SELECT CASE WHEN """ + " OR ".join(
        f"lower(sp.company_status) LIKE '%{w}%'" for w in INSOLVENCY_WORDS) + """ THEN 1 ELSE 0 END
                             FROM sponsors sp WHERE sp.company_number = schemes.sponsor_company_number)""")
    con.execute("""UPDATE schemes SET db_evidence = NULLIF(trim(
            (CASE WHEN name_type IN ('DB_likely','hybrid_likely') THEN 'name,' ELSE '' END) ||
            (CASE WHEN ppf_status IS NOT NULL THEN 'ppf,' ELSE '' END) ||
            (CASE WHEN fas_status IS NOT NULL THEN 'fas,' ELSE '' END) ||
            (CASE WHEN EXISTS (SELECT 1 FROM sponsors sp WHERE sp.company_number = schemes.sponsor_company_number
                               AND (sp.db_obligation IS NOT NULL OR sp.db_plan_assets IS NOT NULL
                                    OR sp.db_surplus_deficit IS NOT NULL)) THEN 'sponsor_accounts,' ELSE '' END) ||
            (CASE WHEN EXISTS (SELECT 1 FROM sponsors sp WHERE sp.company_number = schemes.sponsor_company_number
                               AND sp.db_pdf_status = 'mentioned_no_figures') THEN 'sponsor_accounts_text,' ELSE '' END) ||
            (CASE WHEN EXISTS (SELECT 1 FROM sponsors sp WHERE sp.company_number = schemes.sponsor_company_number
                               AND sp.group_db_company_number IS NOT NULL) THEN 'parent_accounts,' ELSE '' END),
            ','), '')""")
    _live_doubt(con)
    con.execute("""UPDATE schemes SET likely_live = CASE
            WHEN ppf_status = 'Transferred' OR fas_status LIKE '%Transferred%' THEN 0
            WHEN live_doubt IS NOT NULL THEN 0
            -- a low-confidence match still awaiting review doesn't decide liveness (it may be the wrong namesake)
            WHEN sponsor_dissolved = 1 AND NOT (needs_review = 1 AND coalesce(match_confidence, 0) < 0.7) THEN 0
            ELSE 1 END""")
    con.commit()


# ---------------------------------------------------------------- same-district pass

# words that say nothing about which company it is
DISTRICT_SKIP = set("THE AND OF FOR STAFF PENSION PENSIONS SCHEME PLAN FUND RETIREMENT BENEFITS BENEFIT LIFE ASSURANCE "
                    "DEATH SUPERANNUATION GROUP SECTION EMPLOYEES EXECUTIVE WORKS COMPANY LIMITED PLC LLP UK TRUST "
                    "SALARIED OFFICERS DEFINED CONTRIBUTION FINAL SALARY SAVINGS SENIOR MANAGEMENT HOLDINGS "
                    "INTERNATIONAL BRITISH ENGLAND SCOTLAND WALES LONDON NATIONAL GENERAL UNITED SONS CO EUROPE "
                    "EUROPEAN SERVICES SYSTEMS INDUSTRIES".split())
NOT_SPONSOR = re.compile(r"\b(PENSION|PENSIONS|TRUSTEE|TRUSTEES|NOMINEE|NOMINEES)\b")
PUBLIC = re.compile(r"\bLGPS\b|pension fund$|fire (and|&) rescue|fire authority|fire service|police|county council|"
                    r"assembly members|members contributory|scottish enterprise|passenger transport", re.I)


def _district(pc):
    pc = _pc(pc)
    return pc[:-3] if len(pc) >= 5 else None


def _dist_words(name):
    return [t for t in core(norm(name)).split() if t not in DISTRICT_SKIP and len(t) >= 3 and not t.isdigit()]


def district_pass(con):
    """Unmatched schemes named after a company that the name passes could not find, e.g. "Innovia British Pension
    Scheme" (Innovia Films Limited, same Wigton district). Candidates: active companies registered in the scheme's
    postcode district whose name (current or previous) starts with one of the scheme name's distinctive words.
      - a scheme name with 2+ distinctive words needs 2 of them in the company name; one such company -> accepted
        (district_word, 0.8); several -> flagged for review
      - a single distinctive word (5+ letters) -> flagged for review (district_word, 0.6)
    Pension trustee and nominee companies, companies formed after the March 2020 register date and dormant companies
    (unless the only candidate) are not sponsors. Companies already rejected for the scheme are skipped, and so are
    public-sector scheme names (LGPS, fire, police...).
    Shared place names make this noisy (LGPS - South Yorkshire -> South Yorkshire Drilling), so every candidate goes
    to review (needs_review = 1); `review_decisions` records the verdicts, source 'claude' for the first run."""
    m = Matcher()
    rows = con.execute("""SELECT psr, scheme_name, sponsor_name_derived, post_code FROM schemes
        WHERE likely_live = 1 AND name_type <> 'DC_likely' AND sponsor_company_number IS NULL
          AND charity_number IS NULL AND match_method IN ('unmatched', 'rejected')""").fetchall()
    rejected = {(p, c) for p, c in con.execute(
        "SELECT psr, reviewed_company FROM review_decisions WHERE decision = 'reject' AND reviewed_company IS NOT NULL")}
    counts = {"accepted": 0, "review": 0}
    for psr, name, derived, pc in rows:
        d, words = _district(pc), _dist_words(derived or name)
        if not d or not words or PUBLIC.search(name):
            continue
        cands = {}
        for w in dict.fromkeys(words):
            for num, cname, prev, status, cpc, inc, acc in m.ch.execute(
                    """SELECT n.company_number, n.name, n.is_previous, c.company_status, c.reg_post_code,
                              c.incorporation_date, c.accounts_category
                       FROM names n JOIN companies c USING (company_number) WHERE n.first_tok = ?""", (w,)):
                if _district(cpc) != d or not (status or "").startswith("Active") or NOT_SPONSOR.search(cname or "") \
                        or (psr, num) in rejected or (inc or "")[-4:] >= "2020":
                    continue
                overlap = len(set(words) & set(core(norm(cname)).split()))
                if overlap > cands.get(num, (0,))[0]:
                    cands[num] = (overlap, cname, prev, (acc or "").upper() == "DORMANT")
        if not cands:
            continue
        need = 2 if len(words) >= 2 else 1
        good = {n: c for n, c in cands.items() if c[0] >= need}
        if not good or (need == 1 and len(words[0]) < 5):
            continue
        trading = {n: c for n, c in good.items() if not c[3]} or good
        best = max(trading.values(), key=lambda c: c[0])[0]
        top = {n: c for n, c in trading.items() if c[0] == best}
        sure = False        # always reviewed: see docstring
        num = sorted(top)[0]
        co = m.company(num)
        upsert_sponsor(con, co)
        note = (f"same postcode district {d}; {best} of {len(words)} distinctive words; "
                f"{len(top)} candidate(s): " + "; ".join(f"{n} {c[1]}" for n, c in sorted(top.items()))[:300])
        con.execute("""INSERT INTO match_log (psr, scheme_name, candidate_sponsor, method, company_number, company_name,
                           score, confidence, accepted, needs_review, notes) VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                    (psr, name, " ".join(words), "district_word", num, co["company_name"], best,
                     0.8 if sure else 0.6, 0 if sure else 1, note))
        con.execute("""UPDATE schemes SET sponsor_company_number = ?, match_method = 'district_word',
                           match_confidence = ?, needs_review = ? WHERE psr = ?""",
                    (num, 0.8 if sure else 0.6, 0 if sure else 1, psr))
        counts["accepted" if sure else "review"] += 1
    con.commit()
    return counts

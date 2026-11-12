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
    con.execute("""UPDATE schemes SET likely_live = CASE
            WHEN ppf_status = 'Transferred' OR fas_status LIKE '%Transferred%' THEN 0
            -- a low-confidence match still awaiting review doesn't decide liveness (it may be the wrong namesake)
            WHEN sponsor_dissolved = 1 AND NOT (needs_review = 1 AND coalesce(match_confidence, 0) < 0.7) THEN 0
            ELSE 1 END""")
    con.commit()

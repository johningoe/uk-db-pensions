"""Is each target scheme (band in or candidate) still a live DB scheme? Evidence from what is already on disk.

The scheme register is a March 2020 snapshot, and schemes of £50m-£500m have been the core of the buy-out market
since. scan(con) reads the employer's saved accounts text (PDF and iXBRL) and charity accounts, and records per
scheme in scheme_liveness:

  signal     bought_out   accounts say the scheme was bought out, wound up or its liabilities discharged
             winding_up   accounts say a buy-out or wind-up is under way or agreed (still live until it completes)
             buy_in       accounts say the scheme holds a buy-in policy (still live)
             figures      recent DB figures (year end 2024 or later) for the employer's only live DB scheme
             none         nothing either way
  evidence   the sentence (or figure) behind the signal, with the filing it came from
  names      1 when the sentence names this scheme (or the employer has only one live DB scheme)

The signals are leads, not verdicts: a sentence may be about another scheme of the group or about plans not yet
carried out. A lead confirmed by review goes to liveness_overrides.csv (applied in match.flag_status like every
other liveness decision); one set aside goes to live_checked.csv. band.build turns the signals into
schemes.live_check.

    python run.py liveness
"""
import glob
import re
from pathlib import Path

from . import config as C
from . import pdfaccounts as P
from .names import core, norm

PDF_DIR = P.PDF_DIR
CHAR_DIR = C.RAW / "charities" / "accounts"

# completed: past tense, scheme gone
DONE = re.compile(
    r"(?:was|were|has been|have been|had been)\s+(?:fully\s+|formally\s+|subsequently\s+)?(?:bought[- ]out|wound[- ]up|"
    r"discharged|secured with individual|transferred to individual|converted (?:in)?to individual)"
    r"|(?:completed|completion of)\s+(?:a|the|its)?\s*(?:full\s+)?(?:buy[- ]?out|wind(?:ing)?[- ]up)"
    r"|buy[- ]?out\s+(?:was|has been)\s+completed"
    r"|(?:scheme|plan)\s+(?:was|has been)\s+(?:formally\s+)?wound[- ]up"
    r"|individual (?:annuity )?policies (?:were|have been) (?:issued|assigned)"
    r"|no longer (?:has|have|operates?|sponsors?|participates? in) (?:a|any) defined benefit"
    r"|(?:liabilities|obligations) (?:of the scheme )?(?:were|have been) (?:fully )?(?:discharged|extinguished|settled)", re.I)
# under way: a buy-out or wind-up agreed or started, not yet complete
UNDERWAY = re.compile(
    r"(?:in the process of|commenced|began|begun|started|expected to|intends? to|plans? to|will be|is being|"
    r"proceed(?:ing)? with|move to|moving to|with a view to|ahead of|prior to|in preparation for)\s+(?:a\s+|the\s+|full\s+)*"
    r"(?:buy[- ]?out|wind(?:ing)?[- ]up|wound[- ]up)|(?:buy[- ]?out|wind(?:ing)?[- ]up) (?:is|was) (?:expected|anticipated)", re.I)
BUYIN = re.compile(r"buy[- ]?in|bulk annuity|insurance (?:policy|contract) (?:that|which) (?:matches|covers|insures)", re.I)
TOPIC = re.compile(r"pension|scheme|plan|annuit|trustee", re.I)


def _sentences(text):
    text = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.;])\s+(?=[A-Z(])", text)


def _texts(co, reg, rno):
    """[(label, text)] newest first: saved accounts text for a company, else a charity's accounts."""
    out = []
    if co:
        for f in sorted(PDF_DIR.glob(f"{co}_*.txt"), reverse=True)[:2]:
            raw = f.read_text(errors="ignore")
            out.append((f"accounts filed {f.stem.split('_')[1]}", raw.partition("\n")[2]))
    elif rno:
        for f in sorted(glob.glob(str(CHAR_DIR / f"{reg}_{rno}_*.txt")), reverse=True)[:1]:
            out.append((Path(f).stem, Path(f).read_text(errors="ignore")))
    return out


def _ixbrl_text(con, co, files):
    row = con.execute("""SELECT source_file FROM accounts_facts WHERE company_number=? ORDER BY period_end DESC LIMIT 1""",
                      (co,)).fetchone()
    p = files.get(row[0]) if row else None
    if not p:
        return []
    try:
        return [(p.name, P.xhtml_text(P._xhtml(p.read_bytes(), "zip" if p.suffix == ".zip" else "html")))]
    except Exception:
        return []


GENERIC = {"PENSION", "PENSIONS", "SCHEME", "PLAN", "FUND", "RETIREMENT", "BENEFITS", "BENEFIT", "STAFF", "THE", "AND",
           "LIMITED", "LTD", "GROUP", "SECTION", "OF", "FOR", "LIFE", "ASSURANCE", "SUPERANNUATION", "UK", "PLC",
           "EMPLOYEES", "WORKS", "EXECUTIVE", "SENIOR", "TRUST", "COMPANY", "1", "2", "NO"}


def _key_words(scheme_name):
    return [w for w in core(norm(scheme_name)).split() if len(w) > 1 and w not in GENERIC and not w.isdigit()]


def _names_scheme(sentence, scheme_name):
    key = [w for w in _key_words(scheme_name) ]
    s = sentence.upper()
    return bool(key) and sum(w in s for w in key) >= max(1, (len(key) + 1) // 2)


def scan(con):
    con.execute("""CREATE TABLE IF NOT EXISTS scheme_liveness (psr TEXT PRIMARY KEY, signal TEXT, names INTEGER,
                   evidence TEXT, source TEXT)""")
    con.execute("DELETE FROM scheme_liveness")
    rows = con.execute("""
        SELECT s.psr, s.scheme_name, s.sponsor_company_number, s.charity_source, s.charity_number, sf.basis,
               sf.period_end, sf.obligation, sp.db_period_end
        FROM schemes s LEFT JOIN scheme_figures sf ON sf.psr = s.psr
        LEFT JOIN sponsors sp ON sp.company_number = s.sponsor_company_number
        WHERE s.band IN ('in', 'candidate')""").fetchall()
    files = {p.name: p for p in C.CH_ACCOUNTS_DIR.rglob("Prod*")}
    cache, out = {}, []
    rank = {"bought_out": 4, "winding_up": 3, "buy_in": 2}
    for psr, name, co, reg, rno, basis, spe, sob, pe in rows:
        key = co or (reg, rno)
        if key not in cache:
            texts = _texts(co, reg, rno) or (_ixbrl_text(con, co, files) if co else [])
            hits = []
            for label, text in texts:
                for snt in _sentences(text):
                    if len(snt) > 600 or not TOPIC.search(snt):
                        continue
                    sig = "bought_out" if DONE.search(snt) else "winding_up" if UNDERWAY.search(snt) else \
                          "buy_in" if BUYIN.search(snt) else None
                    if sig:
                        hits.append((sig, snt.strip()[:400], label))
            cache[key] = hits
        hits = cache[key]
        single = basis in ("employer_single", "register_single")
        best = None
        for sig, snt, label in hits:
            names = 1 if (single or _names_scheme(snt, name)) else 0
            score = (names, rank[sig])
            if best is None or score > best[0]:
                best = (score, sig, snt, label, names)
        if best and (best[4] or best[1] != "buy_in"):
            out.append((psr, best[1], best[4], best[2], best[3]))
        elif single and spe and str(spe) >= "2024-01-01" and (sob or 0) > 0:
            out.append((psr, "figures", 1, f"DB obligation in accounts to {str(spe)[:10]}", basis))
        else:
            out.append((psr, "none", 0, None, None))
    con.executemany("INSERT INTO scheme_liveness (psr, signal, names, evidence, source) VALUES (?,?,?,?,?)", out)
    con.commit()
    counts = {}
    for _, sig, names, *_ in out:
        k = f"{sig}{'' if names or sig in ('none', 'figures') else ' (not named)'}"
        counts[k] = counts.get(k, 0) + 1
    return counts


# ---------------------------------------------------------------- trustee companies

TRUSTEE = re.compile(r"\bTRUSTEES?\b")
PENSIONISH = re.compile(r"\b(?:PENSIONS?|RETIREMENT|SUPERANNUATION)\b")
DCISH = re.compile(r"DEFINED CONTRIBUTION|\bDC\b|\bSIPP\b|\bSSAS\b")


def trustees(con, limit=None):
    """Corporate trustees: a pension trustee company named like the scheme, from the October 2026 snapshot (active)
    and the Companies House company search (dissolved since April 2020, when the register snapshot was taken).
    A trustee company dissolved after 2020 usually means the scheme was wound up; one still active is weak evidence
    the scheme is live. Writes trustee_status / trustee_evidence on scheme_liveness."""
    import sqlite3
    from . import chapi
    for col in ("trustee_status", "trustee_evidence"):
        try:
            con.execute(f"ALTER TABLE scheme_liveness ADD COLUMN {col} TEXT")
        except Exception:
            pass
    snap = sqlite3.connect(C.DATA / "ch_snapshot.sqlite")
    active = [(n, norm(n), num) for num, n in snap.execute(
        "SELECT company_number, company_name FROM companies WHERE company_name LIKE '%TRUSTEE%'")]
    rows = con.execute("""SELECT l.psr, s.scheme_name FROM scheme_liveness l JOIN schemes s USING (psr)
                          WHERE l.signal IN ('none', 'figures', 'buy_in')""").fetchall()[:limit]
    def fits(name_norm, words):
        toks = name_norm.split()
        return bool(TRUSTEE.search(name_norm) and PENSIONISH.search(name_norm) and not DCISH.search(name_norm)
                    and words and all(w in toks for w in words[:3]))
    out, found = [], {"dissolved": 0, "active": 0}
    for i, (psr, name) in enumerate(rows, 1):
        words = _key_words(name)
        st = ev = None
        if words:
            act = [(n, num) for n, nn, num in active if fits(nn, words)]
            try:
                res = chapi._get("/search/companies", {"q": " ".join(words[:3]) + " PENSION TRUSTEE",
                                                       "items_per_page": 20}) or {}
            except Exception:
                res = {}
            dis = [(it.get("title") or "", it["company_number"], it.get("date_of_cessation") or "")
                   for it in res.get("items", []) if it.get("company_status") == "dissolved"
                   and fits(norm(it.get("title") or ""), words) and (it.get("date_of_cessation") or "") >= "2020-04-01"]
            if act:
                st, ev = "active", "; ".join(f"{n} ({num})" for n, num in act[:3])
            elif dis:
                st, ev = "dissolved", "; ".join(f"{n} ({num}) dissolved {d}" for n, num, d in dis[:3])
        if st:
            found[st] += 1
        out.append((st, ev, psr))
        if i % 100 == 0:
            print(f"  trustees {i:,}/{len(rows):,}: {found}", flush=True)
    con.executemany("UPDATE scheme_liveness SET trustee_status=?, trustee_evidence=? WHERE psr=?", out)
    con.commit()
    return found

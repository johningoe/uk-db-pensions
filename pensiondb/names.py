"""Name normalisation, sponsor derivation from scheme names, and DB/DC hints from scheme names."""
import re
import unicodedata

LEGAL_SUFFIXES = {"LIMITED", "PLC", "LLP", "LP", "CIC", "UNLIMITED"}
TOKEN_MAP = {
    "LTD": "LIMITED", "LTD.": "LIMITED", "CO": "COMPANY", "COY": "COMPANY",
    "INTL": "INTERNATIONAL", "BROS": "BROTHERS", "HLDGS": "HOLDINGS", "GRP": "GROUP",
}
STOPWORDS = {"THE", "AND", "OF", "FOR"}


def _ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def norm(name):
    """Companies-House-style normalised key: upper case, & -> AND, punctuation dropped,
    LTD -> LIMITED, PUBLIC LIMITED COMPANY -> PLC, runs of single letters joined (U K -> UK)."""
    if not name:
        return ""
    s = _ascii(str(name)).upper()
    s = s.replace("&", " AND ").replace("+", " AND ")
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\bPUBLIC LIMITED COMPANY\b", "PLC", s)
    s = re.sub(r"\bP L C\b", "PLC", s)
    toks = [TOKEN_MAP.get(t, t) for t in s.split()]
    out, run = [], []
    for t in toks:                      # join single-letter runs: "T J THOMSON" -> "TJ THOMSON"
        if len(t) == 1 and t.isalpha():
            run.append(t)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(t)
    if run:
        out.append("".join(run))
    if out and out[0] == "THE":
        out = out[1:]
    return " ".join(out)


def core(normed):
    """Normalised name with trailing legal suffixes removed (for suffix-insensitive matching)."""
    toks = normed.split()
    while toks and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def first_token(normed):
    for t in core(normed).split():
        if t not in STOPWORDS:
            return t
    return ""


# ---------------------------------------------------------------- sponsor derivation

_PENSION_WORDS = (
    r"pension|pensions|retirement|superannuation|life assurance|death benefit|benefits? (?:scheme|plan|fund)"
    r"|final salary|money purchase|stakeholder|group personal|gpp|personal pension|savings plan"
    r"|defined contribution|defined benefits?|additional voluntary|free standing|self[- ]invested|avc"
    r"|occupational|supplementary|additional|individual|top[- ]up|unapproved|voluntary|assurance"
    r"|scheme|plan|fund|section"
)
_PRE_PENSION_FILLER = (
    r"staff|executive|executives|directors?|senior|management|managers|employees?|works|workers"
    r"|salaried|hourly paid|officials|company|uk|1st|2nd|no\.? ?\d|number \d|\d{4}|\(\d{4}\)"
)
_LEGAL_RE = re.compile(r"^(.*?\b(?:limited|ltd\.?|plc|p\.l\.c\.?|llp|public limited company))(?=\W|$)", re.I)
_PENSION_RE = re.compile(rf"\b(?:{_PENSION_WORDS})\b", re.I)
_FILLER_TAIL_RE = re.compile(rf"(?:[\s,\-&]+(?:{_PRE_PENSION_FILLER}|and|&|the|of|for)['’]?)+\s*$", re.I)


def _clean(s):
    s = re.sub(r"\((?:the|\d+)\)", " ", s, flags=re.I)
    s = re.sub(r"\s*\([^)]*$", "", s)                 # unclosed trailing "(London"
    s = re.sub(r",\s*the\s*$", "", s, flags=re.I)
    s = re.sub(r"^\s*the\s+", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" ,-:;(")
    if s.count("(") < s.count(")"):
        s = s.rstrip(")").strip()
    return s


def _strip_pension_tail(s):
    m = _PENSION_RE.search(s)
    if not m:
        return _clean(s)
    head = s[: m.start()]
    head = _FILLER_TAIL_RE.sub("", head)
    return _clean(head)


def sponsor_candidates(scheme_name):
    """Ordered list of likely sponsor names derived from a scheme name.
    Section schemes ("X Section of Y Scheme", "Y Scheme (X Section)", "Y: X Section") yield X first, then Y."""
    name = re.sub(r"\s+", " ", str(scheme_name)).strip()
    name = re.sub(r"\s*\(\d+\)", "", name)              # "(806888)" reference numbers
    parts = []
    m = re.match(r"^(.*?)\s+section\s+of\s+(.*)$", name, re.I)
    m2 = re.match(r"^(.*?)\s*[\(:\-–]\s*([^():]*?)\s+section\)?\s*$", name, re.I)
    m3 = re.search(r"\b(?:for (?:the )?(?:staff|employees|members)|(?:scheme|plan|fund)) of (?:the )?(.+)$", name, re.I)
    if m:
        parts = [m.group(1), m.group(2)]
    elif m2 and m2.group(1) and _PENSION_RE.search(m2.group(1)):
        parts = [m2.group(2), m2.group(1)]            # "Y Pension Scheme (X Section)" / "Y Scheme: X Section"
    elif m3:
        parts = [m3.group(1)]
    else:
        m = re.match(r"^(.*?)\s+section\s*$", name, re.I)
        parts = [m.group(1)] if m else [name]

    out = []
    for p in parts:
        lm = _LEGAL_RE.match(p)
        for c in ([_clean(lm.group(1))] if lm else []) + [_strip_pension_tail(p)]:
            if c and len(norm(c)) >= 2 and c not in out:
                out.append(c)
        # also try without a trailing "Group" ("Northern Foods Group" -> "Northern Foods")
        base = out[-1] if out else ""
        g = re.sub(r"\s+group$", "", base, flags=re.I)
        if g and g != base and g not in out:
            out.append(g)
    return out


def is_section(scheme_name):
    return bool(re.search(r"\bsection\b", str(scheme_name), re.I))


# ---------------------------------------------------------------- DB / DC hints from the scheme name

_DC_RE = re.compile(
    r"group personal pension|\bgpp\b|stakeholder|money purchase|defined contribution|\bdc\b|personal pension"
    r"|savings plan|master trust|self[- ]invested|\bsipp\b|\bssas\b|group pension plan|pension builder|flexible pension"
    # insurers' AVC / personal products and master trusts that sit in the register as "schemes"
    r"|free[- ]standing|\bfsavc\b|\bavc\b|additional voluntary|voluntary contribution|top[- ]up|retirement account"
    r"|pension account|\bnest\b|people's pension|now: pensions",
    re.I)
_HYBRID_RE = re.compile(r"hybrid|cash balance|career average|\bcare\b", re.I)
_DB_RE = re.compile(
    r"final salary|defined benefits?|\bdb\b|salary related|life assurance|death benefit|superannuation"
    r"|pension (?:and|&) life|works pension|\bpension fund\b|retirement benefits? (?:scheme|fund)",
    re.I)


def name_type(scheme_name):
    s = str(scheme_name)
    if _HYBRID_RE.search(s):
        return "hybrid_likely"
    if _DC_RE.search(s):
        return "DC_likely"
    if _DB_RE.search(s):
        return "DB_likely"
    return "unknown"


# Bodies that are not Companies House registrants (or not usefully matched by name): skip fuzzy matching.
NON_COMPANY_RE = re.compile(
    r"\bpolice\b|\bteachers?\b|\bcouncil\b|\bcounty\b|\bborough\b|\bnhs\b|fire (?:and rescue|service|fighters?)"
    r"|local government|civil service|armed forces|\buniversity\b|\bcollege\b|friendly society|\bchurch\b"
    r"|\bdiocese\b|\bdiocesan\b|\bregulations\b|\bcc\b|parliament|judicial|\bmet office\b",
    re.I)


def non_company(scheme_name):
    return bool(NON_COMPANY_RE.search(str(scheme_name)))

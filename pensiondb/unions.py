"""Trade unions and employers' associations: their accounts from the Certification Officer.

Unions and employers' associations don't register at Companies House or the Charity Commission. Each one listed
with the Certification Officer files an annual return (form AR21 for unions, AR27 for employers' associations), and
gov.uk publishes it as a PDF in a collection per body ("Usdaw: information held by the Certification Officer").
Most returns attach the full accounts, pension note included; a few (UNISON) attach only the form.

The bodies are added to charity_register with source 'co' (number = the collection's gov.uk slug), so the same
review decisions (charity_decisions.csv) and accounts reader (charities.read_accounts) work for them. Matching is by
hand: most staff schemes carry an old union's name (COHSE, NATFHE, AEEU), so the decision names the successor.

    python run.py charities-download    # also saves the list of Certification Officer bodies
"""
import json
import re

from . import config as C

LIST = C.RAW / "charities" / "certification_officer.json"
SEARCH = ("https://www.gov.uk/api/search.json?filter_organisations=certification-officer"
          "&filter_content_store_document_type=document_collection&count=500&start={}&fields=title,link")
CONTENT = "https://www.gov.uk/api/content"
SUFFIX = re.compile(r"\s*:\s*(?:information held by the certification officer|annual returns.*)$", re.I)


def download(session):
    out, start = [], 0
    while True:
        d = session.get(SEARCH.format(start), timeout=60).json()
        out += d["results"]
        start += 500
        if start >= d["total"]:
            break
    bodies = [{"name": SUFFIX.sub("", r["title"]).strip(), "slug": r["link"].rsplit("/", 1)[-1]} for r in out
              if "certification officer decisions" not in r["title"].lower()]
    LIST.write_text(json.dumps(bodies, indent=1))
    print(f"certification_officer.json: {len(bodies)} bodies")


def bodies():
    return json.loads(LIST.read_text()) if LIST.exists() else []


def latest(slug, http):
    """(year, PDF url) of the body's latest annual return."""
    coll = http(f"{CONTENT}/government/collections/{slug}").json()
    docs = [d for d in coll.get("links", {}).get("documents", []) if "annual return" in d["title"].lower()]
    if not docs:
        return None
    pub = http(CONTENT + docs[0]["base_path"]).json()
    best = None
    for a in pub.get("details", {}).get("attachments", []):
        y = re.search(r"\b(20\d\d)\b", a.get("title", ""))
        if a.get("url", "").lower().endswith(".pdf") and y and (best is None or y.group(1) > best[0]):
            best = (y.group(1), a["url"])
    return best

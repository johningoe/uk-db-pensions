"""Read PDF accounts on GitHub Actions instead of this machine.

The cloud workers need no database: they get a list of company numbers, read each company's latest accounts
(Tesseract OCR on Linux), and hand back one JSON line per company plus the extracted text. The database stays
here; `import` loads the results into pdf_accounts exactly as a local run would.

    python run.py cloud-export        # write cloud/todo.json: own accounts still to read + the parent frontier
    (commit, push, run the "PDF accounts" workflow, download its artifacts into cloud/results/)
    python run.py cloud-import        # load cloud/results/ into pdf_accounts, then run.py status

Parents are read nearest first, so after an import a new export picks up the next level up (a second, smaller round).

Worker (what the workflow runs):
    python -m pensiondb.cloud work --shard 3/8 --procs 4 --minutes 300

Progress: every PROGRESS_EVERY seconds each worker prints a line and, on GitHub Actions, posts it as a commit status
("progress/shard-3") on the commit being run, so progress can be read while the run is going:
    python run.py cloud-progress
"""
import argparse
import collections
import json
import multiprocessing as mp
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as C

CLOUD = C.ROOT / "cloud"
TODO = CLOUD / "todo.json"
RESULTS = CLOUD / "results"
PROGRESS_EVERY = 300
COLS = ["filing_date", "description", "paper_filed", "pages", "text_source", "status", "obligation", "assets",
        "net", "unit", "page", "evidence"]


# ---------------------------------------------------------------- here: export / import

def export(con):
    from . import parents, pdfaccounts
    done = {r[0] for r in con.execute("SELECT company_number FROM pdf_accounts")}
    own = [n for n in pdfaccounts.pilot_sample(con, n=None) if n not in done]
    skip = parents.pending_own_pdf(con)
    par = [n for n in parents.frontier(con, skip) if n not in done]
    todo = [[n, pdfaccounts.MAX_PAGES] for n in own] + [[n, parents.PARENT_MAX_PAGES] for n in par if n not in own]
    CLOUD.mkdir(exist_ok=True)
    TODO.write_text(json.dumps(todo))
    print(f"cloud/todo.json: {len(own):,} sponsors' own accounts + {len(par):,} parents = {len(todo):,} documents")


def import_(con, src=RESULTS):
    from . import pdfaccounts
    pdfaccounts._schema(con)
    n = 0
    for f in sorted(Path(src).rglob("*.jsonl")):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            con.execute(f"INSERT OR REPLACE INTO pdf_accounts (company_number, {', '.join(COLS)}, checked_at) "
                        f"VALUES (?, {', '.join('?' * len(COLS))}, ?)",
                        [r["company_number"], *[r.get(c) for c in COLS], r.get("checked_at")])
            n += 1
    con.commit()
    copied = 0
    for sub, dest in (("pdf_accounts", pdfaccounts.PDF_DIR), ("api_cache", C.CH_API_CACHE)):
        dest.mkdir(parents=True, exist_ok=True)
        for f in Path(src).rglob(f"{sub}/*"):
            if f.is_file() and not (dest / f.name).exists():
                shutil.copy2(f, dest / f.name)
                copied += 1
    print(f"Imported {n:,} results; copied {copied:,} text/cache files")


# ---------------------------------------------------------------- cloud: worker

def _worker(todo, out, deadline):
    from . import pdfaccounts
    with open(out, "a") as fh:
        for i, (num, max_pages) in enumerate(todo, 1):
            if time.time() > deadline:
                print(f"{out.name}: time limit reached after {i - 1} documents", flush=True)
                return
            try:
                r = pdfaccounts.assess(num, max_pages=max_pages)
            except Exception as e:              # noqa: BLE001 - record and carry on
                r = dict(status="error", evidence=str(e)[:300])
            r = {c: r.get(c) for c in COLS}
            r.update(company_number=num, checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            print(f"{out.name} [{i}/{len(todo)}] {num} {r['status']}", flush=True)


def work(shard, procs, minutes, out_dir):
    from . import pdfaccounts
    i, n = (int(x) for x in shard.split("/"))
    todo = json.loads(TODO.read_text())
    deadline = time.time() + minutes * 60
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = n * procs                           # each process takes every total-th document
    ps = []
    for k in range(procs):
        part = todo[i * procs + k::total]
        p = mp.Process(target=_worker, args=(part, out_dir / f"results_{i}_{k}.jsonl", deadline))
        p.start()
        ps.append(p)
    share = sum(len(todo[i * procs + k::total]) for k in range(procs))
    started = last = time.time()
    while any(p.is_alive() for p in ps):
        time.sleep(5)
        if time.time() - last >= PROGRESS_EVERY:
            _report(i, n, out_dir, share, started)
            last = time.time()
    for p in ps:
        p.join()
    _report(i, n, out_dir, share, started, final=True)
    # hand back the extracted text and the API responses, so later steps here need no network
    for sub, src, pattern in (("pdf_accounts", pdfaccounts.PDF_DIR, ("*.txt", "*.xhtml", "*.zip")),
                              ("api_cache", C.CH_API_CACHE, ("*.json",))):
        (out_dir / sub).mkdir(exist_ok=True)
        for pat in pattern:
            for f in src.glob(pat):
                shutil.copy2(f, out_dir / sub / f.name)


def _counts(out_dir):
    c = collections.Counter()
    for f in Path(out_dir).glob("results_*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                c[json.loads(line).get("status")] += 1
    return c


def _report(i, n, out_dir, share, started, final=False):
    """Print a progress line and, on GitHub Actions, post it as a commit status (readable mid-run)."""
    c = _counts(out_dir)
    done = sum(c.values())
    line = (f"{done}/{share} done, {c['figures']} with figures, {c['error']} errors, "
            f"{(time.time() - started) / 60:.0f} min")
    print(f"PROGRESS shard {i}/{n}: {line}", flush=True)
    token, repo, sha = (os.environ.get(k) for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_SHA"))
    if not (token and repo and sha):
        return
    import requests
    run = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
    try:
        requests.post(f"https://api.github.com/repos/{repo}/statuses/{sha}", timeout=20,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"state": "success" if final else "pending", "context": f"progress/shard-{i}",
                            "description": line[:140], "target_url": run})
    except requests.RequestException:
        pass                                    # progress is a nicety; never stop the run for it


def progress(sha=None):
    """Here: show the latest progress line each cloud worker posted (needs the gh CLI, signed in)."""
    import subprocess
    repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if not sha:                                 # the commit the latest workflow run is using, not this checkout's HEAD
        sha = subprocess.run(["gh", "run", "list", "-R", repo, "-w", "pdf-accounts.yml", "-L", "1", "--json", "headSha",
                              "-q", ".[0].headSha"], capture_output=True, text=True, check=True).stdout.strip()
    data = json.loads(subprocess.run(["gh", "api", f"repos/{repo}/commits/{sha}/status?per_page=100"],
                                     capture_output=True, text=True, check=True).stdout)
    rows = sorted((s["context"], s["state"], s["description"]) for s in data.get("statuses", [])
                  if s["context"].startswith("progress/"))
    for ctx, state, desc in rows:
        print(f"{ctx:20} {state:8} {desc}")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["work"])
    ap.add_argument("--shard", required=True)
    ap.add_argument("--procs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--minutes", type=float, default=300)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    work(a.shard, a.procs, a.minutes, a.out)

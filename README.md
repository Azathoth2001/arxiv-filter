# arXiv Filter

**English** · [简体中文](README.zh-CN.md)

> A local-first literature inbox for arXiv. Filter by category, UTC submission date and
> keywords, then read, track and export — with everything stored on your own machine.

<span id="what-it-is"></span>

## What it is

A small Windows desktop tool that turns arXiv's Atom API into a readable, filterable
inbox. You pick a category, a UTC date range and (optionally) keywords; it fetches the
matching papers, de-duplicates them, caches them locally, tracks what you have already
seen or read, and exports the result in five formats.

- No account, no Docker, no database server.
- Data is written only to `%LOCALAPPDATA%\ArxivFilter`.
- The web UI is served on `127.0.0.1` (loopback only) and protected by a per-launch token.

**It is not** an official arXiv product and is not affiliated with arXiv in any way.
It wraps arXiv's public [legacy Atom API](https://info.arxiv.org/help/api/user-manual.html)
with a Chinese-language UI and a local cache.

> Thank you to arXiv for use of its open access interoperability.

## Features

| | |
|---|---|
| **Search** | First-level + second-level category, inclusive UTC date range, keyword expressions with `AND` / `OR` / `NOT` and quoted phrases, field scope (all / title / abstract / author), three sort orders, configurable result cap |
| **Reading list** | Card layout, expandable abstract, mark read/unread, "new vs. seen" tracking that does not change while you scroll |
| **LaTeX rendering** | Math in titles and abstracts renders as real formulas (KaTeX, bundled locally — works offline) |
| **Local cache** | SQLite with WAL; fresh results reused for 6 hours, stale kept 30 days, 500 MiB LRU cap that never evicts your read state |
| **History** | Last 500 queries (or 90 days); click one to refill the form |
| **Export** | CSV (Excel-compatible, formula-injection safe), XLSX, JSON, Markdown, BibTeX |
| **Privacy** | Loopback-only server, CSP with no inline scripts, no third-party analytics, redacted diagnostic logs, proxy credentials never echoed back |

## Requirements

- **Windows 10 (1803+) or Windows 11, 64-bit**
- To run the prebuilt executable: nothing else.
- To build/run from source: **Python 3.12 x64**, plus **Node.js 24+** for the frontend tests.

> The app issues arXiv requests through the system `curl.exe` by default, because arXiv's
> edge intermittently rejects the `httpx` client fingerprint with `HTTP 406`. `curl` ships
> with Windows 10 1803+. If it is missing, the app falls back to `httpx` automatically and
> logs a warning — and may then hit that 406. See `docs/api-verification.md` §3.10.

## Quick start

### Option A — run the prebuilt executable

Download the release archive and double-click:

```text
arxiv-filter-windows-x64\start.bat      # recommended (onedir: starts fastest)
arxiv-filter.exe                        # single-file build, also self-contained
```

Your default browser opens the UI automatically. Nothing is installed system-wide:
deleting the folder uninstalls it.

Windows SmartScreen may warn on first launch because the build is unsigned — choose
**More info → Run anyway**.

### Option B — run from source

```bat
git clone <your-fork-url> arxiv-filter
cd arxiv-filter

:: create an environment (3.12 required)
conda create -n arxiv-filter -c conda-forge python=3.12 pip -y
conda run -n arxiv-filter python -m pip install -e ".[dev]"

:: verify
conda run -n arxiv-filter python -m arxiv_filter --version
```

Then double-click `start-dev.bat`, which finds an interpreter in the order
`.venv` → conda env `arxiv-filter` → `PATH`, starts the loopback server and opens the UI.

## Usage in 30 seconds

1. Pick a **first-level category** (defaults to Mathematics) and optionally a
   **second-level category** (defaults to Analysis of PDEs).
2. Adjust the **UTC date range** — the last 7 days are pre-filled; the `3 / 7 / 30 days`
   and `This month` shortcuts are one click away.
3. Type **keywords** if you want to narrow things down. Leave empty to search by category
   and date alone. Examples:
   - `PDEs AND Hessian AND estimate` — all three terms (intersection)
   - `PDEs Hessian estimate` — identical to the above; adjacent terms imply `AND`
   - `"large language model" AND retrieval NOT survey` — phrase, intersection, exclusion
   - Do **not** separate terms with commas.
4. Click **开始筛选 / Start filtering**. Progress is shown; you can cancel at any time.
5. Work the results: open a title on arXiv, expand the abstract, mark read/unread, page
   through, and **export** the whole filtered set when you are done.

A longer Chinese walkthrough with troubleshooting lives in [`docs/使用教程.md`](docs/使用教程.md).

## How it works

```text
Browser UI  ──HTTP──▶  FastAPI on 127.0.0.1 (token-gated, CSP-locked)
                            │
                            ├─ SearchService ──▶ SQLite cache ──▶ ArxivClient
                            │                        (fresh / stale / miss)
                            └─ JobManager (single serial job, progress, cancel)
                                                     │
                                          curl subprocess ──▶ export.arxiv.org Atom API
```

Design decisions worth knowing:

- **One request at a time.** A process-wide rate limiter enforces a ≥4.0 s gap between
  request starts and a single connection; identical concurrent queries collapse into one
  upstream sequence.
- **406 is treated as upstream rejection, not a syntax error.** It joins `429` on the
  backoff path (retry once, then a 5-minute circuit breaker) and is never blamed on the
  user's keywords.
- **No fallback to third-party mirrors.** If the upstream fails, only a *stale cache* is
  offered, and only after explicit confirmation.
- **The original LaTeX is always preserved.** Rendering only affects the DOM; every export
  format carries `summary_raw` verbatim.

## Testing

```powershell
# offline suite (network is blocked by a socket deny-list)
python -m pytest -m 'not live_arxiv' -q

# browser E2E (needs Chrome/Edge; uses fake providers, never touches arXiv)
$env:ARXIV_FILTER_BROWSER = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
python -m pytest -m e2e -q

# frontend unit tests (Node.js 24+)
node --test "tests/frontend/*.test.mjs"

# opt-in live arXiv tests (default: skipped)
$env:ARXIV_LIVE_TESTS = '1'; python -m pytest tests/live -q
```

Current state: **763 offline tests**, **11 browser E2E**, **14 frontend unit tests**, all
green. Coverage is tracked with `pytest-cov`; the offline suite fails if it ever tries to
open a socket.

## Building the Windows executable

```powershell
powershell -File packaging\build.ps1 -Mode All -Python <path-to-python3.12-x64.exe>
```

The script creates an isolated hash-locked build environment, runs the offline + browser
suites, freezes both an `onedir` and a `onefile` build with PyInstaller, runs a frozen
smoke test, and writes `SHA256SUMS.txt` plus `release-report.json`. It refuses to build on
a dirty tree unless you pass `-AllowDirty` (clearly marked as a QA build).

## Project layout

```text
src/arxiv_filter/
├── arxiv/          client, rate limiter, query lexer/builder, Atom parser, curl transport
├── api/            FastAPI routes, schemas, security headers
├── services/       search orchestration and job manager
├── storage/        SQLite database, cache / history / read repositories, migrations
├── static/         UI (vanilla ES modules, no build step) + vendored KaTeX
└── data/           category taxonomy snapshot (zh-CN)
docs/               verification records, decisions, release checklist, user guide
packaging/          PyInstaller specs and build tooling
scripts/            taxonomy maintenance, API contract probe, license manifest
tests/              unit · e2e (Playwright) · frontend (node:test) · live (opt-in)
```

## Data and privacy

Everything stays on this machine:

```text
%LOCALAPPDATA%\ArxivFilter\
├── config.json          preferences (proxy mode, open-marks-read)
├── arxiv-filter.db      cache, query history, read state (SQLite + WAL)
├── runtime.json         single-instance PID / port / challenge
├── backup/              database snapshots
├── exports/             files you exported
└── logs/                redacted diagnostics (query text is hashed on disk)
```

The only outbound traffic is the arXiv API query itself. No telemetry, no accounts.

## License

MIT — see [`LICENSE`](LICENSE).

Bundled third-party components (KaTeX, and the Python dependencies of the frozen build)
are listed with their licenses in `THIRD-PARTY-LICENSES.txt`, generated by
`scripts/write_third_party_licenses.py`.

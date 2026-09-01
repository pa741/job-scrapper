# CLAUDE.md

Guidance for Claude Code (or any assistant) working in this repository.

## Project overview

A small Python script that scrapes job postings via [JobSpy](https://github.com/speedyapply/JobSpy), optionally through proxies, and uploads the results as a CSV to Azure Blob Storage. This is published as a **public** GitHub repository, so secrets must never be hardcoded.

## Key files

- `scrape_jobs.py` — main script: loads config/secrets, calls `jobspy.scrape_jobs`, uploads a CSV to Azure Blob Storage.
- `config.yaml` — the `defaults:` block every search shares, plus a `searches:` list used **only as a fallback**. Since searches became per-user and configurable from the platform's web UI, what to look for comes from a published blob (below) and this file holds the operational settings of the machine doing the scraping: verbosity, LinkedIn description fetching, salary annualisation. Each search uploads its own blob, named after the search; that name is what the platform groups by, so duplicates are refused at load. The older single `search:` block is still accepted (the file is bind-mounted on the NAS and can lag the image), and logs a deprecation warning.
- `.env.example` — documents required environment variables with placeholder values. Real values go in a local, git-ignored `.env`.
- `requirements.txt` — plain pip dependencies (no lockfile/poetry by design, to keep the template simple). `python-jobspy` is pinned to a **tag of our own fork**, not PyPI — see below.

## The JobSpy fork

Upstream [speedyapply/JobSpy](https://github.com/speedyapply/JobSpy) stopped merging in
February 2026, so this repo depends on [pa741/JobSpy](https://github.com/pa741/JobSpy)
(branch `patches`) instead, installed from a GitHub archive tarball pinned to a tag.

**Anything about how a board is scraped or which boards exist belongs in the fork, not
here.** This repo only decides what to search for and where the CSV goes. To change the
library: commit on `patches`, tag it (`v<upstream-version>-fh<n>`), then bump the URL in
`requirements.txt` — the pin is what makes a build reproducible, so never point it at a
branch. A tarball rather than `git+https` is deliberate: `python:3.11-slim` has no `git`.

The fork currently adds `applicants`/`applicant_count` and `offsite_apply` (LinkedIn) and the `freehire` source, which also contributes `source_board`, `summary` and four posting-freshness columns. `offsite_apply` is three-state and its empty value means "not established" rather than "LinkedIn hosts it" - reading it the other way got a whole corpus wrong once; see the README.
Keep `main` tracking upstream so `patches` stays rebaseable.

## Where the searches come from

`load_published_searches` reads `searches.json` from the `scraper-config` container on the same
storage account results go to. The platform's web UI writes it: each user owns their searches,
and the blob is the whole enabled set across every user, rebuilt from scratch on every save.

**A blob rather than an API, and that is the point.** This machine has no managed identity, so
an API it had to authenticate against would need a client secret or a function key sitting here
— and the platform is built with no secret store at all. The scraper already holds a storage
credential and already talks to exactly one Azure service; reading the config this way keeps
both of those true. Anything that seems to need a second credential is the design being worked
around.

Precedence, and the three cases are deliberately not the same:

| | |
| --- | --- |
| blob absent | warn, fall back to `config.yaml`'s `searches:` — the normal state before the platform is used, and while the file and the image move independently |
| blob unreadable (malformed, wrong version, 403) | **raise.** A corrupt or unreachable published config is a bug, not a lag; falling back would scrape a stale set under those slugs and report success, every night, with nothing saying so |
| blob lists nothing | scrape nothing and exit clean. Somebody paused every search — falling back would scrape exactly what they turned off |

A published search's parameters are merged **over** `defaults:`, and the platform omits an
option nobody chose rather than sending null, precisely so that merge cannot blank one of this
machine's settings. The slug becomes the `Search.name`, so the blob naming contract is unchanged.

## Conventions

- **Secrets only via environment variables**, loaded with `python-dotenv`. Never hardcode connection strings, proxy credentials, or API keys — use `.env.example` placeholders and document the variable in the README. Read them in `scrape_jobs.py` and pass them into the library (as `HIREME_API_KEY` → `freehire_api_key` does); jobspy itself never touches `os.environ`, so there is one place to audit.
- **Two config surfaces, and they answer different questions.** *What to search for* comes from the platform, per user, through the published blob. *How this machine scrapes* lives in `config.yaml`'s `defaults:`. Don't add argparse/CLI flags or extra env vars for either — and in particular, don't "simplify" this back to config.yaml alone: the whole point of the change was that editing YAML on a NAS is not something a user of the platform can do.
- **One place writes a jobspy parameter name on the platform side too.** `ScraperConfigDocument.ToParams` in the job-platform repo is it. The web form is typed fields; nothing a browser sends is ever a keyword-argument name, because the ones it could reach include `proxies` and `freehire_api_key`.
- Keep the script dependency-light and single-file unless the user asks for more structure (e.g. multiple scrapers, scheduling, tests).
- This repo has no test suite configured. If adding one, keep it minimal and don't require real Azure/proxy credentials to run (mock or skip network calls).
- CI is a single workflow, `.github/workflows/docker-publish.yml`: on pushing a tag matching `vX.X`, it builds `Dockerfile` and pushes to GHCR as `<version>` and `latest`. It doesn't run the scraper — no Azure/proxy secrets are needed for it to pass. Keep `Dockerfile`/`.dockerignore` in sync with `requirements.txt`/file list if either changes.

## Running locally

Requires a local `.env` (copied from `.env.example`, not committed) with a real `AZURE_STORAGE_CONNECTION_STRING`. Without it, `scrape_jobs.py` raises a clear `RuntimeError` rather than failing silently or writing anywhere unexpected.

```bash
pip install -r requirements.txt
python scrape_jobs.py
```

## Public repo hygiene

Before committing or opening a PR, double-check no real secrets, proxy lists, or scraped PII ended up in tracked files — `.env`, `output/`, and virtualenvs are git-ignored, but review `git status`/diffs anyway.

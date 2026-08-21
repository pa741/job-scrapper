# CLAUDE.md

Guidance for Claude Code (or any assistant) working in this repository.

## Project overview

A small Python script that scrapes job postings via [JobSpy](https://github.com/speedyapply/JobSpy), optionally through proxies, and uploads the results as a CSV to Azure Blob Storage. This is published as a **public** GitHub repository, so secrets must never be hardcoded.

## Key files

- `scrape_jobs.py` — main script: loads config/secrets, calls `jobspy.scrape_jobs`, uploads a CSV to Azure Blob Storage.
- `config.yaml` — the searches to run: a `defaults:` block plus a `searches:` list, each entry overriding what it names. New search options belong here, not as CLI flags or env vars. Each search uploads its own blob, named after the search; that name is what the platform groups by, so duplicates are refused at load. The older single `search:` block is still accepted (the file is bind-mounted on the NAS and can lag the image), and logs a deprecation warning.
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

The fork currently adds `applicants`/`applicant_count` (LinkedIn) and the `freehire` source, which also contributes `source_board`, `summary` and four posting-freshness columns.
Keep `main` tracking upstream so `patches` stays rebaseable.

## Conventions

- **Secrets only via environment variables**, loaded with `python-dotenv`. Never hardcode connection strings, proxy credentials, or API keys — use `.env.example` placeholders and document the variable in the README. Read them in `scrape_jobs.py` and pass them into the library (as `HIREME_API_KEY` → `freehire_api_key` does); jobspy itself never touches `os.environ`, so there is one place to audit.
- **Search parameters only via `config.yaml`.** Don't add argparse/CLI flags or extra env vars for search behavior — keep one config surface.
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

# CLAUDE.md

Guidance for Claude Code (or any assistant) working in this repository.

## Project overview

A small Python script that scrapes job postings via [JobSpy](https://github.com/speedyapply/JobSpy), optionally through proxies, and uploads the results as a CSV to Azure Blob Storage. This is published as a **public** GitHub repository, so secrets must never be hardcoded.

## Key files

- `scrape_jobs.py` — main script: loads config/secrets, calls `jobspy.scrape_jobs`, uploads a CSV to Azure Blob Storage.
- `config.yaml` — non-secret search parameters (sites, search term, location, results count, etc.). New search options belong here, not as CLI flags or env vars.
- `.env.example` — documents required environment variables with placeholder values. Real values go in a local, git-ignored `.env`.
- `requirements.txt` — plain pip dependencies (no lockfile/poetry by design, to keep the template simple).

## Conventions

- **Secrets only via environment variables**, loaded with `python-dotenv`. Never hardcode connection strings, proxy credentials, or API keys — use `.env.example` placeholders and document the variable in the README.
- **Search parameters only via `config.yaml`.** Don't add argparse/CLI flags or extra env vars for search behavior — keep one config surface.
- Keep the script dependency-light and single-file unless the user asks for more structure (e.g. multiple scrapers, scheduling, tests).
- This repo has no CI/tests configured. If adding either, keep them minimal and don't require real Azure/proxy credentials to run (mock or skip network calls).

## Running locally

Requires a local `.env` (copied from `.env.example`, not committed) with a real `AZURE_STORAGE_CONNECTION_STRING`. Without it, `scrape_jobs.py` raises a clear `RuntimeError` rather than failing silently or writing anywhere unexpected.

```bash
pip install -r requirements.txt
python scrape_jobs.py
```

## Public repo hygiene

Before committing or opening a PR, double-check no real secrets, proxy lists, or scraped PII ended up in tracked files — `.env`, `output/`, and virtualenvs are git-ignored, but review `git status`/diffs anyway.

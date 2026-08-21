# job-scrapper

Scrapes job postings using [JobSpy](https://github.com/speedyapply/JobSpy) (Indeed, LinkedIn, ZipRecruiter, etc.), optionally routed through proxies, and uploads the results as a timestamped CSV to Azure Blob Storage.

It runs against [our own fork of JobSpy](https://github.com/pa741/JobSpy), pinned by tag in `requirements.txt`. Upstream stopped merging in February 2026; the fork adds LinkedIn applicant counts and [freehire.me](https://freehire.me) as a source. Changes to scraping behaviour belong there, not here.

## Prerequisites

- Python 3.10+
- An Azure Storage account (for storing results)
- (Recommended) A pool of proxies, to reduce the chance of being rate-limited or blocked by job sites

## Setup

1. Clone the repo and create a virtual environment:

   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in real values:

   ```bash
   cp .env.example .env
   ```

   | Variable | Description |
   | --- | --- |
   | `AZURE_STORAGE_CONNECTION_STRING` | Connection string for your Azure Storage account. Find it in the Azure Portal under **Storage Account → Access keys**, or run `az storage account show-connection-string --name <account> --resource-group <group>`. |
   | `AZURE_CONTAINER_NAME` | Blob container to upload results to. Must already exist; the scraper fails if it doesn't, so a typo can't strand uploads somewhere nothing reads. Defaults to `jobs-landing`. |
   | `HIREME_API_KEY` | **Optional.** A freehire.me personal API key, sent as a bearer token. freehire's job search is unauthenticated, so leaving this unset changes nothing about the results — a key only identifies the caller. |
   | `PROXIES` | Comma-separated list of proxies (`user:pass@host:port` or `host:port`). Leave blank to scrape without proxies. Not applied to freehire, which is a public API with nothing to route around. |

   `.env` is git-ignored — only the placeholder `.env.example` is committed.

3. Edit `config.yaml` to set your search parameters (job sites, search term, location, number of results, etc.). See the [JobSpy README](https://github.com/speedyapply/JobSpy) for the full list of supported options.

## Usage

```bash
python scrape_jobs.py
```

Each run scrapes jobs per `config.yaml`, writes them to an in-memory CSV, and uploads it to Azure Blob Storage as:

```
jobs/<search-term-slug>_<UTC timestamp>.csv
```

## Container image

A GitHub Actions workflow (`.github/workflows/docker-publish.yml`) builds and pushes a Docker image to GitHub Container Registry whenever a tag matching `vX.X` (e.g. `v1.0`, `v1.2.3`) is pushed. It publishes both `ghcr.io/<owner>/<repo>:<version>` (without the `v` prefix) and `:latest`.

```bash
git tag v1.0
git push origin v1.0
```

To run the published image locally, pass the same environment variables from `.env`:

```bash
docker run --rm --env-file .env ghcr.io/<owner>/<repo>:latest
```

Or with Docker Compose, using the example `docker-compose.yml`: edit the `image:` line to point at this repo's GHCR image, and fill in the `environment:` values (same variables as `.env.example`). `docker-compose.yml` is tracked in git, so **don't commit real secrets into it** — either keep your filled-in copy uncommitted/local-only, or use `docker-compose.override.yml` (git-ignored) for real values instead.

```bash
docker compose up
```

## Sources

`config.yaml` currently searches Indeed, LinkedIn and freehire. Google and Glassdoor are
both commented out with the reason inline — the short version is that JobSpy's scrapers for
them are broken upstream, not misconfigured here.

freehire is the odd one out: it is not scraped but read through a documented public API that
aggregates 227 ATS and job boards, covering IT/tech roles only. It needs no proxy and does
not consume proxy bandwidth, and each posting carries the employer's own application link in
`job_url_direct`. Glassdoor is omitted — see the comment in `config.yaml`.

Three CSV columns come from the fork:

| Column | Source | Meaning |
| --- | --- | --- |
| `applicants` | LinkedIn | The applicant caption verbatim, e.g. `Over 200 applicants`. Requires `linkedin_fetch_description: true`. |
| `applicant_count` | LinkedIn | The figure parsed out of it, e.g. `200`. |
| `source_board` | freehire | Which of freehire's crawled boards the posting came from, e.g. `greenhouse`. |
| `summary` | freehire | A 1–2 sentence synopsis of the posting. |
| `freshness_class` | freehire | `fresh`, `stale` or `likely-evergreen`. |
| `posting_age_days` | freehire | Age of the posting in days. |
| `repost_count` | freehire | How many times the role has been reposted. |
| `fake_freshness` | freehire | True when the stated posting date looks refreshed rather than real. |

freehire also fills `job_level`, `experience_range` and `company_num_employees`, which are
existing columns rather than new ones. The four freshness signals are populated on every
freehire posting and are the ones no scraped board can offer: a board repeats what a listing
says about itself, these say whether the role has been recycled or the date refreshed.

## Notes

- Job sites actively rate-limit and block scrapers; using proxies is strongly recommended for anything beyond light, occasional use.
- A board that fails is logged and skipped, not fatal — the run uploads whatever the other sites returned. If *every* site returns nothing the scraper raises instead of uploading, since an empty CSV would land downstream as a quiet market rather than a broken scraper.
- Never commit `.env` or real credentials/proxy lists — only `.env.example` with placeholder values should be tracked in git.
- No license file is included yet; add one (e.g. MIT) before publishing if you want to set usage terms explicitly.

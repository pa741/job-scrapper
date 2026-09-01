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
   | `AZURE_CONFIG_CONTAINER_NAME` | **Optional.** Container the platform publishes the searches to, on the same account. Defaults to `scraper-config`; only set it if the platform was deployed with a different container name. |
   | `PROXIES` | Comma-separated list of proxies (`user:pass@host:port` or `host:port`). Leave blank to scrape without proxies. Not applied to freehire, which is a public API with nothing to route around. |

   `.env` is git-ignored — only the placeholder `.env.example` is committed.

3. Set up your searches. **The normal way is the platform's web UI** — see "Where the searches
   come from" below. `config.yaml` holds the defaults every search shares whichever way they
   arrive, plus a `searches:` list used only when nothing has been published. See the
   [fork's README](https://github.com/pa741/JobSpy) for the full list of supported options.

   ```yaml
   defaults:
     site_name: [indeed, linkedin, freehire]
     location: "London, UK"
     results_wanted: 500

   searches:                          # fallback only
     - search_term: "software engineer"
     - name: python-remote            # optional; defaults to the slugified search_term
       search_term: "python developer"
       is_remote: true
   ```

## Where the searches come from

Searches belong to people. Each user of the platform configures their own from its **Searches**
page, and the platform writes the whole enabled set — everybody's — to `searches.json` in the
`scraper-config` container on the same storage account results go to. This scraper reads it at
the start of every run.

**A blob rather than an API call.** This machine has no managed identity, so an API would mean a
client secret or a function key living on the NAS, and the platform is built with no secret store
at all. The scraper already holds a storage credential and already talks to one Azure service.

What a published search says wins over `defaults:`; what it does not mention falls through to it.
So the settings about *this machine* — verbosity, whether LinkedIn descriptions are fetched,
whether salaries are annualised — stay here, and the search intent comes from the platform. The
platform omits an option nobody chose rather than sending null, so a default here cannot be
blanked by a form somebody left empty.

| the blob is… | what happens |
| --- | --- |
| not there | warn and run `config.yaml`'s `searches:` — the state before the platform is used |
| unreadable | **fail the run.** A corrupt or unreachable config is a bug rather than a lag, and quietly scraping a stale list instead would hide it every night |
| empty | scrape nothing, exit clean. Somebody paused every search; running the local list would scrape exactly what they turned off |

**Two people asking for the same thing are scraped twice, deliberately.** There is no coalescing
and no cap, so the length and cost of a run is the sum of every enabled search across every user.

## Usage

```bash
python scrape_jobs.py
```

Each search — published or local — is scraped **in turn** and uploaded as its own blob:

```
jobs/<search-name>_<UTC timestamp>.csv
```

The name is not just a filename — the platform reads it back out of the blob name to tell
the searches apart, so it is the axis the dashboard groups by. Two searches resolving to the
same name are refused at startup rather than silently merging into one.

Searches run sequentially, so a run costs the sum of them: JobSpy already scrapes the boards
within one search concurrently, and running searches in parallel would multiply the request
rate against the same boards. Each search uploads as soon as it finishes, so a later failure
cannot cost an earlier success, and one search failing does not stop the rest — though the
run still exits non-zero to say so.

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
| `offsite_apply` | LinkedIn | Whether the application is completed on the employer's own system rather than on LinkedIn. Requires `linkedin_fetch_description: true`. **Three-state**: empty means nothing was established, which is not the same as `False`. See below. |
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

### Why `offsite_apply` exists

LinkedIn used to publish the employer's apply URL on the guest job page, and the fork read it
into `job_url_direct`. **It stopped.** Measured on 2026-09-01 against a live corpus, all 4,470
LinkedIn postings of the previous week had no direct link, while the job detail page had been
fetched for 98.4% of them - so the scraper looked and there was nothing there. Checked directly:
`<code id="applyUrl">` is gone, every apply-redirect endpoint 404s, and a LinkedIn guest job page
now contains no non-LinkedIn URL anywhere on it. The URL is not obtainable without signing in.

What LinkedIn does still publish is *whether* the application is offsite, in two independent
places - the apply button's `offsite-apply` icon, and the sign-in modal's `apply-link-offsite`
impression id. `offsite_apply` reads both.

**The empty state is the important one.** A consumer that reads a missing value as "LinkedIn
hosts this application" gets the whole corpus wrong, which is exactly what happened. Empty means
the scraper did not establish it; `False` means LinkedIn hosts it - Easy Apply.

Indeed and freehire are unaffected: both still publish `job_url_direct`, and 92.6% of Indeed's
point at a genuine external ATS rather than back at a board.

## Notes

- Job sites actively rate-limit and block scrapers; using proxies is strongly recommended for anything beyond light, occasional use.
- A board that fails is logged and skipped, not fatal — the run uploads whatever the other sites returned. If *every* site returns nothing the scraper raises instead of uploading, since an empty CSV would land downstream as a quiet market rather than a broken scraper.
- Never commit `.env` or real credentials/proxy lists — only `.env.example` with placeholder values should be tracked in git.
- No license file is included yet; add one (e.g. MIT) before publishing if you want to set usage terms explicitly.

# job-scrapper

Scrapes job postings using [JobSpy](https://github.com/speedyapply/JobSpy) (Indeed, LinkedIn, ZipRecruiter, etc.), optionally routed through proxies, and uploads the results as a timestamped CSV to Azure Blob Storage.

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
   | `AZURE_CONTAINER_NAME` | Blob container to upload results to. Created automatically if it doesn't exist. Defaults to `jobs`. |
   | `PROXIES` | Comma-separated list of proxies (`user:pass@host:port` or `host:port`). Leave blank to scrape without proxies. |

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

## Notes

- Job sites actively rate-limit and block scrapers; using proxies is strongly recommended for anything beyond light, occasional use.
- Never commit `.env` or real credentials/proxy lists — only `.env.example` with placeholder values should be tracked in git.
- No license file is included yet; add one (e.g. MIT) before publishing if you want to set usage terms explicitly.

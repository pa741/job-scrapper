"""Scrape job postings with JobSpy (optionally through proxies) and upload
the results as a CSV blob to Azure Blob Storage.

Configuration:
    - Search parameters (sites, search term, location, etc.) live in config.yaml.
    - Secrets (Azure connection string, proxies) are read from environment
      variables / a local .env file. See .env.example for the expected keys.
"""

import io
import logging
import os
import re
from datetime import datetime, timezone

import pandas as pd
import yaml
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from jobspy import scrape_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


def load_search_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config["search"]


def load_proxies() -> list[str] | None:
    raw = os.environ.get("PROXIES", "").strip()
    if not raw:
        return None
    proxies = [p.strip() for p in raw.split(",") if p.strip()]
    return proxies or None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "jobs"


def deduplicate_jobs(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate postings: exact repeats (same job_url) and the same
    job cross-posted under different URLs on multiple sites (matched by
    normalized title/company/location)."""
    before = len(df)

    if "job_url" in df.columns:
        df = df.drop_duplicates(subset="job_url")

    key_cols = [c for c in ("title", "company", "location") if c in df.columns]
    if key_cols:
        normalized = df[key_cols].apply(lambda s: s.astype(str).str.strip().str.lower())
        df = df[~normalized.duplicated()]

    removed = before - len(df)
    if removed:
        logger.info("Removed %d duplicate job listing(s); %d unique remain.", removed, len(df))
    return df.reset_index(drop=True)


def upload_csv_to_blob(csv_bytes: bytes, blob_name: str) -> str:
    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not connection_string or connection_string.startswith("<"):
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING is not set. Copy .env.example to .env "
            "and fill in a real Azure Storage connection string."
        )
    container_name = os.environ.get("AZURE_CONTAINER_NAME", "jobs")

    service_client = BlobServiceClient.from_connection_string(connection_string)
    container_client = service_client.get_container_client(container_name)
    if not container_client.exists():
        logger.info("Container '%s' does not exist, creating it.", container_name)
        container_client.create_container()

    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(csv_bytes, overwrite=True)
    return blob_client.url


def main() -> None:
    load_dotenv()

    search_config = load_search_config()
    proxies = load_proxies()
    logger.info(
        "Loaded search config for sites=%s search_term=%r location=%r (proxies=%d)",
        search_config.get("site_name"),
        search_config.get("search_term"),
        search_config.get("location"),
        len(proxies) if proxies else 0,
    )

    jobs_df = scrape_jobs(proxies=proxies, **search_config)
    logger.info("Scraped %d job postings.", len(jobs_df))

    jobs_df = deduplicate_jobs(jobs_df)

    csv_buffer = io.StringIO()
    jobs_df.to_csv(csv_buffer, index=False)
    csv_bytes = csv_buffer.getvalue().encode("utf-8")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    search_term_slug = slugify(str(search_config.get("search_term", "jobs")))
    blob_name = f"jobs/{search_term_slug}_{timestamp}.csv"

    blob_url = upload_csv_to_blob(csv_bytes, blob_name)
    logger.info("Uploaded results to %s", blob_url)


if __name__ == "__main__":
    main()

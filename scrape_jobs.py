"""Scrape job postings with JobSpy (optionally through proxies) and upload
the results as CSV blobs to Azure Blob Storage.

Configuration:
    - Searches (sites, search terms, locations, etc.) live in config.yaml.
    - Secrets (Azure connection string, proxies) are read from environment
      variables / a local .env file. See .env.example for the expected keys.

Each configured search is scraped separately and uploaded as its own blob. The
blob name carries the search's name, which is how the ingestion side recovers
which search a file belongs to - so a search's name is not cosmetic.
"""

import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import yaml
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from jobspy import scrape_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


@dataclass(frozen=True)
class Search:
    """One configured search: a name, and the arguments jobspy is called with."""

    name: str
    params: dict

    @property
    def blob_prefix(self) -> str:
        return f"jobs/{self.name}"


def load_searches(path: str = CONFIG_PATH) -> list[Search]:
    """
    Reads config.yaml into the list of searches to run.

    `defaults` holds whatever every search shares; each entry under `searches`
    overrides the keys it names. Both accept any scrape_jobs() parameter, so a
    search can carry its own location or hours_old without restating the rest.
    """
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    defaults = config.get("defaults") or {}
    entries = config.get("searches")

    if entries is None:
        # The single-search shape this file used to have. Still accepted because
        # config.yaml is bind-mounted into the container: the image and the file
        # are updated independently, and refusing the old shape would break a
        # scheduled run whose config had not been copied across yet.
        legacy = config.get("search")
        if legacy is None:
            raise RuntimeError(
                f"{path} defines neither 'searches:' nor 'search:'. See the "
                "committed config.yaml for the expected shape."
            )
        logger.warning(
            "config.yaml uses the single 'search:' block; prefer 'defaults:' plus "
            "a 'searches:' list, which can hold more than one search."
        )
        entries = [legacy]

    if not entries:
        raise RuntimeError(f"{path} has an empty 'searches:' list; nothing to scrape.")

    searches: list[Search] = []
    for entry in entries:
        params = {**defaults, **(entry or {})}
        name = params.pop("name", None) or slugify(str(params.get("search_term", "jobs")))
        searches.append(Search(name=name, params=params))

    names = [s.name for s in searches]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        # Two searches sharing a name write to the same blob prefix, and the
        # ingestion side reads the name back out of the blob name - so they would
        # silently merge into one search term downstream rather than fail. Easy to
        # hit with the same search_term in two locations; give each a `name`.
        raise RuntimeError(
            f"Duplicate search name(s) in {path}: {', '.join(duplicates)}. "
            "Each search needs a distinct 'name', since it becomes the blob name "
            "and the search term the platform groups by."
        )

    return searches


def load_proxies() -> list[str] | None:
    raw = os.environ.get("PROXIES", "").strip()
    if not raw:
        return None
    proxies = [p.strip() for p in raw.split(",") if p.strip()]
    return proxies or None


def load_freehire_api_key() -> str | None:
    """freehire's search API is unauthenticated, so an absent key is not an
    error - it only identifies the caller. Read here rather than in jobspy so
    every secret in this project enters through one place."""
    key = os.environ.get("HIREME_API_KEY", "").strip()
    if not key or key.startswith("<"):
        return None
    return key


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
    container_name = os.environ.get("AZURE_CONTAINER_NAME", "jobs-landing")

    service_client = BlobServiceClient.from_connection_string(connection_string)
    container_client = service_client.get_container_client(container_name)
    if not container_client.exists():
        # Deliberately not created here. Auto-creating turns a mistyped
        # AZURE_CONTAINER_NAME into a successful upload nobody consumes: the wrong
        # container springs into existence, the run logs clean, and whatever watches
        # the real container never fires. Failing is the only way that surfaces.
        raise RuntimeError(
            f"Container '{container_name}' does not exist in this storage account. "
            "Check AZURE_CONTAINER_NAME - it is a container name, not the storage "
            "account name - or create the container before running."
        )

    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(csv_bytes, overwrite=True)
    return blob_client.url


def run_search(search: Search, proxies: list[str] | None, freehire_api_key: str | None) -> bool:
    """
    Scrapes one search and uploads it. Returns whether anything was uploaded.

    Uploading here rather than after every search has run means a failure in a
    later search cannot cost the ones that already succeeded.
    """
    logger.info(
        "[%s] scraping sites=%s search_term=%r location=%r",
        search.name,
        search.params.get("site_name"),
        search.params.get("search_term"),
        search.params.get("location"),
    )

    jobs_df = scrape_jobs(
        proxies=proxies, freehire_api_key=freehire_api_key, **search.params
    )
    logger.info("[%s] scraped %d job postings.", search.name, len(jobs_df))

    jobs_df = deduplicate_jobs(jobs_df)

    if jobs_df.empty:
        # A single board failing is survivable and jobspy logs it and carries on,
        # but every board failing is not something to upload. An empty CSV lands as
        # a successful run holding no postings, which reads downstream as the market
        # going quiet rather than the scraper being broken.
        logger.error(
            "[%s] every site returned zero postings; see the per-site errors above. "
            "Not uploading an empty CSV.",
            search.name,
        )
        return False

    csv_buffer = io.StringIO()
    jobs_df.to_csv(csv_buffer, index=False)
    csv_bytes = csv_buffer.getvalue().encode("utf-8")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    blob_url = upload_csv_to_blob(csv_bytes, f"{search.blob_prefix}_{timestamp}.csv")
    logger.info("[%s] uploaded results to %s", search.name, blob_url)
    return True


def main() -> None:
    load_dotenv()

    searches = load_searches()
    proxies = load_proxies()
    freehire_api_key = load_freehire_api_key()
    logger.info(
        "Loaded %d search(es): %s (proxies=%d, freehire key=%s)",
        len(searches),
        ", ".join(s.name for s in searches),
        len(proxies) if proxies else 0,
        "set" if freehire_api_key else "unset",
    )

    uploaded: list[str] = []
    failed: list[str] = []

    # Sequential on purpose. jobspy already scrapes the boards within one search
    # concurrently, so running searches in parallel would multiply the request rate
    # against the same boards - and LinkedIn already rate-limits around the tenth
    # page from a single IP.
    for search in searches:
        try:
            if run_search(search, proxies, freehire_api_key):
                uploaded.append(search.name)
            else:
                failed.append(search.name)
        except Exception:
            # One search failing must not end the run, for the same reason one board
            # failing must not: the others have work to do, or have already done it.
            logger.exception("[%s] failed; continuing with the remaining searches", search.name)
            failed.append(search.name)

    logger.info(
        "Finished: %d of %d search(es) uploaded.", len(uploaded), len(searches)
    )

    if failed:
        # Whatever succeeded is already in the container, so raising costs nothing
        # and is the only thing that surfaces a partial failure on a scheduled run.
        raise RuntimeError(
            f"{len(failed)} of {len(searches)} search(es) produced nothing: "
            f"{', '.join(failed)}. See the errors above."
        )


if __name__ == "__main__":
    main()

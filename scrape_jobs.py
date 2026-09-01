"""Scrape job postings with JobSpy (optionally through proxies) and upload
the results as CSV blobs to Azure Blob Storage.

Configuration:
    - Which searches to run comes from the platform, as a JSON blob the web UI
      publishes (see load_published_searches). config.yaml is the fallback for
      when that blob is not there yet, and holds the operational defaults every
      search shares either way.
    - Secrets (Azure connection string, proxies) are read from environment
      variables / a local .env file. See .env.example for the expected keys.

Each configured search is scraped separately and uploaded as its own blob. The
blob name carries the search's name, which is how the ingestion side recovers
which search a file belongs to - so a search's name is not cosmetic.
"""

import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import yaml
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from jobspy import scrape_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

# Where the platform publishes what to scrape. A container of its own, separate from the one
# results are uploaded to: the platform's ingest watches that one for new CSVs, so a config
# write landing there would be picked up as a scrape to ingest.
CONFIG_CONTAINER_DEFAULT = "scraper-config"
CONFIG_BLOB_NAME = "searches.json"

# The document shape this build understands. The platform bumps it only when an existing key
# changes meaning, so refusing an unknown one is refusing something we would misread.
CONFIG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Search:
    """One configured search: a name, and the arguments jobspy is called with."""

    name: str
    params: dict

    @property
    def blob_prefix(self) -> str:
        return f"jobs/{self.name}"


def load_defaults(path: str = CONFIG_PATH) -> dict:
    """
    The `defaults:` block: what every search shares, whatever chose the searches.

    Deliberately still a local file rather than something the platform sends.
    These are operational settings about how this machine scrapes - verbosity,
    whether to fetch LinkedIn descriptions, whether to annualise salaries - and
    they belong with the machine doing the work. The platform decides *what* to
    look for; anything a published search names wins over what is here.
    """
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    return config.get("defaults") or {}


def build_searches(entries: list[dict], defaults: dict, source: str) -> list[Search]:
    """
    Merges each entry over `defaults` and refuses two searches with one name.

    Shared by both config sources so the merge order, the name derivation and
    the duplicate check cannot drift between them - a published search and a
    local one have to become the same thing, or the same name would produce two
    different blobs depending on which path ran.
    """
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
            f"Duplicate search name(s) in {source}: {', '.join(duplicates)}. "
            "Each search needs a distinct 'name', since it becomes the blob name "
            "and the search term the platform groups by."
        )

    return searches


def load_searches(path: str = CONFIG_PATH) -> list[Search]:
    """
    Reads config.yaml into the list of searches to run.

    The fallback path, used when the platform has published nothing. Each entry
    under `searches` overrides the keys it names from `defaults`. Both accept any
    scrape_jobs() parameter, so a search can carry its own location or hours_old
    without restating the rest.
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

    return build_searches(entries, defaults, source=path)


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


def blob_service_client() -> BlobServiceClient:
    """
    The one storage client, for both the config read and the result upload.

    Read here rather than at each call site so there is a single place the
    credential enters, which is the same rule the freehire key follows.
    """
    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not connection_string or connection_string.startswith("<"):
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING is not set. Copy .env.example to .env "
            "and fill in a real Azure Storage connection string."
        )

    return BlobServiceClient.from_connection_string(connection_string)


def load_published_searches(defaults: dict) -> list[Search] | None:
    """
    The searches the platform published, or None if it has published none.

    Read from a blob rather than fetched from an API, and that is deliberate on
    both sides. This machine has no managed identity, so an API would need a
    client secret or a function key living here - and the platform is built with
    no secret store at all. It already holds a storage credential and already
    talks to exactly one Azure service; this keeps both of those true.

    Each published search carries its parameters fully resolved, merged over the
    local `defaults:` block so the operational settings on this machine still
    apply. The platform never sends a null for an option nobody chose, precisely
    so that merge cannot blank one of those defaults.

    Three outcomes, and the difference between them matters:

    - **The blob is not there** - the normal state before the platform has been
      used, and while the file and the image move independently. Warn and fall
      back to config.yaml, the way the legacy single `search:` block is already
      tolerated; a scheduled run should not be lost to a lag.
    - **The blob is there and unreadable** - malformed, or a version this build
      does not understand, or a credential that cannot see the container. Raise.
      Falling back would scrape a stale set under those slugs and report success,
      and the failure would recur every night with nothing saying so.
    - **The blob is there and lists nothing** - somebody has paused every search.
      An empty list, which the caller honours rather than overriding: falling
      back here would scrape exactly what was turned off.
    """
    container_name = (
        os.environ.get("AZURE_CONFIG_CONTAINER_NAME", "").strip() or CONFIG_CONTAINER_DEFAULT
    )

    blob_client = (
        blob_service_client()
        .get_container_client(container_name)
        .get_blob_client(CONFIG_BLOB_NAME)
    )

    try:
        raw = blob_client.download_blob().readall()
    except ResourceNotFoundError:
        logger.warning(
            "No published configuration at %s/%s; falling back to the searches in %s. "
            "Add one from the platform's Searches page to take over from this file.",
            container_name,
            CONFIG_BLOB_NAME,
            CONFIG_PATH,
        )
        return None
    except AzureError as exc:
        # Anything that is not "it is not there" - a 403 above all, which is what a credential
        # scoped to the results container alone produces. Not a fallback: this would recur on
        # every scheduled run, and scraping the local list meanwhile would report success while
        # ignoring everything anybody configured.
        raise RuntimeError(
            f"Could not read {container_name}/{CONFIG_BLOB_NAME} ({exc}). The credential in "
            "AZURE_STORAGE_CONNECTION_STRING needs read access to that container as well as "
            f"write access to {os.environ.get('AZURE_CONTAINER_NAME', 'jobs-landing')}. Check "
            "AZURE_CONFIG_CONTAINER_NAME names a container that exists on this account."
        ) from exc

    try:
        document = json.loads(raw)
        version = int(document["version"])
        entries = document["searches"]
        published_at = document.get("publishedUtc", "an unknown time")
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"{container_name}/{CONFIG_BLOB_NAME} is not a configuration this scraper can "
            f"read ({exc}). Not falling back to {CONFIG_PATH}: a corrupt published config is "
            "a bug rather than a lag, and scraping the local list instead would hide it."
        ) from exc

    if version != CONFIG_SCHEMA_VERSION:
        raise RuntimeError(
            f"{container_name}/{CONFIG_BLOB_NAME} is version {version}; this scraper reads "
            f"version {CONFIG_SCHEMA_VERSION}. Update the image rather than editing the blob."
        )

    logger.info(
        "Loaded %d search(es) published at %s from %s/%s.",
        len(entries),
        published_at,
        container_name,
        CONFIG_BLOB_NAME,
    )

    return build_searches(
        [{"name": entry["slug"], **entry.get("params", {})} for entry in entries],
        defaults,
        source=f"{container_name}/{CONFIG_BLOB_NAME}",
    )


def upload_csv_to_blob(csv_bytes: bytes, blob_name: str) -> str:
    container_name = os.environ.get("AZURE_CONTAINER_NAME", "jobs-landing")

    service_client = blob_service_client()
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

    # The platform first, this file second. Owning a search is a thing people do from the web
    # UI now; config.yaml keeps the operational defaults and stands in until something has been
    # published, so a machine that has never seen the platform still runs.
    defaults = load_defaults()
    searches = load_published_searches(defaults)

    if searches is None:
        searches = load_searches()
    elif not searches:
        # Every search paused, which is a decision rather than an absence. Falling back here
        # would scrape exactly what somebody turned off, so this exits clean and says why.
        logger.info(
            "The published configuration lists no enabled searches; nothing to scrape. "
            "Enable one from the platform's Searches page."
        )
        return

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

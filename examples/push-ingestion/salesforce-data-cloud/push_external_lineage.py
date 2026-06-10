#!/usr/bin/env python3
"""
Data 360 External Source → DLO Lineage Push

Pushes CRM→DLO and Snowflake→DLO lineage edges into Monte Carlo using the
Push Ingest API cross-warehouse lineage feature so that external source tables
appear as trusted lineage upstream of Data 360 Data Lake Objects.

Only sources that already exist as first-class objects in a Monte Carlo
warehouse catalog (CRM tables, Snowflake tables) are linked. Sources that
require custom node creation (e.g. S3 files) are out of scope.

Pipeline:
  1. Authenticate with Salesforce (platform OAuth — org-wide, same token for all data spaces)
  2. Fetch all Data Streams from the Salesforce Connect REST API
  3. Fetch MC catalog tables for Data Cloud, CRM, and/or Snowflake warehouses
  4. Match each stream's source table and DLO to MC catalog entries
  5. Push lineage edges via Ingest API cross-warehouse lineage

Usage:
  python3 push_external_lineage.py --dry-run
  python3 push_external_lineage.py
  python3 push_external_lineage.py --discover   # scan DSO connector types, no MC push

Required env vars (see .env.example):
  SF_ORG_URL, SF_CLIENT_ID, SF_CLIENT_SECRET
  MCD_INGEST_ID, MCD_INGEST_TOKEN — Ingestion key for the push (must be authorized for ALL
                                    referenced warehouses: Data Cloud + CRM and/or Snowflake)
  MCD_ID, MCD_TOKEN               — Personal API key for catalog lookup and warehouse discovery

Warehouse UUIDs are auto-discovered from Monte Carlo using the Salesforce org domain and
Snowflake account identifiers. No manual UUID configuration is needed for most deployments.

Optional env var overrides (rarely needed):
  MCD_DC_WAREHOUSE_UUID / MCD_RESOURCE_UUID — Override auto-discovered Data Cloud warehouse UUID
  MCD_CRM_WAREHOUSE_UUID         — Override auto-discovered CRM warehouse UUID
  MCD_CRM_WAREHOUSE_MAP          — Multi-org CRM map: "connector_id1=uuid1,connector_id2=uuid2"
                                   Only needed when multiple CRM orgs feed the same Data Cloud org.
  MCD_SNOWFLAKE_WAREHOUSE_UUID   — Override auto-discovered Snowflake warehouse UUID
  MCD_SNOWFLAKE_WAREHOUSE_MAP    — Multi-warehouse map: "account_id=uuid,account_id2=uuid2"
                                   Only needed when multiple Snowflake accounts are in use and
                                   auto-discovery picks the wrong one.
  LOG_LEVEL                      — DEBUG/INFO/WARNING/ERROR (default: INFO)
  LOG_FORMAT                     — json for structured output (default: plain)
"""
import argparse
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid as uuid_module
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

try:
    from pycarlo.core import Client, Session
    from pycarlo.features.ingestion import IngestionService
    from pycarlo.features.ingestion.models import LineageAssetRef, LineageEvent
except ImportError as err:
    import sys as _sys
    _sys.exit(
        f"ERROR: pycarlo is not installed. Run: pip install pycarlo>=0.12.478\n  ({err})"
    )

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ── Constants ─────────────────────────────────────────────────────────────────
SF_API_VERSION = "62.0"
MC_GRAPHQL_URL = "https://api.getmontecarlo.com/graphql"
MC_INGEST_URL  = "https://integrations.getmontecarlo.com"

_CONNECTOR_SFDC = "SalesforceDotCom"
_CONNECTOR_SNOWFLAKE = "SNOWFLAKE"
_SUPPORTED_CONNECTORS = {_CONNECTOR_SFDC, _CONNECTOR_SNOWFLAKE}

# Safety ceiling for pagination loops — prevents runaway fetches from a buggy/malicious API
_MAX_PAGES = 500

# Maximum response body size allowed before JSON parsing — guards against memory exhaustion
# from a malicious or misconfigured server sending a very large response body.
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB

# Warn if getWarehouses returns more than this many entries — suggests the account may have
# more connections than a single unparameterised query can return.
_MAX_WAREHOUSES_DISCOVERY = 200

# Batch size for SOQL IN clauses — keeps each query well under URL and record-count limits.
# Salesforce Tooling API returns at most 2000 records per response; batching at 200 keeps
# each batch comfortably under that limit and avoids URL length errors at scale.
_TOOLING_SOQL_BATCH_SIZE = 200

# Maximum per-retry sleep for 429 rate-limit responses
_RETRY_AFTER_MAX = 30.0

# Salesforce DeveloperName / API Name: alphanumeric + underscore, 1-255 chars.
# Used to validate names before embedding in SOQL IN clauses.
_SF_DEVELOPER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,255}$")

# Salesforce record ID: 15 or 18 alphanumeric characters.
# Validated before embedding in Tooling API URL paths.
_SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15,18}$")

# Snowflake account URL: must be exactly https://<account>.snowflakecomputing.com.
# Rejects subdomain-bypass attempts (e.g. foo.snowflakecomputing.com.evil.snowflakecomputing.com).
_SNOWFLAKE_ACCT_URL_RE = re.compile(
    r"^https://[a-z0-9][a-z0-9\-\.]*\.snowflakecomputing\.com(?::\d+)?/?$",
    re.IGNORECASE,
)

# Log-injection hardening: BIDI override characters and ANSI escape sequences.
_BIDI_OVERRIDES = frozenset([
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u200e", "\u200f", "\u2066", "\u2067", "\u2068", "\u2069",
])
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Param names that may contain credentials — redacted before writing to disk.
_SENSITIVE_PARAM_NAMES = frozenset({
    "password", "secret", "token", "key", "apikey", "api_key",
    "clientsecret", "client_secret", "accesskey", "access_key",
    "secretaccesskey", "secret_access_key", "privatekey", "private_key",
    "sessiontoken", "session_token",
    "passphrase",
    "pkcs8privatekey", "pkcs8_private_key",
    "sshprivatekey", "ssh_private_key",
    "bearertoken", "bearer_token",
    "oauthtoken", "oauth_token",
})

# MC resource_type values for cross-warehouse lineage push
_CRM_RESOURCE_TYPE       = "salesforce-crm"
_SNOWFLAKE_RESOURCE_TYPE = "snowflake"
_DC_RESOURCE_TYPE        = "salesforce-data-cloud"


# ── Logging ───────────────────────────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict = {
            "ts":     datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":  record.levelname,
            "run_id": getattr(record, "run_id", ""),
            "msg":    record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data)


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id  # type: ignore[attr-defined]
        return True


def _setup_logging(run_id: str) -> logging.Logger:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    _valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    if level_name not in _valid_levels:
        print(
            f"WARNING: Unknown LOG_LEVEL={level_name!r}, using INFO. "
            f"Valid values: {', '.join(_valid_levels)}",
            file=sys.stderr,
        )
        level_name = "INFO"
    level = getattr(logging, level_name, logging.INFO)

    use_json = os.environ.get("LOG_FORMAT", "").lower() == "json"
    plain_fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] [run=%(run_id)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    json_fmt = _JsonFormatter()

    # stderr handler
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.addFilter(_RunIdFilter(run_id))
    stderr_handler.setFormatter(json_fmt if use_json else plain_fmt)

    # file handler — always plain text regardless of LOG_FORMAT so it's human-readable
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(__file__).parent / f"run_{run_id}_{ts}.log"
    # Create the log file with 0o600 permissions (owner read/write only).
    # os.umask(0o177) masks off group/other bits so FileHandler creates it at 0o600.
    _old_umask = os.umask(0o177)
    try:
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    finally:
        os.umask(_old_umask)
    file_handler.addFilter(_RunIdFilter(run_id))
    file_handler.setFormatter(plain_fmt)

    logger = logging.getLogger("push_external_lineage")
    logger.setLevel(level)
    logger.handlers = [stderr_handler, file_handler]
    logger.propagate = False

    # Announce the log file path on stderr before any other output
    print(f"[run={run_id}] Log file: {log_path.resolve()}", file=sys.stderr)
    return logger


log: logging.Logger = logging.getLogger("push_external_lineage")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error(
            "Required environment variable '%s' is not set. "
            "Set it in your .env file (see .env.example for the expected format).",
            name,
        )
        sys.exit(1)
    return val


def _sanitize_for_log(value: str, max_len: int = 300) -> str:
    """Strip control/injection characters from API strings before embedding in logs."""
    out = value.replace("\n", " ").replace("\r", " ").replace("\t", " ").replace("\x00", "")
    out = out.replace("\u2028", " ").replace("\u2029", " ")  # Unicode line/paragraph separators
    out = _ANSI_ESCAPE_RE.sub("", out)
    out = "".join(" " if c in _BIDI_OVERRIDES else c for c in out)
    return out[:max_len]


def _url_for_log(url: str) -> str:
    """Strip query string from a URL before logging — keeps SOQL queries and cursors out of logs."""
    return urlparse(url)._replace(query="").geturl()


class MCGraphQLError(RuntimeError):
    """Raised for application-level GraphQL errors (error array in response body).

    These are permanent failures that must NOT be retried — the server understood the
    request and rejected it. Transport errors (network, 5xx) use RuntimeError/RequestException.
    """


def _redact_params(params: dict) -> dict:
    """Return a copy of params with sensitive values replaced by '<redacted>'."""
    redacted: dict = {}
    for k, v in params.items():
        redacted[k] = "<redacted>" if k.lower().replace("-", "_") in _SENSITIVE_PARAM_NAMES else v
    return redacted


def _sanitize_stream_for_disk(stream: dict) -> dict:
    """Return a stream dict with credential-bearing nested dicts redacted.

    Covers connectorInfo.connectorDetails (nested and top-level) and advancedAttributes,
    which may contain Snowflake passwords, S3 keys, or other secrets embedded in the
    Salesforce API response. Non-dict values are replaced with {} rather than crashing.
    """
    result = dict(stream)
    connector_info = dict(result.get("connectorInfo") or {})
    if "connectorDetails" in connector_info:
        cd = connector_info.get("connectorDetails")
        connector_info["connectorDetails"] = _redact_params(cd if isinstance(cd, dict) else {})
    result["connectorInfo"] = connector_info
    if "connectorDetails" in result:
        cd = result.get("connectorDetails")
        result["connectorDetails"] = _redact_params(cd if isinstance(cd, dict) else {})
    if "advancedAttributes" in result:
        aa = result.get("advancedAttributes")
        result["advancedAttributes"] = _redact_params(aa if isinstance(aa, dict) else {})
    return result


def _validate_uuid(name: str, value: str) -> None:
    """Validate that value is a well-formed UUID; exit with a clear error if not."""
    try:
        uuid_module.UUID(value)
    except ValueError:
        log.error(
            "Environment variable '%s' is not a valid UUID (got: %r). "
            "Find the correct UUID in Monte Carlo: Settings → Integrations → your connection.",
            name, value,
        )
        sys.exit(1)


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        val = int(raw)
        if val < 1:
            raise ValueError("must be >= 1")
        return val
    except ValueError as exc:
        sys.exit(f"Invalid {name}={raw!r}: {exc}")


def _extract_sf_account_identifier(account_url: str) -> str:
    """Extract lowercased account identifier from a Snowflake accountUrl.

    'https://HDB68299.us-west-2.snowflakecomputing.com' → 'hdb68299.us-west-2'
    'https://xy12345.snowflakecomputing.com'            → 'xy12345'
    """
    parsed = urlparse(account_url)
    hostname = (parsed.hostname or "").lower()
    suffix = ".snowflakecomputing.com"
    if hostname.endswith(suffix):
        return hostname[: -len(suffix)]
    return hostname


def _parse_warehouse_map(raw: str) -> dict:
    """Parse a warehouse map env var into {key.lower(): uuid}.

    Format: 'key1=uuid1,key2=uuid2'
    Logs a warning for any entry that cannot be parsed (missing '=', empty key, or empty uuid).
    """
    if len(raw) > 8192:
        # Truncate at the last complete comma boundary to avoid a split key=uuid entry
        raw = raw[:8192]
        last_comma = raw.rfind(",")
        if last_comma != -1:
            raw = raw[:last_comma]
        log.warning(
            "Warehouse map env var is unexpectedly long — truncated to last complete entry"
        )
    result: dict = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            log.warning("Warehouse map entry %r is malformed (expected key=uuid) — skipped", pair)
            continue
        key, uuid_val = pair.split("=", 1)
        key = key.strip().lower()
        uuid_val = uuid_val.strip()
        if not key or not uuid_val:
            log.warning(
                "Warehouse map entry %r has empty key or UUID after parsing — skipped", pair
            )
            continue
        result[key] = uuid_val
    return result


def _parse_full_table_id(fid: str) -> tuple:
    """Parse fullTableId 'database:schema.name' → (database, schema, name)."""
    db, _, rest = fid.partition(":")
    if not rest:
        rest, db = db, ""
    schema, _, name = rest.rpartition(".")
    return db, schema, name


def _http(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    **kwargs,
) -> requests.Response:
    """HTTP with exponential backoff, 429 rate-limit handling, and 5xx retry.

    Defaults allow_redirects=False to prevent redirect-based SSRF — callers that need
    redirects must explicitly pass allow_redirects=True.
    """
    kwargs.setdefault("allow_redirects", False)
    kwargs.setdefault("timeout", 30)  # safety net — all callers should set explicitly
    _log_url = _url_for_log(url)  # strip query string before any log calls

    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            if attempt >= max_retries:
                raise
            wait = backoff_base * (2 ** attempt)
            log.warning(
                "HTTP %s %s failed (attempt %d/%d): %s — retrying in %.1fs",
                method, _log_url, attempt + 1, max_retries + 1, exc, wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            if attempt >= max_retries:
                resp.raise_for_status()
            try:
                retry_after = float(resp.headers.get("Retry-After", 0))
                wait = min(retry_after, _RETRY_AFTER_MAX) if retry_after > 0 else backoff_base * (2 ** attempt)
            except (ValueError, TypeError):
                wait = backoff_base * (2 ** attempt)
            log.warning(
                "Rate limited (429) on %s — waiting %.1fs (retry %d/%d)",
                _log_url, wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and attempt < max_retries:
            wait = backoff_base * (2 ** attempt)
            log.warning(
                "Server error %d on %s (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, _log_url, attempt + 1, max_retries + 1, wait,
            )
            time.sleep(wait)
            continue

        # Raise explicitly on unexpected redirects — prevents silent 3xx pass-through
        # when allow_redirects=False is in effect.
        if 300 <= resp.status_code < 400:
            location = _sanitize_for_log(resp.headers.get("Location", "?"))
            raise requests.exceptions.TooManyRedirects(
                f"Redirect ({resp.status_code}) blocked on {_log_url} — "
                f"allow_redirects is False. Location: {location!r}"
            )

        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise RuntimeError(
                f"Response body from {_log_url} is too large "
                f"({len(resp.content):,} bytes > {_MAX_RESPONSE_BYTES:,} byte limit). "
                "Refusing to parse."
            )

        return resp

    raise RuntimeError("_http retry loop exhausted unexpectedly")


# ── GraphQL helper ────────────────────────────────────────────────────────────
def _gql(query: str, key_id: str, key_secret: str, variables: Optional[dict] = None) -> dict:
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    resp = _http(
        "POST",
        MC_GRAPHQL_URL,
        json=payload,
        headers={
            "x-mcd-id":     key_id,
            "x-mcd-token":  key_secret,
            "Content-Type": "application/json",
        },
        timeout=30,
        verify=True,
        max_retries=0,  # retry responsibility delegated to tenacity at the push layer
    )
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        log.debug("MC GraphQL non-JSON response body: %s", _sanitize_for_log(resp.text, max_len=500))
        raise RuntimeError(
            f"MC GraphQL returned non-JSON response (status={resp.status_code}). "
            "Enable DEBUG logging for the response body."
        ) from exc
    if "errors" in body:
        msgs = [_sanitize_for_log(e.get("message", str(e))) for e in body["errors"]]
        hint = (
            " (warehouse UUID may be wrong or the table doesn't exist in MC — "
            "verify MCD_DC_WAREHOUSE_UUID/MCD_CRM_WAREHOUSE_UUID/MCD_SNOWFLAKE_WAREHOUSE_UUID)"
            if any("not found" in m.lower() for m in msgs)
            else " (verify MCD_ID/MCD_TOKEN are a Personal API key, not an Ingestion key)"
        )
        raise MCGraphQLError(f"MC GraphQL error: {'; '.join(msgs)}.{hint}")
    data = body.get("data")
    if data is None:
        raise MCGraphQLError(
            f"MC GraphQL returned null/missing 'data' field (status={resp.status_code}). "
            "This may indicate a gateway-level error."
        )
    return data


# ── Step 1: Salesforce OAuth ──────────────────────────────────────────────────
def get_sf_token(instance_url: str, client_id: str, client_secret: str) -> str:
    t0 = time.monotonic()
    log.info("Step 1: Authenticating with Salesforce (%s)", instance_url)
    token_url = f"{instance_url}/services/oauth2/token"
    instance_host = (urlparse(instance_url).hostname or "").lower()
    # Use allow_redirects=False so we can validate redirect destinations before following.
    # We bypass _http here to intercept the raw 3xx response; the redirect GET goes through _http.
    resp = requests.post(
        token_url,
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        verify=True,
        timeout=30,
        allow_redirects=False,
    )
    # MyDomain / org migration: 301/302/303 are safe (requests downgrades POST→GET, no body).
    # 307/308 would re-POST the credentials body to the redirect target — reject them.
    if 300 <= resp.status_code < 400:
        if resp.status_code in (307, 308):
            raise RuntimeError(
                f"Salesforce OAuth returned {resp.status_code} — this redirect type preserves "
                "the POST body, which would exfiltrate client_secret to the redirect target. "
                "Update SF_ORG_URL to match your org's current MyDomain URL and re-run."
            )
        location = resp.headers.get("Location", "")
        if not location:
            raise RuntimeError(
                f"Salesforce OAuth {resp.status_code} redirect has no Location header"
            )
        loc_parsed = urlparse(location)
        # Compare hostname (not netloc) so an explicit :443 suffix doesn't cause a false-positive
        if loc_parsed.scheme != "https" or (loc_parsed.hostname or "").lower() != instance_host.lower():
            raise RuntimeError(
                f"Salesforce OAuth redirect crosses origins "
                f"(expected {instance_host!r}, got {loc_parsed.hostname!r}). "
                "Update SF_ORG_URL to the correct MyDomain URL and re-run."
            )
        resp = _http("GET", location, verify=True, timeout=30)
    if len(resp.content) > _MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"Salesforce OAuth response body too large "
            f"({len(resp.content):,} bytes > {_MAX_RESPONSE_BYTES:,} byte limit)"
        )
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(
            f"Salesforce auth returned non-JSON response (status={resp.status_code})"
        )
    if "error" in data:
        raise RuntimeError(
            f"Salesforce auth failed: {_sanitize_for_log(str(data.get('error')))} — "
            f"{_sanitize_for_log(str(data.get('error_description', '')))}"
        )
    resp.raise_for_status()
    token = data.get("access_token")
    if not token:
        log.debug("Salesforce auth response keys: %s", list(data.keys()))
        raise RuntimeError(
            f"Salesforce auth response missing access_token (status={resp.status_code}). "
            "Enable DEBUG logging to inspect the response body."
        )
    log.info("  Authenticated (%.1fs)", time.monotonic() - t0)
    return token


# ── Step 2: Fetch Data Streams ────────────────────────────────────────────────
def fetch_data_streams(instance_url: str, access_token: str) -> list:
    """
    Fetch all Data Streams from the Salesforce Connect REST API.
    Paginates via nextPageUrl; enforces same-origin validation on each page URL
    to prevent SSRF via a malicious API response.
    """
    t0 = time.monotonic()
    log.info("Step 2: Fetching Data Streams from Salesforce")
    streams: list = []
    url: Optional[str] = f"{instance_url}/services/data/v{SF_API_VERSION}/ssot/data-streams"
    headers = {"Authorization": f"Bearer {access_token}"}
    page = 0
    instance_parsed = urlparse(instance_url)

    while url:
        page += 1
        if page > _MAX_PAGES:
            raise RuntimeError(
                f"Data Streams pagination safety limit ({_MAX_PAGES} pages) exceeded. "
                "This likely indicates a Salesforce API bug or a runaway response."
            )

        resp = _http("GET", url, headers=headers, verify=True, timeout=30)
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            log.debug("Data Streams API non-JSON response: %s", _sanitize_for_log(resp.text, max_len=500))
            raise RuntimeError(
                f"Data Streams API returned non-JSON response (status={resp.status_code}). "
                "Enable DEBUG logging for the response body."
            ) from exc

        page_streams = body.get("dataStreams") or []
        streams.extend(page_streams)
        log.info("  Page %d: %d stream(s) fetched (%d total)", page, len(page_streams), len(streams))

        next_path = body.get("nextPageUrl")
        if not next_path:
            break

        # Construct and validate the next page URL before following it (SSRF guard)
        next_url = next_path if next_path.startswith("https://") else f"{instance_url}{next_path}"
        next_parsed = urlparse(next_url)
        if next_parsed.scheme != "https" or (next_parsed.hostname or "").lower() != (instance_parsed.hostname or "").lower():
            raise RuntimeError(
                f"Salesforce nextPageUrl has unexpected origin (got: {next_url!r}, "
                f"expected scheme=https host={instance_parsed.hostname}). Aborting to prevent SSRF."
            )
        url = next_url

    log.info("  %d total data stream(s) (%.1fs)", len(streams), time.monotonic() - t0)
    return streams


# ── Step 2b: Fetch connection details (all connector types) ───────────────────
def fetch_connection_details(
    instance_url: str,
    access_token: str,
    streams: list,
) -> dict:
    """
    For every data stream, attempt to look up its MktDataConnection via the
    Tooling API to retrieve source connection parameters.

    Lookup path:
      DataStreamDefinition (Tooling API) → DataConnectorId
      → MktDataConnection → Metadata.parameters.*

    Returns dict mapping stream_name → connection detail dict. All entries
    include at minimum:
        "connector_id":   str,   DataConnectorId (routing key for warehouse maps)
        "connector_name": str,   MktDataConnection MasterLabel
        "connector_type": str,   e.g. "SNOWFLAKE", "AwsS3", "SalesforceDotCom"
        "raw_params":     dict,  all Metadata.parameters as {name: value}

    Additional type-specific fields:

    SNOWFLAKE:
        "account_url":        str,   "https://HDB68299.us-west-2.snowflakecomputing.com"
        "account_identifier": str,   "hdb68299.us-west-2" (lowercased, routing key)
        "warehouse":          str,   Snowflake virtual warehouse name
        "region":             str,   Snowflake region

    AwsS3:
        "bucket_name":        str,   e.g. "my-data-bucket"
        "parent_directory":   str,   e.g. "/" or "data/feeds"

    SalesforceDotCom (native same-org connections):
        MktDataConnection may not exist — entry omitted for those streams.
        External linked-org CRM connections will resolve and expose whatever
        parameters Salesforce stores (typically instanceUrl or orgId).

    Streams for which no MktDataConnection exists (e.g. native CRM connections)
    are omitted from the result — callers should fall back to their single
    warehouse UUID env var for those streams.
    """
    t0 = time.monotonic()
    log.info("Step 2b: Fetching connection details for all streams via Tooling API")

    # Validate stream names before embedding in SOQL IN clauses.
    raw_names = [s.get("name", "") for s in streams if s.get("name")]
    stream_names = []
    for n in raw_names:
        if _SF_DEVELOPER_NAME_RE.fullmatch(n):
            stream_names.append(n)
        else:
            log.warning(
                "  Stream name %r failed DeveloperName validation — "
                "excluding from SOQL query (unexpected characters)",
                _sanitize_for_log(n),
            )
    if not stream_names:
        return {}

    headers = {"Authorization": f"Bearer {access_token}"}
    result: dict = {}

    # Query DataStreamDefinition for all stream names → DataConnectorId.
    # Batched (_TOOLING_SOQL_BATCH_SIZE per query) to avoid URL length limits at scale.
    # Each batch paginates via nextRecordsUrl to handle responses truncated at 2000 records.
    stream_to_connector: dict = {}
    instance_parsed_tooling = urlparse(instance_url)

    for batch_start in range(0, len(stream_names), _TOOLING_SOQL_BATCH_SIZE):
        batch = stream_names[batch_start : batch_start + _TOOLING_SOQL_BATCH_SIZE]
        names_in = ", ".join(f"'{n}'" for n in batch)
        soql_query = (
            "SELECT DeveloperName, DataConnectorId, DataConnectorType "
            f"FROM DataStreamDefinition WHERE DeveloperName IN ({names_in})"
        )
        tooling_url: Optional[str] = (
            f"{instance_url}/services/data/v{SF_API_VERSION}/tooling/query/"
        )
        tooling_params: Optional[dict] = {"q": soql_query}
        soql_page = 0

        while tooling_url:
            soql_page += 1
            if soql_page > _MAX_PAGES:
                raise RuntimeError(
                    f"DataStreamDefinition SOQL pagination safety limit "
                    f"({_MAX_PAGES} pages) exceeded."
                )
            resp = _http(
                "GET",
                tooling_url,
                params=tooling_params,
                headers=headers,
                verify=True,
                timeout=30,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    "DataStreamDefinition tooling query returned non-JSON response"
                ) from exc

            for rec in (body.get("records") or []):
                name         = rec.get("DeveloperName") or ""
                connector_id = rec.get("DataConnectorId") or ""
                conn_type    = rec.get("DataConnectorType") or ""
                if not (name and connector_id):
                    continue
                if not _SF_ID_RE.fullmatch(connector_id):
                    log.warning(
                        "  DataConnectorId %r for stream %r failed ID validation — skipping",
                        _sanitize_for_log(connector_id), _sanitize_for_log(name),
                    )
                    continue
                stream_to_connector[name] = (connector_id, conn_type)

            next_path = body.get("nextRecordsUrl")
            if body.get("done", True) or not next_path:
                break
            # Validate same-origin before following nextRecordsUrl (SSRF guard)
            next_url = (
                next_path if next_path.startswith("https://")
                else f"{instance_url}{next_path}"
            )
            next_parsed_t = urlparse(next_url)
            if (next_parsed_t.scheme != "https"
                    or (next_parsed_t.hostname or "").lower() != (instance_parsed_tooling.hostname or "").lower()):
                raise RuntimeError(
                    f"DataStreamDefinition nextRecordsUrl has unexpected origin "
                    f"(got: {next_url!r}, expected host={instance_parsed_tooling.hostname}). "
                    "Aborting to prevent SSRF."
                )
            tooling_url = next_url
            tooling_params = None  # nextRecordsUrl already encodes query params

    if not stream_to_connector:
        log.info("  No DataConnectorId found for any streams — connection details unavailable")
        return {}

    log.info("  Found DataConnectorId for %d/%d stream(s)", len(stream_to_connector), len(stream_names))

    # Fetch MktDataConnection for each unique DataConnectorId — in parallel to avoid
    # O(n) sequential round-trips on orgs with many connectors.
    unique_ids = {cid for cid, _ in stream_to_connector.values()}
    connector_details: dict = {}

    def _fetch_one_connection(connector_id: str) -> Optional[dict]:
        """Return parsed detail dict or None if the record should be skipped."""
        r = _http(
            "GET",
            f"{instance_url}/services/data/v{SF_API_VERSION}/tooling/sobjects/MktDataConnection/{connector_id}",
            headers=headers,
            verify=True,
            timeout=30,
        )
        if r.status_code == 404:
            log.debug("  MktDataConnection %s not found (likely native connector) — skipping", connector_id)
            return None
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            log.warning("  MktDataConnection %s returned non-JSON — skipping", connector_id)
            return None

        metadata       = data.get("Metadata") or {}
        raw_params     = {
            p["paramName"]: p.get("value", "")
            for p in (metadata.get("parameters") or [])
            if p.get("paramName")
        }
        connector_name = _sanitize_for_log(data.get("MasterLabel", ""))
        connector_type = _sanitize_for_log(metadata.get("connectorName") or "")

        detail: dict = {
            "connector_id":   connector_id,
            "connector_name": connector_name,
            "connector_type": connector_type,
            "raw_params":     _redact_params(raw_params),
        }

        if connector_type == _CONNECTOR_SNOWFLAKE:
            account_url_raw = raw_params.get("accountUrl", "")
            if account_url_raw:
                if _SNOWFLAKE_ACCT_URL_RE.match(account_url_raw):
                    _parsed_acct = urlparse(account_url_raw)
                    hostname = (_parsed_acct.hostname or "").lower()
                    if hostname.count("snowflakecomputing.com") == 1:
                        account_url = _sanitize_for_log(account_url_raw)
                    else:
                        log.warning(
                            "  MktDataConnection %s has suspicious accountUrl %r — ignoring",
                            connector_id, _sanitize_for_log(account_url_raw),
                        )
                        account_url = ""
                else:
                    log.warning(
                        "  MktDataConnection %s has unexpected accountUrl %r — ignoring for routing",
                        connector_id, _sanitize_for_log(account_url_raw),
                    )
                    account_url = ""
            else:
                account_url = ""
            detail["account_url"]        = account_url
            detail["account_identifier"] = _extract_sf_account_identifier(account_url) if account_url else ""
            detail["warehouse"]          = _sanitize_for_log(raw_params.get("warehouse", ""))
            detail["region"]             = _sanitize_for_log(raw_params.get("region", ""))
            log.debug(
                "  MktDataConnection %s (%s): account=%s warehouse=%s",
                connector_id, connector_name,
                detail["account_identifier"], detail["warehouse"],
            )
        else:
            log.debug(
                "  MktDataConnection %s (%s type=%s): %d params",
                connector_id, connector_name, connector_type, len(raw_params),
            )
        return detail

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one_connection, cid): cid for cid in unique_ids}
        for future in concurrent.futures.as_completed(futures):
            cid = futures[future]
            try:
                detail = future.result()
            except Exception as exc:
                log.warning("  MktDataConnection %s fetch failed: %s — skipping", cid, _sanitize_for_log(str(exc)))
                detail = None
            if detail is not None:
                connector_details[cid] = detail

    for stream_name, (connector_id, _) in stream_to_connector.items():
        if connector_id in connector_details:
            result[stream_name] = connector_details[connector_id]

    log.info(
        "  Connection details resolved for %d/%d stream(s) (%.1fs)",
        len(result), len(stream_to_connector), time.monotonic() - t0,
    )
    return result


# ── Step 3: Fetch MC catalogs ─────────────────────────────────────────────────
class _CatalogIndex(NamedTuple):
    """Two-dict index built from a warehouse's MC catalog.

    Separating full-qualified and name-only keys into distinct dicts eliminates
    any possibility of a collision between the two key types in a shared namespace.
    """
    by_full: dict   # {fullTableId.lower() → fullTableId}  — for exact lookups
    by_name: dict   # {table_name.lower() → fullTableId or list}  — for name-only lookups
    table_count: int


def _fetch_catalog(warehouse_uuid: str, key_id: str, key_secret: str, label: str) -> _CatalogIndex:
    """
    Fetch all tables for a warehouse from the MC catalog.

    Returns a _CatalogIndex with two separate lookup dicts:
      - by_full: keyed by fullTableId.lower() for exact match
      - by_name: keyed by table name (last segment after final '.'), lowercased

    Both dicts are populated during a single paginated query, scoped to the warehouse UUID
    to prevent cross-org contamination when multiple warehouses exist in the same MC account.

    Note: for Snowflake warehouses, log a DEBUG sample to confirm the fullTableId format
    (e.g., "database:schema.table") matches what the Data Stream advancedAttributes provides.
    """
    by_full: dict = {}
    by_name: dict = {}
    after = None
    page = 0
    table_count = 0
    first_few_logged = False

    while True:
        page += 1
        if page > _MAX_PAGES:
            raise RuntimeError(
                f"{label} catalog pagination safety limit ({_MAX_PAGES} pages) exceeded."
            )

        variables: dict = {"dwId": warehouse_uuid}
        if after:
            variables["after"] = after

        data = _gql(
            """
            query GetAllTables($dwId: UUID, $after: String) {
              getTables(first: 500, dwId: $dwId, after: $after) {
                edges { node { fullTableId } }
                pageInfo { hasNextPage endCursor }
              }
            }
            """,
            key_id,
            key_secret,
            variables=variables,
        )

        result = data.get("getTables") or {}
        page_fids = []
        for edge in (result.get("edges") or []):
            node = edge.get("node") or {}
            fid = node.get("fullTableId") or ""
            if not fid:
                continue
            by_full[fid.lower()] = fid
            # Name-only index: last segment after the final '.'
            if "." in fid:
                name_only = fid.rsplit(".", 1)[1].lower()
                if name_only not in by_name:
                    by_name[name_only] = fid
                else:
                    existing = by_name[name_only]
                    if isinstance(existing, list):
                        existing.append(fid)
                    else:
                        by_name[name_only] = [existing, fid]
            table_count += 1
            page_fids.append(fid)

        # Log a sample of raw fullTableId values on the first page at DEBUG level.
        # This lets operators confirm the key format (e.g. "db:schema.table") matches
        # what the Data Stream advancedAttributes provides — especially important for Snowflake.
        if not first_few_logged and page_fids:
            log.debug("  %s catalog sample fullTableId values: %s", label, page_fids[:5])
            first_few_logged = True

        log.info("  %s catalog page %d: %d table(s) retrieved so far", label, page, table_count)

        page_info = result.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            log.warning(
                "  %s catalog hasNextPage=true but endCursor is null — "
                "stopping pagination to avoid infinite loop",
                label,
            )
            break

    return _CatalogIndex(by_full=by_full, by_name=by_name, table_count=table_count)


def _lookup(idx: _CatalogIndex, key: str, label: str) -> Optional[str]:
    """
    Look up key in the catalog index, case-insensitively.
    Tries the full-qualified dict first (exact match), then the name-only dict.
    Returns fullTableId or None.
    Warns and returns the first entry when a name-only key is ambiguous.
    """
    lower = key.lower()
    # Prefer exact full-qualified match
    result = idx.by_full.get(lower)
    if result is not None:
        return result
    # Fall back to name-only match
    entry = idx.by_name.get(lower)
    if entry is None:
        return None
    if isinstance(entry, list):
        log.warning(
            "  Ambiguous catalog match for '%s' in %s — multiple tables share this name: %s. "
            "Using '%s'. Provide the full-qualified key (database:schema.table) to be precise.",
            key, label, entry, entry[0],
        )
        return entry[0]
    return entry


# ── Step 4: Resolve edges from Data Streams ───────────────────────────────────
def resolve_edges(
    streams: list,
    dc_idx: _CatalogIndex,
    crm_idx: Optional[_CatalogIndex],
    sf_idx: Optional[_CatalogIndex],
    dc_warehouse_uuid: str,
    crm_warehouse_uuid: Optional[str],
    snowflake_warehouse_uuid: Optional[str],
    connection_details: Optional[dict] = None,
    sf_catalogs: Optional[dict] = None,
    crm_catalogs: Optional[dict] = None,
) -> tuple:
    """
    Match each data stream's source table and DLO to MC catalog entries.

    Returns (edges, skipped) where:
      edges   — list of edge dicts ready for the GraphQL push
      skipped — list of dicts with full raw stream data + skip reason for every
                stream that could not be resolved; saved to disk for future
                troubleshooting and manual MC asset mapping

    Multi-warehouse Snowflake:
      sf_catalogs  — {account_identifier: (_CatalogIndex, warehouse_uuid)} built
                     from MCD_SNOWFLAKE_WAREHOUSE_MAP + connection_details
      connection_details — {stream_name: {account_url, account_identifier, connector_id, ...}}
                           from fetch_connection_details(); used to route each
                           Snowflake or linked-CRM stream to the right catalog entry
      sf_idx / snowflake_warehouse_uuid — single-warehouse fallback when sf_catalogs
                                          has no entry for a stream's account

    Multi-org CRM:
      crm_catalogs — {connector_id: (_CatalogIndex, warehouse_uuid)} built from
                     MCD_CRM_WAREHOUSE_MAP; keyed by DataConnectorId so each linked
                     CRM org routes to the correct MC CRM warehouse.
                     Native same-org streams (no MktDataConnection record) have no
                     connector_id in connection_details and fall back to crm_idx.

    Edge dict schema:
      {
        "source_full_id":   str,   fullTableId of source in MC catalog
        "source_warehouse": str,   MC warehouse UUID for source system
        "dest_full_id":     str,   fullTableId of DLO in MC Data Cloud catalog
        "dest_warehouse":   str,   DC warehouse UUID
        "connector_type":   str,   SalesforceDotCom | SNOWFLAKE
        "dlo_name":         str,   raw DLO name from Salesforce
        "source_label":     str,   human-readable source description for logging
      }

    Unsupported connector types (AwsS3 and others) are skipped with a warning.
    AwsS3 sources require custom node creation before edges can be linked — that
    workflow is intentionally out of scope for this script.
    """
    t0 = time.monotonic()
    log.info("Step 4: Resolving %d data stream(s) to MC catalog entries", len(streams))
    edges: list = []
    skipped: list = []           # full raw stream + reason, saved to disk at end
    skipped_bad_data = 0         # missing required field in API response
    skipped_missing_uuid = 0     # connector type present but warehouse UUID not configured
    skipped_unknown_connector = 0
    skipped_not_in_mc = 0        # table not found in MC catalog after lookup

    _conn_details  = connection_details or {}
    _sf_catalogs   = sf_catalogs or {}
    _crm_catalogs  = crm_catalogs or {}

    def _skip(stream: dict, reason: str, detail: str = "") -> None:
        """Record a skipped stream with full DSO + connection details for troubleshooting."""
        stream_name_key = stream.get("name", "")
        stream_name     = _sanitize_for_log(stream_name_key)
        conn_info       = _conn_details.get(stream_name_key, {})
        skipped.append({
            "reason":             reason,
            "detail":             detail,
            "connector_type":     _sanitize_for_log(
                (stream.get("connectorInfo") or {}).get("connectorType", "unknown")
            ),
            "dlo_name":           _sanitize_for_log(
                (stream.get("dataLakeObjectInfo") or {}).get("name", "unknown")
            ),
            "connection_details": conn_info,   # Snowflake account URL, warehouse, region
            "raw_stream":         _sanitize_stream_for_disk(stream),  # credentials redacted
        })

    for stream in streams:
        connector_info = stream.get("connectorInfo") or {}
        connector_type = _sanitize_for_log(connector_info.get("connectorType", ""))
        dlo_info = stream.get("dataLakeObjectInfo") or {}
        dlo_name = _sanitize_for_log(dlo_info.get("name", ""))

        if not dlo_name:
            log.warning(
                "  Stream (connectorType=%s) has no dataLakeObjectInfo.name — skipping",
                connector_type,
            )
            _skip(stream, "missing_dlo_name")
            skipped_bad_data += 1
            continue

        if connector_type not in _SUPPORTED_CONNECTORS:
            log.warning(
                "  Unsupported connector type '%s' (DLO=%s) — skipping. "
                "Supported: %s",
                connector_type, dlo_name, ", ".join(sorted(_SUPPORTED_CONNECTORS)),
            )
            _skip(stream, "unsupported_connector", f"connector_type={connector_type}")
            skipped_unknown_connector += 1
            continue

        # Resolve DLO in Data Cloud catalog
        dlo_full_id = _lookup(dc_idx, dlo_name, "Data Cloud")
        if not dlo_full_id:
            log.warning(
                "  DLO '%s' not found in MC Data Cloud catalog — skipping. "
                "Trigger a metadata sync in Monte Carlo to pick up this table.",
                dlo_name,
            )
            _skip(stream, "dlo_not_in_mc_catalog", f"dlo_name={dlo_name}")
            skipped_not_in_mc += 1
            continue

        # Resolve source table based on connector type
        if connector_type == _CONNECTOR_SFDC:
            # Route to the correct MC CRM warehouse.
            # Priority: crm_catalogs (multi-org map keyed by connector_id) → crm_idx (fallback).
            # Native same-org CRM streams have no MktDataConnection record, so connector_id
            # is absent from connection_details; they always fall back to crm_idx.
            stream_name_crm  = _sanitize_for_log(stream.get("name", ""))  # logging only
            conn_info_crm    = _conn_details.get(stream.get("name", ""), {})
            connector_id_crm = conn_info_crm.get("connector_id", "")
            # crm_catalogs keys are lowercased by _parse_warehouse_map; Salesforce IDs
            # are case-insensitive, so normalise before lookup.
            connector_id_crm_key = connector_id_crm.lower()

            active_crm_idx: Optional[_CatalogIndex] = None
            active_crm_uuid: Optional[str] = None

            if connector_id_crm_key and connector_id_crm_key in _crm_catalogs:
                active_crm_idx, active_crm_uuid = _crm_catalogs[connector_id_crm_key]
                log.debug(
                    "  Routed CRM stream '%s' to warehouse %s via connector_id '%s'",
                    stream_name_crm, active_crm_uuid, connector_id_crm,
                )
            elif crm_idx is not None:
                # If this stream has a resolved connector_id but it's not in the map,
                # it is a linked external org. Routing it to the native-org fallback
                # warehouse would silently look in the wrong catalog — skip instead.
                if connector_id_crm_key and _crm_catalogs:
                    _safe_conn_id = _sanitize_for_log(connector_id_crm)
                    log.warning(
                        "  CRM stream '%s' (connector_id=%s) is a linked-org stream not found "
                        "in MCD_CRM_WAREHOUSE_MAP — skipping to avoid wrong-warehouse routing. "
                        "Add '%s=<uuid>' to MCD_CRM_WAREHOUSE_MAP.",
                        stream_name_crm, _safe_conn_id, _safe_conn_id,
                    )
                    _skip(stream, "linked_crm_org_not_in_warehouse_map",
                          f"connector_id={connector_id_crm} dlo_name={dlo_name}")
                    skipped_missing_uuid += 1
                    continue
                active_crm_idx  = crm_idx
                active_crm_uuid = crm_warehouse_uuid
            else:
                log.warning(
                    "  CRM stream found (DLO=%s) but no CRM warehouse was found in MC — skipping. "
                    "Ensure a Salesforce CRM connection exists in MC for this org.",
                    dlo_name,
                )
                _skip(stream, "crm_warehouse_uuid_not_configured", f"dlo_name={dlo_name}")
                skipped_missing_uuid += 1
                continue

            if active_crm_uuid is None:
                raise RuntimeError(
                    "Internal invariant violated: active_crm_idx is set but active_crm_uuid is None. "
                    "This is a bug — please report it."
                )

            details = connector_info.get("connectorDetails") or {}
            source_obj = _sanitize_for_log(details.get("sourceObject", ""))
            if not source_obj:
                log.warning(
                    "  CRM stream for DLO=%s has no connectorDetails.sourceObject — skipping",
                    dlo_name,
                )
                _skip(stream, "missing_crm_source_object", f"dlo_name={dlo_name}")
                skipped_bad_data += 1
                continue
            src_full_id = _lookup(active_crm_idx, source_obj, "Salesforce CRM")
            if not src_full_id:
                log.warning(
                    "  CRM table '%s' not found in MC CRM catalog (DLO=%s) — skipping. "
                    "Confirm the Salesforce CRM connector has synced this object.",
                    source_obj, dlo_name,
                )
                _skip(stream, "source_not_in_mc_catalog",
                      f"source_object={source_obj} dlo_name={dlo_name}")
                skipped_not_in_mc += 1
                continue
            src_warehouse_uuid = active_crm_uuid
            source_label = f"CRM:{source_obj}"

        else:  # SNOWFLAKE
            # Route to the correct MC warehouse using the stream's Snowflake account identifier.
            # Priority: sf_catalogs (multi-warehouse map) → sf_idx (single-warehouse fallback).
            stream_name  = _sanitize_for_log(stream.get("name", ""))  # logging only
            conn_info    = _conn_details.get(stream.get("name", ""), {})
            account_id   = conn_info.get("account_identifier", "")
            account_url  = conn_info.get("account_url", "")

            active_sf_idx: Optional[_CatalogIndex] = None
            active_sf_uuid: Optional[str] = None

            if account_id and account_id in _sf_catalogs:
                active_sf_idx, active_sf_uuid = _sf_catalogs[account_id]
                log.debug(
                    "  Routed stream '%s' to warehouse %s via account '%s'",
                    stream_name, active_sf_uuid, account_id,
                )
            elif sf_idx is not None:
                # Fall back to single-warehouse mode
                active_sf_idx  = sf_idx
                active_sf_uuid = snowflake_warehouse_uuid
                if account_id:
                    log.warning(
                        "  Snowflake stream '%s' (account=%s) not in warehouse map — "
                        "falling back to MCD_SNOWFLAKE_WAREHOUSE_UUID. "
                        "Add '%s=<uuid>' to MCD_SNOWFLAKE_WAREHOUSE_MAP for precise routing.",
                        stream_name, account_id, account_id,
                    )
            else:
                if _sf_catalogs and account_id:
                    log.warning(
                        "  Snowflake stream '%s' (DLO=%s, account=%s) not in MCD_SNOWFLAKE_WAREHOUSE_MAP "
                        "and no fallback UUID set. Add '%s=<uuid>' to MCD_SNOWFLAKE_WAREHOUSE_MAP.",
                        stream_name, dlo_name, account_id, account_id,
                    )
                else:
                    log.warning(
                        "  Snowflake stream found (DLO=%s, account=%s) but no matching MC warehouse was found. "
                        "Ensure a Snowflake connection exists in MC for account '%s'.",
                        dlo_name, account_id or "unknown", account_id or "unknown",
                    )
                _skip(stream, "snowflake_warehouse_not_configured",
                      f"dlo_name={dlo_name} account_identifier={account_id} account_url={account_url}")
                skipped_missing_uuid += 1
                continue

            if active_sf_uuid is None:
                log.warning(
                    "  Snowflake stream '%s' (DLO=%s): warehouse catalog resolved but "
                    "warehouse UUID is None — skipping. "
                    "Verify MCD_SNOWFLAKE_WAREHOUSE_UUID is set.",
                    stream_name, dlo_name,
                )
                _skip(stream, "snowflake_warehouse_uuid_none",
                      f"dlo_name={dlo_name} account_identifier={account_id}")
                skipped_missing_uuid += 1
                continue

            attrs = stream.get("advancedAttributes") or {}
            sf_db     = _sanitize_for_log(attrs.get("database", "")).lower()
            sf_schema = _sanitize_for_log(attrs.get("schema", "")).lower()
            sf_obj    = _sanitize_for_log(attrs.get("object", "")).lower()
            if not sf_obj:
                log.warning(
                    "  Snowflake stream for DLO=%s has no advancedAttributes.object — skipping",
                    dlo_name,
                )
                _skip(stream, "missing_snowflake_object",
                      f"dlo_name={dlo_name} advancedAttributes={str(_redact_params(dict(attrs)))[:500]}")
                skipped_bad_data += 1
                continue
            # Try full-qualified key first (most specific), then object name only (fallback).
            sf_key_full = f"{sf_db}:{sf_schema}.{sf_obj}" if sf_db and sf_schema else None
            src_full_id = None
            if sf_key_full:
                src_full_id = _lookup(active_sf_idx, sf_key_full, "Snowflake")
            if src_full_id is None:
                src_full_id = _lookup(active_sf_idx, sf_obj, "Snowflake")
                if src_full_id is not None:
                    log.warning(
                        "  Snowflake full-qualified key '%s' not found in catalog; "
                        "matched by table name only ('%s'). "
                        "Run with LOG_LEVEL=DEBUG to verify the fullTableId format.",
                        sf_key_full or sf_obj, sf_obj,
                    )
            if not src_full_id:
                log.warning(
                    "  Snowflake table '%s.%s.%s' not found in MC catalog (DLO=%s, account=%s) — skipping. "
                    "Run with LOG_LEVEL=DEBUG to inspect catalog fullTableId format.",
                    sf_db.upper(), sf_schema.upper(), sf_obj.upper(), dlo_name,
                    account_id or "unknown",
                )
                _skip(stream, "source_not_in_mc_catalog",
                      f"sf_key={sf_key_full} dlo_name={dlo_name} "
                      f"account_identifier={account_id} "
                      f"advancedAttributes={str(_redact_params(dict(attrs)))[:500]}")
                skipped_not_in_mc += 1
                continue
            src_warehouse_uuid = active_sf_uuid
            source_label = f"Snowflake:{sf_db.upper()}.{sf_schema.upper()}.{sf_obj.upper()}"

        edges.append({
            "source_full_id":   src_full_id,
            "source_warehouse": src_warehouse_uuid,
            "dest_full_id":     dlo_full_id,
            "dest_warehouse":   dc_warehouse_uuid,
            "connector_type":   connector_type,
            "dlo_name":         dlo_name,
            "source_label":     source_label,
        })
        log.debug("  Resolved: %s → %s", source_label, dlo_name)

    log.info(
        "  %d edge(s) resolved; %d skipped (not in MC catalog); "
        "%d skipped (warehouse UUID not configured); "
        "%d skipped (unsupported connector); %d skipped (bad API data) (%.1fs)",
        len(edges), skipped_not_in_mc, skipped_missing_uuid,
        skipped_unknown_connector, skipped_bad_data,
        time.monotonic() - t0,
    )
    return edges, skipped


# ── Failed-edge persistence ───────────────────────────────────────────────────
def _save_failed_edges(run_id: str, edges: list) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(__file__).parent / f"failed_edges_external_{run_id}_{ts}.json"
    content = json.dumps(edges, indent=2).encode()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = os.write(fd, content)
        if written != len(content):
            raise OSError(f"Partial write: wrote {written}/{len(content)} bytes")
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)  # don't leave a zero-byte file that blocks retry
        raise
    else:
        os.close(fd)
    log.info("  Failed edges saved to: %s", path.resolve())
    return str(path)


def _save_skipped_streams(run_id: str, skipped: list) -> str:
    """
    Save full raw DSO data for every stream that could not be resolved to an MC
    lineage edge.  Each entry includes:
      - reason          why it was skipped (e.g. "source_not_in_mc_catalog")
      - detail          human-readable detail string
      - connector_type  Salesforce connector type string
      - dlo_name        the DLO the stream feeds
      - raw_stream      complete API response object — includes connectorInfo,
                        advancedAttributes, dataLakeObjectInfo etc.  Use this to
                        manually identify the MC asset and create the edge later,
                        or to diagnose why the lookup failed.
    """
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(__file__).parent / f"skipped_streams_{run_id}_{ts}.json"
    payload = {
        "run_id":        run_id,
        "timestamp_utc": ts,
        "skipped_count": len(skipped),
        "streams":       skipped,
    }
    content = json.dumps(payload, indent=2, default=str).encode()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = os.write(fd, content)
        if written != len(content):
            raise OSError(f"Partial write: wrote {written}/{len(content)} bytes")
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    return str(path.resolve())


# ── Discovery mode ────────────────────────────────────────────────────────────
def _run_discover(streams: list, run_id: str, instance_url: str) -> None:
    """
    Group all data streams by connector type and print a summary table.
    Saves raw connection field samples to discover_{run_id}_{ts}.json.
    Does not touch the MC catalog or push any lineage.
    """
    t0 = time.monotonic()
    log.info("=== Discovery Mode — scanning %d stream(s) ===", len(streams))

    groups: dict = {}
    for stream in streams:
        ct = (stream.get("connectorInfo") or {}).get("connectorType", "(unknown)")
        if ct not in groups:
            groups[ct] = []
        groups[ct].append(stream)

    report_types: dict = {}
    for ct, group_streams in sorted(groups.items()):
        sample = group_streams[0]
        # Redact connectorDetails and advancedAttributes — may contain credentials.
        # isinstance guards handle unexpected non-dict values from the API without crashing.
        _sample_conn_info = dict(sample.get("connectorInfo") or {})
        if "connectorDetails" in _sample_conn_info:
            cd = _sample_conn_info.get("connectorDetails")
            _sample_conn_info["connectorDetails"] = _redact_params(cd if isinstance(cd, dict) else {})
        _sample_aa = sample.get("advancedAttributes")
        report_types[ct] = {
            "count":                  len(group_streams),
            "supported_for_lineage":  ct in _SUPPORTED_CONNECTORS,
            "dlo_names": [
                _sanitize_for_log(
                    (s.get("dataLakeObjectInfo") or {}).get("name", "(unknown)")
                )
                for s in group_streams
            ],
            "sample_connector_info":        _sample_conn_info,
            "sample_advanced_attributes":   _redact_params(_sample_aa if isinstance(_sample_aa, dict) else {}),
            "sample_dlo_info":              sample.get("dataLakeObjectInfo") or {},
        }

    log.info("  %-32s  %5s  %s", "Connector Type", "Count", "Lineage Support")
    log.info("  %s", "-" * 62)
    for ct, info in sorted(report_types.items()):
        if ct == _CONNECTOR_SFDC:
            support = "Yes — CRM → DLO"
        elif ct == _CONNECTOR_SNOWFLAKE:
            support = "Yes — Snowflake → DLO"
        else:
            support = "No — not supported"
        log.info("  %-32s  %5d  %s", ct, info["count"], support)
    log.info("  Total: %d stream(s) across %d connector type(s)", len(streams), len(report_types))

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(__file__).parent / f"discover_{run_id}_{ts}.json"
    payload = {
        "run_id":         run_id,
        "timestamp_utc":  ts,
        "instance_url":   instance_url,
        "total_streams":  len(streams),
        "connector_types": report_types,
    }
    content = json.dumps(payload, indent=2, default=str).encode()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = os.write(fd, content)
        if written != len(content):
            raise OSError(f"Partial write: wrote {written}/{len(content)} bytes")
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)

    log.info(
        "  Full connection field samples saved to: %s (%.1fs)",
        path.resolve(), time.monotonic() - t0,
    )


# ── Step 5: Push via Ingest API (cross-warehouse) ─────────────────────────────

def _is_ingest_retryable(exc: BaseException) -> bool:
    # pycarlo wraps HTTPError into IngestionError (no .response); check __cause__ too
    resp = getattr(exc, "response", None)
    if resp is None and exc.__cause__ is not None:
        resp = getattr(exc.__cause__, "response", None)
    if resp is not None and 400 <= resp.status_code < 500:
        return False
    return True


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception(_is_ingest_retryable),
    reraise=True,
)
def _send_batch(svc: IngestionService, events: list) -> dict:
    return svc.send_lineage(events=events)


def push_edges(
    edges: list,
    ingest_key_id: str,
    ingest_key_secret: str,
    run_id: str,
    batch_size: int = 500,
    shutdown_flag: Optional[threading.Event] = None,
) -> int:
    """
    Push resolved edges via Ingest API cross-warehouse lineage.
    Each edge specifies resource_uuid + resource_type per asset — no top-level resource.
    The push is idempotent — re-running is safe.
    Returns count of successfully pushed edges.
    """
    t0 = time.monotonic()
    log.info("Step 5: Pushing %d edge(s) via Ingest API (cross-warehouse)", len(edges))

    svc = IngestionService(mc_client=Client(session=Session(
        mcd_id=ingest_key_id,
        mcd_token=ingest_key_secret,
        scope="Ingestion",
        endpoint=MC_INGEST_URL,
    )))

    _resource_type_map = {
        _CONNECTOR_SFDC:      _CRM_RESOURCE_TYPE,
        _CONNECTOR_SNOWFLAKE: _SNOWFLAKE_RESOURCE_TYPE,
    }

    pushed = 0
    failures: list = []
    batches = [edges[i:i + batch_size] for i in range(0, len(edges), batch_size)]
    invocation_ids: list = []

    for batch_idx, batch in enumerate(batches, 1):
        if shutdown_flag is not None and shutdown_flag.is_set():
            log.warning(
                "SIGTERM received — stopping push after batch %d/%d. "
                "Partial results written to Monte Carlo. Re-run is safe (idempotent).",
                batch_idx - 1, len(batches),
            )
            break

        events = []
        for e in batch:
            src_db, src_schema, src_name = _parse_full_table_id(e["source_full_id"])
            dst_db, dst_schema, dst_name = _parse_full_table_id(e["dest_full_id"])
            src_resource_type = _resource_type_map.get(
                e["connector_type"], e["connector_type"].lower()
            )
            events.append(LineageEvent(
                destination=LineageAssetRef(
                    type="TABLE",
                    database=dst_db,
                    schema=dst_schema,
                    name=dst_name,
                    resource_uuid=e["dest_warehouse"],
                    resource_type=_DC_RESOURCE_TYPE,
                ),
                sources=[LineageAssetRef(
                    type="TABLE",
                    database=src_db,
                    schema=src_schema,
                    name=src_name,
                    resource_uuid=e["source_warehouse"],
                    resource_type=src_resource_type,
                )],
            ))

        try:
            result = _send_batch(svc, events)
            inv_id = (result or {}).get("invocation_id", "?")
            invocation_ids.append(inv_id)
            log.info(
                "  Batch %d/%d: %d edge(s) pushed — invocation_id=%s",
                batch_idx, len(batches), len(batch), inv_id,
            )
            pushed += len(batch)
        except Exception as exc:
            log.error(
                "  Batch %d/%d failed (%d edge(s)): %s",
                batch_idx, len(batches), len(batch), exc,
            )
            failures.extend(batch)

    if failures:
        try:
            path = _save_failed_edges(run_id, failures)
            log.error(
                "%d edge(s) failed to push. Re-run after addressing errors. "
                "Failed edges saved to: %s",
                len(failures), path,
            )
        except OSError as exc:
            summary = [
                {"connector_type": e["connector_type"], "dlo_name": e["dlo_name"]}
                for e in failures
            ]
            log.error(
                "%d edge(s) failed and the failed-edge file could not be written (%s). "
                "Failed edges: %s",
                len(failures), exc, json.dumps(summary)[:2000],
            )

    log.info(
        "  Push complete: %d succeeded, %d failed (%.1fs)",
        pushed, len(failures), time.monotonic() - t0,
    )
    return pushed


# ── Service classes ───────────────────────────────────────────────────────────
class SalesforceService:
    def __init__(self, instance_url: str, client_id: str, client_secret: str) -> None:
        self.instance_url = instance_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str = ""

    def __repr__(self) -> str:
        return (
            f"SalesforceService(instance_url={self.instance_url!r}, "
            f"client_id={self.client_id!r}, client_secret=<redacted>)"
        )

    def authenticate(self) -> None:
        try:
            self._token = get_sf_token(self.instance_url, self.client_id, self.client_secret)
        finally:
            self.client_secret = ""  # drop reference regardless of success or failure

    @property
    def token(self) -> str:
        if not self._token:
            raise RuntimeError("Not authenticated — call authenticate() first.")
        return self._token

    def fetch_data_streams(self) -> list:
        return fetch_data_streams(self.instance_url, self.token)

    def fetch_connection_details(self, streams: list) -> dict:
        return fetch_connection_details(self.instance_url, self.token, streams)

    def invalidate_token(self) -> None:
        self._token = ""


def _discover_warehouse_uuids(
    gql_key_id: str,
    gql_key_secret: str,
    sf_org_domain: str,
) -> dict:
    """
    Queries MC getWarehouses and matches metadataConnection.identifiers to auto-discover:
      - Data Cloud warehouse: SALESFORCE_DATA_CLOUD whose domain == SF_ORG_URL hostname
      - CRM warehouse:        SALESFORCE_CRM whose domain == SF_ORG_URL hostname
      - Snowflake warehouses: all SNOWFLAKE warehouses, keyed by account identifier (lowercase)

    The domain match is exact (case-insensitive): MC stores the same hostname that
    Salesforce reports in SF_ORG_URL, so there is no ambiguity or guessing.

    Returns:
      {"dc_uuid": str|None, "crm_uuid": str|None, "snowflake_map": {account_lower: uuid}}
      dc_uuid / crm_uuid are None on failure OR when multiple matches are found (caller
      should then require explicit env var override).
    """
    _EMPTY: dict = {"dc_uuid": None, "crm_uuid": None, "snowflake_map": {}}
    query = """
    query {
      getWarehouses {
        uuid connectionType
        metadataConnection { identifiers }
      }
    }
    """
    try:
        data = _gql(query, gql_key_id, gql_key_secret)
        warehouses = (data or {}).get("getWarehouses") or []
    except Exception as exc:
        log.warning("  Warehouse auto-discovery failed (%s) — relying on env var overrides.", exc)
        return _EMPTY

    if len(warehouses) >= _MAX_WAREHOUSES_DISCOVERY:
        log.error(
            "  getWarehouses returned %d warehouse(s) — the result may be truncated. "
            "Auto-discovery is unreliable for this account. Set MCD_DC_WAREHOUSE_UUID "
            "(or MCD_RESOURCE_UUID), MCD_CRM_WAREHOUSE_UUID, and MCD_SNOWFLAKE_WAREHOUSE_MAP "
            "as explicit overrides to bypass discovery.",
            len(warehouses),
        )

    domain_lower = sf_org_domain.lower()
    dc_matches: list = []
    crm_matches: list = []
    snowflake_map: dict = {}

    for wh in warehouses:
        ct   = wh.get("connectionType", "")
        uuid = wh.get("uuid", "")
        raw  = (wh.get("metadataConnection") or {}).get("identifiers") or "[]"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = []
        idents = {
            item["key"]: item.get("value", "")
            for item in raw
            if isinstance(item, dict) and "key" in item
        }

        if ct == "SALESFORCE_DATA_CLOUD":
            if idents.get("domain", "").lower() == domain_lower:
                dc_matches.append(uuid)
        elif ct == "SALESFORCE_CRM":
            if idents.get("domain", "").lower() == domain_lower:
                crm_matches.append(uuid)
        elif ct == "SNOWFLAKE":
            acct = idents.get("account", "")
            if acct:
                snowflake_map[acct.lower()] = uuid

    dc_uuid: Optional[str] = None
    if len(dc_matches) > 1:
        log.error(
            "  Auto-discovery found %d Data Cloud warehouses for domain %r: %s. "
            "Set MCD_DC_WAREHOUSE_UUID (or MCD_RESOURCE_UUID) to the correct UUID.",
            len(dc_matches), sf_org_domain, ", ".join(dc_matches),
        )
    elif dc_matches:
        dc_uuid = dc_matches[0]

    crm_uuid: Optional[str] = None
    if len(crm_matches) > 1:
        log.error(
            "  Auto-discovery found %d CRM warehouses for domain %r: %s. "
            "Set MCD_CRM_WAREHOUSE_UUID to the correct UUID.",
            len(crm_matches), sf_org_domain, ", ".join(crm_matches),
        )
    elif crm_matches:
        crm_uuid = crm_matches[0]

    return {"dc_uuid": dc_uuid, "crm_uuid": crm_uuid, "snowflake_map": snowflake_map}


class MCLineageService:
    def __init__(
        self,
        key_id: str,
        key_secret: str,
        ingest_key_id: str,
        ingest_key_secret: str,
    ) -> None:
        self.key_id = key_id
        self.key_secret = key_secret
        self.ingest_key_id = ingest_key_id
        self.ingest_key_secret = ingest_key_secret

    def fetch_catalog(self, warehouse_uuid: str, label: str) -> _CatalogIndex:
        return _fetch_catalog(warehouse_uuid, self.key_id, self.key_secret, label)

    def push_edges(
        self,
        edges: list,
        run_id: str,
        batch_size: int = 500,
        shutdown_flag: Optional[threading.Event] = None,
    ) -> int:
        return push_edges(
            edges, self.ingest_key_id, self.ingest_key_secret, run_id, batch_size, shutdown_flag
        )


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    run_id = uuid_module.uuid4().hex[:8]
    global log
    log = _setup_logging(run_id)

    parser = argparse.ArgumentParser(
        description="Push Salesforce Data 360 external source → DLO lineage to Monte Carlo"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch live data and preview edges, but skip the MC push",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Scan all Data Streams and print a connector-type summary. "
            "Saves raw connectorInfo + advancedAttributes samples to "
            "discover_{run_id}_{ts}.json. Does not fetch the MC catalog or push lineage."
        ),
    )
    args = parser.parse_args()

    _shutdown_requested = threading.Event()

    def _handle_sigterm(signum, frame):  # noqa: ANN001
        log.warning(
            "Received SIGTERM — stop requested. "
            "Push will halt cleanly after the current batch. Re-run is safe (idempotent)."
        )
        _shutdown_requested.set()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    mode_tag = " [DISCOVER]" if args.discover else " [DRY RUN]" if args.dry_run else ""
    log.info(
        "=== Data 360 External Lineage Push | run_id=%s%s ===",
        run_id, mode_tag,
    )
    t_start = time.monotonic()

    # MC creds — validate at startup (before any Salesforce API calls) so a misconfigured
    # run fails immediately rather than after expensive data collection.
    # Skipped in --discover mode which doesn't need MC credentials.
    if not args.discover:
        mcd_id    = _require("MCD_ID")
        mcd_token = _require("MCD_TOKEN")
        mcd_ingest_id    = _require("MCD_INGEST_ID")
        mcd_ingest_token = _require("MCD_INGEST_TOKEN")
        # Accept MCD_RESOURCE_UUID as an alias for MCD_DC_WAREHOUSE_UUID so that
        # users running both scripts from the same .env only need to set one variable.
        # If neither is set, the UUID is auto-discovered from MC after SF auth.
        dc_warehouse_uuid = (
            os.environ.get("MCD_DC_WAREHOUSE_UUID")
            or os.environ.get("MCD_RESOURCE_UUID")
            or ""
        )
        if dc_warehouse_uuid:
            _validate_uuid("MCD_DC_WAREHOUSE_UUID", dc_warehouse_uuid)

        crm_warehouse_uuid       = os.environ.get("MCD_CRM_WAREHOUSE_UUID")
        crm_warehouse_map_raw    = os.environ.get("MCD_CRM_WAREHOUSE_MAP", "")
        snowflake_warehouse_uuid = os.environ.get("MCD_SNOWFLAKE_WAREHOUSE_UUID")
        snowflake_warehouse_map  = os.environ.get("MCD_SNOWFLAKE_WAREHOUSE_MAP", "")
        if crm_warehouse_uuid:
            _validate_uuid("MCD_CRM_WAREHOUSE_UUID", crm_warehouse_uuid)
        if snowflake_warehouse_uuid:
            _validate_uuid("MCD_SNOWFLAKE_WAREHOUSE_UUID", snowflake_warehouse_uuid)

        crm_warehouse_map: dict = {}
        if crm_warehouse_map_raw:
            crm_warehouse_map = _parse_warehouse_map(crm_warehouse_map_raw)
            for conn_id, wh_uuid in crm_warehouse_map.items():
                _validate_uuid(f"MCD_CRM_WAREHOUSE_MAP[{conn_id}]", wh_uuid)
            log.info("  CRM warehouse map: %d connector(s) configured", len(crm_warehouse_map))

        sf_warehouse_map: dict = {}
        if snowflake_warehouse_map:
            sf_warehouse_map = _parse_warehouse_map(snowflake_warehouse_map)
            for acct_id, wh_uuid in sf_warehouse_map.items():
                _validate_uuid(f"MCD_SNOWFLAKE_WAREHOUSE_MAP[{acct_id}]", wh_uuid)
            log.info("  Snowflake warehouse map: %d account(s) configured", len(sf_warehouse_map))

        _batch_size = _parse_positive_int("INGEST_BATCH_SIZE", 500)

    # Salesforce creds — required for all modes including --discover
    sf_instance_url  = _require("SF_ORG_URL")
    sf_client_id     = _require("SF_CLIENT_ID")
    sf_client_secret = _require("SF_CLIENT_SECRET")

    # SSRF guard: SF_ORG_URL must be HTTPS and must not resolve to a private/loopback IP
    _parsed_url = urlparse(sf_instance_url)
    if _parsed_url.scheme != "https" or not _parsed_url.hostname or "@" in (_parsed_url.netloc or ""):
        log.error(
            "SF_ORG_URL is not a valid HTTPS URL (got: %s). "
            "Expected format: https://myorg.my.salesforce.com",
            sf_instance_url,
        )
        sys.exit(1)
    if _parsed_url.path not in ("", "/"):
        log.error(
            "SF_ORG_URL must not contain a path component (got: %r). "
            "Expected format: https://myorg.my.salesforce.com",
            sf_instance_url,
        )
        sys.exit(1)
    try:
        _sf_addr = ipaddress.ip_address(_parsed_url.hostname)
        if (_sf_addr.is_private or _sf_addr.is_loopback or _sf_addr.is_link_local
                or _sf_addr.is_reserved or _sf_addr.is_multicast):
            log.error(
                "SF_ORG_URL hostname is a private/loopback IP (%s) — refusing to connect. "
                "SF_ORG_URL must be a Salesforce My Domain URL, not an internal address.",
                _parsed_url.hostname,
            )
            sys.exit(1)
    except ValueError:
        pass  # hostname is a domain name — IP-based SSRF check not applicable

    sf_svc = SalesforceService(sf_instance_url, sf_client_id, sf_client_secret)

    # Pre-initialize so references outside the try block are always defined
    streams: list = []
    edges: list = []
    skipped: list = []

    try:
        # Step 1
        sf_svc.authenticate()

        # Step 2
        streams = sf_svc.fetch_data_streams()

        # --discover: print connector-type summary and exit; no MC creds needed
        if args.discover:
            _run_discover(streams, run_id, sf_instance_url)
            sys.exit(0)

        if not streams:
            log.warning("No data streams returned from Salesforce — nothing to push.")
            sys.exit(0)

        mc_svc = MCLineageService(mcd_id, mcd_token, mcd_ingest_id, mcd_ingest_token)

        connector_types_present = {
            (s.get("connectorInfo") or {}).get("connectorType", "") for s in streams
        }
        log.info(
            "  Connector types present in streams: %s",
            ", ".join(sorted(ct for ct in connector_types_present if ct)) or "none",
        )

        need_crm = _CONNECTOR_SFDC in connector_types_present
        need_sf  = _CONNECTOR_SNOWFLAKE in connector_types_present

        # Auto-discover MC warehouse UUIDs for any not provided via env vars.
        # Matches by Salesforce org domain (CRM + Data Cloud) and Snowflake account ID.
        _need_discovery = (
            not dc_warehouse_uuid
            or (need_crm and not crm_warehouse_uuid and not crm_warehouse_map)
            or (need_sf and not sf_warehouse_map)
        )
        if _need_discovery:
            sf_org_domain = _parsed_url.hostname or ""
            log.info(
                "Step 2b (discovery): Auto-discovering MC warehouse UUIDs for org: %s",
                sf_org_domain,
            )
            _disc = _discover_warehouse_uuids(mcd_id, mcd_token, sf_org_domain)
            if not dc_warehouse_uuid and _disc["dc_uuid"]:
                dc_warehouse_uuid = _disc["dc_uuid"]
                _validate_uuid("MCD_DC_WAREHOUSE_UUID", dc_warehouse_uuid)
                log.info("  Discovered Data Cloud warehouse: %s", dc_warehouse_uuid)
            if need_crm and not crm_warehouse_uuid and not crm_warehouse_map and _disc["crm_uuid"]:
                crm_warehouse_uuid = _disc["crm_uuid"]
                _validate_uuid("discovered CRM warehouse", crm_warehouse_uuid)
                log.info("  Discovered CRM warehouse: %s", crm_warehouse_uuid)
            if need_sf and not sf_warehouse_map and _disc["snowflake_map"]:
                for _acct, _wh_uuid in _disc["snowflake_map"].items():
                    _validate_uuid(f"discovered Snowflake warehouse for {_acct}", _wh_uuid)
                sf_warehouse_map = _disc["snowflake_map"]
                log.info(
                    "  Discovered %d Snowflake warehouse(s): %s",
                    len(sf_warehouse_map), ", ".join(sf_warehouse_map),
                )

        if not dc_warehouse_uuid:
            log.error(
                "Could not determine the Data Cloud warehouse UUID. "
                "Set MCD_RESOURCE_UUID in your .env, or ensure your MC account has a "
                "Salesforce Data Cloud connection for this org (%s).",
                _parsed_url.hostname or sf_instance_url,
            )
            sys.exit(1)

        # Step 2d: Fetch connection details for all streams via MktDataConnection.
        # Covers Snowflake (account routing) and any future connector types.
        # Native same-org CRM connections will not have a MktDataConnection record
        # and are silently omitted.
        connection_details: dict = {}
        try:
            connection_details = sf_svc.fetch_connection_details(streams)
        except Exception as exc:
            if (need_sf and sf_warehouse_map) or (need_crm and crm_warehouse_map):
                log.error(
                    "  fetch_connection_details failed (%s). "
                    "Multi-warehouse routing is disabled — streams will fall back to single-UUID "
                    "config or be skipped entirely. This may produce incorrect lineage when "
                    "multiple CRM orgs or Snowflake accounts are in use. "
                    "Resolve the error above and re-run before pushing to production.",
                    exc,
                )
            else:
                log.warning(
                    "  Could not fetch connection details (%s). "
                    "Snowflake routing uses single-UUID fallback.",
                    exc,
                )

        # SF API calls are done — drop the token from memory
        sf_svc.invalidate_token()

        # Eager catalog filter: trim sf_warehouse_map to only accounts actually referenced by
        # streams. Auto-discovery may return every Snowflake warehouse connected to MC; without
        # this filter we'd fetch a catalog for each one even if only one account is in use.
        if need_sf and sf_warehouse_map and connection_details:
            referenced_accounts = {
                info.get("account_identifier", "").lower()
                for info in connection_details.values()
                if info.get("account_identifier")
            }
            if referenced_accounts:
                pruned = {k: v for k, v in sf_warehouse_map.items() if k in referenced_accounts}
                dropped = set(sf_warehouse_map) - set(pruned)
                if dropped:
                    log.info(
                        "  Snowflake warehouse map pruned to %d account(s) referenced by streams "
                        "(dropped unreferenced: %s)",
                        len(pruned), ", ".join(sorted(dropped)),
                    )
                sf_warehouse_map = pruned

        # Step 3: Fetch catalogs for the warehouses we need
        t3 = time.monotonic()
        log.info("Step 3: Fetching MC catalog(s)")
        dc_idx = mc_svc.fetch_catalog(dc_warehouse_uuid, "Data Cloud")

        crm_idx: Optional[_CatalogIndex] = None
        crm_catalogs_built: dict = {}
        if need_crm:
            if crm_warehouse_map:
                for conn_id, wh_uuid in crm_warehouse_map.items():
                    label = f"Salesforce CRM(connector={conn_id})"
                    catalog = mc_svc.fetch_catalog(wh_uuid, label)
                    crm_catalogs_built[conn_id] = (catalog, wh_uuid)
            if crm_warehouse_uuid:
                crm_idx = mc_svc.fetch_catalog(crm_warehouse_uuid, "Salesforce CRM")
            elif not crm_warehouse_map:
                log.warning(
                    "  CRM streams detected but no CRM warehouse was found in MC for this org. "
                    "CRM→DLO edges will be skipped. Ensure a Salesforce CRM connection exists in MC "
                    "for this org, or set MCD_CRM_WAREHOUSE_UUID as an override."
                )

        # Build sf_catalogs: {account_identifier → (_CatalogIndex, warehouse_uuid)}
        # Covers all accounts referenced in the warehouse map.
        sf_idx: Optional[_CatalogIndex] = None
        sf_catalogs: dict = {}
        if need_sf:
            if sf_warehouse_map:
                for acct_id, wh_uuid in sf_warehouse_map.items():
                    label = f"Snowflake({acct_id})"
                    catalog = mc_svc.fetch_catalog(wh_uuid, label)
                    sf_catalogs[acct_id] = (catalog, wh_uuid)
            if snowflake_warehouse_uuid and not sf_warehouse_map:
                # Single-warehouse fallback — also pre-fetch for any unmapped streams
                sf_idx = mc_svc.fetch_catalog(snowflake_warehouse_uuid, "Snowflake")
            elif snowflake_warehouse_uuid:
                # Both map and fallback set — fetch fallback catalog too
                sf_idx = mc_svc.fetch_catalog(snowflake_warehouse_uuid, "Snowflake (fallback)")
            elif not sf_warehouse_map:
                log.warning(
                    "  Snowflake streams detected but no Snowflake warehouse was found in MC. "
                    "Snowflake→DLO edges will be skipped. Ensure a Snowflake connection exists in MC, "
                    "or set MCD_SNOWFLAKE_WAREHOUSE_UUID as an override."
                )

        log.info("  Step 3 complete (%.1fs)", time.monotonic() - t3)

        # Step 4
        edges, skipped = resolve_edges(
            streams=streams,
            dc_idx=dc_idx,
            crm_idx=crm_idx,
            sf_idx=sf_idx,
            dc_warehouse_uuid=dc_warehouse_uuid,
            crm_warehouse_uuid=crm_warehouse_uuid,
            snowflake_warehouse_uuid=snowflake_warehouse_uuid,
            connection_details=connection_details,
            sf_catalogs=sf_catalogs,
            crm_catalogs=crm_catalogs_built,
        )

        # Deduplicate: multiple Data Streams can share the same source→DLO pair
        seen_pairs: set = set()
        deduped: list = []
        for _e in edges:
            _pair = (_e["source_full_id"], _e["dest_full_id"])
            if _pair not in seen_pairs:
                seen_pairs.add(_pair)
                deduped.append(_e)
            else:
                log.debug("  Duplicate edge suppressed: %s → %s", _e["source_label"], _e["dlo_name"])
        edges = deduped

        # Save full DSO details for every skipped stream so they can be
        # reviewed, manually linked to MC assets, or used to diagnose mismatches.
        if skipped:
            try:
                skip_path = _save_skipped_streams(run_id, skipped)
                log.info(
                    "  %d skipped stream(s) with full DSO details saved to: %s",
                    len(skipped), skip_path,
                )
            except OSError as exc:
                log.warning(
                    "  Could not write skipped-streams file (%s). "
                    "Run with LOG_LEVEL=DEBUG to see raw stream data in the log.",
                    exc,
                )

    except (RuntimeError, requests.exceptions.RequestException) as exc:
        log.error("Fatal error during data collection: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.exception("Unexpected error during data collection: %s", exc)
        sys.exit(2)

    log.info("Resolved edges (%d total):", len(edges))
    for e in edges:
        log.info("  [%s] %s → %s", e["connector_type"], e["source_label"], e["dlo_name"])

    if not edges:
        log.warning(
            "0 edges ready to push. "
            "If source or DLO tables are missing from the MC catalog, trigger a metadata sync "
            "(Settings → Integrations → your connection) then re-run. "
            "The push is idempotent — re-running is safe once tables appear."
        )
        sys.exit(0)

    if args.dry_run:
        _by_type: dict = {}
        for _e in edges:
            _by_type[_e["connector_type"]] = _by_type.get(_e["connector_type"], 0) + 1
        _breakdown = ", ".join(f"{ct}={n}" for ct, n in sorted(_by_type.items()))
        log.info(
            "[dry-run] Would push %d edge(s) (%s); %d stream(s) skipped. "
            "Elapsed: %.1fs. Run without --dry-run to commit.",
            len(edges), _breakdown or "none", len(skipped), time.monotonic() - t_start,
        )
        sys.exit(0)

    try:
        pushed_count = mc_svc.push_edges(edges, run_id, _batch_size, _shutdown_requested)
    except KeyboardInterrupt:
        log.warning("Interrupted during push — partial results may have been written to Monte Carlo.")
        sys.exit(1)
    except Exception as exc:
        log.exception("Unexpected error during push: %s", exc)
        sys.exit(2)

    elapsed = time.monotonic() - t_start

    # Exit 1 if every edge failed — treat as a systemic error, not partial success
    if pushed_count == 0 and len(edges) > 0:
        log.error(
            "=== Run failed | run_id=%s | %.1fs | streams=%d | edges_resolved=%d | "
            "edges_pushed=0 | skipped=%d ===",
            run_id, elapsed, len(streams), len(edges), len(skipped),
        )
        sys.exit(1)

    log.info(
        "=== Run complete | run_id=%s | %.1fs | streams=%d | edges_resolved=%d | "
        "edges_pushed=%d | skipped=%d ===",
        run_id, elapsed, len(streams), len(edges), pushed_count, len(skipped),
    )


if __name__ == "__main__":
    main()

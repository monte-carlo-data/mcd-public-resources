#!/usr/bin/env python3
"""
Data 360 Lineage Push — DLO→DMO and DMO→CIO (Pandora Push Model)

Pipeline:
  1. Authenticate with Salesforce via OAuth client credentials
  2. Fetch Salesforce data spaces (for data-space resolution fallback)
  3. Fetch the Monte Carlo catalog and enumerate the catalogued DMOs
  4. Retrieve DLO->DMO mappings via the Data Cloud REST API — one read-only GET per
     catalogued DMO (no SOAP Metadata API, no 'Modify Metadata' permission required)
  4b. Validate tables exist in MC catalog; MC catalog dataset field is authoritative
      for data space
  5. Fetch Calculated Insight Objects (CIOs) via Data Cloud REST API
  6. Parse DMO->CIO edges by extracting table references from each CIO's SQL expression
  7. Push DLO->DMO lineage to Monte Carlo via pycarlo IngestionService
  8. Push DMO->CIO lineage to Monte Carlo via pycarlo IngestionService

Usage:
  python3 push_lineage.py --dry-run
  python3 push_lineage.py

Required env vars (see .env.example):
  SF_ORG_URL, SF_CLIENT_ID, SF_CLIENT_SECRET
  MCD_INGEST_ID, MCD_INGEST_TOKEN   — Ingestion key (push lineage)
  MCD_ID, MCD_TOKEN                 — Personal API key (catalog validation)
  MCD_RESOURCE_UUID                 — Monte Carlo Data Cloud warehouse UUID

Optional env vars:
  LOG_LEVEL              — DEBUG/INFO/WARNING/ERROR (default: INFO)
  LOG_FORMAT             — json for structured output (default: plain)
  INGEST_BATCH_SIZE      — edges per push batch (default: 500)
  MAPPING_MAX_WORKERS    — parallel per-DMO mapping fetches (default: 10)
  SF_DEFAULT_DATA_SPACE  — fallback data space name (default: default)
"""
import argparse
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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
    sys.exit(
        f"ERROR: pycarlo is not installed. Run: pip install pycarlo>=0.12.251\n  ({err})"
    )

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ── Constants ─────────────────────────────────────────────────────────────────
SF_API_VERSION = "62.0"
DB = "salesforce-data-cloud"
DC_RESOURCE_TYPE = "salesforce-data-cloud"
MC_GRAPHQL_URL = "https://api.getmontecarlo.com/graphql"
MC_INGEST_URL = "https://integrations.getmontecarlo.com"


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        val = int(raw)
        if val < 1:
            raise ValueError("must be >= 1")
        return val
    except ValueError as exc:
        sys.exit(f"Invalid {name}={raw!r}: {exc}")


INGEST_BATCH_SIZE   = _parse_positive_int("INGEST_BATCH_SIZE", 500)
MAPPING_MAX_WORKERS = _parse_positive_int("MAPPING_MAX_WORKERS", 10)

# Safety ceiling on paginated API calls to prevent infinite loops if the API
# returns a non-null next-page cursor indefinitely (server bug or misconfiguration).
MAX_CIO_PAGES     = 1_000
MAX_CATALOG_PAGES = 5_000

# Matches any Data Cloud object name (DMO or CIO) anywhere in SQL text.
# Scanning the full expression — rather than only FROM/JOIN clauses — means
# subqueries, CTEs, and any other SQL structure are handled automatically.
# The suffixes __dlm (DMO) and __cio (CIO) are unique to table objects;
# fields use __c, so false positives are not a concern.
_DATA_OBJECT_RE = re.compile(r"\b([A-Za-z0-9_]+(?:__dlm|__cio))\b", re.IGNORECASE)


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

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RunIdFilter(run_id))

    if os.environ.get("LOG_FORMAT", "").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        fmt = "[%(asctime)s] [%(levelname)-7s] [run=%(run_id)s] %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    logger = logging.getLogger("push_lineage")
    logger.setLevel(level)
    logger.handlers = [handler]
    logger.propagate = False
    return logger


# Module-level placeholder — replaced in main() before any function is called
log: logging.Logger = logging.getLogger("push_lineage")


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


def _is_retryable(exc: BaseException) -> bool:
    """Suppress retry for 4xx HTTP errors — they won't resolve on retry."""
    resp = getattr(exc, "response", None)
    if resp is not None and 400 <= resp.status_code < 500:
        return False
    return True


_retrying = retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)


def _save_failed_edges(run_id: str, kind: str, edges: list) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(__file__).parent / f"failed_edges_{kind}_{run_id}_{ts}.json"
    content = json.dumps(edges, indent=2).encode()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, content)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    log.info("  Failed edges saved to: %s", path.resolve())
    return str(path)


def _format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs:02d}s"


_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-]{60,}")


def _parse_sql_inputs(sql: str, cio_api_name: str = "") -> set:
    """
    Extract DMO (__dlm) and CIO (__cio) table references from a CIO SQL expression
    by scanning the full SQL text. This handles subqueries, CTEs, schema-qualified
    names, and any SQL structure — no clause-level parsing required.
    The CIO's own api_name is excluded so self-references don't appear as inputs.
    Results are lowercased to match MC catalog lookup keys and avoid mixed-case
    pushes when SQL uses non-canonical casing (e.g. ACCOUNT__DLM vs Account__dlm).
    """
    found = {name.lower() for name in _DATA_OBJECT_RE.findall(sql)}
    if cio_api_name:
        found.discard(cio_api_name.lower())
    return found


def _safe_snippet(text: str, max_len: int = 200) -> str:
    """Truncate and redact long token-like strings before logging API response bodies."""
    cleaned = _TOKEN_RE.sub("[…]", text or "")
    return cleaned[:max_len]


def _asset_type(name: str) -> str:
    """
    Monte Carlo objectType for a Data Cloud object, by suffix.

    The Data Cloud connector catalogs DLOs (__dll) as `table` and DMOs (__dlm) /
    CIOs (__cio) as `view` (verified live against the catalog). LineageAssetRef.type
    MUST match the catalogued objectType, or the edge lands on a phantom duplicate
    node instead of the real one — a TABLE-typed push to a view-catalogued DMO
    creates a disconnected 'table' node next to the real 'view' (confirmed live).
    """
    return "TABLE" if name.endswith("__dll") else "VIEW"


# ── Salesforce service ────────────────────────────────────────────────────────
class SalesforceDataCloudService:
    """Encapsulates Salesforce authentication and metadata retrieval."""

    def __init__(self, instance_url: str, client_id: str, client_secret: str) -> None:
        self.instance_url = instance_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str = ""

    @property
    def token(self) -> str:
        if not self._token:
            raise RuntimeError("Not authenticated — call authenticate() first.")
        return self._token

    @_retrying
    def authenticate(self) -> None:
        t0 = time.monotonic()
        log.info("Step 1: Authenticating with Salesforce (%s)", self.instance_url)
        resp = requests.post(
            f"{self.instance_url}/services/oauth2/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            verify=True,
            timeout=30,
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
                f"Salesforce auth failed: {data.get('error')} — "
                f"{(data.get('error_description') or '')[:200]}"
            )
        resp.raise_for_status()
        self._token = data["access_token"]
        log.info("  Authenticated (%.1fs)", time.monotonic() - t0)

    def _reauthenticate(self) -> None:
        resp = requests.post(
            f"{self.instance_url}/services/oauth2/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            verify=True,
            timeout=30,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Salesforce re-auth returned non-JSON response (status={resp.status_code})"
            )
        token = data.get("access_token")
        if not token:
            raise RuntimeError(
                f"Salesforce re-auth response missing access_token (status={resp.status_code})"
            )
        self._token = token

    def invalidate_token(self) -> None:
        self._token = ""
        self.client_secret = ""  # drop credential reference when all API calls are complete

    @_retrying
    def _fetch_dataspaces_raw(self) -> requests.Response:
        resp = requests.get(
            f"{self.instance_url}/services/data/v{SF_API_VERSION}/query",
            headers={"Authorization": f"Bearer {self._token}"},
            params={"q": "SELECT DataSpaceApiName FROM Dataspace"},
            verify=True,
            timeout=30,
        )
        # Raise on 5xx to trigger retry; 4xx (403) is handled gracefully by caller
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    def get_dataspaces(self) -> list:
        t0 = time.monotonic()
        log.info("Step 2: Fetching Salesforce data spaces")
        try:
            resp = self._fetch_dataspaces_raw()
        except (requests.exceptions.RequestException, RuntimeError) as exc:
            log.warning(
                "  Dataspace query failed: %s — using 'default' as fallback", exc
            )
            return ["default"]

        if resp.status_code == 200:
            records = resp.json().get("records", [])
            spaces = [r["DataSpaceApiName"] for r in records if r.get("DataSpaceApiName")]
            if spaces:
                log.info(
                    "  Found %d data space(s): %s (%.1fs)",
                    len(spaces), spaces, time.monotonic() - t0,
                )
                return spaces
            log.warning("  Dataspace query returned no records — using 'default' as fallback")
            return ["default"]
        if resp.status_code == 403:
            log.warning(
                "  Dataspace query returned 403 Forbidden — the connected app may lack "
                "'Manage Data Cloud' or 'API Access' permissions. "
                "Salesforce error: %s. Falling back to 'default' — edge data spaces are "
                "still resolved from the MC catalog dataset field during validation.",
                _safe_snippet(resp.text),
            )
        else:
            log.warning(
                "  Could not query Dataspace object (status=%d, body=%s) — using 'default' as fallback",
                resp.status_code, _safe_snippet(resp.text),
            )
        return ["default"]

    @_retrying
    def _fetch_dmo_mappings(self, dmo_developer_name: str) -> list:
        """
        Fetch DLO->DMO source/target mappings for one DMO via the Data Cloud REST API.

        Read-only: uses the same OAuth/API scope as the other /ssot endpoints, with NO
        SOAP Metadata API and NO 'Modify Metadata' permission. A 404 (NOT_FOUND) means
        the DMO has no source mappings and is returned as an empty list (normal — most
        installed standard DMOs are unmapped).

        dmoDeveloperName is matched case-insensitively by the endpoint: verified live that
        the lowercased MC-catalog key (e.g. 'ssot__account__dlm') resolves identically to
        the canonical-case name ('ssot__Account__dlm'), and the extracted edge count matches
        the legacy SOAP path exactly — so passing the catalog's lowercased DMO name here
        does not drop any mappings.
        """
        resp = requests.get(
            f"{self.instance_url}/services/data/v{SF_API_VERSION}/ssot/data-model-object-mappings",
            headers={"Authorization": f"Bearer {self._token}"},
            params={"dmoDeveloperName": dmo_developer_name},
            verify=True,
            timeout=30,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Non-JSON response from data-model-object-mappings for "
                f"{dmo_developer_name!r} (HTTP {resp.status_code}): {_safe_snippet(resp.text)}"
            ) from exc
        return body.get("objectSourceTargetMaps") or []

    def fetch_dlo_dmo_edges(self, dmo_specs: list) -> list:
        """
        Fetch DLO->DMO lineage edges via the Data Cloud REST mapping endpoint.

        `dmo_specs` is a list of (dmo_developer_name, preliminary_data_space) tuples —
        the DMOs Monte Carlo actually catalogs — so we issue one read-only GET per
        catalogued DMO rather than enumerating every installed standard DMO.

        Replaces the former SOAP Metadata API retrieve of ObjectSourceTargetMap. The
        REST record ({sourceEntityDeveloperName, targetEntityDeveloperName, status})
        carries identical DLO->DMO information with no 'Modify Metadata' permission.
        Data space here is preliminary; validate_edges() resolves the authoritative
        value from the MC catalog dataset field.
        """
        t0 = time.monotonic()
        log.info(
            "Step 4: Retrieving DLO->DMO mappings via Data Cloud REST API "
            "(read-only; %d catalogued DMO(s))",
            len(dmo_specs),
        )
        if not dmo_specs:
            log.warning("  No catalogued DMOs to query — 0 DLO->DMO edges extracted.")
            return []

        edges: list = []
        seen: set = set()
        mapped = 0
        errors = 0

        max_workers = min(MAPPING_MAX_WORKERS, len(dmo_specs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_spec = {
                pool.submit(self._fetch_dmo_mappings, name): (name, space)
                for name, space in dmo_specs
            }
            for fut in concurrent.futures.as_completed(future_to_spec):
                dmo_name, prelim_space = future_to_spec[fut]
                try:
                    maps = fut.result()
                except Exception as exc:  # noqa: BLE001 — one DMO must not abort the run
                    errors += 1
                    log.warning("  Mapping fetch failed for DMO '%s': %s", dmo_name, exc)
                    continue
                if not maps:
                    continue
                mapped += 1
                for m in maps:
                    source = m.get("sourceEntityDeveloperName") or ""
                    target = m.get("targetEntityDeveloperName") or ""
                    if not (source.endswith("__dll") and target.endswith("__dlm")):
                        continue
                    key = (source.lower(), target.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append({"source": source, "target": target, "data_space": prelim_space})

        log.info(
            "  %d DLO->DMO edge(s) extracted from %d mapped DMO(s) (%.1fs)",
            len(edges), mapped, time.monotonic() - t0,
        )
        if errors:
            log.warning(
                "  %d DMO mapping fetch(es) errored and were skipped — edges for those "
                "DMOs may be missing. Re-run (idempotent) once the transient issue clears.",
                errors,
            )
        return edges

    def _validate_same_origin(self, url: str) -> str:
        """Return absolute URL, rejecting any nextPageUrl that crosses origin or uses non-HTTPS."""
        # Salesforce sometimes returns nextPageUrl with null-valued params (e.g.
        # definitionType=null) which it then rejects on the subsequent request.
        _p = urlparse(url)
        _clean_qs = urlencode(
            {k: [x for x in v if x not in ("null", "")]
             for k, v in parse_qs(_p.query, keep_blank_values=True).items()
             if any(x not in ("null", "") for x in v)},
            doseq=True,
        )
        url = urlunparse(_p._replace(query=_clean_qs))
        parsed_base = urlparse(self.instance_url)
        parsed = urlparse(url)
        if not parsed.scheme:
            # Relative path — must start with / to prevent user-info injection
            # (e.g. '@evil.com/...' would make the hostname the attacker's domain).
            if not url.startswith("/"):
                raise RuntimeError(
                    f"CIO pagination nextPageUrl is a relative path that does not start with '/': {url!r}"
                )
            return f"{self.instance_url.rstrip('/')}{url}"
        if parsed.scheme != "https":
            raise RuntimeError(
                f"CIO pagination nextPageUrl uses scheme '{parsed.scheme}' — only HTTPS is permitted."
            )
        if parsed.hostname != parsed_base.hostname:
            raise RuntimeError(
                f"CIO pagination nextPageUrl hostname '{parsed.hostname}' does not match "
                f"SF_ORG_URL hostname '{parsed_base.hostname}' — aborting to prevent SSRF."
            )
        if parsed.port != parsed_base.port:
            raise RuntimeError(
                f"CIO pagination nextPageUrl port '{parsed.port}' does not match "
                f"SF_ORG_URL port '{parsed_base.port}' — aborting to prevent SSRF."
            )
        return url

    @_retrying
    def _fetch_cio_page(self, url: str) -> dict:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            verify=True,
            timeout=30,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(
                f"Non-JSON response from CIO endpoint (HTTP {resp.status_code}): "
                f"{_safe_snippet(resp.text)}"
            )

    def fetch_calculated_insights(self) -> list:
        """Fetch all CIOs from the Data Cloud REST API, paginating via nextPageUrl."""
        t0 = time.monotonic()
        log.info("Step 5: Fetching Calculated Insight Objects (CIOs) from Salesforce")
        items = []
        url: Optional[str] = (
            f"{self.instance_url}/services/data/v{SF_API_VERSION}/ssot/calculated-insights"
        )
        page = 0
        while url:
            page += 1
            if page > MAX_CIO_PAGES:
                raise RuntimeError(
                    f"CIO pagination exceeded {MAX_CIO_PAGES} pages — possible infinite loop. "
                    "Check Salesforce API for a looping nextPageUrl."
                )
            for attempt in range(1, 5):
                try:
                    data = self._fetch_cio_page(url)
                    break
                except requests.exceptions.HTTPError as exc:
                    if (exc.response is not None
                            and exc.response.status_code == 401
                            and attempt <= 3):
                        log.warning(
                            "  Token expired on CIO page %d — re-authenticating (attempt %d/3)...",
                            page, attempt,
                        )
                        self._reauthenticate()
                    else:
                        raise
            collection = data.get("collection", {})
            batch = collection.get("items", [])
            items.extend(batch)
            log.debug("  CIO page %d: %d item(s)", page, len(batch))
            next_url = collection.get("nextPageUrl") or None
            url = self._validate_same_origin(next_url) if next_url else None
        log.info("  Found %d CIO(s) (%.1fs)", len(items), time.monotonic() - t0)
        return items

    def parse_cio_edges(self, cio_items: list) -> list:
        """
        Parse DMO→CIO and CIO→CIO edges from each CIO's SQL expression.
        Inputs are found by scanning the full SQL expression for __dlm/__cio tokens:
          __dlm suffix → DMO input
          __cio suffix → chained CIO input
        Data space comes directly from the CIO object (no fallback needed).
        """
        t0 = time.monotonic()
        log.info("Step 6: Parsing DMO->CIO edges from CIO SQL expressions")
        edges = []
        skipped = 0
        processed_cio_count = 0

        for cio in cio_items:
            api_name = cio.get("apiName", "")
            if not api_name.endswith("__cio"):
                log.debug("  Skipping non-CIO object: %s", api_name)
                continue
            processed_cio_count += 1
            data_space = cio.get("dataSpace") or "default"
            sql = cio.get("expression") or ""
            if not sql:
                log.warning("  CIO '%s' has no SQL expression — skipping", api_name)
                skipped += 1
                continue
            inputs = _parse_sql_inputs(sql, cio_api_name=api_name)
            log.debug(
                "  CIO '%s': SQL length=%d, inputs found=%s",
                api_name, len(sql), sorted(inputs) or "(none)",
            )
            if not inputs:
                log.warning(
                    "  CIO '%s': no __dlm or __cio inputs found in SQL expression — skipping. "
                    "Run with LOG_LEVEL=DEBUG to inspect the full expression.",
                    api_name,
                )
                skipped += 1
                continue
            target = api_name.lower()
            for src in sorted(inputs):
                edges.append({"source": src, "target": target, "data_space": data_space})

        log.info(
            "  %d DMO->CIO edge(s) extracted from %d CIO(s) (%.1fs)",
            len(edges), processed_cio_count - skipped, time.monotonic() - t0,
        )
        if skipped:
            log.warning("  Skipped %d CIO(s) (no expression or no recognized inputs)", skipped)
        return edges


# ── Monte Carlo lineage service ───────────────────────────────────────────────
class SalesforceDataCloudLineageService:
    """Encapsulates MC catalog validation and lineage push."""

    def __init__(
        self,
        resource_uuid: str,
        ingest_key_id: str,
        ingest_key_secret: str,
        gql_key_id: str,
        gql_key_secret: str,
    ) -> None:
        self.resource_uuid = resource_uuid
        self.ingest_key_id = ingest_key_id
        self.ingest_key_secret = ingest_key_secret
        self.gql_key_id = gql_key_id
        self.gql_key_secret = gql_key_secret

    @_retrying
    def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = requests.post(
            MC_GRAPHQL_URL,
            json=payload,
            headers={
                "x-mcd-id":     self.gql_key_id,
                "x-mcd-token":  self.gql_key_secret,
                "Content-Type": "application/json",
            },
            timeout=30,
            verify=True,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"MC GraphQL returned non-JSON response (status={resp.status_code}). "
                f"Response preview: {_safe_snippet(resp.text)}"
            ) from exc
        if "errors" in body:
            msgs = [e.get("message", str(e)) for e in body["errors"]]
            raise RuntimeError(
                f"MC GraphQL error: {'; '.join(msgs)}. "
                "Check that MCD_ID/MCD_TOKEN are a Personal API key (not an Ingestion key)."
            )
        return body["data"]

    def _fetch_mc_catalog(self) -> dict:
        """
        Fetch ALL tables for the warehouse from MC in one paginated bulk query.
        Scoping by dwId prevents cross-org contamination when multiple Data Cloud orgs
        are connected to the same MC account.
        Returns dict of {table_name_lower → data_space_or_list}.
        """
        catalog: dict = {}
        after = None
        page = 0

        while True:
            page += 1
            if page > MAX_CATALOG_PAGES:
                raise RuntimeError(
                    f"MC catalog pagination exceeded {MAX_CATALOG_PAGES} pages — possible infinite loop. "
                    "Check MC GraphQL API for a looping endCursor."
                )
            variables: dict = {"dwId": self.resource_uuid}
            if after:
                variables["after"] = after

            data = self._gql(
                """
                query GetAllTables($dwId: UUID, $after: String) {
                  getTables(first: 200, dwId: $dwId, after: $after) {
                    edges { node { fullTableId dataset } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                variables=variables,
            )

            result = data.get("getTables") or {}
            for edge in (result.get("edges") or []):
                node = edge.get("node") or {}
                raw_id = node.get("fullTableId") or ""
                if not raw_id.lower().startswith(DB + ":") or "." not in raw_id:
                    continue
                table_name = raw_id.rsplit(".", 1)[1].lower()
                # dataset field is authoritative; fall back to parsing fullTableId
                data_space = node.get("dataset") or raw_id.split(":", 1)[1].rsplit(".", 1)[0]
                if table_name in catalog:
                    existing = catalog[table_name]
                    if isinstance(existing, list):
                        if data_space not in existing:
                            existing.append(data_space)
                    elif existing != data_space:
                        catalog[table_name] = [existing, data_space]
                else:
                    catalog[table_name] = data_space

            log.info("  Catalog fetch page %d: %d table(s) retrieved so far", page, len(catalog))

            page_info = result.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                log.warning(
                    "  MC catalog hasNextPage=true but endCursor is null — "
                    "stopping pagination to avoid infinite loop"
                )
                break

        return catalog

    def _resolve_catalog(
        self,
        catalog: dict,
        table_name: str,
        preferred_data_space: Optional[str] = None,
    ) -> Optional[str]:
        """Return data_space for table_name, or None if not in catalog."""
        entry = catalog.get(table_name.lower())
        if entry is None:
            return None
        if isinstance(entry, list):
            if preferred_data_space and preferred_data_space in entry:
                return preferred_data_space
            # Catalogued in multiple data spaces and the mapping's space can't be
            # disambiguated — return None so the caller skips the edge rather than
            # guessing a data space (a wrong-space edge is worse than a missing one).
            log.warning(
                "  Table '%s' is catalogued in multiple data spaces %s and the mapping's "
                "data space could not be disambiguated — skipping its edge(s). Set "
                "SF_DEFAULT_DATA_SPACE to the intended data space to resolve.",
                table_name, entry,
            )
            return None
        return entry

    def fetch_catalog(self) -> dict:
        """
        Fetch all tables for the warehouse from MC (bulk, paginated, scoped by dwId).
        Fetched once and reused for both DMO enumeration and edge validation.
        """
        log.info("Step 3: Fetching Monte Carlo catalog for warehouse %s", self.resource_uuid)
        t0 = time.monotonic()
        try:
            mc_catalog = self._fetch_mc_catalog()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise RuntimeError(
                f"MC catalog fetch returned HTTP {status}. Check MCD_ID/MCD_TOKEN."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Network error fetching MC catalog: {exc}. "
                "Check connectivity to https://api.getmontecarlo.com"
            ) from exc
        log.info(
            "  Fetched %d table(s) from MC catalog (%.1fs)",
            len(mc_catalog), time.monotonic() - t0,
        )
        return mc_catalog

    def catalogued_dmos(self, mc_catalog: dict, fallback_dataspace: str) -> list:
        """
        Return [(dmo_developer_name, preliminary_data_space)] for every DMO (__dlm) in
        the MC catalog — the set of DMOs to query for DLO->DMO mappings. Iterating the
        connector's own catalogued DMOs (rather than the full installed-DMO list) keeps
        the REST fetch to one GET per relevant DMO. The preliminary data space is the
        DMO's catalog dataset (authoritative); validate_edges confirms it downstream.
        """
        specs = []
        for name, entry in mc_catalog.items():
            if not name.endswith("__dlm"):
                continue
            space = entry if isinstance(entry, str) else fallback_dataspace
            specs.append((name, space))
        log.info("  %d catalogued DMO(s) to query for DLO->DMO mappings", len(specs))
        return specs

    def validate_edges(self, edges: list, mc_catalog: dict) -> list:
        """
        Validate each edge against the (pre-fetched) MC catalog.
        Resolves the DMO's authoritative data space first, then resolves the DLO
        preferring the DMO's data space — so DLOs shared across multiple data spaces
        (e.g. a DLO added to both 'default' and 'unified_knowledge') resolve to the
        correct pairing rather than the fallback. Edges where either table is missing
        from the MC catalog, or where the DLO is not available in the DMO's data space,
        are removed and logged. Returns the validated subset ready for push.
        """
        if not edges:
            return edges

        log.info("Step 4b: Validating DLO and DMO tables exist in Monte Carlo catalog")
        t0 = time.monotonic()

        unique_names = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
        missing_set: set = set()

        for name in unique_names:
            entry = mc_catalog.get(name.lower())
            if entry is not None:
                ds = entry[0] if isinstance(entry, list) else entry
                log.debug("  Found in MC catalog: %s (data space: %s)", name, ds)
            else:
                missing_set.add(name)
                log.warning(
                    "  NOT in MC catalog: %s — edge(s) using this table will be skipped. "
                    "Confirm the Data Cloud connector has completed a metadata scan in Monte Carlo.",
                    name,
                )

        matched = len(unique_names) - len(missing_set)
        log.info(
            "  Catalog check complete: %d matched, %d not in catalog (%.1fs)",
            matched, len(missing_set), time.monotonic() - t0,
        )

        if missing_set:
            log.warning("  Table(s) not in MC catalog: %s", ", ".join(sorted(missing_set)))
            log.warning(
                "  Lineage edges for these tables will not be pushed. "
                "Once the Data Cloud connector syncs them, re-run this script "
                "to push their edges (the push is idempotent — already-pushed edges are safe to re-send)."
            )

        valid = []
        catalog_skips = 0
        mismatch_skips = 0
        for e in edges:
            preferred = e.get("data_space")
            # Resolve DMO first to get its authoritative data space, then resolve
            # the DLO preferring the DMO's data space. DLOs can be explicitly shared
            # across data spaces in Salesforce; a DLO in both 'default' and
            # 'unified_knowledge' should pair with the DMO's space, not a fallback value.
            tgt_space = self._resolve_catalog(mc_catalog, e["target"], preferred_data_space=preferred)
            if tgt_space is None:
                catalog_skips += 1
                continue
            src_space = self._resolve_catalog(mc_catalog, e["source"], preferred_data_space=tgt_space)
            if src_space is None:
                catalog_skips += 1
                continue
            if src_space != tgt_space:
                log.warning(
                    "  Data space mismatch: %s is in '%s' but %s is in '%s' — skipping edge",
                    e["source"], src_space, e["target"], tgt_space,
                )
                mismatch_skips += 1
                continue
            valid.append({**e, "data_space": src_space})

        if catalog_skips:
            log.warning("  %d edge(s) skipped (table(s) not yet in MC catalog)", catalog_skips)
        if mismatch_skips:
            log.warning("  %d edge(s) skipped (DLO/DMO data space mismatch in MC catalog)", mismatch_skips)
        if valid:
            log.info("  %d edge(s) ready to push", len(valid))
        return valid

    @_retrying
    def _send_batch(self, svc: IngestionService, events: list) -> object:
        return svc.send_lineage(
            resource_uuid=self.resource_uuid,
            resource_type=DC_RESOURCE_TYPE,
            events=events,
        )

    def push_edges(
        self,
        edges: list,
        run_id: str,
        step_label: str = "Step 7",
        edge_kind: str = "dlo_dmo",
    ) -> list:
        t0 = time.monotonic()
        log.info("%s: Pushing %d %s edge(s) via Ingest API", step_label, len(edges), edge_kind.replace("_", "->").upper())

        svc = IngestionService(mc_client=Client(session=Session(
            mcd_id=self.ingest_key_id,
            mcd_token=self.ingest_key_secret,
            scope="Ingestion",
            endpoint=MC_INGEST_URL,
        )))

        events = [
            LineageEvent(
                destination=LineageAssetRef(
                    type=_asset_type(e["target"]),
                    database=DB,
                    schema=e["data_space"],
                    name=e["target"],
                ),
                sources=[LineageAssetRef(
                    type=_asset_type(e["source"]),
                    database=DB,
                    schema=e["data_space"],
                    name=e["source"],
                )],
            )
            for e in edges
        ]

        invocation_ids: list = []
        failures: list = []
        total_batches = (len(events) + INGEST_BATCH_SIZE - 1) // INGEST_BATCH_SIZE

        for i in range(0, len(events), INGEST_BATCH_SIZE):
            batch_events = events[i : i + INGEST_BATCH_SIZE]
            batch_edges  = edges[i : i + INGEST_BATCH_SIZE]
            batch_num    = i // INGEST_BATCH_SIZE + 1
            try:
                resp = self._send_batch(svc, batch_events)
                inv_id = svc.extract_invocation_id(resp)
                if inv_id is None:
                    log.warning(
                        "  Batch %d/%d: push accepted but no invocation_id returned",
                        batch_num, total_batches,
                    )
                invocation_ids.append(inv_id)
                log.info(
                    "  Batch %d/%d: %d edge(s) pushed — invocation_id=%s",
                    batch_num, total_batches, len(batch_events), inv_id,
                )
            except Exception as exc:
                log.error("  Batch %d/%d failed: %s", batch_num, total_batches, exc)
                log.debug("  Batch %d/%d exception detail:", batch_num, total_batches, exc_info=True)
                failures.extend(batch_edges)

        if failures:
            path = _save_failed_edges(run_id, edge_kind, failures)
            label = edge_kind.replace("_", "->").upper()
            raise RuntimeError(
                f"{len(failures)} {label} edge(s) failed to push. "
                f"Failed edges saved for retry: {path}"
            )
        log.info(
            "  All %d %s edge(s) pushed (%.1fs)",
            len(edges), edge_kind.replace("_", "->").upper(), time.monotonic() - t0,
        )
        return invocation_ids


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    run_id = uuid.uuid4().hex[:8]
    global log
    log = _setup_logging(run_id)

    parser = argparse.ArgumentParser(
        description="Push Salesforce Data 360 DLO->DMO and DMO->CIO lineage to Monte Carlo"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch live data and preview edges, but skip the MC push",
    )
    parser.add_argument(
        "--skip-cio",
        action="store_true",
        help="Run DLO->DMO only; skip CIO fetch and DMO->CIO push",
    )
    args = parser.parse_args()

    def _handle_sigterm(signum, frame):  # noqa: ANN001
        log.warning("Received SIGTERM — shutting down. Partial lineage edges may have been pushed.")
        sys.exit(1)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    log.info(
        "=== Data 360 Lineage Push | run_id=%s%s%s ===",
        run_id,
        " [DRY RUN]" if args.dry_run else "",
        " [DLO->DMO only]" if args.skip_cio else "",
    )
    t_start = time.monotonic()

    sf_instance_url   = _require("SF_ORG_URL")
    sf_client_id      = _require("SF_CLIENT_ID")
    sf_client_secret  = _require("SF_CLIENT_SECRET")
    mcd_ingest_id     = _require("MCD_INGEST_ID")
    mcd_ingest_token  = _require("MCD_INGEST_TOKEN")
    mcd_id            = _require("MCD_ID")
    mcd_token         = _require("MCD_TOKEN")
    mcd_resource_uuid = _require("MCD_RESOURCE_UUID")
    try:
        uuid.UUID(mcd_resource_uuid)
    except ValueError:
        log.error(
            "MCD_RESOURCE_UUID is not a valid UUID format. "
            "Copy the value from Monte Carlo Settings → Integrations."
        )
        sys.exit(1)

    # SSRF guard: reject non-HTTPS, embedded credentials, private/reserved IPs (literal and DNS)
    _parsed_url = urlparse(sf_instance_url)
    if _parsed_url.scheme != "https" or not _parsed_url.hostname or "@" in (_parsed_url.netloc or ""):
        log.error(
            "SF_ORG_URL is not a valid HTTPS URL (got: %s). "
            "Expected format: https://myorg.my.salesforce.com",
            f"{_parsed_url.scheme}://{_parsed_url.hostname or '<missing>'}",
        )
        sys.exit(1)

    _hostname = _parsed_url.hostname
    if _hostname.lower() == "localhost":
        log.error("SF_ORG_URL hostname is 'localhost' — refusing to connect.")
        sys.exit(1)

    def _is_internal_ip(addr) -> bool:
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        )

    try:
        _sf_addr = ipaddress.ip_address(_hostname)
        if _is_internal_ip(_sf_addr):
            log.error(
                "SF_ORG_URL hostname is a private/loopback/reserved IP (%s) — refusing to connect. "
                "SF_ORG_URL must be a Salesforce My Domain URL, not an internal address.",
                _hostname,
            )
            sys.exit(1)
    except ValueError:
        # Domain name — resolve and check all returned IPs
        try:
            for _family, _type, _proto, _canonname, _sockaddr in socket.getaddrinfo(_hostname, None):
                _ip_str = _sockaddr[0]
                try:
                    _resolved = ipaddress.ip_address(_ip_str)
                    if _is_internal_ip(_resolved):
                        log.error(
                            "SF_ORG_URL hostname '%s' resolves to a private/reserved IP (%s) — "
                            "refusing to connect.",
                            _hostname, _ip_str,
                        )
                        sys.exit(1)
                except ValueError:
                    pass
        except socket.gaierror:
            pass  # DNS failure — let the actual request surface a clear network error

    sf_instance_url = sf_instance_url.rstrip("/")

    default_dataspace = os.environ.get("SF_DEFAULT_DATA_SPACE", "default")
    if not re.match(r"^[A-Za-z0-9_]{1,80}$", default_dataspace):
        log.error(
            "SF_DEFAULT_DATA_SPACE=%r is not a valid data space name. "
            "Use alphanumeric characters and underscores only (1–80 chars).",
            default_dataspace,
        )
        sys.exit(1)

    sf_svc = SalesforceDataCloudService(sf_instance_url, sf_client_id, sf_client_secret)
    lineage_svc = SalesforceDataCloudLineageService(
        resource_uuid=mcd_resource_uuid,
        ingest_key_id=mcd_ingest_id,
        ingest_key_secret=mcd_ingest_token,
        gql_key_id=mcd_id,
        gql_key_secret=mcd_token,
    )

    try:
        # Steps 1-2
        sf_svc.authenticate()

        data_spaces = sf_svc.get_dataspaces()
        # Single data space: use it directly. Multiple: the MC catalog's dataset field is
        # authoritative (resolved in validate_edges); default_dataspace
        # (SF_DEFAULT_DATA_SPACE) is only a last-resort preliminary value.
        fallback_space = data_spaces[0] if len(data_spaces) == 1 else default_dataspace
        if len(data_spaces) > 1:
            log.info(
                "  Multiple data spaces — DMOs the MC catalog can't place in a single "
                "data space will use '%s' as a preliminary value",
                fallback_space,
            )

        # Steps 3-4b: DLO→DMO via the read-only Data Cloud REST mapping endpoint.
        # Fetch the MC catalog once, enumerate the catalogued DMOs, GET each DMO's
        # mappings (no SOAP, no Modify Metadata), then validate against the same catalog.
        mc_catalog = lineage_svc.fetch_catalog()
        dmo_specs = lineage_svc.catalogued_dmos(mc_catalog, fallback_space)
        dlo_edges = sf_svc.fetch_dlo_dmo_edges(dmo_specs)
        dlo_edges = lineage_svc.validate_edges(dlo_edges, mc_catalog)

        # Steps 5-6: DMO→CIO (skipped if --skip-cio)
        # CIO catalog validation: DMO source tables were already validated in step 4b.
        # CIO objects themselves may not yet be in the MC catalog if a metadata scan
        # has not run since they were created; the push is idempotent and MC will
        # register them on receipt.
        cio_edges: list = []
        if not args.skip_cio:
            cio_items = sf_svc.fetch_calculated_insights()
            cio_edges = sf_svc.parse_cio_edges(cio_items)

    except (RuntimeError, TimeoutError, requests.exceptions.RequestException) as exc:
        log.error("Fatal error during data collection: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Unexpected error during data collection: %s: %s", type(exc).__name__, exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(2)
    finally:
        sf_svc.invalidate_token()

    log.info("DLO->DMO edges (%d total):", len(dlo_edges))
    for e in dlo_edges:
        log.debug("  [%s] %s -> %s", e["data_space"], e["source"], e["target"])

    if not args.skip_cio:
        log.info("DMO->CIO edges (%d total):", len(cio_edges))
        for e in cio_edges:
            log.debug("  [%s] %s -> %s", e["data_space"], e["source"], e["target"])

    if not dlo_edges and not cio_edges:
        log.warning(
            "0 edges ready to push. "
            "If Step 4b removed all DLO->DMO edges: trigger a metadata sync in Monte Carlo "
            "(Settings → Integrations → your Data Cloud connection) then re-run. "
            "If Step 4 also found 0 edges: confirm DLO→DMO mappings exist in your Salesforce org."
        )
        sys.exit(0)

    if args.dry_run:
        log.info(
            "[dry-run] Would push %d DLO->DMO and %d DMO->CIO edge(s) to Monte Carlo "
            "warehouse UUID=%s. Run without --dry-run to commit.",
            len(dlo_edges), len(cio_edges), mcd_resource_uuid,
        )
        sys.exit(0)

    dmo_invocation_ids: list = []
    cio_invocation_ids: list = []

    # Step 7: Push DLO→DMO
    if dlo_edges:
        try:
            dmo_invocation_ids = lineage_svc.push_edges(
                dlo_edges, run_id, step_label="Step 7", edge_kind="dlo_dmo"
            )
        except KeyboardInterrupt:
            log.warning("Interrupted during DLO->DMO push — partial results may have been written.")
            sys.exit(1)
        except RuntimeError as exc:
            log.error("DLO->DMO push failed: %s", exc)
            sys.exit(1)
        except Exception as exc:
            log.error("Unexpected error during DLO->DMO push: %s: %s", type(exc).__name__, exc)
            log.debug("Traceback:", exc_info=True)
            sys.exit(2)
    else:
        log.info("Step 7: No DLO->DMO edges to push — skipping")

    # Step 8: Push DMO→CIO
    if cio_edges and not args.skip_cio:
        try:
            cio_invocation_ids = lineage_svc.push_edges(
                cio_edges, run_id, step_label="Step 8", edge_kind="dmo_cio"
            )
        except KeyboardInterrupt:
            log.warning("Interrupted during DMO->CIO push — partial results may have been written.")
            sys.exit(1)
        except RuntimeError as exc:
            log.error("DMO->CIO push failed: %s", exc)
            sys.exit(1)
        except Exception as exc:
            log.error("Unexpected error during DMO->CIO push: %s: %s", type(exc).__name__, exc)
            log.debug("Traceback:", exc_info=True)
            sys.exit(2)
    elif not args.skip_cio:
        log.info("Step 8: No DMO->CIO edges to push — skipping")

    all_invocation_ids = dmo_invocation_ids + cio_invocation_ids
    elapsed = time.monotonic() - t_start
    log.info(
        "=== Run complete | run_id=%s | %.1fs | DLO->DMO=%d | DMO->CIO=%d | invocation_ids=[%s] ===",
        run_id, elapsed,
        len(dlo_edges),
        len(cio_edges),
        ", ".join(str(iid) for iid in all_invocation_ids if iid is not None) or "none",
    )


if __name__ == "__main__":
    main()

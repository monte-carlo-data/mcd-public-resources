#!/usr/bin/env python3
"""
Data 360 DLO->DMO Lineage Push (Pandora Push Model)

Pipeline:
  1. Authenticate with Salesforce via OAuth client credentials
  2. Fetch Salesforce data spaces (for data-space resolution fallback)
  3. Retrieve ObjectSourceTargetMap metadata via SOAP Metadata API
  4. Parse DLO->DMO edges; assign preliminary data space from XML field or fallback
  4b. Validate tables exist in MC catalog; overwrite data space with authoritative
      value from MC catalog dataset field
  5. Push DLO->DMO lineage to Monte Carlo via pycarlo IngestionService

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
  METADATA_BATCH_SIZE    — ObjectSourceTargetMap records per retrieve batch (default: 10)
  METADATA_MAX_POLLS     — max SOAP polling attempts per batch (default: 120)
  METADATA_POLL_INTERVAL — seconds between SOAP polls (default: 5)
  SF_DEFAULT_DATA_SPACE  — fallback data space name (default: default)
"""
import argparse
import base64
import io
import ipaddress
import json
import logging
import os
import signal
import socket
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

try:
    import defusedxml.ElementTree as SafeET
except ImportError as err:
    sys.exit(
        f"ERROR: defusedxml is not installed. Run: pip install defusedxml>=0.7.1\n  ({err})"
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


def _parse_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        val = float(raw)
        if val <= 0:
            raise ValueError("must be > 0")
        return val
    except ValueError as exc:
        sys.exit(f"Invalid {name}={raw!r}: {exc}")


INGEST_BATCH_SIZE      = _parse_positive_int("INGEST_BATCH_SIZE", 500)
METADATA_BATCH_SIZE    = _parse_positive_int("METADATA_BATCH_SIZE", 10)
METADATA_MAX_POLLS     = _parse_positive_int("METADATA_MAX_POLLS", 120)
METADATA_POLL_INTERVAL = _parse_positive_float("METADATA_POLL_INTERVAL", 5.0)

ZIP_MAX_FILES = 10_000
ZIP_MAX_BYTES = 500 * 1024 * 1024  # 500 MB

OSTM_NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}
SOAP_NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "met":     "http://soap.sforce.com/2006/04/metadata",
}

# ── SOAP envelope templates ───────────────────────────────────────────────────
_LIST_METADATA_ENVELOPE = """\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:SessionHeader><met:sessionId>{session_id}</met:sessionId></met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:listMetadata>
      <met:queries><met:type>ObjectSourceTargetMap</met:type></met:queries>
      <met:asOfVersion>{api_version}</met:asOfVersion>
    </met:listMetadata>
  </soapenv:Body>
</soapenv:Envelope>"""

_RETRIEVE_BATCH_ENVELOPE = """\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:SessionHeader><met:sessionId>{session_id}</met:sessionId></met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:retrieve>
      <met:retrieveRequest>
        <met:apiVersion>{api_version}</met:apiVersion>
        <met:unpackaged>
          <met:types>
            {members}
            <met:name>ObjectSourceTargetMap</met:name>
          </met:types>
        </met:unpackaged>
      </met:retrieveRequest>
    </met:retrieve>
  </soapenv:Body>
</soapenv:Envelope>"""

_STATUS_ENVELOPE = """\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:SessionHeader><met:sessionId>{session_id}</met:sessionId></met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:checkRetrieveStatus>
      <met:asyncProcessId>{async_id}</met:asyncProcessId>
      <met:includeZip>true</met:includeZip>
    </met:checkRetrieveStatus>
  </soapenv:Body>
</soapenv:Envelope>"""


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
        return f"{seconds:.0f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs:02d}s"


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
                f"Salesforce auth failed: {data.get('error')} — {data.get('error_description')}"
            )
        resp.raise_for_status()
        self._token = data["access_token"]
        log.info("  Authenticated (%.1fs)", time.monotonic() - t0)

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
                "Salesforce error: %s. Falling back to 'default'. "
                "Edges without <dataSpace> in XML may land in the wrong data space.",
                resp.text[:300],
            )
        else:
            log.warning(
                "  Could not query Dataspace object (status=%d, body=%s) — using 'default' as fallback",
                resp.status_code, resp.text[:200],
            )
        return ["default"]

    @_retrying
    def _soap_post(self, body: str, action: str) -> ET.Element:
        url = f"{self.instance_url}/services/Soap/m/{SF_API_VERSION}"
        resp = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml", "SOAPAction": action},
            verify=True,
            timeout=60,
        )
        resp.raise_for_status()
        root = SafeET.fromstring(resp.text)
        fault = root.find(".//soapenv:Fault", SOAP_NS)
        if fault is not None:
            code = fault.findtext("faultcode", default="unknown")
            msg  = fault.findtext("faultstring", default="no detail")
            raise RuntimeError(f"Salesforce SOAP fault [{code}]: {msg}")
        return root

    def _list_ostm_names(self) -> list:
        """Call listMetadata to get all ObjectSourceTargetMap record names. Completes in <1s."""
        envelope = _LIST_METADATA_ENVELOPE.format(
            session_id=escape(self._token),
            api_version=escape(SF_API_VERSION),
        )
        root = self._soap_post(envelope, action="listMetadata")
        return [
            el.text
            for el in root.findall(".//{http://soap.sforce.com/2006/04/metadata}fullName")
            if el.text
        ]

    def _submit_retrieve(self, members_xml: str) -> str:
        """Submit a retrieve job for specific members and return the async job ID."""
        envelope = _RETRIEVE_BATCH_ENVELOPE.format(
            session_id=escape(self._token),
            api_version=escape(SF_API_VERSION),
            members=members_xml,
        )
        root = self._soap_post(envelope, action="retrieve")
        async_id_el = root.find(".//met:id", SOAP_NS)
        if async_id_el is None:
            fault_el = root.find(".//met:error", SOAP_NS)
            detail = ET.tostring(fault_el, encoding="unicode") if fault_el is not None else "no error element"
            raise RuntimeError(f"Retrieve returned no async ID. Detail: {detail}")
        if not async_id_el.text:
            raise RuntimeError("Salesforce returned an empty async job ID for the metadata retrieve.")
        return async_id_el.text

    def _poll_retrieve(self, async_id: str, label: str) -> bytes:
        """Poll a retrieve job until complete and return ZIP bytes."""
        consecutive_failures = 0
        max_consecutive = 5

        for attempt in range(METADATA_MAX_POLLS):
            status_body = _STATUS_ENVELOPE.format(
                session_id=escape(self._token),
                async_id=escape(async_id),
            )
            try:
                status_root = self._soap_post(status_body, action="checkRetrieveStatus")
                consecutive_failures = 0
            except (requests.exceptions.RequestException, RuntimeError) as exc:
                consecutive_failures += 1
                log.warning(
                    "  %s poll %d failed (consecutive=%d/%d): %s",
                    label, attempt + 1, consecutive_failures, max_consecutive, exc,
                )
                if consecutive_failures >= max_consecutive:
                    raise RuntimeError(
                        f"Metadata poll failed {max_consecutive} consecutive times. "
                        f"Last error: {exc}. Async job ID: {async_id}"
                    ) from exc
                time.sleep(METADATA_POLL_INTERVAL)
                continue

            done_el  = status_root.find(".//met:done",   SOAP_NS)
            state_el = status_root.find(".//met:status", SOAP_NS)
            state    = state_el.text if state_el is not None else "unknown"

            if done_el is None:
                log.warning("  %s poll %d: missing <done> element — treating as not done", label, attempt + 1)

            if done_el is not None and done_el.text == "true":
                if state == "Failed":
                    err_code = status_root.findtext(".//met:errorStatusCode", default="", namespaces=SOAP_NS)
                    err_msg  = status_root.findtext(".//met:errorMessage",    default="", namespaces=SOAP_NS)
                    raise RuntimeError(f"Salesforce retrieve job failed [{err_code}]: {err_msg}")
                zip_el = status_root.find(".//met:zipFile", SOAP_NS)
                if zip_el is None or not zip_el.text:
                    raise RuntimeError("Retrieve completed but ZIP payload is empty.")
                return base64.b64decode(zip_el.text)

            log.debug("  %s poll %d/%d status=%s", label, attempt + 1, METADATA_MAX_POLLS, state)
            if attempt < METADATA_MAX_POLLS - 1:
                time.sleep(METADATA_POLL_INTERVAL)

        raise TimeoutError(
            f"Batch retrieve did not complete within "
            f"{METADATA_MAX_POLLS * METADATA_POLL_INTERVAL:.0f}s. Async job ID: {async_id}. "
            f"Increase METADATA_MAX_POLLS or METADATA_POLL_INTERVAL, or check Salesforce org status."
        )

    def fetch_metadata(self) -> bytes:
        t0 = time.monotonic()
        log.info("Step 3: Retrieving ObjectSourceTargetMap metadata from Salesforce")

        # Fast list to discover all record names (~0.5s)
        names = self._list_ostm_names()
        if not names:
            log.warning("  listMetadata returned 0 ObjectSourceTargetMap records")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w"):
                pass
            return buf.getvalue()

        batches = [names[i:i + METADATA_BATCH_SIZE] for i in range(0, len(names), METADATA_BATCH_SIZE)]
        total_batches = len(batches)
        log.info(
            "  Found %d ObjectSourceTargetMap record(s) — retrieving in %d batch(es) of up to %d",
            len(names), total_batches, METADATA_BATCH_SIZE,
        )

        combined_buf = io.BytesIO()
        combined_zip = zipfile.ZipFile(combined_buf, "w", zipfile.ZIP_DEFLATED)
        batch_times: list = []

        for i, batch in enumerate(batches):
            batch_num = i + 1
            batch_t0 = time.monotonic()
            members_xml = "\n            ".join(
                f"<met:members>{escape(n)}</met:members>" for n in batch
            )
            async_id = self._submit_retrieve(members_xml)
            log.debug("  Batch %d/%d submitted (id=%s)", batch_num, total_batches, async_id)

            zip_bytes = self._poll_retrieve(async_id, label=f"Batch {batch_num}/{total_batches}")
            batch_elapsed = time.monotonic() - batch_t0
            batch_times.append(batch_elapsed)

            # Rolling average ETA over last 5 batches
            window = batch_times[-5:]
            avg = sum(window) / len(window)
            remaining = total_batches - batch_num
            if remaining > 0:
                log.info(
                    "  Batch %d/%d complete (%d record(s), %.1fs) — est. %s remaining",
                    batch_num, total_batches, len(batch), batch_elapsed, _format_eta(avg * remaining),
                )
            else:
                log.info(
                    "  Batch %d/%d complete (%d record(s), %.1fs)",
                    batch_num, total_batches, len(batch), batch_elapsed,
                )

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as batch_zf:
                for name in batch_zf.namelist():
                    if name.endswith("package.xml"):
                        continue  # skip sidecar manifest — one copy already present
                    combined_zip.writestr(name, batch_zf.read(name))

        combined_zip.close()
        log.info("  All %d record(s) retrieved in %.1fs", len(names), time.monotonic() - t0)
        return combined_buf.getvalue()

    def parse_edges(self, zip_bytes: bytes, fallback_dataspace: str) -> list:
        """
        Parse DLO->DMO edges from the ObjectSourceTargetMap ZIP payload.

        Data space resolution order per record:
          1. <dataSpace> XML field (preferred — present in most orgs)
          2. fallback_dataspace (SF_DEFAULT_DATA_SPACE env var, default: 'default')
        """
        t0 = time.monotonic()
        log.info("Step 4: Parsing DLO->DMO edges from metadata")
        edges = []
        skipped = 0
        xml_had_dataspace = 0

        buf = io.BytesIO(zip_bytes)
        try:
            zf_handle = zipfile.ZipFile(buf)
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"Retrieved payload is not a valid ZIP: {exc}") from exc

        with zf_handle as zf:
            all_names = zf.namelist()
            if len(all_names) > ZIP_MAX_FILES:
                raise RuntimeError(
                    f"ZIP contains {len(all_names)} files — exceeds safety limit of {ZIP_MAX_FILES}. "
                    "Aborting to prevent decompression bomb."
                )
            total_uncompressed = sum(i.file_size for i in zf.infolist())
            if total_uncompressed > ZIP_MAX_BYTES:
                raise RuntimeError(
                    f"ZIP total uncompressed size {total_uncompressed / 1024 / 1024:.0f}MB "
                    f"exceeds safety limit of {ZIP_MAX_BYTES / 1024 / 1024:.0f}MB."
                )

            xml_files = [n for n in all_names if n.endswith(".objectSourceTargetMap")]
            meta_xml_count = sum(
                1 for n in all_names if n.endswith(".objectSourceTargetMap-meta.xml")
            )
            if meta_xml_count:
                log.debug(
                    "  Skipping %d .objectSourceTargetMap-meta.xml file(s) (metadata sidecar, not parsed)",
                    meta_xml_count,
                )

            if not xml_files:
                raise RuntimeError(
                    "ZIP contained no .objectSourceTargetMap files. "
                    "Check that the retrieve succeeded and the metadata type name is correct. "
                    "Run with LOG_LEVEL=DEBUG to inspect the ZIP contents."
                )
            log.info("  Found %d ObjectSourceTargetMap record(s)", len(xml_files))

            for name in xml_files:
                with zf.open(name) as f:
                    try:
                        root = SafeET.parse(f).getroot()
                    except ET.ParseError as exc:
                        log.warning("  Skipping %s: XML parse error: %s", name, exc)
                        skipped += 1
                        continue

                source = root.findtext("sf:sourceObjectName", namespaces=OSTM_NS)
                target = root.findtext("sf:targetObjectName", namespaces=OSTM_NS)

                if not source or not target:
                    log.warning("  Skipping %s: missing sourceObjectName or targetObjectName", name)
                    skipped += 1
                    continue

                if not (source.endswith("__dll") and target.endswith("__dlm")):
                    continue

                data_space = root.findtext("sf:dataSpace", namespaces=OSTM_NS)
                if data_space:
                    xml_had_dataspace += 1
                else:
                    data_space = fallback_dataspace

                edges.append({"source": source, "target": target, "data_space": data_space})

        buf.close()  # ZipFile.__exit__ does not close the underlying BytesIO buffer

        if skipped and skipped == len(xml_files):
            raise RuntimeError(
                f"All {skipped} ObjectSourceTargetMap record(s) failed to parse. "
                "Check Salesforce connectivity and metadata type availability."
            )
        if skipped:
            log.warning("  Skipped %d record(s) due to parse errors", skipped)
        if xml_had_dataspace:
            log.info("  Data space resolved from XML: %d/%d edge(s)", xml_had_dataspace, len(edges))
        if xml_had_dataspace < len(edges):
            no_space_count = len(edges) - xml_had_dataspace
            log.warning(
                "  %d/%d edge(s) have no <dataSpace> in XML — using fallback '%s'. "
                "If your org uses multiple data spaces, set SF_DEFAULT_DATA_SPACE to match "
                "or contact your Salesforce admin to confirm the data space name.",
                no_space_count, len(edges), fallback_dataspace,
            )
        log.info("  %d DLO->DMO edge(s) extracted (%.1fs)", len(edges), time.monotonic() - t0)
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
                f"Response preview: {resp.text[:200]}"
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
                        existing.append(data_space)
                    else:
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
            log.warning(
                "  Table '%s' found in multiple data spaces in MC catalog: %s — "
                "using '%s'. If edges land in the wrong data space, check which "
                "data space the DLO→DMO mapping belongs to and set SF_DEFAULT_DATA_SPACE.",
                table_name, entry, entry[0],
            )
            return entry[0]
        return entry

    def validate_edges(self, edges: list) -> list:
        """
        Fetch all tables for the warehouse from MC in bulk, then validate each edge.
        Extracts the authoritative data space from each table's MC catalog dataset field
        and stamps it onto the returned edges — overriding the XML fallback from parse_edges().
        Edges where either table is missing from the MC catalog, or where DLO and DMO
        are catalogued under different data spaces, are removed and logged.
        Returns the validated subset ready for push.
        """
        if not edges:
            return edges

        log.info("Step 4b: Validating DLO and DMO tables exist in Monte Carlo catalog")
        t0 = time.monotonic()

        try:
            mc_catalog = self._fetch_mc_catalog()
        except requests.exceptions.HTTPError as exc:
            raise RuntimeError(
                f"MC catalog fetch returned {exc.response.status_code}. "
                "Check MCD_ID/MCD_TOKEN."
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

        unique_names = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
        missing_set: set = set()

        for name in unique_names:
            if mc_catalog.get(name.lower()) is not None:
                entry = mc_catalog[name.lower()]
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
            src_space = self._resolve_catalog(mc_catalog, e["source"], preferred_data_space=preferred)
            tgt_space = self._resolve_catalog(mc_catalog, e["target"], preferred_data_space=preferred)
            if src_space is None or tgt_space is None:
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

    def push_edges(self, edges: list, run_id: str) -> list:
        t0 = time.monotonic()
        log.info("Step 5: Pushing %d DLO->DMO edge(s) via Ingest API", len(edges))

        svc = IngestionService(mc_client=Client(session=Session(
            mcd_id=self.ingest_key_id,
            mcd_token=self.ingest_key_secret,
            scope="Ingestion",
            endpoint=MC_INGEST_URL,
        )))

        events = [
            LineageEvent(
                destination=LineageAssetRef(
                    type="TABLE",
                    database=DB,
                    schema=e["data_space"],
                    name=e["target"],
                ),
                sources=[LineageAssetRef(
                    type="TABLE",
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
                invocation_ids.append(inv_id)
                log.info(
                    "  Batch %d/%d: %d edge(s) pushed — invocation_id=%s",
                    batch_num, total_batches, len(batch_events), inv_id,
                )
            except Exception as exc:
                log.error("  Batch %d/%d failed: %s", batch_num, total_batches, exc)
                failures.extend(batch_edges)

        if failures:
            path = _save_failed_edges(run_id, "dlo_dmo", failures)
            raise RuntimeError(
                f"{len(failures)} DLO->DMO edge(s) failed to push. "
                f"Failed edges saved for retry: {path}"
            )
        log.info("  All %d DLO->DMO edge(s) pushed (%.1fs)", len(edges), time.monotonic() - t0)
        return invocation_ids


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    run_id = uuid.uuid4().hex[:8]
    global log
    log = _setup_logging(run_id)

    parser = argparse.ArgumentParser(
        description="Push Salesforce Data 360 DLO->DMO lineage to Monte Carlo"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch live data and preview edges, but skip the MC push",
    )
    args = parser.parse_args()

    def _handle_sigterm(signum, frame):  # noqa: ANN001
        log.warning("Received SIGTERM — shutting down. Partial lineage edges may have been pushed.")
        sys.exit(1)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    log.info(
        "=== Data 360 Lineage Push | run_id=%s%s ===",
        run_id, " [DRY RUN]" if args.dry_run else "",
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

    # SSRF guard: reject non-HTTPS, embedded credentials, private/reserved IPs (literal and DNS)
    _parsed_url = urlparse(sf_instance_url)
    if _parsed_url.scheme != "https" or not _parsed_url.hostname or "@" in (_parsed_url.netloc or ""):
        log.error(
            "SF_ORG_URL is not a valid HTTPS URL (got: %s). "
            "Expected format: https://myorg.my.salesforce.com",
            sf_instance_url,
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

    default_dataspace = os.environ.get("SF_DEFAULT_DATA_SPACE", "default")

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
        # Single data space: use it directly. Multiple: XML <dataSpace> is authoritative;
        # default_dataspace (SF_DEFAULT_DATA_SPACE) is a last-resort fallback only.
        fallback_space = data_spaces[0] if len(data_spaces) == 1 else default_dataspace
        if len(data_spaces) > 1:
            log.info(
                "  Multiple data spaces — DLO records without <dataSpace> will use '%s'",
                fallback_space,
            )

        # Steps 3-4b
        zip_bytes = sf_svc.fetch_metadata()
        dlo_edges = sf_svc.parse_edges(zip_bytes, fallback_space)
        dlo_edges = lineage_svc.validate_edges(dlo_edges)

    except (RuntimeError, TimeoutError, requests.exceptions.RequestException) as exc:
        log.error("Fatal error during data collection: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.exception("Unexpected error during data collection: %s", exc)
        sys.exit(2)

    log.info("DLO->DMO edges (%d total):", len(dlo_edges))
    for e in dlo_edges:
        log.info("  [%s] %s -> %s", e["data_space"], e["source"], e["target"])

    if not dlo_edges:
        log.warning(
            "0 DLO->DMO edges ready to push. "
            "If Step 4b removed all edges: trigger a metadata sync in Monte Carlo "
            "(Settings → Integrations → your Data Cloud connection) then re-run. "
            "If Step 4 also found 0 edges: confirm DLO→DMO mappings exist in your Salesforce org."
        )
        sys.exit(0)

    if args.dry_run:
        log.info(
            "[dry-run] Would push %d DLO->DMO edge(s) to Monte Carlo warehouse UUID=%s. "
            "Run without --dry-run to commit.",
            len(dlo_edges), mcd_resource_uuid,
        )
        sys.exit(0)

    try:
        all_invocation_ids = lineage_svc.push_edges(dlo_edges, run_id)
    except KeyboardInterrupt:
        log.warning("Interrupted during push — partial results may have been written to Monte Carlo.")
        sys.exit(1)
    except RuntimeError as exc:
        log.error("Push failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.exception("Unexpected error during push: %s", exc)
        sys.exit(2)

    elapsed = time.monotonic() - t_start
    log.info(
        "=== Run complete | run_id=%s | %.1fs | DLO->DMO=%d | invocation_ids=[%s] ===",
        run_id, elapsed,
        len(dlo_edges),
        ", ".join(str(iid) for iid in all_invocation_ids if iid is not None) or "none",
    )


if __name__ == "__main__":
    main()

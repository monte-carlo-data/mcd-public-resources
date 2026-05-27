#!/usr/bin/env python3
"""
Salesforce Data 360 — Diagnostic Script

Validates that all Salesforce APIs used by the lineage push script are
accessible and reports timing/volume data. No Monte Carlo credentials needed.

Usage:
  pip install requests python-dotenv defusedxml
  python3 sf_diagnostic.py

Required env vars (or set in .env):
  SF_ORG_URL       — My Domain URL, e.g. https://mycompany.my.salesforce.com
  SF_CLIENT_ID     — Connected app consumer key
  SF_CLIENT_SECRET — Connected app consumer secret
"""
import base64
import io
import ipaddress
import os
import socket
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from defusedxml.ElementTree import ParseError as _ETParseError
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv

try:
    import defusedxml.ElementTree as SafeET
except ImportError as err:
    sys.exit(
        f"ERROR: defusedxml is not installed. Run: pip install defusedxml>=0.7.1\n  ({err})"
    )

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

SF_API_VERSION = "62.0"
ZIP_MAX_FILES  = 10_000
ZIP_MAX_BYTES  = 500 * 1024 * 1024  # 500 MB


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        val = int(raw)
        if val < 1:
            raise ValueError("must be >= 1")
        return val
    except ValueError as exc:
        print(f"[FAIL] Invalid {name}={raw!r}: {exc}")
        sys.exit(1)


def _parse_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        val = float(raw)
        if val <= 0:
            raise ValueError("must be > 0")
        return val
    except ValueError as exc:
        print(f"[FAIL] Invalid {name}={raw!r}: {exc}")
        sys.exit(1)


METADATA_BATCH_SIZE    = _parse_positive_int("METADATA_BATCH_SIZE", 10)
METADATA_MAX_POLLS     = _parse_positive_int("METADATA_MAX_POLLS", 120)
METADATA_POLL_INTERVAL = _parse_positive_float("METADATA_POLL_INTERVAL", 5.0)

SOAP_NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "met":     "http://soap.sforce.com/2006/04/metadata",
}
OSTM_NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}

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


def _sep(label=""):
    width = 60
    if label:
        print(f"\n{'─' * 3} {label} {'─' * (width - len(label) - 5)}")
    else:
        print("─" * width)


def _ok(msg):  print(f"  [OK]   {msg}")
def _warn(msg): print(f"  [WARN] {msg}")
def _fail(msg): print(f"  [FAIL] {msg}")


def _format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs:02d}s"


def _soap_post(url: str, envelope: str, action: str):
    resp = requests.post(
        url,
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml", "SOAPAction": action},
        timeout=60,
        verify=True,
    )
    resp.raise_for_status()
    root = SafeET.fromstring(resp.text)
    fault = root.find(".//soapenv:Fault", SOAP_NS)
    if fault is not None:
        code = fault.findtext("faultcode", default="unknown")
        msg  = (fault.findtext("faultstring", default="no detail") or "")[:200]
        _fail(f"SOAP fault [{code}]: {msg}")
        sys.exit(1)
    return root


def _poll_batch(url: str, token: str, async_id: str, label: str) -> bytes:
    for attempt in range(METADATA_MAX_POLLS):
        status_body = _STATUS_ENVELOPE.format(
            session_id=escape(token), async_id=escape(async_id)
        )
        status_root = _soap_post(url, status_body, action="checkRetrieveStatus")

        done_el  = status_root.find(".//met:done",   SOAP_NS)
        state_el = status_root.find(".//met:status", SOAP_NS)
        state    = state_el.text if state_el is not None else "unknown"

        if done_el is not None and done_el.text == "true":
            if state == "Failed":
                err_code = status_root.findtext(".//met:errorStatusCode", default="", namespaces=SOAP_NS)
                err_msg  = (status_root.findtext(".//met:errorMessage", default="", namespaces=SOAP_NS) or "")[:200]
                _fail(f"Retrieve job failed [{err_code}]: {err_msg}")
                sys.exit(1)
            zip_el = status_root.find(".//met:zipFile", SOAP_NS)
            if zip_el is None or not zip_el.text:
                _fail("Retrieve completed but ZIP payload is empty")
                sys.exit(1)
            return base64.b64decode(zip_el.text)

        if attempt % 5 == 0:
            elapsed_msg = f"attempt {attempt + 1}/{METADATA_MAX_POLLS}, status={state}"
            print(f"  [....] {label}: polling... {elapsed_msg}")
        time.sleep(METADATA_POLL_INTERVAL)

    _fail(
        f"Retrieve did not complete within "
        f"{METADATA_MAX_POLLS * METADATA_POLL_INTERVAL:.0f}s ({label}). "
        f"Try increasing METADATA_MAX_POLLS (current: {METADATA_MAX_POLLS})."
    )
    sys.exit(1)


def check_auth(instance_url, client_id, client_secret):
    _sep("1. OAuth — client credentials")
    t0 = time.monotonic()
    resp = requests.post(
        f"{instance_url}/services/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=30,
        verify=True,
    )
    try:
        data = resp.json()
    except ValueError:
        _fail(f"Non-JSON response from auth endpoint (HTTP {resp.status_code}): {resp.text[:200]}")
        sys.exit(1)
    if "error" in data:
        _fail(f"Auth failed: {data.get('error')} — {data.get('error_description')}")
        sys.exit(1)
    elapsed = time.monotonic() - t0
    _ok(f"Authenticated in {elapsed:.1f}s")
    _ok(f"Instance URL: {data.get('instance_url')}")
    return data["access_token"]


def check_data_spaces(instance_url, token):
    _sep("2. Data Spaces — Dataspace SOQL query")
    t0 = time.monotonic()
    resp = requests.get(
        f"{instance_url}/services/data/v{SF_API_VERSION}/query",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": "SELECT DataSpaceApiName FROM Dataspace"},
        timeout=30,
        verify=True,
    )
    elapsed = time.monotonic() - t0
    if resp.status_code != 200:
        _warn(f"Query returned HTTP {resp.status_code} in {elapsed:.1f}s — data spaces unavailable")
        _warn("The script will fall back to 'default' as the data space name")
        return []
    records = resp.json().get("records", [])
    spaces = [r["DataSpaceApiName"] for r in records if r.get("DataSpaceApiName")]
    if not spaces:
        _warn(f"Query succeeded but returned 0 data spaces in {elapsed:.1f}s")
        _warn("The script will fall back to 'default' as the data space name")
        return []
    _ok(f"Found {len(spaces)} data space(s) in {elapsed:.1f}s")
    for s in spaces:
        _ok(f"  • {s}")
    if len(spaces) > 1:
        _warn(
            "Multiple data spaces found. The lineage push script will use the data space "
            "recorded in each ObjectSourceTargetMap XML record. If a record has no <dataSpace> "
            "field, it will fall back to SF_DEFAULT_DATA_SPACE (default: 'default'). "
            "Set SF_DEFAULT_DATA_SPACE if your primary data space has a different name."
        )
    return spaces


def check_metadata_retrieve(instance_url, token):
    _sep("3. SOAP Metadata API — ObjectSourceTargetMap retrieve")

    url = f"{instance_url}/services/Soap/m/{SF_API_VERSION}"
    t0 = time.monotonic()

    # Step 3a: list all record names via listMetadata (~0.5s)
    list_envelope = _LIST_METADATA_ENVELOPE.format(
        session_id=escape(token), api_version=escape(SF_API_VERSION)
    )
    list_root = _soap_post(url, list_envelope, action="listMetadata")
    names = [
        el.text
        for el in list_root.findall(".//{http://soap.sforce.com/2006/04/metadata}fullName")
        if el.text
    ]
    elapsed = time.monotonic() - t0
    _ok(f"listMetadata returned {len(names)} record(s) in {elapsed:.1f}s")

    if not names:
        _warn("No ObjectSourceTargetMap records found in org")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        return buf.getvalue()

    # Step 3b: retrieve in batches
    batches = [names[i:i + METADATA_BATCH_SIZE] for i in range(0, len(names), METADATA_BATCH_SIZE)]
    total_batches = len(batches)
    _ok(f"Retrieving {len(names)} record(s) in {total_batches} batch(es) of up to {METADATA_BATCH_SIZE}")

    combined_buf = io.BytesIO()
    batch_times: list = []
    cumulative_uncompressed = 0

    with zipfile.ZipFile(combined_buf, "w", zipfile.ZIP_DEFLATED) as combined_zip:
        for i, batch in enumerate(batches):
            batch_num = i + 1
            batch_t0 = time.monotonic()
            members_xml = "\n            ".join(
                f"<met:members>{escape(n)}</met:members>" for n in batch
            )
            retrieve_envelope = _RETRIEVE_BATCH_ENVELOPE.format(
                session_id=escape(token),
                api_version=escape(SF_API_VERSION),
                members=members_xml,
            )
            retrieve_root = _soap_post(url, retrieve_envelope, action="retrieve")
            async_id_el = retrieve_root.find(".//met:id", SOAP_NS)
            if async_id_el is None or not async_id_el.text:
                _fail(f"Batch {batch_num}: no async job ID returned")
                sys.exit(1)
            async_id = async_id_el.text

            zip_bytes = _poll_batch(url, token, async_id, label=f"Batch {batch_num}/{total_batches}")
            batch_elapsed = time.monotonic() - batch_t0
            batch_times.append(batch_elapsed)

            window = batch_times[-5:]
            avg = sum(window) / len(window)
            remaining = total_batches - batch_num
            if remaining > 0:
                _ok(
                    f"Batch {batch_num}/{total_batches} complete ({len(batch)} record(s), "
                    f"{batch_elapsed:.1f}s) — est. {_format_eta(avg * remaining)} remaining"
                )
            else:
                _ok(f"Batch {batch_num}/{total_batches} complete ({len(batch)} record(s), {batch_elapsed:.1f}s)")

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as batch_zf:
                if len(batch_zf.namelist()) > ZIP_MAX_FILES:
                    _fail(
                        f"Batch {batch_num} ZIP contains more than {ZIP_MAX_FILES} files — "
                        "aborting to prevent decompression bomb."
                    )
                    sys.exit(1)
                batch_uncompressed = sum(info.file_size for info in batch_zf.infolist())
                if batch_uncompressed > ZIP_MAX_BYTES:
                    _fail(
                        f"Batch {batch_num} ZIP uncompressed size "
                        f"{batch_uncompressed / 1024 / 1024:.0f}MB exceeds safety limit "
                        f"of {ZIP_MAX_BYTES / 1024 / 1024:.0f}MB."
                    )
                    sys.exit(1)
                cumulative_uncompressed += batch_uncompressed
                if cumulative_uncompressed > ZIP_MAX_BYTES:
                    _fail(
                        f"Cumulative uncompressed size across {batch_num} batch(es) "
                        f"({cumulative_uncompressed / 1024 / 1024:.0f}MB) exceeds safety limit "
                        f"of {ZIP_MAX_BYTES / 1024 / 1024:.0f}MB."
                    )
                    sys.exit(1)
                for name in batch_zf.namelist():
                    if name.endswith("package.xml"):
                        continue
                    if "\x00" in name:
                        print(f"  [WARN] Skipping ZIP entry with null byte in filename")
                        continue
                    safe_name = name.lstrip("/").replace("\\", "/")
                    parts = [p for p in safe_name.split("/") if p and p not in (".", "..")]
                    safe_name = "/".join(parts)
                    if not safe_name:
                        continue
                    combined_zip.writestr(safe_name, batch_zf.read(name))

    total_elapsed = time.monotonic() - t0
    _ok(f"All {len(names)} record(s) retrieved in {total_elapsed:.1f}s")
    return combined_buf.getvalue()


def check_zip_contents(zip_bytes):
    _sep("4. ObjectSourceTargetMap — parse DLO→DMO edges")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        all_names = zf.namelist()
        if len(all_names) > ZIP_MAX_FILES:
            _fail(f"ZIP contains {len(all_names)} files — exceeds safety limit of {ZIP_MAX_FILES}.")
            sys.exit(1)
        total_uncompressed = sum(i.file_size for i in zf.infolist())
        if total_uncompressed > ZIP_MAX_BYTES:
            _fail(
                f"ZIP total uncompressed size {total_uncompressed / 1024 / 1024:.0f}MB "
                f"exceeds safety limit of {ZIP_MAX_BYTES / 1024 / 1024:.0f}MB."
            )
            sys.exit(1)

        xml_files = [n for n in all_names if n.endswith(".objectSourceTargetMap")]
        _ok(f"ZIP contains {len(all_names)} file(s) total")
        _ok(f"ObjectSourceTargetMap records: {len(xml_files)}")

        if not xml_files:
            _warn("No .objectSourceTargetMap files found in ZIP")
            _warn("Files present:")
            for n in all_names[:20]:
                _warn(f"  {n}")
            return

        edges = []
        no_dataspace = 0
        data_spaces_seen = set()

        for name in xml_files:
            with zf.open(name) as f:
                try:
                    root = SafeET.parse(f).getroot()
                except _ETParseError:
                    continue
                source = root.findtext("sf:sourceObjectName", namespaces=OSTM_NS)
                target = root.findtext("sf:targetObjectName", namespaces=OSTM_NS)
                if not source or not target:
                    continue
                if source.endswith("__dll") and target.endswith("__dlm"):
                    ds = root.findtext("sf:dataSpace", namespaces=OSTM_NS)
                    if ds:
                        data_spaces_seen.add(ds)
                    else:
                        no_dataspace += 1
                    edges.append((source, target, ds or "(none in XML)"))

        _ok(f"DLO→DMO edges found: {len(edges)}")

        if data_spaces_seen:
            _ok(f"Data spaces referenced in XML: {sorted(data_spaces_seen)}")
        if no_dataspace:
            _warn(
                f"{no_dataspace}/{len(edges)} edges have no <dataSpace> field in XML. "
                "The script will fall back to the value from the Dataspace SOQL query."
            )

        print()
        print("  Edge list:")
        for source, target, ds in edges:
            print(f"    [{ds}] {source} → {target}")


def check_calculated_insights(instance_url, token):
    _sep("5. Data Cloud REST API — Calculated Insights (CIOs)")
    t0 = time.monotonic()
    base_url = f"{instance_url}/services/data/v{SF_API_VERSION}/ssot/calculated-insights"
    parsed_base = urlparse(instance_url)

    items = []
    next_url = base_url
    page = 0
    max_pages = 1_000

    while next_url:
        page += 1
        if page > max_pages:
            _warn(f"CIO pagination exceeded {max_pages} pages — stopping. Check for looping nextPageUrl.")
            break
        try:
            resp = requests.get(
                next_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=True,
            )
        except requests.exceptions.RequestException as exc:
            _fail(f"Request failed (page {page}): {exc}")
            return

        if resp.status_code == 403:
            elapsed = time.monotonic() - t0
            _warn(f"HTTP 403 Forbidden ({elapsed:.1f}s) — the connected app lacks Data Cloud REST API access.")
            _warn("To resolve: ensure the connected app OAuth scopes include 'api' and the")
            _warn("run-as user has the 'Data Cloud Admin' or 'Data Cloud User' permission set.")
            _warn("If DLO→DMO lineage is all you need, use --skip-cio when running push_lineage.py.")
            return

        if resp.status_code != 200:
            _fail(f"HTTP {resp.status_code} (page {page}): {resp.text[:200]}")
            return

        try:
            data = resp.json()
        except ValueError:
            _fail(f"Non-JSON response (page {page}): {resp.text[:200]}")
            return

        collection = data.get("collection", {})
        batch = collection.get("items", [])
        items.extend(batch)

        raw_next = collection.get("nextPageUrl") or None
        if raw_next:
            parsed_next = urlparse(raw_next)
            if not parsed_next.scheme:
                if not raw_next.startswith("/"):
                    _fail(f"Unexpected relative nextPageUrl (does not start with '/'): {raw_next[:200]!r} — pagination stopped, CIO list above may be incomplete.")
                    return
                next_url = f"{instance_url.rstrip('/')}{raw_next}"
            elif parsed_next.scheme != "https" or parsed_next.hostname != parsed_base.hostname:
                _fail(f"Cross-origin or non-HTTPS nextPageUrl: {raw_next[:200]!r} — pagination stopped, CIO list above may be incomplete.")
                return
            else:
                next_url = raw_next
        else:
            next_url = None

    elapsed = time.monotonic() - t0
    _ok(f"Found {len(items)} CIO(s) across {page} page(s) in {elapsed:.1f}s")

    if not items:
        _warn("No Calculated Insight Objects found in org.")
        _warn("If you expect CIOs, confirm they have been created and activated in")
        _warn("Data Cloud Setup → Calculated Insights. This is not an error — the")
        _warn("push script will simply push 0 DMO→CIO edges.")
        return

    no_expression = 0
    print()
    print("  CIO list (apiName | dataSpace | has SQL expression):")
    for cio in items:
        name = cio.get("apiName", "(unknown)")
        space = cio.get("dataSpace", "(none)")
        has_expr = bool(cio.get("expression"))
        status = "yes" if has_expr else "NO — expression is null"
        print(f"    {name} | {space} | {status}")
        if not has_expr:
            no_expression += 1

    if no_expression:
        _warn(
            f"{no_expression}/{len(items)} CIO(s) have no SQL expression. "
            "These will be skipped during the lineage push."
        )


def main():
    print("=" * 60)
    print("  Salesforce Data 360 — Lineage Push Diagnostic")
    print("=" * 60)

    instance_url  = os.environ.get("SF_ORG_URL", "").rstrip("/")
    client_id     = os.environ.get("SF_CLIENT_ID", "")
    client_secret = os.environ.get("SF_CLIENT_SECRET", "")

    missing = [
        n for n, v in [
            ("SF_ORG_URL", instance_url),
            ("SF_CLIENT_ID", client_id),
            ("SF_CLIENT_SECRET", client_secret),
        ] if not v
    ]
    if missing:
        print(f"\n[FAIL] Missing required env vars: {', '.join(missing)}")
        print("       Set them in a .env file or export them before running.")
        sys.exit(1)

    # SSRF guard: reject non-HTTPS, embedded credentials, private/reserved IPs
    _parsed = urlparse(instance_url)
    if _parsed.scheme != "https" or not _parsed.hostname or "@" in (_parsed.netloc or ""):
        print(f"[FAIL] SF_ORG_URL must be an HTTPS URL (got: {_parsed.scheme}://{_parsed.hostname or '<missing>'}).")
        sys.exit(1)
    _hostname = _parsed.hostname
    if _hostname.lower() == "localhost":
        print("[FAIL] SF_ORG_URL hostname is 'localhost' — refusing to connect.")
        sys.exit(1)

    def _is_internal(addr) -> bool:
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast

    try:
        if _is_internal(ipaddress.ip_address(_hostname)):
            print(f"[FAIL] SF_ORG_URL is a private/reserved IP ({_hostname}) — refusing to connect.")
            sys.exit(1)
    except ValueError:
        try:
            for _fam, _typ, _pro, _can, _sa in socket.getaddrinfo(_hostname, None):
                try:
                    if _is_internal(ipaddress.ip_address(_sa[0])):
                        print(f"[FAIL] SF_ORG_URL resolves to a private IP ({_sa[0]}) — refusing to connect.")
                        sys.exit(1)
                except ValueError:
                    pass
        except socket.gaierror:
            pass  # DNS failure — let the actual request surface a clear network error

    token = check_auth(instance_url, client_id, client_secret)
    check_data_spaces(instance_url, token)
    zip_bytes = check_metadata_retrieve(instance_url, token)
    check_zip_contents(zip_bytes)
    check_calculated_insights(instance_url, token)

    _sep()
    print("  Diagnostic complete — all Salesforce APIs accessible.")
    print("=" * 60)


if __name__ == "__main__":
    main()

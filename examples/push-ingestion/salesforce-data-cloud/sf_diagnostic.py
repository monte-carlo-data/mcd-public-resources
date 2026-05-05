#!/usr/bin/env python3
"""
Salesforce Data 360 — Diagnostic Script

Validates that all Salesforce APIs used by the lineage push script are
accessible and reports timing/volume data. No Monte Carlo credentials needed.

Usage:
  pip install requests python-dotenv
  python3 sf_diagnostic.py

Required env vars (or set in .env):
  SF_ORG_URL       — My Domain URL, e.g. https://mycompany.my.salesforce.com
  SF_CLIENT_ID     — Connected app consumer key
  SF_CLIENT_SECRET — Connected app consumer secret
"""
import base64
import io
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

SF_API_VERSION = "62.0"
METADATA_MAX_POLLS = 120
METADATA_POLL_INTERVAL = 5.0

SOAP_NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "met":     "http://soap.sforce.com/2006/04/metadata",
}
OSTM_NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}

_RETRIEVE_ENVELOPE = """\
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
            <met:members>*</met:members>
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


def check_auth(instance_url, client_id, client_secret):
    _sep("1. OAuth — client credentials")
    t0 = time.monotonic()
    resp = requests.post(
        f"{instance_url}/services/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=30,
    )
    data = resp.json()
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
            "field, it will fall back to the first data space returned here."
        )
    return spaces


def check_metadata_retrieve(instance_url, token):
    _sep("3. SOAP Metadata API — ObjectSourceTargetMap retrieve")

    # Submit retrieve job
    t0 = time.monotonic()
    envelope = _RETRIEVE_ENVELOPE.format(
        session_id=escape(token), api_version=escape(SF_API_VERSION)
    )
    url = f"{instance_url}/services/Soap/m/{SF_API_VERSION}"
    resp = requests.post(
        url,
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml", "SOAPAction": "retrieve"},
        timeout=60,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    fault = root.find(".//soapenv:Fault", SOAP_NS)
    if fault is not None:
        code = fault.findtext("faultcode", default="unknown")
        msg  = fault.findtext("faultstring", default="no detail")
        _fail(f"SOAP fault [{code}]: {msg}")
        sys.exit(1)

    async_id_el = root.find(".//met:id", SOAP_NS)
    if async_id_el is None:
        _fail("No async job ID returned from retrieve request")
        sys.exit(1)

    async_id = async_id_el.text
    _ok(f"Retrieve job submitted (id={async_id})")

    # Poll for completion
    zip_bytes = None
    for attempt in range(METADATA_MAX_POLLS):
        status_body = _STATUS_ENVELOPE.format(
            session_id=escape(token), async_id=escape(async_id)
        )
        status_resp = requests.post(
            url,
            data=status_body.encode("utf-8"),
            headers={"Content-Type": "text/xml", "SOAPAction": "checkRetrieveStatus"},
            timeout=60,
        )
        status_resp.raise_for_status()
        status_root = ET.fromstring(status_resp.text)

        done_el  = status_root.find(".//met:done",   SOAP_NS)
        state_el = status_root.find(".//met:status", SOAP_NS)
        state    = state_el.text if state_el is not None else "unknown"

        if done_el is not None and done_el.text == "true":
            if state == "Failed":
                err_code = status_root.findtext(".//met:errorStatusCode", default="", namespaces=SOAP_NS)
                err_msg  = status_root.findtext(".//met:errorMessage",    default="", namespaces=SOAP_NS)
                _fail(f"Retrieve job failed [{err_code}]: {err_msg}")
                sys.exit(1)
            zip_el = status_root.find(".//met:zipFile", SOAP_NS)
            if zip_el is None or not zip_el.text:
                _fail("Retrieve completed but ZIP payload is empty")
                sys.exit(1)
            elapsed = time.monotonic() - t0
            _ok(f"Retrieve completed after {attempt + 1} poll(s) ({elapsed:.1f}s)")
            zip_bytes = base64.b64decode(zip_el.text)
            break

        if attempt % 5 == 0:
            elapsed = time.monotonic() - t0
            print(f"  [....] Polling... attempt {attempt + 1}/{METADATA_MAX_POLLS} "
                  f"(status={state}, elapsed={elapsed:.0f}s)")
        time.sleep(METADATA_POLL_INTERVAL)
    else:
        _fail(
            f"Retrieve did not complete within "
            f"{METADATA_MAX_POLLS * METADATA_POLL_INTERVAL:.0f}s. "
            f"Consider increasing METADATA_MAX_POLLS in push_lineage.py."
        )
        sys.exit(1)

    return zip_bytes


def check_zip_contents(zip_bytes):
    _sep("4. ObjectSourceTargetMap — parse DLO→DMO edges")
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    all_names = zf.namelist()
    xml_files = [
        n for n in all_names
        if n.endswith(".objectSourceTargetMap") or n.endswith(".objectSourceTargetMap-meta.xml")
    ]
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
                root = ET.parse(f).getroot()
            except ET.ParseError:
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


def main():
    print("=" * 60)
    print("  Salesforce Data 360 — Lineage Push Diagnostic")
    print("=" * 60)

    instance_url  = os.environ.get("SF_ORG_URL", "").rstrip("/")
    client_id     = os.environ.get("SF_CLIENT_ID", "")
    client_secret = os.environ.get("SF_CLIENT_SECRET", "")

    missing = [n for n, v in [("SF_ORG_URL", instance_url), ("SF_CLIENT_ID", client_id), ("SF_CLIENT_SECRET", client_secret)] if not v]
    if missing:
        print(f"\n[FAIL] Missing required env vars: {', '.join(missing)}")
        print("       Set them in a .env file or export them before running.")
        sys.exit(1)

    token = check_auth(instance_url, client_id, client_secret)
    check_data_spaces(instance_url, token)
    zip_bytes = check_metadata_retrieve(instance_url, token)
    check_zip_contents(zip_bytes)

    _sep()
    print("  Diagnostic complete — all Salesforce APIs accessible.")
    print("=" * 60)


if __name__ == "__main__":
    main()

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
import concurrent.futures
import ipaddress
import os
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

SF_API_VERSION = "62.0"

# Redact long token-like strings before printing API response bodies. This script is
# meant to be shared with Salesforce admins and its output pasted into tickets/Slack,
# so mirror push_lineage.py's _safe_snippet and never echo raw bodies verbatim.
_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-]{60,}")


def _safe(text, max_len=200):
    return _TOKEN_RE.sub("[…]", text or "")[:max_len]


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
        verify=True,
    )
    try:
        data = resp.json()
    except ValueError:
        _fail(f"Non-JSON response from auth endpoint (HTTP {resp.status_code}): {_safe(resp.text)}")
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
            "Multiple data spaces found. The push script resolves each edge's data space from "
            "the Monte Carlo catalog (authoritative); SF_DEFAULT_DATA_SPACE (default: 'default') "
            "is only a last-resort fallback. Set it if your primary data space has a different name."
        )
    return spaces


def _enumerate_dmos(instance_url, token, max_dmos=150):
    """List Data Model Object developer names via the Data Cloud REST API (paginated).

    Bounded by max_dmos: this endpoint returns every installed standard DMO (often
    1,000+), so the diagnostic samples up to max_dmos just to prove read access
    quickly. The push script drives off the Monte Carlo catalog (complete/authoritative).
    """
    dmos, seen, offset, limit = [], set(), 0, 50
    for _ in range(400):  # safety cap
        try:
            resp = requests.get(
                f"{instance_url}/services/data/v{SF_API_VERSION}/ssot/data-model-objects",
                headers={"Authorization": f"Bearer {token}"},
                params={"limit": limit, "offset": offset},
                timeout=(10, 60), verify=True,
            )
        except requests.exceptions.RequestException as exc:
            _warn(f"data-model-objects page at offset {offset} failed ({exc}) — "
                  f"proceeding with the {len(dmos)} DMO(s) enumerated so far.")
            break
        if resp.status_code == 403:
            return None
        resp.raise_for_status()
        page = resp.json().get("dataModelObject", []) or []
        new = 0
        for d in page:
            name = d.get("name")
            if name and name not in seen:
                seen.add(name); dmos.append(name); new += 1
        if len(dmos) >= max_dmos or len(page) < limit or new == 0:
            break  # cap reached, last page, or offset not honored
        offset += limit
    return dmos[:max_dmos]


def check_dlo_dmo_mappings(instance_url, token):
    _sep("3. Data Cloud REST API — DLO->DMO mappings (read-only)")
    t0 = time.monotonic()
    try:
        max_dmos = max(1, int(os.environ.get("DIAG_MAX_DMOS", "150")))
    except ValueError:
        max_dmos = 150
    dmos = _enumerate_dmos(instance_url, token, max_dmos=max_dmos)
    if dmos is None:
        _warn("HTTP 403 listing data model objects — the connected app lacks Data Cloud API access.")
        _warn("Ensure the OAuth scope includes 'api' and the run-as user has a Data Cloud permission set.")
        return
    _ok(f"Enumerated {len(dmos)} data model object(s) in {time.monotonic() - t0:.1f}s")
    if not dmos:
        _warn("No data model objects returned — cannot check mappings.")
        return
    if len(dmos) >= max_dmos:
        _warn(f"Sampled the first {max_dmos} DMO(s) to prove access (org has more — raise "
              "DIAG_MAX_DMOS to check more). The push script uses the complete MC catalog.")

    _ok(f"Querying mappings for {len(dmos)} DMO(s) — read-only, no 'Modify Metadata' permission")
    t1 = time.monotonic()
    edges, errors, forbidden, checked = [], 0, False, 0

    def _one(dmo):
        r = requests.get(
            f"{instance_url}/services/data/v{SF_API_VERSION}/ssot/data-model-object-mappings",
            headers={"Authorization": f"Bearer {token}"},
            params={"dmoDeveloperName": dmo}, timeout=(10, 30), verify=True,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        return dmo, r.status_code, body

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_one, d): d for d in dmos}
        for fut in concurrent.futures.as_completed(futs):
            checked += 1
            try:
                dmo, sc, body = fut.result()
            except Exception:
                errors += 1
                continue
            if sc == 403:
                forbidden = True
                continue
            if sc == 404:
                continue  # DMO has no source mappings — normal
            if sc != 200 or not isinstance(body, dict):
                errors += 1
                continue
            for m in (body.get("objectSourceTargetMaps") or []):
                src = m.get("sourceEntityDeveloperName") or ""
                tgt = m.get("targetEntityDeveloperName") or ""
                if src.endswith("__dll") and tgt.endswith("__dlm"):
                    edges.append((src, tgt))

    if forbidden:
        _warn("HTTP 403 on data-model-object-mappings — the connected app cannot read DLO->DMO mappings.")
        _warn("This endpoint uses the standard Data Cloud API scope (no Metadata API / Modify Metadata).")
        return

    edges = sorted(set(edges))
    _ok(f"Queried {checked} DMO(s) in {time.monotonic() - t1:.1f}s")
    _ok(f"DLO->DMO edges found: {len(edges)}")
    if errors:
        _warn(f"{errors} DMO mapping query(ies) returned an unexpected status and were skipped.")
    if not edges:
        _warn("No DLO->DMO mappings found. Confirm DLO->DMO mappings exist in your Data Cloud org.")
        return
    print()
    print("  DLO -> DMO edges (data space is resolved from the MC catalog at push time):")
    for src, tgt in edges:
        print(f"    {src} -> {tgt}")
    _warn(
        "Note: this list is enumerated from /ssot/data-model-objects, which can omit a few "
        "mapped DMOs; the push script drives off the Monte Carlo catalog and is authoritative."
    )


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
            _fail(f"HTTP {resp.status_code} (page {page}): {_safe(resp.text)}")
            return

        try:
            data = resp.json()
        except ValueError:
            _fail(f"Non-JSON response (page {page}): {_safe(resp.text)}")
            return

        collection = data.get("collection", {})
        batch = collection.get("items", [])
        items.extend(batch)

        raw_next = collection.get("nextPageUrl") or None
        if raw_next:
            # Salesforce sometimes returns nextPageUrl with null-valued params (e.g.
            # definitionType=null) which it then rejects on the subsequent request.
            _pn = urlparse(raw_next)
            _clean_qs = urlencode(
                {k: [x for x in v if x not in ("null", "")]
                 for k, v in parse_qs(_pn.query, keep_blank_values=True).items()
                 if any(x not in ("null", "") for x in v)},
                doseq=True,
            )
            raw_next = urlunparse(_pn._replace(query=_clean_qs))
            parsed_next = urlparse(raw_next)
            if not parsed_next.scheme:
                if not raw_next.startswith("/"):
                    _fail(f"Unexpected relative nextPageUrl (does not start with '/'): {_safe(raw_next)!r} — pagination stopped, CIO list above may be incomplete.")
                    return
                next_url = f"{instance_url.rstrip('/')}{raw_next}"
            elif parsed_next.scheme != "https" or parsed_next.hostname != parsed_base.hostname:
                _fail(f"Cross-origin or non-HTTPS nextPageUrl: {_safe(raw_next)!r} — pagination stopped, CIO list above may be incomplete.")
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
    check_dlo_dmo_mappings(instance_url, token)
    check_calculated_insights(instance_url, token)

    _sep()
    print("  Diagnostic complete — all Salesforce APIs accessible.")
    print("=" * 60)


if __name__ == "__main__":
    main()

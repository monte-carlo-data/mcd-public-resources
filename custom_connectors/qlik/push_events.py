#!/usr/bin/env python3
"""
Collect Qlik Cloud BI assets — including their lineage — and push them to
Monte Carlo via the BI Push Ingest API (``POST /ingest/v1/bi/metadata``).

Use this script for lineage on larger Qlik tenants. In Qlik Cloud, lineage
comes from a dedicated API — ``/api/v1/lineage-graphs/nodes/{qri}``, one
request per asset — that is rate-limited to roughly **20 requests per
minute**, far below every other Qlik API. Collecting lineage in one pass is
therefore slow by nature: this script does the asset catalog first (fast)
and then resolves lineage one asset at a time, paced to stay under the
limit, before pushing everything to Monte Carlo.

Assets are keyed by their Qlik resource id, so re-running the script
updates existing assets instead of duplicating them — run it on a
schedule (cron, CI) to keep lineage current.

The script is fully standalone: it needs only ``pycarlo``, ``requests``,
and your Qlik credentials.

Prerequisites:
    1. Install pycarlo (>= 0.15.240). Until that version is on PyPI,
       install from GitHub:
         pip install git+https://github.com/monte-carlo-data/python-sdk.git
    2. Create an integration key with Ingestion scope for your Custom BI
       (Push) integration — see the Monte Carlo docs page for this example.

Monte Carlo credentials (environment variables):
    export MCD_DEFAULT_API_ID=<INTEGRATION_KEY_ID>
    export MCD_DEFAULT_API_TOKEN=<INTEGRATION_KEY_SECRET>

Qlik credentials: a ``credentials.json`` file next to this script (see
``credentials_example.json``) with an OAuth2 ``client_id`` /
``client_secret`` pair, or an ``api_key``.

Usage:
    export MCD_DEFAULT_API_ID=... MCD_DEFAULT_API_TOKEN=...
    python push_events.py --resource-uuid <CUSTOM_BI_CONNECTOR_CONTAINER_UUID>

    # --resource-uuid may instead be supplied via MCD_BI_CONTAINER_UUID.
    # Qlik-side options:
    #   --interval 4     seconds between lineage calls (default 4, ~15/min)
    #   --batch-size 100 events per push request (max 100)
    #   --space-id ...   restrict collection to one Qlik space
    #   --no-lineage     push metadata only, without lineage (fast path)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import requests
from pycarlo.core import Client, Session
from pycarlo.features.ingestion import (
    AssetRef,
    BiAsset,
    BiAssetRef,
    BiOwner,
    IngestionService,
)

DEFAULT_RESOURCE_TYPE = "custom-bi-connector"
# The public Integration Gateway every push is sent to. (The pycarlo Session
# also honors the MCD_API_ENDPOINT environment variable, which overrides
# this — useful for internal testing.)
_ENDPOINT = "https://integrations.getmontecarlo.com"

# Qlik's catalog page ceiling; requesting more is rejected.
_PAGE_SIZE = 100
# Lineage-graphs is tiered at ~20 req/min; 4s between calls keeps us at
# ~15/min with headroom.
_DEFAULT_INTERVAL = 4.0
_MAX_BATCH = 100  # the push API accepts 1-100 events per request
_MAX_PAGES = 10000  # backstop against a server that never ends `links.next`

# /items resourceType -> asset_type label. A data flow is an `app` whose
# resourceSubType is `dataflow-prep`.
_RESOURCE_TYPE_LABELS = {
    "app": "app",
    "qlikview": "qlikview",
    "qvapp": "qvapp",
    "dataset": "dataset",
}
_SUBTYPE_LABELS = {("app", "dataflow-prep"): "dataflow"}

# QRI schemes addressing another BI asset (app, data flow, dataset) — an
# upstream node with one of these becomes a BI->BI ref, not a table input.
_BI_ASSET_QRI_SCHEMES = ("qdf", "app")


########################################
# Qlik REST client
########################################


class QlikClient:
    """Minimal Qlik Cloud REST client: auth, GET with retries, pagination."""

    def __init__(self, credentials: dict):
        self._base = str(credentials["tenant_url"]).rstrip("/")
        self._api_key = credentials.get("api_key") or None
        self._client_id = credentials.get("client_id") or None
        self._client_secret = credentials.get("client_secret") or None
        if self._api_key and (self._client_id or self._client_secret):
            raise ValueError("Provide either api_key or client_id/client_secret, not both")
        if not self._api_key and not (self._client_id and self._client_secret):
            raise ValueError("Credentials must include either api_key, or both client_id and client_secret")
        self._session = requests.Session()
        self._token_expiry = 0.0
        # Fail fast on bad credentials or a wrong tenant URL.
        self.get("/api/v1/items", params={"limit": 1})

    @property
    def base(self) -> str:
        return self._base

    def close(self) -> None:
        self._session.close()

    def _token(self) -> str:
        if self._api_key:
            return self._api_key
        if time.monotonic() < self._token_expiry:
            return self._access_token
        resp = requests.post(
            f"{self._base}/oauth/token",
            json={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._token_expiry = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self._access_token

    def get(self, path_or_url: str, params: Optional[dict] = None) -> dict:
        """GET a path (or absolute cursor URL), retrying on 401/429."""
        url = path_or_url if path_or_url.startswith("http") else f"{self._base}{path_or_url}"
        for attempt in range(4):
            resp = self._session.get(
                url, headers={"Authorization": f"Bearer {self._token()}"}, params=params, timeout=60
            )
            last = attempt == 3
            if resp.status_code == 401 and not self._api_key and not last:
                self._token_expiry = 0.0
                continue
            if resp.status_code == 429 and not last:
                try:
                    time.sleep(min(30, max(1, int(resp.headers.get("Retry-After", "5")))))
                except ValueError:
                    time.sleep(5)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable: retry loop exhausted")

    def paginate(self, path: str, params: Optional[dict] = None) -> list:
        """Walk a cursor-paginated collection, guarding against a loop."""
        url: Optional[str] = f"{self._base}{path}"
        results, seen = [], set()
        while url and url not in seen and len(seen) < _MAX_PAGES:
            seen.add(url)
            body = self.get(url, params=params)
            params = None
            results.extend(body.get("data") or [])
            url = ((body.get("links") or {}).get("next") or {}).get("href")
        return results


########################################
# Collection
########################################


def collect_assets(qlik: QlikClient, space_id: Optional[str], with_lineage: bool, interval: float) -> list[BiAsset]:
    """Build the full BiAsset list — catalog walk first, then paced lineage."""

    # Pass 1: catalog (cheap, ~1000 req/min). Everything except lineage.
    params: dict = {"limit": _PAGE_SIZE}
    if space_id:
        params["spaceId"] = space_id
    items = [
        item
        for item in qlik.paginate("/api/v1/items", params)
        if item.get("resourceType") in _RESOURCE_TYPE_LABELS and item.get("resourceId") and item.get("name")
    ]

    # Index emitted assets two ways so lineage nodes match regardless of QRI
    # form: by resource id (`qri:app:*` embeds it) and by full QRI (a `qri:qdf:*`
    # dataset's space-scoped secureQri does not).
    qri_index: dict = {item["resourceId"]: item["resourceId"] for item in items}
    qris = {}
    for item in items:
        qri = _lineage_qri(item)
        if qri:
            qri_index[qri] = item["resourceId"]
            qris[item["resourceId"]] = qri

    assets = {}
    for item in items:
        source_id = item["resourceId"]
        views = (item.get("itemViews") or {}).get("total")
        assets[source_id] = BiAsset(
            asset_source_id=source_id,
            name=item["name"],
            asset_type=_SUBTYPE_LABELS.get(
                (item["resourceType"], item.get("resourceSubType") or ""),
                _RESOURCE_TYPE_LABELS[item["resourceType"]],
            ),
            description=item.get("description") or None,
            asset_url=((item.get("links") or {}).get("open") or {}).get("href"),
            created_time=_iso_utc(item.get("resourceCreatedAt") or item.get("createdAt")),
            last_modified_time=_iso_utc(item.get("resourceUpdatedAt") or item.get("updatedAt")),
            view_count=views if isinstance(views, int) and not isinstance(views, bool) else None,
            owner=BiOwner(source_id=item.get("ownerId")) if item.get("ownerId") else None,
            attributes={k: v for k, v in (
                ("resource_type", item.get("resourceType")),
                ("resource_sub_type", item.get("resourceSubType")),
                ("space_id", item.get("spaceId")),
            ) if v} or None,
        )

    # Pass 2: lineage (rate-limited) — one paced call per asset with a QRI.
    if with_lineage:
        pace = f" at ~{60 / interval:.0f} calls/min" if interval > 0 else " (unpaced)"
        print(f"resolving lineage for {len(qris)} asset(s){pace}")
        last_call = 0.0
        for i, (source_id, qri) in enumerate(sorted(qris.items()), 1):
            wait = interval - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
            last_call = time.monotonic()
            upstream, inputs = _lineage(qlik, qri, qri_index)
            asset = assets[source_id]
            asset.upstream_assets = upstream
            asset.inputs = inputs
            if i % 10 == 0 or i == len(qris):
                print(f"  {i}/{len(qris)} lineage graphs resolved")

    return [assets[item["resourceId"]] for item in items]


def _lineage(qlik: QlikClient, qri: str, qri_index: dict) -> tuple[list[BiAssetRef], list[AssetRef]]:
    """Resolve one asset's upstream graph. Best-effort: a 404 (no Catalog
    entitlement) yields empty lineage rather than failing the run."""
    url = f"{qlik.base}/api/v1/lineage-graphs/nodes/{quote(qri, safe='')}"
    try:
        body = qlik.get(url, params={"level": "table", "up": -1, "collapse": "true"})
    except requests.HTTPError:
        return [], []

    wrapped = body.get("graph")
    graph = wrapped if isinstance(wrapped, dict) else body
    nodes = graph.get("nodes") or {}
    if not isinstance(nodes, dict):
        return [], []

    upstream, inputs = {}, {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata") or {}
        node_qri = metadata.get("id") or ""
        # The requested asset is its own root node (its graph id carries a
        # "#<hash>" suffix) — not its own upstream.
        if node_qri == qri or node_qri.startswith(qri + "#"):
            continue
        ref_id = _qri_resource_id(node_qri)
        is_bi_asset = ref_id is not None and _qri_scheme(node_qri) in _BI_ASSET_QRI_SCHEMES
        target = qri_index.get(node_qri.split("#", 1)[0]) or (qri_index.get(ref_id) if ref_id else None)
        if is_bi_asset and target:
            upstream[target] = BiAssetRef(asset_source_id=target, relationship_type="DERIVES_FROM")
        elif metadata.get("type") == "TABLE" and not is_bi_asset and not metadata.get("internal"):
            # A table node that isn't a BI asset is a warehouse input;
            # Qlik-internal nodes (data-flow plumbing) carry no warehouse identity.
            fqn = _unquote(metadata.get("queryExpression") or node.get("label"))
            if fqn:
                inputs[fqn] = AssetRef(asset_type="TABLE", role="INPUT", fully_qualified_name=fqn)
    return list(upstream.values()), list(inputs.values())


########################################
# Small helpers
########################################


def _lineage_qri(item: dict) -> Optional[str]:
    """The QRI addressing this item in the lineage graph, if it has one."""
    if item.get("resourceType") == "app" and item.get("resourceId"):
        scheme = "dataflow" if item.get("resourceSubType") == "dataflow-prep" else "sense"
        return f"qri:app:{scheme}://{item['resourceId']}"
    return (item.get("resourceAttributes") or {}).get("secureQri") or None


def _qri_scheme(qri: str) -> Optional[str]:
    parts = qri.split(":")
    return parts[1] if len(parts) > 1 else None


def _qri_resource_id(qri: str) -> Optional[str]:
    if "://" not in qri:
        return None
    return qri.split("://", 1)[1].split("#", 1)[0] or None


def _iso_utc(value) -> Optional[str]:
    """Normalize a Qlik timestamp to a timezone-aware ISO-8601 string."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _unquote(name) -> Optional[str]:
    """Strip SQL identifier quoting (``"DB"."SCHEMA"."T"`` -> ``DB.SCHEMA.T``)."""
    if not name:
        return None
    parts = [p.strip().strip('"').strip("`") for p in str(name).split(".")]
    return ".".join(p for p in parts if p) or None


########################################
# Entry point
########################################


def _require(value: Optional[str], name: str) -> str:
    if not value:
        print(f"Error: {name} is required and was not provided. See the module docstring for setup.", file=sys.stderr)
        raise SystemExit(2)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "Qlik Cloud BI metadata push script").split("\n")[0]
    )
    parser.add_argument(
        "--resource-uuid",
        default=os.environ.get("MCD_BI_CONTAINER_UUID"),
        help="custom-bi-connector container UUID (or set MCD_BI_CONTAINER_UUID)",
    )
    parser.add_argument(
        "--resource-type",
        default=DEFAULT_RESOURCE_TYPE,
        help=f"Resource type (default: {DEFAULT_RESOURCE_TYPE})",
    )
    parser.add_argument("--interval", type=float, default=_DEFAULT_INTERVAL,
                        help=f"seconds between lineage calls (default {_DEFAULT_INTERVAL}, ~15/min)")
    parser.add_argument("--batch-size", type=int, default=_MAX_BATCH,
                        help=f"events per push request, max {_MAX_BATCH} (default {_MAX_BATCH})")
    parser.add_argument("--space-id", default=None, help="restrict collection to one Qlik space id")
    parser.add_argument("--no-lineage", action="store_true", help="push metadata only, without lineage")
    args = parser.parse_args()
    args.batch_size = min(args.batch_size, _MAX_BATCH)

    key_id = _require(os.environ.get("MCD_DEFAULT_API_ID"), "MCD_DEFAULT_API_ID (env)")
    key_token = _require(os.environ.get("MCD_DEFAULT_API_TOKEN"), "MCD_DEFAULT_API_TOKEN (env)")
    resource_uuid = _require(args.resource_uuid, "--resource-uuid / MCD_BI_CONTAINER_UUID")

    client = Client(
        session=Session(mcd_id=key_id, mcd_token=key_token, endpoint=_ENDPOINT, scope="Ingestion")
    )

    creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
    if not os.path.isfile(creds_path):
        sys.exit(f"Qlik credentials file not found: {creds_path} (see credentials_example.json)")
    with open(creds_path) as f:
        credentials = json.load(f)["connect_args"]
    # --space-id overrides the credentials file's own space_id, if set there.
    space_id = args.space_id or credentials.get("space_id") or None

    print(f"Collecting from {credentials['tenant_url']} ...")
    qlik = QlikClient(credentials)
    try:
        assets = collect_assets(qlik, space_id, with_lineage=not args.no_lineage, interval=args.interval)
    finally:
        qlik.close()
    print(f"{len(assets)} asset(s) collected")

    service = IngestionService(mc_client=client)
    print(f"Pushing to {client.session_endpoint} ...")
    pushed = 0
    for i in range(0, len(assets), args.batch_size):
        batch = assets[i : i + args.batch_size]
        result = service.send_bi_metadata(
            resource_uuid=resource_uuid,
            resource_type=args.resource_type,
            events=batch,
        )
        pushed += len(batch)
        invocation_id = IngestionService.extract_invocation_id(result)
        print(f"  pushed {pushed}/{len(assets)}" + (f" (invocation {invocation_id})" if invocation_id else ""))
    print("\nDone.")


if __name__ == "__main__":
    main()

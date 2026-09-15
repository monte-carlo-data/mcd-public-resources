"""Qlik Cloud BI connector (tenant-scoped, API-key or OAuth2 M2M auth).

Emits the BI assets a Qlik Cloud tenant holds, read from the unified catalog
(``GET /api/v1/items``) rather than per-resource endpoints — there is no
``GET /api/v1/apps`` list endpoint, so ``/items`` is the only enumeration
surface:

- **app** — a Qlik Sense app (the dashboard-equivalent)
- **qlikview** / **qvapp** — a QlikView document
- **dataset** — a Qlik dataset (QVD, data file, or catalogued table)

Sheets *inside* an app are not emitted: no REST endpoint enumerates them, and
reaching them needs the Engine JSON-RPC API over a WebSocket. The app is the
leaf asset.

**Pagination.** ``/items`` is cursor-paginated (``links.next.href``) with no
numeric offset, so the connector cannot random-access a page. The full item
list is walked once, sorted, and cached on the instance; ``fetch_metadata``
then slices that list. Cursors are opaque, so this is the only way to honor an
``(limit, offset)`` contract consistently.

**Lineage.** Both BI-to-BI edges and warehouse ``inputs`` come from one
``GET /api/v1/lineage-graphs/nodes/{qri}`` call per asset: upstream ``DATASET``
nodes become ``DERIVES_FROM`` refs (matched back to emitted assets by QRI), and
upstream ``TABLE`` nodes become ``inputs``. Two caveats, both documented in
README.md:

- That endpoint is rate-limited to **20 requests/minute** — far tighter than
  the 1000/min the catalog reads get — so lineage is resolved only for assets
  on the requested page, and can be switched off entirely with the
  ``collect_lineage`` credential.
- Qlik lineage nodes carry a ``label`` and an optional ``filePath``, but no
  documented database/schema attributes. Table names therefore arrive
  **unqualified**, and Monte Carlo may only partially match them. Where exact
  lineage matters, prefer SQL query tagging.

**Auth** is either a tenant API key or an OAuth2 M2M client-credentials pair.
Neither is least-privilege: an API key inherits *all* permissions of the user
who minted it, and an M2M OAuth client is granted Tenant Admin by default. See
README.md for how to scope one down.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

# Qlik's catalog page ceiling; requesting more is rejected.
_PAGE_SIZE = 100

_TOKEN_EXPIRY_SKEW_SECONDS = 60

# Retries cover a 401 (stale OAuth token) and 429s (rate limiting).
_MAX_ATTEMPTS = 4
_DEFAULT_RETRY_AFTER = 5
# Qlik's Retry-After is trusted only up to this; a pathological value would
# otherwise stall a whole collection run.
_MAX_RETRY_AFTER = 60

# /items resourceType -> the asset_type label emitted for it. Items outside
# this map (automations, notes, glossaries, ML experiments...) are not BI
# assets and are skipped.
_RESOURCE_TYPE_LABELS = {
    "app": "app",
    "qlikview": "qlikview",
    "qvapp": "qvapp",
    "dataset": "dataset",
}

# (resourceType, resourceSubType) overrides for the label map above. A Qlik
# data flow is an `app` whose subtype is `dataflow-prep`; labeling it `dataflow`
# keeps it distinct from a dashboard-style app so an app's lineage can point at
# the data flow it reads from.
_SUBTYPE_LABELS = {
    ("app", "dataflow-prep"): "dataflow",
}

# Lineage node types, keyed off metadata.type.
_NODE_TYPE_TABLE = "TABLE"

# QRI schemes that address another BI asset (so an upstream node with one of
# these becomes a BI-to-BI ref, matched to an emitted asset by resource id).
# `qdf` covers datasets/QVDs; `app` covers apps and data flows. Everything else
# (`db`, ...) is treated as a warehouse table input.
_BI_ASSET_QRI_SCHEMES = ("qdf", "app")


class Connector:
    """BI connector for Qlik Cloud apps, QlikView documents, and datasets."""

    credentials: dict

    ########################################
    # Connection Related Methods
    ########################################

    def setup_connection(self) -> None:
        """Initialize the session and validate credentials against the tenant.

        Reads from ``self.credentials`` (``connect_args`` in credentials.json):

        - ``tenant_url`` (required) — tenant base URL, e.g.
          ``https://mytenant.us.qlikcloud.com``
        - ``api_key`` — a tenant API key; mutually exclusive with the OAuth pair
        - ``client_id`` / ``client_secret`` — OAuth2 M2M client credentials
        - ``space_id`` (optional) — restrict collection to a single space;
          omit to collect the whole tenant
        - ``collect_lineage`` (optional, default ``true``) — resolve lineage
          graphs; set ``false`` to skip the 20 req/min endpoint entirely
        """
        self._base = str(self.credentials["tenant_url"]).rstrip("/")
        self._api_key = self.credentials.get("api_key") or None
        self._client_id = self.credentials.get("client_id") or None
        self._client_secret = self.credentials.get("client_secret") or None
        self._space_id = self.credentials.get("space_id") or None
        self._collect_lineage = self.credentials.get("collect_lineage", True)

        if self._api_key and (self._client_id or self._client_secret):
            raise ValueError(
                "Provide either api_key or client_id/client_secret, not both"
            )
        if not self._api_key and not (self._client_id and self._client_secret):
            raise ValueError(
                "Credentials must include either api_key, or both client_id "
                "and client_secret"
            )

        self._session = requests.Session()
        # (access_token, monotonic-expiry) for OAuth; unused with an API key.
        self._oauth: Optional[Tuple[str, float]] = None
        # Resolved lazily and memoized: owner id -> owner dict (or None).
        self._owners: Dict[str, Optional[dict]] = {}
        # Space id -> name (or None on a failed lookup), memoized per session.
        self._spaces: Dict[str, Optional[str]] = {}
        # Built once and sliced across pages so paging stays consistent for
        # the life of the connector session.
        self._stubs: Optional[List[dict]] = None
        # dataset QRI -> asset_source_id, for matching lineage nodes back to
        # assets this connector actually emits.
        self._qri_index: Dict[str, str] = {}

        # Fail fast on bad credentials, a wrong tenant URL, or a missing scope.
        self._get(f"{self._base}/api/v1/items", params={"limit": 1})

    def close_connection(self) -> None:
        """Close the HTTP session."""
        session = getattr(self, "_session", None)
        if session is not None:
            session.close()

    ########################################
    # Auth + HTTP helpers
    ########################################

    def _token(self) -> str:
        """Return a bearer token, exchanging/refreshing OAuth credentials."""
        if self._api_key:
            return self._api_key
        if self._oauth and time.monotonic() < self._oauth[1]:
            return self._oauth[0]
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
        expires_in = int(payload.get("expires_in", 3600))
        self._oauth = (
            payload["access_token"],
            time.monotonic() + expires_in - _TOKEN_EXPIRY_SKEW_SECONDS,
        )
        return self._oauth[0]

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        """GET a Qlik REST URL, refreshing on 401 and backing off on 429.

        Qlik assesses rate limits per tier over a five-minute window and
        returns ``Retry-After`` on a 429, so the advertised delay is honored
        rather than guessed at.
        """
        for attempt in range(_MAX_ATTEMPTS):
            resp = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._token()}"},
                params=params,
                timeout=60,
            )
            last = attempt == _MAX_ATTEMPTS - 1
            if resp.status_code == 401 and not self._api_key and not last:
                self._oauth = None
                continue
            if resp.status_code == 429 and not last:
                time.sleep(_retry_after(resp))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Unreachable: request retry loop exhausted")  # pragma: no cover

    def _paginate(self, path: str, params: Optional[dict] = None) -> List[dict]:
        """Walk a cursor-paginated collection, returning every ``data`` entry.

        The ``links.next.href`` cursor already carries the original query
        string, so ``params`` is applied to the first request only.
        """
        url: Optional[str] = f"{self._base}{path}"
        results: List[dict] = []
        while url:
            body = self._get(url, params=params)
            params = None
            results.extend(body.get("data") or [])
            url = ((body.get("links") or {}).get("next") or {}).get("href")
        return results

    ########################################
    # Metadata Fetching
    ########################################

    def fetch_metadata(self, limit: int, offset: int) -> List[dict]:
        """Fetch Qlik asset metadata for one page of the tenant.

        Assets are sorted by ``(asset_type, asset_source_id)`` so paging is
        stable. Owners and lineage are resolved only for the assets on the
        requested page, keeping the rate-limited lineage endpoint off the path
        for assets the caller did not ask for.

        Args:
            limit: Maximum number of assets to return.
            offset: Number of assets to skip.

        Returns:
            List of sparse ``BiAsset`` dicts.
        """
        if self._stubs is None:
            self._stubs = self._build_stubs()

        page = self._stubs[offset : offset + limit]
        return [self._finalize(stub) for stub in page]

    def _build_stubs(self) -> List[dict]:
        """Build the full, ordered asset list from the tenant's catalog."""
        params: dict = {"limit": _PAGE_SIZE}
        if self._space_id:
            params["spaceId"] = self._space_id
        items = [
            item
            for item in self._paginate("/api/v1/items", params)
            if item.get("resourceType") in _RESOURCE_TYPE_LABELS
        ]

        stubs = [self._stub(item) for item in items]
        stubs = [stub for stub in stubs if stub is not None]

        # Index every emitted asset by resource id so an upstream lineage node
        # (whose QRI embeds that id) can be matched back to an asset_source_id.
        # Refs to anything absent here are dropped rather than emitted dangling.
        self._qri_index = {stub["asset_source_id"]: stub["asset_source_id"] for stub in stubs}

        return sorted(stubs, key=lambda a: (a["asset_type"], a["asset_source_id"]))

    def _stub(self, item: dict) -> Optional[dict]:
        """Build an asset stub from one catalog item.

        ``resourceId`` is preferred over the catalog ``id`` as the identity
        seed: it is the underlying resource's own GUID, it survives the item
        being re-catalogued, and it is what the lineage QRI and the app web URL
        are built from.
        """
        source_id = item.get("resourceId") or item.get("id")
        name = item.get("name")
        if not source_id or not name:
            return None

        resource_type = item["resourceType"]
        space_id = item.get("spaceId")
        views = (item.get("itemViews") or {}).get("total")
        sub_type = item.get("resourceSubType") or ""
        return {
            "asset_source_id": source_id,
            "name": name,
            "asset_type": _SUBTYPE_LABELS.get(
                (resource_type, sub_type),
                _RESOURCE_TYPE_LABELS[resource_type],
            ),
            "description": item.get("description"),
            "asset_url": ((item.get("links") or {}).get("open") or {}).get("href"),
            # Qlik's container for an asset is its space; resolved per page in
            # _finalize (the bot can't list spaces, so the name is fetched by
            # id). Personal-space items carry no spaceId and are folder-less.
            "_space_id": space_id,
            "created_time": _iso_utc(item.get("resourceCreatedAt") or item.get("createdAt")),
            "last_modified_time": _iso_utc(
                item.get("resourceUpdatedAt") or item.get("updatedAt")
            ),
            # itemViews counts the trailing 28 days, not all time.
            "view_count": views if isinstance(views, int) and not isinstance(views, bool) else None,
            "attributes": _attributes(item),
            # Popped in _finalize — resolved per page, not per tenant.
            "_owner_id": item.get("ownerId"),
            "_qri": _lineage_qri(item, resource_type),
        }

    def _finalize(self, stub: dict) -> dict:
        """Resolve a stub's owner and lineage, then drop empty keys."""
        asset = dict(stub)
        owner_id = asset.pop("_owner_id", None)
        space_id = asset.pop("_space_id", None)
        qri = asset.pop("_qri", None)

        if owner_id:
            asset["owner"] = self._owner(owner_id)
        if space_id:
            asset["folder"] = self._space_name(space_id)
        if qri and self._collect_lineage:
            upstream, inputs = self._lineage(qri)
            asset["upstream_assets"] = upstream
            asset["inputs"] = inputs

        return {k: v for k, v in asset.items() if v is not None and v != []}

    ########################################
    # Reference data
    ########################################

    def _space_name(self, space_id: str) -> Optional[str]:
        """Resolve a space id to its name, memoized per session.

        The bot typically can't list ``/spaces`` (that needs a tenant-wide
        scope), so names are fetched one space at a time — and only for the
        spaces the page's assets actually live in. A failed lookup yields
        ``None`` (folder omitted) rather than failing the run.
        """
        if space_id in self._spaces:
            return self._spaces[space_id]
        try:
            body = self._get(f"{self._base}/api/v1/spaces/{space_id}")
        except requests.HTTPError:
            body = {}
        name = body.get("name") or None
        self._spaces[space_id] = name
        return name

    def _owner(self, owner_id: str) -> Optional[dict]:
        """Resolve an owner id to an owner dict, memoized per session.

        Owners are looked up one at a time rather than by listing every user in
        the tenant: the distinct owners of a page of assets are few, and the
        user list can be very large.
        """
        if owner_id in self._owners:
            return self._owners[owner_id]
        try:
            user = self._get(f"{self._base}/api/v1/users/{owner_id}")
        except requests.HTTPError:
            user = {}
        owner = {
            key: value
            for key, value in (
                ("email", user.get("email")),
                ("name", user.get("name")),
                # The raw owner id is useful identity signal even when the user
                # lookup is forbidden (a bot without tenant-wide user scope).
                ("source_id", owner_id),
            )
            if value
        }
        self._owners[owner_id] = owner or None
        return self._owners[owner_id]

    ########################################
    # Lineage
    ########################################

    def _lineage(self, qri: str) -> Tuple[List[dict], List[dict]]:
        """Resolve one asset's upstream graph into BI refs and warehouse inputs.

        ``up=-1`` walks the whole upstream chain, so an app reaches the tables
        behind its datasets as well as the datasets themselves. Returns
        ``([], [])`` on any error — a tenant without the Catalog entitlement
        404s here, and lineage is enrichment rather than a hard requirement.
        """
        url = f"{self._base}/api/v1/lineage-graphs/nodes/{quote(qri, safe='')}"
        try:
            body = self._get(url, params={"level": "table", "up": -1, "collapse": "true"})
        except requests.HTTPError:
            return [], []

        upstream: Dict[str, dict] = {}
        inputs: Dict[str, dict] = {}
        for node_qri, node in _nodes(body):
            metadata = node.get("metadata") or {}
            # The requested asset is its own root node and not its own input.
            # The root's id in the graph carries a "#<hash>" suffix, so the
            # match is by prefix rather than exact.
            if node_qri == qri or (node_qri or "").startswith(qri + "#"):
                continue
            node_type = metadata.get("type")
            # The asset an app/qdf QRI points at is identified by the resource
            # id between the scheme and the "#<hash>" suffix.
            ref_id = _qri_resource_id(node_qri)
            is_bi_asset = ref_id is not None and _qri_scheme(node_qri) in _BI_ASSET_QRI_SCHEMES

            if is_bi_asset and ref_id in self._qri_index:
                upstream[ref_id] = {
                    "asset_source_id": ref_id,
                    "relationship_type": "DERIVES_FROM",
                }
            elif node_type == _NODE_TYPE_TABLE and not is_bi_asset:
                # A table node that isn't a BI asset is a warehouse input.
                # Internal nodes (Qlik's own QDF plumbing, e.g. a data flow's
                # staged output) carry no warehouse identity, so they're dropped.
                if metadata.get("internal"):
                    continue
                ref = _table_input(node, metadata)
                if ref:
                    inputs[ref["fully_qualified_name"]] = ref

        return list(upstream.values()), list(inputs.values())


########################################
# Small helpers
########################################


def _retry_after(resp: requests.Response) -> float:
    """Seconds to wait after a 429, clamped to a sane ceiling."""
    try:
        delay = int(resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER))
    except ValueError:
        delay = _DEFAULT_RETRY_AFTER
    return max(1, min(delay, _MAX_RETRY_AFTER))


def _iso_utc(value: Optional[str]) -> Optional[str]:
    """Normalize a Qlik timestamp to a timezone-aware ISO-8601 string.

    Qlik returns ``2022-11-11T01:08:54.000Z``; the ``Z`` suffix is not accepted
    by ``fromisoformat`` before Python 3.11, and the ingestion validators
    reject naive datetimes.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _attributes(item: dict) -> Optional[dict]:
    """Carry the catalog fields that classify an item but have no BiAsset home."""
    attributes = {
        key: value
        for key, value in (
            ("resource_type", item.get("resourceType")),
            ("resource_sub_type", item.get("resourceSubType")),
            ("space_id", item.get("spaceId")),
            ("reload_status", item.get("resourceReloadStatus")),
        )
        if value
    }
    return attributes or None


def _lineage_qri(item: dict, resource_type: str) -> Optional[str]:
    """Build the QRI that addresses an item in the lineage graph.

    Lineage nodes are keyed by QRI, not by resource id. A Sense app's QRI is
    derived from its GUID; a dataset's is published by the catalog as
    ``resourceAttributes.secureQri``. QlikView documents have neither, so they
    are emitted without lineage.
    """
    if resource_type == "app" and item.get("resourceId"):
        # A data flow is an `app` resource addressed by the `dataflow` scheme,
        # not `sense` — using `sense` for one makes the lineage lookup 404.
        scheme = "dataflow" if item.get("resourceSubType") == "dataflow-prep" else "sense"
        return f"qri:app:{scheme}://{item['resourceId']}"
    secure_qri = (item.get("resourceAttributes") or {}).get("secureQri")
    return secure_qri or None


def _qri_scheme(qri: Optional[str]) -> Optional[str]:
    """Return the QRI scheme (``app``/``qdf``/``db``/...) from ``qri:<scheme>:...``."""
    if not qri:
        return None
    parts = qri.split(":")
    return parts[1] if len(parts) > 1 else None


def _qri_resource_id(qri: Optional[str]) -> Optional[str]:
    """Return the resource id a ``qri:app:*``/``qri:qdf:*`` node points at.

    The id sits between the ``://`` and the ``#<hash>`` suffix. Returns ``None``
    for QRIs without an ``://`` part (and thus no clean resource id).
    """
    if not qri or "://" not in qri:
        return None
    return qri.split("://", 1)[1].split("#", 1)[0] or None


def _nodes(body: dict) -> List[Tuple[str, dict]]:
    """Yield ``(qri, node)`` pairs from a lineage graph payload.

    The graph is returned under a ``graph`` key, with ``nodes`` an object keyed
    by QRI. Both the wrapper and the keying are tolerated in either form so a
    payload shape change degrades (returns nothing) rather than raises.
    """
    wrapped = body.get("graph")
    graph = wrapped if isinstance(wrapped, dict) else body
    nodes = graph.get("nodes") or {}
    if isinstance(nodes, dict):
        return [
            ((node.get("metadata") or {}).get("id") or key, node)
            for key, node in nodes.items()
            if isinstance(node, dict)
        ]
    if isinstance(nodes, list):
        pairs = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            qri = (node.get("metadata") or {}).get("id")
            if qri:
                pairs.append((qri, node))
        return pairs
    return []


def _table_input(node: dict, metadata: dict) -> Optional[dict]:
    """Build a warehouse input ref from an upstream lineage table node.

    A ``filePath`` is the strongest identifier (typed ``FILE``). For a
    relational source the ``queryExpression`` (e.g. ``"DB"."SCHEMA"."TABLE"``)
    is preferred over the node ``label`` — it carries the qualified name, which
    the label may not. SQL identifier quoting is stripped so the FQN Monte Carlo
    matches on is ``DB.SCHEMA.TABLE``, not a quoted string.
    """
    file_path = metadata.get("filePath")
    if file_path:
        return {
            "asset_type": "FILE",
            "role": "INPUT",
            "fully_qualified_name": file_path,
        }
    name = _strip_identifier_quotes(metadata.get("queryExpression") or node.get("label"))
    if name:
        return {
            "asset_type": "TABLE",
            "role": "INPUT",
            "fully_qualified_name": name,
        }
    return None


def _strip_identifier_quotes(name: Optional[str]) -> Optional[str]:
    """Remove SQL identifier quoting from a (possibly qualified) table name.

    Qlik's ``queryExpression`` quotes each identifier — ``"DB"."SCHEMA"."TABLE"``
    — which Monte Carlo won't match. Each dot-separated part is unwrapped from
    double quotes or backticks. Returns ``None`` for empty input.
    """
    if not name:
        return None
    parts = [part.strip().strip('"').strip("`") for part in name.split(".")]
    cleaned = ".".join(part for part in parts if part)
    return cleaned or None


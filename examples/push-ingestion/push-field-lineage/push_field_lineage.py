"""Push field-level (column) lineage to Monte Carlo from a JSON manifest.

Reads a manifest file that declares which source fields map to which
destination fields, then pushes column lineage to Monte Carlo using the
pycarlo SDK's IngestionService.

Usage:
    MCD_INGEST_ID=<key-id> MCD_INGEST_TOKEN=<key-token> \
        python3 push_field_lineage.py --manifest sample_manifest.json

See sample_manifest.json for the expected format.
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from pycarlo.core import Client, Session
from pycarlo.features.ingestion import IngestionService
from pycarlo.features.ingestion.models import (
    ColumnLineageField,
    ColumnLineageSourceField,
    LineageAssetRef,
    LineageEvent,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

_ENDPOINT = "https://integrations.getmontecarlo.com"
_BATCH_SIZE = 500


def _build_events(manifest: dict) -> list[LineageEvent]:
    """Convert a manifest into pycarlo LineageEvent objects."""
    events: list[LineageEvent] = []

    for edge in manifest["lineage_edges"]:
        dest = edge["destination"]
        sources = edge["sources"]
        field_mappings = edge.get("field_mappings", [])

        # Build a lookup: source table name → auto-generated asset_id
        source_asset_ids: dict[str, str] = {}
        source_refs: list[LineageAssetRef] = []
        for i, src in enumerate(sources):
            asset_id = f"src_{i}_{src['name']}"
            source_asset_ids[src["name"]] = asset_id
            source_refs.append(
                LineageAssetRef(
                    type=src.get("type", "TABLE"),
                    name=src["name"],
                    database=src["database"],
                    schema=src["schema"],
                    asset_id=asset_id,
                )
            )

        dest_ref = LineageAssetRef(
            type=dest.get("type", "TABLE"),
            name=dest["name"],
            database=dest["database"],
            schema=dest["schema"],
        )

        # Build column-level field mappings
        fields: list[ColumnLineageField] = []
        for mapping in field_mappings:
            col_sources: list[ColumnLineageSourceField] = []
            for sf in mapping["source_fields"]:
                table_name = sf["table"]
                asset_id = source_asset_ids.get(table_name)
                if asset_id is None:
                    raise ValueError(
                        f"Source table '{table_name}' in field mapping "
                        f"for '{mapping['destination_field']}' does not "
                        f"match any source in this edge. "
                        f"Available sources: {list(source_asset_ids)}"
                    )
                col_sources.append(
                    ColumnLineageSourceField(
                        asset_id=asset_id,
                        field_name=sf["field"],
                    )
                )
            fields.append(
                ColumnLineageField(
                    name=mapping["destination_field"],
                    source_fields=col_sources,
                )
            )

        events.append(
            LineageEvent(
                destination=dest_ref,
                sources=source_refs,
                fields=fields if fields else None,
            )
        )

    return events


def push(
    manifest_path: str,
    key_id: str,
    key_token: str,
    resource_uuid: str | None = None,
    batch_size: int = _BATCH_SIZE,
) -> list[str | None]:
    """Read a manifest and push field lineage to Monte Carlo."""
    with open(manifest_path) as fh:
        manifest = json.load(fh)

    effective_uuid = resource_uuid or manifest.get("resource_uuid")
    resource_type = manifest.get("resource_type", "snowflake")

    if not effective_uuid or effective_uuid == "<your-warehouse-uuid>":
        raise ValueError(
            "resource_uuid is required. Set it in the manifest file, "
            "via --resource-uuid, or MCD_RESOURCE_UUID env var."
        )

    events = _build_events(manifest)
    log.info(
        "Loaded %d lineage edge(s) with %d total field mapping(s) from %s",
        len(events),
        sum(len(e.fields or []) for e in events),
        manifest_path,
    )

    # Print human-readable summary
    for event in events:
        dest = event.destination
        log.info(
            "  %s.%s.%s ← %s",
            dest.database,
            dest.schema,
            dest.name,
            ", ".join(f"{s.database}.{s.schema}.{s.name}" for s in event.sources),
        )
        for field in event.fields or []:
            for sf in field.source_fields:
                src_name = next(
                    s.name for s in event.sources if s.asset_id == sf.asset_id
                )
                log.info(
                    "    %s.%s → %s.%s",
                    src_name,
                    sf.field_name,
                    dest.name,
                    field.name,
                )

    # Batch and push
    batches = [
        events[i : i + batch_size]
        for i in range(0, max(len(events), 1), batch_size)
    ]

    client = Client(
        session=Session(
            mcd_id=key_id,
            mcd_token=key_token,
            scope="Ingestion",
            endpoint=_ENDPOINT,
        )
    )
    service = IngestionService(mc_client=client)

    invocation_ids: list[str | None] = []
    for i, batch in enumerate(batches, 1):
        log.info("Pushing batch %d/%d (%d edge(s)) ...", i, len(batches), len(batch))
        result = service.send_lineage(
            resource_uuid=effective_uuid,
            resource_type=resource_type,
            events=batch,
        )
        inv_id = service.extract_invocation_id(result)
        invocation_ids.append(inv_id)
        log.info("  invocation_id=%s", inv_id)

    log.info("Done — %d batch(es) pushed.", len(batches))
    return invocation_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push field-level (column) lineage to Monte Carlo",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the JSON manifest file (see sample_manifest.json)",
    )
    parser.add_argument(
        "--resource-uuid",
        default=os.getenv("MCD_RESOURCE_UUID"),
        help="Warehouse UUID (overrides manifest value; default: $MCD_RESOURCE_UUID)",
    )
    parser.add_argument(
        "--key-id",
        default=os.getenv("MCD_INGEST_ID"),
        help="Ingestion key ID (default: $MCD_INGEST_ID)",
    )
    parser.add_argument(
        "--key-token",
        default=os.getenv("MCD_INGEST_TOKEN"),
        help="Ingestion key token (default: $MCD_INGEST_TOKEN)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH_SIZE,
        help=f"Max lineage edges per push batch (default: {_BATCH_SIZE})",
    )
    args = parser.parse_args()

    missing = []
    if not args.key_id:
        missing.append("--key-id / MCD_INGEST_ID")
    if not args.key_token:
        missing.append("--key-token / MCD_INGEST_TOKEN")
    if missing:
        parser.error(f"Missing required: {', '.join(missing)}")

    push(
        manifest_path=args.manifest,
        key_id=args.key_id,
        key_token=args.key_token,
        resource_uuid=args.resource_uuid,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()

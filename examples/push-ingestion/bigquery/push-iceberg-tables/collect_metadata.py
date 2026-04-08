"""Collect metadata for BigQuery Iceberg (BigLake-managed) tables.

Queries INFORMATION_SCHEMA.TABLE_STORAGE and INFORMATION_SCHEMA.COLUMNS
to extract volume, freshness, and schema for Iceberg tables, then writes
a JSON manifest that can be fed to push_metadata.py.

Usage:
    # Full collection (first run — includes fields/schema):
    GOOGLE_APPLICATION_CREDENTIALS=./bq-credentials \
        python3 collect_metadata.py \
        --project-id my-gcp-project \
        --datasets my_dataset

    # Periodic updates (freshness + volume only — faster, no COLUMNS query):
    GOOGLE_APPLICATION_CREDENTIALS=./bq-credentials \
        python3 collect_metadata.py \
        --project-id my-gcp-project \
        --datasets my_dataset \
        --only-freshness-and-volume
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone

from google.cloud import bigquery

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RESOURCE_TYPE = "bigquery"

# Default BigQuery region for INFORMATION_SCHEMA queries.
DEFAULT_REGION = os.getenv("BIGQUERY_REGION", "us")

# Column in TABLE_STORAGE that holds the last-modified timestamp for Iceberg
# tables.  Google is rolling out ``storage_last_modified_time`` — if your
# project uses a different column name, override via --freshness-column or
# the FRESHNESS_COLUMN env var.
DEFAULT_FRESHNESS_COLUMN = os.getenv("FRESHNESS_COLUMN", "storage_last_modified_time")

# BigQuery type → Monte Carlo canonical type
BQ_TYPE_MAP: dict[str, str] = {
    "INT64": "INTEGER",
    "INTEGER": "INTEGER",
    "FLOAT64": "FLOAT",
    "FLOAT": "FLOAT",
    "BOOL": "BOOLEAN",
    "BOOLEAN": "BOOLEAN",
    "STRING": "VARCHAR",
    "BYTES": "BINARY",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "TIMESTAMP": "TIMESTAMP",
    "TIME": "TIME",
    "NUMERIC": "DECIMAL",
    "BIGNUMERIC": "DECIMAL",
    "RECORD": "STRUCT",
    "STRUCT": "STRUCT",
    "REPEATED": "ARRAY",
    "JSON": "JSON",
    "GEOGRAPHY": "GEOGRAPHY",
}


def map_bq_type(bq_type: str) -> str:
    base = bq_type.split("(")[0].strip().upper()
    return BQ_TYPE_MAP.get(base, bq_type.upper())


def _fetch_iceberg_tables(
    client: bigquery.Client,
    project_id: str,
    region: str = DEFAULT_REGION,
    freshness_column: str = DEFAULT_FRESHNESS_COLUMN,
    datasets: list[str] | None = None,
    tables: list[str] | None = None,
) -> list[dict]:
    """Query TABLE_STORAGE for BigLake (Iceberg) tables."""
    conditions = [
        "managed_table_type = 'BIGLAKE'",
        "deleted = FALSE",
    ]
    if datasets:
        ds_list = ", ".join(f"'{d}'" for d in datasets)
        conditions.append(f"table_schema IN ({ds_list})")
    if tables:
        tbl_list = ", ".join(f"'{t}'" for t in tables)
        conditions.append(f"table_name IN ({tbl_list})")

    where = " AND ".join(conditions)
    query = f"""
        SELECT
            table_schema,
            table_name,
            total_rows,
            current_physical_bytes,
            {freshness_column},
            creation_time
        FROM `{project_id}.region-{region}`.INFORMATION_SCHEMA.TABLE_STORAGE
        WHERE {where}
        ORDER BY table_schema, table_name
    """
    log.info("Querying TABLE_STORAGE for Iceberg tables (region=%s) ...", region)
    rows = list(client.query(query).result())
    log.info("Found %d Iceberg table(s).", len(rows))
    return [dict(row) for row in rows]


def _fetch_columns(
    client: bigquery.Client,
    project_id: str,
    dataset: str,
    table_name: str,
) -> list[dict]:
    """Fetch column metadata for a specific table."""
    query = f"""
        SELECT column_name, data_type, ordinal_position, is_nullable, column_default
        FROM `{project_id}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = '{table_name}'
        ORDER BY ordinal_position
    """
    return [
        {
            "name": row["column_name"],
            "type": map_bq_type(row["data_type"]),
        }
        for row in client.query(query).result()
    ]


def collect(
    project_id: str,
    datasets: list[str] | None = None,
    tables: list[str] | None = None,
    only_freshness_and_volume: bool = False,
    region: str = DEFAULT_REGION,
    freshness_column: str = DEFAULT_FRESHNESS_COLUMN,
    output_file: str = "metadata_output.json",
) -> dict:
    """Collect Iceberg table metadata and write a JSON manifest.

    When only_freshness_and_volume is True, skips the COLUMNS query and
    omits fields from the manifest. Use this for periodic hourly pushes
    after the initial full metadata push.
    """
    client = bigquery.Client(project=project_id)

    if only_freshness_and_volume:
        log.info("Running in freshness+volume only mode (skipping fields).")

    iceberg_tables = _fetch_iceberg_tables(
        client, project_id, region=region, freshness_column=freshness_column,
        datasets=datasets, tables=tables,
    )
    if not iceberg_tables:
        log.warning("No Iceberg tables found matching the criteria.")
        return {"resource_type": RESOURCE_TYPE, "assets": []}

    assets: list[dict] = []
    for row in iceberg_tables:
        dataset = row["table_schema"]
        name = row["table_name"]

        asset = {
            "name": name,
            "database": project_id,
            "schema": dataset,
            "type": "TABLE",
            "volume": {
                "row_count": row["total_rows"],
                "byte_count": row["current_physical_bytes"],
            },
            "freshness": {
                "last_updated_time": row[freshness_column].isoformat()
                if row.get(freshness_column)
                else None,
            },
        }

        if not only_freshness_and_volume:
            asset["description"] = None
            asset["fields"] = _fetch_columns(client, project_id, dataset, name)

        assets.append(asset)
        log.info(
            "Collected %s.%s.%s — rows=%s, bytes=%s",
            project_id, dataset, name,
            row["total_rows"], row["current_physical_bytes"],
        )

    manifest = {
        "resource_type": RESOURCE_TYPE,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "assets": assets,
    }
    with open(output_file, "w") as fh:
        json.dump(manifest, fh, indent=2)
    log.info("Manifest written to %s (%d assets)", output_file, len(assets))

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect BigQuery Iceberg table metadata into a JSON manifest",
    )
    parser.add_argument(
        "--project-id",
        default=os.getenv("BIGQUERY_PROJECT_ID"),
        help="GCP project ID (or set BIGQUERY_PROJECT_ID env var)",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=None,
        help="Limit to specific dataset(s). Omit to scan all datasets.",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        default=None,
        help="Limit to specific table name(s) within the datasets.",
    )
    parser.add_argument(
        "--only-freshness-and-volume",
        action="store_true",
        help="Skip field/schema collection — only collect freshness and volume. "
             "Use for periodic hourly pushes after the initial full metadata push.",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"BigQuery region for INFORMATION_SCHEMA queries (default: {DEFAULT_REGION}). "
             "Also settable via BIGQUERY_REGION env var.",
    )
    parser.add_argument(
        "--freshness-column",
        default=DEFAULT_FRESHNESS_COLUMN,
        help=f"Column in TABLE_STORAGE for the last-modified timestamp "
             f"(default: {DEFAULT_FRESHNESS_COLUMN}). Also settable via FRESHNESS_COLUMN env var.",
    )
    parser.add_argument("--output-file", default="metadata_output.json")
    args = parser.parse_args()

    if not args.project_id:
        parser.error("--project-id or BIGQUERY_PROJECT_ID env var is required")

    collect(
        project_id=args.project_id,
        datasets=args.datasets,
        tables=args.tables,
        only_freshness_and_volume=args.only_freshness_and_volume,
        region=args.region,
        freshness_column=args.freshness_column,
        output_file=args.output_file,
    )


if __name__ == "__main__":
    main()

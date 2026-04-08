"""Collect BigQuery Iceberg table metadata and push it to Monte Carlo.

Convenience wrapper that runs collect_metadata.collect() followed by
push_metadata.push() in a single invocation.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=./bq-credentials \
    MCD_INGEST_ID=<key-id> MCD_INGEST_TOKEN=<key-token> MCD_RESOURCE_UUID=<uuid> \
        python3 collect_and_push_metadata.py \
        --project-id my-gcp-project \
        --datasets my_dataset
"""

from __future__ import annotations

import argparse
import os

from collect_metadata import DEFAULT_FRESHNESS_COLUMN, DEFAULT_REGION, collect
from push_metadata import _MAX_WORKERS, push


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect BigQuery Iceberg metadata and push to Monte Carlo",
    )
    # Collection args
    parser.add_argument("--project-id", default=os.getenv("BIGQUERY_PROJECT_ID"))
    parser.add_argument("--datasets", nargs="+", default=None)
    parser.add_argument("--tables", nargs="+", default=None)
    parser.add_argument(
        "--only-freshness-and-volume",
        action="store_true",
        help="Skip field/schema collection — only collect freshness and volume.",
    )
    parser.add_argument("--region", default=DEFAULT_REGION,
                        help="BigQuery region for INFORMATION_SCHEMA queries.")
    parser.add_argument("--freshness-column", default=DEFAULT_FRESHNESS_COLUMN,
                        help="TABLE_STORAGE column for last-modified timestamp.")
    parser.add_argument("--manifest-file", default="metadata_output.json")

    # Push args
    parser.add_argument("--resource-uuid", default=os.getenv("MCD_RESOURCE_UUID"))
    parser.add_argument("--key-id", default=os.getenv("MCD_INGEST_ID"))
    parser.add_argument("--key-token", default=os.getenv("MCD_INGEST_TOKEN"))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-workers", type=int, default=_MAX_WORKERS,
                        help="Max parallel push threads. Use 1 for easier debugging.")
    parser.add_argument("--push-result-file", default="metadata_push_result.json")

    args = parser.parse_args()

    if not args.project_id:
        parser.error("--project-id or BIGQUERY_PROJECT_ID env var is required")
    required_push = ["resource_uuid", "key_id", "key_token"]
    missing = [k for k in required_push if getattr(args, k) is None]
    if missing:
        parser.error(f"Missing required push arguments/env vars: {missing}")

    collect(
        project_id=args.project_id,
        datasets=args.datasets,
        tables=args.tables,
        only_freshness_and_volume=args.only_freshness_and_volume,
        region=args.region,
        freshness_column=args.freshness_column,
        output_file=args.manifest_file,
    )

    push(
        input_file=args.manifest_file,
        resource_uuid=args.resource_uuid,
        key_id=args.key_id,
        key_token=args.key_token,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        output_file=args.push_result_file,
    )


if __name__ == "__main__":
    main()

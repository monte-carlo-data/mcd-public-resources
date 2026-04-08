# Push Field-Level (Column) Lineage to Monte Carlo

Push field-level lineage to Monte Carlo using the
[Push Ingestion API](https://docs.getmontecarlo.com/docs/api-push-ingest).

Field-level lineage tells Monte Carlo exactly which source column feeds each
destination column — for example, `orders.created_at` maps to
`order_summary.order_date`. This works across multiple source tables in a single
call.

## When to Use This

Use push field lineage when:

- Your warehouse or ETL tool does not expose column lineage natively
- You want to declare explicit field-to-field mappings from your own metadata
- You need column lineage for custom or external pipelines that Monte Carlo
  cannot observe via query logs
- You want to complement Monte Carlo's auto-discovered lineage with
  authoritative mappings from your own systems

## How It Works

```
┌─────────────────────────┐
│  Your field mappings     │    Define source → destination field
│  (JSON manifest)         │    mappings in a simple JSON file
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│  push_field_lineage.py   │    Reads the manifest, converts to
│                          │    pycarlo SDK calls
└────────────┬────────────┘
             │  POST /ingest/v1/lineage
             ▼
┌─────────────────────────┐
│  Monte Carlo             │    Column lineage visible in the
│  Lineage Graph           │    UI within seconds
└─────────────────────────┘
```

## Prerequisites

1. **Python 3.9+**
2. **Monte Carlo Ingestion API key** — if you don't have one yet, follow the
   steps in
   [Create an Ingestion Key](https://docs.getmontecarlo.com/docs/api-push-ingest#1-create-an-ingestion-key).
   You will need:
   - `MCD_INGEST_ID` — the key ID
   - `MCD_INGEST_TOKEN` — the key token
3. **Monte Carlo warehouse UUID** (`MCD_RESOURCE_UUID`) — the UUID of your
   warehouse resource in Monte Carlo. Find it in **Settings > Integrations**.

## Setup

```bash
pip install -r requirements.txt

export MCD_INGEST_ID=<your-key-id>
export MCD_INGEST_TOKEN=<your-key-token>
export MCD_RESOURCE_UUID=<your-warehouse-uuid>
```

## Quick Start

### 1. Define Your Field Mappings

Create a JSON manifest that describes which source fields map to which
destination fields. See [`sample_manifest.json`](sample_manifest.json) for
the full format.

Here is a minimal example — two source tables feeding one destination:

```json
{
  "resource_uuid": "<your-warehouse-uuid>",
  "resource_type": "snowflake",
  "lineage_edges": [
    {
      "destination": {
        "database": "analytics",
        "schema": "public",
        "name": "order_summary",
        "type": "TABLE"
      },
      "sources": [
        {
          "database": "analytics",
          "schema": "public",
          "name": "orders",
          "type": "TABLE"
        },
        {
          "database": "analytics",
          "schema": "public",
          "name": "customers",
          "type": "TABLE"
        }
      ],
      "field_mappings": [
        {
          "destination_field": "order_id",
          "source_fields": [
            { "table": "orders", "field": "order_id" }
          ]
        },
        {
          "destination_field": "customer_name",
          "source_fields": [
            { "table": "customers", "field": "name" }
          ]
        }
      ]
    }
  ]
}
```

### 2. Push to Monte Carlo

```bash
python3 push_field_lineage.py --manifest sample_manifest.json
```

You should see output like:

```
2026-04-08 12:00:00 INFO Loaded 1 lineage edge(s) with 5 total field mapping(s) from sample_manifest.json
2026-04-08 12:00:00 INFO   analytics.public.order_summary ← analytics.public.orders, analytics.public.customers
2026-04-08 12:00:00 INFO     orders.order_id → order_summary.order_id
2026-04-08 12:00:00 INFO     orders.amount → order_summary.amount
2026-04-08 12:00:00 INFO     orders.created_at → order_summary.order_date
2026-04-08 12:00:00 INFO     customers.name → order_summary.customer_name
2026-04-08 12:00:00 INFO     customers.region → order_summary.region
2026-04-08 12:00:00 INFO Pushing batch 1/1 (1 edge(s)) ...
2026-04-08 12:00:01 INFO   invocation_id=abc12345-6789-...
2026-04-08 12:00:01 INFO Done — 1 batch(es) pushed.
```

### 3. Verify in Monte Carlo

Open the lineage graph for the destination table in the Monte Carlo UI. You
should see the field-level lineage edges within seconds of pushing.

## Manifest Format

### Top-Level Fields

| Field | Required | Description |
|-------|----------|-------------|
| `resource_uuid` | Yes | Warehouse UUID from Monte Carlo (can be overridden via `--resource-uuid` or `MCD_RESOURCE_UUID`) |
| `resource_type` | Yes | Warehouse type — e.g. `snowflake`, `bigquery`, `databricks-metastore-sql-warehouse`, `redshift` |
| `lineage_edges` | Yes | Array of lineage edges (see below) |

### Lineage Edge

Each edge describes one destination table and its source tables, plus
optional field-level mappings.

| Field | Required | Description |
|-------|----------|-------------|
| `destination` | Yes | The destination table (see Table Reference below) |
| `sources` | Yes | One or more source tables |
| `field_mappings` | No | Column-level mappings (omit for table-level lineage only) |

### Table Reference

| Field | Required | Description |
|-------|----------|-------------|
| `database` | Yes | Database name |
| `schema` | Yes | Schema name |
| `name` | Yes | Table or view name |
| `type` | No | `TABLE` (default) or `VIEW` |

### Field Mapping

| Field | Required | Description |
|-------|----------|-------------|
| `destination_field` | Yes | Column name on the destination table |
| `source_fields` | Yes | Array of source columns that feed this destination column |
| `source_fields[].table` | Yes | Source table name (must match a `name` in `sources`) |
| `source_fields[].field` | Yes | Source column name |

## Examples

### Single Source Table

A simple 1:1 mapping from a staging table to a production table:

```json
{
  "resource_uuid": "<uuid>",
  "resource_type": "snowflake",
  "lineage_edges": [
    {
      "destination": {
        "database": "prod",
        "schema": "public",
        "name": "users",
        "type": "TABLE"
      },
      "sources": [
        {
          "database": "staging",
          "schema": "raw",
          "name": "users_raw",
          "type": "TABLE"
        }
      ],
      "field_mappings": [
        {
          "destination_field": "user_id",
          "source_fields": [{ "table": "users_raw", "field": "id" }]
        },
        {
          "destination_field": "full_name",
          "source_fields": [{ "table": "users_raw", "field": "name" }]
        },
        {
          "destination_field": "email_address",
          "source_fields": [{ "table": "users_raw", "field": "email" }]
        }
      ]
    }
  ]
}
```

### Multiple Source Tables (Join)

A destination table built by joining two sources — the most common scenario
for field-level lineage:

```json
{
  "resource_uuid": "<uuid>",
  "resource_type": "bigquery",
  "lineage_edges": [
    {
      "destination": {
        "database": "analytics",
        "schema": "reporting",
        "name": "daily_revenue",
        "type": "TABLE"
      },
      "sources": [
        {
          "database": "analytics",
          "schema": "raw",
          "name": "transactions",
          "type": "TABLE"
        },
        {
          "database": "analytics",
          "schema": "raw",
          "name": "products",
          "type": "TABLE"
        }
      ],
      "field_mappings": [
        {
          "destination_field": "transaction_id",
          "source_fields": [{ "table": "transactions", "field": "id" }]
        },
        {
          "destination_field": "revenue",
          "source_fields": [{ "table": "transactions", "field": "amount" }]
        },
        {
          "destination_field": "product_name",
          "source_fields": [{ "table": "products", "field": "name" }]
        },
        {
          "destination_field": "category",
          "source_fields": [{ "table": "products", "field": "category" }]
        }
      ]
    }
  ]
}
```

### Multiple Lineage Edges in One Manifest

You can declare many edges (destination tables) in a single manifest:

```json
{
  "resource_uuid": "<uuid>",
  "resource_type": "snowflake",
  "lineage_edges": [
    {
      "destination": { "database": "dw", "schema": "mart", "name": "fact_orders" },
      "sources": [
        { "database": "dw", "schema": "staging", "name": "stg_orders" }
      ],
      "field_mappings": [
        {
          "destination_field": "order_key",
          "source_fields": [{ "table": "stg_orders", "field": "order_id" }]
        }
      ]
    },
    {
      "destination": { "database": "dw", "schema": "mart", "name": "dim_customers" },
      "sources": [
        { "database": "dw", "schema": "staging", "name": "stg_customers" }
      ],
      "field_mappings": [
        {
          "destination_field": "customer_key",
          "source_fields": [{ "table": "stg_customers", "field": "customer_id" }]
        }
      ]
    }
  ]
}
```

### Table-Level Lineage Only (No Field Mappings)

Omit `field_mappings` to push only table-level lineage:

```json
{
  "resource_uuid": "<uuid>",
  "resource_type": "snowflake",
  "lineage_edges": [
    {
      "destination": { "database": "dw", "schema": "mart", "name": "fact_sales" },
      "sources": [
        { "database": "dw", "schema": "raw", "name": "sales_events" },
        { "database": "dw", "schema": "raw", "name": "product_catalog" }
      ]
    }
  ]
}
```

## CLI Reference

```
python3 push_field_lineage.py --manifest <path> [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--manifest` | (required) | Path to the JSON manifest file |
| `--resource-uuid` | `$MCD_RESOURCE_UUID` | Warehouse UUID (overrides manifest value) |
| `--key-id` | `$MCD_INGEST_ID` | Ingestion API key ID |
| `--key-token` | `$MCD_INGEST_TOKEN` | Ingestion API key token |
| `--batch-size` | `500` | Max lineage edges per API call |

## Important Notes

- **Source and destination tables must already exist in the Monte Carlo
  catalog.** If the tables referenced in your manifest are not yet ingested,
  **field lineage will not be created**. Push metadata for those tables first
  using the
  [metadata push API](https://docs.getmontecarlo.com/docs/api-push-ingest#2-push-metadata),
  or make sure they have already been collected by Monte Carlo's pull-based
  pipeline, before pushing field lineage.
- **Column lineage expires after 10 days and cannot be removed early.**
  There is no API to selectively delete pushed column lineage — once pushed,
  you must wait for the 10-day TTL to expire. Even deleting the source or
  destination table via `deletePushIngestedTables` does **not** remove the
  column lineage edges — the table disappears from the catalog but the
  field-level lineage remains visible in the Field Lineage tab until the
  10-day TTL expires. **Double-check your manifest carefully before
  pushing**, as incorrect mappings cannot be undone. If you need persistent
  column lineage, schedule the script to re-push periodically (daily is
  sufficient). Table-level lineage pushed via this API never expires.
- **Payload size limit is 1 MB** (compressed). For large manifests with many
  edges, the script automatically batches into multiple API calls.
- **`resource_type` must match your warehouse.** Supported values include:
  `snowflake`, `bigquery`, `redshift`, `databricks-metastore-sql-warehouse`,
  `athena`, `teradata`, `clickhouse`, `s3`, `presto-s3`, `hive-s3`.

## Documentation

- [Monte Carlo Push Ingestion API](https://docs.getmontecarlo.com/docs/api-push-ingest)
- [pycarlo SDK](https://pypi.org/project/pycarlo/)

## License

See [LICENSE](../../../../LICENSE) in the repository root.

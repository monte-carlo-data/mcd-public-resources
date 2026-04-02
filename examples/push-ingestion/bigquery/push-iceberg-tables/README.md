# Push Ingestion for BigQuery Iceberg (BigLake) Tables

Push table metadata and query logs for BigQuery Iceberg (BigLake-managed) tables
to Monte Carlo using the [Push Ingestion API](https://docs.getmontecarlo.com/docs/api-push-ingest).

## Why Push Ingestion for Iceberg Tables?

Monte Carlo's standard BigQuery collector uses `__TABLES__` to discover tables and
collect metadata. BigQuery Iceberg tables (BigLake-managed) do **not** appear in
`__TABLES__`, so they are invisible to Monte Carlo's pull-based collection.

These scripts query `INFORMATION_SCHEMA.TABLE_STORAGE` (filtering on
`managed_table_type = 'BIGLAKE'`) and `INFORMATION_SCHEMA.COLUMNS` to collect
metadata for Iceberg tables, then push it to Monte Carlo via the
[pycarlo](https://pypi.org/project/pycarlo/) SDK.

These scripts were created using the skills from the
[push-ingestion plugin](https://github.com/monte-carlo-data/mcd-agent-toolkit/tree/main/skills/push-ingestion)
in the [mcd-agent-toolkit](https://github.com/monte-carlo-data/mcd-agent-toolkit).

## Prerequisites

1. **Python 3.9+**
2. **GCP service account credentials** with BigQuery read access (`roles/bigquery.dataViewer`
   or equivalent) and BigQuery Jobs list access (`roles/bigquery.user`) for query log collection.
3. **Monte Carlo Ingestion API key** — create one in Monte Carlo Settings > API under the
   `Ingestion` scope. You will need:
   - `MCD_INGEST_ID` — the key ID
   - `MCD_INGEST_TOKEN` — the key token
4. **Monte Carlo warehouse UUID** (`MCD_RESOURCE_UUID`) — the UUID of your BigQuery warehouse
   resource in Monte Carlo. Find it in Settings > Warehouses.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set up GCP credentials (service account JSON)
export GOOGLE_APPLICATION_CREDENTIALS=./bq-credentials.json

# Set Monte Carlo credentials
export MCD_INGEST_ID=<your-key-id>
export MCD_INGEST_TOKEN=<your-key-token>
export MCD_RESOURCE_UUID=<your-warehouse-uuid>
```

## Scripts

### Table Metadata

| Script | Description |
|--------|-------------|
| `collect_metadata.py` | Queries `INFORMATION_SCHEMA.TABLE_STORAGE` and `COLUMNS` for Iceberg tables, writes a JSON manifest |
| `push_metadata.py` | Reads the manifest and pushes metadata to Monte Carlo |
| `collect_and_push_metadata.py` | Combined wrapper — collect + push in one step |

### Query Logs

| Script | Description |
|--------|-------------|
| `collect_query_logs.py` | Collects query logs from the BigQuery Jobs API, writes a JSON manifest |
| `push_query_logs.py` | Reads the manifest and pushes query logs to Monte Carlo |
| `collect_and_push_query_logs.py` | Combined wrapper — collect + push in one step |

## Usage

### Collect and Push Metadata (Combined)

```bash
# Full collection — includes schema/fields, freshness, and volume:
python3 collect_and_push_metadata.py \
    --project-id my-gcp-project \
    --datasets dataset_a dataset_b

# Periodic updates — freshness + volume only (faster, skips COLUMNS query):
python3 collect_and_push_metadata.py \
    --project-id my-gcp-project \
    --datasets dataset_a dataset_b \
    --only-freshness-and-volume
```

### Collect and Push Metadata (Separate Steps)

```bash
# Step 1: Collect metadata into a JSON manifest
python3 collect_metadata.py \
    --project-id my-gcp-project \
    --datasets dataset_a dataset_b

# Step 2: Push the manifest to Monte Carlo
python3 push_metadata.py --input-file metadata_output.json
```

### Collect and Push Query Logs (Combined)

```bash
python3 collect_and_push_query_logs.py \
    --project-id my-gcp-project
```

### Collect and Push Query Logs (Separate Steps)

```bash
# Step 1: Collect query logs from the BigQuery Jobs API
python3 collect_query_logs.py \
    --project-id my-gcp-project \
    --lookback-hours 25

# Step 2: Push the query log manifest to Monte Carlo
python3 push_query_logs.py --input-file query_logs_output.json
```

## Key Flags

### Metadata Collection

| Flag | Default | Description |
|------|---------|-------------|
| `--project-id` | `$BIGQUERY_PROJECT_ID` | GCP project ID (required) |
| `--datasets` | all datasets | Limit to specific dataset(s) |
| `--tables` | all tables | Limit to specific table name(s) within datasets |
| `--only-freshness-and-volume` | off | Skip schema/fields — only collect freshness and volume |
| `--output-file` | `metadata_output.json` | Path for the output manifest |

### Query Log Collection

| Flag | Default | Description |
|------|---------|-------------|
| `--project-id` | `$BIGQUERY_PROJECT_ID` | GCP project ID (required) |
| `--lookback-hours` | `25` | How many hours back to collect logs |
| `--lookback-lag-hours` | `1` | Hours of lag to skip (avoids incomplete recent data) |
| `--output-file` | `query_logs_output.json` | Path for the output manifest |

### Push

| Flag | Default | Description |
|------|---------|-------------|
| `--resource-uuid` | `$MCD_RESOURCE_UUID` | Monte Carlo warehouse UUID |
| `--key-id` | `$MCD_INGEST_ID` | Ingestion API key ID |
| `--key-token` | `$MCD_INGEST_TOKEN` | Ingestion API key token |
| `--batch-size` | `500` (metadata) / `100` (query logs) | Max items per API call |

## Recommended Automation

For ongoing monitoring, schedule these scripts to run periodically:

| Push Type | Recommended Cadence | Notes |
|-----------|-------------------|-------|
| **Full metadata** (with fields) | Daily or on schema change | First run and when schema changes |
| **Freshness + volume only** | Hourly | Use `--only-freshness-and-volume` for faster collection |
| **Query logs** | Hourly | Use default `--lookback-hours 25` for overlap coverage |

### Detector Activation Timelines

After you begin pushing metadata, Monte Carlo's anomaly detectors need a baseline
before they start alerting:

- **Freshness detectors**: ~7 pushes with distinct `last_update_time` values over ~2 weeks
- **Volume detectors**: ~10-48 samples over ~5 weeks

### Example Cron Jobs

```bash
# Hourly freshness + volume push
0 * * * * GOOGLE_APPLICATION_CREDENTIALS=/path/to/creds.json \
    MCD_INGEST_ID=... MCD_INGEST_TOKEN=... MCD_RESOURCE_UUID=... \
    python3 /path/to/collect_and_push_metadata.py \
    --project-id my-gcp-project --only-freshness-and-volume

# Daily full metadata push (with fields)
0 6 * * * GOOGLE_APPLICATION_CREDENTIALS=/path/to/creds.json \
    MCD_INGEST_ID=... MCD_INGEST_TOKEN=... MCD_RESOURCE_UUID=... \
    python3 /path/to/collect_and_push_metadata.py \
    --project-id my-gcp-project

# Hourly query log push
30 * * * * GOOGLE_APPLICATION_CREDENTIALS=/path/to/creds.json \
    MCD_INGEST_ID=... MCD_INGEST_TOKEN=... MCD_RESOURCE_UUID=... \
    python3 /path/to/collect_and_push_query_logs.py \
    --project-id my-gcp-project
```

## How It Works

1. **Iceberg table discovery**: Queries `INFORMATION_SCHEMA.TABLE_STORAGE` filtering on
   `managed_table_type = 'BIGLAKE'` and `deleted = FALSE` to find all Iceberg tables.

2. **Metadata collection**: For each table, extracts:
   - **Volume**: `total_rows` and `current_physical_bytes` from `TABLE_STORAGE`
   - **Freshness**: `storage_last_modified_time` from `TABLE_STORAGE` (falls back to
     current time if not yet populated by Google)
   - **Schema**: Column names and types from `INFORMATION_SCHEMA.COLUMNS`

3. **Query log collection**: Uses the BigQuery Jobs API (`list_jobs`) to collect completed
   query jobs within the lookback window — no `INFORMATION_SCHEMA` query needed.

4. **Push to Monte Carlo**: Sends collected data via pycarlo's `IngestionService` in
   configurable batch sizes.

## Documentation

- [Monte Carlo Push Ingestion API](https://docs.getmontecarlo.com/docs/api-push-ingest)
- [pycarlo SDK](https://pypi.org/project/pycarlo/)
- [BigQuery INFORMATION_SCHEMA.TABLE_STORAGE](https://cloud.google.com/bigquery/docs/information-schema-table-storage)

## License

See [LICENSE](../../../../LICENSE) in the repository root.

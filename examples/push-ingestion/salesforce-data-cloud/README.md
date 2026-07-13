# Data 360 Lineage Push — DLO→DMO and DMO→CIO

Pushes Salesforce Data 360 lineage into Monte Carlo so your full data transformation
pipeline appears as observable, trusted lineage inside Monte Carlo's data catalog —
the observability layer for your Agentforce data.

Covers two lineage hops in a single run:

- **DLO→DMO** — raw Data Lake Objects mapped to harmonized Data Model Objects
- **DMO→CIO** — Data Model Objects feeding Calculated Insight Objects (metrics/aggregations)

This is a standalone Python script. It has no external service dependencies beyond
Salesforce and Monte Carlo. Run it on any machine that can reach both APIs, or schedule
it as a cron job.

---

## What It Does

The script runs an eight-step pipeline:

1. **Authenticate with Salesforce** — client-credentials OAuth flow using your connected
   app credentials. A single token covers all data spaces (no per-dataspace credentials needed).
2. **Fetch Data Spaces** — enumerates your Data 360 data spaces so lineage is attributed
   to the correct schema partition.
3. **Fetch the Monte Carlo catalog** — bulk-fetches all tables for the warehouse in one
   paginated query (scoped to your Data Cloud warehouse UUID to prevent cross-org
   contamination) and enumerates the catalogued DMOs to query.
4. **Retrieve DLO→DMO mappings** — one **read-only** GET per catalogued DMO against the
   Data Cloud REST API (`/ssot/data-model-object-mappings`). No SOAP Metadata API and
   **no "Modify Metadata" permission** — the mapping records carry the same source/target
   developer names.
4b. **Validate catalog coverage** — validates each DLO and DMO against the catalog from
    step 3 locally. Edges for uncatalogued tables are skipped and logged, so you can see
    exactly what's missing and re-run once the connector syncs.
5. **Fetch Calculated Insight Objects** — calls the Data Cloud REST API
   (`/ssot/calculated-insights`) to retrieve all CIOs and their SQL expressions.
6. **Parse DMO→CIO edges** — scans each CIO's SQL expression for `__dlm` and `__cio`
   tokens to determine which DMOs (and other CIOs) feed into it. Handles subqueries,
   CTEs, fan-in (multiple DMOs → one CIO), and CIO chains (CIO → CIO).
7. **Push DLO→DMO lineage to Monte Carlo** — sends validated edges to Monte Carlo's
   Ingest API in batches with automatic retry.
8. **Push DMO→CIO lineage to Monte Carlo** — sends CIO edges to Monte Carlo's Ingest API
   in batches with automatic retry.

Lineage pushed:
- All DLO→DMO mappings configured in the org (e.g. `Account__dll` → `Account__dlm`)
- All DMO→CIO relationships derived from CIO SQL expressions (e.g. `Account__dlm` → `Account_Metrics__cio`)

### About Calculated Insight Objects (CIOs)

CIOs are Salesforce Data 360's metric and aggregation layer — SQL-defined objects that
compute counts, sums, and other calculations on top of DMOs. They are the analytics
surface above the harmonized data model. The script reads the SQL expression from each
CIO to determine which DMOs feed it, then pushes those edges to Monte Carlo.

CIOs may not appear in the Monte Carlo catalog immediately after a push if the native
Data Cloud connector has not yet synced them. The push is idempotent — re-running after
the next connector sync will correctly link the CIO assets.

---

## Prerequisites

- Python 3.9 or later
- Network access to your Salesforce org and `https://api.getmontecarlo.com`
- A Salesforce **Connected App** with client credentials flow enabled
- Two Monte Carlo API keys (see [Configuration](#configuration) below)
- The **Monte Carlo Data Cloud connector** must have completed at least one metadata scan
  so that DLO and DMO tables appear in the catalog (required for Step 4b validation)
- The `montecarlo` CLI installed and authenticated (for creating Ingestion keys):
  `pip install montecarlo-cli` then `montecarlo auth login`

---

## Installation

```bash
# Copy this directory to your machine, then:
cd salesforce-data-cloud

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## Configuration

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

Open `.env` in a text editor. Every variable is documented with inline comments.

### Monte Carlo API keys

You need **two** Monte Carlo keys with different scopes:

**Key 1 — Ingestion key** (pushes lineage to Monte Carlo)

```bash
montecarlo integrations create-key \
  --scope Ingestion \
  --description "Data 360 lineage push"
```

Copy the printed `key_id` → `MCD_INGEST_ID` and `key_secret` → `MCD_INGEST_TOKEN`.

**Key 2 — Personal API key** (catalog validation via GraphQL)

Create one in the Monte Carlo UI: **Settings → API Keys → Create Key**.
Copy both values into `MCD_ID` and `MCD_TOKEN`.

> These are two different keys with different scopes. Mixing them up is the most
> common cause of authentication errors. See [Troubleshooting](TROUBLESHOOTING.md).

### Monte Carlo warehouse UUID

Set `MCD_RESOURCE_UUID` to your Salesforce Data Cloud warehouse UUID.
Find it in the Monte Carlo UI: **Settings → Integrations → your Data Cloud connection**.

### Salesforce Connected App

The script uses the **client credentials** OAuth flow. Your connected app must:

- Have **"Enable Client Credentials Flow"** checked under OAuth settings
- Have the `api` OAuth scope, with a run-as user assigned a Data Cloud permission set
  (Data Cloud Admin or Data Cloud User) so it can read the Data Cloud REST API
- **No "Modify Metadata" / Metadata API permission is required** — DLO→DMO lineage is
  read entirely from the read-only Data Cloud REST API

Set `SF_ORG_URL` to your org's **My Domain URL**
(e.g. `https://mycompany.my.salesforce.com`), not `login.salesforce.com`.

---

## Salesforce-only Diagnostic

Before running the full script, you can validate Salesforce connectivity independently
(no Monte Carlo credentials needed):

```bash
python3 sf_diagnostic.py
```

This tests OAuth auth, the Dataspace SOQL query, the read-only DLO→DMO mapping REST
endpoint, and the Calculated Insights REST endpoint — printing timing, edge count, and
data space information for all pipeline steps.
Useful for handing to a Salesforce admin to confirm API access before involving MC credentials.

---

## Usage

### Step 1 — Dry run (always do this first)

```bash
python3 push_lineage.py --dry-run
```

This runs every step — authenticates, retrieves metadata, fetches CIOs, resolves data
spaces, validates catalog coverage — but skips the final push to Monte Carlo. You will
see every edge that *would* be pushed. Use this to validate all credentials and confirm
the expected edges are found before committing anything.

Example output:

```
[10:42:01] [INFO   ] [run=a1b2c3d4] === Data 360 Lineage Push | run_id=a1b2c3d4 [DRY RUN] ===
[10:42:03] [INFO   ] [run=a1b2c3d4] Step 1: Authenticating with Salesforce (...)
[10:42:03] [INFO   ] [run=a1b2c3d4]   Authenticated (0.8s)
[10:42:04] [INFO   ] [run=a1b2c3d4] Step 2: Fetching Salesforce data spaces
[10:42:04] [INFO   ] [run=a1b2c3d4]   Found 1 data space(s): ['default']
[10:42:04] [INFO   ] [run=a1b2c3d4] Step 3: Fetching Monte Carlo catalog for warehouse <uuid>
[10:42:06] [INFO   ] [run=a1b2c3d4]   Fetched 71 table(s) from MC catalog (1.3s)
[10:42:06] [INFO   ] [run=a1b2c3d4]   46 catalogued DMO(s) to query for DLO->DMO mappings
[10:42:06] [INFO   ] [run=a1b2c3d4] Step 4: Retrieving DLO->DMO mappings via Data Cloud REST API (read-only; 46 catalogued DMO(s))
[10:42:09] [INFO   ] [run=a1b2c3d4]   22 DLO->DMO edge(s) extracted from 16 mapped DMO(s) (3.6s)
[10:42:15] [INFO   ] [run=a1b2c3d4] Step 4b: Validating DLO and DMO tables exist in Monte Carlo catalog
[10:42:28] [INFO   ] [run=a1b2c3d4]   Catalog check complete: 40 matched, 0 not in catalog (1.1s)
[10:42:28] [INFO   ] [run=a1b2c3d4]   20 edge(s) ready to push
[10:42:28] [INFO   ] [run=a1b2c3d4] Step 5: Fetching Calculated Insight Objects (CIOs) from Salesforce
[10:42:29] [INFO   ] [run=a1b2c3d4]   Found 5 CIO(s) (0.9s)
[10:42:29] [INFO   ] [run=a1b2c3d4] Step 6: Parsing DMO->CIO edges from CIO SQL expressions
[10:42:29] [INFO   ] [run=a1b2c3d4]   8 DMO->CIO edge(s) extracted from 5 CIO(s) (0.0s)
[10:42:29] [INFO   ] [run=a1b2c3d4] DLO->DMO edges (20 total):
[10:42:29] [INFO   ] [run=a1b2c3d4]   [default] Account__dll -> Account__dlm
...
[10:42:29] [INFO   ] [run=a1b2c3d4] DMO->CIO edges (8 total):
[10:42:29] [INFO   ] [run=a1b2c3d4]   [default] Account__dlm -> Account_Metrics__cio
...
[10:42:29] [INFO   ] [run=a1b2c3d4] [dry-run] Would push 20 DLO->DMO and 8 DMO->CIO edge(s) to
                                    Monte Carlo warehouse UUID=abc123.... Run without --dry-run to commit.
```

### Step 2 — Live push

```bash
python3 push_lineage.py
```

On success the final log line contains:
- `run_id` — unique identifier for this run (appears in every log line)
- DLO→DMO and DMO→CIO edge counts
- `invocation_ids` — Monte Carlo's receipt tokens for both push steps; keep these if
  you need to report an issue to Monte Carlo support

### DLO→DMO only (skip CIO)

```bash
python3 push_lineage.py --skip-cio
```

Skips Steps 5–6 and the DMO→CIO push entirely. Use this if:
- Your org has no CIOs yet
- Step 5 returns a 403 and you only need DLO→DMO lineage right now
- You want to test DLO→DMO in isolation before enabling the full pipeline

---

## Scheduling

Run the script on a schedule to keep lineage current. A daily run is sufficient for
most orgs; run more frequently if your DLO→DMO mappings change often.

The push is **idempotent** — running it multiple times is safe. Duplicate edges are
deduplicated by Monte Carlo.

### cron (Linux/macOS)

```cron
0 6 * * * cd /opt/data360-lineage && .venv/bin/python push_lineage.py >> /var/log/data360-lineage.log 2>&1
```

### Structured logs for log aggregation (Splunk, Datadog, CloudWatch)

```bash
LOG_FORMAT=json python3 push_lineage.py 2>> /var/log/data360-lineage.jsonl
```

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `SF_ORG_URL` | Salesforce org My Domain URL, e.g. `https://mycompany.my.salesforce.com` |
| `SF_CLIENT_ID` | Connected app consumer key |
| `SF_CLIENT_SECRET` | Connected app consumer secret |
| `MCD_INGEST_ID` | Monte Carlo **Ingestion** key ID |
| `MCD_INGEST_TOKEN` | Monte Carlo **Ingestion** key secret |
| `MCD_ID` | Monte Carlo **Personal API** key ID |
| `MCD_TOKEN` | Monte Carlo **Personal API** key secret |
| `MCD_RESOURCE_UUID` | Monte Carlo Data Cloud warehouse UUID |

### Optional

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | plain | Set to `json` for structured log output |
| `INGEST_BATCH_SIZE` | `500` | Edges per Monte Carlo push batch |
| `MAPPING_MAX_WORKERS` | `10` | Parallel per-DMO mapping fetches from the Data Cloud REST API |
| `SF_DEFAULT_DATA_SPACE` | `default` | Fallback data space when XML has no `<dataSpace>` and the org has multiple data spaces |

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success, or dry run completed with no errors |
| `1` | Known error — check the log line immediately above |
| `2` | Unexpected exception — full traceback is in the log |

---

## Failed-Edge Files

If one or more batches fail during the Monte Carlo push, the script saves the failed
edges to a JSON file in the same directory as `push_lineage.py`:

```
failed_edges_dlo_dmo_<run_id>_<timestamp>.json   # DLO→DMO failures
failed_edges_dmo_cio_<run_id>_<timestamp>.json   # DMO→CIO failures
```

Each file is created with owner-only permissions (mode 0600) and is excluded from
version control via `.gitignore`. The script exits with code `1` after saving.
Re-run after resolving the issue — the push is idempotent so duplicate edges are
safe to re-send.

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for solutions to common errors.

For issues that require Monte Carlo support, share your `run_id` (printed at the start
and end of every run) and the `invocation_ids` from the final log line.

**Monte Carlo contact:** Your Monte Carlo account team

---

## Data Flow

```
Salesforce Data 360 org
  ├── SOAP Metadata API → ObjectSourceTargetMap → DLO→DMO edges
  └── Data Cloud REST API → Calculated Insights → DMO→CIO edges (SQL parsed)

Monte Carlo GraphQL API
  └── Catalog validation (DLO and DMO table presence check)

Monte Carlo Ingest API
  ├── Step 7: DLO→DMO lineage push (batched, idempotent, with automatic retry)
  └── Step 8: DMO→CIO lineage push (batched, idempotent, with automatic retry)
```

---

## Security Notes

- Credentials are read from `.env` or environment variables — never hardcoded.
- `.env` is listed in `.gitignore` and must not be committed to source control.
- The script makes outbound HTTPS calls only. No inbound ports are opened.
- Salesforce authentication uses client credentials flow — no user session, no browser.
- `LOG_LEVEL=DEBUG` does not expose credentials in the log output.
- SOAP XML responses are parsed with `defusedxml` to block entity expansion attacks.
- `SF_ORG_URL` is validated at startup: must be HTTPS and must not resolve to a private/loopback/reserved IP address (both literal and via DNS resolution).

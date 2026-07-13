# Troubleshooting

If you cannot resolve an issue after checking this guide, contact Monte Carlo support
at your Monte Carlo account team and include:
- Your `run_id` (printed at the start and end of every run as `[run=XXXXXXXX]`)
- The full log output from the failing run (use `LOG_LEVEL=DEBUG` for maximum detail)
- The `invocation_ids` from the final summary line if the run reached the push stage

---

## Quick Diagnostic

Run with full debug logging:

```bash
LOG_LEVEL=DEBUG python3 push_lineage.py --dry-run
```

This prints every catalog and edge decision, and step-level
events — without writing anything to Monte Carlo. It is safe to run as many times as needed.

---

## Credential Errors

### `Required environment variable 'SF_ORG_URL' is not set`

A required variable is missing from your `.env` file. Check that:
- Your `.env` file is in the same directory as `push_lineage.py`
- The variable name is spelled correctly (all caps, underscores)
- The value is not empty (no bare `SF_ORG_URL=` with nothing after the `=`)

### `Salesforce auth failed: invalid_client — client identifier invalid`

The `SF_CLIENT_ID` (consumer key) is wrong or the connected app does not exist.
Re-copy the Consumer Key from Salesforce Setup → App Manager → your app → View.

### `Salesforce auth failed: invalid_client_credentials`

The connected app is not configured for client credentials flow, or the connected
app's run-as user is not set. In Salesforce Setup → App Manager → your app:
- Confirm **"Enable Client Credentials Flow"** is checked under OAuth settings
- Confirm a **Run As** user is assigned and that user has API access

### `Salesforce auth failed: invalid_grant`

The org URL is wrong. `SF_ORG_URL` must be your **My Domain URL**
(e.g. `https://mycompany.my.salesforce.com`), not `login.salesforce.com`.

### `MC GraphQL error: Unauthorized`

The GraphQL key is wrong or is the wrong key type. There are **two different** Monte
Carlo keys needed:

| Variable | Key type | How to create |
|---|---|---|
| `MCD_INGEST_ID` / `MCD_INGEST_TOKEN` | Ingestion key | `montecarlo integrations create-key --scope Ingestion` |
| `MCD_ID` / `MCD_TOKEN` | Personal API key | Monte Carlo UI → Settings → API Keys |

Swapping these two will cause 401 errors on one of the two calls. Confirm each key
is in the correct variable pair.

### `403 from Monte Carlo Ingest API`

Your Ingestion key exists but is not authorized for the target warehouse. This means:
- The key was created without `--scope Ingestion`, or
- The warehouse UUID being targeted is not associated with this key

Re-create the key with `montecarlo integrations create-key --scope Ingestion`.

---

## Warehouse UUID

### `Required environment variable 'MCD_RESOURCE_UUID' is not set`

`MCD_RESOURCE_UUID` is required. Find your Data Cloud warehouse UUID in the Monte Carlo
UI: **Settings → Integrations → your Data Cloud connection**.

---

## DLO->DMO Mapping Retrieval Errors

### `0 DLO->DMO edge(s) extracted` (Step 4)

The read-only REST endpoint (`/ssot/data-model-object-mappings`) returned no source
mappings for any catalogued DMO. Common causes:
- No DLO->DMO mappings have been defined yet in Data 360.
- The Data Cloud connector hasn't catalogued any DMOs in Monte Carlo yet — Step 3 will
  show `0 catalogued DMO(s)`. Trigger a metadata sync in Monte Carlo and re-run.

Run with `LOG_LEVEL=DEBUG` for per-DMO detail.

### `Mapping fetch failed for DMO '<name>'` warnings

One or more per-DMO mapping GETs errored (timeout or 5xx) and were skipped, so their
edges may be missing. The push is idempotent — re-run once the transient issue clears.
A `404` from the endpoint is **not** an error: it means that DMO has no source mappings
and is skipped silently (most installed standard DMOs are unmapped). If fetches are slow
on a large org, lower `MAPPING_MAX_WORKERS` and re-run.

### `HTTP 403` fetching data model objects or mappings

The connected app's run-as user cannot read the Data Cloud REST API. Grant the `api`
OAuth scope and assign a Data Cloud permission set (Data Cloud Admin or User). This is
standard read access — **no "Modify Metadata" / Metadata API permission is required.**

---

## Catalog Validation Warnings

### `NOT in MC catalog: Account__dll` (or similar table name)

Step 4b checks that each DLO and DMO table exists in Monte Carlo before pushing.
When a table is missing, its lineage edge is skipped and this warning is logged.

**Common causes:**
- The Monte Carlo Data Cloud connector has not yet completed its first metadata scan
- The table was created in Salesforce after the last connector sync
- The table name in the mapping record does not match the catalogued name

**What to do:**
1. In Monte Carlo, go to Settings → Integrations → your Data Cloud connection
2. Trigger a manual metadata sync if one is available, or wait for the scheduled sync
3. Re-run this script once the sync completes — the push is idempotent, so edges that
   were already pushed for other tables are safe to re-send

### `Catalog check complete: X found, Y missing`

If `Y > 0`, some edges will be skipped. The warning above lists the specific table
names. After the Data Cloud connector syncs those tables into Monte Carlo, re-run the
script to push their lineage edges.

### `MC GraphQL error` during catalog validation (Step 4b)

The catalog validation step failed before the push could proceed. The script exits
rather than push blindly. Check:
- `MCD_ID` / `MCD_TOKEN` are set and are a Personal API key
- Network connectivity to `https://api.getmontecarlo.com`

Run with `LOG_LEVEL=DEBUG` to see the specific error.

---

## CIO / DMO→CIO Issues

### Step 5 returns HTTP 403 (CIO fetch fails)

The connected app does not have permission to call the Data Cloud REST API
(`/services/data/v62.0/ssot/calculated-insights`). This is the same Data Cloud REST
API family used for DLO->DMO mappings in Steps 3-4.

**What to do:**
1. In Salesforce Setup → App Manager → your connected app → Edit
2. Under **OAuth Scopes**, ensure **"Access and manage your data (api)"** and
   **"Perform requests on your behalf at any time (refresh_token, offline_access)"** are included
3. Ensure the run-as user has the **Data Cloud Admin** or **Data Cloud User** permission set assigned
4. Re-authenticate (client credentials tokens may need to be refreshed)

If you only need DLO→DMO lineage and cannot resolve the CIO permission, use `--skip-cio`
to bypass Steps 5–6 entirely:

```bash
python3 push_lineage.py --skip-cio
```

### `Found 0 CIO(s)` at Step 5

The API call succeeded but the org has no Calculated Insight Objects. This is not an error.
- If you expect CIOs to exist, confirm they have been created and activated in Data 360
  (Setup → Data Cloud → Calculated Insights)
- The script will still push DLO→DMO edges normally; it just has no DMO→CIO edges to push

### DMO→CIO edges pushed but not visible in Monte Carlo lineage graph

CIO objects may not yet appear in the Monte Carlo catalog if the native Data Cloud
connector has not synced since the CIOs were created. The lineage edges were accepted
by Monte Carlo's Ingest API, but the CIO asset pages will not appear until after the
next connector metadata sync.

**What to do:**
1. In Monte Carlo, go to **Settings → Integrations → your Data Cloud connection**
2. Trigger a manual metadata sync
3. Re-run the script after the sync completes — the push is idempotent and re-running
   will correctly link the now-synced CIO assets

### `no __dlm or __cio inputs found in SQL expression` warning

The script could not identify any DMO or CIO input tables in a CIO's SQL expression.
Possible causes:
- The CIO uses an unusual SQL pattern (subquery aliasing, function-only expressions)
- The CIO's `expression` field is null or empty in the API response

Run with `LOG_LEVEL=DEBUG` to see the SQL length and exact token scan results for each
CIO. If the SQL is visible and the inputs look correct, contact Monte Carlo support with
the CIO's `apiName` and SQL expression.

---

## Push Errors

### `failed_edges_*.json` file created

One or more batches failed during the Monte Carlo push. The file contains the raw
edge objects that were not pushed. The absolute path is printed in the log.

After resolving the underlying error, re-run the full script:

```bash
python3 push_lineage.py
```

The push is idempotent — duplicate edges are safe and deduplicated by Monte Carlo.

### `0 edges found — nothing to push`

Either your org has no DLO→DMO mappings configured, or all edges were filtered by
the catalog validation step (all tables are missing from MC). Run with `--dry-run`
and `LOG_LEVEL=DEBUG` to trace the full pipeline.

If Step 4b shows many missing tables, the Data Cloud connector has likely not yet
completed a metadata scan. Trigger a manual sync in Monte Carlo and re-run.

If you see `WARNING: Could not resolve data space` in the log, set
`SF_DEFAULT_DATA_SPACE` to your org's primary data space name.

---

## Data Space Issues

### `Data space mismatch: Account__dll is in 'default' but Account__dlm is in 'other'`

Step 4b found the DLO and DMO in the MC catalog, but in **different data spaces**. The
script skips these edges rather than push lineage to the wrong place.

This usually means:
- The tables were recently moved between data spaces and the MC connector has a stale entry
- The DLO and DMO belong to different orgs or data spaces by design

**What to do:**
1. In Monte Carlo, confirm which data space each table is catalogued under
2. Trigger a manual metadata sync (Settings → Integrations → your Data Cloud connection)
3. Re-run once the sync completes — mismatched edges will be pushed once both sides agree

---

### Lineage appears in Monte Carlo but under the wrong schema / data space

The data space assigned to an edge in Monte Carlo comes from (in priority order):
1. The MC catalog's `dataset` field (authoritative — resolved during Step 4b validation)
2. The `SF_DEFAULT_DATA_SPACE` env var (default: `"default"`), as a last-resort fallback

If edges are landing under the wrong data space after validation, check that the
tables are catalogued correctly in Monte Carlo (Settings → Integrations → your Data
Cloud connection). Run with `LOG_LEVEL=DEBUG` to see the data space assigned to each
edge.

---

## Required Salesforce Permissions

The connected app's run-as user must have the following permissions. Missing any of
these is the most common cause of `403` errors.

### DLO→DMO lineage (Steps 1–4)

**Run-as user profile permission:**
- `API Enabled`

**Run-as user permission set:**
- `Data Cloud Admin` **or** `Data Cloud User` — grants read access to the Data Cloud REST
  API, including `/ssot/data-model-object-mappings`

> **No "Modify Metadata" / Metadata API permission is required.** DLO→DMO lineage is read
> entirely from the read-only Data Cloud REST API; the earlier SOAP Metadata API path
> (which required "Modify Metadata Through Metadata API Functions") has been removed.

### DMO→CIO lineage (Steps 5–8)

In addition to the profile permissions above:

**Run-as user permission set:**
- `Data Cloud Admin` **or** `Data Cloud User`

Assign in Salesforce Setup → Users → your run-as user → Permission Set Assignments.

**Connected app OAuth scopes** (Salesforce Setup → App Manager → your app → Edit):
- `Access and manage your data (api)`
- `Perform requests on your behalf at any time (refresh_token, offline_access)`

> All Data Cloud REST endpoints used here — `/ssot/data-model-object-mappings` (DLO→DMO)
> and `/ssot/calculated-insights` (CIOs) — require the `Data Cloud Admin` or `Data Cloud
> User` permission set; profile permissions alone do not grant access. `--skip-cio`
> bypasses only Steps 5–8 (CIOs); the Data Cloud permission set is still needed for the
> DLO→DMO lineage in Steps 3–4.

---

## Common Setup Checklist

If you're unsure where a problem is, work through this list:

- [ ] Python 3.9+ installed (`python3 --version`)
- [ ] Dependencies installed (`pip install -r requirements.txt`)
- [ ] `.env` file exists in the same directory as `push_lineage.py`
- [ ] `SF_ORG_URL` starts with `https://` and is the My Domain URL (not `login.salesforce.com`)
- [ ] Connected app has "Enable Client Credentials Flow" enabled
- [ ] `MCD_INGEST_ID`/`MCD_INGEST_TOKEN` are the **Ingestion** key (not the Personal key)
- [ ] `MCD_ID`/`MCD_TOKEN` are the **Personal API** key (not the Ingestion key)
- [ ] `MCD_RESOURCE_UUID` is set to the Data Cloud warehouse UUID from Monte Carlo UI
- [ ] `--dry-run` completes successfully before attempting a live push
- [ ] Monte Carlo Data Cloud connector has completed at least one metadata scan (required for catalog validation)
- [ ] Connected app has Data Cloud REST API access (required for Step 5 CIO fetch — use `--skip-cio` if not needed)

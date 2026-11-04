# Job Monitor Table Sync Scripts

Keep the table lists of Monte Carlo job monitors in sync after the migration from notification
rules to job monitors.

Some legacy notification rules filtered dbt alerts by something a job monitor can't express:

- **Table importance (key assets).** Job monitors can't filter on importance or key assets.
- **A table-name regex.** Job monitors can't filter by regex.

For these rules, the migration wrote the job monitor's table list as the tables that matched at
migration time. That list is fixed, so new matching tables don't alert and tables that stop
matching keep alerting. Run one of these scripts once a day to refresh the list:

| Script                                                 | Table list it writes                                                        |
|--------------------------------------------------------|-----------------------------------------------------------------------------|
| [sync_key_asset_tables.py](sync_key_asset_tables.py)   | Every live key asset on the monitor's warehouse.                            |
| [sync_pattern_tables.py](sync_pattern_tables.py)       | The live tables that match the monitor's regex scope in your config file.   |

Both scripts share [job_monitor_sync.py](job_monitor_sync.py), so keep the three files together.

> **The scripts own the monitor's table filters.** They replace the whole list, so any table you
> added by hand is removed if it isn't in scope. Every other setting (job filters, table
> exclusions, audiences, alert conditions, tags, and so on) is read from the monitor and written
> back unchanged.

## Installation

In a Python 3.10+ environment:

```
pip install -r requirements.txt
```

The scripts authenticate with a Monte Carlo API key, as [pycarlo](https://github.com/monte-carlo-data/python-sdk)
reads it: the `MCD_DEFAULT_API_ID` and `MCD_DEFAULT_API_TOKEN` environment variables, or a profile
in `~/.mcd/profiles.ini` passed with `--profile`. The key's user needs permission to edit job
monitors.

## Configuration

Monitors are found by their exact name in Monte Carlo. Keep your config file wherever suits your
scheduler; it doesn't need to live in this directory. Config files are YAML (JSON also works).

### Key assets

List the monitor names in a config file (see [key_asset_monitors.example.yaml](key_asset_monitors.example.yaml)):

```yaml
monitors:
  - "Example dbt job monitor"
  - "Another example dbt job monitor"
```

Or pass them on the command line with `--monitor-name`, repeated for each monitor. Names from
both are combined.

The script uses the key-asset flag, not the importance score the legacy rule used. The two can
differ, so review the first `--dry-run` before saving.

### Table patterns

Give each monitor a scope (see [pattern_monitors.example.yaml](pattern_monitors.example.yaml)):

```yaml
monitors:
  - name: "Example marketing job monitor"
    include: '^analytics:marketing\.'
    exclude: '^analytics:marketing\.(tmp|scratch)_'
    key_assets_only: true
    schemas:
      - database: analytics
        schema: marketing
```

| Key               | Required | Meaning                                                                                                                                              |
|-------------------|----------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| `name`            | yes      | The job monitor's exact name.                                                                                                                        |
| `include`         | yes      | A table is in scope when this regex matches the start of its full table id (for example `analytics:marketing.campaigns`) or its MCON, as legacy notification rules matched. |
| `exclude`         | no       | Tables where this regex matches the full table id or MCON are left out.                                                                              |
| `key_assets_only` | no       | Only include key assets. Defaults to `false`.                                                                                                        |
| `schemas`         | no       | Only read tables from these (database, schema) pairs. Faster on large warehouses. List every pair that can hold a matching table, or matches elsewhere are dropped. |

Regexes use Python's [`re.match`](https://docs.python.org/3/library/re.html#re.match). Put them
in single quotes in YAML so backslashes need no escaping.

## Usage

Run with `--dry-run` first. It prints the tables each monitor would gain and lose, and has Monte
Carlo validate the update without saving it:

```
python sync_key_asset_tables.py --config key_asset_monitors.yaml --dry-run
python sync_pattern_tables.py --config pattern_monitors.yaml --dry-run
```

Once the changes look right, run without `--dry-run` to save:

```
python sync_key_asset_tables.py --config key_asset_monitors.yaml
python sync_pattern_tables.py --config pattern_monitors.yaml
```

Other options:

- `--profile <name>`: use a profile from `~/.mcd/profiles.ini` instead of the environment variables.
- `--monitor-name <name>`: with `sync_pattern_tables.py`, only update this monitor from the config file. Repeat for several.

Example output:

```
Example finance job monitor: 42 tables in scope (1 added, 1 removed).
  + analytics:finance.new_table
  - analytics:finance.dropped_table
  Saved.
Example marketing job monitor: 7 tables in scope (0 added, 0 removed).
  No change.
```

### Run daily

Schedule the script once a day with cron or your scheduler, for example:

```
0 6 * * * cd /path/to/job_monitors && /path/to/venv/bin/python sync_pattern_tables.py \
  --profile <name> --config /path/to/pattern_monitors.yaml
```

Don't put the API token in the crontab. Use a profile in `~/.mcd/profiles.ini`, or set
`MCD_DEFAULT_API_ID` and `MCD_DEFAULT_API_TOKEN` from your scheduler's secret store.

A monitor is only saved when its table list changed.

### When a monitor is left unchanged

The script leaves a monitor unchanged, prints why, and keeps going with the others when:

- no job monitor, or more than one, has that name;
- the monitor is managed by monitors-as-code (update its table list there instead);
- its table filters aren't a plain list of tables (for example a database or schema filter, a
  negated table, or an AND joiner), which the script can't rewrite without changing what they mean;
- no warehouse is found for the monitor's dbt connection;
- no table matches its scope (an empty table list would make the monitor alert on every table);
- Monte Carlo returns an error for it (for example, it rejects the update).

If any monitor was left unchanged, the script exits with a nonzero status, so your scheduler can
flag the run.

## Development

```
pip install -r requirements-dev.txt
pytest test_job_monitor_sync.py -v
```

The tests mock all Monte Carlo API responses; they don't call Monte Carlo.

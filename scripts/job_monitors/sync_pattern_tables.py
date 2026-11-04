"""Keep Monte Carlo job monitors' table lists in sync with table-name patterns.

Before the job monitor migration, some notification rules alerted only on tables whose name
matched a regex. Job monitors cannot filter by regex, so the migration wrote each monitor's
table list as the tables that matched at migration time. This script replaces each monitor's
table list with the live tables that match its scope in the config file now. Run it once a day
so new matching tables start alerting and tables that stop matching stop.

A table is in scope when ``include`` matches the start of its full table id or its MCON (as
legacy notification rules matched them), it is a key asset if ``key_assets_only`` is set, and
``exclude`` matches neither. ``schemas``, if set, limits the tables read to those
(database, schema) pairs, which is faster on large warehouses.

The script replaces the monitor's whole table list and changes no other setting. It skips
monitors whose table filters aren't a plain list of tables.

Usage:
    python sync_pattern_tables.py --config pattern_monitors.yaml --dry-run
    python sync_pattern_tables.py --config pattern_monitors.yaml
"""

import argparse
import re
from pathlib import Path
from typing import Any

from job_monitor_sync import ConfigError, Scope, load_config, run, select

SCOPE_KEYS = {"name", "include", "exclude", "key_assets_only", "schemas"}


def parse_scope(entry: Any) -> tuple[str, Scope]:
    """One monitor's name and scope, from its entry in the config file's 'monitors' list."""
    if not isinstance(entry, dict):
        raise ConfigError("each entry in 'monitors' must be a mapping with 'name' and 'include'")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("each entry in 'monitors' needs a 'name'")
    unknown = sorted(set(entry) - SCOPE_KEYS)
    if unknown:
        raise ConfigError(f"{name}: unknown keys {', '.join(unknown)}")

    include, exclude = entry.get("include"), entry.get("exclude")
    if not isinstance(include, str) or not include:
        raise ConfigError(f"{name}: 'include' must be a regex")
    if exclude is not None and (not isinstance(exclude, str) or not exclude):
        raise ConfigError(f"{name}: 'exclude' must be a regex")
    for key, pattern in (("include", include), ("exclude", exclude)):
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ConfigError(f"{name}: invalid '{key}' regex: {error}") from error

    key_assets_only = entry.get("key_assets_only", False)
    if not isinstance(key_assets_only, bool):
        raise ConfigError(f"{name}: 'key_assets_only' must be true or false")

    schemas = []
    for schema in entry.get("schemas") or []:
        if not (
            isinstance(schema, dict)
            and set(schema) == {"database", "schema"}
            and all(isinstance(v, str) and v for v in schema.values())
        ):
            raise ConfigError(f"{name}: each entry in 'schemas' needs a 'database' and 'schema'")
        schemas.append((schema["database"], schema["schema"]))

    return name, Scope(include, exclude, key_assets_only, tuple(schemas))


def pattern_scopes(args: argparse.Namespace) -> dict[str, Scope]:
    """Each monitor's scope from the config file, limited to --monitor-name if given."""
    monitors = load_config(args.config).get("monitors")
    if not isinstance(monitors, list):
        raise ConfigError("'monitors' in the config file must be a list")
    scopes: dict[str, Scope] = {}
    for entry in monitors:
        name, scope = parse_scope(entry)
        if name in scopes:
            raise ConfigError(f"{name}: listed more than once in the config file")
        scopes[name] = scope
    return select(scopes, args.monitor_name)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="YAML or JSON file with each job monitor's table scope.",
    )
    parser.add_argument(
        "--monitor-name",
        action="append",
        help="Only update this monitor from the config file. Repeat for several.",
    )


def main(argv: list[str] | None = None) -> None:
    run(__doc__.split("\n")[0], pattern_scopes, configure, argv)


if __name__ == "__main__":
    main()

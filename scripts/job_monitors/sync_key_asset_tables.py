"""Keep Monte Carlo job monitors' table lists in sync with your key assets.

Before the job monitor migration, some notification rules alerted only on tables with a high
importance score. Job monitors cannot filter on importance or key assets, so the migration wrote
each monitor's table list as the tables that matched at migration time. This script replaces
each named monitor's table list with every live key asset on the monitor's warehouse. Run it
once a day so tables you mark as key assets start alerting and tables you unmark stop.

The script uses the key-asset flag, not the importance score. The two can differ, so review the
first --dry-run before saving.

The script replaces the monitor's whole table list and changes nothing else.

Usage:
    python sync_key_asset_tables.py --config key_asset_monitors.yaml --dry-run
    python sync_key_asset_tables.py --monitor-name "My job monitor" --dry-run
"""

import argparse
from pathlib import Path

from job_monitor_sync import KEY_ASSETS, ConfigError, Scope, load_config, run


def key_asset_scopes(args: argparse.Namespace) -> dict[str, Scope]:
    """The monitors named in the config file and on the command line."""
    names: list[str] = []
    if args.config:
        monitors = load_config(args.config).get("monitors")
        if not isinstance(monitors, list) or not all(
            isinstance(name, str) and name for name in monitors
        ):
            raise ConfigError("'monitors' in the config file must be a list of monitor names")
        names += monitors
    names += args.monitor_name or []
    return {name: KEY_ASSETS for name in dict.fromkeys(names)}


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help="YAML or JSON file with a 'monitors' list of job monitor names.",
    )
    parser.add_argument(
        "--monitor-name",
        action="append",
        help="Job monitor to update. Repeat for several. Added to the config file's monitors.",
    )


def main(argv: list[str] | None = None) -> None:
    run(__doc__.split("\n")[0], key_asset_scopes, configure, argv)


if __name__ == "__main__":
    main()

"""Shared logic for keeping Monte Carlo job monitors' table lists in sync.

The job monitor migration turned legacy notification rules into job monitors. Rules that
filtered on table importance (key assets) or on a table-name regex cannot be expressed in a job
monitor, so the migration froze them into a fixed list of full table ids. The scripts in this
directory refresh that list from the tables that match now:

- sync_key_asset_tables.py: every live key asset on the monitor's warehouse.
- sync_pattern_tables.py: the live tables that match a regex scope from a config file.

Both share this module. A key-asset monitor is a pattern scope that matches every key asset.
"""

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml
from pycarlo.common.errors import GqlError
from pycarlo.core import Client, Session

PAGE_SIZE = 1000


@dataclass(frozen=True)
class Scope:
    """The tables a monitor alerts on, as its legacy rule defined them.

    A table is in scope when ``include`` matches the start of its full table id or MCON, it is
    a key asset if ``key_assets_only``, and ``exclude`` matches neither. ``schemas`` lists the
    (database, schema) pairs that can hold matching tables, so only those are read.
    """

    include: str
    exclude: str | None = None
    key_assets_only: bool = False
    schemas: tuple[tuple[str, str], ...] = ()


# Every live key asset on the warehouse.
KEY_ASSETS = Scope(include=".*", key_assets_only=True)

GET_JOB_MONITORS = """
query GetJobMonitors($limit: Int!, $offset: Int!) {
  getMonitors(monitorTypes: [JOB], limit: $limit, offset: $offset) {
    uuid
    name
  }
}
"""

GET_JOB_MONITOR = """
query GetJobMonitor($uuid: UUID!) {
  getJobMonitor(monitorUuid: $uuid) {
    uuid
    name
    description
    notes
    priority
    alertGrouping { mode maxHours renotifyEveryHours }
    integrationType
    integrationUuid
    assetSelection {
      filters { ...JobFilter }
      filtersJoiner
      exclusions { ...JobFilter }
      exclusionsJoiner
      tableFilters { ...TableFilter }
      tableFiltersJoiner
      tableExclusions { ...TableFilter }
      tableExclusionsJoiner
    }
    jobMcons
    alertConditions { condition }
    audiences
    audienceConditions { audience triagePriority }
    failureAudiences
    tags { name value }
    dataQualityDimension
    domains { uuid }
    isDraft
    isTemplateManaged
    autoTriage
  }
}

fragment JobFilter on JobAssetFilterInterface {
  type
  negated
  ... on JobAssetFilterTag { jobTags jobTagsOperator }
  ... on JobAssetFilterTagPair { jobTagPairs { name value } jobTagsOperator }
  ... on JobAssetFilterJobMcon { jobMcons }
  ... on JobAssetFilterDbtProject { dbtProjectUuids }
}

fragment TableFilter on JobTableFilterInterface {
  type
  negated
  ... on JobTableFilterFullTableId { fullTableId }
  ... on JobTableFilterDatabase { databases }
  ... on JobTableFilterSchema { database schemas }
}
"""

GET_INTEGRATIONS = """
query GetJobMonitorIntegrations {
  getJobMonitorIntegrations { integrationUuid warehouseUuid }
}
"""

GET_TABLES = """
query GetTables($dwId: UUID!, $first: Int!, $after: String%(params)s) {
  getTables(dwId: $dwId, isDeleted: false, first: $first, after: $after%(args)s) {
    edges { node { fullTableId mcon } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

UPDATE_JOB_MONITOR = """
mutation UpdateJobMonitor(
  $uuid: UUID!, $description: String!, $integrationType: JobMonitorIntegrationType!,
  $integrationUuid: UUID!, $alertConditions: [JobMonitorAlertConditionInput!]!,
  $assetSelection: JobAssetSelectionInput!, $jobMcons: [String!]!, $notes: String,
  $priority: String, $alertGrouping: AlertGroupingInput, $audiences: [String!],
  $audienceConditions: [AudienceConditionInput!], $failureAudiences: [String!],
  $domainRestrictions: [UUID!], $tags: [TagKeyValuePairInput!], $dataQualityDimension: String,
  $isDraft: Boolean, $autoTriage: Boolean, $dryRun: Boolean
) {
  createOrUpdateJobMonitor(
    uuid: $uuid, description: $description, integrationType: $integrationType,
    integrationUuid: $integrationUuid, alertConditions: $alertConditions,
    assetSelection: $assetSelection, jobMcons: $jobMcons, notes: $notes, priority: $priority,
    alertGrouping: $alertGrouping, audiences: $audiences,
    audienceConditions: $audienceConditions, failureAudiences: $failureAudiences,
    domainRestrictions: $domainRestrictions, tags: $tags,
    dataQualityDimension: $dataQualityDimension, isDraft: $isDraft, autoTriage: $autoTriage,
    dryRun: $dryRun
  ) {
    jobMonitor { uuid }
  }
}
"""

TableCache = dict[tuple, list[tuple[str, str]]]


class SkipMonitor(Exception):
    """The monitor is left unchanged, for the reason in the message."""


class ConfigError(Exception):
    """The config file or command line arguments are invalid."""


def job_monitor_uuids(client: Client) -> dict[str, list[str]]:
    """Every job monitor's uuids, by name."""
    by_name: dict[str, list[str]] = {}
    offset = 0
    while True:
        page = client(
            GET_JOB_MONITORS, variables={"limit": PAGE_SIZE, "offset": offset}
        ).get_monitors
        for monitor in page:
            by_name.setdefault(monitor.name, []).append(str(monitor.uuid))
        if len(page) < PAGE_SIZE:
            return by_name
        offset += PAGE_SIZE


def warehouse_uuids(client: Client) -> dict[str, str]:
    """Each dbt connection's warehouse uuid, by connection uuid."""
    return {
        str(i.integration_uuid): str(i.warehouse_uuid)
        for i in client(GET_INTEGRATIONS).get_job_monitor_integrations
        if i.warehouse_uuid
    }


def warehouse_tables(
    client: Client, dw_id: str, key_assets_only: bool, schema: tuple[str, str] | None
) -> list[tuple[str, str]]:
    """Live tables on the warehouse, as (full table id, MCON) pairs."""
    params, args = "", ""
    variables: dict[str, Any] = {"dwId": dw_id, "first": PAGE_SIZE}
    if key_assets_only:
        args += ", isImportant: true"
    if schema is not None:
        params += ", $projectName: String!, $dataset: String!"
        args += ", projectName: $projectName, dataset: $dataset"
        variables.update(projectName=schema[0], dataset=schema[1])
    query = GET_TABLES % {"params": params, "args": args}
    tables: list[tuple[str, str]] = []
    after = None
    while True:
        page = client(query, variables={**variables, "after": after}).get_tables
        tables += [(edge.node.full_table_id, edge.node.mcon) for edge in page.edges]
        if not page.page_info.has_next_page:
            return tables
        after = page.page_info.end_cursor


def scoped_tables(client: Client, dw_id: str, scope: Scope, cache: TableCache) -> list[str]:
    """The full table ids in ``scope`` on the warehouse, matched as legacy routing matched them."""
    tables: list[tuple[str, str]] = []
    for schema in scope.schemas or (None,):
        key = (dw_id, scope.key_assets_only, schema)
        if key not in cache:
            cache[key] = warehouse_tables(client, dw_id, scope.key_assets_only, schema)
        tables += cache[key]

    def matches(pattern: str, table: tuple[str, str]) -> bool:
        return any(re.match(pattern, value) for value in table if value)

    return sorted(
        {
            table[0]
            for table in tables
            if matches(scope.include, table)
            and not (scope.exclude and matches(scope.exclude, table))
        }
    )


def job_filter_input(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": row.type, "negated": row.negated}
    for field in ("job_tags", "job_tags_operator", "job_mcons", "dbt_project_uuids"):
        if hasattr(row, field):
            out[_camel(field)] = _plain(getattr(row, field))
    if hasattr(row, "job_tag_pairs"):
        out["jobTagPairs"] = [{"name": p.name, "value": p.value} for p in row.job_tag_pairs]
    return out


def table_filter_input(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": row.type, "negated": row.negated}
    for field in ("full_table_id", "databases", "database", "schemas"):
        if hasattr(row, field):
            out[_camel(field)] = _plain(getattr(row, field))
    return out


def update_variables(monitor: Any, tables: list[str], dry_run: bool) -> dict[str, Any]:
    """The monitor's current settings, with its table list replaced by ``tables``.

    Every UPDATE_JOB_MONITOR variable must be filled here from GET_JOB_MONITOR, or the full
    upsert resets that setting.
    """
    selection = monitor.asset_selection
    grouping = monitor.alert_grouping
    return {
        "uuid": str(monitor.uuid),
        "description": monitor.description,
        "integrationType": monitor.integration_type,
        "integrationUuid": str(monitor.integration_uuid),
        "alertConditions": [{"condition": c.condition} for c in monitor.alert_conditions],
        "assetSelection": {
            "filters": [job_filter_input(r) for r in selection.filters],
            "filtersJoiner": selection.filters_joiner,
            "exclusions": [job_filter_input(r) for r in selection.exclusions],
            "exclusionsJoiner": selection.exclusions_joiner,
            "tableFilters": [{"type": "FULL_TABLE_ID", "fullTableId": t} for t in tables],
            "tableFiltersJoiner": "OR",
            "tableExclusions": [table_filter_input(r) for r in selection.table_exclusions],
            "tableExclusionsJoiner": selection.table_exclusions_joiner,
        },
        "jobMcons": list(monitor.job_mcons),
        "notes": monitor.notes,
        "priority": monitor.priority,
        "alertGrouping": (
            {
                "mode": grouping.mode,
                "maxHours": grouping.max_hours,
                "renotifyEveryHours": grouping.renotify_every_hours,
            }
            if grouping
            else None
        ),
        "audiences": list(monitor.audiences),
        "audienceConditions": [
            {"audience": c.audience, "triagePriority": list(c.triage_priority)}
            for c in monitor.audience_conditions
        ],
        "failureAudiences": list(monitor.failure_audiences),
        "domainRestrictions": [str(d.uuid) for d in monitor.domains],
        "tags": [{"name": t.name, "value": t.value} for t in monitor.tags],
        "dataQualityDimension": monitor.data_quality_dimension,
        "isDraft": monitor.is_draft,
        "autoTriage": monitor.auto_triage,
        "dryRun": dry_run,
    }


def refresh(
    client: Client,
    name: str,
    scope: Scope,
    uuids: dict[str, list[str]],
    warehouses: dict[str, str],
    cache: TableCache,
    dry_run: bool,
) -> None:
    """Replace one monitor's table list with the tables in its scope."""
    matches = uuids.get(name, [])
    if len(matches) != 1:
        raise SkipMonitor(f"expected one job monitor with this name, found {len(matches)}")
    monitor = client(GET_JOB_MONITOR, variables={"uuid": matches[0]}).get_job_monitor
    if monitor.is_template_managed:
        raise SkipMonitor("managed by monitors-as-code; update its table list there instead")
    selection = monitor.asset_selection
    filters = selection.table_filters
    # The joiner only matters with two or more filters.
    if any(r.type != "FULL_TABLE_ID" or r.negated for r in filters) or (
        len(filters) > 1 and selection.table_filters_joiner != "OR"
    ):
        raise SkipMonitor("its table filters are not a plain list of tables; update them by hand")
    dw_id = warehouses.get(str(monitor.integration_uuid))
    if dw_id is None:
        raise SkipMonitor(f"no warehouse found for dbt connection {monitor.integration_uuid}")
    tables = scoped_tables(client, dw_id, scope, cache)
    if not tables:
        # An empty table list would match every table, so the monitor would alert on all of them.
        raise SkipMonitor("no table on its warehouse matches its scope")

    current = {r.full_table_id for r in filters}
    added, removed = sorted(set(tables) - current), sorted(current - set(tables))
    print(f"{name}: {len(tables)} tables in scope ({len(added)} added, {len(removed)} removed).")
    for table in added:
        print(f"  + {table}")
    for table in removed:
        print(f"  - {table}")
    if not added and not removed:
        print("  No change.")
        return
    client(UPDATE_JOB_MONITOR, variables=update_variables(monitor, tables, dry_run))
    print("  Validated. Nothing saved (dry run)." if dry_run else "  Saved.")


def sync(client: Client, scopes: dict[str, Scope], dry_run: bool) -> list[str]:
    """Refresh every monitor in ``scopes``. Returns the names of monitors left unchanged."""
    uuids = job_monitor_uuids(client)
    warehouses = warehouse_uuids(client)
    cache: TableCache = {}
    failed = []
    for name, scope in scopes.items():
        try:
            refresh(client, name, scope, uuids, warehouses, cache, dry_run)
        except SkipMonitor as error:
            print(f"{name}: left unchanged, {error}.")
            failed.append(name)
        except (GqlError, requests.RequestException) as error:
            print(f"{name}: left unchanged, Monte Carlo returned an error: {error}.")
            failed.append(name)
    return failed


def load_config(path: Path) -> dict[str, Any]:
    """The YAML (or JSON) config file's top-level mapping."""
    try:
        config = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot read config file {path}: {error}") from error
    if not isinstance(config, dict):
        raise ConfigError(f"config file {path} must hold a mapping with a 'monitors' key")
    return config


def select(scopes: dict[str, Scope], names: list[str] | None) -> dict[str, Scope]:
    """Only the scopes named by --monitor-name, if given."""
    if not names:
        return scopes
    unknown = sorted(set(names) - set(scopes))
    if unknown:
        raise ConfigError(f"monitors not in the config file: {', '.join(unknown)}")
    return {name: scopes[name] for name in dict.fromkeys(names)}


def run(
    description: str,
    build_scopes: Callable[[argparse.Namespace], dict[str, Scope]],
    configure: Callable[[argparse.ArgumentParser], None],
    argv: list[str] | None = None,
) -> None:
    """Parse the command line, build each monitor's scope, and refresh the monitors."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the changes with Monte Carlo and print them, without saving.",
    )
    parser.add_argument("--profile", help="Profile in ~/.mcd/profiles.ini to use.")
    configure(parser)
    args = parser.parse_args(argv)
    try:
        scopes = build_scopes(args)
    except ConfigError as error:
        parser.error(str(error))
    if not scopes:
        parser.error("no monitors to update")

    session = Session(mcd_profile=args.profile) if args.profile else Session()
    failed = sync(Client(session=session), scopes, args.dry_run)
    if failed:
        sys.exit(f"{len(failed)} monitor(s) left unchanged: {', '.join(failed)}.")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _plain(value: Any) -> Any:
    if isinstance(value, list):
        return [str(v) if not isinstance(v, str) else v for v in value]
    return value

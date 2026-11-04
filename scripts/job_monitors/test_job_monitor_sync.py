"""Tests for the job monitor table sync scripts, with mocked Monte Carlo API responses.

Run:  pytest test_job_monitor_sync.py -v
"""

import copy
import re
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests
from box import Box
from pycarlo.common.errors import GqlError

import job_monitor_sync as sut
import sync_key_asset_tables
import sync_pattern_tables
from job_monitor_sync import KEY_ASSETS, Scope

HERE = Path(__file__).parent
DW = "11111111-1111-1111-1111-111111111111"
INTEGRATION = "22222222-2222-2222-2222-222222222222"


def table(full_table_id: str, important: bool = False) -> dict[str, Any]:
    database, rest = full_table_id.split(":")
    schema, name = rest.split(".")
    return {
        "fullTableId": full_table_id,
        "mcon": f"MCON++acct++{DW}++table++{full_table_id}",
        "important": important,
        "database": database,
        "schema": schema,
    }


def job_monitor(uuid: str, name: str, tables: list[str], **overrides: Any) -> dict[str, Any]:
    """A getJobMonitor response, as the API returns it."""
    monitor = {
        "uuid": uuid,
        "name": name,
        "description": name,
        "notes": "some notes",
        "priority": "P2",
        "alertGrouping": {"mode": "BY_JOB", "maxHours": 6, "renotifyEveryHours": None},
        "integrationType": "DBT",
        "integrationUuid": INTEGRATION,
        "assetSelection": {
            "filters": [
                {
                    "type": "JOB_TAG",
                    "negated": False,
                    "jobTags": ["nightly"],
                    "jobTagsOperator": "OR",
                }
            ],
            "filtersJoiner": "AND",
            "exclusions": [],
            "exclusionsJoiner": "OR",
            "tableFilters": [
                {"type": "FULL_TABLE_ID", "negated": False, "fullTableId": t} for t in tables
            ],
            "tableFiltersJoiner": "OR",
            "tableExclusions": [
                {"type": "SCHEMA", "negated": False, "database": "db", "schemas": ["tmp"]}
            ],
            "tableExclusionsJoiner": "OR",
        },
        "jobMcons": [],
        "alertConditions": [{"condition": "FAILURE"}],
        "audiences": ["data-team"],
        "audienceConditions": [{"audience": "data-team", "triagePriority": ["P1", "P2"]}],
        "failureAudiences": [],
        "tags": [{"name": "team", "value": "data"}],
        "dataQualityDimension": None,
        "domains": [{"uuid": "33333333-3333-3333-3333-333333333333"}],
        "isDraft": False,
        "isTemplateManaged": False,
        "autoTriage": True,
    }
    monitor.update(overrides)
    return monitor


class FakeClient:
    """Answers the scripts' GraphQL queries from in-memory data, as pycarlo's Client would."""

    def __init__(
        self,
        monitors: list[dict[str, Any]],
        tables: list[dict[str, Any]],
        warehouses: dict[str, str] | None = None,
    ):
        self.monitors = monitors
        self.tables = tables
        self.warehouses = {INTEGRATION: DW} if warehouses is None else warehouses
        self.table_reads: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def __call__(self, query: str, variables: dict[str, Any] | None = None) -> Box:
        variables = variables or {}
        if "query GetJobMonitors(" in query:
            page = self.monitors[variables["offset"] : variables["offset"] + variables["limit"]]
            data = {"getMonitors": [{"uuid": m["uuid"], "name": m["name"]} for m in page]}
        elif "query GetJobMonitorIntegrations" in query:
            data = {
                "getJobMonitorIntegrations": [
                    {"integrationUuid": i, "warehouseUuid": w} for i, w in self.warehouses.items()
                ]
            }
        elif "query GetJobMonitor(" in query:
            (monitor,) = [m for m in self.monitors if m["uuid"] == variables["uuid"]]
            data = {"getJobMonitor": copy.deepcopy(monitor)}
        elif "query GetTables(" in query:
            data = {"getTables": self.get_tables(query, variables)}
        elif "mutation UpdateJobMonitor(" in query:
            self.updates.append(variables)
            data = {"createOrUpdateJobMonitor": {"jobMonitor": {"uuid": variables["uuid"]}}}
        else:
            raise AssertionError(f"unexpected query: {query}")
        # pycarlo wraps string query responses the same way.
        return Box({"data": data}, camel_killer_box=True).data

    def get_tables(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert variables["dwId"] == DW
        key_assets_only = "isImportant: true" in query
        self.table_reads.append(
            {
                "key_assets_only": key_assets_only,
                "schema": (variables.get("projectName"), variables.get("dataset")),
            }
        )
        rows = [
            t
            for t in self.tables
            if (t["important"] or not key_assets_only)
            and variables.get("projectName", t["database"]) == t["database"]
            and variables.get("dataset", t["schema"]) == t["schema"]
        ]
        # Two rows per page, to exercise paging.
        start = int(variables["after"] or 0)
        page = rows[start : start + 2]
        return {
            "edges": [{"node": {"fullTableId": t["fullTableId"], "mcon": t["mcon"]}} for t in page],
            "pageInfo": {
                "hasNextPage": start + 2 < len(rows),
                "endCursor": str(start + 2),
            },
        }


def table_ids(update: dict[str, Any]) -> list[str]:
    return [f["fullTableId"] for f in update["assetSelection"]["tableFilters"]]


# ── refresh / sync ────────────────────────────────────────────────────────────


class TestSync:
    def test_change_replaces_table_list_and_keeps_other_settings(self, capsys):
        monitor = job_monitor("m1", "Monitor", ["db:a.old", "db:a.kept"])
        client = FakeClient(
            [monitor], [table("db:a.kept", True), table("db:a.new", True), table("db:a.other")]
        )

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == []

        (update,) = client.updates
        assert table_ids(update) == ["db:a.kept", "db:a.new"]
        assert update == {
            "uuid": "m1",
            "description": "Monitor",
            "integrationType": "DBT",
            "integrationUuid": INTEGRATION,
            "alertConditions": [{"condition": "FAILURE"}],
            "assetSelection": {
                "filters": [
                    {
                        "type": "JOB_TAG",
                        "negated": False,
                        "jobTags": ["nightly"],
                        "jobTagsOperator": "OR",
                    }
                ],
                "filtersJoiner": "AND",
                "exclusions": [],
                "exclusionsJoiner": "OR",
                "tableFilters": [
                    {"type": "FULL_TABLE_ID", "fullTableId": "db:a.kept"},
                    {"type": "FULL_TABLE_ID", "fullTableId": "db:a.new"},
                ],
                "tableFiltersJoiner": "OR",
                "tableExclusions": [
                    {"type": "SCHEMA", "negated": False, "database": "db", "schemas": ["tmp"]}
                ],
                "tableExclusionsJoiner": "OR",
            },
            "jobMcons": [],
            "notes": "some notes",
            "priority": "P2",
            "alertGrouping": {"mode": "BY_JOB", "maxHours": 6, "renotifyEveryHours": None},
            "audiences": ["data-team"],
            "audienceConditions": [{"audience": "data-team", "triagePriority": ["P1", "P2"]}],
            "failureAudiences": [],
            "domainRestrictions": ["33333333-3333-3333-3333-333333333333"],
            "tags": [{"name": "team", "value": "data"}],
            "dataQualityDimension": None,
            "isDraft": False,
            "autoTriage": True,
            "dryRun": False,
        }
        out = capsys.readouterr().out
        assert "Monitor: 2 tables in scope (1 added, 1 removed)." in out
        assert "  + db:a.new" in out
        assert "  - db:a.old" in out
        assert "Saved." in out

    def test_dry_run_validates_without_saving(self, capsys):
        client = FakeClient([job_monitor("m1", "Monitor", ["db:a.old"])], [table("db:a.new", True)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=True) == []

        (update,) = client.updates
        assert update["dryRun"] is True
        assert "Validated. Nothing saved (dry run)." in capsys.readouterr().out

    def test_no_change_saves_nothing(self, capsys):
        client = FakeClient(
            [job_monitor("m1", "Monitor", ["db:a.t1", "db:a.t2"])],
            [table("db:a.t1", True), table("db:a.t2", True)],
        )

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == []

        assert client.updates == []
        assert "No change." in capsys.readouterr().out

    def test_missing_monitor_is_skipped(self, capsys):
        client = FakeClient([job_monitor("m1", "Other", ["db:a.t1"])], [table("db:a.t1", True)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert (
            "Monitor: left unchanged, expected one job monitor with this name, found 0."
            in capsys.readouterr().out
        )

    def test_duplicate_monitor_name_is_skipped(self, capsys):
        client = FakeClient(
            [job_monitor("m1", "Monitor", ["db:a.old"]), job_monitor("m2", "Monitor", [])],
            [table("db:a.t1", True)],
        )

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert "found 2" in capsys.readouterr().out

    def test_template_managed_monitor_is_skipped(self, capsys):
        client = FakeClient(
            [job_monitor("m1", "Monitor", ["db:a.old"], isTemplateManaged=True)],
            [table("db:a.t1", True)],
        )

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert client.table_reads == []
        assert "managed by monitors-as-code" in capsys.readouterr().out

    def test_monitor_without_warehouse_is_skipped(self, capsys):
        client = FakeClient(
            [job_monitor("m1", "Monitor", ["db:a.old"])], [table("db:a.t1", True)], warehouses={}
        )

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert f"no warehouse found for dbt connection {INTEGRATION}" in capsys.readouterr().out

    def test_empty_result_is_skipped_rather_than_saving_an_empty_list(self, capsys):
        client = FakeClient(
            [job_monitor("m1", "Monitor", ["db:a.old"])], [table("db:a.t1", important=False)]
        )

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert "no table on its warehouse matches its scope" in capsys.readouterr().out

    def test_one_skipped_monitor_does_not_stop_the_others(self):
        client = FakeClient(
            [
                job_monitor("m1", "Managed", ["db:a.old"], isTemplateManaged=True),
                job_monitor("m2", "Monitor", ["db:a.old"]),
            ],
            [table("db:a.t1", True)],
        )

        failed = sut.sync(client, {"Managed": KEY_ASSETS, "Monitor": KEY_ASSETS}, dry_run=False)

        assert failed == ["Managed"]
        assert [u["uuid"] for u in client.updates] == ["m2"]

    def test_schema_table_filter_is_skipped(self, capsys):
        monitor = job_monitor("m1", "Monitor", ["db:a.old"])
        monitor["assetSelection"]["tableFilters"].insert(
            0, {"type": "SCHEMA", "negated": False, "database": "db", "schemas": ["a"]}
        )
        client = FakeClient([monitor], [table("db:a.new", True)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert "table filters" in capsys.readouterr().out

    def test_negated_table_filter_is_skipped(self, capsys):
        monitor = job_monitor("m1", "Monitor", ["db:a.t2"])
        monitor["assetSelection"]["tableFilters"].append(
            {"type": "FULL_TABLE_ID", "negated": True, "fullTableId": "db:a.t1"}
        )
        client = FakeClient([monitor], [table("db:a.t1", True), table("db:a.t2", True)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert "table filters" in capsys.readouterr().out

    def test_and_table_filters_joiner_is_skipped(self, capsys):
        monitor = job_monitor("m1", "Monitor", ["db:a.t1", "db:a.t2"])
        monitor["assetSelection"]["tableFiltersJoiner"] = "AND"
        client = FakeClient([monitor], [table(f"db:a.t{i}", True) for i in (1, 2, 3)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == ["Monitor"]

        assert client.updates == []
        assert "table filters" in capsys.readouterr().out

    def test_joiner_is_ignored_with_a_single_table_filter(self):
        monitor = job_monitor("m1", "Monitor", ["db:a.t1"])
        monitor["assetSelection"]["tableFiltersJoiner"] = "AND"
        client = FakeClient([monitor], [table("db:a.t1", True), table("db:a.t2", True)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == []

        (update,) = client.updates
        assert table_ids(update) == ["db:a.t1", "db:a.t2"]
        assert update["assetSelection"]["tableFiltersJoiner"] == "OR"

    def test_api_error_on_update_does_not_stop_the_others(self, capsys):
        class RejectingClient(FakeClient):
            def __call__(self, query, variables=None):
                if "mutation UpdateJobMonitor(" in query and variables["uuid"] == "m1":
                    raise GqlError(summary="rejected")
                return super().__call__(query, variables)

        client = RejectingClient(
            [job_monitor("m1", "A", ["db:a.old"]), job_monitor("m2", "B", ["db:a.old"])],
            [table("db:a.t1", True)],
        )

        failed = sut.sync(client, {"A": KEY_ASSETS, "B": KEY_ASSETS}, dry_run=False)

        assert failed == ["A"]
        assert [u["uuid"] for u in client.updates] == ["m2"]
        assert "A: left unchanged, Monte Carlo returned an error: rejected." in (
            capsys.readouterr().out
        )

    def test_connection_error_on_read_does_not_stop_the_others(self):
        class FlakyClient(FakeClient):
            failed_once = False

            def __call__(self, query, variables=None):
                if "query GetTables(" in query and not self.failed_once:
                    self.failed_once = True
                    raise requests.exceptions.ConnectionError("boom")
                return super().__call__(query, variables)

        client = FlakyClient(
            [job_monitor("m1", "A", ["db:a.old"]), job_monitor("m2", "B", ["db:a.old"])],
            [table("db:a.t1", True)],
        )

        failed = sut.sync(client, {"A": KEY_ASSETS, "B": KEY_ASSETS}, dry_run=False)

        assert failed == ["A"]
        assert [u["uuid"] for u in client.updates] == ["m2"]

    @pytest.mark.parametrize(
        "job_filter",
        [
            {"type": "JOB_TAG", "negated": False, "jobTags": ["nightly"], "jobTagsOperator": "OR"},
            {
                "type": "JOB_TAG_PAIR",
                "negated": True,
                "jobTagPairs": [{"name": "k", "value": "v"}],
                "jobTagsOperator": "AND",
            },
            {"type": "JOB_MCON", "negated": False, "jobMcons": ["MCON++job"]},
            {
                "type": "DBT_PROJECT",
                "negated": False,
                "dbtProjectUuids": ["44444444-4444-4444-4444-444444444444"],
            },
        ],
        ids=lambda f: f["type"],
    )
    def test_job_filters_round_trip(self, job_filter):
        monitor = job_monitor("m1", "Monitor", ["db:a.old"])
        monitor["assetSelection"]["filters"] = [job_filter]
        monitor["assetSelection"]["exclusions"] = [job_filter]
        client = FakeClient([monitor], [table("db:a.new", True)])

        assert sut.sync(client, {"Monitor": KEY_ASSETS}, dry_run=False) == []

        (update,) = client.updates
        assert update["assetSelection"]["filters"] == [job_filter]
        assert update["assetSelection"]["exclusions"] == [job_filter]

    def test_update_variables_cover_every_mutation_variable(self):
        header = sut.UPDATE_JOB_MONITOR.split("mutation UpdateJobMonitor(")[1].split(") {")[0]
        declared = set(re.findall(r"\$(\w+):", header))
        monitor = Box(job_monitor("m1", "Monitor", ["db:a.t1"]), camel_killer_box=True)

        assert declared == set(sut.update_variables(monitor, ["db:a.t1"], False))

    def test_job_monitors_are_paged(self, monkeypatch):
        monkeypatch.setattr(sut, "PAGE_SIZE", 2)
        monitors = [job_monitor(f"m{i}", f"Monitor {i}", []) for i in range(5)]

        uuids = sut.job_monitor_uuids(FakeClient(monitors, []))

        assert uuids == {f"Monitor {i}": [f"m{i}"] for i in range(5)}


# ── scoped_tables ─────────────────────────────────────────────────────────────


class TestScopedTables:
    TABLES = [
        table("analytics:finance.orders", important=True),
        table("analytics:finance.tmp_orders", important=True),
        table("analytics:finance.invoices"),
        table("analytics:marketing.campaigns", important=True),
        table("raw:finance.orders"),
    ]

    def scoped(self, scope: Scope, client: FakeClient | None = None) -> list[str]:
        return sut.scoped_tables(client or FakeClient([], self.TABLES), DW, scope, {})

    def test_key_assets_reads_only_key_assets(self):
        client = FakeClient([], self.TABLES)

        assert self.scoped(KEY_ASSETS, client) == [
            "analytics:finance.orders",
            "analytics:finance.tmp_orders",
            "analytics:marketing.campaigns",
        ]
        assert client.table_reads == [{"key_assets_only": True, "schema": (None, None)}] * 2

    def test_include_matches_start_of_full_table_id(self):
        assert self.scoped(Scope(include=r"analytics:finance\.")) == [
            "analytics:finance.invoices",
            "analytics:finance.orders",
            "analytics:finance.tmp_orders",
        ]

    def test_include_is_anchored_at_the_start(self):
        assert self.scoped(Scope(include=r"finance\.orders")) == []

    def test_include_matches_mcon(self):
        assert self.scoped(Scope(include=r"MCON\+\+acct\+\+.*\+\+table\+\+raw:")) == [
            "raw:finance.orders"
        ]

    def test_exclude_removes_matches_on_full_table_id(self):
        scope = Scope(include=r"analytics:finance\.", exclude=r"analytics:finance\.tmp_")
        assert self.scoped(scope) == ["analytics:finance.invoices", "analytics:finance.orders"]

    def test_exclude_removes_matches_on_mcon(self):
        scope = Scope(include=r"analytics:", exclude=r"MCON.*marketing")
        assert "analytics:marketing.campaigns" not in self.scoped(scope)
        assert "analytics:finance.orders" in self.scoped(scope)

    def test_key_assets_only_with_pattern(self):
        scope = Scope(include=r"analytics:finance\.", key_assets_only=True)
        assert self.scoped(scope) == ["analytics:finance.orders", "analytics:finance.tmp_orders"]

    def test_schemas_limit_reads_to_those_schemas(self):
        client = FakeClient([], self.TABLES)
        scope = Scope(include=r".*", schemas=(("analytics", "finance"), ("raw", "finance")))

        assert self.scoped(scope, client) == [
            "analytics:finance.invoices",
            "analytics:finance.orders",
            "analytics:finance.tmp_orders",
            "raw:finance.orders",
        ]
        assert {tuple(r["schema"]) for r in client.table_reads} == {
            ("analytics", "finance"),
            ("raw", "finance"),
        }

    def test_schema_with_no_tables_skips_rather_than_dropping_its_tables(self):
        # A mis-cased pair reads nothing; the other pair still matches, so without this check
        # the sync would silently remove the first schema's tables.
        scope = Scope(include=r".*", schemas=(("analytics", "Finance"), ("raw", "finance")))

        with pytest.raises(sut.SkipMonitor, match="no live tables in analytics:Finance"):
            self.scoped(scope)

    def test_schema_with_no_key_assets_skips(self):
        scope = Scope(include=r".*", key_assets_only=True, schemas=(("raw", "finance"),))

        with pytest.raises(sut.SkipMonitor, match="no live key assets in raw:finance"):
            self.scoped(scope)

    def test_reads_are_cached_across_monitors_on_the_same_warehouse(self):
        client = FakeClient([], self.TABLES)
        cache: sut.TableCache = {}
        schema = (("analytics", "finance"),)

        sut.scoped_tables(client, DW, Scope(include="a", schemas=schema), cache)
        reads = len(client.table_reads)
        sut.scoped_tables(client, DW, Scope(include="b", schemas=schema), cache)

        assert len(client.table_reads) == reads

    def test_table_without_mcon_matches_on_full_table_id(self):
        row = table("analytics:finance.orders")
        row["mcon"] = None
        assert self.scoped(Scope(include="analytics:"), FakeClient([], [row])) == [
            "analytics:finance.orders"
        ]


# ── command line and config ───────────────────────────────────────────────────


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def run_main(main, argv: list[str], client: FakeClient) -> None:
    with patch.object(sut, "Client", return_value=client), patch.object(sut, "Session"):
        main(argv)


class TestKeyAssetScript:
    def test_names_from_config_and_command_line(self, tmp_path):
        config = write(tmp_path, 'monitors:\n  - "A"\n  - "B"\n')
        args = Namespace(config=config, monitor_name=["C", "A"])

        scopes = sync_key_asset_tables.key_asset_scopes(args)

        assert scopes == {"A": KEY_ASSETS, "B": KEY_ASSETS, "C": KEY_ASSETS}

    def test_config_must_list_names(self, tmp_path):
        config = write(tmp_path, "monitors:\n  - name: A\n")
        with pytest.raises(sut.ConfigError):
            sync_key_asset_tables.key_asset_scopes(Namespace(config=config, monitor_name=None))

    def test_requires_a_monitor(self, capsys):
        with pytest.raises(SystemExit) as exit:
            sync_key_asset_tables.main([])
        assert exit.value.code == 2
        assert "no monitors to update" in capsys.readouterr().err

    def test_example_config_parses(self):
        args = Namespace(config=HERE / "key_asset_monitors.example.yaml", monitor_name=None)
        assert len(sync_key_asset_tables.key_asset_scopes(args)) == 2

    def test_main_updates_monitor(self):
        client = FakeClient([job_monitor("m1", "A", [])], [table("db:a.t1", True)])

        run_main(sync_key_asset_tables.main, ["--monitor-name", "A", "--dry-run"], client)

        (update,) = client.updates
        assert table_ids(update) == ["db:a.t1"]
        assert update["dryRun"] is True

    def test_main_exits_nonzero_when_a_monitor_is_left_unchanged(self):
        client = FakeClient([], [table("db:a.t1", True)])

        with pytest.raises(SystemExit) as exit:
            run_main(sync_key_asset_tables.main, ["--monitor-name", "A"], client)

        assert exit.value.code == "1 monitor(s) left unchanged: A."

    def test_main_uses_profile(self):
        client = FakeClient([job_monitor("m1", "A", ["db:a.t1"])], [table("db:a.t1", True)])
        with (
            patch.object(sut, "Client", return_value=client),
            patch.object(sut, "Session") as session,
        ):
            sync_key_asset_tables.main(["--monitor-name", "A", "--profile", "dev"])
        session.assert_called_once_with(mcd_profile="dev")


class TestPatternScript:
    CONFIG = """
monitors:
  - name: Finance
    include: '^analytics:finance\\.'
    exclude: '^analytics:finance\\.tmp_'
    key_assets_only: true
    schemas:
      - database: analytics
        schema: finance
  - name: Everything
    include: '.*'
"""

    def scopes(self, tmp_path: Path, text: str, names: list[str] | None = None):
        args = Namespace(config=write(tmp_path, text), monitor_name=names)
        return sync_pattern_tables.pattern_scopes(args)

    def test_parses_scopes(self, tmp_path):
        assert self.scopes(tmp_path, self.CONFIG) == {
            "Finance": Scope(
                include=r"^analytics:finance\.",
                exclude=r"^analytics:finance\.tmp_",
                key_assets_only=True,
                schemas=(("analytics", "finance"),),
            ),
            "Everything": Scope(include=".*"),
        }

    def test_json_config_parses(self, tmp_path):
        text = '{"monitors": [{"name": "A", "include": "^db:s\\\\."}]}'
        assert self.scopes(tmp_path, text) == {"A": Scope(include=r"^db:s\.")}

    def test_monitor_name_limits_to_those_monitors(self, tmp_path):
        assert list(self.scopes(tmp_path, self.CONFIG, ["Everything"])) == ["Everything"]

    def test_monitor_name_must_be_in_config(self, tmp_path):
        with pytest.raises(sut.ConfigError, match="not in the config file: Nope"):
            self.scopes(tmp_path, self.CONFIG, ["Nope"])

    @pytest.mark.parametrize(
        "entry, error",
        [
            ("include: 'x'", "needs a 'name'"),
            ("name: A", "'include' must be a regex"),
            ("name: A\n    include: '('", "invalid 'include' regex"),
            ("name: A\n    include: 'x'\n    exclude: '['", "invalid 'exclude' regex"),
            ("name: A\n    include: 'x'\n    key_assets_only: 'yes please'", "true or false"),
            ("name: A\n    include: 'x'\n    schemas: [[db, s]]", "'database' and 'schema'"),
            ("name: A\n    include: 'x'\n    includes: 'y'", "unknown keys includes"),
        ],
    )
    def test_invalid_entries_are_rejected(self, tmp_path, entry, error):
        with pytest.raises(sut.ConfigError, match=error):
            self.scopes(tmp_path, f"monitors:\n  - {entry}\n")

    def test_duplicate_names_are_rejected(self, tmp_path):
        text = "monitors:\n  - {name: A, include: x}\n  - {name: A, include: y}\n"
        with pytest.raises(sut.ConfigError, match="more than once"):
            self.scopes(tmp_path, text)

    def test_missing_config_file_is_rejected(self, tmp_path):
        with pytest.raises(sut.ConfigError, match="cannot read config file"):
            sync_pattern_tables.pattern_scopes(
                Namespace(config=tmp_path / "missing.yaml", monitor_name=None)
            )

    def test_example_config_parses(self):
        args = Namespace(config=HERE / "pattern_monitors.example.yaml", monitor_name=None)
        assert len(sync_pattern_tables.pattern_scopes(args)) == 3

    def test_invalid_config_exits_before_calling_monte_carlo(self, tmp_path, capsys):
        config = write(tmp_path, "monitors:\n  - name: A\n")
        with patch.object(sut, "Client") as client, pytest.raises(SystemExit) as exit:
            sync_pattern_tables.main(["--config", str(config)])
        assert exit.value.code == 2
        client.assert_not_called()
        assert "'include' must be a regex" in capsys.readouterr().err

    def test_main_updates_monitor(self, tmp_path):
        config = write(tmp_path, self.CONFIG)
        client = FakeClient(
            [job_monitor("m1", "Finance", ["analytics:finance.old"])],
            [
                table("analytics:finance.orders", important=True),
                table("analytics:finance.tmp_orders", important=True),
                table("analytics:finance.invoices"),
                table("analytics:marketing.orders", important=True),
            ],
        )

        run_main(
            sync_pattern_tables.main,
            ["--config", str(config), "--monitor-name", "Finance"],
            client,
        )

        (update,) = client.updates
        assert table_ids(update) == ["analytics:finance.orders"]
        assert client.table_reads == [{"key_assets_only": True, "schema": ("analytics", "finance")}]

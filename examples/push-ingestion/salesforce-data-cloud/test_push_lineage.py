"""
Tests for push_lineage.py — Data 360 DLO->DMO + DMO->CIO lineage push.

Focused on the read-only Data Cloud REST migration (mapping fetch, catalog-driven
DMO enumeration, edge validation) plus the SSRF/parse/push helpers.

Run:  pytest test_push_lineage.py -v
"""
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

# Make the module importable without a real .env
os.environ.setdefault("SF_ORG_URL", "https://test.my.salesforce.com")
os.environ.setdefault("SF_CLIENT_ID", "fake_id")
os.environ.setdefault("SF_CLIENT_SECRET", "fake_secret")
os.environ.setdefault("MCD_INGEST_ID", "fake_ingest_id")
os.environ.setdefault("MCD_INGEST_TOKEN", "fake_ingest_token")
os.environ.setdefault("MCD_ID", "fake_mcd_id")
os.environ.setdefault("MCD_TOKEN", "fake_mcd_token")
os.environ.setdefault("MCD_RESOURCE_UUID", "00000000-0000-0000-0000-000000000001")

import push_lineage as sut


# ── helpers ────────────────────────────────────────────────────────────────────
def _resp(status=200, json_body=None, text="", content_type="application/json"):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.headers = {"content-type": content_type}
    r.text = text
    if json_body is not None:
        r.json.return_value = json_body
    else:
        r.json.side_effect = ValueError("no json")

    def _raise():
        if status >= 400:
            err = requests.exceptions.HTTPError(str(status))
            err.response = r
            raise err

    r.raise_for_status.side_effect = _raise
    return r


def _sf():
    s = sut.SalesforceDataCloudService("https://test.my.salesforce.com", "cid", "secret")
    s._token = "tok"
    return s


def _lineage():
    return sut.SalesforceDataCloudLineageService(
        resource_uuid="00000000-0000-0000-0000-000000000001",
        ingest_key_id="ii", ingest_key_secret="is",
        gql_key_id="gi", gql_key_secret="gs",
    )


# ── _parse_positive_int ──────────────────────────────────────────────────────
class TestParsePositiveInt:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("X_INT", raising=False)
        assert sut._parse_positive_int("X_INT", 7) == 7

    def test_parses_env(self, monkeypatch):
        monkeypatch.setenv("X_INT", "42")
        assert sut._parse_positive_int("X_INT", 7) == 42

    def test_rejects_zero(self, monkeypatch):
        monkeypatch.setenv("X_INT", "0")
        with pytest.raises(SystemExit):
            sut._parse_positive_int("X_INT", 7)

    def test_rejects_non_numeric(self, monkeypatch):
        monkeypatch.setenv("X_INT", "abc")
        with pytest.raises(SystemExit):
            sut._parse_positive_int("X_INT", 7)


# ── _is_retryable ────────────────────────────────────────────────────────────
class TestIsRetryable:
    def test_4xx_not_retryable(self):
        err = requests.exceptions.HTTPError()
        err.response = MagicMock(status_code=403)
        assert sut._is_retryable(err) is False

    def test_5xx_retryable(self):
        err = requests.exceptions.HTTPError()
        err.response = MagicMock(status_code=503)
        assert sut._is_retryable(err) is True

    def test_no_response_retryable(self):
        assert sut._is_retryable(requests.exceptions.ConnectionError()) is True


# ── _safe_snippet ────────────────────────────────────────────────────────────
class TestSafeSnippet:
    def test_redacts_long_token(self):
        tok = "A" * 80
        assert tok not in sut._safe_snippet(f"error: {tok}")
        assert "[…]" in sut._safe_snippet(f"error: {tok}")

    def test_truncates(self):
        # Non-token text (spaces break the token regex) is truncated to max_len.
        assert len(sut._safe_snippet("word " * 100)) == 200

    def test_none_safe(self):
        assert sut._safe_snippet(None) == ""

    def test_short_text_unchanged(self):
        assert sut._safe_snippet("bad request") == "bad request"


# ── _parse_sql_inputs ────────────────────────────────────────────────────────
class TestParseSqlInputs:
    def test_extracts_dmo_and_cio(self):
        sql = "SELECT * FROM Account__dlm JOIN Revenue__cio ON x"
        assert sut._parse_sql_inputs(sql) == {"account__dlm", "revenue__cio"}

    def test_lowercases(self):
        assert sut._parse_sql_inputs("FROM ACCOUNT__DLM") == {"account__dlm"}

    def test_excludes_self_reference(self):
        sql = "SELECT * FROM Account__dlm, Self__cio"
        assert sut._parse_sql_inputs(sql, cio_api_name="Self__cio") == {"account__dlm"}

    def test_ignores_fields_and_non_objects(self):
        # __c fields must not be captured
        assert sut._parse_sql_inputs("SELECT Amount__c FROM Order__dlm") == {"order__dlm"}

    def test_handles_subquery(self):
        sql = "SELECT * FROM (SELECT id FROM Nested__dlm) t"
        assert "nested__dlm" in sut._parse_sql_inputs(sql)

    def test_empty(self):
        assert sut._parse_sql_inputs("SELECT 1") == set()


# ── _asset_type ──────────────────────────────────────────────────────────────
class TestAssetType:
    def test_dlo_is_table(self):
        assert sut._asset_type("Account_Home__dll") == "TABLE"

    def test_dmo_is_view(self):
        assert sut._asset_type("ssot__Account__dlm") == "VIEW"

    def test_cio_is_view(self):
        assert sut._asset_type("Revenue__cio") == "VIEW"


# ── SalesforceDataCloudService.token guard ────────────────────────────────────
class TestTokenGuard:
    def test_raises_before_auth(self):
        s = sut.SalesforceDataCloudService("https://x.my.salesforce.com", "a", "b")
        with pytest.raises(RuntimeError):
            _ = s.token

    def test_invalidate_clears_secret(self):
        s = _sf()
        s.invalidate_token()
        assert s._token == ""
        assert s.client_secret == ""


# ── _fetch_dmo_mappings (no-retry branches) ───────────────────────────────────
class TestFetchDmoMappings:
    def test_404_returns_empty(self):
        s = _sf()
        with patch.object(sut.requests, "get", return_value=_resp(404)):
            assert s._fetch_dmo_mappings("Account__dlm") == []

    def test_200_returns_maps(self):
        s = _sf()
        body = {"objectSourceTargetMaps": [{"sourceEntityDeveloperName": "a__dll"}]}
        with patch.object(sut.requests, "get", return_value=_resp(200, body)):
            assert s._fetch_dmo_mappings("Account__dlm") == body["objectSourceTargetMaps"]

    def test_200_missing_key_returns_empty(self):
        s = _sf()
        with patch.object(sut.requests, "get", return_value=_resp(200, {})):
            assert s._fetch_dmo_mappings("Account__dlm") == []

    def test_200_null_key_returns_empty(self):
        s = _sf()
        with patch.object(sut.requests, "get", return_value=_resp(200, {"objectSourceTargetMaps": None})):
            assert s._fetch_dmo_mappings("Account__dlm") == []


# ── fetch_dlo_dmo_edges (aggregation logic; mapping fetch mocked) ─────────────
class TestFetchDloDmoEdges:
    def test_empty_specs(self):
        s = _sf()
        assert s.fetch_dlo_dmo_edges([]) == []

    def test_filters_and_builds_edges(self):
        s = _sf()
        s._fetch_dmo_mappings = MagicMock(return_value=[
            {"sourceEntityDeveloperName": "acct__dll", "targetEntityDeveloperName": "acct__dlm"},
            {"sourceEntityDeveloperName": "not_a_dll", "targetEntityDeveloperName": "acct__dlm"},  # dropped
            {"sourceEntityDeveloperName": "acct__dll", "targetEntityDeveloperName": "wrong"},        # dropped
        ])
        edges = s.fetch_dlo_dmo_edges([("acct__dlm", "sales")])
        assert edges == [{"source": "acct__dll", "target": "acct__dlm", "data_space": "sales"}]

    def test_dedupes_case_insensitive(self):
        s = _sf()
        s._fetch_dmo_mappings = MagicMock(return_value=[
            {"sourceEntityDeveloperName": "Acct__dll", "targetEntityDeveloperName": "Acct__dlm"},
            {"sourceEntityDeveloperName": "acct__dll", "targetEntityDeveloperName": "acct__dlm"},
        ])
        edges = s.fetch_dlo_dmo_edges([("Acct__dlm", "sales")])
        assert len(edges) == 1

    def test_one_dmo_error_does_not_abort(self):
        s = _sf()

        def _maps(name):
            if name == "bad__dlm":
                raise RuntimeError("boom")
            return [{"sourceEntityDeveloperName": "x__dll", "targetEntityDeveloperName": name}]

        s._fetch_dmo_mappings = MagicMock(side_effect=_maps)
        edges = s.fetch_dlo_dmo_edges([("bad__dlm", "s"), ("good__dlm", "s")])
        assert edges == [{"source": "x__dll", "target": "good__dlm", "data_space": "s"}]

    def test_404_empty_maps_skipped(self):
        s = _sf()
        s._fetch_dmo_mappings = MagicMock(return_value=[])
        assert s.fetch_dlo_dmo_edges([("a__dlm", "s")]) == []


# ── catalogued_dmos ──────────────────────────────────────────────────────────
class TestCataloguedDmos:
    def test_only_dlm_entries(self):
        ls = _lineage()
        catalog = {"a__dlm": "sales", "b__dll": "sales", "c__cio": "sales"}
        assert ls.catalogued_dmos(catalog, "fb") == [("a__dlm", "sales")]

    def test_str_entry_uses_space_list_entry_uses_fallback(self):
        ls = _lineage()
        catalog = {"a__dlm": "sales", "b__dlm": ["sales", "uk"]}
        specs = dict(ls.catalogued_dmos(catalog, "FALLBACK"))
        assert specs["a__dlm"] == "sales"
        assert specs["b__dlm"] == "FALLBACK"


# ── _resolve_catalog ─────────────────────────────────────────────────────────
class TestResolveCatalog:
    def test_missing_returns_none(self):
        assert _lineage()._resolve_catalog({}, "x__dll") is None

    def test_str_entry(self):
        assert _lineage()._resolve_catalog({"x__dll": "sales"}, "X__dll") == "sales"

    def test_list_prefers_match(self):
        ls = _lineage()
        assert ls._resolve_catalog({"x": ["a", "b"]}, "x", preferred_data_space="b") == "b"

    def test_list_no_preferred_returns_none(self):
        # Ambiguous multi-space entry, preferred not among them → unresolvable (skip, don't guess).
        ls = _lineage()
        assert ls._resolve_catalog({"x": ["a", "b"]}, "x") is None


# ── validate_edges ───────────────────────────────────────────────────────────
class TestValidateEdges:
    def test_empty(self):
        assert _lineage().validate_edges([], {"a": "b"}) == []

    def test_both_present_same_space(self):
        ls = _lineage()
        catalog = {"a__dll": "sales", "a__dlm": "sales"}
        edges = [{"source": "a__dll", "target": "a__dlm", "data_space": "sales"}]
        assert ls.validate_edges(edges, catalog) == [
            {"source": "a__dll", "target": "a__dlm", "data_space": "sales"}
        ]

    def test_target_missing_skipped(self):
        ls = _lineage()
        edges = [{"source": "a__dll", "target": "a__dlm", "data_space": "sales"}]
        assert ls.validate_edges(edges, {"a__dll": "sales"}) == []

    def test_source_missing_skipped(self):
        ls = _lineage()
        edges = [{"source": "a__dll", "target": "a__dlm", "data_space": "sales"}]
        assert ls.validate_edges(edges, {"a__dlm": "sales"}) == []

    def test_data_space_mismatch_skipped(self):
        ls = _lineage()
        catalog = {"a__dll": "uk", "a__dlm": "sales"}
        edges = [{"source": "a__dll", "target": "a__dlm", "data_space": "sales"}]
        assert ls.validate_edges(edges, catalog) == []

    def test_ambiguous_multispace_target_skipped(self):
        # DMO catalogued in two spaces; preliminary space matches neither → skip, don't guess.
        ls = _lineage()
        catalog = {"a__dll": "sales", "a__dlm": ["uk", "emea"]}
        edges = [{"source": "a__dll", "target": "a__dlm", "data_space": "sales"}]
        assert ls.validate_edges(edges, catalog) == []

    def test_shared_dlo_resolves_to_dmo_space(self):
        ls = _lineage()
        catalog = {"a__dll": ["sales", "uk"], "a__dlm": "uk"}
        edges = [{"source": "a__dll", "target": "a__dlm", "data_space": "sales"}]
        result = ls.validate_edges(edges, catalog)
        assert result == [{"source": "a__dll", "target": "a__dlm", "data_space": "uk"}]


# ── _fetch_mc_catalog (GraphQL mocked) ───────────────────────────────────────
class TestFetchMcCatalog:
    def _page(self, edges, has_next=False, cursor=None):
        return {"getTables": {"edges": edges, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}}

    def _node(self, ftid, dataset=None):
        return {"node": {"fullTableId": ftid, "dataset": dataset}}

    def test_filters_and_lowercases(self):
        ls = _lineage()
        ls._gql = MagicMock(return_value=self._page([
            self._node("salesforce-data-cloud:default.Account__dlm", "default"),
            self._node("snowflake:db.schema.other", "x"),          # wrong DB → skipped
            self._node("salesforce-data-cloud:nodot", "x"),        # no "." → skipped
        ]))
        catalog = ls._fetch_mc_catalog()
        assert catalog == {"account__dlm": "default"}

    def test_dataset_fallback_to_fulltableid(self):
        ls = _lineage()
        ls._gql = MagicMock(return_value=self._page([
            self._node("salesforce-data-cloud:sales.Order__dlm", None),
        ]))
        assert ls._fetch_mc_catalog() == {"order__dlm": "sales"}

    def test_multi_space_accumulates_list(self):
        ls = _lineage()
        ls._gql = MagicMock(return_value=self._page([
            self._node("salesforce-data-cloud:default.Acct__dll", "default"),
            self._node("salesforce-data-cloud:uk.Acct__dll", "uk"),
        ]))
        assert ls._fetch_mc_catalog() == {"acct__dll": ["default", "uk"]}

    def test_pagination(self):
        ls = _lineage()
        ls._gql = MagicMock(side_effect=[
            self._page([self._node("salesforce-data-cloud:default.A__dlm", "default")], has_next=True, cursor="c1"),
            self._page([self._node("salesforce-data-cloud:default.B__dlm", "default")]),
        ])
        catalog = ls._fetch_mc_catalog()
        assert set(catalog) == {"a__dlm", "b__dlm"}
        assert ls._gql.call_count == 2

    def test_hasnext_but_null_cursor_breaks(self):
        ls = _lineage()
        ls._gql = MagicMock(return_value=self._page(
            [self._node("salesforce-data-cloud:default.A__dlm", "default")], has_next=True, cursor=None))
        catalog = ls._fetch_mc_catalog()
        assert catalog == {"a__dlm": "default"}
        assert ls._gql.call_count == 1  # did not loop forever


# ── _validate_same_origin (SSRF guard) ────────────────────────────────────────
class TestValidateSameOrigin:
    def test_relative_path_prefixed(self):
        s = _sf()
        assert s._validate_same_origin("/services/data/x").startswith(
            "https://test.my.salesforce.com/services/data")

    def test_relative_not_slash_rejected(self):
        with pytest.raises(RuntimeError):
            _sf()._validate_same_origin("services/data/x")

    def test_cross_host_rejected(self):
        with pytest.raises(RuntimeError):
            _sf()._validate_same_origin("https://evil.com/x")

    def test_userinfo_host_rejected(self):
        with pytest.raises(RuntimeError):
            _sf()._validate_same_origin("https://test.my.salesforce.com@evil.com/x")

    def test_non_https_rejected(self):
        with pytest.raises(RuntimeError):
            _sf()._validate_same_origin("http://test.my.salesforce.com/x")

    def test_port_mismatch_rejected(self):
        with pytest.raises(RuntimeError):
            _sf()._validate_same_origin("https://test.my.salesforce.com:8443/x")

    def test_same_origin_ok_and_strips_null_params(self):
        s = _sf()
        out = s._validate_same_origin(
            "https://test.my.salesforce.com/x?definitionType=null&keep=1")
        assert "definitionType" not in out
        assert "keep=1" in out


# ── parse_cio_edges ──────────────────────────────────────────────────────────
class TestParseCioEdges:
    def test_extracts_edges(self):
        s = _sf()
        cios = [{"apiName": "Rev__cio", "dataSpace": "sales",
                 "expression": "SELECT * FROM Account__dlm JOIN Cost__cio"}]
        edges = s.parse_cio_edges(cios)
        assert {"source": "account__dlm", "target": "rev__cio", "data_space": "sales"} in edges
        assert {"source": "cost__cio", "target": "rev__cio", "data_space": "sales"} in edges

    def test_skips_non_cio(self):
        s = _sf()
        assert s.parse_cio_edges([{"apiName": "Account__dlm", "expression": "x"}]) == []

    def test_skips_empty_expression(self):
        s = _sf()
        assert s.parse_cio_edges([{"apiName": "Rev__cio", "expression": ""}]) == []


# ── get_dataspaces ───────────────────────────────────────────────────────────
class TestGetDataspaces:
    def test_returns_spaces(self):
        s = _sf()
        s._fetch_dataspaces_raw = MagicMock(return_value=_resp(200, {
            "records": [{"DataSpaceApiName": "default"}, {"DataSpaceApiName": "uk"}]}))
        assert s.get_dataspaces() == ["default", "uk"]

    def test_403_falls_back(self):
        s = _sf()
        s._fetch_dataspaces_raw = MagicMock(return_value=_resp(403, {}, text="forbidden"))
        assert s.get_dataspaces() == ["default"]

    def test_exception_falls_back(self):
        s = _sf()
        s._fetch_dataspaces_raw = MagicMock(side_effect=requests.exceptions.ConnectionError())
        assert s.get_dataspaces() == ["default"]

    def test_empty_records_falls_back(self):
        s = _sf()
        s._fetch_dataspaces_raw = MagicMock(return_value=_resp(200, {"records": []}))
        assert s.get_dataspaces() == ["default"]


# ── push_edges (IngestionService mocked) ──────────────────────────────────────
class TestPushEdges:
    def _edges(self, n):
        return [{"source": f"s{i}__dll", "target": f"t{i}__dlm", "data_space": "sales"} for i in range(n)]

    def test_builds_lineage_events_and_batches(self, monkeypatch):
        monkeypatch.setattr(sut, "INGEST_BATCH_SIZE", 2)
        with patch.object(sut, "IngestionService") as MIS, \
             patch.object(sut, "Client"), patch.object(sut, "Session"):
            MIS.return_value.extract_invocation_id.side_effect = ["inv-1", "inv-2"]
            ls = _lineage()
            captured = []
            ls._send_batch = MagicMock(side_effect=lambda svc, ev: captured.append(ev) or "resp")
            ids = ls.push_edges(self._edges(3), "run1")
        assert ids == ["inv-1", "inv-2"]           # 3 edges / batch size 2 → 2 batches
        assert len(captured) == 2
        first = captured[0][0]
        # objectType must match the catalog: __dlm target -> VIEW, __dll source -> TABLE
        assert first.destination.type == "VIEW"
        assert first.destination.database == "salesforce-data-cloud"
        assert first.destination.schema == "sales"
        assert first.destination.name == "t0__dlm"
        assert first.sources[0].type == "TABLE"
        assert first.sources[0].name == "s0__dll"

    def test_failure_saves_and_raises(self, monkeypatch):
        monkeypatch.setattr(sut, "INGEST_BATCH_SIZE", 500)
        with patch.object(sut, "IngestionService"), \
             patch.object(sut, "Client"), patch.object(sut, "Session"), \
             patch.object(sut, "_save_failed_edges", return_value="/tmp/failed.json") as save:
            ls = _lineage()
            ls._send_batch = MagicMock(side_effect=RuntimeError("push boom"))
            with pytest.raises(RuntimeError, match="failed to push"):
                ls.push_edges(self._edges(2), "run1")
            save.assert_called_once()

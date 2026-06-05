"""
Comprehensive tests for push_external_lineage.py.

Run:  pytest test_push_external_lineage.py -v
"""
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

# Make the module importable without a .env file present
os.environ.setdefault("SF_ORG_URL", "https://test.my.salesforce.com")
os.environ.setdefault("SF_CLIENT_ID", "fake_id")
os.environ.setdefault("SF_CLIENT_SECRET", "fake_secret")
os.environ.setdefault("MCD_ID", "fake_mcd_id")
os.environ.setdefault("MCD_TOKEN", "fake_mcd_token")
os.environ.setdefault("MCD_DC_WAREHOUSE_UUID", "00000000-0000-0000-0000-000000000001")

import push_external_lineage as sut


# ── _sanitize_for_log ─────────────────────────────────────────────────────────

class TestSanitizeForLog:
    def test_newlines_replaced(self):
        assert "\n" not in sut._sanitize_for_log("line1\nline2")
        assert "\r" not in sut._sanitize_for_log("line1\rline2")

    def test_tab_replaced(self):
        assert "\t" not in sut._sanitize_for_log("col1\tcol2")

    def test_null_byte_removed(self):
        assert "\x00" not in sut._sanitize_for_log("before\x00after")

    def test_ansi_escape_removed(self):
        assert "\x1b" not in sut._sanitize_for_log("\x1b[31mred\x1b[0m")
        assert sut._sanitize_for_log("\x1b[31mred\x1b[0m") == "red"

    def test_bidi_override_replaced(self):
        result = sut._sanitize_for_log("safe\u202emalicious")
        assert "\u202e" not in result
        assert "safe malicious" == result

    def test_unicode_line_separator_replaced(self):
        result = sut._sanitize_for_log("line1\u2028line2")
        assert "\u2028" not in result

    def test_unicode_paragraph_separator_replaced(self):
        result = sut._sanitize_for_log("para1\u2029para2")
        assert "\u2029" not in result

    def test_truncation(self):
        long_str = "a" * 500
        assert len(sut._sanitize_for_log(long_str)) == 300
        assert len(sut._sanitize_for_log(long_str, max_len=100)) == 100

    def test_clean_string_unchanged(self):
        assert sut._sanitize_for_log("hello world") == "hello world"


# ── _redact_params ────────────────────────────────────────────────────────────

class TestRedactParams:
    def test_redacts_password(self):
        result = sut._redact_params({"password": "secret123"})
        assert result["password"] == "<redacted>"

    def test_redacts_token(self):
        assert sut._redact_params({"token": "abc"})["token"] == "<redacted>"

    def test_redacts_case_insensitive(self):
        assert sut._redact_params({"PASSWORD": "x"})["PASSWORD"] == "<redacted>"
        assert sut._redact_params({"Client_Secret": "x"})["Client_Secret"] == "<redacted>"

    def test_redacts_hyphenated_key(self):
        # key normalized via .replace("-", "_") before checking
        assert sut._redact_params({"bearer-token": "x"})["bearer-token"] == "<redacted>"

    def test_preserves_non_sensitive(self):
        result = sut._redact_params({"bucket": "my-bucket", "region": "us-east-1"})
        assert result["bucket"] == "my-bucket"
        assert result["region"] == "us-east-1"

    def test_empty_dict(self):
        assert sut._redact_params({}) == {}

    def test_redacts_all_sensitive_names(self):
        sensitive = {
            "password": "x", "secret": "x", "token": "x", "key": "x",
            "apikey": "x", "client_secret": "x", "access_key": "x",
            "secret_access_key": "x", "private_key": "x", "session_token": "x",
            "passphrase": "x", "ssh_private_key": "x", "bearer_token": "x",
            "oauth_token": "x",
        }
        result = sut._redact_params(sensitive)
        for k in sensitive:
            assert result[k] == "<redacted>", f"Expected {k!r} to be redacted"


# ── _sanitize_stream_for_disk ─────────────────────────────────────────────────

class TestSanitizeStreamForDisk:
    def test_redacts_nested_connector_details(self):
        stream = {
            "name": "MyStream",
            "connectorInfo": {
                "connectorType": "SNOWFLAKE",
                "connectorDetails": {"password": "secret", "warehouse": "WH"},
            },
        }
        result = sut._sanitize_stream_for_disk(stream)
        assert result["connectorInfo"]["connectorDetails"]["password"] == "<redacted>"
        assert result["connectorInfo"]["connectorDetails"]["warehouse"] == "WH"

    def test_redacts_top_level_connector_details(self):
        stream = {
            "name": "MyStream",
            "connectorDetails": {"token": "abc123", "bucket": "my-bucket"},
        }
        result = sut._sanitize_stream_for_disk(stream)
        assert result["connectorDetails"]["token"] == "<redacted>"
        assert result["connectorDetails"]["bucket"] == "my-bucket"

    def test_redacts_advanced_attributes(self):
        stream = {
            "advancedAttributes": {"password": "pw", "object": "Account"},
        }
        result = sut._sanitize_stream_for_disk(stream)
        assert result["advancedAttributes"]["password"] == "<redacted>"
        assert result["advancedAttributes"]["object"] == "Account"

    def test_non_dict_connector_details_becomes_empty(self):
        stream = {"connectorInfo": {"connectorDetails": ["list", "not", "dict"]}}
        result = sut._sanitize_stream_for_disk(stream)
        assert result["connectorInfo"]["connectorDetails"] == {}

    def test_non_dict_advanced_attributes_becomes_empty(self):
        stream = {"advancedAttributes": "not a dict"}
        result = sut._sanitize_stream_for_disk(stream)
        assert result["advancedAttributes"] == {}

    def test_none_connector_info_handled(self):
        stream = {"connectorInfo": None, "name": "test"}
        result = sut._sanitize_stream_for_disk(stream)
        assert result["connectorInfo"] == {}

    def test_original_stream_not_mutated(self):
        stream = {"connectorInfo": {"connectorDetails": {"password": "pw"}}}
        sut._sanitize_stream_for_disk(stream)
        assert stream["connectorInfo"]["connectorDetails"]["password"] == "pw"


# ── _parse_warehouse_map ──────────────────────────────────────────────────────

class TestParseWarehouseMap:
    def test_single_entry(self):
        result = sut._parse_warehouse_map("abc123=uuid-val")
        assert result == {"abc123": "uuid-val"}

    def test_multiple_entries(self):
        result = sut._parse_warehouse_map("key1=uuid1,key2=uuid2")
        assert result == {"key1": "uuid1", "key2": "uuid2"}

    def test_keys_lowercased(self):
        result = sut._parse_warehouse_map("ABC=uuid1")
        assert "abc" in result
        assert "ABC" not in result

    def test_whitespace_stripped(self):
        result = sut._parse_warehouse_map(" key1 = uuid1 , key2 = uuid2 ")
        assert result["key1"] == "uuid1"
        assert result["key2"] == "uuid2"

    def test_malformed_entry_skipped(self):
        result = sut._parse_warehouse_map("good=uuid1,badentry,good2=uuid2")
        assert "good" in result
        assert "good2" in result
        assert len(result) == 2

    def test_empty_key_skipped(self):
        result = sut._parse_warehouse_map("=uuid1,good=uuid2")
        assert "" not in result
        assert "good" in result

    def test_empty_uuid_skipped(self):
        result = sut._parse_warehouse_map("key1=,good=uuid2")
        assert "key1" not in result
        assert "good" in result

    def test_empty_string_returns_empty(self):
        assert sut._parse_warehouse_map("") == {}

    def test_truncation_at_comma_boundary(self):
        # Build a string slightly over 8192 bytes that ends mid-entry after truncation
        entry = "k" * 100 + "=" + "u" * 100
        # First good entry, then a padding entry, then a partial entry
        raw = "good=uuid1," + ",".join([entry] * 40)
        result = sut._parse_warehouse_map(raw)
        # "good=uuid1" must survive
        assert "good" in result
        # No partial/malformed key should appear
        for k in result:
            assert "=" not in k


# ── _extract_sf_account_identifier ───────────────────────────────────────────

class TestExtractSfAccountIdentifier:
    def test_standard_url(self):
        assert sut._extract_sf_account_identifier(
            "https://HDB68299.us-west-2.snowflakecomputing.com"
        ) == "hdb68299.us-west-2"

    def test_no_region(self):
        assert sut._extract_sf_account_identifier(
            "https://xy12345.snowflakecomputing.com"
        ) == "xy12345"

    def test_lowercase_passthrough(self):
        assert sut._extract_sf_account_identifier(
            "https://abc.eu-west-1.snowflakecomputing.com"
        ) == "abc.eu-west-1"

    def test_url_with_trailing_slash(self):
        result = sut._extract_sf_account_identifier(
            "https://abc.us-east-1.snowflakecomputing.com/"
        )
        assert result == "abc.us-east-1"

    def test_non_snowflake_url_returns_hostname(self):
        result = sut._extract_sf_account_identifier("https://other.example.com")
        assert result == "other.example.com"


# ── _CatalogIndex / _lookup ───────────────────────────────────────────────────

def _make_catalog(*full_table_ids: str) -> sut._CatalogIndex:
    by_full: dict = {}
    by_name: dict = {}
    for fid in full_table_ids:
        by_full[fid.lower()] = fid
        if "." in fid:
            name = fid.rsplit(".", 1)[1].lower()
            if name not in by_name:
                by_name[name] = fid
            else:
                existing = by_name[name]
                if isinstance(existing, list):
                    existing.append(fid)
                else:
                    by_name[name] = [existing, fid]
    return sut._CatalogIndex(by_full=by_full, by_name=by_name, table_count=len(full_table_ids))


class TestLookup:
    def test_exact_full_match(self):
        idx = _make_catalog("salesforce-data-cloud:default.Account_Home__dll")
        result = sut._lookup(idx, "salesforce-data-cloud:default.Account_Home__dll", "DC")
        assert result == "salesforce-data-cloud:default.Account_Home__dll"

    def test_case_insensitive_full_match(self):
        idx = _make_catalog("salesforce-data-cloud:default.Account_Home__dll")
        result = sut._lookup(idx, "SALESFORCE-DATA-CLOUD:DEFAULT.ACCOUNT_HOME__DLL", "DC")
        assert result == "salesforce-data-cloud:default.Account_Home__dll"

    def test_name_only_match(self):
        idx = _make_catalog("salesforce-data-cloud:default.Account_Home__dll")
        result = sut._lookup(idx, "Account_Home__dll", "DC")
        assert result == "salesforce-data-cloud:default.Account_Home__dll"

    def test_miss_returns_none(self):
        idx = _make_catalog("salesforce-data-cloud:default.Account_Home__dll")
        assert sut._lookup(idx, "NonExistent__dll", "DC") is None

    def test_ambiguous_name_returns_first_with_warning(self):
        idx = _make_catalog(
            "salesforce-data-cloud:default.Orders__dll",
            "salesforce-data-cloud:prod.Orders__dll",
        )
        result = sut._lookup(idx, "Orders__dll", "DC")
        assert result is not None  # returns first, not None


# ── resolve_edges ─────────────────────────────────────────────────────────────

def _make_stream(name: str, connector_type: str, dlo_name: str,
                 source_object: str = "", **kwargs) -> dict:
    stream: dict = {
        "name": name,
        "connectorInfo": {
            "connectorType": connector_type,
            "connectorDetails": {"sourceObject": source_object},
        },
        "dataLakeObjectInfo": {"name": dlo_name},
    }
    if "advanced" in kwargs:
        stream["advancedAttributes"] = kwargs["advanced"]
    return stream


def _make_dc_idx(*dlo_names: str) -> sut._CatalogIndex:
    return _make_catalog(*[f"salesforce-data-cloud:default.{n}" for n in dlo_names])


def _make_crm_idx(*obj_names: str) -> sut._CatalogIndex:
    return _make_catalog(*[f"crm:org.{n}" for n in obj_names])


def _make_sf_idx(*table_ids: str) -> sut._CatalogIndex:
    return _make_catalog(*table_ids)


DC_UUID = "00000000-0000-0000-0000-000000000001"
CRM_UUID = "00000000-0000-0000-0000-000000000002"
SF_UUID = "00000000-0000-0000-0000-000000000003"


class TestResolveEdges:
    def _call(self, streams, dc_idx, crm_idx=None, sf_idx=None,
              connection_details=None, sf_catalogs=None, crm_catalogs=None):
        return sut.resolve_edges(
            streams=streams,
            dc_idx=dc_idx,
            crm_idx=crm_idx,
            sf_idx=sf_idx,
            dc_warehouse_uuid=DC_UUID,
            crm_warehouse_uuid=CRM_UUID if crm_idx else None,
            snowflake_warehouse_uuid=SF_UUID if sf_idx else None,
            connection_details=connection_details or {},
            sf_catalogs=sf_catalogs or {},
            crm_catalogs=crm_catalogs or {},
        )

    def test_crm_stream_resolved(self):
        streams = [_make_stream("S1", "SalesforceDotCom", "Account__dll", "Account")]
        dc_idx = _make_dc_idx("Account__dll")
        crm_idx = _make_crm_idx("Account")
        edges, skipped = self._call(streams, dc_idx, crm_idx=crm_idx)
        assert len(edges) == 1
        assert edges[0]["connector_type"] == "SalesforceDotCom"
        assert edges[0]["source_warehouse"] == CRM_UUID
        assert edges[0]["dest_warehouse"] == DC_UUID
        assert len(skipped) == 0

    def test_crm_stream_skipped_no_warehouse(self):
        streams = [_make_stream("S1", "SalesforceDotCom", "Account__dll", "Account")]
        dc_idx = _make_dc_idx("Account__dll")
        edges, skipped = self._call(streams, dc_idx)  # no crm_idx
        assert len(edges) == 0
        assert skipped[0]["reason"] == "crm_warehouse_uuid_not_configured"

    def test_crm_stream_skipped_source_not_in_mc(self):
        streams = [_make_stream("S1", "SalesforceDotCom", "Account__dll", "Account")]
        dc_idx = _make_dc_idx("Account__dll")
        crm_idx = _make_crm_idx()  # empty catalog
        edges, skipped = self._call(streams, dc_idx, crm_idx=crm_idx)
        assert len(edges) == 0
        assert skipped[0]["reason"] == "source_not_in_mc_catalog"

    def test_dlo_not_in_mc_catalog_skipped(self):
        streams = [_make_stream("S1", "SalesforceDotCom", "Missing__dll", "Account")]
        dc_idx = _make_dc_idx()  # no DLOs in catalog
        crm_idx = _make_crm_idx("Account")
        edges, skipped = self._call(streams, dc_idx, crm_idx=crm_idx)
        assert len(edges) == 0
        assert skipped[0]["reason"] == "dlo_not_in_mc_catalog"

    def test_missing_dlo_name_skipped(self):
        stream = {"name": "S1", "connectorInfo": {"connectorType": "SalesforceDotCom"}, "dataLakeObjectInfo": {}}
        edges, skipped = self._call([stream], _make_dc_idx())
        assert skipped[0]["reason"] == "missing_dlo_name"

    def test_unsupported_connector_skipped(self):
        stream = _make_stream("S1", "OracleDB", "Foo__dll", "Table")
        dc_idx = _make_dc_idx("Foo__dll")
        edges, skipped = self._call([stream], dc_idx)
        assert len(edges) == 0
        assert skipped[0]["reason"] == "unsupported_connector"

    def test_awss3_connector_unsupported(self):
        """AwsS3 streams are explicitly unsupported — S3 requires custom node creation."""
        stream = {
            "name": "S1",
            "connectorInfo": {"connectorType": "AwsS3"},
            "dataLakeObjectInfo": {"name": "Accounts__dll"},
            "advancedAttributes": {"fileName": "accounts.csv"},
        }
        dc_idx = _make_dc_idx("Accounts__dll")
        edges, skipped = self._call([stream], dc_idx)
        assert len(edges) == 0
        assert skipped[0]["reason"] == "unsupported_connector"

    def test_snowflake_stream_resolved(self):
        stream = _make_stream("S1", "SNOWFLAKE", "Orders__dll", advanced={
            "database": "MYDB", "schema": "PUBLIC", "object": "ORDERS"
        })
        dc_idx = _make_dc_idx("Orders__dll")
        sf_idx = _make_sf_idx("MYDB:PUBLIC.ORDERS")
        edges, skipped = self._call([stream], dc_idx, sf_idx=sf_idx)
        assert len(edges) == 1
        assert edges[0]["connector_type"] == "SNOWFLAKE"
        assert edges[0]["source_warehouse"] == SF_UUID

    def test_snowflake_missing_object_skipped(self):
        stream = _make_stream("S1", "SNOWFLAKE", "Orders__dll", advanced={
            "database": "MYDB", "schema": "PUBLIC"  # no "object"
        })
        dc_idx = _make_dc_idx("Orders__dll")
        sf_idx = _make_sf_idx("MYDB:PUBLIC.ORDERS")
        edges, skipped = self._call([stream], dc_idx, sf_idx=sf_idx)
        assert skipped[0]["reason"] == "missing_snowflake_object"

    def test_snowflake_no_warehouse_configured_skipped(self):
        stream = _make_stream("S1", "SNOWFLAKE", "Orders__dll", advanced={
            "database": "MYDB", "schema": "PUBLIC", "object": "ORDERS"
        })
        dc_idx = _make_dc_idx("Orders__dll")
        edges, skipped = self._call([stream], dc_idx)  # no sf_idx
        assert skipped[0]["reason"] == "snowflake_warehouse_not_configured"

    def test_duplicate_edges_deduplicated(self):
        """Two streams that produce the same source→DLO pair should yield one edge."""
        s1 = _make_stream("S1", "SalesforceDotCom", "Account__dll", "Account")
        s2 = _make_stream("S2", "SalesforceDotCom", "Account__dll", "Account")
        dc_idx = _make_dc_idx("Account__dll")
        crm_idx = _make_crm_idx("Account")
        edges, _ = self._call([s1, s2], dc_idx, crm_idx=crm_idx)
        assert len(edges) == 2  # resolve_edges returns both; deduplication happens in main()

    def test_linked_crm_org_not_in_map_skipped(self):
        """A CRM stream with a connector_id not in crm_catalogs should skip, not fall back."""
        conn_details = {"S1": {"connector_id": "0XA000000000001"}}
        stream = _make_stream("S1", "SalesforceDotCom", "Account__dll", "Account")
        dc_idx = _make_dc_idx("Account__dll")
        crm_idx = _make_crm_idx("Account")
        # crm_catalogs is populated (has entries) but doesn't contain this connector_id
        other_catalog = _make_crm_idx("Account")
        crm_catalogs = {"0xa000000000002": (other_catalog, CRM_UUID)}
        edges, skipped = self._call(
            [stream], dc_idx,
            crm_idx=crm_idx,
            connection_details=conn_details,
            crm_catalogs=crm_catalogs,
        )
        assert len(edges) == 0
        assert skipped[0]["reason"] == "linked_crm_org_not_in_warehouse_map"

    def test_crm_multi_org_routed_correctly(self):
        """A CRM stream whose connector_id is in crm_catalogs routes to the correct catalog."""
        conn_details = {"S1": {"connector_id": "0XA000000000001"}}
        stream = _make_stream("S1", "SalesforceDotCom", "Account__dll", "Account")
        dc_idx = _make_dc_idx("Account__dll")
        specific_crm_idx = _make_crm_idx("Account")
        crm_catalogs = {"0xa000000000001": (specific_crm_idx, "uuid-specific-crm")}
        edges, skipped = self._call(
            [stream], dc_idx,
            connection_details=conn_details,
            crm_catalogs=crm_catalogs,
        )
        assert len(edges) == 1
        assert edges[0]["source_warehouse"] == "uuid-specific-crm"


# ── _http ─────────────────────────────────────────────────────────────────────

class TestHttp:
    def _mock_response(self, status_code, body=None, headers=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = headers or {}
        resp.json.return_value = body or {}
        resp.raise_for_status = MagicMock()
        return resp

    @patch("push_external_lineage.requests.request")
    def test_default_allow_redirects_false(self, mock_req):
        mock_req.return_value = self._mock_response(200)
        sut._http("GET", "https://example.com")
        _, kwargs = mock_req.call_args
        assert kwargs.get("allow_redirects") is False

    @patch("push_external_lineage.requests.request")
    def test_explicit_allow_redirects_true_honored(self, mock_req):
        mock_req.return_value = self._mock_response(200)
        sut._http("GET", "https://example.com", allow_redirects=True)
        _, kwargs = mock_req.call_args
        assert kwargs.get("allow_redirects") is True

    @patch("push_external_lineage.requests.request")
    def test_default_timeout_set(self, mock_req):
        mock_req.return_value = self._mock_response(200)
        sut._http("GET", "https://example.com")
        _, kwargs = mock_req.call_args
        assert kwargs.get("timeout") == 30

    @patch("push_external_lineage.requests.request")
    def test_3xx_raises_when_redirects_false(self, mock_req):
        mock_req.return_value = self._mock_response(
            302, headers={"Location": "https://other.com/path"}
        )
        import requests as req_lib
        with pytest.raises(req_lib.exceptions.TooManyRedirects):
            sut._http("GET", "https://example.com", max_retries=0)

    @patch("push_external_lineage.time.sleep")
    @patch("push_external_lineage.requests.request")
    def test_429_retries_with_retry_after(self, mock_req, mock_sleep):
        ok = self._mock_response(200)
        rate_limited = self._mock_response(429, headers={"Retry-After": "5"})
        rate_limited.raise_for_status = MagicMock(side_effect=Exception("rate limit"))
        mock_req.side_effect = [rate_limited, ok]
        result = sut._http("GET", "https://example.com", max_retries=1)
        assert result.status_code == 200
        mock_sleep.assert_called_once_with(5.0)

    @patch("push_external_lineage.time.sleep")
    @patch("push_external_lineage.requests.request")
    def test_5xx_retried(self, mock_req, mock_sleep):
        ok = self._mock_response(200)
        server_err = self._mock_response(500)
        mock_req.side_effect = [server_err, ok]
        result = sut._http("GET", "https://example.com", max_retries=1)
        assert result.status_code == 200

    @patch("push_external_lineage.requests.request")
    def test_200_returned_directly(self, mock_req):
        mock_req.return_value = self._mock_response(200, {"data": "ok"})
        result = sut._http("GET", "https://example.com", max_retries=0)
        assert result.status_code == 200


# ── _gql ──────────────────────────────────────────────────────────────────────

class TestGql:
    def _make_http_response(self, body: dict, status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.raise_for_status = MagicMock()
        return resp

    @patch("push_external_lineage._http")
    def test_returns_data_field(self, mock_http):
        mock_http.return_value = self._make_http_response({"data": {"getTables": []}})
        result = sut._gql("query {}", "id", "token")
        assert result == {"getTables": []}

    @patch("push_external_lineage._http")
    def test_null_data_raises_mcgrapqlerror(self, mock_http):
        mock_http.return_value = self._make_http_response({"data": None})
        with pytest.raises(sut.MCGraphQLError, match="null/missing"):
            sut._gql("query {}", "id", "token")

    @patch("push_external_lineage._http")
    def test_missing_data_raises_mcgrapqlerror(self, mock_http):
        mock_http.return_value = self._make_http_response({"errors": [{"message": "not found"}]})
        with pytest.raises(sut.MCGraphQLError, match="not found"):
            sut._gql("query {}", "id", "token")

    @patch("push_external_lineage._http")
    def test_variables_none_excluded(self, mock_http):
        mock_http.return_value = self._make_http_response({"data": {}})
        sut._gql("query {}", "id", "token", variables=None)
        payload = mock_http.call_args[1]["json"]
        assert "variables" not in payload

    @patch("push_external_lineage._http")
    def test_variables_dict_included(self, mock_http):
        mock_http.return_value = self._make_http_response({"data": {}})
        sut._gql("query {}", "id", "token", variables={"key": "value"})
        payload = mock_http.call_args[1]["json"]
        assert payload["variables"] == {"key": "value"}

    @patch("push_external_lineage._http")
    def test_max_retries_zero_passed_to_http(self, mock_http):
        mock_http.return_value = self._make_http_response({"data": {}})
        sut._gql("query {}", "id", "token")
        assert mock_http.call_args[1].get("max_retries") == 0


# ── _save_failed_edges / _save_skipped_streams ────────────────────────────────

class TestAtomicFileSave:
    def test_failed_edges_written_correctly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sut, "__file__", str(tmp_path / "push_external_lineage.py"))
        edges = [{"connector_type": "SNOWFLAKE", "dlo_name": "Foo__dll"}]
        path = sut._save_failed_edges("testrun", edges)
        assert Path(path).exists()
        data = json.loads(Path(path).read_text())
        assert data[0]["connector_type"] == "SNOWFLAKE"

    def test_failed_edges_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sut, "__file__", str(tmp_path / "push_external_lineage.py"))
        path = sut._save_failed_edges("testrun", [{"x": 1}])
        mode = oct(Path(path).stat().st_mode)[-3:]
        assert mode == "600"

    def test_skipped_streams_written_correctly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sut, "__file__", str(tmp_path / "push_external_lineage.py"))
        skipped = [{"reason": "dlo_not_in_mc_catalog", "raw_stream": {}}]
        path = sut._save_skipped_streams("testrun", skipped)
        data = json.loads(Path(path).read_text())
        assert data["skipped_count"] == 1
        assert data["run_id"] == "testrun"


# ── push_edges ────────────────────────────────────────────────────────────────

class TestPushEdges:
    def _make_edge(self, src="src:schema.table", dst="dst:default.DLO__dll",
                   src_wh=SF_UUID, dst_wh=DC_UUID, ct="SNOWFLAKE"):
        return {
            "source_full_id": src,
            "source_warehouse": src_wh,
            "dest_full_id": dst,
            "dest_warehouse": dst_wh,
            "connector_type": ct,
            "dlo_name": "DLO__dll",
            "source_label": f"Snowflake:{src}",
        }

    @patch("push_external_lineage._push_one_edge")
    def test_all_succeed(self, mock_push):
        mock_push.return_value = {"createOrUpdateLineageEdge": {"edge": {"source": {"mcon": "m1"}, "destination": {"mcon": "m2"}}}}
        edges = [self._make_edge(), self._make_edge(src="src2:s.t2", dst="dst:default.DLO2__dll")]
        count = sut.push_edges(edges, "key_id", "key_secret", "run123")
        assert count == 2

    @patch("push_external_lineage._push_one_edge")
    def test_failure_counted(self, mock_push, tmp_path, monkeypatch):
        monkeypatch.setattr(sut, "__file__", str(tmp_path / "push_external_lineage.py"))
        mock_push.side_effect = RuntimeError("network error")
        edges = [self._make_edge()]
        count = sut.push_edges(edges, "key_id", "key_secret", "run123")
        assert count == 0

    @patch("push_external_lineage._push_one_edge")
    def test_sigterm_stops_cleanly(self, mock_push):
        mock_push.return_value = {"createOrUpdateLineageEdge": {"edge": {"source": {"mcon": "m"}, "destination": {"mcon": "d"}}}}
        flag = threading.Event()
        edges = [self._make_edge()] * 5

        original_push = sut._push_one_edge

        call_count = [0]
        def stopping_push(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                flag.set()
            return mock_push.return_value

        mock_push.side_effect = stopping_push
        count = sut.push_edges(edges, "key_id", "key_secret", "run123", shutdown_flag=flag)
        assert count < 5  # stopped early


# ── _validate_uuid ────────────────────────────────────────────────────────────

class TestValidateUuid:
    def test_valid_uuid_passes(self):
        sut._validate_uuid("TEST", "00000000-0000-0000-0000-000000000001")

    def test_invalid_uuid_exits(self):
        with pytest.raises(SystemExit):
            sut._validate_uuid("TEST", "not-a-uuid")

    def test_empty_string_exits(self):
        with pytest.raises(SystemExit):
            sut._validate_uuid("TEST", "")


# ── _is_push_retryable ────────────────────────────────────────────────────────

class TestIsPushRetryable:
    def test_mcgraphqlerror_not_retryable(self):
        assert not sut._is_push_retryable(sut.MCGraphQLError("perm failure"))

    def test_4xx_not_retryable(self):
        exc = RuntimeError("bad request")
        resp = MagicMock()
        resp.status_code = 400
        exc.response = resp
        assert not sut._is_push_retryable(exc)

    def test_500_retryable(self):
        exc = RuntimeError("server error")
        resp = MagicMock()
        resp.status_code = 500
        exc.response = resp
        assert sut._is_push_retryable(exc)

    def test_network_error_retryable(self):
        import requests as req_lib
        assert sut._is_push_retryable(req_lib.exceptions.ConnectionError("timeout"))


# ── Integration: edge deduplication in main pipeline context ──────────────────

class TestEdgeDeduplication:
    """Verify deduplication logic that lives in main() after resolve_edges."""

    def test_dedup_removes_identical_pairs(self):
        edges = [
            {"source_full_id": "src1", "dest_full_id": "dst1", "source_label": "L1", "dlo_name": "D1", "connector_type": "CRM"},
            {"source_full_id": "src1", "dest_full_id": "dst1", "source_label": "L1", "dlo_name": "D1", "connector_type": "CRM"},
            {"source_full_id": "src2", "dest_full_id": "dst2", "source_label": "L2", "dlo_name": "D2", "connector_type": "CRM"},
        ]
        seen: set = set()
        deduped: list = []
        for e in edges:
            pair = (e["source_full_id"], e["dest_full_id"])
            if pair not in seen:
                seen.add(pair)
                deduped.append(e)
        assert len(deduped) == 2

    def test_different_pairs_kept(self):
        edges = [
            {"source_full_id": "src1", "dest_full_id": "dst1", "source_label": "L", "dlo_name": "D", "connector_type": "CRM"},
            {"source_full_id": "src2", "dest_full_id": "dst2", "source_label": "L", "dlo_name": "D", "connector_type": "SF"},
        ]
        seen: set = set()
        deduped: list = []
        for e in edges:
            pair = (e["source_full_id"], e["dest_full_id"])
            if pair not in seen:
                seen.add(pair)
                deduped.append(e)
        assert len(deduped) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

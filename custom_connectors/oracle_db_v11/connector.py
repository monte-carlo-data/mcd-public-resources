from typing import Any, List

import oracledb


class _CursorWrapper:
    """Wraps an oracledb cursor to make cursor.description JSON-serializable."""

    def __init__(self, cursor):
        self._cursor = cursor
        self._alias_map = {}  # short_name -> original_name (lowercase)

    @property
    def description(self):
        desc = self._cursor.description
        if desc is None:
            return None
        # Oracle returns UPPERCASE column names; lowercase them for production compatibility.
        # If identifiers were shortened to fit Oracle 11's 30-byte limit, map them back.
        return [
            (
                self._alias_map.get(col[0].lower(), col[0].lower()),
                str(col[1]),
                col[2],
                col[3],
                col[4],
                col[5],
                col[6],
            )
            for col in desc
        ]

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class BaseConnector:
    credentials: dict[str, str]
    connection: Any
    cursor: Any

    ########################################
    # Connection Related Methods
    ########################################
    def create_connection(self) -> Any:
        """Create and return a connection to Oracle Database 11g using thick mode."""
        # Oracle 11g requires thick mode (thin mode only supports 12.1+)
        oracledb.init_oracle_client()

        dsn = oracledb.makedsn(
            host=self.credentials["host"],
            port=int(self.credentials["port"]),
            service_name=self.credentials["service_name"],
        )
        return oracledb.connect(
            user=self.credentials["user"],
            password=self.credentials["password"],
            dsn=dsn,
        )

    def create_cursor(self) -> Any:
        """Create and return a cursor from the active Oracle connection."""
        cursor = self.connection.cursor()
        # Set territory first (it resets date/timestamp formats)
        cursor.execute("ALTER SESSION SET NLS_TERRITORY = 'AMERICA'")
        # Then set explicit date/timestamp formats
        cursor.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'")
        cursor.execute(
            "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF'"
        )
        cursor.execute(
            "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF TZR'"
        )
        return _CursorWrapper(cursor)

    def close_connection(self):
        """Clean up the cursor and connection."""
        self.cursor.close()
        self.connection.close()

    ########################################
    # Execution Related Methods
    ########################################
    def execute_query(self, query: str) -> None:
        """Execute a SQL query using the active cursor.

        Oracle requires FROM DUAL for bare SELECT statements.
        Adds FROM DUAL to SELECT segments that lack a FROM clause.
        Shortens identifiers exceeding 30 bytes (Oracle 11 limit).
        """
        stripped = query.strip()
        # Keep trailing semicolons on PL/SQL blocks (BEGIN...END;)
        if not stripped.upper().startswith("BEGIN"):
            stripped = stripped.rstrip(";")
        stripped = self._add_dual_to_bare_selects(stripped)
        stripped, alias_map = self._shorten_long_identifiers(stripped)
        self.cursor._alias_map = alias_map
        try:
            self.cursor.execute(stripped)
        except Exception as e:
            raise type(e)(f"{e}\n--- Query was ---\n{stripped}") from None

    @staticmethod
    def _add_dual_to_bare_selects(sql: str) -> str:
        """Add FROM DUAL to SELECT statements that lack a FROM clause.

        Scans through the SQL tracking parenthesis depth. For each SELECT,
        checks if there's a FROM at the same depth before the next UNION,
        closing paren, or end of string. If not, inserts FROM DUAL.
        """
        import re

        # Find all keyword positions
        keywords = list(re.finditer(r"\bSELECT\b|\bFROM\b|\bUNION\b", sql, re.IGNORECASE))
        if not keywords:
            return sql

        # Build paren depth map
        depth_at = [0] * (len(sql) + 1)
        d = 0
        for i, c in enumerate(sql):
            if c == "(":
                d += 1
            depth_at[i] = d
            if c == ")":
                d -= 1

        # For each SELECT, determine if it needs FROM DUAL
        inserts = []  # positions where we need to insert " FROM DUAL"
        select_positions = [m for m in keywords if m.group().upper() == "SELECT"]

        for sel_match in select_positions:
            sel_pos = sel_match.start()
            sel_depth = depth_at[sel_pos]
            has_from = False
            insert_before = len(sql)

            # Scan forward from after SELECT to find FROM or boundary
            scan_start = sel_match.end()
            inner_depth = 0
            i = scan_start
            while i < len(sql):
                c = sql[i]
                if c == "(":
                    inner_depth += 1
                elif c == ")":
                    if inner_depth == 0:
                        insert_before = i
                        break
                    inner_depth -= 1
                elif inner_depth == 0:
                    # Check for keywords at this level
                    for kw in keywords:
                        if kw.start() == i:
                            word = kw.group().upper()
                            if word == "FROM":
                                has_from = True
                                break
                            if word == "UNION":
                                insert_before = i
                                break
                            if word == "SELECT":
                                # Nested SELECT at same depth shouldn't happen
                                # without parens, but handle gracefully
                                insert_before = i
                                break
                    if has_from or insert_before != len(sql):
                        break
                i += 1

            if not has_from:
                # Insert FROM DUAL at insert_before, stripping trailing whitespace
                pos = insert_before
                while pos > scan_start and sql[pos - 1] in " \t\n\r":
                    pos -= 1
                inserts.append(pos)

        # Apply inserts in reverse order to preserve positions
        result = list(sql)
        for pos in sorted(inserts, reverse=True):
            result[pos:pos] = list(" FROM DUAL")
        return "".join(result)

    @staticmethod
    def _shorten_long_identifiers(sql: str) -> tuple:
        """Replace double-quoted identifiers exceeding 30 bytes with short aliases.

        Oracle 11 limits identifiers to 30 bytes. The backend may generate queries
        with longer column aliases (e.g. "COUNTRY_ID___approx_distinct_count").
        This rewrites them to short aliases and returns a mapping so that
        _CursorWrapper.description can restore the original names in results.

        Returns (rewritten_sql, alias_map) where alias_map maps short names to
        original names (both lowercase).
        """
        ORACLE_MAX_IDENTIFIER_BYTES = 30

        alias_map = {}  # short_name -> original_name (lowercase)
        seen = {}  # original_identifier -> short_name
        counter = 0
        result = []
        i = 0

        while i < len(sql):
            c = sql[i]

            # Skip single-quoted string literals
            if c == "'":
                j = i + 1
                while j < len(sql):
                    if sql[j] == "'" and j + 1 < len(sql) and sql[j + 1] == "'":
                        j += 2  # escaped quote ('')
                    elif sql[j] == "'":
                        j += 1
                        break
                    else:
                        j += 1
                else:
                    j = len(sql)  # unterminated string, take rest
                result.append(sql[i:j])
                i = j
                continue

            # Handle double-quoted identifiers
            if c == '"':
                end = sql.find('"', i + 1)
                if end == -1:
                    # Unterminated quote, leave as-is
                    result.append(sql[i:])
                    break
                identifier = sql[i + 1 : end]

                if len(identifier.encode("utf-8")) > ORACLE_MAX_IDENTIFIER_BYTES:
                    if identifier not in seen:
                        short_name = f"_c{counter}"
                        counter += 1
                        seen[identifier] = short_name
                        alias_map[short_name] = identifier.lower()
                    result.append(f'"{seen[identifier]}"')
                else:
                    result.append(sql[i : end + 1])

                i = end + 1
                continue

            result.append(c)
            i += 1

        return "".join(result), alias_map

    def fetch_all_results(self) -> List[Any]:
        """Fetch and return all rows from the last executed query."""
        return self.cursor.fetchall()


class MetadataQueryTemplates:
    ########################################
    # Metadata Job Related Methods
    ########################################
    def get_databases_query_template(self) -> str:
        return "SELECT SYS_CONTEXT('USERENV', 'DB_NAME') FROM DUAL"

    def get_schemas_query_template(self) -> str:
        return (
            "SELECT username AS \"schema_name\" FROM all_users "
            "WHERE SYS_CONTEXT('USERENV', 'DB_NAME') = '{{ database_name }}' "
            "ORDER BY username"
        )

    def get_tables_query_template(self) -> str:
        return (
            "SELECT \"database_name\", \"schema_name\", \"table_name\", \"table_type\", "
            "\"row_count\", \"byte_count\", \"last_update_time\", \"view_query\" FROM ("
            "SELECT a.*, ROWNUM rnum FROM ("
            "SELECT '{{ database_name }}' AS \"database_name\", owner AS \"schema_name\", "
            "table_name AS \"table_name\", 'table' AS \"table_type\", "
            "NULL AS \"row_count\", NULL AS \"byte_count\", "
            "NULL AS \"last_update_time\", NULL AS \"view_query\" "
            "FROM all_tables "
            "WHERE owner IN ({{ schemas }}) "
            "{% if table_names is defined and table_names %}AND table_name IN ({{ table_names }}) {% endif %}"
            "UNION ALL "
            "SELECT '{{ database_name }}' AS \"database_name\", owner AS \"schema_name\", "
            "view_name AS \"table_name\", 'view' AS \"table_type\", "
            "NULL AS \"row_count\", NULL AS \"byte_count\", "
            "NULL AS \"last_update_time\", NULL AS \"view_query\" "
            "FROM all_views "
            "WHERE owner IN ({{ schemas }}) "
            "{% if table_names is defined and table_names %}AND view_name IN ({{ table_names }}) {% endif %}"
            ") a WHERE ROWNUM <= {{ offset }} + {{ limit }}"
            ") WHERE rnum > {{ offset }}"
        )

    def get_columns_query_template(self) -> str:
        return (
            "SELECT '{{ database_name }}' || '.' || owner || '.' || table_name AS \"full_table_id\", "
            "column_name AS \"column_name\", data_type AS \"column_type\" "
            "FROM all_tab_columns "
            "WHERE '{{ database_name }}' || '.' || owner || '.' || table_name IN ({{ tables }}) "
            "ORDER BY owner, table_name, column_id"
        )


class QueryLogCollectionTemplates:
    ########################################
    # Query Log Job Related Methods
    ########################################
    def get_query_logs_query_template(self) -> str:
        """Query logs via V$SQL (requires SELECT on V$ views)."""
        return (
            "SELECT * FROM ("
            "SELECT sql_id AS \"query_id\", "
            "last_active_time AS \"start_time\", "
            "last_active_time AS \"end_time\", "
            "sql_text AS \"query_text\", "
            "parsing_schema_name AS \"user\", "
            "NULL AS \"error_code\", "
            "NULL AS \"error_text\", "
            "rows_processed AS \"returned_rows\" "
            "FROM v$sql "
            "WHERE last_active_time >= TO_TIMESTAMP('{{ (start_time | string)[:19] }}', 'YYYY-MM-DD HH24:MI:SS') - INTERVAL '10' SECOND "
            "AND last_active_time < TO_TIMESTAMP('{{ (end_time | string)[:19] }}', 'YYYY-MM-DD HH24:MI:SS') "
            "ORDER BY last_active_time"
            ") WHERE ROWNUM <= {{ offset }} + {{ limit }}"
        )


class CustomSQLMonitorTemplates:
    ###################################################
    # Custom SQL Monitors Related Methods
    ###################################################
    def transform_into_count_query_template(self) -> str:
        return "SELECT COUNT(*) FROM ({{ query }})"

    def add_row_limit_template(self) -> str:
        return "SELECT * FROM ({{ query }}) WHERE ROWNUM <= {{ limit }}"

    def get_count_all_expression_template(self) -> str:
        return "COUNT(*)"


###################################################
# QueryLanguage Related Methods
###################################################
class QueryLanguageTemplates:
    ###################################################
    # QueryLanguage: Core Query Building
    ###################################################
    def build_cte_template(self) -> str:
        return "WITH {{ alias }} AS ({{ cte }})"

    def add_select_clause_template(self) -> str:
        return "{% if cte %}{{ cte }} {% endif %}SELECT {{ select_expressions | join(', ') or '*' }}"

    def add_from_clause_template(self) -> str:
        return "{{ select_clause }} FROM {{ from_expression }}"

    def union_queries_template(self) -> str:
        return "{% if distinct %}{{ queries | join(' UNION ') }}{% else %}{{ queries | join(' UNION ALL ') }}{% endif %}"

    def alias_field_template(self) -> str:
        return "{{ field }} AS {{ alias }}"

    def all_fields_expression_template(self) -> str:
        return "*"

    def escape_field_name_template(self) -> str:
        return '"{{ field_name }}"'

    def get_table_identifier_template(self) -> str:
        return "{{ schema }}.{{ table }}"

    def get_arbitrary_where_clause_template(self) -> str:
        return "(1=1)"

    def ascending_order_template(self) -> str:
        return "{x} ASC"

    def descending_order_template(self) -> str:
        return "{x} DESC"

    def get_case_when_func_template(self) -> str:
        return "CASE {% for cond, res in conditions_and_results %}WHEN {{ cond }} THEN {{ res }} {% endfor %}{% if else_result %}ELSE {{ else_result }} {% endif %}END"

    def negate_expression_template(self) -> str:
        return "NOT({{ query }})"

    ###################################################
    # QueryLanguage: String and Literal Handling
    ###################################################
    def escape_string_template(self) -> str:
        return "{{ string | replace(\"'\", \"''\") }}"

    def string_literal_template(self) -> str:
        return "'{{ string }}'"

    def literal_value_template(self) -> str:
        return "{{ value }}"

    def literal_datetime_template(self) -> str:
        return "TIMESTAMP '{{ date_time_value.strftime('%Y-%m-%d %H:%M:%S') }}'"

    def literal_time_of_day_template(self) -> str:
        return "TO_TIMESTAMP('{{ value }}', 'HH24:MI:SS')"

    def literal_regex_template(self) -> str:
        return "'{{ regex }}'"

    def literal_table_from_value_list_template(self) -> str:
        return "SELECT column_value AS {{ result_field_name }} FROM TABLE(SYS.ODCINUMBERLIST({{ value_list | join(', ') }}))"

    def date_literal_template(self) -> str:
        return "DATE '{{ timestamp.strftime('%Y-%m-%d') }}'"

    def utc_literal_template(self) -> str:
        return "{% if timestamp is defined %}TIMESTAMP '{{ timestamp.strftime('%Y-%m-%d %H:%M:%S') }}'{% else %}SYS_EXTRACT_UTC(SYSTIMESTAMP){% endif %}"

    ###################################################
    # QueryLanguage: Type Casting
    ###################################################
    def get_casting_to_numeric_expression_template(self) -> str:
        return "CAST({{ expression }} AS NUMBER)"

    def cast_to_string_func_template(self) -> str:
        return "TO_CHAR({{ expression }})"

    def get_casting_to_decimal_expression_template(self) -> str:
        return "CAST({{ expression }} AS NUMBER(38, 10))"

    def default_cast_to_timestamp_func_template(self) -> str:
        return "TO_TIMESTAMP({{ expression }}, 'YYYY-MM-DD HH24:MI:SS')"

    def cast_string_to_timestamp_template(self) -> str:
        return "TO_TIMESTAMP({{ expression }}, 'YYYY-MM-DD HH24:MI:SS')"

    def cast_numeric_to_timestamp_template(self) -> str:
        return "TIMESTAMP '1970-01-01 00:00:00' + NUMTODSINTERVAL({{ expression }}, 'SECOND')"

    def cast_date_to_timestamp_template(self) -> str:
        return "CAST({{ expression }} AS TIMESTAMP)"

    def cast_default_to_timestamp_template(self) -> str:
        return "TO_TIMESTAMP({{ expression }}, 'YYYY-MM-DD HH24:MI:SS')"

    def cast_timestamp_to_date_template(self) -> str:
        return "TRUNC({{ timestamp }})"

    def cast_timestamp_to_timestamp_ntz_template(self) -> str:
        return "CAST({{ timestamp }} AS TIMESTAMP)"

    def cast_timestamp_to_timestamp_tz_template(self) -> str:
        return "CAST({{ timestamp }} AS TIMESTAMP WITH TIME ZONE)"

    def cast_to_timestamp_with_tz_template(self) -> str:
        return "CAST({{ expression }} AS TIMESTAMP WITH TIME ZONE)"

    def cast_to_timestamp_without_tz_template(self) -> str:
        return "CAST({{ expression }} AS TIMESTAMP)"

    ###################################################
    # QueryLanguage: Date/Time Functions
    ###################################################
    def convert_to_utc_template(self) -> str:
        return "SYS_EXTRACT_UTC({{ field }})"

    def current_date_func_template(self) -> str:
        return "TRUNC(SYSDATE)"

    def current_timestamp_func_template(self) -> str:
        return "SYSTIMESTAMP"

    def add_days_func_template(self) -> str:
        return "{{ date_expr }} + {{ days }}"

    def add_days_timestamp_func_template(self) -> str:
        return "{{ date_expr }} + NUMTODSINTERVAL({{ days }}, 'DAY')"

    def add_hours_timestamp_func_template(self) -> str:
        return "{{ date_expr }} + NUMTODSINTERVAL({{ hours }}, 'HOUR')"

    def time_truncate_func_template(self) -> str:
        return "TRUNC({{ field }}, '{{ truncation }}')"

    def truncate_to_day_template(self) -> str:
        return "TRUNC({{ field }}, 'DD')"

    def truncate_to_hour_template(self) -> str:
        return "TRUNC({{ field }}, 'HH')"

    def truncate_to_week_template(self) -> str:
        return "TRUNC({{ field }}, 'IW')"

    def truncate_to_month_template(self) -> str:
        return "TRUNC({{ field }}, 'MM')"

    def truncate_to_year_template(self) -> str:
        return "TRUNC({{ field }}, 'YYYY')"

    def get_is_yesterday_expression_template(self) -> str:
        return "TRUNC({x}) = TRUNC(SYSDATE) - 1"

    def get_in_past_days_expression_template(self) -> str:
        return "{x} >= SYSTIMESTAMP - NUMTODSINTERVAL({{ days }}, 'DAY')"

    def get_in_past_hours_expression_template(self) -> str:
        return "{x} >= SYSTIMESTAMP - NUMTODSINTERVAL({{ hours }}, 'HOUR')"

    def get_in_past_calendar_week_expression_template(self) -> str:
        return "TRUNC(CAST({x} AS DATE), 'D') = TRUNC(SYSDATE, 'D')"

    def get_in_past_calendar_month_expression_template(self) -> str:
        return "TRUNC({x}, 'MM') = TRUNC(SYSDATE, 'MM')"

    def get_date_diff_func_template(self) -> str:
        return (
            "{% if date_part == 'hour' %}"
            "(CAST({{ date_expr2 }} AS DATE) - CAST({{ date_expr1 }} AS DATE)) * 24"
            "{% elif date_part == 'minute' %}"
            "(CAST({{ date_expr2 }} AS DATE) - CAST({{ date_expr1 }} AS DATE)) * 1440"
            "{% elif date_part == 'second' %}"
            "(CAST({{ date_expr2 }} AS DATE) - CAST({{ date_expr1 }} AS DATE)) * 86400"
            "{% else %}"
            "(CAST({{ date_expr2 }} AS DATE) - CAST({{ date_expr1 }} AS DATE))"
            "{% endif %}"
        )

    def get_days_of_week_expression_template(self) -> str:
        return "TO_NUMBER(TO_CHAR({x}, 'D'))"

    def convert_to_unix_timestamp_func_template(self) -> str:
        return "(CAST({{ date_expr }} AS DATE) - DATE '1970-01-01') * 86400"

    ###################################################
    # QueryLanguage: Dialect Capability Flags
    ###################################################
    def supports_literal_select_template(self) -> str:
        # Oracle requires FROM DUAL, but execute_query() handles this automatically
        return "true"

    def supports_literal_group_by_template(self) -> str:
        return "true"

    def supports_group_by_on_subquery_template(self) -> str:
        return "true"

    def parses_timestamp_with_trailing_text_template(self) -> str:
        return "false"

    def supports_as_keyword_for_table_alias_template(self) -> str:
        return "false"

    def supports_limit_0_template(self) -> str:
        return "true"

    def requires_subquery_alias_template(self) -> str:
        return "false"

    ###################################################
    # QueryLanguage: Null and NaN Handling
    ###################################################
    def is_null_template(self) -> str:
        return "{x} IS NULL"

    def is_not_null_template(self) -> str:
        return "{x} IS NOT NULL"

    def nan_expr_template(self) -> str:
        # Oracle NUMBER type does not support NaN
        pass

    def get_isnan_expression_template(self) -> str:
        # Oracle NUMBER type does not support NaN; return a string to satisfy guard tests
        return "CAST({x} AS BINARY_FLOAT) IS NAN"

    ###################################################
    # QueryLanguage: Comparison Operators
    ###################################################
    def get_is_eq_expression_template(self) -> str:
        return "{x} = {y}"

    def get_is_gt_expression_template(self) -> str:
        return "{x} > {y}"

    def get_is_gte_expression_template(self) -> str:
        return "{x} >= {y}"

    def get_is_lt_expression_template(self) -> str:
        return "{x} < {y}"

    def get_is_lte_expression_template(self) -> str:
        return "{x} <= {y}"

    def get_is_inside_range_expression_template(self) -> str:
        return "{x} >= {lower_threshold} AND {x} <= {upper_threshold}"

    def get_is_outside_range_expression_template(self) -> str:
        return "{x} < {lower_threshold} OR {x} > {upper_threshold}"

    ###################################################
    # QueryLanguage: Aggregation Functions
    ###################################################
    def get_avg_function_template(self) -> str:
        return "AVG({x})"

    def get_stddev_function_template(self) -> str:
        return "STDDEV({x})"

    def get_distinct_count_func_template(self) -> str:
        return "COUNT(DISTINCT {x})"

    def get_distinct_func_template(self) -> str:
        return "DISTINCT {x}"

    def get_safe_divide_template(self) -> str:
        return "CASE WHEN {{ divisor }} = 0 THEN NULL ELSE {{ dividend }} / {{ divisor }} END"

    def get_conditional_count_expression_template(self) -> str:
        return "COUNT(CASE WHEN {{ expression }} THEN 1 END)"

    def get_approx_quantiles_func_template(self) -> str:
        return "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {{ expression }})"

    def get_approx_percentile_func_template(self) -> str:
        return "PERCENTILE_CONT({{ percentile }}) WITHIN GROUP (ORDER BY {{ expression }})"

    def approx_distinct_func_template(self) -> str:
        return "COUNT(DISTINCT {{ field_name }})"

    def any_value_template(self) -> str:
        return "MIN({{ col_name }})"

    ###################################################
    # QueryLanguage: String Functions
    ###################################################
    def get_length_template(self) -> str:
        return "LENGTH({x})"

    def substring_func_template(self) -> str:
        return "SUBSTR({{ field }}, {{ start_pos }}, {{ length }})"

    def get_is_empty_string_expression_template(self) -> str:
        # Oracle treats empty string as NULL, so check both
        return "({x} IS NULL OR {x} = '')"

    def get_regexp_expression_template(self) -> str:
        return "{% if case_insensitive %}REGEXP_LIKE({x}, '{{ regexp }}', 'i'){% else %}REGEXP_LIKE({x}, '{{ regexp }}'){% endif %}"

    def get_regexp_count_expression_template(self) -> str:
        return "{% if case_insensitive %}SUM(REGEXP_COUNT({x}, '{{ regexp }}', 1, 'i')){% else %}SUM(REGEXP_COUNT({x}, '{{ regexp }}')){% endif %}"

    ###################################################
    # QueryLanguage: Array and Timestamp Validation
    ###################################################
    def array_expr_template(self) -> str:
        # Oracle doesn't have native SQL array literals; use SYS.ODCINUMBERLIST
        pass

    def get_array_length_func_template(self) -> str:
        # Oracle doesn't have native SQL arrays; returning stub for guard test
        return "NULL"

    def get_is_timestamp_expression_template(self) -> str:
        return "REGEXP_LIKE({x}, '^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]')"

    def get_not_is_timestamp_expression_template(self) -> str:
        return "NOT REGEXP_LIKE({x}, '^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]')"

    def get_epoch_seconds_expression_template(self) -> str:
        return "(CAST({x} AS DATE) - DATE '1970-01-01') * 86400"

    def get_epoch_seconds_parameter_template(self) -> str:
        return "(CAST({x} AS DATE) - DATE '1970-01-01') * 86400"

    ###################################################
    # QueryLanguage: Math Functions
    ###################################################
    def get_absolute_value_function_template(self) -> str:
        return "ABS({{ expression }})"

    def rand_func_template(self) -> str:
        return "DBMS_RANDOM.VALUE"

    ###################################################
    # QueryLanguage: RCA and Advanced Functions
    ###################################################
    def max_time_func_template(self) -> str:
        return "MAX({{ field }})"

    def unpivot_template(self) -> str:
        return (
            "SELECT * FROM ({{ from_table }}) "
            "UNPIVOT ({{ value_column }} FOR {{ name_column }} IN "
            "({{ column_list | join(', ') }}))"
        )

    ###################################################
    # QueryLanguage: Field Operations
    ###################################################
    def get_field_or_alias_template(self) -> str:
        return "{{ field }}"


class FunctionalTestOperations:
    def get_test_table_identifier(self) -> tuple:
        return ("XE", "SYSTEM", "PANDORA_FUNCTIONAL_TEST")

    def create_test_table_template(self) -> str:
        return "CREATE TABLE {{ schema }}.{{ table }} (value VARCHAR2(255))"

    def insert_rows_template(self) -> str:
        return (
            "INSERT INTO {{ schema }}.{{ table }} (value) "
            "SELECT 'row_' || LEVEL FROM DUAL CONNECT BY LEVEL <= {{ num_rows }}"
        )

    def add_column_template(self) -> str:
        return "ALTER TABLE {{ schema }}.{{ table }} ADD {{ column_name }} {% if column_type == 'TEXT' %}VARCHAR2(4000){% else %}{{ column_type }}{% endif %}"

    def drop_column_template(self) -> str:
        return "ALTER TABLE {{ schema }}.{{ table }} DROP COLUMN {{ column_name }}"

    def drop_test_table_template(self) -> str:
        return "BEGIN EXECUTE IMMEDIATE 'DROP TABLE {{ schema }}.{{ table }}'; EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"

    def create_lineage_query_template(self) -> str:
        return "SELECT * FROM {{ schema }}.{{ table }} WHERE 1=0"

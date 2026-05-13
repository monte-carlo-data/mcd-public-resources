from typing import Any, List

import pyodbc


class _SerializableCursor:
    """Wraps a pyodbc cursor so that cursor.description is JSON-serializable.

    pyodbc includes Python type objects (e.g. <class 'str'>) in the type_code
    field of cursor.description. The agent framework serializes this to JSON,
    which fails. This wrapper converts type objects to their string names.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        desc = self._cursor.description
        if desc is None:
            return None
        return tuple(
            (col[0], col[1].__name__ if isinstance(col[1], type) else col[1], *col[2:])
            for col in desc
        )

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
        """Create and return a connection to Microsoft Fabric via pyodbc.

        Uses ODBC Driver 18 for SQL Server with Azure AD Service Principal auth.
        """
        server = self.credentials["server"]
        database = self.credentials["database"]
        client_id = self.credentials["client_id"]
        client_secret = self.credentials["client_secret"]
        tenant_id = self.credentials["tenant_id"]

        uid = f"{client_id}@{tenant_id}"

        conn_str = (
            "DRIVER={ODBC Driver 18 for SQL Server}"
            f";SERVER={server}"
            f";DATABASE={database}"
            f";UID={uid}"
            f";PWD={client_secret}"
            ";Authentication=ActiveDirectoryServicePrincipal"
            ";Encrypt=yes"
            ";TrustServerCertificate=no"
        )
        conn = pyodbc.connect(conn_str)
        conn.autocommit = True
        return conn

    def create_cursor(self) -> Any:
        """Create and return a cursor from the active connection."""
        return _SerializableCursor(self.connection.cursor())

    def close_connection(self):
        """Clean up the cursor and connection."""
        self.cursor.close()
        self.connection.close()

    ########################################
    # Execution Related Methods
    ########################################
    def execute_query(self, query: str) -> None:
        """Execute a SQL query using the active cursor."""
        self.cursor.execute(query)

    def fetch_all_results(self) -> List[Any]:
        """Fetch and return all rows from the last executed query."""
        return [tuple(row) for row in self.cursor.fetchall()]


class MetadataQueryTemplates:
    ########################################
    # Metadata Job Related Methods
    ########################################
    def get_databases_query_template(self) -> str:
        return "SELECT DB_NAME() AS database_name"

    def get_schemas_query_template(self) -> str:
        return (
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE CATALOG_NAME = '{{ database_name }}'"
        )

    def get_tables_query_template(self) -> str:
        return (
            "SELECT "
            "TABLE_CATALOG AS database_name, "
            "TABLE_SCHEMA AS schema_name, "
            "TABLE_NAME AS table_name, "
            "CASE TABLE_TYPE "
            "WHEN 'BASE TABLE' THEN 'table' "
            "WHEN 'VIEW' THEN 'view' "
            "ELSE LOWER(TABLE_TYPE) END AS table_type "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_CATALOG = '{{ database_name }}' "
            "AND TABLE_SCHEMA IN ({{ schemas }}) "
            "{% if table_names is defined and table_names %}"
            "AND TABLE_NAME IN ({{ table_names }}) "
            "{% endif %}"
            "ORDER BY TABLE_SCHEMA, TABLE_NAME "
            "OFFSET {{ offset }} ROWS "
            "FETCH NEXT {{ limit }} ROWS ONLY"
        )

    def get_columns_query_template(self) -> str:
        return (
            "SELECT "
            "TABLE_CATALOG + '.' + TABLE_SCHEMA + '.' + TABLE_NAME AS full_table_id, "
            "COLUMN_NAME AS column_name, "
            "DATA_TYPE AS column_type "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_CATALOG = '{{ database_name }}' "
            "AND TABLE_CATALOG + '.' + TABLE_SCHEMA + '.' + TABLE_NAME IN ({{ tables }}) "
            "ORDER BY TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )


class QueryLogCollectionTemplates:
    ########################################
    # Query Log Job Related Methods
    ########################################
    def get_query_logs_query_template(self) -> str:
        return (
            "SELECT "
            "distributed_statement_id AS query_id, "
            "start_time, "
            "end_time, "
            "command AS query_text, "
            "login_name AS [user], "
            "NULL AS error_code, "
            "NULL AS error_text, "
            "row_count AS returned_rows "
            "FROM queryinsights.exec_requests_history "
            "WHERE start_time >= '{{ start_time }}' "
            "AND start_time < '{{ end_time }}' "
            "ORDER BY start_time "
            "OFFSET {{ offset }} ROWS "
            "FETCH NEXT {{ limit }} ROWS ONLY"
        )


class CustomSQLMonitorTemplates:
    ###################################################
    # Custom SQL Monitors Related Methods
    ###################################################
    def transform_into_count_query_template(self) -> str:
        return "SELECT COUNT(*) FROM ({{ query }}) AS count_query"

    def add_row_limit_template(self) -> str:
        # Cannot wrap in a subquery because the inner query may contain a CTE.
        # OFFSET/FETCH requires ORDER BY — use (SELECT NULL) as a no-op sort.
        return "{{ query }} ORDER BY (SELECT NULL) OFFSET 0 ROWS FETCH NEXT {{ limit }} ROWS ONLY"

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
        return "[{{ field_name }}]"

    def get_table_identifier_template(self) -> str:
        # Fabric does not support cross-database (three-part) references.
        # Bracket-escape to handle reserved words (e.g. "geography" is a SQL Server type).
        return "[{{ schema }}].[{{ table }}]"

    def get_arbitrary_where_clause_template(self) -> str:
        return "1=1"

    def ascending_order_template(self) -> str:
        return "{x} ASC"

    def descending_order_template(self) -> str:
        return "{x} DESC"

    def get_case_when_func_template(self) -> str:
        return (
            "CASE {% for cond, res in conditions_and_results %}"
            "WHEN {{ cond }} THEN {{ res }} {% endfor %}"
            "{% if else_result %}ELSE {{ else_result }} {% endif %}END"
        )

    def negate_expression_template(self) -> str:
        return "NOT({{ query | replace('TRUE', '(1=1)') | replace('FALSE', '(1=0)') }})"

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
        return "CAST('{{ date_time_value.strftime('%Y-%m-%d %H:%M:%S') }}' AS DATETIME2)"

    def literal_time_of_day_template(self) -> str:
        return "CAST('{{ value }}' AS TIME)"

    def literal_regex_template(self) -> str:
        return "'{{ regex }}'"

    def literal_table_from_value_list_template(self) -> str:
        return (
            "SELECT {{ result_field_name }} FROM (VALUES "
            "{% for v in value_list %}({{ v }})"
            "{% if not loop.last %}, {% endif %}"
            "{% endfor %}"
            ") AS _values({{ result_field_name }})"
        )

    def date_literal_template(self) -> str:
        return "CAST('{{ timestamp.strftime('%Y-%m-%d') }}' AS DATE)"

    def utc_literal_template(self) -> str:
        return (
            "{% if timestamp is defined %}"
            "CAST('{{ timestamp.strftime('%Y-%m-%d %H:%M:%S') }}' AS DATETIME2)"
            "{% else %}CURRENT_TIMESTAMP{% endif %}"
        )

    ###################################################
    # QueryLanguage: Type Casting
    ###################################################
    def get_casting_to_numeric_expression_template(self) -> str:
        return "CAST({{ expression }} AS FLOAT)"

    def cast_to_string_func_template(self) -> str:
        return "CAST({{ expression }} AS VARCHAR(MAX))"

    def get_casting_to_decimal_expression_template(self) -> str:
        return "CAST({{ expression }} AS DECIMAL(38, 10))"

    def default_cast_to_timestamp_func_template(self) -> str:
        return "CAST({{ expression }} AS DATETIME2)"

    def cast_string_to_timestamp_template(self) -> str:
        return "TRY_CAST({{ expression }} AS DATETIME2)"

    def cast_numeric_to_timestamp_template(self) -> str:
        return "DATEADD(second, CAST({{ expression }} AS BIGINT), '1970-01-01')"

    def cast_date_to_timestamp_template(self) -> str:
        return "CAST({{ expression }} AS DATETIME2)"

    def cast_default_to_timestamp_template(self) -> str:
        return "TRY_CAST({{ expression }} AS DATETIME2)"

    def cast_timestamp_to_date_template(self) -> str:
        return "CAST({{ timestamp }} AS DATE)"

    def cast_timestamp_to_timestamp_ntz_template(self) -> str:
        return "CAST({{ timestamp }} AS DATETIME2)"

    def cast_timestamp_to_timestamp_tz_template(self) -> str:
        return "CAST({{ timestamp }} AS DATETIMEOFFSET)"

    def cast_to_timestamp_with_tz_template(self) -> str:
        return "CAST({{ expression }} AS DATETIMEOFFSET)"

    def cast_to_timestamp_without_tz_template(self) -> str:
        return "CAST({{ expression }} AS DATETIME2)"

    ###################################################
    # QueryLanguage: Date/Time Functions
    ###################################################
    def convert_to_utc_template(self) -> str:
        return "{{ field }} AT TIME ZONE 'UTC'"

    def current_date_func_template(self) -> str:
        return "CAST(GETDATE() AS DATE)"

    def current_timestamp_func_template(self) -> str:
        return "CURRENT_TIMESTAMP"

    def add_days_func_template(self) -> str:
        return "DATEADD(day, {{ days }}, {{ date_expr }})"

    def add_days_timestamp_func_template(self) -> str:
        return "DATEADD(day, {{ days }}, {{ date_expr }})"

    def add_hours_timestamp_func_template(self) -> str:
        return "DATEADD(hour, {{ hours }}, {{ date_expr }})"

    def time_truncate_func_template(self) -> str:
        return "DATETRUNC({{ truncation }}, {{ field }})"

    def truncate_to_day_template(self) -> str:
        return "DATETRUNC(day, {{ field }})"

    def truncate_to_hour_template(self) -> str:
        return "DATETRUNC(hour, {{ field }})"

    def truncate_to_week_template(self) -> str:
        return "DATETRUNC(week, {{ field }})"

    def truncate_to_month_template(self) -> str:
        return "DATETRUNC(month, {{ field }})"

    def truncate_to_year_template(self) -> str:
        return "DATETRUNC(year, {{ field }})"

    def get_is_yesterday_expression_template(self) -> str:
        return "CAST({x} AS DATE) = DATEADD(day, -1, CAST(GETDATE() AS DATE))"

    def get_in_past_days_expression_template(self) -> str:
        return "{x} >= DATEADD(day, -{{ days }}, CURRENT_TIMESTAMP)"

    def get_in_past_hours_expression_template(self) -> str:
        return "{x} >= DATEADD(hour, -{{ hours }}, CURRENT_TIMESTAMP)"

    def get_in_past_calendar_week_expression_template(self) -> str:
        return "DATETRUNC(week, {x}) = DATETRUNC(week, CURRENT_TIMESTAMP)"

    def get_in_past_calendar_month_expression_template(self) -> str:
        return "DATETRUNC(month, {x}) = DATETRUNC(month, CURRENT_TIMESTAMP)"

    def get_date_diff_func_template(self) -> str:
        return "DATEDIFF({{ date_part }}, {{ date_expr1 }}, {{ date_expr2 }})"

    def get_days_of_week_expression_template(self) -> str:
        return "DATEPART(weekday, {x})"

    def convert_to_unix_timestamp_func_template(self) -> str:
        return "DATEDIFF_BIG(second, '1970-01-01', {{ date_expr }})"

    ###################################################
    # QueryLanguage: Dialect Capability Flags
    ###################################################
    def supports_literal_select_template(self) -> str:
        return "true"

    def supports_literal_group_by_template(self) -> str:
        return "false"

    def supports_group_by_on_subquery_template(self) -> str:
        return "false"

    def parses_timestamp_with_trailing_text_template(self) -> str:
        return "false"

    def supports_as_keyword_for_table_alias_template(self) -> str:
        return "true"

    def supports_limit_0_template(self) -> str:
        return "true"

    def requires_subquery_alias_template(self) -> str:
        return "true"

    ###################################################
    # QueryLanguage: Null and NaN Handling
    ###################################################
    def is_null_template(self) -> str:
        return "{x} IS NULL"

    def is_not_null_template(self) -> str:
        return "{x} IS NOT NULL"

    def nan_expr_template(self) -> str:
        # Microsoft Fabric does not support CAST('NaN' AS FLOAT).
        pass

    def get_isnan_expression_template(self) -> str:
        return "{x} <> {x}"

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
        return "STDEV({x})"

    def get_distinct_count_func_template(self) -> str:
        return "COUNT(DISTINCT {x})"

    def get_distinct_func_template(self) -> str:
        return "DISTINCT {x}"

    def get_safe_divide_template(self) -> str:
        return "CASE WHEN {{ divisor }} = 0 THEN NULL ELSE {{ dividend }} / {{ divisor }} END"

    def get_conditional_count_expression_template(self) -> str:
        return "COUNT(CASE WHEN {{ expression }} THEN 1 END)"

    def get_approx_quantiles_func_template(self) -> str:
        return "APPROX_PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {{ expression }})"

    def get_approx_percentile_func_template(self) -> str:
        return "APPROX_PERCENTILE_CONT({{ percentile }}) WITHIN GROUP (ORDER BY {{ expression }})"

    def approx_distinct_func_template(self) -> str:
        return "APPROX_COUNT_DISTINCT({{ field_name }})"

    def any_value_template(self) -> str:
        return "MIN({{ col_name }})"

    ###################################################
    # QueryLanguage: String Functions
    ###################################################
    def get_length_template(self) -> str:
        return "LEN({x})"

    def substring_func_template(self) -> str:
        return "SUBSTRING({{ field }}, {{ start_pos }}, {{ length }})"

    def get_is_empty_string_expression_template(self) -> str:
        return "{x} = ''"

    def get_regexp_expression_template(self) -> str:
        # PATINDEX only supports SQL Server wildcard patterns (%, _, []),
        # not full regular expressions. Leave unimplemented.
        pass

    def get_regexp_count_expression_template(self) -> str:
        pass

    ###################################################
    # QueryLanguage: Array and Timestamp Validation
    ###################################################
    def array_expr_template(self) -> str:
        pass

    def get_array_length_func_template(self) -> str:
        # SQL Server does not have native array types.
        pass

    def get_is_timestamp_expression_template(self) -> str:
        return "TRY_CAST({x} AS DATETIME2) IS NOT NULL"

    def get_not_is_timestamp_expression_template(self) -> str:
        return "TRY_CAST({x} AS DATETIME2) IS NULL"

    def get_epoch_seconds_expression_template(self) -> str:
        return "DATEDIFF_BIG(second, '1970-01-01', {x})"

    def get_epoch_seconds_parameter_template(self) -> str:
        return "DATEDIFF_BIG(second, '1970-01-01', {x})"

    ###################################################
    # QueryLanguage: Math Functions
    ###################################################
    def get_absolute_value_function_template(self) -> str:
        return "ABS({{ expression }})"

    def rand_func_template(self) -> str:
        return "ABS(CHECKSUM(NEWID())) * 1.0 / 2147483647"

    ###################################################
    # QueryLanguage: RCA and Advanced Functions
    ###################################################
    def max_time_func_template(self) -> str:
        return "MAX({{ field }})"

    def unpivot_template(self) -> str:
        return (
            "SELECT unpvt.{{ name_column }}, unpvt.{{ value_column }} "
            "FROM ({{ from_table }}) AS src "
            "CROSS APPLY (VALUES "
            "{% for col in column_list %}"
            "(CAST('{{ col }}' AS VARCHAR(128)), CAST(src.{{ col }} AS VARCHAR(MAX)))"
            "{% if not loop.last %}, {% endif %}"
            "{% endfor %}"
            ") AS unpvt({{ name_column }}, {{ value_column }})"
        )

    ###################################################
    # QueryLanguage: Field Operations
    ###################################################
    def get_field_or_alias_template(self) -> str:
        return "{{ field }}"


class FunctionalTestOperations:
    def get_test_table_identifier(self) -> tuple:
        return ("AlexWh", "dbo", "pandora_functional_test")

    def create_test_table_template(self) -> str:
        return "CREATE TABLE {{ schema }}.{{ table }} (id BIGINT IDENTITY, value VARCHAR(255))"

    def insert_rows_template(self) -> str:
        return (
            "WITH nums AS ("
            "SELECT 1 AS n "
            "UNION ALL "
            "SELECT n + 1 FROM nums WHERE n < {{ num_rows }}"
            ") "
            "INSERT INTO {{ schema }}.{{ table }} (value) "
            "SELECT 'row_' + CAST(n AS VARCHAR) FROM nums "
            "OPTION (MAXRECURSION 0)"
        )

    def add_column_template(self) -> str:
        return "ALTER TABLE {{ schema }}.{{ table }} ADD {{ column_name }} {{ column_type }}"

    def drop_column_template(self) -> str:
        return "ALTER TABLE {{ schema }}.{{ table }} DROP COLUMN {{ column_name }}"

    def drop_test_table_template(self) -> str:
        return "DROP TABLE IF EXISTS {{ schema }}.{{ table }}"

    def create_lineage_query_template(self) -> str:
        return "SELECT * FROM {{ schema }}.{{ table }} WHERE 1=0"

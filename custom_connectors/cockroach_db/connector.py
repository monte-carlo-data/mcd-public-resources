from typing import Any, List

import psycopg2


class _SerializableCursor:
    """Wraps a psycopg2 cursor so that cursor.description is JSON-serializable.

    psycopg2 returns Column namedtuples in cursor.description which are not
    JSON-serializable. This wrapper converts them to plain tuples.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        desc = self._cursor.description
        if desc is None:
            return None
        return tuple(tuple(col) for col in desc)

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
        """Create and return a connection to CockroachDB via psycopg2."""
        conn = psycopg2.connect(
            host=self.credentials["host"],
            port=self.credentials["port"],
            dbname=self.credentials["database"],
            user=self.credentials["user"],
            password=self.credentials["password"],
            sslmode=self.credentials.get("sslmode", "require"),
        )
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
        return self.cursor.fetchall()


class MetadataQueryTemplates:
    ########################################
    # Metadata Job Related Methods
    ########################################
    def get_databases_query_template(self) -> str:
        return "SELECT current_database()"

    def get_schemas_query_template(self) -> str:
        return "SELECT schema_name FROM information_schema.schemata WHERE catalog_name = '{{ database_name }}'"

    def get_tables_query_template(self) -> str:
        return (
            "SELECT table_catalog AS database_name, table_schema AS schema_name, "
            "table_name AS table_name, "
            "CASE WHEN table_type = 'BASE TABLE' THEN 'table' "
            "WHEN table_type IN ('VIEW', 'SYSTEM VIEW') THEN 'view' "
            "ELSE LOWER(table_type) END AS table_type "
            "FROM information_schema.tables "
            "WHERE table_catalog = '{{ database_name }}' "
            "AND table_schema IN ({{ schemas }}) "
            "{% if table_names is defined and table_names %}AND table_name IN ({{ table_names }}) {% endif %}"
            "LIMIT {{ limit }} OFFSET {{ offset }}"
        )

    def get_columns_query_template(self) -> str:
        return (
            "SELECT table_catalog || '.' || table_schema || '.' || table_name AS full_table_id, "
            "column_name, data_type AS column_type "
            "FROM information_schema.columns "
            "WHERE table_catalog = '{{ database_name }}' "
            "AND table_catalog || '.' || table_schema || '.' || table_name IN ({{ tables }})"
        )


class QueryLogCollectionTemplates:
    ########################################
    # Query Log Job Related Methods
    ########################################
    def get_query_logs_query_template(self) -> str:
        """Query logs via crdb_internal.statement_statistics (requires VIEWACTIVITY)."""
        return (
            "SELECT fingerprint_id::TEXT AS query_id, "
            "aggregated_ts AS start_time, "
            "aggregated_ts AS end_time, "
            "metadata->>'query' AS query_text, "
            "app_name AS \"user\", "
            "NULL AS error_code, "
            "NULL AS error_text, "
            "NULL AS returned_rows "
            "FROM crdb_internal.statement_statistics "
            "WHERE aggregated_ts >= '{{ start_time }}' "
            "AND aggregated_ts < '{{ end_time }}' "
            "ORDER BY aggregated_ts "
            "LIMIT {{ limit }} OFFSET {{ offset }}"
        )


class CustomSQLMonitorTemplates:
    ###################################################
    # Custom SQL Monitors Related Methods
    ###################################################
    def transform_into_count_query_template(self) -> str:
        return "SELECT COUNT(*) FROM ({{ query }}) AS count_query"

    def add_row_limit_template(self) -> str:
        return "{{ query }} LIMIT {{ limit }}"

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
        return "{{ database }}.{{ schema }}.{{ table }}"

    def get_arbitrary_where_clause_template(self) -> str:
        return "TRUE"

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
        return "TIME '{{ value }}'"

    def literal_regex_template(self) -> str:
        return "'{{ regex }}'"

    def literal_table_from_value_list_template(self) -> str:
        return "SELECT unnest(ARRAY[{{ value_list | join(', ') }}]) AS {{ result_field_name }}"

    def date_literal_template(self) -> str:
        return "DATE '{{ timestamp.strftime('%Y-%m-%d') }}'"

    def utc_literal_template(self) -> str:
        return "{% if timestamp is defined %}TIMESTAMP WITH TIME ZONE '{{ timestamp.strftime('%Y-%m-%d %H:%M:%S') }}+00'{% else %}CURRENT_TIMESTAMP{% endif %}"

    ###################################################
    # QueryLanguage: Type Casting
    ###################################################
    def get_casting_to_numeric_expression_template(self) -> str:
        return "CAST({{ expression }} AS NUMERIC)"

    def cast_to_string_func_template(self) -> str:
        return "CAST({{ expression }} AS TEXT)"

    def get_casting_to_decimal_expression_template(self) -> str:
        return "CAST({{ expression }} AS DECIMAL(38, 10))"

    def default_cast_to_timestamp_func_template(self) -> str:
        return "CAST({{ expression }} AS TIMESTAMP)"

    def cast_string_to_timestamp_template(self) -> str:
        return "{{ expression }}::TIMESTAMP"

    def cast_numeric_to_timestamp_template(self) -> str:
        return "TO_TIMESTAMP({{ expression }})"

    def cast_date_to_timestamp_template(self) -> str:
        return "{{ expression }}::TIMESTAMP"

    def cast_default_to_timestamp_template(self) -> str:
        return "CAST({{ expression }} AS TIMESTAMP)"

    def cast_timestamp_to_date_template(self) -> str:
        return "{{ timestamp }}::DATE"

    def cast_timestamp_to_timestamp_ntz_template(self) -> str:
        return "{{ timestamp }}::TIMESTAMP WITHOUT TIME ZONE"

    def cast_timestamp_to_timestamp_tz_template(self) -> str:
        return "{{ timestamp }}::TIMESTAMP WITH TIME ZONE"

    def cast_to_timestamp_with_tz_template(self) -> str:
        return "{{ expression }}::TIMESTAMP WITH TIME ZONE"

    def cast_to_timestamp_without_tz_template(self) -> str:
        return "{{ expression }}::TIMESTAMP WITHOUT TIME ZONE"

    ###################################################
    # QueryLanguage: Date/Time Functions
    ###################################################
    def convert_to_utc_template(self) -> str:
        return "{{ field }} AT TIME ZONE 'UTC'"

    def current_date_func_template(self) -> str:
        return "CURRENT_DATE"

    def current_timestamp_func_template(self) -> str:
        return "NOW()"

    def add_days_func_template(self) -> str:
        return "{{ date_expr }} + INTERVAL '{{ days }} days'"

    def add_days_timestamp_func_template(self) -> str:
        return "{{ date_expr }} + INTERVAL '{{ days }} days'"

    def add_hours_timestamp_func_template(self) -> str:
        return "{{ date_expr }} + INTERVAL '{{ hours }} hours'"

    def time_truncate_func_template(self) -> str:
        return "DATE_TRUNC('{{ truncation }}', {{ field }})"

    def truncate_to_day_template(self) -> str:
        return "DATE_TRUNC('day', {{ field }})"

    def truncate_to_hour_template(self) -> str:
        return "DATE_TRUNC('hour', {{ field }})"

    def truncate_to_week_template(self) -> str:
        return "DATE_TRUNC('week', {{ field }})"

    def truncate_to_month_template(self) -> str:
        return "DATE_TRUNC('month', {{ field }})"

    def truncate_to_year_template(self) -> str:
        return "DATE_TRUNC('year', {{ field }})"

    def get_is_yesterday_expression_template(self) -> str:
        return "{x}::DATE = CURRENT_DATE - INTERVAL '1 day'"

    def get_in_past_days_expression_template(self) -> str:
        return "{x} >= NOW() - INTERVAL '{{ days }} days'"

    def get_in_past_hours_expression_template(self) -> str:
        return "{x} >= NOW() - INTERVAL '{{ hours }} hours'"

    def get_in_past_calendar_week_expression_template(self) -> str:
        return "DATE_TRUNC('week', {x}) = DATE_TRUNC('week', CURRENT_DATE)"

    def get_in_past_calendar_month_expression_template(self) -> str:
        return "DATE_TRUNC('month', {x}) = DATE_TRUNC('month', CURRENT_DATE)"

    def get_date_diff_func_template(self) -> str:
        return "EXTRACT({{ date_part }} FROM ({{ date_expr2 }} - {{ date_expr1 }}))"

    def get_days_of_week_expression_template(self) -> str:
        return "EXTRACT(DOW FROM {x})"

    def convert_to_unix_timestamp_func_template(self) -> str:
        return "EXTRACT(EPOCH FROM {{ date_expr }})"

    ###################################################
    # QueryLanguage: Dialect Capability Flags
    ###################################################
    def supports_literal_select_template(self) -> str:
        return "true"

    def supports_literal_group_by_template(self) -> str:
        return "false"

    def supports_group_by_on_subquery_template(self) -> str:
        return "true"

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
        return "'NaN'::FLOAT"

    def get_isnan_expression_template(self) -> str:
        return "ISNAN({x}::FLOAT)"

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
        return "STDDEV_SAMP({x})"

    def get_distinct_count_func_template(self) -> str:
        return "COUNT(DISTINCT {x})"

    def get_distinct_func_template(self) -> str:
        return "DISTINCT {x}"

    def get_safe_divide_template(self) -> str:
        return "CASE WHEN {{ divisor }} = 0 THEN NULL ELSE {{ dividend }} / {{ divisor }} END"

    def get_conditional_count_expression_template(self) -> str:
        return "COUNT(CASE WHEN {{ expression }} THEN 1 END)"

    def get_approx_quantiles_func_template(self) -> str:
        return "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {{ expression }}::FLOAT)"

    def get_approx_percentile_func_template(self) -> str:
        return "PERCENTILE_CONT({{ percentile }}) WITHIN GROUP (ORDER BY {{ expression }}::FLOAT)"

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
        return "SUBSTRING({{ field }} FROM {{ start_pos }} FOR {{ length }})"

    def get_is_empty_string_expression_template(self) -> str:
        return "{x} = ''"

    def get_regexp_expression_template(self) -> str:
        return "{% if case_insensitive %}{x} ~* '{{ regexp }}'{% else %}{x} ~ '{{ regexp }}'{% endif %}"

    def get_regexp_count_expression_template(self) -> str:
        return "SUM(CASE WHEN {% if case_insensitive %}{x} ~* '{{ regexp }}'{% else %}{x} ~ '{{ regexp }}'{% endif %} THEN 1 ELSE 0 END)"

    ###################################################
    # QueryLanguage: Array and Timestamp Validation
    ###################################################
    def array_expr_template(self) -> str:
        return "ARRAY[{{ values | join(', ') }}]"

    def get_array_length_func_template(self) -> str:
        return "ARRAY_LENGTH({x}, 1)"

    def get_is_timestamp_expression_template(self) -> str:
        return "{x} ~ '^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]'"

    def get_not_is_timestamp_expression_template(self) -> str:
        return "{x} !~ '^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]'"

    def get_epoch_seconds_expression_template(self) -> str:
        return "EXTRACT(EPOCH FROM {x})"

    def get_epoch_seconds_parameter_template(self) -> str:
        return "EXTRACT(EPOCH FROM {x})"

    ###################################################
    # QueryLanguage: Math Functions
    ###################################################
    def get_absolute_value_function_template(self) -> str:
        return "ABS({{ expression }})"

    def rand_func_template(self) -> str:
        return "RANDOM()"

    ###################################################
    # QueryLanguage: RCA and Advanced Functions
    ###################################################
    def max_time_func_template(self) -> str:
        return "MAX({{ field }})"

    def unpivot_template(self) -> str:
        return (
            "SELECT {{ name_column }}, {{ value_column }} FROM "
            "({{ from_table }}) src CROSS JOIN LATERAL "
            "(VALUES {% for col in column_list %}"
            "(CAST({{ col }} AS TEXT), CAST(src.{{ col }} AS TEXT))"
            "{% if not loop.last %}, {% endif %}"
            "{% endfor %}) AS unpivoted({{ name_column }}, {{ value_column }})"
        )

    ###################################################
    # QueryLanguage: Field Operations
    ###################################################
    def get_field_or_alias_template(self) -> str:
        return "{{ field }}"


class FunctionalTestOperations:
    def get_test_table_identifier(self) -> tuple:
        return ("defaultdb", "public", "pandora_functional_test")

    def create_test_table_template(self) -> str:
        return "CREATE TABLE {{ schema }}.{{ table }} (id SERIAL PRIMARY KEY, value TEXT)"

    def insert_rows_template(self) -> str:
        return "INSERT INTO {{ schema }}.{{ table }} (value) SELECT 'row_' || g FROM generate_series(1, {{ num_rows }}) g"

    def add_column_template(self) -> str:
        return "ALTER TABLE {{ schema }}.{{ table }} ADD COLUMN {{ column_name }} {{ column_type }}"

    def drop_column_template(self) -> str:
        return "ALTER TABLE {{ schema }}.{{ table }} DROP COLUMN {{ column_name }}"

    def drop_test_table_template(self) -> str:
        return "DROP TABLE IF EXISTS {{ schema }}.{{ table }}"

    def create_lineage_query_template(self) -> str:
        return "SELECT * FROM {{ schema }}.{{ table }} WHERE 1=0"

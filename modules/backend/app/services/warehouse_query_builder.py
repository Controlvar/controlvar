"""Build the warehouse analysis statements from a table and a column mapping.

A warehouse analysis reads two of the customer's own tables or views: an
**assignment** source (who saw which variant, and when) and one **metric**
source per metric (what each unit did, and when).  The customer names each
table and maps its columns; this module turns that mapping into SQL.

What goes into a statement
--------------------------
Only values that passed :mod:`modules.backend.app.core.warehouse_identifiers`:
quoted identifiers, allow-listed literals, integers, and UTC timestamps
rendered by us.  There is no free-text SQL anywhere in the input.

Statements
----------
* :func:`build_metric_query` -- one per metric.  It returns one row per
  variant (at most 51, so a wrongly mapped variant column is detected rather
  than returned in full) with the unit count ``n``, the converting units
  ``n_converted``, the grand mean ``k`` and the **centred** sums
  ``sum_d = SUM(y - k)`` and ``sum_d2 = SUM((y - k)^2)``, all in one
  statement.  Centring in the warehouse is what keeps the variance exact
  enough at large offsets; the naive ``SUM(y)``/``SUM(y*y)`` form loses it.
* :func:`build_diagnostics_query` -- one per run, over the assignment source
  only: exposure rows, rows with a NULL unit or variant, units, units seen in
  more than one variant, and distinct variant values.
* :func:`build_assignment_preview_query` and :func:`build_metric_preview_query`
  -- a source preview: counts per variant label, NULL counts and the earliest
  and latest time, as whole epoch seconds.  Aggregates only.

Semantics (the DuckDB fixtures in the module tests pin each one):

* exposures are filtered to the experiment key and ``start <= exposed_at < end``;
* a unit's first exposure is ``MIN(exposed_at)``;
* units seen in more than one variant are excluded (and counted);
* rows with a NULL unit or variant are excluded (and counted);
* ``unit_id`` is cast to a string on both sides of the join;
* an event counts when ``first_exposed_at <= event_at <
  LEAST(first_exposed_at + window, end)``;
* proportion: ``y = 1`` when the unit has at least one counted event;
* mean: ``y`` is the (optionally capped) sum of the unit's counted values,
  NULL values ignored, and 0 for a unit with no event;
* labels are cut to 64 characters in SQL.

Wire format
-----------
Every floating-point result leaves the warehouse as a string our SQL asked
for, with at least 17 significant digits, so nothing depends on how a driver
or an HTTP API renders a FLOAT: Snowflake ``CAST(CAST(x AS DECFLOAT) AS
VARCHAR)``, BigQuery ``FORMAT('%.17g', x)``, Athena ``format('%.17g', x)``.
Amazon Redshift has no such format function: its statement is ``CAST(x AS
VARCHAR)``, and the digits come from the session's ``extra_float_digits``,
which the connector sets before every statement and checks with a probe in
the same batch (:mod:`modules.backend.app.warehouse.redshift`).
Counts are integers.

Dialects
--------
Each dialect's tokens -- quoting, casts, timestamp literal, adding hours,
serialisation -- are written out below for that dialect.  Nothing is
transpiled from another dialect.  The DuckDB token set the tests run the
semantics on is defined in the tests.

Defence in depth
----------------
Before a statement is returned it is parsed again with sqlglot in its own
dialect (:func:`verify_generated_sql`): it must be exactly one ``SELECT``,
contain no data-changing or command node and no function sqlglot does not
recognise, and read exactly the tables we rendered.  If sqlglot cannot be
imported, every statement is refused.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Final, Literal, Optional, Sequence

from modules.backend.app.core.warehouse_identifiers import (
    ATHENA,
    BIGQUERY,
    REDSHIFT,
    SNOWFLAKE,
    IdentifierRules,
    SourceFilter,
    WarehouseQueryRefused,
    render_column,
    render_experiment_key,
    render_filters,
    render_float_literal,
    render_table,
    utc_text,
    validate_experiment_key,
    validate_table_parts,
)

try:  # A guarded import that fails closed: see verify_generated_sql.
    import sqlglot
    from sqlglot import exp as sqlglot_exp
except Exception:  # pragma: no cover - exercised by patching both to None
    sqlglot = None  # type: ignore[assignment]
    sqlglot_exp = None  # type: ignore[assignment]

#: More rows than this means the variant column is not a variant column.
MAX_VARIANT_VALUES: Final = 50
VARIANT_ROW_LIMIT: Final = MAX_VARIANT_VALUES + 1
LABEL_MAX_CHARS: Final = 64
MAX_CONVERSION_WINDOW_HOURS: Final = 8760
MAX_CAP_VALUE: Final = 1e15

#: The CTE names the templates define; an unqualified table reference with
#: one of these names is ours, anything else is refused by the re-parse.
METRIC_CTES: Final = frozenset(
    {"exposures", "units", "events", "per_unit", "y", "k", "diag"}
)
DIAGNOSTICS_CTES: Final = frozenset({"exposures", "units"})
ASSIGNMENT_PREVIEW_CTES: Final = frozenset({"exposures", "per_variant", "totals"})
METRIC_PREVIEW_CTES: Final = frozenset({"events"})

MetricType = Literal["proportion", "mean"]


@dataclass(frozen=True)
class SqlDialect:
    """One dialect's tokens.  Every callable takes SQL we already rendered."""

    #: sqlglot's name for the dialect, used to parse the statement again.
    name: str
    identifiers: IdentifierRules
    string_type: str
    double_type: str
    #: UTC text (``YYYY-MM-DD HH:MM:SS``) -> a literal that carries the offset.
    timestamp_literal: Callable[[str], str]
    #: (expression, hours) -> expression + hours.
    add_hours: Callable[[str, int], str]
    #: A float expression -> a string with at least 17 significant digits.
    serialise: Callable[[str], str]
    #: A mapped time column -> the expression compared with our literals.
    time_column: Callable[[str], str]
    #: An extra SELECT expression returning the session's UTC offset, or None.
    session_offset: Optional[str] = None
    #: A time expression -> whole seconds since 1970-01-01 UTC, an integer.
    #: Used by the previews only; a dialect without it cannot preview.
    epoch_seconds: Optional[Callable[[str], str]] = None


def _identity(expr: str) -> str:
    return expr


#: The values Snowflake's ``TO_CHAR(CURRENT_TIMESTAMP(), 'TZH:TZM')`` returns for a
#: session in UTC.  Snowflake writes a zero offset as ``Z`` (recorded on a real
#: account in run wl-snowflake-20261004T194134Z-155c1c4b); ``+00:00`` is the same
#: offset in the format's numeric spelling.  Every other value is refused as
#: ``timezone_not_utc``, including ``-00:00``, ``+0000``, ``UTC``, padded text and
#: null.
UTC_SESSION_OFFSETS: Final = frozenset({"Z", "+00:00"})


def is_utc_session_offset(value: object) -> bool:
    """True only for one of :data:`UTC_SESSION_OFFSETS`, compared exactly."""
    return isinstance(value, str) and value in UTC_SESSION_OFFSETS


SNOWFLAKE_SQL: Final = SqlDialect(
    name="snowflake",
    identifiers=SNOWFLAKE,
    string_type="VARCHAR",
    double_type="DOUBLE",
    timestamp_literal=lambda t: f"'{t} +00:00'::TIMESTAMP_TZ",
    add_hours=lambda e, h: f"DATEADD(HOUR, {h}, {e})",
    serialise=lambda x: f"CAST(CAST({x} AS DECFLOAT) AS VARCHAR)",
    # TIMESTAMP_NTZ is read in the session time zone, which every request
    # sets to UTC; LTZ and TZ convert exactly.
    time_column=lambda c: f"CAST({c} AS TIMESTAMP_TZ)",
    session_offset="TO_CHAR(CURRENT_TIMESTAMP(), 'TZH:TZM')",
    epoch_seconds=lambda e: f"DATE_PART(EPOCH_SECOND, {e})",
)

BIGQUERY_SQL: Final = SqlDialect(
    name="bigquery",
    identifiers=BIGQUERY,
    string_type="STRING",
    double_type="FLOAT64",
    timestamp_literal=lambda t: f"TIMESTAMP '{t}+00:00'",
    add_hours=lambda e, h: f"TIMESTAMP_ADD({e}, INTERVAL {h} HOUR)",
    serialise=lambda x: f"FORMAT('%.17g', {x})",
    time_column=_identity,
    epoch_seconds=lambda e: f"UNIX_SECONDS({e})",
)

ATHENA_SQL: Final = SqlDialect(
    name="athena",
    identifiers=ATHENA,
    string_type="VARCHAR",
    double_type="DOUBLE",
    timestamp_literal=lambda t: f"TIMESTAMP '{t} UTC'",
    add_hours=lambda e, h: f"date_add('hour', {h}, {e})",
    serialise=lambda x: f"format('%.17g', {x})",
    time_column=_identity,
    epoch_seconds=lambda e: f"CAST(floor(to_unixtime({e})) AS BIGINT)",
)

#: Amazon Redshift, through the Data API (:mod:`modules.backend.app.warehouse.redshift`).
#: The connector runs every statement in a batch that first sets the session's
#: time zone to UTC and ``extra_float_digits`` to 2, then checks both with a
#: probe; so ``serialise`` is a plain cast to text, ``time_column`` reads a
#: ``TIMESTAMP`` column in UTC, and there is no per-row session offset.
REDSHIFT_SQL: Final = SqlDialect(
    name="redshift",
    identifiers=REDSHIFT,
    # VARCHAR with no length is VARCHAR(256) in Redshift, which a longer unit
    # id would not fit; 65535 is the longest VARCHAR it has.
    string_type="VARCHAR(65535)",
    double_type="DOUBLE PRECISION",
    timestamp_literal=lambda t: f"CAST('{t}+00:00' AS TIMESTAMPTZ)",
    add_hours=lambda e, h: f"DATEADD(hour, {h}, {e})",
    serialise=lambda x: f"CAST({x} AS VARCHAR)",
    time_column=lambda c: f"CAST({c} AS TIMESTAMPTZ)",
    epoch_seconds=lambda e: f"CAST(FLOOR(EXTRACT(EPOCH FROM {e})) AS BIGINT)",
)

#: The dialects a connection can have (the DuckDB set lives in the tests).
SQL_DIALECTS: Final = {
    d.name: d for d in (SNOWFLAKE_SQL, BIGQUERY_SQL, ATHENA_SQL, REDSHIFT_SQL)
}


@dataclass(frozen=True)
class AssignmentMapping:
    """An assignment source: a table and the four columns it is read through."""

    table: tuple[str, ...]
    unit_id: str
    experiment_key: str
    variant: str
    exposed_at: str
    filters: tuple[SourceFilter, ...] = ()


@dataclass(frozen=True)
class MetricMapping:
    """A metric source: a table, its columns, and how a unit's value is formed."""

    table: tuple[str, ...]
    unit_id: str
    event_at: str
    metric_type: MetricType
    value: Optional[str] = None
    conversion_window_hours: int = 168
    cap_value: Optional[float] = None
    filters: tuple[SourceFilter, ...] = ()


@dataclass(frozen=True)
class AnalysisWindow:
    """``[start, end)``; both must be timezone-aware."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class BuiltQuery:
    """A generated statement and what it may read."""

    kind: Literal["metric", "diagnostics", "preview"]
    dialect: str
    sql: str
    #: The table references we rendered, as tuples of their parts.
    tables: frozenset[tuple[str, ...]] = field(default_factory=frozenset)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def _window_literals(dialect: SqlDialect, window: AnalysisWindow) -> tuple[str, str]:
    start = utc_text(window.start, "window_start")
    end = utc_text(window.end, "window_end")
    if not window.start < window.end:
        raise WarehouseQueryRefused(
            "invalid_literal", "window", "window_start must be before window_end"
        )
    return dialect.timestamp_literal(start), dialect.timestamp_literal(end)


def _where(conditions: Sequence[str]) -> str:
    return "\n    AND ".join(conditions)


def _exposures_cte(
    dialect: SqlDialect,
    assignment: AssignmentMapping,
    experiment_key: Optional[str],
    start: str,
    end: str,
) -> str:
    """The exposures in the window; for one experiment unless the key is None
    (a source preview across every experiment in the table)."""
    rules = dialect.identifiers
    table = render_table(rules, assignment.table)
    unit = render_column(rules, assignment.unit_id, "columns.unit_id")
    key = render_column(rules, assignment.experiment_key, "columns.experiment_key")
    variant = render_column(rules, assignment.variant, "columns.variant")
    exposed = dialect.time_column(
        render_column(rules, assignment.exposed_at, "columns.exposed_at")
    )
    conditions = [
        *(
            [f"{key} = {render_experiment_key(experiment_key)}"]
            if experiment_key is not None
            else []
        ),
        f"{exposed} >= {start}",
        f"{exposed} < {end}",
        *render_filters(rules, assignment.filters, "filters"),
    ]
    s = dialect.string_type
    return (
        "exposures AS (\n"  # nosec B608 - names pass render_column/render_table (fullmatch, then quoted), key and times are rendered literals, verify_generated_sql re-parses
        f"  SELECT CAST({unit} AS {s}) AS unit_id,\n"
        f"    SUBSTR(CAST({variant} AS {s}), 1, {LABEL_MAX_CHARS}) AS variant,\n"
        f"    {exposed} AS exposed_at\n"
        f"  FROM {table}\n"
        f"  WHERE {_where(conditions)}\n"
        ")"
    )


def _check_metric(metric: MetricMapping) -> None:
    if metric.metric_type not in ("proportion", "mean"):
        raise WarehouseQueryRefused(
            "invalid_literal", "metric_type", "metric_type must be proportion or mean"
        )
    hours = metric.conversion_window_hours
    if type(hours) is not int or not 1 <= hours <= MAX_CONVERSION_WINDOW_HOURS:
        raise WarehouseQueryRefused(
            "invalid_literal",
            "conversion_window_hours",
            f"conversion_window_hours must be 1 to {MAX_CONVERSION_WINDOW_HOURS}",
        )
    if metric.metric_type == "mean":
        if metric.value is None:
            raise WarehouseQueryRefused(
                "invalid_identifier",
                "columns.value",
                "a mean metric needs a value column",
            )
    else:
        if metric.value is not None or metric.cap_value is not None:
            raise WarehouseQueryRefused(
                "invalid_identifier",
                "columns.value",
                "a proportion metric takes no value column and no cap",
            )
    if metric.cap_value is not None and not (
        isinstance(metric.cap_value, (int, float))
        and not isinstance(metric.cap_value, bool)
        and 0 < metric.cap_value <= MAX_CAP_VALUE
    ):
        raise WarehouseQueryRefused(
            "invalid_literal", "cap_value", "cap_value must be above 0 and at most 1e15"
        )


def build_metric_query(
    dialect: SqlDialect,
    assignment: AssignmentMapping,
    metric: MetricMapping,
    experiment_key: str,
    window: AnalysisWindow,
) -> BuiltQuery:
    """The per-metric statement: per-variant counts and centred sums."""
    validate_experiment_key(experiment_key)
    _check_metric(metric)
    rules = dialect.identifiers
    start, end = _window_literals(dialect, window)
    d = dialect.double_type
    s = dialect.string_type

    metric_table = render_table(rules, metric.table)
    m_unit = render_column(rules, metric.unit_id, "columns.unit_id")
    event_at = dialect.time_column(
        render_column(rules, metric.event_at, "columns.event_at")
    )
    is_mean = metric.metric_type == "mean"

    event_columns = [f"CAST({m_unit} AS {s}) AS unit_id", f"{event_at} AS event_at"]
    if is_mean:
        value = render_column(rules, metric.value, "columns.value")  # type: ignore[arg-type]
        event_columns.append(f"CAST({value} AS {d}) AS metric_value")
    event_conditions = [
        f"{event_at} >= {start}",
        f"{event_at} < {end}",
        *render_filters(rules, metric.filters, "filters"),
    ]

    if is_mean:
        y_raw = "COALESCE(SUM(e.metric_value), 0)"
        y_expr = f"CAST(y_raw AS {d})"
        if metric.cap_value is not None:
            cap = render_float_literal(metric.cap_value, "cap_value")
            y_expr = f"LEAST(CAST(y_raw AS {d}), CAST({cap} AS {d}))"
        per_unit_value = f",\n    {y_raw} AS y_raw"
        null_values = "(SELECT COUNT(*) FROM events WHERE metric_value IS NULL)"
    else:
        per_unit_value = ""
        y_expr = f"CAST(CASE WHEN n_events > 0 THEN 1 ELSE 0 END AS {d})"
        null_values = "0"

    event_end = dialect.add_hours("u.first_exposed_at", metric.conversion_window_hours)
    ser = dialect.serialise
    select = [
        "y.variant AS variant",
        "COUNT(*) AS n",
        "SUM(y.converted) AS n_converted",
        f"{ser('MIN(k.k)')} AS k",
        f"{ser('SUM(y.y - k.k)')} AS sum_d",
        f"{ser('SUM((y.y - k.k) * (y.y - k.k))')} AS sum_d2",
        "MIN(diag.metric_rows_in_window) AS metric_rows_in_window",
        "MIN(diag.metric_rows_matched) AS metric_rows_matched",
        "MIN(diag.null_value_rows) AS null_value_rows",
    ]
    diag_offset = ""
    if dialect.session_offset:
        diag_offset = f",\n    {dialect.session_offset} AS session_offset"
        select.append("MIN(diag.session_offset) AS session_offset")
    select_list = ",\n  ".join(select)

    sql = (
        "WITH "
        + _exposures_cte(dialect, assignment, experiment_key, start, end)
        + ",\nunits AS (\n"  # nosec B608 - names pass render_column/render_table (fullmatch, then quoted), literals are rendered or refused, verify_generated_sql re-parses
        "  SELECT unit_id, MIN(variant) AS variant, MIN(exposed_at) AS first_exposed_at,\n"
        "    COUNT(DISTINCT variant) AS n_variants\n"
        "  FROM exposures\n"
        "  WHERE unit_id IS NOT NULL AND variant IS NOT NULL\n"
        "  GROUP BY unit_id\n"
        "),\nevents AS (\n"
        f"  SELECT {', '.join(event_columns)}\n"
        f"  FROM {metric_table}\n"
        f"  WHERE {_where(event_conditions)}\n"
        "),\nper_unit AS (\n"
        "  SELECT u.unit_id, u.variant, COUNT(e.event_at) AS n_events"
        f"{per_unit_value}\n"
        "  FROM units AS u\n"
        "  LEFT JOIN events AS e\n"
        "    ON e.unit_id = u.unit_id\n"
        "    AND e.event_at >= u.first_exposed_at\n"
        f"    AND e.event_at < LEAST({event_end}, {end})\n"
        "  WHERE u.n_variants = 1\n"
        "  GROUP BY u.unit_id, u.variant\n"
        "),\ny AS (\n"
        "  SELECT variant, CASE WHEN n_events > 0 THEN 1 ELSE 0 END AS converted,\n"
        f"    {y_expr} AS y\n"
        "  FROM per_unit\n"
        "),\nk AS (\n"
        # ``y.y``, not ``y``: the CTE and its column share the name, and
        # BigQuery reads a bare ``y`` here as the whole row (a STRUCT), so
        # AVG is refused with invalidQuery.  Snowflake, Athena and DuckDB read
        # it as the column; the qualified name means the column in every dialect.
        "  SELECT AVG(y.y) AS k FROM y\n"
        "),\ndiag AS (\n"
        "  SELECT (SELECT COUNT(*) FROM events) AS metric_rows_in_window,\n"
        "    (SELECT COUNT(*) FROM events AS e INNER JOIN units AS u ON e.unit_id = u.unit_id)"
        " AS metric_rows_matched,\n"
        f"    {null_values} AS null_value_rows{diag_offset}\n"
        ")\n"
        f"SELECT {select_list}\n"
        "FROM y CROSS JOIN k CROSS JOIN diag\n"
        "GROUP BY y.variant\n"
        "ORDER BY n DESC, variant\n"
        f"LIMIT {VARIANT_ROW_LIMIT}\n"
    )
    tables = frozenset(
        {
            validate_table_parts(rules, assignment.table),
            validate_table_parts(rules, metric.table),
        }
    )
    built = BuiltQuery(kind="metric", dialect=dialect.name, sql=sql, tables=tables)
    verify_generated_sql(built, METRIC_CTES)
    return built


def build_diagnostics_query(
    dialect: SqlDialect,
    assignment: AssignmentMapping,
    experiment_key: str,
    window: AnalysisWindow,
) -> BuiltQuery:
    """The per-run statement over the assignment source only."""
    validate_experiment_key(experiment_key)
    start, end = _window_literals(dialect, window)
    select = [
        "(SELECT COUNT(*) FROM exposures) AS exposure_rows",
        "(SELECT COUNT(*) FROM exposures WHERE unit_id IS NULL OR variant IS NULL)"
        " AS null_key_rows",
        "(SELECT COUNT(*) FROM units) AS units",
        "(SELECT COUNT(*) FROM units WHERE n_variants > 1) AS multi_variant_units",
        "(SELECT COUNT(DISTINCT variant) FROM exposures"
        " WHERE unit_id IS NOT NULL AND variant IS NOT NULL) AS variant_values",
    ]
    if dialect.session_offset:
        select.append(f"{dialect.session_offset} AS session_offset")
    select_list = ",\n  ".join(select)
    sql = (
        "WITH "
        + _exposures_cte(dialect, assignment, experiment_key, start, end)
        + ",\nunits AS (\n"  # nosec B608 - only _exposures_cte output and fixed text, verify_generated_sql re-parses
        "  SELECT unit_id, COUNT(DISTINCT variant) AS n_variants\n"
        "  FROM exposures\n"
        "  WHERE unit_id IS NOT NULL AND variant IS NOT NULL\n"
        "  GROUP BY unit_id\n"
        ")\n"
        f"SELECT {select_list}\n"
    )
    tables = frozenset({validate_table_parts(dialect.identifiers, assignment.table)})
    built = BuiltQuery(kind="diagnostics", dialect=dialect.name, sql=sql, tables=tables)
    verify_generated_sql(built, DIAGNOSTICS_CTES)
    return built


def _epoch(dialect: SqlDialect) -> Callable[[str], str]:
    if dialect.epoch_seconds is None:
        raise _refuse_internal()
    return dialect.epoch_seconds


def build_assignment_preview_query(
    dialect: SqlDialect,
    assignment: AssignmentMapping,
    window: AnalysisWindow,
    experiment_key: Optional[str] = None,
) -> BuiltQuery:
    """A source preview over an assignment source: aggregates only.

    One row per variant label (at most 51, the largest first), each carrying
    the same totals: rows in the window, rows with no unit or no variant, and
    the earliest and latest exposure as whole epoch seconds (and, on
    Snowflake, the session's UTC offset, which the connector checks).  With no
    rows in the window there is exactly one row, whose label is NULL.  No
    row-level value leaves the warehouse.
    """
    if experiment_key is not None:
        validate_experiment_key(experiment_key)
    epoch = _epoch(dialect)
    start, end = _window_literals(dialect, window)
    offset_total = offset_select = ""
    if dialect.session_offset:
        offset_total = f",\n    MIN({dialect.session_offset}) AS session_offset"
        offset_select = ",\n  t.session_offset AS session_offset"
    sql = (
        "WITH "
        + _exposures_cte(dialect, assignment, experiment_key, start, end)
        + ",\nper_variant AS (\n"  # nosec B608 - only _exposures_cte output and fixed text, verify_generated_sql re-parses
        "  SELECT variant, COUNT(DISTINCT unit_id) AS units\n"
        "  FROM exposures\n"
        "  WHERE variant IS NOT NULL\n"
        "  GROUP BY variant\n"
        "  ORDER BY units DESC, variant\n"
        f"  LIMIT {VARIANT_ROW_LIMIT}\n"
        "),\ntotals AS (\n"
        "  SELECT COUNT(*) AS total_rows,\n"
        "    COUNT(CASE WHEN unit_id IS NULL THEN 1 END) AS null_unit_rows,\n"
        "    COUNT(CASE WHEN variant IS NULL THEN 1 END) AS null_variant_rows,\n"
        f"    MIN({epoch('exposed_at')}) AS earliest,\n"
        f"    MAX({epoch('exposed_at')}) AS latest{offset_total}\n"
        "  FROM exposures\n"
        ")\n"
        "SELECT t.total_rows AS total_rows, t.null_unit_rows AS null_unit_rows,\n"
        "  t.null_variant_rows AS null_variant_rows, t.earliest AS earliest,\n"
        f"  t.latest AS latest, v.variant AS variant, v.units AS units{offset_select}\n"
        "FROM totals AS t\n"
        "LEFT JOIN per_variant AS v ON 1 = 1\n"
        "ORDER BY v.units DESC, v.variant\n"
        f"LIMIT {VARIANT_ROW_LIMIT}\n"
    )
    tables = frozenset({validate_table_parts(dialect.identifiers, assignment.table)})
    built = BuiltQuery(kind="preview", dialect=dialect.name, sql=sql, tables=tables)
    verify_generated_sql(built, ASSIGNMENT_PREVIEW_CTES)
    return built


def build_metric_preview_query(
    dialect: SqlDialect,
    metric: MetricMapping,
    window: AnalysisWindow,
) -> BuiltQuery:
    """A source preview over a metric source: one row of aggregates.

    Rows in the window, rows with no unit, rows whose value is NULL (a mean
    metric; 0 for a proportion), and the earliest and latest event as whole
    epoch seconds.
    """
    _check_metric(metric)
    epoch = _epoch(dialect)
    rules = dialect.identifiers
    start, end = _window_literals(dialect, window)
    table = render_table(rules, metric.table)
    unit = render_column(rules, metric.unit_id, "columns.unit_id")
    event_at = dialect.time_column(
        render_column(rules, metric.event_at, "columns.event_at")
    )
    columns = [
        f"CAST({unit} AS {dialect.string_type}) AS unit_id",
        f"{event_at} AS event_at",
    ]
    null_values = "0"
    if metric.metric_type == "mean":
        value = render_column(rules, metric.value, "columns.value")  # type: ignore[arg-type]
        columns.append(f"CAST({value} AS {dialect.double_type}) AS metric_value")
        null_values = "COUNT(CASE WHEN metric_value IS NULL THEN 1 END)"
    conditions = [
        f"{event_at} >= {start}",
        f"{event_at} < {end}",
        *render_filters(rules, metric.filters, "filters"),
    ]
    offset = ""
    if dialect.session_offset:
        offset = f",\n  MIN({dialect.session_offset}) AS session_offset"
    sql = (
        "WITH events AS (\n"  # nosec B608 - names pass render_column/render_table (fullmatch, then quoted), literals are rendered or refused, verify_generated_sql re-parses
        f"  SELECT {', '.join(columns)}\n"
        f"  FROM {table}\n"
        f"  WHERE {_where(conditions)}\n"
        ")\n"
        "SELECT COUNT(*) AS total_rows,\n"
        "  COUNT(CASE WHEN unit_id IS NULL THEN 1 END) AS null_unit_rows,\n"
        f"  {null_values} AS null_value_rows,\n"
        f"  MIN({epoch('event_at')}) AS earliest,\n"
        f"  MAX({epoch('event_at')}) AS latest{offset}\n"
        "FROM events\n"
    )
    tables = frozenset({validate_table_parts(rules, metric.table)})
    built = BuiltQuery(kind="preview", dialect=dialect.name, sql=sql, tables=tables)
    verify_generated_sql(built, METRIC_PREVIEW_CTES)
    return built


def _refuse_internal() -> WarehouseQueryRefused:
    return WarehouseQueryRefused(
        "internal", "query", "the generated query did not pass its own check"
    )


def sqlglot_available() -> bool:
    return sqlglot is not None and sqlglot_exp is not None


def verify_generated_sql(built: BuiltQuery, ctes: frozenset[str]) -> None:
    """Parse ``built.sql`` again in its own dialect; refuse unless it is safe.

    The statement must be exactly one ``SELECT``; no node may be a command or
    change data; every function must be one sqlglot recognises (no
    ``Anonymous``); and the tables read, less our own CTE names, must equal
    the tables we rendered.  Any exception -- a tokenizer or parser error,
    ``RecursionError``, anything -- refuses.  So does a missing sqlglot.
    """
    if not sqlglot_available():
        raise _refuse_internal()
    try:
        statements = sqlglot.parse(built.sql, read=built.dialect)
        if len(statements) != 1 or not isinstance(statements[0], sqlglot_exp.Select):
            raise _refuse_internal()
        root = statements[0]
        forbidden = (
            sqlglot_exp.Anonymous,
            sqlglot_exp.Command,
            sqlglot_exp.DML,
            sqlglot_exp.DDL,
        )
        if any(True for _ in root.find_all(*forbidden)):
            raise _refuse_internal()
        read: set[tuple[str, ...]] = set()
        for table in root.find_all(sqlglot_exp.Table):
            parts = tuple(p for p in (table.catalog, table.db, table.name) if p)
            if len(parts) == 1 and parts[0] in ctes:
                continue
            read.add(parts)
        if read != set(built.tables):
            raise _refuse_internal()
    except WarehouseQueryRefused:
        raise
    except Exception:
        # Every failure refuses: SqlglotError (TokenError included),
        # RecursionError, MemoryError, anything the parser raises.
        raise _refuse_internal() from None

"""The second line: every generated statement is parsed again before use.

The gate means a bad value never reaches SQL; this checks the SQL anyway, so
a future template edit that reads another table, adds a statement or calls an
unknown function is refused rather than sent.  With sqlglot missing, nothing
is sent at all.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from modules.backend.app.core.warehouse_identifiers import WarehouseQueryRefused
from modules.backend.app.services import warehouse_query_builder as builder
from modules.backend.app.services.warehouse_query_builder import (
    METRIC_CTES,
    SQL_DIALECTS,
    AnalysisWindow,
    AssignmentMapping,
    MetricMapping,
    build_diagnostics_query,
    build_metric_query,
    verify_generated_sql,
)
from modules.backend.tests.unit.warehouse.test_golden_sql import build_case

pytestmark = pytest.mark.unit

WINDOW = AnalysisWindow(
    datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc)
)


def _refused(built, ctes=METRIC_CTES) -> str:
    with pytest.raises(WarehouseQueryRefused) as refused:
        verify_generated_sql(built, ctes)
    return refused.value.code


@pytest.mark.parametrize("dialect", sorted(SQL_DIALECTS))
def test_generated_sql_is_one_select_over_exactly_the_rendered_tables(dialect):
    built = build_case(dialect, "metric_mean")
    verify_generated_sql(built, METRIC_CTES)  # the real statement passes
    quote = SQL_DIALECTS[dialect].identifiers.quote_open
    other = ".".join(
        f"{quote}{p}{quote}"
        for p in ("otherdb", "secret", "t")[-len(next(iter(built.tables))) :]
    )
    sql = built.sql.rstrip("\n")
    tampered = [
        # an extra table read
        built.sql.replace(
            "FROM y CROSS JOIN k", f"FROM y CROSS JOIN {other} CROSS JOIN k"
        ),
        # a second statement
        sql + ";\nSELECT 1",
        # a statement that is not a SELECT
        "DELETE FROM " + other,
        # an unqualified name that is not one of our CTEs
        built.sql.replace(
            "FROM y CROSS JOIN k", "FROM y CROSS JOIN secrets CROSS JOIN k"
        ),
        # a function sqlglot does not know
        built.sql.replace("COUNT(*) AS n,", "COUNT(*) AS n, mystery_fn(1) AS m,"),
        # nothing parseable
        "SELECT (((",
        "",
    ]
    for sql_text in tampered:
        assert sql_text != built.sql
        assert _refused(replace(built, sql=sql_text)) == "internal", sql_text


def test_a_table_named_like_a_cte_is_still_a_table():
    """``p.d.k`` is the customer's table ``k``, not our CTE ``k``."""
    built = build_case("bigquery", "metric_proportion")
    tampered = built.sql.replace(
        "FROM y CROSS JOIN k",
        "FROM y CROSS JOIN `acme-prod`.`analytics`.`k` CROSS JOIN k",
    )
    assert _refused(replace(built, sql=tampered)) == "internal"


def test_missing_a_rendered_table_is_refused():
    built = build_case("athena", "metric_proportion")
    assert (
        _refused(replace(built, tables=built.tables | {("analytics", "extra")}))
        == "internal"
    )


#: A valid table per dialect, for the statements built below.
_TABLES = {
    "athena": ("analytics", "e"),
    "bigquery": ("acme-prod", "a", "e"),
    "redshift": ("analytics", "e"),
    "snowflake": ("A", "B", "E"),
}


def test_sqlglot_absent_refuses_all(monkeypatch):
    assert set(_TABLES) == set(SQL_DIALECTS)
    monkeypatch.setattr(builder, "sqlglot", None)
    monkeypatch.setattr(builder, "sqlglot_exp", None)
    for dialect in SQL_DIALECTS.values():
        a = AssignmentMapping(_TABLES[dialect.name], "u", "k", "v", "t")
        m = MetricMapping(a.table, "u", "t", "proportion")
        for build in (
            lambda: build_metric_query(dialect, a, m, "key", WINDOW),
            lambda: build_diagnostics_query(dialect, a, "key", WINDOW),
        ):
            with pytest.raises(WarehouseQueryRefused) as refused:
                build()
            assert refused.value.code == "internal"


@pytest.mark.parametrize("error", [RecursionError, MemoryError, ValueError, TypeError])
def test_any_parser_failure_refuses(monkeypatch, error):
    built = build_case("snowflake", "diagnostics")

    def boom(*args, **kwargs):
        raise error("parser failure")

    monkeypatch.setattr(builder.sqlglot, "parse", boom)
    assert _refused(built, builder.DIAGNOSTICS_CTES) == "internal"


def test_recursion_limit_is_never_changed():
    root = Path(builder.__file__).parents[1]
    for path in root.rglob("*.py"):
        assert "setrecursionlimit" not in path.read_text(encoding="utf-8"), path


def test_the_builder_never_transpiles():
    """Each dialect's SQL is written for it; sqlglot only reads it back."""
    tree = ast.parse(Path(builder.__file__).read_text(encoding="utf-8"))
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sqlglot"
    }
    assert used == {"parse"}


def _other_table(dialect: str, built) -> str:
    quote = SQL_DIALECTS[dialect].identifiers.quote_open
    parts = ("otherdb", "secret", "t")[-len(next(iter(built.tables))) :]
    return ".".join(f"{quote}{p}{quote}" for p in parts)


#: Ways a further table could be read without appearing in the top-level
#: FROM. Each rewrites the real metric statement; every one must be refused.
EXTRA_READS = {
    "correlated_scalar_subquery": lambda sql, other: sql.replace(
        "COUNT(*) AS n,",
        f"COUNT(*) AS n, (SELECT MAX(x.c) FROM {other} AS x"
        " WHERE x.c = y.variant) AS m,",
    ),
    "unnest_of_array_agg": lambda sql, other: sql.replace(
        "FROM y CROSS JOIN k",
        f"FROM y CROSS JOIN UNNEST((SELECT ARRAY_AGG(x.c) FROM {other} AS x)) AS z"
        " CROSS JOIN k",
    ),
    "comma_join": lambda sql, other: sql.replace(
        "FROM y CROSS JOIN k", f"FROM y, {other} CROSS JOIN k"
    ),
    "extra_cte": lambda sql, other: sql.replace(
        "WITH exposures AS (",
        f"WITH extra AS (SELECT * FROM {other}),\nexposures AS (",
        1,
    ),
    "external_query": lambda sql, other: sql.replace(
        "FROM y CROSS JOIN k",
        "FROM y CROSS JOIN EXTERNAL_QUERY('acme-prod.us.conn', 'SELECT 1') AS x"
        " CROSS JOIN k",
    ),
}

#: sqlglot 30.20 parses every pair (EXTERNAL_QUERY, BigQuery's, parses in all
#: three as an unrecognised table function), so each refusal comes from the
#: checks on the parsed tree and not from a parse error. A pair that stops
#: parsing fails test_extra_read_cases_parse instead of silently testing a parse
#: error.
EXTRA_READ_CASES = [
    (extra_read, dialect)
    for extra_read in sorted(EXTRA_READS)
    for dialect in sorted(SQL_DIALECTS)
]


def _with_extra_read(extra_read: str, dialect: str):
    built = build_case(dialect, "metric_mean")
    sql = EXTRA_READS[extra_read](built.sql, _other_table(dialect, built))
    assert sql != built.sql
    return replace(built, sql=sql)


@pytest.mark.parametrize(("extra_read", "dialect"), EXTRA_READ_CASES)
def test_extra_read_cases_parse(extra_read, dialect):
    tampered = _with_extra_read(extra_read, dialect)
    assert len(builder.sqlglot.parse(tampered.sql, read=dialect)) == 1


@pytest.mark.parametrize(("extra_read", "dialect"), EXTRA_READ_CASES)
def test_a_table_read_anywhere_in_the_statement_is_refused(extra_read, dialect):
    assert _refused(_with_extra_read(extra_read, dialect)) == "internal"

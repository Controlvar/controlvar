"""Warehouse analysis: operator ceilings, who may test and author, and the counting (#1142).

The ceilings bound what a connection may set; connection tests and source
validation are run by their own roles with a fixed 30-second total and are not
counted by the daily limit. A source may name any table its connection's role
can read. ``/results`` counts converting users with no time bound, where the
warehouse analysis bounds each conversion by the unit's first assignment and the conversion window.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.services import event_matching
from modules.backend.app.api.v1.endpoints import warehouse_analysis as endpoint
from modules.backend.app.services.warehouse_query_builder import (
    SQL_DIALECTS,
    AnalysisWindow,
    AssignmentMapping,
    MetricMapping,
    build_metric_query,
)
from modules.backend.app.settings import ModulesSettings

SOURCE = pathlib.Path(endpoint.__file__)


def test_the_operator_ceilings_and_their_defaults():
    defaults = ModulesSettings(_env_file=None)
    assert defaults.WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS == 1800
    assert defaults.WAREHOUSE_MAX_BYTES_PER_QUERY == 200 * 2**30 == 214748364800
    assert defaults.WAREHOUSE_MAX_RUNS_PER_DAY == 200

    limits = SimpleNamespace(
        WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS=1800,
        WAREHOUSE_MAX_BYTES_PER_QUERY=214748364800,
        WAREHOUSE_MAX_RUNS_PER_DAY=200,
    )
    at_ceiling = {
        "query_timeout_seconds": 1800,
        "max_runs_per_day": 200,
        "max_bytes_per_query": 214748364800,
    }
    with patch.object(endpoint, "_limits", return_value=limits):
        endpoint._check_limits(SimpleNamespace(**at_ceiling))
        for field in at_ceiling:
            above = dict(at_ceiling, **{field: at_ceiling[field] + 1})
            with pytest.raises(HTTPException) as refused:
                endpoint._check_limits(SimpleNamespace(**above))
            assert refused.value.status_code == 422, field
            assert field in str(refused.value.detail), field


def _functions():
    return {
        node.name: node
        for node in ast.walk(ast.parse(SOURCE.read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls(function, name):
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def test_connection_tests_and_validation_have_their_roles_and_thirty_seconds():
    functions = _functions()
    for name in ("test_connection", "test_unsaved_connection"):
        (guard,) = _calls(functions[name], "require")
        assert isinstance(guard.args[1], ast.Name) and guard.args[1].id == "ADMIN", name
    (guard,) = _calls(functions["validate_source"], "require")
    assert ast.unparse(guard.args[1]) == "_source_roles(source.kind)"
    assert endpoint.ADMIN == ("ADMIN",)

    for name in ("test_connection", "test_unsaved_connection", "validate_source"):
        totals = [
            k.value
            for call in ast.walk(functions[name])
            if isinstance(call, ast.Call)
            for k in call.keywords
            if k.arg == "total_seconds"
        ]
        assert [ast.unparse(t) for t in totals] == ["connection_test_total()"], name
        # Not counted by the daily limit: no reservation is taken.
        assert _calls(functions[name], "reserve") == [], name
    assert endpoint.connection_test_total() == 30


def test_a_source_author_reads_any_table_the_connection_role_can():
    functions = _functions()
    for name in ("create_source", "update_source"):
        (guard,) = _calls(functions[name], "require")
        assert "_source_roles" in ast.unparse(guard.args[1]), name
    assert endpoint._source_roles("metric") == ("ADMIN", "DEVELOPER", "ANALYST")
    assert endpoint._source_roles("assignment") == ("ADMIN", "DEVELOPER")

    # No allow-list of tables: the statement reads whatever table the source
    # names, so the connection's grants are the only boundary.
    dialect = SQL_DIALECTS["snowflake"]
    query = build_metric_query(
        dialect,
        AssignmentMapping(
            table=("ANALYTICS", "PUBLIC", "ASSIGNMENTS"),
            unit_id="USER_ID",
            experiment_key="EXPERIMENT",
            variant="VARIANT",
            exposed_at="EXPOSED_AT",
        ),
        MetricMapping(
            table=("HR", "PAYROLL", "SALARIES"),
            unit_id="USER_ID",
            event_at="PAID_AT",
            metric_type="proportion",
        ),
        "checkout_button_color",
        AnalysisWindow(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ),
    )
    assert "SALARIES" in query.sql


def test_results_counts_a_conversion_whatever_its_time():
    experiment_id, variant_id = uuid.uuid4(), uuid.uuid4()
    assigned_at = datetime(2026, 1, 10, tzinfo=timezone.utc)
    converters = [
        ("long-before", assigned_at - timedelta(days=30)),
        ("long-after", assigned_at + timedelta(days=90)),
    ]
    filters = []

    def query(*columns):
        q = MagicMock()

        def remember(*conditions):
            filters.extend(str(c) for c in conditions)
            return q

        q.filter.side_effect = remember
        q.group_by.return_value = q
        if len(columns) == 2 and "variant_id" in str(columns[0]):
            q.all.return_value = [(variant_id, u) for u, _ in converters]
        else:
            q.all.return_value = converters
        return q

    db = MagicMock()
    db.query.side_effect = query
    count = event_matching.count_converting_users(
        db, experiment_id, variant_id, "checkout_completed"
    )
    assert count == 2
    assert not [f for f in filters if "created_at" in f or "timestamp" in f], filters

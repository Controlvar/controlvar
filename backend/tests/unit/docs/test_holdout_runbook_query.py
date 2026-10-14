"""The migrations page's query for ``d29a479daafe`` lists the kept holdout first.

The revision deactivates every active global holdout but one, the one its own
selection orders first. The page gives a read-only query to run before the
upgrade, and says its first row is the one kept. So the query must select the
active rows and order them exactly as the revision does.

The revision's statement is the one ``_keep_one_active()`` builds, captured from
the bind it executes on and compiled for PostgreSQL; nothing runs.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest
from sqlalchemy.dialects import postgresql

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "self-hosting" / "migrations.md"
REVISION = (
    REPO_ROOT
    / "backend"
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "d29a479daafe_holdout_population_and_one_active_holdout.py"
)
LABEL = "-- List the active holdouts, the kept one first"

_SQL_BLOCK = re.compile(r"^[ \t]*```sql\n(.*?)^[ \t]*```", re.M | re.S)
_ORDER = re.compile(r"\bORDER BY\b(?P<terms>.*?)(?:\bLIMIT\b|\)|;|$)", re.I | re.S)


def page_query() -> str:
    (query,) = [
        body.strip()
        for body in _SQL_BLOCK.findall(PAGE.read_text(encoding="utf-8"))
        if body.strip().startswith(LABEL)
    ]
    return query


def _order_terms(sql: str) -> list[tuple[str, str]]:
    """(column, direction) for each ORDER BY term, table names dropped."""
    match = _ORDER.search(sql)
    assert match, sql
    terms = []
    for term in match.group("terms").split(","):
        words = term.split()
        column = words[0].split(".")[-1]
        direction = words[1].upper() if len(words) > 1 else "ASC"
        terms.append((column, direction))
    return terms


def revision_statement() -> str:
    """The SQL of the update ``_keep_one_active()`` runs, kept-row selection included."""
    spec = importlib.util.spec_from_file_location("_d29a479daafe_probe", REVISION)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    captured = []

    class Bind:
        def execute(self, statement):
            captured.append(statement)
            return []

    original = module.op.get_bind
    module.op.get_bind = lambda: Bind()
    try:
        module._keep_one_active()
    finally:
        module.op.get_bind = original
        del sys.modules[spec.name]
    (statement,) = captured
    return str(statement.compile(dialect=postgresql.dialect()))


def test_the_page_query_orders_the_active_holdouts_as_the_revision_keeps_one():
    query = page_query()
    statement = revision_statement()
    assert re.search(r"\bFROM experimentation\.global_holdouts\b", query), query
    assert re.search(r"\bWHERE is_active\b", query), query
    assert "global_holdouts.is_active IS true" in statement, statement
    assert (
        _order_terms(query)
        == _order_terms(statement)
        == [
            ("updated_at", "DESC"),
            ("id", "ASC"),
        ]
    )
    assert not re.search(r"\b(UPDATE|DELETE|INSERT)\b", query, re.I), query

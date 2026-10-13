"""The counters table and the Glue events table, and who writes counters (#1142).

``DYNAMODB_COUNTERS_TABLE`` and ``GLUE_EVENTS_TABLE`` default to
``experiment-counters`` and ``raw_events``. Counters are incremented only
through the ``/api/v1/counters`` routes: the record-driven helpers in
``dynamodb_counter_integration.py`` are imported by nothing, so nothing
writes a counter on its own.
"""

from __future__ import annotations

import ast
import pathlib

from modules.backend.app.settings import ModulesSettings

REPO = pathlib.Path(__file__).resolve().parents[5]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]
INTEGRATION = "modules.backend.app.services.dynamodb_counter_integration"


def test_the_counters_and_events_table_defaults(monkeypatch):
    for name in ("DYNAMODB_COUNTERS_TABLE", "GLUE_EVENTS_TABLE"):
        monkeypatch.delenv(name, raising=False)
    defaults = ModulesSettings(_env_file=None)
    assert defaults.DYNAMODB_COUNTERS_TABLE == "experiment-counters"
    assert defaults.GLUE_EVENTS_TABLE == "raw_events"


def test_nothing_writes_counters_automatically():
    writers, importers = set(), set()
    for tree in TREES:
        for path in tree.rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"increment_counter", "bulk_increment"}
                ):
                    writers.add(rel)
                if isinstance(node, ast.ImportFrom) and node.module == INTEGRATION:
                    importers.add(rel)
                if isinstance(node, ast.Import) and any(
                    a.name == INTEGRATION for a in node.names
                ):
                    importers.add(rel)
    assert writers == {
        "modules/backend/app/api/v1/endpoints/realtime_counters.py",
        "modules/backend/app/services/dynamodb_counter_service.py",
        "modules/backend/app/services/dynamodb_counter_integration.py",
    }
    # The helpers that would increment from tracking records: imported by
    # nothing but their own docstring example.
    assert importers <= {"modules/backend/app/services/dynamodb_counter_integration.py"}


def test_a_counter_is_incremented_by_one_atomic_add():
    from unittest.mock import MagicMock

    from modules.backend.app.services.dynamodb_counter_service import (
        CounterType,
        DynamoDBCounterService,
    )

    resource = MagicMock()
    table = resource.Table.return_value
    table.update_item.return_value = {"Attributes": {"assignments": 3}}
    service = DynamoDBCounterService(table_name="counters", dynamodb_resource=resource)

    assert service.increment_counter("exp-1", "v1", CounterType.ASSIGNMENT, 2) == 3

    # One UpdateItem whose expression ADDs to the counter: no read first, and
    # no write of a value computed here, so concurrent writers never contend.
    (call,) = table.method_calls
    assert call[0] == "update_item"
    expression = call.kwargs["UpdateExpression"]
    assert expression.split()[:3] == ["ADD", "#attr", ":val"]
    assert call.kwargs["ExpressionAttributeNames"] == {"#attr": "assignments"}
    assert int(call.kwargs["ExpressionAttributeValues"][":val"]) == 2

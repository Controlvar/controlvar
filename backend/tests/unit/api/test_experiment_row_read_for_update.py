"""A route that reads an experiment's row for update runs on to its commit
with no ``await`` between (#1545).

PUT /experiments/{id} with visual_config and PUT /experiments/{id}/schedule
with a start date read the row for update (``_read_row_for_update``) and
commit in the service call. The handlers run on the API's event loop: an
``await`` between the two would hand the event loop to another request while
the row is held, and that request's own read of the row would then hold the
event loop until its wait ended. The integration test
``test_put_and_schedule_sent_together_into_one_app_both_answer`` shows that
through one app; this pins it in the source, with no database.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

from backend.app.api.v1.endpoints import experiments as experiments_endpoints
from backend.app.api.v1.endpoints import users as users_endpoints

pytestmark = [pytest.mark.unit]

#: Each route that reads the row for update, and the service call that commits.
ROUTES = {
    "update_experiment": "update_experiment",
    "update_experiment_schedule": "update_experiment_schedule",
}


def _call_lines(fn: ast.AST, name: str) -> List[int]:
    lines = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else None
        if isinstance(func, ast.Name):
            called = func.id
        if called == name:
            lines.append(node.lineno)
    return sorted(lines)


def _awaits_between_read_and_commit(source: str, route: str, service: str) -> List[int]:
    """The lines of every ``await`` in *route* after its read for update and
    before its *service* call. Fails if either call is missing."""
    tree = ast.parse(source)
    (fn,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == route
    ]
    reads = _call_lines(fn, "_read_row_for_update")
    commits = _call_lines(fn, service)
    assert len(reads) == 1, f"{route} reads the row for update {len(reads)} times"
    assert commits, f"{route} never calls {service}"
    read, commit = reads[0], commits[0]
    assert read < commit, f"{route} reads the row for update after {service}"
    return sorted(
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Await) and read < node.lineno < commit
    )


@pytest.mark.regression
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_no_await_between_the_read_for_update_and_the_commit(route):
    source = Path(experiments_endpoints.__file__).read_text(encoding="utf-8")

    assert _awaits_between_read_and_commit(source, route, ROUTES[route]) == []


def test_the_check_finds_a_planted_await():
    """Positive control: an await between the two calls is found."""
    planted = (
        "async def update_experiment(db, experiment_id):\n"
        "    experiment = _read_row_for_update(db, experiment_id)\n"
        "    await asyncio.sleep(0.2)\n"
        "    return service.update_experiment(experiment, {})\n"
    )

    found = _awaits_between_read_and_commit(
        planted, "update_experiment", "update_experiment"
    )

    assert found == [3]


def test_the_bound_is_users_py_bound():
    """The read waits at most what users.py's superuser rows wait."""
    assert experiments_endpoints.EXPERIMENT_ROW_LOCK_TIMEOUT == "5s"
    assert (
        experiments_endpoints.EXPERIMENT_ROW_LOCK_TIMEOUT
        == users_endpoints.SUPERUSER_LOCK_TIMEOUT
    )

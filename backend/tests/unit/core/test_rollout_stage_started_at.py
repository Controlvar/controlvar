"""A rollout stage's start is recorded once and read from one place (#1528).

The scheduler measured a time-based stage's duration and the schedule's
``min_stage_duration`` from the stage's ``updated_at``, which every edit moves,
so renaming a stage in progress restarted its clock. ``rollout_stages`` now has
``started_at``, written when a stage goes from pending to in progress, and
``stage_started_at`` is the one reader. These pins need no database; the
behaviour through the routes and a real tick is
``backend/tests/integration/api/test_rollout_stage_started_at.py``.

- ``stage_started_at`` prefers ``started_at`` and falls back to ``updated_at``
  only for a stage with no recorded start (one an older release started);
- no request schema has a ``started_at`` field, so a request cannot set it;
- the only places in the application that assign a stage's ``started_at`` are
  the three pending -> in progress transitions: the scheduler's
  ``_activate_stage`` and the two in ``manually_advance_stage``. An
  assignment anywhere else -- an edit route, say -- would reopen #1528.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.core.rollout_scheduler import stage_started_at
from backend.app.schemas.rollout_schedule import (
    RolloutScheduleCreate,
    RolloutScheduleUpdate,
    RolloutStageCreate,
    RolloutStageResponse,
    RolloutStageUpdate,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
APP_ROOTS = (
    REPO_ROOT / "backend" / "app",
    REPO_ROOT / "modules" / "backend" / "app",
)

STARTED = datetime(2027, 1, 8, 12, 0)
EDITED = datetime(2027, 1, 9, 8, 30)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_recorded_start_wins_over_the_last_edit():
    stage = SimpleNamespace(started_at=STARTED, updated_at=EDITED)
    assert stage_started_at(stage) == _utc(STARTED)


def test_a_stage_with_no_recorded_start_is_measured_from_its_last_update():
    """A stage an older release started has no ``started_at``."""
    stage = SimpleNamespace(started_at=None, updated_at=EDITED)
    assert stage_started_at(stage) == _utc(EDITED)


def test_a_stage_with_neither_has_no_start():
    assert stage_started_at(SimpleNamespace(started_at=None, updated_at=None)) is None


def test_an_aware_start_is_returned_in_utc():
    plus_two = timezone(timedelta(hours=2))
    stage = SimpleNamespace(
        started_at=datetime(2027, 1, 8, 14, 0, tzinfo=plus_two), updated_at=EDITED
    )
    assert stage_started_at(stage) == _utc(STARTED)


# ---------------------------------------------------------------------------
# No request can set it
# ---------------------------------------------------------------------------

#: Every schema a rollout request body is validated against. ``RolloutScheduleCreate``
#: carries its stages as ``RolloutStageCreate``.
REQUEST_SCHEMAS = (
    RolloutScheduleCreate,
    RolloutScheduleUpdate,
    RolloutStageCreate,
    RolloutStageUpdate,
)


@pytest.mark.regression
@pytest.mark.parametrize("schema", REQUEST_SCHEMAS, ids=lambda s: s.__name__)
def test_no_request_schema_has_a_started_at_field(schema):
    assert "started_at" not in schema.model_fields, (
        f"{schema.__name__} takes started_at: a request could move a stage's "
        "start, which is #1528 again"
    )


def test_the_response_carries_started_at():
    field = RolloutStageResponse.model_fields["started_at"]
    assert field.default is None
    assert not field.is_required()


# ---------------------------------------------------------------------------
# Who writes it
# ---------------------------------------------------------------------------

#: (file, function, assigned object) for every ``<x>.started_at = ...`` in the
#: application, core and modules. Scheduler runs and warehouse runs have a
#: ``started_at`` too; they set it as a keyword or a column value, never as an
#: attribute assignment, so they are not in this set.
STAGE_START_WRITES = {
    ("backend/app/core/rollout_scheduler.py", "_activate_stage", "stage"),
    ("backend/app/services/rollout_service.py", "manually_advance_stage", "stage"),
    (
        "backend/app/services/rollout_service.py",
        "manually_advance_stage",
        "next_stage",
    ),
}


def _started_at_writes(root: Path, base: Path) -> set:
    """Every attribute assignment to ``started_at`` under *root*, and every
    ``setattr(x, "started_at", ...)``, as (path from *base*, function, object)."""
    found = set()
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(base).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "started_at"
                    ):
                        found.add((name, function.name, ast.unparse(target.value)))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "setattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "started_at"
                ):
                    found.add((name, function.name, ast.unparse(node.args[0])))
    return found


@pytest.mark.regression
def test_only_the_three_start_transitions_assign_a_stage_start():
    found = set()
    for root in APP_ROOTS:
        if root.is_dir():
            found |= _started_at_writes(root, REPO_ROOT)
    # Not vacuous: the scan reads the scheduler and the service.
    assert {entry[0] for entry in found} >= {
        "backend/app/core/rollout_scheduler.py",
        "backend/app/services/rollout_service.py",
    }, found
    assert found == STAGE_START_WRITES, (
        f"new: {sorted(found - STAGE_START_WRITES)}, "
        f"gone: {sorted(STAGE_START_WRITES - found)}"
    )


def test_the_scan_finds_a_start_set_by_an_edit(tmp_path):
    """The positive control, through the same scan: an edit route that sets
    the start, by assignment or by ``setattr``, is found."""
    (tmp_path / "planted.py").write_text(
        "def update_rollout_stage(stage, now):\n"
        "    stage.started_at = now\n"
        "\n"
        "\n"
        "def apply(stage, now):\n"
        "    setattr(stage, 'started_at', now)\n",
        encoding="utf-8",
    )
    assert _started_at_writes(tmp_path, tmp_path) == {
        ("planted.py", "update_rollout_stage", "stage"),
        ("planted.py", "apply", "stage"),
    }

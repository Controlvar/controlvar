"""How many users had less than a metric's whole window (#1934).

``metrics[].variants[].open_window_users`` counts a variant's assigned users
whose window had not closed when counting stopped: assigned after
``T - window``. ``T`` is the moment the results were computed for a draft or
an active experiment, the end date once it is completed (counting stopped
there), the end date for an archived one whose end date is not after that
moment, and for a paused one, or an archived one without such an end date,
there is no ``T``: the count is null, because events sent while it is paused or
archived are not counted and when counting stopped is not recorded.

The clock is frozen at ``NOW``. Control's users were assigned 30, 20, 2 and 1
hours before it, treatment's 25 and 10 hours before, so with 24 hours and
``T = NOW`` 3 of control's 4 and 1 of treatment's 2 are open. The sample-ratio
check counts every assigned user whatever ``T`` is.
"""

from __future__ import annotations

import importlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, Optional

import pytest
from freezegun import freeze_time

from backend.app.api.v1.endpoints import results as results_module
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.experiment import ExperimentStatus, MetricType
from backend.app.models.segment import Segment, SegmentKind, SegmentMember
from backend.app.services.analysis_service import AnalysisService, open_window_time
from backend.tests.integration import metric_window_world as mw

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

NOW = datetime(2026, 11, 20, 12, 0, 0)
H = timedelta(hours=1)
#: (label, variant, hours before NOW it was assigned)
ASSIGNED = (
    ("u01", "control", 30),
    ("u03", "control", 20),
    ("u05", "control", 2),
    ("u07", "control", 1),
    ("u02", "treatment", 25),
    ("u04", "treatment", 10),
)
OPEN_AT_NOW = {"control": 3, "treatment": 1}
SRM_N = {"control": 4, "treatment": 2}


def _rows(offset_hours=0) -> tuple:
    """The six users, each with a purchase an hour after being assigned."""
    rows = []
    for label, variant, before in ASSIGNED:
        assigned = NOW - mw.S - (before - offset_hours) * H
        rows.append(
            mw.Row(
                label,
                variant,
                (mw.Sent(assigned + H),),
                True,
                f"assigned {before} h before NOW",
                assigned=assigned,
            )
        )
    return tuple(rows)


@pytest.fixture
def worlds(db_session) -> Iterator[list]:
    made: list = []
    yield made
    mw.remove(db_session, [w.experiment.id for w in made])


@pytest.fixture(autouse=True)
def _imported():
    # Importing statsmodels (and pandas) under a frozen clock does not finish.
    importlib.import_module("statsmodels.stats.power")


def _build(db_session, admin_user, worlds, **fields) -> mw.World:
    fields.setdefault("rows", _rows())
    fields.setdefault(
        "metrics",
        [mw.IN_A_DAY, mw.metric("Purchase ever", hours=None)],
    )
    world = mw.build(db_session, admin_user.id, start_date=NOW - 3 * 24 * H, **fields)
    worlds.append(world)
    return world


def _open(results, name="Purchase in a day") -> Dict[str, Optional[int]]:
    (metric,) = [m for m in results["metrics"] if m["metric_name"] == name]
    return {v["variant_name"]: v["open_window_users"] for v in metric["variants"]}


def _srm_n(world, results) -> Dict[str, int]:
    observed = results["srm"]["observed"]
    return {
        "control": observed[str(world.control.id)],
        "treatment": observed[str(world.treatment.id)],
    }


def _computed(db_session, world, at=NOW, **kwargs):
    with freeze_time(at.replace(tzinfo=timezone.utc)):
        return AnalysisService(db_session).get_experiment_results(
            world.experiment.id, include_bayesian=False, **kwargs
        )


def _set(db_session, world, status, end_date=None):
    world.experiment.status = status
    world.experiment.end_date = end_date
    db_session.commit()
    db_session.refresh(world.experiment)


#: (status, end date as hours from NOW or None, read at hours from NOW, expected)
CASES = [
    ("draft", None, 0, OPEN_AT_NOW),
    ("active", None, 0, OPEN_AT_NOW),
    # An end date that has passed while it is still active (the scheduler has
    # not completed it yet): counting has not stopped, so T is still NOW.
    ("active", -12, 0, OPEN_AT_NOW),
    # Completed at NOW - 0.5 h and read then, and read 30 days later.
    ("completed", -0.5, 0, OPEN_AT_NOW),
    ("completed", -0.5, 30 * 24, OPEN_AT_NOW),
    ("archived", -0.5, 30 * 24, OPEN_AT_NOW),
    ("archived", None, 0, {"control": None, "treatment": None}),
    ("archived", 24, 0, {"control": None, "treatment": None}),
    ("paused", None, 0, {"control": None, "treatment": None}),
]


@pytest.mark.parametrize("status, end, read, expected", CASES)
def test_the_open_windows_of_each_status(
    db_session, admin_user, worlds, status, end, read, expected
):
    world = _build(db_session, admin_user, worlds)
    end_date = None if end is None else NOW + end * H
    _set(db_session, world, ExperimentStatus(status), end_date)
    results = _computed(db_session, world, at=NOW + read * H)
    assert _open(results) == expected
    assert _open(results, "Purchase ever") == {"control": None, "treatment": None}
    (day,) = [m for m in results["metrics"] if m["metric_name"] == "Purchase in a day"]
    assert day["conversion_window_hours"] == 24
    # The sample-ratio check counts every assigned user, whatever T is.
    assert _srm_n(world, results) == SRM_N


def test_open_window_time_follows_the_rule():
    """The moment itself, for each status (naive UTC)."""

    class Experiment:
        def __init__(self, status, end_date=None):
            self.status, self.end_date = status, end_date

    at = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    before, after = NOW - H, NOW + H
    assert open_window_time(Experiment(ExperimentStatus.DRAFT), at) == NOW
    assert open_window_time(Experiment(ExperimentStatus.ACTIVE, before), at) == NOW
    assert (
        open_window_time(Experiment(ExperimentStatus.COMPLETED, before), at) == before
    )
    assert open_window_time(Experiment(ExperimentStatus.COMPLETED, after), at) == after
    assert open_window_time(Experiment(ExperimentStatus.COMPLETED), at) is None
    assert open_window_time(Experiment(ExperimentStatus.ARCHIVED, NOW), at) == NOW
    assert open_window_time(Experiment(ExperimentStatus.ARCHIVED, after), at) is None
    assert open_window_time(Experiment(ExperimentStatus.ARCHIVED), at) is None
    assert open_window_time(Experiment(ExperimentStatus.PAUSED, before), at) is None
    aware = before.replace(tzinfo=timezone.utc)
    assert open_window_time(Experiment(ExperimentStatus.COMPLETED, aware), at) == before


def test_one_aggregate_statement_per_windowed_metric(db_session, admin_user, worlds):
    """Two windowed metrics and one without: two counts of assignments, each
    grouped by variant."""
    from sqlalchemy import event

    world = _build(
        db_session,
        admin_user,
        worlds,
        metrics=[
            mw.IN_A_DAY,
            mw.metric("Purchases in 3 hours", hours=3, metric_type=MetricType.COUNT),
            mw.metric("Purchase ever", hours=None),
        ],
    )
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "count(" in statement and "assignments.created_at >" in statement:
            seen.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        results = _computed(db_session, world)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(seen) == 2, seen
    assert all("GROUP BY" in s for s in seen)
    assert _open(results, "Purchases in 3 hours") == {"control": 2, "treatment": 0}


def test_a_saved_segment_counts_its_members(
    db_session, admin_user, admin_client, worlds
):
    world = _build(db_session, admin_user, worlds)
    segment = Segment(
        name=f"Open windows {uuid.uuid4().hex[:8]}",
        kind=SegmentKind.ID_LIST.value,
        owner_id=admin_user.id,
    )
    db_session.add(segment)
    db_session.flush()
    for label in ("u01", "u05", "u04"):
        db_session.add(
            SegmentMember(segment_id=segment.id, member_id=world.user[label])
        )
    db_session.commit()
    try:
        with freeze_time(NOW.replace(tzinfo=timezone.utc), real_asyncio=True):
            response = admin_client.get(
                f"/api/v1/results/{world.experiment.id}/segments/{segment.id}"
            )
        assert response.status_code == 200, response.text
        # u01 was assigned 30 h before NOW; u05 and u04 are open.
        assert _open(response.json()) == {"control": 1, "treatment": 1}
    finally:
        db_session.rollback()
        db_session.query(SegmentMember).filter(
            SegmentMember.segment_id == segment.id
        ).delete(synchronize_session=False)
        db_session.query(Segment).filter(Segment.id == segment.id).delete(
            synchronize_session=False
        )
        db_session.commit()


def paused_then_completed(db_session, admin_user, admin_client, worlds) -> Dict:
    """Paused for 30 hours, longer than the window, then completed: the
    answer read after it, as the API gives it."""
    world = _build(db_session, admin_user, worlds)
    exp = world.experiment
    paths = f"/api/v1/experiments/{exp.id}"
    with freeze_time(NOW.replace(tzinfo=timezone.utc), real_asyncio=True):
        assert admin_client.post(f"{paths}/pause").status_code == 200
    resumed = NOW + 30 * H
    with freeze_time(resumed.replace(tzinfo=timezone.utc), real_asyncio=True):
        completed = admin_client.post(f"{paths}/complete")
        assert completed.status_code == 200, completed.text
    with freeze_time((resumed + H).replace(tzinfo=timezone.utc), real_asyncio=True):
        response = admin_client.get(
            f"/api/v1/results/{exp.id}", params={"use_cache": "false"}
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_paused_longer_than_the_window_then_completed(
    db_session, admin_user, admin_client, worlds
):
    """Counting stopped at the pause, but the end date is the completion, 30
    hours later: every window had closed by then, so the count is 0, though
    3 and 1 users had less than the full window when the pause began. The
    notice for a completed or archived experiment says a pause can leave more
    users with less than the full window; this is the answer it is read with."""
    body = paused_then_completed(db_session, admin_user, admin_client, worlds)
    assert body["status"] == "completed"
    assert body["end_date"].startswith("2026-11-21T18:00:00")
    assert _open(body) == {"control": 0, "treatment": 0}
    assert _open(body, "Purchase ever") == {"control": None, "treatment": None}


def test_an_answer_cached_by_the_engine_before_is_not_served(
    db_session, admin_user, admin_client, worlds, monkeypatch
):
    """An answer stored under 1.13.0, the engine without windows, with
    another number: this engine's key misses it, and the window's number is
    answered."""
    world = _build(db_session, admin_user, worlds)
    exp = world.experiment
    store: Dict[str, str] = {}

    class Cache:
        def get(self, key):
            return store.get(key)

        def set(self, key, value, expire=0):
            store[key] = value

    monkeypatch.setattr(results_module, "_get_cache_service", lambda: Cache())
    fresh = admin_client.get(f"/api/v1/results/{exp.id}", params={"use_cache": "false"})
    assert fresh.status_code == 200
    stale = fresh.json()
    for metric in stale["metrics"]:
        for variant in metric["variants"]:
            variant["conversions"] = 99
    # The key the engine before wrote it under: the key's version is the
    # module's, as the route reads it.
    running = results_module.ENGINE_VERSION
    monkeypatch.setattr(results_module, "ENGINE_VERSION", "1.13.0")
    old_key = results_module._results_cache_key(
        exp.id, 0.95, "benjamini_hochberg", None
    )
    monkeypatch.setattr(results_module, "ENGINE_VERSION", running)
    store.clear()
    store[old_key] = json.dumps(stale)

    served = admin_client.get(f"/api/v1/results/{exp.id}")
    assert served.status_code == 200
    day = [
        m for m in served.json()["metrics"] if m["metric_name"] == "Purchase in a day"
    ]
    assert [v["conversions"] for v in day[0]["variants"]] == [4, 2]
    # The old answer is still there, beside the new one.
    assert running == ENGINE_VERSION == "1.14.0"
    assert old_key in store and len(store) == 2

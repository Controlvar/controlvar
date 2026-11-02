"""Rollout stage times are stored in UTC whatever the database session's zone (#1462).

``rollout_stages.start_date``, ``completed_date`` and ``updated_at`` are
``timestamp without time zone`` columns. PostgreSQL converts an aware value to
the session's time zone on the way into such a column, so on a session that is
not in UTC a stage date sent with ``Z`` or an offset was stored, and served
back, shifted by the session's offset, and so were the times the scheduler
records for a stage. The scheduler reads them as UTC: on a Los Angeles session
a time-based stage became eligible eight hours early in November, and
``min_stage_duration=24`` was met after 16 hours.

CI's PostgreSQL runs in UTC, where none of this can happen, so every session
here comes from an engine whose ``connect`` listener sets America/Los_Angeles:
the routes' (``deps.get_db`` is overridden with it) and the scheduler's.
``test_the_route_session_is_in_los_angeles`` proves the zone is really set on
that factory. Every instant is written out; none is computed from the current
time, whose offset from UTC changes with daylight saving (#1523).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.api import deps
from backend.app.core.rollout_scheduler import RolloutScheduler
from backend.app.db.session import get_db as _session_get_db
from backend.app.main import app
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutStage,
    RolloutStageStatus,
)
from backend.tests.conftest import DEFAULT_TEST_DB_URL

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

LA = "America/Los_Angeles"
SCHEMA = "test_experimentation"
BASE = "/api/v1/rollout-schedules"

# A stage date of 17:00 UTC on 2026-11-05 (PST, UTC-8), in every shape the API
# takes, and one in July (PDT, UTC-7), which a fixed eight-hour correction
# would get wrong.
NOV_17 = datetime(2026, 11, 5, 17, 0)
SHAPES = [
    pytest.param("2026-11-05T17:00:00Z", NOV_17, id="z"),
    pytest.param("2026-11-05T09:00:00-08:00", NOV_17, id="minus-8"),
    pytest.param("2026-11-05T19:00:00+02:00", NOV_17, id="plus-2"),
    pytest.param("2026-11-05T17:00:00", NOV_17, id="naive"),
    pytest.param("2026-07-05T17:00:00Z", datetime(2026, 7, 5, 17, 0), id="july-z"),
]


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sessions in America/Los_Angeles, for the routes and the scheduler
# ---------------------------------------------------------------------------


@pytest.fixture
def la_factory(test_db):
    """A session factory whose every connection is in America/Los_Angeles.

    NullPool: each commit closes the connection and the next statement opens
    a new one, so the listener runs for each. The ``SET`` is committed at
    once, so the pool's reset cannot roll it back.
    """
    engine = create_engine(DEFAULT_TEST_DB_URL, poolclass=NullPool)

    @sa_event.listens_for(engine, "connect")
    def _los_angeles(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"SET TIME ZONE '{LA}'")
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()
        dbapi_conn.commit()

    # As backend.app.db.session.SessionLocal is configured.
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False)
    engine.dispose()


@pytest.fixture
def la_api(admin_client, la_factory):
    """``admin_client`` with every request's session from ``la_factory``."""

    def override_get_db():
        session = la_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[_session_get_db] = override_get_db
    return admin_client


@pytest.fixture
def flag_id(make_feature_flag) -> str:
    return str(make_feature_flag(key=f"tz-{uuid.uuid4().hex[:10]}").id)


def _stage(order: int, target: int, trigger: str = "time_based", **extra) -> dict:
    return {
        "name": f"Stage {order}",
        "stage_order": order,
        "target_percentage": target,
        "trigger_type": trigger,
        **extra,
    }


def _create(api, flag_id: str, stages: List[dict], **extra) -> Dict[str, Any]:
    response = api.post(
        f"{BASE}/",
        json={"name": "TZ Schedule", "feature_flag_id": flag_id, "stages": stages}
        | extra,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _stored(factory, stage_id: Any, column: str) -> Optional[datetime]:
    session = factory()
    try:
        return session.execute(
            text(f"SELECT {column} FROM rollout_stages WHERE id = :id"),
            {"id": str(stage_id)},
        ).scalar_one()
    finally:
        session.close()


def _statuses(factory, schedule_id: Any) -> List[str]:
    session = factory()
    try:
        return [
            row[0]
            for row in session.execute(
                text(
                    "SELECT status FROM rollout_stages "
                    "WHERE rollout_schedule_id = :id ORDER BY stage_order"
                ),
                {"id": str(schedule_id)},
            )
        ]
    finally:
        session.close()


def _tick(factory, schedule_id: Any, at: datetime, *, keep: bool = True) -> int:
    """One scheduler pass over this schedule at ``at``, in an LA session.

    Returns the number of stages it started. With ``keep=False`` nothing it
    did is committed. The scheduler's own loop reads every active schedule in
    the database at the real time; this narrows it to the test's schedule.
    """
    session = factory()
    try:
        schedule = session.get(RolloutSchedule, uuid.UUID(str(schedule_id)))
        _, transitions, _ = asyncio.run(
            RolloutScheduler()._process_schedule(session, schedule, at)
        )
        if keep:
            session.commit()
        else:
            session.rollback()
        return transitions
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The positive control
# ---------------------------------------------------------------------------


def test_the_route_session_is_in_los_angeles(la_factory):
    """Not vacuous: the factory the routes and the scheduler use is in LA, and
    there a ``Z`` value cast to ``timestamp`` really lands at 09:00."""
    session = la_factory()
    try:
        assert session.execute(text("SHOW TimeZone")).scalar_one() == LA
        landed = session.execute(
            text("SELECT '2026-11-05T17:00:00Z'::timestamptz::timestamp")
        ).scalar_one()
        assert landed == datetime(2026, 11, 5, 9, 0)
        # After a commit (a new NullPool connection) it is still LA.
        session.commit()
        assert session.execute(text("SHOW TimeZone")).scalar_one() == LA
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Stored and served through each route that takes a stage date
# ---------------------------------------------------------------------------


def _send(api, flag_id: str, route: str, given: str) -> Tuple[str, str, str]:
    """Send ``given`` as a stage ``start_date`` through ``route``.

    Returns (schedule id, stage id, the start_date the route answered with).
    """
    if route == "create":
        schedule = _create(api, flag_id, [_stage(1, 10, start_date=given)])
        stage = schedule["stages"][0]
        return schedule["id"], stage["id"], stage["start_date"]
    if route == "add-stage":
        schedule = _create(api, flag_id, [_stage(1, 10, trigger="manual")])
        response = api.post(
            f"{BASE}/{schedule['id']}/stages", json=_stage(2, 50, start_date=given)
        )
        assert response.status_code == 201, response.text
        return schedule["id"], response.json()["id"], response.json()["start_date"]
    schedule = _create(api, flag_id, [_stage(1, 10)])
    stage_id = schedule["stages"][0]["id"]
    response = api.put(f"{BASE}/stages/{stage_id}", json={"start_date": given})
    assert response.status_code == 200, response.text
    return schedule["id"], stage_id, response.json()["start_date"]


@pytest.mark.regression
@pytest.mark.parametrize("route", ["create", "add-stage", "update-stage"])
@pytest.mark.parametrize(("given", "expected"), SHAPES)
def test_a_stage_start_date_is_stored_and_served_in_utc(
    la_api, la_factory, flag_id, route, given, expected
):
    """``POST /``, ``POST /{id}/stages`` and ``PUT /stages/{id}`` store every
    shape of the same moment as that moment in UTC, and serve it back so."""
    schedule_id, stage_id, served = _send(la_api, flag_id, route, given)

    assert _stored(la_factory, stage_id, "start_date") == expected
    assert served == expected.isoformat()
    read = la_api.get(f"{BASE}/{schedule_id}")
    assert read.status_code == 200, read.text
    assert {s["id"]: s["start_date"] for s in read.json()["stages"]}[stage_id] == (
        expected.isoformat()
    )


# ---------------------------------------------------------------------------
# What the scheduler does with them
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_scheduler_starts_a_time_based_stage_at_its_utc_instant(
    la_api, la_factory, flag_id
):
    """Read back on an LA session, a stage set for 09:00 at -08:00 is due at
    17:00 UTC, not at 09:00 UTC."""
    schedule = _create(
        la_api, flag_id, [_stage(1, 10, start_date="2026-11-05T09:00:00-08:00")]
    )
    session = la_factory()
    try:
        stage = session.get(RolloutStage, uuid.UUID(schedule["stages"][0]["id"]))
        eligible = RolloutScheduler()._is_stage_eligible_for_activation
        assert not eligible(stage, _utc(2026, 11, 5, 10, 0)), (
            f"eligible at 10:00Z (start_date stored as {stage.start_date})"
        )
        assert not eligible(stage, _utc(2026, 11, 5, 16, 59, 59))
        assert eligible(stage, _utc(2026, 11, 5, 17, 0))
    finally:
        session.close()


@pytest.mark.regression
def test_min_stage_duration_and_completion_are_recorded_in_utc(
    la_api, la_factory, flag_id
):
    """The scheduler writes ``updated_at`` (the stage's start, as it reads it)
    and ``completed_date`` from an aware now. On an LA session they used to
    be stored eight hours early, so ``min_stage_duration=24`` let the next
    stage start 16 hours after the first."""
    start = _utc(2026, 11, 5, 17, 0)
    schedule = _create(
        la_api,
        flag_id,
        [
            _stage(
                1,
                10,
                start_date="2026-11-05T17:00:00Z",
                trigger_configuration={"duration": 1},
            ),
            _stage(2, 50),
        ],
        min_stage_duration=24,
    )
    first, second = (s["id"] for s in schedule["stages"])
    activated = la_api.post(f"{BASE}/{schedule['id']}/activate")
    assert activated.status_code == 200, activated.text

    assert _tick(la_factory, schedule["id"], start) == 1
    assert _statuses(la_factory, schedule["id"]) == ["IN_PROGRESS", "PENDING"]

    # Stage 1's own duration (1 h) is over; min_stage_duration (24 h) is not.
    started = _stored(la_factory, first, "updated_at")
    later = start + timedelta(hours=17)
    assert _tick(la_factory, schedule["id"], later) == 0, (
        f"advanced at T+17h: min_stage_duration was measured from {started}, "
        "not 2026-11-05 17:00:00"
    )
    assert _statuses(la_factory, schedule["id"]) == ["IN_PROGRESS", "PENDING"]
    assert started == NOV_17

    due = start + timedelta(hours=24, minutes=1)
    assert _tick(la_factory, schedule["id"], due) == 1
    assert _statuses(la_factory, schedule["id"]) == ["COMPLETED", "IN_PROGRESS"]
    naive_due = datetime(2026, 11, 6, 17, 1)
    assert _stored(la_factory, first, "completed_date") == naive_due
    assert _stored(la_factory, first, "updated_at") == naive_due
    assert _stored(la_factory, second, "updated_at") == naive_due


# ---------------------------------------------------------------------------
# Shipped behaviour the docs state: an edit restarts a stage's clock
# ---------------------------------------------------------------------------


def test_editing_an_in_progress_stage_restarts_its_clock(la_api, la_factory, flag_id):
    """The scheduler measures time-based completion and ``min_stage_duration``
    from the stage's ``updated_at``, and any edit sets it, a rename included.
    This pins the behaviour the docs state until it is changed (#1528)."""
    began = _utc(2020, 1, 1, 0, 0)
    schedule = _create(
        la_api,
        flag_id,
        [_stage(1, 10, trigger_configuration={"duration": 1}), _stage(2, 50)],
        min_stage_duration=24,
    )
    first = schedule["stages"][0]["id"]
    activated = la_api.post(f"{BASE}/{schedule['id']}/activate")
    assert activated.status_code == 200, activated.text
    assert _tick(la_factory, schedule["id"], began) == 1
    assert _stored(la_factory, first, "updated_at") == datetime(2020, 1, 1, 0, 0)
    # Untouched, it is due a day later (nothing is kept).
    due = began + timedelta(hours=24, minutes=1)
    assert _tick(la_factory, schedule["id"], due, keep=False) == 1

    renamed = la_api.put(f"{BASE}/stages/{first}", json={"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["status"] == RolloutStageStatus.IN_PROGRESS.value
    edited = _stored(la_factory, first, "updated_at")
    assert edited > datetime(2020, 1, 2, 0, 1), edited
    edited_utc = edited.replace(tzinfo=timezone.utc)

    # Completion (its 1 h duration) is measured from the rename ...
    session = la_factory()
    try:
        stage = session.get(RolloutStage, uuid.UUID(first))
        completes = RolloutScheduler()._is_stage_eligible_for_completion
        assert not completes(stage, edited_utc + timedelta(minutes=30))
        assert completes(stage, edited_utc + timedelta(hours=1))
    finally:
        session.close()
    # ... and so is min_stage_duration (24 h).
    assert (
        _tick(la_factory, schedule["id"], edited_utc + timedelta(hours=2), keep=False)
        == 0
    )
    assert (
        _tick(
            la_factory,
            schedule["id"],
            edited_utc + timedelta(hours=24, minutes=1),
            keep=False,
        )
        == 1
    )

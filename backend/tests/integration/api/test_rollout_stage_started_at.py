"""A rollout stage's clock starts when the stage starts, and an edit does not restart it (#1528).

The scheduler measured a time-based stage's duration
(``trigger_configuration.duration``, 24 hours by default) and the schedule's
``min_stage_duration`` from the stage's ``updated_at``. Every edit moves that
column -- a rename included, and a neighbour's ``stage_order`` shift -- so an
edit restarted the stage's clock. The stage now records ``started_at`` once,
when it goes from pending to in progress, and the scheduler measures from it.

Requests are real ones (a DEVELOPER with a local JWT, ``rollout_rules``); the
scheduler's pass is ``RolloutScheduler._process_schedule`` at a fixed instant,
narrowed to the test's schedule. Each property has a positive control, a tick
one second before the stage is due that must leave it in progress, so a test
that completes nothing cannot pass.

- 2a: renaming a stage in progress leaves its completion time;
- 2b: editing it leaves the ``min_stage_duration`` clock;
- 2c: a ``stage_order`` shift of the stage (which moves ``updated_at``; no
  route does it to a started stage since #1536, so the ORM does it here)
  leaves its completion time;
- 2d: each of the three start transitions records the start;
- 2h: a stage an older release started (no ``started_at``) completes from its
  ``updated_at``, as before;
- 2i: the recorded start wins over ``updated_at``, both ways;
- 2l: a request that sends ``started_at`` does not set it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import text

from backend.app.core.rollout_scheduler import RolloutScheduler
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutStage
from backend.tests.integration.api.rollout_rules import (
    SCHEDULES,
    client,
    create,
    developer,
    fresh,
    local_auth,
    new_flag,
    ok,
    rollout_invariants,
    send,
    session_factory,
    stage,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.usefixtures("local_auth", "rollout_invariants"),
]

#: The instant a tick started the stage: 50 minutes ago, so that an edit made
#: now falls inside the stage's one-hour duration. A reader of ``updated_at``
#: would then complete the stage an hour after the edit, ten minutes after it
#: is due.
T0 = (datetime.now(timezone.utc) - timedelta(minutes=50)).replace(microsecond=0)
ONE_HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _tick(session_factory, schedule_id: Any, at: datetime) -> int:
    """One scheduler pass over this schedule at ``at``; returns stages started."""
    session = session_factory()
    try:
        schedule = session.get(RolloutSchedule, uuid.UUID(str(schedule_id)))
        _, transitions, _ = asyncio.run(
            RolloutScheduler()._process_schedule(session, schedule, at)
        )
        session.commit()
        return transitions
    finally:
        session.close()


def _row(fresh, stage_id: Any) -> Dict[str, Any]:
    """The stored stage: status, updated_at, completed_date.

    Not ``started_at``: the regression tests (2a, 2b, 2c) read the clock
    first and the column after, so that on the code before #1528, which has no
    column, they fail on the clock and say so."""
    return fresh(
        lambda session: dict(
            session.execute(
                text(
                    "SELECT status, updated_at, completed_date "
                    "FROM rollout_stages WHERE id = :id"
                ),
                {"id": str(stage_id)},
            )
            .mappings()
            .one()
        )
    )


def _started(fresh, stage_id: Any) -> Optional[datetime]:
    """The stored ``started_at``."""
    return fresh(
        lambda session: session.execute(
            text("SELECT started_at FROM rollout_stages WHERE id = :id"),
            {"id": str(stage_id)},
        ).scalar_one()
    )


def _set(fresh, stage_id: Any, **columns: Optional[datetime]) -> None:
    """Write stage columns with SQL, as another release or an old row has them."""
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    fresh(
        lambda session: session.execute(
            text(f"UPDATE rollout_stages SET {assignments} WHERE id = :id"),
            {"id": str(stage_id), **columns},
        )
    )


def _started_one_hour_stage(client, developer, new_flag, session_factory, fresh):
    """A schedule whose first stage (time-based, one hour) a tick started at T0.

    The second stage is manual, so completing the first starts nothing.
    """
    created = create(
        client,
        developer,
        new_flag(),
        [
            stage(
                1, 10, "time_based", name="timed", trigger_configuration={"duration": 1}
            ),
            stage(2, 50, name="manual"),
        ],
    )
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
    assert _tick(session_factory, created["id"], T0) == 1
    timed = created["ids"]["timed"]
    row = _row(fresh, timed)
    assert (row["status"], row["updated_at"]) == ("IN_PROGRESS", _naive(T0))
    return created, timed


def _completes_at_exactly(session_factory, fresh, schedule_id, stage_id, due):
    """In progress one second before ``due``; completed at the tick after it."""
    _tick(session_factory, schedule_id, due - SECOND)
    assert _row(fresh, stage_id)["status"] == "IN_PROGRESS", (
        "completed before it was due"
    )
    _tick(session_factory, schedule_id, due + SECOND)
    row = _row(fresh, stage_id)
    assert row["status"] == "COMPLETED", (
        f"the edit restarted the clock: still {row['status']} at its due time + 1 s "
        f"(due {_naive(due)}, updated_at {row['updated_at']})"
    )
    assert row["completed_date"] == _naive(due + SECOND)


# ---------------------------------------------------------------------------
# 2a, 2b, 2c: an edit does not move the start
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_renaming_an_in_progress_stage_keeps_its_completion_time(
    client, developer, new_flag, session_factory, fresh
):
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )

    renamed = ok(
        send(
            client, "PUT", f"{SCHEDULES}/stages/{timed}", developer, {"name": "renamed"}
        )
    )
    assert renamed["status"] == "in_progress"
    row = _row(fresh, timed)
    assert row["updated_at"] > _naive(T0) + timedelta(minutes=40), row  # it moved

    _completes_at_exactly(session_factory, fresh, created["id"], timed, T0 + ONE_HOUR)
    # The rename left the recorded start, and the response showed it.
    assert _started(fresh, timed) == _naive(T0)
    assert renamed["started_at"] == _naive(T0).isoformat()


@pytest.mark.regression
def test_editing_an_in_progress_stage_keeps_its_min_stage_duration_clock(
    client, developer, new_flag, session_factory, fresh
):
    """``min_stage_duration`` (rollout_scheduler.py, the wait before the next
    stage) is measured from the start too: duration 0, minimum one hour."""
    created = create(
        client,
        developer,
        new_flag(),
        [
            stage(
                1, 10, "time_based", name="first", trigger_configuration={"duration": 0}
            ),
            stage(2, 50, "time_based", name="second"),
        ],
        min_stage_duration=1,
    )
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
    assert _tick(session_factory, created["id"], T0) == 1
    first, second = created["ids"]["first"], created["ids"]["second"]

    ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/stages/{first}",
            developer,
            {"description": "edited while in progress"},
        )
    )
    assert _row(fresh, first)["updated_at"] > _naive(T0) + timedelta(minutes=40)

    assert _tick(session_factory, created["id"], T0 + ONE_HOUR - SECOND) == 0
    assert _row(fresh, first)["status"] == "IN_PROGRESS"
    started = _tick(session_factory, created["id"], T0 + ONE_HOUR + SECOND)
    assert (started, _row(fresh, first)["status"], _row(fresh, second)["status"]) == (
        1,
        "COMPLETED",
        "IN_PROGRESS",
    ), "the edit restarted the min_stage_duration clock"


@pytest.mark.regression
def test_a_stage_order_shift_keeps_its_completion_time(
    client, developer, new_flag, session_factory, fresh
):
    """The add-stage shift moved a started stage's ``stage_order`` (and so its
    ``updated_at``) until #1536 refused adding a stage before a started one.
    Any writer that shifts it -- an older release, a later route -- must not
    restart its clock, so the shift is made here as the old route made it."""
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )

    def shift(session):
        for row in (
            session.query(RolloutStage)
            .filter(RolloutStage.rollout_schedule_id == uuid.UUID(created["id"]))
            .all()
        ):
            row.stage_order += 1

    fresh(shift)
    row = _row(fresh, timed)
    assert row["updated_at"] > _naive(T0) + timedelta(minutes=40), row  # it moved

    _completes_at_exactly(session_factory, fresh, created["id"], timed, T0 + ONE_HOUR)
    assert _started(fresh, timed) == _naive(T0)


def test_changing_the_duration_moves_the_completion_time(
    client, developer, new_flag, session_factory, fresh
):
    """The documented way to lengthen or shorten a stage in progress: it then
    completes ``duration`` hours after its start, not after the edit."""
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )
    ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/stages/{timed}",
            developer,
            {"trigger_configuration": {"duration": 2}},
        )
    )
    assert _started(fresh, timed) == _naive(T0)

    _completes_at_exactly(
        session_factory, fresh, created["id"], timed, T0 + 2 * ONE_HOUR
    )


def test_a_pause_does_not_stop_the_clock(
    client, developer, new_flag, session_factory, fresh
):
    """Pausing and resuming leaves the start, so a stage whose duration passed
    while the schedule was paused completes on the first pass after the
    resume (pre-existing; the docs state it)."""
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )
    _tick(session_factory, created["id"], T0 + ONE_HOUR - SECOND)
    assert _row(fresh, timed)["status"] == "IN_PROGRESS"  # not due yet

    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/pause", developer))
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
    assert _started(fresh, timed) == _naive(T0)

    # Resumed after it was due: the first pass completes it.
    resumed = T0 + 2 * ONE_HOUR
    _tick(session_factory, created["id"], resumed)
    row = _row(fresh, timed)
    assert (row["status"], row["completed_date"]) == ("COMPLETED", _naive(resumed))


# ---------------------------------------------------------------------------
# 2d: every start transition records the start
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "site", ["scheduler", "advance-pending", "advance-completes-and-starts-next"]
)
def test_every_start_transition_records_the_start(
    client, developer, new_flag, session_factory, fresh, site
):
    if site == "scheduler":
        # A stage whose start_date passed three hours before the tick that
        # starts it (a stage started late): its start is the tick, not the
        # start_date (#1632 review F2), so its duration runs from the tick.
        late = T0 - timedelta(hours=3)
        created = create(
            client,
            developer,
            new_flag(),
            [
                stage(
                    1,
                    10,
                    "time_based",
                    name="timed",
                    start_date=late.isoformat(),
                    trigger_configuration={"duration": 1},
                ),
                stage(2, 50, name="manual"),
            ],
        )
        ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
        timed = created["ids"]["timed"]
        assert _started(fresh, timed) is None
        assert _tick(session_factory, created["id"], T0) == 1
        assert _row(fresh, timed)["status"] == "IN_PROGRESS"
        started_at = _started(fresh, timed)
        assert started_at == _naive(T0), (
            f"the start is {started_at}, not the tick {_naive(T0)} "
            f"(the start_date was {_naive(late)})"
        )
        return

    created = create(
        client,
        developer,
        new_flag(),
        [
            stage(1, 10, name="canary"),
            stage(2, 50, "time_based", name="next", start_date="2099-01-01T00:00:00Z"),
        ],
    )
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
    canary, nxt = created["ids"]["canary"], created["ids"]["next"]
    assert _started(fresh, canary) is None
    before = _naive(datetime.now(timezone.utc))
    advanced = ok(
        send(client, "POST", f"{SCHEDULES}/stages/{canary}/advance", developer)
    )
    after = _naive(datetime.now(timezone.utc))
    row, canary_start = _row(fresh, canary), _started(fresh, canary)
    assert row["status"] == "IN_PROGRESS"
    assert canary_start is not None, "the advanced stage records no start"
    assert before <= canary_start <= after
    assert canary_start == row["updated_at"]
    assert advanced["started_at"] == canary_start.isoformat()
    if site == "advance-pending":
        return

    # Advancing the manual stage in progress completes it and starts the next
    # one at once, whatever its trigger and start_date: its start is that
    # instant, the canary's completion.
    ok(send(client, "POST", f"{SCHEDULES}/stages/{canary}/advance", developer))
    done, started = _row(fresh, canary), _row(fresh, nxt)
    assert (done["status"], started["status"]) == ("COMPLETED", "IN_PROGRESS")
    next_start = _started(fresh, nxt)
    assert next_start is not None, "the next stage records no start"
    assert next_start == done["completed_date"]
    # The completed stage keeps its own start.
    assert _started(fresh, canary) == canary_start


# ---------------------------------------------------------------------------
# 2h, 2i: what the scheduler reads
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_stage_an_older_release_started_completes_from_its_updated_at(
    client, developer, new_flag, session_factory, fresh
):
    """During a rolling deploy an older replica can win a tick and start a
    stage. It does not know ``started_at``: it writes the status and
    ``updated_at`` only. This release then measures from ``updated_at``."""
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )
    _set(fresh, timed, started_at=None, updated_at=_naive(T0))

    _completes_at_exactly(session_factory, fresh, created["id"], timed, T0 + ONE_HOUR)


@pytest.mark.regression
@pytest.mark.parametrize("edit", [timedelta(minutes=30), -timedelta(hours=2)])
def test_the_recorded_start_wins_over_updated_at(
    client, developer, new_flag, session_factory, fresh, edit
):
    """``updated_at`` after the start (an edit) would complete the stage late;
    before it (a row moved back) would complete it early. Neither is read."""
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )
    _set(fresh, timed, updated_at=_naive(T0 + edit))

    _completes_at_exactly(session_factory, fresh, created["id"], timed, T0 + ONE_HOUR)


# ---------------------------------------------------------------------------
# 2l: a request cannot set it
# ---------------------------------------------------------------------------

SENT = "2001-01-01T00:00:00Z"


@pytest.mark.regression
def test_a_request_that_sends_started_at_does_not_set_it(
    client, developer, new_flag, session_factory, fresh
):
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )
    manual = created["ids"]["manual"]

    # PUT a stage in progress and a pending one: the key is ignored.
    for stage_id in (timed, manual):
        response = ok(
            send(
                client,
                "PUT",
                f"{SCHEDULES}/stages/{stage_id}",
                developer,
                {"description": "with a start", "started_at": SENT},
            )
        )
        assert response["description"] == "with a start"
    assert _started(fresh, timed) == _naive(T0)
    assert _started(fresh, manual) is None

    # A new stage, and a new schedule's stages, start with none.
    added = ok(
        send(
            client,
            "POST",
            f"{SCHEDULES}/{created['id']}/stages",
            developer,
            stage(3, 60, name="added", started_at=SENT),
        ),
        201,
    )
    assert added["started_at"] is None
    assert _started(fresh, added["id"]) is None
    other = create(
        client, developer, new_flag(), [stage(1, 10, name="new", started_at=SENT)]
    )
    assert other["stages"][0]["started_at"] is None
    assert _started(fresh, other["ids"]["new"]) is None


def test_a_schedule_read_shows_each_stage_s_start(
    client, developer, new_flag, session_factory, fresh
):
    created, timed = _started_one_hour_stage(
        client, developer, new_flag, session_factory, fresh
    )
    read = ok(send(client, "GET", f"{SCHEDULES}/{created['id']}", developer))
    starts = {s["name"]: s["started_at"] for s in read["stages"]}
    assert starts == {"timed": _naive(T0).isoformat(), "manual": None}
    listed = ok(
        send(
            client,
            "GET",
            f"{SCHEDULES}/?feature_flag_id={read['feature_flag_id']}",
            developer,
        )
    )
    (mine,) = [s for s in listed["items"] if s["id"] == created["id"]]
    assert {s["name"]: s["started_at"] for s in mine["stages"]} == starts

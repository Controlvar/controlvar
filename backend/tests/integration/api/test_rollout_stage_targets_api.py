"""Every change keeps a schedule's stage targets in order and within its
maximum, and a stage that has started keeps its target and its place
(#1460, #1536).

Measured on main before the change:

* create judged the targets in list order, so ``[{stage_order: 2, 10%},
  {stage_order: 1, 50%}]`` (50% and then 10%) was accepted;
* adding a stage and changing one checked neither the order of the targets nor
  ``max_percentage``, and ``PUT /{id}`` accepted a ``max_percentage`` below a
  stage's target;
* a completed stage could be edited, and an in-progress stage's target changed;
* a stage added (or moved) at or before a stage that had started shifted the
  started stages on, and the scheduler's next tick started it: the flag went
  from 50% to 5% (#1536);
* a tick that read the stages before a stage was added, and then waited for the
  flag lock, started the stage it had read, and the next tick moved the flag
  back (the race the principal engineer measured, PE-1536-7).

Each refusal answers 409 with a fixed ``detail`` built from stored values and
changes nothing. Every request is a real one by a non-superuser DEVELOPER.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text

from backend.app.schemas.rollout_schedule import RolloutStageCreate, TriggerType
from backend.app.services.rollout_service import RolloutService
from backend.tests.integration.api.rollout_rules import (
    SCHEDULES,
    client,
    create,
    developer,
    flag_percentage,
    fresh,
    local_auth,
    new_flag,
    ok,
    rollout_invariants,
    send,
    session_factory,
    stage,
    stage_rows,
    stage_status,
    tick,
    waiting_on,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.usefixtures("local_auth", "rollout_invariants"),
]

JOIN_TIMEOUT_S = 30
WAIT_FOR_BLOCK_S = 15

IN_PROGRESS_REFUSAL = (
    "target_percentage and stage_order cannot be changed on a stage that is "
    "in_progress; its name, description, trigger_type, trigger_configuration "
    "and start_date still can."
)
STARTED_REFUSAL = (
    "Stages that have started keep their place: stage_order 2 is in_progress, "
    "so a stage can only be added or moved after it."
)


def _detail(response, status: int = 409) -> str:
    assert response.status_code == status, response.text
    return response.json()["detail"]


def _schedules_of(fresh, flag_id) -> int:
    return fresh(
        lambda session: session.execute(
            text("SELECT count(*) FROM rollout_schedules WHERE feature_flag_id = :id"),
            {"id": str(flag_id)},
        ).scalar()
    )


def _abc():
    """Three manual stages: a 10%, b 50%, c 100%."""
    return [stage(1, 10, name="a"), stage(2, 50, name="b"), stage(3, 100, name="c")]


def _started(client, user, flag_id, stages):
    """Activate a schedule whose first stage is manual and advance it twice:
    stage 1 completed, stage 2 in progress."""
    created = create(client, user, flag_id, stages)
    first = created["stages"][0]["id"]
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", user))
    ok(send(client, "POST", f"{SCHEDULES}/stages/{first}/advance", user))
    ok(send(client, "POST", f"{SCHEDULES}/stages/{first}/advance", user))
    return created


def _put_stage(client, user, stage_id, body):
    return send(client, "PUT", f"{SCHEDULES}/stages/{stage_id}", user, body)


def _add_stage(client, user, schedule_id, body):
    return send(client, "POST", f"{SCHEDULES}/{schedule_id}/stages", user, body)


# ---------------------------------------------------------------------------
# Create: the targets in stage_order (gate 3a)
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_create_refuses_targets_that_decrease_in_stage_order_whatever_the_list_order(
    client, developer, new_flag, fresh
):
    # On main: 201, a schedule that runs 50% and then 10%.
    flag_id = new_flag()
    body = {
        "name": "backwards",
        "feature_flag_id": str(flag_id),
        "stages": [stage(2, 10), stage(1, 50)],
    }
    response = send(client, "POST", f"{SCHEDULES}/", developer, body)
    assert response.status_code == 422, response.text
    assert [(e["loc"], e["msg"]) for e in response.json()["detail"]] == [
        (
            ["body"],
            "Value error, Stage percentages must be non-decreasing in stage_order",
        )
    ]
    assert _schedules_of(fresh, flag_id) == 0


def test_create_accepts_rising_targets_listed_in_any_order(client, developer, new_flag):
    # Listed 50% then 10%, but stage_order 1 is 10% and stage_order 2 is 50%.
    created = create(client, developer, new_flag(), [stage(2, 50), stage(1, 10)])
    orders = sorted(
        (s["stage_order"], s["target_percentage"]) for s in created["stages"]
    )
    assert orders == [(1, 10), (2, 50)]


def test_create_refuses_a_target_above_max_percentage(client, developer, new_flag):
    body = {
        "name": "over",
        "feature_flag_id": str(new_flag()),
        "max_percentage": 50,
        "stages": [stage(1, 10), stage(2, 60)],
    }
    response = send(client, "POST", f"{SCHEDULES}/", developer, body)
    assert response.status_code == 422, response.text
    assert [e["msg"] for e in response.json()["detail"]] == [
        "Value error, Stage percentages cannot exceed the maximum of 50%"
    ]


# ---------------------------------------------------------------------------
# Add a stage: judged after the shift (gate 3b)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "added, detail",
    [
        (
            stage(1, 60, name="new"),
            "Stage targets must not decrease in stage_order: stage_order 1 "
            "targets 60% and stage_order 2 would target 50%.",
        ),
        (
            stage(2, 40, name="new"),
            "Stage targets must not decrease in stage_order: stage_order 1 "
            "targets 50% and stage_order 2 would target 40%.",
        ),
    ],
    ids=["60-before-50", "40-after-50"],
)
def test_adding_a_stage_that_breaks_the_order_answers_409(
    client, developer, new_flag, fresh, added, detail
):
    # On main: 201. A draft schedule, so no stage has started.
    created = create(client, developer, new_flag(), [stage(1, 50), stage(2, 100)])
    before = stage_rows(fresh, created["id"])
    response = _add_stage(client, developer, created["id"], added)
    assert _detail(response) == detail
    assert stage_rows(fresh, created["id"]) == before


@pytest.mark.regression
def test_adding_a_stage_above_max_percentage_answers_409(
    client, developer, new_flag, fresh
):
    created = create(
        client, developer, new_flag(), [stage(1, 50), stage(2, 80)], max_percentage=80
    )
    before = stage_rows(fresh, created["id"])
    response = _add_stage(client, developer, created["id"], stage(3, 90))
    assert _detail(response) == (
        "A stage's target cannot exceed the schedule's max_percentage: "
        "stage_order 3 would target 90% and max_percentage is 80%."
    )
    assert stage_rows(fresh, created["id"]) == before


def test_adding_a_stage_between_two_in_order_is_accepted(
    client, developer, new_flag, fresh
):
    created = create(
        client, developer, new_flag(), [stage(1, 50, name="a"), stage(2, 100, name="b")]
    )
    ok(_add_stage(client, developer, created["id"], stage(2, 75, name="new")), 201)
    assert [(n, o, p) for n, o, p, _ in stage_rows(fresh, created["id"])] == [
        ("a", 1, 50),
        ("new", 2, 75),
        ("b", 3, 100),
    ]


# ---------------------------------------------------------------------------
# Change a stage (gate 3c, PD-1)
# ---------------------------------------------------------------------------

STAGE_FIELDS = [
    {"name": "renamed"},
    {"description": "described"},
    {"trigger_type": "time_based"},
    {"trigger_configuration": {"duration": 2}},
    {"start_date": "2031-01-08T00:00:00Z"},
    {"target_percentage": 10},
    {"stage_order": 1},
]
FIELD_IDS = [next(iter(body)) for body in STAGE_FIELDS]


@pytest.mark.regression
@pytest.mark.parametrize("body", STAGE_FIELDS, ids=FIELD_IDS)
def test_a_completed_stage_refuses_every_change(
    client, developer, new_flag, fresh, body
):
    # On main: a completed stage was edited (200); stage_order answered 400.
    created = _started(client, developer, new_flag(), _abc())
    before = stage_rows(fresh, created["id"])
    response = _put_stage(client, developer, created["ids"]["a"], body)
    assert _detail(response) == "A stage that is completed cannot be changed."
    assert stage_rows(fresh, created["id"]) == before


def test_a_failed_stage_refuses_every_change(client, developer, new_flag, fresh):
    # No code sets FAILED today; a stored one is refused all the same.
    created = create(client, developer, new_flag(), _abc())
    fresh(
        lambda session: session.execute(
            text("UPDATE rollout_stages SET status = 'FAILED' WHERE id = :id"),
            {"id": created["ids"]["a"]},
        )
    )
    response = _put_stage(client, developer, created["ids"]["a"], {"name": "renamed"})
    assert _detail(response) == "A stage that is failed cannot be changed."
    assert stage_status(fresh, created["ids"]["a"]) == "FAILED"


@pytest.mark.regression
@pytest.mark.parametrize(
    "body",
    [
        {"target_percentage": 60},
        {"stage_order": 3},
        {"target_percentage": 50, "stage_order": 2},
    ],
    ids=["target_percentage", "stage_order", "both-unchanged-values"],
)
def test_a_stage_in_progress_keeps_its_target_and_its_order(
    client, developer, new_flag, fresh, body
):
    # On main: the target was changed (200), and stage_order answered 400.
    created = _started(client, developer, new_flag(), _abc())
    before = stage_rows(fresh, created["id"])
    response = _put_stage(client, developer, created["ids"]["b"], body)
    assert _detail(response) == IN_PROGRESS_REFUSAL
    assert stage_rows(fresh, created["id"]) == before


@pytest.mark.parametrize(
    "body, field, expected",
    [
        ({"trigger_type": "manual"}, "trigger_type", "manual"),
        ({"trigger_type": "time_based"}, "trigger_type", "time_based"),
        (
            {"trigger_configuration": {"duration": 2}},
            "trigger_configuration",
            {"duration": 2},
        ),
        ({"start_date": "2031-01-08T00:00:00Z"}, "start_date", "2031-01-08T00:00:00"),
        ({"name": "renamed"}, "name", "renamed"),
        ({"description": "described"}, "description", "described"),
    ],
    ids=[
        "trigger_type-manual",
        "trigger_type-time_based",
        "trigger_configuration",
        "start_date",
        "name",
        "description",
    ],
)
@pytest.mark.parametrize("which", ["b", "c"], ids=["in_progress", "pending"])
def test_a_stage_in_progress_or_pending_still_takes_its_trigger_start_date_name_and_description(
    client, developer, new_flag, which, body, field, expected
):
    # The StreamPulse story sends {"trigger_type": "manual"} to the stage in
    # progress (rollout_story.py ensure_manual): it must stay 200.
    created = _started(client, developer, new_flag(), _abc())
    changed = ok(_put_stage(client, developer, created["ids"][which], body))
    assert changed[field] == expected


@pytest.mark.regression
def test_a_pending_stage_whose_new_target_breaks_the_order_answers_409(
    client, developer, new_flag, fresh
):
    # Above every started stage (PE-1536-5), so this is the ordering check.
    created = _started(client, developer, new_flag(), _abc())
    before = stage_rows(fresh, created["id"])
    response = _put_stage(
        client, developer, created["ids"]["c"], {"target_percentage": 40}
    )
    assert _detail(response) == (
        "Stage targets must not decrease in stage_order: stage_order 2 targets "
        "50% and stage_order 3 would target 40%."
    )
    assert stage_rows(fresh, created["id"]) == before


@pytest.mark.regression
def test_a_pending_stage_whose_new_target_is_above_max_percentage_answers_409(
    client, developer, new_flag, fresh
):
    stages = [stage(1, 10, name="a"), stage(2, 50, name="b"), stage(3, 80, name="c")]
    created = create(client, developer, new_flag(), stages, max_percentage=80)
    before = stage_rows(fresh, created["id"])
    response = _put_stage(
        client, developer, created["ids"]["c"], {"target_percentage": 90}
    )
    assert _detail(response) == (
        "A stage's target cannot exceed the schedule's max_percentage: "
        "stage_order 3 would target 90% and max_percentage is 80%."
    )
    assert stage_rows(fresh, created["id"]) == before


@pytest.mark.regression
def test_moving_a_pending_stage_is_judged_after_the_move(
    client, developer, new_flag, fresh
):
    # A draft: nothing has started. Moving the 100% stage to the front would
    # run 100%, 10%, 50%.
    created = create(client, developer, new_flag(), _abc())
    before = stage_rows(fresh, created["id"])
    response = _put_stage(client, developer, created["ids"]["c"], {"stage_order": 1})
    assert _detail(response) == (
        "Stage targets must not decrease in stage_order: stage_order 1 targets "
        "100% and stage_order 2 would target 10%."
    )
    assert stage_rows(fresh, created["id"]) == before


def test_moving_a_pending_stage_that_keeps_the_order_is_accepted(
    client, developer, new_flag, fresh
):
    stages = [stage(1, 10, name="a"), stage(2, 10, name="b"), stage(3, 100, name="c")]
    created = create(client, developer, new_flag(), stages)
    ok(_put_stage(client, developer, created["ids"]["a"], {"stage_order": 2}))
    assert [(n, o) for n, o, _, _ in stage_rows(fresh, created["id"])] == [
        ("b", 1),
        ("a", 2),
        ("c", 3),
    ]


# ---------------------------------------------------------------------------
# PUT /{schedule_id}: max_percentage below a stage's target (gate 3d)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "new_max, detail",
    [
        (
            5,
            "max_percentage cannot be below a stage's target: "
            "stage_order 1 targets 25%.",
        ),
        (
            99,
            "max_percentage cannot be below a stage's target: "
            "stage_order 2 targets 100%.",
        ),
    ],
    ids=["5", "99"],
)
def test_lowering_max_percentage_below_a_target_answers_409(
    client, developer, new_flag, new_max, detail
):
    # On main: 200, a schedule whose 100% stage is above its maximum.
    created = create(client, developer, new_flag(), [stage(1, 25), stage(2, 100)])
    response = send(
        client,
        "PUT",
        f"{SCHEDULES}/{created['id']}",
        developer,
        {"max_percentage": new_max, "name": "renamed"},
    )
    assert _detail(response) == detail
    stored = ok(send(client, "GET", f"{SCHEDULES}/{created['id']}", developer))
    assert (stored["max_percentage"], stored["name"]) == (100, created["name"])


def test_a_max_percentage_at_the_highest_target_is_accepted(
    client, developer, new_flag
):
    created = create(client, developer, new_flag(), [stage(1, 25), stage(2, 75)])
    body = {"max_percentage": 75}
    changed = ok(send(client, "PUT", f"{SCHEDULES}/{created['id']}", developer, body))
    assert changed["max_percentage"] == 75


# ---------------------------------------------------------------------------
# No stage is added or moved at or before a started stage (#1536, gate 3r)
# ---------------------------------------------------------------------------


def _measured(client, user, flag_id, *extra):
    """PE-1536-1's setup: stage 1 MANUAL 10%, advanced twice; stage 2
    TIME_BASED 50% that a tick can complete (duration 0). The flag is at 50%."""
    b50 = stage(2, 50, "time_based", name="b50", trigger_configuration={"duration": 0})
    return _started(client, user, flag_id, [stage(1, 10, name="a10"), b50, *extra])


@pytest.mark.regression
def test_adding_a_stage_before_a_started_one_answers_409_and_the_flag_stays_at_50(
    client, developer, new_flag, fresh, session_factory
):
    # #1536 on main: 201, and the next tick started the 5% stage: (201, 5, ...).
    flag_id = new_flag()
    created = _measured(client, developer, flag_id)
    before = stage_rows(fresh, created["id"])
    response = _add_stage(
        client, developer, created["id"], stage(1, 5, "time_based", name="z5")
    )
    tick(session_factory, [created["id"]])

    assert (
        response.status_code,
        flag_percentage(fresh, flag_id),
        stage_status(fresh, created["ids"]["b50"]),
    ) == (409, 50, "COMPLETED")
    assert response.json()["detail"] == STARTED_REFUSAL
    assert [r for r in stage_rows(fresh, created["id"]) if r[0] != "b50"] == [
        r for r in before if r[0] != "b50"
    ]


@pytest.mark.regression
@pytest.mark.parametrize(
    "body",
    [{"stage_order": 1, "target_percentage": 5}, {"stage_order": 2}],
    ids=["to-1-at-5", "to-the-in-progress-order"],
)
def test_moving_a_pending_stage_before_a_started_one_answers_409(
    client, developer, new_flag, fresh, body
):
    # PE-1536-3: the 60% stage is MANUAL, so nothing starts it. Adding it
    # after every started stage is the positive control.
    created = _measured(client, developer, new_flag())
    added = ok(_add_stage(client, developer, created["id"], stage(3, 60)), 201)
    before = stage_rows(fresh, created["id"])
    response = _put_stage(client, developer, added["id"], body)
    assert _detail(response) == STARTED_REFUSAL
    assert stage_rows(fresh, created["id"]) == before


def test_with_no_stage_in_progress_the_highest_started_stage_counts(
    client, developer, new_flag, fresh, session_factory
):
    # The scheduler's other branch (no stage in progress) picks the lowest
    # pending stage too: stage 2 completed by a tick, stage 3 waits for a
    # start date years ahead.
    flag_id = new_flag()
    later = stage(3, 100, "time_based", name="c100", start_date="2031-01-08T00:00:00Z")
    created = _measured(client, developer, flag_id, later)
    tick(session_factory, [created["id"]])
    assert [r[3] for r in stage_rows(fresh, created["id"])] == [
        "COMPLETED",
        "COMPLETED",
        "PENDING",
    ]
    response = _add_stage(
        client, developer, created["id"], stage(1, 5, "time_based", name="z5")
    )
    assert _detail(response) == (
        "Stages that have started keep their place: stage_order 2 is completed, "
        "so a stage can only be added or moved after it."
    )
    tick(session_factory, [created["id"]])
    assert flag_percentage(fresh, flag_id) == 50


def test_a_stage_goes_anywhere_in_a_schedule_where_nothing_has_started(
    client, developer, new_flag
):
    # A draft, and a paused schedule whose stages are all pending.
    draft = create(client, developer, new_flag(), [stage(1, 10, name="a")])
    ok(_add_stage(client, developer, draft["id"], stage(1, 5)), 201)
    paused = create(client, developer, new_flag(), [stage(1, 10, name="a")])
    ok(send(client, "POST", f"{SCHEDULES}/{paused['id']}/activate", developer))
    ok(send(client, "POST", f"{SCHEDULES}/{paused['id']}/pause", developer))
    ok(_add_stage(client, developer, paused["id"], stage(1, 5)), 201)


def test_a_stage_goes_between_the_last_started_stage_and_the_next_pending_one(
    client, developer, new_flag, fresh
):
    created = _measured(client, developer, new_flag(), stage(3, 100, name="c100"))
    ok(_add_stage(client, developer, created["id"], stage(3, 75, name="n75")), 201)
    assert [(n, o) for n, o, _, _ in stage_rows(fresh, created["id"])] == [
        ("a10", 1),
        ("b50", 2),
        ("n75", 3),
        ("c100", 4),
    ]


# ---------------------------------------------------------------------------
# The tick reads the stages under the flag lock (PE-1536-7 (a))
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_tick_that_waited_for_the_flag_lock_starts_the_stage_added_meanwhile(
    client, developer, new_flag, fresh, session_factory
):
    """A stage is added at order 3, after the stage in progress, as the rule
    allows; it holds the locks while a tick waits for the flag lock. The tick
    must start the new 75% stage, and the next tick the 100% one: 50, 75, 100.

    Before, the tick read the stages before it waited, started the 100% stage
    it had read (which the add had moved to order 4), and the next tick fell
    back to the 75% stage: 50, 100, 75.
    """
    flag_id = new_flag()
    c100 = stage(
        3, 100, "time_based", name="c100", trigger_configuration={"duration": 0}
    )
    created = _measured(client, developer, flag_id, c100)
    schedule_id = uuid.UUID(created["id"])
    assert flag_percentage(fresh, flag_id) == 50

    ready, go = threading.Event(), threading.Event()
    outcome: dict = {}

    def add_stage():
        session = session_factory()
        try:
            outcome["pid"] = session.execute(text("SELECT pg_backend_pid()")).scalar()
            commit = session.commit

            def gated_commit():
                ready.set()
                go.wait(JOIN_TIMEOUT_S)
                commit()

            session.commit = gated_commit
            RolloutService.add_rollout_stage(
                session,
                schedule_id,
                RolloutStageCreate(
                    name="n75",
                    stage_order=3,
                    target_percentage=75,
                    trigger_type=TriggerType.TIME_BASED,
                    trigger_configuration={"duration": 0},
                ),
            )
        except BaseException as exc:  # reported to the test thread
            outcome["add_error"] = exc
        finally:
            ready.set()
            session.close()

    def first_tick():
        try:
            tick(session_factory, [schedule_id])
        except BaseException as exc:  # reported to the test thread
            outcome["tick_error"] = exc

    adder = threading.Thread(target=add_stage, daemon=True)
    ticker = threading.Thread(target=first_tick, daemon=True)
    blocked = False
    adder.start()
    try:
        assert ready.wait(JOIN_TIMEOUT_S), "the add never reached its commit"
        ticker.start()
        deadline = time.monotonic() + WAIT_FOR_BLOCK_S
        while time.monotonic() < deadline and ticker.is_alive():
            if waiting_on(session_factory, outcome["pid"]):
                blocked = True
                break
            time.sleep(0.02)
    finally:
        go.set()
        adder.join(JOIN_TIMEOUT_S)
        if ticker.ident is not None:
            ticker.join(JOIN_TIMEOUT_S)
    assert not adder.is_alive() and not ticker.is_alive(), "a thread did not finish"
    assert "add_error" not in outcome and "tick_error" not in outcome, outcome

    after_first = flag_percentage(fresh, flag_id)
    tick(session_factory, [schedule_id])
    after_second = flag_percentage(fresh, flag_id)

    assert (blocked, after_first, after_second) == (True, 75, 100)

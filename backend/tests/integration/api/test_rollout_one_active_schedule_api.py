"""A flag runs at most one ACTIVE rollout schedule (#1460).

On main, ``POST /api/v1/rollout-schedules/{schedule_id}/activate`` did not look
at the flag's other schedules, so two ACTIVE schedules could drive one flag's
rollout percentage. Activating a draft, or resuming a paused schedule, while
another schedule of the same flag is ACTIVE now answers 409 and changes
nothing. The check runs with the flag row locked (``FOR UPDATE``), so two
activations of the same flag's schedules take turns and the second sees the
first.

``PUT /api/v1/rollout-schedules/{schedule_id}`` refuses every ``status`` a
request sends today (its enum is not the model's; #1529's batch owns that
route's input). The rule is in its path all the same, behind that check; the
service-level test reaches it.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text

from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import RolloutScheduleStatus
from backend.app.schemas.rollout_schedule import RolloutScheduleUpdate
from backend.app.services.rollout_service import RolloutService
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
    schedule_statuses,
    send,
    session_factory,
    stage,
    waiting_on,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.usefixtures("local_auth", "rollout_invariants"),
]

JOIN_TIMEOUT_S = 30
WAIT_FOR_BLOCK_S = 15


def _refusal(other_id) -> str:
    return (
        f"Another rollout schedule of this flag is active ({other_id}). "
        "A flag runs one schedule at a time: pause or cancel that one first."
    )


def _two(client, user, flag_id):
    first = create(client, user, flag_id, [stage(1, 10), stage(2, 100)])
    second = create(client, user, flag_id, [stage(1, 20), stage(2, 100)])
    return first, second


@pytest.mark.regression
def test_activating_a_second_schedule_of_the_flag_answers_409(
    client, developer, new_flag, fresh
):
    # On main: 200, and two ACTIVE schedules on one flag.
    flag_id = new_flag()
    first, second = _two(client, developer, flag_id)
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer))

    response = send(client, "POST", f"{SCHEDULES}/{second['id']}/activate", developer)

    assert schedule_statuses(fresh, flag_id).count("ACTIVE") == 1
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _refusal(first["id"])
    assert sorted(schedule_statuses(fresh, flag_id)) == ["ACTIVE", "DRAFT"]


@pytest.mark.regression
def test_resuming_a_paused_schedule_while_another_is_active_answers_409(
    client, developer, new_flag, fresh
):
    flag_id = new_flag()
    first, second = _two(client, developer, flag_id)
    ok(send(client, "POST", f"{SCHEDULES}/{second['id']}/activate", developer))
    ok(send(client, "POST", f"{SCHEDULES}/{second['id']}/pause", developer))
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer))

    response = send(client, "POST", f"{SCHEDULES}/{second['id']}/activate", developer)

    assert schedule_statuses(fresh, flag_id).count("ACTIVE") == 1
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _refusal(first["id"])
    assert sorted(schedule_statuses(fresh, flag_id)) == ["ACTIVE", "PAUSED"]


@pytest.mark.parametrize("verb", ["pause", "cancel"])
def test_once_the_other_is_paused_or_cancelled_the_schedule_activates(
    client, developer, new_flag, fresh, verb
):
    flag_id = new_flag()
    first, second = _two(client, developer, flag_id)
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer))
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/{verb}", developer))

    activated = ok(
        send(client, "POST", f"{SCHEDULES}/{second['id']}/activate", developer)
    )

    assert activated["status"] == "active"
    assert schedule_statuses(fresh, flag_id).count("ACTIVE") == 1


def test_a_schedule_of_another_flag_does_not_count(client, developer, new_flag):
    first = create(client, developer, new_flag(), [stage(1, 10)])
    other = create(client, developer, new_flag(), [stage(1, 10)])
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer))
    assert (
        ok(send(client, "POST", f"{SCHEDULES}/{other['id']}/activate", developer))[
            "status"
        ]
        == "active"
    )


def test_put_still_refuses_every_status_change(client, developer, new_flag, fresh):
    # The request's status enum is not the model's, so every status a PUT
    # sends answers 400 (unchanged); /activate is the way to activate.
    flag_id = new_flag()
    first, second = _two(client, developer, flag_id)
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer))
    response = send(
        client, "PUT", f"{SCHEDULES}/{second['id']}", developer, {"status": "active"}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Invalid status transition from draft to active"
    assert sorted(schedule_statuses(fresh, flag_id)) == ["ACTIVE", "DRAFT"]


def test_the_update_route_refuses_a_second_active_schedule_once_its_status_is_read(
    client, developer, new_flag, fresh, session_factory
):
    # The status check above refuses the request's enum. Given the model's
    # own ACTIVE, the update reaches the rule and refuses it the same way.
    # Imported here, so that the module's other tests run on a tree without
    # the exception (the regression run on the merge base).
    from backend.app.services.rollout_service import RolloutRuleConflict

    flag_id = new_flag()
    first, second = _two(client, developer, flag_id)
    ok(send(client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer))
    session = session_factory()
    try:
        with pytest.raises(RolloutRuleConflict) as refused:
            RolloutService.update_rollout_schedule(
                session,
                uuid.UUID(second["id"]),
                RolloutScheduleUpdate.model_construct(
                    status=RolloutScheduleStatus.ACTIVE
                ),
            )
        session.rollback()
    finally:
        session.close()
    assert str(refused.value) == _refusal(first["id"])
    assert sorted(schedule_statuses(fresh, flag_id)) == ["ACTIVE", "DRAFT"]


@pytest.mark.regression
def test_two_activations_take_turns_on_the_flag_lock(
    client, developer, new_flag, fresh, session_factory
):
    """Gate 3g. Session B holds the flag ``FOR UPDATE``; A's ``/activate`` of
    one schedule starts in a thread and is seen waiting on B in
    ``pg_stat_activity``; B activates the flag's other schedule and commits;
    A then answers 409 and the flag has one ACTIVE schedule. The thread's join
    timeout is the failure detector, not a duration."""
    flag_id = new_flag()
    first, second = _two(client, developer, flag_id)

    holder = session_factory()
    outcome: dict = {}

    def activate_first():
        try:
            outcome["response"] = send(
                client, "POST", f"{SCHEDULES}/{first['id']}/activate", developer
            )
        except BaseException as exc:  # reported to the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=activate_first, daemon=True)
    blocked = False
    try:
        holder.query(FeatureFlag).filter(
            FeatureFlag.id == flag_id
        ).with_for_update().one()
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()
        worker.start()
        deadline = time.monotonic() + WAIT_FOR_BLOCK_S
        while time.monotonic() < deadline and worker.is_alive():
            if waiting_on(session_factory, holder_pid):
                blocked = True
                break
            time.sleep(0.02)
        if blocked:
            # Committed while A waits on the flag lock.
            RolloutService.activate_rollout_schedule(holder, uuid.UUID(second["id"]))
        else:
            holder.rollback()
    finally:
        if holder.in_transaction():
            holder.rollback()
        holder.close()
        if worker.ident is not None:
            worker.join(JOIN_TIMEOUT_S)

    assert blocked, "activate was not seen waiting on the flag lock"
    assert not worker.is_alive(), "the activation did not finish once the lock was free"
    assert "error" not in outcome, outcome
    response = outcome["response"]
    assert schedule_statuses(fresh, flag_id).count("ACTIVE") == 1
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _refusal(second["id"])

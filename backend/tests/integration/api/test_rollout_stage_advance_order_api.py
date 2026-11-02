"""Stages start in ``stage_order``, one at a time (#1460).

On main, ``POST /api/v1/rollout-schedules/stages/{stage_id}/advance`` started
any pending manual stage: advancing stage 1 and then stage 3 left two stages
in progress and the flag at stage 3's percentage. Advancing a pending stage
now answers 409 while another stage is in progress, or while an earlier stage
has not completed, and changes nothing. Advancing the manual stage in
progress still completes it and starts the next one.

The checks run in a fixed order (PD-2): the trigger type (400), the stage's
status (400), the schedule's status (400), then the locks and the schedule's
status again, then this rule (409). So a time-based stage still answers 400
with main's text, whatever the stages before it.
"""

from __future__ import annotations

from typing import List, Tuple

import pytest

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
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.usefixtures("local_auth", "rollout_invariants"),
]


def _three_manual(client, user, flag_id):
    created = create(
        client,
        user,
        flag_id,
        [stage(1, 10, name="s1"), stage(2, 20, name="s2"), stage(3, 30, name="s3")],
    )
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", user))
    return created


def _advance(client, user, created, name):
    return send(
        client, "POST", f"{SCHEDULES}/stages/{created['ids'][name]}/advance", user
    )


def _in_progress(fresh, created) -> List[str]:
    return [
        name
        for name, _, _, status in stage_rows(fresh, created["id"])
        if status == "IN_PROGRESS"
    ]


def _state(fresh, flag_id, created) -> Tuple[List[str], int]:
    return _in_progress(fresh, created), flag_percentage(fresh, flag_id)


@pytest.mark.regression
def test_a_later_stage_cannot_start_while_an_earlier_one_is_in_progress(
    client, developer, new_flag, fresh
):
    # On main: 200, two stages in progress and the flag at 30%.
    flag_id = new_flag()
    created = _three_manual(client, developer, flag_id)
    ok(_advance(client, developer, created, "s1"))

    response = _advance(client, developer, created, "s3")

    assert (response.status_code, *_state(fresh, flag_id, created)) == (
        409,
        ["s1"],
        10,
    )
    assert response.json()["detail"] == (
        "Stages start in stage_order, one at a time: stage_order 1 is "
        "in_progress, so this stage cannot start yet."
    )


@pytest.mark.regression
@pytest.mark.parametrize("name", ["s2", "s3"])
def test_a_stage_cannot_start_before_the_earlier_ones_have_completed(
    client, developer, new_flag, fresh, name
):
    flag_id = new_flag()
    created = _three_manual(client, developer, flag_id)

    response = _advance(client, developer, created, name)

    assert (response.status_code, *_state(fresh, flag_id, created)) == (409, [], 0)
    assert response.json()["detail"] == (
        "Stages start in stage_order, one at a time: stage_order 1 is pending, "
        "so this stage cannot start yet."
    )


def test_stages_advanced_in_order_run_to_the_end(client, developer, new_flag, fresh):
    flag_id = new_flag()
    created = _three_manual(client, developer, flag_id)
    seen = []
    for name in ["s1", "s1", "s2", "s3"]:
        ok(_advance(client, developer, created, name))
        seen.append(_state(fresh, flag_id, created))
    # Each advance of the stage in progress completes it and starts the next;
    # the last one completes the schedule.
    assert seen == [(["s1"], 10), (["s2"], 20), (["s3"], 30), ([], 30)]
    schedule = ok(send(client, "GET", f"{SCHEDULES}/{created['id']}", developer))
    assert schedule["status"] == "completed"


@pytest.mark.regression
def test_a_time_based_stage_still_answers_400_before_the_order_is_judged(
    client, developer, new_flag, fresh
):
    # PD-2: the trigger type is checked first. Stage 1 is in progress, so the
    # order rule would refuse stage 2 too; the answer is main's 400.
    flag_id = new_flag()
    created = create(
        client,
        developer,
        flag_id,
        [stage(1, 10, name="s1"), stage(2, 50, "time_based", name="s2")],
    )
    ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
    ok(_advance(client, developer, created, "s1"))

    response = _advance(client, developer, created, "s2")

    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Can only manually advance stages with manual trigger type"},
    )
    assert _state(fresh, flag_id, created) == (["s1"], 10)


def test_a_completed_stage_still_answers_400_before_the_order_is_judged(
    client, developer, new_flag, fresh
):
    flag_id = new_flag()
    created = _three_manual(client, developer, flag_id)
    ok(_advance(client, developer, created, "s1"))
    ok(_advance(client, developer, created, "s1"))

    response = _advance(client, developer, created, "s1")

    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Cannot advance stage in completed status"},
    )
    assert _state(fresh, flag_id, created) == (["s2"], 20)

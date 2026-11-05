"""A stage's ``trigger_configuration.duration`` is refused unless it is hours (#1535).

On the three routes that write a stage -- create's stages, add-stage and
``PUT /stages/{stage_id}`` -- a ``duration`` that is not a number from 0 to
8760 answers 422 at a ``loc`` ending ``trigger_configuration`` and nothing is
written. It used to be accepted (201 or 200): "abc", null, "1" and 1e20 left
the stage in progress for ever, -5 completed it at once and ``true`` read as
one hour. A stage stored with such a value still reads back, and a ``PUT``
with a good one corrects it. Every request is a real one (a local JWT;
``deps.get_db`` is the only override).
"""

from __future__ import annotations

import json
import math
import uuid

import pytest
from sqlalchemy import text

from backend.app.schemas.rollout_schedule import (
    DURATION_REFUSED,
    MAX_STAGE_DURATION_HOURS,
)
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

#: The values #1535 measured as accepted, and NaN (json.dumps writes ``NaN``,
#: which the API's JSON parser reads as a float).
BAD = [
    pytest.param("abc", id="abc"),
    pytest.param(None, id="null"),
    pytest.param("1", id="string-1"),
    pytest.param(True, id="true"),
    pytest.param(10**20, id="1e20"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(-5, id="-5"),
]
GOOD = [0, 24, MAX_STAGE_DURATION_HOURS]
PATHS = ["create", "add-stage", "put-stage"]
ACCEPTED_STATUS = {"create": 201, "add-stage": 201, "put-stage": 200}


def _timed(order: int, target: int, **configuration) -> dict:
    return stage(
        order,
        target,
        "time_based",
        name=f"timed {order}",
        trigger_configuration=configuration,
    )


def _stored(fresh, schedule_id) -> list:
    """``(name, trigger_configuration)`` of each stored stage, in order."""
    return fresh(
        lambda session: [
            tuple(row)
            for row in session.execute(
                text(
                    "SELECT name, trigger_configuration FROM rollout_stages "
                    "WHERE rollout_schedule_id = :id ORDER BY stage_order"
                ),
                {"id": str(schedule_id)},
            )
        ]
    )


def _schedules_of(fresh, flag_id) -> int:
    return fresh(
        lambda session: session.execute(
            text("SELECT count(*) FROM rollout_schedules WHERE feature_flag_id = :id"),
            {"id": str(flag_id)},
        ).scalar()
    )


def _write(path, client, user, flag_id, value):
    """Send ``duration=value`` down one write path.

    Returns ``(response, loc, written)``: the ``loc`` the refusal must carry,
    and a callable giving what is stored, to compare before and after.
    """
    if path == "create":
        body = {
            "name": f"duration {uuid.uuid4().hex[:8]}",
            "feature_flag_id": str(flag_id),
            "stages": [_timed(1, 10, duration=value)],
        }
        response = send(client, "POST", f"{SCHEDULES}/", user, body)
        return response, ["body", "stages", 0, "trigger_configuration"], None
    created = create(client, user, flag_id, [_timed(1, 10, duration=1)])
    if path == "add-stage":
        response = send(
            client,
            "POST",
            f"{SCHEDULES}/{created['id']}/stages",
            user,
            _timed(2, 50, duration=value),
        )
    else:
        response = send(
            client,
            "PUT",
            f"{SCHEDULES}/stages/{created['stages'][0]['id']}",
            user,
            {"trigger_configuration": {"duration": value}},
        )
    return response, ["body", "trigger_configuration"], created["id"]


@pytest.mark.regression
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("value", BAD)
def test_bad_duration_is_422_on_all_three_write_paths(
    client, developer, new_flag, fresh, path, value
):
    # Before: 201 (create, add-stage) or 200 (put-stage), and stored.
    flag_id = new_flag()
    response, loc, schedule_id = _write(path, client, developer, flag_id, value)
    assert response.status_code == 422, response.text
    expected = [{"type": "value_error", "loc": loc, "msg": DURATION_REFUSED}]
    if isinstance(value, float) and math.isnan(value):
        # Since #1630 a NaN in any JSON body is refused before the route, at
        # its own place in the body.
        expected = [
            {
                "type": "finite_number",
                "loc": [*loc, "duration"],
                "msg": "Input should be a finite number",
            }
        ]
    assert [
        {key: item[key] for key in ("type", "loc", "msg")}
        for item in response.json()["detail"]
    ] == expected
    if path == "create":
        assert _schedules_of(fresh, flag_id) == 0
    else:
        # The schedule as _write created it: one stage, duration 1.
        assert _stored(fresh, schedule_id) == [("timed 1", {"duration": 1})]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("value", GOOD)
def test_a_duration_from_0_to_8760_is_accepted_on_all_three_write_paths(
    client, developer, new_flag, path, value
):
    response, _, _ = _write(path, client, developer, new_flag(), value)
    assert response.status_code == ACCEPTED_STATUS[path], response.text


def test_a_stage_without_duration_keeps_the_default(client, developer, new_flag):
    created = create(client, developer, new_flag(), [_timed(1, 10, label="x")])
    assert created["stages"][0]["trigger_configuration"] == {"label": "x"}


@pytest.fixture
def stored_bad(client, developer, new_flag, fresh) -> dict:
    """A schedule whose one stage was stored with ``{"duration": "abc"}``."""
    flag_id = new_flag()
    created = create(client, developer, flag_id, [_timed(1, 10, duration=1)])
    fresh(
        lambda session: session.execute(
            text(
                "UPDATE rollout_stages SET trigger_configuration = "
                "CAST(:config AS jsonb) WHERE rollout_schedule_id = :id"
            ),
            {"config": json.dumps({"duration": "abc"}), "id": created["id"]},
        )
    )
    created["flag_id"] = flag_id
    return created


def test_a_stored_bad_duration_still_reads_back(client, developer, stored_bad):
    # Gate 1q: the check is on the request models only. On RolloutStageBase,
    # which the response inherits, GET one and GET list answered 500.
    one = ok(send(client, "GET", f"{SCHEDULES}/{stored_bad['id']}", developer))
    assert one["stages"][0]["trigger_configuration"] == {"duration": "abc"}
    listed = ok(
        send(
            client,
            "GET",
            f"{SCHEDULES}/?feature_flag_id={stored_bad['flag_id']}",
            developer,
        )
    )
    assert [
        s["trigger_configuration"] for item in listed["items"] for s in item["stages"]
    ] == [{"duration": "abc"}]


def test_a_put_with_a_good_duration_corrects_a_stored_bad_one(
    client, developer, stored_bad, fresh
):
    stage_id = stored_bad["stages"][0]["id"]
    updated = ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/stages/{stage_id}",
            developer,
            {"trigger_configuration": {"duration": 24}},
        )
    )
    assert updated["trigger_configuration"] == {"duration": 24}
    assert _stored(fresh, stored_bad["id"]) == [("timed 1", {"duration": 24})]

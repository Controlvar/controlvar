"""A body date-time the database cannot store answers 422, not 500 (#956).

PostgreSQL stores a UTC offset of at most 15:59:59 either way. Three request
fields sent with a larger offset reached the database and answered 500: an API
key's ``expires_at`` (``POST /api/v1/api-keys``) and a rollout schedule's
``start_date`` and ``end_date`` (``POST /api/v1/rollout-schedules/`` and
``PUT /api/v1/rollout-schedules/{schedule_id}``). Each is now refused by the
request model with 422 at ``["body", <field>]``, the same fixed message as the
query parameters (``backend.app.api.datetime_bounds``), and nothing is written.
Offsets up to 15:59 either way, ``Z`` and a date with no offset are accepted as
before. A direct grant's ``expires_at`` was fixed by #1827 and is pinned in
``modules/backend/tests/integration/api/test_utc_offset_refused_modules.py``.

Refused, never normalised (#1561). A field that already accepts any offset and
stores the instant keeps doing so: a rollout stage's ``start_date`` here.

Every request is a real one on PostgreSQL (a local JWT; ``deps.get_db``, the
one these routes read, is overridden).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.models.user import User
from backend.tests.integration.api.rollout_rules import (
    SCHEDULES,
    client,
    create,
    developer,
    fresh,
    local_auth,
    new_flag,
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

OFFSET_MSG = "Value error, the UTC offset must be less than 16 hours"

REFUSED = {
    "+23:00": "2030-01-01T00:00:00+23:00",
    "+16:00": "2030-01-01T00:00:00+16:00",
    "-16:00": "2030-01-01T00:00:00-16:00",
}
ACCEPTED = {
    "+15:59": "2030-01-01T00:00:00+15:59",
    "-15:59": "2030-01-01T00:00:00-15:59",
    "Z": "2030-01-01T00:00:00Z",
    "naive": "2030-01-01T00:00:00",
}


def _refused(resp, field: str, sent: str) -> None:
    assert resp.status_code == 422, (
        f"{field}: expected 422, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json()["detail"]
    assert [(d["type"], d["loc"], d["msg"]) for d in detail] == [
        ("value_error", ["body", field], OFFSET_MSG)
    ], detail
    assert sent not in resp.text
    assert sent[-6:] not in resp.text


def _count(fresh, sql: str, **params) -> int:
    return fresh(lambda session: session.execute(text(sql), params).scalar())


def _api_keys_of(fresh, user: User) -> int:
    return _count(
        fresh, "SELECT count(*) FROM api_keys WHERE user_id = :id", id=user.id
    )


def _post_key(client, user: User, sent: str):
    body = {"name": f"offset {uuid.uuid4().hex[:8]}", "expires_at": sent}
    return send(client, "POST", "/api/v1/api-keys", user, body)


@pytest.mark.regression
@pytest.mark.parametrize("sent", list(REFUSED.values()), ids=list(REFUSED))
def test_an_api_key_expiry_offset_of_16_hours_or_more_answers_422(
    client, developer, fresh, sent
):
    before = _api_keys_of(fresh, developer)
    _refused(_post_key(client, developer, sent), "expires_at", sent)
    assert _api_keys_of(fresh, developer) == before


@pytest.mark.parametrize("sent", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_an_api_key_expiry_under_16_hours_is_accepted(client, developer, fresh, sent):
    before = _api_keys_of(fresh, developer)
    resp = _post_key(client, developer, sent)
    assert resp.status_code == 201, resp.text
    assert _api_keys_of(fresh, developer) == before + 1


def _schedules_of(fresh, flag_id) -> int:
    return _count(
        fresh,
        "SELECT count(*) FROM rollout_schedules WHERE feature_flag_id = :id",
        id=str(flag_id),
    )


def _schedule_row(fresh, schedule_id) -> tuple:
    return fresh(
        lambda session: tuple(
            session.execute(
                text(
                    "SELECT name, start_date, end_date, updated_at "
                    "FROM rollout_schedules WHERE id = :id"
                ),
                {"id": str(schedule_id)},
            ).one()
        )
    )


def _post_schedule(client, user: User, flag_id, **extra):
    body = {
        "name": f"offset {uuid.uuid4().hex[:8]}",
        "feature_flag_id": str(flag_id),
        "stages": [stage(1, 10), stage(2, 50)],
    }
    body.update(extra)
    return send(client, "POST", f"{SCHEDULES}/", user, body)


@pytest.mark.regression
@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("sent", list(REFUSED.values()), ids=list(REFUSED))
def test_a_schedule_date_offset_of_16_hours_or_more_is_refused_on_create(
    client, developer, new_flag, fresh, field, sent
):
    flag_id = new_flag()
    _refused(_post_schedule(client, developer, flag_id, **{field: sent}), field, sent)
    assert _schedules_of(fresh, flag_id) == 0


@pytest.mark.regression
@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("sent", list(REFUSED.values()), ids=list(REFUSED))
def test_a_schedule_date_offset_of_16_hours_or_more_is_refused_on_update(
    client, developer, new_flag, fresh, field, sent
):
    schedule = create(client, developer, new_flag(), [stage(1, 10), stage(2, 50)])
    before = _schedule_row(fresh, schedule["id"])
    resp = send(
        client, "PUT", f"{SCHEDULES}/{schedule['id']}", developer, {field: sent}
    )
    _refused(resp, field, sent)
    assert _schedule_row(fresh, schedule["id"]) == before


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("sent", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_a_schedule_date_under_16_hours_is_accepted_on_create_and_update(
    client, developer, new_flag, fresh, field, sent
):
    flag_id = new_flag()
    created = _post_schedule(client, developer, flag_id, **{field: sent})
    assert created.status_code == 201, created.text
    schedule = create(client, developer, new_flag(), [stage(1, 10), stage(2, 50)])
    resp = send(
        client, "PUT", f"{SCHEDULES}/{schedule['id']}", developer, {field: sent}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("sent", list(REFUSED.values()), ids=list(REFUSED))
def test_a_stage_start_date_past_15_59_is_still_accepted(
    client, developer, new_flag, sent
):
    """Not changed by #956: a stage's ``start_date`` is stored as the instant."""
    created = _post_schedule(
        client,
        developer,
        new_flag(),
        stages=[stage(1, 10, start_date=sent), stage(2, 50)],
    )
    assert created.status_code == 201, f"expected 201, got {created.status_code}"
    added = send(
        client,
        "POST",
        f"{SCHEDULES}/{created.json()['id']}/stages",
        developer,
        stage(3, 60, start_date=sent),
    )
    assert added.status_code == 201, f"expected 201, got {added.status_code}"

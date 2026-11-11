"""An explicit ``null`` for a required rollout field answers 422, not 500.

``PUT /api/v1/rollout-schedules/{id}`` and ``PUT /stages/{id}`` take every
field as optional so a request can send only what it changes. An explicit
``null`` is not the same as leaving the field out: it was applied to a NOT
NULL column, the database refused it, and the route answered 500. The update
models now refuse a ``null`` for the fields that have no "cleared" state and
name the field; leaving the field out, or sending a value, is unchanged.

Every request is a real one by a non-superuser DEVELOPER (a local JWT;
``deps.get_db`` is the only override), and the schedule and stages are made
through the API.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

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

SCHEDULE_FIELDS = ["name", "max_percentage", "status"]
STAGE_FIELDS = ["name", "stage_order", "target_percentage", "trigger_type"]


@pytest.fixture
def schedule(client, developer, new_flag) -> dict:
    return create(client, developer, new_flag(), [stage(1, 10), stage(2, 50)])


def _schedule_row(fresh, schedule_id) -> dict:
    return fresh(
        lambda session: dict(
            session.execute(
                text(
                    "SELECT name, max_percentage, status "
                    "FROM rollout_schedules WHERE id = :id"
                ),
                {"id": str(schedule_id)},
            )
            .mappings()
            .one()
        )
    )


def _stage_row(fresh, stage_id) -> dict:
    return fresh(
        lambda session: dict(
            session.execute(
                text(
                    "SELECT name, stage_order, target_percentage, trigger_type "
                    "FROM rollout_stages WHERE id = :id"
                ),
                {"id": str(stage_id)},
            )
            .mappings()
            .one()
        )
    )


def _refused_naming(response, field: str) -> None:
    assert response.status_code == 422, response.text
    items = response.json()["detail"]
    assert any(item["loc"][-1] == field and field in item["msg"] for item in items), (
        items
    )


@pytest.mark.regression
@pytest.mark.parametrize("field", SCHEDULE_FIELDS)
def test_a_schedule_update_with_null_for_a_required_field_is_422(
    client, developer, schedule, fresh, field
):
    # Before: 500 {"detail":"An error occurred while updating the rollout
    # schedule"} from the database's NOT NULL refusal.
    before = _schedule_row(fresh, schedule["id"])

    response = send(
        client, "PUT", f"{SCHEDULES}/{schedule['id']}", developer, {field: None}
    )

    _refused_naming(response, field)
    assert _schedule_row(fresh, schedule["id"]) == before


@pytest.mark.regression
@pytest.mark.parametrize("field", STAGE_FIELDS)
def test_a_stage_update_with_null_for_a_required_field_is_422(
    client, developer, schedule, fresh, field
):
    stage_id = schedule["ids"]["stage 1"]
    before = _stage_row(fresh, stage_id)

    response = send(
        client, "PUT", f"{SCHEDULES}/stages/{stage_id}", developer, {field: None}
    )

    _refused_naming(response, field)
    assert _stage_row(fresh, stage_id) == before


@pytest.mark.regression
def test_the_refusal_names_every_null_field_sent(client, developer, schedule):
    response = send(
        client,
        "PUT",
        f"{SCHEDULES}/{schedule['id']}",
        developer,
        {"name": None, "max_percentage": None},
    )

    assert response.status_code == 422, response.text
    named = {item["loc"][-1] for item in response.json()["detail"]}
    assert {"name", "max_percentage"} <= named


def test_a_schedule_update_that_leaves_the_fields_out_is_unchanged(
    client, developer, schedule, fresh
):
    before = _schedule_row(fresh, schedule["id"])

    body = ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/{schedule['id']}",
            developer,
            {"description": "only the description"},
        )
    )

    assert body["description"] == "only the description"
    assert _schedule_row(fresh, schedule["id"]) == before


def test_a_schedule_update_with_values_is_unchanged(client, developer, schedule, fresh):
    ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/{schedule['id']}",
            developer,
            {"name": "renamed", "max_percentage": 90},
        )
    )

    row = _schedule_row(fresh, schedule["id"])
    assert (row["name"], row["max_percentage"]) == ("renamed", 90)


def test_a_schedule_update_may_still_clear_an_optional_field(
    client, developer, schedule
):
    """``null`` stays meaningful where the field can be cleared."""
    ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/{schedule['id']}",
            developer,
            {"min_stage_duration": None, "description": None},
        )
    )


def test_a_stage_update_that_leaves_the_fields_out_is_unchanged(
    client, developer, schedule, fresh
):
    stage_id = schedule["ids"]["stage 1"]
    before = _stage_row(fresh, stage_id)

    ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/stages/{stage_id}",
            developer,
            {"description": "only the description"},
        )
    )

    assert _stage_row(fresh, stage_id) == before


def test_a_stage_update_with_values_is_unchanged(client, developer, schedule, fresh):
    stage_id = schedule["ids"]["stage 1"]

    ok(
        send(
            client,
            "PUT",
            f"{SCHEDULES}/stages/{stage_id}",
            developer,
            {"name": "renamed", "target_percentage": 20, "trigger_type": "manual"},
        )
    )

    row = _stage_row(fresh, stage_id)
    assert (row["name"], row["target_percentage"]) == ("renamed", 20)

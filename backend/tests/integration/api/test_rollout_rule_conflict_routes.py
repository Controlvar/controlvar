"""``RolloutRuleConflict`` answers 409 on every rollout route that judges a rule (#1460).

The rollout service raises ``RolloutRuleConflict`` when a change breaks a
schedule's rules as they are stored now. It is a ``ValueError``, so a route
without its own ``except`` would still refuse the change, with 400; each of the
six routes maps it to 409 in an ``except`` placed before ``except ValueError``.
Here the service method each route calls raises one, and the route must answer
409 with the exception's text as the ``detail``, a plain string; a plain
``ValueError`` from the same method still answers 400. The texts themselves
are pinned by the rule tests of each route.
"""

from __future__ import annotations

import pytest

from backend.app.services.rollout_service import RolloutRuleConflict, RolloutService
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

TEXT = "a refusal the service wrote"

#: (route, the service method it calls, how to build the request)
ROUTES = [
    ("POST /", "create_rollout_schedule"),
    ("PUT /{schedule_id}", "update_rollout_schedule"),
    ("POST /{schedule_id}/activate", "activate_rollout_schedule"),
    ("POST /{schedule_id}/stages", "add_rollout_stage"),
    ("PUT /stages/{stage_id}", "update_rollout_stage"),
    ("POST /stages/{stage_id}/advance", "manually_advance_stage"),
]


def _request(client, user, flag_id, created, route):
    schedule_id = created["id"]
    stage_id = created["stages"][0]["id"]
    if route == "POST /":
        body = {
            "name": "mapping",
            "feature_flag_id": str(flag_id),
            "stages": [stage(1, 10)],
        }
        return send(client, "POST", f"{SCHEDULES}/", user, body)
    if route == "PUT /{schedule_id}":
        return send(client, "PUT", f"{SCHEDULES}/{schedule_id}", user, {"name": "x"})
    if route == "POST /{schedule_id}/activate":
        return send(client, "POST", f"{SCHEDULES}/{schedule_id}/activate", user)
    if route == "POST /{schedule_id}/stages":
        return send(
            client, "POST", f"{SCHEDULES}/{schedule_id}/stages", user, stage(2, 50)
        )
    if route == "PUT /stages/{stage_id}":
        return send(
            client, "PUT", f"{SCHEDULES}/stages/{stage_id}", user, {"name": "x"}
        )
    return send(client, "POST", f"{SCHEDULES}/stages/{stage_id}/advance", user)


@pytest.mark.parametrize(
    "error, status",
    [(RolloutRuleConflict(TEXT), 409), (ValueError(TEXT), 400)],
    ids=["RolloutRuleConflict", "ValueError"],
)
@pytest.mark.parametrize("route, method", ROUTES, ids=[r for r, _ in ROUTES])
def test_a_rule_conflict_answers_409_and_a_value_error_400(
    client, developer, new_flag, monkeypatch, route, method, error, status
):
    flag_id = new_flag()
    created = create(client, developer, flag_id, [stage(1, 10)])

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(RolloutService, method, staticmethod(refuse))

    response = _request(client, developer, flag_id, created, route)

    assert (response.status_code, response.json()) == (status, {"detail": TEXT})

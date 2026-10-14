"""What a workspace role grants, and what it does not decide.

Through the workspace routes, against a real database, with the callers of
``test_workspaces_api.py`` (none a platform superuser):

* a DEVELOPER, an ANALYST and a VIEWER of a workspace get the same answer on
  every workspace route: they read the workspace and its member list, change
  nothing, and leave;
* an invitation cannot grant the OWNER role, whoever sends it;
* workspaces do not decide which experiments, feature flags or API keys a user
  can see or change: the platform role does. A workspace OWNER who is a
  platform VIEWER cannot create a flag or an experiment; a platform DEVELOPER
  in no workspace can; every caller lists the same flags and experiments.
"""

from __future__ import annotations

import uuid

import pytest

from modules.backend.tests.integration.api.test_workspaces_api import (
    _new_workspace,
    as_user,
    ws_people,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

WS = "/api/v1/workspaces"


def _add(as_user, owner, ws_id, user, role):
    resp = as_user(
        owner,
        "POST",
        f"{WS}/{ws_id}/members",
        json={"user_id": str(user.id), "role": role},
    )
    assert resp.status_code == 201, resp.text


def _answers(as_user, member, ws_id, target):
    """The status of each workspace route for ``member``."""
    suffix = uuid.uuid4().hex[:6]
    return {
        "get": as_user(member, "GET", f"{WS}/{ws_id}").status_code,
        "members": as_user(member, "GET", f"{WS}/{ws_id}/members").status_code,
        "update": as_user(
            member, "PUT", f"{WS}/{ws_id}", json={"name": f"Renamed {suffix}"}
        ).status_code,
        "add": as_user(
            member,
            "POST",
            f"{WS}/{ws_id}/members",
            json={"user_id": str(target.id), "role": "VIEWER"},
        ).status_code,
        "role": as_user(
            member, "PUT", f"{WS}/{ws_id}/members/{target.id}", json={"role": "ADMIN"}
        ).status_code,
        "remove": as_user(
            member, "DELETE", f"{WS}/{ws_id}/members/{target.id}"
        ).status_code,
        "invite": as_user(
            member,
            "POST",
            f"{WS}/{ws_id}/invites",
            json={"email": f"invitee-{suffix}@example.com", "role": "VIEWER"},
        ).status_code,
        "delete": as_user(member, "DELETE", f"{WS}/{ws_id}").status_code,
        "leave": as_user(
            member, "DELETE", f"{WS}/{ws_id}/members/{member.id}"
        ).status_code,
    }


def test_developer_analyst_and_viewer_get_the_same_answers(as_user, ws_people):
    owner = ws_people["owner"]
    ws_id = _new_workspace(as_user, owner, "same")
    target = ws_people["wsadmin"]
    _add(as_user, owner, ws_id, target, "VIEWER")
    members = {
        "DEVELOPER": ws_people["owner2"],
        "ANALYST": ws_people["outsider"],
        "VIEWER": ws_people["bystander"],
    }
    for role, member in members.items():
        _add(as_user, owner, ws_id, member, role)
    answers = {
        role: _answers(as_user, member, ws_id, target)
        for role, member in members.items()
    }
    expected = {
        "get": 200,
        "members": 200,
        "update": 403,
        "add": 403,
        "role": 403,
        "remove": 403,
        "invite": 403,
        "delete": 403,
        "leave": 204,
    }
    assert answers == dict.fromkeys(("DEVELOPER", "ANALYST", "VIEWER"), expected)


@pytest.mark.parametrize("sender", ["owner", "wsadmin"])
def test_an_invitation_cannot_grant_owner(as_user, ws_people, sender):
    owner = ws_people["owner"]
    ws_id = _new_workspace(as_user, owner, "invite")
    _add(as_user, owner, ws_id, ws_people["wsadmin"], "ADMIN")
    resp = as_user(
        ws_people[sender],
        "POST",
        f"{WS}/{ws_id}/invites",
        json={"email": f"owner-{uuid.uuid4().hex[:6]}@example.com", "role": "OWNER"},
    )
    assert resp.status_code == 422, resp.text
    assert "OWNER cannot be invited" in resp.text
    accepted = as_user(
        ws_people[sender],
        "POST",
        f"{WS}/{ws_id}/invites",
        json={"email": f"admin-{uuid.uuid4().hex[:6]}@example.com", "role": "ADMIN"},
    )
    assert accepted.status_code == 201, accepted.text


def _flag(as_user, user):
    suffix = uuid.uuid4().hex[:8]
    return as_user(
        user,
        "POST",
        "/api/v1/feature-flags/",
        json={"key": f"ws-scope-{suffix}", "name": "Workspace scope"},
    )


def _experiment(as_user, user):
    suffix = uuid.uuid4().hex[:8]
    return as_user(
        user,
        "POST",
        "/api/v1/experiments/",
        json={
            "name": f"Workspace scope {suffix}",
            "hypothesis": "the platform role decides",
            "experiment_type": "a_b",
            "variants": [
                {"name": "control", "is_control": True, "traffic_allocation": 50},
                {"name": "treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {"name": "Buy", "event_name": "buy", "metric_type": "conversion"}
            ],
        },
    )


def test_the_platform_role_not_the_workspace_decides(as_user, ws_people):
    # A workspace OWNER who is a platform VIEWER.
    viewer_owner = ws_people["wsadmin"]
    ws_id = _new_workspace(as_user, viewer_owner, "scope")
    assert as_user(viewer_owner, "GET", f"{WS}/{ws_id}").status_code == 200
    assert _flag(as_user, viewer_owner).status_code == 403
    assert _experiment(as_user, viewer_owner).status_code == 403

    # A platform DEVELOPER in no workspace at all.
    loner = ws_people["owner2"]
    assert as_user(loner, "GET", f"{WS}/").json() == []
    flag = _flag(as_user, loner)
    assert flag.status_code in (200, 201), flag.text
    experiment = _experiment(as_user, loner)
    assert experiment.status_code in (200, 201), experiment.text
    key = as_user(loner, "POST", "/api/v1/api-keys", json={"name": "loner"})
    assert key.status_code == 201, key.text

    # Every caller, in a workspace or not, reads them and counts the same lists.
    totals = set()
    for user in (loner, viewer_owner, ws_people["outsider"], ws_people["owner"]):
        flag_read = as_user(user, "GET", f"/api/v1/feature-flags/{flag.json()['id']}")
        assert flag_read.status_code == 200, flag_read.text
        exp_read = as_user(
            user, "GET", f"/api/v1/experiments/{experiment.json()['id']}"
        )
        assert exp_read.status_code == 200, exp_read.text
        flags = as_user(user, "GET", "/api/v1/feature-flags/")
        experiments = as_user(user, "GET", "/api/v1/experiments/")
        assert flags.status_code == 200 and experiments.status_code == 200
        totals.add((flags.json()["total"], experiments.json()["total"]))
    assert len(totals) == 1, totals

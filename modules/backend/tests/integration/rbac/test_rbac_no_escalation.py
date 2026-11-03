"""A custom role or grant never reaches administration (#891, check #35).

A VIEWER holds every pair of the vocabulary (8 resources x 5 actions, the 40
a role or grant could store before the 422), activated, through a custom role
and through direct grants, planted by SQL; and, as only SQL could store them,
the five ``api_key`` pairs too. No check reads ``user``, ``role``,
``permission`` or ``report``, so without ``api_key`` a server that dropped the
``GRANTABLE`` filter would pass this test; with it, ``api-keys?all=true``
answers 200 (the plant that proves the gate). Through real sign-in, every route
that a grantable pair does not govern still answers exactly 403, each sent
with a valid body (FastAPI answers 422 before the endpoint's own check):

* RBAC administration, including a grant to oneself;
* other users' API keys (``?all=true``, deleting another user's key);
* the compliance audit trail (``audit_log (read)`` reads the audit log, not
  this);
* holdout and exclusion-group administration, the bandit override, the
  counter reset (ADMIN only, beside counter writes that ``experiment (update)``
  admits; its counter service is a stand-in, so nothing reaches DynamoDB),
  notifications administration, interaction analysis;
* the superuser-only routes (users, safety settings, results).

Positive control in the same test: ``POST /api/v1/experiments/`` answers 201
for the same user, so the grants are live.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.app.api.v1.endpoints.realtime_counters import get_counter_service
from modules.backend.tests.integration.rbac.rbac_states import assign, cleanup, grant

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "esc891"
RESOURCES = (
    "experiment",
    "feature_flag",
    "user",
    "role",
    "permission",
    "report",
    "audit_log",
    "export",
    # Not in the vocabulary: only SQL could store it. The filter must drop it.
    "api_key",
)
ACTIONS = ["create", "read", "update", "delete", "list"]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def counters():
    """A stand-in counter service, so the counter reset reaches no DynamoDB."""
    service = MagicMock()
    app.dependency_overrides[get_counter_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_counter_service, None)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    db_session.rollback()
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    ids = [u.id for u in users]
    if ids:
        db_session.execute(
            text("DELETE FROM api_keys WHERE user_id = ANY(:ids)"), {"ids": ids}
        )
        db_session.commit()
    cleanup(db_session, TAG, ids)


@pytest.fixture
def holder(db_session):
    """A VIEWER holding all forty pairs and api_key, through a role and grants."""
    user = make_user(db_session, UserRole.VIEWER, tag=TAG)
    role_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO custom_roles (id, name, is_system_role, permissions, "
            "created_at, updated_at) VALUES (:id, :name, false, "
            "CAST(:perms AS jsonb), now(), now())"
        ),
        {
            "id": role_id,
            "name": f"{TAG}-everything",
            "perms": json.dumps(
                [{"resource": r, "actions": ACTIONS} for r in RESOURCES]
            ),
        },
    )
    assign(db_session, user.id, role_id, activated=True)
    for resource in RESOURCES:
        grant(db_session, user.id, resource, ACTIONS)
    db_session.commit()
    return user


def test_all_forty_pairs_reach_no_administration(client, db_session, holder, counters):
    other = make_user(db_session, UserRole.DEVELOPER, tag=TAG)
    other_key, _ = APIKey.create_for_user(db_session, other.id, "other's key")
    experiment = Experiment(
        name=f"esc {uuid.uuid4().hex[:8]}",
        key=f"esc-{uuid.uuid4().hex[:10]}",
        status=ExperimentStatus.DRAFT,
        owner_id=other.id,
        hypothesis="no escalation",
    )
    group = MutualExclusionGroup(
        name=f"esc-{uuid.uuid4().hex[:8]}",
        description="no escalation",
        traffic_allocation=1.0,
        status=MutualExclusionGroupStatus.ACTIVE,
    )
    db_session.add_all([experiment, group])
    db_session.commit()
    me = str(holder.id)
    headers = auth(holder)

    # Positive control: the grants are live for this user.
    suffix = uuid.uuid4().hex[:8]
    resp = client.post(
        f"{V1}/experiments/",
        headers=headers,
        json={
            "name": f"esc {suffix}",
            "key": f"esc-create-{suffix}",
            "hypothesis": "control",
            "experiment_type": "a_b",
            "variants": [
                {"name": "control", "is_control": True, "traffic_allocation": 50},
                {"name": "b", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Purchase",
                    "event_name": f"purchase_{suffix}",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
    )
    assert resp.status_code == 201, f"positive control: {resp.text}"

    refused = [
        # RBAC administration, including a grant to oneself.
        ("POST", f"{V1}/rbac/roles", {"name": f"{TAG}-made", "permissions": []}),
        ("PUT", f"{V1}/rbac/roles/{TAG}-everything", {"description": "mine"}),
        ("DELETE", f"{V1}/rbac/roles/{TAG}-everything", None),
        (
            "POST",
            f"{V1}/rbac/roles/assign",
            {"user_id": me, "role_name": f"{TAG}-everything"},
        ),
        (
            "POST",
            f"{V1}/rbac/roles/revoke",
            {"user_id": str(other.id), "role_name": f"{TAG}-everything"},
        ),
        (
            "POST",
            f"{V1}/rbac/users/{me}/grant",
            {"user_id": me, "resource": "experiment", "actions": ["delete"]},
        ),
        ("DELETE", f"{V1}/rbac/users/{other.id}/grant/experiment", None),
        ("GET", f"{V1}/rbac/users/{other.id}/permissions", None),
        # Other users' API keys.
        ("GET", f"{V1}/api-keys?all=true", None),
        ("DELETE", f"{V1}/api-keys/{other_key.id}", None),
        # The compliance audit trail.
        ("GET", f"{V1}/compliance/audit-events", None),
        ("GET", f"{V1}/compliance/reports/soc2", None),
        ("GET", f"{V1}/compliance/export", None),
        # Holdout and exclusion-group administration, the bandit override.
        ("POST", f"{V1}/holdout", {"name": f"{TAG}-h", "holdout_percentage": 5}),
        ("DELETE", f"{V1}/mutual-exclusion-groups/{group.id}", None),
        (
            "PUT",
            f"{V1}/bandit/{experiment.id}/weights",
            {"weights": {"a": 0.5, "b": 0.5}},
        ),
        # The counter reset, beside counter writes that experiment (update) admits.
        (
            "POST",
            f"{V1}/counters/{experiment.id}/reset",
            {"experiment_id": str(experiment.id), "reason": "no escalation"},
        ),
        # Notifications administration, interaction analysis.
        ("GET", f"{V1}/notifications/admin/preferences", None),
        ("GET", f"{V1}/interactions/scan", None),
        # Superuser only.
        ("GET", f"{V1}/admin/users", None),
        ("PATCH", f"{V1}/admin/users/{other.id}", {"is_active": False}),
        ("POST", f"{V1}/safety/settings", {"enable_automatic_rollbacks": False}),
        ("POST", f"{V1}/results/{experiment.id}/invalidate-cache", None),
    ]
    wrong = []
    for method, path, body in refused:
        resp = client.request(method, path, headers=headers, json=body)
        if resp.status_code != 403:
            wrong.append(
                f"{method} {path}: expected 403, got {resp.status_code}: "
                f"{resp.text[:160]}"
            )
    assert wrong == [], "\n".join(wrong)

    # And nothing was written by those requests.
    assert counters.reset_counters.call_count == 0, "a counter was reset"
    db_session.expire_all()
    assert db_session.get(APIKey, other_key.id) is not None
    assert (
        db_session.execute(
            text("SELECT count(*) FROM custom_roles WHERE name = :n"),
            {"n": f"{TAG}-made"},
        ).scalar_one()
        == 0
    )

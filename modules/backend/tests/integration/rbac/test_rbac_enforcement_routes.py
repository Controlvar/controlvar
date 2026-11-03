"""Each grantable pair takes effect through real sign-in, and only that pair (#891).

Check #30 of the #891 plan. 11 pairs x 8 grant states x {VIEWER, ANALYST} =
176 cases, each a real ``User`` signing in with a real local token and calling
the route that checks the pair. The oracle, written here and never read from
``check_permission``: the answer is not 403 exactly when the built-in role
already holds the pair, or the state is one that takes effect (an activated
custom role, or an activated, active, unexpired direct grant).

The pairs every role already holds answer non-403 in every state:
experiment and feature_flag read and list for both roles, and
``audit_log (read)`` for ANALYST. A VIEWER's ``audit_log (read)`` is observed
on ``GET /audit-logs/stats``, which answers 403 without it (the list route
answers 200 with the viewer's own entries).
"""

from __future__ import annotations

import uuid
from typing import Callable, Dict, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.core.permissions import ROLE_PERMISSIONS, Action, ResourceType
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import (
    ACTIVE_STATES,
    ELEVEN,
    STATES,
    cleanup,
    plant,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "enf891"
ACTORS = (UserRole.VIEWER, UserRole.ANALYST)
PAIRS = tuple(sorted(ELEVEN))


def _base_holds(role: UserRole, pair: Tuple[str, str]) -> bool:
    resource, action = pair
    if resource == "audit_log":
        # can_read_all_audit_logs: ADMIN and ANALYST by role.
        return role in (UserRole.ADMIN, UserRole.ANALYST)
    return Action(action) in ROLE_PERMISSIONS[role].get(ResourceType(resource), [])


CASES = [
    pytest.param(role, pair, state, id=f"{role.value}-{pair[0]}:{pair[1]}-{state}")
    for role in ACTORS
    for pair in PAIRS
    for state in STATES
]


def test_the_case_count_is_176_and_44_take_effect():
    assert len(CASES) == 176
    active = [c for c in CASES if c.values[2] in ACTIVE_STATES]
    assert len(active) == 44
    # 18 of the 44 are pairs the built-in role already holds.
    held = [c for c in active if _base_holds(c.values[0], c.values[1])]
    assert len(held) == 18


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def owner(db_session):
    """Owns the experiment and the flag a case acts on; not the actor."""
    return make_user(db_session, UserRole.ADMIN, tag=TAG)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    cleanup(db_session, TAG, [u.id for u in users])


def _experiment(db: Session, owner: User) -> Experiment:
    experiment = Experiment(
        name=f"enf {uuid.uuid4().hex[:8]}",
        key=f"enf-{uuid.uuid4().hex[:10]}",
        status=ExperimentStatus.DRAFT,
        owner_id=owner.id,
        hypothesis="a grant takes effect",
    )
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    return experiment


def _flag(db: Session, owner: User) -> FeatureFlag:
    flag = FeatureFlag(
        key=f"enf-{uuid.uuid4().hex[:10]}",
        name="enf flag",
        status=FeatureFlagStatus.INACTIVE,
        owner_id=owner.id,
        rollout_percentage=0,
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    return flag


def _delete_experiment(client: TestClient, experiment: Experiment, headers):
    # The route takes the experiment's id again as a confirmation.
    return client.delete(
        f"{V1}/experiments/{experiment.id}",
        params={"experiment_key": str(experiment.id)},
        headers=headers,
    )


def _call(
    client: TestClient, db: Session, owner: User, actor: User, pair: Tuple[str, str]
):
    """The route that checks *pair*, called as *actor*; returns the response."""
    headers = auth(actor)
    suffix = uuid.uuid4().hex[:8]
    routes: Dict[Tuple[str, str], Callable] = {
        ("experiment", "create"): lambda: client.post(
            f"{V1}/experiments/",
            headers=headers,
            json={
                "name": f"enf create {suffix}",
                "key": f"enf-create-{suffix}",
                "hypothesis": "a grant takes effect",
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
        ),
        ("experiment", "read"): lambda: client.get(
            f"{V1}/experiments/{_experiment(db, owner).id}", headers=headers
        ),
        ("experiment", "list"): lambda: client.get(
            f"{V1}/experiments/", headers=headers
        ),
        ("experiment", "update"): lambda: client.put(
            f"{V1}/experiments/{_experiment(db, owner).id}",
            headers=headers,
            json={"description": "changed through a grant"},
        ),
        ("experiment", "delete"): lambda: _delete_experiment(
            client, _experiment(db, owner), headers
        ),
        ("feature_flag", "create"): lambda: client.post(
            f"{V1}/feature-flags/",
            headers=headers,
            json={"key": f"enf-new-{suffix}", "name": "enf new"},
        ),
        ("feature_flag", "read"): lambda: client.get(
            f"{V1}/feature-flags/{_flag(db, owner).id}", headers=headers
        ),
        ("feature_flag", "list"): lambda: client.get(
            f"{V1}/feature-flags/", headers=headers
        ),
        ("feature_flag", "update"): lambda: client.put(
            f"{V1}/feature-flags/{_flag(db, owner).id}",
            headers=headers,
            json={"description": "changed through a grant"},
        ),
        ("feature_flag", "delete"): lambda: client.delete(
            f"{V1}/feature-flags/{_flag(db, owner).id}", headers=headers
        ),
        ("audit_log", "read"): lambda: client.get(
            f"{V1}/audit-logs/stats", headers=headers
        ),
    }
    return routes[pair]()


@pytest.mark.parametrize("role, pair, state", CASES)
def test_a_grant_admits_its_pair_and_only_its_pair(
    client, db_session, owner, role, pair, state
):
    actor = make_user(db_session, role, tag=TAG)
    plant(db_session, actor.id, pair, state, TAG)
    response = _call(client, db_session, owner, actor, pair)
    expected_allowed = _base_holds(role, pair) or state in ACTIVE_STATES
    label = f"{pair[0]}:{pair[1]} ({role.value}, {state})"
    if expected_allowed:
        assert response.status_code != 403, (
            f"{label}: expected not 403, got 403: {response.text[:300]}"
        )
        assert response.status_code < 500, (
            f"{label}: expected 2xx, got {response.status_code}: {response.text[:300]}"
        )
    else:
        assert response.status_code == 403, (
            f"{label}: expected 403, got {response.status_code}: {response.text[:300]}"
        )


def test_the_positive_control_admin_is_admitted_everywhere(client, db_session, owner):
    """A non-superuser ADMIN, with nothing planted, gets no 403 on any route,
    so a 403 above is the check refusing, not a broken request."""
    admin = make_user(db_session, UserRole.ADMIN, tag=TAG)
    for pair in PAIRS:
        response = _call(client, db_session, owner, admin, pair)
        assert response.status_code < 400, (pair, response.status_code, response.text)

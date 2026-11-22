"""``POST /api/v1/tracking/alias`` takes the server-side key rule, not batch
assignment's experiment rule (#449).

The route's key is ``require_sdk_ruleset_key``: the ``sdk:ruleset`` scope,
with an owner who can change feature flags. Batch assignment also requires an
owner who can change experiments (``OWNER_EXPERIMENT_DETAIL``). For the four
built-in roles the two rules give the same answer; they differ for a custom
role or a direct grant, which the full profile has: an owner granted
``feature_flag (update)`` alone links ids and cannot assign a batch.

A VIEWER, through real sign-in and real keys made by ``POST /api/v1/api-keys``;
grants are made and revoked by a non-superuser ADMIN through the RBAC routes.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.api.sdk_scope import OWNER_EXPERIMENT_DETAIL, OWNER_ROLE_DETAIL
from backend.app.models.identity_alias import IdentityAlias
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import cleanup

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "alias449"


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def ids(db_session):
    made = []
    yield made
    db_session.rollback()
    if made:
        db_session.query(IdentityAlias).filter(
            IdentityAlias.anonymous_id.in_(made) | IdentityAlias.user_id.in_(made)
        ).delete(synchronize_session=False)
        db_session.commit()


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    user_ids = [u.id for u in users]
    db_session.rollback()
    if user_ids:
        db_session.execute(
            text(
                "DELETE FROM audit_logs WHERE user_id = ANY(:ids) OR entity_id = ANY(:ids)"
            ),
            {"ids": user_ids},
        )
        db_session.execute(
            text("DELETE FROM api_keys WHERE user_id = ANY(:ids)"), {"ids": user_ids}
        )
        db_session.commit()
    cleanup(db_session, TAG, user_ids)


def _grant(client, admin, user, resource, actions):
    resp = client.post(
        f"{V1}/rbac/users/{user.id}/grant",
        json={"user_id": str(user.id), "resource": resource, "actions": actions},
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text


def _revoke(client, admin, user, resource):
    resp = client.delete(
        f"{V1}/rbac/users/{user.id}/grant/{resource}", headers=auth(admin)
    )
    assert resp.status_code == 200, resp.text


def _alias(client, key, ids):
    anonymous_id = f"anon-{uuid.uuid4().hex[:10]}"
    user_id = f"known-{uuid.uuid4().hex[:10]}"
    ids.extend([anonymous_id, user_id])
    return client.post(
        f"{V1}/tracking/alias",
        json={"anonymous_id": anonymous_id, "user_id": user_id},
        headers={"X-API-Key": key},
    )


def _batch(client, key):
    return client.post(
        f"{V1}/tracking/assign/batch",
        json={
            "experiment_key": f"alias-{uuid.uuid4().hex[:8]}",
            "users": [{"user_id": f"b-{uuid.uuid4().hex[:10]}"}],
        },
        headers={"X-API-Key": key},
    )


def test_an_owner_who_can_change_flags_but_not_experiments_links_ids(
    client, db_session, ids
):
    admin = make_user(db_session, UserRole.ADMIN, tag=TAG)
    viewer = make_user(db_session, UserRole.VIEWER, tag=TAG)
    _grant(client, admin, viewer, "feature_flag", ["update"])
    created = client.post(
        f"{V1}/api-keys",
        json={"name": "server", "scopes": ["sdk:ruleset"]},
        headers=auth(viewer),
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]

    linked = _alias(client, key, ids)
    assert linked.status_code == 200, f"alias (flag update only): {linked.text}"
    assert linked.json()["created"] is True

    batch = _batch(client, key)
    assert batch.status_code == 403, batch.text
    assert batch.json()["detail"] == OWNER_EXPERIMENT_DETAIL

    _revoke(client, admin, viewer, "feature_flag")
    refused = _alias(client, key, ids)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == OWNER_ROLE_DETAIL
    db_session.expire_all()
    assert (
        db_session.query(IdentityAlias)
        .filter(IdentityAlias.anonymous_id.in_(ids))
        .count()
        == 1
    )

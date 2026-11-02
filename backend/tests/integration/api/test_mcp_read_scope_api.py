"""
The ``mcp:read`` scope against a real database (``backend/app/api/read_scope.py``).

Four read operations -- list flags, list experiments, read an experiment, read
its results -- take a sign-in or an API key that carries ``mcp:read``. Only
``deps.get_db`` is overridden: keys are created through ``POST
/api/v1/api-keys`` and authenticated for real, and the users are
non-superusers of each role with a password hash, so the scope, the key's
validity and the owner are decided as production decides them.

What is pinned, in the order of the contract:

* the bearer path is unchanged: a request with a bearer token is judged on
  the token alone, a request with neither credential gets today's 401, and an
  empty bearer counts as none;
* the key is validated as ``get_api_key`` validates it: an unknown, inactive,
  expired or deleted key, or a deactivated owner, is ``401 "Invalid API Key"``;
* the scope is matched exactly, on the presented key only;
* the owner of a scoped key reads what a sign-in of the same user reads, and
  the route's own role check still runs;
* a scoped key opens nothing else: a write route still wants a sign-in.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.read_scope import MISSING_SCOPE_DETAIL, get_current_reader
from backend.app.api.v1.endpoints import feature_flags as feature_flag_endpoints
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.user import User, UserRole

pytestmark = pytest.mark.integration

KEYS = "/api/v1/api-keys"


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _make_user(db_session, role=UserRole.DEVELOPER, is_superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"mcpread_{role.value}_{suffix}",
        email=f"mcpread_{role.value}_{suffix}@mcpread.test",
        full_name="MCP Read User",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def developer(db_session):
    return _make_user(db_session)


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    # Nothing may stand in for the real authentication here.
    for dependency in (
        get_current_reader,
        deps.get_api_key,
        deps.get_current_user,
        deps.get_current_active_user,
    ):
        assert dependency not in app.dependency_overrides, dependency
    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def seeded(make_feature_flag, make_experiment, make_variant, make_metric):
    """One flag and one ACTIVE experiment (a DRAFT's results are refused)."""
    flag = make_feature_flag()
    experiment = make_experiment(
        name=f"MCP read {uuid.uuid4().hex[:6]}",
        key=f"mcp-read-{uuid.uuid4().hex[:6]}",
        status=ExperimentStatus.ACTIVE,
    )
    make_variant(experiment, name="control", is_control=True)
    make_variant(experiment, name="treatment", is_control=False)
    make_metric(experiment)
    return flag, experiment


def _paths(seeded):
    _flag, experiment = seeded
    return {
        "list_flags": "/api/v1/feature-flags/",
        "list_experiments": "/api/v1/experiments/",
        "get_experiment": f"/api/v1/experiments/{experiment.id}",
        "get_results": f"/api/v1/experiments/{experiment.id}/results",
    }


def _bearer(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _key(client, user, scopes=None):
    body = {"name": f"mcpread-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post(KEYS, json=body, headers=_bearer(user))
    assert response.status_code == 201, response.text
    return response.json()


def _with_key(key):
    return {"X-API-Key": key}


# ---------------------------------------------------------------------------
# The owner of a scoped key reads what the same user's sign-in reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role, is_superuser",
    [
        (UserRole.VIEWER, False),
        (UserRole.ANALYST, False),
        (UserRole.DEVELOPER, False),
        (UserRole.ADMIN, False),
        (UserRole.ADMIN, True),
    ],
    ids=["viewer", "analyst", "developer", "admin", "superuser"],
)
def test_each_role_reads_the_four_operations_with_an_mcp_read_key(
    client, db_session, seeded, role, is_superuser
):
    user = _make_user(db_session, role=role, is_superuser=is_superuser)
    key = _key(client, user, ["mcp:read"])["key"]
    for name, path in _paths(seeded).items():
        by_key = client.get(path, headers=_with_key(key))
        signed_in = client.get(path, headers=_bearer(user))
        assert by_key.status_code == 200, (name, by_key.text)
        assert signed_in.status_code == 200, (name, signed_in.text)
        if name == "get_results":
            # Computed on read, with a timestamp: compared by the experiment
            # each answer is for.
            assert by_key.json()["experiment_id"] == signed_in.json()["experiment_id"]
        else:
            assert by_key.json() == signed_in.json(), name


def test_the_routes_own_role_check_runs_for_a_key(client, developer, seeded):
    """The check is asked about the key's owner, and its refusal stands."""
    key = _key(client, developer, ["mcp:read"])["key"]
    asked_about = []

    def refuse(user, *_args):
        # Read while the request's session is open; the row detaches after.
        asked_about.append((user.id, user.role))
        return False

    with patch.object(feature_flag_endpoints, "check_permission", side_effect=refuse):
        response = client.get("/api/v1/feature-flags/", headers=_with_key(key))
    assert response.status_code == 403
    assert response.json() == {
        "detail": "You don't have permission to list feature flags"
    }
    assert asked_about == [(developer.id, developer.role)]


def test_a_read_records_the_keys_use(client, db_session, developer, seeded):
    created = _key(client, developer, ["mcp:read"])
    response = client.get("/api/v1/experiments/", headers=_with_key(created["key"]))
    assert response.status_code == 200, response.text
    db_session.expire_all()
    row = db_session.query(APIKey).filter(APIKey.id == created["id"]).one()
    assert row.last_used_at is not None


# ---------------------------------------------------------------------------
# The scope is matched exactly, on the presented key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scopes",
    [None, [], ["read"], ["MCP:READ"], ["mcp:read-only"], ["xmcp:read"]]
    + [["sdk:ruleset"], ["mcp:*"], ["mcp"], ["read", "write"]],
    ids=repr,
)
@pytest.mark.parametrize("is_superuser", [False, True], ids=["developer", "superuser"])
def test_a_key_without_the_exact_scope_is_refused_on_each_operation(
    client, db_session, seeded, scopes, is_superuser
):
    """A superuser's key without the scope is refused too."""
    owner = _make_user(db_session, is_superuser=is_superuser)
    key = _key(client, owner, scopes)["key"]
    for name, path in _paths(seeded).items():
        response = client.get(path, headers=_with_key(key))
        assert response.status_code == 403, (
            f"scopes {scopes!r} answered {response.status_code} on {name}; expected 403"
        )
        assert response.json() == {"detail": MISSING_SCOPE_DETAIL}, name


@pytest.mark.parametrize("scopes", [["mcp:read"], ["read", " mcp:read "]], ids=repr)
def test_a_key_with_the_scope_is_served(client, developer, seeded, scopes):
    key = _key(client, developer, scopes)["key"]
    for name, path in _paths(seeded).items():
        assert client.get(path, headers=_with_key(key)).status_code == 200, name


def test_a_stored_unnormalised_scope_string_is_parsed(
    client, db_session, developer, seeded
):
    created = _key(client, developer, ["read"])
    row = db_session.query(APIKey).filter(APIKey.id == created["id"]).one()
    row.scopes = "read, mcp:read "
    db_session.commit()
    response = client.get("/api/v1/feature-flags/", headers=_with_key(created["key"]))
    assert response.status_code == 200, response.text


def test_the_presented_key_decides_not_another_key_of_the_same_owner(
    client, developer, seeded
):
    scoped = _key(client, developer, ["mcp:read"])["key"]
    plain = _key(client, developer)["key"]
    accepted = client.get("/api/v1/experiments/", headers=_with_key(scoped))
    assert accepted.status_code == 200, accepted.text
    refused = client.get("/api/v1/experiments/", headers=_with_key(plain))
    assert refused.status_code == 403, (
        "key without mcp:read answered "
        f"{refused.status_code} because another key of its owner has it"
    )


def test_the_scope_carries_no_role_rule_on_creation(client, db_session, seeded):
    """Any role may create an mcp:read key; only sdk:ruleset is role-checked."""
    viewer = _make_user(db_session, role=UserRole.VIEWER)
    created = _key(client, viewer, ["mcp:read"])
    row = db_session.query(APIKey).filter(APIKey.id == created["id"]).one()
    assert row.scopes == "mcp:read"
    response = client.get("/api/v1/feature-flags/", headers=_with_key(created["key"]))
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The key is validated as get_api_key validates it
# ---------------------------------------------------------------------------


def _assert_invalid_key(response, what="the key"):
    assert response.status_code == 401, (
        f"{what} answered {response.status_code}; expected 401: {response.text}"
    )
    assert response.json() == {"detail": "Invalid API Key"}
    assert response.headers["www-authenticate"] == "APIKey"


def test_an_unknown_key_is_401(client, seeded):
    for path in _paths(seeded).values():
        _assert_invalid_key(client.get(path, headers=_with_key("eptk_not-a-real-key")))


def test_a_deleted_key_is_401(client, developer, seeded):
    created = _key(client, developer, ["mcp:read"])
    path = "/api/v1/feature-flags/"
    assert client.get(path, headers=_with_key(created["key"])).status_code == 200
    deleted = client.delete(f"{KEYS}/{created['id']}", headers=_bearer(developer))
    assert deleted.status_code == 204
    _assert_invalid_key(client.get(path, headers=_with_key(created["key"])))


def test_an_expired_or_inactive_key_is_401(client, db_session, developer, seeded):
    expired = _key(client, developer, ["mcp:read"])
    inactive = _key(client, developer, ["mcp:read"])
    rows = {
        row.id: row
        for row in db_session.query(APIKey).filter(
            APIKey.id.in_([expired["id"], inactive["id"]])
        )
    }
    # Naive UTC, as the column stores it.
    rows[uuid.UUID(expired["id"])].expires_at = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) - timedelta(minutes=1)
    rows[uuid.UUID(inactive["id"])].is_active = False
    db_session.commit()
    for path in _paths(seeded).values():
        _assert_invalid_key(
            client.get(path, headers=_with_key(expired["key"])), "expired key"
        )
        _assert_invalid_key(
            client.get(path, headers=_with_key(inactive["key"])), "inactive key"
        )


def test_a_deactivated_owner_is_401(client, db_session, developer, seeded):
    key = _key(client, developer, ["mcp:read"])["key"]
    developer.is_active = False
    db_session.add(developer)
    db_session.commit()
    _assert_invalid_key(
        client.get("/api/v1/experiments/", headers=_with_key(key)),
        "a deactivated owner's key",
    )


def test_a_key_does_not_consult_the_identity_provider(
    client, developer, seeded, monkeypatch
):
    """Removing a user from the identity provider does not revoke their keys:
    the key path reads the Controlvar user row, never the provider, so only
    deactivating that user or deleting the key stops it (the two tests above)."""
    key = _key(client, developer, ["mcp:read"])["key"]

    def removed_from_the_pool(*_args, **_kwargs):
        raise AssertionError("the key path asked the identity provider")

    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(
        deps.auth_service, "get_user_with_groups", removed_from_the_pool
    )
    for path in _paths(seeded).values():
        response = client.get(path, headers=_with_key(key))
        assert response.status_code == 200, (path, response.text)


# ---------------------------------------------------------------------------
# The bearer path is unchanged
# ---------------------------------------------------------------------------


def test_a_request_with_no_credential_gets_todays_401(client, seeded):
    for path in _paths(seeded).values():
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}
        assert response.headers["www-authenticate"] == "Bearer"


def test_an_invalid_bearer_is_401_even_with_a_valid_scoped_key(
    client, developer, seeded
):
    key = _key(client, developer, ["mcp:read"])["key"]
    for path in _paths(seeded).values():
        response = client.get(
            path,
            headers={"Authorization": "Bearer not-a-token", **_with_key(key)},
        )
        assert response.status_code == 401, (
            f"invalid bearer with a valid key answered {response.status_code}; "
            "expected 401"
        )
        assert response.json() == {"detail": "Could not validate credentials"}
        assert response.headers["www-authenticate"] == "Bearer"


def test_a_valid_bearer_does_not_read_the_key(client, developer, seeded):
    for path in _paths(seeded).values():
        response = client.get(
            path, headers={**_bearer(developer), **_with_key("eptk_not-a-real-key")}
        )
        assert response.status_code == 200, (path, response.text)


def test_a_deactivated_user_with_a_token_is_refused_as_before(
    client, db_session, developer, seeded
):
    headers = _bearer(developer)
    developer.is_active = False
    db_session.add(developer)
    db_session.commit()
    response = client.get("/api/v1/feature-flags/", headers=headers)
    assert (response.status_code, response.json()) == (400, {"detail": "Inactive user"})


@pytest.mark.parametrize("authorization", ["Bearer", "Basic dXNlcjpwYXNz"])
def test_an_empty_bearer_or_another_scheme_falls_through_to_the_key(
    client, developer, seeded, authorization
):
    key = _key(client, developer, ["mcp:read"])["key"]
    response = client.get(
        "/api/v1/experiments/",
        headers={"Authorization": authorization, **_with_key(key)},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# A scoped key opens nothing else
# ---------------------------------------------------------------------------


def test_a_scoped_key_is_refused_by_the_routes_that_change_things(
    client, developer, seeded
):
    flag, experiment = seeded
    key = _key(client, developer, ["mcp:read"])["key"]
    for method, path, body in (
        ("POST", "/api/v1/feature-flags/", {"key": "mcp-x", "name": "X"}),
        ("PUT", f"/api/v1/feature-flags/{flag.id}", {"name": "renamed"}),
        ("POST", f"/api/v1/feature-flags/{flag.id}/toggle", None),
        ("DELETE", f"/api/v1/feature-flags/{flag.id}", None),
        ("POST", "/api/v1/experiments/", {"name": "X"}),
        ("PUT", f"/api/v1/experiments/{experiment.id}", {"name": "renamed"}),
        ("POST", f"/api/v1/experiments/{experiment.id}/pause", None),
        ("DELETE", f"/api/v1/experiments/{experiment.id}", None),
        # A read outside the four operations, too.
        ("GET", f"/api/v1/feature-flags/{flag.id}", None),
        ("GET", f"/api/v1/results/{experiment.id}", None),
    ):
        response = client.request(method, path, json=body, headers=_with_key(key))
        assert response.status_code == 401, (method, path, response.text)
        assert response.json() == {"detail": "Not authenticated"}, (method, path)

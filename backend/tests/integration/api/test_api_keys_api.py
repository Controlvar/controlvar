"""
Integration tests for the user-owned API keys router (``/api/v1/api-keys``).

Real database, real local-JWT authentication (only ``deps.get_db`` is
overridden).  The final class proves the created key authenticates SDK
traffic end to end via ``POST /api/v1/tracking/assign``.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import (
    create_local_access_token,
    get_password_hash,
    hash_api_key,
)
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.models.user import User, UserRole

pytestmark = pytest.mark.integration

URL = "/api/v1/api-keys"
# An SDK route that authenticates with deps.get_api_key (which records
# last_used_at) and answers 200 with no other setup.
SDK_PROBE = "/api/v1/feature-flags/user/probe-user"


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _make_user(db_session, role, is_superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"keys_{role.value}_{suffix}",
        email=f"keys_{role.value}_{suffix}@keys.test",
        full_name="API Keys User",
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
def admin(db_session):
    return _make_user(db_session, UserRole.ADMIN, is_superuser=True)


@pytest.fixture
def developer(db_session):
    return _make_user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def other_developer(db_session):
    return _make_user(db_session, UserRole.DEVELOPER)


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

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _create(client, user, **body):
    payload = {"name": f"key-{uuid.uuid4().hex[:6]}"}
    payload.update(body)
    return client.post(URL, json=payload, headers=_auth(user))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_requires_auth(self, client):
        assert client.post(URL, json={"name": "x"}).status_code == 401

    def test_create_returns_plaintext_once(self, client, db_session, developer):
        resp = _create(
            client,
            developer,
            name="SDK key",
            description="ci",
            scopes=["read", "track"],
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert set(body) == {"id", "name", "key", "prefix", "created_at", "expires_at"}
        assert body["name"] == "SDK key"
        assert body["key"].startswith("eptk_") and len(body["key"]) == len("eptk_") + 32
        assert body["prefix"] == body["key"][:9]
        assert body["expires_at"] is None

        row = (
            db_session.query(APIKey).filter(APIKey.id == uuid.UUID(body["id"])).first()
        )
        assert row is not None
        assert row.user_id == developer.id
        assert row.key == hash_api_key(body["key"])  # only the hash is stored
        assert row.key != body["key"]
        assert row.scopes == "read,track"
        assert row.description == "ci"

    def test_create_with_expiry(self, client, developer):
        expires = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0)
        resp = _create(client, developer, expires_at=expires.isoformat())
        assert resp.status_code == 201, resp.text
        assert resp.json()["expires_at"].startswith(expires.isoformat()[:19])

    def test_validation(self, client, developer):
        assert client.post(URL, json={}, headers=_auth(developer)).status_code == 422
        assert (
            client.post(URL, json={"name": "   "}, headers=_auth(developer)).status_code
            == 422
        )
        assert (
            client.post(
                URL, json={"name": "x", "scopes": ["a,b"]}, headers=_auth(developer)
            ).status_code
            == 422
        )

    def test_two_keys_are_distinct(self, client, developer):
        a = _create(client, developer).json()
        b = _create(client, developer).json()
        assert a["key"] != b["key"] and a["id"] != b["id"]

    def test_trailing_slash_is_accepted(self, client, developer):
        resp = client.post(f"{URL}/", json={"name": "slash"}, headers=_auth(developer))
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Creating a key with the sdk:ruleset scope needs a role that can change flags
# ---------------------------------------------------------------------------

# (role, is_superuser, expected status). A non-superuser of every role, plus a
# superuser whose role on its own would be refused, so the superuser case is
# decided by is_superuser and not by the role.
_ACTORS = [
    ("admin", UserRole.ADMIN, False, 201),
    ("developer", UserRole.DEVELOPER, False, 201),
    ("analyst", UserRole.ANALYST, False, 403),
    ("viewer", UserRole.VIEWER, False, 403),
    ("superuser", UserRole.VIEWER, True, 201),
]
RULESET_ACTORS = [pytest.param(r, su, code, id=i) for i, r, su, code in _ACTORS]
ALL_ACTORS = [pytest.param(r, su, id=i) for i, r, su, _ in _ACTORS]


def _key_rows(db_session, user):
    db_session.expire_all()
    return db_session.query(APIKey).filter(APIKey.user_id == user.id).all()


@pytest.mark.regression
class TestRulesetScopeNeedsFlagChangeRole:
    @pytest.mark.parametrize("role,is_superuser,expected", RULESET_ACTORS)
    def test_ruleset_scope_by_role(
        self, client, db_session, role, is_superuser, expected
    ):
        user = _make_user(db_session, role, is_superuser=is_superuser)
        resp = _create(client, user, scopes=["sdk:ruleset"])
        assert resp.status_code == expected, resp.text
        rows = _key_rows(db_session, user)
        if expected == 201:
            assert [r.scopes for r in rows] == ["sdk:ruleset"]
        else:
            detail = resp.json()["detail"]
            assert "ADMIN" in detail and "DEVELOPER" in detail
            assert "superuser" in detail and "sdk:ruleset" in detail
            assert "key" not in resp.json()
            assert rows == []  # nothing is written on a refusal

    @pytest.mark.parametrize("role,is_superuser", ALL_ACTORS)
    def test_keys_without_the_scope_are_unchanged(
        self, client, db_session, role, is_superuser
    ):
        user = _make_user(db_session, role, is_superuser=is_superuser)
        assert _create(client, user).status_code == 201
        assert _create(client, user, scopes=[]).status_code == 201
        resp = _create(client, user, scopes=["read", "track"])
        assert resp.status_code == 201, resp.text
        assert sorted(r.scopes or "" for r in _key_rows(db_session, user)) == [
            "",
            "",
            "read,track",
        ]

    @pytest.mark.parametrize(
        "scopes",
        [
            pytest.param(["read", "sdk:ruleset", "track"], id="mixed-list"),
            pytest.param([" sdk:ruleset "], id="padded"),
            pytest.param("read, sdk:ruleset", id="comma-string"),
        ],
    )
    @pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
    def test_any_list_carrying_the_scope_is_refused(
        self, client, db_session, role, scopes
    ):
        user = _make_user(db_session, role)
        resp = _create(client, user, scopes=scopes)
        assert resp.status_code == 403, resp.text
        assert _key_rows(db_session, user) == []

    @pytest.mark.parametrize(
        "scopes", [["SDK:RULESET"], ["xsdk:ruleset"], ["sdk:ruleset-ro"]]
    )
    def test_near_misses_are_labels_not_the_scope(self, client, db_session, scopes):
        # Scope matching is exact and case-sensitive (api_key_scopes.has_scope),
        # so these are stored as labels and do not need the role.
        user = _make_user(db_session, UserRole.VIEWER)
        resp = _create(client, user, scopes=scopes)
        assert resp.status_code == 201, resp.text
        assert [r.scopes for r in _key_rows(db_session, user)] == scopes


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_requires_auth(self, client):
        assert client.get(URL).status_code == 401

    def test_lists_only_own_keys_without_secret(
        self, client, developer, other_developer
    ):
        mine = _create(client, developer, name="mine").json()
        _create(client, other_developer, name="theirs")

        resp = client.get(URL, headers=_auth(developer))
        assert resp.status_code == 200, resp.text
        items = resp.json()
        ids = {item["id"] for item in items}
        assert mine["id"] in ids
        assert all(item["user_id"] == str(developer.id) for item in items)
        item = next(i for i in items if i["id"] == mine["id"])
        assert set(item) == {
            "id",
            "name",
            "description",
            "scopes",
            "is_active",
            "user_id",
            "created_at",
            "expires_at",
            "last_used_at",
        }
        assert "key" not in item

    def test_scopes_round_trip_as_list(self, client, developer):
        created = _create(client, developer, scopes=["read", "write"]).json()
        items = client.get(URL, headers=_auth(developer)).json()
        assert next(i for i in items if i["id"] == created["id"])["scopes"] == [
            "read",
            "write",
        ]

    def test_unnormalised_stored_scopes_are_listed_by_the_shared_parser(
        self, client, db_session, developer
    ):
        # Seed scripts write the column directly, without normalising. The list
        # endpoint reads it through api_key_scopes.parse_scopes, the same rule
        # the ruleset scope check will use.
        row, _plaintext = APIKey.create_for_user(
            db_session,
            user_id=developer.id,
            name="seed-style",
            scopes=",, read, sdk:ruleset ,,SDK:RULESET",
        )
        items = client.get(URL, headers=_auth(developer)).json()
        assert next(i for i in items if i["id"] == str(row.id))["scopes"] == [
            "read",
            "sdk:ruleset",
            "SDK:RULESET",
        ]

    def test_all_requires_admin(self, client, developer):
        resp = client.get(f"{URL}?all=true", headers=_auth(developer))
        assert resp.status_code == 403

    def test_admin_all_sees_everyone(self, client, admin, developer, other_developer):
        a = _create(client, developer).json()["id"]
        b = _create(client, other_developer).json()["id"]
        own = _create(client, admin).json()["id"]

        resp = client.get(f"{URL}?all=true", headers=_auth(admin))
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()}
        assert {a, b, own} <= ids

        # Without ?all the admin sees only their own.
        ids = {item["id"] for item in client.get(URL, headers=_auth(admin)).json()}
        assert own in ids and a not in ids

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.ANALYST])
    def test_a_superuser_who_is_not_an_admin_sees_everyone_too(
        self, client, db_session, developer, role
    ):
        # The Edit user dialog (#657) lists a user's keys with ?all=true, and the
        # admin area opens for any superuser, whatever role the account holds.
        # The route must therefore answer them too: a role-only check would
        # leave the dialog with an error for exactly these accounts.
        superuser = _make_user(db_session, role, is_superuser=True)
        owned = _create(client, developer).json()
        resp = client.get(f"{URL}?all=true", headers=_auth(superuser))
        assert resp.status_code == 200, resp.text
        item = next(i for i in resp.json() if i["id"] == owned["id"])
        assert item["user_id"] == str(developer.id)
        assert {"name", "scopes", "last_used_at", "expires_at"} <= set(item)

    def test_inactive_keys_hidden_by_default(self, client, db_session, developer):
        created = _create(client, developer).json()
        row = (
            db_session.query(APIKey)
            .filter(APIKey.id == uuid.UUID(created["id"]))
            .first()
        )
        row.is_active = False
        db_session.commit()

        ids = {i["id"] for i in client.get(URL, headers=_auth(developer)).json()}
        assert created["id"] not in ids
        ids = {
            i["id"]
            for i in client.get(
                f"{URL}?include_inactive=true", headers=_auth(developer)
            ).json()
        }
        assert created["id"] in ids


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_requires_auth(self, client):
        assert client.delete(f"{URL}/{uuid.uuid4()}").status_code == 401

    def test_owner_can_delete(self, client, db_session, developer):
        created = _create(client, developer).json()
        resp = client.delete(f"{URL}/{created['id']}", headers=_auth(developer))
        assert resp.status_code == 204
        assert resp.content == b""
        assert (
            db_session.query(APIKey)
            .filter(APIKey.id == uuid.UUID(created["id"]))
            .first()
            is None
        )

    def test_other_user_cannot_delete(
        self, client, db_session, developer, other_developer
    ):
        created = _create(client, developer).json()
        resp = client.delete(f"{URL}/{created['id']}", headers=_auth(other_developer))
        assert resp.status_code == 403
        assert (
            db_session.query(APIKey)
            .filter(APIKey.id == uuid.UUID(created["id"]))
            .first()
            is not None
        )

    def test_admin_can_delete_anyones(self, client, db_session, admin, developer):
        created = _create(client, developer).json()
        resp = client.delete(f"{URL}/{created['id']}", headers=_auth(admin))
        assert resp.status_code == 204

    def test_unknown_id_404(self, client, developer):
        assert (
            client.delete(f"{URL}/{uuid.uuid4()}", headers=_auth(developer)).status_code
            == 404
        )

    def test_malformed_id_422(self, client, developer):
        assert (
            client.delete(f"{URL}/not-a-uuid", headers=_auth(developer)).status_code
            == 422
        )

    def test_deleted_key_no_longer_authenticates(self, client, developer):
        created = _create(client, developer).json()
        headers = {"X-API-Key": created["key"]}
        assert client.get(SDK_PROBE, headers=headers).status_code == 200
        client.delete(f"{URL}/{created['id']}", headers=_auth(developer))
        assert client.get(SDK_PROBE, headers=headers).status_code == 401


# ---------------------------------------------------------------------------
# The key works for SDK traffic
# ---------------------------------------------------------------------------


@pytest.fixture
def active_experiment(db_session, admin):
    from backend.app.models.experiment import Experiment, ExperimentType

    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"API key smoke {suffix}",
        key=f"api-key-smoke-{suffix}",
        description="created by test_api_keys_api",
        hypothesis="keys authenticate SDK calls",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=admin.id,
    )
    db_session.add(experiment)
    db_session.commit()
    db_session.refresh(experiment)
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    yield experiment
    db_session.rollback()
    db_session.query(Assignment).filter(
        Assignment.experiment_id == experiment.id
    ).delete()
    db_session.commit()


class TestKeyAuthenticatesSdkTraffic:
    def test_tracking_assign_with_created_key(
        self, client, developer, active_experiment
    ):
        key = _create(client, developer, name="sdk").json()["key"]
        resp = client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": active_experiment.key,
                "user_id": f"u-{uuid.uuid4().hex[:6]}",
            },
            headers={"X-API-Key": key},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["experiment_key"] == active_experiment.key
        assert body["variant_name"] in ("control", "treatment")
        assert body["assigned"] is True

    def test_tracking_assign_rejects_unknown_key(self, client, active_experiment):
        resp = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": active_experiment.key, "user_id": "u-1"},
            headers={"X-API-Key": "eptk_" + "0" * 32},
        )
        assert resp.status_code == 401

    def test_bearer_token_is_not_an_api_key(self, client, developer, active_experiment):
        """SDK endpoints take X-API-Key, not the dashboard bearer token."""
        resp = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": active_experiment.key, "user_id": "u-1"},
            headers=_auth(developer),
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Issue #198: last_used_at records use, at most once a minute
# ---------------------------------------------------------------------------


def _row(db_session, key_id):
    db_session.expire_all()
    return db_session.query(APIKey).filter(APIKey.id == uuid.UUID(key_id)).one()


def _set_last_used(db_session, key_id, value):
    row = _row(db_session, key_id)
    row.last_used_at = value
    db_session.commit()


@pytest.mark.regression
class TestLastUsedAt:
    def test_using_a_key_sets_last_used_at(self, client, db_session, developer):
        created = _create(client, developer).json()
        items = client.get(URL, headers=_auth(developer)).json()
        assert (
            next(i for i in items if i["id"] == created["id"])["last_used_at"] is None
        )
        updated_at_before = _row(db_session, created["id"]).updated_at

        before = datetime.utcnow() - timedelta(seconds=5)
        resp = client.get(SDK_PROBE, headers={"X-API-Key": created["key"]})
        assert resp.status_code == 200, resp.text

        items = client.get(URL, headers=_auth(developer)).json()
        listed = next(i for i in items if i["id"] == created["id"])
        assert listed["last_used_at"] is not None

        row = _row(db_session, created["id"])
        assert row.last_used_at >= before
        # Using a key is not editing it.
        assert row.updated_at == updated_at_before

    def test_a_recent_value_is_not_rewritten(self, client, db_session, developer):
        created = _create(client, developer).json()
        recent = datetime.utcnow() - timedelta(seconds=10)
        _set_last_used(db_session, created["id"], recent)

        resp = client.get(SDK_PROBE, headers={"X-API-Key": created["key"]})
        assert resp.status_code == 200, resp.text
        assert _row(db_session, created["id"]).last_used_at == recent

    def test_a_stale_value_is_advanced(self, client, db_session, developer):
        created = _create(client, developer).json()
        stale = datetime.utcnow() - timedelta(minutes=5)
        _set_last_used(db_session, created["id"], stale)

        resp = client.get(SDK_PROBE, headers={"X-API-Key": created["key"]})
        assert resp.status_code == 200, resp.text
        assert _row(db_session, created["id"]).last_used_at > stale + timedelta(
            minutes=4
        )

    def test_a_rejected_key_records_nothing(self, client, db_session, developer):
        created = _create(client, developer).json()
        _set_last_used(db_session, created["id"], None)
        row = _row(db_session, created["id"])
        row.is_active = False
        db_session.commit()

        resp = client.get(SDK_PROBE, headers={"X-API-Key": created["key"]})
        assert resp.status_code == 401
        assert _row(db_session, created["id"]).last_used_at is None

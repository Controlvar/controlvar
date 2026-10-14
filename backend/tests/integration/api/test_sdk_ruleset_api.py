"""
``GET /api/v1/sdk/ruleset`` against a real database.

Only ``deps.get_db`` is overridden: keys are created through
``POST /api/v1/api-keys`` and authenticated for real, so the scope check is the
one production runs. A ``dependency_overrides`` entry for ``get_api_key`` --
which some shared fixtures install -- is shown not to bypass it.

The last class stores every flag of ``tests/sdk-contract/ruleset-vectors.json``
and checks the served ruleset entries, and the answers of the public evaluate
endpoint, against the file.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.sdk_scope import MISSING_SCOPE_DETAIL, OWNER_ROLE_DETAIL
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.models.user import User, UserRole
from backend.scripts.generate_ruleset_vectors import (
    VECTOR_SEGMENT,
    VECTOR_SEGMENT_MEMBERS,
    VECTORS_PATH,
)

pytestmark = pytest.mark.integration

URL = "/api/v1/sdk/ruleset"
KEYS = "/api/v1/api-keys"


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _make_user(db_session, role=UserRole.DEVELOPER, is_superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"ruleset_{role.value}_{suffix}",
        email=f"ruleset_{role.value}_{suffix}@ruleset.test",
        full_name="Ruleset User",
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

    assert deps.get_api_key not in app.dependency_overrides
    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _key(client, user, scopes=None):
    body = {"name": f"ruleset-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post(KEYS, json=body, headers=_auth(user))
    assert response.status_code == 201, response.text
    return response.json()


def _get(client, key=None, **headers):
    if key is not None:
        headers["X-API-Key"] = key
    return client.get(URL, headers=headers)


@pytest.fixture
def scoped_key(client, developer):
    return _key(client, developer, ["sdk:ruleset"])["key"]


# ---------------------------------------------------------------------------
# The scope
# ---------------------------------------------------------------------------


class TestScope:
    @pytest.mark.parametrize(
        "scopes",
        [None, [], ["read", "write"], ["SDK:RULESET"], ["xsdk:ruleset"]]
        + [["sdk:ruleset-ro"], ["sdk:*"], ["sdk"]],
        ids=repr,
    )
    def test_a_key_without_the_exact_scope_is_refused(self, client, developer, scopes):
        key = _key(client, developer, scopes)["key"]
        response = _get(client, key)
        assert response.status_code == 403
        assert response.json()["detail"] == MISSING_SCOPE_DETAIL

    @pytest.mark.parametrize("scopes", [["sdk:ruleset"], ["read", " sdk:ruleset "]])
    def test_a_key_with_the_scope_is_served(self, client, developer, scopes):
        key = _key(client, developer, scopes)["key"]
        assert _get(client, key).status_code == 200

    def test_a_stored_unnormalised_scope_string_is_parsed(
        self, client, db_session, developer
    ):
        created = _key(client, developer, ["read"])
        row = db_session.query(APIKey).filter(APIKey.id == created["id"]).one()
        row.scopes = "read, sdk:ruleset "
        db_session.commit()
        assert _get(client, created["key"]).status_code == 200

    def test_the_presented_key_decides_not_another_key_of_the_same_user(
        self, client, developer
    ):
        scoped = _key(client, developer, ["sdk:ruleset"])["key"]
        plain = _key(client, developer)["key"]
        assert _get(client, scoped).status_code == 200
        assert _get(client, plain).status_code == 403

    def test_no_key_or_an_unknown_key_is_401(self, client):
        assert _get(client).status_code == 401
        assert _get(client, "eptk_not-a-real-key").status_code == 401

    def test_a_deleted_key_is_401(self, client, developer):
        created = _key(client, developer, ["sdk:ruleset"])
        assert _get(client, created["key"]).status_code == 200
        client.delete(f"{KEYS}/{created['id']}", headers=_auth(developer))
        assert _get(client, created["key"]).status_code == 401

    def test_an_expired_or_inactive_key_is_401(self, client, db_session, developer):
        expired = _key(client, developer, ["sdk:ruleset"])
        inactive = _key(client, developer, ["sdk:ruleset"])
        rows = {
            row.id: row
            for row in db_session.query(APIKey).filter(
                APIKey.id.in_([expired["id"], inactive["id"]])
            )
        }
        rows[uuid.UUID(expired["id"])].expires_at = datetime.now(
            timezone.utc
        ) - timedelta(minutes=1)
        rows[uuid.UUID(inactive["id"])].is_active = False
        db_session.commit()
        assert _get(client, expired["key"]).status_code == 401
        assert _get(client, inactive["key"]).status_code == 401

    def test_an_inactive_owner_is_401(self, client, db_session, developer):
        key = _key(client, developer, ["sdk:ruleset"])["key"]
        developer.is_active = False
        db_session.commit()
        assert _get(client, key).status_code == 401

    def test_overriding_get_api_key_does_not_bypass_the_scope(self, client, developer):
        plain = _key(client, developer)["key"]
        app.dependency_overrides[deps.get_api_key] = lambda: developer
        try:
            assert _get(client, plain).status_code == 403
            assert _get(client).status_code == 401
        finally:
            app.dependency_overrides.pop(deps.get_api_key, None)


class TestOwnerRole:
    """A key with sdk:ruleset works only while its owner can change feature flags.

    Real keys, real roles, no dependency override: the owner is re-read on
    every request, so a demotion takes effect on the next poll.
    """

    @pytest.mark.parametrize(
        "role, superuser, expected",
        [
            (UserRole.VIEWER, True, 200),
            (UserRole.ADMIN, False, 200),
            (UserRole.DEVELOPER, False, 200),
            (UserRole.ANALYST, False, 403),
            (UserRole.VIEWER, False, 403),
        ],
        ids=["superuser", "admin", "developer", "analyst", "viewer"],
    )
    def test_only_an_owner_who_can_change_flags_is_served(
        self, client, db_session, role, superuser, expected
    ):
        if expected == 200:
            user = _make_user(db_session, role, is_superuser=superuser)
            key = _key(client, user, ["sdk:ruleset"])["key"]
        else:
            # ANALYST and VIEWER cannot create an sdk:ruleset key (#305), so
            # the key is made the way a demotion leaves one: created by a
            # DEVELOPER through the API, then the owner's role changed.
            user = _make_user(db_session, UserRole.DEVELOPER)
            key = _key(client, user, ["sdk:ruleset"])["key"]
            user.role = role
            db_session.commit()
            refused = client.post(
                KEYS,
                json={"name": "ruleset-refused", "scopes": ["sdk:ruleset"]},
                headers=_auth(user),
            )
            assert refused.status_code == 403, refused.text
        response = _get(client, key)
        assert response.status_code == expected
        if expected == 403:
            assert response.json()["detail"] == OWNER_ROLE_DETAIL

    def test_a_demoted_owners_key_stops_working(self, client, db_session, developer):
        key = _key(client, developer, ["sdk:ruleset"])["key"]
        assert _get(client, key).status_code == 200
        developer.role = UserRole.VIEWER
        db_session.commit()
        response = _get(client, key)
        assert response.status_code == 403
        assert response.json()["detail"] == OWNER_ROLE_DETAIL

    def test_a_key_acts_as_its_creator_not_another_user(self, client, db_session):
        # Two users of different roles; the key belongs to the older one.
        creator = _make_user(db_session, UserRole.DEVELOPER)
        key = _key(client, creator, ["sdk:ruleset"])["key"]
        other = _make_user(db_session, UserRole.VIEWER)
        assert other.created_at > creator.created_at
        assert _get(client, key).status_code == 200

        # Swap their roles: the creator's decides.
        creator.role, other.role = UserRole.VIEWER, UserRole.DEVELOPER
        db_session.commit()
        response = _get(client, key)
        assert response.status_code == 403
        assert response.json()["detail"] == OWNER_ROLE_DETAIL

        # A DEVELOPER again, then deactivated while the other stays active:
        # the key no longer signs anyone in.
        creator.role = UserRole.DEVELOPER
        db_session.commit()
        assert _get(client, key).status_code == 200
        creator.is_active = False
        db_session.commit()
        assert other.is_active
        assert _get(client, key).status_code == 401

    def test_a_stored_scope_on_a_viewers_key_is_refused(self, client, db_session):
        viewer = _make_user(db_session, UserRole.VIEWER)
        created = _key(client, viewer)
        row = db_session.query(APIKey).filter(APIKey.id == created["id"]).one()
        row.scopes = "sdk:ruleset"
        db_session.commit()
        response = _get(client, created["key"])
        assert response.status_code == 403
        assert response.json()["detail"] == OWNER_ROLE_DETAIL


# ---------------------------------------------------------------------------
# Versioning: ETag and If-None-Match
# ---------------------------------------------------------------------------


def _store(db_session, owner, **fields):
    flag = FeatureFlag(
        key=fields.pop("key", f"ruleset-{uuid.uuid4().hex[:10]}"),
        name=fields.pop("name", "Ruleset flag"),
        status=fields.pop("status", FeatureFlagStatus.ACTIVE),
        owner_id=owner.id,
        rollout_percentage=fields.pop("rollout_percentage", 10),
        targeting_rules=fields.pop("targeting_rules", None),
        **fields,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    return flag


class TestVersioning:
    def test_the_etag_is_the_body_version(
        self, client, db_session, developer, scoped_key
    ):
        _store(db_session, developer)
        response = _get(client, scoped_key)
        assert response.status_code == 200
        body = response.json()
        assert response.headers["etag"] == f'"{body["version"]}"'
        assert response.headers["cache-control"] == "private, no-cache"
        assert response.headers["content-type"].startswith("application/json")
        assert body["schema"] == 1 and body["bucketing"] == "md5-mod100-v1"

    @pytest.mark.parametrize(
        "header",
        ["{etag}", "W/{etag}", '"other", {etag}', "*", " {etag} "],
        ids=["exact", "weak", "list", "star", "spaces"],
    )
    def test_a_matching_if_none_match_is_304_with_no_body(
        self, client, scoped_key, header
    ):
        etag = _get(client, scoped_key).headers["etag"]
        response = _get(
            client, scoped_key, **{"If-None-Match": header.format(etag=etag)}
        )
        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["etag"] == etag

    @pytest.mark.parametrize(
        "header",
        ['"deadbeef"', "", "{bare}", "W/"],
        ids=["other", "empty", "unquoted", "weak-empty"],
    )
    def test_a_non_matching_if_none_match_is_200(self, client, scoped_key, header):
        etag = _get(client, scoped_key).headers["etag"]
        response = _get(
            client, scoped_key, **{"If-None-Match": header.format(bare=etag.strip('"'))}
        )
        assert response.status_code == 200
        assert response.headers["etag"] == etag

    def test_a_scope_refusal_comes_before_the_etag(self, client, developer, scoped_key):
        etag = _get(client, scoped_key).headers["etag"]
        plain = _key(client, developer)["key"]
        response = _get(client, plain, **{"If-None-Match": etag})
        assert response.status_code == 403
        assert "etag" not in response.headers

    def test_an_evaluation_change_moves_the_etag_and_a_label_change_does_not(
        self, client, db_session, developer, scoped_key
    ):
        flag = _store(db_session, developer, rollout_percentage=10)
        first = _get(client, scoped_key).headers["etag"]

        flag.name = "Renamed"
        flag.description = "New description"
        flag.tags = ["relabelled"]
        db_session.commit()
        assert _get(client, scoped_key, **{"If-None-Match": first}).status_code == 304

        flag.rollout_percentage = 20
        db_session.commit()
        response = _get(client, scoped_key, **{"If-None-Match": first})
        assert response.status_code == 200
        assert response.headers["etag"] != first

        second = response.headers["etag"]
        db_session.delete(flag)
        db_session.commit()
        assert _get(client, scoped_key, **{"If-None-Match": second}).status_code == 200


# ---------------------------------------------------------------------------
# Contents
# ---------------------------------------------------------------------------


class TestContents:
    def test_every_owners_flags_and_nothing_else(
        self, client, db_session, developer, scoped_key
    ):
        other = _make_user(db_session, UserRole.ADMIN)
        mine = _store(
            db_session,
            developer,
            name="SENTINEL-NAME",
            description="SENTINEL-DESCRIPTION",
            tags=["SENTINEL-TAG"],
            variants={"on": "SENTINEL-VARIANT"},
            targeting_rules={
                "groups": [
                    {
                        "conditions": [
                            {"attribute": "plan", "operator": "equals", "value": "pro"}
                        ]
                    }
                ]
            },
        )
        theirs = _store(db_session, other, status=FeatureFlagStatus.INACTIVE)
        legacy = _store(
            db_session,
            other,
            targeting_rules=[{"type": "user_id", "user_ids": ["SENTINEL-USER"]}],
        )
        response = _get(client, scoped_key)
        assert response.status_code == 200
        entries = {entry["key"]: entry for entry in response.json()["flags"]}
        assert entries[mine.key]["evaluation"] == "local"
        assert entries[mine.key]["rules"][0]["match"]["groups"][0]["conditions"] == [
            {"attribute": "plan", "operator": "eq", "value": "pro"}
        ]
        assert entries[theirs.key] == {"key": theirs.key, "active": False}
        assert entries[legacy.key] == {
            "key": legacy.key,
            "active": True,
            "evaluation": "remote",
        }
        body = response.text
        for sentinel in ("SENTINEL", str(developer.id), str(other.id), developer.email):
            assert sentinel not in body

    def test_the_route_is_charged_at_the_sdk_rate(self):
        from backend.app.middleware.rate_limiter import resolve_rate_limit

        assert resolve_rate_limit(URL, 1234) == (1234, 60)


# ---------------------------------------------------------------------------
# The live-contract seed mints one plain and one scoped key
# ---------------------------------------------------------------------------


class TestContractSeed:
    def test_the_seed_writes_a_plain_and_a_scoped_key(
        self, client, db_session, developer, tmp_path, monkeypatch
    ):
        from backend.scripts import seed_sdk_contract as seed

        monkeypatch.setattr(seed, "LOCAL_KEY_FILE", tmp_path / ".api_key_local")
        name = f"sdk-contract-smoke-{uuid.uuid4().hex[:6]}"
        plain = seed.seed_api_key(
            db_session, developer, name=name, key_file=tmp_path / ".api_key"
        )
        scoped = seed.seed_local_api_key(db_session, developer)

        assert (tmp_path / ".api_key").read_text().strip() == plain
        assert (tmp_path / ".api_key_local").read_text().strip() == scoped
        assert _get(client, plain).status_code == 403
        assert _get(client, scoped).status_code == 200

        # The scoped key's owner must be able to change flags (a VIEWER is refused).
        viewer = _make_user(db_session, UserRole.VIEWER)
        with pytest.raises(SystemExit):
            seed.seed_local_api_key(db_session, viewer)

        # Idempotent: a second run keeps both keys.
        assert seed.seed_local_api_key(db_session, developer) == scoped
        assert (
            seed.seed_api_key(
                db_session, developer, name=name, key_file=tmp_path / ".api_key"
            )
            == plain
        )


# ---------------------------------------------------------------------------
# The vectors, through the real routes and a real database
# ---------------------------------------------------------------------------


def _has_lone_surrogate(value) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, list):
        return any(_has_lone_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(
            _has_lone_surrogate(key) or _has_lone_surrogate(item)
            for key, item in value.items()
        )
    return False


class TestVectorsThroughTheRoutes:
    @pytest.fixture
    def stored_corpus(self, db_session, developer):
        vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
        keys = [spec["key"] for spec in vectors["stored_flags"]]
        db_session.query(FeatureFlag).filter(FeatureFlag.key.in_(keys)).delete(
            synchronize_session=False
        )
        for spec in vectors["stored_flags"]:
            db_session.add(
                FeatureFlag(
                    key=spec["key"],
                    name=spec["key"][:100],
                    status=FeatureFlagStatus[spec["status"]],
                    owner_id=developer.id,
                    rollout_percentage=spec["rollout_percentage"],
                    targeting_rules=spec["targeting_rules"],
                )
            )
        # The segment the segment flags name, stored with the members the
        # generator's stubbed resolver gives (#440), so the routes answer
        # them from the database as the vectors expect. The unknown one is
        # left absent.
        db_session.query(Segment).filter(
            Segment.id == uuid.UUID(VECTOR_SEGMENT)
        ).delete(synchronize_session=False)
        db_session.add(
            Segment(
                id=uuid.UUID(VECTOR_SEGMENT),
                name="Vector segment",
                kind=SegmentKind.ID_LIST.value,
                status=SegmentStatus.ACTIVE,
                owner_id=developer.id,
            )
        )
        db_session.flush()
        for member in VECTOR_SEGMENT_MEMBERS[VECTOR_SEGMENT]:
            db_session.add(
                SegmentMember(segment_id=uuid.UUID(VECTOR_SEGMENT), member_id=member)
            )
        db_session.commit()
        yield vectors
        db_session.rollback()
        db_session.query(Segment).filter(
            Segment.id == uuid.UUID(VECTOR_SEGMENT)
        ).delete(synchronize_session=False)
        db_session.commit()

    def test_the_served_entries_are_the_vectors_entries(
        self, client, stored_corpus, scoped_key
    ):
        served = {e["key"]: e for e in _get(client, scoped_key).json()["flags"]}
        for entry in stored_corpus["ruleset"]["flags"]:
            assert served[entry["key"]] == entry

    def test_the_evaluate_endpoint_gives_every_expected_answer(
        self, client, stored_corpus, scoped_key, monkeypatch
    ):
        """GET /feature-flags/evaluate/{key}, as the SDKs call it, per case.

        A case with an ill-formed string cannot be put in a URL at all (the
        SDKs' remote call fails before sending), so only well-formed cases go
        through HTTP; every ``must_local`` case is well-formed.

        The per-evaluation metric rows are not what this checks, and writing
        two thousand of them dominates the run, so that write is stubbed; the
        answer is computed by the unmodified route and service.
        """
        from backend.app.services.metrics_service import MetricsService

        monkeypatch.setattr(
            MetricsService, "record_flag_evaluation", staticmethod(lambda **_: None)
        )
        checked = 0
        wrong = []
        for case in stored_corpus["cases"]:
            if _has_lone_surrogate(case["user_id"]) or _has_lone_surrogate(
                case["context"]
            ):
                assert not case["must_local"], case["id"]
                continue
            params = {"user_id": case["user_id"]}
            if case["context"] is not None:
                params["context"] = json.dumps(case["context"])
            response = client.get(
                f"/api/v1/feature-flags/evaluate/{quote(case['flag'], safe='')}",
                params=params,
                headers={"X-API-Key": scoped_key},
            )
            assert response.status_code == 200, (case["id"], response.text)
            answer = {k: response.json()[k] for k in ("enabled", "reason")}
            if answer != case["expected"]:
                wrong.append((case["id"], answer, case["expected"]))
            checked += 1
        assert not wrong, wrong[:10]
        assert checked >= stored_corpus["counts"]["must_local"]

"""A duplicate flag key answers 409, and the collection answers without a slash (#94).

Create and update each check the key before writing, which leaves a gap: a
second request can store the same key between the check and the commit, and
the unique index on ``key`` then refuses the first. That refusal used to come
out as a 500. Update also checked against a scan of the first 100 flags
(unordered), so on a larger table it missed the clash entirely and the index
refused it -- again a 500.

The races are made deterministic by opening exactly that gap: the service
method is wrapped so the clashing row is committed just before the original
runs. No timing is asserted anywhere.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1.endpoints import feature_flags as flag_endpoints
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.tests.integration.helpers import unique_flag_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/feature-flags"


def _detail(key: str) -> str:
    return f"Feature flag with key '{key}' already exists"


def _store_flag(db_session, owner_id, key: str) -> FeatureFlag:
    flag = FeatureFlag(
        key=key,
        name=f"Stored {key}",
        status=FeatureFlagStatus.INACTIVE,
        owner_id=owner_id,
        rollout_percentage=0,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


@pytest.mark.regression
def test_create_race_is_409(admin_client, db_session, admin_user, monkeypatch):
    """Another create stores the key after the pre-check: 409, not 500."""
    key = unique_flag_key("race-create")
    original = FeatureFlagService.create_feature_flag

    def create_after_a_concurrent_one(self, flag_data, owner_id, **kwargs):
        _store_flag(db_session, admin_user.id, flag_data.key)
        return original(self, flag_data, owner_id, **kwargs)

    monkeypatch.setattr(
        FeatureFlagService, "create_feature_flag", create_after_a_concurrent_one
    )

    response = admin_client.post(
        f"{COLLECTION}/", json={"key": key, "name": "Second", "is_active": False}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _detail(key)
    rows = db_session.query(FeatureFlag).filter(FeatureFlag.key == key).count()
    assert rows == 1


@pytest.mark.regression
def test_put_key_race_is_409(
    admin_client, db_session, admin_user, make_feature_flag, monkeypatch
):
    """Another write stores the key after the update's check: 409, not 500."""
    flag = make_feature_flag(key=unique_flag_key("race-put"))
    wanted = unique_flag_key("race-put-target")
    original = FeatureFlagService.update_feature_flag

    def update_after_a_concurrent_create(self, flag_id, flag_data, **kwargs):
        _store_flag(db_session, admin_user.id, flag_data.key)
        return original(self, flag_id, flag_data, **kwargs)

    monkeypatch.setattr(
        FeatureFlagService, "update_feature_flag", update_after_a_concurrent_create
    )

    response = admin_client.put(f"{COLLECTION}/{flag.id}", json={"key": wanted})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _detail(wanted)
    db_session.expire_all()
    assert db_session.get(FeatureFlag, flag.id).key == flag.key


@pytest.mark.regression
def test_put_key_onto_flag_past_first_100_is_409(
    admin_client, db_session, admin_user, make_feature_flag
):
    """130 flags: changing one flag's key to any other's answers 409.

    The old check scanned ``get_feature_flags()`` -- 100 rows, unordered -- so
    every clash with a flag outside that page reached the unique index and
    answered 500.
    """
    prefix = f"past100-{uuid.uuid4().hex[:6]}"
    others = [
        FeatureFlag(
            key=f"{prefix}-{i:03d}",
            name=f"Other {i}",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        for i in range(130)
    ]
    db_session.add_all(others)
    db_session.commit()
    mover = make_feature_flag(key=unique_flag_key("past100-mover"))

    statuses: dict[int, int] = {}
    for other in others:
        response = admin_client.put(f"{COLLECTION}/{mover.id}", json={"key": other.key})
        statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
        assert response.status_code == 409, (other.key, response.text)
        assert response.json()["detail"] == _detail(other.key)

    assert statuses == {409: 130}
    db_session.expire_all()
    assert db_session.get(FeatureFlag, mover.id).key == mover.key


@pytest.mark.regression
def test_put_key_check_past_first_100_refuses_before_any_write(
    admin_client, db_session, admin_user, make_feature_flag, monkeypatch
):
    """The key check alone answers 409 past row 100; the write is never reached.

    The IntegrityError fallback would also turn a missed clash into 409, so
    the test above passes on either one. This one takes the write away: the
    service update fails the request if it is called, so only the direct key
    query before it can answer 409.
    """
    prefix = f"precheck-{uuid.uuid4().hex[:6]}"
    others = [
        FeatureFlag(
            key=f"{prefix}-{i:03d}",
            name=f"Other {i}",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        for i in range(130)
    ]
    db_session.add_all(others)
    db_session.commit()
    mover = make_feature_flag(key=unique_flag_key("precheck-mover"))

    def the_write_must_not_be_reached(self, flag_id, flag_data, **kwargs):
        raise AssertionError(f"update reached the write for key {flag_data.key!r}")

    monkeypatch.setattr(
        FeatureFlagService, "update_feature_flag", the_write_must_not_be_reached
    )

    for other in others:
        response = admin_client.put(f"{COLLECTION}/{mover.id}", json={"key": other.key})
        assert response.status_code == 409, (other.key, response.text)
        assert response.json()["detail"] == _detail(other.key)


def test_put_to_a_free_key_still_succeeds(admin_client, make_feature_flag):
    """The direct query excludes the flag itself and only refuses a taken key."""
    flag = make_feature_flag(key=unique_flag_key("free"))
    same = admin_client.put(f"{COLLECTION}/{flag.id}", json={"key": flag.key})
    assert same.status_code == 200, same.text
    wanted = unique_flag_key("free-new")
    moved = admin_client.put(f"{COLLECTION}/{flag.id}", json={"key": wanted})
    assert moved.status_code == 200, moved.text
    assert moved.json()["key"] == wanted


def _integrity_error(pgcode: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, SimpleNamespace(pgcode=pgcode))


def test_only_a_unique_violation_on_a_taken_key_is_409(
    db_session, admin_user, make_feature_flag
):
    """Any other refusal is left to the caller, which re-raises it."""
    taken = make_feature_flag(key=unique_flag_key("mapped"))
    free = unique_flag_key("mapped-free")

    with pytest.raises(HTTPException) as caught:
        flag_endpoints._raise_if_key_conflict(
            db_session, _integrity_error("23505"), taken.key, None
        )
    assert caught.value.status_code == 409

    # A unique violation, but the key is free: something else clashed.
    flag_endpoints._raise_if_key_conflict(
        db_session, _integrity_error("23505"), free, None
    )
    # The key is taken, but the refusal is not a unique violation.
    flag_endpoints._raise_if_key_conflict(
        db_session, _integrity_error("23503"), taken.key, None
    )
    # The only flag with the key is the one being updated.
    flag_endpoints._raise_if_key_conflict(
        db_session, _integrity_error("23505"), taken.key, taken.id
    )


# --- the collection without a trailing slash ----------------------------------


@pytest.mark.regression
def test_the_collection_answers_without_a_trailing_slash(
    admin_client, make_feature_flag
):
    """GET and POST at ``/api/v1/feature-flags`` are served, not redirected."""
    listed = admin_client.get(COLLECTION, follow_redirects=False)
    assert listed.status_code == 200, (listed.status_code, listed.headers)
    assert "items" in listed.json()

    key = unique_flag_key("noslash")
    created = admin_client.post(
        COLLECTION,
        json={"key": key, "name": "No slash", "is_active": False},
        follow_redirects=False,
    )
    assert created.status_code == 201, (created.status_code, created.text)
    assert created.json()["key"] == key

    # The duplicate is refused on the twin exactly as on the slash form.
    again = admin_client.post(
        COLLECTION,
        json={"key": key, "name": "Again", "is_active": False},
        follow_redirects=False,
    )
    assert again.status_code == 409, again.text


def test_a_single_flag_url_with_a_trailing_slash_still_redirects(
    admin_client, make_feature_flag
):
    """Only the collection is twinned; ``/{flag_id}/`` keeps its 307."""
    flag = make_feature_flag(key=unique_flag_key("slash-detail"))
    response = admin_client.get(f"{COLLECTION}/{flag.id}/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith(f"{COLLECTION}/{flag.id}")


def test_only_the_slash_form_is_in_the_openapi_document():
    paths = app.openapi()["paths"]
    assert f"{COLLECTION}/" in paths
    assert COLLECTION not in paths
    for method, operation_id in (("post", "create"), ("put", "update")):
        path = f"{COLLECTION}/" if method == "post" else f"{COLLECTION}/{{flag_id}}"
        assert "409" in paths[path][method]["responses"], operation_id

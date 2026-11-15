"""A key with no scopes answers for whichever user id it is sent.

Through the real routes, against a real database, with a key created without
scopes (what a browser or a mobile app is told to use):

* ``POST /feature-flags/evaluate/{key}`` answers a flag that targets an ID-list
  segment for any ``user_id``: the member is served the flag, the other id is
  not, both with the same key;
* ``POST /tracking/assign`` assigns any ``user_id`` to an experiment;
* ``GET /tracking/assignments/{user_id}`` lists the experiments a given id is
  assigned to, for an id the key's owner never was.

The docs say this in ``docs/security/api-keys.md``, ``docs/guides/segments.md``
and ``docs/sdk/javascript.md``.
"""

from __future__ import annotations

import uuid
from typing import Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.segment import Segment, SegmentKind, SegmentMember
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

KEYS_URL = "/api/v1/api-keys"
SCHEMA = "test_experimentation"
MEMBER = "browser-key-member"
OTHER = "browser-key-other"


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
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


@pytest.fixture
def unscoped_key(client, db_session) -> Dict[str, str]:
    """A key created with no scopes, by a developer."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"plainkey_{suffix}",
        email=f"plainkey_{suffix}@plainkey.test",
        full_name="Plain Key Owner",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    response = client.post(
        KEYS_URL,
        json={"name": f"plain-{suffix}"},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert response.status_code in (200, 201), response.text
    assert not response.json().get("scopes")
    yield {"X-API-Key": response.json()["key"]}
    db_session.rollback()
    db_session.query(User).filter(User.id == user.id).delete(False)
    db_session.commit()


@pytest.fixture
def world(db_session, admin_user):
    """An ID-list segment holding MEMBER, a flag that targets it, and a running
    experiment with two arms; no global holdout active. All removed after."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()

    suffix = uuid.uuid4().hex[:8]
    segment = Segment(
        name=f"Browser key {suffix}",
        kind=SegmentKind.ID_LIST.value,
        rules=None,
        owner_id=admin_user.id,
    )
    db_session.add(segment)
    db_session.commit()
    db_session.add(SegmentMember(segment_id=segment.id, member_id=MEMBER))
    flag = FeatureFlag(
        key=f"browser-key-flag-{suffix}",
        name="Browser key flag",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=admin_user.id,
        rollout_percentage=0,
        targeting_rules={
            "groups": [
                {
                    "conditions": [
                        {
                            "attribute": "segment",
                            "operator": "in_segment",
                            "value": str(segment.id),
                        }
                    ]
                }
            ]
        },
    )
    experiment = Experiment(
        name=f"Browser key experiment {suffix}",
        key=f"browser-key-exp-{suffix}",
        status=ExperimentStatus.ACTIVE,
        owner_id=admin_user.id,
        description="d",
        hypothesis="h",
    )
    db_session.add_all([flag, experiment])
    db_session.commit()
    for i, name in enumerate(("control", "treatment")):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                is_control=i == 0,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    db_session.refresh(flag)
    db_session.refresh(experiment)
    ids = (segment.id, flag.id, experiment.id)
    yield type("World", (), {"flag": flag, "experiment": experiment})
    db_session.rollback()
    db_session.query(Event).filter(Event.experiment_id == ids[2]).delete(False)
    db_session.query(Assignment).filter(Assignment.experiment_id == ids[2]).delete(
        False
    )
    db_session.query(Variant).filter(Variant.experiment_id == ids[2]).delete(False)
    db_session.query(Experiment).filter(Experiment.id == ids[2]).delete(False)
    db_session.query(FeatureFlag).filter(FeatureFlag.id == ids[1]).delete(False)
    db_session.query(Segment).filter(Segment.id == ids[0]).delete(False)
    db_session.commit()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


def test_a_flag_answers_for_any_user_id_and_an_id_list_decides_by_that_id(
    client, unscoped_key, world
):
    url = f"/api/v1/feature-flags/evaluate/{world.flag.key}"
    served = {}
    for user_id in (MEMBER, OTHER):
        response = client.post(url, json={"user_id": user_id}, headers=unscoped_key)
        assert response.status_code == 200, response.text
        served[user_id] = (response.json()["enabled"], response.json()["reason"])
    assert served[MEMBER] == (True, "targeting_rule")
    assert served[OTHER] == (False, "rollout")


def test_assign_and_the_assignments_list_answer_for_any_user_id(
    client, unscoped_key, world
):
    for user_id in (MEMBER, OTHER):
        response = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": world.experiment.key, "user_id": user_id},
            headers=unscoped_key,
        )
        assert response.status_code == 200, response.text
        assert response.json()["assigned"] is True

    for user_id in (MEMBER, OTHER):
        listed = client.get(
            f"/api/v1/tracking/assignments/{user_id}", headers=unscoped_key
        )
        assert listed.status_code == 200, listed.text
        assert [row["experiment_id"] for row in listed.json()] == [
            str(world.experiment.id)
        ]

    nobody = client.get(
        "/api/v1/tracking/assignments/never-assigned", headers=unscoped_key
    )
    assert nobody.status_code == 200
    assert nobody.json() == []

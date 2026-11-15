"""Stored rules the server cannot read affect only their owner (#1674), through
the routes and a real database.

The rows are written directly, as only a direct database write can leave them
(the write validators refuse both values): a geo radius of ``10**400``, which
PostgreSQL's ``jsonb`` stores exactly and ``float()`` cannot read
(``OverflowError``), and a semantic version ``"1.²"`` (``ValueError``). On
main every route below that reads more than one owner answered 500:

* ``GET /feature-flags/user/{id}`` for every flag, also when a flag names a
  segment holding such rules;
* ``GET /sdk/ruleset``, so no SDK could load a ruleset;
* ``POST /feature-flags/`` naming that segment (now 422), and
  ``POST /segments/{id}/evaluate`` on it (now its documented 409).

The DB-free rows: ``backend/tests/unit/services/test_unreadable_stored_rules.py``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.segments import SEGMENT_RULES_NOT_VALID
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.segment import Segment, SegmentKind
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
FLAGS = "/api/v1/feature-flags"
SEGMENTS = "/api/v1/segments"
ASSIGN = "/api/v1/tracking/assign"
RULESET = "/api/v1/sdk/ruleset"
USER = "user-1674"
US = {"country": "US"}


def cond(attribute: str, operator: str, value: Any) -> Dict[str, Any]:
    return {"attribute": attribute, "operator": operator, "value": value}


def dashboard(*conditions: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


GEO = dashboard(
    cond("location", "geo_within_radius", {"lat": 1, "lon": 2, "radius": 10**400})
)
SEMVER = dashboard(cond("app_version", "semver_gte", "1.²"))
US_RULES = dashboard(cond("country", "equals", "US"))


def names(segment_id: str) -> Dict[str, Any]:
    return dashboard(cond("segment", "in_segment", segment_id))


@pytest.fixture
def world(db_session, admin_user):
    """Rows written directly; everything removed afterwards."""
    made: Dict[str, List[Any]] = {"segments": [], "flags": [], "experiments": []}
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()

    def segment(rules: Any) -> str:
        row = Segment(
            name=f"Seg 1674 {uuid.uuid4().hex[:8]}",
            kind=SegmentKind.RULES.value,
            rules=rules,
            status=ModelSegmentStatus.ACTIVE,
            owner_id=admin_user.id,
        )
        db_session.add(row)
        db_session.commit()
        made["segments"].append(row.id)
        return str(row.id)

    def flag(name: str, rules: Any, rollout: int = 100) -> FeatureFlag:
        row = FeatureFlag(
            key=f"r1674-{name}-{uuid.uuid4().hex[:8]}",
            name=f"r1674 {name}",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=rollout,
            targeting_rules=rules,
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        made["flags"].append(row.id)
        return row

    def experiment(rules: Any) -> Experiment:
        suffix = uuid.uuid4().hex[:10]
        row = Experiment(
            name=f"r1674 experiment {suffix}",
            key=f"r1674-exp-{suffix}",
            status=ExperimentStatus.ACTIVE,
            owner_id=admin_user.id,
            description="d",
            hypothesis="h",
            targeting_rules=rules,
        )
        db_session.add(row)
        db_session.commit()
        for i, variant in enumerate(("control", "treatment")):
            db_session.add(
                Variant(
                    experiment_id=row.id,
                    name=variant,
                    is_control=i == 0,
                    traffic_allocation=50,
                    configuration={},
                )
            )
        db_session.commit()
        db_session.refresh(row)
        made["experiments"].append(row.id)
        return row

    yield type(
        "World", (), {"segment": segment, "flag": flag, "experiment": experiment}
    )
    db_session.rollback()
    if made["experiments"]:
        ids = made["experiments"]
        db_session.query(Event).filter(Event.experiment_id.in_(ids)).delete(False)
        db_session.query(Assignment).filter(Assignment.experiment_id.in_(ids)).delete(
            False
        )
        db_session.query(Variant).filter(Variant.experiment_id.in_(ids)).delete(False)
        db_session.query(Experiment).filter(Experiment.id.in_(ids)).delete(False)
    created = [
        row.id
        for row in db_session.query(FeatureFlag.id).filter(
            FeatureFlag.key.like("r1674-%")
        )
    ]
    if created:
        for model in (RawMetric, ErrorLog):
            db_session.query(model).filter(model.feature_flag_id.in_(created)).delete(
                False
            )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(created)).delete(False)
    if made["segments"]:
        db_session.query(Segment).filter(Segment.id.in_(made["segments"])).delete(False)
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


@pytest.fixture
def client(admin_client):
    """The admin client's overrides, answering a server error as its 500
    (the status a caller sees) instead of raising it into the test."""
    return TestClient(app, raise_server_exceptions=False)


def user_flags(client, context=US) -> Dict[str, bool]:
    response = client.get(
        f"{FLAGS}/user/{USER}", params={"context": json.dumps(context)}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# GET /feature-flags/user/{user_id}
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("rules", [GEO, SEMVER], ids=["geo_overflow", "semver_digit"])
def test_the_bulk_route_answers_every_other_flag(client, world, rules):
    """On main: 500. Now 200, the bad flag (at 100%) false, the rest as before."""
    bad = world.flag("bad", rules)
    on = world.flag("on", US_RULES, rollout=0)
    off = world.flag("off", None, rollout=0)
    answers = user_flags(client)
    assert {key: answers[key] for key in (bad.key, on.key, off.key)} == {
        bad.key: False,
        on.key: True,
        off.key: False,
    }


@pytest.mark.regression
def test_the_bulk_route_answers_every_other_flag_when_a_segment_is_unreadable(
    client, world
):
    """On main a flag naming the segment made the whole bulk call 500."""
    segment = world.segment(GEO)
    good_segment = world.segment(US_RULES)
    bad = world.flag("names-bad", names(segment))
    good = world.flag("names-good", names(good_segment), rollout=0)
    plain = world.flag("plain", None)
    answers = user_flags(client)
    assert {key: answers[key] for key in (bad.key, good.key, plain.key)} == {
        bad.key: False,
        good.key: True,
        plain.key: True,
    }


@pytest.mark.parametrize("rules", [GEO, SEMVER], ids=["geo", "semver"])
def test_evaluate_answers_error_as_before(client, world, rules):
    """Already so on main; pinned."""
    bad = world.flag("eval", rules)
    response = client.post(
        f"{FLAGS}/evaluate/{bad.key}", json={"user_id": USER, "context": US}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["enabled"], response.json()["reason"]) == (False, "error")


# ---------------------------------------------------------------------------
# GET /api/v1/sdk/ruleset, with a real scoped key
# ---------------------------------------------------------------------------


@pytest.fixture
def key_client(db_session, monkeypatch):
    """Only get_db overridden: the key is created and authenticated for real."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
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

    app.dependency_overrides.clear()
    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def ruleset_key(client, db_session) -> str:
    suffix = uuid.uuid4().hex[:8]
    developer = User(
        username=f"r1674_{suffix}",
        email=f"r1674_{suffix}@ruleset.test",
        full_name="r1674",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(developer)
    db_session.commit()
    db_session.refresh(developer)
    response = client.post(
        "/api/v1/api-keys",
        json={"name": f"r1674-{suffix}", "scopes": ["sdk:ruleset"]},
        headers={"Authorization": f"Bearer {create_local_access_token(developer)}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["key"]


@pytest.mark.regression
def test_the_ruleset_route_serves_the_flag_remote(key_client, db_session, world):
    """On main: 500 for every SDK. Now 200, the bad flags remote, the good
    flag local with its rules."""
    geo = world.flag("ruleset-geo", GEO)
    semver = world.flag("ruleset-semver", SEMVER)
    good = world.flag("ruleset-good", US_RULES, rollout=25)
    response = key_client.get(
        RULESET, headers={"X-API-Key": ruleset_key(key_client, db_session)}
    )
    assert response.status_code == 200, response.text
    entries = {entry["key"]: entry for entry in response.json()["flags"]}
    for bad in (geo, semver):
        assert entries[bad.key] == {
            "key": bad.key,
            "active": True,
            "evaluation": "remote",
        }
    assert entries[good.key]["evaluation"] == "local"
    assert entries[good.key]["rollout_percentage"] == 25
    assert [rule["id"] for rule in entries[good.key]["rules"]] == ["dashboard"]


# ---------------------------------------------------------------------------
# Segments (EMG5-6) and saving a flag that names one
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_segment_evaluate_answers_its_documented_409(client, world):
    """On main: 500. Now the 409 every other unreadable segment answers."""
    segment = world.segment(GEO)
    response = client.post(f"{SEGMENTS}/{segment}/evaluate", json={"user_context": US})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": SEGMENT_RULES_NOT_VALID}


def test_segment_bulk_evaluate_reports_false_and_answers_the_rest(client, world):
    bad = world.segment(GEO)
    good = world.segment(US_RULES)
    response = client.post(
        f"{SEGMENTS}/bulk-evaluate",
        json={"user_context": US, "segment_ids": [bad, good]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["memberships"] == {bad: False, good: True}


@pytest.mark.regression
def test_saving_a_flag_that_names_the_segment_answers_422(client, world):
    """On main: 500. Now the 422 a segment whose rules are not valid gets."""
    segment = world.segment(GEO)
    response = client.post(
        f"{FLAGS}/",
        json={
            "key": f"r1674-save-{uuid.uuid4().hex[:8]}",
            "name": "r1674 save",
            "targeting_rules": names(segment),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["body", "targeting_rules"],
            "msg": "groups[0].conditions[0].value: segment rules not valid",
            "type": "value_error",
        }
    ]


# ---------------------------------------------------------------------------
# Experiments: the issue's experiment clause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rules", [GEO, SEMVER], ids=["geo", "semver"])
def test_assign_refuses_a_user_of_an_experiment_whose_rules_raise(
    client, db_session, world, rules
):
    """Already so on main (the targeting catch answers "Targeting evaluation
    error"); pinned through the route: control, not assigned, nothing stored."""
    experiment = world.experiment(rules)
    response = client.post(
        ASSIGN, json={"experiment_key": experiment.key, "user_id": USER, "context": US}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["assigned"], body["reason"]) == (False, "targeting")
    db_session.expire_all()
    assert (
        db_session.query(Assignment)
        .filter(Assignment.experiment_id == experiment.id)
        .count()
        == 0
    )

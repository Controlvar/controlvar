"""Experiment metrics keep their type, and can be replaced by name (#558, #557).

#558: every ``metric_type`` the request schema accepts was stored as
``conversion``. The service looked the value up by enum *name*
(``MetricType["revenue"]``), which never matches the upper-case names, and
fell back to ``CONVERSION`` without a word. A revenue metric then read back,
and was labelled in ``/results``, as a conversion. Each accepted value is now
stored as the model value of the same spelling, through the one table in
``experiment_service.METRIC_TYPE_STORED_AS``; a value with no entry there is
refused with a fixed message.

#557: replacing an experiment's metrics through ``PUT`` while keeping a
metric's name answered 500. The old rows were deleted and the new ones
inserted in the same flush, and the inserts reach the database first, so the
unique index on (experiment, metric name) refused them. The deletes are now
flushed before the new rows are added. Variants are replaced the same way;
they carry no such index, and the variant test pins that they still work.

Each request is a real one: a DEVELOPER with a local JWT from
``create_local_access_token`` and no dependency override except
``deps.get_db``. What was stored is read back in a session of the test's own.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, Metric, Variant
from backend.app.models.experiment import MetricType as ModelMetricType
from backend.app.models.user import User, UserRole
from backend.app.schemas.experiment import MetricType as SchemaMetricType

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
PREFIX = "mtype"

#: Every value the request schema accepts for ``metric_type``.
ACCEPTED = [member.value for member in SchemaMetricType]

#: A value the schema does not accept. It must not come back in the answer.
UNKNOWN = "mtype-not-a-type"


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def developer(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_developer_{suffix}",
        email=f"{PREFIX}_developer_{suffix}@metric-type.test",
        full_name="Metric Type Developer",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def fresh(test_db):
    """Run ``work(session)`` in a session of the test's own and commit."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def run(work):
        session = factory()
        try:
            session.execute(text("SET search_path TO test_experimentation"))
            result = work(session)
            session.commit()
            return result
        finally:
            session.close()

    return run


@pytest.fixture
def client(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

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


def _metric(name: str, event_name: str, metric_type: str, primary: bool) -> dict:
    return {
        "name": name,
        "event_name": event_name,
        "metric_type": metric_type,
        "is_primary": primary,
    }


def _body(name: str, metrics: list) -> dict:
    return {
        "name": name,
        "description": "Metric types and replacement",
        "hypothesis": "A metric keeps the type it was sent with",
        "experiment_type": "a_b",
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": metrics,
    }


def _create(client, developer, metrics: list) -> str:
    body = _body(f"{PREFIX} {uuid.uuid4().hex[:8]}", metrics)
    response = client.post(f"{EXPERIMENTS}/", json=body, headers=_auth(developer))
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def draft(client, developer):
    """A DRAFT experiment with one conversion metric; returns its id."""
    return _create(
        client,
        developer,
        [_metric("Conversion Rate", "purchase", "conversion", True)],
    )


def _stored_metrics(fresh, experiment_id) -> dict:
    """``{name: (event_name, metric_type)}`` of the experiment's metric rows."""

    def work(session):
        rows = (
            session.query(Metric)
            .filter(Metric.experiment_id == uuid.UUID(experiment_id))
            .all()
        )
        return {row.name: (row.event_name, row.metric_type) for row in rows}

    return fresh(work)


def _stored_variants(fresh, experiment_id) -> dict:
    """``{name: traffic_allocation}`` of the experiment's variant rows."""

    def work(session):
        rows = (
            session.query(Variant)
            .filter(Variant.experiment_id == uuid.UUID(experiment_id))
            .all()
        )
        return {row.name: row.traffic_allocation for row in rows}

    return fresh(work)


def _read_back_types(client, developer, experiment_id) -> dict:
    response = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=_auth(developer))
    assert response.status_code == 200, response.text
    return {m["name"]: m["metric_type"] for m in response.json()["metrics"]}


def _refusal_is_fixed(response) -> None:
    """A 422 on the metric's type, whose body does not repeat the value."""
    assert response.status_code == 422, response.text
    assert UNKNOWN not in response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail, detail
    assert any("metric_type" in [str(p) for p in e["loc"]] for e in detail), detail


# ---------------------------------------------------------------------------
# #558: the type is kept
# ---------------------------------------------------------------------------


def test_every_accepted_type_has_a_stored_value():
    """The mapping table covers the schema exactly, value for value."""
    from backend.app.services.experiment_service import METRIC_TYPE_STORED_AS

    assert set(METRIC_TYPE_STORED_AS) == set(ACCEPTED)
    for value, stored in METRIC_TYPE_STORED_AS.items():
        assert isinstance(stored, ModelMetricType)
        assert stored.value == value


@pytest.mark.regression
@pytest.mark.parametrize("metric_type", ACCEPTED)
def test_create_stores_the_metric_type_sent(client, developer, fresh, metric_type):
    experiment_id = _create(
        client,
        developer,
        [_metric("Primary", "primary_event", metric_type, True)],
    )

    assert _read_back_types(client, developer, experiment_id) == {
        "Primary": metric_type
    }
    assert _stored_metrics(fresh, experiment_id) == {
        "Primary": ("primary_event", ModelMetricType(metric_type))
    }


@pytest.mark.regression
def test_create_with_an_unknown_metric_type_is_refused(client, developer, fresh):
    name = f"{PREFIX} unknown {uuid.uuid4().hex[:8]}"
    body = _body(name, [_metric("Primary", "primary_event", UNKNOWN, True)])

    response = client.post(f"{EXPERIMENTS}/", json=body, headers=_auth(developer))

    _refusal_is_fixed(response)

    def work(session):
        return session.query(Experiment).filter(Experiment.name == name).count()

    assert fresh(work) == 0


@pytest.mark.regression
@pytest.mark.parametrize("metric_type", ACCEPTED)
def test_update_stores_the_metric_type_sent(
    client, developer, draft, fresh, metric_type
):
    response = client.put(
        f"{EXPERIMENTS}/{draft}",
        json={"metrics": [_metric("Primary", "primary_event", metric_type, True)]},
        headers=_auth(developer),
    )

    assert response.status_code == 200, response.text
    assert {m["name"]: m["metric_type"] for m in response.json()["metrics"]} == {
        "Primary": metric_type
    }
    assert _read_back_types(client, developer, draft) == {"Primary": metric_type}
    assert _stored_metrics(fresh, draft) == {
        "Primary": ("primary_event", ModelMetricType(metric_type))
    }


@pytest.mark.regression
def test_update_with_an_unknown_metric_type_is_refused(client, developer, draft, fresh):
    before = _stored_metrics(fresh, draft)

    response = client.put(
        f"{EXPERIMENTS}/{draft}",
        json={"metrics": [_metric("Primary", "primary_event", UNKNOWN, True)]},
        headers=_auth(developer),
    )

    _refusal_is_fixed(response)
    assert _stored_metrics(fresh, draft) == before


# ---------------------------------------------------------------------------
# #557: replacing while keeping a name
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_replacing_metrics_keeping_a_name_succeeds(client, developer, draft, fresh):
    new_metrics = [
        _metric("Conversion Rate", "checkout_completed", "conversion", True),
        _metric("Revenue", "order_value", "revenue", False),
    ]

    response = client.put(
        f"{EXPERIMENTS}/{draft}",
        json={"metrics": new_metrics},
        headers=_auth(developer),
    )

    assert response.status_code == 200, response.text
    assert _stored_metrics(fresh, draft) == {
        "Conversion Rate": ("checkout_completed", ModelMetricType.CONVERSION),
        "Revenue": ("order_value", ModelMetricType.REVENUE),
    }


def test_replacing_variants_keeping_their_names_succeeds(
    client, developer, draft, fresh
):
    response = client.put(
        f"{EXPERIMENTS}/{draft}",
        json={
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 30},
                {"name": "Treatment", "is_control": False, "traffic_allocation": 70},
            ]
        },
        headers=_auth(developer),
    )

    assert response.status_code == 200, response.text
    assert _stored_variants(fresh, draft) == {"Control": 30, "Treatment": 70}

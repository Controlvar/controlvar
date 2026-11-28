"""What the experiment and results routes answer when they fail unexpectedly.

Every route here used to put the text of whatever it caught into ``detail``.
Now each answers a fixed sentence with the request ID -- the form the
experiment-create and tracking routes already use -- and the full error goes to
the server log under that ID. Deliberate 4xx messages are not touched.

Each case makes the call inside the route's ``try`` raise an error whose text
carries marker strings, then checks the status is unchanged, the body is
exactly the documented sentence with the request ID, and none of the markers
reached the body.

The requests are real: real ``User`` rows, a real local JWT from
``create_local_access_token``, and no dependency override except
``deps.get_db``. Read routes are called as a VIEWER, the lowest role that
reaches them; change routes as a non-superuser DEVELOPER.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import experiments as experiments_endpoints
from backend.app.api.v1.endpoints import results as results_endpoints
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.experiment_service import ExperimentService
from backend.app.services.fdr_correction_service import BenjaminiHochbergService

pytestmark = [pytest.mark.integration]

#: What the planted error says. None of it may reach a response body.
CANARY = "CANARY UPDATE experimentation.x Failing row contains (42, secret)"
FORBIDDEN_IN_BODY = ("CANARY", "UPDATE ", "Failing row")

REQUEST_ID = "fixed-msg-540"


def _expected(sentence: str) -> dict:
    return {"detail": f"{sentence} (request ID: {REQUEST_ID})."}


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(CANARY)


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(scope="module")
def people(test_db):
    factory = sessionmaker(bind=test_db, expire_on_commit=False)
    session = factory()
    session.execute(text("SET search_path TO test_experimentation"))
    suffix = uuid.uuid4().hex[:8]

    def make(name: str, role: UserRole) -> User:
        user = User(
            username=f"fixedmsg_{name}_{suffix}",
            email=f"fixedmsg_{name}_{suffix}@fixedmsg.test",
            full_name="Fixed Message User",
            hashed_password="unused: these users sign in by token only",
            is_active=True,
            is_superuser=False,
            role=role,
        )
        session.add(user)
        return user

    users = {
        "viewer": make("viewer", UserRole.VIEWER),
        "developer": make("developer", UserRole.DEVELOPER),
    }
    session.commit()
    try:
        yield users
    finally:
        session.close()


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


def _auth(user: User) -> dict:
    return {
        "Authorization": f"Bearer {create_local_access_token(user)}",
        "X-Request-ID": REQUEST_ID,
    }


def _payload() -> dict:
    return {
        "name": f"fixedmsg {uuid.uuid4().hex[:8]}",
        "description": "An experiment for a failing route",
        "hypothesis": "A failure answers a fixed message",
        "experiment_type": "a_b",
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Conversion Rate",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }


def _experiment(client, db_session, people, status: ExperimentStatus) -> str:
    """Created through the API by the DEVELOPER, then put in ``status``."""
    response = client.post(
        "/api/v1/experiments/", json=_payload(), headers=_auth(people["developer"])
    )
    assert response.status_code == 201, response.text
    experiment_id = response.json()["id"]
    row = db_session.get(Experiment, uuid.UUID(experiment_id))
    row.status = status
    db_session.commit()
    return experiment_id


def _assert_fixed(response, status_code: int, sentence: str) -> None:
    assert response.status_code == status_code, response.text
    assert response.json() == _expected(sentence)
    assert response.headers["x-request-id"] == REQUEST_ID
    for marker in FORBIDDEN_IN_BODY:
        assert marker not in response.text, f"{marker!r} reached the body"


# --- plants -------------------------------------------------------------------


class _NotEncodable:
    """An ORM-ish result the route's fallback serialisation has to handle:
    ``jsonable_encoder`` is made to refuse it, and ``model_dump`` then fails."""

    def model_dump(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(CANARY)


def _refuse_encoding(monkeypatch):
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("model_dump() got an unexpected keyword argument 'mode'")

    monkeypatch.setattr(experiments_endpoints, "jsonable_encoder", refuse)


def _plant_get_fallback(monkeypatch):
    monkeypatch.setattr(
        ExperimentService, "get_experiment_by_id", lambda self, _id: _NotEncodable()
    )
    _refuse_encoding(monkeypatch)


def _plant_update_fallback(monkeypatch):
    monkeypatch.setattr(
        ExperimentService, "update_experiment", lambda self, *a, **k: _NotEncodable()
    )
    _refuse_encoding(monkeypatch)


def _plant_results_fallback(monkeypatch):
    """A TypeError from the analysis. The route used to retry without its
    keyword arguments on one (and label numbers computed with the service's
    defaults with the request's settings); that fallback is gone (#580), so
    the TypeError itself is what must answer the fixed message."""

    def fail(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(CANARY)

    monkeypatch.setattr(AnalysisService, "get_experiment_results", fail)


def _plant_results_serialise(monkeypatch):
    monkeypatch.setattr(
        AnalysisService,
        "get_experiment_results",
        lambda self, *a, **k: {
            "status": "active",
            "confidence_level": 0.95,
            "correction_method": "benjamini_hochberg",
        },
    )
    monkeypatch.setattr(results_endpoints, "ExperimentResultsResponse", _boom)


class _Strict(BaseModel):
    count: int


def _validation_error() -> ValidationError:
    try:
        _Strict(count=CANARY)
    except ValidationError as exc:
        return exc
    raise AssertionError("no ValidationError")


def _plant_cuped_validation(monkeypatch):
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise _validation_error()

    monkeypatch.setattr(results_endpoints, "get_cuped_results_data", fail)


def _plant_bayesian(monkeypatch):
    monkeypatch.setattr(
        AnalysisService, "is_bayesian_enabled", staticmethod(lambda e: True)
    )
    monkeypatch.setattr(AnalysisService, "compute_bayesian_results", _boom)


def _plant_unarchive(monkeypatch):
    """Past the refusal, which an archived row with no dates would get."""
    monkeypatch.setattr(experiments_endpoints, "unarchive_refusal", lambda *a: None)
    monkeypatch.setattr(ExperimentService, "unarchive_experiment", _boom)


def _patch(target: Any, name: str) -> Callable:
    def plant(monkeypatch):
        monkeypatch.setattr(target, name, _boom)

    return plant


@dataclass(frozen=True)
class Case:
    method: str
    path: str  # after /api/v1, with {id}
    caller: str
    status: Optional[ExperimentStatus]  # None: no experiment needed
    plant: Callable
    sentence: str
    body: Any = None
    code: int = 500


EXPERIMENT_CASES = {
    "create": Case(
        "POST",
        "/experiments/",
        "developer",
        None,
        _patch(ExperimentService, "create_experiment"),
        "Something went wrong while creating the experiment",
        body=_payload,
    ),
    "list": Case(
        "GET",
        "/experiments/",
        "viewer",
        None,
        _patch(ExperimentService, "get_experiments"),
        "Could not list the experiments",
    ),
    "detail": Case(
        "GET",
        "/experiments/{id}",
        "viewer",
        ExperimentStatus.DRAFT,
        _patch(ExperimentService, "get_experiment_by_id"),
        "Could not load the experiment",
    ),
    "detail_fallback": Case(
        "GET",
        "/experiments/{id}",
        "viewer",
        ExperimentStatus.DRAFT,
        _plant_get_fallback,
        "Could not load the experiment",
    ),
    "update": Case(
        "PUT",
        "/experiments/{id}",
        "developer",
        ExperimentStatus.DRAFT,
        _patch(ExperimentService, "update_experiment"),
        "Could not update the experiment",
        body={"name": "renamed"},
    ),
    "update_fallback": Case(
        "PUT",
        "/experiments/{id}",
        "developer",
        ExperimentStatus.DRAFT,
        _plant_update_fallback,
        "Could not update the experiment",
        body={"name": "renamed"},
    ),
    "start": Case(
        "POST",
        "/experiments/{id}/start",
        "developer",
        ExperimentStatus.DRAFT,
        _patch(ExperimentService, "start_experiment"),
        "Could not start the experiment",
    ),
    "pause": Case(
        "POST",
        "/experiments/{id}/pause",
        "developer",
        ExperimentStatus.ACTIVE,
        _patch(ExperimentService, "to_response_dict"),
        "Could not pause the experiment",
    ),
    "schedule": Case(
        "PUT",
        "/experiments/{id}/schedule",
        "developer",
        ExperimentStatus.DRAFT,
        _patch(ExperimentService, "update_experiment_schedule"),
        "Could not update the experiment's schedule",
        body={"time_zone": "UTC"},
    ),
    "complete": Case(
        "POST",
        "/experiments/{id}/complete",
        "developer",
        ExperimentStatus.ACTIVE,
        _patch(ExperimentService, "to_response_dict"),
        "Could not complete the experiment",
    ),
    "archive": Case(
        "POST",
        "/experiments/{id}/archive",
        "developer",
        ExperimentStatus.COMPLETED,
        _patch(ExperimentService, "archive_experiment"),
        "Could not archive the experiment",
    ),
    "unarchive": Case(
        "POST",
        "/experiments/{id}/unarchive",
        "developer",
        ExperimentStatus.ARCHIVED,
        _plant_unarchive,
        "Could not unarchive the experiment",
    ),
    "clone": Case(
        "POST",
        "/experiments/{id}/clone",
        "developer",
        ExperimentStatus.ACTIVE,
        _patch(ExperimentService, "clone_experiment"),
        "Could not clone the experiment",
    ),
    "daily_results": Case(
        "GET",
        "/experiments/{id}/daily-results",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(AnalysisService, "get_daily_results"),
        "Could not load the daily results",
    ),
    "segmented_results": Case(
        "GET",
        "/experiments/{id}/segmented-results/country",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(AnalysisService, "get_segmented_results"),
        "Could not load the segmented results",
    ),
    "metadata": Case(
        "POST",
        "/experiments/{id}/metadata",
        "developer",
        ExperimentStatus.DRAFT,
        _patch(ExperimentService, "to_response_dict"),
        "Could not update the experiment's metadata",
        body={"k": "v"},
    ),
}

RESULTS = "Could not compute the experiment's results"

RESULTS_CASES = {
    "results": Case(
        "GET",
        "/results/{id}?use_cache=false",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(AnalysisService, "get_experiment_results"),
        RESULTS,
    ),
    "results_fallback": Case(
        "GET",
        "/results/{id}?use_cache=false",
        "viewer",
        ExperimentStatus.ACTIVE,
        _plant_results_fallback,
        RESULTS,
    ),
    "results_serialise": Case(
        "GET",
        "/results/{id}?use_cache=false",
        "viewer",
        ExperimentStatus.ACTIVE,
        _plant_results_serialise,
        RESULTS,
    ),
    "results_via_experiments": Case(
        "GET",
        "/experiments/{id}/results",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(AnalysisService, "get_experiment_results"),
        RESULTS,
    ),
    "results_daily": Case(
        "GET",
        "/results/{id}/daily",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(AnalysisService, "get_daily_results"),
        "Could not compute the daily results",
    ),
    "sample_size": Case(
        "GET",
        "/results/{id}/sample-size",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(results_endpoints, "SampleSizeResult"),
        "Could not compute the sample size",
    ),
    # A fault while working out the plan, past the existence check.
    "sample_size_counts": Case(
        "GET",
        "/results/{id}/sample-size",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(AnalysisService, "_ordered_metrics"),
        "Could not compute the sample size",
    ),
    "cuped": Case(
        "GET",
        "/results/{id}/cuped",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(results_endpoints, "get_cuped_results_data"),
        "Could not compute the CUPED results",
    ),
    # A pydantic ValidationError is a ValueError, and the ValueError branch
    # answers 404 with the error's text; it must not get there.
    "cuped_validation": Case(
        "GET",
        "/results/{id}/cuped",
        "viewer",
        ExperimentStatus.ACTIVE,
        _plant_cuped_validation,
        "Could not compute the CUPED results",
    ),
    "bayesian": Case(
        "GET",
        "/results/{id}/bayesian",
        "viewer",
        ExperimentStatus.ACTIVE,
        _plant_bayesian,
        "Could not compute the Bayesian results",
    ),
    "fdr": Case(
        "POST",
        "/results/{id}/fdr-correction",
        "viewer",
        ExperimentStatus.ACTIVE,
        _patch(BenjaminiHochbergService, "correct"),
        "Could not apply the FDR correction",
        body={"p_values": {"conversion": 0.01}},
    ),
}

CASES = {**EXPERIMENT_CASES, **RESULTS_CASES}


def _call(client, db_session, people, monkeypatch, case: Case):
    experiment_id = (
        _experiment(client, db_session, people, case.status)
        if case.status is not None
        else None
    )
    case.plant(monkeypatch)
    path = case.path.replace("{id}", experiment_id or "")
    body = case.body() if callable(case.body) else case.body
    return client.request(
        case.method, f"/api/v1{path}", json=body, headers=_auth(people[case.caller])
    )


@pytest.mark.parametrize("name", list(CASES))
def test_unexpected_error_answers_fixed_message(
    client, db_session, people, monkeypatch, caplog, name
):
    case = CASES[name]
    caplog.set_level(logging.ERROR)

    response = _call(client, db_session, people, monkeypatch, case)

    _assert_fixed(response, case.code, case.sentence)
    # "CANARY", not the whole string: pydantic shortens a long input value
    # in a ValidationError's text.
    logged = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.exc_info and "CANARY" in str(r.exc_info[1])
    ]
    assert logged, "the full error must reach the server log"


def test_a_malformed_request_id_is_left_out_of_the_message(
    client, db_session, people, monkeypatch
):
    """Without a well-formed ID the sentence stands alone, as on create."""
    case = EXPERIMENT_CASES["list"]
    case.plant(monkeypatch)
    headers = _auth(people["viewer"])
    headers["X-Request-ID"] = "<b>not an id</b>"

    response = client.get("/api/v1/experiments/", headers=headers)

    assert response.status_code == 500
    assert response.json() == {"detail": "Could not list the experiments."}


# --- deliberate 4xx texts are unchanged ---------------------------------------


def test_a_schedule_refusal_keeps_its_message(client, db_session, people, monkeypatch):
    """The service's own ValueError is a deliberate 400 and keeps its text."""

    def refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError("End date must be after start date")

    experiment_id = _experiment(client, db_session, people, ExperimentStatus.DRAFT)
    monkeypatch.setattr(ExperimentService, "update_experiment_schedule", refuse)

    response = client.put(
        f"/api/v1/experiments/{experiment_id}/schedule",
        json={"time_zone": "UTC"},
        headers=_auth(people["developer"]),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "End date must be after start date"}


def test_a_missing_experiment_on_cuped_is_still_404(client, people):
    response = client.get(
        f"/api/v1/results/{uuid.uuid4()}/cuped", headers=_auth(people["viewer"])
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Experiment not found"}


# --- the database's own refusal on update -------------------------------------


@pytest.mark.regression
def test_a_refused_update_answers_fixed_message_not_the_statement(
    client, db_session, people, monkeypatch
):
    """The database refusing an update: its message names the statement and
    the row. The response must carry neither, and must carry the request ID."""
    experiment_id = _experiment(client, db_session, people, ExperimentStatus.DRAFT)
    refusal = IntegrityError(
        "UPDATE experimentation.experiments SET name=%(name)s WHERE "
        "experimentation.experiments.id = %(id)s",
        {"name": None, "id": experiment_id},
        Exception(
            'null value in column "name" of relation "experiments" violates '
            "not-null constraint\nDETAIL:  Failing row contains "
            f"({experiment_id}, null, secret-hypothesis, draft)."
        ),
    )

    def refuse(self: Session) -> None:
        raise refusal

    monkeypatch.setattr(Session, "commit", refuse)

    response = client.put(
        f"/api/v1/experiments/{experiment_id}",
        json={"name": "renamed"},
        headers=_auth(people["developer"]),
    )

    assert response.status_code == 500, response.text
    assert "Failing row contains" not in response.text
    assert "UPDATE experimentation.experiments" not in response.text
    assert "secret-hypothesis" not in response.text
    assert REQUEST_ID in response.json()["detail"]
    assert response.json() == _expected("Could not update the experiment")

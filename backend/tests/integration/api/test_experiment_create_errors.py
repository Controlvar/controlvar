"""What POST /api/v1/experiments/ answers when a create cannot be completed.

* A key the caller chose that another experiment already has: 409, naming it.
* Two metrics with one name: 422 at ``["body", "metrics"]``, from the request
  schema, before the database is reached (#1461). It used to be the
  database's refusal below, answered 400.
* Anything else the database refuses (a value its column cannot hold that the
  schema let through, a generated key that keeps colliding on every retry):
  400 with a fixed message carrying the request ID.
* A generated key that is taken is replaced by a new one, a bounded number of
  times, so the caller who sent no key gets 201 (#388).
* Any other failure: 500 with the same fixed message.
* Every refused create leaves the session usable for the next request.

The bodies are compared for exact equality, so nothing but the documented
message can be in them.
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.main import app
from backend.app.services import experiment_service
from backend.app.services.experiment_service import ExperimentService

pytestmark = [pytest.mark.integration, pytest.mark.regression]

URL = "/api/v1/experiments/"
GENERIC = "Something went wrong while creating the experiment"


def _payload(key: str | None = None, **metric_overrides) -> dict:
    metric = {
        "name": "Conversion Rate",
        "event_name": "purchase",
        "metric_type": "conversion",
        "is_primary": True,
        "minimum_sample_size": 100,
    }
    metric.update(metric_overrides)
    body = {
        "name": "Create errors",
        "hypothesis": "A refused create answers a plain message",
        "experiment_type": "a_b",
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [metric],
    }
    if key is not None:
        body["key"] = key
    return body


def _key(prefix: str = "dup") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_a_taken_key_answers_409_naming_it(admin_client: TestClient) -> None:
    key = _key()
    first = admin_client.post(URL, json=_payload(key))
    assert first.status_code == 201, first.text

    second = admin_client.post(URL, json=_payload(key))

    assert second.status_code == 409
    assert second.json() == {
        "detail": f"An experiment with the key '{key}' already exists."
    }


def _refuse_with_integrity_error(monkeypatch) -> None:
    """The database refusing the create for a reason that is not the key.

    Two metrics with one name used to be how these tests reached the
    database's refusal; the schema now answers that with 422 (#1461), so the
    refusal is raised directly, as the DataError test below does.
    """

    def refuse(self, *args, **kwargs):
        raise IntegrityError("statement", None, Exception("marker-IE3"))

    monkeypatch.setattr(ExperimentService, "create_experiment", refuse)


def test_two_metrics_with_one_name_answer_422_naming_their_positions(
    admin_client: TestClient,
) -> None:
    """#1461: the request schema refuses a repeated metric name before the
    database does. Main answered 400 with the fixed message below."""
    body = _payload(_key("twin"))
    body["metrics"].append(dict(body["metrics"][0], is_primary=False))

    resp = admin_client.post(URL, json=body, headers={"X-Request-ID": "prb-b-123"})

    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "metrics"],
                "msg": "duplicate metric name; each name may appear once per "
                "experiment (first seen at metrics[0], repeated at metrics[1])",
            }
        ]
    }


def test_another_refusal_answers_the_fixed_message_with_the_request_id(
    admin_client: TestClient, monkeypatch
) -> None:
    _refuse_with_integrity_error(monkeypatch)

    resp = admin_client.post(
        URL, json=_payload(_key("twin")), headers={"X-Request-ID": "prb-b-123"}
    )

    assert resp.status_code == 400
    assert resp.headers["x-request-id"] == "prb-b-123"
    assert resp.json() == {"detail": f"{GENERIC} (request ID: prb-b-123)."}
    assert "marker-IE3" not in resp.text


def test_a_request_id_of_the_wrong_shape_is_left_out_of_the_message(
    admin_client: TestClient, monkeypatch
) -> None:
    _refuse_with_integrity_error(monkeypatch)

    resp = admin_client.post(
        URL, json=_payload(_key("twin")), headers={"X-Request-ID": "<b>x</b>"}
    )

    assert resp.status_code == 400
    assert resp.json() == {"detail": f"{GENERIC}."}


def test_an_unexpected_failure_answers_500_with_the_fixed_message(
    admin_client: TestClient, monkeypatch, caplog
) -> None:
    def fail(self, *args, **kwargs):
        raise RuntimeError("marker-7Q")

    monkeypatch.setattr(ExperimentService, "create_experiment", fail)
    caplog.set_level(logging.INFO)

    resp = admin_client.post(
        URL, json=_payload(_key()), headers={"X-Request-ID": "prb-b3-456"}
    )

    assert resp.status_code == 500
    assert resp.json() == {"detail": f"{GENERIC} (request ID: prb-b3-456)."}
    assert "marker-7Q" not in resp.text
    logged = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and r.exc_info
        and "marker-7Q" in str(r.exc_info[1])
    ]
    assert logged, "the full error must reach the server log"


def test_a_value_its_column_cannot_hold_answers_the_fixed_message(
    admin_client: TestClient, monkeypatch
) -> None:
    # The schemas now refuse every value the request can carry that a column
    # cannot hold (over-length strings #551, out-of-range numbers #559), so
    # the database's own refusal is raised directly to keep this branch pinned.
    def refuse(self, *args, **kwargs):
        raise DataError("statement", None, Exception("marker-DE9"))

    monkeypatch.setattr(ExperimentService, "create_experiment", refuse)

    resp = admin_client.post(
        URL, json=_payload(_key("range")), headers={"X-Request-ID": "prb-e-789"}
    )

    assert resp.status_code == 400
    assert resp.json() == {"detail": f"{GENERIC} (request ID: prb-e-789)."}
    assert "marker-DE9" not in resp.text


def test_a_generated_key_collision_is_not_reported_as_the_callers_key(
    admin_client: TestClient, monkeypatch
) -> None:
    taken = _key("taken")
    first = admin_client.post(URL, json=_payload(taken))
    assert first.status_code == 201, first.text

    calls: list[str] = []

    def always_taken(name: str) -> str:
        calls.append(name)
        return taken

    monkeypatch.setattr(ExperimentService, "generate_key", staticmethod(always_taken))
    resp = admin_client.post(URL, json=_payload(), headers={"X-Request-ID": "prb-d-1"})

    # Every retry collided too, so the bounded retry gave up (#388).
    assert len(calls) == experiment_service.KEY_GENERATION_ATTEMPTS
    assert resp.status_code == 400
    assert resp.json() == {"detail": f"{GENERIC} (request ID: prb-d-1)."}
    assert "None" not in resp.text


def test_a_taken_generated_key_is_retried_with_a_new_one(
    admin_client: TestClient, monkeypatch
) -> None:
    taken = _key("taken")
    first = admin_client.post(URL, json=_payload(taken))
    assert first.status_code == 201, first.text

    real = ExperimentService.generate_key
    calls: list[str] = []

    def taken_once(name: str) -> str:
        key = taken if not calls else real(name)
        calls.append(key)
        return key

    monkeypatch.setattr(ExperimentService, "generate_key", staticmethod(taken_once))
    resp = admin_client.post(URL, json=_payload())

    assert resp.status_code == 201, resp.text
    assert len(calls) == 2
    assert resp.json()["key"] == calls[1] != taken


def test_a_refused_create_leaves_the_session_usable(
    admin_client: TestClient, db_session: Session
) -> None:
    # One session for every request, as a request-scoped session would be if
    # the refused create left it mid-transaction. A fresh session per request
    # would hide a missing rollback.
    shared = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )()
    shared.execute(text("SET search_path TO test_experimentation"))

    def one_session():
        yield shared

    app.dependency_overrides[deps.get_db] = one_session
    try:
        key = _key()
        assert admin_client.post(URL, json=_payload(key)).status_code == 201
        assert admin_client.post(URL, json=_payload(key)).status_code != 201

        after = admin_client.post(URL, json=_payload(_key()))

        assert after.status_code == 201, after.text
    finally:
        shared.close()

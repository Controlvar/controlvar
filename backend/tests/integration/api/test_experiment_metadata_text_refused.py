"""Experiment metadata with NUL or a lone surrogate answers 422 (#961).

``POST /experiments/{id}/metadata`` takes any JSON object and merges it into
the ``experiment_metadata`` JSONB column. PostgreSQL refuses U+0000 in JSONB
and a lone surrogate cannot be encoded at all, so such a body failed at commit
and the route answered 500. The body is now checked where it is declared, with
the helper the path parameters use (#547): 422 at ``["body"]``, a fixed
message that never repeats the value, nothing stored and no audit entry.

The check runs as the body is read, so it comes before the route's own 403
(a role without UPDATE) and 404 (an unknown experiment); authentication still
answers 401 first. Every other body answers as before.

The bodies are sent as raw JSON text: ``\\ud800`` cannot be encoded by a JSON
client, and sending the escapes is what a client on the wire does.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.read_scope import get_current_reader
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.experiment import Experiment
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

EXPERIMENTS = "/api/v1/experiments"

#: Marks the refused text so a test can tell whether the answer repeats it.
MARK = "zq961mark"

#: (id, raw JSON body): a NUL in a value, nested, in a key; a lone surrogate.
REFUSED = [
    ("nul-in-a-value", '{"note": "' + MARK + '\\u0000b"}'),
    ("nul-nested", '{"n": {"x": ["' + MARK + '\\u0000"]}}'),
    ("nul-in-a-key", '{"' + MARK + '\\u0000b": 1}'),
    ("lone-surrogate", '{"note": "' + MARK + '\\ud800"}'),
]

REFUSAL = {
    "type": "value_error",
    "loc": ["body"],
    "msg": "Value error, metadata must not contain NUL or unpaired surrogate characters",
}

KEPT = {"kept": "before"}


def _create(client: TestClient) -> str:
    created = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"metadata text {uuid.uuid4().hex[:8]}",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Purchase",
                    "event_name": "purchase",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _post_raw(client: TestClient, experiment_id: str, body: str):
    return client.post(
        f"{EXPERIMENTS}/{experiment_id}/metadata",
        content=body.encode("ascii"),
        headers={"Content-Type": "application/json"},
    )


def _stored(db_session, experiment_id: str) -> Dict[str, Any]:
    db_session.expire_all()
    return db_session.get(Experiment, uuid.UUID(experiment_id)).experiment_metadata


def _audit_count(db_session, experiment_id: str) -> int:
    db_session.expire_all()
    return (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == uuid.UUID(experiment_id))
        .count()
    )


def _assert_refused(response) -> None:
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert [{k: e[k] for k in ("type", "loc", "msg")} for e in errors] == [REFUSAL]
    assert MARK not in response.text


@pytest.fixture
def experiment_with_metadata(admin_client, db_session) -> str:
    """A draft whose metadata already holds ``KEPT``."""
    experiment_id = _create(admin_client)
    first = admin_client.post(f"{EXPERIMENTS}/{experiment_id}/metadata", json=KEPT)
    assert first.status_code == 200, first.text
    assert _stored(db_session, experiment_id) == KEPT
    return experiment_id


@pytest.mark.parametrize("body", [b for _, b in REFUSED], ids=[i for i, _ in REFUSED])
def test_metadata_holding_unstorable_text_answers_422_and_nothing_is_written(
    admin_client, db_session, experiment_with_metadata, body
):
    audits = _audit_count(db_session, experiment_with_metadata)

    _assert_refused(_post_raw(admin_client, experiment_with_metadata, body))

    assert _stored(db_session, experiment_with_metadata) == KEPT
    assert _audit_count(db_session, experiment_with_metadata) == audits


def test_storable_metadata_is_merged_as_before(
    admin_client, db_session, experiment_with_metadata
):
    response = _post_raw(admin_client, experiment_with_metadata, '{"note": "fine"}')

    assert response.status_code == 200, response.text
    assert response.json()["experiment_metadata"] == {**KEPT, "note": "fine"}
    assert _stored(db_session, experiment_with_metadata) == {**KEPT, "note": "fine"}


def test_the_422_comes_before_the_403_for_a_role_without_update(
    admin_client, db_session, viewer_user
):
    experiment_id = _create(admin_client)
    viewer = make_client_for_user(db_session, viewer_user)

    _assert_refused(_post_raw(viewer, experiment_id, REFUSED[0][1]))
    # A storable body is still the 403 it was.
    assert _post_raw(viewer, experiment_id, '{"note": "x"}').status_code == 403


def test_the_422_comes_before_the_404_for_an_unknown_experiment(admin_client):
    unknown = str(uuid.uuid4())

    _assert_refused(_post_raw(admin_client, unknown, REFUSED[0][1]))
    assert _post_raw(admin_client, unknown, '{"note": "x"}').status_code == 404


def test_authentication_still_answers_401_first(admin_client):
    experiment_id = _create(admin_client)
    # The database stays the test one; the real authentication runs.
    for dependency in (
        deps.get_current_user,
        deps.get_current_active_user,
        deps.get_current_superuser,
        get_current_reader,
    ):
        app.dependency_overrides.pop(dependency, None)
    anonymous = TestClient(app, raise_server_exceptions=False)

    response = _post_raw(anonymous, experiment_id, REFUSED[0][1])

    assert response.status_code == 401, response.text
    assert MARK not in response.text

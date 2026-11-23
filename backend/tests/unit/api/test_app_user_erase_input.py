"""The erase takes exactly the ids tracking can store (#2042).

``POST /api/v1/admin/app-users/erase`` with the real request model and the
app's own 422 handler; the erase itself is replaced by a recorder, so no
database is used. A refused body never reaches the erase and its 422 never
echoes the id; an accepted id reaches it unchanged (no trimming, no case
folding).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import admin
from backend.app.main import app
from backend.app.schemas.app_user_erasure import AppUserEraseRequest
from backend.app.schemas.tracking import AssignmentRequest
from backend.app.services.app_user_erasure import ERASED_TABLES, EraseResult

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ERASE = "/api/v1/admin/app-users/erase"
NEEDLE = "erase-needle-input-contract"


@pytest.fixture
def erased(monkeypatch):
    """The ids that reached the erase (which is replaced by this recorder)."""
    seen = []

    def record(db, user_id, actor):
        seen.append(user_id)
        return EraseResult(
            receipt_id=uuid.uuid4(),
            complete=True,
            ids_erased=1,
            deleted={table.name: 0 for table in ERASED_TABLES},
        )

    monkeypatch.setattr(admin, "erase_app_user", record)
    return seen


def _post(**kwargs):
    """POST the erase as a superuser, with no database behind it."""
    superuser = SimpleNamespace(
        id=uuid.uuid4(), email="su@example.test", is_superuser=True, is_active=True
    )
    app.dependency_overrides[deps.get_current_superuser] = lambda: superuser
    app.dependency_overrides[deps.get_db] = lambda: None
    try:
        return TestClient(app, raise_server_exceptions=False).post(ERASE, **kwargs)
    finally:
        app.dependency_overrides.pop(deps.get_current_superuser, None)
        app.dependency_overrides.pop(deps.get_db, None)


REFUSED = {
    "empty": {"json": {"user_id": ""}},
    "256 characters": {"json": {"user_id": NEEDLE + "x" * (256 - len(NEEDLE))}},
    "NUL": {"content": json.dumps({"user_id": NEEDLE + "\u0000"})},
    "lone surrogate": {"content": '{"user_id": "' + NEEDLE + '\\ud800"}'},
    "an integer": {"json": {"user_id": 12345}},
    "null": {"json": {"user_id": None}},
    "a list": {"json": {"user_id": [NEEDLE]}},
    "no field": {"json": {}},
    "no body": {},
    "an unknown field": {"json": {"user_id": NEEDLE, "dry_run": True}},
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_a_body_tracking_could_not_store_is_a_422_that_never_echoes(case, erased):
    kwargs = dict(REFUSED[case])
    if "content" in kwargs:
        kwargs["headers"] = {"Content-Type": "application/json"}
    response = _post(**kwargs)
    assert response.status_code == 422, f"{case}: {response.status_code} != 422"
    assert NEEDLE not in response.text, case
    assert erased == [], f"{case} reached the erase"


@pytest.mark.parametrize(
    "user_id",
    [
        "x" * 255,
        f" {NEEDLE} ",
        NEEDLE.upper(),
        "u%_",
        "é-ü-日本",
    ],
)
def test_an_id_tracking_can_store_reaches_the_erase_unchanged(user_id, erased):
    response = _post(json={"user_id": user_id})
    assert response.status_code == 200, response.text
    assert erased == [user_id]
    assert set(response.json()) == {"receipt_id", "complete", "ids_erased", "deleted"}
    assert user_id not in response.text


def test_the_erase_takes_the_default_limit_on_its_one_path():
    """300 a minute from one address, counted on the fixed path: the id is in
    the body, so it never reaches the counter's key."""
    from backend.app.middleware.rate_limiter import (
        DEFAULT_BUCKET,
        rate_limit_key,
        resolve_rate_limit_bucket,
    )

    assert resolve_rate_limit_bucket(ERASE) == (DEFAULT_BUCKET, (300, 60))
    assert rate_limit_key("203.0.113.9", ERASE) == f"203.0.113.9:{ERASE}"


def test_the_id_has_the_bounds_of_an_assignments_user_id():
    """Parity with ``AssignmentRequest.user_id``: what tracking stores."""
    keys = ("type", "minLength", "maxLength")
    erase = AppUserEraseRequest.model_json_schema()["properties"]["user_id"]
    track = AssignmentRequest.model_json_schema()["properties"]["user_id"]
    assert {k: erase.get(k) for k in keys} == {k: track.get(k) for k in keys}
    assert AppUserEraseRequest.model_json_schema()["additionalProperties"] is False

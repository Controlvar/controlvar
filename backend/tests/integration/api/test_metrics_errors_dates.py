"""``GET /api/v1/metrics/errors`` refuses a date it cannot use with 422 (#956).

A ``start_date`` or ``end_date`` whose UTC offset is 16 hours or more either way
used to answer 500: PostgreSQL stores offsets up to 15:59:59 and failed the
query with "time zone displacement out of range". The sibling
``GET /api/v1/metrics/summary`` was fixed the same way in #1572.

The refusal sits at ``["query", <field>]`` with a fixed ``msg`` that does not
repeat the value. Everything else answers as before: offsets up to 15:59 either
way, ``Z`` and a naive date. A period holding a stored error is not tested
here: an answer that carries a row is a separate defect (the response
schema's ``metadata`` field reads the ORM's ``MetaData`` object). The OpenAPI
document does not change; that is ``test_openapi_snapshot.py``'s to pin.

Everything runs on PostgreSQL through the real route: which offsets are refused
is decided by what the database can store.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
ERRORS_URL = "/api/v1/metrics/errors"

#: The fixed message, written out: the test pins the text, not whatever the
#: route's constant happens to hold.
OFFSET_MSG = "Value error, the UTC offset must be less than 16 hours"


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

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


def _get(client, user, params: dict):
    return client.get(
        ERRORS_URL,
        params=params,
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )


@pytest.mark.parametrize(
    ("field", "sent"),
    [
        pytest.param("start_date", "2026-10-15T12:00:00+16:00", id="start+16:00"),
        pytest.param("end_date", "2026-10-15T12:00:00-16:00", id="end-16:00"),
        pytest.param("end_date", "0562-05-04T20:38:36.1+19:33", id="end+19:33"),
        pytest.param("start_date", "0562-05-04T20:38:36.1-19:52", id="start-19:52"),
    ],
)
def test_an_offset_of_16_hours_or_more_answers_422(client, viewer_user, field, sent):
    resp = _get(client, viewer_user, {field: sent})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert len(detail) == 1, detail
    assert detail[0]["loc"] == ["query", field]
    assert detail[0]["type"] == "value_error"
    assert detail[0]["msg"] == OFFSET_MSG
    assert sent not in resp.text
    assert sent[-6:] not in resp.text


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize(
    "sent",
    [
        "2026-10-15T12:00:00+15:59",
        "2026-10-15T12:00:00-15:59",
        "2026-10-15T12:00:00Z",
        "2026-10-15T12:00:00",
    ],
    ids=["+15:59", "-15:59", "Z", "naive"],
)
def test_offsets_under_16_hours_utc_and_naive_dates_are_answered(
    client, viewer_user, field, sent
):
    resp = _get(client, viewer_user, {field: sent})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)

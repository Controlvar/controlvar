"""Every date-time query parameter refuses a UTC offset of 16 hours or more (#956).

PostgreSQL stores a UTC offset of at most 15:59:59 either way, and fails a
query that compares with a larger one ("time zone displacement out of range").
A date-time query parameter sent with such an offset reached the database and
answered 500. Each now carries ``backend.app.api.datetime_bounds`` (#1827) and
answers 422 at ``["query", <param>]`` with a fixed message that does not repeat
the value. Every other value answers as before: ``Z``, a date with no offset,
and offsets up to 15:59 either way.

This is the one sweep over all of them, with the list diffed against what the
application declares, so that a date-time parameter added later cannot be left
out. It passes on the code that merged #1827; it is a guard, not a regression
test (the body fields of ``test_body_date_time_offsets.py`` are the regression).

The parameters are a literal list, diffed against what the application
declares: every query parameter of every route context whose type holds
``datetime``, a dependency's included. ``app.routes`` alone lists no route
with a parameter (FastAPI keeps ``include_router`` lazy), so the walk reads
``effective_route_contexts``, has a floor, and its count is pinned per
profile. A new date-time parameter fails the diff until it is listed here, and
then this test sends it the offsets.

Everything runs through the real routes on PostgreSQL: which offsets are
refused is decided by what the database can store. Both ``get_db`` functions
are overridden: ``compliance.py`` and the HIPAA module read
``backend.app.db.session.get_db``, not ``deps.get_db``, and with only the one
overridden their routes answer 500 on every input. The OpenAPI document does
not change; that is ``test_openapi_snapshot.py``'s to pin.
"""

from __future__ import annotations

import datetime
import importlib.util
import typing
from typing import Iterator, List, Set, Tuple

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.db import session as db_session_module
from backend.app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"

#: The fixed message, written out: the test pins the text, not whatever the
#: module's constant happens to hold.
OFFSET_MSG = "Value error, the UTC offset must be less than 16 hours"

Param = Tuple[str, str, str]

#: Every date-time query parameter of the core profile, sorted.
CORE_PARAMS: List[Param] = [
    ("GET", "/api/v1/audit-logs/", "from_date"),
    ("GET", "/api/v1/audit-logs/", "to_date"),
    ("GET", "/api/v1/audit-logs/export", "from_date"),
    ("GET", "/api/v1/audit-logs/export", "to_date"),
    ("GET", "/api/v1/audit-logs/stats", "from_date"),
    ("GET", "/api/v1/audit-logs/stats", "to_date"),
    ("GET", "/api/v1/audit-logs/user/{user_id}", "from_date"),
    ("GET", "/api/v1/audit-logs/user/{user_id}", "to_date"),
    ("GET", "/api/v1/compliance/audit-events", "end_time"),
    ("GET", "/api/v1/compliance/audit-events", "start_time"),
    ("GET", "/api/v1/compliance/export", "end_time"),
    ("GET", "/api/v1/compliance/export", "start_time"),
    ("GET", "/api/v1/compliance/reports/{standard}", "end_time"),
    ("GET", "/api/v1/compliance/reports/{standard}", "start_time"),
    ("GET", "/api/v1/export/experiments", "end_date"),
    ("GET", "/api/v1/export/experiments", "start_date"),
    ("GET", "/api/v1/export/feature-flags", "end_date"),
    ("GET", "/api/v1/export/feature-flags", "start_date"),
    ("GET", "/api/v1/export/reports/overview", "end_date"),
    ("GET", "/api/v1/export/reports/overview", "start_date"),
    ("GET", "/api/v1/export/variants", "end_date"),
    ("GET", "/api/v1/export/variants", "start_date"),
    ("GET", "/api/v1/metrics/aggregated", "end_date"),
    ("GET", "/api/v1/metrics/aggregated", "start_date"),
    ("GET", "/api/v1/metrics/errors", "end_date"),
    ("GET", "/api/v1/metrics/errors", "start_date"),
    ("GET", "/api/v1/metrics/summary", "end_date"),
    ("GET", "/api/v1/metrics/summary", "start_date"),
    ("POST", "/api/v1/metrics/aggregate", "end_time"),
    ("POST", "/api/v1/metrics/aggregate", "start_time"),
]

#: The full profile's own (the HIPAA module), sorted.
MODULE_PARAMS: List[Param] = [
    ("GET", "/api/v1/hipaa/audit-logs", "end_date"),
    ("GET", "/api/v1/hipaa/audit-logs", "start_date"),
    ("GET", "/api/v1/hipaa/report", "end_date"),
    ("GET", "/api/v1/hipaa/report", "start_date"),
]

CORE_COUNT = 30
FULL_COUNT = 34

#: Offsets the database cannot store: refused.
REFUSED = {
    "+16:00": "2026-01-01T00:00:00+16:00",
    "-16:00": "2026-01-01T00:00:00-16:00",
    "+23:00": "2026-01-01T00:00:00+23:00",
    "-23:00": "2026-01-01T00:00:00-23:00",
    "+23:59": "2026-01-01T00:00:00+23:59",
}

#: What works today and keeps working.
ACCEPTED = {
    "Z": "2026-01-01T00:00:00Z",
    "naive": "2026-01-01T00:00:00",
    "+15:59": "2026-01-01T00:00:00+15:59",
    "-15:59": "2026-01-01T00:00:00-15:59",
    "+15:30": "2026-01-01T00:00:00+15:30",
}


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _expected() -> List[Param]:
    return sorted(CORE_PARAMS + (MODULE_PARAMS if _full_profile() else []))


def _contexts() -> Iterator[Tuple[str, str, object]]:
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _holds_datetime(annotation) -> bool:
    if annotation is datetime.datetime:
        return True
    return any(_holds_datetime(arg) for arg in typing.get_args(annotation))


def _query_params(dependant) -> Iterator[object]:
    yield from dependant.query_params
    for sub in dependant.dependencies:
        yield from _query_params(sub)


def _declared() -> Set[Param]:
    found = set()
    for method, path, ctx in _contexts():
        dependant = getattr(ctx, "dependant", None)
        if dependant is None:
            continue
        for param in _query_params(dependant):
            if _holds_datetime(param.field_info.annotation):
                found.add((method, path, param.alias))
    return found


def test_the_lists_are_sorted_and_counted():
    assert CORE_PARAMS == sorted(CORE_PARAMS)
    assert MODULE_PARAMS == sorted(MODULE_PARAMS)
    assert len(CORE_PARAMS) == CORE_COUNT
    assert len(CORE_PARAMS) + len(MODULE_PARAMS) == FULL_COUNT


def test_every_date_time_query_param_is_listed():
    """The walk finds exactly the listed parameters, per profile.

    A date-time query parameter added without being listed here (and so
    without being sent the offsets below) is named by the diff."""
    declared = _declared()
    assert len(declared) >= CORE_COUNT, (
        f"the walk found {len(declared)} date-time query parameters: it reads "
        "fewer routes than the application serves"
    )
    expected = set(_expected())
    assert declared == expected, (
        f"not listed: {sorted(declared - expected)}; "
        f"listed but not declared: {sorted(expected - declared)}"
    )
    assert len(declared) == (FULL_COUNT if _full_profile() else CORE_COUNT)


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

    overridden = (deps.get_db, db_session_module.get_db)
    for get_db in overridden:
        app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for get_db in overridden:
            app.dependency_overrides.pop(get_db, None)


def _send(client, user, method: str, path: str, param: str, sent: str):
    url = path.replace("{user_id}", str(user.id)).replace("{standard}", "soc2")
    return client.request(
        method,
        url,
        params={param: sent},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )


#: The routes the compliance module serves: without it (the core profile) they
#: answer 501.
COMPLIANCE_MODULE_ROUTES = {
    "/api/v1/compliance/export",
    "/api/v1/compliance/reports/{standard}",
}


def _ok_status(path: str) -> int:
    if path in COMPLIANCE_MODULE_ROUTES and not _full_profile():
        return 501
    return 200


def _refusal_problem(resp, param: str, sent: str) -> str:
    if resp.status_code != 422:
        return f"expected 422, got {resp.status_code}"
    detail = resp.json()["detail"]
    if len(detail) != 1:
        return f"expected one item, got {detail}"
    item = detail[0]
    if item["loc"] != ["query", param]:
        return f"loc {item['loc']}"
    if item["type"] != "value_error" or item["msg"] != OFFSET_MSG:
        return f"type {item['type']!r}, msg {item['msg']!r}"
    if sent in resp.text or sent[-6:] in resp.text:
        return "the value sent is in the answer"
    return ""


@pytest.mark.parametrize("sent", list(REFUSED.values()), ids=list(REFUSED))
def test_an_offset_of_16_hours_or_more_answers_422_on_every_param(
    client, superuser, sent
):
    failures = []
    for method, path, param in _expected():
        resp = _send(client, superuser, method, path, param, sent)
        if path in COMPLIANCE_MODULE_ROUTES and not _full_profile():
            # Brokered: without the module the route answers 501 before it
            # validates anything.
            problem = "" if resp.status_code == 501 else f"got {resp.status_code}"
        else:
            problem = _refusal_problem(resp, param, sent)
        if problem:
            failures.append(f"{method} {path} {param}: {problem}")
    assert failures == [], "\n".join(failures)


@pytest.mark.parametrize("sent", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_offsets_under_16_hours_utc_and_naive_dates_are_answered_on_every_param(
    client, superuser, sent
):
    failures = []
    for method, path, param in _expected():
        resp = _send(client, superuser, method, path, param, sent)
        expected = _ok_status(path)
        if resp.status_code != expected:
            failures.append(
                f"{method} {path} {param}: {sent} expected {expected}, "
                f"got {resp.status_code}"
            )
    assert failures == [], "\n".join(failures)


def test_the_audit_log_export_refuses_plus_23_00_for_a_viewer(client, viewer_user):
    """The issue's own request: it answered 500 (InvalidTimeZoneDisplacement)."""
    sent = "2026-01-01T00:00:00+23:00"
    resp = client.get(
        "/api/v1/audit-logs/export",
        params={"from_date": sent},
        headers={"Authorization": f"Bearer {create_local_access_token(viewer_user)}"},
    )
    assert _refusal_problem(resp, "from_date", sent) == "", resp.text

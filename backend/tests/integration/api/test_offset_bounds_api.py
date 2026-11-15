"""``skip``, ``offset`` and ``page`` stop at the database's own limit (#1001).

PostgreSQL takes an OFFSET of at most 9223372036854775807 (2**63 - 1, the
largest bigint) and fails a query given a larger one ("bigint out of range").
Every list route that pages handed its ``skip``, ``offset`` or
``(page - 1) * <size>`` to the query, so one past the limit answered 500. The
flag list had no bound at all, and a negative ``skip`` or ``limit`` answered
500 too. Each now answers 422 at ``["query", <param>]``:

- ``skip``/``offset`` above 2**63 - 1, and ``page`` above 2**63, with
  pydantic's own ``less_than_equal`` item, before the route runs;
- a ``page`` whose ``(page - 1) * <size>`` is above 2**63 - 1, at
  ``["query", "page"]`` with ``"type": "value_error"`` and a ``msg`` naming
  the route's own size parameter;
- on the flag list, ``skip`` or ``limit`` below 0 (``greater_than_equal``).

Everything the database could answer is answered as before: 2**63 - 1 itself,
the last page, ``limit=0`` on the flag list. The flag list's answers are held
byte for byte to the ones captured on main (``golden/feature_flag_list.json``).

Everything runs through the real routes on PostgreSQL, because the bound is
the database's. Both ``get_db`` functions are overridden: ``compliance.py``
and the HIPAA module read ``backend.app.db.session.get_db``. The values are
written out here rather than imported, so the tests pin the numbers, not
whatever a constant holds.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.db import session as db_session_module
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.stale_flags import stale_clock

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"

#: The largest OFFSET PostgreSQL takes, written out.
MAX = 9223372036854775807

#: Every route context that hands ``skip`` or ``offset`` to the query: the
#: flag list twice (its slash-less twin is not in the document) and the flag
#: history's ``{flag_id}`` filled in by the test.
OFFSET_ROUTES: List[Tuple[str, str]] = [
    ("/api/v1/admin/users", "skip"),
    ("/api/v1/experiments/", "skip"),
    ("/api/v1/feature-flags", "skip"),
    ("/api/v1/feature-flags/", "skip"),
    ("/api/v1/feature-flags/{flag_id}/history", "offset"),
    ("/api/v1/holdout/all", "skip"),
    ("/api/v1/metrics/aggregated", "skip"),
    ("/api/v1/metrics/errors", "skip"),
    ("/api/v1/mutual-exclusion-groups", "skip"),
    ("/api/v1/rollout-schedules/", "skip"),
    ("/api/v1/segments", "offset"),
    ("/api/v1/users/", "skip"),
]

#: Every page route, with its own size parameter. The HIPAA one is the full
#: profile's.
CORE_PAGE_ROUTES: List[Tuple[str, str]] = [
    ("/api/v1/audit-logs/", "limit"),
    ("/api/v1/compliance/audit-events", "limit"),
    ("/api/v1/llm-experiments/", "page_size"),
    ("/api/v1/notifications/delivery-log", "limit"),
]
MODULE_PAGE_ROUTES: List[Tuple[str, str]] = [
    ("/api/v1/hipaa/audit-logs", "page_size"),
]

FLAG_LIST_ROUTES = ["/api/v1/feature-flags/", "/api/v1/feature-flags"]


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _page_routes() -> List[Tuple[str, str]]:
    return CORE_PAGE_ROUTES + (MODULE_PAGE_ROUTES if _full_profile() else [])


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


@pytest.fixture
def flag_id(db_session) -> str:
    """A stored flag, so the history route reaches its query."""
    flag = FeatureFlag(key=f"bounds1001-{uuid.uuid4().hex[:10]}", name="bounds 1001")
    db_session.add(flag)
    db_session.commit()
    yield str(flag.id)
    db_session.rollback()
    db_session.delete(flag)
    db_session.commit()


def _get(client, user, path: str, params: Dict[str, object]):
    return client.get(
        path,
        params={k: str(v) for k, v in params.items()},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )


def _refusal_problem(resp, param: str, kind: str, msg: str = "") -> str:
    """What is wrong with *resp* as the one-item 422 at ``["query", param]``."""
    if resp.status_code != 422:
        return f"expected 422, got {resp.status_code}"
    detail = resp.json()["detail"]
    if len(detail) != 1:
        return f"expected one item, got {detail}"
    item = detail[0]
    if item["loc"] != ["query", param]:
        return f"loc {item['loc']}"
    if item["type"] != kind:
        return f"type {item['type']!r}"
    if msg and item["msg"] != msg:
        return f"msg {item['msg']!r}"
    return ""


# ---------------------------------------------------------------------------
# The issue's own requests
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_1001_rollout_schedules_skip_past_the_database_limit_answers_422(
    client, superuser
):
    """It answered 500 (bigint out of range)."""
    resp = _get(client, superuser, "/api/v1/rollout-schedules/", {"skip": MAX + 1})
    assert _refusal_problem(resp, "skip", "less_than_equal") == "", resp.text
    assert resp.json()["detail"][0]["msg"] == (
        "Input should be less than or equal to 9223372036854775807"
    )
    assert str(MAX + 1) not in resp.text


@pytest.mark.regression
@pytest.mark.parametrize("path", FLAG_LIST_ROUTES)
@pytest.mark.parametrize("param", ["skip", "limit"])
def test_1001_the_flag_list_refuses_a_negative_skip_or_limit(
    client, viewer_user, path, param
):
    """``skip=-1`` and ``limit=-1`` reached the query and answered 500."""
    resp = _get(client, viewer_user, path, {param: -1})
    assert _refusal_problem(resp, param, "greater_than_equal") == "", resp.text
    assert resp.json()["detail"][0]["msg"] == (
        "Input should be greater than or equal to 0"
    )


@pytest.mark.regression
@pytest.mark.parametrize("path", FLAG_LIST_ROUTES)
def test_1001_the_flag_list_refuses_a_limit_past_the_database_limit(
    client, viewer_user, path
):
    resp = _get(client, viewer_user, path, {"limit": MAX + 1})
    assert _refusal_problem(resp, "limit", "less_than_equal") == "", resp.text
    assert str(MAX + 1) not in resp.text


@pytest.mark.parametrize("path", FLAG_LIST_ROUTES)
def test_the_flag_list_still_takes_limit_0_and_the_largest_limit(
    client, viewer_user, path
):
    for limit in (0, 5000, MAX):
        resp = _get(client, viewer_user, path, {"limit": limit})
        assert resp.status_code == 200, f"limit={limit}: {resp.text}"
        assert resp.json()["limit"] == limit
    assert _get(client, viewer_user, path, {"limit": 0}).json()["items"] == []


@pytest.mark.regression
@pytest.mark.parametrize("path", FLAG_LIST_ROUTES)
def test_the_flag_list_names_both_refusals_at_once(client, viewer_user, path):
    resp = _get(client, viewer_user, path, {"skip": -1, "limit": MAX + 1})
    assert resp.status_code == 422, resp.text
    found = sorted((d["loc"][1], d["type"]) for d in resp.json()["detail"])
    assert found == [("limit", "less_than_equal"), ("skip", "greater_than_equal")]


# ---------------------------------------------------------------------------
# Every route that takes an offset
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_1001_every_offset_route_takes_the_largest_offset_and_refuses_the_next(
    client, superuser, flag_id
):
    """2**63 - 1 answers 200 everywhere; 2**63 answered 500 everywhere."""
    failures = []
    for path, param in OFFSET_ROUTES:
        url = path.replace("{flag_id}", flag_id)
        largest = _get(client, superuser, url, {param: MAX})
        if largest.status_code != 200:
            failures.append(
                f"{path} {param}={MAX}: expected 200, got {largest.status_code}"
            )
        past = _get(client, superuser, url, {param: MAX + 1})
        problem = _refusal_problem(past, param, "less_than_equal")
        if not problem and str(MAX + 1) in past.text:
            problem = "the value sent is in the answer"
        if problem:
            failures.append(f"{path} {param}={MAX + 1}: {problem}")
        negative = _get(client, superuser, url, {param: -1})
        problem = _refusal_problem(negative, param, "greater_than_equal")
        if problem:
            failures.append(f"{path} {param}=-1: {problem}")
    assert failures == [], "\n".join(failures)


@pytest.mark.regression
def test_1001_the_user_list_refuses_a_skip_past_the_limit_for_a_superuser(
    client, superuser
):
    """The fuzz run's superuser entry for GET /api/v1/users/ (#1001)."""
    resp = _get(client, superuser, "/api/v1/users/", {"skip": MAX + 1})
    assert _refusal_problem(resp, "skip", "less_than_equal") == "", resp.text


# ---------------------------------------------------------------------------
# Every page route: the page, and the offset it makes with the route's size
# ---------------------------------------------------------------------------


def _product_msg(size: str) -> str:
    return f"(page - 1) * {size} must be at most 9223372036854775807"


@pytest.mark.regression
def test_1001_every_page_route_takes_the_last_page_and_refuses_the_next(
    client, superuser
):
    """Page 2**63 with a size of 1 starts at 2**63 - 1 (200); one more 500'd."""
    failures = []
    for path, size in _page_routes():
        last = _get(client, superuser, path, {"page": MAX + 1, size: 1})
        if last.status_code != 200:
            failures.append(
                f"{path} page={MAX + 1}: expected 200, got {last.status_code}"
            )
        past = _get(client, superuser, path, {"page": MAX + 2, size: 1})
        problem = _refusal_problem(past, "page", "less_than_equal")
        if not problem and str(MAX + 2) in past.text:
            problem = "the value sent is in the answer"
        if problem:
            failures.append(f"{path} page={MAX + 2}: {problem}")
    assert failures == [], "\n".join(failures)


@pytest.mark.regression
def test_1001_every_page_route_refuses_a_page_that_starts_past_the_largest_offset(
    client, superuser
):
    """Page and size each in range, their product not: it answered 500.

    ``(2**62 - 1) * 2`` is 2**63 - 2, which the database takes;
    ``2**62 * 2`` is 2**63, which it does not."""
    failures = []
    sent = 2**62 + 1
    for path, size in _page_routes():
        inside = _get(client, superuser, path, {"page": 2**62, size: 2})
        if inside.status_code != 200:
            failures.append(
                f"{path} page=2**62: expected 200, got {inside.status_code}"
            )
        past = _get(client, superuser, path, {"page": sent, size: 2})
        problem = _refusal_problem(past, "page", "value_error", _product_msg(size))
        if not problem and str(sent) in past.text:
            problem = "the value sent is in the answer"
        if problem:
            failures.append(f"{path} page=2**62+1, {size}=2: {problem}")
    assert failures == [], "\n".join(failures)


@pytest.mark.regression
def test_1001_the_audit_log_list_refuses_the_page_for_a_viewer(client, viewer_user):
    """The fuzz run's viewer entry for GET /api/v1/audit-logs/ (#1001)."""
    resp = _get(
        client, viewer_user, "/api/v1/audit-logs/", {"page": 2**62 + 1, "limit": 2}
    )
    problem = _refusal_problem(resp, "page", "value_error", _product_msg("limit"))
    assert problem == "", resp.text


#: Routes that refuse a viewer with 403 in their own body, after the
#: parameters are read.
REFUSING_PAGE_ROUTES = [
    ("/api/v1/compliance/audit-events", "limit"),
    ("/api/v1/notifications/delivery-log", "limit"),
]
REFUSING_OFFSET_ROUTES = [
    ("/api/v1/holdout/all", "skip"),
    ("/api/v1/mutual-exclusion-groups", "skip"),
]


def test_a_caller_the_route_refuses_is_refused_first_unless_a_value_is_out_of_range(
    client, viewer_user
):
    """The page check comes after the route's own role check, so a caller it
    refuses is answered 403, whatever page and size make. A value past its
    own bound is refused before the route runs, as any out-of-range parameter
    already was: 422."""
    pages = REFUSING_PAGE_ROUTES + (
        [("/api/v1/hipaa/audit-logs", "page_size")] if _full_profile() else []
    )
    failures = []
    for path, size in pages:
        product = _get(client, viewer_user, path, {"page": 2**62 + 1, size: 2})
        if product.status_code != 403:
            failures.append(
                f"{path} page product: expected 403, got {product.status_code}"
            )
        past = _get(client, viewer_user, path, {"page": MAX + 2, size: 1})
        if _refusal_problem(past, "page", "less_than_equal"):
            failures.append(
                f"{path} page={MAX + 2}: expected 422, got {past.status_code}"
            )
    for path, param in REFUSING_OFFSET_ROUTES:
        largest = _get(client, viewer_user, path, {param: MAX})
        if largest.status_code != 403:
            failures.append(
                f"{path} {param}={MAX}: expected 403, got {largest.status_code}"
            )
        past = _get(client, viewer_user, path, {param: MAX + 1})
        if _refusal_problem(past, param, "less_than_equal"):
            failures.append(
                f"{path} {param}={MAX + 1}: expected 422, got {past.status_code}"
            )
    assert failures == [], "\n".join(failures)


def test_the_user_list_still_ignores_skip_for_a_caller_it_lists_alone(
    client, viewer_user
):
    """A caller who is not a superuser is listed alone and ``skip`` is never
    read: any value up to the bound answers 200 with their own account, as
    before. Past the bound it answers 422 (it answered 200)."""
    for skip in (0, MAX):
        resp = _get(client, viewer_user, "/api/v1/users/", {"skip": skip})
        assert resp.status_code == 200, f"skip={skip}: {resp.text}"
        assert [u["id"] for u in resp.json()["items"]] == [str(viewer_user.id)]
    past = _get(client, viewer_user, "/api/v1/users/", {"skip": MAX + 1})
    assert _refusal_problem(past, "skip", "less_than_equal") == "", past.text


# ---------------------------------------------------------------------------
# The rollout schedule list's `status` (the parameter's Python name changed)
# ---------------------------------------------------------------------------


def test_the_rollout_schedule_status_filter_answers_as_before(client, superuser):
    bogus = _get(client, superuser, "/api/v1/rollout-schedules/", {"status": "bogus"})
    assert _refusal_problem(bogus, "status", "enum") == "", bogus.text
    assert "bogus" not in bogus.text
    active = _get(client, superuser, "/api/v1/rollout-schedules/", {"status": "active"})
    assert active.status_code == 200, active.text
    assert all(item["status"] == "active" for item in active.json()["items"])


# ---------------------------------------------------------------------------
# The flag list answers byte for byte what main answered
# ---------------------------------------------------------------------------

GOLDEN = Path(__file__).parent / "golden" / "feature_flag_list.json"
GOLDEN_SEARCH = "gold1001flaglist"

#: Three rows with every value fixed, created a day apart (the list is oldest
#: first).
GOLDEN_ROWS = (
    ("10010001-0000-4000-8000-000000000001", "Checkout banner", "ACTIVE", 1),
    ("10010002-0000-4000-8000-000000000002", "Search ranking", "INACTIVE", 2),
    ("10010003-0000-4000-8000-000000000003", "Dark mode", "ACTIVE", 3),
)

#: The header the golden file does not hold: it differs on every request.
VOLATILE_HEADERS = {"x-request-id"}

#: The time the list classifies the golden rows at (its ``stale`` fields,
#: #1808). ``evaluations_seen`` counts any flag evaluation the installation
#: recorded from STALE_FLAG_DAYS before it on, with no upper bound
#: (``any_evaluation_since``), so at the wall clock the answer would depend on
#: what other tests in the session wrote. At this time it counts none: other
#: tests' evaluations are at today's dates, and the stale-flag API tests, which
#: write theirs from the year 2400 on, delete their flags afterwards, and the
#: evaluations with them (``raw_metrics`` cascades on the flag's delete). A
#: test that left a later evaluation behind would change this answer.
GOLDEN_NOW = datetime(2200, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def golden_clock():
    """The list's ``stale_clock`` set to :data:`GOLDEN_NOW`."""
    app.dependency_overrides[stale_clock] = lambda: GOLDEN_NOW
    yield GOLDEN_NOW
    app.dependency_overrides.pop(stale_clock, None)


def golden_rows(db_session) -> None:
    """The golden rows, written once per session."""
    for row_id, name, status, day in GOLDEN_ROWS:
        if db_session.get(FeatureFlag, uuid.UUID(row_id)) is not None:
            continue
        db_session.add(
            FeatureFlag(
                id=uuid.UUID(row_id),
                key=f"{GOLDEN_SEARCH}-{name.lower().replace(' ', '-')}",
                name=name,
                description=f"{GOLDEN_SEARCH}: the flag list as main answers it (#1001)",
                status=FeatureFlagStatus[status],
                rollout_percentage=10 * day,
                created_at=datetime(2026, 3, day, 9, 0, 0),
                updated_at=datetime(2026, 4, day, 9, 0, 0),
            )
        )
    db_session.commit()


def golden_requests() -> Dict[str, Tuple[str, Dict[str, object]]]:
    """Each request the golden file holds, by a readable name.

    The defaults (``skip=0``, ``limit=100``), the largest ``limit`` a client
    in this repository sends (200, scale_stack.py) and the MCP server's
    ceiling (100), ``limit=0``, a large ``skip`` the database takes, a page in
    the middle, ``status``, and the slash-less twin. ``search`` keeps every
    request to the golden rows."""
    s = {"search": GOLDEN_SEARCH}
    return {
        "defaults": ("/api/v1/feature-flags/", dict(s)),
        "limit=200": ("/api/v1/feature-flags/", {**s, "limit": 200}),
        "limit=100": ("/api/v1/feature-flags/", {**s, "limit": 100}),
        "skip=0&limit=0": ("/api/v1/feature-flags/", {**s, "skip": 0, "limit": 0}),
        "skip=2**62": ("/api/v1/feature-flags/", {**s, "skip": 2**62}),
        "skip=1&limit=1": ("/api/v1/feature-flags/", {**s, "skip": 1, "limit": 1}),
        "status=ACTIVE": ("/api/v1/feature-flags/", {**s, "status": "ACTIVE"}),
        "twin defaults": ("/api/v1/feature-flags", dict(s)),
    }


def golden_answer(resp) -> Dict[str, object]:
    """What the golden file holds of one answer."""
    return {
        "status": resp.status_code,
        "headers": {
            k: v for k, v in sorted(resp.headers.items()) if k not in VOLATILE_HEADERS
        },
        "body": resp.text,
    }


@pytest.mark.parametrize("name", sorted(golden_requests()))
def test_the_flag_list_answers_byte_for_byte_as_on_main(
    client, db_session, viewer_user, golden_clock, name
):
    golden_rows(db_session)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == sorted(golden_requests())
    path, params = golden_requests()[name]
    resp = _get(client, viewer_user, path, params)
    assert "x-request-id" in resp.headers
    assert golden_answer(resp) == golden[name]

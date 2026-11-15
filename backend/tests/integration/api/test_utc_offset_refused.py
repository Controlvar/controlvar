"""A date with a UTC offset beyond 15:59 answers 422, not 500, on the core routes (#956).

PostgreSQL stores a UTC offset of at most 15:59:59 either way and fails a
query that compares with a larger one ("time zone displacement out of range").
The routes below passed such a date to the query and answered 500; the two
metrics routes were fixed earlier and keep their own test files.

Each route refuses at ``["query", <field>]`` with a fixed message that does not
repeat the value, after its permission check: a caller who may not use the
route still gets 401 or 403 and learns nothing from a 422. Offsets up to 15:59,
``Z`` and a naive date answer as before. A route that takes no such parameter
is untouched.

Everything runs on PostgreSQL through the real route.
"""

from __future__ import annotations

import pytest

from backend.app.api import deps
from backend.app.db.session import get_db as session_get_db
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression, pytest.mark.requires_db]

V1 = "/api/v1"
TAG = "off956"

#: The fixed message, written out: the test pins the text, not whatever the
#: constant in the code happens to hold.
OFFSET_MSG = "Value error, the UTC offset must be less than 16 hours"

AUDIT_ROLE = UserRole.ANALYST

#: (id, method, path, role that may call it, the two date parameters).
#: ``{me}`` is the caller's own id.
SITES = [
    ("audit-list", "GET", "/audit-logs/", UserRole.VIEWER, ("from_date", "to_date")),
    (
        "audit-export",
        "GET",
        "/audit-logs/export",
        UserRole.VIEWER,
        ("from_date", "to_date"),
    ),
    (
        "audit-user",
        "GET",
        "/audit-logs/user/{me}",
        UserRole.VIEWER,
        ("from_date", "to_date"),
    ),
    (
        "audit-stats",
        "GET",
        "/audit-logs/stats",
        UserRole.ANALYST,
        ("from_date", "to_date"),
    ),
    (
        "compliance-events",
        "GET",
        "/compliance/audit-events",
        UserRole.ANALYST,
        ("start_time", "end_time"),
    ),
    (
        "metrics-list",
        "GET",
        "/metrics/aggregated",
        UserRole.VIEWER,
        ("start_date", "end_date"),
    ),
    (
        "export-experiments",
        "GET",
        "/export/experiments",
        UserRole.VIEWER,
        ("start_date", "end_date"),
    ),
    (
        "export-variants",
        "GET",
        "/export/variants",
        UserRole.VIEWER,
        ("start_date", "end_date"),
    ),
    (
        "export-feature-flags",
        "GET",
        "/export/feature-flags",
        UserRole.VIEWER,
        ("start_date", "end_date"),
    ),
    (
        "report-overview",
        "GET",
        "/export/reports/overview",
        UserRole.VIEWER,
        ("start_date", "end_date"),
    ),
    (
        "metrics-aggregate",
        "POST",
        "/metrics/aggregate",
        UserRole.VIEWER,
        ("start_time", "end_time"),
    ),
]
SITE_IDS = [s[0] for s in SITES]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    # The compliance routes take their session from ``db.session.get_db``, the
    # others from ``deps.get_db``: one test session for both.
    with api_client(db_session) as c:
        c.app.dependency_overrides[session_get_db] = c.app.dependency_overrides[
            deps.get_db
        ]
        try:
            yield c
        finally:
            c.app.dependency_overrides.pop(session_get_db, None)


@pytest.fixture
def users(db_session):
    return {role: make_user(db_session, role, tag=TAG) for role in UserRole}


def _call(client, site, user, params):
    _, method, path, _, _ = site
    return client.request(
        method,
        V1 + path.format(me=user.id),
        params=params,
        headers=auth(user),
    )


def _body_is(resp, field):
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["query", field],
                "msg": OFFSET_MSG,
                "ctx": {"error": {}},
            }
        ]
    }


@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
@pytest.mark.parametrize("which", [0, 1], ids=["first", "second"])
@pytest.mark.parametrize(
    "sent", ["2026-01-01T00:00:00+16:00", "2026-01-01T00:00:00-16:00"]
)
def test_an_offset_of_16_hours_or_more_answers_422(client, users, site, which, sent):
    field = site[4][which]
    resp = _call(client, site, users[site[3]], {field: sent})
    _body_is(resp, field)
    assert sent not in resp.text
    assert sent[-6:] not in resp.text


@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
def test_a_far_offset_beyond_a_day_also_answers_422(client, users, site):
    field = site[4][0]
    resp = _call(client, site, users[site[3]], {field: "2026-01-01T00:00:00+23:00"})
    _body_is(resp, field)


@pytest.mark.parametrize("site", SITES, ids=SITE_IDS)
@pytest.mark.parametrize(
    "sent",
    [
        "2026-01-01T00:00:00+15:59",
        "2026-01-01T00:00:00-15:59",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00",
    ],
    ids=["+15:59", "-15:59", "Z", "naive"],
)
def test_offsets_under_16_hours_utc_and_naive_dates_are_answered(
    client, users, site, sent
):
    resp = _call(client, site, users[site[3]], {site[4][0]: sent})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "site",
    [s for s in SITES if s[3] != UserRole.ANALYST],
    ids=[s[0] for s in SITES if s[3] != UserRole.ANALYST],
)
def test_an_unauthenticated_caller_gets_401_whatever_the_date(client, site, users):
    _, method, path, _, (field, _) = site
    resp = client.request(
        method,
        V1 + path.format(me=users[UserRole.VIEWER].id),
        params={field: "2026-01-01T00:00:00+23:00"},
    )
    assert resp.status_code == 401, resp.text


def test_a_viewer_is_refused_before_the_date_is_looked_at(client, users):
    """Routes that forbid VIEWER answer 403, not 422, for a bad date."""
    viewer = users[UserRole.VIEWER]
    for path in ("/audit-logs/stats", "/compliance/audit-events"):
        field = "from_date" if "audit-logs" in path else "start_time"
        resp = client.get(
            V1 + path,
            params={field: "2026-01-01T00:00:00+23:00"},
            headers=auth(viewer),
        )
        assert resp.status_code == 403, (path, resp.text)


def test_a_route_without_the_parameter_is_untouched(client, users):
    """``/audit-logs/entity/...`` takes no date: it is not in the fix."""
    resp = client.get(
        V1 + "/audit-logs/entity/experiment/00000000-0000-0000-0000-000000000000",
        params={"from_date": "2026-01-01T00:00:00+23:00"},
        headers=auth(users[UserRole.ANALYST]),
    )
    assert resp.status_code == 200, resp.text

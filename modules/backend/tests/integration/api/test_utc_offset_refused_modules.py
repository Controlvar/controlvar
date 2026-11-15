"""A date with a UTC offset beyond 15:59 answers 422, not 500, on the module routes (#956).

The module half of ``backend/tests/integration/api/test_utc_offset_refused.py``:
the compliance report and export, the HIPAA audit log and report, and the
``expires_at`` of an RBAC grant. PostgreSQL stores a UTC offset of at most
15:59:59 and failed the query ("time zone displacement out of range"), a 500.

The query parameters are refused after the route's permission check, so a
caller who may not use the route still gets 403 and learns nothing from a 422.
The grant's body field is refused while the body is parsed, as every other
field of that body already is, so it answers before the route's admin check;
401 still comes first. Offsets up to 15:59, ``Z`` and a naive date answer as
before.

Everything runs on PostgreSQL through the real route.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.api import deps
from backend.app.db.session import get_db as session_get_db
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import cleanup

pytestmark = [pytest.mark.integration, pytest.mark.regression, pytest.mark.requires_db]

V1 = "/api/v1"
TAG = "off956m"

OFFSET_MSG = "Value error, the UTC offset must be less than 16 hours"

BAD = ["2026-01-01T00:00:00+16:00", "2026-01-01T00:00:00-16:00"]
GOOD = [
    "2026-01-01T00:00:00+15:59",
    "2026-01-01T00:00:00-15:59",
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00",
]

#: (id, path, the role that may call it, the two date parameters).
QUERY_SITES = [
    (
        "compliance-report",
        "/compliance/reports/soc2",
        UserRole.ANALYST,
        ("start_time", "end_time"),
    ),
    (
        "compliance-export",
        "/compliance/export",
        UserRole.ADMIN,
        ("start_time", "end_time"),
    ),
    (
        "hipaa-audit-logs",
        "/hipaa/audit-logs",
        UserRole.ADMIN,
        ("start_date", "end_date"),
    ),
    ("hipaa-report", "/hipaa/report", UserRole.ADMIN, ("start_date", "end_date")),
]
QUERY_IDS = [s[0] for s in QUERY_SITES]


def _refusal(field_loc):
    return {
        "detail": [
            {
                "type": "value_error",
                "loc": field_loc,
                "msg": OFFSET_MSG,
                "ctx": {"error": {}},
            }
        ]
    }


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
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
    # A superuser would pass every role check: these are plain role holders.
    yield {role: make_user(db_session, role, tag=TAG) for role in UserRole}
    db_session.rollback()
    rows = db_session.query(User).filter(User.username.like(f"{TAG}%")).all()
    ids = [u.id for u in rows]
    if ids:
        db_session.execute(
            text(
                "DELETE FROM audit_logs WHERE user_id = ANY(:ids) "
                "OR entity_id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        db_session.commit()
    cleanup(db_session, TAG, ids)


@pytest.mark.parametrize("site", QUERY_SITES, ids=QUERY_IDS)
@pytest.mark.parametrize("which", [0, 1], ids=["first", "second"])
@pytest.mark.parametrize("sent", BAD)
def test_an_offset_of_16_hours_or_more_answers_422(client, users, site, which, sent):
    _, path, role, fields = site
    resp = client.get(
        V1 + path, params={fields[which]: sent}, headers=auth(users[role])
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == _refusal(["query", fields[which]])
    assert sent not in resp.text
    assert sent[-6:] not in resp.text


@pytest.mark.parametrize("site", QUERY_SITES, ids=QUERY_IDS)
@pytest.mark.parametrize("sent", GOOD, ids=["+15:59", "-15:59", "Z", "naive"])
def test_offsets_under_16_hours_utc_and_naive_dates_are_answered(
    client, users, site, sent
):
    _, path, role, fields = site
    resp = client.get(V1 + path, params={fields[0]: sent}, headers=auth(users[role]))
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("site", QUERY_SITES, ids=QUERY_IDS)
def test_a_caller_without_the_role_gets_403_whatever_the_date(client, users, site):
    _, path, _, fields = site
    resp = client.get(
        V1 + path,
        params={fields[0]: BAD[0]},
        headers=auth(users[UserRole.VIEWER]),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("site", QUERY_SITES, ids=QUERY_IDS)
def test_an_unauthenticated_caller_gets_401_whatever_the_date(client, site):
    _, path, _, fields = site
    resp = client.get(V1 + path, params={fields[0]: BAD[0]})
    assert resp.status_code == 401, resp.text


# --- the grant's expires_at ------------------------------------------------


def _grant(client, caller, target, expires_at):
    return client.post(
        f"{V1}/rbac/users/{target.id}/grant",
        json={
            "user_id": str(target.id),
            "resource": "experiment",
            "actions": ["read"],
            "expires_at": expires_at,
        },
        headers=auth(caller),
    )


def _grant_rows(db_session, user_id) -> int:
    db_session.rollback()
    return db_session.execute(
        text("SELECT count(*) FROM direct_permission_grants WHERE user_id = :u"),
        {"u": user_id},
    ).scalar_one()


@pytest.mark.parametrize("sent", BAD + ["2099-01-01T00:00:00+23:00"])
def test_a_grant_expiry_beyond_15_59_answers_422_and_writes_nothing(
    client, db_session, users, sent
):
    admin, target = users[UserRole.ADMIN], users[UserRole.DEVELOPER]
    resp = _grant(client, admin, target, sent)
    assert resp.status_code == 422, resp.text
    assert resp.json() == _refusal(["body", "expires_at"])
    assert sent not in resp.text
    assert _grant_rows(db_session, target.id) == 0


@pytest.mark.parametrize(
    "sent",
    ["2099-01-01T00:00:00+15:59", "2099-01-01T00:00:00Z", "2099-01-01T00:00:00", None],
    ids=["+15:59", "Z", "naive", "none"],
)
def test_a_grant_expiry_up_to_15_59_utc_naive_or_absent_still_grants(
    client, db_session, users, sent
):
    admin, target = users[UserRole.ADMIN], users[UserRole.DEVELOPER]
    resp = _grant(client, admin, target, sent)
    assert resp.status_code == 201, resp.text
    assert _grant_rows(db_session, target.id) == 1


def test_a_grant_without_a_token_is_401_whatever_the_expiry(client, users):
    target = users[UserRole.DEVELOPER]
    resp = client.post(
        f"{V1}/rbac/users/{target.id}/grant",
        json={
            "user_id": str(target.id),
            "resource": "experiment",
            "actions": ["read"],
            "expires_at": BAD[0],
        },
    )
    assert resp.status_code == 401, resp.text


def test_a_viewer_with_a_good_expiry_is_still_403_and_a_bad_body_is_parsed_first(
    client, db_session, users
):
    """The body is parsed before the route's admin check, today and after.

    A good body from a VIEWER answers 403 as before. A bad ``expires_at`` from
    a VIEWER answers 422, as a bad ``actions`` already does; nothing is written
    either way.
    """
    viewer, target = users[UserRole.VIEWER], users[UserRole.DEVELOPER]
    assert _grant(client, viewer, target, "2099-01-01T00:00:00Z").status_code == 403
    assert _grant(client, viewer, target, BAD[0]).status_code == 422
    assert _grant_rows(db_session, target.id) == 0

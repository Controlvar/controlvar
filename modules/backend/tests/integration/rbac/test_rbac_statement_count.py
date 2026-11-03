"""No N+1: what a permission check costs in statements (#891, check #37).

A ``before_cursor_execute`` listener on the test engine counts, per request,
the statements that touch ``user_custom_roles``, ``custom_roles`` or
``direct_permission_grants``:

* an ADMIN (the role admits every check these routes make): exactly 0;
* a grant-holder: exactly :data:`P`, the same with 1 and with 3 custom roles,
  and for a bulk toggle of 1 and of 5 flags. The first denied check of a
  request reads the provider (two indexed SELECTs); every later check in that
  request reads the session's cache. ``P > 0`` is asserted, so a listener on
  the wrong engine cannot pass.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import event, text

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import (
    assign,
    cleanup,
    grant,
    make_role,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "n1x891"
TABLES = ("user_custom_roles", "custom_roles", "direct_permission_grants")
#: The statements the provider reads per request for a user it is asked about.
P = 2


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    cleanup(db_session, TAG, [u.id for u in users])


@contextlib.contextmanager
def counting(engine):
    seen = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if any(table in statement for table in TABLES):
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _flags(db_session, owner, n):
    flags = []
    for _ in range(n):
        flag = FeatureFlag(
            key=f"n1-{uuid.uuid4().hex[:10]}",
            name="n1",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=owner.id,
            rollout_percentage=0,
        )
        db_session.add(flag)
        flags.append(flag)
    db_session.commit()
    return [str(f.id) for f in flags]


def _holder(db_session, roles: int):
    """A VIEWER with *roles* activated custom roles and a direct grant."""
    user = make_user(db_session, UserRole.VIEWER, tag=TAG)
    for i in range(roles):
        pair = ("feature_flag", "update") if i == 0 else ("experiment", "read")
        assign(db_session, user.id, make_role(db_session, TAG, [pair]), activated=True)
    grant(db_session, user.id, "audit_log", ["read"])
    db_session.commit()
    return user


def _bulk_toggle(client, user, flag_ids):
    resp = client.post(
        f"{V1}/feature-flags/bulk-toggle",
        json={"flag_ids": flag_ids, "action": "enable"},
        headers=auth(user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == len(flag_ids), body
    return body


@pytest.mark.parametrize("flags", [1, 5])
def test_an_admin_reads_no_rbac_table(client, db_session, flags):
    admin = make_user(db_session, UserRole.ADMIN, tag=TAG)
    flag_ids = _flags(db_session, admin, flags)
    with counting(db_session.get_bind()) as seen:
        _bulk_toggle(client, admin, flag_ids)
    assert len(seen) == 0, f"ADMIN, {flags} flags: {len(seen)} statements != 0"


@pytest.mark.parametrize("roles", [1, 3])
@pytest.mark.parametrize("flags", [1, 5])
def test_a_grant_holder_reads_the_provider_once_per_request(
    client, db_session, roles, flags
):
    owner = make_user(db_session, UserRole.ADMIN, tag=TAG)
    holder = _holder(db_session, roles)
    flag_ids = _flags(db_session, owner, flags)
    with counting(db_session.get_bind()) as seen:
        _bulk_toggle(client, holder, flag_ids)
    assert P > 0
    assert len(seen) == P, (
        f"K={roles}, {flags} flags: {len(seen)} statements != {P}: {seen}"
    )
    # Re-read every flag: the toggle really happened for each (a per-flag
    # failure would answer 200 with success false).
    db_session.expire_all()
    statuses = {
        row[0]
        for row in db_session.execute(
            text("SELECT status FROM feature_flags WHERE id = ANY(:ids)"),
            {"ids": [uuid.UUID(f) for f in flag_ids]},
        )
    }
    assert statuses == {"ACTIVE"}, statuses

"""A password change ends the account's sessions (#501).

Every local access token, and the short token between the two steps of a
two-step sign-in, carries the account's password stamp (``pwv``); a token
whose stamp no longer matches the stored hash is refused. So:

* ``POST /api/v1/users/me/password`` answers 200 with a new access token for
  an account that signs in with its password alone (the same answer
  ``POST /api/v1/auth/login`` gives it), and every earlier token, the one that
  sent the request included, answers 401 from then on;
* an account that sign-in would ask for a code or for setting two-step
  sign-in up, or one with no email address (sign-in finds an account by its
  address), gets 204 as before, and every token of the account ends;
* a sign-in that took its password step before the change starts again;
* the 200 answer is recorded as a sign-in (one ``user_login`` entry), the
  204 answer records nothing;
* only the password does this: a change of address, name or two-step
  sign-in leaves every session as it was; and a change made with raw SQL
  ends them as well, because the check reads the row, not process memory.

Real ``users`` and ``user_totp`` rows, real tokens, the real lockout counter,
through the real routes; the only stand-ins are ``deps.get_db`` (a session of
the test database per request, ``expire_on_commit`` on as in production), the
two-step service's clock and the bcrypt work factor of the stored hashes (4,
for speed; the format is the same).
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.two_factor_answers import SIGN_IN_EXPIRED_DETAIL
from backend.app.core import totp
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, decode_local_token
from backend.app.core.settings_rules import parse_two_factor_roles
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.app.services import two_factor_service as service_module
from backend.app.services.local_auth_service import login_attempt_tracker
from backend.app.services.two_factor_service import two_factor_service
from backend.tests.integration.services.two_factor_fixtures import (
    Clock,
    code_at,
    fast_hash,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

OLD = "Old-Passw0rd-501"
OLD_HASH = fast_hash(OLD)
NEW = "New-Passw0rd-501x"

ME_PASSWORD = "/api/v1/users/me/password"
ME_2FA = "/api/v1/users/me/2fa"
AUTH_ME = "/api/v1/auth/me"
LOGIN = "/api/v1/auth/login"
VERIFY = "/api/v1/auth/mfa/verify"
ENROL = "/api/v1/auth/mfa/enroll"
ENROL_CONFIRM = "/api/v1/auth/mfa/enroll/confirm"
USERS = "/api/v1/users"
ADMIN_USERS = "/api/v1/admin/users"

LOGIN_RESPONSE_FIELDS = {"access_token", "token_type", "expires_in", "user"}


@pytest.fixture(autouse=True)
def _local_provider(monkeypatch):
    # backend/tests/conftest.py already turns the development sign-in off.
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LOCAL_AUTH_2FA_REQUIRED_ROLES", [])
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """The two-step service reads this clock; recovery codes hash at cost 4."""
    now = Clock()
    monkeypatch.setattr(two_factor_service, "clock", now)
    monkeypatch.setattr(service_module, "get_password_hash", fast_hash)
    return now


@pytest.fixture
def require(monkeypatch):
    """Set ``LOCAL_AUTH_2FA_REQUIRED_ROLES`` as the environment would."""

    def set_roles(value):
        monkeypatch.setattr(
            settings, "LOCAL_AUTH_2FA_REQUIRED_ROLES", parse_two_factor_roles(value)
        )

    return set_roles


@pytest.fixture
def session_factory(test_db):
    # expire_on_commit on, as backend/app/db/session.py has it.
    return sessionmaker(bind=test_db, autocommit=False, autoflush=False)


_UNSET = object()


@pytest.fixture
def make_user(session_factory):
    def make(role=UserRole.VIEWER, is_superuser=False, email=_UNSET, username=_UNSET):
        session = session_factory()
        suffix = uuid.uuid4().hex[:10]
        user = User(
            username=f"pw501_{suffix}" if username is _UNSET else username,
            email=f"pw501_{suffix}@example.com" if email is _UNSET else email,
            full_name="Password Sessions",
            hashed_password=OLD_HASH,
            is_active=True,
            is_superuser=is_superuser,
            role=role,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        session.close()
        return user

    return make


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def read(session_factory):
    """Run ``fn(session)`` on a session of its own and close it."""

    def run(fn):
        session = session_factory()
        try:
            return fn(session)
        finally:
            session.close()

    return run


def _bearer(user) -> dict:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _token(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _change(client, headers, current=OLD, new=NEW):
    return client.post(
        ME_PASSWORD,
        json={"current_password": current, "new_password": new},
        headers=headers,
    )


def _login(client, user, password):
    return client.post(LOGIN, json={"email": user.email, "password": password})


def _me(client, headers) -> int:
    return client.get(AUTH_ME, headers=headers).status_code


def _secret(text_: str) -> bytes:
    return base64.b32decode(text_ + "=" * (-len(text_) % 8))


def _turn_on(client, user, clock):
    """Two-step sign-in on through the routes; returns the secret. The confirm
    code's step is used up; the clock moves one step on."""
    enrol = client.post(
        f"{ME_2FA}/enroll", json={"current_password": OLD}, headers=_bearer(user)
    )
    assert enrol.status_code == 200, enrol.text
    secret = _secret(enrol.json()["secret"])
    confirm = client.post(
        f"{ME_2FA}/confirm",
        json={"code": code_at(secret, clock.step())},
        headers=_bearer(user),
    )
    assert confirm.status_code == 200, confirm.text
    clock.now += totp.STEP_SECONDS
    return secret


def _logins(read, user_id):
    return read(
        lambda s: (
            s.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == user_id, AuditLog.action_type == "user_login"
                )
            )
            .scalars()
            .all()
        )
    )


def _set_hash_with_sql(read, user, value):
    def run(session):
        session.execute(
            text("UPDATE users SET hashed_password = :value WHERE id = :id"),
            {"value": value, "id": user.id},
        )
        session.commit()

    read(run)


def _superuser_reset(client, superuser, target, route, password=NEW):
    base = USERS if route == "users" else ADMIN_USERS
    return client.put(
        f"{base}/{target.id}",
        json={"username": target.username, "email": target.email, "password": password},
        headers=_bearer(superuser),
    )


# ---------------------------------------------------------------------------
# The caller's own change
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_answer_carries_a_new_token_and_the_sending_token_is_refused(
    client, make_user
):
    user = make_user()
    earlier = _bearer(user)
    response = _change(client, earlier)
    assert _me(client, earlier) == 401

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == LOGIN_RESPONSE_FIELDS
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.LOCAL_AUTH_TOKEN_TTL_MINUTES * 60
    assert body["user"]["id"] == str(user.id)
    assert body["user"]["email"] == user.email
    assert body["user"]["auth_provider"] == "local"
    # A full lifetime from the change.
    claims = decode_local_token(body["access_token"])
    assert claims["exp"] - claims["iat"] == settings.LOCAL_AUTH_TOKEN_TTL_MINUTES * 60
    assert claims["sub"] == str(user.id)

    me = client.get(AUTH_ME, headers=_token(body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["id"] == str(user.id)
    assert _login(client, user, NEW).status_code == 200


@pytest.mark.regression
@pytest.mark.parametrize("account", ["two-step-on", "required-not-set-up"])
def test_an_account_that_signs_in_with_a_second_step_gets_204_and_signs_in_again(
    client, make_user, clock, require, account
):
    user = make_user(role=UserRole.ANALYST)
    if account == "two-step-on":
        _turn_on(client, user, clock)
    else:
        require("ANALYST")
    earlier = _bearer(user)

    response = _change(client, earlier)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert "access_token" not in response.text
    assert _me(client, earlier) == 401
    again = _login(client, user, NEW)
    assert again.status_code == 200, again.text
    assert "access_token" not in again.json()
    assert again.json()["mfa_required"] is True
    assert again.json()["enrol"] is (account == "required-not-set-up")


@pytest.mark.regression
@pytest.mark.parametrize(
    "account",
    ["plain", "no-username", "two-step-on", "required-not-set-up", "required-set-up"],
)
def test_the_change_issues_a_token_exactly_when_sign_in_would(
    client, make_user, clock, require, account
):
    """The change's answer carries an access token for the same accounts as
    ``POST /auth/login`` with the new password does, and for no other. One
    with no username (#2067) is among them, with ``username`` null in both
    answers."""
    signs_in = account in ("plain", "no-username")
    user = make_user(
        role=UserRole.VIEWER if signs_in else UserRole.ANALYST,
        username=None if account == "no-username" else _UNSET,
    )
    if account in ("required-not-set-up", "required-set-up"):
        require("ANALYST")
    if account in ("two-step-on", "required-set-up"):
        _turn_on(client, user, clock)

    change = _change(client, _bearer(user))
    assert change.status_code in (200, 204), change.text
    login = _login(client, user, NEW)
    assert login.status_code == 200, login.text

    assert ("access_token" in change.text) == ("access_token" in login.text), (
        account,
        change.status_code,
        login.json(),
    )
    assert ("access_token" in change.text) is signs_in
    assert change.status_code == (200 if signs_in else 204)
    if account == "no-username":
        for answer in (change, login):
            assert answer.json()["user"]["username"] is None
            assert answer.json()["user"]["email"] == user.email


@pytest.mark.regression
def test_an_account_with_no_email_address_gets_204_and_its_sessions_end(
    client, make_user, read
):
    """Sign-in finds an account by its email address, so a password does not
    sign this one in: the change ends every session and answers 204 with no
    token, and records no sign-in."""
    user = make_user(email=None)
    earlier = _bearer(user)

    response = _change(client, earlier)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert "access_token" not in response.text
    assert _me(client, earlier) == 401
    assert _logins(read, user.id) == []


@pytest.mark.regression
def test_a_token_minted_from_the_row_as_it_was_before_the_change_is_refused(
    client, make_user
):
    """Why a test that mints a token from an object it read before a change
    gets 401 after it: the token carries the stamp of the hash that object
    still holds."""
    user = make_user()
    assert _change(client, _bearer(user)).status_code == 200
    stale = _bearer(user)  # ``user`` still holds the hash from before
    assert _me(client, stale) == 401


# ---------------------------------------------------------------------------
# A sign-in in flight (a superuser's reset ending the account's sessions is
# in test_own_password_change.py, beside the reset's other tests)
# ---------------------------------------------------------------------------


def _change_between_the_steps(client, make_user, user, how):
    if how == "admin-reset":
        superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
        reset = _superuser_reset(client, superuser, user, "users")
        assert reset.status_code == 200, reset.text
    else:
        own = _change(client, _bearer(user))
        assert own.status_code == 204, own.text


def _assert_sign_in_expired(response):
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": SIGN_IN_EXPIRED_DETAIL}
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.regression
@pytest.mark.parametrize("how", ["admin-reset", "own-change"])
def test_a_code_step_begun_before_the_change_starts_again(
    client, make_user, clock, how
):
    user = make_user(role=UserRole.ANALYST)
    secret = _turn_on(client, user, clock)
    first = _login(client, user, OLD)
    assert first.json()["mfa_required"] is True, first.text
    key = user.email.lower()

    _change_between_the_steps(client, make_user, user, how)
    counted = login_attempt_tracker.status(key).failures
    code = code_at(secret, clock.step())
    late = client.post(
        VERIFY, json={"mfa_token": first.json()["mfa_token"], "code": code}
    )

    _assert_sign_in_expired(late)
    assert login_attempt_tracker.status(key).failures == counted
    # A password step with the new password completes, with the same code:
    # the refused request did not use it.
    again = _login(client, user, NEW)
    assert again.json()["mfa_required"] is True, again.text
    done = client.post(
        VERIFY, json={"mfa_token": again.json()["mfa_token"], "code": code}
    )
    assert done.status_code == 200, done.text
    assert decode_local_token(done.json()["access_token"])["sub"] == str(user.id)


@pytest.mark.regression
@pytest.mark.parametrize("how", ["admin-reset", "own-change"])
def test_setting_two_step_up_begun_before_the_change_starts_again(
    client, make_user, clock, require, how
):
    require("ANALYST")
    user = make_user(role=UserRole.ANALYST)
    first = _login(client, user, OLD)
    assert first.json()["enrol"] is True, first.text
    token = first.json()["mfa_token"]
    pending = client.post(ENROL, json={"mfa_token": token})
    assert pending.status_code == 200, pending.text
    secret = _secret(pending.json()["secret"])
    key = user.email.lower()

    _change_between_the_steps(client, make_user, user, how)
    counted = login_attempt_tracker.status(key).failures

    _assert_sign_in_expired(client.post(ENROL, json={"mfa_token": token}))
    _assert_sign_in_expired(
        client.post(
            ENROL_CONFIRM,
            json={"mfa_token": token, "code": code_at(secret, clock.step())},
        )
    )
    assert login_attempt_tracker.status(key).failures == counted

    # A password step with the new password sets it up and signs in.
    again = _login(client, user, NEW)
    assert again.json()["enrol"] is True, again.text
    fresh = again.json()["mfa_token"]
    enrol = client.post(ENROL, json={"mfa_token": fresh})
    assert enrol.status_code == 200, enrol.text
    done = client.post(
        ENROL_CONFIRM,
        json={
            "mfa_token": fresh,
            "code": code_at(_secret(enrol.json()["secret"]), clock.step()),
        },
    )
    assert done.status_code == 200, done.text
    assert decode_local_token(done.json()["login"]["access_token"])["sub"] == str(
        user.id
    )


# ---------------------------------------------------------------------------
# Only the password, wherever it is written
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_hash_written_with_raw_sql_ends_the_sessions(client, make_user, read):
    """The check reads the row each request loads, not anything remembered."""
    user = make_user()
    earlier = _bearer(user)
    assert _me(client, earlier) == 200
    _set_hash_with_sql(read, user, fast_hash("Written-With-Sql-501"))
    assert _me(client, earlier) == 401


def test_other_changes_to_the_account_leave_its_sessions(client, make_user, clock):
    """An administrator's own address and name, and two-step sign-in turned
    on: the token from before still works."""
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    earlier = _bearer(superuser)
    renamed = client.put(
        f"{USERS}/{superuser.id}",
        json={
            "username": superuser.username,
            "email": f"renamed_{uuid.uuid4().hex[:8]}@example.com",
            "full_name": "Renamed Administrator",
        },
        headers=earlier,
    )
    assert renamed.status_code == 200, renamed.text
    assert _me(client, earlier) == 200

    user = make_user()
    before = _bearer(user)
    _turn_on(client, user, clock)
    assert _me(client, before) == 200


# ---------------------------------------------------------------------------
# The answer that carries a token is recorded as a sign-in
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_answer_with_a_token_is_recorded_as_one_sign_in(client, make_user, read):
    user = make_user()
    assert _logins(read, user.id) == []

    assert _change(client, _bearer(user)).status_code == 200

    entries = _logins(read, user.id)
    assert len(entries) == 1
    entry = entries[0]
    assert json.loads(entry.new_value) == {
        "provider": "local",
        "via": "password_change",
    }
    assert entry.user_id == user.id
    assert entry.entity_type == "user"


@pytest.mark.regression
def test_the_answer_without_a_token_records_no_sign_in(
    client, make_user, read, require
):
    require("ANALYST")
    user = make_user(role=UserRole.ANALYST)

    assert _change(client, _bearer(user)).status_code == 204

    assert len(_logins(read, user.id)) == 0

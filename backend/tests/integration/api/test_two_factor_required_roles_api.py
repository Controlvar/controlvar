"""Two-step sign-in required for chosen roles, through the real routes and a
real database (#1812).

``LOCAL_AUTH_2FA_REQUIRED_ROLES`` names the roles. An account of a listed role
that has not set two-step sign-in up gets ``enrol: true`` at the password step
and sets it up with ``POST /api/v1/auth/mfa/enroll`` and
``/auth/mfa/enroll/confirm`` before it has a session; it cannot turn it off;
its own status says ``required: true``.

Real ``users`` and ``user_totp`` rows, real tokens, the real lockout counter
and the real service; the stand-ins are those of
``test_two_factor_sign_in_api.py``: a session of the test database per
request, the service's clock (a number the test moves) and recovery codes
hashed at bcrypt cost 4. The setting is set on the settings object, as the
environment would set it at start-up.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.two_factor_answers import (
    NO_PENDING_SIGN_IN_SECRET_DETAIL,
    SIGN_IN_EXPIRED_DETAIL,
    TWO_FACTOR_REQUIRED_FOR_ROLE,
)
from backend.app.core import totp
from backend.app.core.config import settings
from backend.app.core.mfa_token import (
    ENROL_PURPOSE,
    ENROL_TTL_SECONDS,
    create_mfa_token,
)
from backend.app.core.security import create_local_access_token, decode_local_token
from backend.app.core.settings_rules import parse_two_factor_roles
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.app.models.user_totp import UserTOTP
from backend.app.services import two_factor_service as service_module
from backend.app.services.local_auth_service import login_attempt_tracker
from backend.app.services.two_factor_service import (
    CODE_STEP_CAP,
    CODE_STEP_LOCKED_TEXT,
    WRONG_CODE_TEXT,
    two_factor_service,
)
from backend.tests.integration.services.two_factor_fixtures import (
    Clock,
    code_at,
    fast_hash,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

PASSWORD = "Required-Roles-1812"
PASSWORD_HASH = fast_hash(PASSWORD)
LOGIN = "/api/v1/auth/login"
TOKEN = "/api/v1/auth/token"
VERIFY = "/api/v1/auth/mfa/verify"
ENROL = "/api/v1/auth/mfa/enroll"
ENROL_CONFIRM = "/api/v1/auth/mfa/enroll/confirm"
ME_2FA = "/api/v1/users/me/2fa"
LOCKED_PREFIX = "Too many failed login attempts; account temporarily locked."


@pytest.fixture(autouse=True)
def _local_provider(monkeypatch):
    # The dev sign-in switch is off for every test (backend/tests/conftest.py).
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


@pytest.fixture
def require(monkeypatch):
    """Set ``LOCAL_AUTH_2FA_REQUIRED_ROLES`` as the environment would."""

    def set_roles(value):
        monkeypatch.setattr(
            settings, "LOCAL_AUTH_2FA_REQUIRED_ROLES", parse_two_factor_roles(value)
        )

    return set_roles


@pytest.fixture
def clock(monkeypatch) -> Clock:
    now = Clock()
    monkeypatch.setattr(two_factor_service, "clock", now)
    monkeypatch.setattr(service_module, "get_password_hash", fast_hash)
    return now


@pytest.fixture
def session_factory(test_db):
    return sessionmaker(bind=test_db, autoflush=False, expire_on_commit=False)


@pytest.fixture
def make_user(session_factory):
    def make(role=UserRole.DEVELOPER, is_superuser=False, password=PASSWORD_HASH):
        session = session_factory()
        suffix = uuid.uuid4().hex[:10]
        user = User(
            username=f"tfa_req_{suffix}",
            email=f"Required.Role.{suffix}@Example.com",
            hashed_password=password,
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
    def run(fn):
        session = session_factory()
        try:
            return fn(session)
        finally:
            session.close()

    return run


def _bearer(user) -> dict:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _secret(text_: str) -> bytes:
    return base64.b32decode(text_ + "=" * (-len(text_) % 8))


def _password_step(client, user, email=None, password=PASSWORD):
    return client.post(LOGIN, json={"email": email or user.email, "password": password})


def _enrol_token(client, user) -> str:
    answer = _password_step(client, user)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["mfa_required"], body["enrol"], body["expires_in"]) == (
        True,
        True,
        ENROL_TTL_SECONDS,
    )
    return body["mfa_token"]


def _set_up_at_sign_in(client, user, clock):
    """The first sign-in of a required role, through the routes: (secret,
    recovery codes, access token). The clock moves one step on after."""
    token = _enrol_token(client, user)
    enrol = client.post(ENROL, json={"mfa_token": token})
    assert enrol.status_code == 200, enrol.text
    secret = _secret(enrol.json()["secret"])
    done = client.post(
        ENROL_CONFIRM,
        json={"mfa_token": token, "code": code_at(secret, clock.step())},
    )
    assert done.status_code == 200, done.text
    clock.now += totp.STEP_SECONDS
    body = done.json()
    return secret, body["recovery_codes"], body["login"]["access_token"]


def _entries(read, user_id, action):
    return read(
        lambda s: (
            s.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == user_id, AuditLog.action_type == action)
                .order_by(AuditLog.timestamp)
            )
            .scalars()
            .all()
        )
    )


def _totp_row(read, user_id):
    return read(
        lambda s: s.execute(
            select(UserTOTP).where(UserTOTP.user_id == user_id)
        ).scalar_one_or_none()
    )


def _key(user) -> str:
    return user.email.lower()


# ---------------------------------------------------------------------------
# The first sign-in of a required role
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_required_role_sets_it_up_at_its_first_sign_in(
    client, make_user, clock, read, require
):
    require("DEVELOPER")
    user = make_user(role=UserRole.DEVELOPER)

    first = _password_step(client, user)
    assert first.status_code == 200, first.text
    assert set(first.json()) == {"mfa_required", "mfa_token", "enrol", "expires_in"}
    form = client.post(TOKEN, data={"username": user.email, "password": PASSWORD})
    assert form.status_code == 200, form.text
    assert form.json()["enrol"] is True
    assert "access_token" not in form.json()

    token = first.json()["mfa_token"]
    stale = _secret(client.post(ENROL, json={"mfa_token": token}).json()["secret"])
    enrol = client.post(ENROL, json={"mfa_token": token})
    assert enrol.status_code == 200, enrol.text
    assert enrol.json()["otpauth_uri"].startswith("otpauth://totp/Controlvar:")
    secret = _secret(enrol.json()["secret"])
    # A secret asked for again replaces the waiting one; until a code confirms
    # it, the password step still asks for set-up.
    refused = client.post(
        ENROL_CONFIRM,
        json={"mfa_token": token, "code": code_at(stale, clock.step())},
    )
    assert refused.status_code == 401
    assert refused.json() == {"detail": WRONG_CODE_TEXT}
    assert _password_step(client, user).json()["enrol"] is True
    assert _totp_row(read, user.id).confirmed_at is None

    step = clock.step()
    done = client.post(
        ENROL_CONFIRM, json={"mfa_token": token, "code": code_at(secret, step)}
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert len(body["recovery_codes"]) == 10
    assert decode_local_token(body["login"]["access_token"])["sub"] == str(user.id)
    assert body["login"]["user"]["email"] == user.email
    stored = _totp_row(read, user.id)
    assert stored.confirmed_at is not None
    assert stored.last_used_step == step
    assert len(_entries(read, user.id, "user_2fa_enable")) == 1
    logins = _entries(read, user.id, "user_login")
    assert [e.new_value for e in logins] == [
        '{"provider": "local", "second_step": "totp"}'
    ]

    # Once on: the code step, never set-up again; the old token is spent.
    clock.now += totp.STEP_SECONDS
    again = _password_step(client, user).json()
    assert (again["enrol"], again["expires_in"]) == (False, 300)
    signed_in = client.post(
        VERIFY,
        json={"mfa_token": again["mfa_token"], "code": code_at(secret, clock.step())},
    )
    assert signed_in.status_code == 200, signed_in.text
    for route, extra in ((ENROL, {}), (ENROL_CONFIRM, {"code": "123456"})):
        spent = client.post(route, json={"mfa_token": token, **extra})
        assert spent.status_code == 401
        assert spent.json() == {"detail": SIGN_IN_EXPIRED_DETAIL}
    status = client.get(ME_2FA, headers=_bearer(user)).json()
    assert status == {"enabled": True, "required": True, "recovery_codes_remaining": 10}


@pytest.mark.regression
def test_wrong_first_codes_share_the_password_lockout(
    client, make_user, clock, read, require
):
    """9 wrong passwords, the right one (the count stays), one wrong first
    code: locked, then the right first code is refused too (V6 at the first
    code)."""
    require("ANALYST")
    user = make_user(role=UserRole.ANALYST)
    for _ in range(9):
        assert _password_step(client, user, password="nope").status_code == 401
    token = _enrol_token(client, user)
    assert login_attempt_tracker.status(_key(user)).failures == 9
    secret = _secret(client.post(ENROL, json={"mfa_token": token}).json()["secret"])

    tenth = client.post(
        ENROL_CONFIRM,
        json={"mfa_token": token, "code": code_at(secret, clock.step() + 9)},
    )
    assert tenth.status_code == 401
    assert tenth.json() == {"detail": WRONG_CODE_TEXT}
    locked = client.post(
        ENROL_CONFIRM, json={"mfa_token": token, "code": code_at(secret, clock.step())}
    )
    assert locked.status_code == 423
    assert locked.json()["detail"].startswith(LOCKED_PREFIX)
    assert int(locked.headers["retry-after"]) > 0
    assert _password_step(client, user).status_code == 423
    assert _totp_row(read, user.id).failed_attempts == 1
    assert _totp_row(read, user.id).confirmed_at is None


def test_the_cap_holds_at_the_first_code(client, make_user, clock, read, require):
    require("DEVELOPER")
    user = make_user(role=UserRole.DEVELOPER)
    token = _enrol_token(client, user)
    secret = _secret(client.post(ENROL, json={"mfa_token": token}).json()["secret"])
    read(
        lambda s: (
            s.execute(
                text("UPDATE user_totp SET failed_attempts = :n WHERE user_id = :u"),
                {"n": CODE_STEP_CAP, "u": user.id},
            ),
            s.commit(),
        )
    )
    capped = client.post(
        ENROL_CONFIRM, json={"mfa_token": token, "code": code_at(secret, clock.step())}
    )
    assert capped.status_code == 423
    assert capped.json() == {"detail": CODE_STEP_LOCKED_TEXT}
    assert "retry-after" not in capped.headers


def test_the_first_code_before_a_secret_answers_409(client, make_user, require):
    require("DEVELOPER")
    user = make_user(role=UserRole.DEVELOPER)
    token = _enrol_token(client, user)
    early = client.post(ENROL_CONFIRM, json={"mfa_token": token, "code": "123456"})
    assert early.status_code == 409
    assert early.json() == {"detail": NO_PENDING_SIGN_IN_SECRET_DETAIL}


# ---------------------------------------------------------------------------
# Re-read at each step
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("change", ["role", "deactivated", "policy"])
def test_the_account_is_read_again_between_the_steps(
    client, make_user, read, require, change
):
    require("DEVELOPER")
    user = make_user(role=UserRole.DEVELOPER)
    token = _enrol_token(client, user)
    if change == "policy":
        require("")
    else:

        def update(session):
            stored = session.get(User, user.id)
            if change == "role":
                stored.role = UserRole.VIEWER
            else:
                stored.is_active = False
            session.commit()

        read(update)
    refused = client.post(ENROL, json={"mfa_token": token})
    assert refused.status_code == 401
    assert refused.json() == {"detail": SIGN_IN_EXPIRED_DETAIL}
    assert _totp_row(read, user.id) is None
    if change != "deactivated":
        assert "access_token" in _password_step(client, user).json()


# ---------------------------------------------------------------------------
# Turning it off, and a reset
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_required_role_cannot_turn_it_off_and_nothing_is_counted(
    client, make_user, clock, read, require
):
    require("DEVELOPER")
    user = make_user(role=UserRole.DEVELOPER)
    secret, codes, _ = _set_up_at_sign_in(client, user, clock)
    for body in ({"code": code_at(secret, clock.step())}, {"recovery_code": codes[0]}):
        off = client.post(f"{ME_2FA}/disable", json=body, headers=_bearer(user))
        assert off.status_code == 403, off.text
        assert off.json() == {"detail": TWO_FACTOR_REQUIRED_FOR_ROLE}
    stored = _totp_row(read, user.id)
    assert stored is not None and stored.failed_attempts == 0
    assert len(stored.recovery_hashes) == 10
    assert login_attempt_tracker.status(_key(user)).failures == 0
    assert _entries(read, user.id, "user_2fa_disable") == []

    # The policy lifted: the same account turns it off with a code.
    require("")
    off = client.post(
        f"{ME_2FA}/disable",
        json={"code": code_at(secret, clock.step())},
        headers=_bearer(user),
    )
    assert off.status_code == 204, off.text


@pytest.mark.regression
def test_a_reset_account_of_a_required_role_sets_it_up_again(
    client, make_user, clock, read, require
):
    require("ADMIN")
    user = make_user(role=UserRole.ADMIN)
    other_admin = make_user(role=UserRole.ADMIN, is_superuser=True)
    _set_up_at_sign_in(client, user, clock)
    reset = client.delete(
        f"/api/v1/admin/users/{user.id}/2fa", headers=_bearer(other_admin)
    )
    assert reset.status_code == 204, reset.text
    assert _totp_row(read, user.id) is None
    assert _password_step(client, user).json()["enrol"] is True


def test_a_superuser_of_a_required_role_is_asked_too(client, make_user, require):
    require("ADMIN")
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    assert _password_step(client, superuser).json()["enrol"] is True


@pytest.mark.regression
@pytest.mark.parametrize("listed", [True, False], ids=["role-listed", "not-listed"])
def test_an_account_without_a_password_reads_required_false(
    client, make_user, require, listed
):
    """PE-S2-6: the requirement applies where a password is checked; an
    account that signs in through SSO has none."""
    require("VIEWER" if listed else "")
    user = make_user(role=UserRole.VIEWER, password=None)
    status = client.get(ME_2FA, headers=_bearer(user))
    assert status.status_code == 200, status.text
    assert status.json()["required"] is False


# ---------------------------------------------------------------------------
# Secrecy, and never a session
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_no_entry_or_log_line_holds_the_secret_a_code_or_the_token(
    client, make_user, clock, read, require, caplog
):
    """V9 for the two enrol routes: only their own answers carry the secret
    and the recovery codes."""
    caplog.set_level(logging.DEBUG)
    require("DEVELOPER")
    user = make_user(role=UserRole.DEVELOPER)
    token = _enrol_token(client, user)
    enrol = client.post(ENROL, json={"mfa_token": token})
    secret_text = enrol.json()["secret"]
    secret = _secret(secret_text)
    wrong = client.post(ENROL_CONFIRM, json={"mfa_token": token, "code": "000000"})
    first_code = code_at(secret, clock.step())
    done = client.post(ENROL_CONFIRM, json={"mfa_token": token, "code": first_code})
    codes = done.json()["recovery_codes"]
    access = done.json()["login"]["access_token"]
    secrets = [secret_text, first_code, token, *codes]

    for answer in (wrong, client.get(ME_2FA, headers=_bearer(user))):
        for value in secrets + [access]:
            assert value not in answer.text
    assert token not in done.text and first_code not in done.text
    rows = read(
        lambda s: (
            s.execute(select(AuditLog).where(AuditLog.entity_id == user.id))
            .scalars()
            .all()
        )
    )
    assert {r.action_type for r in rows} == {"user_2fa_enable", "user_login"}
    for entry in rows:
        stored = " ".join(
            str(v or "")
            for v in (entry.old_value, entry.new_value, entry.reason, entry.entity_name)
        )
        for value in secrets + [access]:
            assert value not in stored
    for record in caplog.records:
        line = " ".join(
            [record.getMessage()]
            + [v for v in vars(record).values() if isinstance(v, str)]
        )
        for value in secrets + [access]:
            assert value not in line


def _signed_in_paths():
    """Every route that reads a bearer token (``get_current_user``), as
    ``test_two_factor_sign_in_api.py`` walks them."""

    def reaches(dependant):
        return dependant.call is deps.get_current_user or any(
            reaches(sub) for sub in dependant.dependencies
        )

    found = {("GET", "/api/v1/auth/me")}
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            dependant = getattr(ctx, "dependant", None)
            if dependant is not None and reaches(dependant):
                for method in getattr(ctx, "methods", None) or ():
                    found.add((method, ctx.path))
    return sorted(found)


@pytest.mark.regression
def test_an_enrol_token_for_a_stored_account_opens_no_route(client, make_user, require):
    """V5's enrol row: minted for a persisted, active superuser whose role is
    required, it is answered like garbage on every signed-in route, and the
    account's real token gets in."""
    require("ADMIN")
    admin = make_user(role=UserRole.ADMIN, is_superuser=True)
    enrol_token = create_mfa_token(
        admin, purpose=ENROL_PURPOSE, ttl_seconds=ENROL_TTL_SECONDS
    )
    placeholder = "00000000-0000-4000-8000-000000000000"
    opened = {}
    paths = _signed_in_paths()
    assert len(paths) > 100, len(paths)
    for method, path in paths:
        url = re.sub(r"\{[^}]+\}", placeholder, path)
        body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
        garbage = client.request(
            method, url, json=body, headers={"Authorization": "Bearer garbage"}
        )
        enrol = client.request(
            method, url, json=body, headers={"Authorization": f"Bearer {enrol_token}"}
        )
        if (enrol.status_code, enrol.text) != (garbage.status_code, garbage.text):
            opened[f"{method} {path}"] = (garbage.status_code, enrol.status_code)
    assert not opened, opened
    me = client.get("/api/v1/auth/me", headers=_bearer(admin))
    assert me.status_code == 200, me.text

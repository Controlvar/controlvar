"""Two-step sign-in through the real routes and a real database (#1812).

Real ``users`` and ``user_totp`` rows, real local tokens, the real lockout
counter and the real service; the only stand-ins are ``deps.get_db`` (a
session of the test database per request), the service's clock (a number the
test moves) and the bcrypt work factor of new recovery codes (4, for speed;
the format is the same). Each test signs in the way a client does: password,
then ``POST /api/v1/auth/mfa/verify``.
"""

from __future__ import annotations

import logging
import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.users import (
    CURRENT_PASSWORD_INCORRECT_DETAIL,
    TWO_FACTOR_NO_PASSWORD_DETAIL,
    TWO_FACTOR_PASSWORD_MISSING_DETAIL,
)
from backend.app.core import totp
from backend.app.core.config import settings
from backend.app.core.mfa_token import create_mfa_token
from backend.app.core.security import create_local_access_token, decode_local_token
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
    TwoFactorService,
    two_factor_service,
)
from backend.tests.integration.services.two_factor_fixtures import (
    Clock,
    code_at,
    fast_hash,
    lock_wait,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

PASSWORD = "Two-Step-Sign-In-1812"
PASSWORD_HASH = fast_hash(PASSWORD)
LOGIN = "/api/v1/auth/login"
TOKEN = "/api/v1/auth/token"
VERIFY = "/api/v1/auth/mfa/verify"
ME_2FA = "/api/v1/users/me/2fa"
ME_PASSWORD = "/api/v1/users/me/password"
WRONG_PASSWORD = "Not-The-Password-1812"
LOCKED_PREFIX = "Too many failed login attempts; account temporarily locked."
#: The password change's 423, which setting up two-step sign-in answers too.
PASSWORD_LOCKED_PREFIX = "Too many failed attempts; account temporarily locked."


@pytest.fixture(autouse=True)
def _local_provider(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """The routes' service reads this clock; recovery codes hash at cost 4."""
    now = Clock()
    monkeypatch.setattr(two_factor_service, "clock", now)
    monkeypatch.setattr(service_module, "get_password_hash", fast_hash)
    return now


@pytest.fixture
def session_factory(test_db):
    return sessionmaker(bind=test_db, autoflush=False, expire_on_commit=False)


@pytest.fixture
def make_user(session_factory):
    def make(
        role=UserRole.DEVELOPER,
        is_superuser=False,
        email=None,
        password_hash=PASSWORD_HASH,
    ) -> User:
        """``password_hash=None`` makes an account with no password of its own."""
        session = session_factory()
        suffix = uuid.uuid4().hex[:10]
        user = User(
            username=f"tfa_api_{suffix}",
            email=email or f"Two.Step.Api.{suffix}@Example.com",
            hashed_password=password_hash,
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
    """Read the database as it is now: ``read(fn)`` runs ``fn(session)``."""

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
    import base64

    return base64.b32decode(text_ + "=" * (-len(text_) % 8))


def _enrol(client, user, password=PASSWORD):
    """``POST /users/me/2fa/enroll`` with ``password`` as the current one."""
    return client.post(
        f"{ME_2FA}/enroll", json={"current_password": password}, headers=_bearer(user)
    )


def _turn_on(client, user, clock):
    """Two-step sign-in on through the routes: (secret, recovery codes). The
    confirm code's step is used up; the clock moves one step on."""
    enrol = _enrol(client, user)
    assert enrol.status_code == 200, enrol.text
    secret = _secret(enrol.json()["secret"])
    confirm = client.post(
        f"{ME_2FA}/confirm",
        json={"code": code_at(secret, clock.step())},
        headers=_bearer(user),
    )
    assert confirm.status_code == 200, confirm.text
    clock.now += totp.STEP_SECONDS
    return secret, confirm.json()["recovery_codes"]


def _password_step(client, user, email=None):
    return client.post(LOGIN, json={"email": email or user.email, "password": PASSWORD})


def _mfa_token(client, user, email=None) -> str:
    response = _password_step(client, user, email)
    assert response.status_code == 200, response.text
    assert response.json()["mfa_required"] is True
    return response.json()["mfa_token"]


def _key(user) -> str:
    return user.email.lower()


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


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_sign_in_asks_for_a_code_and_a_code_is_used_once(
    client, make_user, clock, read
):
    user = make_user()
    secret, _ = _turn_on(client, user, clock)

    first = _password_step(client, user)
    assert first.status_code == 200, first.text
    assert set(first.json()) == {"mfa_required", "mfa_token", "enrol", "expires_in"}
    form = client.post(TOKEN, data={"username": user.email, "password": PASSWORD})
    assert form.status_code == 200, form.text
    assert "access_token" not in form.json()

    code = code_at(secret, clock.step())
    signed_in = client.post(
        VERIFY, json={"mfa_token": first.json()["mfa_token"], "code": code}
    )
    assert signed_in.status_code == 200, signed_in.text
    assert decode_local_token(signed_in.json()["access_token"])["sub"] == str(user.id)
    logins = _entries(read, user.id, "user_login")
    assert [e.new_value for e in logins][-1] == (
        '{"provider": "local", "second_step": "totp"}'
    )
    assert len(_entries(read, user.id, "user_2fa_enable")) == 1

    again = client.post(
        VERIFY, json={"mfa_token": _mfa_token(client, user), "code": code}
    )
    assert again.status_code == 401
    assert again.json() == {"detail": WRONG_CODE_TEXT}


@pytest.mark.regression
def test_a_recovery_code_signs_in_once(client, make_user, clock, read):
    user = make_user()
    _, codes = _turn_on(client, user, clock)
    typed = "-".join([codes[0][:5].lower(), codes[0][5:]])

    signed_in = client.post(
        VERIFY, json={"mfa_token": _mfa_token(client, user), "recovery_code": typed}
    )
    assert signed_in.status_code == 200, signed_in.text
    again = client.post(
        VERIFY, json={"mfa_token": _mfa_token(client, user), "recovery_code": codes[0]}
    )
    assert again.status_code == 401
    status = client.get(ME_2FA, headers=_bearer(user)).json()
    assert status == {"enabled": True, "required": False, "recovery_codes_remaining": 9}
    assert len(_entries(read, user.id, "user_2fa_recovery_used")) == 1
    logins = _entries(read, user.id, "user_login")
    assert logins[-1].new_value == '{"provider": "local", "second_step": "recovery"}'


def test_a_waiting_secret_changes_nothing_and_a_new_one_replaces_it(
    client, make_user, clock
):
    user = make_user()
    first = _secret(_enrol(client, user).json()["secret"])
    plain = _password_step(client, user)
    assert "access_token" in plain.json()

    second = _secret(_enrol(client, user).json()["secret"])
    stale = client.post(
        f"{ME_2FA}/confirm",
        json={"code": code_at(first, clock.step())},
        headers=_bearer(user),
    )
    assert stale.status_code == 403
    assert stale.json() == {"detail": WRONG_CODE_TEXT}
    fresh = client.post(
        f"{ME_2FA}/confirm",
        json={"code": code_at(second, clock.step())},
        headers=_bearer(user),
    )
    assert fresh.status_code == 200, fresh.text
    assert _enrol(client, user).status_code == 409
    twice = client.post(
        f"{ME_2FA}/confirm",
        json={"code": code_at(second, clock.step() + 1)},
        headers=_bearer(user),
    )
    assert twice.status_code == 409


def test_turning_it_off_and_new_recovery_codes(client, make_user, clock, read):
    user = make_user()
    secret, old_codes = _turn_on(client, user, clock)

    renewed = client.post(
        f"{ME_2FA}/recovery-codes",
        json={"code": code_at(secret, clock.step())},
        headers=_bearer(user),
    )
    assert renewed.status_code == 200, renewed.text
    new_codes = renewed.json()["recovery_codes"]
    old = client.post(
        f"{ME_2FA}/disable", json={"recovery_code": old_codes[0]}, headers=_bearer(user)
    )
    assert old.status_code == 403
    off = client.post(
        f"{ME_2FA}/disable", json={"recovery_code": new_codes[0]}, headers=_bearer(user)
    )
    assert off.status_code == 204, off.text
    assert "access_token" in _password_step(client, user).json()
    assert len(_entries(read, user.id, "user_2fa_recovery_regenerate")) == 1
    assert len(_entries(read, user.id, "user_2fa_disable")) == 1


# ---------------------------------------------------------------------------
# One lockout for passwords and codes
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_wrong_codes_share_the_password_lockout(client, make_user, clock):
    """9 wrong passwords, the right one (no reset), a wrong code: locked, and
    then the right code is refused too. The address in another letter case
    draws on the same count."""
    user = make_user()
    secret, _ = _turn_on(client, user, clock)
    for _ in range(9):
        wrong = client.post(LOGIN, json={"email": user.email, "password": "nope"})
        assert wrong.status_code == 401
    token = _mfa_token(client, user, email=user.email.upper())
    assert login_attempt_tracker.status(_key(user)).failures == 9

    tenth = client.post(
        VERIFY, json={"mfa_token": token, "code": code_at(secret, clock.step() + 9)}
    )
    assert tenth.status_code == 401
    locked = client.post(
        VERIFY, json={"mfa_token": token, "code": code_at(secret, clock.step())}
    )
    assert locked.status_code == 423
    assert locked.json()["detail"].startswith(LOCKED_PREFIX)
    assert int(locked.headers["retry-after"]) > 0
    assert _password_step(client, user).status_code == 423


def _prove_password(client, user, route, password):
    """Send ``password`` as the current one: a change to the same password
    (``POST /users/me/password``), or a start of setting up two-step sign-in."""
    if route == "password":
        return client.post(
            ME_PASSWORD,
            json={"current_password": password, "new_password": PASSWORD},
            headers=_bearer(user),
        )
    return client.post(
        f"{ME_2FA}/enroll", json={"current_password": password}, headers=_bearer(user)
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "two_step, route, status, left",
    [
        (False, "password", 204, 0),
        (True, "password", 204, 9),
        (False, "enroll", 200, 0),
        (True, "enroll", 409, 9),
    ],
    ids=[
        "plain-password-change",
        "two-step-password-change",
        "plain-enroll",
        "two-step-enroll",
    ],
)
def test_a_right_current_password_clears_the_count_only_without_two_step_sign_in(
    client, make_user, clock, two_step, route, status, left
):
    """9 wrong current passwords, then the right one. An account with two-step
    sign-in on keeps the 9, so one more wrong password locks its sign-in; one
    without it goes to 0, as a right password at sign-in clears it."""
    user = make_user()
    if two_step:
        _turn_on(client, user, clock)
    for _ in range(9):
        wrong = _prove_password(client, user, "password", WRONG_PASSWORD)
        assert wrong.status_code == 403, wrong.text

    right = _prove_password(client, user, route, PASSWORD)
    assert right.status_code == status, right.text
    assert login_attempt_tracker.status(_key(user)).failures == left

    tenth = client.post(LOGIN, json={"email": user.email, "password": WRONG_PASSWORD})
    assert tenth.status_code == 401
    after = _password_step(client, user)
    if two_step:
        assert after.status_code == 423, after.text
        assert after.json()["detail"].startswith(LOCKED_PREFIX)
    else:
        assert after.status_code == 200, after.text
        assert "access_token" in after.json()


@pytest.mark.regression
def test_setting_it_up_checks_the_password_before_any_secret_is_stored(
    client, make_user, read
):
    """A refused call stores no secret and leaves a waiting one as it was.
    Only a wrong password is counted, and the right one clears the count of
    an account without two-step sign-in."""
    user = make_user()
    missing = [
        client.post(f"{ME_2FA}/enroll", headers=_bearer(user)),
        client.post(f"{ME_2FA}/enroll", json={}, headers=_bearer(user)),
        _enrol(client, user, None),
        _enrol(client, user, ""),
    ]
    assert [r.status_code for r in missing] == [403] * 4
    assert {r.json()["detail"] for r in missing} == {TWO_FACTOR_PASSWORD_MISSING_DETAIL}
    wrong = _enrol(client, user, WRONG_PASSWORD)
    assert wrong.status_code == 403
    assert wrong.json() == {"detail": CURRENT_PASSWORD_INCORRECT_DETAIL}
    assert _totp_row(read, user.id) is None
    assert login_attempt_tracker.status(_key(user)).failures == 1

    assert _enrol(client, user).status_code == 200
    waiting = _totp_row(read, user.id).secret_enc
    assert login_attempt_tracker.status(_key(user)).failures == 0
    assert _enrol(client, user, WRONG_PASSWORD).status_code == 403
    assert _totp_row(read, user.id).secret_enc == waiting


@pytest.mark.regression
def test_an_account_with_no_password_cannot_set_it_up(client, make_user, read):
    """Refused before the password is looked at, and not counted."""
    user = make_user(password_hash=None)
    for password in (None, "", "Anything-1a"):
        refused = _enrol(client, user, password)
        assert refused.status_code == 403, refused.text
        assert refused.json() == {"detail": TWO_FACTOR_NO_PASSWORD_DETAIL}
    assert _totp_row(read, user.id) is None
    assert login_attempt_tracker.status(_key(user)).failures == 0


@pytest.mark.regression
def test_wrong_passwords_at_set_up_lock_it_and_sign_in(client, make_user, read):
    user = make_user()
    answers = [_enrol(client, user, WRONG_PASSWORD).status_code for _ in range(10)]
    assert answers == [403] * 10
    locked = _enrol(client, user)
    assert locked.status_code == 423, locked.text
    assert locked.json()["detail"].startswith(PASSWORD_LOCKED_PREFIX)
    assert int(locked.headers["retry-after"]) > 0
    assert _password_step(client, user).status_code == 423
    assert _totp_row(read, user.id) is None


@pytest.mark.regression
def test_no_2fa_user_login_unchanged(client, make_user):
    user = make_user()
    for _ in range(9):
        client.post(LOGIN, json={"email": user.email, "password": "nope"})
    response = _password_step(client, user, email=user.email.upper())
    assert set(response.json()) == {"access_token", "token_type", "expires_in", "user"}
    assert login_attempt_tracker.status(_key(user)).failures == 0


@pytest.mark.regression
def test_ten_wrong_codes_while_signed_in_lock_the_sign_in(
    client, make_user, clock, read
):
    user = make_user()
    secret, _ = _turn_on(client, user, clock)
    wrong = code_at(secret, clock.step() + 9)
    answers = [
        client.post(
            f"{ME_2FA}/disable", json={"code": wrong}, headers=_bearer(user)
        ).status_code
        for _ in range(10)
    ]
    assert answers == [403] * 10
    assert _password_step(client, user).status_code == 423
    assert _totp_row(read, user.id).failed_attempts == 10


@pytest.mark.regression
def test_the_cap_refuses_the_right_code_until_a_superuser_resets_it(
    client, make_user, clock, read, caplog
):
    user, admin = make_user(), make_user(role=UserRole.ADMIN, is_superuser=True)
    secret, _ = _turn_on(client, user, clock)
    read(
        lambda s: (
            s.execute(
                text("UPDATE user_totp SET failed_attempts = :n WHERE user_id = :u"),
                {"n": CODE_STEP_CAP, "u": user.id},
            ),
            s.commit(),
        )
    )
    caplog.set_level(logging.WARNING)
    capped = client.post(
        VERIFY,
        json={
            "mfa_token": _mfa_token(client, user),
            "code": code_at(secret, clock.step()),
        },
    )
    assert capped.status_code == 423
    assert capped.json() == {"detail": CODE_STEP_LOCKED_TEXT}
    assert "retry-after" not in capped.headers
    reasons = [
        r.reason
        for r in caplog.records
        if getattr(r, "event", "") == "two_factor_refused"
    ]
    assert reasons == ["cap"]

    held = _bearer(user)
    reset = client.delete(f"/api/v1/admin/users/{user.id}/2fa", headers=_bearer(admin))
    assert reset.status_code == 204, reset.text
    assert _totp_row(read, user.id) is None
    assert "access_token" in _password_step(client, user).json()
    # A reset signs nobody out: a token issued before it still works.
    assert client.get(ME_2FA, headers=held).json()["enabled"] is False


def test_a_right_code_before_the_cap_clears_the_count(client, make_user, clock, read):
    user = make_user()
    secret, _ = _turn_on(client, user, clock)
    read(
        lambda s: (
            s.execute(
                text("UPDATE user_totp SET failed_attempts = :n WHERE user_id = :u"),
                {"n": CODE_STEP_CAP - 1, "u": user.id},
            ),
            s.commit(),
        )
    )
    signed_in = client.post(
        VERIFY,
        json={
            "mfa_token": _mfa_token(client, user),
            "code": code_at(secret, clock.step()),
        },
    )
    assert signed_in.status_code == 200, signed_in.text
    assert _totp_row(read, user.id).failed_attempts == 0


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------

ROLES = {
    "viewer": (UserRole.VIEWER, False),
    "analyst": (UserRole.ANALYST, False),
    "developer": (UserRole.DEVELOPER, False),
    "admin_role": (UserRole.ADMIN, False),
    "superuser": (UserRole.ADMIN, True),
}


@pytest.mark.regression
@pytest.mark.parametrize("role", sorted(ROLES))
def test_every_role_manages_its_own_and_only_a_superuser_resets_others(
    client, make_user, clock, role
):
    user_role, is_superuser = ROLES[role]
    caller, other = make_user(role=user_role, is_superuser=is_superuser), make_user()
    _turn_on(client, caller, clock)
    _turn_on(client, other, clock)
    assert client.get(ME_2FA, headers=_bearer(caller)).json()["enabled"] is True

    reset_other = client.delete(
        f"/api/v1/admin/users/{other.id}/2fa", headers=_bearer(caller)
    )
    reset_own = client.delete(
        f"/api/v1/admin/users/{caller.id}/2fa", headers=_bearer(caller)
    )
    if is_superuser:
        assert reset_other.status_code == 204, reset_other.text
        assert reset_own.status_code == 400
    else:
        assert reset_other.status_code == 403
        assert reset_own.status_code == 403
    assert client.get(ME_2FA, headers=_bearer(other)).json()["enabled"] is (
        not is_superuser
    )


def test_the_admin_list_says_who_has_two_step_sign_in_on(client, make_user, clock):
    admin, on, off = (
        make_user(role=UserRole.ADMIN, is_superuser=True),
        make_user(),
        make_user(),
    )
    _turn_on(client, on, clock)
    _enrol(client, off)
    for user, expected in ((on, True), (off, False)):
        one = client.get(f"/api/v1/admin/users/{user.id}", headers=_bearer(admin))
        assert one.json()["totp_enabled"] is expected
        listed = client.get(
            "/api/v1/admin/users",
            params={"search": user.username},
            headers=_bearer(admin),
        )
        assert [item["totp_enabled"] for item in listed.json()["items"]] == [expected]
    me = client.get("/api/v1/users/me", headers=_bearer(on))
    assert me.json()["totp_enabled"] is True


# ---------------------------------------------------------------------------
# What is recorded, and what never is
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_no_entry_log_line_or_answer_holds_a_secret_code_or_token(
    client, make_user, clock, read, caplog
):
    caplog.set_level(logging.DEBUG)
    user, admin = make_user(), make_user(role=UserRole.ADMIN, is_superuser=True)
    refused = _enrol(client, user, WRONG_PASSWORD)
    assert refused.status_code == 403
    enrol = _enrol(client, user)
    # The current password is never repeated, not even beside the secret.
    assert PASSWORD not in enrol.text
    secret_text = enrol.json()["secret"]
    secret = _secret(secret_text)
    first_code = code_at(secret, clock.step())
    confirm = client.post(
        f"{ME_2FA}/confirm", json={"code": first_code}, headers=_bearer(user)
    )
    codes = confirm.json()["recovery_codes"]
    clock.now += totp.STEP_SECONDS
    mfa_token = _mfa_token(client, user)
    second_code = code_at(secret, clock.step())
    answers = [
        refused,
        client.post(VERIFY, json={"mfa_token": mfa_token, "code": "000000"}),
        client.post(VERIFY, json={"mfa_token": mfa_token, "code": second_code}),
        client.post(VERIFY, json={"mfa_token": mfa_token, "recovery_code": codes[0]}),
        client.get(ME_2FA, headers=_bearer(user)),
        client.delete(f"/api/v1/admin/users/{user.id}/2fa", headers=_bearer(admin)),
    ]
    secrets = [
        secret_text,
        first_code,
        second_code,
        mfa_token,
        *codes,
        PASSWORD,
        WRONG_PASSWORD,
    ]

    for answer in answers:
        for value in secrets:
            assert value not in answer.text
    rows = read(
        lambda s: (
            s.execute(select(AuditLog).where(AuditLog.entity_id == user.id))
            .scalars()
            .all()
        )
    )
    for entry in rows:
        stored = " ".join(
            str(v or "")
            for v in (entry.old_value, entry.new_value, entry.reason, entry.entity_name)
        )
        for value in secrets:
            assert value not in stored
    for record in caplog.records:
        # The message and every text field (``extra`` included); numbers such
        # as the thread id are left out, where six digits occur by chance.
        line = " ".join(
            [record.getMessage()]
            + [v for v in vars(record).values() if isinstance(v, str)]
        )
        for value in secrets:
            assert value not in line
    resets = _entries(read, user.id, "user_2fa_reset")
    assert len(resets) == 1
    assert resets[0].user_id == admin.id
    assert resets[0].old_value == '{"enabled": true}'


# ---------------------------------------------------------------------------
# A code raced through the route
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_one_code_raced_through_the_route_signs_in_once(
    make_user, clock, session_factory
):
    """Session A accepts the code and keeps its transaction open; the route,
    on session B, is seen waiting on A's row lock, then refuses the code."""
    setup = session_factory()
    user = make_user()
    service = TwoFactorService(tracker=login_attempt_tracker, clock=clock)
    secret = _secret(service.enrol(setup, setup.merge(user)).secret)
    service.confirm(setup, setup.merge(user), code_at(secret, clock.step()))
    setup.close()
    clock.now += totp.STEP_SECONDS
    code = code_at(secret, clock.step())
    mfa_token = create_mfa_token(user)
    sessions = {}

    def hold(a):
        return service.check_code(
            a, a.merge(user), code=code, purpose=service_module.SIGN_IN
        )

    def contend(b):
        sessions["b"] = b

        def give_b():
            yield b

        app.dependency_overrides[deps.get_db] = give_b
        try:
            racer = TestClient(app, raise_server_exceptions=False)
            return racer.post(VERIFY, json={"mfa_token": mfa_token, "code": code})
        finally:
            app.dependency_overrides.pop(deps.get_db, None)

    outcome = lock_wait(session_factory, hold, contend)

    assert outcome["waited"] is True
    assert outcome["a"].method == "totp"
    assert outcome["b"].status_code == 401
    assert outcome["b"].json() == {"detail": WRONG_CODE_TEXT}


# ---------------------------------------------------------------------------
# The mfa_token is never a session (with a stored, active account)
# ---------------------------------------------------------------------------


def _signed_in_paths():
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
def test_an_mfa_token_for_a_stored_account_opens_no_route(client, make_user):
    admin = make_user(role=UserRole.ADMIN, is_superuser=True)
    mfa_token = create_mfa_token(admin)
    placeholder = "00000000-0000-4000-8000-000000000000"
    opened = {}
    for method, path in _signed_in_paths():
        url = re.sub(r"\{[^}]+\}", placeholder, path)
        body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
        garbage = client.request(
            method, url, json=body, headers={"Authorization": "Bearer garbage"}
        )
        mfa = client.request(
            method, url, json=body, headers={"Authorization": f"Bearer {mfa_token}"}
        )
        if (mfa.status_code, mfa.text) != (garbage.status_code, garbage.text):
            opened[f"{method} {path}"] = (garbage.status_code, mfa.status_code)
    assert not opened, opened
    me = client.get("/api/v1/auth/me", headers=_bearer(admin))
    assert me.status_code == 200, me.text

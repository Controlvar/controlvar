"""The two-step sign-in routes' contract, with no database (#1812).

What each route answers is decided here: the status and fixed text of every
refusal, the shape of every answer, what the password step answers for an
account with two-step sign-in on, and that the failure count is not cleared
by the password alone. The service is stubbed where its answer is the input
(``backend/tests/integration/services/test_two_factor_service.py`` holds the
service; ``backend/tests/integration/api/test_two_factor_sign_in_api.py`` the
routes against a database).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from backend.app.api import deps
from backend.app.api import two_factor_answers as answers
from backend.app.api.v1.endpoints import auth as auth_endpoints
from backend.app.api.v1.endpoints import users as users_endpoints
from backend.app.core.config import settings
from backend.app.core.mfa_token import create_mfa_token
from backend.app.core.security import (
    create_local_access_token,
    decode_local_token,
    get_password_hash,
)
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services import two_factor_service as service_module
from backend.app.services.local_auth_service import (
    AccountLockedError,
    LocalAuthService,
    LoginAttemptTracker,
    login_attempt_tracker,
)
from backend.app.services.two_factor_service import (
    CODE_STEP_LOCKED_TEXT,
    UNAVAILABLE_TEXT,
    WRONG_CODE_TEXT,
    AlreadyEnabledError,
    CheckResult,
    CodeStepLockedError,
    Enrolment,
    NotEnabledError,
    TwoFactorStatus,
    TwoFactorUnavailableError,
    WrongCodeError,
    two_factor_service,
)

pytestmark = [pytest.mark.unit]

PASSWORD = "Two-Step-1812"
PASSWORD_HASH = get_password_hash(PASSWORD)
LOCAL_ONLY = "Endpoint not available: AUTH_PROVIDER is not 'local'"
SIGN_IN_EXPIRED = "Sign-in expired. Start again."
LOCKED_PREFIX = "Too many failed login attempts; account temporarily locked."

LOGIN = "/api/v1/auth/login"
TOKEN = "/api/v1/auth/token"
VERIFY = "/api/v1/auth/mfa/verify"
AUTH_ENROL = "/api/v1/auth/mfa/enroll"
ENROL_CONFIRM = "/api/v1/auth/mfa/enroll/confirm"
ME_2FA = "/api/v1/users/me/2fa"

#: Every two-step sign-in route, with a body that passes validation.
ROUTES = [
    ("GET", ME_2FA, None),
    ("POST", f"{ME_2FA}/enroll", {"current_password": PASSWORD}),
    ("POST", f"{ME_2FA}/confirm", {"code": "123456"}),
    ("POST", f"{ME_2FA}/disable", {"code": "123456"}),
    ("POST", f"{ME_2FA}/recovery-codes", {"code": "123456"}),
    ("POST", VERIFY, {"mfa_token": "x", "code": "123456"}),
    ("POST", AUTH_ENROL, {"mfa_token": "x"}),
    ("POST", ENROL_CONFIRM, {"mfa_token": "x", "code": "123456"}),
    ("DELETE", "/api/v1/admin/users/00000000-0000-4000-8000-000000001812/2fa", None),
]
ROUTE_IDS = [
    "read",
    "enroll",
    "confirm",
    "disable",
    "recovery-codes",
    "verify",
    "sign-in-enroll",
    "sign-in-enroll-confirm",
    "admin-reset",
]


def _user(**overrides) -> User:
    fields = {
        "id": uuid.uuid4(),
        "username": "tfa_unit",
        "email": "Two.Step@Example.com",
        "hashed_password": PASSWORD_HASH,
        "is_active": True,
        "is_superuser": False,
        "role": UserRole.DEVELOPER,
    }
    fields.update(overrides)
    return User(**fields)


def _key(email: str) -> str:
    return f"key:{email.lower()}"


def _db(user, *, enabled: bool = False) -> MagicMock:
    """A session for ``user``: the address lookup and the counter key that
    ``authenticate`` asks for, the id lookup of the code step, and whether a
    confirmed ``user_totp`` row exists."""
    db = MagicMock(name="stub-session")
    db.scalar.side_effect = lambda statement: _key(
        str(next(iter(statement.compile().params.values())))
    )
    db.query.return_value.filter.return_value.limit.return_value.all.return_value = (
        [user] if user is not None else []
    )
    db.query.return_value.filter.return_value.first.return_value = user
    db.execute.return_value.first.return_value = (
        SimpleNamespace(user_id=user.id) if enabled and user is not None else None
    )
    return db


@pytest.fixture(autouse=True)
def local_provider(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def audit(monkeypatch):
    """Every ``user_login`` entry the sign-in routes write."""
    written = []
    monkeypatch.setattr(
        auth_endpoints.AuditService,
        "record_after_commit",
        lambda db, **entry: written.append(entry),
    )
    return written


def _client(db, *, signed_in=None, superuser=None) -> TestClient:
    def stub_db():
        yield db

    app.dependency_overrides[deps.get_db] = stub_db
    if signed_in is not None:
        app.dependency_overrides[deps.get_current_active_user] = lambda: signed_in
    if superuser is not None:
        app.dependency_overrides[deps.get_current_superuser] = lambda: superuser
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# The password step
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("path", [LOGIN, TOKEN])
def test_the_password_step_answers_mfa_required_and_no_access_token(path, audit):
    user = _user()
    client = _client(_db(user, enabled=True))
    if path == LOGIN:
        response = client.post(path, json={"email": user.email, "password": PASSWORD})
    else:
        response = client.post(
            path, data={"username": user.email, "password": PASSWORD}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"mfa_required", "mfa_token", "enrol", "expires_in"}
    assert body["mfa_required"] is True
    assert body["enrol"] is False
    assert body["expires_in"] == 300
    assert "access_token" not in body
    # Not a session: refused where access tokens are read.
    with pytest.raises(Exception):
        decode_local_token(body["mfa_token"])
    assert audit == []


@pytest.mark.regression
def test_no_2fa_user_login_unchanged(audit):
    """An account without two-step sign-in gets its token, as before."""
    user = _user()
    response = _client(_db(user)).post(
        LOGIN, json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    assert decode_local_token(body["access_token"])["sub"] == str(user.id)
    assert [entry["after"] for entry in audit] == [{"provider": "local"}]


@pytest.mark.regression
def test_a_right_password_clears_the_count_only_without_two_step_sign_in():
    """The count it shares with the code step stays until the code step."""
    plain, enrolled = _user(email="plain@example.com"), _user(email="tfa@example.com")
    for user, enabled, left in ((plain, False, 0), (enrolled, True, 9)):
        for _ in range(9):
            login_attempt_tracker.record_failure(_key(user.email))
        response = _client(_db(user, enabled=enabled)).post(
            LOGIN, json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        assert login_attempt_tracker.status(_key(user.email)).failures == left
        app.dependency_overrides.clear()


def test_authenticate_keeps_the_count_when_asked_to():
    user = _user()
    tracker = LoginAttemptTracker(10, 900)
    service = LocalAuthService(tracker)
    for reset, left in ((False, 3), (True, 0)):
        for _ in range(3 - tracker.status(_key(user.email)).failures):
            tracker.record_failure(_key(user.email))
        assert service.authenticate(_db(user), user.email, PASSWORD, reset=reset) is (
            user
        )
        assert tracker.status(_key(user.email)).failures == left


def test_reset_failures_clears_the_key_authenticate_counts_on(monkeypatch):
    user = _user()
    tracker = LoginAttemptTracker(10, 900)
    service = LocalAuthService(tracker)
    tracker.record_failure(_key(user.email))
    service.reset_failures(_db(user), user)
    assert tracker.status(_key(user.email)).failures == 0
    # No count is kept while a demo account is published: nothing to clear.
    tracker.record_failure(_key(user.email))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", "demo@example.com")
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", "Demo-Password-1")
    assert settings.demo_account_published
    service.reset_failures(_db(user), user)
    assert tracker.status(_key(user.email)).failures == 1


@pytest.mark.regression
def test_a_failed_two_step_lookup_is_a_500_never_a_token(monkeypatch):
    """The ``user_totp`` table missing (or any lookup failing) never signs in
    with the password alone."""
    user = _user()

    def missing_table(db, user_id):
        raise ProgrammingError(
            "SELECT user_totp", {}, Exception('relation "user_totp" does not exist')
        )

    monkeypatch.setattr(two_factor_service, "is_enabled", missing_table)
    response = _client(_db(user)).post(
        LOGIN, json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 500
    assert "access_token" not in response.text


# ---------------------------------------------------------------------------
# The code step: POST /auth/mfa/verify
# ---------------------------------------------------------------------------


def _verify(client, token, **fields):
    return client.post(VERIFY, json={"mfa_token": token, **fields})


def _bad_tokens(user):
    from datetime import datetime, timedelta, timezone

    long_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
    return {
        "expired": create_mfa_token(user, now=long_ago),
        "access token": create_local_access_token(user),
        "other purpose": create_mfa_token(user, purpose="enrol"),
        "other key": jwt.encode(
            {"sub": str(user.id), "iss": "controlvar-local-mfa", "purpose": "verify"},
            "a-key-that-is-not-the-mfa-token-key",
            algorithm="HS256",
        ),
        "garbage": "garbage",
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "kind", ["expired", "access token", "other purpose", "other key", "garbage"]
)
def test_a_bad_mfa_token_is_refused_before_any_lookup(kind, monkeypatch):
    user = _user()
    db = _db(user)
    called = []
    monkeypatch.setattr(
        two_factor_service, "verify_sign_in", lambda *a, **k: called.append(1)
    )
    response = _verify(_client(db), _bad_tokens(user)[kind], code="123456")
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN_EXPIRED}
    assert response.headers["www-authenticate"] == "Bearer"
    db.query.assert_not_called()
    assert called == []


@pytest.mark.parametrize("state", ["gone", "deactivated"])
def test_an_account_that_can_no_longer_sign_in_starts_again(state, monkeypatch):
    user = _user(is_active=state != "deactivated")
    db = _db(None if state == "gone" else user)
    called = []
    monkeypatch.setattr(
        two_factor_service, "verify_sign_in", lambda *a, **k: called.append(1)
    )
    response = _verify(_client(db), create_mfa_token(user), code="123456")
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN_EXPIRED}
    assert called == []


def test_demo_mode_refuses_the_code_step_and_every_two_step_write(monkeypatch):
    """Demo mode allows eight POSTs, none of these (core/demo_mode.py); and
    the code step would refuse any account but a non-superuser VIEWER."""
    from backend.app.services.local_auth_service import account_may_sign_in

    monkeypatch.setattr(settings, "DEMO_MODE", True)
    user = _user()
    client = _client(_db(user), signed_in=user, superuser=user)
    for method, path, body in ROUTES:
        if method == "GET":
            continue
        response = client.request(method, path, json=body)
        assert response.status_code == 403, (path, response.text)
    assert account_may_sign_in(_user(role=UserRole.DEVELOPER)) is False
    assert account_may_sign_in(_user(role=UserRole.VIEWER)) is True


@pytest.mark.parametrize(
    "fields",
    [
        {"code": "1" * 65},
        {"recovery_code": "A" * 65},
        {"code": "123456", "recovery_code": "ABCDEFGHJK"},
        {},
        {"code": None, "recovery_code": None},
    ],
    ids=["code-65", "recovery-65", "both", "neither", "both-null"],
)
def test_input_that_is_not_a_code_answers_422_before_the_service(fields, monkeypatch):
    user = _user()
    called = []
    monkeypatch.setattr(
        two_factor_service, "verify_sign_in", lambda *a, **k: called.append(1)
    )
    response = _verify(_client(_db(user)), create_mfa_token(user), **fields)
    assert response.status_code == 422, response.text
    assert called == []


def _raising(exc):
    def verify_sign_in(*args, **kwargs):
        raise exc

    return verify_sign_in


VERIFY_REFUSALS = {
    "wrong code": (WrongCodeError(), 401, WRONG_CODE_TEXT, None),
    "locked": (AccountLockedError(42), 423, None, "42"),
    "capped": (CodeStepLockedError(), 423, CODE_STEP_LOCKED_TEXT, None),
    "unreadable secret": (TwoFactorUnavailableError(), 503, UNAVAILABLE_TEXT, None),
    "turned off since": (NotEnabledError(), 401, SIGN_IN_EXPIRED, None),
}


@pytest.mark.parametrize("kind", sorted(VERIFY_REFUSALS))
def test_each_refusal_of_the_code_step(kind, monkeypatch, audit):
    exc, status, detail, retry_after = VERIFY_REFUSALS[kind]
    user = _user()
    monkeypatch.setattr(two_factor_service, "verify_sign_in", _raising(exc))
    response = _verify(_client(_db(user)), create_mfa_token(user), code="123456")
    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json() == {"detail": detail}
    else:
        assert response.json()["detail"].startswith(LOCKED_PREFIX)
    assert response.headers.get("retry-after") == retry_after
    assert "access_token" not in response.text
    assert audit == []


@pytest.mark.parametrize("method", ["totp", "recovery"])
def test_the_code_step_signs_in_and_records_the_second_step(method, monkeypatch, audit):
    user = _user()
    seen = {}

    def verify_sign_in(db, account, *, code, recovery_code, client_host):
        seen.update(account=account, code=code, recovery_code=recovery_code)
        return CheckResult(method=method)

    monkeypatch.setattr(two_factor_service, "verify_sign_in", verify_sign_in)
    fields = {"code": "123 456"} if method == "totp" else {"recovery_code": "abcd-efgh"}
    response = _verify(_client(_db(user)), create_mfa_token(user), **fields)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    assert decode_local_token(body["access_token"])["sub"] == str(user.id)
    assert seen["account"] is user
    # Passed on as typed: the service normalises.
    assert (seen["code"], seen["recovery_code"]) == (
        (fields.get("code"), fields.get("recovery_code"))
    )
    assert [entry["after"] for entry in audit] == [
        {"provider": "local", "second_step": method}
    ]


def test_the_role_in_the_token_is_read_at_the_code_step(monkeypatch, audit):
    user = _user(role=UserRole.DEVELOPER)
    token = create_mfa_token(user)
    user.role = UserRole.VIEWER
    monkeypatch.setattr(
        two_factor_service, "verify_sign_in", lambda *a, **k: CheckResult("totp")
    )
    response = _verify(_client(_db(user)), token, code="123456")
    assert response.status_code == 200, response.text
    assert decode_local_token(response.json()["access_token"])["role"] == "VIEWER"


# ---------------------------------------------------------------------------
# Your own two-step sign-in: /users/me/2fa
# ---------------------------------------------------------------------------


def test_reading_your_own_status(monkeypatch):
    user = _user()
    monkeypatch.setattr(
        two_factor_service,
        "status",
        lambda db, account: TwoFactorStatus(enabled=True, recovery_codes_remaining=7),
    )
    response = _client(_db(user), signed_in=user).get(ME_2FA)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": True,
        "required": False,
        "recovery_codes_remaining": 7,
    }


def test_the_answers_that_succeed(monkeypatch):
    user = _user()
    monkeypatch.setattr(
        two_factor_service,
        "enrol",
        lambda db, account: Enrolment(secret="ABC", otpauth_uri="otpauth://totp/x"),
    )
    monkeypatch.setattr(
        two_factor_service,
        "confirm",
        lambda *a, **k: ["CODE%06d" % i for i in range(10)],
    )
    monkeypatch.setattr(two_factor_service, "disable", lambda *a, **k: None)
    monkeypatch.setattr(
        two_factor_service,
        "regenerate_recovery_codes",
        lambda *a, **k: ["NEW%07d" % i for i in range(10)],
    )
    client = _client(_db(user), signed_in=user)

    enrol = client.post(f"{ME_2FA}/enroll", json={"current_password": PASSWORD})
    confirm = client.post(f"{ME_2FA}/confirm", json={"code": "123456"})
    regenerate = client.post(f"{ME_2FA}/recovery-codes", json={"code": "123456"})
    disable = client.post(f"{ME_2FA}/disable", json={"recovery_code": "ABCDEFGHJK"})

    assert enrol.json() == {"secret": "ABC", "otpauth_uri": "otpauth://totp/x"}
    assert len(confirm.json()["recovery_codes"]) == 10
    assert len(regenerate.json()["recovery_codes"]) == 10
    assert (disable.status_code, disable.content) == (204, b"")


SIGNED_IN = {
    "confirm": ("confirm", f"{ME_2FA}/confirm", {"code": "1"}),
    "disable": ("disable", f"{ME_2FA}/disable", {"code": "1"}),
    "recovery-codes": (
        "regenerate_recovery_codes",
        f"{ME_2FA}/recovery-codes",
        {"code": "1"},
    ),
}

SIGNED_IN_REFUSALS = {
    "wrong code": (WrongCodeError(), 403, WRONG_CODE_TEXT),
    "locked": (AccountLockedError(42), 423, None),
    "capped": (CodeStepLockedError(), 423, CODE_STEP_LOCKED_TEXT),
    "unreadable secret": (TwoFactorUnavailableError(), 503, UNAVAILABLE_TEXT),
}


@pytest.mark.regression
@pytest.mark.parametrize("route", sorted(SIGNED_IN))
@pytest.mark.parametrize("kind", sorted(SIGNED_IN_REFUSALS))
def test_a_signed_in_route_refuses_a_wrong_code_with_403(route, kind, monkeypatch):
    """403, not 401: a 401 there would read as a signed-out session."""
    method, path, body = SIGNED_IN[route]
    exc, status, detail = SIGNED_IN_REFUSALS[kind]
    user = _user()
    monkeypatch.setattr(two_factor_service, method, _raising(exc))
    response = _client(_db(user), signed_in=user).post(path, json=body)
    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json() == {"detail": detail}
    else:
        assert response.json()["detail"].startswith(LOCKED_PREFIX)
        assert response.headers["retry-after"] == "42"


@pytest.mark.parametrize(
    "method, path, body, exc, detail",
    [
        (
            "enrol",
            f"{ME_2FA}/enroll",
            {"current_password": PASSWORD},
            AlreadyEnabledError(),
            answers.ALREADY_ENABLED_DETAIL,
        ),
        (
            "confirm",
            f"{ME_2FA}/confirm",
            {"code": "1"},
            NotEnabledError(),
            answers.NO_PENDING_SECRET_DETAIL,
        ),
        (
            "disable",
            f"{ME_2FA}/disable",
            {"code": "1"},
            NotEnabledError(),
            answers.NOT_ENABLED_DETAIL,
        ),
        (
            "regenerate_recovery_codes",
            f"{ME_2FA}/recovery-codes",
            {"code": "1"},
            NotEnabledError(),
            answers.NOT_ENABLED_DETAIL,
        ),
    ],
    ids=["enroll-once-on", "confirm-nothing-waiting", "disable-off", "codes-off"],
)
def test_the_wrong_state_answers_409(method, path, body, exc, detail, monkeypatch):
    user = _user()
    monkeypatch.setattr(two_factor_service, method, _raising(exc))
    response = _client(_db(user), signed_in=user).post(path, json=body)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": detail}


def test_disable_takes_exactly_one_of_code_and_recovery_code(monkeypatch):
    user = _user()
    called = []
    monkeypatch.setattr(two_factor_service, "disable", lambda *a, **k: called.append(1))
    client = _client(_db(user), signed_in=user)
    for body in ({}, {"code": "1", "recovery_code": "A"}, {"code": "1" * 65}):
        assert client.post(f"{ME_2FA}/disable", json=body).status_code == 422
    assert called == []


# ---------------------------------------------------------------------------
# Setting it up asks for the current password: POST /users/me/2fa/enroll
# ---------------------------------------------------------------------------

ENROL = f"{ME_2FA}/enroll"
WRONG_PASSWORD = "Not-The-Password-1812"
#: The password change's 423, which setting up two-step sign-in answers too.
PASSWORD_LOCKED_PREFIX = "Too many failed attempts; account temporarily locked."
MISSING = users_endpoints.TWO_FACTOR_PASSWORD_MISSING_DETAIL
NO_PASSWORD = users_endpoints.TWO_FACTOR_NO_PASSWORD_DETAIL
INCORRECT = users_endpoints.CURRENT_PASSWORD_INCORRECT_DETAIL


@pytest.fixture
def enrolments(monkeypatch):
    """The accounts each call that reached ``two_factor_service.enrol`` was for."""
    reached = []

    def enrol(db, account):
        reached.append(account)
        return Enrolment(secret="ABC", otpauth_uri="otpauth://totp/x")

    monkeypatch.setattr(two_factor_service, "enrol", enrol)
    return reached


def _failures(user) -> int:
    return login_attempt_tracker.status(_key(user.email)).failures


@pytest.mark.regression
@pytest.mark.parametrize(
    "locked, hashed, body, status, detail, counted",
    [
        (True, None, None, 423, None, 0),
        (True, PASSWORD_HASH, {"current_password": PASSWORD}, 423, None, 0),
        (False, None, None, 403, NO_PASSWORD, 0),
        (False, None, {"current_password": WRONG_PASSWORD}, 403, NO_PASSWORD, 0),
        (False, PASSWORD_HASH, None, 403, MISSING, 0),
        (False, PASSWORD_HASH, {}, 403, MISSING, 0),
        (False, PASSWORD_HASH, {"current_password": None}, 403, MISSING, 0),
        (False, PASSWORD_HASH, {"current_password": ""}, 403, MISSING, 0),
        (False, PASSWORD_HASH, {"current_password": WRONG_PASSWORD}, 403, INCORRECT, 1),
    ],
    ids=[
        "locked-before-no-password",
        "locked-even-with-the-right-password",
        "no-password-before-missing",
        "no-password-not-counted",
        "no-body",
        "empty-body",
        "null",
        "empty",
        "wrong-counted",
    ],
)
def test_setting_it_up_checks_the_current_password_first_in_this_order(
    locked, hashed, body, status, detail, counted, enrolments
):
    """Locked (423), no password of its own, none sent, then wrong (counted):
    each refused before any secret is made, and never 422."""
    user = _user(hashed_password=hashed)
    if locked:
        for _ in range(login_attempt_tracker.max_attempts):
            login_attempt_tracker.record_failure(_key(user.email))
    before = _failures(user)

    response = _client(_db(user), signed_in=user).post(ENROL, json=body)

    assert response.status_code == status, response.text
    if detail is None:
        assert response.json()["detail"].startswith(PASSWORD_LOCKED_PREFIX)
        assert int(response.headers["retry-after"]) > 0
    else:
        assert response.json() == {"detail": detail}
    assert _failures(user) - before == counted
    assert enrolments == []


@pytest.mark.regression
def test_the_right_password_reaches_the_secret_and_clears_the_count(enrolments):
    user = _user()
    for _ in range(3):
        login_attempt_tracker.record_failure(_key(user.email))
    response = _client(_db(user), signed_in=user).post(
        ENROL, json={"current_password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": "ABC", "otpauth_uri": "otpauth://totp/x"}
    assert enrolments == [user]
    assert _failures(user) == 0


def test_the_password_comes_before_the_409_of_an_account_already_on(monkeypatch):
    user = _user()
    monkeypatch.setattr(two_factor_service, "enrol", _raising(AlreadyEnabledError()))
    client = _client(_db(user), signed_in=user)
    assert client.post(ENROL, json={"current_password": WRONG_PASSWORD}).json() == {
        "detail": INCORRECT
    }
    right = client.post(ENROL, json={"current_password": PASSWORD})
    assert right.status_code == 409
    assert right.json() == {"detail": answers.ALREADY_ENABLED_DETAIL}


@pytest.mark.regression
@pytest.mark.parametrize(
    "body",
    [None, {}, {"current_password": 12345}],
    ids=["no-body", "empty-body", "schema-invalid-body"],
)
def test_under_cognito_enroll_answers_the_local_only_404_whatever_its_body(
    body, monkeypatch
):
    """V13 for the route's new body, #516's shape: no token, and the provider
    check answers before the body is validated."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    response = TestClient(app, raise_server_exceptions=False).post(ENROL, json=body)
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": LOCAL_ONLY}


# ---------------------------------------------------------------------------
# A superuser's reset: DELETE /admin/users/{user_id}/2fa
# ---------------------------------------------------------------------------


def test_a_superuser_resets_someone_else(monkeypatch):
    admin, target = _user(is_superuser=True, role=UserRole.ADMIN), _user()
    seen = {}

    def reset(db, users, actor):
        seen.update(users=list(users), actor=actor)
        return 1

    monkeypatch.setattr(two_factor_service, "reset", reset)
    db = _db(target)
    response = _client(db, superuser=admin).delete(
        f"/api/v1/admin/users/{target.id}/2fa"
    )
    assert (response.status_code, response.content) == (204, b"")
    assert seen == {"users": [target], "actor": admin}
    db.commit.assert_called_once()


@pytest.mark.regression
def test_a_superuser_cannot_reset_their_own(monkeypatch):
    admin = _user(is_superuser=True, role=UserRole.ADMIN)
    called = []
    monkeypatch.setattr(two_factor_service, "reset", lambda *a, **k: called.append(1))
    response = _client(_db(admin), superuser=admin).delete(
        f"/api/v1/admin/users/{admin.id}/2fa"
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": "You can't reset your own two-step sign-in. "
        "Ask another administrator to do it."
    }
    assert called == []


def test_resetting_an_unknown_account_answers_404(monkeypatch):
    admin = _user(is_superuser=True, role=UserRole.ADMIN)
    monkeypatch.setattr(two_factor_service, "reset", lambda *a, **k: 0)
    response = _client(_db(None), superuser=admin).delete(
        f"/api/v1/admin/users/{uuid.uuid4()}/2fa"
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


# ---------------------------------------------------------------------------
# A role that requires it: LOCAL_AUTH_2FA_REQUIRED_ROLES
# ---------------------------------------------------------------------------


@pytest.fixture
def require(monkeypatch):
    """Set the roles that must use two-step sign-in, as the setting would."""
    from backend.app.core.settings_rules import parse_two_factor_roles

    def set_roles(value):
        monkeypatch.setattr(
            settings, "LOCAL_AUTH_2FA_REQUIRED_ROLES", parse_two_factor_roles(value)
        )

    return set_roles


def _enrol_token(user, **kwargs):
    from backend.app.core.mfa_token import ENROL_PURPOSE, ENROL_TTL_SECONDS

    return create_mfa_token(
        user, purpose=ENROL_PURPOSE, ttl_seconds=ENROL_TTL_SECONDS, **kwargs
    )


@pytest.mark.regression
@pytest.mark.parametrize("path", [LOGIN, TOKEN])
def test_a_required_role_without_it_answers_enrol_and_no_access_token(
    path, require, audit
):
    """The required-not-enrolled password step (V5's PR 4 row)."""
    from backend.app.core.mfa_token import (
        ENROL_PURPOSE,
        InvalidMfaToken,
        decode_mfa_token,
    )

    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    client = _client(_db(user, enabled=False))
    if path == LOGIN:
        response = client.post(path, json={"email": user.email, "password": PASSWORD})
    else:
        response = client.post(
            path, data={"username": user.email, "password": PASSWORD}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"mfa_required", "mfa_token", "enrol", "expires_in"}
    assert (body["mfa_required"], body["enrol"], body["expires_in"]) == (
        True,
        True,
        600,
    )
    assert "access_token" not in body
    assert decode_mfa_token(body["mfa_token"], purpose=ENROL_PURPOSE) == user.id
    # The token's own lifetime, not only the number the answer reports. The
    # literal 600 is deliberate: a changed constant fails here too.
    claims = jwt.decode(body["mfa_token"], options={"verify_signature": False})
    assert claims["purpose"] == "enrol"
    assert claims["exp"] - claims["iat"] == 600
    # Never a code-step token, never a session.
    with pytest.raises(InvalidMfaToken):
        decode_mfa_token(body["mfa_token"])
    with pytest.raises(Exception):
        decode_local_token(body["mfa_token"])
    assert audit == []


@pytest.mark.regression
def test_the_password_alone_does_not_clear_the_count_for_a_required_role(require):
    """Only the first code that turns it on clears it, as only an accepted
    code does for an account that has it on."""
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER, email="required@example.com")
    for _ in range(9):
        login_attempt_tracker.record_failure(_key(user.email))
    response = _client(_db(user)).post(
        LOGIN, json={"email": user.email, "password": PASSWORD}
    )
    assert response.json()["enrol"] is True
    assert login_attempt_tracker.status(_key(user.email)).failures == 9


def test_a_required_role_with_it_on_takes_the_code_step(require):
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    body = (
        _client(_db(user, enabled=True))
        .post(LOGIN, json={"email": user.email, "password": PASSWORD})
        .json()
    )
    assert (body["enrol"], body["expires_in"]) == (False, 300)


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.ANALYST, UserRole.VIEWER])
def test_a_role_not_listed_signs_in_with_the_password_alone(role, require, audit):
    require("DEVELOPER")
    user = _user(role=role)
    body = (
        _client(_db(user))
        .post(LOGIN, json={"email": user.email, "password": PASSWORD})
        .json()
    )
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}


@pytest.mark.regression
def test_a_failed_lookup_for_a_required_role_is_a_500_never_a_token(
    require, monkeypatch
):
    """V19's PR 4 row: the ``user_totp`` table missing never signs a required
    role in with the password alone."""
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)

    def missing_table(db, user_id):
        raise ProgrammingError(
            "SELECT user_totp", {}, Exception('relation "user_totp" does not exist')
        )

    monkeypatch.setattr(two_factor_service, "is_enabled", missing_table)
    response = _client(_db(user)).post(
        LOGIN, json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 500
    assert "access_token" not in response.text
    assert "mfa_token" not in response.text


def _enrol_bad_tokens(user):
    from datetime import datetime, timedelta, timezone

    long_ago = datetime.now(timezone.utc) - timedelta(minutes=11)
    return {
        "expired": _enrol_token(user, now=long_ago),
        "access token": create_local_access_token(user),
        "verify purpose": create_mfa_token(user),
        "other key": jwt.encode(
            {"sub": str(user.id), "iss": "controlvar-local-mfa", "purpose": "enrol"},
            "a-key-that-is-not-the-mfa-token-key",
            algorithm="HS256",
        ),
        "garbage": "garbage",
    }


@pytest.mark.regression
@pytest.mark.parametrize("route", [AUTH_ENROL, ENROL_CONFIRM])
@pytest.mark.parametrize(
    "kind", ["expired", "access token", "verify purpose", "other key", "garbage"]
)
def test_the_enrol_routes_take_only_an_enrol_token(route, kind, require, monkeypatch):
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    db = _db(user)
    called = []
    monkeypatch.setattr(two_factor_service, "enrol", lambda *a, **k: called.append(1))
    monkeypatch.setattr(two_factor_service, "confirm", lambda *a, **k: called.append(1))
    body = {"mfa_token": _enrol_bad_tokens(user)[kind]}
    if route == ENROL_CONFIRM:
        body["code"] = "123456"
    response = _client(db).post(route, json=body)
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": SIGN_IN_EXPIRED}
    assert response.headers["www-authenticate"] == "Bearer"
    db.query.assert_not_called()
    assert called == []


@pytest.mark.regression
def test_an_enrol_token_is_refused_at_the_code_step(require, monkeypatch):
    """Each route takes only its own purpose: an enrol token is no way past
    an account's code step."""
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    called = []
    monkeypatch.setattr(
        two_factor_service, "verify_sign_in", lambda *a, **k: called.append(1)
    )
    response = _verify(_client(_db(user)), _enrol_token(user), code="123456")
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN_EXPIRED}
    assert called == []


@pytest.mark.regression
@pytest.mark.parametrize("route", [AUTH_ENROL, ENROL_CONFIRM])
@pytest.mark.parametrize(
    "state", ["gone", "deactivated", "role changed", "policy removed", "already on"]
)
def test_the_account_is_read_again_at_each_enrol_route(
    route, state, require, monkeypatch
):
    """Re-evaluated from the database: an account that can no longer sign in,
    whose role no longer requires it, or that turned it on elsewhere since the
    password step, starts again."""
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    token = _enrol_token(user)
    if state == "deactivated":
        user.is_active = False
    elif state == "role changed":
        user.role = UserRole.VIEWER
    elif state == "policy removed":
        require("")
    db = _db(None if state == "gone" else user, enabled=state == "already on")
    called = []
    monkeypatch.setattr(two_factor_service, "enrol", lambda *a, **k: called.append(1))
    monkeypatch.setattr(two_factor_service, "confirm", lambda *a, **k: called.append(1))
    body = {"mfa_token": token}
    if route == ENROL_CONFIRM:
        body["code"] = "123456"
    response = _client(db).post(route, json=body)
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": SIGN_IN_EXPIRED}
    assert called == []


def test_enrol_during_sign_in_answers_a_new_secret(require, monkeypatch):
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    seen = []

    def enrol(db, account):
        seen.append(account)
        return Enrolment(secret="ABC", otpauth_uri="otpauth://totp/x")

    monkeypatch.setattr(two_factor_service, "enrol", enrol)
    response = _client(_db(user)).post(
        AUTH_ENROL, json={"mfa_token": _enrol_token(user)}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"secret": "ABC", "otpauth_uri": "otpauth://totp/x"}
    assert seen == [user]


def test_enrol_turned_on_meanwhile_starts_again(require, monkeypatch):
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    monkeypatch.setattr(two_factor_service, "enrol", _raising(AlreadyEnabledError()))
    response = _client(_db(user)).post(
        AUTH_ENROL, json={"mfa_token": _enrol_token(user)}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN_EXPIRED}


@pytest.mark.regression
def test_the_first_code_turns_it_on_and_signs_in(require, monkeypatch, audit):
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER, email="first@example.com")
    for _ in range(9):
        login_attempt_tracker.record_failure(_key(user.email))
    seen = {}

    def confirm(db, account, code, client_host=None):
        seen.update(account=account, code=code)
        return ["CODE%06d" % i for i in range(10)]

    monkeypatch.setattr(two_factor_service, "confirm", confirm)
    response = _client(_db(user)).post(
        ENROL_CONFIRM, json={"mfa_token": _enrol_token(user), "code": "123 456"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"recovery_codes", "login"}
    assert len(body["recovery_codes"]) == 10
    assert set(body["login"]) == {"access_token", "token_type", "expires_in", "user"}
    assert decode_local_token(body["login"]["access_token"])["sub"] == str(user.id)
    assert seen == {"account": user, "code": "123 456"}
    # The first code is the sign-in's second step: the count is cleared.
    assert login_attempt_tracker.status(_key(user.email)).failures == 0
    assert [entry["after"] for entry in audit] == [
        {"provider": "local", "second_step": "totp"}
    ]


ENROL_CONFIRM_REFUSALS = {
    "wrong code": (WrongCodeError(), 401, WRONG_CODE_TEXT, None),
    "locked": (AccountLockedError(42), 423, None, "42"),
    "capped": (CodeStepLockedError(), 423, CODE_STEP_LOCKED_TEXT, None),
    "unreadable secret": (TwoFactorUnavailableError(), 503, UNAVAILABLE_TEXT, None),
    "nothing waiting": (
        NotEnabledError(),
        409,
        answers.NO_PENDING_SIGN_IN_SECRET_DETAIL,
        None,
    ),
}


@pytest.mark.parametrize("kind", sorted(ENROL_CONFIRM_REFUSALS))
def test_each_refusal_of_the_first_code(kind, require, monkeypatch, audit):
    require("DEVELOPER")
    exc, status, detail, retry_after = ENROL_CONFIRM_REFUSALS[kind]
    user = _user(role=UserRole.DEVELOPER, email="refused@example.com")
    for _ in range(3):
        login_attempt_tracker.record_failure(_key(user.email))
    monkeypatch.setattr(two_factor_service, "confirm", _raising(exc))
    response = _client(_db(user)).post(
        ENROL_CONFIRM, json={"mfa_token": _enrol_token(user), "code": "123456"}
    )
    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json() == {"detail": detail}
    else:
        assert response.json()["detail"].startswith(LOCKED_PREFIX)
    assert response.headers.get("retry-after") == retry_after
    assert "access_token" not in response.text
    assert audit == []
    # Nothing cleared the count.
    assert login_attempt_tracker.status(_key(user.email)).failures == 3


@pytest.mark.parametrize(
    "body",
    [{"code": "1" * 65}, {}, {"code": None}],
    ids=["code-65", "no-code", "null-code"],
)
def test_the_first_code_that_is_not_a_code_answers_422(body, require, monkeypatch):
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    called = []
    monkeypatch.setattr(two_factor_service, "confirm", lambda *a, **k: called.append(1))
    response = _client(_db(user)).post(
        ENROL_CONFIRM, json={"mfa_token": _enrol_token(user), **body}
    )
    assert response.status_code == 422, response.text
    assert called == []


@pytest.mark.regression
def test_a_required_role_cannot_turn_it_off(require, monkeypatch):
    """403 before any code is checked or counted."""
    require("DEVELOPER")
    user = _user(role=UserRole.DEVELOPER)
    called = []
    monkeypatch.setattr(two_factor_service, "disable", lambda *a, **k: called.append(1))
    client = _client(_db(user), signed_in=user)
    for body in ({"code": "123456"}, {"recovery_code": "ABCDEFGHJK"}):
        response = client.post(f"{ME_2FA}/disable", json=body)
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": answers.TWO_FACTOR_REQUIRED_FOR_ROLE}
    assert called == []


def test_a_role_not_listed_still_turns_it_off(require, monkeypatch):
    require("ADMIN")
    user = _user(role=UserRole.DEVELOPER)
    called = []
    monkeypatch.setattr(two_factor_service, "disable", lambda *a, **k: called.append(1))
    response = _client(_db(user), signed_in=user).post(
        f"{ME_2FA}/disable", json={"code": "123456"}
    )
    assert response.status_code == 204
    assert called == [1]


@pytest.mark.regression
@pytest.mark.parametrize(
    "roles, role, password, required",
    [
        ("", UserRole.ADMIN, PASSWORD_HASH, False),
        ("ADMIN", UserRole.ADMIN, PASSWORD_HASH, True),
        ("ADMIN", UserRole.VIEWER, PASSWORD_HASH, False),
        ("ADMIN", UserRole.ADMIN, None, False),
        ("", UserRole.ADMIN, None, False),
    ],
    ids=[
        "no-policy",
        "listed",
        "not-listed",
        "no-password-listed",
        "no-password-no-policy",
    ],
)
def test_your_status_says_whether_your_role_requires_it(
    roles, role, password, required, require, monkeypatch
):
    """PE-S2-6: an account with no password of its own reads ``required:
    false`` whether or not its role is listed: the requirement applies where
    the password is checked."""
    require(roles)
    user = _user(role=role, hashed_password=password)
    monkeypatch.setattr(
        two_factor_service,
        "status",
        lambda db, account: TwoFactorStatus(enabled=False, recovery_codes_remaining=0),
    )
    response = _client(_db(user), signed_in=user).get(ME_2FA)
    assert response.status_code == 200, response.text
    assert response.json()["required"] is required


# ---------------------------------------------------------------------------
# Every route: local provider only
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("method, path, body", ROUTES, ids=ROUTE_IDS)
def test_under_cognito_every_route_answers_the_local_only_404(
    method, path, body, monkeypatch
):
    """Before authentication and before the body is read."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    response = TestClient(app, raise_server_exceptions=False).request(
        method, path, json=body
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": LOCAL_ONLY}


def test_the_nine_routes_are_all_the_routes():
    assert len(ROUTES) == len(ROUTE_IDS) == 9


# ---------------------------------------------------------------------------
# The mapping itself
# ---------------------------------------------------------------------------


def test_the_mapping_raises_anything_that_is_not_a_refusal():
    with pytest.raises(KeyError):
        answers.refusal_answer(KeyError("x"), signed_in=True)


def test_a_wrong_code_reads_the_same_on_both_kinds_of_route():
    signed_in = answers.refusal_answer(WrongCodeError(), signed_in=True)
    signing_in = answers.refusal_answer(WrongCodeError(), signed_in=False)
    assert isinstance(signed_in, HTTPException)
    assert (signed_in.status_code, signing_in.status_code) == (403, 401)
    assert signed_in.detail == signing_in.detail == WRONG_CODE_TEXT
    assert signed_in.headers is None
    assert signing_in.headers == {"WWW-Authenticate": "Bearer"}


def test_the_cap_has_no_retry_after_because_waiting_does_not_end_it():
    capped = answers.refusal_answer(CodeStepLockedError(), signed_in=False)
    assert (capped.status_code, capped.detail, capped.headers) == (
        423,
        CODE_STEP_LOCKED_TEXT,
        None,
    )


def test_users_and_auth_share_the_service_texts():
    assert users_endpoints.WRONG_CODE_TEXT is service_module.WRONG_CODE_TEXT
    assert auth_endpoints.UNAVAILABLE_TEXT is service_module.UNAVAILABLE_TEXT

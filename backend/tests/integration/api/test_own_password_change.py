"""Changing your own password requires the current password (#344).

``POST /api/v1/users/me/password`` is the only way to change your own
password: it asks for the current one, applies the password rule, and counts
a wrong current password toward the same lockout as sign-in. ``PUT
/api/v1/users/{id}`` and ``PUT /api/v1/admin/users/{id}`` refuse the caller's
own password for every caller, superusers included, and a superuser's reset of
another account now takes effect on both.

Real ``users`` rows with real bcrypt hashes and real local JWTs from
``create_local_access_token``; the only dependency override is
``deps.get_db``, so every request goes through the real authentication, the
real validation and the real lockout counter. The stored hash is read back
with a column query, never from a response.
"""

from __future__ import annotations

import base64
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.users import (
    CURRENT_PASSWORD_INCORRECT_DETAIL,
    CURRENT_PASSWORD_MISSING_DETAIL,
    NO_LOCAL_PASSWORD_DETAIL,
    OWN_PASSWORD_DETAIL,
)
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services.local_auth_service import login_attempt_tracker

pytestmark = [pytest.mark.integration, pytest.mark.regression]

ME_PASSWORD = "/api/v1/users/me/password"
USERS = "/api/v1/users"
ADMIN_USERS = "/api/v1/admin/users"
LOGIN = "/api/v1/auth/login"
AUTH_ME = "/api/v1/auth/me"

#: Every account starts with this password. One hash, computed once: bcrypt
#: at 12 rounds is a quarter of a second a call.
OLD = "Old-Passw0rd-344"
OLD_HASH = get_password_hash(OLD)
#: A valid new password that appears nowhere else.
NEW = "New-Passw0rd-344x"

#: 73 bytes: one over bcrypt's limit.
SEVENTY_THREE_BYTES = "A1" + "a" * 71
#: 72 bytes exactly: accepted.
SEVENTY_TWO_BYTES = "A1" + "a" * 70

WEAK = {
    "empty": "",
    "short": "Aa1",
    "seven": "Abcdef1",
    "no_upper": "lowercase123",
    "no_lower": "UPPERCASE123",
    "no_digit": "NoDigitsHere",
    "73_bytes": SEVENTY_THREE_BYTES,
    "73_bytes_multibyte": "A1" + "é" * 35 + "a",
    "lone_surrogate": "Abcdefg1\ud800",
}

ROLES = {
    "viewer": (UserRole.VIEWER, False),
    "analyst": (UserRole.ANALYST, False),
    "developer": (UserRole.DEVELOPER, False),
    "admin_role": (UserRole.ADMIN, False),
    "superuser": (UserRole.ADMIN, True),
}


def test_the_weak_corpus_is_what_its_names_say():
    assert len(SEVENTY_THREE_BYTES.encode()) == 73
    assert len(SEVENTY_TWO_BYTES.encode()) == 72
    assert len(WEAK["73_bytes_multibyte"].encode()) == 73
    assert len(WEAK["73_bytes_multibyte"]) < 72


@pytest.fixture(autouse=True)
def _local_provider(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


@pytest.fixture
def session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), autocommit=False, autoflush=False)


@pytest.fixture
def make_user(session_factory):
    """Create a committed user; ``password_hash=None`` makes one with no password."""

    def make(role=UserRole.VIEWER, is_superuser=False, password_hash=OLD_HASH):
        session = session_factory()
        session.execute(text("SET search_path TO test_experimentation"))
        suffix = uuid.uuid4().hex[:10]
        user = User(
            username=f"pw344_{suffix}",
            email=f"pw344_{suffix}@example.com",
            full_name="Password Change",
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
def stored_hash(session_factory):
    """The ``hashed_password`` column as the database holds it now."""

    def read(user):
        session = session_factory()
        try:
            session.execute(text("SET search_path TO test_experimentation"))
            return session.execute(
                text("SELECT hashed_password FROM users WHERE id = :id"),
                {"id": user.id},
            ).scalar_one()
        finally:
            session.close()

    return read


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
        session.execute(text("SET search_path TO test_experimentation"))
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


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _login(client, user, password):
    return client.post(LOGIN, json={"email": user.email, "password": password})


def _post_raw(client, path, body, headers):
    """POST ``body`` serialised with ASCII escapes, so a lone surrogate travels."""
    return client.post(
        path,
        content=json.dumps(body),
        headers={**headers, "Content-Type": "application/json"},
    )


def _put_raw(client, path, body, headers):
    return client.put(
        path,
        content=json.dumps(body),
        headers={**headers, "Content-Type": "application/json"},
    )


def _update_body(user, password):
    """What ``UserUpdate`` needs: the stored username and email, plus a password."""
    return {"username": user.username, "email": user.email, "password": password}


#: Three base64url segments, the first a JWT header (``{"`` is ``eyJ``).
_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]{1,4096}\.[A-Za-z0-9_-]{1,4096}\.[A-Za-z0-9_-]{0,4096}"
)


def _jwts(text_: str):
    return _JWT.findall(text_)


def _jwt_payload(token: str) -> str:
    """A JWT's claims as text, decoded without checking anything."""
    payload = token.split(".")[1]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")


def _assert_old_password_still_works(client, user, stored_hash):
    assert stored_hash(user) == OLD_HASH
    assert _login(client, user, OLD).status_code == 200
    login_attempt_tracker.clear()


# ---------------------------------------------------------------------------
# PUT refuses your own password, whoever you are
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["users", "admin"])
@pytest.mark.parametrize("role", list(ROLES))
def test_own_password_via_put_is_refused(client, make_user, stored_hash, role, route):
    user_role, is_superuser = ROLES[role]
    user = make_user(role=user_role, is_superuser=is_superuser)
    base = USERS if route == "users" else ADMIN_USERS

    response = client.put(
        f"{base}/{user.id}", json=_update_body(user, NEW), headers=_auth(user)
    )

    assert response.status_code == 403, response.text
    if route == "users" or is_superuser:
        # Everyone who reaches the password gets the same pointer; a
        # non-superuser on /admin/users is refused before that.
        assert response.json()["detail"] == OWN_PASSWORD_DETAIL
    _assert_old_password_still_works(client, user, stored_hash)
    assert _login(client, user, NEW).status_code == 401


def test_null_password_via_put_changes_nothing(client, make_user, stored_hash):
    user = make_user()
    response = client.put(
        f"{USERS}/{user.id}",
        json={**_update_body(user, None), "full_name": "Renamed"},
        headers=_auth(user),
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "Renamed"
    assert stored_hash(user) == OLD_HASH


# ---------------------------------------------------------------------------
# POST /me/password
# ---------------------------------------------------------------------------


def test_missing_current_password_is_403(client, make_user, stored_hash):
    user = make_user()
    response = client.post(ME_PASSWORD, json={"new_password": NEW}, headers=_auth(user))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == CURRENT_PASSWORD_MISSING_DETAIL
    _assert_old_password_still_works(client, user, stored_hash)


def test_empty_current_password_is_403(client, make_user, stored_hash):
    user = make_user()
    response = client.post(
        ME_PASSWORD,
        json={"current_password": "", "new_password": NEW},
        headers=_auth(user),
    )
    assert response.status_code == 403, response.text
    _assert_old_password_still_works(client, user, stored_hash)


def test_wrong_current_password_is_403_not_401(client, make_user, stored_hash):
    """401 would tell the dashboard the session had ended; it signs the user out."""
    user = make_user()
    response = client.post(
        ME_PASSWORD,
        json={"current_password": "Wrong-Passw0rd", "new_password": NEW},
        headers=_auth(user),
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == CURRENT_PASSWORD_INCORRECT_DETAIL
    _assert_old_password_still_works(client, user, stored_hash)
    assert _login(client, user, NEW).status_code == 401


@pytest.mark.parametrize("role", list(ROLES))
def test_correct_current_password_changes_it(client, make_user, stored_hash, role):
    user_role, is_superuser = ROLES[role]
    user = make_user(role=user_role, is_superuser=is_superuser)

    response = client.post(
        ME_PASSWORD,
        json={"current_password": OLD, "new_password": NEW},
        headers=_auth(user),
    )

    # #501: a new access token, as sign-in answers (the session that sent
    # the change has ended; test_password_change_ends_sessions.py).
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"access_token", "token_type", "expires_in", "user"}
    assert response.json()["user"]["id"] == str(user.id)
    assert stored_hash(user) not in (None, OLD_HASH)
    assert _login(client, user, NEW).status_code == 200
    assert _login(client, user, OLD).status_code == 401


def test_a_72_byte_password_is_accepted(client, make_user):
    user = make_user()
    response = client.post(
        ME_PASSWORD,
        json={"current_password": OLD, "new_password": SEVENTY_TWO_BYTES},
        headers=_auth(user),
    )
    assert response.status_code == 200, response.text
    assert _login(client, user, SEVENTY_TWO_BYTES).status_code == 200


@pytest.mark.parametrize("name", list(WEAK))
def test_a_weak_new_password_is_422(client, make_user, stored_hash, name):
    user = make_user()
    weak = WEAK[name]
    response = _post_raw(
        client,
        ME_PASSWORD,
        {"current_password": OLD, "new_password": weak},
        _auth(user),
    )
    assert response.status_code == 422, response.text
    assert ["body", "new_password"] in [e["loc"] for e in response.json()["detail"]]
    if weak:
        assert json.dumps(weak)[1:-1] not in response.text
        assert weak not in response.text
    assert OLD not in response.text
    _assert_old_password_still_works(client, user, stored_hash)


@pytest.mark.parametrize("name", list(WEAK))
def test_a_weak_password_in_a_superuser_reset_is_422(
    client, make_user, stored_hash, name
):
    """``""`` used to be a silent no-op and ``"Aa1"`` was stored."""
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    target = make_user()
    weak = WEAK[name]
    response = _put_raw(
        client, f"{USERS}/{target.id}", _update_body(target, weak), _auth(superuser)
    )
    assert response.status_code == 422, response.text
    if weak:
        assert json.dumps(weak)[1:-1] not in response.text
    assert stored_hash(target) == OLD_HASH


def test_create_user_over_72_bytes_is_422_not_500(client, make_user):
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        f"{USERS}/",
        json={
            "username": f"pw344_new_{suffix}",
            "email": f"pw344_new_{suffix}@example.com",
            "password": SEVENTY_THREE_BYTES,
        },
        headers=_auth(superuser),
    )
    assert response.status_code == 422, response.text
    assert SEVENTY_THREE_BYTES not in response.text


# ---------------------------------------------------------------------------
# An account with no password of its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"current_password": "Anything-1a", "new_password": NEW},
        {"current_password": "", "new_password": NEW},
        {"new_password": NEW},
    ],
    ids=["some_current", "empty_current", "no_current"],
)
def test_no_password_account_cannot_set_one_on_me_password(
    client, make_user, stored_hash, body
):
    user = make_user(password_hash=None)
    response = client.post(ME_PASSWORD, json=body, headers=_auth(user))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == NO_LOCAL_PASSWORD_DETAIL
    assert stored_hash(user) is None
    # Not a guess: nothing is counted toward the lockout.
    assert login_attempt_tracker.status(user.email).failures == 0


@pytest.mark.parametrize("route", ["users", "admin"])
@pytest.mark.parametrize("is_superuser", [False, True])
def test_no_password_account_cannot_set_one_via_put(
    client, make_user, stored_hash, route, is_superuser
):
    user = make_user(
        role=UserRole.ADMIN if is_superuser else UserRole.VIEWER,
        is_superuser=is_superuser,
        password_hash=None,
    )
    base = USERS if route == "users" else ADMIN_USERS
    response = client.put(
        f"{base}/{user.id}", json=_update_body(user, NEW), headers=_auth(user)
    )
    assert response.status_code == 403, response.text
    assert stored_hash(user) is None
    assert _login(client, user, NEW).status_code == 401


# ---------------------------------------------------------------------------
# Lockout: one budget with sign-in
# ---------------------------------------------------------------------------


def _wrong(client, user):
    return client.post(
        ME_PASSWORD,
        json={"current_password": "Wrong-Passw0rd", "new_password": NEW},
        headers=_auth(user),
    )


def _right(client, user):
    return client.post(
        ME_PASSWORD,
        json={"current_password": OLD, "new_password": NEW},
        headers=_auth(user),
    )


def test_repeated_wrong_current_passwords_lock_the_account(
    client, make_user, stored_hash
):
    user = make_user()
    n = login_attempt_tracker.max_attempts

    for _ in range(n):
        assert _wrong(client, user).status_code == 403

    locked = _wrong(client, user)
    assert locked.status_code == 423, locked.text
    assert int(locked.headers["Retry-After"]) > 0

    # The correct current password is refused too, while the lock holds...
    correct = _right(client, user)
    assert correct.status_code == 423, correct.text
    assert "Retry-After" in correct.headers
    assert stored_hash(user) == OLD_HASH
    # ...and so is sign-in: it is the same counter.
    assert _login(client, user, OLD).status_code == 423


def test_sign_in_failures_lock_the_password_change(client, make_user, stored_hash):
    user = make_user()
    for _ in range(login_attempt_tracker.max_attempts):
        assert _login(client, user, "Wrong-Passw0rd").status_code == 401

    response = _right(client, user)
    assert response.status_code == 423, response.text
    assert "Retry-After" in response.headers
    assert stored_hash(user) == OLD_HASH


def test_a_correct_current_password_clears_the_counter(client, make_user):
    user = make_user()
    for _ in range(login_attempt_tracker.max_attempts - 1):
        assert _wrong(client, user).status_code == 403
    assert _right(client, user).status_code == 200
    assert login_attempt_tracker.status(user.email).failures == 0


# ---------------------------------------------------------------------------
# A superuser resets someone else's password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["users", "admin"])
def test_superuser_resets_another_account(client, make_user, stored_hash, route):
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    target = make_user()
    base = USERS if route == "users" else ADMIN_USERS

    response = client.put(
        f"{base}/{target.id}", json=_update_body(target, NEW), headers=_auth(superuser)
    )

    assert response.status_code == 200, response.text
    assert stored_hash(target) not in (None, OLD_HASH)
    assert _login(client, target, NEW).status_code == 200
    assert _login(client, target, OLD).status_code == 401


@pytest.mark.parametrize("route", ["users", "admin"])
def test_a_superuser_reset_ends_the_accounts_sessions(client, make_user, route):
    """#501: every token the account held before the reset answers 401; the
    superuser's own session is another account's and keeps working, and the
    answer carries no token for the account."""
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    target = make_user()
    theirs = _auth(target)
    mine = _auth(superuser)
    assert client.get(AUTH_ME, headers=theirs).status_code == 200
    base = USERS if route == "users" else ADMIN_USERS

    response = client.put(
        f"{base}/{target.id}", json=_update_body(target, NEW), headers=mine
    )

    assert client.get(AUTH_ME, headers=theirs).status_code == 401
    assert response.status_code == 200, response.text
    assert "access_token" not in response.text
    assert client.get(AUTH_ME, headers=mine).status_code == 200


@pytest.mark.parametrize("route", ["users", "admin"])
def test_admin_role_without_superuser_cannot_reset_another(
    client, make_user, stored_hash, route
):
    admin = make_user(role=UserRole.ADMIN, is_superuser=False)
    target = make_user()
    base = USERS if route == "users" else ADMIN_USERS

    response = client.put(
        f"{base}/{target.id}", json=_update_body(target, NEW), headers=_auth(admin)
    )

    assert response.status_code == 403, response.text
    _assert_old_password_still_works(client, target, stored_hash)


# ---------------------------------------------------------------------------
# No response carries a password or a hash
# ---------------------------------------------------------------------------


def test_no_response_carries_a_password_or_a_hash(client, make_user, stored_hash):
    superuser = make_user(role=UserRole.ADMIN, is_superuser=True)
    user = make_user()
    target = make_user()
    wrong = "Wrong-Passw0rd-probe344"
    responses = [
        # 422: weak new password; missing new password (the whole body used to
        # be repeated); a body of the wrong shape.
        client.post(
            ME_PASSWORD,
            json={"current_password": OLD, "new_password": "weakprobe"},
            headers=_auth(user),
        ),
        client.post(ME_PASSWORD, json={"current_password": OLD}, headers=_auth(user)),
        client.post(ME_PASSWORD, json=[{"current_password": OLD}], headers=_auth(user)),
        # 403: wrong current password; own password via PUT.
        client.post(
            ME_PASSWORD,
            json={"current_password": wrong, "new_password": NEW},
            headers=_auth(user),
        ),
        client.put(
            f"{USERS}/{user.id}", json=_update_body(user, NEW), headers=_auth(user)
        ),
        # 200: a superuser's reset, which returns the account.
        client.put(
            f"{USERS}/{target.id}",
            json=_update_body(target, NEW),
            headers=_auth(superuser),
        ),
        client.put(
            f"{ADMIN_USERS}/{target.id}",
            json=_update_body(target, NEW + "2"),
            headers=_auth(superuser),
        ),
        # 200: the change itself, with a new access token (#501).
        client.post(
            ME_PASSWORD,
            json={"current_password": OLD, "new_password": NEW},
            headers=_auth(user),
        ),
    ]
    assert [r.status_code for r in responses] == [
        422,
        422,
        422,
        403,
        403,
        200,
        200,
        200,
    ]
    secrets = [
        OLD,
        NEW,
        NEW + "2",
        wrong,
        "weakprobe",
        OLD_HASH,
        stored_hash(user),
        stored_hash(target),
    ]
    # A token's claims are base64 inside the answer, where a substring check
    # cannot see them: every JWT in an answer is decoded and checked too.
    texts = [r.text for r in responses] + [
        _jwt_payload(token) for r in responses for token in _jwts(r.text)
    ]
    assert any('"pwv"' in t for t in texts), "the change's token was not decoded"
    for body in texts:
        for secret in secrets:
            assert secret not in body, body
        assert "hashed_password" not in body
        assert "$2b$" not in body


# ---------------------------------------------------------------------------
# Sessions: documented behaviour, pinned
# ---------------------------------------------------------------------------


def test_a_token_issued_before_the_change_is_refused(client, make_user):
    """A password change signs out the account's other sessions (#501;
    documented in docs/auth/auth-user-guide.md). The earlier token gets the
    same answer as a token that does not decode at all, byte for byte: status,
    body and ``WWW-Authenticate``. Checked before the change's own answer is
    read, so it fails on the assertion wherever the change answers."""
    user = make_user()
    earlier = _auth(user)
    change = client.post(
        ME_PASSWORD,
        json={"current_password": OLD, "new_password": NEW},
        headers=earlier,
    )

    response = client.get(AUTH_ME, headers=earlier)
    assert response.status_code == 401, response.text
    malformed = client.get(AUTH_ME, headers={"Authorization": "Bearer abc.def.ghi"})
    assert (
        response.status_code,
        response.content,
        response.headers.get("www-authenticate"),
    ) == (
        malformed.status_code,
        malformed.content,
        malformed.headers.get("www-authenticate"),
    )
    assert response.headers.get("www-authenticate") == "Bearer"
    assert change.status_code == 200, change.text


# ---------------------------------------------------------------------------
# Only the local provider has the route
# ---------------------------------------------------------------------------


def test_cognito_answers_404_before_validating_the_body(client, monkeypatch):
    """A schema-INVALID body (not malformed JSON, which fails earlier) and no
    token: the provider gate answers first."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    response = client.post(ME_PASSWORD, json={"new_password": 12345})
    assert response.status_code == 404, response.text

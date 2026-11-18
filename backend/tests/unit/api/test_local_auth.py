"""
Unit tests for the local (email + password) authentication provider.

No database: ``deps.get_current_user`` is exercised with a MagicMock session,
the token helpers are pure, and the login endpoints are driven through
``TestClient`` with ``deps.get_db`` overridden.  DB-backed coverage lives in
``backend/tests/integration/api/test_local_auth_path.py``.
"""

import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api import deps
from backend.app.core import security
from backend.app.core.config import settings
from backend.app.core.security import (
    LOCAL_TOKEN_ISSUER,
    InvalidTokenError,
    create_local_access_token,
    decode_local_token,
    get_password_hash,
    role_name,
)
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.schemas.auth import LoginResponse, UserMe
from backend.app.services import local_auth_service as local_auth_module
from backend.app.services.local_auth_service import (
    AccountLockedError,
    InvalidCredentialsError,
    LocalAuthService,
    LoginAttemptTracker,
    login_attempt_tracker,
)

pytestmark = pytest.mark.unit

PASSWORD = "Demo1234!"


def _user(**overrides) -> User:
    fields = {
        "id": uuid.uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "full_name": "Alice Example",
        "hashed_password": get_password_hash(PASSWORD),
        "is_active": True,
        "is_superuser": False,
        "role": UserRole.DEVELOPER,
    }
    fields.update(overrides)
    return User(**fields)


@pytest.fixture
def local_provider(monkeypatch):
    """Local provider, fail-closed, clean lockout counters."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


class TestLocalTokens:
    def test_round_trip_claims(self):
        user = _user()
        token = create_local_access_token(user)
        claims = decode_local_token(token)
        assert claims["sub"] == str(user.id)
        assert claims["email"] == user.email
        assert claims["role"] == "DEVELOPER"
        assert claims["iss"] == LOCAL_TOKEN_ISSUER
        assert (
            claims["exp"] - claims["iat"] == settings.LOCAL_AUTH_TOKEN_TTL_MINUTES * 60
        )
        assert len(claims["jti"]) == 32

    def test_header_is_hs256(self):
        header = jwt.get_unverified_header(create_local_access_token(_user()))
        assert header["alg"] == "HS256"

    def test_jti_is_unique_per_token(self):
        user = _user()
        a = decode_local_token(create_local_access_token(user))["jti"]
        b = decode_local_token(create_local_access_token(user))["jti"]
        assert a != b

    def test_expired_token_rejected(self):
        token = create_local_access_token(_user(), expires_minutes=-1)
        with pytest.raises(InvalidTokenError):
            decode_local_token(token)

    def test_wrong_secret_rejected(self):
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": LOCAL_TOKEN_ISSUER,
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            "some-other-secret",
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError):
            decode_local_token(token)

    def test_wrong_issuer_rejected(self):
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": "cognito",
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            settings.SECRET_KEY,
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError):
            decode_local_token(token)

    def test_missing_required_claims_rejected(self):
        token = jwt.encode(
            {"iss": LOCAL_TOKEN_ISSUER}, settings.SECRET_KEY, algorithm="HS256"
        )
        with pytest.raises(InvalidTokenError):
            decode_local_token(token)

    def test_alg_none_rejected(self):
        token = jwt.encode(
            {"sub": "x", "iss": LOCAL_TOKEN_ISSUER, "iat": 1, "exp": 2**31},
            key=None,
            algorithm="none",
        )
        with pytest.raises(InvalidTokenError):
            decode_local_token(token)

    @pytest.mark.parametrize("bad", ["", None, "not-a-jwt", "a.b.c"])
    def test_garbage_rejected(self, bad):
        with pytest.raises(InvalidTokenError):
            decode_local_token(bad)

    def test_invalid_token_error_is_value_error(self):
        assert issubclass(InvalidTokenError, ValueError)

    @pytest.mark.parametrize(
        "role,expected",
        [
            (UserRole.ADMIN, "ADMIN"),
            ("viewer", "VIEWER"),
            (None, "VIEWER"),
            ("ANALYST", "ANALYST"),
        ],
    )
    def test_role_name(self, role, expected):
        assert role_name(role) == expected

    def test_oauth2_scheme_does_not_auto_error(self):
        assert security.oauth2_scheme.auto_error is False


# ---------------------------------------------------------------------------
# Lockout tracker + service
# ---------------------------------------------------------------------------


class TestLoginAttemptTracker:
    def test_locks_after_threshold_within_window(self):
        t = LoginAttemptTracker(max_attempts=3, window_seconds=60)
        now = 1000.0
        assert t.record_failure("A@x.com", now).locked is False
        assert t.record_failure("a@x.com", now + 1).locked is False
        status = t.record_failure("a@X.com", now + 2)
        assert status.locked is True and status.failures == 3
        assert t.status("a@x.com", now + 3).locked is True
        assert t.status("a@x.com", now + 3).retry_after_seconds > 0

    def test_failures_age_out(self):
        t = LoginAttemptTracker(max_attempts=2, window_seconds=10)
        t.record_failure("a@x.com", 0.0)
        t.record_failure("a@x.com", 1.0)
        assert t.status("a@x.com", 5.0).locked is True
        assert t.status("a@x.com", 11.0).locked is False

    def test_reset_clears(self):
        t = LoginAttemptTracker(max_attempts=1, window_seconds=60)
        t.record_failure("a@x.com", 0.0)
        assert t.status("a@x.com", 1.0).locked
        t.reset("a@x.com")
        assert t.status("a@x.com", 1.0).locked is False

    def test_keys_are_independent(self):
        t = LoginAttemptTracker(max_attempts=1, window_seconds=60)
        t.record_failure("a@x.com", 0.0)
        assert t.status("b@x.com", 0.0).locked is False

    def test_expired_addresses_are_swept_without_being_touched_again(self):
        """Addresses that are never queried again must not stay in memory forever."""
        t = LoginAttemptTracker(max_attempts=5, window_seconds=10, sweep_interval=30)
        for i in range(100):
            t.record_failure(f"u{i}@x.com", 0.0)
        assert len(t) == 100
        # A single new failure long after the window + sweep interval triggers a sweep.
        t.record_failure("fresh@x.com", 100.0)
        assert len(t) == 1

    def test_size_cap_evicts_oldest_addresses(self):
        t = LoginAttemptTracker(
            max_attempts=5, window_seconds=3600, max_tracked=50, sweep_interval=0
        )
        for i in range(60):
            t.record_failure(f"u{i}@x.com", float(i))
        assert len(t) <= 51  # cap is enforced before the new key is added
        # The most recent addresses survive, the oldest were evicted.
        assert t.status("u59@x.com", 60.0).failures == 1
        assert t.status("u0@x.com", 60.0).failures == 0

    def test_sweep_keeps_live_windows(self):
        t = LoginAttemptTracker(max_attempts=3, window_seconds=100, sweep_interval=0)
        t.record_failure("live@x.com", 0.0)
        t.record_failure("live@x.com", 50.0)
        t.record_failure(
            "other@x.com", 60.0
        )  # sweep runs: live has a failure at 50 (still in window)
        assert t.status("live@x.com", 60.0).failures == 2


def _key_from_statement(statement) -> str:
    """Stand-in for ``SELECT lower(:typed)``: a distinct ``str`` per input.

    ``authenticate`` asks the database for the counter key; a bare MagicMock
    would hand back one shared child mock for every address, so every address
    would collapse onto one counter and the lockout tests would pass for the
    wrong reason.
    """
    (value,) = statement.compile().params.values()
    return f"key:{value.lower()}"


def _mock_db(rows):
    """A MagicMock session whose lookup returns *rows* (a list, or a callable)."""
    db = MagicMock()
    db.scalar.side_effect = _key_from_statement
    lookup = db.query.return_value.filter.return_value.limit.return_value.all
    if callable(rows):
        lookup.side_effect = rows
    else:
        lookup.return_value = rows
    return db


@pytest.fixture
def bcrypt_calls(monkeypatch):
    """Record the hash every ``verify_password`` call checks, then really check it."""
    import backend.app.services.local_auth_service as mod

    calls = []
    real = mod.verify_password

    def recording(password, hashed):
        calls.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(mod, "verify_password", recording)
    return calls


DUMMY_HASH = local_auth_module._DUMMY_PASSWORD_HASH


class TestLocalAuthService:
    def _db_with(self, user):
        return _mock_db([user] if user is not None else [])

    def test_success_returns_user_and_resets_counter(self, bcrypt_calls):
        user = _user()
        tracker = LoginAttemptTracker(3, 60)
        svc = LocalAuthService(tracker)
        db = self._db_with(user)
        key = _key_from_statement(select(func.lower(user.email)))
        tracker.record_failure(key)
        assert svc.authenticate(db, user.email, PASSWORD) is user
        assert tracker.status(key).failures == 0
        assert bcrypt_calls == [user.hashed_password]
        # One lookup, asking for at most two rows.
        db.query.return_value.filter.return_value.limit.assert_called_once_with(2)

    def test_counter_key_is_the_database_lower_of_the_trimmed_address(self):
        user = _user()
        tracker = LoginAttemptTracker(3, 60)
        svc = LocalAuthService(tracker)
        db = self._db_with(user)
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(db, "  Alice@Example.com ", "wrong")
        (statement,), _ = db.scalar.call_args
        assert list(statement.compile().params.values()) == ["Alice@Example.com"]
        assert tracker.status("key:alice@example.com").failures == 1

    def test_a_key_that_is_not_a_string_fails_loudly(self):
        db = self._db_with(_user())
        db.scalar.side_effect = None  # a bare MagicMock again
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(TypeError):
            svc.authenticate(db, "alice@example.com", PASSWORD)

    def test_wrong_password(self, bcrypt_calls):
        user = _user()
        tracker = LoginAttemptTracker(3, 60)
        svc = LocalAuthService(tracker)
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(self._db_with(user), user.email, "nope")
        assert bcrypt_calls == [user.hashed_password]
        assert tracker.status(f"key:{user.email}").failures == 1

    def test_unknown_user(self, bcrypt_calls):
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(self._db_with(None), "ghost@example.com", PASSWORD)
        assert bcrypt_calls == [DUMMY_HASH]

    def test_inactive_user_is_invalid_credentials(self, bcrypt_calls):
        user = _user(is_active=False)
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(self._db_with(user), user.email, PASSWORD)
        assert bcrypt_calls == [user.hashed_password]

    def test_user_without_password_hash_cannot_log_in(self, bcrypt_calls):
        user = _user(hashed_password=None)
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(self._db_with(user), user.email, PASSWORD)
        assert bcrypt_calls == [DUMMY_HASH]

    def test_an_address_matching_two_accounts_is_refused(self, bcrypt_calls):
        one, two = _user(email="Dup@example.com"), _user(email="dup@example.com")
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(_mock_db([one, two]), "DUP@example.com", PASSWORD)
        assert bcrypt_calls == [DUMMY_HASH]

    def test_lockout_after_threshold_even_with_correct_password(self, bcrypt_calls):
        user = _user()
        svc = LocalAuthService(LoginAttemptTracker(max_attempts=2, window_seconds=60))
        db = self._db_with(user)
        for _ in range(2):
            with pytest.raises(InvalidCredentialsError):
                svc.authenticate(db, user.email, "wrong")
        assert bcrypt_calls == [user.hashed_password] * 2
        bcrypt_calls.clear()
        with pytest.raises(AccountLockedError) as exc:
            svc.authenticate(db, user.email, PASSWORD)
        assert exc.value.retry_after_seconds >= 1
        # A locked address is answered before any password is checked.
        assert bcrypt_calls == []

    def test_lockouts_of_different_addresses_are_independent(self):
        alice, bob = _user(), _user(email="bob@example.com")
        svc = LocalAuthService(LoginAttemptTracker(max_attempts=1, window_seconds=60))
        db = _mock_db(list)
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(db, alice.email, "wrong")
        db.query.return_value.filter.return_value.limit.return_value.all.side_effect = (
            lambda: [bob]
        )
        assert svc.authenticate(db, bob.email, PASSWORD) is bob

    def test_unknown_email_costs_a_password_verification(self, bcrypt_calls):
        """Response time must not reveal whether the address exists (timing oracle)."""
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(self._db_with(None), "ghost@example.com", "x")
        assert bcrypt_calls == [DUMMY_HASH]

    def test_passwordless_user_costs_a_password_verification(self, bcrypt_calls):
        user = _user(hashed_password=None)
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(self._db_with(user), user.email, "x")
        assert bcrypt_calls == [DUMMY_HASH]

    def test_unknown_email_also_counts_toward_lockout(self):
        svc = LocalAuthService(LoginAttemptTracker(max_attempts=1, window_seconds=60))
        db = self._db_with(None)
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(db, "ghost@example.com", "x")
        with pytest.raises(AccountLockedError):
            svc.authenticate(db, "ghost@example.com", "x")

    @pytest.mark.parametrize(
        "typed",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace"),
            pytest.param("alice\x00@example.com", id="nul"),
            pytest.param("\x00", id="nul-only"),
            pytest.param("a" * 321, id="321-chars"),
            pytest.param("a" * 1_000_000, id="1m-chars"),
        ],
    )
    def test_an_address_no_account_can_hold_is_refused_without_a_lookup(
        self, bcrypt_calls, typed
    ):
        tracker = LoginAttemptTracker(max_attempts=1, window_seconds=60)
        svc = LocalAuthService(tracker)
        db = self._db_with(_user())
        for _ in range(3):
            with pytest.raises(InvalidCredentialsError):
                svc.authenticate(db, typed, PASSWORD)
        # Never locked, never looked up, never counted; same bcrypt cost.
        db.scalar.assert_not_called()
        db.query.assert_not_called()
        assert len(tracker) == 0
        assert bcrypt_calls == [DUMMY_HASH] * 3

    def test_a_320_character_address_is_looked_up(self):
        db = self._db_with(None)
        svc = LocalAuthService(LoginAttemptTracker(3, 60))
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate(db, "a" * 320, PASSWORD)
        db.scalar.assert_called_once()

    def test_password_change_keys_on_the_database_lower_of_the_address(self):
        user = _user(email="Alice@example.com")
        tracker = LoginAttemptTracker(max_attempts=1, window_seconds=60)
        svc = LocalAuthService(tracker)
        db = _mock_db([])
        with pytest.raises(InvalidCredentialsError):
            svc.verify_current_password(db, user, "wrong")
        assert tracker.status("key:alice@example.com").failures == 1

    def test_password_change_without_an_email_has_a_key_sign_in_cannot_reach(self):
        user = _user(email=None)
        tracker = LoginAttemptTracker(max_attempts=1, window_seconds=60)
        svc = LocalAuthService(tracker)
        db = _mock_db([])
        with pytest.raises(InvalidCredentialsError):
            svc.verify_current_password(db, user, "wrong")
        db.scalar.assert_not_called()
        (key,) = tracker._failures
        assert key.startswith("\x00")
        assert str(user.id) in key
        # Sign-in cannot reach that counter: the key itself contains NUL and
        # is refused before any key is built, and the same text without the
        # NUL is a different key.  Neither answers 423.
        for typed in (key, f"id:{user.id}"):
            with pytest.raises(InvalidCredentialsError):
                svc.authenticate(db, typed, PASSWORD)
        assert tracker.status(key).failures == 1


# ---------------------------------------------------------------------------
# deps.get_current_user — local path
# ---------------------------------------------------------------------------


class TestGetCurrentUserLocal:
    def _db_with(self, user):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        return db

    def test_valid_token_loads_user_by_id(self, local_provider):
        user = _user()
        token = create_local_access_token(user)
        with patch("backend.app.api.deps.auth_service") as cognito:
            result = deps.get_current_user(token, self._db_with(user))
        assert result is user
        cognito.get_user_with_groups.assert_not_called()

    def test_missing_token_is_401(self, local_provider):
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(None, MagicMock())
        assert exc.value.status_code == 401
        assert exc.value.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("token", ["garbage", "a.b.c"])
    def test_malformed_token_is_401(self, local_provider, token):
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(token, MagicMock())
        assert exc.value.status_code == 401

    def test_expired_token_is_401(self, local_provider):
        token = create_local_access_token(_user(), expires_minutes=-5)
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(token, MagicMock())
        assert exc.value.status_code == 401

    def test_unknown_subject_is_401(self, local_provider):
        token = create_local_access_token(_user())
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(token, self._db_with(None))
        assert exc.value.status_code == 401

    def test_non_uuid_subject_is_401(self, local_provider):
        token = jwt.encode(
            {
                "sub": "not-a-uuid",
                "iss": LOCAL_TOKEN_ISSUER,
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            settings.SECRET_KEY,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(token, MagicMock())
        assert exc.value.status_code == 401

    def test_inactive_user_is_400(self, local_provider):
        user = _user(is_active=False)
        token = create_local_access_token(user)
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(token, self._db_with(user))
        assert exc.value.status_code == 400
        assert exc.value.detail == "Inactive user"

    def test_cognito_token_is_rejected_by_local_provider(self, local_provider):
        """A foreign issuer never resolves, even with the shared secret."""
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": "https://cognito-idp.us-east-1.amazonaws.com/pool",
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
            },
            settings.SECRET_KEY,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(token, MagicMock())
        assert exc.value.status_code == 401

    def test_bypass_returns_dev_admin_without_token(self, local_provider, monkeypatch):
        monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
        dev = _user(
            username=deps.DEV_BYPASS_USERNAME, role=UserRole.ADMIN, is_superuser=True
        )
        assert deps.get_current_user(None, self._db_with(dev)) is dev

    def test_bypass_ignored_in_production(self, local_provider, monkeypatch):
        monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        with pytest.raises(HTTPException) as exc:
            deps.get_current_user(None, MagicMock())
        assert exc.value.status_code == 401

    def test_cognito_provider_still_uses_cognito(self, local_provider, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
        with patch("backend.app.api.deps.auth_service") as cognito:
            cognito.get_user_with_groups.side_effect = ValueError("bad token")
            with pytest.raises(HTTPException) as exc:
                deps.get_current_user("opaque-cognito-token", MagicMock())
        assert exc.value.status_code == 401
        cognito.get_user_with_groups.assert_called_once_with("opaque-cognito-token")


# ---------------------------------------------------------------------------
# UserMe schema
# ---------------------------------------------------------------------------


class TestUserMeSchema:
    @pytest.mark.parametrize(
        "role,expected",
        [
            (UserRole.ADMIN, "ADMIN"),
            (UserRole.VIEWER, "VIEWER"),
            ("developer", "DEVELOPER"),
            (None, "VIEWER"),
        ],
    )
    def test_role_is_upper_case_name(self, role, expected):
        me = UserMe(
            id=uuid.uuid4(),
            email="a@b.c",
            username="a",
            role=role,
            is_superuser=False,
            is_active=True,
            auth_provider="local",
        )
        assert me.role == expected

    def test_contract_fields(self):
        assert set(UserMe.model_fields) == {
            "id",
            "email",
            "username",
            "full_name",
            "role",
            "is_superuser",
            "is_active",
            "auth_provider",
        }
        assert set(LoginResponse.model_fields) == {
            "access_token",
            "token_type",
            "expires_in",
            "user",
        }


# ---------------------------------------------------------------------------
# HTTP endpoints with a mocked DB
# ---------------------------------------------------------------------------


@pytest.fixture
def http(local_provider):
    """TestClient whose ``get_db`` yields a MagicMock; ``client.state.user`` is the row."""
    state = SimpleNamespace(user=None)

    def _rows():
        return [state.user] if state.user is not None else []

    db = _mock_db(_rows)
    # deps.get_current_user loads the token's user with .filter().first(); the
    # /auth/me tests below go through it.
    db.query.return_value.filter.return_value.first.side_effect = lambda: state.user
    # No confirmed user_totp row: the account has no two-step sign-in, so the
    # password step issues the token (#1812; test_two_factor_routes.py covers
    # an account that has it).
    db.execute.return_value.first.return_value = None

    def override_get_db():
        yield db

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            client.state = state  # type: ignore[attr-defined]
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


class TestLoginEndpoint:
    def test_login_success_shape(self, http):
        user = _user()
        http.state.user = user
        resp = http.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"access_token", "token_type", "expires_in", "user"}
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.LOCAL_AUTH_TOKEN_TTL_MINUTES * 60
        assert body["user"] == {
            "id": str(user.id),
            "email": user.email,
            "username": user.username,
            "full_name": "Alice Example",
            "role": "DEVELOPER",
            "is_superuser": False,
            "is_active": True,
            "auth_provider": "local",
        }
        assert decode_local_token(body["access_token"])["sub"] == str(user.id)

    def test_wrong_password_401_generic_message(self, http):
        user = _user()
        http.state.user = user
        resp = http.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "wrong"}
        )
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid email or password"}

    def test_unknown_user_same_message(self, http):
        http.state.user = None
        resp = http.post(
            "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "x"}
        )
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid email or password"}

    def test_inactive_user_same_message(self, http):
        http.state.user = _user(is_active=False)
        resp = http.post(
            "/api/v1/auth/login",
            json={"email": http.state.user.email, "password": PASSWORD},
        )
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid email or password"}

    def test_missing_fields_422(self, http):
        assert http.post("/api/v1/auth/login", json={}).status_code == 422
        assert (
            http.post("/api/v1/auth/login", json={"email": "a@b.c"}).status_code == 422
        )

    def test_lockout_returns_423_after_max_failures(self, http, monkeypatch):
        monkeypatch.setattr(login_attempt_tracker, "max_attempts", 3)
        user = _user()
        http.state.user = user
        for _ in range(3):
            r = http.post(
                "/api/v1/auth/login", json={"email": user.email, "password": "wrong"}
            )
            assert r.status_code == 401
        r = http.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert r.status_code == 423, r.text
        assert "Retry-After" in r.headers
        assert "locked" in r.json()["detail"].lower()

    def test_login_404_under_cognito_provider(self, http, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
        resp = http.post("/api/v1/auth/login", json={"email": "a@b.c", "password": "x"})
        assert resp.status_code == 404

    def test_login_is_rate_limited(self):
        from backend.app.middleware.rate_limiter import (
            RATE_LIMIT_CONFIG,
            resolve_rate_limit,
        )

        assert RATE_LIMIT_CONFIG["/api/v1/auth/login"] == (10, 60)
        assert resolve_rate_limit("/api/v1/auth/login") == (10, 60)


class TestTokenFormEndpoint:
    def test_form_login_returns_same_shape(self, http):
        user = _user()
        http.state.user = user
        resp = http.post(
            "/api/v1/auth/token", data={"username": user.email, "password": PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["email"] == user.email
        assert decode_local_token(body["access_token"])["email"] == user.email

    def test_form_login_wrong_password(self, http):
        user = _user()
        http.state.user = user
        resp = http.post(
            "/api/v1/auth/token", data={"username": user.email, "password": "no"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password"


class TestMeAndLogoutEndpoints:
    def test_me_with_valid_token(self, http):
        user = _user(role=UserRole.ADMIN, is_superuser=True)
        http.state.user = user
        token = create_local_access_token(user)
        resp = http.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["role"] == "ADMIN"
        assert body["is_superuser"] is True
        assert body["auth_provider"] == "local"
        assert body["id"] == str(user.id)

    def test_me_without_token_401(self, http):
        resp = http.get("/api/v1/auth/me")
        assert resp.status_code == 401
        assert "detail" in resp.json()

    def test_me_with_bad_token_401(self, http):
        resp = http.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_me_with_bypass_returns_dev_admin(self, http, monkeypatch):
        monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
        http.state.user = _user(
            username=deps.DEV_BYPASS_USERNAME,
            email=deps.DEV_BYPASS_EMAIL,
            role=UserRole.ADMIN,
            is_superuser=True,
        )
        resp = http.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == deps.DEV_BYPASS_USERNAME

    def test_logout_204(self, http):
        resp = http.post("/api/v1/auth/logout")
        assert resp.status_code == 204
        assert resp.content == b""

    def test_protected_endpoint_rejects_missing_token(self, http):
        resp = http.get("/api/v1/api-keys")
        assert resp.status_code == 401

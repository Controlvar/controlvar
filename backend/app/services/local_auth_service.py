"""
Local (email + password) authentication -- the default provider.

``AUTH_PROVIDER=local`` authenticates against ``users.email`` /
``users.hashed_password`` (bcrypt, see ``core.security``) and issues HS256
JWTs.  No AWS dependency.

Matching the address
--------------------
The typed address is trimmed and compared with ``lower()`` on both sides, in
the database: ``lower(users.email) = lower(:typed)``.  An account stored as
``Bob@acme.com`` therefore signs in as ``bob@acme.com`` or ``BOB@ACME.COM``,
and the unique index on ``lower(email)`` (``ix_users_email_lower``) both
serves that lookup and keeps two accounts from differing only in case.  An
address that is empty after trimming, contains a NUL character or is longer
than ``MAX_EMAIL_LENGTH`` cannot belong to any account; it is refused with the
same 401 without a database call and is not counted.

Brute-force protection
----------------------
Failed logins are counted per e-mail address, keyed on the same database
``lower()`` the lookup uses, so every spelling that reaches one account
draws on one budget.  After
``LOCAL_AUTH_MAX_FAILED_ATTEMPTS`` failures inside a rolling window of
``LOCAL_AUTH_LOCKOUT_MINUTES`` the address is locked for the remainder of
that window and ``/auth/login`` answers ``423 Locked``.  A successful login
clears the counter.  A wrong current password on ``POST /users/me/password``
counts against the same address and is locked by the same counter (see
``LocalAuthService.verify_current_password``).

The counter is an **in-process** dictionary.  That is deliberate: it
needs no extra infrastructure and is correct for the default single-worker
container (``WEB_CONCURRENCY=1``).  Deployments running several uvicorn
workers or replicas get an independent counter per worker, so the effective
threshold is ``LOCAL_AUTH_MAX_FAILED_ATTEMPTS x workers``.  Either accept
that (the per-IP rate limit on ``/api/v1/auth/login`` still applies) or run
a single worker.  Unknown e-mail addresses are counted too, so an attacker
cannot use the 423 as a user-enumeration oracle, and a bcrypt verification
runs against a dummy hash for unknown addresses so response time does not
reveal whether the address exists either.

The tracker sweeps expired windows periodically and caps the number of
tracked addresses, so unauthenticated traffic cannot grow it without bound.

Demo mode
---------
With ``DEMO_MODE=true`` (``backend/app/core/demo_mode.py``) only an active
non-superuser VIEWER signs in, and while a demo account is published the
counter is neither checked nor written, for any address; see
``LocalAuthService.authenticate``.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.core.security import get_password_hash, verify_password
from backend.app.models.user import User, UserRole

# Upper bound on distinct addresses kept in memory.  When exceeded (after a
# sweep) the entries with the oldest most-recent failure are evicted; an
# attacker spraying random addresses can therefore only evict their own noise.
MAX_TRACKED_ADDRESSES = 10_000

# How often (seconds) record_failure sweeps expired windows for every key.
SWEEP_INTERVAL_SECONDS = 60.0

# The longest typed address that is looked up (RFC 5321's 64 + 1 + 255, as
# ``LoginRequest.email`` allows).  Anything longer cannot match a stored
# address and is refused before it reaches the database or the tracker.
MAX_EMAIL_LENGTH = 320

logger = logging.getLogger(__name__)


class AccountLockedError(Exception):
    """Raised when the address has exceeded the failed-login threshold."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(
            f"Too many failed login attempts; retry in {self.retry_after_seconds}s"
        )


class InvalidCredentialsError(Exception):
    """Raised for unknown e-mail, wrong password, or an inactive account."""


class NoLocalPasswordError(Exception):
    """Raised when an account has no password of its own to check.

    Such an account signs in some other way; it has no current password to
    prove, so it cannot set one through a self-service change.
    """


class CurrentPasswordMissingError(Exception):
    """Raised when a password change arrives without the current password."""


def _is_demo_viewer(user: User) -> bool:
    """The only account demo mode signs in: a non-superuser VIEWER."""
    return user.role == UserRole.VIEWER and not user.is_superuser


def _normalise_email(email: str) -> str:
    # A second fold on top of the database's lower() key: it hides a lost SQL
    # lower() for A-Z, so the non-ASCII budget test is what guards the key.
    return (email or "").strip().lower()


def _counter_key(value: object) -> str:
    """The database's answer to ``SELECT lower(...)``, which must be a string.

    An explicit check rather than ``assert``, so it holds under ``python -O``.
    """
    if not isinstance(value, str):
        raise TypeError(f"lockout counter key must be str, got {type(value).__name__}")
    return value


@dataclass
class LockoutStatus:
    locked: bool
    failures: int
    retry_after_seconds: int


class LoginAttemptTracker:
    """
    Thread-safe rolling-window failure counter keyed by e-mail.

    ``max_attempts`` failures within ``window_seconds`` lock the key until the
    oldest failure ages out of the window.
    """

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        max_tracked: int = MAX_TRACKED_ADDRESSES,
        sweep_interval: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.max_tracked = max(1, int(max_tracked))
        self.sweep_interval = max(0.0, float(sweep_interval))
        self._lock = threading.Lock()
        self._failures: Dict[str, Deque[float]] = {}
        # Clock of the last sweep, in whatever time base the caller uses
        # (time.monotonic() by default, synthetic values in tests).
        self._last_sweep: Optional[float] = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)

    def _sweep(self, now: float) -> None:
        """Drop every expired window, then enforce the size cap.  Caller holds the lock."""
        cutoff = now - self.window_seconds
        for key in [k for k, w in self._failures.items() if not w or w[-1] <= cutoff]:
            self._failures.pop(key, None)
        self._last_sweep = now
        overflow = len(self._failures) - self.max_tracked
        if overflow > 0:
            oldest = sorted(self._failures.items(), key=lambda item: item[1][-1])[
                :overflow
            ]
            for key, _ in oldest:
                self._failures.pop(key, None)

    def _prune(self, key: str, now: float) -> Deque[float]:
        window = self._failures.get(key)
        if window is None:
            window = deque()
            self._failures[key] = window
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if not window:
            # Drop empty keys so the dict does not grow without bound.
            self._failures.pop(key, None)
        return window

    def status(self, email: str, now: Optional[float] = None) -> LockoutStatus:
        key = _normalise_email(email)
        now = time.monotonic() if now is None else now
        with self._lock:
            window = self._prune(key, now)
            failures = len(window)
            if failures >= self.max_attempts:
                retry_after = int(window[0] + self.window_seconds - now) + 1
                return LockoutStatus(True, failures, retry_after)
            return LockoutStatus(False, failures, 0)

    def record_failure(self, email: str, now: Optional[float] = None) -> LockoutStatus:
        key = _normalise_email(email)
        now = time.monotonic() if now is None else now
        with self._lock:
            if (
                self._last_sweep is None
                or now - self._last_sweep >= self.sweep_interval
                or len(self._failures) >= self.max_tracked
            ):
                self._sweep(now)
            window = self._prune(key, now)
            window.append(now)
            self._failures[key] = window
            failures = len(window)
            if failures >= self.max_attempts:
                retry_after = int(window[0] + self.window_seconds - now) + 1
                return LockoutStatus(True, failures, retry_after)
            return LockoutStatus(False, failures, 0)

    def reset(self, email: str) -> None:
        key = _normalise_email(email)
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        """Forget every counter (tests)."""
        with self._lock:
            self._failures.clear()


# A real bcrypt hash of a random secret: verifying against it costs the same
# as verifying a real user's password, which keeps the response time of an
# unknown address indistinguishable from a wrong password.
_DUMMY_PASSWORD_HASH = get_password_hash(secrets.token_urlsafe(24))


class LocalAuthService:
    """Email/password authentication with per-address lockout."""

    def __init__(self, tracker: Optional[LoginAttemptTracker] = None) -> None:
        # ``is not None``: an empty tracker is falsy (``__len__``).
        self.tracker = (
            tracker
            if tracker is not None
            else LoginAttemptTracker(
                max_attempts=settings.LOCAL_AUTH_MAX_FAILED_ATTEMPTS,
                window_seconds=settings.LOCAL_AUTH_LOCKOUT_MINUTES * 60,
            )
        )

    def authenticate(self, db: Session, email: str, password: str) -> User:
        """
        Return the active ``User`` matching *email*/*password*.

        *email* is trimmed and matched whatever its letter case, as the
        database's ``lower()`` matches (see the module docstring).

        Demo mode (``DEMO_MODE=true``) compares no addresses. Only an active
        non-superuser VIEWER signs in: any other account, with its right
        password, gets the same refusal as a wrong password. And while a demo
        account is published (``settings.demo_account_published``: its
        password is public) failed attempts are neither counted nor checked,
        for any address, so the shared account cannot be made to answer 423.
        Demo mode without a published account counts them as usual.

        Raises:
            AccountLockedError: the address is currently locked out (never
                while a demo account is published).
            InvalidCredentialsError: unknown address, wrong password or the
                account is inactive, or, in demo mode, not a non-superuser
                viewer.  The caller must not distinguish these.
        """
        demo_mode = bool(settings.DEMO_MODE)
        counted = not settings.demo_account_published
        typed = (email or "").strip()
        if not typed or "\x00" in typed or len(typed) > MAX_EMAIL_LENGTH:
            # No account can hold this address, so there is no budget to
            # protect: nothing is looked up and nothing is counted.  The
            # bcrypt run keeps the cost the same as any other refusal.
            verify_password(password or "", _DUMMY_PASSWORD_HASH)
            raise InvalidCredentialsError("Invalid email or password")

        # The counter key is the database's own lower() of the typed address:
        # the same function the lookup applies, so every spelling that can
        # match one account lands on one key.  No counter while a demo account
        # is published.
        email_key: Optional[str] = None
        if counted:
            email_key = _counter_key(db.scalar(select(func.lower(typed))))
            status = self.tracker.status(email_key)
            if status.locked:
                raise AccountLockedError(status.retry_after_seconds)

        rows: List[User] = (
            db.query(User)
            .filter(func.lower(User.email) == func.lower(typed))
            .limit(2)
            .all()
        )
        user: Optional[User] = None
        if len(rows) == 1:
            user = rows[0]
        elif len(rows) > 1:
            # ix_users_email_lower makes this impossible; if the index has
            # gone, refuse rather than pick one of the accounts.
            logger.warning(
                "Local sign-in refused: accounts %s share one address "
                "regardless of case; is ix_users_email_lower missing?",
                ", ".join(str(row.id) for row in rows),
            )

        if user is not None and user.hashed_password:
            password_ok = verify_password(password or "", user.hashed_password)
        else:
            # Constant-cost path for unknown addresses / passwordless rows
            # (SSO-only accounts): burn a bcrypt verification anyway.
            verify_password(password or "", _DUMMY_PASSWORD_HASH)
            password_ok = False
        ok = password_ok and user is not None and bool(user.is_active)
        if ok and demo_mode:
            ok = _is_demo_viewer(user)
        if not ok:
            # The failure that reaches the threshold is still answered with
            # 401; every attempt after it (correct password or not) gets 423
            # until the oldest failure ages out of the window.
            if email_key is not None:
                self.tracker.record_failure(email_key)
            raise InvalidCredentialsError("Invalid email or password")

        if email_key is not None:
            self.tracker.reset(email_key)
        return user

    def verify_current_password(
        self, db: Session, user: User, password: Optional[str]
    ) -> None:
        """
        Check that *password* is *user*'s current password, or raise.

        Used by a signed-in user's own password change. It draws on the SAME
        failure budget as ``/auth/login`` (``self.tracker``), so a session
        cannot be used to guess the password faster than the sign-in form
        allows, and a lockout from either place applies to both.

        In order:

        1. the address is locked out: ``AccountLockedError`` (423);
        2. the account has no password of its own (``hashed_password`` empty
           or NULL): ``NoLocalPasswordError``. Not counted as a failure --
           nothing was guessed;
        3. no current password was sent: ``CurrentPasswordMissingError``.
           Not counted either;
        4. the password does not match: the failure is recorded, then
           ``InvalidCredentialsError``;
        5. it matches: the counter is cleared.

        The counter is keyed on the database's ``lower()`` of the account's
        e-mail address -- the key ``authenticate`` uses for any spelling of
        that address -- so sign-in and this check share one budget. A
        superuser who changes the account's address starts it on a fresh
        counter (accepted). An account with no e-mail address gets a key of
        its own, built from its id with a NUL prefix: ``authenticate``
        refuses every typed address containing NUL before it builds a key,
        so no sign-in attempt can reach that counter.
        """
        if user.email is None:
            email_key = "\x00id:" + str(user.id)
        else:
            email_key = _counter_key(db.scalar(select(func.lower(user.email))))
        status = self.tracker.status(email_key)
        if status.locked:
            raise AccountLockedError(status.retry_after_seconds)

        if not user.hashed_password:
            raise NoLocalPasswordError()

        if not password:
            raise CurrentPasswordMissingError()

        if not verify_password(password, user.hashed_password):
            self.tracker.record_failure(email_key)
            raise InvalidCredentialsError("The current password is incorrect.")

        self.tracker.reset(email_key)


# Process-wide tracker shared by /auth/login, /auth/token (local provider) and
# POST /users/me/password.
login_attempt_tracker = LoginAttemptTracker(
    max_attempts=settings.LOCAL_AUTH_MAX_FAILED_ATTEMPTS,
    window_seconds=settings.LOCAL_AUTH_LOCKOUT_MINUTES * 60,
)
local_auth_service = LocalAuthService(tracker=login_attempt_tracker)

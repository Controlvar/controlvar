"""Shared pieces of the two-step sign-in service tests (#1812).

Not a test module. Accounts, a clock the tests move by hand, a fresh lockout
counter per test, sessions of their own for the races, and the forced
interleaving the races use: session A takes the row lock and stays open,
session B runs in a thread and is seen waiting on a lock in
``pg_stat_activity`` before A commits.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from typing import Callable, Dict, Optional

import bcrypt
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core import totp
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.app.models.user_totp import UserTOTP
from backend.app.services import two_factor_service as service_module
from backend.app.services.local_auth_service import LoginAttemptTracker
from backend.app.services.two_factor_service import TwoFactorService

#: A second in the middle of a step, far from the epoch.
START = 1_800_000_015.0


class Clock:
    """The only clock: a number the test sets."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def step(self) -> int:
        return totp.step_at(self.now)


class BcryptCounter:
    """Counts every recovery-code bcrypt verification the service runs."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, plain: str, hashed: str) -> bool:
        self.calls += 1
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def fast_hash(plain: str) -> str:
    """bcrypt at its lowest work factor: the same format, a test's speed."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture
def bcrypt_calls(monkeypatch) -> BcryptCounter:
    counter = BcryptCounter()
    monkeypatch.setattr(service_module, "verify_password", counter)
    monkeypatch.setattr(service_module, "get_password_hash", fast_hash)
    return counter


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tracker() -> LoginAttemptTracker:
    return LoginAttemptTracker(max_attempts=10, window_seconds=900)


@pytest.fixture
def service(tracker, clock, bcrypt_calls) -> TwoFactorService:
    return TwoFactorService(tracker=tracker, clock=clock)


@pytest.fixture
def session_factory(test_db) -> Callable[[], Session]:
    return sessionmaker(bind=test_db, autoflush=False, expire_on_commit=False)


def make_user(db: Session, *, email: Optional[str] = None) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"tfa_{suffix}",
        email=email or f"Two.Step.{suffix}@Example.com",
        hashed_password=fast_hash("a password nobody types here"),
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db.add(user)
    db.commit()
    return user


def secret_of(enrolment) -> bytes:
    text_ = enrolment.secret
    return base64.b32decode(text_ + "=" * (-len(text_) % 8))


def code_at(secret: bytes, step: int) -> str:
    return totp.hotp(secret, step)


def enabled_user(db: Session, service: TwoFactorService, clock: Clock):
    """An account with two-step sign-in on: (user, secret, recovery codes).

    The confirm code is the current step's, so that step is used up; the
    clock then moves one step on, so the next code is fresh."""
    user = make_user(db)
    secret = secret_of(service.enrol(db, user))
    codes = service.confirm(db, user, code_at(secret, clock.step()))
    clock.now += totp.STEP_SECONDS
    return user, secret, codes


def row(db: Session, user_id) -> Optional[UserTOTP]:
    db.expire_all()
    return db.execute(
        select(UserTOTP).where(UserTOTP.user_id == user_id)
    ).scalar_one_or_none()


def entries(db: Session, user_id, action: str):
    db.expire_all()
    return (
        db.execute(
            select(AuditLog)
            .where(AuditLog.entity_id == user_id, AuditLog.action_type == action)
            .order_by(AuditLog.timestamp)
        )
        .scalars()
        .all()
    )


def lock_wait(
    session_factory: Callable[[], Session],
    hold: Callable[[Session], object],
    contend: Callable[[Session], object],
) -> Dict[str, object]:
    """Force the interleaving: A runs ``hold`` and keeps its transaction open;
    B runs ``contend`` then commits, in a thread; B must be seen waiting on a
    lock before A commits.

    Returns ``{"a": hold's result, "b": contend's result, "waited": bool}``.
    The 20-second limit only bounds a hung run; nothing is timed.
    """
    a, b, probe = session_factory(), session_factory(), session_factory()
    outcome: Dict[str, object] = {"waited": False}
    try:
        outcome["a"] = hold(a)
        b_pid = b.execute(text("SELECT pg_backend_pid()")).scalar_one()

        def run_b():
            try:
                outcome["b"] = contend(b)
                b.commit()
            except Exception as exc:  # pragma: no cover - reported by the caller
                outcome["error"] = exc
                b.rollback()

        runner = threading.Thread(target=run_b)
        runner.start()
        deadline = time.monotonic() + 20
        while runner.is_alive() and time.monotonic() < deadline:
            waiting = probe.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p"),
                {"p": b_pid},
            ).scalar_one_or_none()
            probe.rollback()
            if waiting == "Lock":
                outcome["waited"] = True
                break
            time.sleep(0.02)
        a.commit()
        runner.join(timeout=20)
        assert not runner.is_alive(), "session B never finished"
    finally:
        for session in (a, b, probe):
            session.close()
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome

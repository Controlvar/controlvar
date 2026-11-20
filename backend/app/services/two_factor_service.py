"""Two-step sign-in for local accounts: storage, and the one code check (#1812).

Every check of an authenticator code or a recovery code goes through
:meth:`TwoFactorService.check_code`; nothing else imports
``backend.app.core.totp`` to compare a code. In order, it:

1. refuses input that is not a code at all, uncounted: neither or both of
   ``code`` and ``recovery_code``, or more than ``RAW_INPUT_MAX`` characters
   (no bcrypt runs for it);
2. checks the password lockout this account shares with sign-in and password
   change (``local_auth_service.login_attempt_tracker``, keyed the same way):
   locked is ``AccountLockedError``. While a demo account is published that
   counter is neither checked nor written, exactly as ``authenticate`` skips
   it; the per-account count below still counts;
3. checks the account's own count of wrong codes, ``user_totp.failed_attempts``:
   at ``CODE_STEP_CAP`` every code is refused, a right one too, with
   ``CodeStepLockedError``, until the row is reset;
4. checks the code. A code is accepted for one step either side of the
   server's (``core/totp.py``) and only for a step later than the last one
   accepted, written by a conditional ``UPDATE`` whose row count decides; a
   recovery code is removed by a conditional ``UPDATE`` that only one request
   can win. Each refusal adds one to ``failed_attempts`` (in the database,
   ``failed_attempts + 1``) and records a failure on the shared counter, is
   committed, and logs one ``two_factor_refused`` line with the account id,
   the reason and the client's address -- never the code. An accepted code
   sets ``failed_attempts`` to 0. Only an accepted code at sign-in
   (``SIGN_IN``) also clears the shared counter, as an accepted password
   clears it for an account without two-step sign-in; a code accepted to
   confirm a secret, make new recovery codes or turn two-step sign-in off
   leaves it as it was.

A secret that cannot be opened with the current ``SECRET_KEY`` is refused with
``TwoFactorUnavailableError``: never a fall back to the password alone.

Enabling, disabling, resetting, using a recovery code and making new recovery
codes each write one audit entry in the same transaction as the change, so the
two are kept or lost together. A wrong code writes none.

This module has no routes. Its callers are the routes that ask for a code and
the operator's reset command (``backend/app/db/two_factor_reset.py``).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import Text, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.app.core import totp
from backend.app.core.config import settings
from backend.app.core.security import get_password_hash, verify_password
from backend.app.core.two_factor_keys import SecretUnreadable, open_secret, seal_secret
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User
from backend.app.models.user_totp import UserTOTP
from backend.app.services.audit_service import AuditActor, AuditService
from backend.app.services.local_auth_service import (
    AccountLockedError,
    LoginAttemptTracker,
    login_attempt_tracker,
)

logger = logging.getLogger(__name__)

#: Wrong codes in a row after which the code step refuses every code until a
#: reset (NIST SP 800-63B 5.2.2 allows at most 100).
CODE_STEP_CAP = 100
#: Longest ``code`` or ``recovery_code`` looked at; longer is refused before
#: anything else, uncounted, and before any bcrypt.
RAW_INPUT_MAX = 64
#: Recovery codes in a set, and characters in each.
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 10
#: The characters of a recovery code: base32's, so 0/O and 1/I/L never mix.
RECOVERY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

#: The fixed answers. API, dashboard and documentation use these strings.
WRONG_CODE_TEXT = "That code is not correct."
CODE_STEP_LOCKED_TEXT = (
    "Two-step sign-in is locked after too many wrong codes. "
    "Ask an administrator to reset it."
)
UNAVAILABLE_TEXT = (
    "Two-step sign-in is unavailable for this account. "
    "Ask an administrator to reset it."
)

#: Spaces, tabs and hyphens people type or paste inside a code.
_SEPARATORS = re.compile(r"[\s\-]")
#: Literal patterns (the regex gate takes fixed text only); a unit test pins
#: the counts to ``totp.DIGITS`` and ``RECOVERY_CODE_LENGTH``.
_CODE_SHAPE = re.compile(r"[0-9]{6}")
_RECOVERY_SHAPE = re.compile(r"[A-Z2-7]{10}")

#: What a check is for: the row it needs, and whether a recovery code may
#: stand in for a code.
SIGN_IN = "sign_in"
CONFIRM = "confirm"
SIGNED_IN = "signed_in"
_PURPOSES = (SIGN_IN, CONFIRM, SIGNED_IN)


class TwoFactorError(Exception):
    """Base of every refusal here."""


class TwoFactorInputError(TwoFactorError):
    """Not a code at all (too long, neither or both fields). Not counted."""


class WrongCodeError(TwoFactorError):
    """A wrong, reused or malformed code or recovery code. Counted."""

    def __init__(self) -> None:
        super().__init__(WRONG_CODE_TEXT)


class CodeStepLockedError(TwoFactorError):
    """``failed_attempts`` reached ``CODE_STEP_CAP``: refused until a reset."""

    def __init__(self) -> None:
        super().__init__(CODE_STEP_LOCKED_TEXT)


class TwoFactorUnavailableError(TwoFactorError):
    """The stored secret cannot be opened with the current ``SECRET_KEY``."""

    def __init__(self) -> None:
        super().__init__(UNAVAILABLE_TEXT)


class AlreadyEnabledError(TwoFactorError):
    """Two-step sign-in is already on for this account."""


class NotEnabledError(TwoFactorError):
    """No row in the state the step needs (on, or waiting for a first code)."""


@dataclass(frozen=True)
class Enrolment:
    """A new secret, for the account's app: typed in, or as a QR link."""

    secret: str
    otpauth_uri: str


@dataclass(frozen=True)
class TwoFactorStatus:
    enabled: bool
    recovery_codes_remaining: int


@dataclass(frozen=True)
class CheckResult:
    """An accepted check: ``method`` is ``"totp"`` or ``"recovery"``."""

    method: str
    step: Optional[int] = None


def normalise_code(raw: str) -> str:
    """``raw`` without spaces, tabs or hyphens; a code is then 6 ASCII digits."""
    return _SEPARATORS.sub("", raw)


def normalise_recovery_code(raw: str) -> str:
    """``raw`` without spaces, tabs or hyphens, in capitals; a recovery code
    is then 10 of ``A-Z`` and ``2-7``."""
    return _SEPARATORS.sub("", raw).upper()


def new_recovery_codes() -> List[str]:
    """A new set: ``RECOVERY_CODE_COUNT`` codes of ``RECOVERY_CODE_LENGTH``."""
    return [
        "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        for _ in range(RECOVERY_CODE_COUNT)
    ]


class TwoFactorService:
    """Two-step sign-in for local accounts. ``clock`` is the only clock."""

    def __init__(
        self,
        tracker: Optional[LoginAttemptTracker] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        # ``is not None``: an empty tracker is falsy (``__len__``).
        self.tracker = tracker if tracker is not None else login_attempt_tracker
        self.clock = clock if clock is not None else time.time

    # -- reading ---------------------------------------------------------------

    def is_enabled(self, db: Session, user_id: UUID) -> bool:
        """Whether the account has confirmed two-step sign-in."""
        found = db.execute(
            select(UserTOTP.user_id).where(
                UserTOTP.user_id == user_id, UserTOTP.confirmed_at.is_not(None)
            )
        ).first()
        return found is not None

    def status(self, db: Session, user: User) -> TwoFactorStatus:
        row = db.execute(
            select(UserTOTP.confirmed_at, UserTOTP.recovery_hashes).where(
                UserTOTP.user_id == user.id
            )
        ).first()
        if row is None or row.confirmed_at is None:
            return TwoFactorStatus(enabled=False, recovery_codes_remaining=0)
        return TwoFactorStatus(
            enabled=True, recovery_codes_remaining=len(row.recovery_hashes or [])
        )

    # -- setting it up ---------------------------------------------------------

    def enrol(self, db: Session, user: User) -> Enrolment:
        """A new secret for ``user``, stored as waiting for a first code.

        A waiting secret is replaced, so the code from an earlier one no
        longer works. Refused with ``AlreadyEnabledError`` once two-step
        sign-in is on. Not audited: a waiting secret changes nothing at
        sign-in; :meth:`confirm` writes ``user_2fa_enable``.
        """
        raw = totp.new_secret()
        sealed = seal_secret(raw)
        stored = db.execute(
            pg_insert(UserTOTP)
            .values(user_id=user.id, secret_enc=sealed)
            .on_conflict_do_update(
                index_elements=[UserTOTP.user_id],
                set_={"secret_enc": sealed, "created_at": func.now()},
                where=UserTOTP.confirmed_at.is_(None),
            )
        )
        if stored.rowcount != 1:
            db.rollback()
            raise AlreadyEnabledError()
        db.commit()
        return Enrolment(
            secret=totp.secret_text(raw),
            otpauth_uri=totp.otpauth_uri(raw, user.email or user.username or ""),
        )

    def confirm(
        self, db: Session, user: User, code: str, client_host: Optional[str] = None
    ) -> List[str]:
        """Turn two-step sign-in on with a first code from the app.

        Returns a new set of recovery codes, the only time they are shown.
        A wrong code leaves the secret waiting. The code's step becomes
        ``last_used_step``, so the same code cannot then sign in.
        """
        actor = AuditActor.of(user)
        self.check_code(db, user, code=code, purpose=CONFIRM, client_host=client_host)
        codes = new_recovery_codes()
        hashes = [get_password_hash(c) for c in codes]
        db.execute(
            update(UserTOTP)
            .where(UserTOTP.user_id == user.id, UserTOTP.confirmed_at.is_(None))
            .values(confirmed_at=self._now(), recovery_hashes=hashes)
        )
        AuditService.record(
            db,
            actor=actor,
            action=ActionType.USER_2FA_ENABLE,
            entity_type=EntityType.USER,
            entity_id=user.id,
            entity_name=user.username or str(user.id),
            after={"recovery_codes": len(codes)},
        )
        db.commit()
        return codes

    # -- using it --------------------------------------------------------------

    def verify_sign_in(
        self,
        db: Session,
        user: User,
        *,
        code: Optional[str] = None,
        recovery_code: Optional[str] = None,
        client_host: Optional[str] = None,
    ) -> CheckResult:
        """The code step of a sign-in: a code, or one recovery code."""
        result = self.check_code(
            db,
            user,
            code=code,
            recovery_code=recovery_code,
            purpose=SIGN_IN,
            client_host=client_host,
        )
        db.commit()
        return result

    def disable(
        self,
        db: Session,
        user: User,
        *,
        code: Optional[str] = None,
        recovery_code: Optional[str] = None,
        client_host: Optional[str] = None,
    ) -> None:
        """Turn two-step sign-in off with a code or a recovery code."""
        actor = AuditActor.of(user)
        result = self.check_code(
            db,
            user,
            code=code,
            recovery_code=recovery_code,
            purpose=SIGNED_IN,
            client_host=client_host,
        )
        db.execute(delete(UserTOTP).where(UserTOTP.user_id == user.id))
        AuditService.record(
            db,
            actor=actor,
            action=ActionType.USER_2FA_DISABLE,
            entity_type=EntityType.USER,
            entity_id=user.id,
            entity_name=user.username or str(user.id),
            after={"second_step": result.method},
        )
        db.commit()

    def regenerate_recovery_codes(
        self, db: Session, user: User, code: str, client_host: Optional[str] = None
    ) -> List[str]:
        """A new set of recovery codes for a code; the old set stops working."""
        actor = AuditActor.of(user)
        self.check_code(db, user, code=code, purpose=SIGNED_IN, client_host=client_host)
        codes = new_recovery_codes()
        hashes = [get_password_hash(c) for c in codes]
        db.execute(
            update(UserTOTP)
            .where(UserTOTP.user_id == user.id, UserTOTP.confirmed_at.is_not(None))
            .values(recovery_hashes=hashes)
        )
        AuditService.record(
            db,
            actor=actor,
            action=ActionType.USER_2FA_RECOVERY_REGENERATE,
            entity_type=EntityType.USER,
            entity_id=user.id,
            entity_name=user.username or str(user.id),
            after={"recovery_codes": len(codes)},
        )
        db.commit()
        return codes

    # -- someone else turning it off ---------------------------------------------

    def reset(self, db: Session, users: Sequence[User], actor) -> int:
        """Delete each account's two-step sign-in, waiting or on; returns how
        many rows went.

        One ``user_2fa_reset`` entry per deleted row, against ``actor``, in
        ``db``'s transaction. Never commits: the caller's commit keeps the
        deletes and their entries together, or loses both.
        """
        by_id = {user.id: user for user in users}
        if not by_id:
            return 0
        gone = db.execute(
            delete(UserTOTP)
            .where(UserTOTP.user_id.in_(list(by_id)))
            .returning(UserTOTP.user_id, UserTOTP.confirmed_at)
        ).all()
        for user_id, confirmed_at in sorted(gone, key=lambda row: str(row[0])):
            user = by_id[user_id]
            AuditService.record(
                db,
                actor=actor,
                action=ActionType.USER_2FA_RESET,
                entity_type=EntityType.USER,
                entity_id=user_id,
                entity_name=user.username or str(user_id),
                before={"enabled": confirmed_at is not None},
            )
        return len(gone)

    # -- the one code check --------------------------------------------------------

    def check_code(
        self,
        db: Session,
        user: User,
        *,
        code: Optional[str] = None,
        recovery_code: Optional[str] = None,
        purpose: str,
        client_host: Optional[str] = None,
    ) -> CheckResult:
        """The only check of a code or a recovery code (see the module text).

        ``purpose``: ``SIGN_IN`` and ``SIGNED_IN`` need two-step sign-in on,
        ``CONFIRM`` a secret waiting for its first code. A recovery code is
        taken only for ``SIGN_IN`` and ``SIGNED_IN`` routes that offer it;
        ``CONFIRM`` takes a code only.

        A counted refusal first rolls back whatever ``db`` holds uncommitted,
        then commits the count (it must outlive the request), then raises:
        call this before changing anything else in the session. An acceptance
        is not committed: the caller commits it with whatever it does next.
        """
        if purpose not in _PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}")
        if (code is None) == (recovery_code is None):
            raise TwoFactorInputError("send exactly one of code and recovery_code")
        raw = code if code is not None else recovery_code
        if not isinstance(raw, str) or len(raw) > RAW_INPUT_MAX:
            raise TwoFactorInputError("too long")
        if recovery_code is not None and purpose == CONFIRM:
            raise TwoFactorInputError("a recovery code cannot confirm a secret")

        counted = not settings.demo_account_published
        key = self._tracker_key(db, user) if counted else None
        if key is not None:
            status = self.tracker.status(key)
            if status.locked:
                self._log_refusal(user.id, "locked", client_host)
                raise AccountLockedError(status.retry_after_seconds)

        row = db.execute(
            select(
                UserTOTP.secret_enc,
                UserTOTP.confirmed_at,
                UserTOTP.failed_attempts,
                UserTOTP.recovery_hashes,
            ).where(UserTOTP.user_id == user.id)
        ).first()
        pending = purpose == CONFIRM
        if row is None or (row.confirmed_at is None) != pending:
            raise NotEnabledError()
        if row.failed_attempts >= CODE_STEP_CAP:
            self._log_refusal(user.id, "cap", client_host)
            raise CodeStepLockedError()

        if code is not None:
            try:
                secret = open_secret(row.secret_enc)
            except SecretUnreadable:
                self._log_refusal(user.id, "unavailable", client_host)
                raise TwoFactorUnavailableError() from None
            normalised = normalise_code(code)
            step = None
            if _CODE_SHAPE.fullmatch(normalised):
                step = totp.matching_step(secret, normalised, self.clock())
            if step is None:
                self._refuse(db, user.id, key, "wrong_code", client_host)
            if not self._accept_totp_step(db, user.id, step, pending=pending):
                self._refuse_after_race(db, user.id, key, "reused_code", client_host)
            result = CheckResult(method="totp", step=step)
        else:
            normalised = normalise_recovery_code(recovery_code)
            matched = None
            if _RECOVERY_SHAPE.fullmatch(normalised):
                for hashed in row.recovery_hashes or []:
                    if verify_password(normalised, hashed):
                        matched = hashed
                        break
            if matched is None:
                self._refuse(db, user.id, key, "wrong_recovery_code", client_host)
            if self._consume_recovery_hash(db, user, matched) is None:
                self._refuse_after_race(
                    db, user.id, key, "used_recovery_code", client_host
                )
            result = CheckResult(method="recovery")

        # Only the sign-in code step clears the count it shares with the
        # password, as an accepted password does without two-step sign-in.
        if key is not None and purpose == SIGN_IN:
            self.tracker.reset(key)
        return result

    # -- the conditional writes (each one statement; the row count decides) ------

    @staticmethod
    def _accept_totp_step(
        db: Session, user_id: UUID, step: int, *, pending: bool
    ) -> bool:
        """Store ``step`` as the last one used, if it is later than the stored
        one and the code step is not capped; True if this request won."""
        state = (
            UserTOTP.confirmed_at.is_(None)
            if pending
            else UserTOTP.confirmed_at.is_not(None)
        )
        won = db.execute(
            update(UserTOTP)
            .where(
                UserTOTP.user_id == user_id,
                UserTOTP.last_used_step < step,
                UserTOTP.failed_attempts < CODE_STEP_CAP,
                state,
            )
            .values(last_used_step=step, failed_attempts=0)
        )
        return won.rowcount == 1

    @staticmethod
    def _consume_recovery_hash(db: Session, user: User, hashed: str) -> Optional[int]:
        """Remove one recovery code's hash, if it is still there and the code
        step is not capped, and record ``user_2fa_recovery_used`` in the same
        transaction. Returns how many are left if this request won, else None
        (and nothing is recorded)."""
        actor = AuditActor.of(user)
        won = db.execute(
            update(UserTOTP)
            .where(
                UserTOTP.user_id == actor.id,
                UserTOTP.recovery_hashes.has_key(hashed),
                UserTOTP.failed_attempts < CODE_STEP_CAP,
                UserTOTP.confirmed_at.is_not(None),
            )
            .values(
                recovery_hashes=UserTOTP.recovery_hashes.op("-", return_type=JSONB)(
                    cast(hashed, Text)
                ),
                failed_attempts=0,
            )
            .returning(func.jsonb_array_length(UserTOTP.recovery_hashes))
        ).first()
        if won is None:
            return None
        remaining = int(won[0])
        AuditService.record(
            db,
            actor=actor,
            action=ActionType.USER_2FA_RECOVERY_USED,
            entity_type=EntityType.USER,
            entity_id=actor.id,
            entity_name=actor.username or str(actor.id),
            after={"recovery_codes_remaining": remaining},
        )
        return remaining

    @staticmethod
    def _count_refusal(db: Session, user_id: UUID) -> None:
        """One more wrong code, added in the database."""
        db.execute(
            update(UserTOTP)
            .where(UserTOTP.user_id == user_id)
            .values(failed_attempts=UserTOTP.failed_attempts + 1)
        )

    # -- helpers --------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc)

    @staticmethod
    def _tracker_key(db: Session, user: User) -> str:
        """The shared counter's key: the database's ``lower()`` of the
        address, as ``authenticate`` and ``verify_current_password`` build it."""
        if user.email is None:
            return "\x00id:" + str(user.id)
        key = db.scalar(select(func.lower(user.email)))
        if not isinstance(key, str):
            raise TypeError("lockout counter key must be str")
        return key

    def _refuse(
        self,
        db: Session,
        user_id: UUID,
        key: Optional[str],
        reason: str,
        client_host: Optional[str],
    ) -> None:
        """Count one wrong code in both places, commit, log, raise."""
        db.rollback()
        self._count_refusal(db, user_id)
        db.commit()
        if key is not None:
            self.tracker.record_failure(key)
        self._log_refusal(user_id, reason, client_host)
        raise WrongCodeError()

    def _refuse_after_race(
        self,
        db: Session,
        user_id: UUID,
        key: Optional[str],
        reason: str,
        client_host: Optional[str],
    ) -> None:
        """A right code that lost its conditional write: the cap was reached
        meanwhile (refused as capped), or the code was used (counted)."""
        db.rollback()
        failed = db.scalar(
            select(UserTOTP.failed_attempts).where(UserTOTP.user_id == user_id)
        )
        if failed is not None and failed >= CODE_STEP_CAP:
            self._log_refusal(user_id, "cap", client_host)
            raise CodeStepLockedError()
        self._refuse(db, user_id, key, reason, client_host)

    @staticmethod
    def _log_refusal(user_id: UUID, reason: str, client_host: Optional[str]) -> None:
        logger.warning(
            "two_factor_refused user_id=%s reason=%s client_host=%s",
            user_id,
            reason,
            client_host or "-",
            extra={
                "event": "two_factor_refused",
                "user_id": str(user_id),
                "reason": reason,
                "client_host": client_host,
            },
        )


two_factor_service = TwoFactorService()

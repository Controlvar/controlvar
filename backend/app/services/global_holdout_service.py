"""
Global Holdout Service for managing user holdout groups (EP-022, #445).

Keeps a fixed percentage of new users out of every experiment, so they can be
compared with everyone else first seen while the holdout was active.

The lifecycle has one path for activation, used by ``POST`` and ``PUT``
alike:

* activating stamps ``activated_at`` (never on a legacy-salt row) and
  deactivates every *other* active holdout, stamping their ``deactivated_at``;
  activating the holdout that is already active changes nothing;
* deactivating stamps ``deactivated_at``.  An ended holdout cannot restart
  (``HoldoutEnded``);
* the percentage cannot change once the holdout has been active
  (``HoldoutPercentageLocked``); re-sending the same value is accepted;
* two activations racing each other: the database's one-active index refuses
  the second (``HoldoutActivationConflict``);
* a name another holdout has is refused (``HoldoutNameTaken``), by a check
  before the write and, for two writes that race, by the name's unique
  constraint (#1019).

Membership is one function, ``holdout_bucket(user_id, salt)``, used by
``/holdout/check`` and ``AssignmentService.assign_user`` alike, with the
holdout's own ``hash_salt``.
"""

import hashlib
import logging
import struct
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.global_holdout import (
    LEGACY_HOLDOUT_SALT,
    ONE_ACTIVE_INDEX,
    GlobalHoldout,
)

logger = logging.getLogger(__name__)

#: The salt every holdout used before #445, and the one the static, DB-free
#: helpers still use.  A holdout's own ``hash_salt`` decides its buckets.
HOLDOUT_SALT = LEGACY_HOLDOUT_SALT

#: Passed for ``holdout`` to mean "look the active holdout up".
_LOOK_UP = object()

PERCENTAGE_LOCKED_DETAIL = (
    "holdout_percentage cannot change once a holdout has been active: it would "
    "move users between the holdout and the rest and mix the two groups. "
    "Create a new holdout instead."
)
ENDED_DETAIL = "A holdout that has ended cannot restart. Create a new holdout."
ACTIVATION_CONFLICT_DETAIL = "Another holdout was activated at the same time; retry."


class HoldoutPercentageLocked(ValueError):
    """The percentage of a holdout that has been active cannot change."""

    def __init__(self) -> None:
        super().__init__(PERCENTAGE_LOCKED_DETAIL)


class HoldoutEnded(ValueError):
    """A holdout that has been deactivated cannot be activated again."""

    def __init__(self) -> None:
        super().__init__(ENDED_DETAIL)


class HoldoutActivationConflict(Exception):
    """Another holdout became active concurrently (the one-active index refused)."""

    def __init__(self) -> None:
        super().__init__(ACTIVATION_CONFLICT_DETAIL)


#: SQLSTATE ``unique_violation``.
_UNIQUE_VIOLATION = "23505"

#: The unique constraint on ``global_holdouts.name``. It is unnamed in the model
#: and the migration, so PostgreSQL names it ``<table>_<column>_key``, the same
#: in every schema. It compares names exactly: "Q4" and "q4" are two names.
NAME_UNIQUE_CONSTRAINT = "global_holdouts_name_key"


def holdout_name_taken_detail(name: str) -> str:
    """The 409 detail for a name another holdout has (#1019)."""
    return f"A global holdout with the name '{name}' already exists."


class HoldoutNameTaken(Exception):
    """Another holdout already has this name (#1019)."""

    def __init__(self, name: str) -> None:
        super().__init__(holdout_name_taken_detail(name))


def _is_name_violation(error: IntegrityError) -> bool:
    """True when *error* is the unique constraint on the name refusing a write.

    Decided from the driver's diagnostics (SQLSTATE, table, constraint), never
    from the message text; the one-active index and every other integrity
    error are not this one.
    """
    orig = getattr(error, "orig", None)
    if getattr(orig, "pgcode", None) != _UNIQUE_VIOLATION:
        return False
    diag = getattr(orig, "diag", None)
    return (
        getattr(diag, "table_name", None) == GlobalHoldout.__tablename__
        and getattr(diag, "constraint_name", None) == NAME_UNIQUE_CONSTRAINT
    )


def holdout_bucket(user_id: str, salt: str) -> int:
    """The user's holdout bucket, 0-99, under ``salt``.

    ``md5("<user_id>:<salt>")``, first four bytes little-endian, modulo 100:
    with the legacy salt this is exactly the bucket every pre-#445 holdout
    used.
    """
    combined = f"{user_id}:{salt}".encode("utf-8")
    hash_bytes = hashlib.md5(combined, usedforsecurity=False).digest()[:4]
    hash_int = struct.unpack("<I", hash_bytes)[0]
    return hash_int % 100


def _utcnow() -> datetime:
    """Naive UTC, like ``BaseModel.created_at``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_been_active(holdout: GlobalHoldout) -> bool:
    return (
        bool(holdout.is_active)
        or holdout.activated_at is not None
        or holdout.deactivated_at is not None
    )


def _is_one_active_violation(error: IntegrityError) -> bool:
    return ONE_ACTIVE_INDEX in str(getattr(error, "orig", error))


class GlobalHoldoutService:
    """Service for managing global holdout configuration and user checks."""

    MAX_HASH_VALUE = 0xFFFFFFFF

    def __init__(self, db: Session):
        self.db = db
        #: (id, name) of every other holdout ``_activate`` turned off through
        #: this service object, so the route can record each one.
        self.implicitly_deactivated: List[Tuple[UUID, str]] = []

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_holdout(
        self,
        name: str,
        description: Optional[str] = None,
        holdout_percentage: int = 10,
        is_active: bool = False,
        owner_id: Optional[UUID] = None,
        hash_salt: Optional[str] = None,
    ) -> GlobalHoldout:
        """Create a new global holdout; ``is_active=True`` activates it.

        Activation goes through the same path as ``update_holdout``: the
        other active holdout is deactivated and this one's ``activated_at``
        is stamped.  ``hash_salt`` is left out everywhere but the demo seed,
        which fixes its holdout's salt so its documented device ids stay in or
        out across re-seeds; the model's default gives every other holdout a
        fresh one.  The legacy salt is refused: a holdout with it is never
        measurable.  Raises ``HoldoutNameTaken`` when another holdout has
        ``name``; nothing is written then.
        """
        if hash_salt == LEGACY_HOLDOUT_SALT:
            raise ValueError("a new holdout cannot use the legacy salt")
        if self._name_taken(name):
            raise HoldoutNameTaken(name)
        holdout = GlobalHoldout(
            name=name,
            description=description,
            holdout_percentage=holdout_percentage,
            is_active=False,
            owner_id=owner_id,
        )
        if hash_salt is not None:
            holdout.hash_salt = hash_salt
        with self._name_guard(name):
            self.db.add(holdout)
            if is_active:
                self.db.flush()
                self._activate(holdout)
            self._commit()
        self.db.refresh(holdout)
        return holdout

    def get_holdout(self, holdout_id: UUID) -> Optional[GlobalHoldout]:
        """Get a holdout by ID."""
        return (
            self.db.query(GlobalHoldout).filter(GlobalHoldout.id == holdout_id).first()
        )

    def get_active_holdout(self) -> Optional[GlobalHoldout]:
        """Get the active global holdout (the database allows at most one)."""
        return (
            self.db.query(GlobalHoldout).filter(GlobalHoldout.is_active == True).first()
        )

    def list_holdouts(self, skip: int = 0, limit: int = 100) -> List[GlobalHoldout]:
        """List all holdouts."""
        return self.db.query(GlobalHoldout).offset(skip).limit(limit).all()

    def count_holdouts(self) -> int:
        """Count all holdouts."""
        return self.db.query(GlobalHoldout).count()

    def update_holdout(
        self,
        holdout_id: UUID,
        name: Optional[str] = None,
        description: Optional[str] = None,
        holdout_percentage: Optional[int] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[GlobalHoldout]:
        """Update a holdout configuration.

        Raises ``HoldoutPercentageLocked`` for a different percentage on a
        holdout that has been active, ``HoldoutEnded`` for ``is_active=True``
        on one that has ended, ``HoldoutNameTaken`` for a name another holdout
        has, and ``HoldoutActivationConflict`` when another holdout became
        active concurrently.  Nothing is changed when any of them is raised.
        """
        holdout = self.get_holdout(holdout_id)
        if not holdout:
            return None

        # Every refusal is decided before anything changes.
        if (
            holdout_percentage is not None
            and holdout_percentage != holdout.holdout_percentage
            and _has_been_active(holdout)
        ):
            raise HoldoutPercentageLocked()
        if is_active and not holdout.is_active and holdout.deactivated_at is not None:
            raise HoldoutEnded()
        if (
            name is not None
            and name != holdout.name
            and self._name_taken(name, holdout.id)
        ):
            raise HoldoutNameTaken(name)

        with self._name_guard(name):
            if name is not None:
                holdout.name = name
            if description is not None:
                holdout.description = description
            if holdout_percentage is not None:
                holdout.holdout_percentage = holdout_percentage
            if is_active is True:
                self._activate(holdout)
            elif is_active is False:
                self._deactivate(holdout)

            self._commit()
        self.db.refresh(holdout)
        return holdout

    def activate_holdout(self, holdout_id: UUID) -> Optional[GlobalHoldout]:
        """Activate a holdout (deactivates any other active holdout)."""
        return self.update_holdout(holdout_id, is_active=True)

    def deactivate_holdout(self, holdout_id: UUID) -> Optional[GlobalHoldout]:
        """Deactivate a holdout; it has then ended and cannot restart."""
        return self.update_holdout(holdout_id, is_active=False)

    # ------------------------------------------------------------------
    # User Holdout Check
    # ------------------------------------------------------------------

    def is_user_in_holdout(
        self, user_id: str, holdout=_LOOK_UP
    ) -> Tuple[bool, int, int]:
        """
        Check if a user is in the global holdout.

        ``holdout`` is the active holdout when the caller has already loaded
        it (``None`` for "none is active"); left out, it is looked up.  The
        bucket uses that holdout's own ``hash_salt``.

        Returns:
            Tuple of (is_in_holdout, holdout_percentage, bucket)
        """
        if holdout is _LOOK_UP:
            holdout = self.get_active_holdout()
        if not holdout:
            return (False, 0, 0)

        bucket = holdout_bucket(user_id, holdout.hash_salt)
        is_in = bucket < holdout.holdout_percentage
        return (is_in, holdout.holdout_percentage, bucket)

    @staticmethod
    def is_user_in_holdout_static(
        user_id: str, holdout_percentage: int, salt: str = HOLDOUT_SALT
    ) -> Tuple[bool, int]:
        """
        Static version for Lambda use — no DB dependency.

        Returns:
            Tuple of (is_in_holdout, bucket)
        """
        bucket = holdout_bucket(user_id, salt)
        return (bucket < holdout_percentage, bucket)

    @staticmethod
    def _get_holdout_bucket_static(user_id: str) -> int:
        """Holdout bucket (0-99) under the legacy salt. Static version."""
        return holdout_bucket(user_id, HOLDOUT_SALT)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _activate(self, holdout: GlobalHoldout) -> None:
        """Make ``holdout`` the active one.

        Already active: nothing changes, timestamps included.  Otherwise every
        other active holdout is deactivated (and stamped) first, then this one
        is activated; ``activated_at`` is stamped only on a non-legacy row that
        has none, so a legacy-salt holdout never becomes measurable.
        """
        if holdout.is_active:
            return
        now = _utcnow()
        others = (
            self.db.query(GlobalHoldout)
            .filter(GlobalHoldout.is_active == True, GlobalHoldout.id != holdout.id)
            .all()
        )
        for other in others:
            other.is_active = False
            other.deactivated_at = now
            self.implicitly_deactivated.append((other.id, other.name))
        # The others are written before this row turns active, so the
        # one-active index sees at most one active row at every statement.
        self._flush()
        holdout.is_active = True
        if holdout.hash_salt != LEGACY_HOLDOUT_SALT and holdout.activated_at is None:
            holdout.activated_at = now
        self._flush()

    def _deactivate(self, holdout: GlobalHoldout) -> None:
        """End an active holdout; a holdout that is not active is left as it is."""
        if not holdout.is_active:
            return
        holdout.is_active = False
        holdout.deactivated_at = _utcnow()

    def _flush(self) -> None:
        try:
            self.db.flush()
        except IntegrityError as error:
            self.db.rollback()
            if _is_one_active_violation(error):
                raise HoldoutActivationConflict() from error
            raise

    def _name_taken(self, name: str, exclude_id: Optional[UUID] = None) -> bool:
        """Whether a holdout other than ``exclude_id`` has ``name``, compared as
        the unique constraint compares it (exactly). A check before the write;
        the constraint itself is the guarantee (``_name_guard``)."""
        query = self.db.query(GlobalHoldout.id).filter(GlobalHoldout.name == name)
        if exclude_id is not None:
            query = query.filter(GlobalHoldout.id != exclude_id)
        return query.first() is not None

    @contextmanager
    def _name_guard(self, name: Optional[str]) -> Iterator[None]:
        """The name's unique constraint refusing a write inside is ``HoldoutNameTaken``.

        A request that wrote the same name after this one's ``_name_taken``
        is refused here, at whichever flush or commit reaches the database.
        The session is rolled back first, so it stays usable; any other
        integrity error is raised as it was (``HoldoutActivationConflict`` is
        not an integrity error and passes through).
        """
        try:
            yield
        except IntegrityError as error:
            self.db.rollback()
            if name is not None and _is_name_violation(error):
                raise HoldoutNameTaken(name) from error
            raise

    def _commit(self) -> None:
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            if _is_one_active_violation(error):
                raise HoldoutActivationConflict() from error
            raise

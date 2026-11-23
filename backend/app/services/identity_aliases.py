"""The rules for ids linked as one person (#449). The one definition.

``identity_aliases`` links an anonymous id to the known id of the same person:
one row per anonymous id. This module is the only reader and writer of that
table, as ``event_matching`` is the one definition of a conversion.

The rules:

* **One link per anonymous id.** An anonymous id is linked to at most one known
  id and is never moved to another; a known id has at most
  :data:`MAX_ANONYMOUS_IDS_PER_USER` anonymous ids. Links are one level deep:
  an id that has anonymous ids linked to it is never itself linked, and an id
  linked as an anonymous id never has ids linked to it.
* **The person of an id ``p``.** ``k`` is the known id of the link whose
  anonymous id is ``p``, or ``p`` itself when there is none. The person's ids
  are ``k`` and every anonymous id linked to ``k``. The person is *linked*
  when that is more than one id. :func:`person_of` reads it in one statement.
* **Which id holds an experiment** (:func:`holder_of`): of the person's ids,
  the one whose assignment row in the experiment was stored first, by
  ``created_at`` and then by the row's id. When none of them holds a row, the
  holder is ``k``, and a new assignment for the person is made under ``k``.
  Rows that existed before a link all stay; the person is answered with the
  earliest one.
* ``person_of(...).ids`` is the set of ids whose rows belong to one person:
  the set an erasure of that person covers.

Nothing here writes to any table but ``identity_aliases``, and no id appears
in a log line or in an exception's text: :class:`AliasConflict` carries a
reason name only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple, Union
from uuid import UUID

from sqlalchemy import func, insert, or_, select, text
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.identity_alias import IdentityAlias

logger = logging.getLogger(__name__)

#: The most anonymous ids one known id may have linked to it.
MAX_ANONYMOUS_IDS_PER_USER = 100

#: The first key of every advisory lock :func:`link` takes, so its two-key
#: locks never meet the one-key locks the rest of the code base takes.
LOCK_SPACE = 449

#: :class:`AliasConflict` reasons, one per rule a link can break.
ANONYMOUS_ID_LINKED_ELSEWHERE = "anonymous_id_linked_to_another_user_id"
ANONYMOUS_ID_HAS_LINKS = "anonymous_id_has_links"
USER_ID_IS_LINKED = "user_id_is_linked_as_anonymous_id"
USER_ID_AT_CAP = "user_id_at_cap"
CONFLICT_REASONS = (
    ANONYMOUS_ID_LINKED_ELSEWHERE,
    ANONYMOUS_ID_HAS_LINKS,
    USER_ID_IS_LINKED,
    USER_ID_AT_CAP,
)

_ALIASES = IdentityAlias.__table__


class AliasConflict(Exception):
    """A link the rules refuse. ``reason`` is one of :data:`CONFLICT_REASONS`.

    The text is the reason only, never either id.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Person:
    """The ids of one person: ``known_id`` first, then its anonymous ids."""

    known_id: str
    ids: Tuple[str, ...]

    @property
    def linked(self) -> bool:
        return len(self.ids) > 1


def person_of(db: Session, user_id: str) -> Person:
    """The person *user_id* belongs to, in one statement.

    It reads every link whose known id is *user_id*, or is the known id
    *user_id* is linked to, at most ``MAX_ANONYMOUS_IDS_PER_USER + 1`` rows.
    An id never linked is a person of one id, ``Person(user_id, (user_id,))``.
    """
    known = _ALIASES.alias("known")
    known_id_of_user = (
        select(known.c.user_id).where(known.c.anonymous_id == user_id).scalar_subquery()
    )
    rows = (
        db.query(_ALIASES.c.anonymous_id, _ALIASES.c.user_id)
        .filter(
            or_(_ALIASES.c.user_id == user_id, _ALIASES.c.user_id == known_id_of_user)
        )
        .limit(MAX_ANONYMOUS_IDS_PER_USER + 1)
        .all()
    )
    if not rows:
        return Person(user_id, (user_id,))
    known_id = next(
        (row.user_id for row in rows if row.anonymous_id == user_id), user_id
    )
    anonymous_ids = sorted(row.anonymous_id for row in rows if row.user_id == known_id)
    return Person(known_id, (known_id, *anonymous_ids))


def holder_of(
    db: Session, person: Person, experiment_id: Union[str, UUID]
) -> Tuple[str, Optional[Assignment]]:
    """``(holder, row)`` for a linked *person* in an experiment, in one statement.

    ``row`` is the person's assignment stored first in the experiment (by
    ``created_at``, then by the row's id) and ``holder`` its user id; with no
    row among the person's ids, ``(person.known_id, None)``. Callers read an
    id never linked as they always have; this is for linked persons.
    """
    row = (
        db.query(Assignment)
        .filter(
            Assignment.experiment_id == experiment_id,
            Assignment.user_id.in_(person.ids),
        )
        .order_by(Assignment.created_at, Assignment.id)
        .first()
    )
    if row is None:
        return person.known_id, None
    return row.user_id, row


def lock_keys(hashes: Iterable[int]) -> List[int]:
    """The advisory-lock keys of :func:`link`, in the order it takes them.

    The ids' ``hashtext`` values, de-duplicated, in ascending numeric order.
    Never the ids' text order: two different ids can share a ``hashtext``
    value, and two links that take one shared key in opposite orders
    deadlock.
    """
    return sorted(set(hashes))


def link(db: Session, anonymous_id: str, user_id: str) -> bool:
    """Link *anonymous_id* to *user_id*; True if this call stored the link.

    One transaction: it takes ``pg_advisory_xact_lock(LOCK_SPACE, k)`` for
    each key of :func:`lock_keys`, reads the links touching either id, decides,
    inserts and commits. A link that already exists answers False and writes
    nothing. A link the rules refuse rolls back and raises
    :class:`AliasConflict`:

    * the anonymous id is linked to a different known id;
    * the anonymous id has anonymous ids of its own;
    * the known id is itself linked as an anonymous id;
    * the known id already has ``MAX_ANONYMOUS_IDS_PER_USER`` anonymous ids.

    The table's check constraint refuses two equal ids. The locks make the
    one-level and cap rules exact under concurrent calls; the primary key on
    ``anonymous_id`` makes "linked once" exact.
    """
    try:
        hashes = db.execute(
            text("SELECT hashtext(:anonymous_id), hashtext(:user_id)"),
            {"anonymous_id": anonymous_id, "user_id": user_id},
        ).one()
        for key in lock_keys(hashes):
            db.execute(
                text("SELECT pg_advisory_xact_lock(:space, :key)"),
                {"space": LOCK_SPACE, "key": key},
            )
        if _decide(db, anonymous_id, user_id):
            db.commit()
            return False
        db.execute(insert(_ALIASES).values(anonymous_id=anonymous_id, user_id=user_id))
        db.commit()
        return True
    except AliasConflict as conflict:
        db.rollback()
        # The rule's name only, never an id.
        logger.debug("Link refused: %s", conflict.reason)
        raise
    except BaseException:
        db.rollback()
        raise


def _decide(db: Session, anonymous_id: str, user_id: str) -> bool:
    """True when the link already exists; raises :class:`AliasConflict` when
    the rules refuse it; False when it may be stored."""
    linked_to = db.execute(
        select(_ALIASES.c.user_id).where(_ALIASES.c.anonymous_id == anonymous_id)
    ).scalar_one_or_none()
    if linked_to is not None:
        if linked_to == user_id:
            return True
        raise AliasConflict(ANONYMOUS_ID_LINKED_ELSEWHERE)
    if _exists(db, _ALIASES.c.user_id == anonymous_id):
        raise AliasConflict(ANONYMOUS_ID_HAS_LINKS)
    if _exists(db, _ALIASES.c.anonymous_id == user_id):
        raise AliasConflict(USER_ID_IS_LINKED)
    linked = db.execute(
        select(func.count()).select_from(_ALIASES).where(_ALIASES.c.user_id == user_id)
    ).scalar_one()
    if linked >= MAX_ANONYMOUS_IDS_PER_USER:
        raise AliasConflict(USER_ID_AT_CAP)
    return False


def _exists(db: Session, criterion) -> bool:
    return (
        db.execute(select(_ALIASES.c.anonymous_id).where(criterion).limit(1)).first()
        is not None
    )

"""Erase one app user's data (#2042, beta).

An *app user* is an id your app sends as ``user_id`` (or as an id-list
segment's ``member_id``): a person your app tracks, not a dashboard account.
``POST /api/v1/admin/app-users/erase`` calls :func:`erase_app_user`, and every
rule of the erase is here:

* :data:`ERASED_TABLES` names each table that stores such an id, its id
  column, and the order the tables are erased in. The delete reads it, and so
  does the inventory gate
  (``backend/tests/unit/services/test_app_user_erase_inventory.py``), which
  classifies every other table as kept, so the two cannot drift.
* :func:`erasure_ids` is the set of ids one erase covers: the whole person of
  the id sent (#449), ``person_of(id).ids`` read without its row limit, so a
  person linked past the cap by other means is still erased whole. It is read
  before anything is deleted: once the links are gone the set is the one id.
* **Links last.** ``identity_aliases`` is erased in the final transaction, and
  only when every other table is done. That transaction first takes
  ``pg_advisory_xact_lock(LOCK_SPACE, k)`` for each of ``lock_keys`` of the
  set's ``hashtext`` values, one statement per key in ascending order: the
  keys and order :func:`backend.app.services.identity_aliases.link` takes,
  imported, so the two never wait on each other in a cycle. It then reads the
  set again: if a link added an id, no link is deleted and the answer is
  ``complete: false`` (the next call's set has the new id). Through ``link`` a
  set has at most 101 ids; a set of thousands written by hand needs as many
  keys, can exhaust the server's shared memory for them, and the final
  transaction then fails (500, start entry only).
* **The record** is two ``audit_logs`` entries per call with the same
  ``entity_id``, the response's ``receipt_id``. The start entry
  (``app_user_erase_started``) is committed before anything is deleted; if it
  cannot be written, nothing is deleted. The final entry
  (``app_user_erased``) is written last, in its own transaction, with this
  call's counts: ``{"deleted": {table: n}, "ids_erased": n, "complete": b}``.
  Neither holds the id. A start entry with no final entry is a call that did
  not finish: some rows may be gone, or none.
* **Bounded.** Each table is deleted :data:`CHUNK_ROWS` rows at a time, each
  chunk its own transaction under ``statement_timeout`` (at most
  :data:`CHUNK_TIMEOUT_MS`) and ``enable_seqscan = off``, so every statement
  reads through an index whatever the planner's statistics say. No
  ``lock_timeout`` is set, so a chunk that waits for a row ends as a
  statement timeout (57014). A timed-out chunk is rolled back, the chunk
  phase ends, and the answer is ``complete: false``: not an error. The chunk
  phase stops when fewer than :data:`MIN_CHUNK_MS` of its budget are left;
  :data:`FINAL_RESERVE_S` of :data:`REQUEST_BUDGET_S` is kept for the final
  transaction. Time is read from :data:`_clock` (``time.monotonic``), never
  from the wall clock.
* **Done means checked.** A table counts as done only when its last
  statement (a chunk shorter than :data:`CHUNK_ROWS`, or the one statement
  through the parent's primary key) is followed by an existence check that
  finds no row left.
* **Statement shapes.** A table with an ``id`` column: ``id IN (SELECT id ...
  WHERE <column> IN (:ids) LIMIT n)``. ``event_dedup_keys``: its primary key
  ``(user_id, dedup_key) IN (SELECT ... LIMIT n)``. ``holdout_population``,
  ``segment_members`` and ``feature_flag_overrides``, whose only index on the
  id is the second column of ``(parent, id)``: one unchunked statement through
  the parent's primary key, ``<parent> IN (SELECT id FROM <parent table>) AND
  <column> IN (:ids)`` (a person has at most one row per parent).

Nothing here touches Redis, DynamoDB or any table outside
:data:`ERASED_TABLES` but ``audit_logs``; nothing logs the id.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, literal_column, or_, select, text, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.event import Event
from backend.app.models.event_dedup_key import EventDedupKey
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagOverride
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.identity_alias import IdentityAlias
from backend.app.models.llm_experiment import LLMEvaluation
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.segment import Segment, SegmentMember
from backend.app.services.audit_service import AuditActor, AuditService
from backend.app.services.identity_aliases import LOCK_SPACE, lock_keys

logger = logging.getLogger(__name__)

#: Rows deleted per chunk transaction.
CHUNK_ROWS = 1000
#: The most one chunk's statements may take, in milliseconds.
CHUNK_TIMEOUT_MS = 2000
#: The most one call works for, in seconds.
REQUEST_BUDGET_S = 20
#: Seconds of :data:`REQUEST_BUDGET_S` kept for the final transaction.
FINAL_RESERVE_S = 2
#: The chunk phase stops when fewer milliseconds than this are left.
MIN_CHUNK_MS = 50

#: SQLSTATEs that end the chunk phase with ``complete: false``: a statement
#: timeout, and a lock timeout should one ever be set.
STOPPED_EARLY = frozenset({"57014", "55P03"})

#: ``entity_name`` of both entries: fixed text, never the id.
ENTRY_NAME = "app user"

#: The clock of the budget. Tests replace this name, never ``time.monotonic``.
_clock = time.monotonic


@dataclass(frozen=True)
class ErasedTable:
    """A table that stores an app user's id, and how its rows are deleted.

    ``parent`` is ``(column, parent model)`` for a table whose only index on
    the id leads with that column: its rows are deleted in one statement
    through the parent's primary key. ``also`` names more id columns, matched
    with ``column``. A ``last`` table is erased in the final transaction, after
    every other table is done, never in the chunk phase.
    """

    model: Any
    column: str
    parent: Optional[Tuple[str, Any]] = None
    also: Tuple[str, ...] = ()
    last: bool = False

    @property
    def name(self) -> str:
        return self.model.__table__.name

    @property
    def columns(self) -> Tuple[str, ...]:
        return (self.column, *self.also)


#: Every table that stores an app user's id, in the order they are erased.
#: ``event_dedup_keys`` comes after ``events``: a call that ends between them
#: leaves keys, which only refuse an ``event_id`` sent again. The links come
#: last: a call that ends before them leaves the person's set whole for the
#: next one.
ERASED_TABLES: Tuple[ErasedTable, ...] = (
    ErasedTable(Event, "user_id"),
    ErasedTable(RawMetric, "user_id"),
    ErasedTable(ErrorLog, "user_id"),
    ErasedTable(LLMEvaluation, "user_id"),
    ErasedTable(EventDedupKey, "user_id"),
    ErasedTable(Assignment, "user_id"),
    ErasedTable(HoldoutPopulation, "user_id", ("holdout_id", GlobalHoldout)),
    ErasedTable(SegmentMember, "member_id", ("segment_id", Segment)),
    ErasedTable(FeatureFlagOverride, "user_id", ("feature_flag_id", FeatureFlag)),
    ErasedTable(IdentityAlias, "user_id", also=("anonymous_id",), last=True),
)


@dataclass
class EraseResult:
    """What one call did: the response's fields."""

    receipt_id: UUID
    complete: bool
    ids_erased: int
    deleted: Dict[str, int] = field(default_factory=dict)


class EraseFailed(Exception):
    """An unplanned failure; ``cause`` is what was raised. The text is the
    cause's type name only."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause


class EraseNotStarted(EraseFailed):
    """The start entry could not be committed, so nothing was deleted."""


class EraseNotFinished(EraseFailed):
    """A failure after the start entry: part of the data may be erased."""


def erasure_ids(db: Session, user_id: str) -> Tuple[str, ...]:
    """The ids one erase of *user_id* covers: its person, matched exactly.

    The known id ``k`` is the one *user_id* is linked to as an anonymous id,
    or *user_id* itself; the set is ``k`` and every anonymous id linked to
    ``k``, in the order ``person_of`` gives them: ``person_of(user_id).ids``,
    read with no row limit. An id never linked is a set of one.
    """
    aliases = IdentityAlias.__table__
    known = db.execute(
        select(aliases.c.user_id).where(aliases.c.anonymous_id == user_id)
    ).scalar_one_or_none()
    known = user_id if known is None else known
    linked = (
        db.execute(select(aliases.c.anonymous_id).where(aliases.c.user_id == known))
        .scalars()
        .all()
    )
    return (known, *sorted(linked))


def statements(table: ErasedTable, ids: Sequence[str]) -> Tuple[Any, Any]:
    """``(delete, check)`` for *table* and *ids*.

    ``delete`` removes at most :data:`CHUNK_ROWS` rows (all of them, through
    the parent's primary key, for a table with a ``parent``, and for a
    ``last`` table); ``check`` selects one row that is left, if any.
    """
    t = table.model.__table__
    match = or_(*(t.c[column].in_(list(ids)) for column in table.columns))
    if table.last:
        return (
            delete(t).where(match),
            select(literal_column("1")).select_from(t).where(match).limit(1),
        )
    if table.parent is not None:
        parent_column, parent_model = table.parent
        where = and_(t.c[parent_column].in_(select(parent_model.__table__.c.id)), match)
        return (
            delete(t).where(where),
            select(literal_column("1")).select_from(t).where(where).limit(1),
        )
    keys = list(t.primary_key.columns)
    chosen = select(*keys).where(match).limit(CHUNK_ROWS)
    where = keys[0].in_(chosen) if len(keys) == 1 else tuple_(*keys).in_(chosen)
    return (
        delete(t).where(where),
        select(literal_column("1")).select_from(t).where(match).limit(1),
    )


def apply_settings(db: Session, timeout_ms: int) -> None:
    """The transaction's settings, before any statement on an erased table.

    ``set_config(..., true)`` is ``SET LOCAL``: both end with the transaction.
    """
    db.execute(
        select(
            func.set_config("statement_timeout", str(timeout_ms), True),
            func.set_config("enable_seqscan", "off", True),
        )
    )


def _ms_left(until: float) -> float:
    return (until - _clock()) * 1000.0


def _clamped(ms: float, cap_ms: int) -> int:
    """*ms* as a ``statement_timeout``: within ``[1, cap_ms]``.

    ``0`` would turn the timeout off and a negative value is refused by the
    server, so neither is ever sent.
    """
    return max(1, min(cap_ms, int(ms)))


def _stopped_early(exc: BaseException) -> bool:
    return getattr(getattr(exc, "orig", None), "pgcode", None) in STOPPED_EARLY


def _erase_table(
    db: Session, table: ErasedTable, ids: Sequence[str], until: float
) -> Tuple[int, str]:
    """Delete *table*'s rows for *ids* until none is left or time runs out.

    Returns ``(rows deleted, how it ended)``: ``"done"`` when the check found
    nothing left, ``"budget"`` when time ran out, ``"timeout"`` when a
    statement timed out (that chunk is rolled back).
    """
    delete_rows, check = statements(table, ids)
    deleted = 0
    while True:
        ms = _ms_left(until)
        if ms < MIN_CHUNK_MS:
            return deleted, "budget"
        try:
            apply_settings(db, _clamped(ms, CHUNK_TIMEOUT_MS))
            n = db.execute(delete_rows).rowcount
            last = table.parent is not None or n < CHUNK_ROWS
            left = last and db.execute(check).first() is not None
            db.commit()
        except DBAPIError as exc:
            if not _stopped_early(exc):
                raise
            db.rollback()
            logger.warning("App user erase stopped early (%s)", type(exc).__name__)
            return deleted, "timeout"
        deleted += n
        if last and not left:
            return deleted, "done"


def advisory_keys(db: Session, ids: Sequence[str]) -> list:
    """``link``'s keys for *ids*: their ``hashtext`` values through the
    imported ``lock_keys`` (de-duplicated, ascending)."""
    hashes = db.execute(
        text("SELECT hashtext(id) FROM unnest(CAST(:ids AS text[])) AS id"),
        {"ids": list(ids)},
    ).scalars()
    return lock_keys(hashes)


def _erase_links(
    db: Session, user_id: str, ids: Sequence[str], deleted: Dict[str, int]
) -> bool:
    """Delete the person's links in the final transaction; True if done.

    Takes ``link``'s key for each id, one statement per key in ascending
    order, then reads the set again: a set that changed since the call read
    it (a link added an id) deletes no link and answers False.
    """
    for key in advisory_keys(db, ids):
        db.execute(
            text("SELECT pg_advisory_xact_lock(:space, :key)"),
            {"space": LOCK_SPACE, "key": key},
        )
    if set(erasure_ids(db, user_id)) != set(ids):
        return False
    for table in ERASED_TABLES:
        if table.last:
            delete_rows, _ = statements(table, ids)
            deleted[table.name] += db.execute(delete_rows).rowcount
    return True


def erase_app_user(db: Session, user_id: str, actor: Any) -> EraseResult:
    """Erase *user_id*'s rows from every table in :data:`ERASED_TABLES`.

    Raises :class:`EraseNotStarted` when the start entry could not be
    committed (nothing was deleted), and :class:`EraseNotFinished` on any
    unplanned failure after it, the final transaction's included.
    """
    who = AuditActor.of(actor)
    receipt_id = uuid4()
    deadline = _clock() + REQUEST_BUDGET_S
    try:
        AuditService.record(
            db,
            actor=who,
            action=ActionType.APP_USER_ERASE_STARTED,
            entity_type=EntityType.APP_USER,
            entity_id=receipt_id,
            entity_name=ENTRY_NAME,
        )
        db.commit()
    except Exception as exc:
        raise EraseNotStarted(exc) from None

    try:
        # The person, before anything is deleted: afterwards it is one id.
        ids = erasure_ids(db, user_id)
        deleted = {table.name: 0 for table in ERASED_TABLES}
        complete = True
        chunk_deadline = deadline - FINAL_RESERVE_S
        for table in ERASED_TABLES:
            if table.last:
                continue
            n, ended = _erase_table(db, table, ids, chunk_deadline)
            deleted[table.name] += n
            if ended != "done":
                complete = False
                break

        apply_settings(db, _clamped(_ms_left(deadline), FINAL_RESERVE_S * 1000))
        if complete:
            complete = _erase_links(db, user_id, ids, deleted)
        AuditService.record(
            db,
            actor=who,
            action=ActionType.APP_USER_ERASED,
            entity_type=EntityType.APP_USER,
            entity_id=receipt_id,
            entity_name=ENTRY_NAME,
            after={"deleted": deleted, "ids_erased": len(ids), "complete": complete},
        )
        db.commit()
    except Exception as exc:
        raise EraseNotFinished(exc) from None

    logger.info(
        "App user erase %s finished: complete=%s, %d rows",
        receipt_id,
        complete,
        sum(deleted.values()),
    )
    return EraseResult(
        receipt_id=receipt_id,
        complete=complete,
        ids_erased=len(ids),
        deleted=deleted,
    )

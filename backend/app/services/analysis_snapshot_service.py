"""
Best-effort persistence of analysis results (``analysis_snapshots``).

The results endpoints call :func:`record_snapshot` after they have computed a
response; the bandit scheduler stages :func:`build_snapshot` rows in its own
transaction.  A snapshot is an audit artefact, not part of the contract with
the caller, so **a failure to write one must never fail the request**: every
error is logged at WARNING and swallowed.

Transaction ownership
---------------------
:func:`record_snapshot` writes in a **short-lived session of its own**, opened
on the caller's engine (``db.get_bind()``) and closed before it returns.  The
request session is never committed or rolled back on its behalf.  This matters
on the read endpoints: ``AnalysisService.compute_bayesian_results`` leaves
``experiment.bayesian_decision`` *flushed but not committed*, so a
``db.commit()`` from an audit write would silently persist it from a GET, and
the old failure path's ``db.rollback()`` would throw away whatever else the
caller had pending.

One row per experiment, kind and day
------------------------------------
``as_of`` is stored truncated to its UTC calendar day — the same bucket the RNG
seed is derived from in :mod:`backend.app.core.stats_engine`, so a day's
snapshot matches that day's seed — and ``(experiment_id, kind, as_of)`` is
unique.  A dashboard polling ``GET /results/{id}`` every five seconds therefore
keeps one row per kind per day (the latest payload), instead of ~17k rows a
day.  ``updated_at`` records when that row was last refreshed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterator, Optional, Union
from uuid import UUID

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.stats_engine import ENGINE_VERSION, as_of_bucket
from backend.app.models.analysis_snapshot import AnalysisKind, AnalysisSnapshot

logger = logging.getLogger(__name__)


def _coerce_as_of(as_of: Optional[Union[datetime, str]]) -> datetime:
    """Normalise ``as_of`` to an aware UTC datetime (default: now)."""
    if as_of is None:
        return datetime.now(timezone.utc)
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=timezone.utc)
    return as_of.astimezone(timezone.utc)


def snapshot_day(as_of: Optional[Union[datetime, str]] = None) -> datetime:
    """Return the UTC start-of-day bucket an ``as_of`` time belongs to.

    The same bucket :func:`backend.app.core.stats_engine.as_of_bucket` uses
    for seed derivation, as a datetime so it can be stored and compared.
    """
    day = as_of_bucket(_coerce_as_of(as_of))
    return datetime.combine(
        datetime.fromisoformat(day).date(), time.min, tzinfo=timezone.utc
    )


def build_snapshot(
    experiment_id: Union[UUID, str],
    kind: Union[AnalysisKind, str],
    payload: Dict[str, Any],
    *,
    engine_version: str = ENGINE_VERSION,
    seed: Optional[int] = None,
    n_samples: Optional[int] = None,
    as_of: Optional[Union[datetime, str]] = None,
) -> AnalysisSnapshot:
    """Construct (but do not persist) an ``AnalysisSnapshot`` row.

    ``as_of`` is kept as given (defaulting to now).  Callers that want the
    daily, de-duplicated granularity of the results endpoints use
    :func:`record_snapshot`, which buckets it with :func:`snapshot_day`; the
    bandit scheduler deliberately keeps the exact tick instant so every tick
    is its own row.
    """
    kind_value = kind.value if isinstance(kind, AnalysisKind) else str(kind)
    AnalysisKind(kind_value)  # raises ValueError on an unknown kind
    return AnalysisSnapshot(
        experiment_id=experiment_id
        if isinstance(experiment_id, UUID)
        else UUID(str(experiment_id)),
        kind=kind_value,
        engine_version=engine_version,
        seed=int(seed) if seed is not None else None,
        n_samples=int(n_samples) if n_samples is not None else None,
        as_of=_coerce_as_of(as_of),
        payload=payload if payload is not None else {},
    )


@contextmanager
def _audit_session(db: Optional[Session]) -> Iterator[Session]:
    """Yield a short-lived session for the audit write, then close it.

    Bound to the same engine as ``db`` so it reaches the same database (the
    test suite runs against a per-process database that the module-level
    ``SessionLocal`` does not know about), but with its own connection and its
    own transaction: committing or rolling it back cannot touch the caller's.

    Raises:
        TypeError: if ``db`` is not bound to a real engine or connection (a
            mocked session in a unit test).  ``record_snapshot`` turns that
            into a logged warning and no write, rather than sending an audit
            row to whatever database the process default points at.
    """
    bind = db.get_bind() if db is not None else None
    if bind is None:
        from backend.app.db.session import engine as default_engine

        engine = default_engine
    elif isinstance(bind, Connection):
        engine = bind.engine
    elif isinstance(bind, Engine):
        engine = bind
    else:
        raise TypeError(f"session is not bound to a database engine: {bind!r}")

    factory = sessionmaker(
        bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    session = factory()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:  # pragma: no cover - defensive
            pass


def _apply(row: AnalysisSnapshot, snapshot: AnalysisSnapshot) -> bool:
    """Copy ``snapshot``'s values onto ``row``; True when anything changed."""
    changed = False
    for field in ("engine_version", "seed", "n_samples", "payload"):
        new = getattr(snapshot, field)
        if getattr(row, field) != new:
            setattr(row, field, new)
            changed = True
    return changed


def _upsert(session: Session, snapshot: AnalysisSnapshot) -> AnalysisSnapshot:
    """Insert ``snapshot``, or refresh the existing row for its day."""
    existing = (
        session.query(AnalysisSnapshot)
        .filter(
            AnalysisSnapshot.experiment_id == snapshot.experiment_id,
            AnalysisSnapshot.kind == snapshot.kind,
            AnalysisSnapshot.as_of == snapshot.as_of,
        )
        .one_or_none()
    )
    if existing is None:
        session.add(snapshot)
        try:
            session.commit()
            return snapshot
        except IntegrityError:
            # Another request inserted the same (experiment, kind, day)
            # between the SELECT and the INSERT: fall through to the update.
            session.rollback()
            existing = (
                session.query(AnalysisSnapshot)
                .filter(
                    AnalysisSnapshot.experiment_id == snapshot.experiment_id,
                    AnalysisSnapshot.kind == snapshot.kind,
                    AnalysisSnapshot.as_of == snapshot.as_of,
                )
                .one()
            )

    if _apply(existing, snapshot):
        session.commit()
    return existing


def record_snapshot(
    db: Optional[Session],
    experiment_id: Union[UUID, str],
    kind: Union[AnalysisKind, str],
    payload: Dict[str, Any],
    *,
    engine_version: str = ENGINE_VERSION,
    seed: Optional[int] = None,
    n_samples: Optional[int] = None,
    as_of: Optional[Union[datetime, str]] = None,
) -> Optional[AnalysisSnapshot]:
    """Persist one analysis snapshot per experiment, kind and UTC day.

    Idempotent: repeated calls on the same day update that day's row in place
    (so a polling dashboard cannot grow the table) and the first call the next
    day starts a new one.

    The write happens in its own session on ``db``'s engine and commits
    itself; the caller's transaction is never committed or rolled back here
    (see the module docstring).  Never raises.

    Args:
        db: The request's session — used only to find the right engine.
        experiment_id: Experiment the analysis belongs to.
        kind: ``AnalysisKind`` (or its string value).
        payload: JSON-serialisable body of the analysis as served.
        engine_version: Engine version that produced ``payload``.
        seed: RNG seed used for Monte Carlo draws (``None`` if none).
        n_samples: Monte Carlo samples per variant (``None`` if none).
        as_of: When the underlying data was read; defaults to now.  Stored
            truncated to its UTC day.

    Returns:
        The persisted row (detached), or ``None`` when the write failed.
    """
    try:
        snapshot = build_snapshot(
            experiment_id,
            kind,
            payload,
            engine_version=engine_version,
            seed=seed,
            n_samples=n_samples,
            as_of=snapshot_day(as_of),
        )
        with _audit_session(db) as session:
            return _upsert(session, snapshot)
    except Exception as exc:
        logger.warning(
            "analysis_snapshots write failed for experiment %s (%s): %s",
            experiment_id,
            kind.value if isinstance(kind, AnalysisKind) else kind,
            type(exc).__name__,
        )
        return None


def purge_expired_history(
    db: Optional[Session] = None,
    retention_days: Optional[int] = None,
) -> Dict[str, int]:
    """
    Delete analysis history older than the retention window.

    Covers ``analysis_snapshots`` (one row per experiment, kind and day from
    the request path) and ``bandit_state_history`` (one row per arm per
    scheduler tick, so the larger of the two). ``retention_days <= 0``
    disables the purge.

    Returns ``{"analysis_snapshots": n, "bandit_state_history": m}``; never
    raises — a failed purge is logged and reported as zero.
    """
    from backend.app.core.config import settings

    days = (
        settings.ANALYSIS_HISTORY_RETENTION_DAYS
        if retention_days is None
        else retention_days
    )
    deleted = {"analysis_snapshots": 0, "bandit_state_history": 0}
    if days <= 0:
        return deleted

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    try:
        with _audit_session(db) as session:
            deleted["analysis_snapshots"] = (
                session.query(AnalysisSnapshot)
                .filter(AnalysisSnapshot.as_of < cutoff)
                .delete(synchronize_session=False)
            )
            try:
                from backend.app.models.bandit_state import BanditStateHistory

                deleted["bandit_state_history"] = (
                    session.query(BanditStateHistory)
                    .filter(BanditStateHistory.tick_at < cutoff)
                    .delete(synchronize_session=False)
                )
            except ImportError:  # pragma: no cover - model always present
                pass
            # `_audit_session` never commits on its own.
            session.commit()
    except Exception as exc:
        logger.warning("analysis history purge failed: %s", type(exc).__name__)
        return {"analysis_snapshots": 0, "bandit_state_history": 0}

    if deleted["analysis_snapshots"] or deleted["bandit_state_history"]:
        logger.info(
            "Purged analysis history older than %s days: %s snapshots, %s bandit rows",
            days,
            deleted["analysis_snapshots"],
            deleted["bandit_state_history"],
        )
    return deleted

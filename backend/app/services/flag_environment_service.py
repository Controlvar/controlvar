"""A flag's staging and development settings (#2011).

Every write of them, and the dashboard routes' reads (the flag list, which an
API key with the ``mcp:read`` scope may also call, among them); evaluation, user
flags and the ruleset read them through ``flag_environment_reads``.

Production's settings are the flag row and go through ``FeatureFlagService``.
Staging's and development's are side rows of ``flag_environment_settings``
(``FlagEnvironmentSetting``), at most one per flag and environment. Nothing
else in ``backend/app`` writes that table
(``backend/tests/smoke/test_flag_environment_writers.py``), and its two
writers here are called only by routes, which write the audit entry in the
same transaction:

* :meth:`FlagEnvironmentService.save_environment_setting`: the environment
  PUT and promotion;
* :func:`turn_off_environment_settings`: bulk archive.

A write takes the flag row's lock first (:func:`lock_flag`), then its side
row, and never a rollout schedule's, so two changes of one flag wait for each
other and the last one is kept. A side row's write never touches the flag row:
``feature_flags.updated_at`` does not move.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.app.core.flag_environments import (
    NON_PRODUCTION,
    PRODUCTION,
    flag_in_environment,
)
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
    flag_status_name,
)
from backend.app.services.audit_service import name_only_fingerprint

#: The settings one environment has of a flag, and the settings a promotion
#: copies: every column of ``FlagEnvironmentSetting`` that is not an identity
#: column (``SETTING_IDENTITY_COLUMNS``), as a change sends them (#2012 adds
#: ``split``).
SETTING_FIELDS: Tuple[str, ...] = (
    "is_active",
    "targeting_rules",
    "rollout_percentage",
    "split",
)

#: A flag nobody configured in an environment: off, with no rules, 0% and no
#: split.
NOT_CONFIGURED: Dict[str, Any] = {
    "is_active": False,
    "targeting_rules": None,
    "rollout_percentage": 0,
    "split": None,
}

#: A side row by flag and environment.
SettingsByFlag = Dict[Tuple[UUID, str], FlagEnvironmentSetting]


def _utcnow() -> datetime:
    """Now as the naive UTC the timestamp columns hold."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def settings_of(
    db: Session, flag_ids: Iterable[UUID], environment: Optional[str] = None
) -> SettingsByFlag:
    """The side rows of these flags, in one statement; none asks nothing."""
    ids = list(flag_ids)
    if not ids:
        return {}
    query = db.query(FlagEnvironmentSetting).filter(
        FlagEnvironmentSetting.feature_flag_id.in_(ids)
    )
    if environment is not None:
        query = query.filter(FlagEnvironmentSetting.environment == environment)
    return {(row.feature_flag_id, row.environment): row for row in query}


def setting_of(
    db: Session, flag_id: UUID, environment: str
) -> Optional[FlagEnvironmentSetting]:
    """The flag's side row in *environment*, or ``None``; production has none."""
    if environment == PRODUCTION:
        return None
    return settings_of(db, [flag_id], environment).get((flag_id, environment))


def effective_settings(
    flag: Any, setting: Optional[Any], environment: str
) -> Dict[str, Any]:
    """What *environment* serves for *flag*, as a change would send it.

    The fields are :data:`SETTING_FIELDS`. A flag nobody configured there is
    off, and an archived flag is off in every environment.
    """
    view = flag_in_environment(flag, setting, environment)
    return {
        "is_active": flag_status_name(view.status) == FeatureFlagStatus.ACTIVE.value,
        "targeting_rules": view.targeting_rules,
        "rollout_percentage": view.rollout_percentage,
        "split": view.split,
    }


def flags_on_outside_production(db: Session, flag_ids: Iterable[UUID]) -> Set[UUID]:
    """Those of *flag_ids* that are on in staging or development."""
    ids = list(flag_ids)
    if not ids:
        return set()
    rows = db.execute(
        select(FlagEnvironmentSetting.feature_flag_id)
        .where(
            FlagEnvironmentSetting.feature_flag_id.in_(ids),
            FlagEnvironmentSetting.is_active.is_(True),
        )
        .distinct()
    )
    return {row[0] for row in rows}


def settings_mentioning(db: Session, words: str) -> List[Any]:
    """The side rows whose stored rules' text contains *words*, in any case.

    ``(feature_flag_id, environment, targeting_rules)`` per row: a prefilter,
    as the segment readers use on ``feature_flags.targeting_rules``.
    """
    return (
        db.query(
            FlagEnvironmentSetting.feature_flag_id,
            FlagEnvironmentSetting.environment,
            FlagEnvironmentSetting.targeting_rules,
        )
        .filter(
            sa.cast(FlagEnvironmentSetting.targeting_rules, sa.Text).ilike(f"%{words}%")
        )
        .all()
    )


# ---------------------------------------------------------------------------
# What an audit entry says
# ---------------------------------------------------------------------------


def audit_setting(
    flag: Any, setting: Optional[Any], environment: str
) -> Dict[str, Any]:
    """A snapshot of *flag* in *environment* for ``audit_changes``.

    Whether it is configured there, and its on/off and percentage as they are
    stored; the rules and the split are named only (``#``), never recorded,
    and compared as JSON text, so a change ``==`` does not see (``true`` for
    ``1``) is named too.
    """
    if environment == PRODUCTION:
        stored = effective_settings(flag, None, PRODUCTION)
    elif setting is None:
        stored = NOT_CONFIGURED
    else:
        stored = {field: getattr(setting, field) for field in SETTING_FIELDS}
    return {
        "configured": environment == PRODUCTION or setting is not None,
        "is_active": stored["is_active"],
        "rollout_percentage": stored["rollout_percentage"],
        "#targeting_rules": name_only_fingerprint(stored["targeting_rules"]),
        "#split": name_only_fingerprint(stored["split"]),
    }


def audit_side_rows(db: Session, flag_id: UUID) -> Dict[str, Dict[str, Any]]:
    """The flag's side rows as a delete's audit entry records them, by
    environment; empty when it has none."""
    rows = settings_of(db, [flag_id])
    return {
        environment: {
            "is_active": row.is_active,
            "rollout_percentage": row.rollout_percentage,
        }
        for environment in NON_PRODUCTION
        if (row := rows.get((flag_id, environment))) is not None
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def lock_flag(db: Session, flag_id: UUID) -> Optional[FeatureFlag]:
    """The flag row, locked until this transaction ends, as it is stored now.

    ``FOR NO KEY UPDATE``: it waits for, and holds off, every other change or
    delete of the flag (an update, a rollback, a rollout stage, an archive),
    but not a row that only refers to it, such as the metric a production
    evaluation writes. ``None`` when no flag has this id.
    """
    return (
        db.query(FeatureFlag)
        .filter(FeatureFlag.id == flag_id)
        .populate_existing()
        .with_for_update(key_share=True)
        .one_or_none()
    )


class FlagEnvironmentService:
    """Writes a flag's settings in staging or development."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def save_environment_setting(
        self,
        flag: FeatureFlag,
        environment: str,
        changes: Mapping[str, Any],
        *,
        record: Optional[Callable[[FlagEnvironmentSetting], None]] = None,
    ) -> FlagEnvironmentSetting:
        """Store *changes* (of :data:`SETTING_FIELDS`) for *flag* in
        *environment*, and return the side row.

        One statement creates the row or changes the fields sent, and
        ``updated_at`` moves either way: the database does not apply the
        column's ``onupdate`` to an upsert. *flag* is locked by the caller
        (:func:`lock_flag`). ``record``, when given, writes the audit entries
        in the same transaction: if it raises, nothing is saved, and if it
        does not, the row and its entries are committed together.
        """
        unknown = sorted(set(changes) - set(SETTING_FIELDS))
        if environment not in NON_PRODUCTION or unknown:
            raise ValueError(f"not a side-row change: {environment!r}, {unknown}")
        row = (
            pg_insert(FlagEnvironmentSetting)
            .values(
                feature_flag_id=flag.id,
                environment=environment,
                **{**NOT_CONFIGURED, **changes},
            )
            .on_conflict_do_update(
                index_elements=["feature_flag_id", "environment"],
                set_={**changes, "updated_at": _utcnow()},
            )
            .returning(FlagEnvironmentSetting)
        )
        try:
            setting = self.db.scalars(
                row, execution_options={"populate_existing": True}
            ).one()
            if record is not None:
                record(setting)
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()
        self.db.refresh(setting)
        return setting


def turn_off_environment_settings(db: Session, flag_id: UUID) -> List[str]:
    """Turn the flag off wherever it is on outside production; never commits.

    Returns the environments it turned off, in their fixed order. Rules and
    percentages are kept. Bulk archive calls it in the transaction that
    archives the flag, after the flag row is written (and so locked).
    """
    rows = db.execute(
        update(FlagEnvironmentSetting)
        .where(
            FlagEnvironmentSetting.feature_flag_id == flag_id,
            FlagEnvironmentSetting.is_active.is_(True),
        )
        .values(is_active=False)
        .returning(FlagEnvironmentSetting.environment)
        .execution_options(synchronize_session=False)
    )
    turned_off = {row[0] for row in rows}
    return [name for name in NON_PRODUCTION if name in turned_off]

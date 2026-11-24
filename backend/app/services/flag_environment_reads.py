"""
What each flag environment serves, read from the database (#2011).

Production is the flag row itself, so every function here returns production's
answer from the flag rows alone and reads nothing else. Staging and
development are read from ``flag_environment_settings``: one query for one
flag or for every flag, never one per flag. This is the one module the key
routes' read path reads that table through, and
:func:`backend.app.core.flag_environments.flag_in_environment` decides what a
flag row and its side row (or none) serve.
"""

from __future__ import annotations

from typing import Any, List, Sequence

from sqlalchemy.orm import Session

from backend.app.core.flag_environments import PRODUCTION, flag_in_environment
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)


def served_flag(db: Session, flag: FeatureFlag, environment: str) -> Any:
    """What *environment* serves for *flag*: the row itself in production (no
    query), otherwise its view there, after one query for its side row."""
    if environment == PRODUCTION:
        return flag
    setting = (
        db.query(FlagEnvironmentSetting)
        .filter(
            FlagEnvironmentSetting.feature_flag_id == flag.id,
            FlagEnvironmentSetting.environment == environment,
        )
        .first()
    )
    return flag_in_environment(flag, setting, environment)


def served_flags(
    db: Session, flags: Sequence[FeatureFlag], environment: str
) -> Sequence[Any]:
    """What *environment* serves for each of *flags*, in their order: the rows
    themselves in production (no query), otherwise their views there, after
    one query for the environment's side rows."""
    if environment == PRODUCTION:
        return flags
    side_rows = {
        setting.feature_flag_id: setting
        for setting in db.query(FlagEnvironmentSetting)
        .filter(FlagEnvironmentSetting.environment == environment)
        .all()
    }
    return [
        flag_in_environment(flag, side_rows.get(flag.id), environment) for flag in flags
    ]


def flags_on_in(db: Session, environment: str) -> List[Any]:
    """The flags that are on in *environment*, in one query: production's
    ACTIVE flag rows, otherwise the views of the flags whose side row there is
    active and whose flag row is not archived."""
    if environment == PRODUCTION:
        return (
            db.query(FeatureFlag)
            .filter(FeatureFlag.status == FeatureFlagStatus.ACTIVE.value)
            .all()
        )
    rows = (
        db.query(FeatureFlag, FlagEnvironmentSetting)
        .join(
            FlagEnvironmentSetting,
            FlagEnvironmentSetting.feature_flag_id == FeatureFlag.id,
        )
        .filter(
            FlagEnvironmentSetting.environment == environment,
            FlagEnvironmentSetting.is_active.is_(True),
        )
        .all()
    )
    views = [flag_in_environment(flag, setting, environment) for flag, setting in rows]
    # An archived flag row wins over an active side row.
    return [view for view in views if view.status == FeatureFlagStatus.ACTIVE]

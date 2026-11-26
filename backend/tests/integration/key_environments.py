"""API keys and flag settings of each flag environment, for integration tests (#2011).

The tests that use these write the rows: through the models, so a key can be
one no request makes (``mcp:read`` outside production), and a key of the
previous release as it inserts one.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.core.security import hash_api_key
from backend.app.models.api_key import APIKey, generate_api_key
from backend.app.models.feature_flag import FeatureFlag, FlagEnvironmentSetting
from backend.app.models.user import User


def environment_key(
    db: Session,
    user: User,
    environment: Optional[str] = None,
    scopes: Optional[str] = None,
) -> Dict[str, str]:
    """The ``X-API-Key`` header of a new key of *environment*; with ``None``,
    of a key whose INSERT names no environment, as the previous release writes
    one (the model would name its default)."""
    plaintext = generate_api_key()
    row = {
        "id": uuid.uuid4(),
        "user_id": user.id,
        "key": hash_api_key(plaintext),
        "name": f"{environment or 'unnamed'}-{uuid.uuid4().hex[:6]}",
        "scopes": scopes,
    }
    if environment is None:
        db.execute(
            text(
                f"INSERT INTO {APIKey.__table__.fullname} (id, user_id, key, name, "
                "scopes, is_active, created_at, updated_at) VALUES (:id, :user_id, "
                ":key, :name, :scopes, true, now(), now())"
            ),
            row,
        )
    else:
        db.add(APIKey(**row, environment=environment, is_active=True))
    db.commit()
    return {"X-API-Key": plaintext}


def configure(
    db: Session,
    flag: FeatureFlag,
    environment: str,
    *,
    is_active: bool = True,
    rollout_percentage: int = 100,
    targeting_rules: Any = None,
    split: Any = None,
) -> FlagEnvironmentSetting:
    """Set what *flag* serves in *environment*, replacing what it served there."""
    setting = (
        db.query(FlagEnvironmentSetting)
        .filter(
            FlagEnvironmentSetting.feature_flag_id == flag.id,
            FlagEnvironmentSetting.environment == environment,
        )
        .first()
    )
    if setting is None:
        setting = FlagEnvironmentSetting(
            feature_flag_id=flag.id, environment=environment
        )
        db.add(setting)
    setting.is_active = is_active
    setting.rollout_percentage = rollout_percentage
    setting.targeting_rules = targeting_rules
    setting.split = split
    db.commit()
    return setting

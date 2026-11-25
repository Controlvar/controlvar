"""Safety monitoring measures production only (#2011).

One ACTIVE flag is at 50% in production and at 100% in staging. Everything a
staging server reports against it fails: every error report, its failed
evaluations, and its local evaluation counts. One real
``SafetyScheduler.check_feature_flags_safety`` tick, called directly (the API
starts no scheduler under test), with automatic rollbacks on and a global
error-rate rule, finds the flag healthy and leaves production as it was. The
same errors sent with a production key roll it back on the next tick, and
staging's setting stays as it was.

The tick's returned counts are asserted exactly, so every other ACTIVE flag of
the shared test database is parked for the test.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import func, text, update
from sqlalchemy.orm import sessionmaker

from backend.app.core.safety_scheduler import SafetyScheduler
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.app.models.metrics.metric import ErrorLog
from backend.app.models.safety import SafetyRollbackRecord, SafetySettings
from backend.app.models.user import UserRole
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.safety_service import SafetyService
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    local_sign_in,
    make_user,
)
from backend.tests.integration.key_environments import configure, environment_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

EVALUATIONS = 10
ERRORS = 10


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def parked_flags(db_session):
    """Every other ACTIVE flag is off for the test, and back on afterwards."""
    parked = [
        row.id
        for row in db_session.query(FeatureFlag.id).filter(
            FeatureFlag.status == FeatureFlagStatus.ACTIVE
        )
    ]

    def set_status(status: FeatureFlagStatus) -> None:
        db_session.execute(
            update(FeatureFlag)
            .where(FeatureFlag.id.in_(parked))
            .values(status=status, updated_at=FeatureFlag.updated_at)
            .execution_options(synchronize_session=False)
        )
        db_session.commit()

    set_status(FeatureFlagStatus.INACTIVE)
    try:
        yield
    finally:
        db_session.rollback()
        set_status(FeatureFlagStatus.ACTIVE)


@pytest.fixture
def error_rate_rule(db_session):
    """Automatic rollbacks on, with one global rule on the error rate."""
    row = db_session.query(SafetySettings).first()
    created = row is None
    if created:
        row = SafetySettings()
        db_session.add(row)
    previous = (
        None if created else (row.enable_automatic_rollbacks, row.default_metrics)
    )
    row.enable_automatic_rollbacks = True
    row.default_metrics = {
        "error_rate": {"critical_threshold": 0.10, "comparison_type": "greater_than"}
    }
    db_session.commit()
    yield
    db_session.rollback()
    row = db_session.query(SafetySettings).first()
    if created:
        db_session.delete(row)
    else:
        row.enable_automatic_rollbacks, row.default_metrics = previous
    db_session.commit()


async def _tick(db_session) -> Dict[str, Any]:
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def session():
        made = factory()
        made.execute(text(f"SET search_path TO {SCHEMA}"))
        return made

    scheduler = SafetyScheduler()
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session),
        patch.object(scheduler, "_notification_service"),
    ):
        return await scheduler.check_feature_flags_safety()


def _production(db_session, flag) -> tuple:
    db_session.expire_all()
    row = db_session.get(FeatureFlag, flag.id)
    records = (
        db_session.query(func.count(SafetyRollbackRecord.id))
        .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
        .scalar()
    )
    return row.status, row.rollout_percentage, records


def _staging(db_session, flag) -> tuple:
    db_session.expire_all()
    (row,) = (
        db_session.query(FlagEnvironmentSetting)
        .filter(FlagEnvironmentSetting.feature_flag_id == flag.id)
        .all()
    )
    return (
        row.environment,
        row.is_active,
        row.rollout_percentage,
        row.targeting_rules,
        row.updated_at,
    )


def _evaluate(client, headers, flag, times: int) -> None:
    for n in range(times):
        answer = client.get(
            f"/api/v1/feature-flags/evaluate/{flag.key}",
            params={"user_id": f"u-{n}"},
            headers=headers,
        )
        assert answer.status_code == 200, answer.text


def _report_errors(client, headers, flag, times: int) -> None:
    item = {"feature_flag_key": flag.key, "error_type": "crash", "message": "m"}
    for _ in range(times // 2):
        single = client.post("/api/v1/tracking/errors", json=item, headers=headers)
        assert single.status_code == 201, single.text
    batch = client.post(
        "/api/v1/tracking/errors/batch",
        json={"errors": [item] * (times - times // 2)},
        headers=headers,
    )
    assert batch.status_code == 200, batch.text


def _report_counts(client, headers, flag, count: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    answer = client.post(
        "/api/v1/tracking/evaluations",
        json={
            "evaluations": [
                {
                    "flag_key": flag.key,
                    "count": count,
                    "enabled_count": count,
                    "window_start": (now - timedelta(seconds=30)).isoformat(),
                    "window_end": now.isoformat(),
                }
            ]
        },
        headers=headers,
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


def _stored(db_session, flag) -> Dict[Optional[str], int]:
    """How many error rows name each environment."""
    db_session.expire_all()
    found: Dict[Optional[str], int] = {}
    for row in (
        db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).all()
    ):
        name = row.request_data.get("environment")
        found[name] = found.get(name, 0) + 1
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "development"])
async def test_errors_of_another_environment_never_roll_production_back(
    client,
    db_session,
    make_feature_flag,
    parked_flags,
    error_rate_rule,
    monkeypatch,
    environment,
):
    flag = make_feature_flag(
        key=f"env-safety-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=50,
    )
    configure(db_session, flag, environment)
    before = _staging(db_session, flag)
    owner = make_user(db_session, UserRole.DEVELOPER, tag="envsafety")
    production = environment_key(db_session, owner, "production", "sdk:ruleset")
    other = environment_key(db_session, owner, environment, "sdk:ruleset")

    # Production: evaluated, no error. The other environment: every error
    # report a server key can send, failed evaluations, and a flood of counts.
    _evaluate(client, production, flag, EVALUATIONS)
    _evaluate(client, other, flag, EVALUATIONS)
    _report_errors(client, other, flag, ERRORS)

    def fails(self, *args, **kwargs):
        raise RuntimeError("planted evaluation failure")

    with monkeypatch.context() as patched:
        patched.setattr(FeatureFlagService, "_match_targeting_rule", fails)
        _evaluate(client, other, flag, ERRORS)
    counts = _report_counts(client, other, flag, 5000)
    assert counts["accepted"] == 0, (
        f"/tracking/evaluations with a {environment} key answered accepted "
        f"{counts['accepted']}"
    )

    result = await _tick(db_session)
    state = _production(db_session, flag)
    assert state == (FeatureFlagStatus.ACTIVE, 50, 0), (
        f"production rolled back 50 → {state[1]} by errors reported with a "
        f"{environment} key"
    )
    assert result == {
        "items_processed": 1,
        "items_failed": 0,
        "metadata": {"rollbacks": 0},
    }
    # The rows are stored, each naming its environment, and in neither number.
    assert _stored(db_session, flag) == {environment: 2 * ERRORS}
    check = await SafetyService(db_session).check_feature_flag_safety(flag.id)
    assert check.is_healthy is True, check
    assert (
        check.details["error_count"],
        check.details["uncounted_error_count"],
        check.details["total_evaluations"],
    ) == (0, 0, EVALUATIONS)

    # The same errors with a production key do roll it back, and leave the
    # other environment's setting as it was.
    _report_errors(client, production, flag, ERRORS)
    result = await _tick(db_session)
    assert result == {
        "items_processed": 1,
        "items_failed": 0,
        "metadata": {"rollbacks": 1},
    }
    assert _production(db_session, flag) == (FeatureFlagStatus.INACTIVE, 0, 1)
    assert _staging(db_session, flag) == before
    answer = client.get(
        f"/api/v1/feature-flags/evaluate/{flag.key}",
        params={"user_id": "u-1"},
        headers=other,
    )
    assert answer.json()["enabled"] is True, answer.text

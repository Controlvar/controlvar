"""Deleting a feature flag removes its metrics and error logs set-wise.

The flag's raw metrics, aggregated metrics and error logs are removed with
one DELETE per table, not row by row through the ORM. Its events stay,
untagged (``events.feature_flag_id`` is ``ON DELETE SET NULL``).

The listener counts rows the ORM loads as persistent instances of the three
child models while the DELETE request runs; counting rows, not timing.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.event import Event
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.metrics.metric import AggregatedMetric, ErrorLog, RawMetric
from backend.tests.integration.conftest import make_client_for_user

BASE = "/api/v1/feature-flags"
PREFIX = "ffsw-"
RAW_ROWS = 1000


@pytest.fixture(autouse=True)
def _clean_up(db_session):
    yield
    app.dependency_overrides.clear()
    db_session.rollback()
    db_session.query(Event).filter(Event.user_id.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.query(RawMetric).filter(RawMetric.user_id.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.query(ErrorLog).filter(ErrorLog.user_id.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.query(FeatureFlag).filter(FeatureFlag.key.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.commit()


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_deleting_a_flag_removes_its_metrics_and_error_logs(db_session, developer_user):
    client = make_client_for_user(db_session, developer_user)
    key = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    created = client.post(
        f"{BASE}/", json={"key": key, "name": "ffsw", "is_active": False}
    )
    assert created.status_code == 201, created.text
    flag_id = uuid.UUID(created.json()["id"])

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    raw_ids = [uuid.uuid4() for _ in range(RAW_ROWS)]
    db_session.bulk_insert_mappings(
        RawMetric,
        [
            {
                "id": rid,
                "metric_type": "latency",
                "timestamp": now,
                "feature_flag_id": flag_id,
                "user_id": f"{PREFIX}u",
                "value": 1.0,
                "count": 1,
            }
            for rid in raw_ids
        ],
    )
    agg_id, err_id, event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.bulk_insert_mappings(
        AggregatedMetric,
        [
            {
                "id": agg_id,
                "metric_type": "latency",
                "period": "hour",
                "period_start": now,
                "feature_flag_id": flag_id,
                "count": 3,
            }
        ],
    )
    db_session.bulk_insert_mappings(
        ErrorLog,
        [
            {
                "id": err_id,
                "error_type": "boom",
                "timestamp": now,
                "feature_flag_id": flag_id,
                "user_id": f"{PREFIX}u",
                "message": "boom",
            }
        ],
    )
    db_session.add(
        Event(
            id=event_id,
            event_type="purchase",
            event_name="purchase",
            user_id=f"{PREFIX}event",
            value=1.0,
            feature_flag_id=flag_id,
            created_at=now.isoformat(),
        )
    )
    db_session.commit()

    child_models = (RawMetric, AggregatedMetric, ErrorLog)
    loaded: list = []

    def _count(session, instance):
        if isinstance(instance, child_models):
            loaded.append(instance)

    sa_event.listen(Session, "loaded_as_persistent", _count)
    try:
        deleted = client.delete(f"{BASE}/{flag_id}")
    finally:
        sa_event.remove(Session, "loaded_as_persistent", _count)

    assert deleted.status_code == 204, deleted.text
    assert len(loaded) == 0, f"the ORM loaded {len(loaded)} child rows"

    db_session.expire_all()
    assert db_session.get(FeatureFlag, flag_id) is None
    assert db_session.query(RawMetric).filter(RawMetric.id.in_(raw_ids)).count() == 0
    assert db_session.get(AggregatedMetric, agg_id) is None
    assert db_session.get(ErrorLog, err_id) is None
    kept = db_session.get(Event, event_id)
    assert kept is not None and kept.feature_flag_id is None

    audits = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action_type == "feature_flag_delete",
            AuditLog.entity_id == flag_id,
        )
        .all()
    )
    assert len(audits) == 1

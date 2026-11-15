"""Delete a flag that has dependents, then create a new flag with the same key.

``DELETE /api/v1/feature-flags/{id}`` removes the flag and everything that
belongs to it: its rollout schedules and their stages, its safety
configuration, its rollback records, its raw and aggregated metrics, its error
logs, its overrides and its reports. Its events are kept, with
``feature_flag_id`` set to NULL (#855). One ``feature_flag_delete`` audit entry
names the key. The key is free at once: a ``POST`` with it creates a new id
that inherits none of that, and evaluation answers by the new flag on the next
request (the server keeps no flag cache).

Every dependent is seeded before the delete, so each check below has something
to find. After the delete each seeded record is looked up again by its own
primary key, never by ``feature_flag_id``: the foreign keys of ``error_logs``
and ``reports`` are ``ON DELETE SET NULL``, so a record the delete left behind
untagged would not match a query on the old flag id.

The schema is shared with other tests, so everything uses its own key prefix
and the assertions read only this test's ids. Flags are created, changed and
deleted through the API by a DEVELOPER who is not a superuser (D11); the two
safety routes need a superuser.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.event import Event
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagOverride
from backend.app.models.metrics.metric import AggregatedMetric, ErrorLog, RawMetric
from backend.app.models.report import Report, ReportType
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutStage
from backend.app.models.safety import FeatureFlagSafetyConfig, SafetyRollbackRecord
from backend.app.services.safety_service import DEFAULT_CONFIG_ID
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

FLAGS = "/api/v1/feature-flags"
SCHEDULES = "/api/v1/rollout-schedules"
SAFETY = "/api/v1/safety"
ERRORS = "/api/v1/tracking/errors"
PREFIX = "ffrc-"
USER = f"{PREFIX}user"
#: Both flags' name: the audit entries carry it, so the clean-up finds the
#: deleted flag's entries too.
NAME = f"{PREFIX}delete and re-create"

#: Every table that holds records belonging to a flag. ``events`` is not here:
#: a flag's events outlive it.
DEPENDENTS = (
    RolloutSchedule,
    RolloutStage,
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    RawMetric,
    AggregatedMetric,
    ErrorLog,
    FeatureFlagOverride,
    Report,
)


@pytest.fixture(autouse=True)
def _clean_up(db_session):
    """Remove what the test created. Deleting the flags takes everything with
    an ``ON DELETE CASCADE`` key; the ``SET NULL`` tables go first."""
    yield
    app.dependency_overrides.clear()
    db_session.rollback()
    flag_ids = [
        row.id
        for row in db_session.query(FeatureFlag.id).filter(
            FeatureFlag.key.like(f"{PREFIX}%")
        )
    ]
    db_session.query(Event).filter(Event.user_id.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.query(ErrorLog).filter(ErrorLog.user_id.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.query(Report).filter(Report.title.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.query(AuditLog).filter(AuditLog.entity_name.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    if flag_ids:
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _schedule_body(flag_id: uuid.UUID) -> dict:
    """A DRAFT schedule with one manual stage; its dates are never reached."""
    start = datetime.now(timezone.utc) + timedelta(days=30)
    return {
        "name": f"{PREFIX}schedule",
        "feature_flag_id": str(flag_id),
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=30)).isoformat(),
        "max_percentage": 100,
        "min_stage_duration": 1,
        "stages": [
            {
                "name": "only stage",
                "stage_order": 1,
                "target_percentage": 100,
                "trigger_type": "manual",
            }
        ],
    }


def _seed_every_dependent(db_session, developer_user, admin_user, key, flag_id):
    """One or more records of every dependent table, and one tagged event.

    Through the API where a route writes the record: the schedule and its
    stage, a flag evaluation (raw metrics), an error report, the safety
    configuration and a manual rollback (a rollback record). Through the ORM
    where none does: an aggregated metric, an override and a report.

    Returns ``{model: [primary key, ...]}`` for every table in ``DEPENDENTS``,
    and the event's primary key.
    """
    developer = make_client_for_user(db_session, developer_user)
    schedule = _ok(developer.post(f"{SCHEDULES}/", json=_schedule_body(flag_id)), 201)
    evaluated = _ok(developer.get(f"{FLAGS}/evaluate/{key}", params={"user_id": USER}))
    assert evaluated["enabled"] is True, evaluated
    error = _ok(
        developer.post(
            ERRORS,
            json={
                "feature_flag_key": key,
                "user_id": USER,
                "error_type": "crash",
                "message": f"{PREFIX}error",
            },
        ),
        201,
    )

    admin = make_client_for_user(db_session, admin_user)
    _ok(
        admin.post(
            f"{SAFETY}/feature-flags/{flag_id}/config",
            json={
                "metrics": {
                    "error_rate": {
                        "critical_threshold": 0.5,
                        "comparison_type": "greater_than",
                    }
                }
            },
        )
    )
    rolled_back = _ok(
        admin.post(
            f"{SAFETY}/feature-flags/{flag_id}/rollback", params={"percentage": 0}
        )
    )
    assert rolled_back["success"] is True, rolled_back

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    aggregated = AggregatedMetric(
        id=uuid.uuid4(),
        metric_type="latency",
        period="hour",
        period_start=now,
        feature_flag_id=flag_id,
        count=1,
    )
    override = FeatureFlagOverride(
        id=uuid.uuid4(), feature_flag_id=flag_id, user_id=USER, value=True
    )
    report = Report(
        id=uuid.uuid4(),
        title=f"{PREFIX}report",
        report_type=ReportType.FEATURE_FLAG_USAGE,
        owner_id=developer_user.id,
        feature_flag_id=flag_id,
    )
    event = Event(
        id=uuid.uuid4(),
        event_type="purchase",
        event_name="purchase",
        user_id=USER,
        value=1.0,
        feature_flag_id=flag_id,
        created_at=now.isoformat(),
    )
    db_session.add_all([aggregated, override, report, event])
    db_session.commit()

    def ids_of(model):
        return [
            row.id
            for row in db_session.query(model.id).filter(
                model.feature_flag_id == flag_id
            )
        ]

    seeded = {
        RolloutSchedule: [uuid.UUID(schedule["id"])],
        RolloutStage: [uuid.UUID(stage["id"]) for stage in schedule["stages"]],
        FeatureFlagSafetyConfig: ids_of(FeatureFlagSafetyConfig),
        SafetyRollbackRecord: ids_of(SafetyRollbackRecord),
        RawMetric: ids_of(RawMetric),
        AggregatedMetric: [aggregated.id],
        ErrorLog: [uuid.UUID(error["id"])],
        FeatureFlagOverride: [override.id],
        Report: [report.id],
    }
    assert set(seeded) == set(DEPENDENTS)
    empty = [model.__tablename__ for model, ids in seeded.items() if not ids]
    assert not empty, f"nothing was seeded in {empty}"
    return seeded, event.id


def _left_behind(db_session, seeded) -> dict:
    """``{table: [feature_flag_id of each seeded record still present]}``,
    looked up by primary key; empty when every record is gone."""
    db_session.expire_all()
    left = {}
    for model, ids in seeded.items():
        rows = db_session.query(model).filter(model.id.in_(ids)).all()
        if rows:
            left[model.__tablename__] = [
                str(getattr(row, "feature_flag_id", "a stage")) for row in rows
            ]
    return left


def _audit(db_session, action, entity_id):
    return (
        db_session.query(AuditLog)
        .filter(AuditLog.action_type == action, AuditLog.entity_id == entity_id)
        .all()
    )


def test_a_deleted_flag_leaves_nothing_behind_and_its_key_starts_clean(
    db_session, developer_user, admin_user
):
    key = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    developer = make_client_for_user(db_session, developer_user)
    old_id = uuid.UUID(
        _ok(
            developer.post(
                f"{FLAGS}/",
                json={
                    "key": key,
                    "name": NAME,
                    "is_active": True,
                    "rollout_percentage": 100,
                },
            ),
            201,
        )["id"]
    )
    seeded, event_id = _seed_every_dependent(
        db_session, developer_user, admin_user, key, old_id
    )

    # The delete: by the DEVELOPER, and every seeded record goes with the flag.
    developer = make_client_for_user(db_session, developer_user)
    deleted = developer.delete(f"{FLAGS}/{old_id}")
    assert deleted.status_code == 204, deleted.text
    assert db_session.get(FeatureFlag, old_id) is None
    assert _left_behind(db_session, seeded) == {}, "the delete left records behind"

    kept = db_session.get(Event, event_id)
    assert kept is not None, "the flag's event must be kept"
    assert kept.feature_flag_id is None

    gone = developer.get(f"{FLAGS}/evaluate/{key}", params={"user_id": USER})
    assert gone.status_code == 404, gone.text

    # The key is free at once, and the new flag inherits nothing.
    new_id = uuid.UUID(
        _ok(
            developer.post(
                f"{FLAGS}/",
                json={
                    "key": key,
                    "name": NAME,
                    "is_active": False,
                    "rollout_percentage": 100,
                },
            ),
            201,
        )["id"]
    )
    assert new_id != old_id
    # A stage belongs to a schedule, not to a flag: no schedule, no stage.
    for model in (m for m in DEPENDENTS if m is not RolloutStage):
        found = (
            db_session.query(model.id).filter(model.feature_flag_id == new_id).count()
        )
        assert found == 0, f"{model.__tablename__} holds {found} for the new flag"
    db_session.expire_all()
    assert db_session.get(Event, event_id).feature_flag_id is None

    config = _ok(developer.get(f"{SAFETY}/feature-flags/{new_id}/config"))
    assert config["id"] == str(DEFAULT_CONFIG_ID), config
    schedules = _ok(developer.get(f"{SCHEDULES}/", params={"feature_flag_id": new_id}))
    assert schedules["total"] == 0, schedules

    # Evaluation answers by the new flag: created off, then turned on at 100%
    # (the old flag was rolled back to 0%, which turned it off).
    off = _ok(developer.get(f"{FLAGS}/evaluate/{key}", params={"user_id": USER}))
    assert (off["enabled"], off["reason"]) == (False, "inactive"), off
    _ok(developer.post(f"{FLAGS}/{new_id}/activate", json={}))
    on = _ok(developer.get(f"{FLAGS}/evaluate/{key}", params={"user_id": USER}))
    assert (on["enabled"], on["reason"]) == (True, "rollout"), on

    # One delete entry for the old id and one create entry for the new id,
    # each naming the key.
    db_session.expire_all()
    deletes = _audit(db_session, "feature_flag_delete", old_id)
    assert len(deletes) == 1, [row.action_type for row in deletes]
    assert json.loads(deletes[0].old_value)["key"] == key
    creates = _audit(db_session, "feature_flag_create", new_id)
    assert len(creates) == 1, [row.action_type for row in creates]
    assert json.loads(creates[0].new_value)["key"] == key
    assert _audit(db_session, "feature_flag_delete", new_id) == []

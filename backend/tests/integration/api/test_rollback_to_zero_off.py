"""A safety rollback to 0% turns the flag off for every user (#629).

Before: a rollback, manual or automatic, wrote only the flag's global
``rollout_percentage``. A user matched by a targeting rule is bucketed by the
rule's own percentage, so after a "rollback to 0" every such user kept the
flag (``enabled: true, reason: targeting_rule``); a native ``default_rule``
kept it for everyone; a flag serving only rule-matched users (global 0%)
could not be rolled back at all ("already at 0%"); the monitor never looked
at such a flag; and a rollout schedule's next stage raised the percentage
straight back.

Now ``execute_rollback`` asks ``rollback_change(flag, target)``:

* target 0 -> the flag turns off (INACTIVE) and its global percentage is 0,
  so every evaluator answers ``enabled: false, reason: inactive``;
* target 1-100 -> only the global percentage drops; users matched by a rule
  keep their rule's percentage (D39-4 as recorded in T108);
* either way every ACTIVE rollout schedule of the flag is paused, in the same
  transaction as the flag write and the record.

The flag keys are fixed and the users are ``user-0`` .. ``user-199``, so the
bucket sets below are exact and compared for equality, never by count.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.safety_service import SafetyService
from backend.app.services.sdk_ruleset import build_ruleset

pytestmark = [pytest.mark.integration, pytest.mark.regression]

PREFIX = "r629off-"
SCHEMA = "test_experimentation"
FLAGS = "/api/v1/feature-flags"
SAFETY = "/api/v1/safety/feature-flags"
SCHEDULES = "/api/v1/rollout-schedules"
USERS = [f"user-{i}" for i in range(200)]
MATCHED = {"country": "US"}
UNMATCHED = {"country": "FR"}

# The Python SDK's local evaluator, loaded from source: it imports only the
# standard library, and the integration job does not install the SDK.
_REPO = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "r629_sdk_evaluator", _REPO / "sdk" / "python" / "experimentation" / "evaluator.py"
)
sdk_evaluator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdk_evaluator)


# ---------------------------------------------------------------------------
# Rules, in the two stored shapes the evaluators read
# ---------------------------------------------------------------------------


def _dashboard_rule() -> dict:
    """``country equals US``, dashboard shape: no rule percentage, so 100%."""
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            }
        ],
    }


def _native_rule(rule_percentage: int, default_percentage: int | None = None) -> dict:
    """``country eq US`` at *rule_percentage*, native shape, optional default rule."""
    rules: dict = {
        "rules": [
            {
                "id": "us",
                "priority": 0,
                "rollout_percentage": rule_percentage,
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {"attribute": "country", "operator": "eq", "value": "US"}
                    ],
                },
            }
        ]
    }
    if default_percentage is not None:
        rules["default_rule"] = {
            "id": "everyone",
            "rollout_percentage": default_percentage,
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "country", "operator": "neq", "value": "US"}
                ],
            },
        }
    return rules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(db_session):
    # autoflush=False, as backend.app.db.session.SessionLocal is configured.
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def automatic_rollbacks(db_session):
    """Global automatic rollbacks on for the test, restored afterwards."""
    row = db_session.query(SafetySettings).first()
    created = row is None
    if created:
        row = SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        db_session.add(row)
        previous = None
    else:
        previous = row.enable_automatic_rollbacks
        row.enable_automatic_rollbacks = True
    db_session.commit()
    yield
    db_session.rollback()
    row = db_session.query(SafetySettings).first()
    if created:
        db_session.delete(row)
    else:
        row.enable_automatic_rollbacks = previous
    db_session.commit()


@pytest.fixture(autouse=True)
def _no_evaluation_metrics(monkeypatch):
    """Each evaluation writes and commits a metrics row; thousands of them make
    this file minutes slower and change no answer. The safety monitor's inputs
    are written by ``_unhealthy`` directly."""
    monkeypatch.setattr(
        "backend.app.services.feature_flag_service.MetricsService.record_flag_evaluation",
        lambda *args, **kwargs: None,
    )


@pytest.fixture(autouse=True)
def _remove_what_the_test_created(db_session):
    yield
    db_session.rollback()
    flag_ids = [
        row.id
        for row in db_session.query(FeatureFlag.id).filter(
            FeatureFlag.key.like(f"{PREFIX}%")
        )
    ]
    if flag_ids:
        schedule_ids = [
            row.id
            for row in db_session.query(RolloutSchedule.id).filter(
                RolloutSchedule.feature_flag_id.in_(flag_ids)
            )
        ]
        if schedule_ids:
            db_session.query(RolloutStage).filter(
                RolloutStage.rollout_schedule_id.in_(schedule_ids)
            ).delete(synchronize_session=False)
            db_session.query(RolloutSchedule).filter(
                RolloutSchedule.id.in_(schedule_ids)
            ).delete(synchronize_session=False)
        for model in (
            SafetyRollbackRecord,
            FeatureFlagSafetyConfig,
            ErrorLog,
            RawMetric,
        ):
            db_session.query(model).filter(model.feature_flag_id.in_(flag_ids)).delete(
                synchronize_session=False
            )
        db_session.query(AuditLog).filter(AuditLog.entity_id.in_(flag_ids)).delete(
            synchronize_session=False
        )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bucket(user_id: str, key: str) -> int:
    """The flag bucket, from the SDK's copy of the server's ``md5-mod100-v1``
    (the ruleset vectors pin that the two agree)."""
    return sdk_evaluator.flag_bucket(user_id, key)


def _under(percentage: int, key: str) -> set[str]:
    return {u for u in USERS if _bucket(u, key) < percentage}


def _flag(db_session, name: str, rollout: int, rules) -> FeatureFlag:
    row = FeatureFlag(
        key=f"{PREFIX}{name}",
        name=f"r629 {name}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=rollout,
        targeting_rules=rules,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _stored(db_session, flag) -> FeatureFlag:
    db_session.expire_all()
    return db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()


def _answers(client, key: str, context: dict) -> dict[str, tuple[bool, str]]:
    """``GET /evaluate`` for every user: ``{user: (enabled, reason)}``."""
    out = {}
    for user in USERS:
        resp = client.get(
            f"{FLAGS}/evaluate/{key}",
            params={"user_id": user, "context": json.dumps(context)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        out[user] = (body["enabled"], body["reason"])
    return out


def _served(session_factory, key: str, context: dict) -> dict[str, tuple[bool, str]]:
    """What the server's evaluation answers every user, without HTTP.

    ``FeatureFlagService.evaluate_flag_detailed`` on a freshly read flag is
    what ``GET /evaluate`` calls; P1 and P5 go through the routes themselves.
    """
    session = session_factory()
    try:
        row = session.query(FeatureFlag).filter(FeatureFlag.key == key).one()
        service = FeatureFlagService(session)
        out = {}
        for user in USERS:
            answer = service.evaluate_flag_detailed(row, user, context or None)
            out[user] = (answer["enabled"], answer["reason"])
        return out
    finally:
        session.close()


def _enabled(answers: dict[str, tuple[bool, str]]) -> set[str]:
    return {user for user, (enabled, _) in answers.items() if enabled}


def _rollback(client, flag, percentage: int = 0) -> dict:
    resp = client.post(
        f"{SAFETY}/{flag.id}/rollback",
        params={"percentage": percentage, "reason": "r629"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _records(db_session, flag) -> list[SafetyRollbackRecord]:
    db_session.expire_all()
    return (
        db_session.query(SafetyRollbackRecord)
        .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
        .all()
    )


def _hours_ago(hours: float) -> datetime:
    """A naive UTC timestamp, as ``rollout_stages`` stores them."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None)


def _schedule(db_session, flag) -> RolloutSchedule:
    """An ACTIVE schedule whose first stage (the flag's percentage) is due to
    complete, so the next tick starts stage 2 at 80%."""
    schedule = RolloutSchedule(
        name=f"{PREFIX}{uuid.uuid4().hex[:6]}",
        feature_flag_id=flag.id,
        status=RolloutScheduleStatus.ACTIVE,
        max_percentage=100,
        min_stage_duration=0,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)
    for order, spec in (
        (
            1,
            {
                "target_percentage": flag.rollout_percentage,
                "status": RolloutStageStatus.IN_PROGRESS,
                "updated_at": _hours_ago(30),
            },
        ),
        (2, {"target_percentage": 80, "status": RolloutStageStatus.PENDING}),
    ):
        db_session.add(
            RolloutStage(
                rollout_schedule_id=schedule.id,
                name=f"stage {order}",
                stage_order=order,
                trigger_type=TriggerType.TIME_BASED,
                trigger_configuration={"duration": 24},
                **spec,
            )
        )
    db_session.commit()
    return schedule


def _stage_ids(db_session, schedule) -> list:
    db_session.expire_all()
    return [
        row.id
        for row in db_session.query(RolloutStage)
        .filter(RolloutStage.rollout_schedule_id == schedule.id)
        .order_by(RolloutStage.stage_order)
    ]


def _schedule_status(db_session, schedule) -> RolloutScheduleStatus:
    db_session.expire_all()
    return (
        db_session.query(RolloutSchedule)
        .filter(RolloutSchedule.id == schedule.id)
        .one()
        .status
    )


def _rollout_tick(session_factory) -> None:
    from backend.app.core.rollout_scheduler import RolloutScheduler

    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()
    with patch("backend.app.core.rollout_scheduler.SessionLocal", session_factory):
        asyncio.run(scheduler.process_rollout_schedules())


def _unhealthy(db_session, flag, rollback_percentage: int = 0) -> None:
    """A safety config on the flag and a 20% error rate in the window."""
    db_session.add(
        FeatureFlagSafetyConfig(
            feature_flag_id=flag.id,
            enabled=True,
            metrics={
                "error_rate": {
                    "critical_threshold": 0.05,
                    "comparison_type": "greater_than",
                }
            },
            rollback_percentage=rollback_percentage,
        )
    )
    for _ in range(100):
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id, metric_type=MetricType.FLAG_EVALUATION.value
            )
        )
    for i in range(20):
        db_session.add(
            ErrorLog(feature_flag_id=flag.id, error_type="crash", message=f"r629 {i}")
        )
    db_session.commit()


def _safety_tick(session_factory) -> dict:
    from backend.app.core.safety_scheduler import SafetyScheduler

    scheduler = SafetyScheduler(interval_minutes=1)
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service", MagicMock()),
    ):
        return asyncio.run(scheduler.check_feature_flags_safety())


ALL_OFF = dict.fromkeys(USERS, (False, "inactive"))


# ---------------------------------------------------------------------------
# P1-P3: a rollback to 0 turns the flag off
# ---------------------------------------------------------------------------


def test_p1_a_rule_matched_user_is_off_after_a_rollback_to_0(admin_client, db_session):
    flag = _flag(db_session, "matched", 100, _dashboard_rule())
    assert _enabled(_answers(admin_client, flag.key, MATCHED)) == set(USERS)

    body = _rollback(admin_client, flag, 0)

    assert body["success"] is True, body
    # The issue's test: every rule-matched user is off, through the route.
    assert _answers(admin_client, flag.key, MATCHED) == ALL_OFF
    assert _answers(admin_client, flag.key, UNMATCHED) == ALL_OFF
    assert body["previous_percentage"] == 100
    assert body["new_percentage"] == 0
    assert body["details"]["deactivated"] is True
    assert body["details"]["paused_schedules"] == []
    assert body["message"] == (
        f"Feature flag '{flag.key}' turned off and rolled back from 100% to 0%"
    )
    stored = _stored(db_session, flag)
    assert stored.status == FeatureFlagStatus.INACTIVE
    assert stored.rollout_percentage == 0
    assert stored.targeting_rules == _dashboard_rule()  # rules are left as written


def test_p2_a_default_rule_user_is_off_after_a_rollback_to_0(
    admin_client, db_session, session_factory
):
    flag = _flag(db_session, "default", 100, _native_rule(100, default_percentage=100))
    before = _served(session_factory, flag.key, UNMATCHED)
    assert before == dict.fromkeys(USERS, (True, "targeting_rule"))

    assert _rollback(admin_client, flag, 0)["success"] is True

    assert _served(session_factory, flag.key, UNMATCHED) == ALL_OFF
    assert _served(session_factory, flag.key, MATCHED) == ALL_OFF


def test_p3_a_rule_only_flag_can_be_rolled_back(
    admin_client, db_session, session_factory
):
    flag = _flag(db_session, "rule-only", 0, _dashboard_rule())
    assert _enabled(_served(session_factory, flag.key, MATCHED)) == set(USERS)

    body = _rollback(admin_client, flag, 0)

    assert body["success"] is True, body
    assert body["previous_percentage"] == 0
    assert _stored(db_session, flag).status == FeatureFlagStatus.INACTIVE
    assert _answers(admin_client, flag.key, MATCHED) == ALL_OFF
    assert len(_records(db_session, flag)) == 1


# ---------------------------------------------------------------------------
# P4: a partial rollback lowers only the global rollout
# ---------------------------------------------------------------------------


def test_p4_a_partial_rollback_lowers_only_the_global_rollout(
    admin_client, db_session, session_factory
):
    flag = _flag(db_session, "partial", 40, _native_rule(30))
    matched_before = _enabled(_served(session_factory, flag.key, MATCHED))
    assert matched_before == _under(30, flag.key)
    assert _enabled(_served(session_factory, flag.key, UNMATCHED)) == _under(
        40, flag.key
    )

    body = _rollback(admin_client, flag, 10)

    assert body["success"] is True, body
    assert body["details"]["deactivated"] is False
    stored = _stored(db_session, flag)
    assert stored.status == FeatureFlagStatus.ACTIVE
    assert stored.rollout_percentage == 10
    assert _enabled(_served(session_factory, flag.key, UNMATCHED)) == _under(
        10, flag.key
    )
    assert _enabled(_served(session_factory, flag.key, MATCHED)) == matched_before


# ---------------------------------------------------------------------------
# P5, P6: every evaluation path agrees
# ---------------------------------------------------------------------------


def _local(flag_row, user: str, context: dict) -> dict:
    ruleset = json.loads(json.dumps(build_ruleset([flag_row])))
    indexed = sdk_evaluator.index_ruleset(ruleset)
    assert indexed is not None
    answer = sdk_evaluator.evaluate_locally(
        indexed, flag_row.key, user, sdk_evaluator.to_wire_context(context)
    )
    assert answer is not sdk_evaluator.DEFER
    return answer


@pytest.mark.parametrize("target", [0, 10], ids=["to-0", "to-10"])
def test_p5_get_post_and_user_routes_agree_after_a_rollback(
    admin_client, db_session, target
):
    flag = _flag(db_session, f"paths-{target}", 40, _native_rule(100))
    assert _rollback(admin_client, flag, target)["success"] is True

    for context in (MATCHED, UNMATCHED):
        get_answers = _answers(admin_client, flag.key, context)
        for user in USERS:
            post = admin_client.post(
                f"{FLAGS}/evaluate/{flag.key}",
                json={"user_id": user, "context": context},
            )
            assert post.status_code == 200, post.text
            assert (post.json()["enabled"], post.json()["reason"]) == get_answers[user]
            listing = admin_client.get(
                f"{FLAGS}/user/{user}", params={"context": json.dumps(context)}
            )
            assert listing.status_code == 200, listing.text
            if target == 0:
                # /user lists ACTIVE flags only: an off flag is absent, and
                # the SDK's caller default (off) applies. Pinned.
                assert flag.key not in listing.json()
            else:
                assert listing.json()[flag.key] is get_answers[user][0]


@pytest.mark.parametrize("target", [0, 10], ids=["to-0", "to-10"])
def test_p6_the_ruleset_and_local_sdk_agree_with_the_server(
    admin_client, db_session, session_factory, target
):
    flag = _flag(
        db_session, f"local-{target}", 40, _native_rule(100, default_percentage=50)
    )
    assert _rollback(admin_client, flag, target)["success"] is True
    stored = _stored(db_session, flag)
    if target == 0:
        assert build_ruleset([stored])["flags"] == [{"key": flag.key, "active": False}]

    # matched (the rule), default (the default rule) and a user with no
    # context at all, who matches neither rule's condition.
    for context in (MATCHED, UNMATCHED, {}):
        server = _served(session_factory, flag.key, context)
        for user in USERS:
            local = _local(stored, user, context)
            assert (local["enabled"], local["reason"]) == server[user], (
                user,
                context,
            )


# ---------------------------------------------------------------------------
# P8, P9: the automatic monitor
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("automatic_rollbacks")
@pytest.mark.parametrize("rollout", [50, 0], ids=["global-50", "rule-only"])
def test_p8_p9_the_monitor_turns_the_flag_off_once(
    admin_client, db_session, session_factory, rollout
):
    flag = _flag(db_session, f"monitor-{rollout}", rollout, _dashboard_rule())
    _unhealthy(db_session, flag, rollback_percentage=0)

    first = _safety_tick(session_factory)
    assert first["metadata"]["rollbacks"] >= 1, first
    stored = _stored(db_session, flag)
    assert stored.status == FeatureFlagStatus.INACTIVE
    assert stored.rollout_percentage == 0
    assert _served(session_factory, flag.key, MATCHED) == ALL_OFF

    # The error window is still hot: a second tick writes no second record.
    _safety_tick(session_factory)
    (record,) = _records(db_session, flag)
    assert record.trigger_type == "automatic"
    assert record.previous_percentage == rollout
    assert record.target_percentage == 0


@pytest.mark.usefixtures("automatic_rollbacks")
def test_p9_a_partial_automatic_rollback_is_written_once(db_session, session_factory):
    flag = _flag(db_session, "monitor-partial", 50, _dashboard_rule())
    _unhealthy(db_session, flag, rollback_percentage=5)

    _safety_tick(session_factory)
    _safety_tick(session_factory)

    stored = _stored(db_session, flag)
    assert (stored.status, stored.rollout_percentage) == (FeatureFlagStatus.ACTIVE, 5)
    assert len(_records(db_session, flag)) == 1


# ---------------------------------------------------------------------------
# P10: a rollout schedule cannot undo a rollback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [0, 10], ids=["to-0", "to-10"])
def test_p10_a_tick_and_a_stage_advance_after_a_rollback_leave_users_off(
    admin_client, db_session, session_factory, target
):
    flag = _flag(db_session, f"schedule-{target}", 50, None)
    schedule = _schedule(db_session, flag)

    body = _rollback(admin_client, flag, target)
    assert body["success"] is True, body
    assert body["details"]["paused_schedules"] == [str(schedule.id)]
    assert _schedule_status(db_session, schedule) == RolloutScheduleStatus.PAUSED

    _rollout_tick(session_factory)
    pending = _stage_ids(db_session, schedule)[1]
    advance = admin_client.post(f"{SCHEDULES}/stages/{pending}/advance")
    assert advance.status_code == 400, advance.text

    stored = _stored(db_session, flag)
    assert stored.rollout_percentage == target
    assert _schedule_status(db_session, schedule) == RolloutScheduleStatus.PAUSED
    expected_on = set() if target == 0 else _under(target, flag.key)
    assert _enabled(_served(session_factory, flag.key, UNMATCHED)) == expected_on


# ---------------------------------------------------------------------------
# P11: the documented restore steps give back the pre-rollback state
# ---------------------------------------------------------------------------


def test_p11_the_documented_restore_steps_restore_every_user(
    admin_client, db_session, session_factory
):
    flag = _flag(db_session, "restore", 40, _native_rule(30))
    schedule = _schedule(db_session, flag)
    before = {
        "matched": _served(session_factory, flag.key, MATCHED),
        "unmatched": _served(session_factory, flag.key, UNMATCHED),
    }

    body = _rollback(admin_client, flag, 0)
    assert body["success"] is True, body

    # safety.md "Re-Enabling After Rollback", in order:
    # 1. turn the flag on;
    on = admin_client.post(f"{FLAGS}/{flag.id}/activate")
    assert on.status_code == 200, on.text
    # 2. set the rollout back to the response's previous_percentage;
    put = admin_client.put(
        f"{FLAGS}/{flag.id}",
        json={"rollout_percentage": body["previous_percentage"]},
    )
    assert put.status_code == 200, put.text
    # 3. resume the paused schedule explicitly.
    resumed = admin_client.post(f"{SCHEDULES}/{schedule.id}/activate")
    assert resumed.status_code == 200, resumed.text

    after = {
        "matched": _served(session_factory, flag.key, MATCHED),
        "unmatched": _served(session_factory, flag.key, UNMATCHED),
    }
    assert after == before
    assert _schedule_status(db_session, schedule) == RolloutScheduleStatus.ACTIVE


# ---------------------------------------------------------------------------
# Refusals name their reason; nothing is written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "rollout", "target", "message"),
    [
        (FeatureFlagStatus.ARCHIVED, 50, 0, "is archived; nothing to roll back"),
        (FeatureFlagStatus.INACTIVE, 50, 0, "is already off; nothing to roll back"),
        (FeatureFlagStatus.ACTIVE, 5, 10, "is already at 5% (target 10%)"),
    ],
    ids=["archived", "inactive", "at-or-below"],
)
def test_a_rollback_that_changes_nothing_says_why(
    admin_client, db_session, status, rollout, target, message
):
    flag = _flag(db_session, f"noop-{status.value.lower()}", rollout, None)
    flag.status = status
    db_session.commit()
    schedule = _schedule(db_session, _stored(db_session, flag))

    body = _rollback(admin_client, flag, target)

    assert body["success"] is False, body
    assert message in body["message"]
    stored = _stored(db_session, flag)
    assert (stored.status, stored.rollout_percentage) == (status, rollout)
    assert _schedule_status(db_session, schedule) == RolloutScheduleStatus.ACTIVE
    assert _records(db_session, flag) == []


# ---------------------------------------------------------------------------
# PE C1: one transaction, even when the flag has no stored safety config
# ---------------------------------------------------------------------------


def test_a_rollback_commits_exactly_once_on_a_flag_with_no_config(
    db_session, session_factory
):
    flag = _flag(db_session, "one-commit", 50, _dashboard_rule())
    assert (
        db_session.query(FeatureFlagSafetyConfig)
        .filter(FeatureFlagSafetyConfig.feature_flag_id == flag.id)
        .count()
        == 0
    )

    session = session_factory()
    commits = []

    def count(s):
        # A savepoint's release also fires after_commit (the audit entry is
        # written in one, #221); it is not a commit and releases no lock.
        if not s.in_nested_transaction():
            commits.append(1)

    event.listen(session, "after_commit", count)
    try:
        result = SafetyService(session).execute_rollback(
            session, flag.id, target_percentage=0
        )
    finally:
        session.close()

    assert result.success is True, result.message
    assert len(commits) == 1
    db_session.expire_all()
    # And it gave the flag no safety config: it stays on the defaults (#1206).
    assert (
        db_session.query(FeatureFlagSafetyConfig)
        .filter(FeatureFlagSafetyConfig.feature_flag_id == flag.id)
        .count()
        == 0
    )

"""The flag list marks stale flags, against a real database (#1808).

``GET /api/v1/feature-flags/`` classifies the flags it answers with (all the
matching ones when ``stale`` is sent) and says, per item, ``stale`` and
``stale_reasons``, and at the top level ``stale_days`` and
``evaluations_seen``. The pure rules are
``backend/tests/unit/services/test_stale_flags.py``; this file holds what the
database decides: which rows count as an evaluation, the install-wide guard,
the filter and its paging, who may read it, and that reading changes nothing.

The tests share one database with every other test and nothing is truncated,
so each one lists only flags whose keys carry a prefix of its own, and decides
``evaluations_seen`` itself: most inject a time centuries ahead of every row
another test writes (each test its own, 100 days apart), so only the rows the
test writes are in the window. The time is the route's ``stale_clock``
dependency, overridden; no test reads the wall clock to decide an answer.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.read_scope import get_current_reader
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import AuditLog
from backend.app.models.compliance_audit_event import ComplianceAuditEvent
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import MetricType, RawMetric
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.sdk_evaluation_count import SdkEvaluationFlagCount
from backend.app.models.user import User, UserRole
from backend.app.services import sdk_evaluation_service
from backend.app.services.stale_flags import stale_clock
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/feature-flags/"
ACTIVE = FeatureFlagStatus.ACTIVE
INACTIVE = FeatureFlagStatus.INACTIVE
ARCHIVED = FeatureFlagStatus.ARCHIVED
NO_EVAL = "not_evaluated"
ON = "on_for_everyone"
OFF = "off_for_everyone"
EVALUATION = MetricType.FLAG_EVALUATION.value
LATENCY = MetricType.LATENCY.value
SECOND = timedelta(seconds=1)

#: Far ahead of any row another test writes; each test takes its own slot.
_FAR = datetime(2400, 1, 1, tzinfo=timezone.utc)
_SLOTS = itertools.count()


def far_now() -> datetime:
    """A time of this test's own: 100 days from any other test's."""
    return _FAR + timedelta(days=100 * next(_SLOTS))


def naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def days() -> int:
    return settings.STALE_FLAG_DAYS


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def prefix(db_session):
    """A key prefix of this test's own; its flags (and so their rows) go after."""
    word = f"stl{uuid.uuid4().hex[:10]}"
    yield word
    db_session.rollback()
    db_session.query(FeatureFlag).filter(FeatureFlag.key.like(f"{word}%")).delete(
        synchronize_session=False
    )
    db_session.commit()


def set_now(moment: datetime) -> None:
    """The time the list classifies at, through the route's own dependency."""
    app.dependency_overrides[stale_clock] = lambda: moment


def add_flag(db_session, key, *, created_at, updated_at=None, **fields):
    defaults = {
        "name": key,
        "status": ACTIVE,
        "rollout_percentage": 50,
    }
    defaults.update(fields)
    row = FeatureFlag(
        key=key,
        created_at=created_at,
        updated_at=updated_at or created_at,
        **defaults,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def add_row(db_session, flag, timestamp, metric_type=EVALUATION, **fields):
    db_session.add(
        RawMetric(
            metric_type=metric_type,
            feature_flag_id=flag.id,
            timestamp=timestamp,
            count=1,
            **fields,
        )
    )
    db_session.commit()


def listing(client, search, **params):
    response = client.get(BASE, params={"search": search, "limit": 500, **params})
    assert response.status_code == 200, response.text
    return response.json()


def by_key(body):
    return {item["key"]: item for item in body["items"]}


def reasons_of(body):
    return {item["key"]: item["stale_reasons"] for item in body["items"]}


# ---------------------------------------------------------------------------
# V7: an edit through the API restarts the count
# ---------------------------------------------------------------------------


def test_v7_a_rename_through_the_api_restarts_the_count(
    admin_client, db_session, prefix
):
    wall = datetime.utcnow()
    set_now((wall + timedelta(days=1)).replace(tzinfo=timezone.utc))
    old = wall - timedelta(days=days() + 60)
    flag = add_flag(db_session, f"{prefix}-renamed", created_at=old, status=INACTIVE)
    # Evaluated, so the only reason is the one the edit decides.
    add_row(db_session, flag, wall)

    assert reasons_of(listing(admin_client, prefix)) == {flag.key: [OFF]}

    response = admin_client.put(f"{BASE}{flag.id}", json={"name": "Renamed"})
    assert response.status_code == 200, response.text

    assert reasons_of(listing(admin_client, prefix)) == {flag.key: []}


def _turn_off(client, db_session, flag):
    response = client.post(f"{BASE}{flag.id}/deactivate")
    assert response.status_code == 200, response.text


def _bulk_disable(client, db_session, flag):
    response = client.post(
        f"{BASE}bulk-toggle", json={"flag_ids": [str(flag.id)], "action": "disable"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["success"] is True, response.text


def _safety_rollback(client, db_session, flag):
    response = client.post(
        f"/api/v1/safety/feature-flags/{flag.id}/rollback",
        params={"percentage": 0, "reason": "stale-flags"},
    )
    assert response.status_code == 200, response.text


def _rollout_step(client, db_session, flag):
    schedule = RolloutSchedule(
        name=f"Step {flag.key}",
        feature_flag_id=flag.id,
        status=RolloutScheduleStatus.ACTIVE,
        max_percentage=100,
        min_stage_duration=0,
    )
    db_session.add(schedule)
    db_session.commit()
    stage = RolloutStage(
        rollout_schedule_id=schedule.id,
        name="all",
        stage_order=1,
        target_percentage=100,
        trigger_type=TriggerType.MANUAL,
        status=RolloutStageStatus.PENDING,
    )
    db_session.add(stage)
    db_session.commit()
    response = client.post(f"/api/v1/rollout-schedules/stages/{stage.id}/advance")
    assert response.status_code == 200, response.text
    # Once the schedule is no longer ACTIVE it no longer exempts the flag, so
    # what is left is whether the step restarted the count.
    db_session.expire_all()
    db_session.get(RolloutSchedule, schedule.id).status = RolloutScheduleStatus.PAUSED
    db_session.commit()


@pytest.mark.parametrize(
    "writer",
    [_turn_off, _bulk_disable, _safety_rollback, _rollout_step],
    ids=["status change", "bulk toggle", "safety rollback", "rollout step"],
)
def test_v7_every_other_change_restarts_the_count(
    admin_client, db_session, prefix, writer
):
    """Each writer of the flag sets updated_at, so the one-answer reason goes."""
    wall = datetime.utcnow()
    set_now((wall + timedelta(days=1)).replace(tzinfo=timezone.utc))
    old = wall - timedelta(days=days() + 60)
    flag = add_flag(
        db_session,
        f"{prefix}-changed",
        created_at=old,
        rollout_percentage=100 if writer is not _rollout_step else 0,
    )
    add_row(db_session, flag, wall)
    before = ON if writer is not _rollout_step else OFF
    assert reasons_of(listing(admin_client, prefix)) == {flag.key: [before]}

    writer(admin_client, db_session, flag)

    assert reasons_of(listing(admin_client, prefix)) == {flag.key: []}


# ---------------------------------------------------------------------------
# V9: a server evaluation counts, for exactly N days; a latency row does not
# ---------------------------------------------------------------------------


def test_v9_a_post_evaluation_counts_too(admin_client, db_session, prefix):
    wall = datetime.utcnow()
    old = wall - timedelta(days=400)
    posted = add_flag(db_session, f"{prefix}-posted", created_at=old)
    never = add_flag(db_session, f"{prefix}-never", created_at=old)
    response = admin_client.post(
        f"{BASE}evaluate/{posted.key}", json={"user_id": f"user-{prefix}"}
    )
    assert response.status_code == 200, response.text
    set_now((wall + timedelta(days=1)).replace(tzinfo=timezone.utc))
    body = listing(admin_client, prefix)
    assert body["evaluations_seen"] is True
    assert reasons_of(body) == {posted.key: [], never.key: [NO_EVAL]}


def test_v9_a_server_evaluation_keeps_a_flag_fresh_for_exactly_n_days(
    admin_client, db_session, prefix
):
    old = datetime.utcnow() - timedelta(days=400)
    served = add_flag(db_session, f"{prefix}-served", created_at=old)
    latency_only = add_flag(db_session, f"{prefix}-latency", created_at=old)
    control = add_flag(db_session, f"{prefix}-control", created_at=old)

    response = admin_client.get(
        f"{BASE}evaluate/{served.key}", params={"user_id": f"user-{prefix}"}
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    evaluated_at = (
        db_session.query(func.max(RawMetric.timestamp))
        .filter(
            RawMetric.feature_flag_id == served.id,
            RawMetric.metric_type == EVALUATION,
        )
        .scalar()
    )
    assert evaluated_at is not None
    window_end = evaluated_at + timedelta(days=days())
    # Recent rows that are not evaluations, and one evaluation of another flag
    # so that the installation has seen evaluations at every time below.
    add_row(db_session, latency_only, window_end, metric_type=LATENCY, value=1.0)
    add_row(db_session, latency_only, window_end, metric_type="rule_match")
    add_row(db_session, control, window_end)

    expected = {
        "a second before N days": (window_end - SECOND, []),
        "exactly N days": (window_end, []),
        "a second after N days": (window_end + SECOND, [NO_EVAL]),
    }
    for name, (moment, served_reasons) in expected.items():
        set_now(moment.replace(tzinfo=timezone.utc))
        body = listing(admin_client, prefix)
        assert body["evaluations_seen"] is True, name
        assert reasons_of(body) == {
            served.key: served_reasons,
            latency_only.key: [NO_EVAL],
            control.key: [],
        }, name


# ---------------------------------------------------------------------------
# V11 (V10 folded in): a local report counts, after its counters are pruned
# ---------------------------------------------------------------------------


def test_v11_a_local_report_keeps_a_flag_fresh_after_its_counters_are_pruned(
    admin_client, admin_user, db_session, prefix
):
    reported_at = naive(far_now())
    old = reported_at - timedelta(days=400)
    local = add_flag(db_session, f"{prefix}-local", created_at=old)
    later = add_flag(db_session, f"{prefix}-later", created_at=old)
    key = APIKey(
        key=uuid.uuid4().hex,
        name=f"stale-{prefix}",
        user_id=admin_user.id,
        scopes="sdk:ruleset",
    )
    db_session.add(key)
    db_session.commit()

    def report(flag, at):
        sdk_evaluation_service.record_local_evaluations(
            db_session,
            key.id,
            [
                sdk_evaluation_service.EvaluationReport(
                    flag_key=flag.key,
                    count=3,
                    enabled_count=1,
                    window_start=at,
                    window_end=at,
                )
            ],
            received_at=at,
        )

    report(local, reported_at)
    # Ten minutes on, another report prunes the first one's counters.
    report(later, reported_at + timedelta(minutes=10))
    db_session.expire_all()
    assert (
        db_session.query(func.count())
        .select_from(SdkEvaluationFlagCount)
        .filter(SdkEvaluationFlagCount.flag_key == local.key)
        .scalar()
    ) == 0

    window_end = reported_at + timedelta(days=days())
    set_now((window_end - SECOND).replace(tzinfo=timezone.utc))
    assert reasons_of(listing(admin_client, prefix)) == {local.key: [], later.key: []}

    set_now((window_end + SECOND).replace(tzinfo=timezone.utc))
    body = listing(admin_client, prefix)
    assert body["evaluations_seen"] is True
    assert reasons_of(body) == {local.key: [NO_EVAL], later.key: []}


# ---------------------------------------------------------------------------
# V12: one call for every flag counts for every ACTIVE flag, and no other
# ---------------------------------------------------------------------------


def test_v12_fetching_every_flag_for_a_user_counts_for_every_active_flag(
    admin_client, db_session, prefix
):
    wall = datetime.utcnow()
    old = wall - timedelta(days=400)
    active = [
        add_flag(db_session, f"{prefix}-on-{n}", created_at=old) for n in range(2)
    ]
    inactive = add_flag(db_session, f"{prefix}-off", created_at=old, status=INACTIVE)

    response = admin_client.get(f"{BASE}user/user-{prefix}")
    assert response.status_code == 200, response.text
    assert {flag.key for flag in active} <= set(response.json())
    assert inactive.key not in response.json()

    set_now((wall + timedelta(days=1)).replace(tzinfo=timezone.utc))
    body = listing(admin_client, prefix)
    assert body["evaluations_seen"] is True
    found = reasons_of(body)
    assert [found[flag.key] for flag in active] == [[], []]
    assert found[inactive.key] == [NO_EVAL, OFF]


# ---------------------------------------------------------------------------
# V13: the install-wide guard
# ---------------------------------------------------------------------------


def test_v13_with_no_evaluation_in_n_days_not_evaluated_is_not_reported(
    admin_client, db_session, prefix
):
    now = far_now()
    at = naive(now)
    old = at - timedelta(days=400)
    target = add_flag(db_session, f"{prefix}-target", created_at=old)
    other = add_flag(db_session, f"{prefix}-other", created_at=old)
    # An evaluation older than the window, and recent rows of other kinds.
    add_row(db_session, other, at - timedelta(days=days() + 1))
    add_row(db_session, other, at - timedelta(hours=1), metric_type=LATENCY, value=2.0)
    add_row(db_session, other, at - timedelta(hours=1), metric_type="rule_match")
    set_now(now)

    body = listing(admin_client, prefix)
    assert body["evaluations_seen"] is False
    assert reasons_of(body) == {target.key: [], other.key: []}

    # One recent evaluation, of another flag, and the guard lifts.
    add_row(db_session, other, at - timedelta(hours=1))
    body = listing(admin_client, prefix)
    assert body["evaluations_seen"] is True
    assert reasons_of(body) == {target.key: [NO_EVAL], other.key: []}


# ---------------------------------------------------------------------------
# V14: the filter's exact sets
# ---------------------------------------------------------------------------


def _seven_flags(db_session, prefix, at):
    cutoff = at - timedelta(days=days())
    old = cutoff - timedelta(days=10)
    flags = {
        "not-evaluated": add_flag(db_session, f"{prefix}-a", created_at=old),
        "on": add_flag(
            db_session, f"{prefix}-b", created_at=old, rollout_percentage=100
        ),
        "off": add_flag(db_session, f"{prefix}-c", created_at=old, status=INACTIVE),
        "boundary": add_flag(
            db_session, f"{prefix}-d", created_at=cutoff, status=INACTIVE
        ),
        "permanent": add_flag(
            db_session,
            f"{prefix}-e",
            created_at=old,
            status=INACTIVE,
            tags=["checkout", "permanent"],
        ),
        "archived": add_flag(
            db_session, f"{prefix}-f", created_at=old, status=ARCHIVED
        ),
        "new": add_flag(
            db_session,
            f"{prefix}-g",
            created_at=cutoff + SECOND,
            status=INACTIVE,
        ),
    }
    for name in ("on", "off", "boundary"):
        add_row(db_session, flags[name], at - timedelta(hours=1))
    return flags


def test_v14_stale_true_false_and_omitted_are_exact_sets(
    admin_client, db_session, prefix
):
    now = far_now()
    flags = _seven_flags(db_session, prefix, naive(now))
    set_now(now)
    key = {name: flag.key for name, flag in flags.items()}
    stale = {key["not-evaluated"], key["on"], key["off"], key["boundary"]}

    every = listing(admin_client, prefix)
    assert every["total"] == 7
    assert every["stale_days"] == days()
    assert every["evaluations_seen"] is True
    assert reasons_of(every) == {
        key["not-evaluated"]: [NO_EVAL],
        key["on"]: [ON],
        key["off"]: [OFF],
        key["boundary"]: [OFF],
        key["permanent"]: [],
        key["archived"]: [],
        key["new"]: [],
    }
    assert {k for k, item in by_key(every).items() if item["stale"]} == stale

    only = listing(admin_client, prefix, stale="true")
    assert only["total"] == 4
    assert set(by_key(only)) == stale
    assert all(item["stale"] for item in only["items"])

    rest = listing(admin_client, prefix, stale="false")
    assert rest["total"] == 3
    assert set(by_key(rest)) == set(key.values()) - stale
    assert not any(item["stale"] or item["stale_reasons"] for item in rest["items"])


def test_the_list_uses_stale_flag_days(admin_client, db_session, prefix, monkeypatch):
    now = far_now()
    at = naive(now)
    flag = add_flag(
        db_session, f"{prefix}-week", created_at=at - timedelta(days=8), status=INACTIVE
    )
    add_row(db_session, flag, at - timedelta(hours=1))
    set_now(now)

    monkeypatch.setattr(settings, "STALE_FLAG_DAYS", 7)
    body = listing(admin_client, prefix)
    assert body["stale_days"] == 7
    assert reasons_of(body) == {flag.key: [OFF]}

    monkeypatch.setattr(settings, "STALE_FLAG_DAYS", 9)
    body = listing(admin_client, prefix)
    assert body["stale_days"] == 9
    assert reasons_of(body) == {flag.key: []}


# ---------------------------------------------------------------------------
# V15, V16: the filter pages, and combines with status and search
# ---------------------------------------------------------------------------


def test_v15_the_filter_comes_before_the_paging(admin_client, db_session, prefix):
    now = far_now()
    at = naive(now)
    start = at - timedelta(days=days() + 100)
    stale_positions = {3, 11, 20}
    flags = [
        add_flag(
            db_session,
            f"{prefix}-{n:02d}",
            created_at=start + timedelta(minutes=n),
            status=INACTIVE if n in stale_positions else ACTIVE,
        )
        for n in range(25)
    ]
    # No evaluation in the window: only the one-answer reasons apply.
    set_now(now)
    stale_keys = [flags[n].key for n in sorted(stale_positions)]

    page = listing(admin_client, prefix, stale="true", skip=2, limit=2)
    assert page["evaluations_seen"] is False
    assert page["total"] == 3
    assert [item["key"] for item in page["items"]] == stale_keys[2:]
    assert (page["skip"], page["limit"]) == (2, 2)

    first = listing(admin_client, prefix, stale="true", skip=0, limit=2)
    assert [item["key"] for item in first["items"]] == stale_keys[:2]

    rest = listing(admin_client, prefix, stale="false", skip=20, limit=10)
    assert rest["total"] == 22
    assert [item["key"] for item in rest["items"]] == [
        flag.key for flag in flags if flag.key not in stale_keys
    ][20:]


def test_v16_the_filter_combines_with_status_and_search(
    admin_client, db_session, prefix
):
    now = far_now()
    at = naive(now)
    old = at - timedelta(days=days() + 10)
    wanted = add_flag(db_session, f"{prefix}-x-off", created_at=old, status=INACTIVE)
    add_flag(db_session, f"{prefix}-x-on", created_at=old, rollout_percentage=100)
    add_flag(db_session, f"{prefix}-x-new", created_at=at, status=INACTIVE)
    add_flag(db_session, f"{prefix}-y-off", created_at=old, status=INACTIVE)
    set_now(now)

    body = listing(admin_client, f"{prefix}-x", stale="true", status="inactive")
    assert body["total"] == 1
    assert [item["key"] for item in body["items"]] == [wanted.key]

    both = listing(admin_client, f"{prefix}-x", stale="true")
    assert both["total"] == 2


# ---------------------------------------------------------------------------
# V17: what the parameter accepts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["maybe", "2", ""])
def test_v17_a_value_that_is_not_a_boolean_answers_422(admin_client, prefix, value):
    response = admin_client.get(BASE, params={"search": prefix, "stale": value})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["query", "stale"]


@pytest.mark.parametrize("value", ["true", "false", "True", "1", "0"])
def test_v17_true_and_false_are_accepted(admin_client, prefix, value):
    response = admin_client.get(BASE, params={"search": prefix, "stale": value})
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# V18: who may read it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER]
)
def test_v18_every_role_reads_the_marker(db_session, prefix, role):
    creator = _user(db_session, UserRole.DEVELOPER)
    client = make_client_for_user(db_session, creator)
    response = client.post(BASE, json={"key": f"{prefix}-made", "name": "Made"})
    assert response.status_code == 201, response.text

    reader = _user(db_session, role)
    client = make_client_for_user(db_session, reader)
    set_now(far_now())
    body = listing(client, prefix)
    assert [item["key"] for item in body["items"]] == [f"{prefix}-made"]
    assert body["stale_days"] == days()
    assert body["evaluations_seen"] is False
    (item,) = body["items"]
    assert (item["stale"], item["stale_reasons"]) == (True, [OFF])


def test_v18_an_mcp_read_key_reads_the_marker(db_session, prefix, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    now = far_now()
    add_flag(
        db_session,
        f"{prefix}-keyed",
        created_at=naive(now) - timedelta(days=400),
        status=INACTIVE,
    )
    owner = _user(db_session, UserRole.VIEWER)
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    # Nothing stands in for the real authentication: the key is checked.
    for dependency in (
        get_current_reader,
        deps.get_api_key,
        deps.get_current_user,
        deps.get_current_active_user,
    ):
        app.dependency_overrides.pop(dependency, None)
    app.dependency_overrides[deps.get_db] = override_get_db
    set_now(now)
    client = TestClient(app, raise_server_exceptions=False)
    bearer = {"Authorization": f"Bearer {create_local_access_token(owner)}"}
    created = client.post(
        "/api/v1/api-keys",
        json={"name": f"mcp-{prefix}", "scopes": ["mcp:read"]},
        headers=bearer,
    )
    assert created.status_code == 201, created.text

    response = client.get(
        BASE,
        params={"search": prefix, "stale": "true"},
        headers={"X-API-Key": created.json()["key"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total"], body["stale_days"], body["evaluations_seen"]) == (
        1,
        days(),
        False,
    )
    assert body["items"][0]["stale_reasons"] == [OFF]


# ---------------------------------------------------------------------------
# The documented way to keep a flag: tag it permanent through the API
# ---------------------------------------------------------------------------


def test_tagging_a_flag_permanent_through_the_api_keeps_it_off_the_list(
    admin_client, db_session, prefix
):
    """The page's example: read the tags, add permanent, PUT the whole list."""
    created = admin_client.post(
        BASE, json={"key": f"{prefix}-kept", "name": "Kept", "tags": ["checkout"]}
    )
    assert created.status_code == 201, created.text
    flag_id = created.json()["id"]
    set_now(far_now())
    (item,) = listing(admin_client, prefix)["items"]
    assert (item["stale"], item["stale_reasons"]) == (True, [OFF])

    tags = admin_client.get(f"{BASE}{flag_id}").json()["tags"]
    response = admin_client.put(
        f"{BASE}{flag_id}", json={"tags": sorted(set(tags or []) | {"permanent"})}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tags"] == ["checkout", "permanent"]
    (item,) = listing(admin_client, prefix)["items"]
    assert (item["stale"], item["stale_reasons"]) == (False, [])

    # A PUT replaces the list: one that sends only "permanent" drops "checkout".
    response = admin_client.put(f"{BASE}{flag_id}", json={"tags": ["Permanent "]})
    assert response.json()["tags"] == ["Permanent "]
    assert listing(admin_client, prefix)["items"][0]["stale"] is False


def _user(db_session, role):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"stale_{role.value}_{suffix}",
        email=f"stale_{role.value}_{suffix}@stale.test",
        full_name="Stale Flags User",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# V19: listing changes no flag
# ---------------------------------------------------------------------------


def test_v19_listing_changes_no_flag_and_records_nothing(
    admin_client, db_session, prefix
):
    now = far_now()
    _seven_flags(db_session, prefix, naive(now))
    set_now(now)
    table = FeatureFlag.__table__

    def snapshot():
        db_session.expire_all()
        flags = (
            db_session.execute(
                select(table)
                .where(table.c.key.like(f"{prefix}%"))
                .order_by(table.c.key)
            )
            .mappings()
            .all()
        )
        ids = [row["id"] for row in flags]
        return {
            "flags": [dict(row) for row in flags],
            "metrics": db_session.query(func.count())
            .select_from(RawMetric)
            .filter(RawMetric.feature_flag_id.in_(ids))
            .scalar(),
            "audit": db_session.query(func.count()).select_from(AuditLog).scalar(),
            "compliance": db_session.query(func.count())
            .select_from(ComplianceAuditEvent)
            .scalar(),
        }

    before = snapshot()
    assert len(before["flags"]) == 7
    for params in ({}, {"stale": "true"}, {"stale": "false"}):
        listing(admin_client, prefix, **params)
    assert snapshot() == before


# ---------------------------------------------------------------------------
# V20: the number of statements does not grow with the flags
# ---------------------------------------------------------------------------


def test_v20_the_statements_do_not_grow_with_the_number_of_flags(
    admin_client, db_session, prefix
):
    now = far_now()
    at = naive(now)
    old = at - timedelta(days=400)
    rows = [
        FeatureFlag(
            key=f"{prefix}{size}-{n:03d}",
            name="Counted",
            status=ACTIVE,
            rollout_percentage=50,
            created_at=old + timedelta(seconds=n),
            updated_at=old,
        )
        for size, count in (("s", 5), ("b", 200))
        for n in range(count)
    ]
    db_session.add_all(rows)
    db_session.commit()
    # The installation has seen evaluations, so every flag's are looked up.
    add_row(db_session, rows[0], at - timedelta(hours=1))
    add_row(db_session, rows[5], at - timedelta(hours=1))
    set_now(now)

    engine = db_session.get_bind()
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    def run(search, **params):
        statements.clear()
        body = listing(admin_client, search, **params)
        return len(statements), body

    event.listen(engine, "before_cursor_execute", count)
    try:
        run(f"{prefix}s")  # warm-up
        # One flag of each set was evaluated; the others are not_evaluated.
        for params, items in (
            ({}, (5, 200)),
            ({"stale": "true"}, (4, 199)),
            ({"stale": "false"}, (1, 1)),
        ):
            small, small_body = run(f"{prefix}s", **params)
            big, big_body = run(f"{prefix}b", **params)
            assert (len(small_body["items"]), len(big_body["items"])) == items
            assert small == big, (params, small, big)
    finally:
        event.remove(engine, "before_cursor_execute", count)


# ---------------------------------------------------------------------------
# A list item can be sent back: create and update ignore the two stale fields
# ---------------------------------------------------------------------------


def test_a_list_item_sent_back_is_accepted_and_its_stale_fields_ignored(
    admin_client, db_session, prefix
):
    now = far_now()
    add_flag(
        db_session,
        f"{prefix}-source",
        created_at=naive(now) - timedelta(days=400),
        status=INACTIVE,
        tags=["checkout"],
    )
    set_now(now)
    (item,) = listing(admin_client, prefix)["items"]
    assert item["stale"] is True

    copy = {**item, "key": f"{prefix}-copy", "status": "inactive"}
    created = admin_client.post(BASE, json=copy)
    assert created.status_code == 201, created.text
    assert set(created.json()) == set(item) - {"stale", "stale_reasons"}

    updated = admin_client.put(f"{BASE}{item['id']}", json=item)
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["checkout"]


# ---------------------------------------------------------------------------
# V25: a flag with an ACTIVE rollout schedule is never stale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schedule_status, expected",
    [
        (RolloutScheduleStatus.ACTIVE, []),
        (RolloutScheduleStatus.PAUSED, [OFF]),
        (RolloutScheduleStatus.DRAFT, [OFF]),
        (RolloutScheduleStatus.COMPLETED, [OFF]),
        (RolloutScheduleStatus.CANCELLED, [OFF]),
    ],
    ids=["active", "paused", "draft", "completed", "cancelled"],
)
def test_v25_an_active_rollout_schedule_exempts_a_flag(
    admin_client, db_session, prefix, schedule_status, expected
):
    now = far_now()
    at = naive(now)
    flag = add_flag(
        db_session,
        f"{prefix}-scheduled",
        created_at=at - timedelta(days=days() + 1),
        rollout_percentage=0,
    )
    add_row(db_session, flag, at - timedelta(hours=1))
    db_session.add(
        RolloutSchedule(
            name=f"Launch {prefix}",
            feature_flag_id=flag.id,
            status=schedule_status,
            max_percentage=100,
        )
    )
    db_session.commit()
    set_now(now)

    (item,) = listing(admin_client, prefix)["items"]
    assert (item["stale"], item["stale_reasons"]) == (bool(expected), expected)

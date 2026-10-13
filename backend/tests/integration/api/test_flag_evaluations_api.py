"""
Integration tests for ``POST /api/v1/tracking/evaluations`` (beta).

Real database and real API-key authentication: only ``deps.get_db`` is
overridden, keys are created through ``POST /api/v1/api-keys``, and nothing
replaces ``deps.get_api_key`` -- so the ``sdk:ruleset`` check runs on the key
presented, exactly as in production.

Covered:
* the scope: who is refused, with what;
* what is written: one ``flag_evaluation`` row per flag, stamped with the
  server's receive time, which safety monitoring then counts;
* the ceilings: per key/flag/minute and per flag/minute across keys, and that
  two reports in parallel cannot both get under the per-key ceiling;
* 7c: locally evaluated traffic plus reported errors rolls the flag back
  through the real safety scheduler, deterministically (no scheduler wait).
"""

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps, sdk_scope
from backend.app.api.v1.endpoints import flag_evaluations
from backend.app.core.config import settings
from backend.app.core.safety_scheduler import SafetyScheduler
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    AggregationPeriod,
    ErrorLog,
    MetricType,
    RawMetric,
)
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.models.sdk_evaluation_count import (
    SdkEvaluationFlagCount,
    SdkEvaluationKeyCount,
)
from backend.app.models.user import User, UserRole
from backend.app.services import sdk_evaluation_service
from backend.app.services.metrics_service import MetricsService
from backend.app.services.safety_service import SafetyService

pytestmark = pytest.mark.integration

URL = "/api/v1/tracking/evaluations"
KEYS_URL = "/api/v1/api-keys"
SCHEMA = "test_experimentation"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def session_factory(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def developer(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"evals_{suffix}",
        email=f"evals_{suffix}@evals.test",
        full_name="Evaluations User",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _create_key(client, user, scopes=None) -> dict:
    body = {"name": f"key-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    resp = client.post(
        KEYS_URL,
        json=body,
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture
def scoped_key(client, developer) -> dict:
    return _create_key(client, developer, scopes=["sdk:ruleset"])


@pytest.fixture
def flags(db_session, make_feature_flag):
    """Factory for ACTIVE flags; everything written against them is removed."""
    created = []

    def make(rollout_percentage=50):
        flag = make_feature_flag(
            key=f"evals-{uuid.uuid4().hex[:10]}",
            name="Evaluations Flag",
            status=FeatureFlagStatus.ACTIVE,
            rollout_percentage=rollout_percentage,
        )
        created.append(flag)
        return flag

    yield make

    db_session.rollback()
    for flag in created:
        for model, column in (
            (RawMetric, RawMetric.feature_flag_id),
            (ErrorLog, ErrorLog.feature_flag_id),
            (SafetyRollbackRecord, SafetyRollbackRecord.feature_flag_id),
            (FeatureFlagSafetyConfig, FeatureFlagSafetyConfig.feature_flag_id),
        ):
            db_session.query(model).filter(column == flag.id).delete(
                synchronize_session=False
            )
        for model in (SdkEvaluationKeyCount, SdkEvaluationFlagCount):
            db_session.query(model).filter(model.flag_key == flag.key).delete(
                synchronize_session=False
            )
        db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.commit()


#: One fixed receive time for the tests that must stay inside one minute.
FROZEN_NOW = datetime.utcnow().replace(second=30, microsecond=0) - timedelta(minutes=1)


class _FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return FROZEN_NOW


@pytest.fixture
def frozen_minute(monkeypatch):
    """Every report in the test is received in the same minute."""
    monkeypatch.setattr(flag_evaluations, "datetime", _FrozenDatetime)
    return FROZEN_NOW


def _entry(flag_key, count, enabled_count=0, window_end=None):
    end = window_end or datetime.now(timezone.utc)
    return {
        "flag_key": flag_key,
        "count": count,
        "enabled_count": enabled_count,
        "window_start": (end - timedelta(seconds=60)).isoformat(),
        "window_end": end.isoformat(),
    }


def _post(client, key, *entries):
    return client.post(
        URL, json={"evaluations": list(entries)}, headers={"X-API-Key": key}
    )


def _evaluation_rows(db_session, flag):
    db_session.expire_all()
    return (
        db_session.query(RawMetric)
        .filter(
            RawMetric.feature_flag_id == flag.id,
            RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
        )
        .all()
    )


def _recorded(db_session, flag) -> int:
    return sum(row.count for row in _evaluation_rows(db_session, flag))


# ---------------------------------------------------------------------------
# The scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_a_key_with_the_scope_is_accepted(self, client, scoped_key, flags):
        flag = flags()
        resp = _post(client, scoped_key["key"], _entry(flag.key, 10))
        assert resp.status_code == 201, resp.text
        assert resp.json() == {"accepted": 10, "errors": []}

    def test_no_key_is_401(self, client, flags):
        flag = flags()
        resp = client.post(URL, json={"evaluations": [_entry(flag.key, 1)]})
        assert resp.status_code == 401, resp.text

    def test_an_unknown_key_is_401(self, client, flags):
        flag = flags()
        resp = _post(client, "eptk_" + "0" * 32, _entry(flag.key, 1))
        assert resp.status_code == 401, resp.text

    @pytest.mark.parametrize(
        "stored",
        [None, "SDK:RULESET", "xsdk:ruleset", "sdk:ruleset-ro", "read,write"],
        ids=["null", "upper-case", "prefixed", "suffixed", "other-scopes"],
    )
    def test_a_key_without_exactly_the_scope_is_403(
        self, client, db_session, developer, flags, stored
    ):
        created = _create_key(client, developer)
        db_session.query(APIKey).filter(APIKey.id == uuid.UUID(created["id"])).update(
            {"scopes": stored}, synchronize_session=False
        )
        db_session.commit()
        flag = flags()

        resp = _post(client, created["key"], _entry(flag.key, 5))

        assert resp.status_code == 403, resp.text
        assert "sdk:ruleset" in resp.json()["detail"]
        assert _recorded(db_session, flag) == 0

    def test_an_unnormalised_stored_scope_list_is_accepted(
        self, client, db_session, developer, flags
    ):
        created = _create_key(client, developer)
        db_session.query(APIKey).filter(APIKey.id == uuid.UUID(created["id"])).update(
            {"scopes": "read, sdk:ruleset "}, synchronize_session=False
        )
        db_session.commit()
        flag = flags()

        resp = _post(client, created["key"], _entry(flag.key, 5))

        assert resp.status_code == 201, resp.text

    def test_the_same_users_unscoped_key_is_403(
        self, client, db_session, developer, scoped_key, flags
    ):
        """The presented key decides, not any key its owner holds."""
        unscoped = _create_key(client, developer)
        flag = flags()

        resp = _post(client, unscoped["key"], _entry(flag.key, 5))

        assert resp.status_code == 403, resp.text
        assert _recorded(db_session, flag) == 0


# ---------------------------------------------------------------------------
# The owner's role: the scope takes effect only while its owner can change flags
# ---------------------------------------------------------------------------


def _make_actor(db_session, role, is_superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"evals_{role.value}_{suffix}",
        email=f"evals_{role.value}_{suffix}@evals.test",
        full_name="Evaluations Actor",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _stored_scoped_key(client, db_session, user) -> dict:
    """A key created through the API, its scope written the way a 0.7.0 database
    could hold it (0.7.0 let any role create an ``sdk:ruleset`` key)."""
    created = _create_key(client, user)
    db_session.query(APIKey).filter(APIKey.id == uuid.UUID(created["id"])).update(
        {"scopes": "sdk:ruleset"}, synchronize_session=False
    )
    db_session.commit()
    return created


class TestOwnerRole:
    @pytest.mark.parametrize(
        ("role", "is_superuser", "expected"),
        [
            (UserRole.VIEWER, True, 201),
            (UserRole.ADMIN, False, 201),
            (UserRole.DEVELOPER, False, 201),
            (UserRole.ANALYST, False, 403),
            (UserRole.VIEWER, False, 403),
        ],
        ids=["superuser", "admin", "developer", "analyst", "viewer"],
    )
    def test_each_actor(self, client, db_session, flags, role, is_superuser, expected):
        owner = _make_actor(db_session, role, is_superuser=is_superuser)
        key = _stored_scoped_key(client, db_session, owner)
        flag = flags()

        resp = _post(client, key["key"], _entry(flag.key, 5))

        assert resp.status_code == expected, resp.text
        if expected == 403:
            assert resp.json()["detail"] == sdk_scope.OWNER_ROLE_DETAIL
            assert _recorded(db_session, flag) == 0
        else:
            assert _recorded(db_session, flag) == 5

    @pytest.mark.regression
    def test_a_developer_key_stops_working_when_its_owner_is_demoted(
        self, client, db_session, flags
    ):
        owner = _make_actor(db_session, UserRole.DEVELOPER)
        key = _create_key(client, owner, scopes=["sdk:ruleset"])
        flag = flags()
        assert _post(client, key["key"], _entry(flag.key, 5)).status_code == 201

        db_session.query(User).filter(User.id == owner.id).update(
            {"role": UserRole.VIEWER}, synchronize_session=False
        )
        db_session.commit()

        resp = _post(client, key["key"], _entry(flag.key, 5))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == sdk_scope.OWNER_ROLE_DETAIL
        assert _recorded(db_session, flag) == 5

    @pytest.mark.regression
    def test_a_stored_scope_on_a_viewers_key_is_refused(
        self, client, db_session, flags
    ):
        owner = _make_actor(db_session, UserRole.VIEWER)
        key = _stored_scoped_key(client, db_session, owner)
        flag = flags()

        resp = _post(client, key["key"], _entry(flag.key, 5))

        assert resp.status_code == 403, resp.text
        assert _recorded(db_session, flag) == 0


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


class TestRecording:
    def test_one_row_per_flag_stamped_with_the_server_receive_time(
        self, client, db_session, scoped_key, flags
    ):
        flag = flags()
        # A window that ended an hour ago: the row still lands "now", inside
        # the safety monitor's 15-minute window.
        stale_end = datetime.now(timezone.utc) - timedelta(hours=1)
        before = datetime.utcnow()
        resp = _post(
            client,
            scoped_key["key"],
            _entry(flag.key, 300, enabled_count=120, window_end=stale_end),
        )
        after = datetime.utcnow()
        assert resp.status_code == 201, resp.text

        rows = _evaluation_rows(db_session, flag)
        assert len(rows) == 1
        row = rows[0]
        assert row.count == 300
        assert row.user_id is None
        assert before - timedelta(seconds=1) <= row.timestamp <= after
        assert row.meta_data["source"] == "sdk_local"
        assert row.meta_data["api_key_id"] == scoped_key["id"]
        assert row.meta_data["enabled_count"] == 120
        assert row.meta_data["window_end"].startswith(
            stale_end.replace(tzinfo=None).isoformat()[:16]
        )

    def test_the_safety_denominator_counts_the_rows(
        self, client, db_session, scoped_key, flags
    ):
        flag = flags()
        assert (
            _post(client, scoped_key["key"], _entry(flag.key, 400)).status_code == 201
        )
        db_session.expire_all()
        metrics = SafetyService(db_session).get_error_metrics(db_session, flag.id)
        assert metrics["total_evaluations"] == 400

    def test_entries_for_the_same_flag_are_merged(
        self, client, db_session, scoped_key, flags
    ):
        flag = flags()
        resp = _post(
            client,
            scoped_key["key"],
            _entry(flag.key, 10, 1),
            _entry(flag.key, 15, 2),
        )
        assert resp.json() == {"accepted": 25, "errors": []}
        rows = _evaluation_rows(db_session, flag)
        assert [(r.count, r.meta_data["enabled_count"]) for r in rows] == [(25, 3)]

    def test_an_unknown_flag_is_skipped_and_reported(
        self, client, db_session, scoped_key, flags
    ):
        flag = flags()
        missing = f"missing-{uuid.uuid4().hex[:8]}"
        resp = _post(client, scoped_key["key"], _entry(flag.key, 7), _entry(missing, 3))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["accepted"] == 7
        assert body["errors"] == [
            {
                "flag_key": missing,
                "code": "unknown_flag",
                "message": f"Feature flag with key '{missing}' not found",
                "requested": 3,
                "accepted": 0,
            }
        ]

    @pytest.mark.parametrize(
        "entry",
        [
            {"count": 0},
            {"count": 1_000_001},
            {"count": 5, "enabled_count": 6},
            {"count": 5, "enabled_count": -1},
            {"window_minutes": 11},
            {"window_minutes": -1},
        ],
        ids=[
            "zero",
            "over-entry-max",
            "enabled-over-count",
            "negative-enabled",
            "long-window",
            "reversed-window",
        ],
    )
    def test_a_malformed_entry_is_422_and_writes_nothing(
        self, client, db_session, scoped_key, flags, entry
    ):
        flag = flags()
        body = _entry(flag.key, 5)
        body.update({k: v for k, v in entry.items() if k != "window_minutes"})
        if "window_minutes" in entry:
            end = datetime.now(timezone.utc)
            body["window_end"] = end.isoformat()
            body["window_start"] = (
                end - timedelta(minutes=entry["window_minutes"])
            ).isoformat()
        resp = _post(client, scoped_key["key"], body)
        assert resp.status_code == 422, resp.text
        assert _recorded(db_session, flag) == 0

    def test_more_than_1000_entries_is_422(self, client, scoped_key, flags):
        flag = flags()
        resp = _post(client, scoped_key["key"], *[_entry(flag.key, 1)] * 1001)
        assert resp.status_code == 422, resp.text

    def test_local_counts_add_evaluations_that_carry_no_user_and_no_rule(
        self, client, db_session, scoped_key, flags
    ):
        """A local count raises the flag's evaluations and nothing else: its
        rule-match rate falls and its unique users stay those the server saw."""
        flag = flags()
        MetricsService.record_flag_evaluation(
            db_session, flag.id, "user-1", value=True, targeting_rule_id="rule-1"
        )
        assert _post(client, scoped_key["key"], _entry(flag.key, 9)).status_code == 201
        db_session.expire_all()
        try:
            MetricsService.aggregate_metrics(
                db_session, AggregationPeriod.DAY, feature_flag_id=flag.id
            )
            summary = MetricsService.get_metrics_summary(
                db_session, feature_flag_id=flag.id
            )
            assert summary.total_evaluations == 10
            assert summary.rule_match_rate == pytest.approx(10.0)
            assert summary.unique_users == 1
        finally:
            db_session.rollback()
            db_session.query(AggregatedMetric).filter(
                AggregatedMetric.feature_flag_id == flag.id
            ).delete(synchronize_session=False)
            db_session.commit()


# ---------------------------------------------------------------------------
# The ceilings
# ---------------------------------------------------------------------------


class TestCeilings:
    def test_one_key_is_capped_at_100000_per_flag_per_minute(
        self, client, db_session, scoped_key, flags, frozen_minute
    ):
        flag = flags()
        first = _post(client, scoped_key["key"], _entry(flag.key, 150_000)).json()
        second = _post(client, scoped_key["key"], _entry(flag.key, 10)).json()

        assert first["accepted"] == 100_000
        assert first["errors"][0]["code"] == "ceiling"
        assert first["errors"][0]["requested"] == 150_000
        assert first["errors"][0]["accepted"] == 100_000
        assert second["accepted"] == 0
        assert second["errors"][0]["code"] == "ceiling"
        assert _recorded(db_session, flag) == 100_000

        counter = (
            db_session.query(SdkEvaluationKeyCount)
            .filter(SdkEvaluationKeyCount.flag_key == flag.key)
            .one()
        )
        # The key id is on the counter, and it holds everything requested.
        assert str(counter.api_key_id) == scoped_key["id"]
        assert counter.minute == frozen_minute.replace(second=0)
        assert counter.requested == 150_010

    def test_the_cap_is_per_flag(
        self, client, db_session, scoped_key, flags, frozen_minute
    ):
        a, b = flags(), flags()
        body = _post(
            client, scoped_key["key"], _entry(a.key, 100_000), _entry(b.key, 100_000)
        ).json()
        assert body == {"accepted": 200_000, "errors": []}

    def test_all_keys_together_are_capped_at_1000000_per_flag_per_minute(
        self, client, db_session, developer, flags, frozen_minute
    ):
        flag = flags()
        keys = [
            _create_key(client, developer, scopes=["sdk:ruleset"])["key"]
            for _ in range(11)
        ]
        accepted = [
            _post(client, key, _entry(flag.key, 100_000)).json()["accepted"]
            for key in keys
        ]

        assert accepted == [100_000] * 10 + [0]
        assert _recorded(db_session, flag) == 1_000_000
        db_session.expire_all()
        total = (
            db_session.query(SdkEvaluationFlagCount)
            .filter(SdkEvaluationFlagCount.flag_key == flag.key)
            .one()
        )
        assert total.offered == 1_100_000

    def test_a_new_minute_starts_a_new_count(
        self, client, db_session, scoped_key, flags, monkeypatch
    ):
        flag = flags()
        monkeypatch.setattr(flag_evaluations, "datetime", _FrozenDatetime)
        assert (
            _post(client, scoped_key["key"], _entry(flag.key, 100_000)).json()[
                "accepted"
            ]
            == 100_000
        )

        class _NextMinute(datetime):
            @classmethod
            def utcnow(cls):  # type: ignore[override]
                return FROZEN_NOW + timedelta(minutes=1)

        monkeypatch.setattr(flag_evaluations, "datetime", _NextMinute)
        assert (
            _post(client, scoped_key["key"], _entry(flag.key, 5)).json()["accepted"]
            == 5
        )

    def test_two_reports_in_parallel_share_one_ceiling(
        self, client, db_session, scoped_key, flags, frozen_minute
    ):
        """Two requests at once for the same key and flag: 60k + 60k -> 100k."""
        flag = flags()
        barrier = threading.Barrier(2)
        results = []

        def send():
            barrier.wait()
            results.append(_post(client, scoped_key["key"], _entry(flag.key, 60_000)))

        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
        assert sorted(r.json()["accepted"] for r in results) == [40_000, 60_000]
        assert _recorded(db_session, flag) == 100_000


class TestAtomicity:
    """The per-key counter cannot be overrun by two transactions at once.

    Deterministic: transaction A takes 60,000 and stays open; transaction B
    asks for 60,000 while A is uncommitted. B must wait for A's row lock and
    then get the 40,000 left. A read-then-write counter lets B read 0 and take
    60,000 as well -- 120,000 against a ceiling of 100,000.
    """

    def test_a_concurrent_reservation_sees_the_uncommitted_one(
        self, session_factory, db_session, scoped_key, flags
    ):
        flag = flags()
        key_id = uuid.UUID(scoped_key["id"])
        minute = sdk_evaluation_service.minute_of(datetime.utcnow())
        a, b, probe = session_factory(), session_factory(), session_factory()
        outcome = {}
        try:
            got_a = sdk_evaluation_service.reserve(a, key_id, flag.key, minute, 60_000)
            assert got_a == 60_000

            b_pid = b.execute(text("SELECT pg_backend_pid()")).scalar_one()

            def run_b():
                try:
                    outcome["b"] = sdk_evaluation_service.reserve(
                        b, key_id, flag.key, minute, 60_000
                    )
                    b.commit()
                except Exception as exc:  # pragma: no cover - reported below
                    outcome["error"] = exc
                    b.rollback()

            thread = threading.Thread(target=run_b)
            thread.start()
            # Wait until B is blocked on A's lock, or has finished without
            # blocking (which is itself the defect).
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and thread.is_alive():
                waiting = probe.execute(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": b_pid},
                ).scalar_one_or_none()
                probe.rollback()
                if waiting == "Lock":
                    break
                time.sleep(0.05)
            a.commit()
            thread.join(timeout=20)
        finally:
            for session in (a, b, probe):
                session.close()

        assert "error" not in outcome, outcome.get("error")
        assert outcome["b"] == 40_000, (
            f"B was granted {outcome['b']} after A took 60000: the per-key "
            "ceiling of 100000 was overrun"
        )


# ---------------------------------------------------------------------------
# 7c: local evaluations + reported errors -> the safety monitor rolls back
# ---------------------------------------------------------------------------


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


@pytest.mark.regression
@pytest.mark.asyncio
async def test_7c_locally_evaluated_flag_is_rolled_back_by_the_safety_monitor(
    client, db_session, session_factory, scoped_key, flags, automatic_rollbacks
):
    """Local evaluations + reported errors -> an automatic rollback.

    The real scheduler tick (``SafetyScheduler.check_feature_flags_safety``) is
    run directly against the test database: no interval, no wall-clock wait.
    Without the evaluation rows the error rate reads 0.0 (nothing to divide
    by), the flag looks healthy and nothing is rolled back.
    """
    flag = flags(rollout_percentage=50)
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
            rollback_percentage=5,
        )
    )
    db_session.commit()

    # An SDK evaluated the flag 1,000 times locally and reported the count ...
    sent = _post(client, scoped_key["key"], _entry(flag.key, 1000, enabled_count=500))
    assert sent.status_code == 201, sent.text
    # ... and its users hit 100 errors (a 10% error rate).
    errors = client.post(
        "/api/v1/tracking/errors/batch",
        json={
            "errors": [
                {
                    "feature_flag_key": flag.key,
                    "error_type": "crash",
                    "message": f"boom {i}",
                }
                for i in range(100)
            ]
        },
        headers={"X-API-Key": scoped_key["key"]},
    )
    assert errors.status_code == 200, errors.text
    assert errors.json()["success_count"] == 100

    db_session.expire_all()
    window = SafetyService(db_session).get_error_metrics(db_session, flag.id)
    assert window["total_evaluations"] == 1000
    assert window["error_count"] == 100
    assert window["error_rate"] == pytest.approx(0.1)

    scheduler = SafetyScheduler()
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service"),
    ):
        await scheduler.check_feature_flags_safety()

    db_session.expire_all()
    records = (
        db_session.query(SafetyRollbackRecord)
        .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
        .all()
    )
    assert len(records) == 1, "the safety monitor did not roll the flag back"
    assert records[0].previous_percentage == 50
    assert records[0].target_percentage == 5
    assert db_session.get(FeatureFlag, flag.id).rollout_percentage == 5


@pytest.mark.asyncio
async def test_the_check_reports_errors_with_zero_evaluations(
    client, db_session, scoped_key, flags
):
    """What the dashboard reads to say "the error rate cannot be computed"."""
    flag = flags()
    resp = client.post(
        "/api/v1/tracking/errors",
        json={"feature_flag_key": flag.key, "error_type": "crash", "message": "x"},
        headers={"X-API-Key": scoped_key["key"]},
    )
    assert resp.status_code == 201, resp.text

    db_session.expire_all()
    check = await SafetyService(db_session).check_feature_flag_safety(flag.id)

    assert check.details["error_count"] == 1
    assert check.details["total_evaluations"] == 0
    assert check.details["timeframe_minutes"] == 15


def test_counter_rows_are_pruned_after_the_retention(
    session_factory, db_session, scoped_key, flags
):
    flag = flags()
    key_id = uuid.UUID(scoped_key["id"])
    now = datetime.utcnow()
    old = sdk_evaluation_service.minute_of(now) - timedelta(minutes=10)
    session = session_factory()
    try:
        sdk_evaluation_service.reserve(session, key_id, flag.key, old, 5)
        session.commit()
        sdk_evaluation_service.record_local_evaluations(
            session,
            key_id,
            [
                sdk_evaluation_service.EvaluationReport(
                    flag_key=flag.key,
                    count=1,
                    enabled_count=0,
                    window_start=now,
                    window_end=now,
                )
            ],
            received_at=now,
        )
    finally:
        session.close()

    db_session.expire_all()
    minutes = {
        row.minute
        for row in db_session.query(SdkEvaluationKeyCount).filter(
            SdkEvaluationKeyCount.flag_key == flag.key
        )
    }
    assert minutes == {sdk_evaluation_service.minute_of(now)}
    assert (
        db_session.query(func.count())
        .select_from(SdkEvaluationFlagCount)
        .filter(
            SdkEvaluationFlagCount.flag_key == flag.key,
            SdkEvaluationFlagCount.minute == old,
        )
        .scalar()
        == 0
    )

"""No scheduler tick, and no results-socket snapshot, checks out a database
connection on the event loop's thread.

Under sustained overload every pooled connection can be held by a request
that is itself waiting for the event loop. A checkout made on the loop's own
thread then waits the whole pool timeout, and for that long the API answers
nothing. Each tick used to make such checkouts: the experiment and rollout
bodies, the run record every tick writes (``_persist_run``), and the
background task of ``POST /experiments/schedules/process``; so did the
snapshot an open results socket asks for every
``RESULTS_STREAM_INTERVAL_SECONDS``. Now each runs in a worker thread.

* T1  one real tick of each scheduler, through its own loop and the real
      ``run_locked_tick``, lock and ``_persist_run``, and one snapshot: a
      ``checkout`` listener on the engine names the site of every checkout
      and the thread it ran on. Per site the count on the loop's thread is 0,
      and every site is seen (an empty walk cannot pass). The run record and
      the rows the tick changed are exact (T3, T7).
* T2  a tick records its run while every pooled connection is held by a
      request that lets go only once the loop runs: no timings, the event
      order and the row count.
* T4  the experiment tick drops its cache entries on the API's loop, after
      the commit, with the cache on and Redis unreachable; the rollout tick,
      run in its worker, never asks for the Redis pool.
* T5  ``POST /experiments/schedules/process`` answers as before, and its
      background work checks out nothing on the loop's thread.
* T6  a cancelled tick, and a torn-down loop, keep the advisory lock until
      the worker has returned (the safety tick's tests, for these two ticks).

The application's one ``SessionLocal`` is rebound to a pooled engine on the
test database rather than patched module by module: every tick module, the
run record and the socket open their sessions from that same object, so a
module this file forgot to patch cannot keep a connection the listener never
sees; a listener on the original engine fails the test if anything still
reaches it. The database is shared with other tests, so the ticks are
narrowed to the rows each test created (``with_loader_criteria`` on that
``SessionLocal``), which is what lets the counts be exact.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.orm import sessionmaker, with_loader_criteria

from backend.app.api import deps, experiment_cache
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core import scheduler as experiment_scheduler_module
from backend.app.core import scheduler_lock, scheduler_tick
from backend.app.core.bandit_scheduler import BanditSchedulerRunner
from backend.app.core.config import settings
from backend.app.core.metrics_scheduler import MetricsScheduler
from backend.app.core.rollout_scheduler import RolloutScheduler
from backend.app.core.safety_scheduler import SafetyScheduler
from backend.app.core.scheduler import ExperimentScheduler, experiment_scheduler
from backend.app.core.scheduler_tick import TickResult, run_locked_tick
from backend.app.db import session as session_module
from backend.app.main import app
from backend.app.models.audit_log import ActionType, AuditLog
from backend.app.models.experiment import Experiment, ExperimentStatus
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
from backend.app.models.scheduler_run import SchedulerRun
from backend.app.models.user import User, UserRole
from backend.app.services.results_streaming_service import ResultsStreamingService

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
PREFIX = "offloop-"
BASE = "/api/v1/experiments"
NAMES = ("experiment", "rollout", "safety", "metrics", "bandit")
#: The held pool's checkout timeout (T2). On the old code the loop stops for
#: this long once per tick; nothing is asserted about it.
HELD_POOL_TIMEOUT_S = 5
COULD_NOT_RECORD = "could not record run"


# ---------------------------------------------------------------------------
# Where each checkout came from
# ---------------------------------------------------------------------------

#: A checkout made under one of these files belongs to that site.
_SITE_FILES = {
    "backend/app/core/scheduler.py": "experiment",
    "backend/app/core/rollout_scheduler.py": "rollout",
    "backend/app/core/safety_scheduler.py": "safety",
    "backend/app/core/metrics_scheduler.py": "metrics",
    "backend/app/core/bandit_scheduler.py": "bandit",
    "backend/app/services/results_streaming_service.py": "snapshot",
}


def _site(frame) -> Optional[str]:
    """The site a checkout belongs to: the innermost frame that names one.

    ``run record:<name>`` and ``lock:<name>`` carry the scheduler's name from
    the frame's own ``name`` argument; ``None`` is a checkout from anywhere
    else.
    """
    while frame is not None:
        path = frame.f_code.co_filename.replace(os.sep, "/")
        function = frame.f_code.co_name
        if path.endswith("backend/app/core/scheduler_tick.py") and (
            function == "_persist_run"
        ):
            return f"run record:{frame.f_locals.get('name')}"
        if path.endswith("backend/app/core/scheduler_lock.py") and (
            function == "_acquire"
        ):
            return f"lock:{frame.f_locals.get('name')}"
        for suffix, site in _SITE_FILES.items():
            if path.endswith(suffix):
                return site
        frame = frame.f_back
    return None


class _Checkouts:
    """A ``checkout`` listener: (site, thread) of each checkout while on."""

    def __init__(self) -> None:
        self.rows: List[Tuple[Optional[str], int]] = []
        self.recording = False

    def __call__(self, dbapi_connection, connection_record, connection_proxy):
        if self.recording:
            self.rows.append((_site(sys._getframe(1)), threading.get_ident()))

    def start(self) -> None:
        self.rows.clear()
        self.recording = True

    def stop(self) -> None:
        self.recording = False

    def on_loop(self, loop_thread: int) -> Dict[Optional[str], int]:
        """Every site seen, with how many of its checkouts ran on the loop."""
        tally: Dict[Optional[str], int] = {}
        for site, thread in self.rows:
            tally[site] = tally.get(site, 0) + (thread == loop_thread)
        return tally


class _Narrowing:
    """A ``do_orm_execute`` listener: every SELECT sees only these rows."""

    def __init__(self) -> None:
        self.criteria: Dict[Any, List[Any]] = {}

    def __call__(self, state) -> None:
        if not state.is_select or not self.criteria:
            return
        state.statement = state.statement.options(
            *(
                with_loader_criteria(
                    entity, entity.id.in_(list(ids)), include_aliases=True
                )
                for entity, ids in self.criteria.items()
            )
        )


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: List[str] = []

    def emit(self, record) -> None:
        self.messages.append(record.getMessage())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _pooled_engine(db_session, **pool):
    """A pooled engine (QueuePool, as production's) on the test database."""
    engine = create_engine(db_session.get_bind().url, **pool)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    return engine


@pytest.fixture
def app_db(db_session, monkeypatch):
    """The application's ``SessionLocal`` and engine, on the test database.

    Yields ``.engine`` (pooled), ``.checkouts`` (its listener, off until
    started), ``.narrowing`` (set ``.criteria`` to narrow every SELECT) and
    ``.elsewhere`` (checkouts that reached the original engine: none may).
    """
    engine = _pooled_engine(db_session, pool_size=5, max_overflow=5, pool_timeout=30)
    checkouts = _Checkouts()
    event.listen(engine, "checkout", checkouts)

    original_engine = session_module.engine
    elsewhere: List[Optional[str]] = []

    def _reached_elsewhere(dbapi_connection, connection_record, connection_proxy):
        elsewhere.append(_site(sys._getframe(1)))

    event.listen(original_engine, "checkout", _reached_elsewhere)
    narrowing = _Narrowing()
    original_bind = session_module.SessionLocal.kw.get("bind")
    session_module.SessionLocal.configure(bind=engine)
    event.listen(session_module.SessionLocal, "do_orm_execute", narrowing)
    # The advisory lock and purge_expired_history read the engine by name.
    monkeypatch.setattr(session_module, "engine", engine)
    try:
        yield SimpleNamespace(
            engine=engine,
            checkouts=checkouts,
            narrowing=narrowing,
            elsewhere=elsewhere,
        )
    finally:
        event.remove(session_module.SessionLocal, "do_orm_execute", narrowing)
        session_module.SessionLocal.configure(bind=original_bind)
        event.remove(original_engine, "checkout", _reached_elsewhere)
        engine.dispose()


@pytest.fixture
def tick_log():
    """WARNINGs of the tick runner (``could not record run``)."""
    records = _Records()
    logger = logging.getLogger(scheduler_tick.__name__)
    logger.addHandler(records)
    try:
        yield records
    finally:
        logger.removeHandler(records)


@pytest.fixture(autouse=True)
def _remove_what_the_test_created(db_session):
    since = datetime.now(timezone.utc)
    yield
    db_session.rollback()
    db_session.query(SchedulerRun).filter(SchedulerRun.started_at >= since).delete(
        synchronize_session=False
    )
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


def _user(db_session, *, superuser: bool) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"offloop_{suffix}",
        email=f"offloop_{suffix}@offloop.test",
        full_name="Off The Loop User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=UserRole.ADMIN if superuser else UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session):
    return _user(db_session, superuser=False)


@pytest.fixture
def admin(db_session):
    return _user(db_session, superuser=True)


@pytest.fixture
def client(app_db):
    """The app, its ``get_db`` opening the rebound ``SessionLocal``."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


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


# ---------------------------------------------------------------------------
# Seeds and reads
# ---------------------------------------------------------------------------


def _fresh(db_session):
    """A new session on the test database: it sees only what is committed."""
    return sessionmaker(bind=db_session.get_bind(), autoflush=False)()


def _naive_utc(delta: timedelta) -> datetime:
    return (datetime.now(timezone.utc) + delta).replace(tzinfo=None)


def _auth(user) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _experiment(client, user, db_session, **columns) -> uuid.UUID:
    """A startable experiment (two variants, a control, a metric), then
    ``columns`` written straight to its row."""
    response = client.post(
        f"{BASE}/",
        json={
            "name": f"offloop {uuid.uuid4().hex[:8]}",
            "description": "Ticks off the loop",
            "hypothesis": "No checkout on the loop's thread",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Conversion Rate",
                    "event_name": "purchase",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
        headers=_auth(user),
    )
    assert response.status_code == 201, response.text
    experiment_id = uuid.UUID(response.json()["id"])
    if columns:
        session = _fresh(db_session)
        try:
            session.execute(
                update(Experiment)
                .where(Experiment.id == experiment_id)
                .values(**columns)
            )
            session.commit()
        finally:
            session.close()
    return experiment_id


def _due_experiments(client, user, db_session) -> Tuple[uuid.UUID, uuid.UUID]:
    """A DRAFT due to start and an ACTIVE experiment due to end."""
    to_start = _experiment(
        client, user, db_session, start_date=_naive_utc(-timedelta(minutes=5))
    )
    to_end = _experiment(
        client,
        user,
        db_session,
        status=ExperimentStatus.ACTIVE,
        start_date=_naive_utc(-timedelta(hours=2)),
        end_date=_naive_utc(-timedelta(hours=1)),
    )
    return to_start, to_end


#: The audit actions a tick records for an experiment it moves.
_TICK_ACTIONS = (
    ActionType.EXPERIMENT_START.value,
    ActionType.EXPERIMENT_COMPLETE.value,
)


def _transitions(db_session, experiment_id) -> Tuple[Any, List[str]]:
    """The experiment's status and the start/complete audit entries for it."""
    session = _fresh(db_session)
    try:
        status = session.get(Experiment, experiment_id).status
        actions = sorted(
            row.action_type
            for row in session.query(AuditLog).filter(
                AuditLog.entity_id == experiment_id,
                AuditLog.action_type.in_(_TICK_ACTIONS),
            )
        )
        return status, actions
    finally:
        session.close()


def _flag(session, *, percentage: int) -> FeatureFlag:
    flag = FeatureFlag(
        key=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        name="offloop flag",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=percentage,
    )
    session.add(flag)
    session.flush()
    return flag


def _due_rollouts(db_session) -> SimpleNamespace:
    """Two ACTIVE schedules: one whose first stage (30%) may start now, and
    one whose last stage is due to complete."""
    session = _fresh(db_session)
    try:
        start_flag = _flag(session, percentage=0)
        start = RolloutSchedule(
            name="offloop start",
            feature_flag_id=start_flag.id,
            status=RolloutScheduleStatus.ACTIVE,
        )
        end_flag = _flag(session, percentage=50)
        end = RolloutSchedule(
            name="offloop end",
            feature_flag_id=end_flag.id,
            status=RolloutScheduleStatus.ACTIVE,
        )
        session.add_all([start, end])
        session.flush()
        first = RolloutStage(
            rollout_schedule_id=start.id,
            name="first",
            stage_order=1,
            target_percentage=30,
            status=RolloutStageStatus.PENDING,
            trigger_type=TriggerType.TIME_BASED,
            trigger_configuration={"duration": 24},
        )
        started = _naive_utc(-timedelta(hours=48))
        last = RolloutStage(
            rollout_schedule_id=end.id,
            name="last",
            stage_order=1,
            target_percentage=50,
            status=RolloutStageStatus.IN_PROGRESS,
            trigger_type=TriggerType.TIME_BASED,
            trigger_configuration={"duration": 24},
            started_at=started,
            updated_at=started,
        )
        session.add_all([first, last])
        session.commit()
        return SimpleNamespace(
            start_flag=start_flag.id,
            start=start.id,
            first=first.id,
            end_flag=end_flag.id,
            end=end.id,
            last=last.id,
        )
    finally:
        session.close()


def _rollout_state(db_session, seeded) -> Dict[str, Any]:
    session = _fresh(db_session)
    try:
        return {
            "first": session.get(RolloutStage, seeded.first).status,
            "start flag": session.get(
                FeatureFlag, seeded.start_flag
            ).rollout_percentage,
            "start flag audits": session.query(AuditLog)
            .filter(
                AuditLog.entity_id == seeded.start_flag,
                AuditLog.action_type == ActionType.FEATURE_FLAG_UPDATE.value,
            )
            .count(),
            "last": session.get(RolloutStage, seeded.last).status,
            "end": session.get(RolloutSchedule, seeded.end).status,
        }
    finally:
        session.close()


ROLLOUT_DONE = {
    "first": RolloutStageStatus.IN_PROGRESS,
    "start flag": 30,
    "start flag audits": 1,
    "last": RolloutStageStatus.COMPLETED,
    "end": RolloutScheduleStatus.COMPLETED,
}


def _monitored_flag(db_session) -> uuid.UUID:
    """An ACTIVE flag at 50%, monitored (error rate above 5%), rolled back to
    0, with 20 errors in 100 evaluations: one rollback is due."""
    session = _fresh(db_session)
    try:
        flag = _flag(session, percentage=50)
        session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={
                    "error_rate": {
                        "critical_threshold": 0.05,
                        "comparison_type": "greater_than",
                    }
                },
                rollback_percentage=0,
            )
        )
        session.add_all(
            RawMetric(
                feature_flag_id=flag.id,
                metric_type=MetricType.FLAG_EVALUATION.value,
            )
            for _ in range(100)
        )
        session.add_all(
            ErrorLog(feature_flag_id=flag.id, error_type="crash", message=f"e {i}")
            for i in range(20)
        )
        session.commit()
        return flag.id
    finally:
        session.close()


def _runs(db_session, name: str, since: datetime) -> List[Tuple]:
    """The run records written for ``name`` since ``since``."""
    session = _fresh(db_session)
    try:
        return [
            (
                row.status,
                row.items_processed,
                row.items_failed,
                row.metadata_ or {},
            )
            for row in session.query(SchedulerRun).filter(
                SchedulerRun.scheduler_name == name,
                SchedulerRun.started_at >= since,
            )
        ]
    finally:
        session.close()


def _one_tick(scheduler) -> int:
    """Drive the scheduler's own loop for one tick; return the loop's thread."""
    loop_thread: List[int] = []

    async def stop_after_one_tick(_seconds):
        scheduler.is_running = False

    async def main():
        loop_thread.append(threading.get_ident())
        scheduler.is_running = True
        with patch.object(asyncio, "sleep", stop_after_one_tick):
            await scheduler._run_scheduler()

    asyncio.run(main())
    return loop_thread[0]


# ---------------------------------------------------------------------------
# T1: one real tick of each scheduler; no checkout on the loop's thread
# ---------------------------------------------------------------------------


def _experiment_case(request, app_db, db_session):
    client = request.getfixturevalue("client")
    user = request.getfixturevalue("developer")
    to_start, to_end = _due_experiments(client, user, db_session)
    app_db.narrowing.criteria = {Experiment: [to_start, to_end]}
    scheduler = ExperimentScheduler()
    scheduler._notification_service = MagicMock()

    def check():
        assert _transitions(db_session, to_start) == (
            ExperimentStatus.ACTIVE,
            [ActionType.EXPERIMENT_START.value],
        )
        assert _transitions(db_session, to_end) == (
            ExperimentStatus.COMPLETED,
            [ActionType.EXPERIMENT_COMPLETE.value],
        )
        notified = scheduler._notification_service
        notified.notify_experiment_started.assert_called_once()
        assert notified.notify_experiment_started.call_args.kwargs[
            "experiment_id"
        ] == str(to_start)
        notified.notify_experiment_ended.assert_called_once()
        assert notified.notify_experiment_ended.call_args.kwargs[
            "experiment_id"
        ] == str(to_end)

    run = ("success", 2, 0, {"activated": 1, "completed": 1})
    return scheduler, run, check


def _rollout_case(request, app_db, db_session):
    seeded = _due_rollouts(db_session)
    app_db.narrowing.criteria = {RolloutSchedule: [seeded.start, seeded.end]}
    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()

    def check():
        assert _rollout_state(db_session, seeded) == ROLLOUT_DONE
        scheduler._notification_service.notify_rollout_advanced.assert_called_once_with(
            feature_flag_id=str(seeded.start_flag),
            stage_name="first",
            new_percentage=30,
        )

    run = ("success", 2, 0, {"stage_transitions": 1, "schedules_skipped": 0})
    return scheduler, run, check


def _safety_case(request, app_db, db_session):
    request.getfixturevalue("automatic_rollbacks")
    flag_id = _monitored_flag(db_session)
    app_db.narrowing.criteria = {FeatureFlag: [flag_id]}
    scheduler = SafetyScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()

    def check():
        session = _fresh(db_session)
        try:
            flag = session.get(FeatureFlag, flag_id)
            rollbacks = [
                (r.previous_percentage, r.target_percentage)
                for r in session.query(SafetyRollbackRecord).filter(
                    SafetyRollbackRecord.feature_flag_id == flag_id
                )
            ]
        finally:
            session.close()
        assert (flag.status, flag.rollout_percentage, rollbacks) == (
            FeatureFlagStatus.INACTIVE,
            0,
            [(50, 0)],
        )

    run = ("success", 1, 0, {"rollbacks": 1})
    return scheduler, run, check


def _metrics_case(request, app_db, db_session):
    # The aggregation reads every raw metric in the shared database, so its
    # count is not knowable here: the run record must say it succeeded and
    # failed nothing (None below).
    return MetricsScheduler(interval_minutes=1), None, lambda: None


def _bandit_case(request, app_db, db_session):
    # Narrowed to no experiment at all: the pass reads and updates nothing.
    app_db.narrowing.criteria = {Experiment: []}
    run = ("success", 0, 0, {"updated": 0, "skipped": 0, "errors": 0})
    return (
        BanditSchedulerRunner(interval_minutes=1, run_in_tests=True),
        run,
        lambda: None,
    )


_CASES: Dict[str, Callable] = {
    "experiment": _experiment_case,
    "rollout": _rollout_case,
    "safety": _safety_case,
    "metrics": _metrics_case,
    "bandit": _bandit_case,
}


@pytest.mark.regression
@pytest.mark.parametrize("name", NAMES)
def test_one_tick_checks_out_nothing_on_the_loop_thread(
    name, request, app_db, db_session, tick_log
):
    scheduler, run, check = _CASES[name](request, app_db, db_session)
    since = datetime.now(timezone.utc)

    app_db.checkouts.start()
    try:
        loop_thread = _one_tick(scheduler)
    finally:
        app_db.checkouts.stop()

    # Every site of this tick is seen, and none of its checkouts ran on the
    # loop's thread. A site missing from the left means the walk saw nothing
    # there (vacuous); a None key, a checkout from somewhere unexpected.
    tally = app_db.checkouts.on_loop(loop_thread)
    assert tally == {name: 0, f"lock:{name}": 0, f"run record:{name}": 0}, (
        f"checkouts on the loop's thread per site, want 0 each: {tally}"
    )
    assert app_db.elsewhere == [], "a checkout reached the original engine"

    runs = _runs(db_session, name, since)
    assert len(runs) == 1, f"{len(runs)} run records for one {name} tick"
    if run is None:
        status, _, failed, _ = runs[0]
        assert (status, failed) == ("success", 0), runs
    else:
        status, processed, failed, metadata = runs[0]
        want_status, want_processed, want_failed, want_metadata = run
        assert (status, processed, failed) == (
            want_status,
            want_processed,
            want_failed,
        ), runs
        assert {k: metadata.get(k) for k in want_metadata} == want_metadata, runs
    assert [m for m in tick_log.messages if COULD_NOT_RECORD in m] == []
    check()


def test_a_results_snapshot_checks_out_nothing_on_the_loop_thread(
    app_db, client, developer, db_session
):
    """The snapshot an open results socket asks for: the session is opened,
    queried and closed in the worker, and the snapshot is the real one."""
    experiment_id = _experiment(
        client, developer, db_session, status=ExperimentStatus.ACTIVE
    )
    opened: List[Tuple[str, int]] = []

    def factory():
        session = session_module.SessionLocal()
        opened.append(("open", threading.get_ident()))
        close = session.close

        def recorded_close():
            opened.append(("close", threading.get_ident()))
            close()

        session.close = recorded_close
        return session

    service = ResultsStreamingService(factory)
    loop_thread: List[int] = []

    async def main():
        loop_thread.append(threading.get_ident())
        return await service.get_live_snapshot(str(experiment_id))

    app_db.checkouts.start()
    try:
        snapshot = asyncio.run(main())
    finally:
        app_db.checkouts.stop()

    tally = app_db.checkouts.on_loop(loop_thread[0])
    assert tally == {"snapshot": 0}, (
        f"checkouts on the loop's thread per site, want 0 each: {tally}"
    )
    assert [what for what, thread in opened if thread == loop_thread[0]] == [], (
        "the snapshot's session was opened or closed on the loop's thread"
    )
    assert [what for what, _ in opened] == ["open", "close"]
    assert app_db.elsewhere == []
    assert (
        snapshot["event"],
        snapshot["experiment_id"],
        snapshot["status"],
        sorted((v["name"], v["participant_count"]) for v in snapshot["variants"]),
        snapshot["total_participants"],
        "error" in snapshot,
    ) == (
        "results_update",
        str(experiment_id),
        "active",
        [("Control", 0), ("Treatment", 0)],
        0,
        False,
    ), snapshot


def test_the_probe_sees_a_checkout_made_on_the_loop_thread(app_db):
    """Each site's work run on the loop's thread, the way it ran before: the
    probe names every one of them, so a 0 in the tests above is not blindness.
    (Narrowed to no rows: the queries run, they change nothing.)"""
    app_db.narrowing.criteria = {Experiment: [], RolloutSchedule: []}
    now = datetime.now(timezone.utc)
    service = ResultsStreamingService(session_module.SessionLocal)

    async def main():
        app_db.checkouts.start()
        try:
            scheduler_tick._persist_run(
                "experiment", now, now, "success", TickResult(), None
            )
            ExperimentScheduler()._process_due()
            await RolloutScheduler(interval_minutes=1).process_rollout_schedules()
            service._snapshot(str(uuid.uuid4()), now.isoformat())
        finally:
            app_db.checkouts.stop()
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    tally = app_db.checkouts.on_loop(loop_thread)
    assert {site for site, on_loop in tally.items() if on_loop} == {
        "run record:experiment",
        "experiment",
        "rollout",
        "snapshot",
    }, tally


# ---------------------------------------------------------------------------
# T2: the run record is written while requests waiting for the loop hold
# every pooled connection
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", NAMES)
def test_a_tick_records_its_run_while_the_pool_is_held_by_requests_waiting_for_the_loop(
    name, db_session, monkeypatch, tick_log
):
    """The pool has one connection. During the tick a holder thread checks it
    out and, like a request in a worker thread, lets go only once the event
    loop has run a callback for it; it asks for that callback only when the
    run record has started. A run record written on the loop's thread waits
    for a connection the holder cannot give back: it times out and the run is
    lost. Written in a worker, it gets the connection once the holder is
    released."""
    engine = _pooled_engine(
        db_session, pool_size=1, max_overflow=0, pool_timeout=HELD_POOL_TIMEOUT_S
    )
    events: List[str] = []
    holder_threads: List[int] = []

    def on_checkout(dbapi_connection, connection_record, connection_proxy):
        if threading.get_ident() not in holder_threads:
            events.append("run record checked out")

    event.listen(engine, "checkout", on_checkout)
    factory = sessionmaker(bind=engine, autoflush=False)
    opening = threading.Event()

    def open_session():
        opening.set()
        return factory()

    @asynccontextmanager
    async def lock(_name, engine=None):
        yield True

    monkeypatch.setattr(session_module, "SessionLocal", open_session)
    monkeypatch.setattr(scheduler_tick, "async_scheduler_lock", lock)

    async def callback():
        return None

    def hold(loop, held):
        holder_threads.append(threading.get_ident())
        connection = engine.connect()
        events.append("holder checked out")
        held.set()
        try:
            if opening.wait(30):
                asyncio.run_coroutine_threadsafe(callback(), loop).result(timeout=10)
        except BaseException:
            # The old code's loop may close before it runs the callback.
            pass
        finally:
            events.append("holder released")
            connection.close()

    holders: List[threading.Thread] = []

    async def tick():
        held = threading.Event()
        holder = threading.Thread(
            target=hold, args=(asyncio.get_running_loop(), held), daemon=True
        )
        holders.append(holder)
        holder.start()
        assert await asyncio.to_thread(held.wait, 30), "the holder never checked out"
        return {"items_processed": 1, "items_failed": 0}

    try:
        outcome = asyncio.run(run_locked_tick(name, tick))
    finally:
        for holder in holders:
            holder.join(60)
        engine.dispose()

    session = _fresh(db_session)
    try:
        rows = (
            session.query(SchedulerRun)
            .filter(
                SchedulerRun.scheduler_name == name,
                SchedulerRun.started_at == outcome.started_at,
            )
            .count()
        )
    finally:
        session.close()
    lost = len([m for m in tick_log.messages if COULD_NOT_RECORD in m])
    assert (events, rows, lost) == (
        ["holder checked out", "holder released", "run record checked out"],
        1,
        0,
    ), f"events {events}, run records {rows} (want 1), '{COULD_NOT_RECORD}' x{lost}"


# ---------------------------------------------------------------------------
# T4: the cache stays on the API's loop
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_on_redis_unreachable(monkeypatch):
    """The cache on and Redis unreachable; every request for the shared Redis
    pool recorded with the loop it came from."""
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", 1)
    monkeypatch.setattr(deps, "_redis_pool", None)
    # Without the redis package the cache never asks for the pool, and the
    # checks on the pool requests would pass whatever the tick did.
    assert deps.REDIS_AVAILABLE
    requests: List[Any] = []
    real_get_redis_pool = deps.get_redis_pool

    async def recording_get_redis_pool():
        requests.append(asyncio.get_running_loop())
        return await real_get_redis_pool()

    monkeypatch.setattr(deps, "get_redis_pool", recording_get_redis_pool)
    return requests


def test_the_experiment_ticks_cache_drops_run_on_the_api_loop_after_their_commits(
    app_db, client, developer, db_session, cache_on_redis_unreachable, monkeypatch
):
    to_start, to_end = _due_experiments(client, developer, db_session)
    app_db.narrowing.criteria = {Experiment: [to_start, to_end]}
    drops: List[Tuple] = []
    real_drop = experiment_cache.drop_cached_experiment_details
    api: Dict[str, Any] = {}

    async def recording_drop(ids):
        ids = list(ids)
        committed = [_transitions(db_session, i)[0] for i in ids]
        drops.append(
            (
                ids,
                asyncio.get_running_loop() is api["loop"],
                threading.get_ident() == api["thread"],
                committed,
            )
        )
        return await real_drop(ids)

    monkeypatch.setattr(
        experiment_cache, "drop_cached_experiment_details", recording_drop
    )
    scheduler = ExperimentScheduler()
    scheduler._notification_service = MagicMock()
    # Only the tick's requests: creating the experiments asked for the pool too.
    cache_on_redis_unreachable.clear()

    async def main():
        api["loop"] = asyncio.get_running_loop()
        api["thread"] = threading.get_ident()
        return await scheduler.process_scheduled_experiments()

    result = asyncio.run(main())

    # (ids, on the API's loop, on its thread, committed status at the drop)
    assert drops == [
        ([to_start], True, True, [ExperimentStatus.ACTIVE]),
        ([to_end], True, True, [ExperimentStatus.COMPLETED]),
    ]
    assert [loop is api["loop"] for loop in cache_on_redis_unreachable] == [
        True,
        True,
    ], "the shared Redis pool was asked for on another loop"
    assert result == {
        "items_processed": 2,
        "items_failed": 0,
        "metadata": {"activated": 1, "completed": 1},
    }


def test_the_rollout_tick_in_its_worker_never_asks_for_the_redis_pool(
    app_db, db_session, cache_on_redis_unreachable
):
    """Run the way the scheduler runs it, on a private loop in a worker: a
    Redis pool created there would be reused by the API's requests after the
    loop has closed."""
    seeded = _due_rollouts(db_session)
    app_db.narrowing.criteria = {RolloutSchedule: [seeded.start, seeded.end]}
    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()
    cache_on_redis_unreachable.clear()

    result = asyncio.run(scheduler._process_off_loop())

    assert cache_on_redis_unreachable == [], "the rollout tick asked for the pool"
    assert deps._redis_pool is None, "the rollout tick created the shared pool"
    assert result["items_processed"] == 2, result
    assert _rollout_state(db_session, seeded) == ROLLOUT_DONE


# ---------------------------------------------------------------------------
# T5: the process route
# ---------------------------------------------------------------------------


def test_the_process_route_answers_as_before_and_checks_out_nothing_on_the_loop(
    app_db, client, admin, developer, db_session, monkeypatch
):
    to_start = _experiment(
        client, developer, db_session, start_date=_naive_utc(-timedelta(minutes=5))
    )
    app_db.narrowing.criteria = {Experiment: [to_start]}
    monkeypatch.setattr(experiment_scheduler, "_notification_service", MagicMock())
    loop_threads: List[int] = []
    real = experiment_scheduler.process_scheduled_experiments

    async def recording():
        loop_threads.append(threading.get_ident())
        return await real()

    monkeypatch.setattr(
        experiment_scheduler, "process_scheduled_experiments", recording
    )

    refused = client.post(f"{BASE}/schedules/process", headers=_auth(developer))
    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": "Only administrators can trigger scheduled processing"},
    )
    assert loop_threads == []

    app_db.checkouts.start()
    try:
        response = client.post(f"{BASE}/schedules/process", headers=_auth(admin))
    finally:
        app_db.checkouts.stop()

    assert (response.status_code, response.json()) == (
        202,
        {"status": "Processing scheduled experiments in the background"},
    )
    assert len(loop_threads) == 1, "the background task did not run once"
    tally = app_db.checkouts.on_loop(loop_threads[0])
    assert tally.get("experiment") == 0, (
        f"checkouts on the loop's thread by the background task: {tally}"
    )
    assert _transitions(db_session, to_start) == (
        ExperimentStatus.ACTIVE,
        [ActionType.EXPERIMENT_START.value],
    )


# ---------------------------------------------------------------------------
# T6: the advisory lock is held until the worker returns
# ---------------------------------------------------------------------------


def _lock_is_free(engine, name: str) -> bool:
    """Whether another session could take the scheduler's advisory lock now."""
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if scheduler_lock.try_acquire(connection, name):
            scheduler_lock.release(connection, name)
            return True
        return False
    finally:
        connection.close()


def _blocked_tick(name, started, finish, events) -> Callable:
    """The tick ``name``'s scheduler runs, its worker blocked until
    ``finish`` is set."""

    def block():
        started.set()
        finish.wait(60)  # a safety bound so a failing test cannot hang a thread
        events.append("worker returned")

    if name == "experiment":
        scheduler = ExperimentScheduler()
        scheduler._notification_service = MagicMock()

        def due():
            block()
            # One committed start: a cancelled tick still must not announce it.
            return experiment_scheduler_module._DueWork(
                activated=1,
                activated_ids=["committed"],
                started=[
                    {
                        "experiment_id": "committed",
                        "experiment_name": "committed",
                        "resumed": False,
                    }
                ],
            )

        scheduler._process_due = due
        return scheduler.process_scheduled_experiments

    scheduler = RolloutScheduler(interval_minutes=1)

    async def body():
        block()
        return {"items_processed": 0, "items_failed": 0}

    scheduler.process_rollout_schedules = body
    return scheduler._process_off_loop


@pytest.mark.parametrize("name", ["experiment", "rollout"])
def test_a_cancelled_tick_keeps_the_lock_until_its_worker_returns(
    name, app_db, monkeypatch
):
    """Shutdown cancels the tick while its worker runs. The advisory lock is
    released when run_locked_tick unwinds, so the tick must not unwind before
    the worker returns, or another replica's tick runs beside it (and
    advances the same stage twice). A second cancel does not end the wait.
    The cancelled experiment tick drops nothing and announces nothing."""
    started = threading.Event()
    finish = threading.Event()
    events: List[str] = []
    tick = _blocked_tick(name, started, finish, events)
    drops: List[Any] = []

    async def recording_drop(ids):
        drops.append(list(ids))

    monkeypatch.setattr(
        experiment_cache, "drop_cached_experiment_details", recording_drop
    )

    async def main():
        task = asyncio.create_task(
            run_locked_tick(name, tick, engine=app_db.engine, record=False)
        )
        try:
            assert await asyncio.to_thread(started.wait, 30), "worker never started"
            for _ in range(2):
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not task.done(), "the tick returned before its worker"
                assert not await asyncio.to_thread(
                    _lock_is_free, app_db.engine, name
                ), "lock released before the worker returned"
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert events == ["worker returned"]
    assert _lock_is_free(app_db.engine, name), "the lock was never released"
    if name == "experiment":
        assert (drops, tick.__self__._notification_service.mock_calls) == ([], [])


@pytest.mark.parametrize("name", ["experiment", "rollout"])
def test_loop_teardown_keeps_the_lock_until_the_worker_returns(name, app_db):
    """asyncio.run's teardown cancels EVERY task. A worker that is a task
    would be cancelled with the tick, and the lock released while the thread
    still runs; run_in_worker's worker is a plain Future, which teardown
    leaves alone."""
    started = threading.Event()
    finish = threading.Event()
    events: List[str] = []
    tick_fn = _blocked_tick(name, started, finish, events)

    async def spin():
        for _ in range(20):
            await asyncio.sleep(0)

    async def wait_started():
        assert await asyncio.to_thread(started.wait, 30), "worker never started"

    loop = asyncio.new_event_loop()
    try:
        tick = loop.create_task(
            run_locked_tick(name, tick_fn, engine=app_db.engine, record=False)
        )
        loop.run_until_complete(wait_started())
        # What asyncio.run does on the way out: cancel every task.
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(spin())
        try:
            assert not tick.done(), "the tick returned before its worker"
            assert not _lock_is_free(app_db.engine, name), (
                "lock released before the worker returned"
            )
        finally:
            finish.set()
        loop.run_until_complete(asyncio.gather(tick, return_exceptions=True))
        assert events == ["worker returned"]
        assert _lock_is_free(app_db.engine, name), "the lock was never released"
    finally:
        finish.set()
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()

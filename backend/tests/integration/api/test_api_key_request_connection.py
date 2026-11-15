"""An API-key request holds a database connection from the key check to the
end of the route, whether or not the check records the key's use.

``deps.get_api_key`` records the use through ``APIKey.update_last_used``,
which commits when ``last_used_at`` is null or older than
``LAST_USED_RESOLUTION`` and otherwise writes nothing. The route that follows
should run on a connection held from the key check in both cases.

Driven through the real app with httpx's ``ASGITransport``: the real
``deps.get_db``, ``deps.get_api_key`` and route handlers, with only the
session factory pointed at a QueuePool engine on the test database. A pool
``checkout`` listener records the thread of every checkout, and the test
counts the ones made on the event loop's thread. For the flag evaluate and
the tracking assign routes, a request whose key use is recorded must make no
more of those checkouts than the next request with the same key, which
records nothing. The ruleset route, with a key carrying ``sdk:ruleset``, must
make none in either case. Nothing here measures time.
"""

import asyncio
import threading
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.api_key import LAST_USED_RESOLUTION, APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric

pytestmark = pytest.mark.integration


def _utcnow() -> datetime:
    """Naive UTC, like the stored ``last_used_at``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def checkouts(test_db, monkeypatch):
    """Point ``deps.get_db`` at a QueuePool engine and record checkout threads.

    Yields the list the ``checkout`` listener appends each checkout's thread
    id to, in order.
    """
    engine = create_engine(test_db.url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    assert isinstance(engine.pool, QueuePool)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET search_path TO test_experimentation")
        cursor.close()

    threads: list[int] = []

    @event.listens_for(engine, "checkout")
    def _record_thread(_dbapi_connection, _record, _proxy):
        threads.append(threading.get_ident())

    monkeypatch.setattr(
        deps,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield threads
    finally:
        engine.dispose()


@pytest.fixture
def api_key(db_session, developer_user):
    """A key of an active developer, and its plaintext."""
    row, plaintext = APIKey.create_for_user(
        db_session, developer_user.id, f"connection-{uuid.uuid4().hex[:8]}"
    )
    yield row.id, plaintext
    db_session.rollback()
    db_session.query(APIKey).filter(APIKey.id == row.id).delete()
    db_session.commit()


@pytest.fixture
def ruleset_api_key(db_session, developer_user):
    """A key with ``sdk:ruleset`` of an active developer, and its plaintext."""
    row, plaintext = APIKey.create_for_user(
        db_session,
        developer_user.id,
        f"connection-{uuid.uuid4().hex[:8]}",
        scopes="sdk:ruleset",
    )
    yield row.id, plaintext
    db_session.rollback()
    db_session.query(APIKey).filter(APIKey.id == row.id).delete()
    db_session.commit()


@pytest.fixture
def active_flag(db_session, make_feature_flag):
    flag = make_feature_flag(
        key=f"connection-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    yield flag
    db_session.rollback()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).delete()
    db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.commit()


@pytest.fixture
def active_experiment(db_session, admin_user):
    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"Connection {suffix}",
        key=f"connection-{suffix}",
        description="created by test_api_key_request_connection",
        hypothesis="an API-key request keeps its connection",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
    )
    db_session.add(experiment)
    db_session.commit()
    db_session.refresh(experiment)
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    yield experiment
    db_session.rollback()
    for model in (Event, Assignment, Variant):
        db_session.query(model).filter(model.experiment_id == experiment.id).delete()
    db_session.query(Experiment).filter(Experiment.id == experiment.id).delete()
    db_session.commit()


def _set_last_used(db_session, key_id, value) -> None:
    db_session.execute(
        update(APIKey).where(APIKey.id == key_id).values(last_used_at=value)
    )
    db_session.commit()


def _last_used(db_session, key_id):
    db_session.expire_all()
    return db_session.query(APIKey.last_used_at).filter(APIKey.id == key_id).scalar()


@pytest.mark.regression
@pytest.mark.parametrize("route", ["evaluate", "assign", "ruleset"])
async def test_recording_the_key_use_adds_no_checkout_on_the_event_loop(
    route, checkouts, request, active_flag, active_experiment, db_session
):
    asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    key_id, plaintext = request.getfixturevalue(
        "ruleset_api_key" if route == "ruleset" else "api_key"
    )
    headers = {"X-API-Key": plaintext}

    async def send(http: httpx.AsyncClient) -> httpx.Response:
        user_id = f"u-{uuid.uuid4().hex[:12]}"
        if route == "ruleset":
            return await http.get("/api/v1/sdk/ruleset", headers=headers)
        if route == "evaluate":
            return await http.get(
                f"/api/v1/feature-flags/evaluate/{active_flag.key}",
                params={"user_id": user_id},
                headers=headers,
            )
        return await http.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": active_experiment.key, "user_id": user_id},
            headers=headers,
        )

    async def loop_checkouts(http: httpx.AsyncClient) -> int:
        start = len(checkouts)
        response = await send(http)
        assert response.status_code == 200, response.text
        if route == "assign":
            assert response.json()["assigned"] is True, response.text
        return sum(1 for thread in checkouts[start:] if thread == loop_thread)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        # One request first, so the measured ones run on a route already used.
        await loop_checkouts(http)

        counts = []
        for stored in (None, _utcnow() - 2 * LAST_USED_RESOLUTION):
            _set_last_used(db_session, key_id, stored)

            recorded = await loop_checkouts(http)
            after_recorded = _last_used(db_session, key_id)
            not_recorded = await loop_checkouts(http)
            after_not_recorded = _last_used(db_session, key_id)

            # The first request recorded the use and the second did not, so
            # the comparison below is between the two paths.
            assert after_recorded is not None and (
                stored is None or after_recorded > stored
            ), (
                f"stored last_used_at {stored!r}: the first {route} request did "
                f"not record the key's use (now {after_recorded!r})"
            )
            assert after_not_recorded == after_recorded, (
                f"the second {route} request recorded the key's use again "
                f"({after_recorded!r} -> {after_not_recorded!r})"
            )
            if route == "ruleset":
                assert recorded == 0 and not_recorded == 0, (
                    f"checkouts on the event loop's thread by the ruleset "
                    f"request that recorded the key's use, then by the next "
                    f"one: {recorded} then {not_recorded}, want 0 then 0"
                )
            else:
                # The listener counts on the loop's thread in both runs.
                assert recorded >= 1 and not_recorded >= 1, (
                    f"no checkout seen on the event loop's thread "
                    f"({recorded=}, {not_recorded=})"
                )
            counts.append((stored, recorded, not_recorded))

    assert all(recorded - not_recorded == 0 for _, recorded, not_recorded in counts), (
        f"checkouts on the event loop's thread by the {route} request that "
        f"recorded the key's use, then by the next one with the same key: "
        + "; ".join(
            f"stored last_used_at {stored!r}: {recorded} then {not_recorded}"
            for stored, recorded, not_recorded in counts
        )
    )

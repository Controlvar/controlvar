"""``POST /api/v1/tracking/assign`` works out the assignment in a worker
thread (#823): two first assignments of one user can now cross in one API
process, and every answer keeps its status and text across the trip.

Driven through the real app with httpx's ``ASGITransport``: the real
``deps.get_db``, ``deps.get_api_key`` and handler, the event loop's default
worker limit, and the session factory pointed at a QueuePool engine on the
test database whose sessions expire on commit, as production's do.

* Crossing first assignments (#1026). For each of ten new users, two requests
  are sent at once, one pair at a time. A hook on
  ``GlobalHoldoutService.is_user_in_holdout`` -- the first call
  ``assign_user`` makes for a new user after finding no stored row, before
  the insert -- waits on a ``threading.Barrier`` of two, new for each user,
  so neither request inserts before both have looked. One insert is refused
  by the unique index on ``(experiment_id, user_id)``, and that request
  answers the row the other stored. Both answers carry one variant, the user
  has one row, and each request records one view, as a sticky hit does. The
  hook does not catch ``BrokenBarrierError``: if the two requests do not reach
  it together, the barrier breaks, the request answers 500, and the test also
  asserts that no barrier broke.
* Answers across the trip: the three failures of the assignment itself (a
  service ``ValueError``, any other exception, and a response that does not
  build) keep their status and exact text. The refusals that come first are
  pinned elsewhere and unchanged: an unknown key and a draft with no control
  (``test_tracking_assign_not_running.py``, ``test_tracking_api.py``) and a
  context over the limits (``test_targeting_context_limits.py``).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import Counter
from typing import Dict, List

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from backend.app.api import deps
from backend.app.api.v1.endpoints import tracking
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.global_holdout_service import GlobalHoldoutService

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
URL = "/api/v1/tracking/assign"
USERS = 10
#: How long one request of a pair waits in the hook for the other.
BARRIER_SECONDS = 2
REQUEST_ID = "assign-worker-1"
ASSIGN_FAILURE = "Could not assign the user to the experiment"


@pytest.fixture
def production_sessions(test_db, monkeypatch):
    """Point ``deps.get_db`` at a QueuePool engine on the test database."""
    engine = create_engine(test_db.url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    assert isinstance(engine.pool, QueuePool)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(deps, "SessionLocal", factory)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def headers(db_session, developer_user):
    """The ``X-API-Key`` header of an active developer's key."""
    row, plaintext = APIKey.create_for_user(
        db_session, developer_user.id, f"assign-worker-{uuid.uuid4().hex[:8]}"
    )
    yield {"X-API-Key": plaintext, "X-Request-ID": REQUEST_ID}
    db_session.rollback()
    db_session.query(APIKey).filter(APIKey.id == row.id).delete()
    db_session.commit()


@pytest.fixture
def parked_holdouts(db_session):
    """No active global holdout for the test, so every new user is assigned."""
    parked = [
        row.id
        for row in db_session.query(GlobalHoldout).filter(
            GlobalHoldout.is_active.is_(True)
        )
    ]
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id.in_(parked)).update(
        {"is_active": False}, synchronize_session=False
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.query(GlobalHoldout).filter(GlobalHoldout.id.in_(parked)).update(
            {"is_active": True}, synchronize_session=False
        )
        db_session.commit()


@pytest.fixture
def experiment(db_session, admin_user, parked_holdouts):
    """An ACTIVE two-arm experiment with fixed allocation."""
    suffix = uuid.uuid4().hex[:8]
    created = Experiment(
        name=f"Assign in a worker {suffix}",
        key=f"assign-worker-{suffix}",
        description="created by test_tracking_assign_in_a_worker",
        hypothesis="crossing first assignments answer one variant",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
    )
    db_session.add(created)
    db_session.commit()
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=created.id,
                name=name,
                description=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={"arm": name},
            )
        )
    db_session.commit()
    experiment_id, key = created.id, created.key
    yield experiment_id, key
    db_session.rollback()
    for model in (Event, Assignment, Variant):
        db_session.query(model).filter(model.experiment_id == experiment_id).delete()
    db_session.query(Experiment).filter(Experiment.id == experiment_id).delete()
    db_session.commit()


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _outcome(result) -> object:
    if isinstance(result, httpx.Response):
        return result.status_code
    return type(result).__name__


# ---------------------------------------------------------------------------
# Crossing first assignments in one process (#1026)
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_two_first_assigns_of_one_user_at_once_answer_one_variant(
    production_sessions, headers, experiment, monkeypatch
):
    experiment_id, key = experiment
    barriers: Dict[str, threading.Barrier] = {}
    waits: Counter = Counter()
    waits_lock = threading.Lock()
    real = GlobalHoldoutService.is_user_in_holdout

    def hooked(self, user_id, *args, **kwargs):
        barrier = barriers.get(user_id)
        if barrier is not None:
            with waits_lock:
                waits[user_id] += 1
            # Not caught: a broken barrier fails the request.
            barrier.wait()
        return real(self, user_id, *args, **kwargs)

    monkeypatch.setattr(GlobalHoldoutService, "is_user_in_holdout", hooked)

    answers: Dict[str, List[object]] = {}
    async with _client() as http:

        async def send(user_id: str) -> httpx.Response:
            return await http.post(
                URL, json={"experiment_key": key, "user_id": user_id}, headers=headers
            )

        for index in range(USERS):
            user_id = f"pair-{index}-{uuid.uuid4().hex[:10]}"
            barriers[user_id] = threading.Barrier(2, timeout=BARRIER_SECONDS)
            answers[user_id] = await asyncio.gather(
                send(user_id), send(user_id), return_exceptions=True
            )

    tally = Counter(_outcome(a) for pair in answers.values() for a in pair)
    broken = sorted(u for u, barrier in barriers.items() if barrier.broken)
    assert tally == {200: 2 * USERS}, (
        f"{USERS} pairs of first assigns, each pair at once: {dict(tally)}, "
        f"want {{200: {2 * USERS}}}; barriers broken for {len(broken)} users"
    )
    assert not broken, f"the two requests of a pair did not meet: {broken}"
    assert dict(waits) == dict.fromkeys(barriers, 2), dict(waits)

    session = production_sessions()
    try:
        rows: Dict[str, List[str]] = {}
        for user_id, variant_id in session.query(
            Assignment.user_id, Assignment.variant_id
        ).filter(Assignment.experiment_id == experiment_id):
            rows.setdefault(user_id, []).append(str(variant_id))
        views = (
            session.query(Event)
            .filter(
                Event.experiment_id == experiment_id,
                Event.event_type == EventType.EXPOSURE.value,
            )
            .count()
        )
    finally:
        session.close()

    assert set(rows) == set(barriers), sorted(set(rows) ^ set(barriers))
    for user_id, pair in answers.items():
        bodies = [response.json() for response in pair]
        assert len(rows[user_id]) == 1, (user_id, rows[user_id])
        assert {body["variant_id"] for body in bodies} == set(rows[user_id]), (
            user_id,
            bodies,
            rows[user_id],
        )
        assert all(body["assigned"] is True for body in bodies), bodies
        assert all(body["reason"] == "assigned" for body in bodies), bodies
    assert views == 2 * USERS, f"{views} views recorded, want one per request"


# ---------------------------------------------------------------------------
# The answers of the assignment's failures, across the trip
# ---------------------------------------------------------------------------

SERVICE_REFUSAL = "Cannot assign users to experiment with status: archived"
CANARY = "canary-823-Kq"


class _Strict(BaseModel):
    number: int


def _validation_error() -> ValidationError:
    try:
        _Strict(number=CANARY)
    except ValidationError as exc:
        return exc
    raise AssertionError("the model accepted a string")


def _refuse_with_a_value_error(*args, **kwargs):
    raise ValueError(SERVICE_REFUSAL)


def _fail_unexpectedly(*args, **kwargs):
    raise RuntimeError(CANARY)


def _answer_that_does_not_build(**kwargs):
    raise _validation_error()


FAILURES = [
    pytest.param(
        (AssignmentService, "assign_user", _refuse_with_a_value_error),
        400,
        SERVICE_REFUSAL,
        id="service-value-error",
    ),
    pytest.param(
        (AssignmentService, "assign_user", _fail_unexpectedly),
        500,
        f"{ASSIGN_FAILURE} (request ID: {REQUEST_ID}).",
        id="unexpected-error",
    ),
    pytest.param(
        (tracking, "VariantAssignmentResponse", _answer_that_does_not_build),
        500,
        f"{ASSIGN_FAILURE} (request ID: {REQUEST_ID}).",
        id="response-does-not-build",
    ),
]


@pytest.mark.parametrize("patch, status, detail", FAILURES)
async def test_a_failed_assignment_keeps_its_answer(
    patch, status, detail, production_sessions, headers, experiment, monkeypatch
):
    _experiment_id, key = experiment
    monkeypatch.setattr(*patch)
    async with _client() as http:
        response = await http.post(
            URL,
            json={"experiment_key": key, "user_id": f"u-{uuid.uuid4().hex[:12]}"},
            headers=headers,
        )
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert response.headers["x-request-id"] == REQUEST_ID
    assert CANARY not in response.text

"""A refused rollout change releases its locks before it returns to the event loop.

The rollout routes take the flag and schedule locks (``FOR UPDATE``) before
they judge a change. A change refused after the locks used to keep them until
``deps.get_db`` closed the request's session, and ``get_db`` does that on the
event loop. The rollout tick runs synchronous SQLAlchemy on that same loop and
locks the flag of every ACTIVE schedule, so a tick that reached the flag while
a refused request still held it waited on a lock only the loop could release:
the API process hung (#1604 review, finding 1, measured with the reviewer's
probe). Now the service ends the transaction in the request's worker thread
before the refusal leaves it.

Each case runs the real app through ``httpx.ASGITransport`` on one event loop,
with the real ``deps.get_db`` (its ``SessionLocal`` pointed at the test
database). The request holds its locks until the tick, started on the same
loop, is seen waiting on them in ``pg_stat_activity``; then it goes on. The
tick must finish. If it does not, a watchdog thread terminates the request's
own backend (idle in transaction, holding the locks) so the test can end, and
the test fails: the watchdog firing is the failure, not a duration.

The last test covers a row deleted while the request waited for the locks:
the route answers its own 404, where the generic ``except Exception`` turned it
into a 500 (finding 4).
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Dict, List
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import text

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag
from backend.app.schemas.rollout_schedule import (
    RolloutScheduleCreate,
    RolloutStageCreate,
    TriggerType,
)
from backend.app.services import rollout_service
from backend.app.services.rollout_service import RolloutService
from backend.tests.integration.api.rollout_rules import (
    SCHEDULES,
    client,
    create,
    developer,
    fresh,
    local_auth,
    new_flag,
    ok,
    rollout_invariants,
    send,
    session_factory,
    stage,
    stage_rows,
    waiting_on,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.regression,
    pytest.mark.usefixtures("local_auth", "rollout_invariants"),
]

#: How long the watchdog lets the tick run before it frees the locks and the
#: test fails. A tick here does nothing but wait for the request; on the fixed
#: code it returns as soon as the request ends its transaction.
WATCHDOG_S = 20
#: How long the request waits to see the tick blocked on its locks.
HOLD_S = 15


def _schedule(session_factory, flag_id, owner_id, targets: List[int]) -> Dict[str, Any]:
    """A draft schedule of manual stages, made through the service."""
    session = session_factory()
    try:
        created = RolloutService.create_rollout_schedule(
            session,
            RolloutScheduleCreate(
                name=f"locks {uuid.uuid4().hex[:8]}",
                feature_flag_id=flag_id,
                stages=[
                    RolloutStageCreate(
                        name=f"s{order}",
                        stage_order=order,
                        target_percentage=target,
                        trigger_type=TriggerType.MANUAL,
                    )
                    for order, target in enumerate(targets, start=1)
                ],
            ),
            owner_id,
        )
        return {
            "id": created.id,
            "stages": {s.name: s.id for s in created.stages},
        }
    finally:
        session.close()


def _service(session_factory, method: str, *args) -> None:
    session = session_factory()
    try:
        getattr(RolloutService, method)(session, *args)
    finally:
        session.close()


def _scenario(name, session_factory, flag_id, owner_id):
    """``(method, path, the schedule the request locks, the schedules the
    tick runs, the expected status)`` for one case."""
    if name == "activate-a-second-schedule":
        # The worst case: refused only when the flag already has an ACTIVE
        # schedule, whose flag the tick locks on every pass.
        active = _schedule(session_factory, flag_id, owner_id, [10])
        _service(session_factory, "activate_rollout_schedule", active["id"])
        second = _schedule(session_factory, flag_id, owner_id, [20])
        path = f"{SCHEDULES}/{second['id']}/activate"
        return "POST", path, second["id"], [active["id"]], 409
    schedule = _schedule(session_factory, flag_id, owner_id, [10, 20, 30])
    _service(session_factory, "activate_rollout_schedule", schedule["id"])
    _service(session_factory, "manually_advance_stage", schedule["stages"]["s1"])
    if name == "advance-out-of-order":
        stage_id = schedule["stages"]["s3"]
        expected = 409
    else:  # "advance-in-order", the control: commits in the worker thread
        stage_id = schedule["stages"]["s1"]
        expected = 200
    path = f"{SCHEDULES}/stages/{stage_id}/advance"
    return "POST", path, schedule["id"], [schedule["id"]], expected


@pytest.mark.parametrize(
    "case", ["advance-out-of-order", "activate-a-second-schedule", "advance-in-order"]
)
def test_the_tick_on_the_same_event_loop_finishes_while_a_request_holds_the_flag(
    case, session_factory, developer, new_flag, monkeypatch
):
    from backend.app.core.rollout_scheduler import RolloutScheduler

    flag_id = new_flag()
    method, path, locked_schedule, ticked, expected = _scenario(
        case, session_factory, flag_id, developer.id
    )

    # The real deps.get_db, whose session is closed on the event loop.
    monkeypatch.delitem(app.dependency_overrides, deps.get_db, raising=False)
    monkeypatch.setattr(deps, "SessionLocal", session_factory)

    # The request holds its locks until the tick is seen waiting on them.
    state: Dict[str, Any] = {"pid": None, "seen": False, "fired": False}
    locked = threading.Event()
    original_lock = rollout_service._lock_schedule_for_change

    def lock_and_hold(db, schedule):
        result = original_lock(db, schedule)
        if result[1].id == locked_schedule and not locked.is_set():
            state["pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar()
            locked.set()
            deadline = time.monotonic() + HOLD_S
            while time.monotonic() < deadline:
                if waiting_on(session_factory, state["pid"]):
                    state["seen"] = True
                    break
                time.sleep(0.02)
        return result

    monkeypatch.setattr(rollout_service, "_lock_schedule_for_change", lock_and_hold)

    # The tick, narrowed to this test's schedules (the database is shared).
    ours = {str(s) for s in ticked}
    original_process = RolloutScheduler._process_schedule

    async def only_ours(self, db, schedule, current_time):
        if str(schedule.id) not in ours:
            return False, 0, []
        return await original_process(self, db, schedule, current_time)

    monkeypatch.setattr(RolloutScheduler, "_process_schedule", only_ours)
    monkeypatch.setattr(
        "backend.app.core.rollout_scheduler.SessionLocal", session_factory
    )

    tick_done = threading.Event()

    def watchdog():
        if tick_done.wait(WATCHDOG_S):
            return
        state["fired"] = True
        observer = session_factory()
        try:
            # Only the request's own backend, and only while it sits idle in
            # its transaction holding the locks.
            observer.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE pid = :pid AND state = 'idle in transaction'"
                ),
                {"pid": state["pid"]},
            )
            observer.commit()
        finally:
            observer.close()

    token = create_local_access_token(developer)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            request = asyncio.create_task(
                http.request(method, path, headers={"Authorization": f"Bearer {token}"})
            )
            deadline = time.monotonic() + HOLD_S
            while not locked.is_set() and not request.done():
                assert time.monotonic() < deadline, "the request never took the locks"
                await asyncio.sleep(0.01)
            threading.Thread(target=watchdog, daemon=True).start()
            scheduler = RolloutScheduler(interval_minutes=1)
            scheduler._notification_service = MagicMock()
            try:
                await scheduler.process_rollout_schedules()
            finally:
                tick_done.set()
            try:
                response = await asyncio.wait_for(request, WATCHDOG_S)
                return response.status_code
            except Exception as exc:  # reported in the assertion below
                return repr(exc)

    status = asyncio.run(run())

    assert (state["seen"], state["fired"], status) == (True, False, expected), (
        "the tick waited on the request's locks until the watchdog freed them"
        if state["fired"]
        else "the request answered unexpectedly"
    )


@pytest.mark.parametrize("route", ["update", "advance"])
def test_a_stage_deleted_while_the_request_waits_answers_404(
    client, developer, new_flag, fresh, session_factory, route
):
    """Another session holds the flag and deletes the stage; the request,
    waiting for the flag, then finds the stage gone. It answers 404, where the
    route's ``except Exception`` turned its own 404 into a 500."""
    flag_id = new_flag()
    created = create(
        client,
        developer,
        flag_id,
        [stage(1, 10, name="a"), stage(2, 20, name="b")],
    )
    if route == "advance":
        ok(send(client, "POST", f"{SCHEDULES}/{created['id']}/activate", developer))
        target = created["ids"]["a"]
        method, path, body = "POST", f"{SCHEDULES}/stages/{target}/advance", None
    else:
        target = created["ids"]["b"]
        method, path, body = "PUT", f"{SCHEDULES}/stages/{target}", {"name": "x"}

    holder = session_factory()
    outcome: Dict[str, Any] = {}

    def request():
        try:
            outcome["response"] = send(client, method, path, developer, body)
        except BaseException as exc:  # reported to the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=request, daemon=True)
    blocked = False
    try:
        holder.query(FeatureFlag).filter(
            FeatureFlag.id == flag_id
        ).with_for_update().one()
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()
        worker.start()
        deadline = time.monotonic() + HOLD_S
        while time.monotonic() < deadline and worker.is_alive():
            if waiting_on(session_factory, holder_pid):
                blocked = True
                break
            time.sleep(0.02)
        if blocked:
            # Deleted, and committed, while the request waits on the flag.
            holder.execute(
                text("DELETE FROM rollout_stages WHERE id = :id"), {"id": str(target)}
            )
            holder.commit()
        else:
            holder.rollback()
    finally:
        if holder.in_transaction():
            holder.rollback()
        holder.close()
        if worker.ident is not None:
            worker.join(WATCHDOG_S)

    assert blocked, "the request was not seen waiting on the flag lock"
    assert not worker.is_alive() and "error" not in outcome, outcome
    response = outcome["response"]
    assert (response.status_code, response.json()) == (
        404,
        {"detail": f"Rollout stage not found with ID: {target}"},
    )
    assert [name for name, *_ in stage_rows(fresh, created["id"])] == (
        ["b"] if route == "advance" else ["a"]
    )

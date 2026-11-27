"""A change of an experiment's status clears its cached results (#2206).

``GET /api/v1/results/{experiment_id}``, and
``GET /api/v1/experiments/{experiment_id}/results``, which answers through it
with the same key, keep their answer in Redis whenever Redis answers,
``CACHE_ENABLED`` or not: for 5 minutes while the experiment is running and 24
hours otherwise. The key names the experiment but not its status, so an answer
read while the experiment was a draft, or paused, went on being served after it
started or resumed, with the old status and the old counts. Each change of
status now clears the experiment's ``results:{experiment_id}:*`` keys once it
is committed: ``POST /start`` (which also resumes), ``/pause``, ``/complete``
and ``/archive``, and the experiment scheduler's start, resume and end.

What would let these pass on the old code, and is avoided:

* ``CACHE_ENABLED`` on: the results cache does not read it, but a clear made
  only with it on would pass. These run with it off, the default.
* a fake cache: the clear is async and has a client of its own. This is the
  real Redis (``test_experiment_cache_redis``'s ``redis_server``, database 15),
  which the integration-tests job requires.
* the status code alone: each test reads the results so that a key exists,
  then writes users and conversions, makes the change, and asserts that no key
  is left and that the next read answers the new status and those counts.

The first read goes through ``/results/{experiment_id}`` (a draft's included),
the one after the change through ``/experiments/{experiment_id}/results``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Tuple

import pytest

from backend.app.core.config import settings
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.tests.integration.api import test_experiment_cache_redis as cache_redis

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = cache_redis.BASE
CLEAR_FAILED = "Results cache clear failed for"

# The Redis on database 15, emptied, and the removal of the experiments made
# with ``cache_redis._create``.
redis_server = cache_redis.redis_server
_remove_the_experiments_created = cache_redis._remove_the_experiments_created

#: What ``write_users`` writes: variant -> (users assigned, of them converting).
WRITTEN = {"Control": (3, 1), "Treatment": (2, 2)}
#: Nothing assigned yet.
NONE_YET = {"Control": (0, 0), "Treatment": (0, 0)}


@pytest.fixture
def uncached_client(redis_server, db_session, developer_user, monkeypatch):
    """A DEVELOPER's client with the real cache dependency and ``CACHE_ENABLED``
    off, as it is by default."""
    yield from cache_redis._cached_client_for(
        db_session, developer_user, monkeypatch, cache_enabled=False
    )


@pytest.fixture
def write_users(db_session) -> Callable:
    """Writes ``WRITTEN`` for an experiment: the assignments, and a purchase by
    each converting user. Removes them afterwards."""
    prefix = f"status-change-{uuid.uuid4().hex[:8]}-"

    def write(experiment: dict, at: datetime) -> None:
        variants = {v["name"]: uuid.UUID(v["id"]) for v in experiment["variants"]}
        session = cache_redis._session_factory(db_session)()
        try:
            for name, (assigned, converting) in WRITTEN.items():
                for n in range(assigned):
                    user_id = f"{prefix}{name}-{n}"
                    session.add(
                        Assignment(
                            experiment_id=uuid.UUID(experiment["id"]),
                            variant_id=variants[name],
                            user_id=user_id,
                            created_at=at.replace(tzinfo=None),
                        )
                    )
                    if n < converting:
                        session.add(
                            Event(
                                event_type="purchase",
                                event_name="purchase",
                                user_id=user_id,
                                experiment_id=uuid.UUID(experiment["id"]),
                                variant_id=variants[name],
                                value=1.0,
                                created_at=(at + timedelta(seconds=1)).isoformat(),
                            )
                        )
            session.commit()
        finally:
            session.close()

    yield write

    session = cache_redis._session_factory(db_session)()
    try:
        for model in (Event, Assignment):
            rows = session.query(model).filter(model.user_id.like(f"{prefix}%"))
            rows.delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keys(redis_server, experiment_id: str) -> list:
    return sorted(redis_server.scan_iter(match=f"results:{experiment_id}:*"))


def _first_read(client, redis_server, experiment_id: str, status: str) -> None:
    """Read the results, so that the next read can come from the cache."""
    resp = client.get(f"/api/v1/results/{experiment_id}")
    assert resp.status_code == 200, resp.text
    assert _answer(resp.json()) == (status, NONE_YET)
    assert _keys(redis_server, experiment_id), "the results were not cached"


def _answer(body: dict) -> Tuple[str, Dict[str, Tuple[int, int]]]:
    """The status, and per variant (users, converting users)."""
    (metric,) = body["metrics"]
    counts = {
        v["variant_name"]: (v["sample_size"], v["conversions"])
        for v in metric["variants"]
    }
    return body["status"], counts


def _read(client, experiment_id: str) -> Tuple[str, Dict[str, Tuple[int, int]]]:
    resp = client.get(f"{BASE}/{experiment_id}/results")
    assert resp.status_code == 200, resp.text
    return _answer(resp.json())


def _post(client, experiment_id: str, action: str) -> dict:
    resp = client.post(f"{BASE}/{experiment_id}/{action}")
    assert resp.status_code == 200, resp.text
    return resp.json()


#: change -> (the route, the routes called before, the status before, after)
ROUTES = {
    "start": ("start", (), "draft", "active"),
    "pause": ("pause", ("start",), "active", "paused"),
    "resume": ("start", ("start", "pause"), "paused", "active"),
    "complete": ("complete", ("start",), "active", "completed"),
    "archive": ("archive", ("start", "complete"), "completed", "archived"),
}


@pytest.mark.parametrize("change", sorted(ROUTES))
def test_a_status_change_through_the_routes_clears_the_cached_results(
    uncached_client, redis_server, write_users, caplog, change
):
    client = uncached_client
    route, before, status_before, status_after = ROUTES[change]
    exp = cache_redis._create(client)
    for action in before:
        _post(client, exp["id"], action)
    _first_read(client, redis_server, exp["id"], status_before)
    write_users(exp, _now())

    assert _post(client, exp["id"], route)["status"] == status_after

    assert _keys(redis_server, exp["id"]) == [], "a cached result survived"
    assert _read(client, exp["id"]) == (status_after, WRITTEN)
    assert CLEAR_FAILED not in caplog.text


def test_pause_resume_pause_with_no_read_between_clears_the_first_pause_s_results(
    uncached_client, redis_server, write_users
):
    """The results read during the first pause are not served during the
    second, though the status is the same: a key that named the status would
    find them again."""
    client = uncached_client
    exp = cache_redis._started(client)
    _post(client, exp["id"], "pause")
    _first_read(client, redis_server, exp["id"], "paused")

    _post(client, exp["id"], "start")
    write_users(exp, _now())
    _post(client, exp["id"], "pause")

    assert _keys(redis_server, exp["id"]) == [], "a cached result survived"
    assert _read(client, exp["id"]) == ("paused", WRITTEN)


#: due -> (how it is made due, the status before the tick, after)
DUE = {
    "start": (lambda client, db: cache_redis._due_to_start(client), "draft", "active"),
    "resume": (
        lambda client, db: cache_redis._due_to_resume(client),
        "paused",
        "active",
    ),
    "end": (cache_redis._due_to_end, "active", "completed"),
}


@pytest.mark.parametrize("due", sorted(DUE))
def test_a_scheduled_status_change_clears_the_cached_results(
    uncached_client, redis_server, db_session, write_users, caplog, due
):
    client = uncached_client
    make_due, status_before, status_after = DUE[due]
    exp = make_due(client, db_session)
    _first_read(client, redis_server, exp["id"], status_before)
    # Before the end date of the one made due to end (an hour ago).
    write_users(exp, _now() - timedelta(minutes=90))

    cache_redis._tick(client, db_session)

    assert _keys(redis_server, exp["id"]) == [], "a cached result survived"
    assert _read(client, exp["id"]) == (status_after, WRITTEN)
    assert CLEAR_FAILED not in caplog.text


@pytest.fixture
def refused_redis_client(db_session, developer_user, monkeypatch):
    """``uncached_client`` with nothing listening at the Redis port."""
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", "1")
    yield from cache_redis._cached_client_for(
        db_session, developer_user, monkeypatch, cache_enabled=False
    )


@pytest.mark.parametrize("change", sorted(ROUTES))
def test_a_status_change_is_made_when_the_results_cannot_be_cleared(
    refused_redis_client, caplog, change
):
    """The change is committed before the clear: a Redis that cannot be reached
    then is logged, and the change is answered as made."""
    client = refused_redis_client
    route, before, _, status_after = ROUTES[change]
    exp = cache_redis._create(client)
    for action in before:
        _post(client, exp["id"], action)
    caplog.clear()

    assert _post(client, exp["id"], route)["status"] == status_after

    assert f"{CLEAR_FAILED} {exp['id']}" in caplog.text


def test_a_tick_makes_every_change_when_the_results_cannot_be_cleared(
    refused_redis_client, db_session, caplog
):
    client = refused_redis_client
    before = cache_redis._tick(client, db_session)
    starts = cache_redis._due_to_start(client)
    ends = cache_redis._due_to_end(client, db_session)
    caplog.clear()

    after = cache_redis._tick(client, db_session)

    assert after["items_failed"] == before["items_failed"]
    for exp, status in ((starts, "active"), (ends, "completed")):
        resp = client.get(f"{BASE}/{exp['id']}/results")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == status
        assert f"{CLEAR_FAILED} {exp['id']}" in caplog.text

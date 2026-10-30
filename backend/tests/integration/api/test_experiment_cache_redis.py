"""The experiment routes with the cache on, against a real Redis (#428).

``deps.get_cache_control`` hands the routes a ``redis.asyncio`` client. Most
experiment routes called it without ``await``: ``delete`` returned a coroutine
that never ran, and ``for key in redis.scan_iter(...)`` raised ``TypeError``
(an async generator is not iterable) *after* the change was committed -- so
update, delete, start, pause, schedule, complete, archive and clone answered
500 for a change that had in fact been made. Metadata left the cached detail
behind, and the daily and segmented results were never written to the cache
(``setex`` un-awaited). Create cleared only the caller's own lists. The same
defect #427 fixed for the feature-flag routes (#100).

What would let these pass on the old code, and is avoided:

* a fake or synchronous Redis hides a missing ``await`` -- this is the real
  server and the real client, built by the real dependency;
* the shared client fixtures override ``get_cache_control`` to disabled --
  that override is removed here;
* a ``TestClient`` outside a ``with`` block runs requests on different event
  loops, the pooled client breaks, ``get_cache_control`` swallows the error and
  the cache is silently off -- so the client is a context manager, and every
  test asserts on what is actually in Redis, not only on the status code.

The Redis is ``REDIS_HOST``/``REDIS_PORT`` (the integration-tests job's
``redis`` service). Database 15 is used and flushed, never the default one.
Without a reachable Redis this skips -- core-build and the nightly run have
none -- unless ``EXPERIMENTLY_REQUIRE_REDIS=1``, which makes it a failure.

The writers outside the experiment routes are at the end: adding an experiment
to an exclusion group or removing it, and the experiment scheduler starting,
resuming or completing one. They left the cached detail behind, so with the
cache on the experiment page showed the old group or the old status for an
hour.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import redis as sync_redis
import redis.asyncio as async_redis
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import experiments as experiments_endpoint
from backend.app.core.config import settings
from backend.app.core.permissions import Action, ResourceType
from backend.app.main import app
from backend.app.models.experiment import Experiment
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.schemas.experiment import ExperimentResponse
from backend.app.services.experiment_service import ExperimentService
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/experiments"
CACHE_DB = 15
PREFIX = "cache428-"


@pytest.fixture
def redis_server(monkeypatch):
    """A synchronous handle on the Redis the application will use, emptied."""
    monkeypatch.setattr(settings, "REDIS_DB", CACHE_DB)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", None)
    monkeypatch.setattr(settings, "REDIS_SSL", False)
    handle = sync_redis.Redis(
        host=str(settings.REDIS_HOST),
        port=int(settings.REDIS_PORT),
        db=CACHE_DB,
        decode_responses=True,
        socket_connect_timeout=2,
    )
    try:
        handle.ping()
    except sync_redis.RedisError as exc:
        message = f"no Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT}: {exc}"
        if os.environ.get("EXPERIMENTLY_REQUIRE_REDIS") == "1":
            pytest.fail(message + " but EXPERIMENTLY_REQUIRE_REDIS=1")
        pytest.skip(message)
    handle.flushdb()
    yield handle
    handle.flushdb()
    handle.close()


def _cached_client_for(db_session, user, monkeypatch, cache_enabled=True):
    """``user``'s client with the real ``get_cache_control`` and the cache on
    (or, with ``cache_enabled=False``, off as it is by default)."""
    monkeypatch.setattr(settings, "CACHE_ENABLED", cache_enabled)
    monkeypatch.setattr(deps, "_redis_pool", None)  # built on this client's loop
    make_client_for_user(db_session, user)
    app.dependency_overrides.pop(deps.get_cache_control)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def cached_client(redis_server, db_session, developer_user, monkeypatch):
    """A non-superuser DEVELOPER, who owns every experiment it creates."""
    yield from _cached_client_for(db_session, developer_user, monkeypatch)


@pytest.fixture
def cached_superuser_client(redis_server, db_session, admin_user, monkeypatch):
    """Only for the detail read, which answers 403 to every non-superuser --
    owner included -- whether or not the cache is on (a separate defect)."""
    yield from _cached_client_for(db_session, admin_user, monkeypatch)


@pytest.fixture(autouse=True)
def _remove_the_experiments_created(db_session):
    yield
    db_session.rollback()
    for row in db_session.query(Experiment).filter(Experiment.name.like(f"%{PREFIX}%")):
        db_session.delete(row)  # the ORM cascades to variants and metrics
    db_session.commit()
    groups = db_session.query(MutualExclusionGroup)
    for row in groups.filter(MutualExclusionGroup.name.like(f"{PREFIX}%")):
        db_session.delete(row)
    db_session.commit()


def _create(client: TestClient) -> dict:
    name = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"{BASE}/",
        json={
            "name": name,
            "description": "cache test",
            "hypothesis": "the cache is dropped",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Conversion",
                    "event_name": "purchase",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _started(client: TestClient) -> dict:
    exp = _create(client)
    resp = client.post(f"{BASE}/{exp['id']}/start")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _plant_lists(redis_server, owner_id) -> tuple[str, str]:
    """A cached list for the owner and one for someone else, both stale after
    a change: every role sees every experiment (#83)."""
    own_list = f"experiments:{owner_id}:0:100"
    other_list = f"experiments:{uuid.uuid4()}:0:100"
    redis_server.set(own_list, "{}")
    redis_server.set(other_list, "{}")
    return own_list, other_list


def test_the_cache_dependency_is_live(cached_client, redis_server):
    """Guards the rest: were the cache silently off, they could not fail."""
    control = cached_client.portal.call(deps.get_cache_control)
    assert control.enabled is True
    assert control.redis is not None


def test_the_experiment_detail_is_cached_and_served_from_the_cache(
    cached_superuser_client, redis_server
):
    cached_client = cached_superuser_client
    exp = _create(cached_client)
    first = cached_client.get(f"{BASE}/{exp['id']}")
    assert first.status_code == 200, first.text

    key = f"experiment:{exp['id']}"
    stored = redis_server.get(key)
    assert stored is not None, "the detail was not written to the cache"
    assert json.loads(stored)["id"] == exp["id"]

    # A marker only the cache holds: the second answer must come from Redis.
    redis_server.set(key, json.dumps({**json.loads(stored), "name": "from-cache"}))
    second = cached_client.get(f"{BASE}/{exp['id']}")
    assert second.status_code == 200, second.text
    assert second.json()["name"] == "from-cache"


@pytest.fixture
def cached_unreadable_client(redis_server, db_session, viewer_user, monkeypatch):
    """A non-superuser VIEWER whose role is denied READ on experiments.

    Every role carries that READ today, so the denial is planted in the
    route's own permission check: the route then refuses this caller on its
    role alone, whatever it decides about ownership.
    """
    real = experiments_endpoint.check_permission

    def no_experiment_read(user, resource, action):
        if resource == ResourceType.EXPERIMENT and action == Action.READ:
            return False
        return real(user, resource, action)

    monkeypatch.setattr(experiments_endpoint, "check_permission", no_experiment_read)
    yield from _cached_client_for(db_session, viewer_user, monkeypatch)


def test_a_caller_refused_the_detail_is_refused_it_from_the_cache(
    cached_unreadable_client, redis_server, make_experiment, db_session
):
    """The cached detail is returned only after the same checks as an
    uncached read."""
    exp = make_experiment(name=f"{PREFIX}{uuid.uuid4().hex[:8]}")
    url = f"{BASE}/{exp.id}"

    uncached = cached_unreadable_client.get(url)
    assert uncached.status_code == 403, uncached.text
    assert redis_server.exists(f"experiment:{exp.id}") == 0

    # A complete, valid detail, as the route itself would have cached it.
    detail = ExperimentResponse.model_validate(
        ExperimentService(db_session).get_experiment_by_id(exp.id)
    ).model_dump(mode="json")
    redis_server.set(
        f"experiment:{exp.id}", json.dumps({**detail, "name": "from-cache"})
    )
    cached = cached_unreadable_client.get(url)
    assert cached.status_code == uncached.status_code, cached.text
    assert "from-cache" not in cached.text


# name -> (path suffix under the experiment, cache key suffix)
RESULTS = {
    "daily": ("/daily-results", "experiment_daily_results:{id}"),
    "segmented": (
        "/segmented-results/country",
        "experiment_segmented_results:{id}:country",
    ),
}


@pytest.mark.parametrize("kind", sorted(RESULTS))
def test_results_are_cached_and_served_from_the_cache(
    cached_client, redis_server, kind
):
    suffix, key_shape = RESULTS[kind]
    exp = _started(cached_client)
    first = cached_client.get(f"{BASE}/{exp['id']}{suffix}")
    assert first.status_code == 200, first.text

    key = key_shape.format(id=exp["id"])
    stored = redis_server.get(key)
    assert stored is not None, f"the {kind} results were not written to the cache"
    assert json.loads(stored) == first.json()

    marker = [{"from": "cache"}] if kind == "daily" else {"from": "cache"}
    redis_server.set(key, json.dumps(marker))
    second = cached_client.get(f"{BASE}/{exp['id']}{suffix}")
    assert second.status_code == 200, second.text
    assert second.json() == marker


def _future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# name -> (method, suffix, json body, query, starts ACTIVE?, success code)
MUTATIONS = {
    "update": ("PUT", "", {"description": "changed"}, None, False, 200),
    "update_targeting": (
        "PUT",
        "",
        {
            "targeting_rules": {
                "logical_operator": "AND",
                "groups": [
                    {
                        "conditions": [
                            {"attribute": "country", "operator": "in", "value": ["GB"]}
                        ]
                    }
                ],
            }
        },
        None,
        False,
        200,
    ),
    "delete": ("DELETE", "", None, "experiment_key", False, 204),
    "start": ("POST", "/start", None, None, False, 200),
    "pause": ("POST", "/pause", None, None, True, 200),
    "schedule": (
        "PUT",
        "/schedule",
        {"start_date": _future(1), "end_date": _future(8), "time_zone": "UTC"},
        None,
        False,
        200,
    ),
    "complete": ("POST", "/complete", None, None, True, 200),
    "archive": ("POST", "/archive", None, None, False, 200),
    "metadata": ("POST", "/metadata", {"note": "cache428"}, None, False, 200),
}


@pytest.mark.parametrize("action", sorted(MUTATIONS))
def test_a_change_drops_the_experiment_and_every_cached_list(
    cached_client, redis_server, developer_user, action
):
    method, suffix, body, query, active, ok = MUTATIONS[action]
    exp = _started(cached_client) if active else _create(cached_client)

    # Planted, not read through the route: the detail read refuses this
    # developer (see ``cached_superuser_client``).
    detail_key = f"experiment:{exp['id']}"
    redis_server.set(detail_key, json.dumps(exp))
    own_list, other_list = _plant_lists(redis_server, developer_user.id)
    assert redis_server.exists(detail_key, own_list, other_list) == 3

    params = {"experiment_key": exp["id"]} if query else None
    resp = cached_client.request(
        method, f"{BASE}/{exp['id']}{suffix}", json=body, params=params
    )
    assert resp.status_code == ok, resp.text

    assert redis_server.exists(detail_key) == 0, "the cached detail survived"
    assert redis_server.exists(own_list, other_list) == 0, "a cached list survived"


#: Dashboard-shape rules, before and after a targeting change (#523).
TARGETING_BEFORE = {
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
TARGETING_AFTER = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "GB"}
            ],
        }
    ],
}


def test_a_targeting_change_is_read_back_at_once(cached_superuser_client, redis_server):
    """After a targeting change the detail is the new rules, not the cached
    old ones: the PUT drops the cached detail."""
    client = cached_superuser_client
    exp = _create(client)
    set_rules = client.put(
        f"{BASE}/{exp['id']}", json={"targeting_rules": TARGETING_BEFORE}
    )
    assert set_rules.status_code == 200, set_rules.text
    first = client.get(f"{BASE}/{exp['id']}")
    assert first.status_code == 200, first.text
    assert first.json()["targeting_rules"] == TARGETING_BEFORE
    assert redis_server.exists(f"experiment:{exp['id']}") == 1, "not cached"

    change = client.put(
        f"{BASE}/{exp['id']}", json={"targeting_rules": TARGETING_AFTER}
    )
    assert change.status_code == 200, change.text

    assert redis_server.exists(f"experiment:{exp['id']}") == 0, (
        "the cached detail survived"
    )
    second = client.get(f"{BASE}/{exp['id']}")
    assert second.status_code == 200, second.text
    assert second.json()["targeting_rules"] == TARGETING_AFTER


@pytest.mark.parametrize("action", ["create", "clone"])
def test_a_new_experiment_drops_every_cached_list(
    cached_client, redis_server, developer_user, action
):
    source = _create(cached_client) if action == "clone" else None
    own_list, other_list = _plant_lists(redis_server, developer_user.id)

    if action == "clone":
        resp = cached_client.post(f"{BASE}/{source['id']}/clone")
        assert resp.status_code == 201, resp.text
    else:
        _create(cached_client)

    assert redis_server.exists(own_list, other_list) == 0, "a cached list survived"


def test_a_redis_error_after_the_change_is_logged_not_raised(
    cached_client, redis_server, monkeypatch, caplog
):
    """The change is committed before the cache is touched: a Redis failure
    then must not turn a change that was made into a 500."""
    exp = _create(cached_client)
    control = cached_client.portal.call(deps.get_cache_control)

    def broken_scan_iter(*args, **kwargs):
        raise sync_redis.ConnectionError("redis went away")

    monkeypatch.setattr(type(control.redis), "scan_iter", broken_scan_iter)
    resp = cached_client.put(f"{BASE}/{exp['id']}", json={"description": "changed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "changed"
    assert "Experiment cache invalidation failed" in caplog.text


# --- the writers outside the experiment routes --------------------------------
#
# Adding an experiment to an exclusion group or removing it, and the experiment
# scheduler starting, resuming or completing one, change what the detail shows.
# Each drops the cached detail once its change is committed, through
# ``backend.app.api.experiment_cache``, which also builds the key the detail
# route reads and writes.

GROUPS = "/api/v1/mutual-exclusion-groups"
#: What the "renamed" run of ``detail_key_prefix`` puts before the detail's key.
RENAMED = "renamed-by-the-test:"


@pytest.fixture(params=["as-built", "renamed"])
def detail_key_prefix(request, monkeypatch):
    """What the detail's cache key starts with in this run ("" as built).

    The "renamed" run puts ``RENAMED`` before what
    ``experiment_cache.experiment_detail_cache_key`` returns. The detail route
    and every writer build the key with it, so they all follow; a site that
    typed the key out itself reads or drops the old name, and the run fails.
    """
    if request.param == "as-built":
        return ""
    from backend.app.api import experiment_cache

    built = experiment_cache.experiment_detail_cache_key
    monkeypatch.setattr(
        experiment_cache,
        "experiment_detail_cache_key",
        lambda experiment_id: RENAMED + built(experiment_id),
    )
    return RENAMED


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _warm(client: TestClient, redis_server, experiment_id: str, key_prefix: str):
    """Read the detail once, so that the next read can come from the cache."""
    resp = client.get(f"{BASE}/{experiment_id}")
    assert resp.status_code == 200, resp.text
    keys = [key for key in redis_server.scan_iter() if experiment_id in key]
    assert len(keys) == 1 and keys[0].startswith(key_prefix), (
        f"the detail was not cached under the builder's key: {keys}"
    )
    return resp.json()


def _detail(client: TestClient, experiment_id: str) -> dict:
    resp = client.get(f"{BASE}/{experiment_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_group(client: TestClient) -> dict:
    resp = client.post(GROUPS, json={"name": f"{PREFIX}{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add(client: TestClient, group_id: str, experiment_id: str):
    return client.post(
        f"{GROUPS}/{group_id}/experiments", json={"experiment_id": experiment_id}
    )


def _remove(client: TestClient, group_id: str, experiment_id: str):
    return client.delete(f"{GROUPS}/{group_id}/experiments/{experiment_id}")


def _session_factory(db_session):
    """Sessions on the test database, for the scheduler's ``SessionLocal``."""
    factory = sessionmaker(bind=db_session.get_bind())

    def session():
        s = factory()
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


def _tick(client: TestClient, db_session) -> dict:
    """One experiment scheduler tick against the test database; its counts.

    It runs on the client's event loop: the scheduler uses the API's shared
    Redis pool, which was built on that loop, as in production the scheduler
    runs on the server's. The database is shared, so the tick also acts on any
    other experiment that is due.
    """
    from backend.app.core.scheduler import ExperimentScheduler

    scheduler = ExperimentScheduler()
    scheduler._notification_service = MagicMock()
    with patch("backend.app.core.scheduler.SessionLocal", _session_factory(db_session)):
        return client.portal.call(scheduler.process_scheduled_experiments)


def _schedule_a_minute_ago(client: TestClient, experiment_id: str) -> dict:
    resp = client.put(
        f"{BASE}/{experiment_id}/schedule",
        json={"start_date": (_now() - timedelta(minutes=1)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _due_to_start(client: TestClient) -> dict:
    """A draft whose scheduled start was a minute ago."""
    return _schedule_a_minute_ago(client, _create(client)["id"])


def _due_to_resume(client: TestClient) -> dict:
    """A paused experiment whose scheduled resume was a minute ago."""
    exp = _started(client)
    paused = client.post(f"{BASE}/{exp['id']}/pause")
    assert paused.status_code == 200, paused.text
    scheduled = _schedule_a_minute_ago(client, exp["id"])
    assert scheduled["status"] == "paused"
    return scheduled


def _due_to_end(client: TestClient, db_session) -> dict:
    """A running experiment whose end date has passed, written to its row."""
    exp = _started(client)
    session = _session_factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(exp["id"]))
        row.start_date = _now() - timedelta(hours=2)
        row.end_date = _now() - timedelta(hours=1)
        session.commit()
    finally:
        session.close()
    return exp


def test_adding_an_experiment_to_a_group_is_read_back_at_once(
    cached_superuser_client, redis_server, detail_key_prefix
):
    client = cached_superuser_client
    exp = _create(client)
    group = _create_group(client)
    warm = _warm(client, redis_server, exp["id"], detail_key_prefix)
    assert warm["mutual_exclusion_group_id"] is None

    added = _add(client, group["id"], exp["id"])
    assert added.status_code == 200, added.text

    assert _detail(client, exp["id"])["mutual_exclusion_group_id"] == group["id"]


def test_removing_an_experiment_from_a_group_is_read_back_at_once(
    cached_superuser_client, redis_server, detail_key_prefix
):
    client = cached_superuser_client
    exp = _create(client)
    group = _create_group(client)
    assert _add(client, group["id"], exp["id"]).status_code == 200
    warm = _warm(client, redis_server, exp["id"], detail_key_prefix)
    assert warm["mutual_exclusion_group_id"] == group["id"]

    removed = _remove(client, group["id"], exp["id"])
    assert removed.status_code == 200, removed.text

    assert _detail(client, exp["id"])["mutual_exclusion_group_id"] is None


@pytest.mark.parametrize("due", ["start", "resume"])
def test_a_scheduled_start_or_resume_is_read_back_at_once(
    cached_superuser_client, redis_server, db_session, detail_key_prefix, due
):
    client = cached_superuser_client
    exp = _due_to_start(client) if due == "start" else _due_to_resume(client)
    warm = _warm(client, redis_server, exp["id"], detail_key_prefix)
    assert warm["status"] == ("draft" if due == "start" else "paused")

    _tick(client, db_session)

    assert _detail(client, exp["id"])["status"] == "active"


def test_a_scheduled_end_is_read_back_at_once(
    cached_superuser_client, redis_server, db_session, detail_key_prefix
):
    client = cached_superuser_client
    exp = _due_to_end(client, db_session)
    assert (
        _warm(client, redis_server, exp["id"], detail_key_prefix)["status"] == "active"
    )

    _tick(client, db_session)

    assert _detail(client, exp["id"])["status"] == "completed"


@pytest.fixture
def uncached_superuser_client(db_session, admin_user, monkeypatch):
    """The real ``get_cache_control`` with ``CACHE_ENABLED`` off, the default."""
    yield from _cached_client_for(
        db_session, admin_user, monkeypatch, cache_enabled=False
    )


def test_with_the_cache_off_no_writer_calls_redis(
    uncached_superuser_client, db_session, monkeypatch
):
    """No Redis is asked for and no command is sent: not by the group routes,
    not by a tick that starts one experiment and completes another."""
    client = uncached_superuser_client
    calls = []
    real_pool = deps.get_redis_pool

    async def spied_pool():
        calls.append("get_redis_pool")
        return await real_pool()

    async def spied_command(self, *args, **options):
        calls.append(args)

    monkeypatch.setattr(deps, "get_redis_pool", spied_pool)
    monkeypatch.setattr(async_redis.Redis, "execute_command", spied_command)

    exp = _create(client)
    group = _create_group(client)
    assert _add(client, group["id"], exp["id"]).status_code == 200
    assert _remove(client, group["id"], exp["id"]).status_code == 200
    starts = _due_to_start(client)
    ends = _due_to_end(client, db_session)
    _tick(client, db_session)

    assert _detail(client, starts["id"])["status"] == "active"
    assert _detail(client, ends["id"])["status"] == "completed"
    assert calls == []


@pytest.fixture
def failing_redis_client(redis_server, db_session, admin_user, monkeypatch):
    """A superuser's client with the cache on and a Redis that goes away after
    answering the ping: every delete then raises, which is what reaches the
    writers' own error handling. (A Redis that never answers is the dependency's
    own path: its ping fails and the cache is treated as off.)"""

    def broken_delete(*args, **kwargs):
        raise sync_redis.ConnectionError("redis went away")

    for client in _cached_client_for(db_session, admin_user, monkeypatch):
        control = client.portal.call(deps.get_cache_control)
        assert control.enabled, "the cache is off: this would test nothing"
        monkeypatch.setattr(type(control.redis), "delete", broken_delete)
        yield client


@pytest.mark.parametrize("change", ["add", "remove"])
def test_a_group_change_is_made_when_redis_fails(failing_redis_client, caplog, change):
    """The change is committed before the cache is touched: a Redis failure
    then must not turn a change that was made into an error."""
    client = failing_redis_client
    exp = _create(client)
    group = _create_group(client)
    if change == "remove":
        assert _add(client, group["id"], exp["id"]).status_code == 200
    caplog.clear()

    if change == "add":
        resp = _add(client, group["id"], exp["id"])
    else:
        resp = _remove(client, group["id"], exp["id"])

    assert resp.status_code == 200, resp.text
    expected = group["id"] if change == "add" else None
    assert _detail(client, exp["id"])["mutual_exclusion_group_id"] == expected
    assert "Experiment cache invalidation failed" in caplog.text


def test_a_tick_makes_every_change_when_redis_fails(
    failing_redis_client, db_session, caplog
):
    """A tick drops the started experiments' details before it completes any:
    a Redis failure there must not stop the completions, or count as a
    failed item."""
    client = failing_redis_client
    before = _tick(client, db_session)
    starts = _due_to_start(client)
    ends = _due_to_end(client, db_session)
    caplog.clear()

    after = _tick(client, db_session)

    assert _detail(client, starts["id"])["status"] == "active"
    assert _detail(client, ends["id"])["status"] == "completed"
    assert after["items_failed"] == before["items_failed"]
    assert "Experiment cache invalidation failed" in caplog.text

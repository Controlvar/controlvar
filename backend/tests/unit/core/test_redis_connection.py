"""Every Redis client takes its connection from one helper (#147, #236).

``backend/app/core/redis_client.py`` is the only place a Redis client is
built. It reads ``REDIS_HOST``, ``REDIS_PORT``, ``REDIS_PASSWORD``,
``REDIS_DB`` and ``REDIS_SSL`` from the settings and refuses a caller that
tries to pass them itself.

History: the clients used to be built at each call site, and each site chose
its own subset of the settings. #147 found that none of them passed
``ssl=`` -- the deployed ElastiCache refuses plaintext. #236 found that three
of the five still passed no ``password=``: ``deps.get_redis_pool`` and both
clients in ``results.py``, so against a Redis that requires AUTH the cache
silently never worked while the rate limiter and the readiness probe did.

The gate has three parts, and each is needed:

* **The constructor scan.** Every call in ``backend/`` and ``modules/``
  (tests excluded) whose callee is ``Redis``, ``StrictRedis``,
  ``RedisCluster``, ``ConnectionPool`` or ``from_url`` is found by walking the
  AST. The only ones allowed are the two in the helper. A raw
  ``redis.Redis(host=...)`` anywhere else fails here. A call is skipped only
  when the file imports the name it is made through from a package other than
  ``redis`` (``httpcore.ConnectionPool``, or ``ConnectionPool`` imported from
  ``httpcore``); one made through a ``redis`` import, or through a name of
  unknown origin, is still counted.
* **The call-site scan.** Every call of ``create_redis_client`` /
  ``create_async_redis_client`` must be in :data:`SITES`, with a driver.
* **The drive.** Each site is executed against a recording stand-in for the
  client class with distinctive settings, and must connect with exactly those
  settings. The stand-in records the helper frame and the frame that called
  it, and the latter must be the line the scan found -- so a driver cannot
  quietly exercise a different call from the one scanned.

``deps.get_redis_pool`` builds ``redis.asyncio.Redis``, a different class from
``redis.Redis``: patching the latter does not intercept it, so the two are
recorded separately and each site must build the class it is classified as.

**One connect attempt (#810).** redis-py retries ten times with backoff by
default, so one call against a Redis that was down made eleven connect
attempts: seconds with Redis refused, and longer than the 5 s probe timeouts
with Redis unreachable. Every site has a retry decision in
:data:`SINGLE_ATTEMPT` or :data:`RETRY_DECIDED_ELSEWHERE`, and each
``SINGLE_ATTEMPT`` site is driven with the real client against a refused
address and must try to connect exactly once.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import redis
import redis.asyncio

from backend.app.core import redis_client
from backend.app.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[4]
SCANNED_ROOTS = ("backend", "modules")

HELPER_PATH = "backend/app/core/redis_client.py"

#: The callee names that build a Redis connection.
CONSTRUCTORS = {"Redis", "StrictRedis", "RedisCluster", "ConnectionPool", "from_url"}

#: The only places a constructor may be called.
ALLOWED_CONSTRUCTORS = {
    (HELPER_PATH, "create_redis_client"),
    (HELPER_PATH, "create_async_redis_client"),
}

#: helper name -> the client class it builds
HELPERS = {"create_redis_client": "sync", "create_async_redis_client": "asyncio"}

#: Every call of a helper outside the tests, as (path, enclosing function).
#: Adding one means adding it here AND a driver in ``DRIVERS``.
SITES = {
    ("backend/app/middleware/rate_limiter.py", "RedisRateLimiter._get_redis"),
    ("backend/app/core/health.py", "check_redis"),
    ("backend/app/api/deps.py", "get_redis_pool"),
    ("backend/app/api/v1/endpoints/results.py", "_get_cache_service"),
    ("backend/app/api/v1/endpoints/results.py", "invalidate_results_cache"),
    ("backend/app/api/v1/endpoints/results.py", "_async_cache_client"),
}

#: Distinctive settings, so a site that ignores one is visible.
CONFIGURED = {
    "REDIS_HOST": "redis.s2.internal",
    "REDIS_PORT": "6390",
    "REDIS_PASSWORD": "s3cret-236",
    "REDIS_DB": 3,
    "REDIS_SSL": True,
}
EXPECTED = {
    "host": "redis.s2.internal",
    "port": 6390,
    "password": "s3cret-236",
    "db": 3,
    "ssl": True,
}


def _is_test_path(relative: Path) -> bool:
    return (
        "tests" in relative.parts
        or relative.name.startswith("test_")
        or relative.name == "conftest.py"
    )


def _is_redis_module(module: str | None) -> bool:
    return module is not None and (module == "redis" or module.startswith("redis."))


def _import_origins(tree: ast.AST) -> dict[str, bool]:
    """``{bound name: True if it comes from the redis package}`` for every import.

    A name bound by more than one import counts as redis if any of them is.
    """
    origins: dict[str, bool] = {}

    def bind(name: str, is_redis: bool) -> None:
        origins[name] = origins.get(name, False) or is_redis

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bind(alias.asname, _is_redis_module(alias.name))
                else:
                    # ``import a.b`` binds ``a``.
                    bind(alias.name.split(".")[0], _is_redis_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            from_redis = node.level == 0 and _is_redis_module(node.module)
            for alias in node.names:
                bind(alias.asname or alias.name, from_redis)
    return origins


def _redis_aliases(tree: ast.AST) -> dict[str, str]:
    """``{local name: imported name}`` for ``from redis... import X as Y``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_redis_module(node.module)
        ):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def _root_name(expr: ast.expr) -> str | None:
    """The name an attribute chain starts from (``a`` in ``a.b.c``), if any."""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _made_through_another_package(call: ast.Call, origins: dict[str, bool]) -> bool:
    """True only when the callee is reached through a non-redis import."""
    root = (
        call.func.id
        if isinstance(call.func, ast.Name)
        else _root_name(call.func.value)
        if isinstance(call.func, ast.Attribute)
        else None
    )
    return root is not None and origins.get(root) is False


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _calls_with_scope(tree: ast.AST):
    """Yield ``(qualified enclosing function, call)`` for every call."""

    def visit(node: ast.AST, scope: list[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from visit(child, scope + [child.name])
            else:
                if isinstance(child, ast.Call):
                    yield ".".join(scope) or "<module>", child
                yield from visit(child, scope)

    yield from visit(tree, [])


def scan(
    names, roots=SCANNED_ROOTS, *, redis_only: bool = False
) -> dict[tuple[str, str], tuple[int, str]]:
    """``{(path, enclosing function): (line, callee)}`` for calls of ``names``.

    ``redis_only`` (the constructor scan) skips a call made through a name the
    file imports from a package other than ``redis``; the helper scan keeps
    every call, since the helpers are imported from ``backend``.
    """
    found: dict[tuple[str, str], tuple[int, str]] = {}
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT)
            if _is_test_path(relative) or "node_modules" in relative.parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(relative))
            origins = _import_origins(tree)
            aliases = _redis_aliases(tree) if redis_only else {}
            for scope, call in _calls_with_scope(tree):
                callee = _callee_name(call)
                if isinstance(call.func, ast.Name):
                    # ``from redis import Redis as R; R(...)`` is a Redis(...).
                    callee = aliases.get(callee, callee)
                if callee in names and not (
                    redis_only and _made_through_another_package(call, origins)
                ):
                    key = (relative.as_posix(), scope)
                    assert key not in found, f"two Redis client calls in {key}"
                    found[key] = (call.lineno, callee)
    return found


# ---------------------------------------------------------------------------
# The recording stand-in
# ---------------------------------------------------------------------------


def _where(frame) -> tuple[str, int]:
    filename = Path(frame.f_code.co_filename).resolve()
    try:
        return filename.relative_to(REPO_ROOT).as_posix(), frame.f_lineno
    except ValueError:
        return str(filename), frame.f_lineno


class Recorder:
    """Stands in for a Redis client class; records every construction."""

    def __init__(self):
        #: ((constructor frame), (the frame that called it), kwargs)
        self.calls: list[tuple[tuple[str, int], tuple[str, int], dict]] = []

    def __call__(self, *args, **kwargs):
        constructor = sys._getframe(1)
        self.calls.append((_where(constructor), _where(constructor.f_back), kwargs))
        client = MagicMock(name="redis-client")
        client.ping.return_value = True
        return client


def _drive_rate_limiter() -> None:
    from backend.app.middleware.rate_limiter import RateLimitMiddleware

    async def app(scope, receive, send):  # pragma: no cover - never called
        pass

    # Through the middleware, as the application builds it.
    middleware = RateLimitMiddleware(app, enabled=True)
    assert middleware._limiter._get_redis() is not None


def _drive_health() -> None:
    from backend.app.core.health import check_redis

    assert check_redis()["status"] == "healthy"


def _drive_deps() -> None:
    from backend.app.api import deps

    deps._redis_pool = None
    try:
        assert asyncio.run(deps.get_redis_pool()) is not None
    finally:
        deps._redis_pool = None


def _drive_results_cache() -> None:
    from backend.app.api.v1.endpoints.results import _get_cache_service

    assert _get_cache_service().enabled


def _drive_results_invalidate() -> None:
    from backend.app.api.v1.endpoints.results import invalidate_results_cache

    invalidate_results_cache(experiment_id=uuid4(), db=None, current_user=None)


def _drive_results_clear() -> None:
    from backend.app.api.v1.endpoints.results import clear_cached_results

    # The stand-in client cannot scan; the clear is best-effort and says nothing.
    assert asyncio.run(clear_cached_results(uuid4())) is None


#: site -> (driver, which client class it builds)
DRIVERS: dict[tuple[str, str], tuple[Callable[[], None], str]] = {
    ("backend/app/middleware/rate_limiter.py", "RedisRateLimiter._get_redis"): (
        _drive_rate_limiter,
        "sync",
    ),
    ("backend/app/core/health.py", "check_redis"): (_drive_health, "sync"),
    ("backend/app/api/deps.py", "get_redis_pool"): (_drive_deps, "asyncio"),
    ("backend/app/api/v1/endpoints/results.py", "_get_cache_service"): (
        _drive_results_cache,
        "sync",
    ),
    ("backend/app/api/v1/endpoints/results.py", "invalidate_results_cache"): (
        _drive_results_invalidate,
        "sync",
    ),
    ("backend/app/api/v1/endpoints/results.py", "_async_cache_client"): (
        _drive_results_clear,
        "asyncio",
    ),
}


def _configure(monkeypatch, values: dict) -> None:
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


def _record(monkeypatch) -> tuple[Recorder, Recorder]:
    sync, asyncio_ = Recorder(), Recorder()
    monkeypatch.setattr(redis, "Redis", sync)
    monkeypatch.setattr(redis.asyncio, "Redis", asyncio_)
    return sync, asyncio_


# ---------------------------------------------------------------------------
# The scans
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.regression
def test_redis_clients_are_constructed_only_in_the_helper():
    """A raw ``redis.Redis(...)`` outside redis_client.py fails here (#236)."""
    found = scan(CONSTRUCTORS, redis_only=True)
    assert set(found) == ALLOWED_CONSTRUCTORS, (
        "A Redis client is constructed outside backend/app/core/redis_client.py. "
        "Build it with create_redis_client()/create_async_redis_client(), which "
        "take REDIS_HOST/PORT/PASSWORD/DB/SSL from the settings. Outside the "
        f"helper: {sorted(set(found) - ALLOWED_CONSTRUCTORS)}; missing from the "
        f"helper: {sorted(ALLOWED_CONSTRUCTORS - set(found))}"
    )


@pytest.mark.unit
def test_every_helper_call_site_is_classified_and_driven():
    """A sixth client fails until it is classified in SITES and given a driver."""
    found = scan(HELPERS)
    assert set(found) == SITES, (
        "The Redis client call sites are not the classified set. New (add to "
        f"SITES and DRIVERS): {sorted(set(found) - SITES)}; gone: "
        f"{sorted(SITES - set(found))}"
    )
    assert set(DRIVERS) == SITES
    for site, (_, callee) in found.items():
        assert HELPERS[callee] == DRIVERS[site][1], (site, callee)


@pytest.mark.unit
def test_the_scans_are_not_blind(tmp_path, monkeypatch):
    """A planted constructor of each spelling is found; a test file is not."""
    planted = tmp_path / "backend" / "app" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "import redis\n"
        "def a():\n    redis.Redis(host='h')\n"
        "def b():\n    redis.StrictRedis(host='h')\n"
        "def c():\n    redis.from_url('redis://h')\n"
        "def d():\n    redis.ConnectionPool(host='h')\n"
        "class K:\n    def e(self):\n        Redis()\n"
        "def f():\n    create_redis_client()\n"
        "async def g():\n    create_async_redis_client()\n"
    )
    module = tmp_path / "modules" / "backend" / "app" / "m.py"
    module.parent.mkdir(parents=True)
    module.write_text("import redis\ndef h():\n    redis.asyncio.Redis(host='h')\n")
    # Each spelling that reaches the redis package is found, however imported.
    spellings = tmp_path / "modules" / "backend" / "app" / "spellings.py"
    spellings.write_text(
        "from redis import ConnectionPool\n"
        "from redis.asyncio import ConnectionPool as AsyncPool\n"
        "from redis import asyncio as aioredis\n"
        "import redis as r\n"
        "import redis.asyncio\n"
        "def p():\n    ConnectionPool(host='h')\n"
        "def q():\n    AsyncPool(host='h')\n"
        "def s():\n    aioredis.ConnectionPool(host='h')\n"
        "def t():\n    r.Redis(host='h')\n"
        "def u():\n    redis.asyncio.ConnectionPool(host='h')\n"
        "def v(factory):\n    factory().from_url('redis://h')\n"
    )
    # A same-named constructor of another package is not a Redis client.
    other = tmp_path / "modules" / "backend" / "app" / "other.py"
    other.write_text(
        "import httpcore\n"
        "from httpcore import ConnectionPool\n"
        "def w():\n    httpcore.ConnectionPool(retries=0)\n"
        "def x():\n    ConnectionPool(retries=0)\n"
    )
    ignored = tmp_path / "backend" / "tests" / "test_x.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("import redis\nredis.Redis()\ncreate_redis_client()\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    assert set(scan(CONSTRUCTORS, redis_only=True)) == {
        ("backend/app/planted.py", "a"),
        ("backend/app/planted.py", "b"),
        ("backend/app/planted.py", "c"),
        ("backend/app/planted.py", "d"),
        ("backend/app/planted.py", "K.e"),
        ("modules/backend/app/m.py", "h"),
        ("modules/backend/app/spellings.py", "p"),
        ("modules/backend/app/spellings.py", "q"),
        ("modules/backend/app/spellings.py", "s"),
        ("modules/backend/app/spellings.py", "t"),
        ("modules/backend/app/spellings.py", "u"),
        ("modules/backend/app/spellings.py", "v"),
    }
    assert set(scan(HELPERS)) == {
        ("backend/app/planted.py", "f"),
        ("backend/app/planted.py", "g"),
    }


# ---------------------------------------------------------------------------
# The drive
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("site", sorted(SITES), ids=lambda s: f"{s[0]}:{s[1]}")
def test_every_client_connects_with_every_redis_setting(site, monkeypatch):
    """Host, port, password, db and ssl all reach the client, at every site."""
    line, _ = scan(HELPERS)[site]
    _configure(monkeypatch, CONFIGURED)
    sync, asyncio_ = _record(monkeypatch)
    driver, flavour = DRIVERS[site]
    driver()
    built, other = (sync, asyncio_) if flavour == "sync" else (asyncio_, sync)

    assert other.calls == [], f"{site} built the wrong client class: {other.calls}"
    assert len(built.calls) == 1, built.calls
    ((constructor, caller, kwargs),) = built.calls
    assert constructor[0] == HELPER_PATH, constructor
    assert caller == (site[0], line), (
        f"driving {site} did not reach the helper from the scanned line "
        f"{site[0]}:{line}; it came from {caller}"
    )
    assert {k: kwargs.get(k) for k in EXPECTED} == EXPECTED, (
        f"{site[0]}:{line} does not connect with the configured Redis settings"
    )


@pytest.mark.unit
@pytest.mark.parametrize("site", sorted(SITES), ids=lambda s: f"{s[0]}:{s[1]}")
def test_the_defaults_are_plaintext_and_passwordless(site, monkeypatch):
    """The local and CI redis:7 containers have no TLS and no password."""
    _configure(monkeypatch, {"REDIS_PASSWORD": None, "REDIS_SSL": False})
    sync, asyncio_ = _record(monkeypatch)
    DRIVERS[site][0]()
    ((_, _, kwargs),) = sync.calls + asyncio_.calls
    assert kwargs["ssl"] is False
    assert kwargs["password"] is None


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_helper_reads_the_settings_at_call_time(monkeypatch):
    _configure(monkeypatch, CONFIGURED)
    assert redis_client.redis_connection_kwargs() == EXPECTED
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "")
    assert redis_client.redis_connection_kwargs()["password"] is None


@pytest.mark.unit
def test_the_helper_passes_client_options_through(monkeypatch):
    _configure(monkeypatch, CONFIGURED)
    sync, asyncio_ = _record(monkeypatch)
    redis_client.create_redis_client(socket_timeout=1, decode_responses=True)
    redis_client.create_async_redis_client(decode_responses=True)
    ((_, _, sync_kwargs),) = sync.calls
    ((_, _, async_kwargs),) = asyncio_.calls
    assert sync_kwargs == {**EXPECTED, "socket_timeout": 1, "decode_responses": True}
    assert async_kwargs == {**EXPECTED, "decode_responses": True}


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(redis_client.CONNECTION_KEYS))
@pytest.mark.parametrize(
    "factory",
    [redis_client.create_redis_client, redis_client.create_async_redis_client],
    ids=["sync", "asyncio"],
)
def test_the_helper_refuses_a_connection_parameter(factory, key, monkeypatch):
    """A caller cannot override the settings, so no site can drift again."""
    sync, asyncio_ = _record(monkeypatch)
    with pytest.raises(ValueError, match=key):
        factory(**{key: "x"})
    assert sync.calls == asyncio_.calls == []


@pytest.mark.unit
def test_redis_ssl_defaults_off_and_reads_the_environment(monkeypatch):
    from backend.app.core.config import Settings

    monkeypatch.delenv("REDIS_SSL", raising=False)
    assert Settings.model_fields["REDIS_SSL"].default is False
    monkeypatch.setenv("REDIS_SSL", "true")
    assert Settings().REDIS_SSL is True


# ---------------------------------------------------------------------------
# One connect attempt (#810)
# ---------------------------------------------------------------------------


def _ping_health() -> None:
    from backend.app.core.health import check_redis

    assert check_redis()["status"] == "unhealthy"


def _read_results_cache() -> None:
    from backend.app.api.v1.endpoints.results import _get_cache_service

    assert _get_cache_service().enabled is False


def _invalidate_results_cache() -> None:
    from backend.app.api.v1.endpoints.results import invalidate_results_cache

    result = invalidate_results_cache(experiment_id=uuid4(), db=None, current_user=None)
    assert result["status"] == "ok"


#: Sites whose client makes one connect attempt, with a driver that runs the
#: real client. A new site fails ``test_every_site_has_a_retry_decision``
#: until it is added here or to ``RETRY_DECIDED_ELSEWHERE``.
class _Attempts(list):
    """Each connect attempt's port; ``timeouts`` holds its (connect, socket) timeouts."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[tuple[object, object]] = []


SINGLE_ATTEMPT: dict[tuple[str, str], Callable[[], None]] = {
    ("backend/app/core/health.py", "check_redis"): _ping_health,
    ("backend/app/api/v1/endpoints/results.py", "_get_cache_service"): (
        _read_results_cache
    ),
    ("backend/app/api/v1/endpoints/results.py", "invalidate_results_cache"): (
        _invalidate_results_cache
    ),
}

#: Sites whose retry policy is decided and pinned somewhere else, and where.
RETRY_DECIDED_ELSEWHERE = {
    ("backend/app/middleware/rate_limiter.py", "RedisRateLimiter._get_redis"): (
        "one attempt: TestNoClientRetries in "
        "backend/tests/unit/middleware/test_rate_limiter.py (#790)"
    ),
    ("backend/app/api/deps.py", "get_redis_pool"): (
        "unchanged (#810): an asyncio client, so a slow connect delays its "
        "own request without holding the event loop, and it is reached only "
        "through get_cache_control when CACHE_ENABLED is on (default off)"
    ),
    ("backend/app/api/v1/endpoints/results.py", "_async_cache_client"): (
        "one attempt and 1 s timeouts, on the event loop (#1934): an asyncio "
        "client, which the connects fixture here does not count; "
        "test_the_results_cache_is_cleared_on_the_event_loop_at_most_a_second "
        "in backend/tests/unit/services/test_metric_window_writes.py counts it"
    ),
}


@pytest.fixture
def connects(monkeypatch) -> list:
    """Redis refused at 127.0.0.1:1; the port of every connect attempt."""
    from redis.connection import Connection

    _configure(
        monkeypatch,
        {
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": 1,
            "REDIS_PASSWORD": None,
            "REDIS_DB": 0,
            "REDIS_SSL": False,
        },
    )
    attempts = _Attempts()
    real_connect = Connection._connect

    def counting_connect(conn):
        attempts.append(conn.port)
        attempts.timeouts.append((conn.socket_connect_timeout, conn.socket_timeout))
        return real_connect(conn)

    monkeypatch.setattr(Connection, "_connect", counting_connect)
    return attempts


@pytest.mark.unit
def test_every_site_has_a_retry_decision():
    assert not set(SINGLE_ATTEMPT) & set(RETRY_DECIDED_ELSEWHERE)
    assert set(SINGLE_ATTEMPT) | set(RETRY_DECIDED_ELSEWHERE) == SITES, (
        "Every Redis client call site needs a retry decision. Undecided: "
        f"{sorted(SITES - set(SINGLE_ATTEMPT) - set(RETRY_DECIDED_ELSEWHERE))}"
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("site", sorted(SINGLE_ATTEMPT), ids=lambda s: f"{s[0]}:{s[1]}")
def test_the_client_tries_to_connect_once(site, connects):
    """No timing: a refused connect fails at once, so the count is the signal."""
    SINGLE_ATTEMPT[site]()
    assert connects == [1], (
        f"{site[0]}:{site[1]} made {len(connects)} connect attempts against a "
        "refused Redis, not 1; build its client with retry=Retry(NoBackoff(), 0)"
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("site", sorted(SINGLE_ATTEMPT), ids=lambda s: f"{s[0]}:{s[1]}")
def test_the_client_gives_up_on_an_unreachable_redis_after_a_second(site, connects):
    """A refused port fails at once, so the count above cannot see the timeouts.

    They are what bounds an unreachable Redis (a dropped packet, not a refusal):
    without them a connect waits for the operating system's own timeout.
    """
    SINGLE_ATTEMPT[site]()
    assert connects.timeouts == [(1, 1)], (
        f"{site[0]}:{site[1]} connected with (connect, socket) timeouts "
        f"{connects.timeouts}, not [(1, 1)]; an unreachable Redis would hold it"
    )

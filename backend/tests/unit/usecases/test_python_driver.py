"""The Python SDK's driver, with no database: this checkout's ``sdk/python``
(not an installed copy), a round trip in server and in local mode through the
recording proxy to a fake upstream, and the refusals it answers rather than
crashing on.

The fake upstream (``harness.Upstream``) serves one flag, the ruleset with its
ETag, and the evaluation counts. The SDK reaches it only through the proxy,
whose records say what went over the wire: the positive control that the SDK
is behind the proxy at all.

The ``on_error`` tally (``status()["errors"]``) is checked against the proxy:
a driver started while the proxy faults the ruleset counts one refresh per
faulted request. The JS driver's tally is checked the same way beside the
cases (``tests/usecases/test_js_driver.py``).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from backend.tests.unit.usecases.harness import (
    FLAG_KEY,
    PYTHON_DRIVER,
    REPO_ROOT,
    SDK_ROOT,
    Upstream,
    closed_port,
    gates,
    proxy,
    runner,
)

pytestmark = pytest.mark.unit

#: A stand-in with no entropy, so no secret scanner mistakes it for one.
KEY = "K" * 32
EVALUATE = f"/api/v1/feature-flags/evaluate/{FLAG_KEY}"
EVALUATIONS = "/api/v1/tracking/evaluations"
#: ``status()["errors"]`` before anything failed.
NO_ERRORS = dict.fromkeys(gates.ERROR_KEYS, 0)


def _driver_module() -> ModuleType:
    """The driver script as a module (it does nothing at import: the SDK is
    loaded by ``main``)."""
    spec = importlib.util.spec_from_file_location(
        "usecases_python_driver", PYTHON_DRIVER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Runs the driver after another ``controlvar`` was imported first: argv is
#: the decoy's directory, then the driver.
PRELOADED = (
    "import runpy, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import controlvar\n"
    "sys.argv = [sys.argv[2]]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _sdk_version() -> str:
    text = (SDK_ROOT / "controlvar" / "version.py").read_text(encoding="utf-8")
    (version,) = re.findall(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    return version


def _decoy(tmp_path: Path) -> Path:
    """A ``controlvar`` package that is not the checkout's, as an installed
    copy would be."""
    package = tmp_path / "controlvar"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.0.0"\n', encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _not_in_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream().start()
    yield server
    server.stop()


@pytest.fixture
def relay(upstream: Upstream) -> Iterator[proxy.RecordingProxy]:
    with proxy.RecordingProxy(upstream.origin) as recording:
        yield recording


@pytest.fixture
def driver() -> Iterator[runner.Runner]:
    session = runner.Runner([sys.executable, "-B", str(PYTHON_DRIVER)], timeout=10.0)
    session.start()
    yield session
    session.stop()


# -- identity ------------------------------------------------------------------------
def test_hello_names_this_checkout_s_sdk(driver):
    answer = driver.call("hello")
    assert answer == {
        "ok": True,
        "sdk": "python",
        "version": _sdk_version(),
        "module": "sdk/python/controlvar/__init__.py",
        "capabilities": ["local-eval", "remote-eval", "error-tally"],
    }


def test_an_installed_copy_does_not_replace_the_checkout_s_sdk(tmp_path):
    decoy = _decoy(tmp_path)
    env = {"PATH": "", "PYTHONPATH": str(decoy)}
    with runner.Runner(
        [sys.executable, "-B", str(PYTHON_DRIVER)], env=env, timeout=10.0
    ) as session:
        answer = session.call("hello")
    assert answer["module"] == "sdk/python/controlvar/__init__.py"
    assert answer["version"] == _sdk_version()


def test_the_driver_refuses_to_start_when_controlvar_is_another_copy(tmp_path):
    decoy = _decoy(tmp_path)
    argv = [sys.executable, "-B", "-c", PRELOADED, str(decoy), str(PYTHON_DRIVER)]
    with runner.Runner(argv, timeout=10.0) as session:
        with pytest.raises(runner.DriverError, match=r"before answering hello"):
            session.call("hello")
    assert session.returncode == 3


# -- round trips through the proxy ------------------------------------------------------
def test_server_mode_round_trip_behind_the_proxy(driver, relay, upstream):
    init = driver.call(
        "init", api_url=relay.origin, api_key=KEY, mode="server", cache_ttl_seconds=10
    )
    assert init == {"ok": True, "mode": "server"}
    first = driver.call("evaluate", flag=FLAG_KEY, user="u-1", default=False)
    assert {k: first[k] for k in ("ok", "enabled", "value", "reason", "source")} == {
        "ok": True,
        "enabled": True,
        "value": True,
        "reason": "rollout",
        "source": "server",
    }
    assert first["error"] is None and first["ms"] >= 0
    # Within its cache lifetime the SDK sends nothing for the same user.
    again = driver.call("evaluate", flag=FLAG_KEY, user="u-1", default=False)
    assert again["enabled"] is True
    status = driver.call("status")
    assert (status["evaluation"], status["ready"]) == ("server", True)
    assert driver.call("close") == {"ok": True}
    driver.stop()
    assert driver.returncode == 0
    assert [(r.method, r.path, r.status) for r in relay.records()] == [
        ("GET", EVALUATE, 200)
    ]
    (received,) = upstream.requests_to(EVALUATE)
    assert received.target == f"{EVALUATE}?user_id=u-1"
    assert received.headers["x-api-key"] == KEY


def test_local_mode_round_trip_behind_the_proxy(driver, relay):
    init = driver.call(
        "init", api_url=relay.origin, api_key=KEY, mode="local", refresh_seconds=5
    )
    assert init == {"ok": True, "mode": "local", "ready": True}
    answer = driver.call("evaluate", flag=FLAG_KEY, user="u-1", default=False)
    assert (answer["enabled"], answer["reason"], answer["source"], answer["error"]) == (
        True,
        "rollout",
        "local",
        None,
    )
    status = driver.call("status")
    assert (status["evaluation"], status["ready"], status["ruleset_version"]) == (
        "local",
        True,
        "v1",
    )
    assert status["last_error"] is None and status["last_refresh_at"]
    assert status["errors"] == NO_ERRORS
    assert driver.call("close") == {"ok": True}
    records = relay.records()
    ruleset = [r for r in records if r.path == proxy.RULESET_PATH]
    assert [(r.status, r.etag, r.if_none_match) for r in ruleset[:1]] == [
        (200, '"v1"', False)
    ]
    # Answered from the ruleset: the evaluate route was never called.
    assert [r for r in records if r.path == EVALUATE] == []
    # close sent the one local evaluation it counted.
    assert [(r.method, r.status) for r in records if r.path == EVALUATIONS] == [
        ("POST", 202)
    ]


# -- the error tally and the cache lifetime -----------------------------------------------
def test_on_error_counts_one_refresh_per_ruleset_request_the_proxy_faulted(
    driver, relay, upstream
):
    """Started while the proxy answers the ruleset 503: the first refresh
    fails, ``init`` answers ``ready`` false, and ``errors`` counts that one
    refresh, as many as the proxy faulted (the next attempt is a backoff step,
    5 s, away). The upstream never saw the faulted request."""
    relay.inject(proxy.Fault("status", paths=(proxy.RULESET_PATH,)))
    init = driver.call(
        "init",
        api_url=relay.origin,
        api_key=KEY,
        mode="local",
        refresh_seconds=5,
        timeout_ms=2000,
    )
    assert init == {"ok": True, "mode": "local", "ready": False}
    status = driver.call("status")
    records = relay.records()
    assert [(r.path, r.status, r.fault) for r in records] == [
        (proxy.RULESET_PATH, 503, "status:503")
    ]
    assert (
        gates.error_tally_findings(status["errors"], records, "the python driver") == []
    )
    assert status["errors"] == {**NO_ERRORS, "refresh": 1}
    assert upstream.requests_to(proxy.RULESET_PATH) == []
    relay.clear()
    assert driver.call("close") == {"ok": True}


def test_cache_ttl_zero_reaches_the_server_on_every_call(driver, relay, upstream):
    init = driver.call(
        "init", api_url=relay.origin, api_key=KEY, mode="server", cache_ttl_seconds=0
    )
    assert init == {"ok": True, "mode": "server"}
    answers = [
        driver.call("evaluate", flag=FLAG_KEY, user="u-1", default=False)
        for _ in range(2)
    ]
    assert [(a["enabled"], a["source"], a["error"]) for a in answers] == [
        (True, "server", None)
    ] * 2
    assert driver.call("close") == {"ok": True}
    # 0 caches nothing: the same user's second call asked the API again.
    assert len(upstream.requests_to(EVALUATE)) == 2


def test_the_tally_counts_per_operation_and_keeps_no_error():
    """Any operation other than refresh, flush and evaluate counts as other;
    only counts are kept; the keys are the gates'."""
    module = _driver_module()
    tally = module.ErrorTally()
    secret = RuntimeError("GET /api/v1/feature-flags/evaluate/x?user_id=someone")
    for operation in ("refresh", "refresh", "flush", "evaluate", "value", "exposure"):
        tally(secret, operation)
    assert tally.counts() == {"refresh": 2, "flush": 1, "evaluate": 1, "other": 2}
    assert tuple(tally.counts()) == gates.ERROR_KEYS
    assert "someone" not in repr(vars(tally))


# -- refusals and errors, read off the raw stdout --------------------------------------
def test_refusals_are_answered_and_an_sdk_error_is_reported_not_swallowed():
    port = closed_port()
    commands = [
        {"cmd": "nope"},
        {"cmd": "evaluate", "flag": FLAG_KEY, "user": "u-1", "default": False},
        {"cmd": "status"},
        {"cmd": "init", "api_url": f"http://127.0.0.1:{port}", "mode": "server"},
        {
            "cmd": "init",
            "api_url": f"http://127.0.0.1:{port}",
            "api_key": KEY,
            "mode": "server",
        },
        {
            "cmd": "init",
            "api_url": f"http://127.0.0.1:{port}",
            "api_key": KEY,
            "mode": "server",
        },
        {"cmd": "evaluate", "flag": FLAG_KEY, "user": "u-1", "default": False},
        {"cmd": "close"},
    ]
    lines = [json.dumps(c) for c in commands]
    lines.insert(1, "this is not json")
    result = subprocess.run(
        [sys.executable, "-B", str(PYTHON_DRIVER)],
        input=("\n".join(lines) + "\n").encode(),
        capture_output=True,
        env={"PATH": ""},
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0
    stdout = result.stdout.decode()
    # The key was sent in init and is never written back.
    assert KEY not in stdout and KEY not in result.stderr.decode()
    answers = [json.loads(line) for line in stdout.splitlines()]
    assert answers[:6] == [
        {"ok": False, "error": "unknown_command"},
        {"ok": False, "error": "bad_request"},
        {"ok": False, "error": "not_initialised"},
        {"ok": False, "error": "not_initialised"},
        {"ok": False, "error": "bad_request"},
        {"ok": True, "mode": "server"},
    ]
    assert answers[6] == {"ok": False, "error": "already_initialised"}
    failed = answers[7]
    assert {k: failed[k] for k in ("ok", "enabled", "value", "reason", "source")} == {
        "ok": True,
        "enabled": False,
        "value": False,
        "reason": None,
        "source": "default",
    }
    assert failed["error"]["status"] is None
    assert failed["error"]["type"].endswith("Error")
    assert answers[8:] == [{"ok": True}]

"""The JavaScript SDK's driver, with no server: this checkout's built
``sdk/js`` (not an installed copy), the API key never written to its raw
stdout or stderr, a round trip in server and in local mode through the
recording proxy to the harness's fake upstream (``usecases/fake_upstream.py``,
the one the Python driver's gates use), the ``onError`` tally counting one
refresh per request the proxy faulted, and ``cache_ttl_seconds`` 0 reaching
the SDK as "cache nothing".

These need Node, so they run here, beside the cases, in ``_platform.yml``'s
``usecases-flags`` arm (which builds ``sdk/js`` first), not in the unit job,
which has no Node. No Node, a Node below 18 or no ``sdk/js/dist`` fails them
(``conftest.py``'s ``node``); nothing here skips. The Node floor and the
identity check themselves are checked without Node in the unit job
(``backend/tests/unit/usecases/test_sdks.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from usecases import gates, proxy, runner, sdks
from usecases.fake_upstream import FLAG_KEY, Upstream, closed_port

#: A stand-in with no entropy, so no secret scanner mistakes it for one.
KEY = "K" * 32
EVALUATE = f"/api/v1/feature-flags/evaluate/{FLAG_KEY}"
EVALUATIONS = "/api/v1/tracking/evaluations"
#: A module that is not the checkout's SDK, as an installed copy would be.
DECOY = "exports.ExperimentationClient = class {};\n"
#: ``status()["errors"]`` before anything failed.
NO_ERRORS = dict.fromkeys(gates.ERROR_KEYS, 0)


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
def driver(node: sdks.Node) -> Iterator[runner.Runner]:
    with sdks.driver("js", node=node, timeout=10.0) as (session, _):
        yield session


def _sdk_version() -> str:
    package = sdks.REPO_ROOT / "sdk" / "js" / "package.json"
    return json.loads(package.read_text(encoding="utf-8"))["version"]


# -- identity ------------------------------------------------------------------------
def test_hello_names_this_checkout_s_built_sdk(node):
    with runner.Runner(sdks.driver_argv("js", node), timeout=10.0) as session:
        answer = session.call("hello")
    assert answer == {
        "ok": True,
        "sdk": "js",
        "version": _sdk_version(),
        "module": "sdk/js/dist/index.js",
        "capabilities": ["local-eval", "remote-eval", "error-tally"],
    }
    assert sdks.identity_problem("js", answer) is None


def test_the_driver_refuses_an_sdk_outside_sdk_js_dist(node, tmp_path: Path):
    """A tree laid out like this one whose ``sdk/js/dist/index.js`` is a link
    to a module elsewhere: the driver loads by path, Node resolves the link,
    and the driver exits 3 before answering anything."""
    drivers = tmp_path / "tests" / "usecases" / "drivers"
    drivers.mkdir(parents=True)
    shutil.copy2(sdks.JS_DRIVER, drivers / sdks.JS_DRIVER.name)
    package = tmp_path / "sdk" / "js"
    (package / "dist").mkdir(parents=True)
    shutil.copy2(
        sdks.REPO_ROOT / "sdk" / "js" / "package.json", package / "package.json"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "index.js").write_text(DECOY, encoding="utf-8")
    (package / "dist" / "index.js").symlink_to(elsewhere / "index.js")
    argv = [node.path, str(drivers / sdks.JS_DRIVER.name)]
    with runner.Runner(argv, timeout=10.0) as session:
        with pytest.raises(runner.DriverError, match=r"before answering hello"):
            session.call("hello")
    assert session.returncode == sdks.NOT_THE_CHECKOUT


# -- the key never reaches the raw output ---------------------------------------------
def test_refusals_are_answered_and_the_key_never_reaches_raw_stdout_or_stderr(node):
    port = closed_port()
    origin = f"http://127.0.0.1:{port}"
    commands = [
        {"cmd": "nope"},
        {"cmd": "evaluate", "flag": FLAG_KEY, "user": "u-1", "default": False},
        {"cmd": "status"},
        {"cmd": "init", "api_url": origin, "mode": "server"},
        {"cmd": "init", "api_url": origin, "api_key": KEY, "mode": "server"},
        {"cmd": "init", "api_url": origin, "api_key": KEY, "mode": "server"},
        {"cmd": "evaluate", "flag": FLAG_KEY, "user": "u-1", "default": False},
        {"cmd": "status"},
        {"cmd": "close"},
    ]
    lines = [json.dumps(c) for c in commands]
    lines.insert(1, "this is not json")
    result = subprocess.run(
        sdks.driver_argv("js", node),
        input=("\n".join(lines) + "\n").encode(),
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        cwd=sdks.REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0
    stdout = result.stdout.decode()
    # The key was sent in init and is never written back, not even by the SDK.
    assert KEY not in stdout and KEY not in result.stderr.decode()
    answers = [json.loads(line) for line in stdout.splitlines()]
    assert answers[:7] == [
        {"ok": False, "error": "unknown_command"},
        {"ok": False, "error": "bad_request"},
        {"ok": False, "error": "not_initialised"},
        {"ok": False, "error": "not_initialised"},
        {"ok": False, "error": "bad_request"},
        {"ok": True, "mode": "server"},
        {"ok": False, "error": "already_initialised"},
    ]
    failed = answers[7]
    assert {k: failed[k] for k in ("ok", "enabled", "value", "reason", "source")} == {
        "ok": True,
        "enabled": False,
        "value": False,
        "reason": None,
        "source": "default",
    }
    assert failed["error"] == {"type": "ExperimentationError", "status": None}
    assert answers[8]["ok"] is True and answers[8]["evaluation"] == "server"
    assert answers[9:] == [{"ok": True}]


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
    assert gates.error_tally_findings(status["errors"], records, "the js driver") == []
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

"""``evaluation="local"`` through the public client: the vectors end to end, the refresh failure
table, the evaluation-count flush, fork safety and the API surface. The HTTP layer is a fake;
nothing here needs a server. Runs on Python 3.9.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

import pytest

from controlvar import ExperimentationClient, ExperimentationError, ReadyResult
from controlvar import local as local_module
from controlvar.evaluator import is_well_formed
from controlvar.local import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    EVALUATIONS_PATH,
    FLUSH_INTERVAL_SECONDS,
    MAX_BACKOFF_SECONDS,
    MIN_SERVER_RELEASE,
    REFUSED_RETRY_SECONDS,
    RULESET_PATH,
    LocalEvaluation,
)
from controlvar.testing import make_response
from controlvar.transport import Response

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "tests" / "sdk-contract" / "ruleset-vectors.json").read_text("utf-8")
)

RULESET = {
    "schema": 1,
    "version": "v1",
    "bucketing": "md5-mod100-v1",
    "flags": [
        {"key": "off", "active": False},
        {"key": "all-on", "active": True, "evaluation": "local", "rollout_percentage": 100, "rules": [], "default_rule": None},
        {
            "key": "us-only",
            "active": True,
            "evaluation": "local",
            "rollout_percentage": 0,
            "rules": [
                {
                    "id": "us",
                    "rollout_percentage": 100,
                    "match": {
                        "op": "and",
                        "conditions": [{"attribute": "user.country", "operator": "eq", "value": "US"}],
                        "groups": [],
                    },
                }
            ],
            "default_rule": None,
        },
    ],
}
RULESET_WITH_REMOTE = dict(
    RULESET, version="v2", flags=RULESET["flags"] + [{"key": "semver", "active": True, "evaluation": "remote"}]
)

US = {"country": "US"}
FR = {"country": "FR"}


def ok(body: Any = RULESET, etag: str = '"v1"') -> Response:
    return make_response(200, body, headers={"ETag": etag})


class FakeServer:
    """Ruleset responses are consumed in order (the last one repeats); everything is recorded."""

    def __init__(self, *rulesets: Any) -> None:
        self.rulesets: List[Any] = list(rulesets)
        self.evaluate: Callable[[Dict[str, str]], Response] = lambda query: make_response(
            200, {"key": "x", "enabled": True, "config": None, "reason": "rollout"}
        )
        self.user_flags: Callable[[], Response] = lambda: make_response(200, {})
        self.evaluations_status = 201
        self.requests: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def send(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        with self.lock:
            self.requests.append(
                {
                    "method": method,
                    "path": parts.path,
                    "query": dict(parse_qsl(parts.query)),
                    "headers": dict(headers),
                    "body": None if body is None else json.loads(body.decode("utf-8")),
                }
            )
            if parts.path == RULESET_PATH:
                step = self.rulesets.pop(0) if len(self.rulesets) > 1 else self.rulesets[0]
                if isinstance(step, Exception):
                    raise step
                return step
        if parts.path == EVALUATIONS_PATH:
            return make_response(self.evaluations_status, {"accepted": 0, "errors": []})
        if parts.path.startswith("/api/v1/feature-flags/evaluate/"):
            return self.evaluate(dict(parse_qsl(parts.query)))
        if parts.path.startswith("/api/v1/feature-flags/user/"):
            return self.user_flags()
        return make_response(200, {"success_count": 1, "failure_count": 0, "errors": []})

    def calls(self, path_prefix: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"].startswith(path_prefix)]


@pytest.fixture
def no_thread(monkeypatch):
    """Drive refreshes by hand: the client's background thread is not started."""
    monkeypatch.setattr(LocalEvaluation, "_start_thread", lambda self: None)


def local_client(server: FakeServer, **kwargs: Any):
    errors: List[Any] = []
    client = ExperimentationClient(
        "http://test",
        "scoped-key",
        transport=server,
        evaluation="local",
        on_error=lambda error, operation: errors.append((operation, error)),
        **kwargs,
    )
    return client, errors


def loaded(server: FakeServer, **kwargs: Any):
    client, errors = local_client(server, **kwargs)
    client._local.refresh_once()
    return client, errors


# ----------------------------------------------------------------------- the vectors end to end


def test_the_vectors_through_get_feature_flag(no_thread):
    server = FakeServer(ok(VECTORS["ruleset"], '"%s"' % VECTORS["ruleset"]["version"]))
    client, _ = loaded(server)
    assert client.ready(0) == ReadyResult(True, VECTORS["ruleset"]["version"])

    wrong, unsendable = [], []
    local_answers = 0
    for case in VECTORS["cases"]:
        server.evaluate = lambda query, case=case: make_response(
            200, {"key": case["flag"], "enabled": case["expected"]["enabled"], "config": None, "reason": case["expected"]["reason"]}
        )
        before = len(server.calls("/api/v1/feature-flags/evaluate/"))
        try:
            result = client.get_feature_flag(case["flag"], case["user_id"], case["context"])
        except UnicodeEncodeError:
            # A lone surrogate in the user id cannot be put in a URL: local mode deferred, and
            # the server path fails before sending, exactly as it does in server mode.
            unsendable.append(case)
            continue
        remote = len(server.calls("/api/v1/feature-flags/evaluate/")) - before
        if (result.enabled, result.reason) != (case["expected"]["enabled"], case["expected"]["reason"]):
            wrong.append((case["id"], result))
        if remote != (0 if case["must_local"] else 1) or result.source != ("server" if remote else "local"):
            wrong.append((case["id"], "must_local", case["must_local"], remote, result.source))
        local_answers += result.source == "local"
        client.clear_cache()
    assert wrong == []
    assert unsendable and all(not c["must_local"] and not is_well_formed(c["user_id"]) for c in unsendable)
    assert local_answers == 1294


# ----------------------------------------------------------------------- answers


def test_after_ready_evaluations_make_no_request_and_follow_the_attributes(no_thread):
    server = FakeServer(ok())
    client, _ = loaded(server)
    before = len(server.requests)
    for i in range(1000):
        assert client.is_feature_enabled("all-on", f"u{i}")
    us = client.get_feature_flag("us-only", "user-1", US)
    assert (us.enabled, us.reason, us.source, us.config) == (True, "targeting_rule", "local", None)
    fr = client.get_feature_flag("us-only", "user-1", FR)  # same user, no clear_cache()
    assert (fr.enabled, fr.reason, fr.source) == (False, "rollout", "local")
    assert len(server.requests) == before


def test_an_inactive_flag_is_disabled_with_reason_inactive(no_thread):
    client, _ = loaded(FakeServer(ok()))
    result = client.get_feature_flag("off", "user-1")
    assert (result.enabled, result.reason, result.source) == (False, "inactive", "local")


def test_an_unknown_key_and_a_remote_flag_go_to_the_server(no_thread):
    server = FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'))
    client, _ = loaded(server)
    assert client.get_feature_flag("semver", "user-1", US).source == "server"
    assert client.get_feature_flag("brand-new", "user-1", US).source == "server"
    calls = server.calls("/api/v1/feature-flags/evaluate/")
    assert [c["path"] for c in calls] == ["/api/v1/feature-flags/evaluate/semver", "/api/v1/feature-flags/evaluate/brand-new"]
    assert calls[0]["query"]["context"] == '{"country":"US"}'
    assert client.status().server_evaluated_flags == ["semver"]


def test_a_local_answer_is_recorded_for_the_keyless_track_fan_out(no_thread):
    server = FakeServer(ok())
    client, _ = loaded(server)
    client.is_feature_enabled("us-only", "user-1", US)
    assert client.track("user-1", "page_view")
    batch = server.calls("/api/v1/tracking/batch")
    assert batch[0]["body"]["events"] == [
        {"event_type": "page_view", "event_name": "page_view", "user_id": "user-1", "feature_flag_key": "us-only"}
    ]


def test_get_all_flags_locally_with_inactive_omitted(no_thread):
    server = FakeServer(ok())
    client, _ = loaded(server)
    assert client.get_all_flags("user-1", US) == {"all-on": True, "us-only": True}
    assert server.calls("/api/v1/feature-flags/user/") == []


def test_get_all_flags_one_remote_flag_sends_the_whole_call(no_thread):
    server = FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'))
    server.user_flags = lambda: make_response(200, {"all-on": True, "us-only": False, "semver": True})
    client, _ = loaded(server)
    assert client.get_all_flags("user-1", US) == {"all-on": True, "us-only": False, "semver": True}
    assert len(server.calls("/api/v1/feature-flags/user/")) == 1


def test_before_the_first_ruleset_evaluations_go_to_the_server(no_thread):
    server = FakeServer(ok())
    client, _ = local_client(server)
    assert client.get_feature_flag("all-on", "user-1").source == "server"
    assert client.status().ready is False
    client._local.refresh_once()
    client.clear_cache()
    assert client.get_feature_flag("all-on", "user-1").source == "local"


def test_experiments_are_assigned_by_the_server_in_local_mode(no_thread):
    server = FakeServer(ok())
    client, _ = loaded(server)
    answer_ruleset_and_flags = server.send

    def send(method, url, headers, body, timeout):
        if urlsplit(url).path != "/api/v1/tracking/assign":
            return answer_ruleset_and_flags(method, url, headers, body, timeout)
        server.requests.append({"method": method, "path": urlsplit(url).path, "body": json.loads(body)})
        return make_response(
            200,
            {
                "experiment_key": "checkout_flow",
                "user_id": "user-1",
                "variant_id": "v-2",
                "variant_name": "treatment",
                "is_control": False,
                "configuration": None,
            },
        )

    server.send = send
    assert client.get_assignment("checkout_flow", "user-1", US).variant_name == "treatment"
    assert [(c["method"], c["body"]["experiment_key"]) for c in server.calls("/api/v1/tracking/assign")] == [
        ("POST", "checkout_flow")
    ]


def test_server_mode_is_unchanged():
    server = FakeServer(ok())
    client = ExperimentationClient("http://test", "k", transport=server)
    assert client.ready() == ReadyResult(True)
    result = client.get_feature_flag("x", "user-1", US)
    assert result.source is None and result.reason == "rollout"
    assert server.calls(RULESET_PATH) == []
    assert client.status().evaluation == "server"
    with client:
        pass


# ----------------------------------------------------------------------- the refresh failure table


def test_5xx_after_a_load_keeps_serving_and_reports_each_failure(no_thread):
    server = FakeServer(ok(), make_response(503), make_response(503))
    client, errors = loaded(server)
    loaded_at = client.status().last_refresh_at
    assert client._local.refresh_once() == DEFAULT_REFRESH_INTERVAL_SECONDS
    assert client._local.refresh_once() == 2 * DEFAULT_REFRESH_INTERVAL_SECONDS
    assert [(op, err.status) for op, err in errors] == [("refresh", 503), ("refresh", 503)]
    status = client.status()
    assert (status.ready, status.ruleset_version, status.last_refresh_at) == (True, "v1", loaded_at)
    assert status.last_error.status == 503
    assert client.get_feature_flag("us-only", "user-1", US).source == "local"


def test_a_network_failure_before_the_first_load(no_thread):
    from controlvar import TransportError

    server = FakeServer(TransportError("connection refused"))
    client, errors = loaded(server)
    result = client.ready(0)
    assert not result and result.error is not None
    assert len(errors) == 1
    assert client.get_feature_flag("all-on", "user-1").source == "server"


def test_backs_off_exponentially_to_five_minutes(no_thread):
    client, _ = local_client(FakeServer(make_response(500)))
    delays = [client._local.refresh_once() for _ in range(6)]
    assert delays == [30.0, 60.0, 120.0, 240.0, MAX_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_after_a_load_discards_the_ruleset(no_thread, status):
    server = FakeServer(ok(), make_response(status, {"detail": "nope"}))
    client, errors = loaded(server)
    assert client.get_feature_flag("all-on", "user-1").source == "local"

    assert client._local.refresh_once() == REFUSED_RETRY_SECONDS  # the key is revoked
    client.clear_cache()
    assert client.get_feature_flag("all-on", "user-1").source == "server"
    status_now = client.status()
    assert (status_now.ready, status_now.ruleset_version) == (False, None)
    assert len(errors) == 1
    assert f"refused ({status})" in errors[0][1].message and "nope" in errors[0][1].message

    # Retried every 10 minutes, reported once, and the retry sends no stale ETag.
    assert client._local.refresh_once() == REFUSED_RETRY_SECONDS
    assert "If-None-Match" not in server.calls(RULESET_PATH)[-1]["headers"]
    assert len(errors) == 1


def test_a_scope_granted_later_is_picked_up(no_thread):
    server = FakeServer(make_response(403, {"detail": "no scope"}), ok())
    client, _ = loaded(server)
    assert not client.ready(0)
    client._local.refresh_once()
    assert client.ready(0) == ReadyResult(True, "v1")


def test_404_names_the_minimum_server_release_and_discards(no_thread):
    client, errors = loaded(FakeServer(make_response(404)))
    assert not client.ready(0)
    assert f"Controlvar {MIN_SERVER_RELEASE} or later" in errors[0][1].message

    later, _ = loaded(FakeServer(ok(), make_response(404)))
    later._local.refresh_once()
    assert later.status().ready is False


def test_429_keeps_the_ruleset_and_waits_for_retry_after(no_thread):
    server = FakeServer(ok(), make_response(429, headers={"Retry-After": "120"}))
    client, _ = loaded(server)
    assert client._local.refresh_once() == 120.0
    assert client.status().ready is True


@pytest.mark.parametrize(
    "response",
    [
        make_response(200, body=b"<html>"),
        make_response(200, {"schema": 1, "bucketing": "md5-mod100-v1", "version": "v9"}),
        make_response(200, dict(RULESET, flags=[{"key": "x", "active": True, "evaluation": "local"}])),
    ],
    ids=["html", "no-flags", "flag-without-rules"],
)
def test_a_malformed_200_keeps_the_last_good_ruleset(no_thread, response):
    client, errors = loaded(FakeServer(ok(), response))
    client._local.refresh_once()
    assert (client.status().ready, client.status().ruleset_version) == (True, "v1")
    assert len(errors) == 1

    fresh, _ = loaded(FakeServer(response))
    assert not fresh.ready(0)
    assert fresh.get_feature_flag("all-on", "user-1").source == "server"


@pytest.mark.parametrize("change", [{"schema": 2}, {"bucketing": "md5-mod100-v2"}], ids=["schema", "bucketing"])
def test_an_unknown_format_discards_and_defers(no_thread, change):
    client, errors = loaded(FakeServer(ok(), ok(dict(RULESET, **change), '"v9"')))
    client._local.refresh_once()
    assert client.status().ready is False
    assert "does not understand" in errors[0][1].message
    assert client.get_feature_flag("all-on", "user-1").source == "server"


def test_polls_send_the_etag_and_a_304_keeps_the_ruleset(no_thread):
    server = FakeServer(ok(), make_response(304))
    client, _ = loaded(server)
    first = client.status().last_refresh_at
    time.sleep(0.01)
    client._local.refresh_once()
    polls = server.calls(RULESET_PATH)
    assert "If-None-Match" not in polls[0]["headers"]
    assert polls[1]["headers"]["If-None-Match"] == '"v1"'
    assert polls[1]["headers"]["X-API-Key"] == "scoped-key"
    assert client.status().ready is True
    assert client.status().last_refresh_at > first


def test_max_stale_seconds(no_thread):
    now = [1000.0]
    server = FakeServer(ok(), make_response(503))
    client, _ = local_client(server, max_stale_seconds=45)
    client._local._clock = lambda: now[0]
    client._local.refresh_once()
    now[0] += 40
    assert client.get_feature_flag("all-on", "user-1").source == "local"
    now[0] += 10
    client.clear_cache()
    assert client.get_feature_flag("all-on", "user-1").source == "server"
    assert client.status().ready is False


# ----------------------------------------------------------------------- the background thread


def run_loop(responses, uniform, polls_wanted):
    """Drive the refresh thread's loop with a fake clock; return the times of each ruleset poll."""
    now = [0.0]
    times: List[float] = []
    queue = list(responses)

    def send(method, path, headers, body):
        if path == RULESET_PATH:
            times.append(now[0])
            if len(times) >= polls_wanted:
                runtime._stop_flag = True
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return make_response(201, {"accepted": 0, "errors": []})

    runtime = LocalEvaluation(
        send=send, report=lambda *a: None, refresh_interval_seconds=30, clock=lambda: now[0], start=False
    )

    class Stop:
        def is_set(self):
            return runtime._stop_flag

        def set(self):
            runtime._stop_flag = True

        def wait(self, timeout):
            now[0] += timeout

    runtime._stop_flag = False
    runtime._stop = Stop()
    original = local_module.random.uniform
    local_module.random.uniform = lambda a, b: uniform(a, b)
    try:
        runtime._run()
    finally:
        local_module.random.uniform = original
    return [round(b - a, 6) for a, b in zip(times, times[1:])]


@pytest.mark.parametrize("pick", [min, max], ids=["-10%", "+10%"])
def test_only_the_regular_interval_is_jittered(pick):
    uniform = lambda a, b: pick(a, b)  # noqa: E731
    # success: the interval, jittered
    assert run_loop([ok(), make_response(304)], uniform, 3) == [round(30 * pick(0.9, 1.1), 6)] * 2
    # 429 with Retry-After 120: exactly 120 s, a floor
    assert run_loop([ok(), make_response(429, headers={"Retry-After": "120"}), make_response(304)], uniform, 3)[1] == 120.0
    # backoff: exact steps, and the 5-minute cap is a ceiling
    assert run_loop([make_response(500)], uniform, 8) == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]
    # refused: exactly 10 minutes
    assert run_loop([make_response(403, {"detail": "no"})], uniform, 3) == [600.0, 600.0]


def test_the_minimum_interval_is_five_seconds(no_thread):
    client, _ = local_client(FakeServer(ok()), refresh_interval_seconds=1)
    assert client._local._interval == 5.0
    assert client._local.refresh_once() == 5.0


def test_the_thread_is_a_daemon_and_close_stops_it():
    server = FakeServer(ok())
    client, _ = local_client(server)
    assert client.ready(5)
    thread = client._local._thread
    assert thread.daemon and thread.is_alive()
    client.close()
    assert not thread.is_alive()
    assert client.get_feature_flag("all-on", "user-1").source == "server"


# ----------------------------------------------------------------------- evaluation counts


def test_close_sends_one_entry_per_flag_and_deferred_evaluations_are_not_counted(no_thread):
    server = FakeServer(ok(RULESET_WITH_REMOTE, '"v2"'))
    client, _ = loaded(server)
    for i in range(7):
        client.is_feature_enabled("us-only", "user-1", US if i < 3 else FR)
    client.is_feature_enabled("off", "user-1")
    client.is_feature_enabled("semver", "user-1")  # remote: the server records it itself
    client.get_all_flags("user-2", US)  # defers (semver is remote): not counted
    client.close()

    posts = server.calls(EVALUATIONS_PATH)
    assert len(posts) == 1 and posts[0]["method"] == "POST"
    assert posts[0]["headers"]["X-API-Key"] == "scoped-key"
    entries = posts[0]["body"]["evaluations"]
    assert [(e["flag_key"], e["count"], e["enabled_count"]) for e in entries] == [("us-only", 7, 3), ("off", 1, 0)]
    assert entries[0]["window_start"] <= entries[0]["window_end"]
    # After close() nothing is answered locally, so nothing is left uncounted.
    assert client.get_feature_flag("all-on", "user-1").source == "server"


def test_get_all_flags_counts_every_flag_it_answered(no_thread):
    server = FakeServer(ok())
    client, _ = loaded(server)
    client.get_all_flags("user-1", US)
    client.close()
    entries = server.calls(EVALUATIONS_PATH)[0]["body"]["evaluations"]
    assert [(e["flag_key"], e["count"]) for e in entries] == [("all-on", 1), ("us-only", 1)]


def test_the_thread_flushes_every_sixty_seconds():
    assert FLUSH_INTERVAL_SECONDS == 60.0


def test_nothing_is_sent_without_counts(no_thread):
    server = FakeServer(ok())
    client, _ = loaded(server)
    client._local.flush()
    assert server.calls(EVALUATIONS_PATH) == []


def test_a_failed_report_goes_to_on_error_as_flush(no_thread):
    server = FakeServer(ok())
    server.evaluations_status = 403
    client, errors = loaded(server)
    client.is_feature_enabled("all-on", "user-1")
    client.close()
    assert [(op, err.status) for op, err in errors] == [("flush", 403)]


# ----------------------------------------------------------------------- fork safety


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_fork_while_the_lock_is_held_gives_the_child_working_state(no_thread):
    """Fork while another thread holds the runtime's lock (as the poller does mid-refresh).

    The child must be able to evaluate (fresh locks), must not report the parent's counts, and
    must start its own refresh thread.
    """
    server = FakeServer(ok())
    client, _ = loaded(server)
    client.is_feature_enabled("all-on", "user-1")  # a count the parent owns
    runtime = client._local
    started: List[int] = []
    runtime._start_thread = lambda: started.append(os.getpid())  # instance attribute, survives fork

    holding = threading.Event()
    release = threading.Event()

    def hold_the_lock():
        with runtime._lock:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    assert holding.wait(5)
    read_fd, write_fd = os.pipe()
    # The child exits within seconds whatever happens; the parent never waits forever.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # 3.12+: fork with threads
        pid = os.fork()
    if pid == 0:  # the child
        code = 1
        try:
            os.close(read_fd)
            got = runtime._lock.acquire(timeout=2)
            if got:
                runtime._lock.release()
            answer = client.get_feature_flag("all-on", "user-2")
            result = {
                "lock": got,
                "source": answer.source,
                "tallies": {k: v for k, v in runtime._tallies.items()},
                "started_in_child": started[-1:] == [os.getpid()],
            }
            os.write(write_fd, json.dumps(result).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(write_fd)
    deadline = time.monotonic() + 20
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            release.set()
            holder.join()
            pytest.fail("the forked child hung")
        time.sleep(0.05)
    report = json.loads(os.read(read_fd, 4096).decode() or "{}")
    os.close(read_fd)
    release.set()
    holder.join()
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    # The child's lock was usable, it answered locally, it counted only its own evaluation, and
    # it started its own thread.
    assert report == {"lock": True, "source": "local", "tallies": {"all-on": [1, 1]}, "started_in_child": True}
    # The parent still owns its count.
    assert runtime._tallies == {"all-on": [1, 1]}


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="needs os.register_at_fork")
def test_the_pid_check_reinitialises_when_the_fork_hook_did_not_run(no_thread):
    client, _ = loaded(FakeServer(ok()))
    client.is_feature_enabled("all-on", "user-1")
    runtime = client._local
    runtime._pid = -1  # as if this process were a fork the hook missed
    assert client.get_feature_flag("all-on", "user-2").source == "local"
    assert runtime._pid == os.getpid()
    assert runtime._tallies == {"all-on": [1, 1]}


# ----------------------------------------------------------------------- construction


def test_rejects_a_bad_mode_or_interval():
    with pytest.raises(ValueError, match="evaluation must be"):
        ExperimentationClient("http://test", "k", evaluation="edge")
    with pytest.raises(ValueError, match="refresh_interval_seconds"):
        ExperimentationClient("http://test", "k", evaluation="local", refresh_interval_seconds=float("nan"))
    with pytest.raises(ValueError, match="max_stale_seconds"):
        ExperimentationClient("http://test", "k", evaluation="local", max_stale_seconds=0)


def test_status_reports_the_ruleset(no_thread):
    client, _ = local_client(FakeServer(ok(RULESET_WITH_REMOTE, '"v2"')))
    status = client.status()
    assert (status.evaluation, status.ready, status.ruleset_version, status.last_refresh_at) == ("local", False, None, None)
    client._local.refresh_once()
    status = client.status()
    assert (status.ready, status.ruleset_version, status.last_error, status.server_evaluated_flags) == (True, "v2", None, ["semver"])
    assert status.last_refresh_at is not None


def test_ready_with_a_timeout_does_not_block_forever(no_thread):
    client, _ = local_client(FakeServer(ok()))
    started = time.monotonic()
    result = client.ready(0.05)
    assert not result and isinstance(result.error, ExperimentationError)
    assert time.monotonic() - started < 5


def test_the_context_manager_closes(no_thread):
    server = FakeServer(ok())
    with local_client(server)[0] as client:
        client._local.refresh_once()
        client.is_feature_enabled("all-on", "user-1")
    assert len(server.calls(EVALUATIONS_PATH)) == 1


def test_a_throwing_on_error_does_not_break_the_refresh(no_thread):
    def boom(error, operation):
        raise RuntimeError("handler failed")

    client = ExperimentationClient("http://test", "k", transport=FakeServer(make_response(500)), evaluation="local", on_error=boom)
    assert client._local.refresh_once() == 30.0


def test_runs_on_the_supported_python():
    assert sys.version_info >= (3, 9)


# ----------------------------------------------------------------------- review fixes


def test_an_unexpected_failure_in_local_evaluation_is_reported_and_goes_to_the_server(no_thread, monkeypatch):
    server = FakeServer(ok())
    server.user_flags = lambda: make_response(200, {"all-on": True})
    client, errors = loaded(server)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(local_module, "evaluate_locally", boom)
    assert client.get_feature_flag("all-on", "user-1").source == "server"
    assert client.get_all_flags("user-1", US) == {"all-on": True}
    assert [op for op, _ in errors] == ["evaluate", "evaluate"]
    assert "boom" in errors[0][1].message
    client.close()
    assert server.calls(EVALUATIONS_PATH) == []  # nothing counted locally


def _many_flags(n: int):
    flags = [
        {"key": f"f{i:05d}", "active": True, "evaluation": "local", "rollout_percentage": 100, "rules": [], "default_rule": None}
        for i in range(n)
    ]
    return dict(RULESET, version=f"n{n}", flags=flags)


@pytest.mark.parametrize("n, sizes", [(1000, [1000]), (1001, [1000, 1])])
def test_reports_are_chunked_at_1000_entries(no_thread, n, sizes):
    server = FakeServer(ok(_many_flags(n), f'"n{n}"'))
    client, _ = loaded(server)
    client.get_all_flags("user-1")
    client.close()
    posts = server.calls(EVALUATIONS_PATH)
    assert [len(p["body"]["evaluations"]) for p in posts] == sizes
    assert sum(e["count"] for p in posts for e in p["body"]["evaluations"]) == n


@pytest.mark.parametrize(
    "count, enabled, expected",
    [
        (1_000_000, 1_000_000, [(1_000_000, 1_000_000)]),
        (1_000_001, 1_000_001, [(1_000_000, 1_000_000), (1, 1)]),
        (1_000_001, 5, [(1_000_000, 5), (1, 0)]),
        (2_000_000, 0, [(1_000_000, 0), (1_000_000, 0)]),
    ],
)
def test_counts_are_split_at_one_million(no_thread, count, enabled, expected):
    server = FakeServer(ok())
    client, _ = loaded(server)
    client.is_feature_enabled("all-on", "user-1")
    client._local._tallies["all-on"] = [count, enabled]
    client.close()
    entries = server.calls(EVALUATIONS_PATH)[0]["body"]["evaluations"]
    assert [(e["count"], e["enabled_count"]) for e in entries] == expected


# ----------------------------------------------------------------------- concurrency and fork fallbacks


def test_concurrent_evaluations_are_counted_exactly(no_thread):
    """N threads evaluating at once: the reported count is exactly N x calls (the tally is locked)."""
    server = FakeServer(ok())
    client, _ = loaded(server)
    threads_n, calls = 16, 500
    start = threading.Barrier(threads_n)
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible, so a race shows
    try:

        def work():
            start.wait()
            for i in range(calls):
                client.is_feature_enabled("us-only", "user-1", US if i % 2 else FR)

        workers = [threading.Thread(target=work) for _ in range(threads_n)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    finally:
        sys.setswitchinterval(old_interval)
    client.close()
    entries = [e for p in server.calls(EVALUATIONS_PATH) for e in p["body"]["evaluations"]]
    assert [(e["flag_key"], e["count"], e["enabled_count"]) for e in entries] == [
        ("us-only", threads_n * calls, threads_n * calls // 2)
    ]


@pytest.mark.parametrize("call", ["status", "close"])
def test_status_and_close_reinitialise_when_the_fork_hook_did_not_run(no_thread, call):
    """The PID-check fallback also runs from status() and close(), not only from evaluations."""
    server = FakeServer(ok())
    client, _ = loaded(server)
    client.is_feature_enabled("all-on", "user-1")  # a count the "parent" owns
    runtime = client._local
    runtime._pid = -1  # as if this process were a fork that os.register_at_fork missed
    getattr(client, call)()
    assert runtime._pid == os.getpid()
    # The inherited count was dropped, so close() reports nothing from before the "fork".
    if call == "close":
        assert server.calls(EVALUATIONS_PATH) == []
    else:
        assert runtime._tallies == {}

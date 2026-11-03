"""The recording proxy (H5-lite, D210 v3 SPEC 4) against a fake upstream on
127.0.0.1: what it refuses before binding or sending anything, what it
records, how it relays, and its connection policy.

Its faults (use case #4): every refusal; a ``status`` fault answering its code
on its paths only; a ``corrupt`` fault synthesizing its 200 even to a request
the upstream would answer 304; a ``blackhole`` ending at its hold, at its
client's close and at ``stop``; the fault decided when the request arrives
(never forwarded, and finishing as decided after ``clear``), the upstream
receiving exactly the unfaulted requests; ``changes``, ``wait_idle`` and the
teardown probe. Every hold here is at most 2 s, but the one ``stop`` ends: it
asks for 30 s and checks only that ``stop`` ended it (a wide liveness bound,
never a tight one, in this required job).

Every server here listens on 127.0.0.1:0 and is stopped by its fixture. The
founder-port refusal reads ``GITHUB_ACTIONS``; every test here starts with it
unset (``monkeypatch``), and the one that sets it to ``true`` only constructs
the proxy: nothing is bound, nothing is sent.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from backend.tests.unit.usecases.harness import (
    FLAG_KEY,
    Received,
    Upstream,
    closed_port,
    proxy,
)

pytestmark = pytest.mark.unit

RULESET = proxy.RULESET_PATH
EVALUATE = f"/api/v1/feature-flags/evaluate/{FLAG_KEY}"


@pytest.fixture(autouse=True)
def _not_in_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream().start()
    yield server
    server.stop()


class Ticks:
    """A clock that moves one second each time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _get(
    connection: http.client.HTTPConnection,
    path: str,
    headers: dict[str, str] | None = None,
) -> http.client.HTTPResponse:
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    response.read()
    return response


def _connect(relay: proxy.RecordingProxy) -> http.client.HTTPConnection:
    host, port = relay.server_address
    return http.client.HTTPConnection(host, port, timeout=5)


def _fetch(
    relay: proxy.RecordingProxy,
    path: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes]:
    """One request on a connection of its own: status, headers (lower-case
    names) and body."""
    connection = _connect(relay)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            body,
        )
    finally:
        connection.close()


def _until(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    """Wait for *condition*, at most *seconds* (a liveness bound)."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition did not hold in time"
        time.sleep(0.01)


def _in_background(call: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    """Run *call* on a thread; the list gets what it returned or raised."""
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(call())
        except Exception as exc:  # the test asserts on what was raised
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


# -- refusals, before anything is bound or sent ------------------------------------
def test_the_proxy_listens_on_127_0_0_1_on_an_ephemeral_port(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        host, port = relay.server_address
        assert host == "127.0.0.1"
        assert port not in proxy.FOUNDER_PORTS and port > 0
        assert relay.origin == f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("listen", ["0.0.0.0", "", "localhost", "::1", "127.0.0.2"])
def test_listening_anywhere_but_127_0_0_1_is_refused(listen):
    with pytest.raises(proxy.ProxyRefused, match="listens only on 127.0.0.1"):
        proxy.RecordingProxy(f"http://127.0.0.1:{closed_port()}", listen=listen)


@pytest.mark.parametrize(
    "host",
    ["localhost.example", "127.0.0.1.nip.io", "0.0.0.0", "127.0.0.2", "example.com"],
)
def test_an_upstream_host_that_is_not_exactly_loopback_is_refused(host):
    with pytest.raises(proxy.ProxyRefused, match="not exactly 127.0.0.1 or localhost"):
        proxy.RecordingProxy(f"http://{host}:{closed_port()}")


@pytest.mark.parametrize(
    "upstream_url, reason",
    [
        ("https://127.0.0.1:{port}", "http:// origin"),
        ("http://user:pw@127.0.0.1:{port}", "user or a password"),
        ("http://127.0.0.1:{port}/api", "no path, query or fragment"),
        ("http://127.0.0.1:{port}?x=1", "no path, query or fragment"),
        ("http://127.0.0.1:{port}#x", "no path, query or fragment"),
        ("http://127.0.0.1:port", "not a port number"),
    ],
)
def test_an_upstream_that_is_not_an_http_origin_is_refused(upstream_url, reason):
    with pytest.raises(proxy.ProxyRefused, match=reason):
        proxy.RecordingProxy(upstream_url.format(port=closed_port()))


@pytest.mark.parametrize("port", sorted(proxy.FOUNDER_PORTS))
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_founder_port_upstream_is_refused_outside_github_actions(host, port):
    with pytest.raises(proxy.ProxyRefused, match=f"upstream port {port} is refused"):
        proxy.RecordingProxy(f"http://{host}:{port}")


@pytest.mark.parametrize("value", ["1", "TRUE", "True", "yes", "", " true"])
def test_github_actions_must_be_exactly_true(monkeypatch, value):
    monkeypatch.setenv("GITHUB_ACTIONS", value)
    with pytest.raises(proxy.ProxyRefused, match="upstream port 8000 is refused"):
        proxy.RecordingProxy("http://127.0.0.1:8000")


@pytest.mark.parametrize("port", sorted(proxy.FOUNDER_PORTS))
def test_in_github_actions_a_founder_port_upstream_is_accepted(monkeypatch, port):
    # Constructed only: nothing is bound and nothing is sent (8000 is the
    # founder's API on a laptop, 5432 the Unit job's Postgres in CI).
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    relay = proxy.RecordingProxy(f"http://127.0.0.1:{port}")
    assert relay.records() == [] and relay.connections == 0 and relay.origin == ""


# -- connection policy --------------------------------------------------------------
def test_n_requests_are_n_connections_and_n_records(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        connection = _connect(relay)
        try:
            answers = [_get(connection, EVALUATE + "?user_id=u-1") for _ in range(3)]
        finally:
            connection.close()
        assert [a.status for a in answers] == [200, 200, 200]
        # One keep-alive client: N requests are still N connections.
        assert (relay.connections, len(relay.records())) == (3, 3)
        assert [a.getheader("Connection") for a in answers] == ["close"] * 3
        # Nothing is cached: every request reached the upstream.
        assert len(upstream.requests_to(EVALUATE)) == 3
        assert relay.handler_errors == 0


# -- what is recorded -----------------------------------------------------------------
def test_a_record_is_the_sequence_times_method_path_status_etag_and_if_none_match():
    assert [f.name for f in dataclasses.fields(proxy.Record)] == [
        "seq",
        "t_recv",
        "t_answer",
        "method",
        "path",
        "status",
        "etag",
        "if_none_match",
        "fault",
        "client_closed",
    ]
    # The fault fields come last, with defaults: an 8-argument record is a
    # relayed one.
    record = proxy.Record(1, 1.0, 2.0, "GET", RULESET, 200, '"v1"', False)
    assert (record.fault, record.client_closed) == (None, False)


def test_the_ruleset_etag_and_a_304_round_trip_are_recorded(upstream):
    def respond(received: Received):
        if received.target.startswith(EVALUATE):
            # An ETag on another path is not recorded.
            return 200, {"ETag": '"other"'}, b'{"enabled": true}'
        return upstream.default(received)

    upstream.respond = respond
    with proxy.RecordingProxy(upstream.origin, clock=Ticks()) as relay:
        connection = _connect(relay)
        try:
            first = _get(connection, RULESET)
            again = _get(connection, RULESET, {"If-None-Match": '"v1"'})
            other = _get(connection, EVALUATE + "?user_id=u-1")
        finally:
            connection.close()
    assert (first.status, first.getheader("ETag")) == (200, '"v1"')
    assert (again.status, again.getheader("ETag")) == (304, '"v1"')
    assert other.status == 200
    assert upstream.requests_to(RULESET)[1].headers["if-none-match"] == '"v1"'
    assert relay.records() == [
        proxy.Record(1, 1.0, 2.0, "GET", RULESET, 200, '"v1"', False),
        proxy.Record(2, 3.0, 4.0, "GET", RULESET, 304, '"v1"', True),
        proxy.Record(3, 5.0, 6.0, "GET", EVALUATE, 200, None, False),
    ]


# -- how it relays ---------------------------------------------------------------------
def test_a_body_and_the_end_to_end_headers_are_forwarded_and_hop_by_hop_ones_are_not(
    upstream,
):
    body = json.dumps({"evaluations": []}).encode()
    with proxy.RecordingProxy(upstream.origin) as relay:
        connection = _connect(relay)
        try:
            connection.request(
                "POST",
                "/api/v1/tracking/evaluations",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "K" * 32,
                    "Keep-Alive": "timeout=5",
                    "TE": "trailers",
                    "Accept-Encoding": "gzip",
                },
            )
            answer = connection.getresponse()
            answer.read()
        finally:
            connection.close()
    assert answer.status == 202
    (received,) = upstream.received
    assert (received.method, received.body) == ("POST", body)
    assert received.headers["x-api-key"] == "K" * 32
    assert received.headers["content-type"] == "application/json"
    assert received.headers["host"] == f"127.0.0.1:{upstream.port}"
    assert "keep-alive" not in received.headers and "te" not in received.headers
    assert received.headers.get("accept-encoding") != "gzip"
    (record,) = relay.records()
    assert (record.method, record.path, record.status) == (
        "POST",
        "/api/v1/tracking/evaluations",
        202,
    )


def test_both_loopback_names_reach_the_upstream_at_127_0_0_1(upstream):
    with proxy.RecordingProxy(f"http://localhost:{upstream.port}") as relay:
        connection = _connect(relay)
        try:
            assert _get(connection, RULESET).status == 200
        finally:
            connection.close()
    assert len(upstream.requests_to(RULESET)) == 1


def test_an_upstream_redirect_is_passed_back_not_followed(upstream):
    def respond(received: Received):
        if received.target == "/start":
            return 307, {"Location": "/elsewhere"}, b""
        return 200, {}, b"followed"

    upstream.respond = respond
    with proxy.RecordingProxy(upstream.origin) as relay:
        connection = _connect(relay)
        try:
            answer = _get(connection, "/start")
        finally:
            connection.close()
    assert (answer.status, answer.getheader("Location")) == (307, "/elsewhere")
    assert [r.target for r in upstream.received] == ["/start"]
    assert [(r.path, r.status) for r in relay.records()] == [("/start", 307)]


def test_an_unreachable_upstream_is_answered_502_and_recorded():
    with proxy.RecordingProxy(f"http://127.0.0.1:{closed_port()}") as relay:
        connection = _connect(relay)
        try:
            answer = _get(connection, EVALUATE + "?user_id=u-1")
        finally:
            connection.close()
    assert answer.status == 502
    assert [(r.path, r.status, r.etag) for r in relay.records()] == [
        (EVALUATE, 502, None)
    ]


def test_stop_closes_the_listening_port(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        host, port = relay.server_address
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((host, port), timeout=5).close()


# -- faults: what is refused ------------------------------------------------------------
ONLY_RULESET = (RULESET,)
REFUSED_FAULTS = {
    "reset": ({"kind": "reset"}, "fault kind 'reset' is not one of"),
    "unknown-kind": ({"kind": "slow"}, "fault kind 'slow' is not one of"),
    "empty-paths": ({"kind": "status", "paths": ()}, "a non-empty tuple"),
    "paths-list": ({"kind": "status", "paths": [RULESET]}, "a non-empty tuple"),
    "paths-str": ({"kind": "status", "paths": RULESET}, "a non-empty tuple"),
    "path-outside-api": ({"kind": "status", "paths": ("/health",)}, "under /api/v1/"),
    "path-relative": (
        {"kind": "status", "paths": ("api/v1/sdk/ruleset",)},
        "under /api/v1/",
    ),
    "path-query": ({"kind": "status", "paths": (RULESET + "?x=1",)}, "under /api/v1/"),
    "path-fragment": ({"kind": "status", "paths": (RULESET + "#x",)}, "under /api/v1/"),
    "code-404": ({"kind": "status", "status": 404}, "not one of 500, 502, 503, 504"),
    "code-501": ({"kind": "status", "status": 501}, "not one of 500, 502, 503, 504"),
    "code-bool": ({"kind": "status", "status": True}, "not one of 500, 502, 503, 504"),
    "code-float": ({"kind": "status", "status": 503.0}, "not one of 500, 502, 503"),
    "status-for-corrupt": (
        {"kind": "corrupt", "paths": ONLY_RULESET, "body": "html", "status": 503},
        "status is for a status fault",
    ),
    "status-for-blackhole": (
        {"kind": "blackhole", "status": 503},
        "status is for a status fault",
    ),
    "body-for-status": (
        {"kind": "status", "body": "truncated"},
        "body is for a corrupt fault",
    ),
    "corrupt-no-body": (
        {"kind": "corrupt", "paths": ONLY_RULESET},
        "corrupt body None is not one of html, not-a-ruleset, truncated",
    ),
    "corrupt-unknown-body": (
        {"kind": "corrupt", "paths": ONLY_RULESET, "body": "empty"},
        "corrupt body 'empty' is not one of",
    ),
    "corrupt-every-path": (
        {"kind": "corrupt", "body": "truncated"},
        "only for paths=('/api/v1/sdk/ruleset',)",
    ),
    "corrupt-other-path": (
        {"kind": "corrupt", "paths": (EVALUATE,), "body": "truncated"},
        "only for paths=",
    ),
    "corrupt-more-paths": (
        {"kind": "corrupt", "paths": (RULESET, EVALUATE), "body": "truncated"},
        "only for paths=",
    ),
    "hold-for-status": (
        {"kind": "status", "hold_seconds": 1.0},
        "hold_seconds is for a blackhole fault",
    ),
    "hold-zero": ({"kind": "blackhole", "hold_seconds": 0}, "not more than 0"),
    "hold-negative": ({"kind": "blackhole", "hold_seconds": -1}, "not more than 0"),
    "hold-over-30": ({"kind": "blackhole", "hold_seconds": 30.5}, "at most 30"),
    "hold-nan": ({"kind": "blackhole", "hold_seconds": float("nan")}, "at most 30"),
    "hold-bool": ({"kind": "blackhole", "hold_seconds": True}, "at most 30"),
}


@pytest.mark.parametrize("name", list(REFUSED_FAULTS))
def test_a_fault_outside_the_contract_is_refused(name):
    arguments, reason = REFUSED_FAULTS[name]
    with pytest.raises(proxy.ProxyRefused, match=re.escape(reason)):
        proxy.Fault(**arguments)


def test_a_fault_s_defaults_and_tags():
    assert (proxy.Fault("status").status, proxy.Fault("status").tag) == (
        503,
        "status:503",
    )
    assert proxy.Fault("status", status=504).tag == "status:504"
    hole = proxy.Fault("blackhole", paths=ONLY_RULESET)
    assert (hole.hold_seconds, hole.status, hole.body, hole.tag) == (
        10.0,
        None,
        None,
        "blackhole",
    )
    corrupt = proxy.Fault("corrupt", paths=ONLY_RULESET, body="not-a-ruleset")
    assert (corrupt.status, corrupt.hold_seconds, corrupt.tag) == (
        None,
        None,
        "corrupt:not-a-ruleset",
    )
    assert sorted(proxy.FAULT_KINDS) == ["blackhole", "corrupt", "status"]


def test_inject_is_refused_before_start_after_stop_and_while_a_fault_is_active(
    upstream,
):
    relay = proxy.RecordingProxy(upstream.origin)
    fault = proxy.Fault("status")
    with pytest.raises(proxy.ProxyRefused, match="needs a running proxy"):
        relay.inject(fault)
    assert (relay.active, relay.changes()) == (None, [])
    with relay:
        relay.inject(fault)
        with pytest.raises(proxy.ProxyRefused, match="already active"):
            relay.inject(proxy.Fault("blackhole", hold_seconds=1.0))
        with pytest.raises(proxy.ProxyRefused, match="takes a Fault"):
            relay.inject("status")  # type: ignore[arg-type]
        # Refused before anything changed: the first fault is still the one.
        assert relay.active is fault and len(relay.changes()) == 1
        relay.clear()
    with pytest.raises(proxy.ProxyRefused, match="needs a running proxy"):
        relay.inject(fault)
    assert relay.active is None
    assert [change.event for change in relay.changes()] == ["inject:status", "clear"]


# -- faults: what each answers ------------------------------------------------------------
@pytest.mark.parametrize("code", proxy.FAULT_STATUSES)
def test_a_status_fault_answers_its_code_on_its_paths_only(upstream, code):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("status", paths=ONLY_RULESET, status=code))
        status, headers, body = _fetch(relay, RULESET, {"If-None-Match": '"v1"'})
        other = _fetch(relay, EVALUATE + "?user_id=u-1")
        relay.clear()
    assert (status, body) == (code, proxy.FAULT_DETAIL)
    assert json.loads(body) == {"detail": "injected by the use-case proxy"}
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(body))
    assert headers["connection"] == "close"
    assert "etag" not in headers and "retry-after" not in headers
    assert other[0] == 200
    assert [
        (r.path, r.status, r.etag, r.if_none_match, r.fault) for r in relay.records()
    ] == [
        (RULESET, code, None, True, f"status:{code}"),
        (EVALUATE, 200, None, False, None),
    ]
    assert [r.target.split("?", 1)[0] for r in upstream.received] == [EVALUATE]


def test_a_fault_with_no_paths_answers_every_request(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("status"))
        answers = [
            _fetch(relay, RULESET)[0],
            _fetch(relay, EVALUATE + "?user_id=u-1")[0],
            _fetch(relay, "/api/v1/tracking/evaluations", method="POST")[0],
        ]
        relay.clear()
    assert answers == [503, 503, 503]
    assert [r.fault for r in relay.records()] == ["status:503"] * 3
    assert upstream.received == []


def test_a_fault_matches_the_exact_path_without_its_query(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("status", paths=ONLY_RULESET))
        for target in (RULESET + "?x=1", RULESET + "/x", RULESET + "s"):
            _fetch(relay, target)
        relay.clear()
    assert [(r.path, r.fault) for r in relay.records()] == [
        (RULESET, "status:503"),
        (RULESET + "/x", None),
        (RULESET + "s", None),
    ]


def test_the_upstream_receives_exactly_the_unfaulted_requests(upstream):
    """The fault is never forwarded: what the upstream received is, in order,
    exactly the requests recorded with no fault."""
    with proxy.RecordingProxy(upstream.origin) as relay:
        _fetch(relay, RULESET)
        relay.inject(proxy.Fault("status", paths=ONLY_RULESET))
        _fetch(relay, RULESET)
        _fetch(relay, EVALUATE + "?user_id=u-1")
        _fetch(relay, RULESET, {"If-None-Match": '"v1"'})
        relay.clear()
        relay.inject(proxy.Fault("corrupt", paths=ONLY_RULESET, body="truncated"))
        _fetch(relay, RULESET, {"If-None-Match": '"v1"'})
        relay.clear()
        _fetch(relay, RULESET, {"If-None-Match": '"v1"'})
    records = relay.records()
    assert [r.fault for r in records] == [
        None,
        "status:503",
        None,
        "status:503",
        "corrupt:truncated",
        None,
    ]
    relayed = [(r.method, r.path) for r in records if r.fault is None]
    received = [(r.method, r.target.split("?", 1)[0]) for r in upstream.received]
    assert received == relayed


@pytest.mark.parametrize("name", sorted(proxy.CORRUPT_BODIES))
def test_a_corrupt_fault_answers_200_even_to_if_none_match(upstream, name):
    """After a good load the SDKs send If-None-Match and the API answers 304
    with no body: a corrupt fault synthesizes its 200, never edits an answer."""
    with proxy.RecordingProxy(upstream.origin) as relay:
        assert _fetch(relay, RULESET)[0] == 200
        relay.inject(proxy.Fault("corrupt", paths=ONLY_RULESET, body=name))
        status, headers, body = _fetch(relay, RULESET, {"If-None-Match": '"v1"'})
        relay.clear()
    assert status == 200, f"got {status}, expected a corrupt 200"
    assert body == proxy.CORRUPT_BODIES[name]
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(body))
    assert "etag" not in headers
    faulted = relay.records()[1]
    assert (faulted.status, faulted.etag, faulted.if_none_match, faulted.fault) == (
        200,
        None,
        True,
        f"corrupt:{name}",
    )
    assert len(upstream.requests_to(RULESET)) == 1


def test_each_corrupt_body_is_a_200_that_is_not_a_valid_ruleset():
    """Both SDKs keep their ruleset and back off on a 200 whose body is not
    JSON, or a schema-1 document with no ``flags`` (an unknown schema or
    bucketing would be the refusal that discards it: not one of these)."""
    for name, body in proxy.CORRUPT_BODIES.items():
        try:
            document = json.loads(body)
        except ValueError:
            assert name in ("truncated", "html")
            continue
        assert name == "not-a-ruleset"
        assert (document["schema"], document["bucketing"]) == (1, "md5-mod100-v1")
        assert "flags" not in document


def test_a_blackhole_sends_nothing_and_ends_at_its_hold(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("blackhole", paths=ONLY_RULESET, hold_seconds=0.5))
        with pytest.raises(ConnectionResetError):
            _fetch(relay, RULESET)
        relay.clear()
        assert relay.wait_idle(5.0)
    (record,) = relay.records()
    assert (record.status, record.etag, record.fault, record.client_closed) == (
        0,
        None,
        "blackhole",
        False,
    )
    # A hold never ends early (a lower bound only).
    assert record.t_answer - record.t_recv >= 0.5
    assert upstream.received == []


def test_a_blackhole_ends_when_its_client_closes(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("blackhole", paths=ONLY_RULESET, hold_seconds=2.0))
        host, port = relay.server_address
        with socket.create_connection((host, port), timeout=5) as client:
            client.sendall(f"GET {RULESET} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            _until(lambda: relay.in_flight == 1)
        assert relay.wait_idle(5.0)
        relay.clear()
    (record,) = relay.records()
    assert (record.status, record.fault, record.client_closed) == (0, "blackhole", True)
    assert upstream.received == []


def test_stop_ends_a_held_request_and_records_it(upstream):
    relay = proxy.RecordingProxy(upstream.origin).start()
    relay.inject(proxy.Fault("blackhole", paths=ONLY_RULESET, hold_seconds=30))
    thread, outcome = _in_background(lambda: _fetch(relay, RULESET))
    _until(lambda: relay.in_flight == 1)
    started = time.monotonic()
    relay.stop()
    took = time.monotonic() - started
    # Recorded by the time stop returns (read before the client is joined:
    # its disconnect comes after the record).
    records = relay.records()
    thread.join(timeout=10)
    # Liveness, wide: stop does not wait out the 30 s hold.
    assert took < 10
    (record,) = records
    # Ended by stop: not by its client (client_closed is false), and long
    # before its 30 s hold.
    assert (record.fault, record.status, record.client_closed) == (
        "blackhole",
        0,
        False,
    )
    assert record.t_answer - record.t_recv < 10
    assert len(outcome) == 1 and isinstance(outcome[0], ConnectionResetError)


def test_a_held_request_finishes_as_decided_after_clear(upstream):
    """Decided at arrival: clear() during the hold does not forward it; its
    record appears when the hold ends, after a request made later."""
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("blackhole", paths=ONLY_RULESET, hold_seconds=2.0))
        thread, outcome = _in_background(lambda: _fetch(relay, RULESET))
        _until(lambda: relay.in_flight == 1)
        relay.clear()
        assert _fetch(relay, RULESET)[0] == 200
        assert [(r.seq, r.fault) for r in relay.records()] == [(2, None)]
        assert relay.wait_idle(5.0)
        thread.join(timeout=5)
    assert [(r.seq, r.status, r.fault) for r in relay.records()] == [
        (1, 0, "blackhole"),
        (2, 200, None),
    ]
    assert [change.seq for change in relay.changes()] == [0, 1]
    assert len(upstream.requests_to(RULESET)) == 1
    assert isinstance(outcome[0], ConnectionResetError)


def test_a_request_that_arrived_before_inject_is_relayed(upstream):
    release = threading.Event()

    def respond(received: Received):
        if received.target.startswith(EVALUATE):
            release.wait(5)
        return upstream.default(received)

    upstream.respond = respond
    with proxy.RecordingProxy(upstream.origin) as relay:
        thread, outcome = _in_background(
            lambda: _fetch(relay, EVALUATE + "?user_id=u-1")[0]
        )
        _until(lambda: len(upstream.received) == 1)
        relay.inject(proxy.Fault("status"))
        release.set()
        thread.join(timeout=5)
        later = _fetch(relay, EVALUATE + "?user_id=u-1")[0]
        relay.clear()
    assert (outcome, later) == ([200], 503)
    assert [(r.seq, r.fault) for r in relay.records()] == [(1, None), (2, "status:503")]
    assert relay.changes()[0].seq == 1


def test_changes_record_each_inject_and_clear_with_the_last_seq(upstream):
    with proxy.RecordingProxy(upstream.origin, clock=Ticks()) as relay:
        relay.clear()  # nothing active: nothing to record
        _fetch(relay, RULESET)  # seq 1: t_recv 1, t_answer 2
        relay.inject(proxy.Fault("status"))  # 3
        _fetch(relay, RULESET)  # seq 2: 4, 5
        relay.clear()  # 6
        relay.clear()  # idempotent: not recorded
        relay.inject(proxy.Fault("corrupt", paths=ONLY_RULESET, body="html"))  # 7
        relay.clear()  # 8
    assert relay.changes() == [
        proxy.Change(1, 3.0, "inject:status"),
        proxy.Change(2, 6.0, "clear"),
        proxy.Change(2, 7.0, "inject:corrupt"),
        proxy.Change(2, 8.0, "clear"),
    ]
    assert relay.active is None


def test_wait_idle_waits_more_than_0_and_at_most_30_s(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        for timeout in (0, -1, 30.5, True, "1"):
            with pytest.raises(proxy.ProxyRefused, match="at most 30 s"):
                relay.wait_idle(timeout)  # type: ignore[arg-type]
        assert relay.wait_idle(1.0) is True


# -- the teardown probe -------------------------------------------------------------------
def test_the_teardown_probe_passes_once_the_fault_is_cleared(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("status"))
        _fetch(relay, RULESET)
        relay.clear()
        assert proxy.teardown_problem(relay) is None
    assert [(r.path, r.status, r.fault) for r in relay.records()] == [
        (RULESET, 503, "status:503"),
        (RULESET, 200, None),
    ]


def test_a_fault_left_on_fails_the_teardown_probe(upstream):
    with proxy.RecordingProxy(upstream.origin) as relay:
        relay.inject(proxy.Fault("status", paths=ONLY_RULESET))
        problem = proxy.teardown_problem(relay)
    assert problem is not None and "status:503 fault was left on" in problem


def test_an_upstream_failure_fails_the_teardown_probe(upstream):
    upstream.respond = lambda received: (500, {}, b"{}")
    with proxy.RecordingProxy(upstream.origin) as relay:
        problem = proxy.teardown_problem(relay)
    assert problem is not None and "answered 500 (fault None)" in problem

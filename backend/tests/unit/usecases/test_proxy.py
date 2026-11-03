"""The recording proxy (H5-lite, D210 v3 SPEC 4) against a fake upstream on
127.0.0.1: what it refuses before binding or sending anything, what it
records, how it relays, and its connection policy.

Every server here listens on 127.0.0.1:0 and is stopped by its fixture. The
founder-port refusal reads ``GITHUB_ACTIONS``; every test here starts with it
unset (``monkeypatch``), and the one that sets it to ``true`` only constructs
the proxy: nothing is bound, nothing is sent.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import socket
from collections.abc import Iterator

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
        assert [a.getheader("Connection") for a in answers] == ["close"] * 3
        assert (relay.connections, len(relay.records())) == (3, 3)
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
    ]


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

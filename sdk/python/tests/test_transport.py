"""UrllibTransport with ``urllib.request.urlopen`` mocked."""

from __future__ import annotations

import io
import urllib.error
from email.message import Message
from unittest import mock

import pytest

from experimentation import ExperimentationError, Response, TransportError, UrllibTransport


def _http_response(status=200, headers=None, body=b'{"ok":true}'):
    """A stand-in for the object ``urlopen`` yields as a context manager."""
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    raw = mock.MagicMock()
    raw.status = status
    raw.headers = message
    raw.read.return_value = body
    raw.__enter__.return_value = raw
    raw.__exit__.return_value = False
    return raw


def test_get_builds_request_and_maps_response():
    raw = _http_response(200, {"Content-Type": "application/json"}, b'{"a": 1}')
    with mock.patch("urllib.request.urlopen", return_value=raw) as urlopen:
        response = UrllibTransport().send(
            "GET", "http://x/api/v1/y?z=1", {"X-API-Key": "k", "Accept": "application/json"}, None, 2.5
        )

    assert isinstance(response, Response)
    assert response.status == 200
    assert response.json() == {"a": 1}
    assert response.header("content-type") == "application/json"

    request = urlopen.call_args[0][0]
    assert request.full_url == "http://x/api/v1/y?z=1"
    assert request.get_method() == "GET"
    assert request.get_header("X-api-key") == "k"  # urllib capitalises header names
    assert request.data is None
    assert urlopen.call_args[1]["timeout"] == 2.5


def test_post_sends_body():
    with mock.patch("urllib.request.urlopen", return_value=_http_response()) as urlopen:
        UrllibTransport().send("POST", "http://x/p", {}, b'{"x":1}', 1.0)
    request = urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.data == b'{"x":1}'


def test_http_error_becomes_response_with_status_headers_and_body():
    headers = Message()
    headers["Retry-After"] = "3"
    error = urllib.error.HTTPError(
        "http://x/p", 429, "Too Many Requests", headers, io.BytesIO(b'{"detail":"slow down"}')
    )
    with mock.patch("urllib.request.urlopen", side_effect=error):
        response = UrllibTransport().send("GET", "http://x/p", {}, None, 1.0)
    assert response.status == 429
    assert response.header("retry-after") == "3"
    assert response.json() == {"detail": "slow down"}


@pytest.mark.parametrize(
    "exc",
    [urllib.error.URLError("connection refused"), TimeoutError("timed out"), ConnectionResetError("reset")],
    ids=["urlerror", "timeout", "reset"],
)
def test_network_failures_raise_transport_error(exc):
    with mock.patch("urllib.request.urlopen", side_effect=exc):
        with pytest.raises(TransportError) as info:
            UrllibTransport().send("GET", "http://x/p", {}, None, 1.0)
    assert isinstance(info.value, ExperimentationError)
    assert info.value.status is None
    assert "http://x/p" in str(info.value)


def test_the_suite_cannot_open_a_real_connection():
    """conftest.py refuses every connection, so HTTP in this suite is faked or
    the test fails; the real urlopen below never leaves the process."""
    with pytest.raises(TransportError, match="a real connection was attempted"):
        UrllibTransport().send("GET", "http://127.0.0.1:9/never", {}, None, 1.0)

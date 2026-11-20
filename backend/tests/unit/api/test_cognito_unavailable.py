"""A Cognito that does not answer is a 503, not a 401; a bad token stays a 401.

The Cognito client gives up after 2 s to connect and 3 s to read, with one
retry. When ``GetUser`` times out, cannot connect, or Cognito reports a
throttle or a server error, a signed-in request answers 503 with Retry-After,
so the dashboard keeps the stored sign-in. A token Cognito refuses (or any
failure not on the allow-list) answers 401 as before. The results WebSocket
keeps its close code 4401.

No database and no AWS: a stub client, or a 127.0.0.1 listener that accepts a
connection and never answers (the endpoint is pointed there with
``AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER``; the port is picked by the OS).
"""

import logging
import socket
import threading
import time
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from backend.app.api import deps, read_scope
from backend.app.api.v1.endpoints import auth as auth_endpoints
from backend.app.api.v1.endpoints import websocket_results
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.services import auth_service as auth_service_module
from backend.app.services.auth_service import (
    COGNITO_CONNECT_TIMEOUT,
    COGNITO_MAX_ATTEMPTS,
    COGNITO_READ_TIMEOUT,
    CognitoAuthService,
    CognitoTokenRefused,
    CognitoUnavailable,
)

pytestmark = pytest.mark.unit

COPY = "Sign-in service is not answering. Try again in a moment."
SECRET_TOKEN = "eyJ-secret-token-text-that-must-not-be-logged"
LOCAL = "http://127.0.0.1:1"


def _client_error(
    code: str, status: Optional[int] = 400, message: str = "m"
) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": message}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return ClientError(response, "GetUser")


def _service_raising(exc: Exception) -> CognitoAuthService:
    service = CognitoAuthService()
    service.client = MagicMock()
    service.client.get_user.side_effect = exc
    return service


@pytest.fixture
def cognito_mode(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)


def _get_current_user_with(monkeypatch, service) -> HTTPException:
    monkeypatch.setattr("backend.app.api.deps.auth_service", service)
    assert deps.auth_service is service
    with pytest.raises(HTTPException) as caught:
        deps.get_current_user(token=SECRET_TOKEN, db=MagicMock())
    return caught.value


def _assert_503(exc: HTTPException) -> None:
    assert exc.status_code == 503
    assert exc.detail == COPY
    assert exc.headers == {"Retry-After": "2"}


# V1 ---------------------------------------------------------------------


@pytest.mark.regression
def test_the_cognito_client_has_finite_timeouts(monkeypatch):
    """Old code: no ``config`` was passed, so botocore's defaults applied."""
    created: List[dict] = []
    monkeypatch.setattr(
        auth_service_module.boto3,
        "client",
        lambda *a, **kw: created.append(kw) or MagicMock(),
    )
    _ = CognitoAuthService().client
    config = created[0]["config"]
    assert config.connect_timeout == 2 == COGNITO_CONNECT_TIMEOUT
    assert config.read_timeout == 3 == COGNITO_READ_TIMEOUT
    assert config.retries == {"mode": "standard", "total_max_attempts": 2}
    assert COGNITO_MAX_ATTEMPTS == 2


@pytest.mark.regression
def test_the_real_cognito_client_carries_the_config():
    """The client boto3 builds, not a stub: its own config says 2 s and 3 s.
    Old code: no Config, so botocore's defaults applied."""
    config = CognitoAuthService().client.meta.config
    assert config.connect_timeout == 2
    assert config.read_timeout == 3
    assert config.retries["mode"] == "standard"
    assert config.retries["total_max_attempts"] == 2


# V2 ---------------------------------------------------------------------


class _SilentListener:
    """Accepts connections and never answers; keeps each one open."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted: List[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for conn in self.accepted:
            conn.close()
        self.sock.close()


@pytest.mark.regression
def test_a_never_answering_cognito_gives_503_after_two_attempts(
    monkeypatch, cognito_mode, caplog
):
    listener = _SilentListener()
    try:
        monkeypatch.setenv(
            "AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER",
            f"http://127.0.0.1:{listener.port}",
        )
        fresh = CognitoAuthService()
        assert fresh.client.meta.endpoint_url.startswith("http://127.0.0.1")
        monkeypatch.setattr("backend.app.api.deps.auth_service", fresh)
        assert deps.auth_service is fresh
        started = time.monotonic()
        with caplog.at_level(logging.ERROR, logger=auth_service_module.logger.name):
            with pytest.raises(HTTPException) as caught:
                deps.get_current_user(token=SECRET_TOKEN, db=MagicMock())
        elapsed = time.monotonic() - started
        time.sleep(0.3)  # let the listener thread record the last accept
        accepts = len(listener.accepted)
    finally:
        listener.close()
    assert accepts == 2
    _assert_503(caught.value)
    assert elapsed < 30  # generous: the point is that it ends, not how fast
    assert "ReadTimeoutError" in caplog.text
    assert SECRET_TOKEN not in caplog.text


# V3, V3b, V3c ----------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        _client_error("NotAuthorizedException", 400),
        _client_error("UserNotFoundException", 400),
        _client_error("ForbiddenException", 403),
        _client_error("NotAuthorizedException", None),  # no ResponseMetadata
        RuntimeError("anything else"),
    ],
    ids=["NotAuthorized", "UserNotFound", "Forbidden", "no-metadata", "RuntimeError"],
)
def test_a_bad_token_stays_401(exc, monkeypatch, cognito_mode):
    caught = _get_current_user_with(monkeypatch, _service_raising(exc))
    assert caught.status_code == 401
    assert caught.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("reason", ["wrong_issuer", "not_configured"])
def test_a_refused_token_stays_401_not_503(reason, monkeypatch, cognito_mode):
    """A token Cognito accepted but this deployment did not issue is refused,
    not unavailable: the answer is the 401 it always was."""
    caught = _get_current_user_with(
        monkeypatch, _service_raising(CognitoTokenRefused(reason))
    )
    assert caught.status_code == 401
    assert caught.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize(
    "exc",
    [
        _client_error("TooManyRequestsException", 400),
        _client_error("ThrottlingException", 400),
        _client_error("InternalErrorException", 500),
        _client_error("502", 502),
        _client_error("ServiceUnavailable", None),
    ],
    ids=["TooManyRequests", "Throttling", "InternalError", "html-502", "no-metadata"],
)
def test_throttles_and_cognito_server_errors_give_503(exc, monkeypatch, cognito_mode):
    _assert_503(_get_current_user_with(monkeypatch, _service_raising(exc)))


@pytest.mark.regression
@pytest.mark.parametrize(
    "exc",
    [
        ConnectTimeoutError(endpoint_url=LOCAL),
        ReadTimeoutError(endpoint_url=LOCAL),
        EndpointConnectionError(endpoint_url=LOCAL),
        ConnectionClosedError(endpoint_url=LOCAL),
        SSLError(endpoint_url=LOCAL, error="bad"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_cognito_read_timeout_is_not_a_bad_token(exc, monkeypatch, cognito_mode):
    """Old code: every failure of GetUser became a ValueError, then a 401."""
    _assert_503(_get_current_user_with(monkeypatch, _service_raising(exc)))


@pytest.mark.parametrize("method", ["get_user", "get_user_with_groups"])
def test_the_service_raises_cognito_unavailable_not_a_value_error(method):
    service = _service_raising(ReadTimeoutError(endpoint_url=LOCAL))
    with pytest.raises(CognitoUnavailable) as caught:
        getattr(service, method)(SECRET_TOKEN)
    assert not isinstance(caught.value, ValueError)


# V5: every caller -------------------------------------------------------


class _UnavailableService:
    """Stands in for CognitoAuthService in the auth routes."""

    def get_user(self, access_token):
        raise CognitoUnavailable("ReadTimeoutError")

    def global_sign_out(self, access_token):  # pragma: no cover - never reached
        raise AssertionError("GlobalSignOut must not run")


@pytest.fixture
def http(monkeypatch, cognito_mode):
    monkeypatch.setattr(auth_endpoints, "CognitoAuthService", _UnavailableService)
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(deps.get_db, None)


def test_me_answers_503(http):
    response = http.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {SECRET_TOKEN}"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": COPY}
    assert response.headers["Retry-After"] == "2"


def test_logout_answers_503(http):
    response = http.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {SECRET_TOKEN}"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": COPY}
    assert response.headers["Retry-After"] == "2"


def test_read_scope_answers_503(monkeypatch, cognito_mode):
    monkeypatch.setattr(
        "backend.app.api.deps.auth_service",
        _service_raising(ReadTimeoutError(endpoint_url=LOCAL)),
    )
    with pytest.raises(HTTPException) as caught:
        read_scope.get_current_reader(
            db=MagicMock(), token=SECRET_TOKEN, api_key_header=None
        )
    _assert_503(caught.value)


def test_the_websocket_keeps_close_code_4401(monkeypatch, cognito_mode):
    monkeypatch.setattr(
        "backend.app.api.deps.auth_service",
        _service_raising(ReadTimeoutError(endpoint_url=LOCAL)),
    )
    statuses: List[int] = []
    real = deps.get_current_user

    def spy(**kwargs):
        try:
            return real(**kwargs)
        except HTTPException as exc:
            statuses.append(exc.status_code)
            raise

    monkeypatch.setattr(deps, "get_current_user", spy)
    websocket = MagicMock()
    websocket.headers = {"authorization": f"Bearer {SECRET_TOKEN}"}
    code = websocket_results._admit_websocket(
        websocket=websocket, token=None, db=MagicMock()
    )
    assert statuses == [503]  # the 503 happened, and the socket still says 4401
    assert code == websocket_results.WS_CLOSE_UNAUTHORIZED == 4401


# V8: the log names the class and the code, never the token --------------


@pytest.mark.parametrize(
    "exc, name, code",
    [
        (ReadTimeoutError(endpoint_url=LOCAL), "ReadTimeoutError", "None"),
        (
            _client_error("TooManyRequestsException", 400, message=SECRET_TOKEN),
            "ClientError",
            "TooManyRequestsException",
        ),
    ],
    ids=["ReadTimeout", "TooManyRequests"],
)
def test_the_log_names_the_class_and_code_and_no_token(
    exc, name, code, monkeypatch, cognito_mode, caplog
):
    sunk: List[str] = []
    sink_id = loguru_logger.add(lambda m: sunk.append(str(m)), level="ERROR")
    try:
        with caplog.at_level(logging.ERROR, logger=auth_service_module.logger.name):
            _get_current_user_with(monkeypatch, _service_raising(exc))
    finally:
        loguru_logger.remove(sink_id)
    assert name in caplog.text and code in caplog.text
    assert any(name in line for line in sunk)
    assert SECRET_TOKEN not in caplog.text
    assert not any(SECRET_TOKEN in line for line in sunk)


def test_the_classifier_reads_a_hand_built_client_error_without_raising():
    """A ClientError built without ResponseMetadata is read with ``.get``; the
    service's catch-all would hide a KeyError as a 401, so ask directly."""
    assert (
        auth_service_module._unavailable(_client_error("NotAuthorized", None)) is False
    )
    assert auth_service_module._unavailable(_client_error("ThrottlingException", None))
    assert auth_service_module._unavailable(RuntimeError("x")) is False

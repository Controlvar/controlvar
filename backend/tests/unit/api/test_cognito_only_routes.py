"""
The six Cognito-only auth routes answer 404 unless ``AUTH_PROVIDER`` is
``cognito`` (#387, #708).

``POST /api/v1/auth/{signup,confirm,forgot-password,reset-password,refresh}``
and the Hosted UI's code exchange, ``POST /api/v1/auth/cognito/exchange``,
exist only for the Cognito provider.  Under any other provider each answers
404 with one fixed message, whatever the body, and creates no Cognito client.
Under ``cognito`` the first five still reach ``CognitoAuthService``; the
exchange's own tests are ``integration/api/test_cognito_hosted_ui_relay.py``.

No database and no modules: the routes under test never open a session, and
``CognitoAuthService`` is replaced by a recording stub.  The AWS credential
chain is blanked in every test, and the low-level session class that every
boto3 client is created through records any client it is asked for, so a
regression here cannot reach a real account.
"""

from typing import Any, Dict, List, Set, Tuple
from unittest.mock import MagicMock

import boto3
import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1.endpoints import auth as auth_endpoints
from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: Spelled out, not imported: a change to the message has to change this test.
FIXED = (
    "Endpoint not available: AUTH_PROVIDER is not 'cognito'. "
    "Sign in with POST /api/v1/auth/login; "
    "an administrator creates accounts and resets passwords."
)

PREFIX = "/api/v1/auth"

#: path -> a body that passes the route's request model.
VALID_BODIES: Dict[str, Dict[str, Any]] = {
    f"{PREFIX}/signup": {
        "username": "probeuser",
        "password": "Abcdefg1x",
        "email": "probe@example.com",
        "given_name": "Probe",
        "family_name": "User",
    },
    f"{PREFIX}/confirm": {"username": "probeuser", "confirmation_code": "123456"},
    f"{PREFIX}/forgot-password": {"username": "probeuser"},
    f"{PREFIX}/reset-password": {
        "username": "probeuser",
        "confirmation_code": "123456",
        "new_password": "Abcdefg1x",
    },
    f"{PREFIX}/refresh": {"refresh_token": "a-refresh-token"},
    f"{PREFIX}/cognito/exchange": {
        "code": "a-code",
        "code_verifier": "v" * 43,
        "redirect_uri": "https://dashboard.example.com/auth/callback",
    },
}

COGNITO_ONLY: Set[Tuple[str, str]] = {(path, "POST") for path in VALID_BODIES}

#: The stub method each route calls, and the response it hands back (shaped
#: to the route's response model), and the status the route then answers.
STUB_CALLS: Dict[str, Tuple[str, Dict[str, Any], int]] = {
    f"{PREFIX}/signup": (
        "sign_up",
        {"user_id": "stub-user", "confirmed": False, "message": "stub"},
        201,
    ),
    f"{PREFIX}/confirm": (
        "confirm_sign_up",
        {"message": "stub", "confirmed": True},
        200,
    ),
    f"{PREFIX}/forgot-password": ("forgot_password", {"message": "stub"}, 200),
    f"{PREFIX}/reset-password": ("confirm_forgot_password", {"message": "stub"}, 200),
    f"{PREFIX}/refresh": (
        "refresh_token",
        {
            "access_token": "stub-access",
            "id_token": "stub-id",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        200,
    ),
}

#: The class every boto3 client is created through (``boto3.client`` and
#: ``boto3.session.Session().client`` both end in its ``create_client``).
LOW_LEVEL_SESSION = type(boto3.session.Session()._session)


class RecordingCognitoService:
    """Stands in for ``CognitoAuthService``: records construction and calls."""

    constructed: List[None] = []
    calls: List[Tuple[str, Dict[str, Any]]] = []

    def __init__(self) -> None:
        RecordingCognitoService.constructed.append(None)

    def __getattr__(self, name: str):
        def method(**kwargs: Any) -> Dict[str, Any]:
            RecordingCognitoService.calls.append((name, kwargs))
            path = next(p for p, (m, _, _) in STUB_CALLS.items() if m == name)
            return dict(STUB_CALLS[path][1])

        return method


@pytest.fixture
def recorders(monkeypatch):
    """Blank the AWS chain and record every Cognito service and AWS client."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    clients: List[Tuple[Any, ...]] = []

    def record_client(self, *args: Any, **kwargs: Any) -> MagicMock:
        # Record rather than raise: the service catches broad exceptions, so
        # a raising guard would be turned into an ordinary error response.
        clients.append(args)
        return MagicMock()

    monkeypatch.setattr(LOW_LEVEL_SESSION, "create_client", record_client)

    RecordingCognitoService.constructed = []
    RecordingCognitoService.calls = []
    monkeypatch.setattr(auth_endpoints, "CognitoAuthService", RecordingCognitoService)

    return clients


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, path: str, body_kind: str):
    if body_kind == "valid":
        return client.post(path, json=VALID_BODIES[path])
    if body_kind == "empty-object":
        return client.post(path, json={})
    return client.post(path)


def _route_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


@pytest.mark.parametrize("body_kind", ["valid", "empty-object", "no-body"])
@pytest.mark.parametrize("path", sorted(VALID_BODIES), ids=_route_name)
def test_local_provider_answers_fixed_404(
    path, body_kind, client, recorders, monkeypatch
):
    """Under ``local`` each route answers the fixed 404 and builds no client."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")

    response = _post(client, path, body_kind)

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": FIXED}
    assert recorders == []
    assert RecordingCognitoService.constructed == []
    assert RecordingCognitoService.calls == []


@pytest.mark.parametrize("provider", ["saml", ""], ids=["saml", "empty-string"])
@pytest.mark.parametrize("path", sorted(VALID_BODIES), ids=_route_name)
def test_any_other_provider_answers_fixed_404(
    path, provider, client, recorders, monkeypatch
):
    """The check is "not cognito", so an unexpected value is refused too."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", provider)

    response = _post(client, path, "valid")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": FIXED}
    assert recorders == []
    assert RecordingCognitoService.constructed == []


#: Self sign-up also needs COGNITO_SELF_SIGNUP_ENABLED (T94); the gate and its
#: full matrix are pinned in test_cognito_self_signup_gate.py.
SELF_SIGNUP = {f"{PREFIX}/signup", f"{PREFIX}/confirm"}
SELF_SIGNUP_OFF = (
    "Endpoint not available: self sign-up is turned off "
    "(COGNITO_SELF_SIGNUP_ENABLED is not true). "
    "An administrator creates users in the Cognito user pool."
)


@pytest.mark.parametrize("path", sorted(STUB_CALLS), ids=_route_name)
def test_cognito_provider_reaches_the_service(path, client, recorders, monkeypatch):
    """Under ``cognito`` the request reaches the service (self sign-up with
    the setting turned on). Not the code exchange, which asks the Hosted UI's
    token endpoint and has its own tests."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    if path in SELF_SIGNUP:
        monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", True)
    method, _, expected_status = STUB_CALLS[path]

    response = _post(client, path, "valid")

    assert response.status_code == expected_status, response.text
    assert response.json().get("detail") != FIXED
    assert RecordingCognitoService.constructed == [None]
    assert [name for name, _ in RecordingCognitoService.calls] == [method]
    assert recorders == []


@pytest.mark.parametrize("path", sorted(SELF_SIGNUP), ids=_route_name)
def test_cognito_provider_with_self_signup_off_answers_404(
    path, client, recorders, monkeypatch
):
    """Under ``cognito`` with the setting false, self sign-up is refused
    before the service is built."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", False)

    response = _post(client, path, "valid")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": SELF_SIGNUP_OFF}
    assert RecordingCognitoService.constructed == []
    assert RecordingCognitoService.calls == []
    assert recorders == []


def _mounted_routes():
    """(path, method, route context) for every HTTP route of the app.

    ``include_router`` is kept as a lazy entry, so plain ``app.routes`` holds
    only a handful of entries; ``effective_route_contexts`` flattens it (the
    same reading as ``backend/tests/smoke/test_flag_change_routes_guarded.py``).
    """
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield ctx.path, method, ctx


def _gate_calls(dependant) -> bool:
    """True if ``dependant`` or any of its sub-dependencies is the gate."""
    return any(
        dep.call is auth_endpoints._require_cognito_provider or _gate_calls(dep)
        for dep in dependant.dependencies
    )


def test_exactly_the_six_routes_carry_the_gate():
    """Read from the mounted app: no route gains or loses the gate unnoticed."""
    routes = list(_mounted_routes())
    gated = {
        (path, method) for path, method, ctx in routes if _gate_calls(ctx.dependant)
    }

    assert gated == COGNITO_ONLY
    assert len(COGNITO_ONLY) == 6
    assert COGNITO_ONLY <= {(path, method) for path, method, _ in routes}

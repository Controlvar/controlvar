"""The token between the two sign-in steps is never an access token (#1812).

``POST /api/v1/auth/login`` answers an account with two-step sign-in on with
an ``mfa_token`` (the ``verify`` purpose), and an account whose role requires
it but has not set it up with one of the ``enrol`` purpose. If any route took
either as a bearer token, the second step would be skipped altogether. For
each purpose, this sends every HTTP route of the application three ways, as
the same active superuser:

* with a garbage bearer token;
* with an ``mfa_token`` minted for that account;
* (one route) with the account's real access token.

The ``mfa_token`` must be answered exactly as the garbage one is, on every
route (status, ``WWW-Authenticate``, and the body of a 401 or 403), and the
real token must get in where the garbage one does not, so the comparison is
not vacuous.

The account must resolve: an unknown account, or a database that cannot be
reached, answers 401 whatever the token, and would hide a route that took the
``mfa_token``. So every database session the application asks for is a stub
that answers this one active superuser to any lookup, and no database is
needed. ``backend/tests/integration/api/test_two_factor_sign_in_api.py`` sends
the same tokens with a row that is really stored. The WebSocket route is not
an HTTP route: its own check is called directly at the end.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import websocket_results
from backend.app.core.config import settings
from backend.app.core.mfa_token import (
    ENROL_PURPOSE,
    ENROL_TTL_SECONDS,
    VERIFY_PURPOSE,
    VERIFY_TTL_SECONDS,
    create_mfa_token,
)
from backend.app.core.security import create_local_access_token
from backend.app.db import session as session_module
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

PLACEHOLDER = "00000000-0000-4000-8000-000000000000"
GARBAGE = "not-a-token"
#: A route the real token gets into with no database at all.
ME = "/api/v1/auth/me"


def _account() -> User:
    return User(
        id=uuid.uuid4(),
        username="never-a-bearer",
        email="never-a-bearer@example.com",
        hashed_password="unused",
        is_active=True,
        is_superuser=True,
        role=UserRole.ADMIN,
    )


def _session_answering(user: User) -> MagicMock:
    """A session whose every ``query(...).filter(...).first()`` is ``user``."""
    session = MagicMock(name="stub-session")
    session.query.return_value.filter.return_value.first.return_value = user
    return session


def _http_routes():
    found = set()
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                found.add((method, ctx.path))
    return sorted(found)


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", PLACEHOLDER, path)


def _answer(response):
    """What is compared: status, WWW-Authenticate, and a refusal's body."""
    body = None
    if response.status_code in (401, 403):
        try:
            body = response.json()
        except ValueError:
            body = response.text
    return response.status_code, response.headers.get("www-authenticate"), body


#: Each purpose an mfa_token is issued for, with its lifetime.
PURPOSES = {VERIFY_PURPOSE: VERIFY_TTL_SECONDS, ENROL_PURPOSE: ENROL_TTL_SECONDS}


def _mfa_token(account: User, purpose: str) -> str:
    return create_mfa_token(account, purpose=purpose, ttl_seconds=PURPOSES[purpose])


@pytest.fixture
def account():
    return _account()


@pytest.fixture
def client(monkeypatch, account):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    assert app.dependency_overrides == {}

    def stub_db():
        yield _session_answering(account)

    app.dependency_overrides[deps.get_db] = stub_db
    app.dependency_overrides[session_module.get_db] = stub_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _send(client, method, path, token):
    body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
    return client.request(
        method,
        _url(path),
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("purpose", sorted(PURPOSES))
def test_mfa_token_never_a_bearer(client, account, purpose):
    mfa_token = _mfa_token(account, purpose)
    routes = _http_routes()
    assert len(routes) >= 150, len(routes)
    taken = {}
    for method, path in routes:
        garbage = _answer(_send(client, method, path, GARBAGE))
        mfa = _answer(_send(client, method, path, mfa_token))
        if mfa != garbage:
            taken[f"{method} {path}"] = {"garbage": garbage, "mfa_token": mfa}
    assert not taken, f"routes that answer an mfa_token as more than garbage: {taken}"


def test_the_real_token_gets_in_where_garbage_does_not(client, account):
    """Without this the comparison above could pass with every route shut."""
    garbage = _send(client, "GET", ME, GARBAGE)
    real = _send(client, "GET", ME, create_local_access_token(account))
    assert garbage.status_code == 401, garbage.text
    assert real.status_code == 200, real.text
    assert real.json()["email"] == account.email


@pytest.mark.parametrize("purpose", sorted(PURPOSES))
def test_the_results_websocket_refuses_an_mfa_token(monkeypatch, account, purpose):
    """The WebSocket's own check (``get_current_user`` on its token)."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    socket = SimpleNamespace(headers={}, scope={"subprotocols": []})
    db = _session_answering(account)

    refused = websocket_results._admit_websocket(
        socket, token=_mfa_token(account, purpose), db=db
    )
    admitted = websocket_results._admit_websocket(
        socket, token=create_local_access_token(account), db=db
    )

    assert refused == websocket_results.WS_CLOSE_UNAUTHORIZED
    assert admitted is None


@pytest.mark.parametrize("purpose", sorted(PURPOSES))
def test_get_current_user_refuses_an_mfa_token(monkeypatch, account, purpose):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    db = _session_answering(account)
    with pytest.raises(Exception) as refused:
        deps.get_current_user(token=_mfa_token(account, purpose), db=db)
    assert getattr(refused.value, "status_code", None) == 401
    assert deps.get_current_user(token=create_local_access_token(account), db=db) is (
        account
    )

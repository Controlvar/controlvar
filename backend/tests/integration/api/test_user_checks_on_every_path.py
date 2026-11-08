"""The active-user check holds on every path that resolves a user, and the
sign-in step stays the request's only trip to a worker before its handler.

Driven through the real app: the real ``deps.get_db`` (both session
factories are pointed at a QueuePool engine on the test database, the one
``deps`` uses and the one ``backend.app.db.session`` uses), the real
``get_current_user`` and the real active-user checks. Only the identity
provider is stubbed: ``deps.auth_service.get_user_with_groups`` returns the
payload ``backend/tests/integration/cognito_identity.py`` builds.

* With ``AUTH_PROVIDER=cognito``, an account linked to its Cognito user ID but
  deactivated is refused with ``400 {"detail": "Inactive user"}`` on the five
  reader routes (``read_scope.get_current_reader``, which resolves the user
  itself), on a route that takes ``Depends(deps.get_current_active_user)``,
  and on the results WebSocket (closed with 4401). The local provider already
  refuses an inactive account inside ``get_current_user``, so only the
  Cognito path shows whether the separate check still runs.
* ``GET /api/v1/auth/me`` answers an active local user with the ``UserMe``
  body and an inactive one with 400.
* On an async signed-in route, ``GET /api/v1/users/me``, no connection is
  checked out on the event loop's thread for any way of signing in: a local
  token, the development bypass, a linked Cognito account, and a first
  Cognito sign-in that creates the account. The identity provider is never
  called on the event loop's thread.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.websockets import WebSocketDisconnect

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import user_to_me
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.db import session as session_module
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services.cognito_accounts import cognito_external_id
from backend.tests.integration.cognito_identity import identity

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
INACTIVE = {"detail": "Inactive user"}
COGNITO_TOKEN = "cognito-access-token"


@dataclass
class Checkouts:
    """The thread of every checkout from the test engine, in order."""

    threads: List[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def on(self, thread: int, start: int = 0) -> int:
        with self.lock:
            return sum(1 for t in self.threads[start:] if t == thread)

    def mark(self) -> int:
        with self.lock:
            return len(self.threads)


@pytest.fixture
def checkouts(test_db, monkeypatch):
    """Point both session factories at a QueuePool engine on the test database."""
    engine = create_engine(test_db.url, pool_pre_ping=True, pool_size=4, max_overflow=4)
    assert isinstance(engine.pool, QueuePool)
    seen = Checkouts()

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_connection, _record, _proxy):
        with seen.lock:
            seen.threads.append(threading.get_ident())

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(deps, "SessionLocal", factory)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    assert app.dependency_overrides == {}
    try:
        yield seen
    finally:
        engine.dispose()


@pytest.fixture
def provider(monkeypatch):
    """Settings for a fail-closed run; the provider is chosen by each test."""
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", False)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    return monkeypatch


@dataclass
class CognitoStub:
    """``get_user_with_groups``: the payload it answers, and its threads."""

    payload: Optional[dict] = None
    threads: List[int] = field(default_factory=list)

    def __call__(self, token: str) -> dict:
        assert token == COGNITO_TOKEN, token
        self.threads.append(threading.get_ident())
        assert self.payload is not None
        return self.payload


@pytest.fixture
def cognito(provider):
    stub = CognitoStub()
    provider.setattr(settings, "AUTH_PROVIDER", "cognito")
    provider.setattr(deps.auth_service, "get_user_with_groups", stub)
    return stub


def _user(db_session, *, active: bool, sub: Optional[str] = None) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"checks_{suffix}",
        email=f"checks_{suffix}@int.test",
        full_name="User Checks",
        hashed_password="not-a-real-hash",
        is_active=active,
        is_superuser=False,
        role=UserRole.DEVELOPER,
        external_id=cognito_external_id(sub) if sub else None,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def cleanup(db_session):
    """Delete, at the end, the users a test names."""
    names: List[str] = []
    yield names
    db_session.rollback()
    db_session.query(User).filter(User.username.in_(names)).delete(
        synchronize_session=False
    )
    db_session.commit()


@pytest.fixture
def inactive_linked(db_session, cognito, cleanup):
    """A deactivated account linked to a Cognito user, and the stub answering
    for it."""
    sub = f"sub-{uuid.uuid4().hex}"
    user = _user(db_session, active=False, sub=sub)
    cleanup.append(user.username)
    cognito.payload = identity(user.username, sub, user.email, ["developer-group"])
    return user


def _reader_paths() -> List[str]:
    experiment_id = str(uuid.uuid4())
    return [
        "/api/v1/feature-flags/",
        "/api/v1/feature-flags",
        "/api/v1/experiments/",
        f"/api/v1/experiments/{experiment_id}",
        f"/api/v1/experiments/{experiment_id}/results",
    ]


COGNITO_BEARER = {"Authorization": f"Bearer {COGNITO_TOKEN}"}


@pytest.mark.regression
def test_an_inactive_cognito_account_is_refused_on_the_reader_routes(
    checkouts, inactive_linked
):
    paths = _reader_paths()
    with TestClient(app, raise_server_exceptions=False) as client:
        answers = {
            path: (response.status_code, response.json())
            for path in paths
            for response in [client.get(path, headers=COGNITO_BEARER)]
        }
    assert answers == dict.fromkeys(paths, (400, INACTIVE))


@pytest.mark.regression
def test_an_inactive_cognito_account_is_refused_on_a_signed_in_route(
    checkouts, inactive_linked
):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/rollout-schedules/", headers=COGNITO_BEARER)
    assert (response.status_code, response.json()) == (400, INACTIVE)


@pytest.mark.regression
def test_an_inactive_cognito_account_is_refused_by_the_results_websocket(
    checkouts, inactive_linked
):
    path = f"/api/v1/ws/experiments/{uuid.uuid4()}/results"
    with TestClient(app) as client:
        with client.websocket_connect(path, headers=COGNITO_BEARER) as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
    assert closed.value.code == 4401, (
        f"the results WebSocket closed with {closed.value.code} for an inactive "
        "account, want 4401"
    )


@pytest.mark.regression
def test_auth_me_answers_an_active_local_user_and_refuses_an_inactive_one(
    checkouts, provider, db_session, cleanup
):
    active = _user(db_session, active=True)
    inactive = _user(db_session, active=False)
    cleanup.extend([active.username, inactive.username])
    with TestClient(app, raise_server_exceptions=False) as client:
        answers = [
            client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {create_local_access_token(u)}"},
            )
            for u in (active, inactive)
        ]
    assert answers[0].status_code == 200, answers[0].text
    assert answers[0].json() == user_to_me(active).model_dump(mode="json")
    assert (answers[1].status_code, answers[1].json()) == (400, INACTIVE)


# ---------------------------------------------------------------------------
# No checkout on the event loop's thread, for every way of signing in
# ---------------------------------------------------------------------------


async def _users_me(headers: dict, checkouts: Checkouts) -> tuple:
    """``GET /api/v1/users/me`` in-process: its answer and the checkouts made
    on the event loop's thread while it ran."""
    loop_thread = threading.get_ident()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        start = checkouts.mark()
        response = await http.get("/api/v1/users/me", headers=headers)
    return response, checkouts.on(loop_thread, start), loop_thread


@pytest.mark.regression
async def test_a_local_token_checks_out_nothing_on_the_event_loop(
    checkouts, provider, db_session, cleanup
):
    user = _user(db_session, active=True)
    cleanup.append(user.username)
    headers = {"Authorization": f"Bearer {create_local_access_token(user)}"}
    response, on_loop, _ = await _users_me(headers, checkouts)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(user.id)
    assert on_loop == 0, f"{on_loop} checkouts on the event loop's thread"


@pytest.mark.regression
async def test_the_development_bypass_checks_out_nothing_on_the_event_loop(
    checkouts, provider, db_session
):
    provider.setattr(settings, "DEV_AUTH_BYPASS", True)
    existed = (
        db_session.query(User).filter(User.username == deps.DEV_BYPASS_USERNAME).count()
    )
    try:
        response, on_loop, _ = await _users_me({}, checkouts)
    finally:
        if not existed:
            db_session.rollback()
            db_session.query(User).filter(
                User.username == deps.DEV_BYPASS_USERNAME
            ).delete(synchronize_session=False)
            db_session.commit()
    assert response.status_code == 200, response.text
    assert response.json()["username"] == deps.DEV_BYPASS_USERNAME
    assert on_loop == 0, f"{on_loop} checkouts on the event loop's thread"


@pytest.mark.regression
async def test_a_linked_cognito_account_checks_out_nothing_on_the_event_loop(
    checkouts, cognito, db_session, cleanup
):
    sub = f"sub-{uuid.uuid4().hex}"
    user = _user(db_session, active=True, sub=sub)
    cleanup.append(user.username)
    cognito.payload = identity(user.username, sub, user.email, ["developer-group"])
    response, on_loop, loop_thread = await _users_me(COGNITO_BEARER, checkouts)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(user.id)
    assert on_loop == 0, f"{on_loop} checkouts on the event loop's thread"
    assert cognito.threads and loop_thread not in cognito.threads


@pytest.mark.regression
async def test_a_first_cognito_sign_in_checks_out_nothing_on_the_event_loop(
    checkouts, cognito, db_session, cleanup
):
    suffix = uuid.uuid4().hex[:8]
    username = f"checks_first_{suffix}"
    cleanup.append(username)
    cognito.payload = identity(
        username, f"sub-{uuid.uuid4().hex}", f"{username}@int.test", []
    )
    response, on_loop, loop_thread = await _users_me(COGNITO_BEARER, checkouts)
    assert response.status_code == 200, response.text
    assert response.json()["username"] == username
    assert on_loop == 0, f"{on_loop} checkouts on the event loop's thread"
    assert cognito.threads and loop_thread not in cognito.threads

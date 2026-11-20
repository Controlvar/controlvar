"""A token from before a password change is refused on every path that
resolves a local user (#501), and the check costs no query.

Driven through the real app as ``test_user_checks_on_every_path.py`` is: the
real ``deps.get_db`` (both session factories pointed at an engine on the test
database), the real ``get_current_user``. Each row signs in with a real local
token, then the account's stored hash changes with raw SQL (any write to
``hashed_password`` counts, not only the route's), and the same token is
sent again:

* ``GET /api/v1/users/me`` (``Depends(get_current_active_user)``): 401;
* ``GET /api/v1/auth/me`` (resolves the user itself): 401;
* ``GET /api/v1/experiments/`` (``read_scope.get_current_reader``): 401;
* the results WebSocket: closed with 4401 at connect.

The invitation preview, which takes an optional sign-in, is the modules'
``test_workspace_invite_preview.py``.

And the check reads the row the lookup already loaded: a signed-in request
runs as many SQL statements as before (one, the account's row).
"""

from __future__ import annotations

import threading
import uuid
from typing import List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.db import session as session_module
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
UNAUTHORISED = {"detail": "Could not validate credentials"}


class Statements:
    """Every SQL statement the test engine runs, in order."""

    def __init__(self) -> None:
        self.seen: List[str] = []
        self.lock = threading.Lock()

    def mark(self) -> int:
        with self.lock:
            return len(self.seen)

    def since(self, start: int) -> List[str]:
        with self.lock:
            return list(self.seen[start:])


@pytest.fixture
def statements(test_db, monkeypatch):
    """Point both session factories at an engine on the test database and
    record what it runs."""
    engine = create_engine(test_db.url, pool_size=4, max_overflow=4)
    seen = Statements()

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _statement(_conn, _cursor, statement, _params, _context, _many):
        with seen.lock:
            seen.seen.append(statement)

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(deps, "SessionLocal", factory)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    # backend/tests/conftest.py already turns the development sign-in off.
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    assert app.dependency_overrides == {}
    try:
        yield seen
    finally:
        engine.dispose()


@pytest.fixture
def account(db_session):
    """An active account with a password; deleted at the end."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"stale_{suffix}",
        email=f"stale_{suffix}@int.test",
        full_name="Stale Token",
        hashed_password="stored-hash-before-the-change",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    yield user
    db_session.rollback()
    db_session.query(User).filter(User.id == user.id).delete(synchronize_session=False)
    db_session.commit()


def _change_the_hash(db_session, user) -> None:
    db_session.execute(
        text("UPDATE users SET hashed_password = :value WHERE id = :id"),
        {"value": "stored-hash-after-the-change", "id": user.id},
    )
    db_session.commit()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


ROUTES = ["/api/v1/users/me", "/api/v1/auth/me", "/api/v1/experiments/"]


@pytest.mark.regression
def test_a_stale_token_is_refused_on_every_signed_in_path(
    statements, db_session, account
):
    token = create_local_access_token(account)
    with TestClient(app, raise_server_exceptions=False) as client:
        before = {path: client.get(path, headers=_bearer(token)) for path in ROUTES}
        _change_the_hash(db_session, account)
        after = {path: client.get(path, headers=_bearer(token)) for path in ROUTES}

    assert {p: r.status_code for p, r in before.items()} == dict.fromkeys(ROUTES, 200)
    assert {p: (r.status_code, r.json()) for p, r in after.items()} == dict.fromkeys(
        ROUTES, (401, UNAUTHORISED)
    )
    assert {p: r.headers.get("www-authenticate") for p, r in after.items()} == (
        dict.fromkeys(ROUTES, "Bearer")
    )


@pytest.mark.regression
def test_a_stale_token_is_refused_by_the_results_websocket(
    statements, db_session, account
):
    token = create_local_access_token(account)
    _change_the_hash(db_session, account)
    path = f"/api/v1/ws/experiments/{uuid.uuid4()}/results"
    with TestClient(app) as client:
        with client.websocket_connect(path, headers=_bearer(token)) as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
    assert closed.value.code == 4401, (
        f"the results WebSocket closed with {closed.value.code} for a token from "
        "before the password changed, want 4401"
    )


#: Statements a signed-in request runs, as measured before the stamp existed:
#: the account's row (which already selects ``hashed_password``), then for
#: ``/users/me`` whether two-step sign-in is on.
STATEMENTS = {"/api/v1/users/me": 2, "/api/v1/auth/me": 1}


@pytest.mark.parametrize("path", list(STATEMENTS))
def test_the_check_adds_no_statement(statements, account, path):
    token = create_local_access_token(account)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get(path, headers=_bearer(token))  # warm: the app's first request
        start = statements.mark()
        response = client.get(path, headers=_bearer(token))
    assert response.status_code == 200, response.text
    ran = statements.since(start)
    assert len(ran) == STATEMENTS[path], ran
    assert "users.hashed_password" in ran[0], ran[0]

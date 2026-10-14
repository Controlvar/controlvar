"""What the API writes about a request made with an API key, and revoking one.

The API is run as the image runs it (``uvicorn backend.app.main:app``, a
separate process, JSON logs) against the test database, with a real key, and
everything it writes is read:

* each request has an access line with the peer address, the method, the
  path, the status and a request id;
* nothing it writes contains the key, any part of it, or the key's id.

In-process, with ``caplog``: the one line that names a key's id is the
warning written when recording ``last_used_at`` fails, and nothing else the
application logs names the key. And deactivating the key's row, found by the
SHA-256 of the key as the API stores it, makes every request with the key 401.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.regression]

REPO = pathlib.Path(__file__).resolve().parents[4]
#: Routes a server SDK calls with its key.
SDK_ROUTES = (
    "/api/v1/feature-flags/user/key-log-user",
    "/api/v1/feature-flags/evaluate/key-log-flag?user_id=key-log-user",
)


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)


@pytest.fixture
def key(db_session):
    """A plaintext key, its row, and its owner, committed."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"keylog_{suffix}",
        email=f"keylog_{suffix}@keys.test",
        full_name="Key Log",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    plaintext = f"eptk_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    row = APIKey(
        key=hashlib.sha256(plaintext.encode()).hexdigest(),
        name=f"key-log-{suffix}",
        user_id=user.id,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return plaintext, row


@pytest.fixture
def client(db_session):
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _parts(plaintext: str, row: APIKey) -> list[str]:
    """The key, every 8-character piece of it, its hash and its id."""
    pieces = [plaintext[i : i + 8] for i in range(0, len(plaintext) - 7, 4)]
    return [plaintext, *pieces, row.key, str(row.id), str(row.id).replace("-", "")]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_running_api_logs_an_access_line_and_nothing_of_the_key(
    db_session, key, tmp_path
):
    plaintext, row = key
    url = db_session.get_bind().url
    port = _free_port()
    log_path = tmp_path / "uvicorn.log"
    log = log_path.open("w")
    env = dict(
        os.environ,
        LOG_FORMAT="json",
        POSTGRES_SERVER=url.host,
        POSTGRES_PORT=str(url.port),
        POSTGRES_USER=url.username,
        POSTGRES_PASSWORD=url.password or "",
        POSTGRES_DB=url.database,
        POSTGRES_SCHEMA="test_experimentation",
        DATABASE_URI=url.render_as_string(hide_password=False),
        AUTH_PROVIDER="local",
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--http",
            "h11",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    request_ids = []
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live", timeout=1
                )
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(
                        "uvicorn did not start:\n" + log_path.read_text()[-3000:]
                    )
                time.sleep(0.3)
        for route in SDK_ROUTES:
            request_id = uuid.uuid4().hex
            response = httpx.get(
                f"http://127.0.0.1:{port}{route}",
                headers={"X-API-Key": plaintext, "X-Request-ID": request_id},
                timeout=30,
            )
            assert response.status_code in (200, 404), (route, response.text)
            request_ids.append((route, request_id, response.status_code))
    finally:
        server.terminate()
        server.wait(timeout=20)
        log.close()

    written = log_path.read_text()
    # The key authenticated: its use was recorded.
    db_session.refresh(row)
    assert row.last_used_at is not None
    for part in _parts(plaintext, row):
        assert part not in written, f"{part!r} was logged"
    lines = [json.loads(x) for x in written.splitlines() if x.startswith("{")]
    access = [line for line in lines if line.get("logger") == "uvicorn.access"]
    for route, request_id, status in request_ids:
        (line,) = [a for a in access if a.get("request_id") == request_id]
        assert line["method"] == "GET"
        assert line["path"] == route.split("?")[0]
        assert f'"GET {route} HTTP/1.1" {status}' in line["event"]
        assert line["event"].startswith("127.0.0.1:"), line["event"]


def _logged(caplog, call) -> list[str]:
    """Every line the application logs while ``call`` runs: the standard
    library's records and loguru's (``backend/app/api/deps.py`` logs with
    loguru, which ``caplog`` does not see)."""
    lines: list[str] = []
    sink = loguru_logger.add(lambda message: lines.append(str(message)), level="TRACE")
    caplog.clear()
    try:
        with caplog.at_level(logging.DEBUG):
            call()
    finally:
        loguru_logger.remove(sink)
    return lines + [
        f"{r.levelname} {r.name} {r.getMessage()} {json.dumps(r.__dict__, default=str)}"
        for r in caplog.records
    ]


def test_only_a_failed_last_used_write_names_the_key_and_by_its_id(
    client, key, caplog, monkeypatch
):
    plaintext, row = key
    headers = {"X-API-Key": plaintext}

    def use():
        assert client.get(SDK_ROUTES[0], headers=headers).status_code == 200

    lines = _logged(caplog, use)
    assert lines, "nothing was captured, so nothing was checked"
    for line in lines:
        for part in _parts(plaintext, row):
            assert part not in line, (line, part)

    def refuse(self, db, **kwargs):
        raise OperationalError("UPDATE api_keys", {}, Exception("the write failed"))

    monkeypatch.setattr(APIKey, "update_last_used", refuse)
    lines = _logged(caplog, use)
    naming = [line for line in lines if str(row.id) in line]
    assert len(naming) == 1, naming
    assert "WARNING" in naming[0] and "backend.app.api.deps" in naming[0]
    assert f"Could not record API key use for key {row.id}" in naming[0]
    for line in lines:
        for part in _parts(plaintext, row)[:-2]:
            assert part not in line, (line, part)


def test_a_key_deactivated_by_its_hash_gets_401_everywhere(client, db_session, key):
    plaintext, row = key
    for route in SDK_ROUTES:
        assert client.get(route, headers={"X-API-Key": plaintext}).status_code in (
            200,
            404,
        )
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    deactivated = db_session.execute(
        text(
            "UPDATE test_experimentation.api_keys SET is_active = false "
            "WHERE key = :hash RETURNING id, name, user_id, scopes, last_used_at"
        ),
        {"hash": key_hash},
    ).all()
    db_session.commit()
    assert [r.id for r in deactivated] == [row.id]
    for route in (*SDK_ROUTES, "/api/v1/tracking/track"):
        method = client.post if route.endswith("track") else client.get
        kwargs = (
            {"json": {"user_id": "u", "event_type": "x"}}
            if route.endswith("track")
            else {}
        )
        response = method(route, headers={"X-API-Key": plaintext}, **kwargs)
        assert response.status_code == 401, (route, response.text)

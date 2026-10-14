"""The API against a database that has no schema yet.

On AWS the API's tasks never migrate (``RUN_MIGRATIONS=false``, pinned by
``infrastructure/tests/test_api_task_does_not_migrate.py``): the schema is
written by the migration task the Deploy workflow runs. Between the first
``cdk deploy`` and the first Deploy the API therefore serves an empty
database. This runs the API as the image does (``uvicorn
backend.app.main:app``, a separate process, JSON logs) against a freshly
created database with no tables, and reads everything it wrote:

* ``GET /health`` answers 200 and reports the database healthy: the check is
  ``SELECT 1``, which needs no table, so the load balancer's health checks
  pass;
* a sign-in, an SDK evaluation and a tracked event each answer 500, and each
  writes an ``error`` line to the log.
"""

from __future__ import annotations

import json
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
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.regression]

REPO = pathlib.Path(__file__).resolve().parents[4]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def empty_database(test_db):
    """A new database on the test server, with no schema and no table."""
    name = f"no_schema_{uuid.uuid4().hex[:12]}"
    server = test_db.url.set(database="postgres")
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield test_db.url.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _serve(url, log_path: pathlib.Path):
    port = _free_port()
    env = dict(
        os.environ,
        LOG_FORMAT="json",
        POSTGRES_SERVER=url.host,
        POSTGRES_PORT=str(url.port),
        POSTGRES_USER=url.username,
        POSTGRES_PASSWORD=url.password or "",
        POSTGRES_DB=url.database,
        DATABASE_URI=url.render_as_string(hide_password=False),
        # What the API task definition sets: the task never migrates or seeds.
        RUN_MIGRATIONS="false",
        SEED="",
    )
    log = log_path.open("w")
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
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1)
            break
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                server.terminate()
                log.close()
                pytest.fail("uvicorn did not start:\n" + log_path.read_text()[-3000:])
            time.sleep(0.3)
    return server, log, f"http://127.0.0.1:{port}"


def test_health_is_200_and_real_requests_are_500_until_the_schema_exists(
    empty_database, tmp_path
):
    with create_engine(empty_database).connect() as conn:
        tables = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
        ).scalar()
    assert tables == 0

    log_path = tmp_path / "uvicorn.log"
    server, log, base = _serve(empty_database, log_path)
    try:
        health = httpx.get(f"{base}/health", timeout=30)
        sign_in = httpx.post(
            f"{base}/api/v1/auth/login",
            json={"email": "first@example.com", "password": "not-the-password-1"},
            timeout=30,
        )
        key = {"X-API-Key": f"key-{uuid.uuid4().hex}"}
        evaluate = httpx.get(
            f"{base}/api/v1/feature-flags/evaluate/a-flag?user_id=u1",
            headers=key,
            timeout=30,
        )
        track = httpx.post(
            f"{base}/api/v1/tracking/track",
            json={"user_id": "u1", "event_type": "purchase"},
            headers=key,
            timeout=30,
        )
    finally:
        server.terminate()
        server.wait(timeout=20)
        log.close()

    assert health.status_code == 200, health.text
    assert health.json()["checks"]["database"]["status"] == "healthy"
    for response in (sign_in, evaluate, track):
        assert response.status_code == 500, (response.request.url, response.text)

    lines = []
    for line in log_path.read_text().splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    errors = [line for line in lines if line.get("level") == "error"]
    failed = {line.get("path") for line in errors}
    assert {
        "/api/v1/auth/login",
        "/api/v1/feature-flags/evaluate/a-flag",
        "/api/v1/tracking/track",
    } <= failed, errors
    assert any("does not exist" in str(line.get("exception")) for line in errors), (
        errors
    )

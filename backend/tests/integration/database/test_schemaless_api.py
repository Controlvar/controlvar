"""An API whose database has no schema yet: healthy to a probe, 500 to a request.

What a new AWS environment looks like between its first ``cdk deploy`` and its
first Deploy: the API tasks start without the bootstrap (``RUN_MIGRATIONS=false``)
against a database that answers but holds no tables. ``/health`` checks that the
database answers, so it says 200; a request that reads a table answers 500, and
the API writes an error line. Served here by uvicorn, as the task serves it,
against an empty schema of the test database.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest
from sqlalchemy import text

from backend.tests.integration.database import tree_profiles

pytestmark = [pytest.mark.integration]

#: A ceiling on the wait for the server to answer, never an assertion on time.
START_DEADLINE_SECONDS = 180


@pytest.fixture
def empty_schema(test_db):
    schema = f"schemaless_{uuid.uuid4().hex[:8]}"
    with test_db.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _request(url: str, data: bytes | None = None, headers: dict | None = None) -> int:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.mark.regression
def test_health_answers_200_while_requests_answer_500_and_are_logged(
    test_db, empty_schema, tmp_path
):
    with test_db.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"
            ),
            {"s": empty_schema},
        ).scalar()
    assert tables == 0

    port = _free_port()
    log = tmp_path / "api.log"
    server = tree_profiles.serve(
        tree_profiles.REPO_ROOT,
        empty_schema,
        port,
        log,
        {"ENVIRONMENT": "development", "LOG_FORMAT": "json"},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + START_DEADLINE_SECONDS
        health = 0
        while time.monotonic() < deadline and server.poll() is None:
            try:
                health = _request(f"{base}/health")
                break
            except OSError:
                time.sleep(0.5)
        assert health == 200, log.read_text("utf-8", "replace")[-4000:]
        login = _request(
            f"{base}/api/v1/auth/login",
            data=json.dumps({"email": "a@b.example", "password": "x"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        evaluate = _request(
            f"{base}/api/v1/feature-flags/evaluate/f?user_id=u",
            headers={"X-API-Key": "eptk_probe"},
        )
        tail = log.read_text("utf-8", "replace")[-4000:]
        assert (login, evaluate) == (500, 500), tail
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=30)

    errors = []
    for record in log.read_text("utf-8", "replace").splitlines():
        if record.startswith("{"):
            line = json.loads(record)
            if line.get("level") == "error":
                errors.append(line)
    assert errors, log.read_text("utf-8", "replace")[-4000:]
    assert any("does not exist" in json.dumps(e) for e in errors), errors[:3]

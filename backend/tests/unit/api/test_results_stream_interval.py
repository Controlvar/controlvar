"""When the results WebSocket sends a fresh snapshot without being asked.

``/api/v1/ws/experiments/{id}/results`` waits for a client message for
``RESULTS_STREAM_INTERVAL_SECONDS`` (30 unless the variable is set when the
API starts) and sends a fresh snapshot when the wait runs out. Each message
from the client, a ping or a refresh, starts a new wait of the full interval.

The socket runs through the app; only the snapshot is replaced, and the wait
is recorded (and shortened, so that no test waits 30 seconds): every wait
asks for the full interval, whatever came before it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1.endpoints import websocket_results
from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
PATCH_SNAPSHOT = (
    "backend.app.services.results_streaming_service."
    "ResultsStreamingService.get_live_snapshot"
)


@pytest.fixture(autouse=True)
def _dev_admin(monkeypatch):
    # The stream's protocol, not its sign-in (held in test_local_auth_path.py).
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _read_at_start(value):
    env = {
        k: v for k, v in os.environ.items() if k != "RESULTS_STREAM_INTERVAL_SECONDS"
    }
    if value is not None:
        env["RESULTS_STREAM_INTERVAL_SECONDS"] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend.app.api.v1.endpoints import websocket_results as w;"
            "print(w.RESULTS_STREAM_INTERVAL_SECONDS)",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return int(result.stdout.strip().splitlines()[-1])


def test_the_interval_is_30_seconds_unless_set_when_the_api_starts():
    assert _read_at_start(None) == 30
    assert _read_at_start("7") == 7


def test_every_wait_is_the_full_interval_and_a_message_starts_it_again(monkeypatch):
    waits = []
    real_wait_for = asyncio.wait_for

    async def recorded_wait_for(awaitable, timeout):
        waits.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout / 100.0)

    monkeypatch.setattr(
        websocket_results,
        "asyncio",
        SimpleNamespace(wait_for=recorded_wait_for, TimeoutError=asyncio.TimeoutError),
    )
    # Set after the API started: not read again.
    monkeypatch.setenv("RESULTS_STREAM_INTERVAL_SECONDS", "5")
    snapshots = []

    def snapshot(experiment_id):
        snapshots.append(experiment_id)
        return {"event": "results_update", "n": len(snapshots)}

    with patch(PATCH_SNAPSHOT, side_effect=snapshot):
        client = TestClient(app)
        with client.websocket_connect("/api/v1/ws/experiments/exp-int/results") as ws:
            assert ws.receive_json() == {"event": "results_update", "n": 1}
            ws.send_json({"action": "ping"})
            assert ws.receive_json() == {"event": "pong"}
            ws.send_json({"action": "refresh"})
            assert ws.receive_json() == {"event": "results_update", "n": 2}
            # Nothing from the client: the wait runs out and a fresh one comes.
            assert ws.receive_json() == {"event": "results_update", "n": 3}

    interval = float(websocket_results.RESULTS_STREAM_INTERVAL_SECONDS)
    assert interval == 30.0
    assert len(waits) >= 3, waits
    assert all(wait == interval for wait in waits), waits

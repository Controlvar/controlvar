"""The dashboard's demo refusal fixture is the API's refusal.

``frontend/src/tests/fixtures/demo-read-only-refusal.json`` is what the
dashboard's tests feed through ``apiFetch`` as "the API's answer to a write it
refuses in demo mode". This test sends one refused write to the real
application with ``DEMO_MODE`` on and holds the answer (status and parsed body)
to that file, so the two cannot drift: a reworded message on either side fails
here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = (
    REPO_ROOT
    / "frontend"
    / "src"
    / "tests"
    / "fixtures"
    / "demo-read-only-refusal.json"
)


def test_the_dashboards_refusal_fixture_is_what_the_api_answers(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    client = TestClient(
        app, base_url="http://demo.example.com", raise_server_exceptions=False
    )
    response = client.post("/api/v1/experiments/", json={})

    print("API answered:", response.status_code, response.json())
    print("fixture holds:", fixture["status"], fixture["body"])
    assert response.status_code == fixture["status"] == 403
    assert response.json() == fixture["body"]

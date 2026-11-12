"""``GET /api/v1/auth/config`` in demo mode.

* ``DEMO_MODE`` off: unchanged, ``{"provider": "local"}``.
* On, with ``DEMO_ACCOUNT_EMAIL`` and ``DEMO_ACCOUNT_PASSWORD``: adds
  ``"demo_mode": true`` and ``"demo_account": {"email", "password"}``, the
  shared account a visitor signs in with.
* On, without an account: adds ``"demo_mode": true`` only.

The route takes no credentials and needs no database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit]

CONFIG = "/api/v1/auth/config"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", "")
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", "")
    return TestClient(app)


def test_off_the_answer_is_unchanged(client):
    response = client.get(CONFIG)
    assert response.status_code == 200
    assert response.json() == {"provider": "local"}


def test_on_with_an_account_it_publishes_the_account(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", "viewer@demo.com")
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", "Demo1234!")
    response = client.get(CONFIG)
    assert response.status_code == 200
    assert response.json() == {
        "provider": "local",
        "demo_mode": True,
        "demo_account": {"email": "viewer@demo.com", "password": "Demo1234!"},
    }
    # The JSON boolean, not 1 or "true": a client turns demo mode on for
    # exactly that value (Python's == would accept 1).
    assert response.json()["demo_mode"] is True
    assert '"demo_mode":true' in response.text


def test_on_without_an_account_it_says_only_demo_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    response = client.get(CONFIG)
    assert response.status_code == 200
    assert response.json() == {"provider": "local", "demo_mode": True}
    assert response.json()["demo_mode"] is True


def test_an_account_without_demo_mode_is_never_published(client, monkeypatch):
    """Start-up refuses this configuration; the route holds the line too."""
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", "viewer@demo.com")
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", "Demo1234!")
    assert client.get(CONFIG).json() == {"provider": "local"}

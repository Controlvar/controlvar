"""Integration configurations: activation, deletion, and the webhook checks (#1142).

A new configuration is inactive, and only an active one answers webhooks.
DELETE removes the row; a PUT of ``{"is_active": false}`` stops it and keeps
what it stores. The shared-secret header carries no nonce, so the same
delivery is accepted again; both checks compare with ``hmac.compare_digest``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.user import User, UserRole
from modules.backend.app.api.v1.endpoints import integrations as endpoint
from modules.backend.app.models.integration_config import (
    IntegrationConfig,
    IntegrationType,
)
from modules.backend.app.schemas.integration import IntegrationConfigCreate
from modules.backend.app.services.integrations import webhook_auth
from modules.backend.app.services.integrations.github_service import GitHubService
from modules.backend.app.services.integrations.jira_service import JiraService

SECRET = "jira-shared-secret"
STORED_CONFIG = {
    "base_url": "https://jira.example.com",
    "email": "bot@example.com",
    "api_token": "t",
    "webhook_secret": SECRET,
}


def _admin():
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.ADMIN
    return user


def _config(is_active=True):
    cfg = MagicMock(spec=IntegrationConfig)
    cfg.id = uuid.uuid4()
    cfg.integration_type = IntegrationType.JIRA
    cfg.is_active = is_active
    cfg.encrypted_config = dict(STORED_CONFIG)
    cfg.last_sync_at = None
    cfg.last_error = None
    cfg.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cfg.updated_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return cfg


@pytest.fixture
def db():
    session = MagicMock()
    user = _admin()
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: session
    yield session
    app.dependency_overrides.clear()


def test_a_new_configuration_is_inactive_and_only_an_active_one_answers(db):
    assert IntegrationConfigCreate(integration_type="jira").is_active is False
    assert IntegrationConfig.__table__.c.is_active.default.arg is False

    endpoint._active_config(db, IntegrationType.JIRA)
    conditions = [str(c) for c in db.query.return_value.filter.call_args.args]
    assert any("is_active IS true" in c for c in conditions), conditions

    # With no active configuration the lookup finds nothing, and the
    # delivery is refused even with the right secret.
    db.query.return_value.filter.return_value.first.return_value = None
    response = TestClient(app).post(
        "/api/v1/integrations/webhooks/jira",
        json={"webhookEvent": "jira:issue_created"},
        headers={webhook_auth.SHARED_SECRET_HEADER: SECRET},
    )
    assert response.status_code == 401


def test_delete_removes_the_row_and_a_put_to_inactive_keeps_the_stored_config(db):
    config = _config()
    db.query.return_value.filter.return_value.first.return_value = config
    client = TestClient(app)

    stopped = client.put("/api/v1/integrations/jira", json={"is_active": False})
    assert stopped.status_code == 200, stopped.text
    assert config.is_active is False
    assert config.encrypted_config == STORED_CONFIG
    db.delete.assert_not_called()

    deleted = client.delete("/api/v1/integrations/jira")
    assert deleted.status_code == 204
    db.delete.assert_called_once_with(config)


def test_a_shared_secret_delivery_is_accepted_again_when_replayed(db):
    db.query.return_value.filter.return_value.first.return_value = _config()
    client = TestClient(app)
    with patch.object(JiraService, "parse_webhook_event") as parse:
        for _ in range(2):
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                json={"webhookEvent": "jira:issue_created", "id": "1"},
                headers={webhook_auth.SHARED_SECRET_HEADER: SECRET},
            )
            assert response.status_code == 200, response.text
    assert parse.call_count == 2


def test_both_comparisons_are_constant_time():
    body = b'{"a": 1}'
    with patch.object(
        webhook_auth.hmac, "compare_digest", wraps=webhook_auth.hmac.compare_digest
    ) as compare:
        assert webhook_auth.verify_signature(
            SECRET, body, webhook_auth.expected_signature(SECRET, body)
        )
        assert webhook_auth.verify_shared_secret(SECRET, SECRET)
        assert not webhook_auth.verify_shared_secret(SECRET, SECRET + "x")
    assert compare.call_count == 3


def test_the_github_client_sends_a_bearer_token():
    service = GitHubService("ghp_token", "acme", "app")
    assert service._headers()["Authorization"] == "Bearer ghp_token"


def test_an_unexpected_failure_is_a_500(db):
    db.query.side_effect = RuntimeError("database went away")
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/integrations"
    )
    assert response.status_code == 500


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-GitHub-Event": "pull_request"},
        {"X-GitHub-Delivery": "72d3162e", "Content-Type": "text/plain"},
    ],
    ids=["no-event-header", "event-header", "delivery-and-any-content-type"],
)
def test_a_signed_github_delivery_needs_only_its_signature(db, headers):
    config = _config()
    config.integration_type = IntegrationType.GITHUB
    config.encrypted_config = {
        "token": "t",
        "repo_owner": "o",
        "repo_name": "r",
        "webhook_secret": SECRET,
    }
    db.query.return_value.filter.return_value.first.return_value = config
    body = b'{"action": "opened", "number": 42}'
    signature = webhook_auth.expected_signature(SECRET, body)
    with patch.object(GitHubService, "parse_webhook_event") as parse:
        response = TestClient(app).post(
            "/api/v1/integrations/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": signature, **headers},
        )
    assert response.status_code == 200, response.text
    parse.assert_called_once()
    assert parse.call_args.args[0] == headers.get("X-GitHub-Event", "")


def test_the_salesforce_client_reads_no_stored_access_token():
    from modules.backend.app.services.integrations.salesforce_service import (
        SalesforceService,
    )

    config = _config()
    config.encrypted_config = {
        "instance_url": "https://acme.my.salesforce.com",
        "client_id": "cid",
        "client_secret": "cs",
        "access_token": "stored-token",
    }
    service = SalesforceService.from_config(config)
    assert service._access_token is None
    assert "stored-token" not in repr(vars(service))

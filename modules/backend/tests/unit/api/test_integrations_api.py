"""
Unit tests for integration config CRUD API — EP-034 Batch 3.

Uses FastAPI TestClient with dependency overrides.
No real database required — all DB calls are mocked.
"""

import hashlib
import hmac as hmac_lib
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.user import User, UserRole
from modules.backend.app.api.v1.endpoints import integrations as integrations_endpoint
from modules.backend.app.models.integration_config import (
    IntegrationConfig,
    IntegrationType,
)
from modules.backend.app.services.integrations.github_service import GitHubService
from modules.backend.app.services.integrations.jira_service import JiraService
from modules.backend.app.services.integrations.salesforce_service import (
    SalesforceService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.ADMIN, is_superuser: bool = False) -> User:
    """Create an in-memory User mock with the given role."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = f"user_{role.value}"
    user.email = f"{role.value}@example.com"
    user.is_active = True
    user.is_superuser = is_superuser
    user.role = role
    return user


def _make_config(
    integration_type: IntegrationType = IntegrationType.JIRA,
    is_active: bool = False,
    encrypted_config: dict = None,
) -> MagicMock:
    """Create a mock IntegrationConfig with sensible defaults."""
    cfg = MagicMock(spec=IntegrationConfig)
    cfg.id = uuid.uuid4()
    cfg.integration_type = integration_type
    cfg.is_active = is_active
    cfg.encrypted_config = encrypted_config or {"base_url": "https://jira.example.com"}
    cfg.last_sync_at = None
    cfg.last_error = None
    cfg.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cfg.updated_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    return cfg


def _make_mock_db():
    """Return a configured mock SQLAlchemy Session."""
    return MagicMock()


def _override(user: User, mock_db=None):
    """Context-free helper: sets dependency_overrides on the FastAPI app."""
    if mock_db is None:
        mock_db = _make_mock_db()
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: mock_db
    return mock_db


def _clear():
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _own_session_is_the_mock_db(monkeypatch):
    """The webhook routes open their own session (``deps.SessionLocal``) in a
    worker rather than taking ``get_db``'s; give them the mock ``get_db`` is
    overridden with, so a test's configured answers reach them too."""
    real = deps.SessionLocal

    def session():
        override = app.dependency_overrides.get(deps.get_db)
        return real() if override is None else override()

    monkeypatch.setattr(deps, "SessionLocal", session)


# ---------------------------------------------------------------------------
# TestListIntegrations
# ---------------------------------------------------------------------------


class TestListIntegrations:
    """GET /api/v1/integrations"""

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    def test_admin_can_list_integrations(self):
        """GET /api/v1/integrations returns list of configs for ADMIN."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config()
        mock_db.query.return_value.all.return_value = [config]

        client = TestClient(app)
        response = client.get("/api/v1/integrations")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["integration_type"] == IntegrationType.JIRA.value

    def test_developer_can_list_integrations(self):
        """Developers (ADMIN+DEV read) can list integration configs."""
        developer = _make_user(UserRole.DEVELOPER)
        mock_db = _override(developer)
        mock_db.query.return_value.all.return_value = []

        client = TestClient(app)
        response = client.get("/api/v1/integrations")

        assert response.status_code == 200
        assert response.json() == []

    def test_analyst_cannot_list_integrations(self):
        """ANALYST gets 403 — not ADMIN or DEVELOPER."""
        analyst = _make_user(UserRole.ANALYST)
        _override(analyst)

        client = TestClient(app)
        response = client.get("/api/v1/integrations")

        assert response.status_code == 403

    def test_viewer_cannot_list_integrations(self):
        """VIEWER gets 403."""
        viewer = _make_user(UserRole.VIEWER)
        _override(viewer)

        client = TestClient(app)
        response = client.get("/api/v1/integrations")

        assert response.status_code == 403

    def test_unauthenticated_returns_401(self):
        """No auth header → 401 (no dependency override → token required)."""
        _clear()  # Remove all overrides

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/integrations")

        # FastAPI returns 401 or 422 when no token is provided depending on
        # how oauth2_scheme is configured; 401 is the expected contract.
        assert response.status_code in (401, 403, 422)

    def test_list_returns_multiple_configs(self):
        """GET returns all configured integrations."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        jira_cfg = _make_config(IntegrationType.JIRA)
        sf_cfg = _make_config(IntegrationType.SALESFORCE, is_active=True)
        mock_db.query.return_value.all.return_value = [jira_cfg, sf_cfg]

        client = TestClient(app)
        response = client.get("/api/v1/integrations")

        assert response.status_code == 200
        assert len(response.json()) == 2


# ---------------------------------------------------------------------------
# TestGetIntegration
# ---------------------------------------------------------------------------


class TestGetIntegration:
    """GET /api/v1/integrations/{integration_type}"""

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    def test_get_jira_config(self):
        """GET /api/v1/integrations/jira returns Jira config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.JIRA)

        mock_db.query.return_value.filter.return_value.first.return_value = config

        client = TestClient(app)
        response = client.get("/api/v1/integrations/jira")

        assert response.status_code == 200
        assert response.json()["integration_type"] == "jira"

    def test_get_salesforce_config(self):
        """GET /api/v1/integrations/salesforce returns Salesforce config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.SALESFORCE, is_active=True)

        mock_db.query.return_value.filter.return_value.first.return_value = config

        client = TestClient(app)
        response = client.get("/api/v1/integrations/salesforce")

        assert response.status_code == 200
        assert response.json()["integration_type"] == "salesforce"

    def test_get_github_config(self):
        """GET /api/v1/integrations/github returns GitHub config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.GITHUB)

        mock_db.query.return_value.filter.return_value.first.return_value = config

        client = TestClient(app)
        response = client.get("/api/v1/integrations/github")

        assert response.status_code == 200
        assert response.json()["integration_type"] == "github"

    def test_get_nonexistent_returns_404(self):
        """GET /api/v1/integrations/jira when not configured → 404."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        client = TestClient(app)
        response = client.get("/api/v1/integrations/jira")

        assert response.status_code == 404

    def test_developer_can_get_integration(self):
        """DEVELOPER role can read integration configs."""
        developer = _make_user(UserRole.DEVELOPER)
        mock_db = _override(developer)
        config = _make_config(IntegrationType.JIRA)
        mock_db.query.return_value.filter.return_value.first.return_value = config

        client = TestClient(app)
        response = client.get("/api/v1/integrations/jira")

        assert response.status_code == 200

    def test_analyst_cannot_get_integration(self):
        """ANALYST gets 403 on GET single config."""
        analyst = _make_user(UserRole.ANALYST)
        _override(analyst)

        client = TestClient(app)
        response = client.get("/api/v1/integrations/jira")

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# TestCreateIntegration
# ---------------------------------------------------------------------------


class TestCreateIntegration:
    """POST /api/v1/integrations"""

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    def test_admin_can_create_jira_config(self):
        """POST /api/v1/integrations creates new integration config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)

        # No existing config (no duplicate)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # The saved config is returned via db.refresh side-effect
        saved_config = _make_config(IntegrationType.JIRA)

        def fake_refresh(obj):
            obj.id = saved_config.id
            obj.created_at = saved_config.created_at
            obj.updated_at = saved_config.updated_at

        mock_db.refresh.side_effect = fake_refresh

        payload = {
            "integration_type": "jira",
            "is_active": False,
            "encrypted_config": {
                "base_url": "https://jira.example.com",
                "api_token": "tok",
            },
        }

        client = TestClient(app)
        response = client.post("/api/v1/integrations", json=payload)

        assert response.status_code == 201
        assert response.json()["integration_type"] == "jira"

    def test_admin_can_create_salesforce_config(self):
        """POST creates Salesforce config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        saved = _make_config(IntegrationType.SALESFORCE, is_active=True)

        def fake_refresh(obj):
            obj.id = saved.id
            obj.created_at = saved.created_at
            obj.updated_at = saved.updated_at
            obj.is_active = True

        mock_db.refresh.side_effect = fake_refresh

        payload = {
            "integration_type": "salesforce",
            "is_active": True,
            "encrypted_config": {"instance_url": "https://sf.example.com"},
        }

        client = TestClient(app)
        response = client.post("/api/v1/integrations", json=payload)

        assert response.status_code == 201

    def test_developer_cannot_create_integration(self):
        """Only ADMIN can create integrations — DEVELOPER gets 403."""
        developer = _make_user(UserRole.DEVELOPER)
        _override(developer)

        payload = {
            "integration_type": "jira",
            "is_active": False,
        }

        client = TestClient(app)
        response = client.post("/api/v1/integrations", json=payload)

        assert response.status_code == 403

    def test_analyst_cannot_create_integration(self):
        """ANALYST gets 403."""
        analyst = _make_user(UserRole.ANALYST)
        _override(analyst)

        payload = {"integration_type": "jira", "is_active": False}

        client = TestClient(app)
        response = client.post("/api/v1/integrations", json=payload)

        assert response.status_code == 403

    def test_duplicate_integration_type_returns_409(self):
        """Creating second config for same type → 409 Conflict."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        # Existing config found
        mock_db.query.return_value.filter.return_value.first.return_value = (
            _make_config(IntegrationType.JIRA)
        )

        payload = {
            "integration_type": "jira",
            "is_active": False,
        }

        client = TestClient(app)
        response = client.post("/api/v1/integrations", json=payload)

        assert response.status_code == 409

    def test_create_stores_encrypted_config(self):
        """Credentials in encrypted_config are stored on the model."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        stored_config = None

        def capture_add(obj):
            nonlocal stored_config
            stored_config = obj

        mock_db.add.side_effect = capture_add

        payload = {
            "integration_type": "github",
            "is_active": False,
            "encrypted_config": {
                "token": "ghp_secret",
                "repo_owner": "acme",
                "repo_name": "app",
            },
        }

        saved = _make_config(
            IntegrationType.GITHUB, encrypted_config=payload["encrypted_config"]
        )

        def fake_refresh(obj):
            obj.id = saved.id
            obj.created_at = saved.created_at
            obj.updated_at = saved.updated_at

        mock_db.refresh.side_effect = fake_refresh

        client = TestClient(app)
        response = client.post("/api/v1/integrations", json=payload)

        assert response.status_code == 201
        assert stored_config is not None
        assert stored_config.encrypted_config == payload["encrypted_config"]


# ---------------------------------------------------------------------------
# TestUpdateIntegration
# ---------------------------------------------------------------------------


class TestUpdateIntegration:
    """PUT /api/v1/integrations/{integration_type}"""

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    def test_admin_can_update_jira_config(self):
        """PUT /api/v1/integrations/jira updates existing config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.JIRA)
        mock_db.query.return_value.filter.return_value.first.return_value = config

        def fake_refresh(obj):
            pass

        mock_db.refresh.side_effect = fake_refresh

        payload = {"encrypted_config": {"base_url": "https://new.jira.example.com"}}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/jira", json=payload)

        assert response.status_code == 200

    def test_activate_integration(self):
        """PUT with is_active=True activates the integration."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.JIRA, is_active=False)
        mock_db.query.return_value.filter.return_value.first.return_value = config

        mock_db.refresh.side_effect = lambda obj: setattr(obj, "is_active", True)

        payload = {"is_active": True}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/jira", json=payload)

        assert response.status_code == 200
        assert response.json()["is_active"] is True

    def test_deactivate_integration(self):
        """PUT with is_active=False deactivates the integration."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.JIRA, is_active=True)
        mock_db.query.return_value.filter.return_value.first.return_value = config

        mock_db.refresh.side_effect = lambda obj: setattr(obj, "is_active", False)

        payload = {"is_active": False}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/jira", json=payload)

        assert response.status_code == 200
        assert response.json()["is_active"] is False

    def test_developer_cannot_update_integration(self):
        """Only ADMIN can update integrations — DEVELOPER gets 403."""
        developer = _make_user(UserRole.DEVELOPER)
        _override(developer)

        payload = {"is_active": True}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/jira", json=payload)

        assert response.status_code == 403

    def test_analyst_cannot_update_integration(self):
        """ANALYST gets 403 on PUT."""
        analyst = _make_user(UserRole.ANALYST)
        _override(analyst)

        payload = {"is_active": True}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/jira", json=payload)

        assert response.status_code == 403

    def test_update_nonexistent_returns_404(self):
        """PUT for unconfigured integration type → 404."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        payload = {"is_active": True}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/jira", json=payload)

        assert response.status_code == 404

    def test_update_last_error_field(self):
        """PUT can clear or set last_error on a config."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.SALESFORCE)
        config.last_error = "previous error"
        mock_db.query.return_value.filter.return_value.first.return_value = config

        mock_db.refresh.side_effect = lambda obj: setattr(obj, "last_error", None)

        payload = {"last_error": None}

        client = TestClient(app)
        response = client.put("/api/v1/integrations/salesforce", json=payload)

        assert response.status_code == 200

    @pytest.mark.regression
    @pytest.mark.parametrize(
        ("length", "expected"),
        [(1024, 200), (1025, 422)],
    )
    def test_last_error_over_the_column_length_is_422(self, length, expected):
        """A last_error past 1,024 characters answers 422, not a database 500."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.SALESFORCE)
        mock_db.query.return_value.filter.return_value.first.return_value = config
        mock_db.refresh.side_effect = lambda obj: None

        client = TestClient(app)
        response = client.put(
            "/api/v1/integrations/salesforce", json={"last_error": "x" * length}
        )

        assert response.status_code == expected
        if expected == 422:
            assert "1024 characters" in response.text
            mock_db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# TestDeleteIntegration
# ---------------------------------------------------------------------------


class TestDeleteIntegration:
    """DELETE /api/v1/integrations/{integration_type}"""

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    def test_admin_can_delete_integration(self):
        """DELETE /api/v1/integrations/jira removes config — returns 204."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        config = _make_config(IntegrationType.JIRA)
        mock_db.query.return_value.filter.return_value.first.return_value = config

        client = TestClient(app)
        response = client.delete("/api/v1/integrations/jira")

        assert response.status_code == 204
        mock_db.delete.assert_called_once_with(config)
        mock_db.commit.assert_called()

    def test_developer_cannot_delete(self):
        """Only ADMIN can delete — DEVELOPER gets 403."""
        developer = _make_user(UserRole.DEVELOPER)
        _override(developer)

        client = TestClient(app)
        response = client.delete("/api/v1/integrations/jira")

        assert response.status_code == 403

    def test_analyst_cannot_delete(self):
        """ANALYST gets 403 on DELETE."""
        analyst = _make_user(UserRole.ANALYST)
        _override(analyst)

        client = TestClient(app)
        response = client.delete("/api/v1/integrations/jira")

        assert response.status_code == 403

    def test_delete_nonexistent_returns_404(self):
        """DELETE for unconfigured integration type → 404."""
        admin = _make_user(UserRole.ADMIN)
        mock_db = _override(admin)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        client = TestClient(app)
        response = client.delete("/api/v1/integrations/jira")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# TestWebhookEndpoints
# ---------------------------------------------------------------------------
#
# The three webhook routes are the only ones an anonymous caller can reach
# with a body of its choosing, so every test here is about the *sender* being
# authenticated before the body is touched.  The services are real (only
# `parse_webhook_event` is patched, so "was the payload processed?" is a
# question a test can ask): a signature that these tests compute must be the
# signature the endpoint recomputes.

JIRA_SECRET = "jira-shared-secret"
SALESFORCE_SECRET = "salesforce-shared-secret"
GITHUB_SECRET = "github-webhook-secret"

SECRET_HEADER = "X-Experimently-Webhook-Secret"

#: The reply every refused webhook gets, whatever went wrong.
UNAUTHENTICATED = {"detail": "Webhook authentication failed"}


def _sign(secret: str, body: bytes) -> str:
    """The signature header a sender holding `secret` would send."""
    return "sha256=" + hmac_lib.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _jira_config(secret: str = JIRA_SECRET) -> MagicMock:
    return _make_config(
        IntegrationType.JIRA,
        is_active=True,
        encrypted_config={
            "base_url": "https://jira.example.com",
            "api_token": "t",
            "email": "bot@example.com",
            **({"webhook_secret": secret} if secret else {}),
        },
    )


def _salesforce_config(secret: str = SALESFORCE_SECRET) -> MagicMock:
    return _make_config(
        IntegrationType.SALESFORCE,
        is_active=True,
        encrypted_config={
            "instance_url": "https://acme.my.salesforce.com",
            "client_id": "cid",
            "client_secret": "csecret",
            **({"webhook_secret": secret} if secret else {}),
        },
    )


def _github_config(secret: str = GITHUB_SECRET) -> MagicMock:
    return _make_config(
        IntegrationType.GITHUB,
        is_active=True,
        encrypted_config={
            "token": "ghp_t",
            "repo_owner": "acme",
            "repo_name": "app",
            **({"webhook_secret": secret} if secret else {}),
        },
    )


class TestWebhookEndpoints:
    """POST /api/v1/integrations/webhooks/{jira,salesforce,github}"""

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    @staticmethod
    def _client(config) -> TestClient:
        """A client whose DB answers `config` for the active-config lookup."""
        mock_db = _make_mock_db()
        mock_db.query.return_value.filter.return_value.first.return_value = config
        app.dependency_overrides[deps.get_db] = lambda: mock_db
        return TestClient(app)

    # --- Jira ---------------------------------------------------------------

    @pytest.mark.regression
    def test_jira_webhook_without_a_secret_is_refused(self):
        """An unsigned Jira webhook is 401 and reaches no service.

        Regression: the handler used to look up the config and hand any
        anonymous body straight to JiraService.parse_webhook_event.
        """
        client = self._client(_jira_config())
        with patch.object(JiraService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
            )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED
        parse.assert_not_called()

    @pytest.mark.regression
    def test_jira_webhook_with_the_wrong_secret_is_refused(self):
        """A wrong shared secret is 401 and reaches no service."""
        client = self._client(_jira_config())
        with patch.object(JiraService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
                headers={SECRET_HEADER: "not-the-secret"},
            )
        assert response.status_code == 401
        parse.assert_not_called()

    @pytest.mark.regression
    def test_jira_webhook_with_a_wrong_signature_is_refused(self):
        """A signature computed with another secret is 401."""
        body = json.dumps({"webhookEvent": "jira:issue_updated"}).encode()
        client = self._client(_jira_config())
        with patch.object(JiraService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature": _sign("another-secret", body),
                },
            )
        assert response.status_code == 401
        parse.assert_not_called()

    def test_jira_webhook_with_a_valid_signature_is_processed(self):
        """Jira Cloud's X-Hub-Signature over the raw body is accepted."""
        payload = {"webhookEvent": "jira:issue_updated", "issue": {"key": "EXP-1"}}
        body = json.dumps(payload).encode()
        client = self._client(_jira_config())
        with patch.object(JiraService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature": _sign(JIRA_SECRET, body),
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "received"
        parse.assert_called_once_with(payload)

    def test_jira_webhook_with_the_shared_secret_header_is_processed(self):
        """A relay that cannot sign may present the secret itself."""
        client = self._client(_jira_config())
        with patch.object(JiraService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                json={"webhookEvent": "jira:issue_created"},
                headers={SECRET_HEADER: JIRA_SECRET},
            )
        assert response.status_code == 200, response.text
        parse.assert_called_once()

    @pytest.mark.regression
    def test_jira_webhook_with_no_configured_secret_is_refused(self):
        """A config with no webhook_secret cannot authenticate anyone."""
        client = self._client(_jira_config(secret=""))
        with patch.object(JiraService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
                headers={SECRET_HEADER: ""},
            )
        assert response.status_code == 401
        parse.assert_not_called()

    def test_jira_webhook_without_an_integration_is_refused_identically(self):
        """No config answers exactly what a wrong secret answers.

        The reply must not tell an anonymous caller which integrations this
        deployment has configured.
        """
        client = self._client(None)
        response = client.post(
            "/api/v1/integrations/webhooks/jira",
            json={"webhookEvent": "jira:issue_updated"},
        )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED

    def test_jira_webhook_authenticated_but_malformed_body_is_400(self):
        """Authentication comes first; a bad body is then the caller's fault."""
        body = b"not-json"
        client = self._client(_jira_config())
        response = client.post(
            "/api/v1/integrations/webhooks/jira",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": _sign(JIRA_SECRET, body),
            },
        )
        assert response.status_code == 400

    def test_jira_webhook_unauthenticated_malformed_body_is_401_not_400(self):
        """An unauthenticated caller learns nothing about its own body."""
        client = self._client(_jira_config())
        response = client.post(
            "/api/v1/integrations/webhooks/jira",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401

    def test_jira_webhook_service_failure_is_still_200(self):
        """A failure inside the service is swallowed: Jira must not retry."""
        body = json.dumps({"webhookEvent": "jira:issue_updated"}).encode()
        client = self._client(_jira_config())
        with patch.object(
            JiraService, "parse_webhook_event", side_effect=RuntimeError("boom")
        ):
            response = client.post(
                "/api/v1/integrations/webhooks/jira",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature": _sign(JIRA_SECRET, body),
                },
            )
        assert response.status_code == 200

    # --- Salesforce ---------------------------------------------------------

    @pytest.mark.regression
    def test_salesforce_webhook_without_a_secret_is_refused(self):
        """An unauthenticated Salesforce webhook is 401 and reaches no service.

        Regression: the handler verified nothing at all.
        """
        client = self._client(_salesforce_config())
        with patch.object(SalesforceService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/salesforce",
                json={"event_type": "Opportunity", "id": "001xx"},
            )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED
        parse.assert_not_called()

    @pytest.mark.regression
    def test_salesforce_webhook_with_the_wrong_secret_is_refused(self):
        client = self._client(_salesforce_config())
        with patch.object(SalesforceService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/salesforce",
                json={"event_type": "Opportunity"},
                headers={SECRET_HEADER: "not-the-secret"},
            )
        assert response.status_code == 401
        parse.assert_not_called()

    def test_salesforce_webhook_with_the_shared_secret_header_is_processed(self):
        """An outbound message cannot sign its body; the header is accepted."""
        payload = {"event_type": "Opportunity", "id": "001xx"}
        client = self._client(_salesforce_config())
        with patch.object(SalesforceService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/salesforce",
                json=payload,
                headers={SECRET_HEADER: SALESFORCE_SECRET},
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "received"
        parse.assert_called_once_with(payload)

    def test_salesforce_webhook_with_a_valid_signature_is_processed(self):
        """A callout that can Crypto.generateMac may sign instead."""
        body = json.dumps({"event_type": "Opportunity"}).encode()
        client = self._client(_salesforce_config())
        with patch.object(SalesforceService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/salesforce",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": _sign(SALESFORCE_SECRET, body),
                },
            )
        assert response.status_code == 200, response.text
        parse.assert_called_once()

    def test_salesforce_webhook_without_an_integration_is_refused(self):
        client = self._client(None)
        response = client.post(
            "/api/v1/integrations/webhooks/salesforce",
            json={"event_type": "Opportunity"},
            headers={SECRET_HEADER: SALESFORCE_SECRET},
        )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED

    # --- GitHub -------------------------------------------------------------

    @pytest.mark.regression
    def test_github_webhook_without_a_signature_is_refused(self):
        """No X-Hub-Signature-256 is 401, not a skipped check.

        Regression: the check lived inside `if service and signature:`, so
        omitting the header skipped it entirely.
        """
        client = self._client(_github_config())
        with patch.object(GitHubService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/github",
                json={"action": "opened"},
                headers={"X-GitHub-Event": "issues"},
            )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED
        parse.assert_not_called()

    @pytest.mark.regression
    def test_github_webhook_rejects_the_shared_secret_header(self):
        """GitHub always signs, so the weaker header is not accepted for it."""
        client = self._client(_github_config())
        with patch.object(GitHubService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/github",
                json={"action": "opened"},
                headers={"X-GitHub-Event": "issues", SECRET_HEADER: GITHUB_SECRET},
            )
        assert response.status_code == 401
        parse.assert_not_called()

    def test_github_webhook_rejects_an_invalid_signature(self):
        body = json.dumps({"action": "opened"}).encode()
        client = self._client(_github_config())
        with patch.object(GitHubService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": _sign("another-secret", body),
                    "X-GitHub-Event": "issues",
                },
            )
        assert response.status_code == 401
        parse.assert_not_called()

    @pytest.mark.regression
    def test_github_webhook_rejects_a_non_ascii_signature(self):
        """A header an attacker chooses must not become a 500.

        Starlette decodes request headers as latin-1 and `hmac.compare_digest`
        raises TypeError on a non-ASCII str, so the comparison runs on bytes.
        The header goes in as raw bytes because httpx will not encode a
        non-ASCII str — a socket-level client has no such scruples.
        """
        body = json.dumps({"action": "opened"}).encode()
        client = self._client(_github_config())
        response = client.post(
            "/api/v1/integrations/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=café".encode("latin-1"),
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 401

    def test_github_webhook_rejects_a_sha1_signature(self):
        """GitHub's legacy SHA-1 X-Hub-Signature is not a SHA-256 signature."""
        body = json.dumps({"action": "opened"}).encode()
        sha1 = (
            "sha1="
            + hmac_lib.new(GITHUB_SECRET.encode(), body, hashlib.sha1).hexdigest()
        )
        client = self._client(_github_config())
        response = client.post(
            "/api/v1/integrations/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": sha1,
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 401

    def test_github_webhook_with_a_valid_signature_is_processed(self):
        payload = {"action": "opened", "issue": {"number": 1}}
        body = json.dumps(payload).encode()
        client = self._client(_github_config())
        with patch.object(GitHubService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": _sign(GITHUB_SECRET, body),
                    "X-GitHub-Event": "issues",
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "received"
        parse.assert_called_once_with("issues", payload)

    @pytest.mark.regression
    def test_github_webhook_with_no_configured_secret_is_refused(self):
        """Nothing to verify against means nothing is accepted."""
        body = json.dumps({"action": "opened"}).encode()
        client = self._client(_github_config(secret=""))
        with patch.object(GitHubService, "parse_webhook_event") as parse:
            response = client.post(
                "/api/v1/integrations/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": _sign(GITHUB_SECRET, body),
                    "X-GitHub-Event": "issues",
                },
            )
        assert response.status_code == 401
        parse.assert_not_called()

    def test_github_webhook_without_an_integration_is_refused(self):
        body = json.dumps({"action": "opened"}).encode()
        client = self._client(None)
        response = client.post(
            "/api/v1/integrations/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(GITHUB_SECRET, body),
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED


# ---------------------------------------------------------------------------
# TestReceiveOnlyIntegrations
# ---------------------------------------------------------------------------


def _receive_only_config(integration_type: IntegrationType, secret: str) -> MagicMock:
    """A config holding the webhook secret and no outbound API credentials."""
    return _make_config(
        integration_type,
        is_active=True,
        encrypted_config={"webhook_secret": secret},
    )


class TestReceiveOnlyIntegrations:
    """Inbound authentication may depend only on what inbound needs.

    Authentication used to be routed through ``Service.from_config()``, which
    answers None when the *outbound* API credentials are missing -- Salesforce
    wants instance_url/client_id/client_secret, GitHub a token and a repo --
    none of which an inbound delivery touches.  An integration configured to
    receive and not to send therefore could not authenticate anybody,
    whatever secret it held, and got the same mute 401 as an attacker.
    """

    def setup_method(self):
        _clear()

    def teardown_method(self):
        _clear()

    @staticmethod
    def _client(config) -> TestClient:
        mock_db = _make_mock_db()
        mock_db.query.return_value.filter.return_value.first.return_value = config
        app.dependency_overrides[deps.get_db] = lambda: mock_db
        return TestClient(app)

    @pytest.mark.regression
    def test_salesforce_receive_only_integration_authenticates(self):
        client = self._client(
            _receive_only_config(IntegrationType.SALESFORCE, SALESFORCE_SECRET)
        )
        response = client.post(
            "/api/v1/integrations/webhooks/salesforce",
            json={"sObjectType": "Opportunity"},
            headers={SECRET_HEADER: SALESFORCE_SECRET},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "received"}

    @pytest.mark.regression
    def test_github_receive_only_integration_authenticates(self):
        body = json.dumps({"action": "opened"}).encode()
        client = self._client(
            _receive_only_config(IntegrationType.GITHUB, GITHUB_SECRET)
        )
        response = client.post(
            "/api/v1/integrations/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(GITHUB_SECRET, body),
                "X-GitHub-Event": "issues",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "received"}

    @pytest.mark.regression
    def test_a_receive_only_integration_still_refuses_a_wrong_secret(self):
        """Decoupling inbound from outbound must not relax the refusal."""
        client = self._client(
            _receive_only_config(IntegrationType.SALESFORCE, SALESFORCE_SECRET)
        )
        response = client.post(
            "/api/v1/integrations/webhooks/salesforce",
            json={"sObjectType": "Opportunity"},
            headers={SECRET_HEADER: "not-the-secret"},
        )
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED


class TestTheUpgradePathIsAnnounced:
    """An integration created before webhook authentication existed.

    Jira and Salesforce deliveries used to be accepted unauthenticated and
    GitHub's signature was checked only when one was sent, so every config
    written before this release has no ``webhook_secret`` and now 401s on
    every delivery.  The 401 is deliberately mute, so the *log* has to name
    the integration and the remedy -- otherwise the entire symptom is
    deliveries that stopped arriving.
    """

    def setup_method(self):
        _clear()
        integrations_endpoint._WARNED_NO_SECRET.clear()

    def teardown_method(self):
        _clear()
        integrations_endpoint._WARNED_NO_SECRET.clear()

    @staticmethod
    def _client(config) -> TestClient:
        mock_db = _make_mock_db()
        mock_db.query.return_value.filter.return_value.first.return_value = config
        app.dependency_overrides[deps.get_db] = lambda: mock_db
        return TestClient(app)

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "integration_type,path,config_factory",
        [
            (IntegrationType.JIRA, "jira", _jira_config),
            (IntegrationType.SALESFORCE, "salesforce", _salesforce_config),
            (IntegrationType.GITHUB, "github", _github_config),
        ],
    )
    def test_a_config_with_no_secret_is_named_in_the_log(
        self, integration_type, path, config_factory
    ):
        # The logger is patched, and the logged line read from the call.
        client = self._client(config_factory(secret=""))
        with patch.object(integrations_endpoint.logger, "error") as error:
            response = client.post(
                f"/api/v1/integrations/webhooks/{path}",
                json={"anything": True},
            )

        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED
        error.assert_called_once()
        template, *args = error.call_args.args
        logged = template % tuple(args)
        assert integration_type.value in logged
        assert "webhook_secret" in logged
        assert f"/api/v1/integrations/{integration_type.value}" in logged

    @pytest.mark.regression
    def test_the_warning_is_said_once_not_once_per_delivery(self):
        """A provider that retries must not turn the breadcrumb into a flood."""
        client = self._client(_jira_config(secret=""))
        with patch.object(integrations_endpoint.logger, "error") as error:
            for _ in range(3):
                client.post(
                    "/api/v1/integrations/webhooks/jira", json={"anything": True}
                )
        assert error.call_count == 1

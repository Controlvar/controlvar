"""
Integration tests for EP-037 SSO/SAML & OIDC API.

Tests the full HTTP request/response cycle for:
  GET    /api/v1/auth/sso/configs           — list configs (admin only)
  POST   /api/v1/auth/sso/configs           — create config (admin only)
  GET    /api/v1/auth/sso/configs/{id}      — get config (admin only)
  PUT    /api/v1/auth/sso/configs/{id}      — update config (admin only)
  DELETE /api/v1/auth/sso/configs/{id}      — delete config (admin only)
  GET    /api/v1/auth/sso/saml/{id}/metadata — SP metadata XML
  POST   /api/v1/auth/sso/saml/{id}/acs    — SAML ACS (mocked)
  GET    /api/v1/auth/sso/oidc/google/login — redirects to Google
  GET    /api/v1/auth/sso/oidc/google/callback — OIDC callback (mocked)

All tests use the conftest.py role-specific client fixtures from
backend/tests/integration/conftest.py.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models.user import User, UserRole
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


BASE = "/api/v1/auth/sso"


def _saml_config_payload(**overrides) -> Dict[str, Any]:
    base = {
        "org_name": "Acme Corp",
        "org_domain": f"acme-{uuid.uuid4().hex[:6]}.com",
        "provider_type": "saml",
        "entity_id": "https://idp.acme.com",
        "sso_url": "https://idp.acme.com/sso",
        "x509_certificate": "MIIC...cert",
        "role_mapping": {"Admins": "admin", "Developers": "developer"},
        "is_enforced": False,
        "is_active": True,
    }
    base.update(overrides)
    return base


def _google_config_payload(**overrides) -> Dict[str, Any]:
    base = {
        "org_name": "Acme Corp Google",
        "org_domain": f"acme-google-{uuid.uuid4().hex[:6]}.com",
        "provider_type": "google",
        "entity_id": "google-client-id",
        "client_secret": "google-client-secret",
        "role_mapping": {},
        "is_enforced": False,
        "is_active": True,
    }
    base.update(overrides)
    return base


def _create_config(client: TestClient, payload: Dict) -> Dict:
    resp = client.post(f"{BASE}/configs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _start_login(
    client: TestClient, org_domain: str, provider: str = "google"
) -> Dict[str, str]:
    """Start a login the way a browser does and keep what it would keep.

    Returns the ``state`` from the provider redirect and the ``Cookie`` header
    that sends the login's state cookie back. The cookie is ``Secure`` and the
    test client talks plain http, so it is passed by hand, never by the jar.
    """
    from urllib.parse import parse_qs, urlparse

    from modules.backend.app.services import sso_service

    resp = client.get(
        f"{BASE}/oidc/{provider}/login",
        params={"org_domain": org_domain},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    name = sso_service.OIDC_STATE_COOKIE
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith(f"{name}="), set_cookie
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    client.cookies.clear()
    return {"state": state, "cookie": f"{name}={value}"}


def _callback(
    client: TestClient, login: Dict[str, str], provider: str = "google", **params
):
    query = {"code": "auth-code", "state": login["state"], **params}
    return client.get(
        f"{BASE}/oidc/{provider}/callback",
        params=query,
        headers={"cookie": login["cookie"]},
    )


@pytest.fixture
def skip_id_token_check():
    """These tests mock the exchange; the ID-token checks have their own tests
    (``test_sso_oidc_flow.py`` end to end, ``test_sso_service.py`` per claim)."""

    async def passthrough(config, provider, id_claims, user_info, access_token):
        return user_info

    with (
        patch(
            "modules.backend.app.services.sso_service.verify_id_token",
            return_value={},
        ),
        patch(
            "modules.backend.app.services.sso_service.verified_email",
            side_effect=passthrough,
        ),
    ):
        yield


def _make_saml_response_b64(name_id: str = "user@acme.com") -> str:
    xml = (
        '<?xml version="1.0"?>'
        "<samlp:Response"
        ' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
        ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
        "<saml:Assertion>"
        "<saml:Subject>"
        f"<saml:NameID>{name_id}</saml:NameID>"
        "</saml:Subject>"
        "</saml:Assertion>"
        "</samlp:Response>"
    )
    return base64.b64encode(xml.encode()).decode()


# ---------------------------------------------------------------------------
# GET /configs — list SSO configs
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestListSSOConfigs:
    def test_admin_can_list_configs(self, admin_client: TestClient):
        resp = admin_client.get(f"{BASE}/configs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_developer_gets_403(self, developer_client: TestClient):
        resp = developer_client.get(f"{BASE}/configs")
        assert resp.status_code == 403

    def test_analyst_gets_403(self, analyst_client: TestClient):
        resp = analyst_client.get(f"{BASE}/configs")
        assert resp.status_code == 403

    def test_viewer_gets_403(self, viewer_client: TestClient):
        resp = viewer_client.get(f"{BASE}/configs")
        assert resp.status_code == 403

    def test_empty_list_returns_array(self, admin_client: TestClient):
        resp = admin_client.get(f"{BASE}/configs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_created_config_appears_in_list(self, admin_client: TestClient):
        payload = _saml_config_payload()
        _create_config(admin_client, payload)
        resp = admin_client.get(f"{BASE}/configs")
        assert resp.status_code == 200
        domains = [c["org_domain"] for c in resp.json()]
        assert payload["org_domain"] in domains


# ---------------------------------------------------------------------------
# POST /configs — create SSO config
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestCreateSSOConfig:
    def test_admin_creates_saml_config(self, admin_client: TestClient):
        payload = _saml_config_payload()
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["provider_type"] == "saml"
        assert data["org_domain"] == payload["org_domain"]

    def test_admin_creates_google_config(self, admin_client: TestClient):
        payload = _google_config_payload()
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["provider_type"] == "google"

    def test_developer_cannot_create_config(self, developer_client: TestClient):
        payload = _saml_config_payload()
        resp = developer_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 403

    def test_duplicate_domain_returns_409(self, admin_client: TestClient):
        payload = _saml_config_payload()
        _create_config(admin_client, payload)
        # Second creation with same org_domain
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 409

    def test_missing_org_name_returns_422(self, admin_client: TestClient):
        payload = _saml_config_payload()
        del payload["org_name"]
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 422

    def test_missing_org_domain_returns_422(self, admin_client: TestClient):
        payload = _saml_config_payload()
        del payload["org_domain"]
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 422

    def test_missing_provider_type_returns_422(self, admin_client: TestClient):
        payload = _saml_config_payload()
        del payload["provider_type"]
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 422

    def test_invalid_provider_type_returns_422(self, admin_client: TestClient):
        payload = _saml_config_payload()
        payload["provider_type"] = "invalid_provider"
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 422

    def test_created_config_has_id(self, admin_client: TestClient):
        payload = _saml_config_payload()
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        uuid.UUID(data["id"])

    def test_created_config_has_timestamps(self, admin_client: TestClient):
        payload = _saml_config_payload()
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        data = resp.json()
        assert "created_at" in data
        assert "updated_at" in data

    def test_role_mapping_preserved(self, admin_client: TestClient):
        mapping = {"Admins": "admin", "Devs": "developer"}
        payload = _saml_config_payload(role_mapping=mapping)
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["role_mapping"] == mapping


# ---------------------------------------------------------------------------
# GET /configs/{id} — get specific SSO config
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetSSOConfig:
    def test_admin_can_get_config_by_id(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)
        resp = admin_client.get(f"{BASE}/configs/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_developer_gets_403(
        self, developer_client: TestClient, db_session: Session
    ):
        # Create config directly in DB so we don't need admin_client (which would
        # overwrite the global dependency overrides and confuse developer_client).
        import uuid as _uuid

        from modules.backend.app.models.sso_config import SSOConfig
        from modules.backend.app.models.sso_config import SSOProviderType as PT

        cfg = SSOConfig(
            org_name="Dev Test Org",
            org_domain=f"devtest-{_uuid.uuid4().hex[:6]}.com",
            provider_type=PT.SAML,
            is_active=True,
            is_enforced=False,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)
        resp = developer_client.get(f"{BASE}/configs/{cfg.id}")
        assert resp.status_code == 403

    def test_nonexistent_id_returns_404(self, admin_client: TestClient):
        fake_id = str(uuid.uuid4())
        resp = admin_client.get(f"{BASE}/configs/{fake_id}")
        assert resp.status_code == 404

    def test_config_fields_are_correct(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)
        resp = admin_client.get(f"{BASE}/configs/{created['id']}")
        data = resp.json()
        assert data["org_name"] == payload["org_name"]
        assert data["provider_type"] == payload["provider_type"]
        assert data["entity_id"] == payload["entity_id"]


# ---------------------------------------------------------------------------
# PUT /configs/{id} — update SSO config
# ---------------------------------------------------------------------------


HTTPS_DETAIL = (
    "OIDC provider endpoints must use https; check this SSO configuration's sso_url"
)


def _okta_config_payload(**overrides) -> Dict[str, Any]:
    base = {
        "org_name": "Acme Okta",
        "org_domain": f"acme-okta-{uuid.uuid4().hex[:6]}.com",
        "provider_type": "okta",
        "entity_id": "okta-client-id",
        "client_secret": "okta-client-pw",
        "sso_url": "https://acme.okta.example/oauth2/default",
    }
    base.update(overrides)
    return base


@pytest.fixture
def outside_test(monkeypatch):
    from backend.app.core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "ENVIRONMENT", "development")
    assert not core_settings.is_test


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
class TestAnOktaSsoUrlMustBeHttps:
    """Refused when the configuration is saved, as it is at sign-in."""

    def test_create_with_an_http_sso_url_is_refused(
        self, admin_client: TestClient, outside_test
    ):
        payload = _okta_config_payload(sso_url="http://acme.okta.example/oauth2")
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == HTTPS_DETAIL
        listed = admin_client.get(f"{BASE}/configs").json()
        assert payload["org_domain"] not in [c["org_domain"] for c in listed]

    def test_update_to_an_http_sso_url_is_refused(
        self, admin_client: TestClient, outside_test
    ):
        created = _create_config(admin_client, _okta_config_payload())
        resp = admin_client.put(
            f"{BASE}/configs/{created['id']}",
            json={"sso_url": "http://acme.okta.example/oauth2"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == HTTPS_DETAIL
        stored = admin_client.get(f"{BASE}/configs/{created['id']}").json()
        assert stored["sso_url"] == "https://acme.okta.example/oauth2/default"

    def test_a_google_config_with_an_http_sso_url_is_saved(
        self, admin_client: TestClient, outside_test
    ):
        """Google's endpoints are fixed; its ``sso_url`` is not used, so not checked."""
        payload = _google_config_payload(sso_url="http://ignored.example")
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 201, resp.text
        resp = admin_client.put(
            f"{BASE}/configs/{resp.json()['id']}", json={"sso_url": "ignored.example"}
        )
        assert resp.status_code == 200, resp.text


SAML_HTTPS_DETAIL = (
    "SAML single sign-on URL must use https; check this SSO configuration's sso_url"
)


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
class TestASamlSsoUrlMustBeHttps:
    """Refused when the configuration is saved (#1100); sign-in is unchanged."""

    def test_create_with_an_http_sso_url_is_refused(
        self, admin_client: TestClient, outside_test
    ):
        payload = _saml_config_payload(sso_url="http://idp.acme.example/sso")
        resp = admin_client.post(f"{BASE}/configs", json=payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == SAML_HTTPS_DETAIL
        listed = admin_client.get(f"{BASE}/configs").json()
        assert payload["org_domain"] not in [c["org_domain"] for c in listed]

    def test_update_to_an_http_sso_url_is_refused(
        self, admin_client: TestClient, outside_test
    ):
        created = _create_config(admin_client, _saml_config_payload())
        resp = admin_client.put(
            f"{BASE}/configs/{created['id']}",
            json={"sso_url": "http://idp.acme.example/sso"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == SAML_HTTPS_DETAIL
        stored = admin_client.get(f"{BASE}/configs/{created['id']}").json()
        assert stored["sso_url"] == "https://idp.acme.com/sso"

    def test_a_stored_http_config_can_still_be_renamed_and_switched_off(
        self, admin_client: TestClient, db_session: Session, outside_test
    ):
        """A row stored with an http sso_url before the check: an update that
        sends neither sso_url nor provider_type is not checked."""
        cfg = SSOConfig(
            org_name="Legacy SAML Org",
            org_domain=f"legacy-saml-{uuid.uuid4().hex[:6]}.com",
            provider_type=SSOProviderType.SAML,
            entity_id="https://idp.legacy.example",
            sso_url="http://idp.legacy.example/sso",
            x509_certificate="MIIC...cert",
            is_active=True,
            is_enforced=False,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)
        for change in ({"org_name": "Legacy SAML Org Renamed"}, {"is_active": False}):
            resp = admin_client.put(f"{BASE}/configs/{cfg.id}", json=change)
            assert resp.status_code == 200, resp.text
        stored = admin_client.get(f"{BASE}/configs/{cfg.id}").json()
        assert stored["org_name"] == "Legacy SAML Org Renamed"
        assert stored["is_active"] is False
        assert stored["sso_url"] == "http://idp.legacy.example/sso"


@pytest.mark.integration
@pytest.mark.requires_db
class TestUpdateSSOConfig:
    def test_admin_can_update_config(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        update = {"org_name": "Updated Org Name"}
        resp = admin_client.put(f"{BASE}/configs/{created['id']}", json=update)
        assert resp.status_code == 200, resp.text
        assert resp.json()["org_name"] == "Updated Org Name"

    def test_developer_cannot_update(
        self, developer_client: TestClient, db_session: Session
    ):
        import uuid as _uuid

        from modules.backend.app.models.sso_config import SSOConfig
        from modules.backend.app.models.sso_config import SSOProviderType as PT

        cfg = SSOConfig(
            org_name="Dev Update Test Org",
            org_domain=f"devupdate-{_uuid.uuid4().hex[:6]}.com",
            provider_type=PT.SAML,
            is_active=True,
            is_enforced=False,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)
        resp = developer_client.put(f"{BASE}/configs/{cfg.id}", json={"org_name": "X"})
        assert resp.status_code == 403

    def test_update_nonexistent_returns_404(self, admin_client: TestClient):
        fake_id = str(uuid.uuid4())
        resp = admin_client.put(f"{BASE}/configs/{fake_id}", json={"org_name": "X"})
        assert resp.status_code == 404

    def test_update_role_mapping(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        new_mapping = {"super-admins": "admin"}
        resp = admin_client.put(
            f"{BASE}/configs/{created['id']}",
            json={"role_mapping": new_mapping},
        )
        assert resp.status_code == 200
        assert resp.json()["role_mapping"] == new_mapping

    def test_update_is_active(self, admin_client: TestClient):
        payload = _saml_config_payload(is_active=True)
        created = _create_config(admin_client, payload)

        resp = admin_client.put(
            f"{BASE}/configs/{created['id']}",
            json={"is_active": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    def test_update_is_enforced(self, admin_client: TestClient):
        payload = _saml_config_payload(is_enforced=False)
        created = _create_config(admin_client, payload)

        resp = admin_client.put(
            f"{BASE}/configs/{created['id']}",
            json={"is_enforced": True},
        )
        assert resp.status_code == 200
        assert resp.json()["is_enforced"] is True


# ---------------------------------------------------------------------------
# DELETE /configs/{id} — delete SSO config
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestDeleteSSOConfig:
    def test_admin_can_delete_config(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.delete(f"{BASE}/configs/{created['id']}")
        assert resp.status_code == 204

    def test_deleted_config_not_found(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        admin_client.delete(f"{BASE}/configs/{created['id']}")
        resp = admin_client.get(f"{BASE}/configs/{created['id']}")
        assert resp.status_code == 404

    def test_developer_cannot_delete(
        self, developer_client: TestClient, db_session: Session
    ):
        import uuid as _uuid

        from modules.backend.app.models.sso_config import SSOConfig
        from modules.backend.app.models.sso_config import SSOProviderType as PT

        cfg = SSOConfig(
            org_name="Dev Delete Test Org",
            org_domain=f"devdelete-{_uuid.uuid4().hex[:6]}.com",
            provider_type=PT.SAML,
            is_active=True,
            is_enforced=False,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)
        resp = developer_client.delete(f"{BASE}/configs/{cfg.id}")
        assert resp.status_code == 403

    def test_delete_nonexistent_returns_404(self, admin_client: TestClient):
        fake_id = str(uuid.uuid4())
        resp = admin_client.delete(f"{BASE}/configs/{fake_id}")
        assert resp.status_code == 404

    def test_deleted_config_not_in_list(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)
        admin_client.delete(f"{BASE}/configs/{created['id']}")

        resp = admin_client.get(f"{BASE}/configs")
        domains = [c["org_domain"] for c in resp.json()]
        assert payload["org_domain"] not in domains


# ---------------------------------------------------------------------------
# GET /saml/{config_id}/metadata — SP metadata XML
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestSAMLMetadata:
    def test_returns_xml_for_saml_config(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.get(f"{BASE}/saml/{created['id']}/metadata")
        assert resp.status_code == 200
        assert "xml" in resp.headers.get("content-type", "").lower()

    def test_xml_content_contains_entity_descriptor(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.get(f"{BASE}/saml/{created['id']}/metadata")
        text = resp.text
        assert "EntityDescriptor" in text or "entityID" in text

    def test_nonexistent_config_returns_404(self, admin_client: TestClient):
        fake_id = str(uuid.uuid4())
        resp = admin_client.get(f"{BASE}/saml/{fake_id}/metadata")
        assert resp.status_code == 404

    def test_non_saml_config_returns_400(self, admin_client: TestClient):
        payload = _google_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.get(f"{BASE}/saml/{created['id']}/metadata")
        assert resp.status_code == 400

    def test_metadata_is_valid_xml(self, admin_client: TestClient):
        import xml.etree.ElementTree as ET

        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.get(f"{BASE}/saml/{created['id']}/metadata")
        assert resp.status_code == 200
        ET.fromstring(resp.text)  # Should not raise


# ---------------------------------------------------------------------------
# POST /saml/{config_id}/acs — ACS endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestSAMLACS:
    def test_valid_saml_response_issues_token(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        saml_b64 = _make_saml_response_b64(f"newuser@{payload['org_domain']}")

        # Mock parse_saml_response to bypass actual SAML validation
        with patch(
            "modules.backend.app.services.sso_service.parse_saml_response",
            return_value={
                "email": f"newuser@{payload['org_domain']}",
                "name_id": f"newuser@{payload['org_domain']}",
                "groups": [],
                "attributes": {},
                "first_name": "New",
                "last_name": "User",
            },
        ):
            resp = admin_client.post(
                f"{BASE}/saml/{created['id']}/acs",
                data={"SAMLResponse": saml_b64},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "access_token" in data

    def test_acs_returns_user_email(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)
        saml_b64 = _make_saml_response_b64(f"acs_user@{payload['org_domain']}")

        with patch(
            "modules.backend.app.services.sso_service.parse_saml_response",
            return_value={
                "email": f"acs_user@{payload['org_domain']}",
                "name_id": f"acs_user@{payload['org_domain']}",
                "groups": [],
                "attributes": {},
                "first_name": None,
                "last_name": None,
            },
        ):
            resp = admin_client.post(
                f"{BASE}/saml/{created['id']}/acs",
                data={"SAMLResponse": saml_b64},
            )
        assert resp.status_code == 200
        assert resp.json()["email"] == f"acs_user@{payload['org_domain']}"

    def test_acs_missing_saml_response_returns_400(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.post(f"{BASE}/saml/{created['id']}/acs", data={})
        assert resp.status_code == 400

    def test_acs_nonexistent_config_returns_404(self, admin_client: TestClient):
        fake_id = str(uuid.uuid4())
        resp = admin_client.post(
            f"{BASE}/saml/{fake_id}/acs",
            data={"SAMLResponse": "foo"},
        )
        assert resp.status_code == 404

    def test_acs_non_saml_config_returns_400(self, admin_client: TestClient):
        payload = _google_config_payload()
        created = _create_config(admin_client, payload)

        resp = admin_client.post(
            f"{BASE}/saml/{created['id']}/acs",
            data={"SAMLResponse": _make_saml_response_b64()},
        )
        assert resp.status_code == 400

    def test_acs_response_contains_token_type(self, admin_client: TestClient):
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)
        saml_b64 = _make_saml_response_b64(f"tokentype@{payload['org_domain']}")

        with patch(
            "modules.backend.app.services.sso_service.parse_saml_response",
            return_value={
                "email": f"tokentype@{payload['org_domain']}",
                "name_id": f"tokentype@{payload['org_domain']}",
                "groups": [],
                "attributes": {},
                "first_name": None,
                "last_name": None,
            },
        ):
            resp = admin_client.post(
                f"{BASE}/saml/{created['id']}/acs",
                data={"SAMLResponse": saml_b64},
            )
        assert resp.json()["token_type"] == "bearer"


@pytest.mark.integration
@pytest.mark.requires_db
class TestSAMLWithoutTheLibrary:
    """POST /saml/{id}/acs is unauthenticated by design (it *is* the login),
    so whatever validates the assertion is the only thing standing between a
    stranger and a session.  When python3-saml is not installed that is the
    stub parser, which validates nothing."""

    @pytest.fixture
    def no_saml_library(self):
        with patch("modules.backend.app.services.sso_service._SAML_AVAILABLE", False):
            yield

    @pytest.fixture
    def environment(self, monkeypatch):
        from modules.backend.app.services import sso_service

        def _set(name: str):
            monkeypatch.setattr(sso_service.core_settings, "ENVIRONMENT", name)

        return _set

    @pytest.mark.regression
    def test_a_forged_assertion_cannot_mint_a_token_in_production(
        self,
        admin_client: TestClient,
        admin_user: User,
        no_saml_library,
        environment,
    ):
        """The exploit: hand-written XML naming an existing administrator's
        email address, POSTed to the ACS with no credentials at all.  The stub
        read the NameID straight out of it, `provision_user` matched the
        administrator by email and the route issued that administrator an
        HS256 JWT.  With python3-saml missing, production now answers 501."""
        created = _create_config(admin_client, _saml_config_payload())
        forged = _make_saml_response_b64(admin_user.email)

        environment("production")
        resp = admin_client.post(
            f"{BASE}/saml/{created['id']}/acs",
            data={"SAMLResponse": forged},
        )
        assert resp.status_code == 501, resp.text
        assert "access_token" not in resp.text
        assert "python3-saml" in resp.json()["detail"]

    def test_the_same_request_is_served_in_the_test_environment(
        self,
        admin_client: TestClient,
        admin_user: User,
        no_saml_library,
        environment,
    ):
        """The other half of the gate: the stub is still the development
        convenience it was written to be, so the suite and a local `make dev`
        keep working without the optional dependency."""
        payload = _saml_config_payload()
        created = _create_config(admin_client, payload)
        environment("test")
        # The admin's own mailbox name, at the configuration's domain.
        email = f"{admin_user.email.split('@')[0]}@{payload['org_domain']}"
        resp = admin_client.post(
            f"{BASE}/saml/{created['id']}/acs",
            data={"SAMLResponse": _make_saml_response_b64(email)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == email

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_metadata_answers_501_outside_development(
        self, admin_client: TestClient, no_saml_library, environment, env
    ):
        created = _create_config(admin_client, _saml_config_payload())
        environment(env)
        resp = admin_client.get(f"{BASE}/saml/{created['id']}/metadata")
        assert resp.status_code == 501, resp.text


# ---------------------------------------------------------------------------
# GET /oidc/google/login — OIDC login redirect
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestOIDCLogin:
    def test_google_login_redirects(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)

        resp = admin_client.get(
            f"{BASE}/oidc/google/login",
            params={"org_domain": payload["org_domain"]},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "google" in location.lower() or "accounts" in location.lower()

    def test_unknown_provider_returns_404(self, admin_client: TestClient):
        resp = admin_client.get(
            f"{BASE}/oidc/nonexistent_provider/login",
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_login_redirect_contains_client_id(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)

        resp = admin_client.get(
            f"{BASE}/oidc/google/login",
            params={"org_domain": payload["org_domain"]},
            follow_redirects=False,
        )
        location = resp.headers.get("location", "")
        assert "client_id" in location

    def test_login_redirect_contains_state(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)

        resp = admin_client.get(
            f"{BASE}/oidc/google/login",
            params={"org_domain": payload["org_domain"]},
            follow_redirects=False,
        )
        location = resp.headers.get("location", "")
        assert "state=" in location


# ---------------------------------------------------------------------------
# GET /oidc/google/callback — OIDC callback
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.usefixtures("skip_id_token_check")
class TestOIDCCallback:
    def test_google_callback_issues_token(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)
        login = _start_login(admin_client, payload["org_domain"])

        with (
            patch(
                "modules.backend.app.services.sso_service.exchange_oidc_code",
                new_callable=AsyncMock,
                return_value={"access_token": "google-at"},
            ),
            patch(
                "modules.backend.app.services.sso_service.get_oidc_user_info",
                new_callable=AsyncMock,
                return_value={
                    "email": f"alice@{payload['org_domain']}",
                    "name": "Alice",
                    "sub": "google-sub-123",
                    "groups": [],
                    "raw": {},
                },
            ),
        ):
            resp = _callback(admin_client, login)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "access_token" in data
        assert data["provider"] == "google"

    def test_callback_missing_code_returns_400(self, admin_client: TestClient):
        resp = admin_client.get(
            f"{BASE}/oidc/google/callback",
            params={"state": "some-state"},
        )
        assert resp.status_code == 400

    def test_callback_with_error_param_returns_400(self, admin_client: TestClient):
        resp = admin_client.get(
            f"{BASE}/oidc/google/callback",
            params={"error": "access_denied"},
        )
        assert resp.status_code == 400

    def test_callback_unknown_provider_returns_404(self, admin_client: TestClient):
        """A login started for google, called back on another provider's path."""
        payload = _google_config_payload()
        _create_config(admin_client, payload)
        login = _start_login(admin_client, payload["org_domain"])
        with (
            patch(
                "modules.backend.app.services.sso_service.exchange_oidc_code",
                new_callable=AsyncMock,
                return_value={"access_token": "token"},
            ),
            patch(
                "modules.backend.app.services.sso_service.get_oidc_user_info",
                new_callable=AsyncMock,
                return_value={
                    "email": "alice@acme.com",
                    "sub": "123",
                    "groups": [],
                    "raw": {},
                },
            ),
        ):
            resp = _callback(admin_client, login, provider="nonexistent")
        assert resp.status_code == 404

    def test_callback_returns_user_email(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)
        login = _start_login(admin_client, payload["org_domain"])

        with (
            patch(
                "modules.backend.app.services.sso_service.exchange_oidc_code",
                new_callable=AsyncMock,
                return_value={"access_token": "google-at"},
            ),
            patch(
                "modules.backend.app.services.sso_service.get_oidc_user_info",
                new_callable=AsyncMock,
                return_value={
                    "email": f"bob@{payload['org_domain']}",
                    "name": "Bob",
                    "sub": "sub-456",
                    "groups": [],
                    "raw": {},
                },
            ),
        ):
            resp = _callback(admin_client, login)
        assert resp.status_code == 200
        assert resp.json()["email"] == f"bob@{payload['org_domain']}"

    def test_callback_returns_role(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)
        login = _start_login(admin_client, payload["org_domain"])

        with (
            patch(
                "modules.backend.app.services.sso_service.exchange_oidc_code",
                new_callable=AsyncMock,
                return_value={"access_token": "google-at"},
            ),
            patch(
                "modules.backend.app.services.sso_service.get_oidc_user_info",
                new_callable=AsyncMock,
                return_value={
                    "email": f"role_user_{uuid.uuid4().hex[:4]}@{payload['org_domain']}",
                    "name": "Role User",
                    "sub": "sub-789",
                    "groups": [],
                    "raw": {},
                },
            ),
        ):
            resp = _callback(admin_client, login)
        assert resp.status_code == 200
        assert "role" in resp.json()


# ---------------------------------------------------------------------------
# is_active is the off-switch: a deactivated provider must serve neither the
# ACS nor its metadata (review round 3, finding 1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestDeactivatedSAMLProvider:
    """`is_active = false` is the only administrative off-switch these two
    routes have — there is no licence gate above them any more.  Both read the
    row and ignored the flag, so an operator responding to a compromised IdP
    could set it false and the ACS would carry on consuming assertions,
    provisioning users and minting platform JWTs while the metadata endpoint
    advertised the ACS.  Only deleting the row stopped it."""

    @staticmethod
    def _deactivate(client: TestClient, config_id: str) -> None:
        resp = client.put(f"{BASE}/configs/{config_id}", json={"is_active": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False

    @pytest.mark.regression
    def test_acs_refuses_a_deactivated_config(self, admin_client: TestClient):
        created = _create_config(admin_client, _saml_config_payload(is_active=True))
        self._deactivate(admin_client, created["id"])

        with patch(
            "modules.backend.app.services.sso_service.parse_saml_response",
            return_value={
                "email": "intruder@acme.com",
                "name_id": "intruder@acme.com",
                "groups": [],
                "attributes": {},
                "first_name": None,
                "last_name": None,
            },
        ) as parse:
            resp = admin_client.post(
                f"{BASE}/saml/{created['id']}/acs",
                data={"SAMLResponse": _make_saml_response_b64("intruder@acme.com")},
            )

        assert resp.status_code == 404, resp.text
        assert "not active" in resp.json()["detail"]
        # No token, and the assertion was never even parsed.
        assert "access_token" not in resp.text
        parse.assert_not_called()

    @pytest.mark.regression
    def test_a_deactivated_config_stops_advertising_its_acs(
        self, admin_client: TestClient
    ):
        created = _create_config(admin_client, _saml_config_payload(is_active=True))
        assert (
            admin_client.get(f"{BASE}/saml/{created['id']}/metadata").status_code == 200
        )

        self._deactivate(admin_client, created["id"])

        resp = admin_client.get(f"{BASE}/saml/{created['id']}/metadata")
        assert resp.status_code == 404, resp.text
        assert "not active" in resp.json()["detail"]
        assert "AssertionConsumerService" not in resp.text

    def test_reactivating_restores_both_routes(self, admin_client: TestClient):
        """The switch is a switch, not a one-way door."""
        created = _create_config(admin_client, _saml_config_payload(is_active=True))
        self._deactivate(admin_client, created["id"])
        assert (
            admin_client.put(
                f"{BASE}/configs/{created['id']}", json={"is_active": True}
            ).status_code
            == 200
        )
        assert (
            admin_client.get(f"{BASE}/saml/{created['id']}/metadata").status_code == 200
        )


# ---------------------------------------------------------------------------
# The OIDC callback's CSRF check is not optional (review round 3, finding 2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.usefixtures("skip_id_token_check")
class TestOIDCCallbackStateIsMandatory:
    """`state` was `Query(None)` and the verification sat inside `if state:`,
    so *omitting the parameter* skipped `verify_state_token()` entirely and
    fell through to the code exchange, `provision_user` and `_issue_jwt`.  The
    handler is unchanged from main, but main mounted this router behind the
    licence gate; dropping that gate made it reachable on every full-profile
    deployment."""

    @staticmethod
    def _mocks(domain: str = "acme.com"):
        return (
            patch(
                "modules.backend.app.services.sso_service.exchange_oidc_code",
                new_callable=AsyncMock,
                return_value={"access_token": "google-at"},
            ),
            patch(
                "modules.backend.app.services.sso_service.get_oidc_user_info",
                new_callable=AsyncMock,
                return_value={
                    "email": f"csrf-victim@{domain}",
                    "name": "Victim",
                    "sub": "google-sub-csrf",
                    "groups": [],
                    "raw": {},
                },
            ),
        )

    @pytest.mark.regression
    def test_a_callback_with_no_state_is_refused(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)

        login = _start_login(admin_client, payload["org_domain"])

        exchange, user_info = self._mocks()
        with exchange as exchange_mock, user_info:
            resp = admin_client.get(
                f"{BASE}/oidc/google/callback",
                params={"code": "attacker-code"},
                headers={"cookie": login["cookie"]},
            )

        assert resp.status_code == 400, resp.text
        assert "state" in resp.json()["detail"].lower()
        assert "access_token" not in resp.text
        # The code was never exchanged, so no session was minted for it.
        exchange_mock.assert_not_awaited()

    def test_a_callback_with_a_forged_state_is_refused(self, admin_client: TestClient):
        payload = _google_config_payload()
        _create_config(admin_client, payload)
        login = _start_login(admin_client, payload["org_domain"])

        exchange, user_info = self._mocks()
        with exchange as exchange_mock, user_info:
            resp = _callback(
                admin_client,
                {**login, "state": "not-a-state-we-issued"},
                code="attacker-code",
            )

        assert resp.status_code == 400, resp.text
        assert "access_token" not in resp.text
        exchange_mock.assert_not_awaited()

    @pytest.mark.regression
    def test_a_state_without_its_browsers_cookie_is_refused(
        self, admin_client: TestClient
    ):
        """#66: a state the server issued, but to a different browser.

        The attacker starts a login, gets their own state and code, and sends
        the victim a callback link carrying both. The victim's browser has no
        cookie for that login -- or has one for a login of its own -- so the
        callback is refused before the code is exchanged. It used to be
        accepted: the state was checked against a server-side list, bound to
        nobody.
        """
        payload = _google_config_payload()
        _create_config(admin_client, payload)
        attackers = _start_login(admin_client, payload["org_domain"])
        victims = _start_login(admin_client, payload["org_domain"])

        exchange, user_info = self._mocks()
        with exchange as exchange_mock, user_info:
            no_cookie = admin_client.get(
                f"{BASE}/oidc/google/callback",
                params={"code": "attacker-code", "state": attackers["state"]},
            )
            wrong_cookie = _callback(
                admin_client,
                {"state": attackers["state"], "cookie": victims["cookie"]},
                code="attacker-code",
            )

        assert no_cookie.status_code == 400, no_cookie.text
        assert wrong_cookie.status_code == 400, wrong_cookie.text
        assert "does not match" in wrong_cookie.json()["detail"]
        exchange_mock.assert_not_awaited()

    def test_every_callback_expires_the_state_cookie(self, admin_client: TestClient):
        """The browser's half of single use: success and refusal alike."""
        from modules.backend.app.services import sso_service

        payload = _google_config_payload()
        _create_config(admin_client, payload)
        expected = (
            f'{sso_service.OIDC_STATE_COOKIE}=""; HttpOnly; Max-Age=0; Path=/; '
            "SameSite=lax; Secure"
        )

        exchange, user_info = self._mocks(payload["org_domain"])
        with exchange, user_info:
            ok = _callback(
                admin_client, _start_login(admin_client, payload["org_domain"])
            )
            refused = _callback(
                admin_client,
                {**_start_login(admin_client, payload["org_domain"]), "state": "x"},
            )

        assert ok.status_code == 200, ok.text
        assert refused.status_code == 400, refused.text
        for resp in (ok, refused):
            assert resp.headers.get_list("set-cookie") == [expected]

    def test_the_login_sets_a_host_prefixed_lax_cookie(self, admin_client: TestClient):
        from modules.backend.app.services import sso_service

        payload = _google_config_payload()
        _create_config(admin_client, payload)
        resp = admin_client.get(
            f"{BASE}/oidc/google/login",
            params={"org_domain": payload["org_domain"]},
            follow_redirects=False,
        )
        (cookie,) = resp.headers.get_list("set-cookie")
        name, rest = cookie.split("=", 1)
        assert name == sso_service.OIDC_STATE_COOKIE
        attributes = [part.strip() for part in rest.split(";")[1:]]
        assert attributes == [
            "HttpOnly",
            # Five minutes past the login's own expiry, so a late callback
            # still brings the cookie and is told `sso_expired` (C2b).
            f"Max-Age={sso_service.OIDC_STATE_TTL_SECONDS + 300}",
            "Path=/",
            "SameSite=lax",
            "Secure",
        ]
        assert "Domain" not in cookie

    def test_the_login_sends_a_pkce_challenge(self, admin_client: TestClient):
        from urllib.parse import parse_qs, urlparse

        payload = _google_config_payload()
        _create_config(admin_client, payload)
        resp = admin_client.get(
            f"{BASE}/oidc/google/login",
            params={"org_domain": payload["org_domain"]},
            follow_redirects=False,
        )
        query = parse_qs(urlparse(resp.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["code_challenge"][0]) == 43

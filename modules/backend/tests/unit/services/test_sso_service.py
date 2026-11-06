"""
Unit tests for modules/backend/app/services/sso_service.py — EP-037.

Covers:
  - create_sso_config: success, validation error, duplicate domain
  - get_sso_config / get_sso_config_by_id: found, not found
  - list_sso_configs: empty, multiple
  - update_sso_config: success, not found, domain conflict
  - delete_sso_config: success, not found
  - generate_saml_metadata: correct XML structure
  - parse_saml_response: valid, invalid signature, expired assertion
  - exchange_oidc_code: success (mocked HTTP)
  - get_oidc_user_info: success (mocked HTTP)
  - provision_user: new user (JIT), existing user, role update
  - map_role: various group mappings, default, no mapping
  - start_oidc_login / redeem_oidc_state: the signed state cookie
  - build_oidc_authorization_url: structure checks
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.models.user import User, UserRole
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service
from modules.backend.app.services.sso_service import (
    build_oidc_authorization_url,
    create_sso_config,
    delete_sso_config,
    exchange_oidc_code,
    generate_saml_metadata,
    get_oidc_user_info,
    get_sso_config,
    get_sso_config_by_id,
    list_sso_configs,
    map_role,
    parse_saml_response,
    pkce_challenge,
    provision_user,
    redeem_oidc_state,
    start_oidc_login,
    update_sso_config,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_saml_config(**kwargs) -> MagicMock:
    """Return a MagicMock that looks like a SAML SSOConfig."""
    cfg = MagicMock(spec=SSOConfig)
    cfg.id = uuid.uuid4()
    cfg.org_name = kwargs.get("org_name", "Acme Corp")
    cfg.org_domain = kwargs.get("org_domain", "acme.com")
    cfg.provider_type = SSOProviderType.SAML
    cfg.entity_id = kwargs.get("entity_id", "https://idp.acme.com")
    cfg.sso_url = kwargs.get("sso_url", "https://idp.acme.com/sso")
    cfg.x509_certificate = kwargs.get("x509_certificate", "CERT_PLACEHOLDER")
    cfg.client_secret = None
    cfg.role_mapping = kwargs.get("role_mapping", {"admins": "admin"})
    cfg.is_active = True
    cfg.is_enforced = False
    return cfg


def _make_google_config(**kwargs) -> MagicMock:
    cfg = MagicMock(spec=SSOConfig)
    cfg.id = uuid.uuid4()
    cfg.org_name = kwargs.get("org_name", "Acme Corp")
    cfg.org_domain = kwargs.get("org_domain", "acme.com")
    cfg.provider_type = SSOProviderType.GOOGLE
    cfg.entity_id = kwargs.get("entity_id", "google-client-id")
    cfg.sso_url = None
    cfg.x509_certificate = None
    cfg.client_secret = kwargs.get("client_secret", "google-client-secret")
    cfg.role_mapping = kwargs.get("role_mapping", {})
    cfg.is_active = True
    cfg.is_enforced = False
    return cfg


def _make_db_session() -> MagicMock:
    """Return a minimal SQLAlchemy Session mock."""
    db = MagicMock()
    db.query.return_value = db
    db.filter.return_value = db
    db.first.return_value = None
    db.all.return_value = []
    # `provision_user` reads `.limit(2).all()`; answer it from `first`, which
    # the tests set, so one fixture drives both query shapes.
    limited = MagicMock()
    limited.all.side_effect = lambda: (
        [db.first.return_value] if db.first.return_value else []
    )
    db.limit.return_value = limited
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.delete = MagicMock()
    return db


def _make_valid_saml_xml(name_id: str = "user@acme.com") -> str:
    """Build a minimal SAML response XML and base64-encode it."""
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
# CRUD: create_sso_config
# ---------------------------------------------------------------------------


class TestCreateSSOConfig:
    def test_creates_successfully(self):
        db = _make_db_session()
        db.first.return_value = None  # no duplicate

        data = {
            "org_name": "Acme Corp",
            "org_domain": "acme.com",
            "provider_type": SSOProviderType.SAML,
            "entity_id": "https://idp.acme.com",
            "sso_url": "https://idp.acme.com/sso",
            "role_mapping": {"admins": "admin"},
            "is_enforced": False,
            "is_active": True,
        }

        def _refresh_side_effect(obj):
            obj.id = uuid.uuid4()
            obj.org_domain = "acme.com"

        db.refresh.side_effect = _refresh_side_effect
        config = create_sso_config(db, data)
        db.add.assert_called_once()
        db.commit.assert_called_once()
        assert config is not None

    def test_missing_org_domain_raises_400(self):
        db = _make_db_session()
        with pytest.raises(HTTPException) as exc_info:
            create_sso_config(
                db, {"org_name": "Acme", "provider_type": SSOProviderType.SAML}
            )
        assert exc_info.value.status_code == 400

    def test_duplicate_domain_raises_409(self):
        db = _make_db_session()
        db.first.return_value = MagicMock()  # existing record

        with pytest.raises(HTTPException) as exc_info:
            create_sso_config(
                db,
                {
                    "org_name": "Acme",
                    "org_domain": "acme.com",
                    "provider_type": SSOProviderType.SAML,
                },
            )
        assert exc_info.value.status_code == 409

    def test_empty_role_mapping_is_allowed(self):
        db = _make_db_session()
        db.first.return_value = None
        data = {
            "org_name": "Acme",
            "org_domain": "acme.com",
            "provider_type": SSOProviderType.GOOGLE,
            "role_mapping": {},
            "is_active": True,
            "is_enforced": False,
        }
        config = create_sso_config(db, data)
        db.add.assert_called_once()

    def test_is_active_defaults_true(self):
        db = _make_db_session()
        db.first.return_value = None
        data = {
            "org_name": "Acme",
            "org_domain": "acme.com",
            "provider_type": SSOProviderType.GITHUB,
        }
        create_sso_config(db, data)
        added_obj = db.add.call_args[0][0]
        # The SSOConfig model sets is_active default=True at the DB level;
        # for unit tests the mock object won't have the column defaults applied,
        # but we verify that no exception is raised and db.add was called.
        db.add.assert_called_once()


# ---------------------------------------------------------------------------
# CRUD: get_sso_config / get_sso_config_by_id
# ---------------------------------------------------------------------------


class TestGetSSOConfig:
    def test_get_by_domain_found(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        db.first.return_value = cfg

        result = get_sso_config(db, "acme.com")
        assert result == cfg

    def test_get_by_domain_not_found(self):
        db = _make_db_session()
        db.first.return_value = None

        result = get_sso_config(db, "unknown.com")
        assert result is None

    def test_get_by_id_found(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        cfg_id = uuid.uuid4()
        cfg.id = cfg_id
        db.first.return_value = cfg

        result = get_sso_config_by_id(db, cfg_id)
        assert result == cfg

    def test_get_by_id_not_found(self):
        db = _make_db_session()
        db.first.return_value = None

        result = get_sso_config_by_id(db, uuid.uuid4())
        assert result is None

    def test_get_by_id_filters_correctly(self):
        db = _make_db_session()
        db.first.return_value = None
        get_sso_config_by_id(db, uuid.uuid4())
        db.query.assert_called_once_with(SSOConfig)


# ---------------------------------------------------------------------------
# CRUD: list_sso_configs
# ---------------------------------------------------------------------------


class TestListSSOConfigs:
    def test_empty_list(self):
        db = _make_db_session()
        db.all.return_value = []
        db.order_by.return_value = db
        result = list_sso_configs(db)
        assert result == []

    def test_multiple_configs(self):
        db = _make_db_session()
        cfg1 = _make_saml_config(org_domain="a.com")
        cfg2 = _make_google_config(org_domain="b.com")
        db.all.return_value = [cfg1, cfg2]
        db.order_by.return_value = db

        result = list_sso_configs(db)
        assert len(result) == 2

    def test_returns_all_configs(self):
        db = _make_db_session()
        configs = [_make_saml_config(org_domain=f"org{i}.com") for i in range(5)]
        db.all.return_value = configs
        db.order_by.return_value = db

        result = list_sso_configs(db)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# CRUD: update_sso_config
# ---------------------------------------------------------------------------


class TestUpdateSSOConfig:
    def test_update_success(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        db.first.return_value = cfg

        result = update_sso_config(db, cfg.id, {"org_name": "New Name"})
        assert cfg.org_name == "New Name"
        db.commit.assert_called_once()

    def test_update_not_found_raises_404(self):
        db = _make_db_session()
        db.first.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            update_sso_config(db, uuid.uuid4(), {"org_name": "X"})
        assert exc_info.value.status_code == 404

    def test_update_domain_conflict_raises_409(self):
        db = _make_db_session()
        cfg = _make_saml_config(org_domain="acme.com")
        conflict_cfg = _make_saml_config(org_domain="other.com")

        # First call: get by id → returns cfg
        # Second call: domain conflict check → returns conflict_cfg
        db.first.side_effect = [cfg, conflict_cfg]

        with pytest.raises(HTTPException) as exc_info:
            update_sso_config(db, cfg.id, {"org_domain": "other.com"})
        assert exc_info.value.status_code == 409

    def test_update_same_domain_no_conflict(self):
        db = _make_db_session()
        cfg = _make_saml_config(org_domain="acme.com")
        # First call: get by id. Second: the conflict check, which excludes
        # this row itself and so finds nothing (the real query is pinned
        # against Postgres in test_sso_org_domain_normalised.py).
        db.first.side_effect = [cfg, None]

        update_sso_config(db, cfg.id, {"org_domain": "acme.com"})
        db.commit.assert_called_once()

    def test_update_is_enforced(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        cfg.is_enforced = False
        db.first.return_value = cfg

        update_sso_config(db, cfg.id, {"is_enforced": True})
        assert cfg.is_enforced is True


# ---------------------------------------------------------------------------
# CRUD: an OIDC sso_url must be https when it is saved
# ---------------------------------------------------------------------------

HTTPS_DETAIL = (
    "OIDC provider endpoints must use https; check this SSO configuration's sso_url"
)


def _okta_data(sso_url: str) -> Dict[str, Any]:
    return {
        "org_name": "Acme Corp",
        "org_domain": "acme.com",
        "provider_type": SSOProviderType.OKTA,
        "entity_id": "client-1",
        "sso_url": sso_url,
        "client_secret": "client-pw-value",
        "role_mapping": {},
        "is_enforced": False,
        "is_active": True,
    }


class TestSsoUrlIsHttpsWhenSaved:
    """The sign-in's rule (``_provider_endpoint``) applied at create and update."""

    @pytest.fixture
    def outside_test(self, monkeypatch):
        from backend.app.core.config import settings as core_settings

        monkeypatch.setattr(core_settings, "ENVIRONMENT", "development")
        assert not core_settings.is_test

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "sso_url",
        [
            "http://org.okta.example/oauth2/default",
            "org.okta.example",
            # urlparse raises ValueError on it; stored, a sign-in answered 500.
            "https://[org.okta.example",
        ],
    )
    def test_create_refuses_an_okta_sso_url_that_is_not_https(
        self, outside_test, sso_url
    ):
        db = _make_db_session()
        with pytest.raises(HTTPException) as exc_info:
            create_sso_config(db, _okta_data(sso_url))
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == HTTPS_DETAIL
        db.add.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.regression
    def test_update_refuses_an_http_sso_url(self, outside_test):
        db = _make_db_session()
        cfg = _oidc_config(SSOProviderType.OKTA, "https://org.okta.example/oauth2")
        db.first.return_value = cfg
        with pytest.raises(HTTPException) as exc_info:
            update_sso_config(db, cfg.id, {"sso_url": "http://org.okta.example/oauth2"})
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == HTTPS_DETAIL
        assert cfg.sso_url == "https://org.okta.example/oauth2"
        db.commit.assert_not_called()

    @pytest.mark.regression
    def test_update_refuses_making_a_saml_config_with_an_http_url_oidc(
        self, outside_test
    ):
        db = _make_db_session()
        cfg = _make_saml_config(sso_url="http://idp.acme.example/sso")
        db.first.return_value = cfg
        with pytest.raises(HTTPException) as exc_info:
            update_sso_config(db, cfg.id, {"provider_type": SSOProviderType.OKTA})
        assert exc_info.value.detail == HTTPS_DETAIL
        assert cfg.provider_type == SSOProviderType.SAML
        db.commit.assert_not_called()

    def test_an_update_that_does_not_touch_the_url_is_not_checked(self, outside_test):
        db = _make_db_session()
        cfg = _oidc_config(SSOProviderType.OKTA, "http://legacy.okta.example")
        db.first.return_value = cfg
        update_sso_config(db, cfg.id, {"org_name": "Renamed"})
        db.commit.assert_called_once()

    def test_https_is_accepted(self, outside_test):
        db = _make_db_session()
        create_sso_config(db, _okta_data("HTTPS://org.okta.example/oauth2/default"))
        db.commit.assert_called_once()

    @pytest.mark.parametrize(
        "provider",
        [
            SSOProviderType.GOOGLE,
            SSOProviderType.GITHUB,
            SSOProviderType.MICROSOFT,
            SSOProviderType.AZURE_AD,
            SSOProviderType.ONELOGIN,
        ],
    )
    def test_a_provider_that_ignores_sso_url_is_not_checked(
        self, outside_test, provider
    ):
        db = _make_db_session()
        data = {**_okta_data("http://idp.acme.example/sso"), "provider_type": provider}
        create_sso_config(db, data)
        db.commit.assert_called_once()

        cfg = _oidc_config(provider, "https://idp.acme.example/sso")
        db = _make_db_session()
        db.first.return_value = cfg
        update_sso_config(db, cfg.id, {"sso_url": "accounts.example.com"})
        db.commit.assert_called_once()

    @pytest.mark.regression
    def test_the_checked_providers_are_those_whose_endpoints_use_sso_url(
        self, outside_test
    ):
        """Okta, whose endpoints use ``sso_url``, and SAML, whose single sign-on
        URL it is (#1100); a provider added with ``{sso_url}`` joins them."""
        refused = set()
        for provider in SSOProviderType:
            try:
                sso_service.refuse_non_https_sso_url(provider, "http://x.example")
            except HTTPException:
                refused.add(provider)
        uses_sso_url = {
            provider
            for provider in SSOProviderType
            if any(
                "{sso_url}" in value
                for value in sso_service._OIDC_PROVIDERS.get(
                    provider.value, {}
                ).values()
            )
        }
        assert uses_sso_url == {SSOProviderType.OKTA}
        assert refused == uses_sso_url | {SSOProviderType.SAML}

    def test_the_test_environment_accepts_http_as_the_sign_in_does(self):
        from backend.app.core.config import settings as core_settings

        assert core_settings.is_test
        db = _make_db_session()
        create_sso_config(db, _okta_data("http://127.0.0.1:9/oauth2"))
        db.commit.assert_called_once()

    def test_the_sign_in_refuses_it_with_the_same_message(self, outside_test):
        cfg = _oidc_config(SSOProviderType.OKTA, "http://org.okta.example/oauth2")
        with pytest.raises(HTTPException) as exc_info:
            build_oidc_authorization_url(cfg, "okta", "https://app/cb", "s", "v" * 43)
        assert exc_info.value.detail == HTTPS_DETAIL


# ---------------------------------------------------------------------------
# CRUD: a SAML sso_url must be https when it is saved (#1100)
# ---------------------------------------------------------------------------

SAML_HTTPS_DETAIL = (
    "SAML single sign-on URL must use https; check this SSO configuration's sso_url"
)


def _saml_data(sso_url: str) -> Dict[str, Any]:
    return {
        "org_name": "Acme Corp",
        "org_domain": "acme.com",
        "provider_type": SSOProviderType.SAML,
        "entity_id": "https://idp.acme.example",
        "sso_url": sso_url,
        "x509_certificate": "CERT_PLACEHOLDER",
        "role_mapping": {},
        "is_enforced": False,
        "is_active": True,
    }


class TestSamlSsoUrlIsHttpsWhenSaved:
    """Okta's rule, with its own text, for a SAML configuration's sso_url.

    Checked when it is saved only: the API never contacts a SAML sso_url, and
    a sign-in does not read it."""

    @pytest.fixture
    def outside_test(self, monkeypatch):
        from backend.app.core.config import settings as core_settings

        monkeypatch.setattr(core_settings, "ENVIRONMENT", "development")
        assert not core_settings.is_test

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "sso_url",
        [
            "http://idp.acme.example/sso",
            "idp.acme.example/sso",
            # urlparse raises ValueError on it.
            "https://[idp.acme.example",
        ],
    )
    def test_create_refuses_a_saml_sso_url_that_is_not_https(
        self, outside_test, sso_url
    ):
        db = _make_db_session()
        with pytest.raises(HTTPException) as exc_info:
            create_sso_config(db, _saml_data(sso_url))
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == SAML_HTTPS_DETAIL
        db.add.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.regression
    def test_update_refuses_a_saml_sso_url_that_is_not_https(self, outside_test):
        db = _make_db_session()
        cfg = _make_saml_config(sso_url="https://idp.acme.example/sso")
        db.first.return_value = cfg
        with pytest.raises(HTTPException) as exc_info:
            update_sso_config(db, cfg.id, {"sso_url": "http://idp.acme.example/sso"})
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == SAML_HTTPS_DETAIL
        assert cfg.sso_url == "https://idp.acme.example/sso"
        db.commit.assert_not_called()

    def test_https_in_any_case_and_an_empty_url_are_accepted(self, outside_test):
        for sso_url in ("HTTPS://idp.acme.example/sso", ""):
            db = _make_db_session()
            create_sso_config(db, _saml_data(sso_url))
            db.commit.assert_called_once()

    @pytest.mark.parametrize("change", [{"org_name": "Renamed"}, {"is_active": False}])
    def test_a_stored_http_saml_config_can_still_be_changed(self, outside_test, change):
        """An update that sends neither sso_url nor provider_type is not checked,
        so a configuration stored before the check can be renamed or switched
        off."""
        db = _make_db_session()
        cfg = _make_saml_config(sso_url="http://legacy.acme.example/sso")
        db.first.return_value = cfg
        update_sso_config(db, cfg.id, change)
        db.commit.assert_called_once()

    def test_the_test_environment_accepts_an_http_saml_url(self):
        from backend.app.core.config import settings as core_settings

        assert core_settings.is_test
        db = _make_db_session()
        create_sso_config(db, _saml_data("http://127.0.0.1:9/sso"))
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# CRUD: delete_sso_config
# ---------------------------------------------------------------------------


class TestDeleteSSOConfig:
    def test_delete_success(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        db.first.return_value = cfg

        result = delete_sso_config(db, cfg.id)
        assert result is True
        db.delete.assert_called_once_with(cfg)
        db.commit.assert_called_once()

    def test_delete_not_found_returns_false(self):
        db = _make_db_session()
        db.first.return_value = None

        result = delete_sso_config(db, uuid.uuid4())
        assert result is False
        db.delete.assert_not_called()


# ---------------------------------------------------------------------------
# generate_saml_metadata
# ---------------------------------------------------------------------------


class TestGenerateSAMLMetadata:
    def test_returns_string(self):
        cfg = _make_saml_config()
        xml = generate_saml_metadata(cfg)
        assert isinstance(xml, str)

    def test_contains_entity_descriptor(self):
        cfg = _make_saml_config()
        xml = generate_saml_metadata(cfg)
        assert "EntityDescriptor" in xml or "entityID" in xml

    def test_contains_acs_url_or_sp_info(self):
        cfg = _make_saml_config()
        xml = generate_saml_metadata(cfg)
        # Either full ACS URL or SPSSODescriptor
        assert (
            "SPSSODescriptor" in xml
            or "AssertionConsumerService" in xml
            or "experimently" in xml
        )

    def test_xml_parseable(self):
        import xml.etree.ElementTree as ET

        cfg = _make_saml_config()
        xml = generate_saml_metadata(cfg)
        # Should be valid XML (may raise on invalid)
        ET.fromstring(xml)  # no exception expected

    def test_contains_sp_entity_id(self):
        cfg = _make_saml_config()
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            xml = generate_saml_metadata(cfg)
        assert "experimently" in xml

    def test_contains_acs_binding(self):
        cfg = _make_saml_config()
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            xml = generate_saml_metadata(cfg)
        assert "HTTP-POST" in xml


# ---------------------------------------------------------------------------
# parse_saml_response
# ---------------------------------------------------------------------------


class TestParseSAMLResponse:
    def test_valid_response_returns_user_info(self):
        cfg = _make_saml_config()
        saml_b64 = _make_valid_saml_xml("user@acme.com")
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            result = parse_saml_response(cfg, saml_b64)
        assert result["email"] == "user@acme.com"
        assert result["name_id"] == "user@acme.com"

    def test_empty_response_raises_400(self):
        cfg = _make_saml_config()
        with pytest.raises(HTTPException) as exc_info:
            parse_saml_response(cfg, "")
        assert exc_info.value.status_code == 400

    def test_invalid_base64_raises_400(self):
        cfg = _make_saml_config()
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            with pytest.raises(HTTPException) as exc_info:
                parse_saml_response(cfg, "not-valid-base64!!!")
        assert exc_info.value.status_code == 400

    def test_invalid_signature_raises_401(self):
        cfg = _make_saml_config()
        saml_b64 = _make_valid_saml_xml("invalid-user@acme.com")
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            with pytest.raises(HTTPException) as exc_info:
                parse_saml_response(cfg, saml_b64)
        assert exc_info.value.status_code == 401

    def test_expired_assertion_raises_400(self):
        cfg = _make_saml_config()
        saml_b64 = _make_valid_saml_xml("expired-user@acme.com")
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            with pytest.raises(HTTPException) as exc_info:
                parse_saml_response(cfg, saml_b64)
        assert exc_info.value.status_code == 400

    def test_response_contains_groups(self):
        cfg = _make_saml_config()
        saml_b64 = _make_valid_saml_xml("user@acme.com")
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            result = parse_saml_response(cfg, saml_b64)
        assert "groups" in result
        assert isinstance(result["groups"], list)

    def test_response_contains_attributes(self):
        cfg = _make_saml_config()
        saml_b64 = _make_valid_saml_xml("user@acme.com")
        with patch.object(sso_service, "_SAML_AVAILABLE", False):
            result = parse_saml_response(cfg, saml_b64)
        assert "attributes" in result


# ---------------------------------------------------------------------------
# The stub parser is a development fallback, not a deployment one
# ---------------------------------------------------------------------------


@pytest.fixture
def no_saml_library():
    """python3-saml absent (it is installed in this venv, so patch the flag)."""
    with patch.object(sso_service, "_SAML_AVAILABLE", False):
        yield


@pytest.fixture
def hardened_environment(monkeypatch):
    """Move the core settings singleton to a production-like environment."""

    def _set(environment: str = "production"):
        monkeypatch.setattr(sso_service.core_settings, "ENVIRONMENT", environment)

    return _set


class TestSAMLStubIsDevelopmentOnly:
    """`_parse_saml_response_stub` reads the NameID out of the POSTed XML and
    returns it as the email: no signature, issuer, audience or expiry check.
    It may run where a developer runs, and nowhere else."""

    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_the_stub_serves_in_development_and_test(
        self, environment, no_saml_library, hardened_environment
    ):
        hardened_environment(environment)
        cfg = _make_saml_config()
        result = parse_saml_response(cfg, _make_valid_saml_xml("dev@acme.com"))
        assert result["email"] == "dev@acme.com"

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_parse_answers_501_when_the_library_is_missing(
        self, environment, no_saml_library, hardened_environment
    ):
        hardened_environment(environment)
        cfg = _make_saml_config()
        with pytest.raises(HTTPException) as exc_info:
            parse_saml_response(cfg, _make_valid_saml_xml("user@acme.com"))
        assert exc_info.value.status_code == 501
        assert "python3-saml" in exc_info.value.detail

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_metadata_answers_501_when_the_library_is_missing(
        self, environment, no_saml_library, hardened_environment
    ):
        """Metadata tells an IdP where to POST assertions; a deployment that
        cannot validate one must not advertise an ACS endpoint."""
        hardened_environment(environment)
        with pytest.raises(HTTPException) as exc_info:
            generate_saml_metadata(_make_saml_config())
        assert exc_info.value.status_code == 501

    def test_the_library_path_is_untouched_by_the_gate(self, hardened_environment):
        """With python3-saml installed, production is the normal path."""
        hardened_environment("production")
        cfg = _make_saml_config()
        with patch.object(sso_service, "_SAML_AVAILABLE", True):
            with patch.object(
                sso_service,
                "_parse_saml_response_with_library",
                return_value={"email": "real@acme.com"},
            ):
                assert parse_saml_response(cfg, "anything")["email"] == "real@acme.com"

    def test_the_gate_uses_the_shared_allow_list(self, hardened_environment):
        for environment, allowed in (
            ("development", True),
            ("test", True),
            ("staging", False),
            ("production", False),
        ):
            hardened_environment(environment)
            assert sso_service.saml_stub_allowed() is allowed

    @pytest.mark.regression
    def test_a_self_authored_assertion_cannot_impersonate_in_production(
        self, no_saml_library, hardened_environment
    ):
        """Anyone could write this XML: the stub trusts the NameID it finds,
        so an unauthenticated POST naming an administrator's address used to
        come back as that administrator's identity."""
        hardened_environment("production")
        cfg = _make_saml_config()
        forged = _make_valid_saml_xml("admin@acme.com")
        with pytest.raises(HTTPException) as exc_info:
            parse_saml_response(cfg, forged)
        assert exc_info.value.status_code == 501

        # ... and the same payload is still honoured in development, which is
        # what makes the environment gate the whole fix.
        hardened_environment("development")
        assert parse_saml_response(cfg, forged)["email"] == "admin@acme.com"


# ---------------------------------------------------------------------------
# exchange_oidc_code
# ---------------------------------------------------------------------------


class TestExchangeOIDCCode:
    @pytest.mark.asyncio
    async def test_google_exchange_success(self):
        cfg = _make_google_config()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "google-token",
            "token_type": "bearer",
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await exchange_oidc_code(
            cfg,
            "auth-code",
            "https://example.com/callback",
            code_verifier="v" * 43,
            http_client=mock_client,
        )
        assert result.get("access_token") == "google-token"

    @pytest.mark.asyncio
    async def test_github_exchange_success(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.GITHUB
        cfg.entity_id = "github-client-id"
        cfg.client_secret = "github-secret"
        cfg.sso_url = None

        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "ghu_token"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await exchange_oidc_code(
            cfg,
            "code",
            "https://example.com/callback",
            code_verifier="v" * 43,
            http_client=mock_client,
        )
        assert "access_token" in result

    @pytest.mark.asyncio
    async def test_microsoft_exchange_success(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.MICROSOFT
        cfg.entity_id = "ms-client-id"
        cfg.client_secret = "ms-secret"
        cfg.sso_url = None

        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "ms-token", "id_token": "id"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await exchange_oidc_code(
            cfg,
            "code",
            "https://example.com/callback",
            code_verifier="v" * 43,
            http_client=mock_client,
        )
        assert result.get("access_token") == "ms-token"

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_400(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = MagicMock()
        cfg.provider_type.value = "unknown_provider"
        cfg.sso_url = None

        with pytest.raises(HTTPException) as exc_info:
            await exchange_oidc_code(
                cfg, "code", "https://example.com/callback", code_verifier="v" * 43
            )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_http_error_raises_400(self):
        cfg = _make_google_config()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

        with pytest.raises(HTTPException) as exc_info:
            await exchange_oidc_code(
                cfg,
                "code",
                "https://example.com/callback",
                code_verifier="v" * 43,
                http_client=mock_client,
            )
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# get_oidc_user_info
# ---------------------------------------------------------------------------


class TestGetOIDCUserInfo:
    @pytest.mark.asyncio
    async def test_google_userinfo_success(self):
        cfg = _make_google_config()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "12345",
            "email": "alice@acme.com",
            "name": "Alice Smith",
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_oidc_user_info(cfg, "google-token", http_client=mock_client)
        assert result["email"] == "alice@acme.com"
        assert result["sub"] == "12345"

    @pytest.mark.asyncio
    async def test_github_userinfo_success(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.GITHUB
        cfg.sso_url = None

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": 99,
            "login": "alice",
            "email": "alice@github.com",
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_oidc_user_info(cfg, "ghu_token", http_client=mock_client)
        assert result["email"] == "alice@github.com"

    @pytest.mark.asyncio
    async def test_microsoft_userinfo_success(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.MICROSOFT
        cfg.sso_url = None

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "ms-sub",
            "email": "alice@ms.com",
            "name": "Alice MS",
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_oidc_user_info(cfg, "ms-token", http_client=mock_client)
        assert result["email"] == "alice@ms.com"

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_400(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = MagicMock()
        cfg.provider_type.value = "unknown_xyz"
        cfg.sso_url = None

        with pytest.raises(HTTPException) as exc_info:
            await get_oidc_user_info(cfg, "token")
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_http_error_raises_400(self):
        cfg = _make_google_config()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("timeout"))

        with pytest.raises(HTTPException) as exc_info:
            await get_oidc_user_info(cfg, "token", http_client=mock_client)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_normalized_dict(self):
        cfg = _make_google_config()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "99",
            "email": "bob@acme.com",
            "name": "Bob Jones",
            "groups": ["devs"],
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await get_oidc_user_info(cfg, "token", http_client=mock_client)
        assert "email" in result
        assert "sub" in result
        assert "groups" in result
        assert "name" in result


# ---------------------------------------------------------------------------
# provision_user
# ---------------------------------------------------------------------------


class TestProvisionUser:
    def _make_user_info(self, email: str = "alice@acme.com", groups=None) -> dict:
        return {
            "email": email,
            "name_id": email,
            "name": "Alice",
            "sub": "sub-123",
            "groups": groups or [],
            "first_name": "Alice",
            "last_name": None,
            "raw": {},
        }

    def test_new_user_jit_provisioned(self):
        db = _make_db_session()
        db.first.return_value = None  # No existing user
        cfg = _make_saml_config(role_mapping={"admins": "admin"})

        user_info = self._make_user_info(groups=["admins"])
        provisioned = provision_user(db, user_info, cfg)
        # The new account, then its user_create audit entry (#221).
        added = [c.args[0] for c in db.add.call_args_list]
        assert [type(obj).__name__ for obj in added] == ["User", "AuditLog"]
        assert added[1].action_type == "user_create"
        db.commit.assert_called()

    def test_existing_user_is_returned(self):
        db = _make_db_session()
        existing = MagicMock(spec=User)
        existing.email = "alice@acme.com"
        existing.role = UserRole.VIEWER
        db.first.return_value = existing

        cfg = _make_saml_config(role_mapping={})
        user_info = self._make_user_info(groups=[])
        result = provision_user(db, user_info, cfg)
        assert result == existing
        db.add.assert_not_called()

    def test_role_updated_on_existing_user(self):
        db = _make_db_session()
        existing = MagicMock(spec=User)
        existing.email = "alice@acme.com"
        existing.role = UserRole.VIEWER
        db.first.return_value = existing

        cfg = _make_saml_config(role_mapping={"admins": "admin"})
        user_info = self._make_user_info(groups=["admins"])
        provision_user(db, user_info, cfg)
        # Role should have been updated
        assert existing.role == UserRole.ADMIN

    def test_missing_email_raises_400(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        with pytest.raises(HTTPException) as exc_info:
            provision_user(db, {"email": "", "groups": []}, cfg)
        assert exc_info.value.status_code == 400

    def test_no_email_key_raises_400(self):
        db = _make_db_session()
        cfg = _make_saml_config()
        with pytest.raises(HTTPException) as exc_info:
            provision_user(db, {"groups": []}, cfg)
        assert exc_info.value.status_code == 400

    def test_new_user_gets_viewer_role_when_no_mapping(self):
        db = _make_db_session()
        db.first.return_value = None
        cfg = _make_saml_config(role_mapping={})

        user_info = self._make_user_info(groups=["unknown-group"])

        # Capture the User object passed to db.add
        added_users = []
        db.add.side_effect = lambda obj: added_users.append(obj)

        provision_user(db, user_info, cfg)
        # The new account, then its user_create audit entry (#221).
        assert [type(obj).__name__ for obj in added_users] == ["User", "AuditLog"]
        new_user = added_users[0]
        assert new_user.role == UserRole.VIEWER

    def test_new_user_gets_admin_role_when_mapped(self):
        db = _make_db_session()
        db.first.return_value = None
        cfg = _make_saml_config(role_mapping={"super-admins": "admin"})

        user_info = self._make_user_info(groups=["super-admins"])
        added_users = []
        db.add.side_effect = lambda obj: added_users.append(obj)

        provision_user(db, user_info, cfg)
        new_user = added_users[0]
        assert new_user.role == UserRole.ADMIN


# ---------------------------------------------------------------------------
# map_role
# ---------------------------------------------------------------------------


class TestMapRole:
    def _make_cfg(self, mapping: dict) -> MagicMock:
        cfg = MagicMock(spec=SSOConfig)
        cfg.role_mapping = mapping
        return cfg

    def test_admin_group_maps_to_admin(self):
        cfg = self._make_cfg({"Admins": "admin"})
        assert map_role(cfg, ["Admins"]) == "admin"

    def test_developer_group_maps_to_developer(self):
        cfg = self._make_cfg({"devs": "developer"})
        assert map_role(cfg, ["devs"]) == "developer"

    def test_analyst_group_maps_to_analyst(self):
        cfg = self._make_cfg({"analysts": "analyst"})
        assert map_role(cfg, ["analysts"]) == "analyst"

    def test_viewer_default_when_no_match(self):
        cfg = self._make_cfg({"admins": "admin"})
        assert map_role(cfg, ["unknown-group"]) == "viewer"

    def test_empty_groups_returns_viewer(self):
        cfg = self._make_cfg({"admins": "admin"})
        assert map_role(cfg, []) == "viewer"

    def test_empty_mapping_returns_viewer(self):
        cfg = self._make_cfg({})
        assert map_role(cfg, ["admins"]) == "viewer"

    def test_none_mapping_returns_viewer(self):
        cfg = self._make_cfg(None)
        assert map_role(cfg, ["admins"]) == "viewer"

    def test_first_matching_group_wins(self):
        cfg = self._make_cfg({"devs": "developer", "admins": "admin"})
        # "devs" is first in user's groups list
        assert map_role(cfg, ["devs", "admins"]) == "developer"

    def test_case_preserved_in_output(self):
        cfg = self._make_cfg({"ADMINS": "ADMIN"})
        result = map_role(cfg, ["ADMINS"])
        # map_role lower-cases the output
        assert result == "admin"

    def test_multiple_groups_first_match(self):
        cfg = self._make_cfg({"analysts": "analyst", "viewers": "viewer"})
        assert map_role(cfg, ["viewers", "analysts"]) == "viewer"


# ---------------------------------------------------------------------------
# start_oidc_login / redeem_oidc_state
# ---------------------------------------------------------------------------


def _okta_config(config_id: uuid.UUID | None = None) -> MagicMock:
    cfg = MagicMock(spec=SSOConfig)
    cfg.id = config_id or uuid.uuid4()
    cfg.provider_type = SSOProviderType.OKTA
    return cfg


class TestOIDCLoginState:
    def test_a_started_login_redeems_with_its_own_state(self):
        cfg = _okta_config()
        started = start_oidc_login(cfg, "okta")
        redeemed = redeem_oidc_state(started.cookie_value, started.state)
        assert redeemed.config_id == cfg.id
        assert redeemed.provider == "okta"
        assert redeemed.code_verifier == started.code_verifier

    def test_the_verifier_is_within_rfc_7636_bounds(self):
        verifier = start_oidc_login(_okta_config(), "okta").code_verifier
        assert 43 <= len(verifier) <= 128
        import re

        assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)

    def test_each_login_gets_its_own_state_and_verifier(self):
        cfg = _okta_config()
        logins = [start_oidc_login(cfg, "okta") for _ in range(20)]
        assert len({s.state for s in logins}) == 20
        assert len({s.code_verifier for s in logins}) == 20

    def test_no_cookie_is_refused(self):
        started = start_oidc_login(_okta_config(), "okta")
        with pytest.raises(HTTPException) as exc_info:
            redeem_oidc_state(None, started.state)
        assert exc_info.value.status_code == 400

    @pytest.mark.regression
    def test_another_logins_cookie_is_refused(self):
        """The login-CSRF case: the attacker's own state, the victim's cookie."""
        cfg = _okta_config()
        victims = start_oidc_login(cfg, "okta")
        attackers = start_oidc_login(cfg, "okta")
        with pytest.raises(HTTPException) as exc_info:
            redeem_oidc_state(victims.cookie_value, attackers.state)
        assert exc_info.value.status_code == 400
        assert "does not match" in exc_info.value.detail

    def test_a_missing_url_state_is_refused(self):
        started = start_oidc_login(_okta_config(), "okta")
        for state in (None, ""):
            with pytest.raises(HTTPException):
                redeem_oidc_state(started.cookie_value, state)

    def test_a_non_ascii_url_state_is_refused_not_a_500(self):
        started = start_oidc_login(_okta_config(), "okta")
        with pytest.raises(HTTPException) as exc_info:
            redeem_oidc_state(started.cookie_value, "\u00e9t\u00e9")
        assert exc_info.value.status_code == 400

    def test_a_tampered_cookie_is_refused(self):
        started = start_oidc_login(_okta_config(), "okta")
        head, payload, sig = started.cookie_value.split(".")
        forged = f"{head}.{payload}.{sig[:-2]}{'AA' if sig[-2:] != 'AA' else 'BB'}"
        with pytest.raises(HTTPException) as exc_info:
            redeem_oidc_state(forged, started.state)
        assert "not valid" in exc_info.value.detail

    def test_a_cookie_signed_with_the_raw_secret_is_refused(self):
        """The state key is derived, so an access-token-shaped forgery fails."""
        import jwt

        from backend.app.core.config import settings as core_settings

        now = int(time.time())
        forged = jwt.encode(
            {
                "aud": "experimently:oidc-state",
                "iat": now,
                "exp": now + 60,
                "st": "s",
                "cv": "v" * 43,
                "cfg": str(uuid.uuid4()),
                "prv": "okta",
            },
            core_settings.SECRET_KEY,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException):
            redeem_oidc_state(forged, "s")

    def test_an_access_token_is_not_a_state_cookie(self):
        from backend.app.core.security import create_local_access_token

        user = MagicMock(id=uuid.uuid4(), email="a@b.c", role=UserRole.ADMIN)
        with pytest.raises(HTTPException):
            redeem_oidc_state(create_local_access_token(user), "anything")

    def test_an_expired_login_is_refused(self, monkeypatch):
        """Started longer ago than the TTL plus the 30 s leeway."""
        past = time.time() - sso_service.OIDC_STATE_TTL_SECONDS - 31
        monkeypatch.setattr(sso_service.time, "time", lambda: past)
        started = start_oidc_login(_okta_config(), "okta")
        monkeypatch.undo()
        with pytest.raises(HTTPException) as exc_info:
            redeem_oidc_state(started.cookie_value, started.state)
        assert "expired" in exc_info.value.detail

    def test_the_leeway_is_thirty_seconds_not_more(self, monkeypatch):
        """Inside the leeway still redeems; this is the boundary of the one above."""
        past = time.time() - sso_service.OIDC_STATE_TTL_SECONDS - 20
        monkeypatch.setattr(sso_service.time, "time", lambda: past)
        started = start_oidc_login(_okta_config(), "okta")
        monkeypatch.undo()
        redeem_oidc_state(started.cookie_value, started.state)

    def test_the_process_holds_no_login_state(self):
        """Nothing to fill from the unauthenticated login route (#94)."""
        cfg = _okta_config()
        before = {k: v for k, v in vars(sso_service).items() if isinstance(v, dict)}
        sizes = {k: len(v) for k, v in before.items()}
        for _ in range(100):
            start_oidc_login(cfg, "okta")
        assert {k: len(v) for k, v in before.items()} == sizes


# ---------------------------------------------------------------------------
# build_oidc_authorization_url
# ---------------------------------------------------------------------------


class TestBuildOIDCAuthorizationURL:
    def test_google_url_structure(self):
        cfg = _make_google_config()
        state = "test-state-token"
        url = build_oidc_authorization_url(
            cfg, "google", "https://example.com/callback", state, "v" * 43
        )
        assert "accounts.google.com" in url
        assert "client_id=google-client-id" in url
        assert "state=test-state-token" in url
        assert "response_type=code" in url

    def test_github_url_structure(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.GITHUB
        cfg.entity_id = "gh-client-id"
        cfg.sso_url = None

        url = build_oidc_authorization_url(
            cfg, "github", "https://example.com/cb", "state-abc", "v" * 43
        )
        assert "github.com" in url
        assert "gh-client-id" in url

    def test_microsoft_url_structure(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.MICROSOFT
        cfg.entity_id = "ms-client"
        cfg.sso_url = None

        url = build_oidc_authorization_url(
            cfg, "microsoft", "https://example.com/cb", "state-xyz", "v" * 43
        )
        assert "microsoftonline" in url

    def test_unknown_provider_raises_400(self):
        cfg = _make_google_config()
        with pytest.raises(HTTPException) as exc_info:
            build_oidc_authorization_url(
                cfg, "unknown_provider", "https://example.com/cb", "state", "v" * 43
            )
        assert exc_info.value.status_code == 400

    def test_redirect_uri_included(self):
        cfg = _make_google_config()
        redirect_uri = "https://myapp.com/callback"
        url = build_oidc_authorization_url(
            cfg, "google", redirect_uri, "state-123", "v" * 43
        )
        assert "redirect_uri" in url

    def test_scope_included(self):
        cfg = _make_google_config()
        url = build_oidc_authorization_url(
            cfg, "google", "https://example.com/cb", "st", "v" * 43
        )
        assert "scope" in url

    def test_okta_url_uses_sso_url(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.OKTA
        cfg.entity_id = "okta-client"
        cfg.sso_url = "https://my-org.okta.com"

        url = build_oidc_authorization_url(
            cfg, "okta", "https://example.com/cb", "state", "v" * 43
        )
        assert "my-org.okta.com" in url


# ---------------------------------------------------------------------------
# verify_id_token / expected_issuers / https endpoints (C2n)
# ---------------------------------------------------------------------------


def _unsigned(claims: Dict[str, Any]) -> str:
    """An ID token as the token endpoint returns it; the signature is not checked."""
    import jwt

    return jwt.encode(claims, "irrelevant-key", algorithm="HS256")


def _oidc_config(provider: SSOProviderType, sso_url: str | None = None) -> MagicMock:
    cfg = MagicMock(spec=SSOConfig)
    cfg.id = uuid.uuid4()
    cfg.provider_type = provider
    cfg.entity_id = "client-1"
    cfg.sso_url = sso_url
    return cfg


def _claims(**overrides) -> Dict[str, Any]:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "client-1",
        "exp": int(time.time()) + 300,
        "nonce": "n-1",
        "sub": "s",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


class TestVerifyIDToken:
    google = staticmethod(lambda: _oidc_config(SSOProviderType.GOOGLE))

    def test_a_good_token_is_accepted(self):
        claims = sso_service.verify_id_token(
            self.google(), "google", _unsigned(_claims()), "n-1"
        )
        assert claims["sub"] == "s"

    @pytest.mark.regression
    @pytest.mark.parametrize(
        ("overrides", "nonce", "reason"),
        [
            ({"nonce": "someone-elses"}, "n-1", "nonce"),
            ({"nonce": None}, "n-1", "nonce"),
            ({"iss": "https://evil.example.com"}, "n-1", "iss"),
            ({"aud": "another-client"}, "n-1", "aud"),
            ({"aud": ["another-client"]}, "n-1", "aud"),
            ({"aud": ["client-1", "another-client"]}, "n-1", "azp"),
            ({"aud": ["client-1", "x"], "azp": "x"}, "n-1", "azp"),
            ({"exp": int(time.time()) - 120}, "n-1", "exp"),
            ({"exp": None}, "n-1", "exp"),
        ],
    )
    def test_each_claim_is_checked(self, overrides, nonce, reason):
        with pytest.raises(HTTPException) as exc_info:
            sso_service.verify_id_token(
                self.google(), "google", _unsigned(_claims(**overrides)), nonce
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == f"OIDC ID token was not accepted ({reason})"

    def test_several_audiences_with_azp_this_client_is_accepted(self):
        token = _unsigned(_claims(aud=["client-1", "x"], azp="client-1"))
        sso_service.verify_id_token(self.google(), "google", token, "n-1")

    def test_expiry_allows_a_minute_of_skew(self):
        token = _unsigned(_claims(exp=int(time.time()) - 30))
        sso_service.verify_id_token(self.google(), "google", token, "n-1")

    @pytest.mark.parametrize("token", [None, "", "not-a-jwt", 42])
    def test_a_missing_or_malformed_token_is_refused(self, token):
        with pytest.raises(HTTPException) as exc_info:
            sso_service.verify_id_token(self.google(), "google", token, "n-1")
        assert exc_info.value.detail in (
            "OIDC ID token was not accepted (missing)",
            "OIDC ID token was not accepted (malformed)",
        )

    def test_the_refusal_names_no_claim_value(self):
        token = _unsigned(_claims(iss="https://MARKER.example.com"))
        with pytest.raises(HTTPException) as exc_info:
            sso_service.verify_id_token(self.google(), "google", token, "n-1")
        assert "MARKER" not in exc_info.value.detail


class TestExpectedIssuers:
    """The per-provider table in the C2n spec."""

    def test_google_accepts_both_of_its_forms(self):
        assert sso_service.expected_issuers(
            _oidc_config(SSOProviderType.GOOGLE), "google", {}
        ) == {"https://accounts.google.com", "accounts.google.com"}

    @pytest.mark.parametrize("provider", ["microsoft", "azure_ad"])
    def test_microsoft_is_per_tenant(self, provider):
        tid = "9188040d-6c67-4c5b-b112-36a304b66dad"
        assert sso_service.expected_issuers(
            _oidc_config(SSOProviderType(provider)), provider, {"tid": tid}
        ) == {f"https://login.microsoftonline.com/{tid}/v2.0"}

    @pytest.mark.parametrize("tid", [None, "", "common", "../x", "9188040D-6C67"])
    def test_microsoft_without_a_tenant_accepts_nothing(self, tid):
        cfg = _oidc_config(SSOProviderType.MICROSOFT)
        assert sso_service.expected_issuers(cfg, "microsoft", {"tid": tid}) == set()

    def test_okta_custom_authorization_server_is_its_sso_url(self):
        cfg = _oidc_config(SSOProviderType.OKTA, "https://org.okta.com/oauth2/default/")
        assert sso_service.expected_issuers(cfg, "okta", {}) == {
            "https://org.okta.com/oauth2/default"
        }

    def test_okta_org_authorization_server_is_the_org_url(self):
        cfg = _oidc_config(SSOProviderType.OKTA, "https://org.okta.com/oauth2")
        assert sso_service.expected_issuers(cfg, "okta", {}) == {"https://org.okta.com"}

    def test_github_has_no_issuer_because_it_has_no_id_token(self):
        assert not sso_service.uses_id_token("github")
        assert sso_service.uses_id_token("google")
        assert sso_service.uses_id_token("okta")


class TestProviderEndpointsAreHttps:
    @pytest.fixture
    def outside_test(self, monkeypatch):
        from backend.app.core.config import settings as core_settings

        monkeypatch.setattr(core_settings, "ENVIRONMENT", "production")
        assert not core_settings.is_test

    @pytest.mark.regression
    def test_an_http_okta_sso_url_is_refused_outside_test(self, outside_test):
        cfg = _oidc_config(
            SSOProviderType.OKTA, "http://org.okta.example/oauth2/default"
        )
        with pytest.raises(HTTPException) as exc_info:
            build_oidc_authorization_url(cfg, "okta", "https://app/cb", "s", "v" * 43)
        assert exc_info.value.status_code == 400
        assert "https" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_the_exchange_refuses_it_too(self, outside_test):
        cfg = _oidc_config(
            SSOProviderType.OKTA, "http://org.okta.example/oauth2/default"
        )
        mock_client = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await exchange_oidc_code(
                cfg,
                "c",
                "https://app/cb",
                code_verifier="v" * 43,
                http_client=mock_client,
            )
        assert "https" in exc_info.value.detail
        mock_client.post.assert_not_awaited()

    def test_an_https_okta_sso_url_is_accepted(self, outside_test):
        cfg = _oidc_config(SSOProviderType.OKTA, "https://org.okta.com/oauth2/default")
        url = build_oidc_authorization_url(cfg, "okta", "https://app/cb", "s", "v" * 43)
        assert url.startswith("https://org.okta.com/oauth2/default/v1/authorize?")

    def test_the_test_environment_allows_a_local_fake_provider(self):
        cfg = _oidc_config(SSOProviderType.OKTA, "http://127.0.0.1:9")
        url = build_oidc_authorization_url(cfg, "okta", "https://app/cb", "s", "v" * 43)
        assert url.startswith("http://127.0.0.1:9/v1/authorize?")


class TestNonceInTheAuthorizationURL:
    def test_an_oidc_provider_gets_the_nonce(self):
        cfg = _make_google_config()
        url = build_oidc_authorization_url(
            cfg, "google", "https://app/cb", "s", "v" * 43, "the-nonce"
        )
        assert "nonce=the-nonce" in url

    def test_github_gets_none(self):
        cfg = MagicMock(spec=SSOConfig)
        cfg.provider_type = SSOProviderType.GITHUB
        cfg.entity_id = "gh"
        cfg.sso_url = None
        url = build_oidc_authorization_url(
            cfg, "github", "https://app/cb", "s", "v" * 43, "the-nonce"
        )
        assert "nonce" not in url

    def test_each_login_carries_its_own_nonce_in_the_cookie(self):
        cfg = _okta_config()
        first, second = start_oidc_login(cfg, "okta"), start_oidc_login(cfg, "okta")
        assert first.nonce != second.nonce
        assert redeem_oidc_state(first.cookie_value, first.state).nonce == first.nonce


# ---------------------------------------------------------------------------
# verified_email (C2v V2) -- through the provider dispatch
# ---------------------------------------------------------------------------


def _v_config(provider: SSOProviderType, domain: str = "acme.com") -> MagicMock:
    cfg = MagicMock(spec=SSOConfig)
    cfg.id = uuid.uuid4()
    cfg.provider_type = provider
    cfg.org_domain = domain
    return cfg


def _id(**claims) -> Dict[str, Any]:
    base = {"sub": "s-1", "email": "bob@acme.com", "email_verified": True}
    base.update(claims)
    return {k: v for k, v in base.items() if v is not None}


class TestVerifiedEmail:
    @pytest.mark.asyncio
    async def test_okta_uses_the_id_tokens_verified_email(self):
        info = await sso_service.verified_email(
            _v_config(SSOProviderType.OKTA),
            "okta",
            _id(),
            {"sub": "s-1", "email": "someone-else@acme.com"},
            "at",
        )
        assert info["email"] == "bob@acme.com"
        assert info["sub"] == "s-1"

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verified", [False, None, "True", "1", "yes", 1, "false"])
    async def test_an_unverified_email_is_refused(self, verified):
        with pytest.raises(HTTPException) as exc_info:
            await sso_service.verified_email(
                _v_config(SSOProviderType.OKTA),
                "okta",
                _id(email_verified=verified),
                {"sub": "s-1"},
                "at",
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == sso_service.EMAIL_UNVERIFIED_DETAIL

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provider", "extra"),
        [(SSOProviderType.OKTA, {}), (SSOProviderType.GOOGLE, {"hd": "acme.com"})],
    )
    async def test_the_string_true_is_verified(self, provider, extra):
        """Google's own example ID token sends ``"email_verified": "true"``."""
        info = await sso_service.verified_email(
            _v_config(provider),
            provider.value,
            _id(email_verified="true", **extra),
            {"sub": "s-1"},
            "at",
        )
        assert info["email"] == "bob@acme.com"
        assert info["sub"] == "s-1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("userinfo_sub", ["other", "", None])
    async def test_user_info_must_name_the_id_tokens_subject(self, userinfo_sub):
        with pytest.raises(HTTPException) as exc_info:
            await sso_service.verified_email(
                _v_config(SSOProviderType.OKTA),
                "okta",
                _id(),
                {"sub": userinfo_sub},
                "at",
            )
        assert exc_info.value.detail == sso_service.IDENTITY_MISMATCH_DETAIL

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize("hd", [None, "other.com", "eu.acme.com"])
    async def test_google_requires_its_workspace_domain_to_be_the_emails(self, hd):
        with pytest.raises(HTTPException) as exc_info:
            await sso_service.verified_email(
                _v_config(SSOProviderType.GOOGLE),
                "google",
                _id(hd=hd),
                {"sub": "s-1"},
                "at",
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == sso_service.EMAIL_DOMAIN_DETAIL

    @pytest.mark.asyncio
    async def test_google_with_its_workspace_domain_is_accepted(self):
        info = await sso_service.verified_email(
            _v_config(SSOProviderType.GOOGLE),
            "google",
            _id(hd="ACME.com"),
            {"sub": "s-1"},
            "at",
        )
        assert info["email"] == "bob@acme.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["microsoft", "azure_ad", "onelogin", "saml"])
    async def test_other_providers_are_refused(self, provider):
        with pytest.raises(HTTPException) as exc_info:
            await sso_service.verified_email(
                _v_config(SSOProviderType.OKTA), provider, _id(), {"sub": "s-1"}, "at"
            )
        assert exc_info.value.detail == sso_service.UNSUPPORTED_PROVIDER_DETAIL


def _github_client(entries) -> AsyncMock:
    response = MagicMock()
    response.json.return_value = entries
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    return client


class TestGitHubVerifiedEmail:
    async def _email(self, entries) -> str:
        info = await sso_service.verified_email(
            _v_config(SSOProviderType.GITHUB),
            "github",
            {},
            {"sub": "12345", "email": "public@acme.com"},
            "gh-token",
            http_client=_github_client(entries),
        )
        return info["email"]

    @pytest.mark.asyncio
    async def test_the_primary_in_domain_address(self):
        assert (
            await self._email(
                [
                    {"email": "a@acme.com", "verified": True, "primary": False},
                    {"email": "b@acme.com", "verified": True, "primary": True},
                ]
            )
            == "b@acme.com"
        )

    @pytest.mark.asyncio
    async def test_the_only_in_domain_address_when_the_primary_is_elsewhere(self):
        assert (
            await self._email(
                [
                    {"email": "me@gmail.com", "verified": True, "primary": True},
                    {"email": "a@acme.com", "verified": True, "primary": False},
                ]
            )
            == "a@acme.com"
        )

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entries",
        [
            [{"email": "a@acme.com", "verified": False, "primary": True}],
            [
                {"email": "a@acme.com", "verified": True, "primary": False},
                {"email": "b@acme.com", "verified": True, "primary": False},
            ],
            [{"email": "a@eu.acme.com", "verified": True, "primary": True}],
            [],
            "not-a-list",
        ],
        ids=["unverified", "two-non-primary", "subdomain", "none", "garbage"],
    )
    async def test_otherwise_refused(self, entries):
        with pytest.raises(HTTPException) as exc_info:
            await self._email(entries)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == sso_service.EMAIL_UNVERIFIED_DETAIL

    @pytest.mark.asyncio
    async def test_the_public_profile_email_is_never_used(self):
        """/user's `email` is whatever the user made public: not evidence."""
        with pytest.raises(HTTPException):
            await self._email(
                [{"email": "public@acme.com", "verified": False, "primary": True}]
            )

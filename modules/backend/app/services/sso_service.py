"""
SSO Service — EP-037 SSO/SAML & OIDC authentication.

Provides helpers for:
  - CRUD operations on SSOConfig records
  - SAML 2.0 SP metadata generation and assertion parsing
  - OIDC/OAuth2 authorization-code exchange and user-info fetch
  - Just-In-Time (JIT) user provisioning
  - Role mapping from IdP groups to platform roles
  - The OIDC login's state: a signed cookie binding the callback to the
    browser that started the login, carrying the PKCE verifier

SAML operations degrade to stub implementations when the ``python3-saml``
(onelogin) package is not installed, so the service remains importable in
CI/test environments that skip the optional dependency -- but the stub only
*runs* in development and test.  It reads the NameID out of the POSTed XML
without checking a signature, an issuer, an audience or an expiry, so anywhere
else a missing library is an error and every SAML route answers 501
(:func:`_require_saml_support`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

# defusedxml guards against entity-expansion / external-entity attacks when
# parsing IdP-supplied SAML responses (Bandit B314, Semgrep use-defused-xml).
import jwt
from defusedxml import ElementTree as ET
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.app.core.config import settings as core_settings
from backend.app.core.logger import get_log_context
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User, UserRole
from backend.app.schemas.storable_text import contains_unstorable_text
from backend.app.services.audit_service import (
    SYSTEM_SSO_SYNC,
    AuditService,
    audit_identity,
    audit_snapshot,
    role_value,
)
from modules.backend.app.models.sso_config import SSOConfig
from modules.backend.app.settings import settings

# Optional: python3-saml
try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth  # type: ignore
    from onelogin.saml2.settings import OneLogin_Saml2_Settings  # type: ignore

    _SAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    OneLogin_Saml2_Auth = None  # type: ignore
    OneLogin_Saml2_Settings = None  # type: ignore
    _SAML_AVAILABLE = False

# Optional: authlib
try:
    from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore

    _AUTHLIB_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncOAuth2Client = None  # type: ignore
    _AUTHLIB_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Refusals and the dashboard's `sso_error` codes
# ---------------------------------------------------------------------------
#
# Every refusal of an OIDC sign-in is an `SSORefusal`: an HTTPException, so a
# JSON-mode caller (the legacy `/oidc/{provider}/login` flow) gets exactly the
# status and detail it always did, which also carries the code a dashboard
# sign-in is redirected to `/login?sso_error=<code>` with. A plain
# HTTPException that reaches the dashboard callback is `sso_failed`.

SSO_EXPIRED = "sso_expired"
SSO_STATE = "sso_state"
SSO_IDP_ERROR = "sso_idp_error"
SSO_EMAIL = "sso_email"
SSO_UNVERIFIED = "sso_unverified"
SSO_DOMAIN = "sso_domain"
SSO_ACCOUNT = "sso_account"
SSO_INACTIVE = "sso_inactive"
SSO_NOT_CONFIGURED = "sso_not_configured"
SSO_SAML_ONLY = "sso_saml_only"
SSO_FAILED = "sso_failed"

SSO_ERROR_CODES = frozenset(
    {
        SSO_EXPIRED,
        SSO_STATE,
        SSO_IDP_ERROR,
        SSO_EMAIL,
        SSO_UNVERIFIED,
        SSO_DOMAIN,
        SSO_ACCOUNT,
        SSO_INACTIVE,
        SSO_NOT_CONFIGURED,
        SSO_SAML_ONLY,
        SSO_FAILED,
    }
)


class SSORefusal(HTTPException):
    """A refused sign-in: the JSON status and detail, plus its `sso_error` code."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        sso_error: str = SSO_FAILED,
        *,
        idp_error: Optional[str] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.sso_error = sso_error if sso_error in SSO_ERROR_CODES else SSO_FAILED
        self.idp_error = idp_error


#: The shape a request id must have to be shown to a user: in a redirect URL
#: or in a refusal's detail.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def current_request_id() -> Optional[str]:
    """This request's id (the one on its `X-Request-ID`), if it is safe to show."""
    value = get_log_context().get("request_id")
    if isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


#: The ACS's answer to a SAML response it cannot read. Fixed text, plus the
#: request id an administrator searches the API log for; the reason is logged.
SAML_PARSE_FAILED_DETAIL = "Failed to parse SAML response"
#: The same, from the development and test stand-in.
SAML_DECODE_FAILED_DETAIL = "Cannot decode SAML response"
#: How much of the reason the log line keeps.
SAML_LOG_REASON_LIMIT = 200


def _saml_unreadable(detail: str, exc: BaseException) -> HTTPException:
    """A 400 with *detail* and the request id; the reason goes to the log only."""
    request_id = current_request_id()
    reason = str(exc)[:SAML_LOG_REASON_LIMIT]
    logger.warning(
        "SAML response refused: %s: %s reason=%r request_id=%s",
        detail,
        type(exc).__name__,
        reason,
        request_id,
    )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{detail} (request ID: {request_id})" if request_id else detail,
    )


#: Refusal used by every SAML route when ``python3-saml`` is not installed and
#: the environment is not one where the stub may stand in for it.  501 is the
#: same "this deployment does not have it" answer the core gives for a
#: capability no module registered (``backend/app/core/optional_modules.py``).
SAML_NOT_INSTALLED_DETAIL = (
    "SAML single sign-on is not available in this deployment: the optional "
    "'python3-saml' dependency is not installed, and without it assertions "
    "cannot be validated. Install python3-saml (it needs the system xmlsec1 "
    "and libxml2 libraries) and restart the API."
)


def saml_stub_allowed() -> bool:
    """Whether the signature-blind SAML stub below may stand in for the library.

    ``development`` and ``test`` only -- the allow-list
    ``settings.dev_fallbacks_allowed`` shares with the dev-admin auth bypass,
    read from the *core* settings singleton (the modules' settings only mirror
    ``ENVIRONMENT``).  Asked at call time rather than at import so a test can
    move the environment, and asked as a named allow-list rather than
    ``ENVIRONMENT == "production"`` so staging is hardened too and an
    unrecognised environment name fails closed.
    """
    return bool(core_settings.dev_fallbacks_allowed)


def _require_saml_support() -> None:
    """Raise 501 when neither python3-saml nor the stub may serve this request.

    The stub is a development convenience that reads the NameID out of the
    POSTed XML: it checks no signature, no issuer, no audience and no expiry,
    so an unauthenticated caller who knows an administrator's email address
    could mint a session with a SAML response they wrote themselves.  Anywhere
    but development and test, a missing library is an error, never a fallback.
    """
    if _SAML_AVAILABLE:
        return
    if saml_stub_allowed():
        logger.warning(
            "python3-saml is not installed; serving SAML with the stub parser, "
            "which does NOT validate assertion signatures. ENVIRONMENT=%s.",
            core_settings.ENVIRONMENT,
        )
        return
    logger.error(
        "Refusing a SAML request: python3-saml is not installed and "
        "ENVIRONMENT=%s does not permit the unvalidating stub parser.",
        core_settings.ENVIRONMENT,
    )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=SAML_NOT_INSTALLED_DETAIL,
    )


# ---------------------------------------------------------------------------
# OIDC provider discovery metadata
# ---------------------------------------------------------------------------

_OIDC_PROVIDERS: Dict[str, Dict[str, str]] = {
    "google": {
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "userinfo_endpoint": "https://api.github.com/user",
        "scope": "read:user user:email",
    },
    "microsoft": {
        "authorization_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo_endpoint": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
    },
    "okta": {
        "authorization_endpoint": "{sso_url}/v1/authorize",
        "token_endpoint": "{sso_url}/v1/token",
        "userinfo_endpoint": "{sso_url}/v1/userinfo",
        "scope": "openid email profile groups",
    },
    "azure_ad": {
        "authorization_endpoint": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_endpoint": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "userinfo_endpoint": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
    },
    "onelogin": {
        "authorization_endpoint": "https://{subdomain}.onelogin.com/oidc/2/auth",
        "token_endpoint": "https://{subdomain}.onelogin.com/oidc/2/token",
        "userinfo_endpoint": "https://{subdomain}.onelogin.com/oidc/2/me",
        "scope": "openid email profile groups",
    },
}


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


ORG_DOMAIN_REQUIRED_DETAIL = "org_domain is required"
ORG_DOMAIN_TAKEN_DETAIL = "An SSO configuration already exists for this domain"
#: The 400 for a configuration whose OIDC endpoints are built from an
#: ``sso_url`` that is not https: when the configuration is saved, and when a
#: sign-in uses it (``_provider_endpoint``).
SSO_URL_HTTPS_DETAIL = (
    "OIDC provider endpoints must use https; check this SSO configuration's sso_url"
)
#: The 400 for a SAML configuration whose ``sso_url``, the IdP's single sign-on
#: URL, is not https, when the configuration is saved (#1100). A sign-in does
#: not check it: the API never contacts a SAML ``sso_url``.
SAML_SSO_URL_HTTPS_DETAIL = (
    "SAML single sign-on URL must use https; check this SSO configuration's sso_url"
)


def _endpoints_use_sso_url(provider_type: Any) -> bool:
    """Whether the provider's OIDC endpoints are built from ``sso_url``.

    Okta's are (``{sso_url}/v1/token``); the other providers' are constants
    and SAML has none, so their ``sso_url`` never reaches an OIDC request.
    """
    key = getattr(provider_type, "value", provider_type)
    meta = _OIDC_PROVIDERS.get(key) if isinstance(key, str) else None
    return bool(meta) and any("{sso_url}" in value for value in meta.values())


def refuse_non_https_sso_url(provider_type: Any, sso_url: Any) -> None:
    """400 when a configuration's ``sso_url`` must be https and is not, outside test.

    The rule a sign-in applies to the provider endpoints built from
    ``sso_url`` (:func:`_provider_endpoint`), applied when the configuration is
    created or updated, so an ``http://`` URL is refused when it is saved
    rather than at the first sign-in. It applies to the same providers: those
    whose endpoints contain ``{sso_url}`` (Okta today), with
    ``SSO_URL_HTTPS_DETAIL``. It applies to SAML too, whose ``sso_url`` is the
    IdP's single sign-on URL, with ``SAML_SSO_URL_HTTPS_DETAIL`` (#1100); a
    SAML sign-in does not check it. Google, GitHub and the other providers
    ignore ``sso_url``, and an empty value is not checked. The test
    environment, whose fake provider is local, is exempt, as it is at sign-in.
    """
    if not sso_url:
        return
    if _endpoints_use_sso_url(provider_type):
        detail = SSO_URL_HTTPS_DETAIL
    elif getattr(provider_type, "value", provider_type) == "saml":
        detail = SAML_SSO_URL_HTTPS_DETAIL
    else:
        return
    if core_settings.is_test:
        return
    try:
        scheme = urlparse(str(sso_url)).scheme
    except ValueError:
        scheme = ""
    if scheme != "https":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )


def _stored_domain_normalised() -> Any:
    """``SSOConfig.org_domain`` normalised in SQL, as :func:`normalise_domain` does.

    Rows written before ``org_domain`` was normalised on write may carry
    capitals, surrounding spaces or a leading ``@``; this makes them compare
    as the domain they sign in for. SQL ``trim`` strips spaces only, where
    ``str.strip`` strips all whitespace.
    """
    return func.ltrim(func.lower(func.trim(SSOConfig.org_domain)), "@")


def _refuse_taken_domain(
    db: Session, domain: str, exclude_id: Optional[uuid.UUID] = None
) -> None:
    """409 when another configuration's normalised ``org_domain`` is *domain*."""
    query = db.query(SSOConfig).filter(_stored_domain_normalised() == domain)
    if exclude_id is not None:
        query = query.filter(SSOConfig.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ORG_DOMAIN_TAKEN_DETAIL,
        )


def create_sso_config(db: Session, config_data: Dict[str, Any]) -> SSOConfig:
    """Create a new SSOConfig record, storing ``normalise_domain(org_domain)``.

    Raises:
        HTTPException 400 if ``org_domain`` is empty once normalised.
        HTTPException 400 if the provider builds its endpoints from
        ``sso_url``, or is SAML, and it is not https
        (:func:`refuse_non_https_sso_url`).
        HTTPException 409 if another configuration's ``org_domain``
        normalises to the same domain.
    """
    org_domain = normalise_domain(config_data.get("org_domain"))
    if not org_domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ORG_DOMAIN_REQUIRED_DETAIL,
        )
    refuse_non_https_sso_url(
        config_data.get("provider_type"), config_data.get("sso_url")
    )

    _refuse_taken_domain(db, org_domain)

    sso_config = SSOConfig(**{**config_data, "org_domain": org_domain})
    db.add(sso_config)
    db.commit()
    db.refresh(sso_config)
    return sso_config


def get_sso_config(db: Session, org_domain: str) -> Optional[SSOConfig]:
    """Return the SSOConfig for the given *org_domain*, or ``None``."""
    return db.query(SSOConfig).filter(SSOConfig.org_domain == org_domain).first()


def get_sso_config_by_id(db: Session, config_id: uuid.UUID) -> Optional[SSOConfig]:
    """Return the SSOConfig with the given *config_id*, or ``None``."""
    return db.query(SSOConfig).filter(SSOConfig.id == config_id).first()


def list_sso_configs(db: Session) -> List[SSOConfig]:
    """Return all SSOConfig records ordered by creation date."""
    return db.query(SSOConfig).order_by(SSOConfig.created_at).all()


def update_sso_config(
    db: Session,
    config_id: uuid.UUID,
    data: Dict[str, Any],
) -> SSOConfig:
    """Update an existing SSOConfig.

    Raises:
        HTTPException 404 if not found.
        HTTPException 400 if a given ``org_domain`` is empty once normalised.
        HTTPException 400 if the update sets ``sso_url`` or ``provider_type``
        and the result is a provider that builds its endpoints from an
        ``sso_url`` that is not https, or a SAML configuration whose
        ``sso_url`` is not https (:func:`refuse_non_https_sso_url`).
        HTTPException 409 if a given ``org_domain`` normalises to another
        configuration's.

    An update that does not name ``org_domain`` leaves the stored value as it
    is, normalised or not.
    """
    sso_config = get_sso_config_by_id(db, config_id)
    if not sso_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO config '{config_id}' not found",
        )

    if "sso_url" in data or "provider_type" in data:
        refuse_non_https_sso_url(
            data.get("provider_type", sso_config.provider_type),
            data.get("sso_url", sso_config.sso_url),
        )

    # Checked even when the normalised value equals this row's own: two
    # legacy rows may differ only in case, and writing one's normalised form
    # must not collide with the other.
    if "org_domain" in data:
        new_domain = normalise_domain(data["org_domain"])
        if not new_domain:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ORG_DOMAIN_REQUIRED_DETAIL,
            )
        _refuse_taken_domain(db, new_domain, exclude_id=config_id)
        data = {**data, "org_domain": new_domain}

    for key, value in data.items():
        setattr(sso_config, key, value)

    db.commit()
    db.refresh(sso_config)
    return sso_config


def delete_sso_config(db: Session, config_id: uuid.UUID) -> bool:
    """Delete an SSOConfig.

    Returns:
        True if deleted, False if not found.
    """
    sso_config = get_sso_config_by_id(db, config_id)
    if not sso_config:
        return False
    db.delete(sso_config)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# SAML helpers
# ---------------------------------------------------------------------------


def generate_saml_metadata(config: SSOConfig) -> str:
    """Generate Service-Provider (SP) metadata XML for the given SSOConfig.

    When the *python3-saml* library is available it delegates to
    ``OneLogin_Saml2_Settings``; in development and test it falls back to
    minimal XML so the endpoint works without the optional dependency.

    Raises:
        HTTPException 501 when the library is missing outside development/test.
        Metadata is the document that tells an IdP where to POST assertions,
        and a deployment that cannot validate one has no business advertising
        an ACS endpoint -- ``/saml/{id}/acs`` answers 501 there too.
    """
    _require_saml_support()
    sp_entity_id = getattr(
        settings, "SAML_SP_ENTITY_ID", "https://experimently.example.com"
    )
    sp_acs_url = getattr(
        settings,
        "SAML_SP_ACS_URL",
        f"https://experimently.example.com/api/v1/auth/sso/saml/{config.id}/acs",
    )

    if _SAML_AVAILABLE and OneLogin_Saml2_Settings is not None:
        saml_settings: Dict[str, Any] = {
            "sp": {
                "entityId": sp_entity_id,
                "assertionConsumerService": {
                    "url": sp_acs_url,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
                "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
            },
            "idp": {
                "entityId": config.entity_id or "",
                "singleSignOnService": {
                    "url": config.sso_url or "",
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
                },
                "x509cert": config.x509_certificate or "",
            },
            "strict": False,
            "debug": False,
        }
        try:
            settings_obj = OneLogin_Saml2_Settings(saml_settings)
            return settings_obj.get_sp_metadata()
        except Exception as exc:
            logger.warning("python3-saml metadata generation failed: %s", exc)

    # Fallback: minimal SP metadata XML
    return _build_minimal_sp_metadata(config, sp_entity_id, sp_acs_url)


def _build_minimal_sp_metadata(
    config: SSOConfig, sp_entity_id: str, sp_acs_url: str
) -> str:
    """Build a minimal SAML 2.0 SP metadata XML string without external libraries."""
    return (
        '<?xml version="1.0"?>\n'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"\n'
        f'  entityID="{sp_entity_id}">\n'
        "  <md:SPSSODescriptor\n"
        '    AuthnRequestsSigned="false"\n'
        '    WantAssertionsSigned="true"\n'
        '    protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        "    <md:AssertionConsumerService\n"
        '      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"\n'
        f'      Location="{sp_acs_url}"\n'
        '      index="1"/>\n'
        "  </md:SPSSODescriptor>\n"
        "</md:EntityDescriptor>"
    )


def parse_saml_response(config: SSOConfig, saml_response_b64: str) -> Dict[str, Any]:
    """Parse and validate a base-64-encoded SAML response.

    Returns a ``user_info`` dict with at minimum:
      - ``email``
      - ``name_id``
      - ``attributes`` (raw IdP attributes dict)
      - ``groups`` (list of group/role strings if present)

    Raises:
        HTTPException 400 for invalid/expired assertions.
        HTTPException 401 for signature validation failures.
        HTTPException 501 when *python3-saml* is not installed and the
        environment does not permit the stub parser (see
        :func:`_require_saml_support`).
    """
    if not saml_response_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response is required",
        )

    if _SAML_AVAILABLE and OneLogin_Saml2_Auth is not None:
        return _parse_saml_response_with_library(config, saml_response_b64)

    # Stub implementation used when python3-saml is not installed.  It
    # validates nothing, so it may only run where _require_saml_support()
    # permits it; everywhere else this raises 501 instead.
    _require_saml_support()
    return _parse_saml_response_stub(config, saml_response_b64)


def _parse_saml_response_with_library(
    config: SSOConfig, saml_response_b64: str
) -> Dict[str, Any]:
    """Parse SAML response using the python3-saml library."""
    sp_entity_id = getattr(
        settings, "SAML_SP_ENTITY_ID", "https://experimently.example.com"
    )
    sp_acs_url = getattr(
        settings,
        "SAML_SP_ACS_URL",
        f"https://experimently.example.com/api/v1/auth/sso/saml/{config.id}/acs",
    )

    saml_settings_dict: Dict[str, Any] = {
        "sp": {
            "entityId": sp_entity_id,
            "assertionConsumerService": {
                "url": sp_acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
        },
        "idp": {
            "entityId": config.entity_id or "",
            "singleSignOnService": {
                "url": config.sso_url or "",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": config.x509_certificate or "",
        },
        "strict": True,
        "debug": False,
    }

    # Build a minimal request dict that OneLogin_Saml2_Auth expects
    request_data = {
        "https": "on",
        "http_host": urlparse(sp_acs_url).netloc,
        "server_port": "443",
        "script_name": urlparse(sp_acs_url).path,
        "get_data": {},
        "post_data": {"SAMLResponse": saml_response_b64},
    }

    try:
        auth = OneLogin_Saml2_Auth(request_data, saml_settings_dict)
        auth.process_response()
        errors = auth.get_errors()
        if errors:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"SAML validation failed: {', '.join(errors)}",
            )
        if not auth.is_authenticated():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SAML authentication failed",
            )
        attributes = auth.get_attributes()
        name_id = auth.get_nameid()
        email = (
            name_id
            if "@" in (name_id or "")
            else (
                attributes.get("email", [None])[0]
                or attributes.get(
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                    [None],
                )[0]
                or name_id
            )
        )
        groups = attributes.get("groups", []) or attributes.get(
            "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups", []
        )
        first_name = (attributes.get("firstName", [None]) or [None])[0]
        last_name = (attributes.get("lastName", [None]) or [None])[0]

        return {
            "email": email,
            "name_id": name_id,
            "attributes": attributes,
            "groups": groups,
            "first_name": first_name,
            "last_name": last_name,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _saml_unreadable(SAML_PARSE_FAILED_DETAIL, exc) from None


def _parse_saml_response_stub(
    config: SSOConfig, saml_response_b64: str
) -> Dict[str, Any]:
    """Minimal stub parser used in test environments without python3-saml.

    Attempts to decode the base-64 payload as XML and extract a NameID/email.
    If the payload is not valid XML, raises 400.

    The stub treats a NameID starting with ``"invalid"`` or ``"expired"``
    as an error condition (used in unit tests).

    **This function trusts its input completely**: no signature, issuer,
    audience or expiry is checked, so whoever POSTs the assertion chooses the
    email it provisions.  It is reachable only through
    :func:`_require_saml_support`, which permits it in development and test and
    answers 501 everywhere else.  Never call it directly.
    """
    import base64

    try:
        xml_bytes = base64.b64decode(saml_response_b64)
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise _saml_unreadable(SAML_DECODE_FAILED_DETAIL, exc) from None

    # Find NameID
    ns = {
        "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
        "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    }
    name_id_el = root.find(".//saml:NameID", ns)
    name_id = name_id_el.text if name_id_el is not None else None

    if name_id and name_id.startswith("invalid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SAML assertion signature",
        )
    if name_id and name_id.startswith("expired"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML assertion has expired",
        )

    email = name_id
    return {
        "email": email,
        "name_id": name_id,
        "attributes": {},
        "groups": [],
        "first_name": None,
        "last_name": None,
    }


# ---------------------------------------------------------------------------
# OIDC helpers
# ---------------------------------------------------------------------------


def _provider_endpoint(config: SSOConfig, meta: Dict[str, str], key: str) -> str:
    """One of the provider's endpoints, with ``{sso_url}`` filled in; https only.

    The ID token is checked without its signature, relying on TLS to the
    token endpoint instead (OIDC Core s.3.1.3.7 step 6), and the client secret
    and access token travel to these endpoints -- so a plain-http endpoint is
    refused everywhere except the test environment, whose fake provider is
    local. The built-in providers' endpoints are https constants; this is the
    check on an administrator-supplied ``sso_url``.
    """
    url = meta[key]
    if "{sso_url}" in url and config.sso_url:
        url = url.replace("{sso_url}", config.sso_url.rstrip("/"))
    if urlparse(url).scheme != "https" and not core_settings.is_test:
        logger.error("Refusing a non-https OIDC %s for SSO config %s", key, config.id)
        raise SSORefusal(
            status.HTTP_400_BAD_REQUEST,
            SSO_URL_HTTPS_DETAIL,
            SSO_FAILED,
        )
    return url


def uses_id_token(provider_key: str) -> bool:
    """Whether the provider is OpenID Connect (an ID token) or plain OAuth 2 (GitHub)."""
    meta = _OIDC_PROVIDERS.get(provider_key) or {}
    return "openid" in meta.get("scope", "").split()


_MICROSOFT_TENANT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def expected_issuers(
    config: SSOConfig, provider_key: str, claims: Dict[str, Any]
) -> set:
    """The ``iss`` values an ID token from this configuration may carry.

    Per provider, because each names itself differently:

    * google -- both ``https://accounts.google.com`` and ``accounts.google.com``.
    * microsoft (the ``common`` endpoint), azure_ad -- one issuer per tenant,
      ``https://login.microsoftonline.com/{tid}/v2.0``, with ``tid`` the
      token's own tenant claim.
    * okta -- the authorization server: ``sso_url`` for a custom one
      (``https://org.okta.com/oauth2/default``), the org URL for the org one
      (``sso_url`` ``https://org.okta.com/oauth2`` -> ``https://org.okta.com``).
    * onelogin -- ``https://{subdomain}.onelogin.com/oidc/2``, from the token
      endpoint's host.
    """
    if provider_key == "google":
        return {"https://accounts.google.com", "accounts.google.com"}
    if provider_key in ("microsoft", "azure_ad"):
        tid = str(claims.get("tid") or "")
        if _MICROSOFT_TENANT.fullmatch(tid):
            return {f"https://login.microsoftonline.com/{tid}/v2.0"}
        return set()
    if provider_key == "okta":
        base = (config.sso_url or "").rstrip("/")
        if not base:
            return set()
        return {base[: -len("/oauth2")]} if base.endswith("/oauth2") else {base}
    if provider_key == "onelogin":
        host = urlparse(_OIDC_PROVIDERS["onelogin"]["token_endpoint"]).hostname
        return {f"https://{host}/oidc/2"} if host else set()
    return set()


#: Clock skew allowed on the ID token's ``exp``.
_ID_TOKEN_LEEWAY_SECONDS = 60


def verify_id_token(
    config: SSOConfig,
    provider_key: str,
    id_token: Optional[str],
    nonce: str,
) -> Dict[str, Any]:
    """Check the ID token from this login's own token response (OIDC Core s.3.1.3.7).

    Its signature is not checked: it came straight from the token endpoint
    over https (step 6 allows TLS server validation instead). The claims are:
    ``iss`` (step 2, per provider -- :func:`expected_issuers`), ``aud`` holds
    this client and ``azp`` is this client when there are several (steps 3-5),
    ``exp`` (step 9), and ``nonce`` equals the one this browser's cookie
    carries (step 11). The nonce is what binds the token to this login where a
    provider ignores PKCE.

    Refuses with a fixed 400 naming only the claim that failed.
    """

    def refuse(reason: str) -> HTTPException:
        # Logs a claim NAME ("nonce", "aud", ...), never the token or a value.
        logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            "OIDC ID token refused for SSO config %s: %s", config.id, reason
        )
        return SSORefusal(
            status.HTTP_400_BAD_REQUEST,
            f"OIDC ID token was not accepted ({reason})",
            SSO_FAILED,
        )

    if not id_token or not isinstance(id_token, str):
        raise refuse("missing")
    try:
        # Deliberately unverified: this token came straight from the token
        # endpoint over https (OIDC Core s.3.1.3.7 step 6); iss, aud, exp and
        # nonce are checked below. Never call this on a token from the browser.
        # fmt: off
        claims = jwt.decode(id_token, options={"verify_signature": False})  # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        # fmt: on
    except jwt.PyJWTError:
        raise refuse("malformed") from None

    issuers = expected_issuers(config, provider_key, claims)
    if not issuers or claims.get("iss") not in issuers:
        raise refuse("iss")

    client_id = config.entity_id or ""
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if not client_id or client_id not in audiences:
        raise refuse("aud")
    if len(audiences) > 1 and claims.get("azp") != client_id:
        raise refuse("azp")

    exp = claims.get("exp")
    if (
        not isinstance(exp, (int, float))
        or exp + _ID_TOKEN_LEEWAY_SECONDS < time.time()
    ):
        raise refuse("exp")

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(
        token_nonce.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise refuse("nonce")
    return claims


_OAUTH_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


def _idp_error_code(exc: BaseException) -> Optional[str]:
    """The OAuth ``error`` code a provider sent, if any -- never its free text.

    A failed exchange or userinfo call carries text the caller must not see: the
    provider's ``error_description``, the token URL, connection errors naming
    internal hosts. Only the standard error code (``invalid_grant``, ...) is
    passed on, and only if it looks like one.
    """
    code = getattr(exc, "error", None)
    if code is None:
        response = getattr(exc, "response", None)
        try:
            code = response.json().get("error") if response is not None else None
        except Exception:
            code = None
    if isinstance(code, str) and _OAUTH_ERROR_CODE.fullmatch(code):
        return code
    return None


def _refusal(what: str, exc: BaseException) -> HTTPException:
    code = _idp_error_code(exc)
    logger.error("%s: %s%s", what, type(exc).__name__, f" ({code})" if code else "")
    return SSORefusal(
        status.HTTP_400_BAD_REQUEST,
        f"{what}" + (f" ({code})" if code else ""),
        SSO_FAILED,
    )


async def exchange_oidc_code(
    config: SSOConfig,
    code: str,
    redirect_uri: str,
    *,
    code_verifier: str,
    http_client: Any = None,
) -> Dict[str, Any]:
    """Exchange an OAuth2 authorization code for tokens.

    ``code_verifier`` is the PKCE verifier from the login's state cookie; it
    is sent on every branch, so a code intercepted on its way back to the
    callback cannot be redeemed without it.

    ``http_client`` is an optional injected HTTP client (e.g. an
    ``httpx.AsyncClient`` mock) used in tests.  When ``None`` and authlib is
    available a fresh ``AsyncOAuth2Client`` is used; otherwise falls back to
    a synchronous ``requests`` call.

    Returns the token response dict (access_token, id_token, …).

    Raises:
        HTTPException 400 on error.
    """
    provider_key = config.provider_type.value
    meta = _OIDC_PROVIDERS.get(provider_key)
    if not meta:
        raise SSORefusal(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown OIDC provider '{provider_key}'",
            SSO_FAILED,
        )

    token_url = _provider_endpoint(config, meta, "token_endpoint")

    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.entity_id or "",
        "client_secret": config.client_secret or "",
        "code_verifier": code_verifier,
    }

    try:
        if http_client is not None:
            # Injected mock client (used in tests)
            response = await http_client.post(token_url, data=token_params)
            if hasattr(response, "json"):
                return response.json()
            return response

        if _AUTHLIB_AVAILABLE and AsyncOAuth2Client is not None:
            async with AsyncOAuth2Client(
                client_id=config.entity_id or "",
                client_secret=config.client_secret or "",
            ) as client:
                token = await client.fetch_token(
                    token_url,
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=code_verifier,
                )
                return dict(token)

        # Fallback: synchronous requests (always available in stdlib-adjacent)
        import requests  # type: ignore

        # No redirects: a redirected POST would re-send the client secret to
        # wherever the redirect points.
        resp = requests.post(
            token_url, data=token_params, timeout=10, allow_redirects=False
        )
        if resp.is_redirect:
            raise requests.HTTPError("token endpoint redirected", response=resp)
        resp.raise_for_status()
        return resp.json()

    except HTTPException:
        raise
    except Exception as exc:
        raise _refusal("OIDC token exchange failed", exc) from None


async def get_oidc_user_info(
    config: SSOConfig,
    access_token: str,
    http_client: Any = None,
) -> Dict[str, Any]:
    """Fetch user info from the OIDC provider using the given access token.

    Returns a normalized ``user_info`` dict with at minimum:
      - ``email``
      - ``name`` (optional)
      - ``groups`` (list, may be empty)
      - ``sub`` (provider-specific user identifier)

    Raises:
        HTTPException 400 on error.
    """
    provider_key = config.provider_type.value
    meta = _OIDC_PROVIDERS.get(provider_key)
    if not meta:
        raise SSORefusal(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown OIDC provider '{provider_key}'",
            SSO_FAILED,
        )

    userinfo_url = _provider_endpoint(config, meta, "userinfo_endpoint")

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        if http_client is not None:
            response = await http_client.get(userinfo_url, headers=headers)
            if hasattr(response, "json"):
                raw = response.json()
            else:
                raw = response
        elif _AUTHLIB_AVAILABLE and AsyncOAuth2Client is not None:
            async with AsyncOAuth2Client(
                token={"access_token": access_token}
            ) as client:
                resp = await client.get(userinfo_url)
                resp.raise_for_status()  # an error body is not user info
                raw = resp.json()
        else:
            import requests  # type: ignore

            resp = requests.get(
                userinfo_url, headers=headers, timeout=10, allow_redirects=False
            )
            if resp.is_redirect:
                raise requests.HTTPError("userinfo endpoint redirected", response=resp)
            resp.raise_for_status()
            raw = resp.json()

        return _normalize_userinfo(provider_key, raw)

    except HTTPException:
        raise
    except Exception as exc:
        raise _refusal("OIDC userinfo fetch failed", exc) from None


def _normalize_userinfo(provider_key: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize provider-specific user-info into a common dict."""
    # Extract email from various provider-specific fields
    email = raw.get("email") or ""
    if not email and isinstance(raw.get("emails"), list):
        emails_list = raw.get("emails", [])
        if emails_list and isinstance(emails_list[0], dict):
            email = emails_list[0].get("value", "")
        elif emails_list and isinstance(emails_list[0], str):
            email = emails_list[0]

    name = raw.get("name") or raw.get("login") or raw.get("displayName") or ""
    sub = str(raw.get("sub") or raw.get("id") or raw.get("login") or "")
    groups = raw.get("groups", []) or []

    return {
        "email": email,
        "name": name,
        "sub": sub,
        "groups": groups,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# User provisioning
# ---------------------------------------------------------------------------


#: Providers whose sign-in is refused until they are supported properly.
UNSUPPORTED_OIDC_PROVIDERS = frozenset({"microsoft", "azure_ad", "onelogin"})

UNSUPPORTED_PROVIDER_DETAIL = "This single sign-on provider is not supported yet"
EMAIL_INVALID_DETAIL = "SSO assertion did not contain a valid email address"
EMAIL_DOMAIN_DETAIL = (
    "This account's email is not in the domain this single sign-on is configured for"
)
EMAIL_UNVERIFIED_DETAIL = (
    "The identity provider has not verified this account's email address"
)
IDENTITY_MISMATCH_DETAIL = (
    "The identity provider's user info does not match its ID token"
)
IDENTITY_TAKEN_DETAIL = (
    "This identity is already linked to another account; ask an administrator"
)
EMAIL_AMBIGUOUS_DETAIL = (
    "More than one account matches this email address; ask an administrator"
)

#: The most each value a sign-in stores can hold, read from the columns (#662),
#: so a value is refused or shortened here instead of failing in the database.
EMAIL_MAX: int = User.__table__.c.email.type.length
NAME_MAX: int = min(
    User.__table__.c.first_name.type.length, User.__table__.c.last_name.type.length
)
EXTERNAL_ID_MAX: int = User.__table__.c.external_id.type.length

EMAIL_TOO_LONG_DETAIL = (
    f"This account's email address is longer than {EMAIL_MAX} characters, "
    "the most an account can hold. Ask an administrator."
)
IDENTITY_UNUSABLE_DETAIL = (
    "The identity provider's identifier for this account is longer than "
    f"{EXTERNAL_ID_MAX} characters or not valid text; ask an administrator"
)


def require_supported_provider(provider: str) -> None:
    """Refuse, with a fixed 400, a provider on the unsupported list."""
    if provider in UNSUPPORTED_OIDC_PROVIDERS:
        raise SSORefusal(
            status.HTTP_400_BAD_REQUEST, UNSUPPORTED_PROVIDER_DETAIL, SSO_FAILED
        )


def normalise_domain(value: Optional[str]) -> str:
    """``org_domain`` as compared: stripped, lower-cased, no leading ``@``."""
    return (value or "").strip().lstrip("@").lower()


def _sso_email(user_info: Dict[str, Any], config: SSOConfig) -> str:
    """The email a sign-in may use: well-formed, ASCII, in the config's domain.

    ASCII is checked before lower-casing: ``str.lower`` folds some non-ASCII
    characters into ASCII ones (U+212A KELVIN SIGN becomes ``k``), which would
    let an address that is not in the domain compare equal to one that is.
    """
    raw = user_info.get("email")
    email = raw.strip() if isinstance(raw, str) else ""
    if not email or not email.isascii():
        raise SSORefusal(status.HTTP_400_BAD_REQUEST, EMAIL_INVALID_DETAIL, SSO_EMAIL)
    email = email.lower()
    local, _, domain = email.rpartition("@")
    if not local or not domain:
        raise SSORefusal(status.HTTP_400_BAD_REQUEST, EMAIL_INVALID_DETAIL, SSO_EMAIL)
    expected = normalise_domain(config.org_domain)
    if not expected or domain != expected:
        raise SSORefusal(status.HTTP_403_FORBIDDEN, EMAIL_DOMAIN_DETAIL, SSO_DOMAIN)
    # psycopg2 refuses a NUL, and an address is never shortened: a shortened
    # one could be another person's (#662).
    if "\x00" in email:
        raise SSORefusal(status.HTTP_400_BAD_REQUEST, EMAIL_INVALID_DETAIL, SSO_EMAIL)
    if len(email) > EMAIL_MAX:
        raise SSORefusal(status.HTTP_400_BAD_REQUEST, EMAIL_TOO_LONG_DETAIL, SSO_EMAIL)
    return email


#: NUL and the lone surrogates U+D800-U+DFFF (``json.loads`` makes one from
#: ``"\\ud800"``): the characters the database cannot store. One character
#: class, so a search cannot backtrack.
_UNSTORABLE_CHARS = re.compile("[\x00\ud800-\udfff]")


def _display_name(value: Any) -> Optional[str]:
    """A name a provider sent, as a new account stores it: text only, without
    NUL or surrogates, stripped and cut to ``NAME_MAX`` characters; ``None``
    when nothing is left (#662)."""
    if not isinstance(value, str):
        return None
    return _UNSTORABLE_CHARS.sub("", value).strip()[:NAME_MAX].strip() or None


def _usable_external_id(value: Any) -> bool:
    """Text the ``external_id`` column can store and compare."""
    return (
        isinstance(value, str)
        and len(value) <= EXTERNAL_ID_MAX
        and not contains_unstorable_text(value)
    )


def _is_verified_claim(value: Any) -> bool:
    """``email_verified`` as providers send it: the JSON boolean, or the string.

    Google's own example ID token carries ``"true"``. Exactly ``True`` or
    ``"true"``; not ``1`` (which ``== True`` in Python), ``"True"`` or anything else.
    """
    return value is True or (isinstance(value, str) and value == "true")


async def verified_email(
    config: SSOConfig,
    provider_key: str,
    id_claims: Dict[str, Any],
    user_info: Dict[str, Any],
    access_token: str,
    http_client: Any = None,
) -> Dict[str, Any]:
    """The user info a sign-in may provision with: an email the provider verified.

    Returns ``user_info`` with ``email`` (and, for OpenID Connect providers,
    ``sub``) replaced by the values the provider vouches for:

    * okta -- the ID token's ``email``, with ``email_verified`` true.
    * google -- the ID token's ``email``, with ``email_verified`` true and
      ``hd`` (the Workspace domain) equal to the email's domain: Google is
      authoritative for an address only for its own Workspace domains.
    * github -- no ID token; ``GET /user/emails``: a verified address in the
      configuration's domain -- the primary one if it is one of them, otherwise
      the only one, otherwise refused.
    * anything else -- refused (microsoft, azure_ad and onelogin are refused
      before this by :func:`require_supported_provider`).

    For OpenID Connect providers the user info's ``sub`` must equal the ID
    token's (OIDC Core s.5.3.2). ``email_verified`` must be the boolean
    ``true`` or the exact string ``"true"`` (:func:`_is_verified_claim`).
    """
    checked = dict(user_info)
    if provider_key in ("okta", "google"):
        if str(user_info.get("sub") or "") != str(id_claims.get("sub") or "") or not (
            id_claims.get("sub")
        ):
            raise SSORefusal(
                status.HTTP_400_BAD_REQUEST, IDENTITY_MISMATCH_DETAIL, SSO_ACCOUNT
            )
        if not _is_verified_claim(id_claims.get("email_verified")):
            raise SSORefusal(
                status.HTTP_403_FORBIDDEN, EMAIL_UNVERIFIED_DETAIL, SSO_UNVERIFIED
            )
        email = id_claims.get("email")
        if provider_key == "google":
            hd = id_claims.get("hd")
            domain = email.rpartition("@")[2].lower() if isinstance(email, str) else ""
            if not isinstance(hd, str) or hd.lower() != domain:
                raise SSORefusal(
                    status.HTTP_403_FORBIDDEN, EMAIL_DOMAIN_DETAIL, SSO_DOMAIN
                )
        checked["email"] = email
        checked["sub"] = str(id_claims["sub"])
        return checked
    if provider_key == "github":
        checked["email"] = await _github_verified_email(
            config, access_token, http_client
        )
        return checked
    raise SSORefusal(
        status.HTTP_400_BAD_REQUEST, UNSUPPORTED_PROVIDER_DETAIL, SSO_FAILED
    )


_GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


async def _github_verified_email(
    config: SSOConfig, access_token: str, http_client: Any = None
) -> str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        if http_client is not None:
            response = await http_client.get(_GITHUB_EMAILS_URL, headers=headers)
            entries = response.json() if hasattr(response, "json") else response
        else:
            import requests  # type: ignore

            # A blocking client: in a worker thread, so the wait for GitHub
            # (up to the timeout) holds up only this request.
            resp = await run_in_threadpool(
                requests.get,
                _GITHUB_EMAILS_URL,
                headers=headers,
                timeout=10,
                allow_redirects=False,
            )
            if resp.is_redirect:
                raise requests.HTTPError("emails endpoint redirected", response=resp)
            resp.raise_for_status()
            entries = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise _refusal("OIDC userinfo fetch failed", exc) from None

    domain = normalise_domain(config.org_domain)
    candidates = [
        entry
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
        and entry.get("verified") is True
        and isinstance(entry.get("email"), str)
        and entry["email"].strip().rpartition("@")[2].lower() == domain
    ]
    primary = [entry for entry in candidates if entry.get("primary") is True]
    if primary:
        return primary[0]["email"]
    if len(candidates) == 1:
        return candidates[0]["email"]
    raise SSORefusal(status.HTTP_403_FORBIDDEN, EMAIL_UNVERIFIED_DETAIL, SSO_UNVERIFIED)


def provision_user(
    db: Session,
    user_info: Dict[str, Any],
    config: SSOConfig,
) -> User:
    """Find or create (JIT provision) a User from SSO user_info.

    - The email must be in the configuration's ``org_domain``, exactly (no
      subdomains): one configuration per domain.
    - Looks the user up by email, case-insensitively; more than one match is
      refused rather than guessed.
    - An existing user's role changes only when one of their groups matches
      the configuration's ``role_mapping``. A sign-in with no matching group
      leaves the role as it is; to demote someone through SSO, map one of
      their groups to ``viewer``. ``is_superuser`` is never changed. A change
      writes one ``role_assign`` audit entry by ``system:sso-sync``.
    - A new user gets the mapped role, or ``viewer``, and one ``user_create``
      audit entry by the new account.

    Returns the existing or newly-created User object.
    """
    email = _sso_email(user_info, config)
    groups = user_info.get("groups", []) or []
    mapped = mapped_role(config, groups)

    matches = db.query(User).filter(func.lower(User.email) == email).limit(2).all()
    if len(matches) > 1:
        raise SSORefusal(status.HTTP_409_CONFLICT, EMAIL_AMBIGUOUS_DETAIL, SSO_ACCOUNT)
    if matches:
        existing_user = matches[0]
        new_role = UserRole(mapped) if mapped is not None else None
        if new_role is not None and existing_user.role != new_role:
            # Recorded as ``role_assign`` by ``system:sso-sync``, with the
            # values read before the change. The entry rides in a savepoint:
            # if it cannot be written the role change still applies, one
            # ERROR is logged, and it is not retried (the next sign-in finds
            # the role already equal and writes nothing).
            before = role_value(existing_user)
            existing_user.role = new_role
            AuditService.record_in_savepoint(
                db,
                actor=SYSTEM_SSO_SYNC,
                action=ActionType.ROLE_ASSIGN,
                entity_type=EntityType.USER,
                entity_id=existing_user.id,
                entity_name=existing_user.username or str(existing_user.id),
                before=before,
                after=role_value(existing_user),
                reason="SSO groups changed",
            )
            db.commit()
            db.refresh(existing_user)
        return existing_user

    # JIT provisioning. The account is found by email only, never by the
    # provider's subject: a row created earlier under the same subject keeps
    # its own email, and a sign-in under another email is refused rather than
    # handed that row or crashing on the unique column (#122).
    external_id = user_info.get("sub") or user_info.get("name_id")
    # Checked before the lookup, which psycopg2 fails on a NUL or a surrogate.
    if external_id and not _usable_external_id(external_id):
        raise SSORefusal(
            status.HTTP_400_BAD_REQUEST, IDENTITY_UNUSABLE_DETAIL, SSO_ACCOUNT
        )
    if external_id and db.query(User).filter(User.external_id == external_id).first():
        raise SSORefusal(status.HTTP_409_CONFLICT, IDENTITY_TAKEN_DETAIL, SSO_ACCOUNT)
    role = UserRole(mapped) if mapped else UserRole.VIEWER

    # Derive a username from email local part + random suffix to avoid collisions
    local_part = email.split("@")[0].replace(".", "_").replace("+", "_")[:40]
    username = f"{local_part}_{uuid.uuid4().hex[:8]}"

    raw = user_info.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    name = user_info.get("name")
    name_parts = name.split() if isinstance(name, str) else []
    first_name = (
        _display_name(user_info.get("first_name"))
        or _display_name(raw.get("given_name"))
        or (_display_name(name_parts[0]) if name_parts else None)
    )
    last_name = (
        _display_name(user_info.get("last_name"))
        or _display_name(raw.get("family_name"))
        or (_display_name(" ".join(name_parts[1:])) if len(name_parts) > 1 else None)
    )

    new_user = User(
        username=username,
        email=email,
        hashed_password=None,  # No password for SSO users
        is_active=True,
        is_superuser=False,
        role=role,
        first_name=first_name,
        last_name=last_name,
        external_id=external_id,
    )
    db.add(new_user)
    # The new account's ``user_create`` entry rides in a savepoint: the
    # account is created even if the entry cannot be written.
    db.flush()
    AuditService.record_in_savepoint(
        db,
        actor=new_user,
        action=ActionType.USER_CREATE,
        entity_type=EntityType.USER,
        entity_id=new_user.id,
        entity_name=new_user.username or str(new_user.id),
        after=audit_identity(audit_snapshot(EntityType.USER, new_user)),
        reason="first SSO sign-in",
    )
    db.commit()
    db.refresh(new_user)
    return new_user


def mapped_role(config: SSOConfig, groups: List[str]) -> Optional[str]:
    """The role the first group with a mapping maps to, or ``None`` if none does."""
    mapping: Dict[str, str] = config.role_mapping or {}
    for group in groups or []:
        role = mapping.get(group)
        if role:
            return role.lower()
    return None


def map_role(config: SSOConfig, groups: List[str]) -> str:
    """Map a list of IdP group names to a platform role string.

    The ``role_mapping`` JSONB on ``SSOConfig`` maps group names to role strings,
    e.g. ``{"admins": "admin", "devs": "developer"}``.

    Priority: first matching group wins.  Falls back to ``"viewer"`` when no
    group matches (or mapping is empty / absent). This is the role for a NEW
    account; an existing account keeps its role unless a group matches
    (:func:`provision_user`).
    """
    return mapped_role(config, groups) or "viewer"


# ---------------------------------------------------------------------------
# The OIDC login's state (#66)
# ---------------------------------------------------------------------------
#
# The login route used to mint a random state and remember it in a
# process-local dictionary. That state was bound to no browser -- any
# callback carrying any unredeemed state was accepted, so an attacker could
# complete a login with their own code in a victim's browser -- it did not
# survive more than one API task, and the route that filled the dictionary is
# unauthenticated.
#
# Now the server holds nothing. The login route sets a signed, short-lived
# cookie in the browser that started the login; the callback accepts only a
# `state` that matches the one inside that cookie. The cookie also carries the
# PKCE verifier, so a code intercepted on its way back is useless without it,
# and names the SSO configuration the login was started for.

#: `__Host-`: the browser accepts it only with `Secure`, `Path=/` and no
#: `Domain`, so no other host -- a sibling subdomain included -- can set or
#: overwrite it.
OIDC_STATE_COOKIE = "__Host-experimently_oidc"

#: How long a started login stays redeemable.
OIDC_STATE_TTL_SECONDS = 600

#: The cookie outlives the login it carries by five minutes, so a callback
#: that arrives late still brings it -- and is told `sso_expired`, from the
#: cookie's own `exp`, rather than finding no cookie at all.
OIDC_STATE_COOKIE_MAX_AGE = OIDC_STATE_TTL_SECONDS + 300

#: Clock skew allowed on the cookie's `exp` and `iat`.
_STATE_LEEWAY_SECONDS = 30

_STATE_REQUIRED_CLAIMS = ["aud", "iat", "exp", "st", "cv", "nn", "cfg", "prv"]

_STATE_AUDIENCE = "experimently:oidc-state"


def _state_key() -> bytes:
    """The cookie's signing key: derived from SECRET_KEY, used for nothing else.

    A token signed with this key can never pass for an access token, and an
    access token can never pass for one of these, even though both are HS256
    under the same secret.
    """
    return hmac.new(
        core_settings.SECRET_KEY.encode("utf-8"),
        b"experimently:oidc-state:v1",
        hashlib.sha256,
    ).digest()


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge for *verifier* (RFC 7636 s.4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class OIDCLoginStart:
    """What the login route needs: the URL's `state` and the cookie to set."""

    state: str
    code_verifier: str
    nonce: str
    cookie_value: str


@dataclass(frozen=True)
class OIDCLoginState:
    """What the callback gets back from a cookie that verified."""

    config_id: uuid.UUID
    provider: str
    code_verifier: str
    nonce: str


def start_oidc_login(
    config: SSOConfig,
    provider: str,
    *,
    return_to: Optional[str] = None,
    handoff: Optional[str] = None,
) -> OIDCLoginStart:
    """Mint the state, the PKCE verifier and the signed cookie that holds both.

    A login started from the dashboard also carries ``rt`` (the dashboard
    origin to return to -- an entry of ``dashboard_origins``, never a value
    rebuilt from the request) and ``hh`` (the hand-off hash the code minted at
    the callback is bound to). ``rt`` is what puts the callback in redirect
    mode.
    """
    state = secrets.token_urlsafe(32)
    # 64 random bytes -> 86 characters, inside RFC 7636's 43..128.
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    now = int(time.time())
    claims = {
        "aud": _STATE_AUDIENCE,
        "iat": now,
        "exp": now + OIDC_STATE_TTL_SECONDS,
        "st": state,
        "cv": verifier,
        "nn": nonce,
        "cfg": str(config.id),
        "prv": provider,
    }
    if return_to is not None:
        claims["rt"] = return_to
    if handoff is not None:
        claims["hh"] = handoff
    cookie = jwt.encode(claims, _state_key(), algorithm="HS256")
    return OIDCLoginStart(
        state=state, code_verifier=verifier, nonce=nonce, cookie_value=cookie
    )


def _state_refusal(detail: str, sso_error: str = SSO_STATE) -> SSORefusal:
    return SSORefusal(status.HTTP_400_BAD_REQUEST, detail, sso_error)


def redeem_oidc_state(
    cookie_value: Optional[str], url_state: Optional[str]
) -> OIDCLoginState:
    """Accept a callback only from the browser that started this login.

    Refuses, with a fixed 400: no cookie; a cookie that does not verify, has
    expired or is not one of ours; and a URL `state` that is absent or is not
    the one in the cookie. Single use is the identity provider's side of it:
    an authorization code is redeemable once, and the callback expires the
    cookie whatever the outcome.
    """
    if not cookie_value:
        raise _state_refusal(
            "OIDC sign-in was not started in this browser, or its cookie was not sent"
        )
    try:
        claims = jwt.decode(
            cookie_value,
            _state_key(),
            algorithms=["HS256"],
            audience=_STATE_AUDIENCE,
            leeway=_STATE_LEEWAY_SECONDS,
            options={"require": _STATE_REQUIRED_CLAIMS},
        )
    except jwt.ExpiredSignatureError:
        raise _state_refusal(
            "OIDC sign-in expired; start it again", SSO_EXPIRED
        ) from None
    except jwt.PyJWTError:
        raise _state_refusal("OIDC sign-in state is not valid") from None

    expected = str(claims["st"]).encode("utf-8")
    if not url_state or not hmac.compare_digest(url_state.encode("utf-8"), expected):
        raise _state_refusal("OIDC state does not match this browser's sign-in")
    try:
        config_id = uuid.UUID(str(claims["cfg"]))
    except ValueError:
        raise _state_refusal("OIDC sign-in state is not valid") from None
    return OIDCLoginState(
        config_id=config_id,
        provider=str(claims["prv"]),
        code_verifier=str(claims["cv"]),
        nonce=str(claims["nn"]),
    )


def peek_oidc_state(cookie_value: Optional[str]) -> Optional[Dict[str, Any]]:
    """The claims of a state cookie that is ours, expired or not; else None.

    Signature, audience and the required claims are checked; ``exp`` is not.
    Used only to learn where a dashboard sign-in returns to (``rt``), so that
    an expired one can still be sent back there as ``sso_expired``. Nothing is
    redeemed on the strength of this: the callback still relies on
    :func:`redeem_oidc_state`, which checks ``exp``.
    """
    if not cookie_value:
        return None
    try:
        return jwt.decode(
            cookie_value,
            _state_key(),
            algorithms=["HS256"],
            audience=_STATE_AUDIENCE,
            leeway=_STATE_LEEWAY_SECONDS,
            options={"require": _STATE_REQUIRED_CLAIMS, "verify_exp": False},
        )
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# The dashboard hand-off (C2b)
# ---------------------------------------------------------------------------
#
# A dashboard sign-in does not put the access token in a URL. The callback
# redirects to `{dashboard}/sso/complete#code=<hand-off code>`: a 60-second
# JWT naming the user and `hh`, the SHA-256 of a 32-byte secret the dashboard
# generated and kept in this tab's sessionStorage. The dashboard then POSTs
# `{code, secret}` to `/exchange` for the token. A code read out of browser
# history is dead after 60 s and useless without the secret, which leaves the
# browser only in that POST body. It is not single-use within its 60 s (the
# server keeps no state); the secret binding is what stops replay by anyone
# who can read history.

HANDOFF_TTL_SECONDS = 60
_HANDOFF_AUDIENCE = "experimently:sso-handoff"
#: `secret` and `hh`: unpadded base64url of 32 bytes.
_B64URL_32_BYTES = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_HANDOFF_CODE_LENGTH = 2048

HANDOFF_REFUSED_DETAIL = "This sign-in could not be completed; start it again"


def _handoff_key() -> bytes:
    """The hand-off code's signing key: derived from SECRET_KEY, used for nothing else."""
    return hmac.new(
        core_settings.SECRET_KEY.encode("utf-8"),
        b"experimently:sso-handoff:v1",
        hashlib.sha256,
    ).digest()


def is_handoff_value(value: Any) -> bool:
    """True for exactly 43 base64url characters: the shape of `secret` and `hh`."""
    return isinstance(value, str) and bool(_B64URL_32_BYTES.fullmatch(value))


def handoff_hash(secret: str) -> str:
    """``hh`` for *secret*: unpadded base64url(SHA-256(the 32 decoded bytes)).

    The bytes, not the base64 text, are hashed; the dashboard computes the
    same value with Web Crypto.
    """
    if not is_handoff_value(secret):
        raise ValueError("secret must be 43 base64url characters")
    raw = base64.urlsafe_b64decode(secret + "=")
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_handoff_code(user_id: uuid.UUID, hh: str) -> str:
    """The code the callback puts in the dashboard's URL fragment."""
    now = int(time.time())
    claims = {
        "aud": _HANDOFF_AUDIENCE,
        "iat": now,
        "exp": now + HANDOFF_TTL_SECONDS,
        "sub": str(user_id),
        "hh": hh,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(claims, _handoff_key(), algorithm="HS256")


def redeem_handoff_code(code: Any, secret: Any) -> uuid.UUID:
    """The user id a hand-off code names, if *secret* is the one it is bound to.

    Refuses with a fixed 400: a code that is not a string of at most 2048
    characters; a secret that is not 43 base64url characters; a code whose
    signature, audience or ``exp`` (no leeway) does not verify; and a secret
    whose hash is not the code's ``hh`` (compared in constant time, as bytes).
    """
    refused = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=HANDOFF_REFUSED_DETAIL
    )
    # ASCII first: a JSON body can carry a lone surrogate ("\ud800"), and
    # PyJWT's utf-8 encode of it raises UnicodeEncodeError, not PyJWTError.
    if (
        not isinstance(code, str)
        or not code
        or len(code) > MAX_HANDOFF_CODE_LENGTH
        or not code.isascii()
    ):
        raise refused
    if not is_handoff_value(secret):
        raise refused
    try:
        claims = jwt.decode(
            code,
            _handoff_key(),
            algorithms=["HS256"],
            audience=_HANDOFF_AUDIENCE,
            leeway=0,
            options={"require": ["aud", "exp", "sub", "hh", "jti"]},
        )
    except jwt.PyJWTError:
        raise refused from None
    expected = claims.get("hh")
    if not isinstance(expected, str) or not hmac.compare_digest(
        handoff_hash(secret).encode("ascii"), expected.encode("utf-8")
    ):
        raise refused
    try:
        return uuid.UUID(str(claims["sub"]))
    except ValueError:
        raise refused from None


def build_oidc_authorization_url(
    config: SSOConfig,
    provider_key: str,
    redirect_uri: str,
    state: str,
    code_verifier: str,
    nonce: Optional[str] = None,
) -> str:
    """Build the authorization URL to redirect the user to the OIDC provider.

    Always with a PKCE S256 challenge (RFC 9700 s.2.1.1). A provider that does
    not support PKCE ignores the parameters.
    """
    meta = _OIDC_PROVIDERS.get(provider_key)
    if not meta:
        raise SSORefusal(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown OIDC provider '{provider_key}'",
            SSO_FAILED,
        )

    auth_url = _provider_endpoint(config, meta, "authorization_endpoint")

    params = {
        "client_id": config.entity_id or "",
        "response_type": "code",
        "scope": meta.get("scope", "openid email profile"),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if nonce and uses_id_token(provider_key):
        params["nonce"] = nonce
    return f"{auth_url}?{urlencode(params)}"

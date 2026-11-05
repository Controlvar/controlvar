"""
SSO/SAML & OIDC authentication endpoints — EP-037.

Endpoints:
  GET  /auth/sso/saml/{config_id}/metadata  — SP metadata XML
  POST /auth/sso/saml/{config_id}/acs        — SAML Assertion Consumer Service
  GET  /auth/sso/login                       — Dashboard sign-in: to the domain's provider
  POST /auth/sso/exchange                    — Dashboard hand-off code for a session
  GET  /auth/sso/oidc/{provider}/login       — Redirect to OIDC provider
  GET  /auth/sso/oidc/{provider}/callback    — OIDC callback / token issue
  GET  /auth/sso/configs                     — List SSO configs (admin)
  POST /auth/sso/configs                     — Create SSO config (admin)
  GET  /auth/sso/configs/{config_id}         — Get SSO config (admin)
  PUT  /auth/sso/configs/{config_id}         — Update SSO config (admin)
  DELETE /auth/sso/configs/{config_id}       — Delete SSO config (admin)

Note: the router is mounted at /api/v1/auth/sso in api.py, so the
endpoint paths below are relative to that prefix.
"""

from __future__ import annotations

import copy
import hmac
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import user_to_me
from backend.app.core.config import normalise_origin, settings
from backend.app.core.security import create_local_access_token
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User, UserRole
from backend.app.schemas.auth import UserMe
from backend.app.services.audit_service import AuditService
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service

logger = logging.getLogger(__name__)

router = APIRouter()

#: The login flow: SP metadata, the SAML ACS and the OIDC login/callback.
#: Reached by a browser that has no session yet, so it carries no user
#: authentication; the registration mounts it behind nothing.  The
#: configuration CRUD stays on `router`, which is mounted behind
#: authentication.
public_router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class SSOConfigCreate(BaseModel):
    """Schema for creating a new SSO configuration."""

    org_name: str = Field(..., description="Human-readable organization name")
    org_domain: str = Field(
        ..., description="Organization email domain (e.g. acme.com)"
    )
    provider_type: SSOProviderType
    entity_id: Optional[str] = Field(
        None, description="SAML Entity ID or OIDC client_id"
    )
    sso_url: Optional[str] = Field(
        None, description="IdP SSO URL (SAML) or OIDC issuer URL"
    )
    x509_certificate: Optional[str] = Field(
        None, description="SAML IdP signing certificate (PEM)"
    )
    client_secret: Optional[str] = Field(None, description="OIDC client secret")
    role_mapping: Optional[Dict[str, str]] = Field(
        default_factory=dict,
        description='Map IdP groups to roles: {"admin-group": "admin"}',
    )
    is_enforced: bool = Field(
        False,
        description=(
            "Stored and returned, but it has no effect: password sign-in is "
            "not blocked when True"
        ),
    )
    is_active: bool = Field(True)


class SSOConfigUpdate(BaseModel):
    """Schema for updating an existing SSO configuration."""

    org_name: Optional[str] = None
    org_domain: Optional[str] = None
    provider_type: Optional[SSOProviderType] = None
    entity_id: Optional[str] = None
    sso_url: Optional[str] = None
    x509_certificate: Optional[str] = None
    client_secret: Optional[str] = None
    role_mapping: Optional[Dict[str, str]] = None
    is_enforced: Optional[bool] = None
    is_active: Optional[bool] = None


class SSOConfigResponse(BaseModel):
    """Schema for returning SSO config data (secrets redacted)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_name: str
    org_domain: str
    provider_type: SSOProviderType
    entity_id: Optional[str]
    sso_url: Optional[str]
    x509_certificate: Optional[str]
    role_mapping: Optional[Dict[str, Any]]
    is_enforced: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SAMLLoginResponse(BaseModel):
    """Response after a successful SAML ACS callback."""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    role: str


class OIDCLoginResponse(BaseModel):
    """Response after a successful OIDC callback."""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    role: str
    provider: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_admin(current_user: User) -> User:
    """Raise 403 if the caller is not ADMIN or superuser."""
    if not current_user.is_superuser and current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can manage SSO configurations",
        )
    return current_user


def _issue_jwt(user: User) -> str:
    """Issue a simple JWT for the provisioned user.

    Uses the platform SECRET_KEY as the signing secret.  In production this
    should go through the same Cognito / token infrastructure as regular login.
    """
    import jwt

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "username": user.username,
        "role": user.role.value if user.role else "viewer",
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow()
        + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    return token


def _record_sso_login(db: Session, user: User) -> None:
    """One ``user_login`` audit entry for an SSO sign-in that has succeeded.

    Written once the response is built, after any account change has
    committed; a failed entry is logged and does not refuse the sign-in. The
    entry records the provider only. A dashboard sign-in is recorded once, at
    ``POST /exchange``, where its session token is issued; the callback that
    hands it the code records nothing.
    """
    AuditService.record_after_commit(
        db,
        actor=user,
        action=ActionType.USER_LOGIN,
        entity_type=EntityType.USER,
        entity_id=user.id,
        entity_name=user.username or str(user.id),
        after={"provider": "sso"},
    )


_T = TypeVar("_T")


def _in_own_session(work: Callable[[Session], _T]) -> _T:
    """Run *work* on a session of its own, closed before this returns.

    Called in a worker thread: the session is opened, used (and committed by
    *work* when it writes) and closed here, so the request holds no
    connection once the step is over -- not while it waits for the identity
    provider, and not while it waits for another worker.
    """
    db = deps.SessionLocal()
    try:
        return work(db)
    finally:
        db.close()


async def _database_step(work: Callable[[Session], _T]) -> _T:
    """What *work* returns, run in a worker thread on its own short session.

    The async routes below never use the database on the event loop: a query
    there that has to wait for a connection holds up every other request the
    process is serving until it gets one. *work* returns plain values, never
    an ORM row, because its session is closed by the time this returns.
    """
    return await run_in_threadpool(_in_own_session, work)


@dataclass(frozen=True)
class _ConfigValues:
    """The fields of one SSO configuration that a sign-in reads, as plain values.

    Read from the row in a worker and used after that worker's session has
    closed, so nothing after the lookup can reach the database through it.
    It carries every ``config.<field>`` that ``sso_service`` reads on the OIDC
    path (``start_oidc_login``, ``build_oidc_authorization_url``,
    ``exchange_oidc_code``, ``verify_id_token``, ``get_oidc_user_info``,
    ``verified_email``, ``provision_user``).
    """

    id: uuid.UUID
    org_domain: str
    provider_type: SSOProviderType
    entity_id: Optional[str]
    sso_url: Optional[str]
    client_secret: Optional[str] = field(repr=False)
    role_mapping: Any
    is_active: bool

    @classmethod
    def of(cls, row: SSOConfig) -> "_ConfigValues":
        return cls(
            id=row.id,
            org_domain=row.org_domain,
            provider_type=row.provider_type,
            entity_id=row.entity_id,
            sso_url=row.sso_url,
            client_secret=row.client_secret,
            role_mapping=copy.deepcopy(row.role_mapping),
            is_active=row.is_active,
        )


def _config_by_id(db: Session, config_id: uuid.UUID) -> Optional[_ConfigValues]:
    """The configuration *config_id* names, active or not; None if there is none."""
    config = sso_service.get_sso_config_by_id(db, config_id)
    return None if config is None else _ConfigValues.of(config)


def _get_redirect_uri(
    request: Request, provider: str, config_id: Optional[str] = None
) -> str:
    """Build the OIDC callback redirect URI.

    From ``PUBLIC_BASE_URL`` when it is set, and only then from the request.

    ``request.base_url`` is the scheme and the ``Host`` header, and both halves
    are client-controlled: ``Host`` on any path that reaches the app, and the
    scheme through ``X-Forwarded-Proto`` for as long as uvicorn is started with
    a permissive ``--forwarded-allow-ips`` (#237). This is the value an
    authorization code comes back to, so a crafted ``Host`` steered the code to
    an attacker's host -- authorization-code interception, #220.

    ``ALLOWED_HOSTS`` now refuses a foreign ``Host`` before this function is
    reached, so this is the second of two locks rather than the only one. It is
    still worth having: it is the difference between "no host we do not accept"
    and "exactly one host, the one we published to the IdP" -- and an OIDC
    ``redirect_uri`` must match the IdP's registration EXACTLY, so deriving it
    from whichever of several accepted hosts a particular request happened to
    use is a bug even with no attacker present.

    It also fixes the ordinary case behind a TLS-terminating proxy, where the
    request's own scheme is ``http`` and every absolute URL built from it is
    wrong in a way no test that talks to the app directly would show.
    """
    base = settings.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/api/v1/auth/sso/oidc/{provider}/callback"


def _active_saml_config(db: Session, config_id: uuid.UUID) -> SSOConfig:
    """The SAML config *config_id* names, or raise.

    ``is_active`` is checked here because it is the only administrative
    off-switch these two routes have: an operator whose IdP is compromised
    flips it false and expects the assertion consumer to stop minting
    platform sessions.  Both routes used to read the row and ignore the
    flag, so only deleting it stopped them -- and the metadata document kept
    advertising the ACS of a provider the operator had just disabled.

    A disabled config answers **404**, the same status (and the same shape of
    answer) the two OIDC routes already give for this exact condition: "no
    *active* config found".  A disabled provider is not there to serve you,
    and a scanner cannot tell "disabled" from "deleted" by status alone; the
    detail line names the difference for the operator reading their own logs,
    which costs nothing -- the row's existence is not a secret (the metadata
    it serves is a public document) and config ids are v4 UUIDs.
    """
    config = sso_service.get_sso_config_by_id(db, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO config '{config_id}' not found",
        )
    if config.provider_type != SSOProviderType.SAML:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This SSO config is not a SAML provider",
        )
    if not config.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO config '{config_id}' is not active",
        )
    return config


# ---------------------------------------------------------------------------
# SAML endpoints
# ---------------------------------------------------------------------------


@public_router.get(
    "/saml/{config_id}/metadata",
    response_class=Response,
    summary="SAML SP metadata",
    tags=["sso"],
)
def saml_metadata(
    config_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
) -> Response:
    """Return the Service-Provider metadata XML for the given SSO config."""
    config = _active_saml_config(db, config_id)
    xml = sso_service.generate_saml_metadata(config)
    return Response(content=xml, media_type="application/xml")


@public_router.post(
    "/saml/{config_id}/acs",
    response_model=SAMLLoginResponse,
    summary="SAML Assertion Consumer Service",
    tags=["sso"],
)
def saml_acs(
    config_id: uuid.UUID,
    SAMLResponse: Optional[str] = Form(None),
    db: Session = Depends(deps.get_db),
) -> SAMLLoginResponse:
    """Receive and process a SAML assertion from the IdP.

    On success, provisions/updates the user and returns a JWT.
    """
    config = _active_saml_config(db, config_id)

    saml_response_b64 = SAMLResponse
    if not saml_response_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAMLResponse is required",
        )

    user_info = sso_service.parse_saml_response(config, saml_response_b64)
    user = sso_service.provision_user(db, user_info, config)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user"
        )
    token = _issue_jwt(user)

    result = SAMLLoginResponse(
        access_token=token,
        user_id=str(user.id),
        email=user.email,
        role=user.role.value if user.role else "viewer",
    )
    _record_sso_login(db, user)
    return result


# ---------------------------------------------------------------------------
# OIDC endpoints
# ---------------------------------------------------------------------------


def _set_state_cookie(response: Response, value: str, max_age: int) -> None:
    """The login's state cookie; `max_age=0` with an empty value expires it.

    `Secure` even on `http://localhost`: the `__Host-` prefix requires it, and
    browsers treat localhost as a secure context. `SameSite=Lax`, because the
    callback is a top-level GET arriving from the identity provider's site --
    `Strict` would not send it.
    """
    response.set_cookie(
        sso_service.OIDC_STATE_COOKIE,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _expiring_state_cookie_header() -> str:
    """The `Set-Cookie` value that expires the state cookie, for an error response."""
    scratch = Response()
    _set_state_cookie(scratch, "", 0)
    return scratch.headers["set-cookie"]


@public_router.get(
    "/oidc/{provider}/login",
    summary="Initiate OIDC login",
    tags=["sso"],
)
async def oidc_login(
    provider: str,
    request: Request,
    org_domain: Optional[str] = Query(None, description="Org domain for config lookup"),
) -> RedirectResponse:
    """Redirect the browser to the provider, holding the login's state in a cookie."""
    sso_service.require_supported_provider(provider)
    config = await _database_step(lambda db: _login_config(db, provider, org_domain))

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active SSO config found for provider '{provider}'",
        )

    started = sso_service.start_oidc_login(config, provider)
    return _redirect_to_provider(request, config, provider, started)


def _login_config(
    db: Session, provider: str, org_domain: Optional[str]
) -> Optional[_ConfigValues]:
    """The first active config for *provider* (and *org_domain*, when given)."""
    # Find config by provider type and optional org_domain
    for c in sso_service.list_sso_configs(db):
        if c.provider_type.value == provider and c.is_active:
            if not org_domain or c.org_domain == org_domain:
                return _ConfigValues.of(c)
    return None


def _redirect_to_provider(
    request: Request,
    config: _ConfigValues,
    provider: str,
    started: sso_service.OIDCLoginStart,
) -> RedirectResponse:
    """The 302 to the provider's authorization endpoint, setting the state cookie."""
    redirect_uri = _get_redirect_uri(request, provider)
    auth_url = sso_service.build_oidc_authorization_url(
        config,
        provider,
        redirect_uri,
        started.state,
        started.code_verifier,
        started.nonce,
    )

    response = RedirectResponse(url=auth_url, status_code=302)
    _set_state_cookie(
        response, started.cookie_value, sso_service.OIDC_STATE_COOKIE_MAX_AGE
    )
    return response


# ---------------------------------------------------------------------------
# Sign-in from the dashboard (C2b): redirect mode and the hand-off
# ---------------------------------------------------------------------------

#: An OAuth `error` code as the dashboard may show it; anything else is dropped.
_IDP_ERROR = re.compile(r"[a-z_]{1,64}")
_PROVIDER_NAMES = frozenset(p.value for p in SSOProviderType)
_DOMAIN = re.compile(r"[a-z0-9.-]{1,253}")

RETURN_TO_REFUSED_DETAIL = "return_to is not a dashboard origin this API accepts"
HANDOFF_PARAM_DETAIL = "handoff must be 43 base64url characters"
DOMAIN_PARAM_DETAIL = "domain must be an email domain such as example.com"
EXCHANGE_BODY_DETAIL = sso_service.HANDOFF_REFUSED_DETAIL


def _request_id() -> Optional[str]:
    """This request's id (the one on its `X-Request-ID`), if it is safe to show."""
    return sso_service.current_request_id()


def _login_error_url(
    origin: str,
    sso_error: str,
    *,
    provider: Optional[str] = None,
    idp_error: Optional[str] = None,
) -> str:
    """`{origin}/login?sso_error=...`: the four parameters and nothing else."""
    params: Dict[str, str] = {"sso_error": sso_error}
    if provider in _PROVIDER_NAMES:
        params["provider"] = str(provider)
    if isinstance(idp_error, str) and _IDP_ERROR.fullmatch(idp_error):
        params["idp_error"] = idp_error
    request_id = _request_id()
    if request_id:
        params["request_id"] = request_id
    return f"{origin}/login?{urlencode(params)}"


def _redirect(url: str) -> RedirectResponse:
    """A 302 that also expires the state cookie.

    Built here and returned, so the `Set-Cookie` is on the response that is
    sent: a cookie set on the injected `response` is dropped when the route
    returns its own Response.
    """
    response = RedirectResponse(url=url, status_code=302)
    _set_state_cookie(response, "", 0)
    response.headers["Cache-Control"] = "no-store"
    return response


def _dashboard_origin_for(return_to: Optional[str]) -> Optional[str]:
    """The `dashboard_origins` entry *return_to* names, verbatim; else None.

    Refused before normalising: a backslash, an `@`, whitespace or a control
    character anywhere. Then anything `normalise_origin` refuses (a path other
    than `/`, a query, a fragment, userinfo, a port that does not parse), and
    anything whose normalised form is not in the list.
    """
    if not isinstance(return_to, str) or not return_to:
        return None
    if any(c in "\\@" or ord(c) <= 0x20 or ord(c) == 0x7F for c in return_to):
        return None
    candidate = normalise_origin(return_to)
    if candidate is None or candidate == "*":
        return None
    for entry in settings.dashboard_origins:
        if entry == candidate:
            return entry
    return None


def _log_refusal(where: str, exc: HTTPException, sso_error: str) -> None:
    logger.warning(
        "SSO sign-in refused at %s: sso_error=%s status=%s detail=%r request_id=%s",
        where,
        sso_error,
        exc.status_code,
        str(exc.detail)[:200],
        _request_id(),
    )


@public_router.get(
    "/login",
    summary="Start single sign-on from the dashboard",
    tags=["sso"],
    status_code=302,
    response_class=RedirectResponse,
    responses={
        302: {
            "description": "To the identity provider, or back to the dashboard's "
            "/login?sso_error=... when the domain cannot sign in with SSO"
        },
        400: {"description": "A parameter is missing or malformed (JSON)"},
    },
    openapi_extra={"x-stability": "beta"},
)
async def sso_login(
    request: Request,
    domain: Optional[str] = Query(None, description="The work email's domain"),
    return_to: Optional[str] = Query(
        None, description="The dashboard origin to come back to"
    ),
    handoff: Optional[str] = Query(
        None, description="base64url(SHA-256(secret)), 43 characters"
    ),
) -> RedirectResponse:
    """Start a dashboard sign-in: find the domain's OIDC config, go to its provider.

    Every parameter is required. A missing or malformed one is a 400 JSON
    answer, never a redirect and never an unbound login.
    """
    origin = _dashboard_origin_for(return_to)
    if origin is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, RETURN_TO_REFUSED_DETAIL)
    if not sso_service.is_handoff_value(handoff):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, HANDOFF_PARAM_DETAIL)
    wanted = domain.lower() if isinstance(domain, str) else ""
    if not _DOMAIN.fullmatch(wanted):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, DOMAIN_PARAM_DETAIL)

    active = await _database_step(lambda db: _active_domain_configs(db, wanted))
    oidc = [c for c in active if c.provider_type != SSOProviderType.SAML]
    if len(oidc) > 1:
        logger.warning(
            "More than one active OIDC SSO config matches domain %r (%s); refusing "
            "the sign-in until an administrator removes the duplicates",
            wanted,
            ", ".join(sorted(str(c.id) for c in oidc)),
        )
        return _redirect(_login_error_url(origin, sso_service.SSO_NOT_CONFIGURED))
    if not oidc:
        code = (
            sso_service.SSO_SAML_ONLY
            if any(c.provider_type == SSOProviderType.SAML for c in active)
            else sso_service.SSO_NOT_CONFIGURED
        )
        return _redirect(_login_error_url(origin, code))

    config = oidc[0]
    provider = config.provider_type.value
    try:
        sso_service.require_supported_provider(provider)
        started = sso_service.start_oidc_login(
            config, provider, return_to=origin, handoff=handoff
        )
        return _redirect_to_provider(request, config, provider, started)
    except HTTPException as exc:
        sso_error = getattr(exc, "sso_error", sso_service.SSO_FAILED)
        _log_refusal("login", exc, sso_error)
        return _redirect(_login_error_url(origin, sso_error, provider=provider))


def _active_domain_configs(db: Session, wanted: str) -> List[_ConfigValues]:
    """Every active config whose ``org_domain``, trimmed and lower-cased, is *wanted*."""
    rows = (
        db.query(SSOConfig)
        .filter(
            func.lower(func.trim(SSOConfig.org_domain)) == wanted,
            SSOConfig.is_active.is_(True),
        )
        .all()
    )
    return [_ConfigValues.of(c) for c in rows]


class SSOExchangeResponse(BaseModel):
    """The session a hand-off code is exchanged for: what a password login returns."""

    access_token: str
    token_type: str = "bearer"
    user: UserMe


@public_router.post(
    "/exchange",
    response_model=SSOExchangeResponse,
    summary="Exchange a dashboard sign-in's hand-off code for a session",
    tags=["sso"],
    responses={400: {"description": EXCHANGE_BODY_DETAIL}},
    openapi_extra={
        "x-stability": "beta",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["code", "secret"],
                        "properties": {
                            "code": {
                                "type": "string",
                                "maxLength": sso_service.MAX_HANDOFF_CODE_LENGTH,
                            },
                            "secret": {
                                "type": "string",
                                "pattern": "^[A-Za-z0-9_-]{43}$",
                            },
                        },
                    }
                }
            },
        },
    },
)
async def sso_exchange(request: Request) -> JSONResponse:
    """`{code, secret}` -> `{access_token, token_type, user}`, or a fixed 400."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, EXCHANGE_BODY_DETAIL)
    user_id = sso_service.redeem_handoff_code(body.get("code"), body.get("secret"))
    result = await _database_step(lambda db: _exchange_session(db, user_id))
    if result is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, EXCHANGE_BODY_DETAIL)
    return JSONResponse(
        content=jsonable_encoder(result), headers={"Cache-Control": "no-store"}
    )


def _exchange_session(db: Session, user_id: uuid.UUID) -> Optional[SSOExchangeResponse]:
    """The session for an active account *user_id* names, and its entry; else None."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        return None
    result = SSOExchangeResponse(
        # The same token a password login issues, so every auth path accepts it.
        access_token=create_local_access_token(user),
        user=user_to_me(user),
    )
    _record_sso_login(db, user)
    return result


def _provisioned_account(
    db: Session, user_info: Dict[str, Any], config: _ConfigValues
) -> Tuple[uuid.UUID, bool]:
    """Find or create the account (``provision_user`` commits): its id and is_active."""
    user = sso_service.provision_user(db, user_info, config)
    return user.id, bool(user.is_active)


def _oidc_sign_in(
    db: Session, user_info: Dict[str, Any], config: _ConfigValues, provider: str
) -> Optional[OIDCLoginResponse]:
    """Find or create the account; for an active one, its token and its entry.

    None for an inactive account, after ``provision_user`` has committed any
    change it made.
    """
    user = sso_service.provision_user(db, user_info, config)
    if not user.is_active:
        return None
    result = OIDCLoginResponse(
        # The same token a password login issues, so every auth path accepts it.
        access_token=create_local_access_token(user),
        user_id=str(user.id),
        email=user.email,
        role=user.role.value if user.role else "viewer",
        provider=provider,
    )
    _record_sso_login(db, user)
    return result


async def _redirect_mode_callback(
    provider: str,
    request: Request,
    code: Optional[str],
    state: Optional[str],
    error: Optional[str],
    cookie: str,
    peeked: Dict[str, Any],
) -> RedirectResponse:
    """The callback of a sign-in started from the dashboard; always a 302.

    The order refusals are checked in decides which code the user is told:
    `error=`, no code, state mismatch, expiry, the configuration, the exchange
    and ID token, the email, the account, and anything else.
    """
    return_to = str(peeked["rt"])
    try:
        if error:
            raise sso_service.SSORefusal(
                status.HTTP_400_BAD_REQUEST,
                "OIDC provider returned error",
                sso_service.SSO_IDP_ERROR,
                idp_error=error,
            )
        if not code:
            raise sso_service.SSORefusal(
                status.HTTP_400_BAD_REQUEST,
                "Authorization code is required",
                sso_service.SSO_FAILED,
            )
        expected = str(peeked.get("st") or "").encode("utf-8")
        if not state or not hmac.compare_digest(state.encode("utf-8"), expected):
            raise sso_service.SSORefusal(
                status.HTTP_400_BAD_REQUEST,
                "OIDC state does not match this browser's sign-in",
                sso_service.SSO_STATE,
            )
        hh = peeked.get("hh")
        if not sso_service.is_handoff_value(hh):
            raise sso_service.SSORefusal(
                status.HTTP_400_BAD_REQUEST,
                "OIDC sign-in state is not valid",
                sso_service.SSO_STATE,
            )
        # The state matched, so this refuses only an expired cookie.
        login = sso_service.redeem_oidc_state(cookie, state)

        config = await _database_step(lambda db: _config_by_id(db, login.config_id))
        if (
            config is None
            or not config.is_active
            or config.provider_type.value != provider
            or login.provider != provider
        ):
            raise sso_service.SSORefusal(
                status.HTTP_404_NOT_FOUND,
                f"No active SSO config found for provider '{provider}'",
                sso_service.SSO_NOT_CONFIGURED,
            )
        sso_service.require_supported_provider(provider)

        redirect_uri = _get_redirect_uri(request, provider)
        token_data = await sso_service.exchange_oidc_code(
            config, code, redirect_uri, code_verifier=login.code_verifier
        )
        if sso_service.uses_id_token(provider):
            id_claims = sso_service.verify_id_token(
                config, provider, token_data.get("id_token"), login.nonce
            )
        else:
            id_claims = {}
        access_token = token_data.get("access_token", "")
        user_info = await sso_service.get_oidc_user_info(config, access_token)
        user_info = await sso_service.verified_email(
            config, provider, id_claims, user_info, access_token
        )
        user_id, is_active = await _database_step(
            lambda db: _provisioned_account(db, user_info, config)
        )
        if not is_active:
            raise sso_service.SSORefusal(
                status.HTTP_400_BAD_REQUEST, "Inactive user", sso_service.SSO_INACTIVE
            )
        handoff = sso_service.issue_handoff_code(user_id, str(hh))
    except sso_service.SSORefusal as exc:
        _log_refusal("callback", exc, exc.sso_error)
        return _redirect(
            _login_error_url(
                return_to, exc.sso_error, provider=provider, idp_error=exc.idp_error
            )
        )
    except HTTPException as exc:
        _log_refusal("callback", exc, sso_service.SSO_FAILED)
        return _redirect(
            _login_error_url(return_to, sso_service.SSO_FAILED, provider=provider)
        )
    except Exception:
        logger.exception(
            "SSO sign-in failed with an unexpected error (request_id=%s)",
            _request_id(),
        )
        return _redirect(
            _login_error_url(return_to, sso_service.SSO_FAILED, provider=provider)
        )
    return _redirect(f"{return_to}/sso/complete#code={handoff}")


@public_router.get(
    "/oidc/{provider}/callback",
    response_model=OIDCLoginResponse,
    summary="OIDC callback",
    tags=["sso"],
)
async def oidc_callback(
    provider: str,
    request: Request,
    response: Response,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
) -> Any:
    """Finish a login this browser started: redeem the code, provision the user.

    Every outcome expires the state cookie, so a login is redeemable once from
    this browser; the provider refuses a second use of the code.
    """
    # (A comment, not the docstring: the docstring is this operation's
    # published OpenAPI description, pinned by the stable snapshot.)
    # A login started from the dashboard (its cookie carries `rt`) always ends
    # in a 302: to `{rt}/sso/complete#code=<hand-off code>`, or to
    # `{rt}/login?sso_error=<code>`. A callback with no cookie, or one that is
    # not ours, cannot say where it came from, and goes to the primary
    # dashboard origin's `/login?sso_error=sso_state` when one is configured.
    # Otherwise -- a login started with `/oidc/{provider}/login` -- the answer
    # is JSON, as it always was.
    cookie = request.cookies.get(sso_service.OIDC_STATE_COOKIE)
    peeked = sso_service.peek_oidc_state(cookie)
    if peeked is not None and peeked.get("rt"):
        return await _redirect_mode_callback(
            provider, request, code, state, error, str(cookie), peeked
        )
    if peeked is None:
        primary = settings.primary_dashboard_origin
        if primary:
            logger.warning(
                "SSO callback with no state cookie, or one that is not ours; "
                "sending the browser to %s/login (request_id=%s)",
                primary,
                _request_id(),
            )
            return _redirect(
                _login_error_url(primary, sso_service.SSO_STATE, provider=provider)
            )
    try:
        result = await _finish_oidc_login(provider, request, code, state, error)
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        headers["set-cookie"] = _expiring_state_cookie_header()
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=headers
        ) from None
    _set_state_cookie(response, "", 0)
    return result


async def _finish_oidc_login(
    provider: str,
    request: Request,
    code: Optional[str],
    state: Optional[str],
    error: Optional[str],
) -> OIDCLoginResponse:
    sso_service.require_supported_provider(provider)
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OIDC provider returned error: {error}",
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authorization code is required",
        )

    # The state must be the one in the cookie this browser was given when it
    # started the login (#66). A state alone proves nothing -- anyone can start
    # a login and get one -- so a callback from any other browser, including
    # one carrying an attacker's own code and state, is refused here.
    login = sso_service.redeem_oidc_state(
        request.cookies.get(sso_service.OIDC_STATE_COOKIE), state
    )

    # The configuration the login was started for, not whichever active one
    # matches the provider name now.
    config = await _database_step(lambda db: _config_by_id(db, login.config_id))
    if (
        config is None
        or not config.is_active
        or config.provider_type.value != provider
        or login.provider != provider
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active SSO config found for provider '{provider}'",
        )

    redirect_uri = _get_redirect_uri(request, provider)
    token_data = await sso_service.exchange_oidc_code(
        config, code, redirect_uri, code_verifier=login.code_verifier
    )
    if sso_service.uses_id_token(provider):
        # From this exchange's own response, never from the browser.
        id_claims = sso_service.verify_id_token(
            config, provider, token_data.get("id_token"), login.nonce
        )
    else:
        id_claims = {}
    access_token = token_data.get("access_token", "")

    user_info = await sso_service.get_oidc_user_info(config, access_token)
    # Only an email the provider verified, in this configuration's domain.
    user_info = await sso_service.verified_email(
        config, provider, id_claims, user_info, access_token
    )
    result = await _database_step(
        lambda db: _oidc_sign_in(db, user_info, config, provider)
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user"
        )
    return result


# ---------------------------------------------------------------------------
# Admin CRUD endpoints for SSO configs
# ---------------------------------------------------------------------------


@router.get(
    "/configs",
    response_model=List[SSOConfigResponse],
    summary="List SSO configs",
    tags=["sso"],
)
def list_sso_configs(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> List[SSOConfigResponse]:
    """Return all SSO configurations. Admin only."""
    _require_admin(current_user)
    configs = sso_service.list_sso_configs(db)
    return [SSOConfigResponse.model_validate(c) for c in configs]


@router.post(
    "/configs",
    response_model=SSOConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create SSO config",
    tags=["sso"],
)
def create_sso_config(
    config_data: SSOConfigCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SSOConfigResponse:
    """Create a new SSO configuration. Admin only."""
    _require_admin(current_user)
    data_dict = config_data.model_dump(exclude_none=False)
    sso_config = sso_service.create_sso_config(db, data_dict)
    return SSOConfigResponse.model_validate(sso_config)


@router.get(
    "/configs/{config_id}",
    response_model=SSOConfigResponse,
    summary="Get SSO config",
    tags=["sso"],
)
def get_sso_config(
    config_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SSOConfigResponse:
    """Get a specific SSO configuration by ID. Admin only."""
    _require_admin(current_user)
    sso_config = sso_service.get_sso_config_by_id(db, config_id)
    if not sso_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO config '{config_id}' not found",
        )
    return SSOConfigResponse.model_validate(sso_config)


@router.put(
    "/configs/{config_id}",
    response_model=SSOConfigResponse,
    summary="Update SSO config",
    tags=["sso"],
)
def update_sso_config(
    config_id: uuid.UUID,
    update_data: SSOConfigUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SSOConfigResponse:
    """Update an existing SSO configuration. Admin only."""
    _require_admin(current_user)
    data_dict = update_data.model_dump(exclude_none=True)
    sso_config = sso_service.update_sso_config(db, config_id, data_dict)
    return SSOConfigResponse.model_validate(sso_config)


@router.delete(
    "/configs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete SSO config",
    tags=["sso"],
    response_model=None,
)
def delete_sso_config(
    config_id: uuid.UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """Delete an SSO configuration. Admin only."""
    _require_admin(current_user)
    deleted = sso_service.delete_sso_config(db, config_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SSO config '{config_id}' not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

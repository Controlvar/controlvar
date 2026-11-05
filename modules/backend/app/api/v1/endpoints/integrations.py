"""
Integration configuration management API endpoints — EP-034 Batch 3.

Provides CRUD for Salesforce / Jira / GitHub integration configs and
inbound webhook handlers for each integration type.

Permission model:
  - GET /integrations, GET /integrations/{type}  → ADMIN or DEVELOPER
  - POST / PUT / DELETE                           → ADMIN only
  - Webhook endpoints                             → no platform user; the
      *sender* is authenticated against the integration's own
      ``webhook_secret`` before the payload is read (see
      ``services.integrations.webhook_auth``).  Failing that is 401.

Every route that answers with a configuration answers with
``IntegrationConfigResponse``, which shows the connection settings and names
the stored secrets without their values, for every caller (see
``schemas.integration.SHOWN_CONFIG_KEYS``).  Because no response carries a
secret's value, ``PUT`` merges ``encrypted_config`` key by key rather than
replacing it: see :func:`merge_config`.
"""

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.models.user import User, UserRole
from modules.backend.app.models.integration_config import (
    IntegrationConfig,
    IntegrationType,
)
from modules.backend.app.schemas.integration import (
    IntegrationConfigCreate,
    IntegrationConfigResponse,
    IntegrationConfigUpdate,
)
from modules.backend.app.services.integrations import webhook_auth
from modules.backend.app.services.integrations.github_service import GitHubService
from modules.backend.app.services.integrations.jira_service import JiraService
from modules.backend.app.services.integrations.salesforce_service import (
    SalesforceService,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Inbound webhooks from Jira, Salesforce and GitHub.  There is no platform
#: user to ask for, so the registration mounts this without an authentication
#: dependency; each handler instead authenticates the *sender* against the
#: integration's ``webhook_secret`` before it reads the body, and answers 401
#: when that fails.  Nothing here is reachable anonymously without the secret.
public_router = APIRouter()


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _require_admin(current_user: User) -> None:
    """Raise HTTP 403 if the caller is not ADMIN (or superuser)."""
    if current_user.is_superuser:
        return
    if getattr(current_user, "role", None) == UserRole.ADMIN:
        return
    raise HTTPException(
        status_code=403, detail="Only ADMIN users can manage integrations"
    )


def _require_admin_or_developer(current_user: User) -> None:
    """Raise HTTP 403 if the caller is neither ADMIN nor DEVELOPER (or superuser)."""
    if current_user.is_superuser:
        return
    allowed = {UserRole.ADMIN, UserRole.DEVELOPER}
    if getattr(current_user, "role", None) in allowed:
        return
    raise HTTPException(status_code=403, detail="ADMIN or DEVELOPER role required")


def merge_config(
    stored: Optional[Dict[str, Any]], changes: Dict[str, Any]
) -> Dict[str, Any]:
    """*stored* with *changes* applied key by key, as a new dict.

    A key in *changes* with a value sets it, a key sent as ``None`` (JSON
    ``null``) removes it, and a stored key that *changes* does not mention is
    kept.  A response never carries a secret's value, so a client that reads a
    configuration, changes one key and sends it back must not lose the
    secrets it could not send.
    """
    merged = dict(stored or {})
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[IntegrationConfigResponse])
def list_integrations(
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Return all integration configurations (ADMIN / DEVELOPER only)."""
    _require_admin_or_developer(current_user)
    return db.query(IntegrationConfig).all()


@router.get("/{integration_type}", response_model=IntegrationConfigResponse)
def get_integration(
    integration_type: IntegrationType,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Retrieve a single integration config by type (ADMIN / DEVELOPER only)."""
    _require_admin_or_developer(current_user)
    config = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.integration_type == integration_type)
        .first()
    )
    if not config:
        raise HTTPException(
            status_code=404,
            detail=f"No {integration_type.value} integration configured",
        )
    return config


@router.post("", response_model=IntegrationConfigResponse, status_code=201)
def create_integration(
    data: IntegrationConfigCreate,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Create a new integration configuration (ADMIN only)."""
    _require_admin(current_user)

    existing = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.integration_type == data.integration_type)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"{data.integration_type.value} integration already configured",
        )

    config = IntegrationConfig(
        integration_type=data.integration_type,
        is_active=data.is_active,
        encrypted_config=data.encrypted_config,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.put("/{integration_type}", response_model=IntegrationConfigResponse)
def update_integration(
    integration_type: IntegrationType,
    data: IntegrationConfigUpdate,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Update an existing integration configuration (ADMIN only).

    Every field is optional, and a field left out is unchanged.
    `encrypted_config` is merged key by key: a key sent with a value replaces
    it, a key sent as `null` removes it, and a stored key not sent is kept.
    """
    _require_admin(current_user)

    config = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.integration_type == integration_type)
        .first()
    )
    if not config:
        raise HTTPException(
            status_code=404,
            detail=f"No {integration_type.value} integration configured",
        )

    if data.is_active is not None:
        config.is_active = data.is_active
    if data.encrypted_config is not None:
        config.encrypted_config = merge_config(
            config.encrypted_config, data.encrypted_config
        )
    if data.last_error is not None or "last_error" in (data.model_fields_set or set()):
        config.last_error = data.last_error

    db.commit()
    db.refresh(config)
    return config


@router.delete("/{integration_type}", status_code=204)
def delete_integration(
    integration_type: IntegrationType,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Delete an integration configuration (ADMIN only)."""
    _require_admin(current_user)

    config = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.integration_type == integration_type)
        .first()
    )
    if not config:
        raise HTTPException(
            status_code=404,
            detail=f"No {integration_type.value} integration configured",
        )

    db.delete(config)
    db.commit()


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------
#
# These three routes are the platform's only anonymous write path: no bearer
# token, no API key, a body of the caller's choosing.  So every one of them
# authenticates the sender against the integration's own ``webhook_secret``
# (``services.integrations.webhook_auth``) *before* the payload is parsed or
# handed to a service.
#
# The rejection is always the same 401 with the same wording, whether the
# secret was wrong, absent, unconfigured, or the integration does not exist at
# all: the reply must not tell an anonymous caller which integrations this
# deployment has.  Only after the sender is known does the old behaviour
# resume — a malformed body is 400, and a failure *inside* the service is
# swallowed and answered 200, because a provider must not retry forever over
# something it cannot fix.
#
# Two things follow from that, and both were got wrong once:
#
# * The check reads ``encrypted_config["webhook_secret"]`` directly, not
#   ``Service.from_config()``.  That factory answers None when the *outbound*
#   API credentials are missing (Salesforce wants instance_url/client_id/
#   client_secret, GitHub a token and a repo), which an inbound delivery never
#   touches: routing authentication through it meant a receive-only
#   integration — a perfectly ordinary way to configure one — could not
#   authenticate anybody, whatever secret it held.  The service is built
#   afterwards, for parsing, and its absence is a 200 with a log line, not a
#   401.
#
# * An integration configured before this release has no ``webhook_secret``
#   at all, so every delivery to it is refused — correctly (an anonymous write
#   path fails closed) but silently, since the 401 is deliberately mute.  So
#   the *log* names it and says what to do: see `_warn_no_secret`, and
#   "Upgrading an integration created before webhook authentication" in
#   docs/api/integrations.md.


#: One wording for every refused webhook (see above).
UNAUTHENTICATED_DETAIL = "Webhook authentication failed"


#: What the OpenAPI document says about that refusal.
UNAUTHENTICATED_RESPONSE: Dict[Union[int, str], Dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": (
            "The sender did not present the integration's webhook_secret, or "
            "no active integration of this type is configured"
        ),
    },
}


def _unauthenticated() -> HTTPException:
    """The single refusal every failed webhook authentication raises."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED_DETAIL
    )


def _active_config(
    db: Session, integration_type: IntegrationType
) -> Optional[IntegrationConfig]:
    """The active IntegrationConfig of this type, or None."""
    return (
        db.query(IntegrationConfig)
        .filter(
            IntegrationConfig.integration_type == integration_type,
            IntegrationConfig.is_active.is_(True),
        )
        .first()
    )


@dataclass(frozen=True)
class _ActiveIntegration:
    """An active integration's ``encrypted_config``, read in a worker.

    Everything a delivery reads from its configuration once it is found --
    the ``webhook_secret`` and what ``<Service>.from_config`` builds a service
    from -- is in this one field, so it is all the route keeps: a plain value,
    not the row, whose session is closed by then.
    """

    encrypted_config: Any = field(repr=False)


def _lookup_active_integration(
    integration_type: IntegrationType,
) -> Optional[_ActiveIntegration]:
    """Run in a worker thread: the lookup on a session of its own, then closed.

    The session is opened, queried and closed here, so the request holds no
    connection once the lookup is over.
    """
    db = deps.SessionLocal()
    try:
        config = _active_config(db, integration_type)
        if config is None:
            return None
        return _ActiveIntegration(copy.deepcopy(config.encrypted_config))
    finally:
        db.close()


async def _active_integration(
    integration_type: IntegrationType,
) -> Optional[_ActiveIntegration]:
    """The active integration of this type, or None, looked up in a worker.

    The webhook routes are async and never use the database on the event
    loop: a query there that has to wait for a connection holds up every
    other request the process is serving until it gets one.
    """
    return await run_in_threadpool(_lookup_active_integration, integration_type)


def _webhook_secret(config: Union[IntegrationConfig, _ActiveIntegration]) -> str:
    """The shared secret an inbound delivery to *config* is checked against.

    The one thing webhook authentication needs, read straight off the config
    — see the note above on why ``Service.from_config()`` is not asked.
    """
    return (config.encrypted_config or {}).get("webhook_secret") or ""


#: Integration types already named in a "no webhook_secret" log line.  The
#: warning is worth saying once per process, not once per delivery: a provider
#: that retries turns the second into a flood, and the first is what an
#: operator greps for.
_WARNED_NO_SECRET: set = set()


def _warn_no_secret(integration_type: IntegrationType) -> None:
    """Say, once, that this integration cannot authenticate anything.

    The 401 that follows says nothing (by design), and an integration created
    before webhook authentication existed carries no ``webhook_secret``, so
    without this line the whole symptom is deliveries that stopped arriving.
    """
    if integration_type in _WARNED_NO_SECRET:
        return
    _WARNED_NO_SECRET.add(integration_type)
    logger.error(
        "The active %s integration has no webhook_secret in its "
        "encrypted_config, so every inbound delivery to "
        "/api/v1/integrations/webhooks/%s is refused with 401. Add one (any "
        "strong random string, e.g. `openssl rand -hex 32`) with PUT "
        "/api/v1/integrations/%s and configure the same value at the "
        "provider; see docs/api/integrations.md.",
        integration_type.value,
        integration_type.value,
        integration_type.value,
    )


def _signature_header(request: Request) -> str:
    """The first signature header the request carries, or ``""``."""
    for name in webhook_auth.SIGNATURE_HEADERS:
        value = request.headers.get(name)
        if value:
            return value
    return ""


def _secret_header(request: Request) -> str:
    """The shared-secret header, or ``""``."""
    return request.headers.get(webhook_auth.SHARED_SECRET_HEADER, "")


#: The outbound credentials each ``from_config`` needs, named in the log line
#: below when an *authenticated* delivery arrives for an integration that has
#: none of them.
_API_CREDENTIALS: Dict[IntegrationType, str] = {
    IntegrationType.JIRA: "base_url/api_token/email",
    IntegrationType.SALESFORCE: "instance_url/client_id/client_secret",
    IntegrationType.GITHUB: "token/repo_owner/repo_name",
}


def _parse_authenticated(
    service: Any, integration_type: IntegrationType, parse: Any
) -> None:
    """Run *parse* against *service*, tolerating a service that could not be built.

    The sender is already authenticated by the time this runs, so nothing here
    may turn into a refusal.  ``SalesforceService.from_config`` and
    ``GitHubService.from_config`` answer None when the *outbound* credentials
    are missing — a receive-only integration — and that means the delivery is
    accepted and not parsed, with a line saying so, rather than rejected.
    """
    if service is None:
        logger.warning(
            "%s webhook authenticated, but the integration has no API "
            "credentials (%s), so the event was not parsed",
            integration_type.value,
            _API_CREDENTIALS[integration_type],
        )
        return
    try:
        parse(service)
    except Exception as exc:
        logger.warning("%s_webhook processing error: %s", integration_type.value, exc)


def _decode(body: bytes) -> dict:
    """Parse an authenticated webhook body, or raise 400."""
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    return payload


@public_router.post(
    "/webhooks/jira",
    status_code=200,
    responses=UNAUTHENTICATED_RESPONSE,
)
async def jira_webhook(
    request: Request,
):
    """Receive and process Jira webhook events.

    Authenticates the sender against the Jira integration's ``webhook_secret``
    — Jira Cloud's ``X-Hub-Signature`` over the raw body, or the secret itself
    in ``X-Experimently-Webhook-Secret`` — and answers 401 when that fails.
    Once the sender is known, delegates to ``JiraService.parse_webhook_event``
    and returns 200 even if that raises: Jira delivery failures must not be
    surfaced to the caller.
    """
    body = await request.body()

    config = await _active_integration(IntegrationType.JIRA)
    if config is None:
        raise _unauthenticated()
    secret = _webhook_secret(config)
    if not secret:
        _warn_no_secret(IntegrationType.JIRA)
        raise _unauthenticated()
    if not webhook_auth.verify_webhook(
        secret, body, _signature_header(request), _secret_header(request)
    ):
        raise _unauthenticated()

    payload = _decode(body)
    _parse_authenticated(
        JiraService.from_config(config),
        IntegrationType.JIRA,
        lambda service: service.parse_webhook_event(payload),
    )

    return {"status": "received"}


@public_router.post(
    "/webhooks/salesforce",
    status_code=200,
    responses=UNAUTHENTICATED_RESPONSE,
)
async def salesforce_webhook(
    request: Request,
):
    """Receive and process a Salesforce webhook delivery.

    The body is a JSON object posted by a Salesforce Flow HTTP Callout, an
    Apex callout or a relay.  A native Salesforce Outbound Message sends
    SOAP/XML with no custom headers, so it cannot call this route.

    Authenticates the sender against the Salesforce integration's
    ``webhook_secret`` — the secret in ``X-Experimently-Webhook-Secret``, or an
    ``X-Hub-Signature-256`` from a callout that can sign — and answers 401 when
    that fails.  Once the sender is known, a body that is not a JSON object is
    400, and any other failure is logged and still answered 200: Salesforce
    failures must not be surfaced.
    """
    body = await request.body()

    config = await _active_integration(IntegrationType.SALESFORCE)
    if config is None:
        raise _unauthenticated()
    secret = _webhook_secret(config)
    if not secret:
        _warn_no_secret(IntegrationType.SALESFORCE)
        raise _unauthenticated()
    if not webhook_auth.verify_webhook(
        secret, body, _signature_header(request), _secret_header(request)
    ):
        raise _unauthenticated()

    payload = _decode(body)
    _parse_authenticated(
        SalesforceService.from_config(config),
        IntegrationType.SALESFORCE,
        lambda service: service.parse_webhook_event(payload),
    )

    return {"status": "received"}


@public_router.post(
    "/webhooks/github",
    status_code=200,
    responses=UNAUTHENTICATED_RESPONSE,
)
async def github_webhook(
    request: Request,
):
    """Receive and process GitHub webhook events.

    GitHub signs every delivery, so the ``X-Hub-Signature-256`` HMAC-SHA256 of
    the raw body is *required*: a missing, malformed or wrong signature — and a
    config with no ``webhook_secret`` to check it against — is 401.  A verified
    delivery is parsed and handed to ``GitHubService.parse_webhook_event``;
    failures there are logged and still answered 200.
    """
    body = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "")

    config = await _active_integration(IntegrationType.GITHUB)
    if config is None:
        raise _unauthenticated()
    secret = _webhook_secret(config)
    if not secret:
        _warn_no_secret(IntegrationType.GITHUB)
        raise _unauthenticated()
    # Only the header GitHub actually sends: it signs with SHA-256 and puts it
    # here. (It sends a legacy SHA-1 `X-Hub-Signature` too, which this ignores.)
    if not webhook_auth.verify_signature(
        secret, body, request.headers.get("X-Hub-Signature-256", "")
    ):
        raise _unauthenticated()

    payload = _decode(body)
    _parse_authenticated(
        GitHubService.from_config(config),
        IntegrationType.GITHUB,
        lambda service: service.parse_webhook_event(event_type, payload),
    )

    return {"status": "received"}

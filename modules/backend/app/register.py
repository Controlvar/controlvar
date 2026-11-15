"""
The modules' registration entry point: ``modules.register(hooks)``.

One ``register(hooks)`` that installs the optional modules into the core
application through the seam in :mod:`backend.app.core.hooks`: the modules'
settings, the nine model modules, the audit signer, the nine endpoint modules' routers, the
OpenAPI tags, the six capabilities and the ten module names.
``backend/app/modules_loader.py`` calls it once per process, from whichever of
``modules.register`` and ``modules.backend.app.register`` it finds first
(``modules/__init__`` re-exports this function), and never by importing
anything else from here.

It exists so that the three places that consume the seam agree:

* ``db/bootstrap.py`` on a fresh database creates the module tables that the
  module routers need (without this, ``make dev`` on an empty database served
  ``/api/v1/workspaces`` against no ``workspaces`` table);
* every real process signs compliance audit events, not only the test suite
  (``hooks.audit_signer`` was the null signer everywhere except ``conftest``);
* ``GET /api/v1/modules`` and the routers describe the same deployment: the
  names passed to ``hooks.register_modules`` are exactly the modules whose
  routes and capabilities this function mounts.

A core build has no ``modules/`` directory at all, so it has neither this
module nor anything it names, and ``load_modules()`` reports the core profile.

The loader may ask for ``register(hooks, with_routers=False)``: everything a
*schema* needs and none of the API graph, which is what the bootstrap, alembic
and the test conftest go through.  See :func:`register`.

Nothing outside ``modules_loader`` (and ``modules/__init__``) may import this
module.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The modules this registration provides -- every name in
#: ``hooks.KNOWN_MODULES``, one per group of ``modules-manifest.txt``.
#: ``warehouse`` provides the warehouse analysis routes (#312), all beta.
PROVIDED_MODULES: tuple[str, ...] = (
    "workspaces",
    "hipaa",
    "compliance",
    "sso",
    "rbac",
    "warehouse",
    "integrations",
    "counters",
    "etl",
    "split_url",
)

#: SQLAlchemy model modules whose tables belong to the optional modules.
#: Kept in step with the MODULE TABLES section of ``modules-manifest.txt``.
MODEL_MODULES: tuple[str, ...] = (
    "modules.backend.app.models.baa_config",
    "modules.backend.app.models.custom_role",
    "modules.backend.app.models.integration_config",
    "modules.backend.app.models.phi_audit_log",
    "modules.backend.app.models.sso_config",
    "modules.backend.app.models.warehouse_analysis_run",
    "modules.backend.app.models.warehouse_connection",
    "modules.backend.app.models.warehouse_source",
    "modules.backend.app.models.workspace",
)

MODULE_TAGS: list[dict[str, str]] = [
    {
        "name": "RBAC",
        "description": "Operations for managing custom roles, permission delegation, and effective permission resolution",
    },
    {
        "name": "Real-time Counters",
        "description": "Real-time DynamoDB atomic counters for experiment assignments, events, and conversions",
    },
    {
        "name": "ETL",
        "description": "Operations for managing AWS Glue ETL jobs, S3 partitions, and Glue crawlers",
    },
    {
        "name": "Integrations",
        "description": "Jira, Salesforce and GitHub integration configuration and webhooks",
    },
    {
        "name": "SSO",
        "description": (
            "OIDC single sign-on with just-in-time provisioning. "
            "SAML 2.0 sign-in is not available yet."
        ),
    },
    {
        "name": "Workspaces",
        "description": (
            "EP-057: Team Workspaces — group users into teams with workspace roles "
            "(OWNER/ADMIN/DEVELOPER/ANALYST/VIEWER) and create and accept "
            "invitation links; no email is sent. "
            "Workspaces do not limit access to experiments or feature flags, which is "
            "by platform role. Workspaces have no plan and no limits, and issue no "
            "API keys: SDKs and integrations use a platform API key."
        ),
    },
    {
        "name": "Warehouse analysis",
        "description": (
            "Beta. Frequentist analysis of an experiment on the customer's own "
            "exposure and metric tables in BigQuery or Snowflake: connections, "
            "sources (a table or view and a column mapping), previews and runs. "
            "Each connector is available only once it has been verified against "
            "a real account."
        ),
    },
    {
        "name": "HIPAA",
        "description": (
            "EP-050: HIPAA Compliance — PHI audit logging, Business Associate Agreement (BAA) "
            "management, PHI field encryption/decryption, data residency validation, "
            "and HIPAA readiness status reporting. All endpoints require ADMIN role."
        ),
    },
]


def _authenticated() -> list:
    """``dependencies=`` for a module router whose every route needs a user.

    Mounted behind authentication and nothing else: an installed module is
    available to every authenticated user its routes' own role checks admit.
    The user dependency is cached per request, so the route's own
    ``get_current_active_user`` does not run twice.
    """
    from fastapi import Depends

    from backend.app.api import deps

    return [Depends(deps.get_current_active_user)]


def mount_routers(router: Any) -> None:
    """Mount every module router on the v1 router.

    Routers whose every route needs a user are mounted behind authentication.
    The three modules that also serve public routes -- inbound webhooks, the
    SSO login flow, the invite preview -- expose them on a separate
    ``public_router`` that is mounted behind nothing, as their routes are
    public by design. Split URL routing has only a ``public_router``: its
    routes are called by a server or edge, and each takes an API key.
    """
    modules = _endpoint_modules()
    etl = modules["etl"]
    hipaa = modules["hipaa"]
    integrations = modules["integrations"]
    rbac = modules["rbac"]
    realtime_counters = modules["realtime_counters"]
    split_url_routing = modules["split_url_routing"]
    sso = modules["sso"]
    warehouse_analysis = modules["warehouse_analysis"]
    workspaces = modules["workspaces"]

    # P2-A: RBAC Post-MVP Enhancements
    router.include_router(
        rbac.router,
        prefix="/rbac",
        tags=["RBAC"],
        dependencies=_authenticated(),
    )
    # P2-B: Real-time Counters in DynamoDB
    router.include_router(
        realtime_counters.router,
        prefix="/counters",
        tags=["Real-time Counters"],
        dependencies=_authenticated(),
    )
    # P3-A: ETL & Glue Jobs for S3 Data Lake
    router.include_router(
        etl.router,
        prefix="/etl",
        tags=["ETL"],
        dependencies=_authenticated(),
    )
    # Warehouse analysis (#312), beta.
    router.include_router(
        warehouse_analysis.router,
        prefix="/warehouse/analysis",
        tags=["Warehouse analysis"],
        dependencies=_authenticated(),
    )

    # EP-034: Integration Config Management (Salesforce / Jira / GitHub)
    router.include_router(
        integrations.router,
        prefix="/integrations",
        tags=["Integrations"],
        dependencies=_authenticated(),
    )
    # No platform user to ask for: each handler authenticates the *sender*
    # against the integration's own webhook_secret and answers 401 otherwise.
    router.include_router(
        integrations.public_router,
        prefix="/integrations",
        tags=["Integrations"],
    )
    # EP-037: SSO/SAML & OIDC authentication
    router.include_router(
        sso.router,
        prefix="/auth/sso",
        tags=["SSO"],
        dependencies=_authenticated(),
    )
    # The login flow: by definition there is no user yet.
    router.include_router(
        sso.public_router,
        prefix="/auth/sso",
        tags=["SSO"],
    )
    # EP-057: Multi-Tenant Team Workspaces
    router.include_router(
        workspaces.router,
        prefix="/workspaces",
        tags=["Workspaces"],
        dependencies=_authenticated(),
    )
    # The invite preview: read by the invitee before they have an account.
    router.include_router(
        workspaces.public_router,
        prefix="/workspaces",
        tags=["Workspaces"],
    )
    # EP-050: HIPAA Compliance
    router.include_router(
        hipaa.router,
        prefix="/hipaa",
        tags=["HIPAA"],
        dependencies=_authenticated(),
    )
    # Split URL routing (#393), beta: a server or edge asks where to send each
    # visitor. No user: each route takes an API key, as the tracking routes do.
    router.include_router(
        split_url_routing.public_router,
        prefix="/tracking/split-url",
        tags=["Tracking"],
    )


#: The endpoint modules, by name under ``modules.backend.app.api.v1.endpoints``.
ENDPOINT_MODULES: tuple[str, ...] = (
    "etl",
    "hipaa",
    "integrations",
    "rbac",
    "realtime_counters",
    "split_url_routing",
    "sso",
    "warehouse_analysis",
    "workspaces",
)


def _endpoint_modules() -> dict[str, Any]:
    """Import (or fetch, once imported) every endpoint module."""
    import importlib

    return {
        name: importlib.import_module(f"modules.backend.app.api.v1.endpoints.{name}")
        for name in ENDPOINT_MODULES
    }


def _import_endpoint_modules() -> None:
    """Import every endpoint module now, during the registration.

    ``mount_routers`` runs later, from the router build.  Both are guarded by
    ``backend/app/modules_loader.py`` (``load_modules`` and
    ``mount_module_routers``), so neither can take the API down -- but only a
    failure *here*, while the registration is running, is rolled back: the
    loader clears the registries and the process starts cleanly on the core
    profile.  A failure at mount time leaves a registration that succeeded
    standing and costs the deployment its module routes.  Importing the
    nine modules up front puts the likeliest failure -- an endpoint module
    that does not import -- on the side that rolls back.
    """
    _endpoint_modules()


def register(hooks: Any, with_routers: bool = True) -> None:
    """Install the optional modules into *hooks*.  Idempotent.

    Settings first: ``load_modules_settings()`` builds and validates the
    ``ModulesSettings``, and a placeholder ``AUDIT_HMAC_KEY`` in
    staging/production is refused *here* rather than in the core ``Settings``
    (issue #91) -- the loader records the failure, the running API continues
    on the core profile with an ERROR in the log, and the schema builders
    (bootstrap, alembic, the test conftest) refuse to build.

    Then the models, then everything that needs the API graph: the schema
    builders call this too, and the nine model modules are cheap and
    self-contained, while the endpoint modules pull in the whole service
    layer.  Ordering them this way keeps the part a schema tool needs ahead of
    the part most likely to fail on an endpoint-only dependency.

    ``with_routers=False`` stops before the API graph, and is what
    ``require_modules_or_absent()`` asks for on behalf of the three schema
    builders -- alembic's ``env.py``, ``db/bootstrap.py`` and the test
    conftest.  Importing the endpoint modules is ~99% of a registration's
    cost (3.5 s here when there were eleven, against 0.03 s for the seven
    model modules), and it buys a schema tool nothing: it pulls in FastAPI
    routers and client libraries that no ``Base.metadata`` and no migration
    touches.
    Every container start pays it inside the bootstrap's advisory lock, and so
    does every ``alembic upgrade``/``current``/``revision``.

    What a schema-only registration must still do is everything that decides
    what the *schema* is, and it does: the settings (so a bad secret is still
    the registration's failure) and the nine model modules (so
    ``Base.metadata`` holds the module tables and autogenerate does not
    propose dropping them).  The routers' own guarantee -- that an endpoint
    module which fails to import is caught by the loader rather than by
    ``apply_routers`` -- is unaffected, because the process that builds a
    router asks for a registration ``with_routers=True``, which re-runs this
    function (see :func:`backend.app.modules_loader.load_modules`).
    """
    from modules.backend.app import settings as modules_settings

    modules_settings.load_modules_settings()

    for module_path in MODEL_MODULES:
        hooks.register_model_module(module_path)

    from modules.backend.app.services.audit_signing_service import (
        AuditSigningService,
    )

    # Tamper-evidence for compliance audit events (EP-033).  Not part of the
    # API graph, and a schema builder that writes an audit row needs it.
    hooks.set_audit_signer(AuditSigningService())

    if not with_routers:
        logger.debug(
            "Modules registered from modules.backend.app.register "
            "(schema only: the endpoint modules were not imported)"
        )
        return

    _import_endpoint_modules()

    from modules.backend.app.api.v1.endpoints.compliance_reports import (
        export_audit_events,
        generate_compliance_report,
    )
    from modules.backend.app.api.v1.endpoints.experiments_split_url import (
        preview_split_url_assignment,
    )
    from modules.backend.app.services.split_url_service import stored_or_new_variant

    hooks.register_router(mount_routers)
    hooks.register_tags(MODULE_TAGS)

    # Capabilities: the keys are the contract with core/optional_modules.py.
    # Route bodies and the routing function are registered as they are; the
    # core caller only ever asks "is this here?" and has its own 501.
    hooks.register_capability("split_url.routing", stored_or_new_variant)
    hooks.register_capability("split_url.preview", preview_split_url_assignment)
    hooks.register_capability("compliance.report", generate_compliance_report)
    hooks.register_capability("compliance.export", export_audit_events)

    # A *provider*, not the class: resolved at each call rather than captured
    # at registration, so the class is whatever the service module holds
    # *now* -- which is what lets a test patch it at its definition site.
    def counter_service_provider() -> type:
        from modules.backend.app.services import dynamodb_counter_service

        return dynamodb_counter_service.DynamoDBCounterService

    hooks.register_capability("counters.service", counter_service_provider)

    # Custom roles and direct grants take effect (#891): every core permission
    # check asks this provider when the built-in role says no.  Only on this
    # branch, so a schema tool (which checks no permission) registers none.
    from modules.backend.app.services.rbac_service import granted_permission_pairs

    hooks.register_capability("rbac.granted_permissions", granted_permission_pairs)

    # Last, and only on this branch: the names `GET /api/v1/modules` reports
    # are exactly the modules whose routes and capabilities were just mounted
    # (see the module docstring).  A schema-only registration mounts none, so
    # it claims none -- nothing but that route reads them.
    hooks.register_modules(PROVIDED_MODULES)
    logger.debug("Modules registered from modules.backend.app.register")

"""
Core-side accessors for the optional modules' capabilities.

Core code must never import the ``modules`` package directly.  Where a core
route or service needs to *reach* a module's capability when that module
happens to be installed, it asks this module, which looks the capability up in
``backend.app.core.hooks`` by key.  Every accessor returns ``None`` in a build
that has not registered the capability, and each caller answers that with an
explicit HTTP 501 -- never with silence.  501 means exactly one thing: this
deployment does not have the module installed.

The keys are the contract with ``modules.register(hooks)``
(``modules/backend/app/register.py``):

``split_url.routing``
    The routing resolver itself (the variant a visitor gets now, without
    recording anything).  Read through :func:`split_url_routing_available`,
    which gates *creation* of ``experiment_type=split_url`` -- without
    routing, a core-profile user could store a split-URL experiment that
    nothing ever routes.

``split_url.preview``
    The body of ``GET /experiments/{id}/split-url/preview``.

``compliance.report`` / ``compliance.export``
    The bodies of ``GET /compliance/reports/{standard}`` and
    ``GET /compliance/export``.  ``GET /compliance/audit-events`` is core and
    is NOT brokered here.

``counters.service``
    A **provider**: a zero-argument callable answering the
    ``DynamoDBCounterService`` class -- the first of ``BanditScheduler``'s
    four stats sources -- resolved at each call rather than captured at
    registration, so the class is whatever the service module holds *now*
    (which is what lets a test patch it at its definition site).  Absent, the
    scheduler simply falls through to PostgreSQL.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from backend.app.core import hooks

# Detail strings used by the core 501 responses.  Kept here so the wording
# stays identical everywhere a capability is refused.
SPLIT_URL_UNAVAILABLE_DETAIL = (
    "Split URL testing is not installed in this deployment. "
    "experiment_type='split_url' requires the split_url module."
)
COMPLIANCE_REPORTING_UNAVAILABLE_DETAIL = (
    "Compliance report generation and audit export are not installed in this "
    "deployment (the compliance module). The compliance audit event log "
    "(GET /compliance/audit-events) is available in every profile."
)


def _provided(capability: str) -> Optional[Any]:
    """Resolve a provider-style capability: call it, return what it answers."""
    provider = hooks.get_capability(capability)
    if provider is None:
        return None
    return provider()


def split_url_routing_available() -> bool:
    """True when the split_url module's routing is installed."""
    return hooks.get_capability("split_url.routing") is not None


def split_url_preview_handler() -> Optional[Callable[..., Any]]:
    """The split_url module's handler for the preview route, or ``None``."""
    return hooks.get_capability("split_url.preview")


def compliance_report_handler() -> Optional[Callable[..., Any]]:
    """The compliance module's handler for report generation, or ``None``."""
    return hooks.get_capability("compliance.report")


def compliance_export_handler() -> Optional[Callable[..., Any]]:
    """The compliance module's handler for the audit export route, or ``None``."""
    return hooks.get_capability("compliance.export")


def realtime_counter_service() -> Optional[type]:
    """The counters module's ``DynamoDBCounterService`` class, or ``None``."""
    return _provided("counters.service")

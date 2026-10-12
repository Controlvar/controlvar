"""
API router configuration.

This module configures the API router by including all endpoint modules
and applying appropriate prefixes and tags.
"""

from fastapi import APIRouter

from backend.app.api.v1.endpoints import (
    admin,
    ai_design,
    api_keys,
    assignments,
    audit_logs,
    auth,
    bandit,
    bulk_toggle,
    client_errors,
    compliance,
    events,
    experiment_wizard,
    experiments,
    export,
    feature_flags,
    flag_evaluations,
    global_holdout,
    interactions,
    llm_experiments,
    llm_proxy,
    mcp,
    metrics,
    modules,
    mutual_exclusion_groups,
    notifications,
    post_stratification,
    power_calculator,
    results,
    rollout_schedules,
    safety,
    scheduler_health,
    sdk,
    segments,
    tracking,
    users,
    websocket_results,
)

# Import the sample size calculator router
from backend.app.api.v1.sample_size_calculator import router as sample_size_router
from backend.app.core import hooks
from backend.app.modules_loader import load_modules, mount_module_routers


def register_core_routers(router: APIRouter) -> APIRouter:
    """Mount every core endpoint router on *router*; returns it.

    The core half of the seam for the API surface: a core build calls only
    this, and whatever the modules package registered through
    ``hooks.register_router()`` is mounted afterwards by
    :func:`build_v1_router`.
    """
    router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
    # P0 (open-core): user-owned API keys for SDK authentication
    router.include_router(api_keys.router, prefix="/api-keys", tags=["API Keys"])
    router.include_router(users.router, prefix="/users", tags=["Users"])
    # Unauthenticated profile/modules probe for the dashboard chrome.
    router.include_router(modules.router, tags=["Modules"])
    router.include_router(
        experiments.router, prefix="/experiments", tags=["Experiments"]
    )
    router.include_router(tracking.router, prefix="/tracking", tags=["Tracking"])
    # Error reports (POST /tracking/errors, /tracking/errors/batch) feed feature flag
    # safety monitoring; same prefix and API-key auth as event tracking. Only reports
    # sent with an sdk:ruleset key count toward automatic rollback.
    router.include_router(client_errors.router, prefix="/tracking", tags=["Tracking"])
    # Locally evaluated flag counts (POST /tracking/evaluations, beta): the safety
    # denominator for SDKs that evaluate flags in-process; needs sdk:ruleset.
    router.include_router(
        flag_evaluations.router, prefix="/tracking", tags=["Tracking"]
    )
    router.include_router(
        feature_flags.router, prefix="/feature-flags", tags=["Feature Flags"]
    )
    router.include_router(admin.router, prefix="/admin", tags=["Administration"])
    router.include_router(
        assignments.router, prefix="/assignments", tags=["Assignments"]
    )
    router.include_router(events.router, prefix="/events", tags=["Events"])
    router.include_router(results.router, prefix="/results", tags=["Results"])
    router.include_router(
        rollout_schedules.router,
        prefix="/rollout-schedules",
        tags=["Rollout Schedules"],
    )
    router.include_router(metrics.router, prefix="/metrics", tags=["Metrics"])
    router.include_router(safety.router, prefix="/safety", tags=["Safety"])
    router.include_router(audit_logs.router, prefix="/audit-logs", tags=["Audit Logs"])
    # Add new utility endpoints
    router.include_router(sample_size_router, prefix="/utils", tags=["Utilities"])
    # EP-020: Data Export & Reporting
    router.include_router(export.router, prefix="/export", tags=["Export"])
    # P1-B: Advanced Toggle Features & Audit Logging
    router.include_router(bulk_toggle.router, prefix="", tags=["Advanced Toggle"])
    # P3-B: Scheduler Enhancements — health, history, and notifications
    router.include_router(
        scheduler_health.router, prefix="/scheduler", tags=["Scheduler Health"]
    )
    # P3-C: Audience Segmentation API
    router.include_router(segments.router, prefix="/segments", tags=["Segments"])
    # EP-022: Mutual Exclusion Groups & Global Holdout
    router.include_router(
        mutual_exclusion_groups.router,
        prefix="/mutual-exclusion-groups",
        tags=["Mutual Exclusion Groups"],
    )
    router.include_router(
        global_holdout.router, prefix="/holdout", tags=["Global Holdout"]
    )
    # Issue #22: Multi-Armed Bandit (MAB) endpoints
    router.include_router(bandit.router, prefix="/bandit", tags=["Bandit"])
    # Issue #25: Cross-Experiment Interaction Detection & Analysis
    router.include_router(
        interactions.router, prefix="/interactions", tags=["Interactions"]
    )
    # Issue #23: AI-Powered Experiment Design & Recommendations
    router.include_router(ai_design.router, prefix="/ai", tags=["AI Design"])
    # Issue #23: MCP (Model Context Protocol) server for AI agent access
    router.include_router(mcp.router, prefix="/mcp", tags=["MCP"])
    # Issue #27: guided experiment builder (draft-and-submit API; no dashboard UI)
    router.include_router(
        experiment_wizard.router, prefix="/wizard", tags=["Experiment Wizard"]
    )
    # EP-030: Notification preferences and delivery log
    router.include_router(
        notifications.router, prefix="/notifications", tags=["Notifications"]
    )
    # EP-033: Compliance Audit Logging
    router.include_router(compliance.router, prefix="/compliance", tags=["Compliance"])
    # EP-043: Post-Stratification & Benjamini-Hochberg FDR Correction
    router.include_router(
        post_stratification.router, prefix="/results", tags=["Results"]
    )
    # EP-046: LLM/AI Model Evaluation
    router.include_router(
        llm_experiments.router, prefix="/llm-experiments", tags=["LLM Experiments"]
    )
    router.include_router(
        llm_proxy.router, prefix="/llm-experiments", tags=["LLM Proxy"]
    )
    # EP-056: Pre-Experiment Power Calculator & MDE Estimator
    router.include_router(
        power_calculator.router, prefix="/power", tags=["Power Calculator"]
    )
    # #226: the flag ruleset for server-side local evaluation (needs sdk:ruleset)
    router.include_router(sdk.router, prefix="/sdk", tags=["SDK"])
    # EP-058: Real-time WebSocket Streaming Results
    router.include_router(
        websocket_results.router, prefix="", tags=["WebSocket Results"]
    )

    return router


def build_v1_router() -> APIRouter:
    """Build the v1 router: core routers, then anything the seam added.

    Module routers arrive only through the seam -- from ``modules.register
    (hooks)`` (see ``modules_loader``).  Nothing in this module names a module
    endpoint.

    Mounting goes through ``modules_loader.mount_module_routers`` rather than
    ``hooks.apply_routers`` directly, so that module code raising while it
    mounts is the loader's failure -- logged, recorded, refused at start-up
    outside development -- and not an exception out of ``import
    backend.app.api.api``, which used to take the whole API down after the
    load had already reported success.
    """
    load_modules()
    router = APIRouter()
    register_core_routers(router)
    mount_module_routers(router)
    return router


# Create API router for v1
api_router_v1 = build_v1_router()

# Main API router that includes versioned routers
api_router = APIRouter()
api_router.include_router(api_router_v1)

# Documentation configuration
#
# CORE_TAGS_METADATA describes the core API surface. The modules' tags
# arrive through hooks.register_tags(); `tags_metadata` below is the union
# and is what an OpenAPI document should be built from.
CORE_TAGS_METADATA = [
    {
        "name": "Authentication",
        "description": "Operations for user authentication, registration and token management",
    },
    {
        "name": "Power Calculator",
        "description": (
            "Pre-experiment statistical power analysis: sample size computation, "
            "MDE estimation, runtime estimation, power curves, and "
            "planning advice. No authentication required."
        ),
    },
    {
        "name": "API Keys",
        "description": (
            "User-owned API keys for SDK authentication (X-API-Key header). "
            "The plaintext key is returned once, at creation."
        ),
    },
    {
        "name": "Users",
        "description": "Operations for managing user accounts and profiles",
    },
    {
        "name": "Experiments",
        "description": "Operations for creating, managing and analyzing A/B tests and experiments",
    },
    {
        "name": "Tracking",
        "description": "Operations for tracking user interactions and experiment events",
    },
    {
        "name": "Feature Flags",
        "description": "Operations for managing feature flags and gradual rollouts",
    },
    {
        "name": "Assignments",
        "description": "Operations for managing user assignments to experiment variants",
    },
    {
        "name": "Events",
        "description": "Operations for querying and managing tracking events",
    },
    {
        "name": "Results",
        "description": "Operations for viewing results and analysis of experiments",
    },
    {
        "name": "Administration",
        "description": "Operations for system administration and management",
    },
    {
        "name": "Utilities",
        "description": "Utility operations for experiment design and planning",
    },
    {
        "name": "Rollout Schedules",
        "description": "Operations for managing gradual feature flag rollout schedules",
    },
    {
        "name": "Metrics",
        "description": "Operations for retrieving and analyzing feature flag metrics and performance data",
    },
    {
        "name": "Safety",
        "description": "Operations for monitoring feature flag safety and managing rollbacks",
    },
    {
        "name": "Audit Logs",
        "description": "Operations for querying audit logs and tracking system activity",
    },
    {
        "name": "Export",
        "description": "Operations for exporting experiment and feature flag data as CSV or JSON",
    },
    {
        "name": "Reports",
        "description": "Operations for generating platform and experiment summary reports",
    },
    {
        "name": "Scheduler Health",
        "description": "Operations for monitoring background scheduler health, run history, and webhook notifications",
    },
    {
        "name": "Segments",
        "description": "Operations for creating and managing audience segments and evaluating user membership",
    },
    {
        "name": "Mutual Exclusion Groups",
        "description": "Operations for managing mutual exclusion groups that prevent users from being in conflicting experiments",
    },
    {
        "name": "Global Holdout",
        "description": "Operations for managing global holdout configurations that reserve a percentage of users from all experiments",
    },
    {
        "name": "Interactions",
        "description": "Experiments that share users: the overlap between them, and (beta) whether one experiment's effect differs across the other's arms",
    },
    {
        "name": "AI Design",
        "description": "AI-powered experiment design assistant, results interpretation, sample size calculator, and experiment template library",
    },
    {
        "name": "MCP",
        "description": "Model Context Protocol server — exposes platform APIs to AI coding assistants and agents",
    },
    {
        "name": "Experiment Wizard",
        "description": "Step-by-step draft-and-submit flow for designing an experiment; submitting creates it",
    },
    {
        "name": "Compliance",
        "description": (
            "Compliance audit trail: append-only events for every experiment and "
            "feature-flag change, readable by ADMIN and ANALYST. HMAC signing, "
            "SOC 2 / ISO 27001 reports and the audit export are the compliance "
            "module's, on the same routes."
        ),
    },
]


def build_tags_metadata() -> list:
    """Core tag metadata plus whatever the seam contributed."""
    return CORE_TAGS_METADATA + hooks.extra_tags_metadata()


tags_metadata = build_tags_metadata()

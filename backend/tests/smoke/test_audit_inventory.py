"""Every core route that changes something is classified for the audit log (#221).

``INVENTORY`` names each POST, PUT, PATCH and DELETE route whose endpoint is
defined under ``backend.``, and says either which ``audit_logs`` actions it
writes (``Audited``) or why it writes none (``NotAudited``). The set of routes
is compared exactly, so a new mutating route fails
``test_the_inventory_is_exact`` until it is classified here, and a removed one
fails it until its line goes.

The module routes (``modules.``) have their own inventory. No mutating route
may live anywhere else: ``test_every_mutating_route_is_under_backend_or_modules``
keeps the ``backend.`` filter from dropping a route silently.

That each ``Audited`` route really writes its entry is pinned by the
integration tests in ``backend/tests/integration/api/test_audit_route_events.py``.
``WRITTEN_ACTION_TYPES`` is the union of the ``Audited`` sets here and of the
non-route sites (``test_audit_non_route_inventory.py``), checked below.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.routing import APIRoute

from backend.app.main import app
from backend.app.models.audit_log import ActionType as A
from backend.app.services.audit_service import WRITTEN_ACTION_TYPES
from backend.tests.smoke.test_audit_non_route_inventory import NON_ROUTE_ACTIONS

pytestmark = [pytest.mark.smoke]

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class Audited:
    actions: frozenset

    def __init__(self, *actions: A) -> None:
        object.__setattr__(self, "actions", frozenset(actions))


@dataclass(frozen=True)
class NotAudited:
    reason: str


SDK = NotAudited("SDK traffic with an API key, not a change made by a person")
CALCULATION = NotAudited("a calculation: it stores nothing")
READ_ONLY = NotAudited("a POST that only reads: it changes nothing")
LLM = NotAudited("LLM experiments are not on the audited event list")
DRAFT = NotAudited("a wizard draft is not an experiment until it is submitted")
COGNITO_ACCOUNT = NotAudited(
    "a Cognito account step (sign-up, confirmation, password reset, token "
    "refresh); not on the audited event list"
)
ROLLOUT = Audited(A.FEATURE_FLAG_UPDATE)
EXP_UPDATE = Audited(A.EXPERIMENT_UPDATE)
ACCESS = Audited(A.ROLE_ASSIGN, A.USER_ACTIVATE, A.USER_DEACTIVATE)
TOGGLE = Audited(A.TOGGLE_ENABLE, A.TOGGLE_DISABLE)

V1 = "/api/v1"

INVENTORY = {
    # --- admin ---------------------------------------------------------------
    ("POST", f"{V1}/admin/cache/clear"): NotAudited(
        "clears the Redis cache; no stored record changes"
    ),
    ("DELETE", f"{V1}/admin/users/{{user_id}}"): Audited(A.USER_DELETE),
    ("PATCH", f"{V1}/admin/users/{{user_id}}"): ACCESS,
    ("PUT", f"{V1}/admin/users/{{user_id}}"): ACCESS,
    # --- AI design helpers ---------------------------------------------------
    ("POST", f"{V1}/ai/design"): CALCULATION,
    ("POST", f"{V1}/ai/interpret/{{experiment_id}}"): CALCULATION,
    # --- API keys ------------------------------------------------------------
    ("POST", f"{V1}/api-keys"): Audited(A.API_KEY_CREATE),
    ("DELETE", f"{V1}/api-keys/{{key_id}}"): Audited(A.API_KEY_REVOKE),
    # --- auth ----------------------------------------------------------------
    # The Hosted UI's code exchange: a sign-in, recorded as POST /auth/token
    # records one under Cognito, when the identity already has an account.
    ("POST", f"{V1}/auth/cognito/exchange"): Audited(A.USER_LOGIN),
    ("POST", f"{V1}/auth/confirm"): COGNITO_ACCOUNT,
    ("POST", f"{V1}/auth/forgot-password"): COGNITO_ACCOUNT,
    ("POST", f"{V1}/auth/login"): Audited(A.USER_LOGIN),
    ("POST", f"{V1}/auth/logout"): NotAudited(
        "signing out is not on the audited event list: under local it changes "
        "nothing on the server (the tokens are stateless); under Cognito it "
        "asks Cognito to revoke the user's tokens, which changes no stored row"
    ),
    ("POST", f"{V1}/auth/refresh"): COGNITO_ACCOUNT,
    ("POST", f"{V1}/auth/reset-password"): COGNITO_ACCOUNT,
    ("POST", f"{V1}/auth/signup"): COGNITO_ACCOUNT,
    # Local, and Cognito when the identity already has an account.
    ("POST", f"{V1}/auth/token"): Audited(A.USER_LOGIN),
    # --- bandits -------------------------------------------------------------
    ("POST", f"{V1}/bandit/{{experiment_id}}/update"): NotAudited(
        "recomputes the bandit's weights from the data; no person chooses them"
    ),
    ("PUT", f"{V1}/bandit/{{experiment_id}}/weights"): EXP_UPDATE,
    # --- feature flags -------------------------------------------------------
    ("POST", f"{V1}/feature-flags/bulk-toggle"): Audited(
        A.TOGGLE_ENABLE, A.TOGGLE_DISABLE, A.FEATURE_FLAG_UPDATE
    ),
    ("POST", f"{V1}/feature-flags"): Audited(A.FEATURE_FLAG_CREATE),
    ("POST", f"{V1}/feature-flags/"): Audited(A.FEATURE_FLAG_CREATE),
    ("POST", f"{V1}/feature-flags/evaluate/{{flag_key}}"): SDK,
    ("DELETE", f"{V1}/feature-flags/{{flag_id}}"): Audited(A.FEATURE_FLAG_DELETE),
    ("PUT", f"{V1}/feature-flags/{{flag_id}}"): Audited(A.FEATURE_FLAG_UPDATE),
    ("POST", f"{V1}/feature-flags/{{flag_id}}/activate"): Audited(
        A.FEATURE_FLAG_ACTIVATE
    ),
    ("POST", f"{V1}/feature-flags/{{flag_id}}/deactivate"): Audited(
        A.FEATURE_FLAG_DEACTIVATE
    ),
    ("POST", f"{V1}/feature-flags/{{flag_id}}/disable"): Audited(A.TOGGLE_DISABLE),
    ("POST", f"{V1}/feature-flags/{{flag_id}}/enable"): Audited(A.TOGGLE_ENABLE),
    ("POST", f"{V1}/feature-flags/{{flag_id}}/toggle"): TOGGLE,
    ("POST", f"{V1}/feature-flags/{{flag_id}}/unarchive"): Audited(
        A.FEATURE_FLAG_UPDATE
    ),
    # --- rollout schedules: an entry on the schedule's flag ------------------
    ("POST", f"{V1}/rollout-schedules/"): ROLLOUT,
    ("PUT", f"{V1}/rollout-schedules/{{schedule_id}}"): ROLLOUT,
    ("DELETE", f"{V1}/rollout-schedules/{{schedule_id}}"): ROLLOUT,
    ("POST", f"{V1}/rollout-schedules/{{schedule_id}}/activate"): ROLLOUT,
    ("POST", f"{V1}/rollout-schedules/{{schedule_id}}/pause"): ROLLOUT,
    ("POST", f"{V1}/rollout-schedules/{{schedule_id}}/cancel"): ROLLOUT,
    ("POST", f"{V1}/rollout-schedules/{{schedule_id}}/stages"): ROLLOUT,
    ("PUT", f"{V1}/rollout-schedules/stages/{{stage_id}}"): ROLLOUT,
    ("DELETE", f"{V1}/rollout-schedules/stages/{{stage_id}}"): ROLLOUT,
    ("POST", f"{V1}/rollout-schedules/stages/{{stage_id}}/advance"): ROLLOUT,
    # --- safety --------------------------------------------------------------
    ("POST", f"{V1}/safety/settings"): NotAudited(
        "safety settings are not on the audited event list"
    ),
    ("POST", f"{V1}/safety/feature-flags/{{feature_flag_id}}/config"): NotAudited(
        "a flag's safety config is not on the audited event list"
    ),
    ("POST", f"{V1}/safety/feature-flags/{{feature_flag_id}}/rollback"): Audited(
        A.SAFETY_ROLLBACK
    ),
    # --- experiments ---------------------------------------------------------
    ("POST", f"{V1}/experiments/"): Audited(A.EXPERIMENT_CREATE),
    ("POST", f"{V1}/experiments/schedules/process"): NotAudited(
        "runs the experiment scheduler, a non-route site that writes its "
        "own entries as system:experiment-scheduler"
    ),
    ("DELETE", f"{V1}/experiments/{{experiment_id}}"): Audited(A.EXPERIMENT_DELETE),
    ("PUT", f"{V1}/experiments/{{experiment_id}}"): EXP_UPDATE,
    ("POST", f"{V1}/experiments/{{experiment_id}}/archive"): EXP_UPDATE,
    ("POST", f"{V1}/experiments/{{experiment_id}}/clone"): Audited(A.EXPERIMENT_CREATE),
    ("POST", f"{V1}/experiments/{{experiment_id}}/complete"): Audited(
        A.EXPERIMENT_COMPLETE
    ),
    ("POST", f"{V1}/experiments/{{experiment_id}}/metadata"): EXP_UPDATE,
    ("POST", f"{V1}/experiments/{{experiment_id}}/pause"): Audited(A.EXPERIMENT_PAUSE),
    ("PUT", f"{V1}/experiments/{{experiment_id}}/schedule"): EXP_UPDATE,
    ("POST", f"{V1}/experiments/{{experiment_id}}/start"): Audited(A.EXPERIMENT_START),
    # #1545: a visual experiment's preview links.
    ("POST", f"{V1}/experiments/{{experiment_id}}/visual/preview-links"): Audited(
        A.EXPERIMENT_PREVIEW_LINK_CREATE
    ),
    ("DELETE", f"{V1}/experiments/{{experiment_id}}/visual/preview-links"): Audited(
        A.EXPERIMENT_PREVIEW_LINKS_REVOKE
    ),
    # --- experiment wizard ---------------------------------------------------
    ("POST", f"{V1}/wizard/drafts"): DRAFT,
    ("PUT", f"{V1}/wizard/drafts/{{draft_id}}/step"): DRAFT,
    ("POST", f"{V1}/wizard/drafts/{{draft_id}}/submit"): Audited(A.EXPERIMENT_CREATE),
    ("POST", f"{V1}/wizard/validate"): DRAFT,
    # --- holdouts ------------------------------------------------------------
    # is_active true also ends the active holdout: one deactivate for it.
    ("POST", f"{V1}/holdout"): Audited(A.HOLDOUT_CREATE, A.HOLDOUT_DEACTIVATE),
    ("PUT", f"{V1}/holdout/{{holdout_id}}"): Audited(
        A.HOLDOUT_UPDATE, A.HOLDOUT_ACTIVATE, A.HOLDOUT_DEACTIVATE
    ),
    # --- mutual exclusion groups ---------------------------------------------
    ("POST", f"{V1}/mutual-exclusion-groups"): Audited(A.MUTUAL_EXCLUSION_GROUP_CREATE),
    ("PUT", f"{V1}/mutual-exclusion-groups/{{group_id}}"): Audited(
        A.MUTUAL_EXCLUSION_GROUP_UPDATE
    ),
    ("DELETE", f"{V1}/mutual-exclusion-groups/{{group_id}}"): Audited(
        A.MUTUAL_EXCLUSION_GROUP_ARCHIVE
    ),
    ("POST", f"{V1}/mutual-exclusion-groups/{{group_id}}/experiments"): Audited(
        A.MUTUAL_EXCLUSION_GROUP_UPDATE
    ),
    (
        "DELETE",
        f"{V1}/mutual-exclusion-groups/{{group_id}}/experiments/{{experiment_id}}",
    ): Audited(A.MUTUAL_EXCLUSION_GROUP_UPDATE),
    # --- segments ------------------------------------------------------------
    ("POST", f"{V1}/segments"): Audited(A.SEGMENT_CREATE),
    ("PUT", f"{V1}/segments/{{segment_id}}"): Audited(A.SEGMENT_UPDATE),
    ("DELETE", f"{V1}/segments/{{segment_id}}"): Audited(A.SEGMENT_ARCHIVE),
    # #440: an update with reason "members added"/"members removed" and the
    # counts only, never the ids (#221 EM C8).
    ("POST", f"{V1}/segments/{{segment_id}}/members"): Audited(A.SEGMENT_UPDATE),
    ("POST", f"{V1}/segments/{{segment_id}}/members/remove"): Audited(A.SEGMENT_UPDATE),
    ("POST", f"{V1}/segments/bulk-evaluate"): READ_ONLY,
    ("POST", f"{V1}/segments/{{segment_id}}/evaluate"): READ_ONLY,
    ("POST", f"{V1}/segments/{{segment_id}}/preview"): READ_ONLY,
    # --- users ---------------------------------------------------------------
    ("POST", f"{V1}/users/"): Audited(A.USER_CREATE),
    ("POST", f"{V1}/users/me/password"): NotAudited(
        "a user changing their own password is not on the audited event list"
    ),
    ("PUT", f"{V1}/users/{{user_id}}"): ACCESS,
    ("DELETE", f"{V1}/users/{{user_id}}"): Audited(A.USER_DELETE),
    # --- LLM experiments -----------------------------------------------------
    ("POST", f"{V1}/llm-experiments/"): LLM,
    ("PUT", f"{V1}/llm-experiments/{{experiment_id}}"): LLM,
    ("POST", f"{V1}/llm-experiments/{{experiment_id}}/pause"): LLM,
    ("POST", f"{V1}/llm-experiments/{{experiment_id}}/start"): LLM,
    ("POST", f"{V1}/llm-experiments/{{experiment_id}}/variants"): LLM,
    ("PUT", f"{V1}/llm-experiments/{{experiment_id}}/variants/{{variant_id}}"): LLM,
    ("POST", f"{V1}/llm-experiments/{{experiment_id}}/complete"): LLM,
    ("POST", f"{V1}/llm-experiments/{{experiment_id}}/evaluate"): LLM,
    ("POST", f"{V1}/llm-experiments/{{experiment_id}}/judge"): LLM,
    # --- notifications -------------------------------------------------------
    ("PUT", f"{V1}/notifications/preferences"): NotAudited(
        "a user's own notification preferences"
    ),
    ("POST", f"{V1}/notifications/test"): NotAudited("sends a test notification"),
    ("POST", f"{V1}/scheduler/notify/test"): NotAudited("sends a test notification"),
    # --- results and calculators --------------------------------------------
    ("POST", f"{V1}/metrics/aggregate"): NotAudited(
        "recomputes metric aggregates, which are derived data"
    ),
    ("POST", f"{V1}/results/{{experiment_id}}/fdr-correction"): CALCULATION,
    ("POST", f"{V1}/results/{{experiment_id}}/post-stratification"): CALCULATION,
    ("POST", f"{V1}/results/{{experiment_id}}/invalidate-cache"): NotAudited(
        "clears cached results; no stored record changes"
    ),
    ("POST", f"{V1}/power/mde"): CALCULATION,
    ("POST", f"{V1}/power/plan"): CALCULATION,
    ("POST", f"{V1}/power/runtime"): CALCULATION,
    ("POST", f"{V1}/power/sample-size"): CALCULATION,
    ("POST", f"{V1}/utils/utils/sample-size"): CALCULATION,
    # --- tracking (SDKs) -----------------------------------------------------
    ("POST", f"{V1}/tracking/assign"): SDK,
    ("POST", f"{V1}/tracking/assign/batch"): NotAudited(
        "assignments are not audit events (D49)"
    ),
    ("POST", f"{V1}/tracking/batch"): SDK,
    ("POST", f"{V1}/tracking/events"): SDK,
    ("POST", f"{V1}/tracking/track"): SDK,
    ("POST", f"{V1}/tracking/errors"): SDK,
    ("POST", f"{V1}/tracking/errors/batch"): SDK,
    ("POST", f"{V1}/tracking/evaluations"): SDK,
    # #1545: the visual editor's browser script, with a key in the page.
    ("POST", f"{V1}/tracking/visual"): SDK,
    ("POST", f"{V1}/tracking/visual/preview"): READ_ONLY,
}

#: GET routes a reader could expect to be recorded, and why they are not.
#: Only the export is listed: the audit log records changes, not reads.
#: ``test_audit_export.py`` checks that an export writes no entry.
READ_ROUTES = {
    ("GET", f"{V1}/audit-logs/export"): NotAudited("reading, not changing"),
}


def _contexts(application):
    """(method, path, route context) for every HTTP route, flattened.

    ``include_router`` entries are lazy (FastAPI >= 0.141), so each is read
    through ``effective_route_contexts``; a route declared on the app itself
    is a plain ``APIRoute`` and is its own context.
    """
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _mutating(application):
    return [
        (method, path, ctx.endpoint.__module__)
        for method, path, ctx in _contexts(application)
        if method in MUTATING
    ]


def _core_mutating_routes(application):
    return {
        (method, path)
        for method, path, module in _mutating(application)
        if module.startswith("backend.")
    }


def test_the_inventory_is_exact():
    found = _core_mutating_routes(app)
    assert found == set(INVENTORY), (
        f"unclassified: {sorted(found - set(INVENTORY))}; "
        f"gone: {sorted(set(INVENTORY) - found)}"
    )
    assert len(INVENTORY) == 111


def test_every_mutating_route_is_under_backend_or_modules():
    """A route defined anywhere else would be dropped by both inventories."""
    stray = sorted(
        (method, path, module)
        for method, path, module in _mutating(app)
        if not module.startswith(("backend.", "modules."))
    )
    assert stray == []


@pytest.mark.parametrize("key", sorted(INVENTORY), ids=lambda k: f"{k[0]} {k[1]}")
def test_each_entry_is_well_formed(key):
    entry = INVENTORY[key]
    if isinstance(entry, Audited):
        assert entry.actions, "Audited() names at least one action"
        assert all(isinstance(a, A) for a in entry.actions)
    else:
        assert isinstance(entry, NotAudited)
        assert entry.reason.strip(), "NotAudited needs a reason"


def test_written_action_types_are_what_the_inventory_writes():
    audited = set(NON_ROUTE_ACTIONS)
    for entry in INVENTORY.values():
        if isinstance(entry, Audited):
            audited |= entry.actions
    assert set(WRITTEN_ACTION_TYPES) == audited, (
        f"written but not in the inventory: "
        f"{sorted(a.value for a in set(WRITTEN_ACTION_TYPES) - audited)}; "
        f"in the inventory but not written: "
        f"{sorted(a.value for a in audited - set(WRITTEN_ACTION_TYPES))}"
    )


@pytest.mark.parametrize("key", sorted(READ_ROUTES), ids=lambda k: f"{k[0]} {k[1]}")
def test_each_read_route_exists_and_is_not_audited(key):
    found = {
        (method, path)
        for method, path, ctx in _contexts(app)
        if ctx.endpoint.__module__.startswith("backend.")
    }
    assert key in found, f"{key} is not a core route"
    assert key[0] == "GET"
    assert isinstance(READ_ROUTES[key], NotAudited)
    assert READ_ROUTES[key].reason.strip()

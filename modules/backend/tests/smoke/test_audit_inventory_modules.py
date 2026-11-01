"""Every module route that changes something is classified for the audit log (#221).

The twin of ``backend/tests/smoke/test_audit_inventory.py``, for the routes
whose endpoint is defined under ``modules.``. ``INVENTORY`` names each POST,
PUT, PATCH and DELETE route and says either which ``audit_logs`` actions it
writes (``Audited``) or why it writes none (``NotAudited``). The set of routes
is compared exactly, so a new mutating module route fails
``test_the_inventory_is_exact`` until it is classified here, and a removed
one fails it until its line goes.

``SIGN_IN_GET_ROUTES`` lists the one GET route that writes an entry: the
OIDC callback, when it answers with a token (a sign-in started with
``/oidc/{provider}/login``).

What the modules write, and core does not, is ``MODULES_ONLY_ACTION_TYPES``;
``test_modules_only_action_types_are_what_the_modules_add`` pins it, so the
dashboard's list (``action-types.json``) stays right in both profiles. That
each ``Audited`` route really writes its entry is pinned by
``modules/backend/tests/integration/api/test_audit_module_events.py``.
"""

from __future__ import annotations

import pytest

from backend.app.main import app
from backend.app.models.audit_log import ActionType as A
from backend.app.services.audit_service import (
    MODULES_ONLY_ACTION_TYPES,
    WRITTEN_ACTION_TYPES,
)
from backend.tests.smoke.test_audit_inventory import (
    MUTATING,
    Audited,
    NotAudited,
    _contexts,
)
from modules.backend.tests.smoke.test_audit_non_route_inventory_modules import (
    NON_ROUTE_ACTIONS,
)

pytestmark = [pytest.mark.smoke]

V1 = "/api/v1"

SSO_CONFIG = NotAudited("SSO configuration is not on the audited event list")
WORKSPACE = NotAudited("workspaces are not on the audited event list")
MEMBERSHIP = NotAudited(
    "workspace membership is not on the audited event list; a member's role "
    "change is (PUT /workspaces/{workspace_id}/members/{user_id})"
)
INTEGRATION = NotAudited("integration settings are not on the audited event list")
WEBHOOK = NotAudited("an inbound webhook from another system, not a change by a person")
WAREHOUSE = NotAudited("warehouse analysis is not on the audited event list")
CHECK = NotAudited("checks a connection or a source; stores nothing")
COUNTERS = NotAudited(
    "real-time counters are derived data, not on the audited event list"
)
ETL = NotAudited("starts an ETL job over derived data; not on the audited event list")
HIPAA = NotAudited(
    "HIPAA records are kept in the modules' own PHI audit log, not this one"
)

INVENTORY = {
    # --- SSO ----------------------------------------------------------------
    ("POST", f"{V1}/auth/sso/configs"): SSO_CONFIG,
    ("PUT", f"{V1}/auth/sso/configs/{{config_id}}"): SSO_CONFIG,
    ("DELETE", f"{V1}/auth/sso/configs/{{config_id}}"): SSO_CONFIG,
    # The sign-in. An account created or a role changed on the way writes its
    # own entry (provision_user, in the non-route inventory).
    ("POST", f"{V1}/auth/sso/exchange"): Audited(A.USER_LOGIN),
    ("POST", f"{V1}/auth/sso/saml/{{config_id}}/acs"): Audited(A.USER_LOGIN),
    # --- custom roles (rbac) ------------------------------------------------
    # Defining a role, assigning one and granting directly are each recorded
    # (#891); test_audit_module_events.py pins each entry.
    ("POST", f"{V1}/rbac/roles"): Audited(A.ROLE_CREATE),
    ("PUT", f"{V1}/rbac/roles/{{role_name}}"): Audited(A.ROLE_UPDATE),
    ("DELETE", f"{V1}/rbac/roles/{{role_name}}"): Audited(A.ROLE_DELETE),
    ("POST", f"{V1}/rbac/roles/assign"): Audited(A.ROLE_ASSIGN),
    ("POST", f"{V1}/rbac/roles/revoke"): Audited(A.ROLE_UNASSIGN),
    ("POST", f"{V1}/rbac/users/{{user_id}}/grant"): Audited(A.PERMISSION_GRANT),
    ("DELETE", f"{V1}/rbac/users/{{user_id}}/grant/{{resource}}"): Audited(
        A.PERMISSION_REVOKE
    ),
    # --- workspaces ---------------------------------------------------------
    ("POST", f"{V1}/workspaces/"): WORKSPACE,
    ("PUT", f"{V1}/workspaces/{{workspace_id}}"): WORKSPACE,
    ("DELETE", f"{V1}/workspaces/{{workspace_id}}"): WORKSPACE,
    ("POST", f"{V1}/workspaces/{{workspace_id}}/members"): MEMBERSHIP,
    ("PUT", f"{V1}/workspaces/{{workspace_id}}/members/{{user_id}}"): Audited(
        A.ROLE_ASSIGN
    ),
    ("DELETE", f"{V1}/workspaces/{{workspace_id}}/members/{{user_id}}"): MEMBERSHIP,
    ("POST", f"{V1}/workspaces/{{workspace_id}}/invites"): MEMBERSHIP,
    ("POST", f"{V1}/workspaces/invites/{{token}}/accept"): MEMBERSHIP,
    # --- integrations -------------------------------------------------------
    ("POST", f"{V1}/integrations"): INTEGRATION,
    ("PUT", f"{V1}/integrations/{{integration_type}}"): INTEGRATION,
    ("DELETE", f"{V1}/integrations/{{integration_type}}"): INTEGRATION,
    ("POST", f"{V1}/integrations/webhooks/github"): WEBHOOK,
    ("POST", f"{V1}/integrations/webhooks/jira"): WEBHOOK,
    ("POST", f"{V1}/integrations/webhooks/salesforce"): WEBHOOK,
    # --- warehouse analysis -------------------------------------------------
    ("POST", f"{V1}/warehouse/analysis/connections"): WAREHOUSE,
    ("PUT", f"{V1}/warehouse/analysis/connections/{{connection_id}}"): WAREHOUSE,
    ("DELETE", f"{V1}/warehouse/analysis/connections/{{connection_id}}"): WAREHOUSE,
    (
        "POST",
        f"{V1}/warehouse/analysis/connections/{{connection_id}}/regenerate-key",
    ): WAREHOUSE,
    ("POST", f"{V1}/warehouse/analysis/connections/test"): CHECK,
    ("POST", f"{V1}/warehouse/analysis/connections/{{connection_id}}/test"): CHECK,
    ("POST", f"{V1}/warehouse/analysis/experiments/{{experiment_id}}/runs"): WAREHOUSE,
    ("POST", f"{V1}/warehouse/analysis/sources"): WAREHOUSE,
    ("PUT", f"{V1}/warehouse/analysis/sources/{{source_id}}"): WAREHOUSE,
    ("DELETE", f"{V1}/warehouse/analysis/sources/{{source_id}}"): WAREHOUSE,
    ("POST", f"{V1}/warehouse/analysis/sources/{{source_id}}/preview"): CHECK,
    ("POST", f"{V1}/warehouse/analysis/sources/{{source_id}}/validate"): CHECK,
    # --- real-time counters and ETL -----------------------------------------
    ("POST", f"{V1}/counters/bulk"): COUNTERS,
    ("POST", f"{V1}/counters/{{experiment_id}}/increment"): COUNTERS,
    ("POST", f"{V1}/counters/{{experiment_id}}/reset"): COUNTERS,
    ("POST", f"{V1}/etl/crawler/run"): ETL,
    ("POST", f"{V1}/etl/jobs/run"): ETL,
    ("POST", f"{V1}/etl/partitions/add"): ETL,
    # --- HIPAA --------------------------------------------------------------
    ("POST", f"{V1}/hipaa/baa"): HIPAA,
    ("DELETE", f"{V1}/hipaa/baa/{{baa_id}}"): HIPAA,
    ("POST", f"{V1}/hipaa/audit-logs"): HIPAA,
    ("POST", f"{V1}/hipaa/encrypt"): NotAudited(
        "encrypts the value it is given; stores nothing"
    ),
    ("POST", f"{V1}/hipaa/decrypt"): HIPAA,
}

#: GET routes that write an entry. The callback signs a user in when it
#: answers with a token; when it hands the dashboard a code instead, it
#: writes nothing and ``POST /auth/sso/exchange`` records the sign-in.
SIGN_IN_GET_ROUTES = {
    ("GET", f"{V1}/auth/sso/oidc/{{provider}}/callback"): Audited(A.USER_LOGIN),
}


def _module_routes(application, methods):
    return {
        (method, ctx.path)
        for method, _path, ctx in _contexts(application)
        if method in methods and ctx.endpoint.__module__.startswith("modules.")
    }


def test_the_inventory_is_exact():
    found = _module_routes(app, MUTATING)
    assert found == set(INVENTORY), (
        f"unclassified: {sorted(found - set(INVENTORY))}; "
        f"gone: {sorted(set(INVENTORY) - found)}"
    )
    assert len(INVENTORY) == 49
    audited = [k for k, e in INVENTORY.items() if isinstance(e, Audited)]
    assert len(audited) == 10


@pytest.mark.parametrize("key", sorted(INVENTORY), ids=lambda k: f"{k[0]} {k[1]}")
def test_each_entry_is_well_formed(key):
    entry = INVENTORY[key]
    if isinstance(entry, Audited):
        assert entry.actions, "Audited() names at least one action"
        assert all(isinstance(a, A) for a in entry.actions)
    else:
        assert isinstance(entry, NotAudited)
        assert entry.reason.strip(), "NotAudited needs a reason"


@pytest.mark.parametrize(
    "key", sorted(SIGN_IN_GET_ROUTES), ids=lambda k: f"{k[0]} {k[1]}"
)
def test_each_sign_in_get_route_exists(key):
    assert key in _module_routes(app, {"GET"}), f"{key} is not a module route"
    assert isinstance(SIGN_IN_GET_ROUTES[key], Audited)


def _module_actions():
    actions = set(NON_ROUTE_ACTIONS)
    for entry in [*INVENTORY.values(), *SIGN_IN_GET_ROUTES.values()]:
        if isinstance(entry, Audited):
            actions |= entry.actions
    return actions


def test_modules_only_action_types_are_what_the_modules_add():
    """Exactly what the modules write that core does not."""
    added = _module_actions() - set(WRITTEN_ACTION_TYPES)
    assert set(MODULES_ONLY_ACTION_TYPES) == added, (
        f"written by the modules only, but not in MODULES_ONLY_ACTION_TYPES: "
        f"{sorted(a.value for a in added - set(MODULES_ONLY_ACTION_TYPES))}; "
        f"in MODULES_ONLY_ACTION_TYPES but not written by the modules: "
        f"{sorted(a.value for a in set(MODULES_ONLY_ACTION_TYPES) - added)}"
    )
    assert {a.value for a in MODULES_ONLY_ACTION_TYPES} == {
        "role_unassign",
        "role_create",
        "role_update",
        "role_delete",
        "permission_grant",
        "permission_revoke",
    }

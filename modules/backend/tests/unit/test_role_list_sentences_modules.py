"""Every role-list sentence under ``modules/`` is classified (#891, check #54, the twin).

``backend/tests/unit/docs/test_role_list_sentences.py`` holds the docs,
``frontend/src`` and ``backend/app``; a core file may not name a module's
path, so ``modules/frontend/src`` and ``modules/backend/app`` are held here,
by the same scan and the same markers.
"""

from __future__ import annotations

from typing import Dict, Tuple

import pytest

from backend.tests.unit.docs.test_role_list_sentences import scan

pytestmark = [pytest.mark.unit]

TREES = ("modules/frontend/src", "modules/backend/app")

_ADMIN = "module administration stays with the built-in role"
_WAREHOUSE = "the warehouse analysis role bands read the built-in role"

#: Class B under modules/: (file, sentence), whitespace collapsed.
_SPLIT = "the split-URL preview stays with the built-in role"
CLASS_B: Dict[Tuple[str, str], str] = {
    (
        "modules/backend/app/api/v1/endpoints/etl.py",
        'Only ADMIN or DEVELOPER users can perform this action."',
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/etl.py",
        'Raise 403 if the user is not ADMIN or DEVELOPER."',
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/etl.py",
        'Requires ADMIN or DEVELOPER role."',
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/experiments_split_url.py",
        "# A superuser, or the ADMIN or DEVELOPER role: the same rule as the core # route that calls this handler (experiments.preview_split_url_assignment).",
    ): _SPLIT,
    (
        "modules/backend/app/api/v1/endpoints/experiments_split_url.py",
        'Only DEVELOPER or ADMIN users can access the split URL preview endpoint"',
    ): _SPLIT,
    (
        "modules/backend/app/api/v1/endpoints/experiments_split_url.py",
        'Returns: Dict containing experiment_id, user_id, variant_name, url and traffic_allocation Raises: HTTPException 403: If user does not have DEVELOPER or ADMIN role HTTPException 404: If experiment not found HTTPException 400: If experiment is not a split_url type or has no config HTTPException 409: If the stored config breaks the rules (one saved before a rule, say); it is answered, not served "',
    ): _SPLIT,
    (
        "modules/backend/app/api/v1/endpoints/experiments_split_url.py",
        "This endpoint requires DEVELOPER or ADMIN role.",
    ): _SPLIT,
    (
        "modules/backend/app/api/v1/endpoints/hipaa.py",
        "**Access**: ADMIN or DEVELOPER.",
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/hipaa.py",
        'Raise 403 if the current user does not have ADMIN or DEVELOPER role."',
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/hipaa.py",
        'This endpoint requires ADMIN or DEVELOPER role"',
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/integrations.py",
        'ADMIN or DEVELOPER role required"',
    ): _ADMIN,
    (
        "modules/backend/app/api/v1/endpoints/integrations.py",
        "Permission model: - GET /integrations, GET /integrations/{type} → ADMIN or DEVELOPER - POST / PUT / DELETE → ADMIN only - Webhook endpoints → no platform user; the *sender* is authenticated against the integration's own ``webhook_secret`` before the payload is read (see ``services.integrations.webhook_auth``).",
    ): _ADMIN,
    (
        "modules/frontend/src/components/admin/roles/PermissionCheckboxGrid.tsx",
        "Without it, Developer and Viewer users read only their own entries.",
    ): "class A in context: the audit_log (read) description, whose first sentence is the grant",
    (
        "modules/frontend/src/components/warehouse/runs/StartRunForm.tsx",
        "A developer or admin defines one in Warehouse › Sources.",
    ): _WAREHOUSE,
    (
        "modules/frontend/src/components/warehouse/runs/StartRunForm.tsx",
        "ADMIN and DEVELOPER * only — the caller renders this only for them.",
    ): _WAREHOUSE,
    (
        "modules/frontend/src/components/warehouse/runs/StartRunForm.tsx",
        "An analyst, developer or admin defines one in Warehouse › Sources.",
    ): _WAREHOUSE,
    (
        "modules/frontend/src/components/warehouse/runs/WarehouseAnalysisSection.tsx",
        "* * Roles (the API's matrix): ADMIN, DEVELOPER and ANALYST read runs, their * results and the SQL they sent; ADMIN and DEVELOPER start runs.",
    ): _WAREHOUSE,
    (
        "modules/frontend/src/components/warehouse/runs/WarehouseAnalysisSection.tsx",
        "> Starting a warehouse analysis requires the ADMIN or DEVELOPER role; you are{'",
    ): _WAREHOUSE,
    (
        "modules/frontend/src/services/warehouseRuns.ts",
        "* ADMIN and DEVELOPER start runs.",
    ): _WAREHOUSE,
}


def test_every_role_list_sentence_under_modules_is_classified():
    hits = scan(TREES)
    unclassified = sorted(hits - set(CLASS_B))
    assert unclassified == [], (
        "a sentence lists the roles allowed to do something and names no custom "
        "role or grant; rewrite it (the #891 replacement rule) or, if no grant "
        f"can change the decision, add it to CLASS_B: {unclassified}"
    )
    gone = sorted(set(CLASS_B) - hits)
    assert gone == [], f"CLASS_B entries no longer in the tree: {gone}"

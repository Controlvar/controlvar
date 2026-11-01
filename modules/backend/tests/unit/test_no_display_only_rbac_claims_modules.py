"""The sentences under ``modules/`` that say custom roles and grants change nothing.

The twin of ``backend/tests/unit/docs/test_no_display_only_rbac_claims.py``
(#891), which holds the rest of the tree: a core file may not name a module's
path, so the sentences in the modules' own files are listed here, found by the
same scan. A sentence removed or reworded early, or a new one added, fails
``test_the_display_only_sentences_under_modules_are_exactly_these`` and names
it. The intent is that when roles and grants take effect every one of them
goes, and the set is empty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

from backend.tests.unit.docs.test_no_display_only_rbac_claims import (
    REPO,
    Hit,
    differences,
    scan,
)

pytestmark = [pytest.mark.unit]

#: The sentences under modules/ now. Not one of them may move without this.
EXPECTED: Dict[Hit, int] = {
    (
        "modules/backend/tests/unit/services/test_rbac_layers_are_display_only.py",
        "Custom roles and direct grants are stored and shown, and decide nothing (#1142, #891).",
    ): 1,
    (
        "modules/frontend/src/pages/admin/roles.tsx",
        "Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides.\"",
    ): 2,
    (
        "modules/frontend/src/pages/admin/roles.tsx",
        "Custom roles and direct permission grants are recorded, but they do not change what anyone can do yet: each user's built-in role decides.\"",
    ): 2,
    (
        "modules/frontend/src/pages/admin/roles.tsx",
        "No permission check reads custom roles or direct grants yet (#891).",
    ): 1,
    (
        "modules/frontend/src/tests/pages/admin-roles.test.tsx",
        "* * /admin/roles says that custom roles and direct permission grants do not * change what a user can do yet (#891): no permission check reads them, and * each user's built-in role decides.",
    ): 2,
    (
        "modules/frontend/src/tests/pages/admin-roles.test.tsx",
        "Custom roles and direct permission grants are recorded, but they do not change what anyone can do yet: each user's built-in role decides.\"",
    ): 2,
    (
        "modules/frontend/src/tests/pages/admin-roles.test.tsx",
        "says that custom roles and direct grants are recorded and do not change access yet'",
    ): 1,
}


def test_the_display_only_sentences_under_modules_are_exactly_these():
    found = scan(REPO, modules=True, skip=Path(__file__))
    assert not differences(found, EXPECTED), differences(found, EXPECTED)

"""The sentences under ``modules/`` that say custom roles and grants change nothing.

The twin of ``backend/tests/unit/docs/test_no_display_only_rbac_claims.py``
(#891), which holds the rest of the tree: a core file may not name a module's
path, so the sentences in the modules' own files are listed here, found by the
same scan. Roles and grants take effect now, so the set is empty: a sentence
that comes back fails
``test_the_display_only_sentences_under_modules_are_exactly_these`` and names
it.
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

#: The sentences in the tree now: none. Custom roles and direct grants take
#: effect (#891), so a sentence saying they change nothing is false; a new
#: one fails here.
EXPECTED: Dict[Hit, int] = {}


def test_the_display_only_sentences_under_modules_are_exactly_these():
    found = scan(REPO, modules=True, skip=Path(__file__))
    assert not differences(found, EXPECTED), differences(found, EXPECTED)

# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The published description of ``is_enforced`` matches what it does (#504).

The field was described as "Block password login when True", but nothing reads
it: password sign-in is never blocked. The description now says so, and the
second test pins the premise, so whoever makes the field do something has to
change the description in the same change.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from modules.backend.app.api.v1.endpoints.sso import SSOConfigCreate

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
APP_ROOTS = ("backend/app", "modules/backend/app")


@pytest.mark.regression
def test_the_description_does_not_promise_blocking():
    description = SSOConfigCreate.model_fields["is_enforced"].description
    assert "no effect" in description, description
    assert not description.lower().startswith("block"), description


def test_no_application_code_reads_is_enforced():
    """Every ``x.is_enforced`` in application code, outside migrations. None
    exists today; one appearing means the field now does something and the
    description above (and docs/auth/sso.md) must say what."""
    reads = []
    scanned = 0
    for root in APP_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "migrations" in path.parts:
                continue
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "is_enforced":
                    reads.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert scanned > 100, scanned  # a wrong root cannot turn this into a pass
    assert reads == [], reads

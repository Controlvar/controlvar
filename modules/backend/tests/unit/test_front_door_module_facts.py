"""What the front door says about the modules, read from the module tree.

``docs/getting-started/modules.md``, the architecture overview and the FAQ
state these: which dashboard pages the modules add, and which dependencies
only the modules install. The pages' sentences are tied to these tests by
their registries under ``docs/_claims/``. (What the split URL edge app
bundles for its router is held by
``split_url_router/test_split_url_edge_app.py``.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = Path(__file__).resolve().parents[4]


def _pins(relative: str) -> set:
    names = set()
    for line in (ROOT / relative).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-\[\]]+)==", line)
        if match:
            names.add(match.group(1).lower())
    return names


def test_the_module_dashboard_pages():
    pages = ROOT / "modules" / "frontend" / "src" / "pages"
    found = sorted(p.relative_to(pages).as_posix() for p in pages.rglob("*.tsx"))
    top = sorted({path.split("/")[0] for path in found})
    assert top == ["admin", "sso", "warehouse", "workspaces"], found
    assert "admin/roles.tsx" in found and "sso/complete.tsx" in found


def test_the_modules_bring_saml_oidc_and_the_sql_parser_and_the_backend_none_of_them():
    modules = _pins("modules/requirements.txt")
    assert {"python3-saml", "authlib", "sqlglot"} <= modules
    backend = _pins("backend/requirements.txt")
    assert not ({"python3-saml", "authlib", "sqlglot"} & backend)

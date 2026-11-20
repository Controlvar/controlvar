"""The full profile's password sign-ins are named in docs/api/auth.md (#1812).

The core half (``backend/tests/unit/docs/test_two_step_sign_in_docs.py``)
classifies every core file that names the password sign-in routes and holds
the page's table to the core ones; a core file may not name a module's path,
so the files under ``modules/`` are this test's. Each file here that names the
routes is classified, and the ones that sign in against a running stack and
read the access token are exactly the table's rows under ``modules/``: each
breaks if its account turns two-step sign-in on. It is
``mechanism:password-sign-ins-modules`` in ``scripts/doc_claims.py``.

Reads files only (``docs/api/auth.md`` and ``modules/``), with no git.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULES = REPO_ROOT / "modules"
PAGE = REPO_ROOT / "docs" / "api" / "auth.md"
HERE = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

#: The two sign-in routes, assembled so that this file does not match itself.
SIGN_IN = re.compile(("auth/" + "(?:login|token)").encode())

#: Sign in with a password against a running stack and read ``access_token``.
CONSUMERS = {
    "modules/backend/tests/realistic/scenarios/multi_tenant_isolation.py",
}

#: Scenarios that run against a stack: a file here that names the routes is
#: a consumer, never classified by where it is.
NAMED_ONLY = ("modules/backend/tests/realistic/",)

#: Everything else under modules/ that names the routes, by where it is.
BY_PREFIX = (
    ("modules/backend/app/", "the API itself"),
    ("modules/backend/tests/", "tests of the application, in process"),
)

#: Generated directories, as scripts/check_legacy_name.py leaves them out.
SKIP_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".next"}


def _hits() -> Set[str]:
    found = set()
    for path in MODULES.rglob("*"):
        if not path.is_file() or SKIP_DIRS.intersection(path.parts):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel != HERE and SIGN_IN.search(path.read_bytes()):
            found.add(rel)
    return found


def _class(rel: str):
    if rel in CONSUMERS:
        return "consumer"
    if rel.startswith(NAMED_ONLY):
        return None
    for prefix, name in BY_PREFIX:
        if rel.startswith(prefix):
            return name
    return None


def _table_paths() -> Set[str]:
    text = PAGE.read_text(encoding="utf-8")
    heading = "#### Password sign-ins in this repository"
    section = text[text.index(heading) + len(heading) :]
    end = re.search(r"^#{1,4} ", section, re.M)
    if end:
        section = section[: end.start()]
    return set(re.findall(r"^\| `(modules/[^`]{1,200})` \|", section, re.M))


def test_the_page_names_the_full_profiles_password_sign_ins():
    hits = _hits()
    assert hits, "the search found nothing under modules/: it is broken"
    unclassified = sorted(rel for rel in hits if _class(rel) is None)
    assert unclassified == [], (
        "name each in CONSUMERS (it signs in with a password against a stack) "
        f"or say what else it is: {unclassified}"
    )
    assert sorted(CONSUMERS - hits) == []
    assert _table_paths() == CONSUMERS

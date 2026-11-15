"""The grant and revoke tests sign in for real (#891, check #52).

``make_client_for_user`` and the shared ``client`` fixture
(``backend/tests/conftest.py``) bind the user to the fixture session, so a
revoke made through the API is never seen by the user object the next
request checks: a revoke test written with them passes against a server that
ignores revocation. The files that hold custom roles and grants to their
effect therefore must:

* never name ``make_client_for_user``;
* define their own ``client`` fixture, built on ``route_helpers.api_client``
  (real ``User`` rows, real local tokens, a session per request), which
  shadows the shared one for every test in the file;
* never build a ``TestClient`` of their own.

The provider's contract test (check #29) calls the provider directly, and the
writers' test (check #33) calls the route functions directly on one session;
neither makes a request, so they are held to the first rule only.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.smoke]

REPO = pathlib.Path(__file__).resolve().parents[4]
RBAC = REPO / "modules" / "backend" / "tests" / "integration" / "rbac"

#: Checks #30, #34, #35, #36 (requests through the API), and #1002's grant to
#: a user id with no account.
REQUEST_FILES = (
    "test_rbac_enforcement_routes.py",
    "test_rbac_revocation.py",
    "test_rbac_no_escalation.py",
    "test_rbac_server_keys.py",
    "test_rbac_activation.py",
    "test_rbac_effective_permissions_display.py",
    "test_rbac_statement_count.py",
    "test_rbac_grant_unknown_user.py",
)
#: Check #29 (the provider, called directly) and check #33 (the writers, called
#: directly on one session).
DIRECT_FILES = ("test_rbac_provider.py", "test_rbac_writers_forget.py")


def problems(name: str, source: str, *, makes_requests: bool) -> list[str]:
    module = ast.parse(source)
    found = []
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and node.id == "make_client_for_user":
            found.append(f"{name}: uses make_client_for_user")
        elif isinstance(node, ast.alias) and node.name == "make_client_for_user":
            found.append(f"{name}: imports make_client_for_user")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "TestClient"
        ):
            found.append(f"{name}: builds a TestClient of its own")
    if makes_requests:
        fixtures = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and any(
                "fixture" in ast.unparse(decorator) for decorator in node.decorator_list
            )
        }
        client = fixtures.get("client")
        if client is None:
            found.append(f"{name}: no client fixture of its own (the shared one binds)")
        elif "api_client(" not in ast.unparse(client):
            found.append(f"{name}: its client fixture is not built on api_client")
    return found


def test_every_rbac_effect_test_file_is_listed():
    on_disk = {p.name for p in RBAC.glob("test_*.py")}
    assert on_disk == set(REQUEST_FILES) | set(DIRECT_FILES), (
        f"classify each new file here: {sorted(on_disk ^ (set(REQUEST_FILES) | set(DIRECT_FILES)))}"
    )


@pytest.mark.parametrize("name", REQUEST_FILES + DIRECT_FILES)
def test_the_file_signs_in_for_real(name):
    source = (RBAC / name).read_text(encoding="utf-8")
    assert problems(name, source, makes_requests=name in REQUEST_FILES) == []


def test_a_revoke_test_with_the_fixture_client_is_named():
    """Positive control: the shapes that bind the user are each refused."""
    source = """
from backend.tests.integration.conftest import make_client_for_user


def test_revoke(db_session, user):
    make_client_for_user(db_session, user).post("/x")
"""
    assert problems("planted.py", source, makes_requests=True) == [
        "planted.py: imports make_client_for_user",
        "planted.py: uses make_client_for_user",
        "planted.py: no client fixture of its own (the shared one binds)",
    ]

"""Which routes exist, what each takes, and how a session ends (#1142).

Read from the application's own routes: ReDoc is served at ``/api/v1/redoc``;
no route overrides a flag for one user; the experiments collection lives at
``/api/v1/experiments/``; an API key is read only by the routes an SDK calls;
signing out revokes nothing; ``sdk:ruleset`` and its owner rule; and the
dashboard pages that need a superuser.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app.api import deps, sdk_scope
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, decode_local_token
from backend.app.main import app
from backend.app.models.user import UserRole

REPO = pathlib.Path(__file__).resolve().parents[4]

#: The prefixes of the routes an SDK calls: tracking, flag evaluation (one
#: flag, or every flag for a user) and the flag ruleset.
SDK_PREFIXES = (
    "/api/v1/tracking/",
    "/api/v1/feature-flags/evaluate/",
    "/api/v1/feature-flags/user/",
    "/api/v1/sdk/",
)


def _contexts():
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _calls(dependant):
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _calls(dependency)


def test_redoc_is_served_at_api_v1_redoc():
    response = TestClient(app).get("/api/v1/redoc")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "redoc" in response.text.lower()


def test_no_route_overrides_a_flag_for_one_user():
    flag_routes = {
        (method, path)
        for method, path, _ in _contexts()
        if path.startswith("/api/v1/feature-flags")
    }
    assert len(flag_routes) > 10, "the walk sees no flag routes"
    for method, path in flag_routes:
        assert "override" not in path.lower(), (method, path)
    # The one per-user route reads every flag's evaluation for that user.
    per_user = {(m, p) for m, p in flag_routes if "{user_id}" in p}
    assert per_user == {("GET", "/api/v1/feature-flags/user/{user_id}")}


def test_the_experiments_collection_url_ends_with_a_slash():
    response = TestClient(app, follow_redirects=False).get("/api/v1/experiments")
    assert response.status_code == 307
    assert response.headers["location"].endswith("/api/v1/experiments/")


def test_an_api_key_is_accepted_only_by_the_routes_an_sdk_calls():
    keyed = {
        (method, path)
        for method, path, ctx in _contexts()
        if deps.get_api_key in set(_calls(ctx.dependant))
    }
    assert len(keyed) >= 10, keyed
    assert [r for r in keyed if not r[1].startswith(SDK_PREFIXES)] == []
    # Nothing else reads the header: deps.py declares it, sdk_scope.py re-reads
    # it for the scoped routes.
    readers = set()
    for tree in (REPO / "backend" / "app", REPO / "modules" / "backend" / "app"):
        if not tree.is_dir():
            continue
        for path in tree.rglob("*.py"):
            code = re.sub(r'"""[\s\S]*?"""', "", path.read_text())
            if re.search(r"""["']x-api-key["']""", code, re.IGNORECASE):
                readers.add(path.relative_to(REPO).as_posix())
    assert readers <= {
        "backend/app/api/deps.py",
        "backend/app/api/sdk_scope.py",
        "backend/app/main.py",  # the CORS allow-list names it
        "backend/app/middleware/error_middleware.py",  # redacted from error reports
    }, readers


def test_signing_out_revokes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    user = SimpleNamespace(
        id=uuid.uuid4(), email="ana@example.com", role=UserRole.VIEWER
    )
    token = create_local_access_token(user)
    response = TestClient(app).post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 204
    # The same token still authenticates afterwards.
    assert decode_local_token(token)["sub"] == str(user.id)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        id=user.id, is_active=True
    )
    assert deps._authenticate_local_token(token, db).id == user.id


@pytest.mark.parametrize(
    "scopes, owner, refusal",
    [
        (
            "",
            SimpleNamespace(role=UserRole.ADMIN, is_active=True, is_superuser=False),
            "scope",
        ),
        (
            "sdk:ruleset",
            SimpleNamespace(role=UserRole.VIEWER, is_active=True, is_superuser=False),
            "owner",
        ),
        (
            "sdk:ruleset",
            SimpleNamespace(role=UserRole.ANALYST, is_active=True, is_superuser=False),
            "owner",
        ),
        (
            "sdk:ruleset",
            SimpleNamespace(
                role=UserRole.DEVELOPER, is_active=False, is_superuser=False
            ),
            "owner",
        ),
        ("sdk:ruleset", None, "owner"),
        (
            "sdk:ruleset",
            SimpleNamespace(
                role=UserRole.DEVELOPER, is_active=True, is_superuser=False
            ),
            None,
        ),
        (
            "sdk:ruleset",
            SimpleNamespace(role=UserRole.ADMIN, is_active=True, is_superuser=False),
            None,
        ),
    ],
)
def test_the_ruleset_scope_and_its_owner_rule(scopes, owner, refusal):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = owner
    key = SimpleNamespace(scopes=scopes, user_id=uuid.uuid4())
    expected = {
        "scope": sdk_scope.MISSING_SCOPE_DETAIL,
        "owner": sdk_scope.OWNER_ROLE_DETAIL,
        None: None,
    }[refusal]
    assert sdk_scope.scope_refusal(db, key) == expected


@pytest.mark.parametrize(
    "page",
    [
        "frontend/src/pages/admin/users.tsx",
        "frontend/src/pages/admin/api-keys.tsx",
        "frontend/src/pages/admin/safety.tsx",
        "modules/frontend/src/pages/admin/roles.tsx",
    ],
)
def test_the_users_api_keys_safety_and_roles_pages_need_a_superuser(page):
    path = REPO / page
    if page.startswith("modules/") and not path.is_file():
        return  # core checkout: no rbac module
    source = path.read_text()
    assert "export default withAdminGuard(" in source
    # The guard's default audience is superusers; only an explicit
    # `superuser: false` opens a page to anyone else.
    assert "superuser: false" not in source
    guard = (
        REPO / "frontend" / "src" / "components" / "admin" / "withAdminGuard.tsx"
    ).read_text()
    assert re.search(r"superuser\s*=\s*true", guard), (
        "the guard's default is not superuser"
    )


def test_a_password_set_in_the_database_never_reaches_cognito():
    import ast

    from backend.app.api.v1.endpoints.users import apply_password_change

    cognito_password_calls = {
        "admin_set_user_password",
        "admin_reset_user_password",
        "change_password",
        "set_user_password",
    }
    for tree in (REPO / "backend" / "app", REPO / "modules" / "backend" / "app"):
        if not tree.is_dir():
            continue
        for path in tree.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in cognito_password_calls, path

    # PUT /admin/users/{id} and PUT /users/{id}: the password becomes the
    # account's hash in this database, and that is all.
    target = SimpleNamespace(id=uuid.uuid4())
    caller = SimpleNamespace(id=uuid.uuid4())
    update = {"password": "N3w-passw0rd!"}
    apply_password_change(target, caller, update)
    assert set(update) == {"hashed_password"}
    assert update["hashed_password"] != "N3w-passw0rd!"


def test_custom_roles_are_assigned_through_the_api_and_no_dashboard_page():
    assigners = []
    for tree in (REPO / "frontend" / "src", REPO / "modules" / "frontend" / "src"):
        if not tree.is_dir():
            continue
        for path in tree.rglob("*.ts*"):
            if "/tests/" in path.as_posix() or ".test." in path.name:
                continue
            if "roles/assign" in path.read_text(errors="ignore"):
                assigners.append(path)
    assert assigners == []
    if (REPO / "modules").is_dir():
        assert ("POST", "/api/v1/rbac/roles/assign") in {
            (m, p) for m, p, _ in _contexts()
        }


def test_a_deactivated_account_is_refused():
    from fastapi import HTTPException

    user = SimpleNamespace(
        id=uuid.uuid4(), email="ana@example.com", role=UserRole.VIEWER
    )
    token = create_local_access_token(user)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        id=user.id, is_active=False
    )
    with pytest.raises(HTTPException) as refused:
        deps._authenticate_local_token(token, db)
    assert (refused.value.status_code, refused.value.detail) == (400, "Inactive user")


def test_a_new_key_may_repeat_the_old_ones_name_and_scopes_and_leaves_it_working():
    from backend.app.models.api_key import APIKey

    table = APIKey.__table__
    assert not table.c.name.unique
    assert not [
        c
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint" and "name" in c.columns.keys()
    ]

    db = MagicMock()
    owner = uuid.uuid4()
    first, _ = APIKey.create_for_user(
        db, user_id=owner, name="checkout", scopes="sdk:ruleset"
    )
    second, _ = APIKey.create_for_user(
        db, user_id=owner, name="checkout", scopes="sdk:ruleset"
    )
    # Creating the new key adds a row and touches no other: the old key keeps
    # working until it is deleted.
    assert [c.args[0] for c in db.add.call_args_list] == [first, second]
    db.delete.assert_not_called()
    db.query.assert_not_called()
    assert first.key != second.key


def test_the_scope_refusal_says_what_to_create_and_the_key_header_is_x_api_key():
    assert sdk_scope.MISSING_SCOPE_DETAIL == (
        "This API key does not have the 'sdk:ruleset' scope. Create a key with the "
        "'sdk:ruleset' scope for server-side SDK use."
    )
    assert deps.API_KEY_HEADER.model.name == "X-API-Key"

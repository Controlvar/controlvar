"""No test may request two fixtures that each sign a client in.

``make_client_for_user`` (``backend/tests/integration/conftest.py``)
authenticates by writing ``app.dependency_overrides``, which is app-global.
Every client it has returned then acts as the user of the most recent call.
A test that takes ``admin_client`` and ``developer_client`` therefore runs
every request as whichever fixture pytest set up last -- two "developer can
read results" tests ran as ADMIN this way and passed against a route that
refused developers (#470).  A fixture that writes
``app.dependency_overrides[...get_current_active_user]`` (or
``get_current_user`` / ``get_current_superuser``) itself uses the same
mechanism, so it counts too (#476).

The fix for such a test is to do the setup with one client and make the
actor's client afterwards with ``make_client_for_user`` (or a lazy factory,
as ``modules/backend/tests/integration/warehouse/test_run_sql_roles.py``
does).  This is a static AST scan: no database, no app import.

Not covered: a fixture requested dynamically with
``request.getfixturevalue`` or ``@pytest.mark.usefixtures``, a fixture that
signs in through a helper in another file other than ``make_client_for_user``,
an override keyed by a variable rather than the dependency itself, and a client
variable kept in a test body after a later sign-in has replaced its user.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import pytest

from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = pytest.mark.smoke

# The dependencies whose override decides who a request acts as.  A fixture
# that writes one of them into app.dependency_overrides signs a client in.
AUTH_DEPENDENCIES = frozenset(
    {"get_current_active_user", "get_current_user", "get_current_superuser"}
)

# Every fixture that installs the auth override: it calls make_client_for_user
# or writes an AUTH_DEPENDENCIES override, directly or through a helper in its
# own file.  Pinned against the tree by test_the_fixture_list_is_complete, so a
# new one cannot be missed.  Names are matched tree-wide, which is conservative
# for the file-local ones (``client`` exists in a dozen files).
ROLE_CLIENT_FIXTURES = frozenset(
    {
        # backend/tests/conftest.py
        "client",
        "mock_auth",
        "mock_auth_superuser",
        # backend/tests/integration/conftest.py, plus file-local ones of the
        # same names (contract/e2e conftests, test_interaction_endpoints.py,
        # test_experiment_wizard_endpoints.py)
        "admin_client",
        "developer_client",
        "analyst_client",
        "viewer_client",
        # test_experiment_cache_redis.py, test_flag_reads_after_writers.py
        "cached_client",
        "cached_superuser_client",
        "cached_unreadable_client",
        # test_user_email_and_username_updates.py
        "member",
        "member_as_written",
        # test_tracking_text_refused.py, test_sdk_path_text_refused.py,
        # test_tracking_events_unknown_ids.py
        "sdk_client",
        # test_unexpected_errors_fixed_message.py (core); the modules'
        # test_workspaces_api.py defines a fixture of the same name.
        "as_user",
        # Fixtures that write the override themselves (#476).
        "authenticated_client",  # test_experiment_wizard_endpoints.py
        "client_analyst",  # test_bayesian_results_api.py
        "client_developer",  # test_bayesian_results_api.py
        "client_with_developer",  # test_ai_design_endpoints.py
        "client_with_viewer",  # test_ai_design_endpoints.py
        "preview",  # test_segment_preview_limits.py
        "setup_app_overrides",  # test_results_integration.py
        "shared_client",  # test_rollout_schedules_api.py
        "superuser_results_client",  # test_results_correction_pins.py
        "harness",  # test_results_stored_settings.py
        "scan_client",  # test_interaction_scan_failures.py
        "signed_in",  # test_flag_list_forbidden.py
        "as_role",  # test_breakdown_reads.py
    }
)
# Defined only under modules/backend/tests, which a core build does not have.
MODULE_ROLE_CLIENT_FIXTURES: frozenset[str] = frozenset(
    {
        # test_etl_endpoints.py
        "client_as_admin",
        "client_as_developer",
        "client_as_analyst",
        "client_as_viewer",
        # test_etl_errors_fixed_message.py
        "call",
        # test_integration_config_rules.py
        "admin_session",
    }
)

# The module trees are absent from a core build; scanning what exists is right.
SCAN_ROOTS = ("backend/tests", "modules/backend/tests")

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_ALL = ROLE_CLIENT_FIXTURES | MODULE_ROLE_CLIENT_FIXTURES


def _files() -> Iterator[Path]:
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))


def _modules() -> Iterator[Tuple[str, List[ast.AST]]]:
    """(path relative to REPO_ROOT, every function def in the file)."""
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = [n for n in ast.walk(tree) if isinstance(n, _FUNCS)]
        yield path.relative_to(REPO_ROOT).as_posix(), funcs


def _params(func: ast.AST) -> Set[str]:
    args = func.args
    return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", None)


def _is_fixture(func: ast.AST) -> bool:
    for dec in func.decorator_list:
        if _name(dec.func if isinstance(dec, ast.Call) else dec) == "fixture":
            return True
    return False


def _called(func: ast.AST) -> Set[str]:
    return {_name(n.func) for n in ast.walk(func) if isinstance(n, ast.Call)} - {None}


def _is_overrides(node: ast.AST) -> bool:
    return _name(node) == "dependency_overrides"


def _overrides_auth(func: ast.AST) -> bool:
    """True if the function writes an auth dependency override itself:
    ``x.dependency_overrides[<...>.get_current_active_user] = ...`` (or one of
    the other AUTH_DEPENDENCIES), or ``x.dependency_overrides.update({...})``
    with such a key."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            targets = []
        for target in targets:
            for t in target.elts if isinstance(target, ast.Tuple) else [target]:
                if (
                    isinstance(t, ast.Subscript)
                    and _is_overrides(t.value)
                    and _name(t.slice) in AUTH_DEPENDENCIES
                ):
                    return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("update", "setdefault")
            and _is_overrides(node.func.value)
        ):
            for arg in node.args:
                keys = arg.keys if isinstance(arg, ast.Dict) else [arg]
                if any(k is not None and _name(k) in AUTH_DEPENDENCIES for k in keys):
                    return True
    return False


def _signing_in(funcs: List[ast.AST]) -> Set[str]:
    """Names of functions in one file that sign a client in: they write an
    auth override themselves or reach make_client_for_user, directly or
    through other functions of the same file."""
    calls: Dict[str, Set[str]] = {}
    for f in funcs:
        calls.setdefault(f.name, set()).update(_called(f))
    seeds = {"make_client_for_user"} | {f.name for f in funcs if _overrides_auth(f)}
    reach = set(seeds)
    while True:
        more = {name for name, c in calls.items() if c & reach} - reach
        if not more:
            return reach - {"make_client_for_user"}
        reach |= more


def _signing_in_fixtures(funcs: List[ast.AST]) -> Set[str]:
    signing_in = _signing_in(funcs)
    return {f.name for f in funcs if _is_fixture(f) and f.name in signing_in}


def offenders() -> List[str]:
    """Tests and fixtures that request more than one sign-in fixture."""
    found = []
    for rel, funcs in _modules():
        for func in funcs:
            if not (func.name.startswith("test") or _is_fixture(func)):
                continue
            requested = _params(func) & _ALL
            if len(requested) > 1:
                found.append(
                    f"{rel}:{func.lineno} {func.name} requests {sorted(requested)}"
                )
    return found


@pytest.mark.regression
def test_no_test_requests_two_role_clients():
    bad = offenders()
    assert not bad, (
        "These request more than one fixture that signs a client in. The auth "
        "override is app-global, so every request runs as whichever fixture "
        "was set up last. Do the setup with one client, then make the actor's "
        "client with make_client_for_user:\n  " + "\n  ".join(bad)
    )


def test_the_fixture_list_is_complete():
    """Every fixture that reaches make_client_for_user is in the list above."""
    fixtures = set()
    for _, funcs in _modules():
        fixtures |= _signing_in_fixtures(funcs)
    expected = set(ROLE_CLIENT_FIXTURES)
    if (REPO_ROOT / "modules" / "backend" / "tests").is_dir():
        expected |= MODULE_ROLE_CLIENT_FIXTURES
    assert fixtures == expected, (
        f"missing from the lists: {sorted(fixtures - expected)}; "
        f"listed but not found: {sorted(expected - fixtures)}"
    )


def test_the_scan_is_not_vacuous():
    """The scan sees the tests that use these fixtures, so a wrong root or a
    broken parse cannot turn the gate into a pass."""
    users = [
        func.name
        for _, funcs in _modules()
        for func in funcs
        if func.name.startswith("test") and _params(func) & _ALL
    ]
    assert len(users) > 100, len(users)


# Fixture shapes that sign a client in without make_client_for_user (#476).
# Each is parsed as if it were a test file; the scan has to report every one.
_DIRECT_OVERRIDE_SHAPES = {
    "subscript": """
@pytest.fixture
def planted(db_session):
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    yield TestClient(app)
""",
    "bare_name_get_current_user": """
@pytest.fixture()
def planted():
    app.dependency_overrides[get_current_user] = override
    return TestClient(app)
""",
    "superuser_via_update": """
@pytest.fixture
def planted():
    app.dependency_overrides.update({deps.get_current_superuser: override})
    return TestClient(app)
""",
    "through_local_helper": """
def _sign_in(user):
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    return TestClient(app)


@pytest.fixture
def planted(viewer):
    return _sign_in(viewer)
""",
}


def _parse_funcs(source: str) -> List[ast.AST]:
    return [n for n in ast.walk(ast.parse(source)) if isinstance(n, _FUNCS)]


@pytest.mark.regression
@pytest.mark.parametrize("shape", sorted(_DIRECT_OVERRIDE_SHAPES))
def test_a_fixture_writing_the_auth_override_counts_as_signing_in(shape):
    """#476: the scan saw only fixtures that reach make_client_for_user, so a
    fixture writing ``dependency_overrides[...get_current_active_user]``
    itself was invisible to test_the_fixture_list_is_complete."""
    funcs = _parse_funcs(_DIRECT_OVERRIDE_SHAPES[shape])
    assert _signing_in_fixtures(funcs) == {"planted"}


def test_other_overrides_do_not_count_as_signing_in():
    """Overriding get_db, or only removing the auth override, signs nobody
    in, so the list stays exact rather than growing to every client fixture."""
    funcs = _parse_funcs(
        """
@pytest.fixture
def db_only(db_session):
    app.dependency_overrides[deps.get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    del app.dependency_overrides[deps.get_current_user]
"""
    )
    assert _signing_in_fixtures(funcs) == set()

"""Every change the audit log covers that is made outside a route is classified (#221).

The route inventory (``test_audit_inventory.py``) classifies each mutating
route. This one reads the source of ``backend/app`` -- every module except the
route modules under ``backend/app/api/v1/endpoints/`` -- and finds each site
that:

* sets an experiment's ``status`` to an ``ExperimentStatus`` member;
* sets ``rollout_percentage`` (a flag's rollout);
* sets ``role``, ``is_superuser`` or ``is_active``;
* constructs a ``User``.

It finds them syntactically, so a site is any assignment to one of those
attributes, whatever the object. A dynamic ``setattr`` is not seen.

``SITES`` names each one -- module, function, kind, and how many times the
function does it -- and classifies it:

* ``Audited(actions)``: the function writes these actions. The test checks
  that the function passes each ``ActionType`` member to a call; that each
  site really writes its entry is pinned by
  ``backend/tests/integration/services/test_audit_service_sites.py``.
* ``RouteReached(names)``: the change is made only on behalf of a route, and
  the route writes the entry. The test follows every reference to ``names``
  (the function's own name by default) upwards; each chain must end in a
  route module or in a function nothing refers to. A chain that reaches
  module level elsewhere (an app lifespan, a scheduler) fails it.
* ``Exempt(reason)``: not on the audited event list, with the reason.

The comparison is exact, so a new site fails ``test_the_sites_are_exact``
until it is classified, and a removed one fails it until its line goes. The
source is read from this file's own path (no git), so the check runs the same
in core-build's copy of the tree.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

import pytest

from backend.app.models.audit_log import ActionType as A

pytestmark = [pytest.mark.smoke]

REPO_ROOT = Path(__file__).resolve().parents[3]
APP = REPO_ROOT / "backend" / "app"
ROUTES = "backend/app/api/v1/endpoints/"

#: Attributes whose assignment is a site, whatever the value.
ATTRIBUTES = ("rollout_percentage", "role", "is_superuser", "is_active")


@dataclass(frozen=True)
class Audited:
    actions: frozenset

    def __init__(self, *actions: A) -> None:
        object.__setattr__(self, "actions", frozenset(actions))


@dataclass(frozen=True)
class RouteReached:
    names: Tuple[str, ...] = ()
    reason: str = "called only by routes, which write the entry"


@dataclass(frozen=True)
class Exempt:
    reason: str


DEV_USER = Exempt(
    "the synthetic administrator of the development sign-in shortcut, which "
    "is refused outside the development and test environments"
)

#: (module, function, kind) -> (how many times, classification).
SITES: Dict[Tuple[str, str, str], Tuple[int, object]] = {
    # --- the experiment scheduler -------------------------------------------
    (
        "backend/app/core/scheduler.py",
        "ExperimentScheduler._process_due",
        "status=ACTIVE",
    ): (1, Audited(A.EXPERIMENT_START)),
    (
        "backend/app/core/scheduler.py",
        "ExperimentScheduler._process_due",
        "status=COMPLETED",
    ): (1, Audited(A.EXPERIMENT_COMPLETE)),
    # --- the rollout scheduler ----------------------------------------------
    (
        "backend/app/core/rollout_scheduler.py",
        "RolloutScheduler._activate_stage",
        "rollout_percentage",
    ): (1, Audited(A.FEATURE_FLAG_UPDATE)),
    # --- safety rollback (manual and monitor) -------------------------------
    (
        "backend/app/services/safety_service.py",
        "SafetyService.execute_rollback",
        "rollout_percentage",
    ): (2, Audited(A.SAFETY_ROLLBACK)),
    # --- Cognito sign-in: group role sync and the first sign-in --------------
    (
        "backend/app/services/cognito_accounts.py",
        "resolve_cognito_user",
        "role",
    ): (1, Audited(A.ROLE_ASSIGN)),
    (
        "backend/app/services/cognito_accounts.py",
        "resolve_cognito_user",
        "is_superuser",
    ): (1, Audited(A.ROLE_ASSIGN)),
    (
        "backend/app/services/cognito_accounts.py",
        "resolve_cognito_user",
        "User()",
    ): (1, Audited(A.USER_CREATE)),
    # --- services the routes call -------------------------------------------
    (
        "backend/app/services/experiment_service.py",
        "ExperimentService.start_experiment",
        "status=ACTIVE",
    ): (1, RouteReached()),
    (
        "backend/app/services/experiment_service.py",
        "ExperimentService.pause_experiment",
        "status=PAUSED",
    ): (1, RouteReached()),
    (
        "backend/app/services/experiment_service.py",
        "ExperimentService.complete_experiment",
        "status=COMPLETED",
    ): (1, RouteReached()),
    (
        "backend/app/services/experiment_service.py",
        "ExperimentService.archive_experiment",
        "status=ARCHIVED",
    ): (1, RouteReached()),
    (
        "backend/app/services/global_holdout_service.py",
        "GlobalHoldoutService._activate",
        "is_active",
    ): (2, RouteReached()),
    (
        "backend/app/services/global_holdout_service.py",
        "GlobalHoldoutService._deactivate",
        "is_active",
    ): (1, RouteReached()),
    (
        "backend/app/services/rollout_service.py",
        "RolloutService.manually_advance_stage",
        "rollout_percentage",
    ): (2, RouteReached()),
    (
        "backend/app/crud/crud_user.py",
        "CRUDUser.create",
        "User()",
    ): (1, RouteReached(("crud_user",))),
    # --- not on the audited event list --------------------------------------
    ("backend/app/api/deps.py", "_get_or_create_dev_user", "User()"): (1, DEV_USER),
    ("backend/app/api/deps_dev.py", "get_current_user_dev", "User()"): (1, DEV_USER),
    ("backend/app/db/bootstrap.py", "ensure_first_superuser", "User()"): (
        1,
        Exempt(
            "the first administrator, made by the bootstrap when the "
            "database is created, before anyone can sign in"
        ),
    ),
}


# --- reading the source -------------------------------------------------------


@lru_cache(maxsize=1)
def _parsed() -> Tuple[Tuple[str, ast.Module], ...]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        found.append((rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel)))
    return tuple(found)


def _modules() -> Iterator[Tuple[str, ast.Module]]:
    return iter(_parsed())


class _Walker(ast.NodeVisitor):
    """Visits a module keeping the qualified name of the enclosing function."""

    def __init__(self) -> None:
        self.stack: list = []

    @property
    def qualname(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _scoped(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped


def _site_kind(target: ast.expr, value: ast.expr):
    if not isinstance(target, ast.Attribute):
        return None
    if target.attr == "status":
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "ExperimentStatus"
        ):
            return f"status={value.attr}"
        return None
    if target.attr in ATTRIBUTES:
        return target.attr
    return None


def sites_in(rel: str, tree: ast.Module) -> Counter:
    """(module, function, kind) -> count, for one module."""
    found: Counter = Counter()

    class V(_Walker):
        def visit_Assign(self, node):
            for target in node.targets:
                kind = _site_kind(target, node.value)
                if kind:
                    found[(rel, self.qualname, kind)] += 1
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            kind = _site_kind(node.target, node.value) if node.value else None
            if kind:
                found[(rel, self.qualname, kind)] += 1
            self.generic_visit(node)

        def visit_Call(self, node):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "User") or (
                isinstance(func, ast.Attribute) and func.attr == "User"
            ):
                found[(rel, self.qualname, "User()")] += 1
            self.generic_visit(node)

    V().visit(tree)
    return found


def all_sites() -> Counter:
    found: Counter = Counter()
    for rel, tree in _modules():
        if rel.startswith(ROUTES):
            continue
        found.update(sites_in(rel, tree))
    return found


def actions_named(rel: str, qualname: str) -> Set[str]:
    """The ``ActionType`` members the function passes to a call."""
    tree = dict(_modules())[rel]
    named: Set[str] = set()

    class V(_Walker):
        def visit_Call(self, node):
            if self.qualname == qualname:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    if (
                        isinstance(arg, ast.Attribute)
                        and isinstance(arg.value, ast.Name)
                        and arg.value.id == "ActionType"
                    ):
                        named.add(arg.attr)
            self.generic_visit(node)

    V().visit(tree)
    return named


@lru_cache(maxsize=1)
def references() -> Dict[str, Set[Tuple[str, str]]]:
    """name -> {(module, enclosing function)} for every name read in backend/app."""
    refs: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for rel, tree in _modules():

        class V(_Walker):
            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Load):
                    refs[node.id].add((rel, self.qualname))

            def visit_Attribute(self, node):
                if isinstance(node.ctx, ast.Load):
                    refs[node.attr].add((rel, self.qualname))
                self.generic_visit(node)

        V().visit(tree)
    return refs


def roots_outside_routes(names, refs) -> Set[Tuple[str, str]]:
    """Where a chain of references to ``names`` ends outside a route module.

    Follows each reference to the function that holds it, then references to
    that function's name, and so on. A chain ends in a route module (fine),
    in a function nothing refers to (fine), or at module level outside the
    route modules, which is returned.
    """
    roots: Set[Tuple[str, str]] = set()
    seen: Set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for rel, qualname in refs.get(name, ()):
            if rel.startswith(ROUTES):
                continue
            if qualname == "<module>":
                roots.add((rel, qualname))
                continue
            todo.append(qualname.split(".")[-1])
    return roots


#: Actions written outside a route; ``test_audit_inventory.py`` adds them to
#: the route inventory's when it pins ``WRITTEN_ACTION_TYPES``.
NON_ROUTE_ACTIONS: frozenset = frozenset(
    action
    for _, entry in SITES.values()
    if isinstance(entry, Audited)
    for action in entry.actions
)


# --- the gates ----------------------------------------------------------------


def test_the_sites_are_exact():
    found = all_sites()
    expected = Counter({key: count for key, (count, _) in SITES.items()})
    assert found == expected, (
        f"unclassified or miscounted: "
        f"{sorted((k, n) for k, n in found.items() if expected.get(k) != n)}; "
        f"gone: {sorted(k for k in expected if k not in found)}"
    )
    assert sum(expected.values()) == 21


@pytest.mark.parametrize(
    "key",
    sorted(k for k, (_, e) in SITES.items() if isinstance(e, Audited)),
    ids=lambda k: f"{k[1]} {k[2]}",
)
def test_each_audited_site_names_its_actions(key):
    rel, qualname, _ = key
    _, entry = SITES[key]
    named = actions_named(rel, qualname)
    missing = sorted(a.name for a in entry.actions if a.name not in named)
    assert not missing, f"{qualname} writes no {missing}"


@pytest.mark.parametrize(
    "key",
    sorted(k for k, (_, e) in SITES.items() if isinstance(e, RouteReached)),
    ids=lambda k: f"{k[1]} {k[2]}",
)
def test_each_route_reached_site_is_reached_only_from_routes(key):
    _, qualname, _ = key
    _, entry = SITES[key]
    names = entry.names or (qualname.split(".")[-1],)
    roots = roots_outside_routes(names, references())
    assert roots == set(), f"{qualname} is reached from outside a route: {roots}"


@pytest.mark.parametrize("key", sorted(SITES), ids=lambda k: f"{k[1]} {k[2]}")
def test_each_entry_is_well_formed(key):
    count, entry = SITES[key]
    assert count >= 1
    if isinstance(entry, Audited):
        assert entry.actions and all(isinstance(a, A) for a in entry.actions)
    elif isinstance(entry, RouteReached):
        assert entry.reason.strip()
    else:
        assert isinstance(entry, Exempt) and entry.reason.strip()


# The scanner itself, with each kind of site planted.


def _scan(source: str) -> Counter:
    return sites_in("x.py", ast.parse(source))


def test_the_scanner_finds_each_kind_of_site():
    found = _scan(
        "class S:\n"
        "    def tick(self, e, f, u):\n"
        "        e.status = ExperimentStatus.ACTIVE\n"
        "        f.rollout_percentage = 5\n"
        "        u.role = r\n"
        "        u.is_superuser = True\n"
        "        u.is_active = False\n"
        "        User(email='a@example.com')\n"
        "        e.status = 'x'\n"
    )
    assert found == Counter(
        {
            ("x.py", "S.tick", k): 1
            for k in (
                "status=ACTIVE",
                "rollout_percentage",
                "role",
                "is_superuser",
                "is_active",
                "User()",
            )
        }
    )


def test_a_reference_from_module_level_is_a_root():
    refs = {
        "helper": {("backend/app/core/x.py", "Job.run")},
        "run": {("backend/app/core/x.py", "<module>")},
    }
    assert roots_outside_routes(("helper",), refs) == {
        ("backend/app/core/x.py", "<module>")
    }
    refs["run"] = {(ROUTES + "x.py", "<module>")}
    assert roots_outside_routes(("helper",), refs) == set()

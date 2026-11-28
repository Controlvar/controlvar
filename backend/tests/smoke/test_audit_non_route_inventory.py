"""Every change the audit log covers that is made outside a route is classified (#221).

The route inventory (``test_audit_inventory.py``) classifies each mutating
route. This one reads the source of ``backend/app`` -- every module except the
route modules under ``backend/app/api/v1/endpoints/`` -- and finds each site
that:

* sets an experiment's ``status`` to an ``ExperimentStatus`` member;
* sets a flag's ``status``: to a ``FeatureFlagStatus`` member, to what
  ``transition`` answers, or to a name bound to either in that module;
* sets ``rollout_percentage`` or ``targeting_rules`` (a flag's, in any
  environment);
* sets ``role``, ``is_superuser`` or ``is_active``;
* constructs a ``User``;
* writes an account's two-step sign-in (#1812) or a flag's settings in staging
  or development (#2011): ``insert``, ``pg_insert``, ``update`` or ``delete``
  called with ``UserTOTP`` or ``FlagEnvironmentSetting`` first, or one of them
  constructed. Those tables are written with statements, not by setting
  attributes, so their sites are the statements.

It finds them syntactically, so a site is any assignment to one of those
attributes, whatever the object. An ORM ``session.delete(row)`` or
``query(Model).delete()`` is not seen here, nor is a statement on
``Model.__table__`` rather than ``Model`` itself, a session's bulk methods or
raw SQL; for ``FlagEnvironmentSetting``, ``test_flag_environment_writers.py``
refuses those and says what it does not see. A ``setattr`` whose attribute is
not written out sets any attribute, so every one in ``backend/app`` is listed
in ``DYNAMIC_SETATTR``, with what it sets.

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
ATTRIBUTES = (
    "rollout_percentage",
    "targeting_rules",
    "split",
    "role",
    "is_superuser",
    "is_active",
)


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
    # A scheduled start also closes the experiment's open visual editor
    # sessions, one entry each (#1545).
    (
        "backend/app/core/scheduler.py",
        "ExperimentScheduler._process_due",
        "status=ACTIVE",
    ): (1, Audited(A.EXPERIMENT_START, A.VISUAL_EDITOR_SESSION_CLOSED)),
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
    (
        "backend/app/services/safety_service.py",
        "SafetyService.execute_rollback",
        "flag status",
    ): (1, Audited(A.SAFETY_ROLLBACK)),
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
        "backend/app/services/experiment_service.py",
        "ExperimentService.unarchive_experiment",
        "status=COMPLETED",
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
    (
        "backend/app/services/feature_flag_service.py",
        "FeatureFlagService.activate_feature_flag",
        "flag status",
    ): (1, RouteReached()),
    (
        "backend/app/services/feature_flag_service.py",
        "FeatureFlagService.deactivate_feature_flag",
        "flag status",
    ): (1, RouteReached()),
    # The split a promotion into production copies (#2012); the update's own
    # field goes through the dynamic setattr below. Both recorded by record=.
    (
        "backend/app/services/feature_flag_service.py",
        "FeatureFlagService.update_feature_flag",
        "split",
    ): (1, RouteReached()),
    (
        "backend/app/crud/crud_feature_flag.py",
        "CRUDFeatureFlag.activate",
        "flag status",
    ): (1, RouteReached(("crud_feature_flag",))),
    (
        "backend/app/crud/crud_feature_flag.py",
        "CRUDFeatureFlag.deactivate",
        "flag status",
    ): (1, RouteReached(("crud_feature_flag",))),
    # --- a flag's settings in staging and development (#2011) ---------------
    # The environment PUT and promotion; bulk archive.
    (
        "backend/app/services/flag_environment_service.py",
        "FlagEnvironmentService.save_environment_setting",
        "flag_environment_settings insert",
    ): (1, RouteReached()),
    (
        "backend/app/services/flag_environment_service.py",
        "turn_off_environment_settings",
        "flag_environment_settings update",
    ): (1, RouteReached()),
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
    (
        "backend/app/services/experiment_service.py",
        "stamp_rollout_rule_id",
        "targeting_rules",
    ): (
        1,
        Exempt(
            "an experiment's own rule, given the experiment's id when it "
            "starts, which admits the users it would have; the start writes "
            "its entry"
        ),
    ),
    # --- two-step sign-in (#1812): every write to user_totp --------------------
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService.confirm",
        "user_totp update",
    ): (1, Audited(A.USER_2FA_ENABLE)),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService.disable",
        "user_totp delete",
    ): (1, Audited(A.USER_2FA_DISABLE)),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService.regenerate_recovery_codes",
        "user_totp update",
    ): (1, Audited(A.USER_2FA_RECOVERY_REGENERATE)),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService.reset",
        "user_totp delete",
    ): (1, Audited(A.USER_2FA_RESET)),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService._consume_recovery_hash",
        "user_totp update",
    ): (1, Audited(A.USER_2FA_RECOVERY_USED)),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService.enrol",
        "user_totp insert",
    ): (
        1,
        Exempt(
            "a secret waiting for its first code, which changes nothing at "
            "sign-in; confirming it writes user_2fa_enable"
        ),
    ),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService._accept_totp_step",
        "user_totp update",
    ): (
        1,
        Exempt(
            "the step of an accepted code, so it is not used again; the "
            "change the code was for writes its own entry"
        ),
    ),
    (
        "backend/app/services/two_factor_service.py",
        "TwoFactorService._count_refusal",
        "user_totp update",
    ): (
        1,
        Exempt(
            "one more wrong code; a refused code is logged "
            "(two_factor_refused), never audited"
        ),
    ),
}

#: (module, function, object) -> (how many times, what it sets), for every
#: ``setattr`` in ``backend/app``, routes included, whose attribute name is
#: not written out.
DYNAMIC_SETATTR: Dict[Tuple[str, str, str], Tuple[int, str]] = {
    # --- a flag ---------------------------------------------------------------
    (
        "backend/app/services/feature_flag_service.py",
        "FeatureFlagService.update_feature_flag",
        "flag",
    ): (
        1,
        "the fields a flag update sent; every caller passes record= "
        "(test_flag_audit_strict_sites.py)",
    ),
    (
        "backend/app/core/flag_environments.py",
        "FlagInEnvironment.__init__",
        "self",
    ): (1, "a read-only view of a flag in an environment, not a stored row"),
    # --- rows of other tables ---------------------------------------------------
    ("backend/app/crud/base.py", "CRUDBase.update", "db_obj"): (
        1,
        "any model's row; nothing in backend/app calls update on a crud object",
    ),
    ("backend/app/api/v1/endpoints/admin.py", "update_user", "user"): (1, "a user"),
    ("backend/app/api/v1/endpoints/users.py", "update_user", "user"): (1, "a user"),
    (
        "backend/app/api/v1/endpoints/notifications.py",
        "update_my_preferences",
        "prefs",
    ): (1, "a user's notification preferences"),
    ("backend/app/services/analysis_snapshot_service.py", "_apply", "row"): (
        1,
        "an analysis snapshot",
    ),
    (
        "backend/app/services/experiment_service.py",
        "ExperimentService.update_experiment",
        "experiment",
    ): (1, "an experiment"),
    (
        "backend/app/services/llm_experiment_service.py",
        "LLMExperimentService.update_experiment",
        "experiment",
    ): (1, "an LLM experiment"),
    (
        "backend/app/services/llm_experiment_service.py",
        "LLMExperimentService.update_variant",
        "variant",
    ): (1, "an LLM experiment's variant"),
    (
        "backend/app/services/rollout_service.py",
        "RolloutService.update_rollout_schedule",
        "schedule",
    ): (1, "a rollout schedule"),
    (
        "backend/app/services/rollout_service.py",
        "RolloutService.update_rollout_stage",
        "stage",
    ): (1, "a rollout stage"),
    (
        "backend/app/services/safety_service.py",
        "SafetyService.create_or_update_feature_flag_safety_config",
        "existing",
    ): (1, "a flag's safety configuration"),
    (
        "backend/app/services/safety_service.py",
        "SafetyService.update_feature_flag_safety_config",
        "db_obj",
    ): (1, "a flag's safety configuration"),
    (
        "backend/app/services/safety_service.py",
        "SafetyService.create_or_update_safety_settings",
        "existing",
    ): (1, "the safety settings"),
    (
        "backend/app/services/safety_service.py",
        "SafetyService.update_safety_settings",
        "db_obj",
    ): (1, "the safety settings"),
    # --- not a stored row -------------------------------------------------------
    ("backend/app/api/off_loop_route.py", "off_loop", "handler"): (
        1,
        "a marker on a route's function",
    ),
    ("backend/app/api/v1/endpoints/tracking.py", "_assign_users_in_batch", "counts"): (
        1,
        "a response's counts",
    ),
    ("backend/app/schemas/experiment.py", "ScheduleConfig.validate_dates", "self"): (
        1,
        "a request model's dates",
    ),
}

#: The models written with statements, and their tables.
_STATEMENT_MODELS = {
    "UserTOTP": "user_totp",
    "FlagEnvironmentSetting": "flag_environment_settings",
}
#: The calls that write such a table when the model is their first argument,
#: and the kind each site is recorded as, after the table.
_STATEMENTS = {
    "insert": "insert",
    "pg_insert": "insert",
    "update": "update",
    "delete": "delete",
}


def _named(node):
    """The last name of ``x`` or ``a.b.x``, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def statement_kind(node: ast.Call):
    """How a call writes a model in ``_STATEMENT_MODELS``, or None."""
    name = _named(node.func)
    if name in _STATEMENT_MODELS:
        return f"{name}()"
    model = _named(node.args[0]) if node.args else None
    if name in _STATEMENTS and model in _STATEMENT_MODELS:
        return f"{_STATEMENT_MODELS[model]} {_STATEMENTS[name]}"
    return None


def _answers_a_flag_status(value: ast.AST, names: Set[str]) -> bool:
    """Whether *value* reads ``FeatureFlagStatus``, calls ``transition``, or
    reads a name bound to either."""
    return any(
        (isinstance(sub, ast.Call) and _named(sub.func) == "transition")
        or (
            isinstance(sub, ast.Name)
            and (sub.id in names or sub.id == "FeatureFlagStatus")
        )
        for sub in ast.walk(value)
    )


def _flag_status_names(tree: ast.Module) -> Set[str]:
    """The names the module binds to a flag's status, anywhere in it."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            if node.value is None or not _answers_a_flag_status(node.value, set()):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


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


def _site_kind(target: ast.expr, value: ast.expr, flag_statuses: Set[str]):
    if not isinstance(target, ast.Attribute):
        return None
    if target.attr == "status":
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "ExperimentStatus"
        ):
            return f"status={value.attr}"
        if _answers_a_flag_status(value, flag_statuses):
            return "flag status"
        return None
    if target.attr in ATTRIBUTES:
        return target.attr
    return None


def sites_in(rel: str, tree: ast.Module) -> Counter:
    """(module, function, kind) -> count, for one module."""
    found: Counter = Counter()
    flag_statuses = _flag_status_names(tree)

    class V(_Walker):
        def visit_Assign(self, node):
            for target in node.targets:
                kind = _site_kind(target, node.value, flag_statuses)
                if kind:
                    found[(rel, self.qualname, kind)] += 1
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if node.value:
                kind = _site_kind(node.target, node.value, flag_statuses)
                if kind:
                    found[(rel, self.qualname, kind)] += 1
            self.generic_visit(node)

        def visit_Call(self, node):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "User") or (
                isinstance(func, ast.Attribute) and func.attr == "User"
            ):
                found[(rel, self.qualname, "User()")] += 1
            statement = statement_kind(node)
            if statement:
                found[(rel, self.qualname, statement)] += 1
            self.generic_visit(node)

    V().visit(tree)
    return found


def dynamic_setattrs_in(rel: str, tree: ast.Module) -> Counter:
    """(module, function, object) -> count of the ``setattr`` and
    ``__setattr__`` calls whose attribute name is not written out."""
    found: Counter = Counter()

    class V(_Walker):
        def visit_Call(self, node):
            if _named(node.func) in ("setattr", "__setattr__") and node.args:
                # ``object.__setattr__(self, name, value)`` and the plain form.
                name = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                    found[(rel, self.qualname, ast.unparse(node.args[0]))] += 1
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
    assert sum(expected.values()) == 39


def test_every_dynamic_setattr_is_listed():
    """A ``setattr`` whose attribute is not written out can set a flag's or a
    side row's columns without being a site above: a loop over a change's
    items, for example. Every one in ``backend/app`` is listed with what it
    sets, and only ``update_feature_flag`` sets a flag's."""
    found: Counter = Counter()
    for rel, tree in _modules():
        found.update(dynamic_setattrs_in(rel, tree))
    expected = Counter({key: count for key, (count, _) in DYNAMIC_SETATTR.items()})
    assert found == expected, (
        f"unlisted or miscounted: "
        f"{sorted((k, n) for k, n in found.items() if expected.get(k) != n)}; "
        f"gone: {sorted(k for k in expected if k not in found)}"
    )
    assert all(what.strip() for _, what in DYNAMIC_SETATTR.values())


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
        "        db.execute(update(UserTOTP).values(failed_attempts=0))\n"
        "        db.execute(sa.delete(UserTOTP))\n"
        "        db.execute(pg_insert(UserTOTP).values(user_id=u))\n"
        "        UserTOTP(user_id=u)\n"
        "        db.execute(update(Other))\n"
        "        db.execute(select(UserTOTP))\n"
        "        f.targeting_rules = None\n"
        "        f.status = transition(f.status, FlagVerb.ON).value\n"
        "        db.execute(update(FlagEnvironmentSetting).values(is_active=False))\n"
        "        db.execute(models.delete(models.FlagEnvironmentSetting))\n"
        "        db.execute(pg_insert(FlagEnvironmentSetting).values(environment='x'))\n"
        "        FlagEnvironmentSetting(environment='staging')\n"
        "        db.execute(select(FlagEnvironmentSetting))\n"
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
                "user_totp update",
                "user_totp delete",
                "user_totp insert",
                "UserTOTP()",
                "targeting_rules",
                "flag status",
                "flag_environment_settings update",
                "flag_environment_settings delete",
                "flag_environment_settings insert",
                "FlagEnvironmentSetting()",
            )
        }
    )


@pytest.mark.parametrize(
    "line, found",
    [
        ("f.status = FeatureFlagStatus.ACTIVE", True),
        ("f.status = FeatureFlagStatus('ACTIVE').value", True),
        ("f.status = transition(f.status, FlagVerb.OFF)", True),
        ("new = transition(f.status, verb); f.status = new.value", True),
        ("f.status = 'x'", False),
        ("f.status = RolloutStageStatus.COMPLETED", False),
    ],
)
def test_the_scanner_finds_each_way_to_set_a_flags_status(line, found):
    sites = _scan(f"def tick(f, verb):\n    {line}\n")
    assert sites == (Counter({("x.py", "tick", "flag status"): 1}) if found else {})


@pytest.mark.parametrize(
    "line, listed",
    [
        ("setattr(flag, field, value)", "flag"),
        ("setattr(row, f'{name}_at', value)", "row"),
        ("object.__setattr__(flag, name, value)", "flag"),
        ("setattr(flag, 'status', value)", None),
    ],
)
def test_the_scanner_lists_a_setattr_whose_attribute_is_not_written_out(line, listed):
    source = f"def apply(flag, row, change):\n    for x in change:\n        {line}\n"
    found = dynamic_setattrs_in("x.py", ast.parse(source))
    assert found == (Counter({("x.py", "apply", listed): 1}) if listed else {})


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

"""No application code calls an ``async`` dependency as a function.

``deps.get_current_active_user``, ``deps.get_current_superuser`` and
``deps.get_current_superuser_or_none`` are ``async def``. Under ``Depends``
FastAPI awaits them; called as a plain function, each returns a coroutine
that has checked nothing, and nothing fails: a caller that discards the
result admits an inactive user, and one that keeps it treats the coroutine
as the user. Code that resolves the user itself calls the sync checks
``deps.require_active_user`` and ``deps.require_superuser`` instead.

The nine module factories made ``async`` with them (the warehouse routes'
seven, ``etl.get_etl_service`` and ``realtime_counters.get_counter_service``)
have the same trap: called as a plain function, each returns a coroutine in
place of the executor, factory, clock, connector set, estimator or service.

The walk parses every module under ``backend/app`` and, when present,
``modules/`` (tests excluded) and reports each call whose callee is one of the
twelve, by name or by attribute (``deps.get_current_active_user(...)``), and
through an ``import ... as`` alias. Passing one of them to ``Depends`` is not
a call of it and is not reported.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator, List, Tuple

import pytest

from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke]

#: The ``async`` user dependencies that must never be called directly.
ASYNC_USER_DEPENDENCIES = frozenset(
    {
        "get_current_active_user",
        "get_current_superuser",
        "get_current_superuser_or_none",
    }
)

#: The ``async`` module factories, named here as strings: a core file never
#: imports a module.
ASYNC_FACTORIES = frozenset(
    {
        "get_warehouse_executor",
        "get_client_factory",
        "get_job_session_factory",
        "get_clock",
        "get_enabled_connectors",
        "get_estimator",
        "get_mean_estimator",
        "get_etl_service",
        "get_counter_service",
    }
)

ASYNC_DEPENDENCIES = ASYNC_USER_DEPENDENCIES | ASYNC_FACTORIES

#: Where application code lives; ``modules`` is absent from a core build.
ROOTS = ("backend/app", "modules")


def _sources() -> Iterator[pathlib.Path]:
    for root in ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "tests" in path.relative_to(REPO_ROOT).parts:
                continue
            yield path


def direct_calls(source: str, filename: str = "<string>") -> List[Tuple[int, str]]:
    """``(line, name)`` for every direct call of an async dependency."""
    tree = ast.parse(source, filename=filename)
    names = set(ASYNC_DEPENDENCIES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in ASYNC_DEPENDENCIES and alias.asname:
                    names.add(alias.asname)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id in names:
            found.append((node.lineno, callee.id))
        elif isinstance(callee, ast.Attribute) and callee.attr in names:
            found.append((node.lineno, callee.attr))
    return found


def test_the_walk_reads_the_application():
    """Guard the guard: the walk sees the modules that use the dependencies."""
    sources = list(_sources())
    assert len(sources) > 100
    deps_source = (REPO_ROOT / "backend/app/api/deps.py").read_text()
    assert "async def get_current_active_user(" in deps_source
    if (REPO_ROOT / "modules").is_dir():
        defined = {
            name
            for path in sources
            for name in ASYNC_FACTORIES
            if f"async def {name}(" in path.read_text()
        }
        assert defined == ASYNC_FACTORIES, sorted(ASYNC_FACTORIES - defined)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("deps.get_current_active_user(user)", [(1, "get_current_active_user")]),
        ("get_current_superuser(current_user=u)", [(1, "get_current_superuser")]),
        (
            "from backend.app.api.deps import get_current_superuser_or_none as su\n"
            "su(user)",
            [(2, "su")],
        ),
        ("x = Depends(deps.get_current_active_user)", []),
        ("deps.require_active_user(deps.get_current_user(token=t, db=db))", []),
        ("executor = get_warehouse_executor()", [(1, "get_warehouse_executor")]),
        ("service = etl.get_etl_service()", [(1, "get_etl_service")]),
        ("clock: Any = Depends(get_clock)", []),
    ],
    ids=[
        "attribute",
        "name",
        "alias",
        "depends",
        "the-sync-check",
        "factory-name",
        "factory-attribute",
        "factory-depends",
    ],
)
def test_the_walk_reports_a_direct_call(source, expected):
    assert direct_calls(source) == expected


def test_no_direct_call_of_an_async_user_dependency():
    found = []
    for path in _sources():
        for line, name in direct_calls(path.read_text(), str(path)):
            found.append(f"{path.relative_to(REPO_ROOT)}:{line}: {name}(...)")
    assert not found, (
        "these call an async dependency as a function, which returns a "
        "coroutine and checks or builds nothing; for the user checks call "
        "deps.require_active_user or deps.require_superuser, and take a factory "
        "through Depends:\n" + "\n".join(found)
    )

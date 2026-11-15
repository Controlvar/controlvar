"""Nothing caches a feature flag's detail or list (#630).

The opt-in flag cache (#100) was deleted rather than repaired: five writers
outside the flag routes changed flags without dropping its entries, so the
detail and list served stale values for up to an hour. Every flag read now
comes from the database. This pins that, across the core and the modules:

* no source file under ``backend/app`` or ``modules/backend/app`` writes or
  reads a ``feature_flag:``/``feature_flags:`` key (a quoted literal starting
  with either prefix -- f-strings and ``scan_iter`` patterns included);
* ``deps.get_cache_control`` -- the dependency that hands out the Redis
  client -- is referenced only by the files that still cache (the experiment
  routes, ``experiment_cache.py`` that drops an experiment's cached detail
  for the writers outside them, and the admin cache routes) and by
  ``deps.py``, which defines it;
* no route under ``/api/v1/feature-flags`` depends on it.

No exception: ``admin.py``'s ``CACHE_NAMESPACES`` has no flag entry.

The source scan walks the files that exist, so a core build (no ``modules/``)
checks the core; it reads no git state, so it holds in a non-repository copy.
The route check is ``test_flag_routes_do_not_depend_on_the_cache`` and the
regression test for the stale reads is
``backend/tests/integration/api/test_flag_reads_after_writers.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List

import pytest

from backend.app.api import deps
from backend.app.main import app
from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

SOURCE_ROOTS = ("backend/app", "modules/backend/app")

# A quoted string that starts with a flag cache key prefix, in any string
# prefix form: "feature_flag:{id}", f"feature_flags:{user}", 'feature_flags:*'.
FLAG_KEY = re.compile(r"""["']feature_flags?:""")

# The only files that may name get_cache_control (paths from the repo root).
CACHE_CONTROL_USERS = frozenset(
    {
        "backend/app/api/deps.py",  # defines it
        "backend/app/api/v1/endpoints/experiments.py",  # the experiment cache
        "backend/app/api/experiment_cache.py",  # drops an experiment's detail
        "backend/app/api/v1/endpoints/admin.py",  # /admin/stats, /cache/clear
    }
)

FLAG_PREFIX = "/api/v1/feature-flags"


def _sources() -> Iterator[Path]:
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_the_scan_sees_the_flag_routes():
    """Guards the scans below: an empty walk would pass them."""
    found = {_rel(p) for p in _sources()}
    assert "backend/app/api/v1/endpoints/feature_flags.py" in found
    assert CACHE_CONTROL_USERS <= found


def test_no_source_names_a_flag_cache_key():
    hits: List[str] = []
    for path in _sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in zip(range(1, len(lines) + 1), lines):
            if FLAG_KEY.search(line):
                hits.append(f"{_rel(path)}:{number}: {line.strip()}")
    assert not hits, (
        "a feature-flag cache key is back; the flag cache was deleted (#630) "
        "because writers outside the flag routes leave it stale:\n" + "\n".join(hits)
    )


def test_only_the_caching_routes_name_get_cache_control():
    hits = sorted(
        _rel(path)
        for path in _sources()
        if "get_cache_control" in path.read_text(encoding="utf-8")
        and _rel(path) not in CACHE_CONTROL_USERS
    )
    assert not hits, (
        f"{hits} use deps.get_cache_control; the flag routes read the database "
        "only (#630)"
    )


def _contexts():
    """(path, route context) for every HTTP route, flattened (FastAPI keeps
    ``include_router`` lazy; see test_flag_change_routes_guarded.py)."""
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            if getattr(ctx, "methods", None):
                yield ctx.path, ctx


def _calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def test_flag_routes_do_not_depend_on_the_cache():
    flag_routes = [(p, c) for p, c in _contexts() if p.startswith(FLAG_PREFIX)]
    assert len(flag_routes) > 10, "the route walk found no flag routes"
    offenders = sorted(
        path
        for path, ctx in flag_routes
        if deps.get_cache_control in set(_calls(ctx.dependant))
    )
    assert not offenders, f"flag routes depend on get_cache_control: {offenders}"

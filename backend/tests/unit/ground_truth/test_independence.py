"""G4: the ground-truth library imports nothing but the standard library, scipy
and statsmodels.

The library computes the answers the product is checked against. If it
imported the product (``backend``, ``modules``), the docs runner it is
cross-checked with, or an HTTP library (the standard library's ``urllib.request``,
``http.client`` and ``socket`` included), a defect could be shared rather than
caught. Two checks, because each misses what the other sees:

* (a) a static scan of every module's imports, ``importlib.import_module`` and
  ``__import__`` with a literal name included, naming the file and line;
* (b) a fresh ``python -I`` that refuses those packages from a meta-path
  finder, then imports every module and computes one reference and one flag
  bucket (an import inside a function runs only when it is called). It has to be
  a fresh process: in this one ``backend`` is imported already, so a blocker
  here would refuse nothing.

Adding ``from backend.app.core.consistent_hash import bucket_of`` to
``generator.py`` fails both.
"""

from __future__ import annotations

import ast
import subprocess
import sys

import pytest

from backend.tests.unit.ground_truth.library import LIBRARY, LIBRARY_ROOT

pytestmark = pytest.mark.unit

#: Every module of the library, a future subpackage's included.
MODULES = sorted(LIBRARY.rglob("*.py"))
ALLOWED = set(sys.stdlib_module_names) | {"scipy", "statsmodels", "ground_truth"}
#: The standard library's own network modules: allowed as "the standard
#: library", but the library is no HTTP client, so named here.
NETWORK = ("urllib.request", "http.client", "socket")
#: What the fresh interpreter refuses, beside anything (a) would name. A dotted
#: name is refused with what is under it.
BLOCKED = (
    "backend",
    "modules",
    "docs_runner",
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
    "pytest",
    *NETWORK,
)


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.append((node.lineno, node.module or ""))
            # `from urllib import request` imports urllib.request
            found.extend(
                (node.lineno, f"{node.module}.{alias.name}")
                for alias in node.names
                if node.module
            )
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            first = node.args[0]
            if name in ("import_module", "__import__"):
                literal = first.value if isinstance(first, ast.Constant) else None
                found.append((node.lineno, str(literal)))
    return found


def _is_network(name: str) -> bool:
    return any(name == n or name.startswith(n + ".") for n in NETWORK)


def test_the_scan_reads_every_module():
    assert {path.stem for path in MODULES} == {
        "__init__",
        "hashing",
        "generator",
        "ledger",
        "contract",
        "reference",
        "faults",
        "seed",
        "targeting",
    }


def test_the_library_imports_only_the_standard_library_scipy_and_statsmodels():
    offenders = [
        f"{path.relative_to(LIBRARY)}:{line} imports {name}"
        for path in MODULES
        for line, name in _imports(ast.parse(path.read_text(encoding="utf-8")))
        if name.split(".")[0] not in ALLOWED or _is_network(name)
    ]
    assert offenders == []


_SCRIPT = """
import importlib.abc
import io
import sys

BLOCKED = {blocked!r}


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in BLOCKED):
            raise ImportError("the ground-truth library may not import " + name)
        return None


sys.meta_path.insert(0, Refuse())
sys.path.insert(0, {root!r})

from ground_truth import contract, faults, generator, hashing, ledger, reference, seed, targeting

assert seed.SEED >= 0
scenario = generator.Scenario(
    name="g4", experiment_key="gt-g4",
    arms=(generator.Arm("control", 50, 0.5), generator.Arm("treatment", 50, 0.5)),
    users=20, event_name="purchase",
    metrics=(generator.Metric("buyers", "purchase", "conversion"),),
)
plan = generator.simulate(1, scenario)
assert faults.inject(plan).digest() == plan.digest()
arms = plan.allocations()
users = [f"gt-g4-{{n}}" for n in range(40)]
lines = [ledger.dumps(r) for r in [
    {{"kind": "start", "source": "g4", "seed": 1, "flags": [], "planted": [],
      "declared": {{"event_dedup": True}},
      "experiments": [{{"exp": "gt-g4", "status": "active",
                        "variants": [{{"name": n, "allocation": a}} for n, a in arms]}}]}},
    *[{{"kind": "assign", "user": u, "exp": "gt-g4", "status": 200, "assigned": True,
        "variant": hashing.variant_for(u, "gt-g4", arms), "reason": "assigned",
        "attempts": 1, "route": "/assign"}} for u in users],
    *[{{"kind": "event", "user": u, "exp": "gt-g4", "flag": None,
        "event_type": "purchase", "event_name": None, "value": None, "status": 200,
        "attempts": 1, "route": "/track", "event_id": None}} for u in users[::3]],
    {{"kind": "end", "errors": [], "counts": {{"assign": 40, "flag": 0,
      "event": 14, "batch": 0, "alias": 0, "action": 0, "429": 0}}}},
]]
expected = contract.valid_events(ledger.read(lines)).experiments["gt-g4"]
comparison = reference.compare(
    {{v: expected.per_user(v, "purchase", "conversion") for v in expected.variants}},
    reference.ANALYSIS["conversion"], "benjamini_hochberg", 0.95,
)
srm = reference.srm_p([expected.sample_size(v) for v in expected.variants], [50, 50])
flag = hashing.flag_bucket("user-123", "my-flag")
leaked = sorted(
    m for m in sys.modules
    if any(m == b or m.startswith(b + ".") for b in BLOCKED)
)
print("leaked", leaked)
print("independent", len(plan.ops), comparison[0].p_value, srm)
print("flag bucket", flag)
"""


def test_a_fresh_interpreter_that_refuses_the_product_runs_the_library():
    script = _SCRIPT.format(blocked=set(BLOCKED), root=str(LIBRARY_ROOT))
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "leaked []" in result.stdout, result.stdout
    assert "independent" in result.stdout, result.stdout
    assert "flag bucket 79" in result.stdout, result.stdout

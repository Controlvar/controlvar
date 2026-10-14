"""Every test the claims registries name is one the release candidate runs,
and runs without skipping.

``scripts/rc_evidence.py`` fails a ``test:`` target that no suite in its
``SUITES`` runs, by name, at the release (#1142), and one that was skipped.
This holds the registries (``docs/_claims``) to the same rules on the pull
request that adds the target, which is a docs-only change: a target in a tree
no RC suite runs, or in a module that skips unless an environment variable has
a value its RC session does not give it, is refused here, not found at the
release. A target in another SDK's tree is held by that SDK's job in
sdk-unit-tests.yml instead, as the verifier holds it.

Reads files only (the two scripts are loaded from the tree, the test modules
are parsed), so it runs the same in the docs-only lane and in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib.util
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
CHART_RENDER = "backend/tests/unit/chart/test_chart_render.py"


def _verifier():
    spec = importlib.util.spec_from_file_location(
        "rc_evidence_for_registry_test", REPO_ROOT / "scripts" / "rc_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _verifier()


@functools.lru_cache(maxsize=1)
def _registry_targets() -> Tuple[str, ...]:
    """Every ``test:`` target of the registries (read once: it parses them all)."""
    return tuple(target for target, _ in rc.read_registry(REPO_ROOT).targets)


def test_every_registry_target_is_run_by_a_release_candidate_suite():
    targets = _registry_targets()
    assert targets, "the registries name no test: this checked nothing"
    outside = sorted(
        t for t in targets if rc.sdk_job_of(t) is None and rc.suite_of(t) is None
    )
    assert not outside, (
        "no suite in scripts/rc_evidence.py's SUITES runs these, so the release"
        " candidate's evidence would fail them; add the tree to a suite (and to"
        f" rc-evidence.yml), or name a test a suite runs: {outside}"
    )


def test_the_rule_refuses_a_target_outside_every_suite():
    assert rc.suite_of("test:charts/experimently/tests/test_values.py::test_x") is None
    assert (
        rc.sdk_job_of("test:charts/experimently/tests/test_values.py::test_x") is None
    )
    assert rc.suite_of("test:infrastructure/tests/test_redis_sizing.py::test_x")
    assert rc.sdk_job_of("test:sdk/go/client_test.go::TestX") == "go"
    # A tree a session ignores is not run by it: the chart's render tests are
    # under unit's root, and run by the chart suite.
    assert rc.suite_of(f"test:{CHART_RENDER}::test_x") == "chart"
    assert rc.suite_of("test:backend/tests/unit/core/test_x.py::test_x") == "unit"


# ---------------------------------------------------------------------------
# environment gates
# ---------------------------------------------------------------------------
def _environment_read(node: ast.AST) -> Optional[str]:
    """The variable *node* reads: ``os.environ.get("N"...)``,
    ``os.getenv("N"...)`` or ``os.environ["N"]``; None for anything else."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        first = node.args[0] if node.args else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            return None
        owner = node.func.value
        if node.func.attr == "getenv" or (
            node.func.attr == "get"
            and isinstance(owner, ast.Attribute)
            and owner.attr == "environ"
        ):
            return first.value
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def environment_gates(source: str) -> Dict[str, str]:
    """``{NAME: VALUE}`` for a test module that skips (``skipif`` or
    ``pytest.skip``) and compares an environment variable to a string
    (``os.environ.get("NAME") == "VALUE"``, ``!=``, ``os.getenv``,
    ``os.environ["NAME"]``): its tests can skip unless NAME is VALUE.

    Deliberately broad: it does not follow the comparison to the skip, so a
    module whose comparison gates something else is held too, and fails
    closed. The chart's render tests are this shape
    (``EXPERIMENTLY_CHART_TESTS``), as are the Redis-backed integration tests,
    which fail rather than skip under ``EXPERIMENTLY_REQUIRE_REDIS=1``. A gate
    of another shape still fails at the release, where a skipped target is
    refused; a ``benchmark`` marker is refused by ``scripts/doc_claims.py``.
    """
    tree = ast.parse(source)
    skips = any(
        isinstance(node, ast.Attribute)
        and (
            node.attr == "skipif"
            or (
                node.attr == "skip"
                and isinstance(node.value, ast.Name)
                and node.value.id == "pytest"
            )
        )
        for node in ast.walk(tree)
    )
    gates: Dict[str, str] = {}
    if not skips:
        return gates
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
        ):
            continue
        pair = (node.left, node.comparators[0])
        for read, value in (pair, pair[::-1]):
            name = _environment_read(read)
            if (
                name is not None
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                gates[name] = value.value
    return gates


@dataclasses.dataclass(frozen=True)
class Gated:
    target: str
    suite: str
    report: str
    name: str
    value: str

    def __str__(self) -> str:
        return (
            f"{self.target}: its module skips unless {self.name}={self.value},"
            f" which suite {self.suite} ({self.report}) does not set"
        )


@functools.lru_cache(maxsize=None)
def _gates_of(path: Path) -> Dict[str, str]:
    return environment_gates(path.read_text(encoding="utf-8"))


def ungated(targets: Sequence[str], root: Path = REPO_ROOT) -> List[Gated]:
    """Each target whose module gates on a variable its RC session does not
    set to that value: it would only skip at the release."""
    found: List[Gated] = []
    for target in targets:
        session = rc.session_of(target)
        match = rc.TEST_TARGET.fullmatch(target)
        if session is None or match is None or not match.group("path").endswith(".py"):
            continue  # outside every suite: the first test names it
        path = root / match.group("path")
        if not path.is_file():
            continue  # a modules/ target in a core copy; doc_claims checks it
        suite, invocation = session
        env = dict(invocation.env)
        for name, value in _gates_of(path).items():
            if env.get(name) != value:
                found.append(Gated(target, suite, invocation.report, name, value))
    return found


def test_no_registry_target_would_only_skip_in_its_release_candidate_suite():
    targets = _registry_targets()
    problems = ungated(targets)
    assert not problems, (
        "these targets skip unless a variable is set that their suite in"
        " scripts/rc_evidence.py's SUITES does not set, so the release"
        " candidate's evidence would fail them as skipped; set it in that"
        " session's env (and install what it needs in rc-evidence.yml), or name"
        " a test that runs there:\n" + "\n".join(map(str, problems))
    )
    # Not vacuous: the registries name the chart's render tests, which gate.
    assert any(t.startswith(f"test:{CHART_RENDER}") for t in targets)
    source = (REPO_ROOT / CHART_RENDER).read_text(encoding="utf-8")
    assert environment_gates(source) == {"EXPERIMENTLY_CHART_TESTS": "1"}


def _without(name: str, variable: str):
    """SUITES with *variable* dropped from every session of suite *name*."""
    suites = dict(rc.SUITES)
    suites[name] = tuple(
        dataclasses.replace(i, env=tuple(kv for kv in i.env if kv[0] != variable))
        for i in suites[name]
    )
    return suites


def _unit_runs_the_chart():
    """SUITES as before the chart suite: unit collects backend/tests/unit/chart."""
    suites = {k: v for k, v in rc.SUITES.items() if k != "chart"}
    first, *rest = suites["unit"]
    args = tuple(a for a in first.args if not a.startswith("--ignore="))
    suites["unit"] = (dataclasses.replace(first, args=args), *rest)
    return suites


@pytest.mark.parametrize(
    "suites, suite, variable",
    [
        (
            lambda: _without("chart", "EXPERIMENTLY_CHART_TESTS"),
            "chart",
            "EXPERIMENTLY_CHART_TESTS",
        ),
        (_unit_runs_the_chart, "unit", "EXPERIMENTLY_CHART_TESTS"),
        (
            lambda: _without("integration", "EXPERIMENTLY_REQUIRE_REDIS"),
            "integration",
            "EXPERIMENTLY_REQUIRE_REDIS",
        ),
    ],
    ids=[
        "chart-without-the-variable",
        "chart-tests-back-in-unit",
        "integration-without-redis-required",
    ],
)
def test_the_rule_fires_when_a_session_loses_the_variable(
    monkeypatch, suites, suite, variable
):
    monkeypatch.setattr(rc, "SUITES", suites())
    problems = ungated(_registry_targets())
    assert problems, "the gate check found nothing to refuse"
    assert {(p.suite, p.name) for p in problems} == {(suite, variable)}


def test_the_gate_reader_reads_each_shape_and_ignores_a_module_that_never_skips():
    skips = "import os, pytest\npytestmark = pytest.mark.skipif(True, reason='x')\n"
    for compare in (
        'os.environ.get("X") == "1"',
        '"1" == os.environ.get("X", "0")',
        'os.getenv("X") != "1"',
        'os.environ["X"] == "1"',
    ):
        assert environment_gates(skips + f"ON = {compare}\n") == {"X": "1"}, compare
    assert environment_gates("import os\nON = os.environ.get('X') == '1'\n") == {}
    assert environment_gates(skips + "PATH = os.environ.get('PATH', '')\n") == {}
    fixture = (
        "import os, pytest\n\ndef f():\n"
        "    if os.environ.get('R') == '1':\n        pytest.fail('x')\n"
        "    pytest.skip('x')\n"
    )
    assert environment_gates(fixture) == {"R": "1"}

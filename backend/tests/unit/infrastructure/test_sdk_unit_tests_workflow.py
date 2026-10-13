"""Which SDK suites ``sdk-unit-tests.yml`` runs, on what, and when.

The SDK pages say where each SDK's own tests run: the Linux tier on the pull
requests that touch an SDK, with js, python and react on every one; elixir,
flutter, android and ios nightly and when touched; the Python suites on
Python 3.11. These read the workflow, including the Python block that builds
its matrix (read with ``ast``, not run), so those sentences have something to
fail when the workflow changes.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sdk-unit-tests.yml"

#: A distribution that does not ship the workflows has nothing here to check.
if not WORKFLOW.is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)


def workflow() -> Dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def matrix_constants() -> Dict[str, Any]:
    """LINUX, HEAVY and ALWAYS from the 'Build the matrix' step's Python block."""
    steps = {s.get("name"): s for s in workflow()["jobs"]["changes"]["steps"]}
    script = steps["Build the matrix"]["run"]
    body = re.search(r"<<'PY'[^\n]*\n(?P<body>.*?)\n\s*PY\s*$", script, re.S)
    assert body, "the step no longer runs a python heredoc"
    source = textwrap.dedent(body["body"])
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("LINUX", "HEAVY", "ALWAYS"):
                found[node.targets[0].id] = ast.literal_eval(node.value)
    assert set(found) == {"LINUX", "HEAVY", "ALWAYS"}, found
    return found


def test_the_linux_tier_runs_these_sdks():
    assert sorted(matrix_constants()["LINUX"]) == sorted(
        [
            "js",
            "react",
            "react-native",
            "edge",
            "openfeature",
            "python",
            "openfeature-python",
            "go",
            "java",
            "ruby",
            "php",
            "dotnet",
        ]
    )


def test_js_python_and_react_run_on_every_pull_request():
    assert sorted(matrix_constants()["ALWAYS"]) == ["js", "python", "react"]
    triggers = workflow()[True]  # YAML 1.1 reads the key `on` as True
    assert "pull_request" in triggers


def test_elixir_flutter_android_and_ios_run_nightly_and_when_touched():
    constants = matrix_constants()
    assert constants["HEAVY"] == ["elixir", "flutter", "android", "ios"]
    assert workflow()[True]["schedule"], "no nightly schedule"
    jobs = workflow()["jobs"]
    for name in constants["HEAVY"]:
        assert jobs[name]["if"] == f"needs.changes.outputs.{name} == 'true'", name


def test_the_python_suites_run_on_python_3_11():
    steps = {s.get("name"): s for s in workflow()["jobs"]["linux"]["steps"]}
    assert steps["Set up Python"]["with"]["python-version"] == "3.11"
    run = steps["Run unit tests"]["run"]
    assert "python -m pytest sdk/python/tests" in run
    assert "python -m pytest sdk/openfeature-python/tests" in run


def test_the_js_sdk_runs_jest():
    run = {s.get("name"): s for s in workflow()["jobs"]["linux"]["steps"]}[
        "Run unit tests"
    ]["run"]
    assert re.search(
        r"js\|react\|react-native\|edge\|openfeature\)\s*\n\s*cd \"sdk/\$SDK\"", run
    )
    assert "npm test -- --ci" in run

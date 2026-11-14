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
            "mcp",
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


def test_the_python_packages_are_built_checked_and_installed_on_their_oldest_python():
    """#358 item 4: a change to [build-system] or the extras is exercised."""
    steps = {s.get("name"): s for s in workflow()["jobs"]["linux"]["steps"]}
    build = steps["Build and twine-check the package"]
    assert build["if"] == "matrix.lang == 'python'"
    assert "python -m build" in build["run"]
    assert "twine check --strict" in build["run"]
    # the sibling wheel openfeature-python depends on is built too
    assert "sdk/python" in build["run"]

    oldest = steps["Set up the oldest supported Python"]
    assert oldest["if"] == "matrix.lang == 'python'"
    assert oldest["uses"].startswith("actions/setup-python@")
    # the oldest each package declares: 3.9 for python, 3.10 for openfeature-python
    assert oldest["with"]["python-version"] == (
        "${{ matrix.sdk == 'python' && '3.9' || '3.10' }}"
    )
    for sdk, floor in (("python", "3.9"), ("openfeature-python", "3.10")):
        pyproject = REPO_ROOT / "sdk" / sdk / "pyproject.toml"
        assert f'requires-python = ">={floor}"' in pyproject.read_text(
            encoding="utf-8"
        ), f"sdk/{sdk} no longer declares >={floor}: update the matrix expression"

    # CPython 3.9 is not in actions/python-versions for Ubuntu 26.04
    # (ubuntu-latest): the python leg, whose floor is 3.9, must run on 24.04.
    assert workflow()["jobs"]["linux"]["runs-on"] == (
        "${{ matrix.sdk == 'python' && 'ubuntu-24.04' || 'ubuntu-latest' }}"
    )

    install = steps["Install the wheel on the oldest Python"]
    assert install["if"] == "matrix.lang == 'python'"
    assert "OLDEST_PYTHON" in install["env"]
    assert "steps.oldest.outputs.python-path" in install["env"]["OLDEST_PYTHON"]
    assert '"$OLDEST_PYTHON" -m venv' in install["run"]
    assert "mod=controlvar_openfeature" in install["run"]
    assert "mod=controlvar\n" in install["run"]
    run = install["run"]
    # the local sibling wheel goes in BEFORE the openfeature wheel (PyPI has the
    # same 0.3.0 and would otherwise satisfy the pin), and the step proves it
    local = 'pip install "$out"/python/*.whl'
    final = '"$wheel"'
    assert local in run, "the local controlvar wheel is no longer installed first"
    assert run.index(local) < run.index(final), "the sibling wheel must come first"
    assert "direct_url.json" in run and 'startswith("file://")' in run, (
        "nothing proves controlvar came from the local wheel"
    )
    assert run.index("direct_url.json") < run.index(final)
    # the import must come from the venv
    assert 'case "$f" in' in run and '"$venv"/*)' in run
    assert "import $mod" in run


def test_the_js_sdk_runs_jest():
    run = {s.get("name"): s for s in workflow()["jobs"]["linux"]["steps"]}[
        "Run unit tests"
    ]["run"]
    assert re.search(
        r"js\|react\|react-native\|edge\|openfeature\)\s*\n\s*cd \"sdk/\$SDK\"", run
    )
    assert "npm test -- --ci" in run


def test_the_mcp_server_runs_its_own_suite():
    """sdk/mcp's leg installs from its lock and runs its whole suite: `npm
    test` builds dist/ and runs jest, with no fallback install."""
    run = {s.get("name"): s for s in workflow()["jobs"]["linux"]["steps"]}[
        "Run unit tests"
    ]["run"]
    leg = re.search(r"\n\s*mcp\)\n(?P<body>.*?)\n\s*;;", run, re.S)
    assert leg, "sdk-unit-tests.yml has no mcp) case"
    commands = [
        line.strip()
        for line in leg["body"].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert commands == [
        "cd sdk/mcp",
        "npm ci --no-audit --no-fund",
        "npm test -- --ci",
    ], commands
    assert matrix_constants()["LINUX"]["mcp"] == "node"


def sdk_directories() -> set:
    """Every SDK in the tree: a directory under ``sdk/``, as
    ``backend/tests/smoke/test_sdk_release_wiring.py`` counts them (a name
    starting with a dot or an underscore is tooling, ``sdk/LICENSE`` a file)."""
    return {
        entry.name
        for entry in (REPO_ROOT / "sdk").iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    }


def path_filters() -> Dict[str, list]:
    """key -> flat list of patterns, from the 'Filter changed paths' step.

    The filters are a YAML string whose anchor (``*all``) nests a list, so the
    parse is flattened."""
    steps = {s.get("name"): s for s in workflow()["jobs"]["changes"]["steps"]}
    filters = yaml.safe_load(steps["Filter changed paths"]["with"]["filters"])

    def flat(value):
        if isinstance(value, list):
            return [leaf for item in value for leaf in flat(item)]
        return [value]

    return {key: flat(patterns) for key, patterns in filters.items()}


def test_every_sdk_directory_runs_somewhere():
    constants = matrix_constants()
    runs = set(constants["LINUX"]) | set(constants["HEAVY"])
    directories = sdk_directories()
    for name in sorted(directories - runs):
        pytest.fail(f"sdk/{name} exists but sdk-unit-tests.yml runs no suite for it")
    for name in sorted(runs - directories):
        pytest.fail(f"sdk-unit-tests.yml runs {name}, which is not a directory in sdk/")
    filters = path_filters()
    for name in sorted(runs):
        assert f"sdk/{name}/**" in filters.get(name, []), (
            f"sdk-unit-tests.yml has no paths-filter key for sdk/{name}/**"
        )

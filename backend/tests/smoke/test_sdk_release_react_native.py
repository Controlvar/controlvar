"""The React Native package's first release (Plan P, P1-RN), and where its app
check runs (EMP-P-8).

The package: 0.1.0 in the manifest and the lock's own fields (R1a), its
TypeScript source as ``types``, and ``@types/md5`` as a dependency.

``scripts/check_react_native_app.py`` installs the packed React Native SDK
into a TypeScript React Native app already on each AsyncStorage major the
peer range allows, and requires the app to keep its one AsyncStorage and to
type-check the SDK's source. It runs in two places:

* sdk-release.yml's ``npm-build``, for react-native only, on the tarball the
  job records, after the README gate and before the tarball is recorded, so
  a release whose package an app on 1.x, 2.x or 3.x cannot type-check stops
  before the environment's approval is asked for;
* sdk-unit-tests.yml's react-native leg, on a tarball packed the way the
  release packs it, so the pull request that breaks it fails, not a tag.

The same leg runs the SDK's own two type checks (``npm run typecheck``, the
source; ``npm run typecheck:tests``, the tests and both AsyncStorage mocks).

The steps run here the way the runner runs them, through
test_sdk_release_wiring.py's ``_run_step``, with the fake ``npm`` (and the
``tsc`` it installs) of test_check_react_native_app.py; the plant (``tsc``
failing under 3.x) changes only that fake.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import List

import pytest
import yaml

from backend.tests.smoke.test_sdk_release_wiring import (
    ROOT,
    _run_of,
    _run_step,
    _step,
    _workflow,
)
from backend.tests.unit.scripts.test_check_react_native_app import fake_npm

pytestmark = pytest.mark.smoke

SDK = ROOT / "sdk" / "react-native"
CHECK = "scripts/check_react_native_app.py"
STEP = "A TypeScript app type-checks the packed SDK"
UNIT_TESTS = ROOT / ".github" / "workflows" / "sdk-unit-tests.yml"


def _labels(job: str) -> List[str]:
    return [s.get("name") or s.get("uses") for s in _workflow()["jobs"][job]["steps"]]


def _unit_steps() -> list:
    return yaml.safe_load(UNIT_TESTS.read_text(encoding="utf-8"))["jobs"]["linux"][
        "steps"
    ]


def _unit_step(name: str) -> dict:
    steps = [s for s in _unit_steps() if s.get("name") == name]
    assert len(steps) == 1, (name, len(steps))
    return steps[0]


# --------------------------------------------------------------------------
# The package
# --------------------------------------------------------------------------


def _manifest() -> dict:
    return json.loads((SDK / "package.json").read_text(encoding="utf-8"))


@pytest.mark.regression
def test_the_react_native_package_is_first_published_at_0_1_0() -> None:
    """R1a for React Native: its first release is 0.1.0, a 0.x beta, in the
    manifest and the lock's own two fields. Edit this pin in the pull request
    that bumps the version after 0.1.0 is on npm."""
    lock = json.loads((SDK / "package-lock.json").read_text(encoding="utf-8"))
    assert _manifest()["name"] == "@controlvar/react-native-sdk"
    assert _manifest()["version"] == "0.1.0"
    assert (lock["version"], lock["packages"][""]["version"]) == ("0.1.0", "0.1.0")


@pytest.mark.regression
def test_the_package_ships_its_typescript_source_and_the_md5_types() -> None:
    """The tarball is `src` alone and `types` is its TypeScript entry, so an
    app's tsc reads the SDK's source; `@types/md5` is a dependency, so that
    tsc finds the declarations of `md5`, which the source imports (as a
    devDependency it failed every TypeScript app with TS7016)."""
    manifest = _manifest()
    assert manifest["files"] == ["src"]
    assert manifest["main"] == manifest["types"] == "src/index.ts"
    assert "md5" in manifest["dependencies"]
    assert "@types/md5" in manifest["dependencies"]
    assert "@types/md5" not in manifest.get("devDependencies", {})
    imports = (SDK / "src" / "hash.ts").read_text(encoding="utf-8")
    assert re.search(r"^import md5 from 'md5';$", imports, re.M)


# --------------------------------------------------------------------------
# The steps, as written
# --------------------------------------------------------------------------


def test_npm_build_runs_the_app_check_on_the_react_native_tarball_it_records() -> None:
    jobs = _workflow()["jobs"]
    running = sorted(
        (name, step.get("name"))
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if CHECK in _run_of(step)
    )
    assert running == [("npm-build", STEP)], running
    step = _step("npm-build", STEP)
    assert step.get("if") == "needs.resolve.outputs.sdk == 'react-native'"
    assert step.get("env") == {"TGZ": "${{ steps.pack.outputs.tgz }}"}
    assert _run_of(step) == f'python3 {CHECK} "$TGZ"'
    assert "continue-on-error" not in step
    labels = _labels("npm-build")
    assert (
        labels.index("Check the packed README")
        < labels.index(STEP)
        < labels.index("Record the tarball")
    ), labels


def test_the_react_native_leg_packs_the_sdk_and_runs_the_app_check() -> None:
    running = [s.get("name") for s in _unit_steps() if CHECK in _run_of(s)]
    assert running == [STEP], running
    step = _unit_step(STEP)
    assert step.get("if") == "matrix.sdk == 'react-native'"
    assert "continue-on-error" not in step
    lines = [line.strip() for line in _run_of(step).splitlines() if line.strip()]
    # The check's exit status is the step's: `set -e` is on and it is the
    # last line, with nothing after it.
    assert lines[0] == "set -euo pipefail", lines
    assert lines[-1] == f'python3 {CHECK} "$out"/*.tgz', lines
    assert '(cd sdk/react-native && npm pack --pack-destination "$out")' in lines


def test_the_react_native_leg_runs_both_of_its_type_checks() -> None:
    run = _run_of(_unit_step("Run unit tests"))
    block = run.split('if [ "$SDK" = "react-native" ]; then', 1)[1].split("fi", 1)[0]
    assert [line.strip() for line in block.strip().splitlines()] == [
        "npm run typecheck",
        "npm run typecheck:tests",
    ]
    scripts = json.loads((ROOT / "sdk" / "react-native" / "package.json").read_text())[
        "scripts"
    ]
    assert scripts["typecheck"] == "tsc --noEmit"
    assert scripts["typecheck:tests"] == "tsc --noEmit -p tsconfig.jest.json"
    jest_config = json.loads(
        (ROOT / "sdk" / "react-native" / "tsconfig.jest.json").read_text()
    )
    assert "__mocks__/**/*" in jest_config["include"]


# --------------------------------------------------------------------------
# The steps, as the runner runs them
# --------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    """A checkout holding a copy of the check, and sdk/react-native (the
    tree's, read only), whose lock the check reads."""
    workspace = tmp_path / "ws"
    (workspace / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / CHECK, workspace / CHECK)
    (workspace / "sdk").mkdir()
    (workspace / "sdk" / "react-native").symlink_to(ROOT / "sdk" / "react-native")
    return workspace


def _runner_env(tmp_path: Path, tsc_fails: str = "") -> dict:
    fake_npm(tmp_path / "bin")
    return {
        "FAKE_NPM_LOG": str(tmp_path / "npm.log"),
        "FAKE_NPM_PLANT": "",
        "FAKE_TSC_FAILS": tsc_fails,
    }


@pytest.mark.parametrize(("tsc_fails", "status"), [("", 0), ("3", 1)])
def test_npm_build_s_step_passes_and_fails_with_the_check(
    tsc_fails: str, status: int, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    tgz = tmp_path / "controlvar-react-native-sdk-0.1.0.tgz"
    tgz.write_bytes(b"tgz")
    result = _run_step(
        _step("npm-build", STEP),
        {"steps.pack.outputs.tgz": str(tgz)},
        tmp_path,
        workspace,
        runner_env=_runner_env(tmp_path, tsc_fails),
    )
    assert result.returncode == status, result.stdout + result.stderr
    if status:
        assert "error: AsyncStorage 3.1.1: tsc --noEmit exited 2" in result.stdout
    else:
        assert result.stdout.count("keeps its one AsyncStorage") == 3, result.stdout


@pytest.mark.parametrize(("tsc_fails", "status"), [("", 0), ("3", 1)])
def test_the_unit_tests_step_packs_then_checks(
    tsc_fails: str, status: int, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    result = _run_step(
        _unit_step(STEP),
        {"matrix.sdk": "react-native"},
        tmp_path,
        workspace,
        runner_env=_runner_env(tmp_path, tsc_fails),
    )
    assert result.returncode == status, result.stdout + result.stderr
    calls = (tmp_path / "npm.log").read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("pack --pack-destination "), calls
    assert calls[-1].endswith("controlvar-react-native-sdk-0.1.0.tgz"), calls

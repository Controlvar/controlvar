"""The use-case harness's gates and the use cases themselves are all
collected, and none can skip.

A gate that silently stops running looks exactly like a gate that passes: a
parametrize case deleted, a module renamed so pytest no longer collects it, an
import that skips when CI happens to lack a package, a ``-m`` or
``--deselect`` slipped into ``tests/usecases/pytest.ini`` (which no scan of
``*.py`` reads). So:

* the exact number of tests collected here is pinned (``EXPECTED``);
* the exact number of cases collected under ``tests/usecases`` is pinned
  (``EXPECTED_CASES``), collected the way the ``usecases-flags`` arm runs
  them: ``python -P -m pytest tests/usecases`` through that root's own
  ``pytest.ini`` (no ``-o addopts``, no ``-c``), from a copy of the tree made
  with ``shutil`` (``scripts/core_build.sh``'s copy has no ``.git``, so no
  ``git archive``), with nothing on ``PYTHONPATH``. ``-P`` keeps the working
  directory off ``sys.path``, so only the ini's ``pythonpath`` can make the
  imports work; dropping ``../ground_truth`` from it fails collection (exit 2:
  the cases import the admin helper, which imports the ground-truth driver)
  and dropping ``../..`` fails the ``-p backend.tests.no_real_aws`` plugin
  (exit 1), both checked here on the copy;
* no file here or under ``tests/usecases`` (the harness, the drivers, the
  cases) may carry a skip or an expected-failure marker.

When you add or remove a gate or a case, change ``EXPECTED`` or
``EXPECTED_CASES`` in the same commit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
USECASES_ROOT = REPO_ROOT / "tests" / "usecases"

#: test node id count per module, as ``pytest --collect-only -q`` lists them.
EXPECTED = {
    "test_admin.py": 21,
    "test_collection_count.py": 5,
    "test_gates.py": 25,
    "test_proxy.py": 55,
    "test_python_driver.py": 6,
    "test_runner.py": 12,
    "test_sdks.py": 22,
    "test_usecases_workflow.py": 12,
}
#: The same, for the cases under ``tests/usecases``.
EXPECTED_CASES = {
    "test_flags_over_time.py": 5,
    "test_js_driver.py": 5,
}
#: What a copy needs to collect the cases: the root, the ground-truth library
#: and driver it imports, and the one plugin it loads from the repository.
COPIED_DIRS = (("tests", "usecases"), ("tests", "ground_truth"))
COPIED_FILES = (
    ("backend", "__init__.py"),
    ("backend", "tests", "__init__.py"),
    ("backend", "tests", "no_real_aws.py"),
)
PYTHONPATH_LINE = "pythonpath = . ../ground_truth ../.."
#: The longest a collection may take.
COLLECT_SECONDS = 120

# Assembled so that this file does not match its own check.
_FORBIDDEN = tuple(
    "".join(parts)
    for parts in (
        ("pytest.", "skip("),
        ("mark.", "skip"),
        ("import", "orskip"),
        ("x", "fail"),
        ("Skip", "Test"),
    )
)


def _counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        if "::" not in line:
            continue
        module = line.split("::", 1)[0].rsplit("/", 1)[-1]
        counts[module] = counts.get(module, 0) + 1
    return counts


def test_exactly_the_expected_tests_are_collected():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cov",
            "-p",
            "no:cacheprovider",
            str(HERE.relative_to(REPO_ROOT)),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == EXPECTED, result.stdout


def _copy(tmp_path: Path) -> Path:
    """The files the cases' collection needs, copied with ``shutil``."""
    root = tmp_path / "tree"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for parts in COPIED_DIRS:
        shutil.copytree(
            REPO_ROOT.joinpath(*parts), root.joinpath(*parts), ignore=ignore
        )
    for parts in COPIED_FILES:
        target = root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT.joinpath(*parts), target)
    return root


def _collect(root: Path) -> subprocess.CompletedProcess[str]:
    """``python -P -m pytest tests/usecases --collect-only`` in *root*, as the
    arm runs it, with nothing on PYTHONPATH and no PYTEST_ADDOPTS."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in ("PYTHONPATH", "PYTEST_ADDOPTS")
    }
    return subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "tests/usecases",
            "--collect-only",
            "-q",
            "-p",
            "no:cov",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=COLLECT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def test_exactly_the_expected_cases_are_collected_from_a_copy(tmp_path):
    result = _collect(_copy(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == EXPECTED_CASES, result.stdout


@pytest.mark.parametrize(("entry", "status"), [("../ground_truth", 2), ("../..", 1)])
def test_the_copy_collects_only_through_its_pythonpath(tmp_path, entry, status):
    """Each ``pythonpath`` entry is needed: without it the copy does not
    collect. (Without ``-P`` the working directory would stand in for
    ``../..`` and hide its loss.)"""
    root = _copy(tmp_path)
    ini = root / "tests" / "usecases" / "pytest.ini"
    text = ini.read_text(encoding="utf-8")
    assert PYTHONPATH_LINE in text
    kept = " ".join(part for part in PYTHONPATH_LINE.split(" ") if part != entry)
    ini.write_text(text.replace(PYTHONPATH_LINE, kept), encoding="utf-8")
    assert ini.read_text(encoding="utf-8") != text
    result = _collect(root)
    assert result.returncode == status, result.stdout + result.stderr


def test_no_gate_here_or_under_tests_usecases_can_skip():
    files = sorted(HERE.glob("*.py")) + sorted(USECASES_ROOT.rglob("*.py"))
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in files}
    # The scan reaches the harness, the drivers and the cases, or it proves
    # nothing.
    assert {
        "tests/usecases/usecases/admin.py",
        "tests/usecases/usecases/fake_upstream.py",
        "tests/usecases/usecases/gates.py",
        "tests/usecases/usecases/proxy.py",
        "tests/usecases/usecases/runner.py",
        "tests/usecases/usecases/sdks.py",
        "tests/usecases/drivers/python_driver.py",
        "tests/usecases/test_flags_over_time.py",
        "tests/usecases/test_js_driver.py",
    } <= scanned
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: {token}"
        for path in files
        for token in _FORBIDDEN
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

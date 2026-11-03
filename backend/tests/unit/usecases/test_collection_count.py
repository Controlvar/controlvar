"""The use-case harness's gates are all collected, and none can skip.

A gate that silently stops running looks exactly like a gate that passes: a
parametrize case deleted, a module renamed so pytest no longer collects it, an
import that skips when CI happens to lack a package. So the exact number of
tests collected here is pinned, and no file here or under ``tests/usecases``
(the harness, the drivers and, from the next pull request, the cases) may
carry a skip or an expected-failure marker.

The count pins the gates in this directory. The cases under
``tests/usecases`` get their own exact count with the first case: there is
none yet, and a count of zero would only be rewritten.

When you add or remove a gate, change ``EXPECTED`` in the same commit.
"""

from __future__ import annotations

import os
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
    "test_admin.py": 18,
    "test_collection_count.py": 2,
    "test_proxy.py": 55,
    "test_python_driver.py": 6,
    "test_runner.py": 12,
}

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
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if "::" not in line:
            continue
        module = line.split("::", 1)[0].rsplit("/", 1)[-1]
        counts[module] = counts.get(module, 0) + 1
    assert counts == EXPECTED, result.stdout


def test_no_gate_here_or_under_tests_usecases_can_skip():
    files = sorted(HERE.glob("*.py")) + sorted(USECASES_ROOT.rglob("*.py"))
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in files}
    # The scan reaches the harness and the driver, or it proves nothing.
    assert {
        "tests/usecases/usecases/admin.py",
        "tests/usecases/usecases/proxy.py",
        "tests/usecases/usecases/runner.py",
        "tests/usecases/drivers/python_driver.py",
    } <= scanned
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: {token}"
        for path in files
        for token in _FORBIDDEN
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

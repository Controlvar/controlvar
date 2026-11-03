"""G5: the ground-truth gates are all collected, and none can skip.

A gate that silently stops running looks exactly like a gate that passes: a
parametrize case deleted, a module renamed so pytest no longer collects it, an
import that skips when CI happens to lack a package. So the exact number of tests
collected here is pinned, and no file here or in the library may carry a skip
or an expected-failure marker. The CI ``unit`` job also runs
``scripts/check_junit_skips.py`` over this directory, which fails on any test
reported as skipped at run time.

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
LIBRARY_ROOT = REPO_ROOT / "tests" / "ground_truth"

#: test node id count per module, as ``pytest --collect-only -q`` lists them.
EXPECTED = {
    "test_collection_count.py": 2,
    "test_contract.py": 22,
    "test_contract_citations.py": 27,
    "test_generator.py": 21,
    "test_hashing.py": 20,
    "test_independence.py": 3,
    "test_ledger.py": 55,
    "test_reference.py": 27,
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


def test_no_gate_here_or_in_the_library_can_skip():
    files = sorted(HERE.glob("*.py")) + sorted(LIBRARY_ROOT.rglob("*.py"))
    assert any(path.parent == LIBRARY_ROOT / "ground_truth" for path in files)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {token}"
        for path in files
        for token in _FORBIDDEN
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

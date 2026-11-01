"""The statistical gates in this directory are all collected, and none can skip.

A gate that silently stops running looks exactly like a gate that passes: a
parametrize case deleted, a module renamed so pytest no longer collects it, a
``skipif`` added "for CI". So the exact number of tests collected here is
pinned, and no file here may carry a skip or expected-failure marker.

The CI ``unit`` job also runs ``scripts/check_junit_skips.py`` over this
directory, which fails on any test reported as skipped at run time.

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

#: test node id count per module, as ``pytest --collect-only -q`` lists them.
EXPECTED = {
    "test_bayesian_stop_winner.py": 13,
    "test_collection_count.py": 2,
    "test_golden_aa_significance_rate.py": 2,
    "test_golden_interval_coverage.py": 4,
    "test_golden_planted_lift.py": 3,
    "test_golden_srm.py": 4,
    "test_multiple_comparison_reference.py": 21,
    "test_sequential_confidence_sequence.py": 20,
    "test_sequential_p_value.py": 18,
    "test_sequential_unequal_arms.py": 12,
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


def test_no_gate_here_can_skip():
    offenders = [
        f"{path.name}: {token}"
        for path in sorted(HERE.glob("*.py"))
        for token in _FORBIDDEN
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

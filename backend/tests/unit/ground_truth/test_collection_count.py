"""G5: the ground-truth gates are all collected, and none can skip.

A gate that silently stops running looks exactly like a gate that passes: a
parametrize case deleted, a module renamed so pytest no longer collects it, an
import that skips when CI happens to lack a package. So the exact number of tests
collected here is pinned, and no file here or in the library may carry a skip
or an expected-failure marker. The CI ``unit`` job also runs
``scripts/check_junit_skips.py`` over this directory, which fails on any test
reported as skipped at run time.

When you add or remove a gate, change ``EXPECTED`` in the same commit.

The live replay (``tests/ground_truth``, run by ``_platform.yml``'s
``ground-truth`` arm, not here) is pinned the same way: its exact node ids
(``REPLAY_SUITE``), collected without a server. A test added there changes
them, and ``test_replay_workflow.py`` fails until the arm runs its file too,
so a new test cannot sit in a file CI never runs. FX
(``tests/ground_truth/nightly_fx.py``, the ``ground-truth-faults-nightly``
arm's one file) is pinned the same way (``FX_SUITE``); it is not a
``test_*.py`` file, so ``REPLAY_SUITE`` holds none of its tests.
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
    "test_collection_count.py": 4,
    "test_contract.py": 38,
    "test_contract_citations.py": 32,
    "test_driver.py": 21,
    "test_fault_gates.py": 57,
    "test_fault_library.py": 135,
    "test_flag_rollout_use_cases.py": 2,
    "test_generator.py": 21,
    "test_hashing.py": 23,
    "test_independence.py": 3,
    "test_ledger.py": 66,
    "test_nightly_fx.py": 27,
    "test_reference.py": 27,
    "test_replay_gates.py": 37,
    "test_replay_workflow.py": 32,
    "test_sensitivity.py": 33,
    "test_targeting_use_cases.py": 26,
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


#: The fault scenarios of ``test_faults.py``, and the gate only its own fault
#: runs.
FAULTS = ("F1", "F3", "F5", "F6", "F7", "F9")
FAULT_GATES = {
    "test_f1d_every_repeat_is_answered_as_one": "F1",
    "test_f7_the_sample_ratio_check_overrides_the_recommendation": "F7",
    "test_f9_each_hostile_class_gets_its_listed_answer": "F9",
}

#: The live replay's tests, as ``pytest --collect-only -q tests/ground_truth``
#: lists them (its own pytest.ini; no server is needed to collect).
REPLAY_SUITE = [
    *(
        f"test_faults.py::{test}[{fault}]"
        for test in (
            "test_p_every_answer_is_the_one_its_op_expects",
            "test_g0_every_assignment_answer_is_o_assign",
            "test_g1_counts_and_means_equal_the_ledger",
            "test_g2_statistics_equal_the_reference",
            "test_z_no_answer_is_a_5xx",
            "test_s_the_fault_reached_the_ledger_as_planted",
        )
        for fault in FAULTS
    ),
    *(f"test_faults.py::{test}[{fault}]" for test, fault in FAULT_GATES.items()),
    "test_replay.py::test_g6_the_replay_runs_the_core_profile",
    *(
        f"test_replay.py::{test}[{scenario}]"
        for scenario in ("S1", "S2", "S3", "S4")
        for test in (
            "test_g0_every_assignment_answer_is_o_assign",
            "test_g1_counts_and_means_equal_the_ledger",
            "test_g2_statistics_equal_the_reference",
            "test_z_no_answer_is_a_5xx",
        )
    ),
]


#: FX's tests, as ``pytest --collect-only -q tests/ground_truth/nightly_fx.py``
#: lists them: what the ``ground-truth-faults-nightly`` arm runs.
FX_SUITE = [
    f"nightly_fx.py::{test}"
    for test in (
        "test_p_every_answer_is_the_one_its_op_expects",
        "test_g0_every_assignment_answer_is_o_assign",
        "test_g1_counts_and_means_equal_the_ledger",
        "test_g2_statistics_equal_the_reference",
        "test_z_no_answer_is_a_5xx",
        "test_s_every_fault_reached_the_ledger_as_planted",
        "test_f1d_every_repeat_is_answered_as_one",
        "test_f7_the_sample_ratio_check_overrides_the_recommendation",
        "test_f9_each_hostile_class_gets_its_listed_answer",
    )
]


def _collected(path: Path) -> list[str]:
    """The node ids ``pytest --collect-only -q <path>`` lists (the live
    replay's own pytest.ini; no server is needed to collect)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cov",
            str(path.relative_to(REPO_ROOT)),
        ],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_the_live_replay_collects_exactly_its_tests():
    assert _collected(LIBRARY_ROOT) == REPLAY_SUITE


def test_the_fx_arm_collects_exactly_its_tests():
    """Named, nightly_fx.py collects FX's nine gates and nothing else; the
    directory, as the pull-request arm runs it, collects none of them."""
    assert _collected(LIBRARY_ROOT / "nightly_fx.py") == FX_SUITE


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

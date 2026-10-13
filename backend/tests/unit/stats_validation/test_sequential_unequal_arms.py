"""The sequential interval keeps its coverage when arms are split unequally (#854).

``GET /results/{id}/sequential`` decides ``can_stop`` with the mSPRT and
reports the confidence sequence that inverts it, so 0 lies outside the interval
exactly when ``can_stop`` is true.  Both used the plug-in variance
``p(1-p)/n`` per arm until #854.  With a small, low-rate arm that variance is
too small (it is 0 while the arm has no conversions), so the interval was too
narrow and, by the identity above, A/A experiments were stopped too often: at
9:1 and a 1% rate about one in five A/A paths was told ``stop_for_effect``.
Since #854 both use the Agresti-Caffo variance.

Each cell simulates ``PATHS`` experiments read ``LOOKS`` times, with ``M``
users per look in the small arm and ``M * ratio`` in the large one, through
``SequentialTestingService`` itself.  The seed is fixed, so the counts below
are deterministic; the floors are the nominal level, 1 - alpha = 0.95.
Measured at this seed (coverage / share of A/A paths that ever stop):

==========================  ==================  ==============
cell                        plug-in (pre-#854)  Agresti-Caffo
==========================  ==================  ==============
A/A 9:1, small treatment    0.788 / 0.212       0.967 / 0.033
A/A 9:1, small control      0.781 / 0.220       0.970 / 0.030
A/A 4:1                     0.931 / 0.069       0.982 / 0.018
1% vs 3%, 9:1               0.920               0.955
==========================  ==================  ==============

Agresti-Caffo is not exact at every design: with a far smaller arm (99:1)
and 10 to 50 users per look in it, coverage measured 0.93 to 0.95.

The 1:1 coverage and A/A gates stay in ``test_sequential_confidence_sequence.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pytest

from backend.app.services.sequential_testing_service import SequentialTestingService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ALPHA = 0.05
TAU_SQUARED = 0.001
PATHS = 4_000
LOOKS = 20
#: Users per look in the small arm.
M = 100
SEED = 854

#: The nominal level: at least 95% of paths hold the true effect at every look.
MIN_COVERAGE = 1.0 - ALPHA


@dataclass(frozen=True)
class Cell:
    """One simulated design.  ``ratio`` is large arm / small arm."""

    name: str
    p_control: float
    p_treatment: float
    ratio: int
    small_arm: str  # "treatment" or "control"


CELLS = {
    cell.name: cell
    for cell in (
        Cell("aa_9to1", 0.01, 0.01, 9, "treatment"),
        Cell("aa_9to1_small_control", 0.01, 0.01, 9, "control"),
        Cell("aa_4to1", 0.01, 0.01, 4, "treatment"),
        Cell("ab_9to1", 0.01, 0.03, 9, "treatment"),
    )
}


@dataclass(frozen=True)
class CellResult:
    covered_paths: int
    paths_that_stop: int
    looks: int
    identity_violations: int


@lru_cache(maxsize=None)
def run_cell(name: str) -> CellResult:
    """Simulate one cell through the service; deterministic for a given cell."""
    cell = CELLS[name]
    service = SequentialTestingService()
    # One stream per cell, so adding a cell never changes another's draws.
    rng = np.random.default_rng([SEED, list(CELLS).index(name)])
    step_small, step_large = M, M * cell.ratio
    if cell.small_arm == "treatment":
        step_c, step_t = step_large, step_small
    else:
        step_c, step_t = step_small, step_large
    true_delta = cell.p_treatment - cell.p_control

    conv_c = np.cumsum(rng.binomial(step_c, cell.p_control, size=(PATHS, LOOKS)), 1)
    conv_t = np.cumsum(rng.binomial(step_t, cell.p_treatment, size=(PATHS, LOOKS)), 1)

    covered = stopped = looks = violations = 0
    for i in range(PATHS):
        path_covered = True
        path_stopped = False
        for k in range(LOOKS):
            n_c, n_t = step_c * (k + 1), step_t * (k + 1)
            s_c, s_t = int(conv_c[i, k]), int(conv_t[i, k])
            msprt = service.compute_msprt(
                s_c, n_c, s_t, n_t, tau_squared=TAU_SQUARED, alpha=ALPHA
            )
            cs = service.compute_always_valid_ci(
                s_c, n_c, s_t, n_t, alpha=ALPHA, tau_squared=TAU_SQUARED
            )
            looks += 1
            if msprt.can_stop != (not (cs.lower <= 0.0 <= cs.upper)):
                violations += 1
            path_stopped |= msprt.can_stop
            path_covered &= cs.lower <= true_delta <= cs.upper
        covered += path_covered
        stopped += path_stopped
    return CellResult(covered, stopped, looks, violations)


@pytest.mark.parametrize("name", sorted(CELLS))
def test_anytime_coverage_at_unequal_splits(name):
    """At least 95% of paths hold the true effect at every one of 20 looks."""
    result = run_cell(name)
    coverage = result.covered_paths / PATHS
    assert coverage >= MIN_COVERAGE, (name, coverage)


@pytest.mark.parametrize(
    "name", [n for n, c in CELLS.items() if c.p_control == c.p_treatment]
)
def test_aa_paths_rarely_stop_at_unequal_splits(name):
    """On A/A paths, at most 5% are ever told they can stop."""
    result = run_cell(name)
    stop_rate = result.paths_that_stop / PATHS
    assert stop_rate <= ALPHA, (name, stop_rate)


@pytest.mark.parametrize("name", sorted(CELLS))
def test_can_stop_is_zero_outside_the_interval_on_every_look(name):
    """The identity ``can_stop == (0 not in CS)`` holds on every look."""
    result = run_cell(name)
    assert result.looks == PATHS * LOOKS
    assert result.identity_violations == 0, (name, result.identity_violations)


def test_the_effect_cell_is_not_vacuous():
    """The A/B cell stops on some paths, so its coverage is not met by never deciding."""
    assert run_cell("ab_9to1").paths_that_stop > PATHS // 10

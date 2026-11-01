"""
#1496: each treatment's difference from the control with its interval, the
interval of the relative change and the incremental impact, held to oracles
that share no code with ``sufficient_stats_analysis``.

* The proportion difference's interval: statsmodels'
  ``confint_proportions_2indep(method="newcomb")``.
* Each arm's limits: scipy's ``binomtest(...).proportion_ci(method="wilson")``
  for a proportion; for a mean, the arm's mean plus or minus
  ``t(1 - alpha / 2, df)`` times its standard error, with the
  Welch-Satterthwaite df, from numpy and scipy.
* The relative change's bounds: ``scipy.optimize.brentq`` on MOVER's limits
  of ``D(theta) = m_t - theta * m_c``, sign-aware (the limits of
  ``theta * m_c`` swap below 0), with the floor of the ratio set by the
  analysis under test (0 for two proportions, none for two means), never by
  the code's flag or predicate; and the residual of the code's bound in its
  defining equation.  No closed form is typed again here, and Katz's log
  interval is not used: it is another method, and on 150/1000 against
  120/1000 it covers a ratio of 1 where MOVER-R does not.  The oracle finds an
  unbounded set itself: no bound within its search is a relative interval of
  None.
* The relations to ``is_significant``, counted exactly on fixed grids.

Plants that turn this file red (PR evidence): the arms swapped; a Wald
interval in place of Newcombe's; the old guard (``u_c < 2 m_c``) kept for
proportions; a 0-conversion treatment's lower bound divided by ``a_low``;
the proportion flag on the mean path; the mean guard reduced to ``l_c > 0``;
a negative control negated without reflecting the interval; ``l_t`` and
``u_t`` swapped in one bound; the impact scaled by the control's users.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
from fractions import Fraction
from functools import lru_cache
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest
from scipy import stats
from scipy.optimize import brentq
from statsmodels.stats.proportion import confint_proportions_2indep

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.experiment import MetricType
from backend.app.schemas.export import ExperimentExportRow, VariantExportRow
from backend.app.schemas.results import MetricResult, VariantResult
from backend.app.services import holdout_results
from backend.app.services import sufficient_stats_analysis as ssa
from backend.app.services.sufficient_stats_analysis import (
    DIFFERENCE_FIELDS,
    MEAN_METRIC_TYPES,
    ArmLimits,
    BinomialVariant,
    binomial_metric_result,
    mean_metric_result,
    per_user_mean_metric_result,
    relative_change_interval,
)
from backend.tests.unit.services import (
    test_binomial_metric_result_characterisation as characterisation,
)
from backend.tests.unit.services import (
    test_revenue_per_user_routing as routing,
)
from backend.tests.unit.services import (
    test_sufficient_stats_fingerprint as sufficient_stats,
)
from backend.tests.unit.services import (
    test_value_metrics_per_user as value_metrics,
)

pytestmark = [pytest.mark.unit]

#: The metric types analysed as a mean, read from the module (S2.7): a test
#: below holds that this is that very tuple, so the mean cases follow
#: ``MEAN_METRIC_TYPES`` when more types join it (#439).
MEAN_TYPES_UNDER_TEST = MEAN_METRIC_TYPES
#: Every other ``MetricType`` is analysed as a proportion.
PROPORTION_TYPES_UNDER_TEST = tuple(
    t.value for t in MetricType if t.value not in MEAN_TYPES_UNDER_TEST
)

#: The last engine whose results carry none of the #1496 fields (#439's);
#: 1.8.0 adds them.
BEFORE_DIFFERENCE_FIELDS = "1.7.0"

#: The fields #1496 adds to a mean treatment, which already carried
#: ``absolute_difference`` and its interval (#439).
NEW_MEAN_FIELDS = (
    "relative_improvement_pct_ci",
    "incremental_impact",
    "incremental_impact_ci",
)

_CONTROL = BinomialVariant("00000000-0000-4000-8000-0000000000c0", "control", True)
_T1 = BinomialVariant("00000000-0000-4000-8000-0000000000e1", "t1", False)
_T2 = BinomialVariant("00000000-0000-4000-8000-0000000000e2", "t2", False)


def _metric(metric_type: str = "conversion", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id="00000000-0000-4000-8000-0000000000a0",
        name=f"{metric_type} metric",
        metric_type=metric_type,
        is_primary=True,
        **extra,
    )


def _proportion(
    x_t: int,
    n_t: int,
    x_c: int,
    n_c: int,
    level: float = 0.95,
    correction: str = "none",
    metric_type: str = "conversion",
) -> Dict[str, Any]:
    """The treatment's row of ``binomial_metric_result`` for one table."""
    result = binomial_metric_result(
        [(_CONTROL, n_c, x_c), (_T1, n_t, x_t)],
        1.0 - level,
        correction,
        metric=_metric(metric_type),
    )
    [treatment] = [v for v in result["variants"] if not v["is_control"]]
    return treatment


def _mean_result(
    control: Sequence[float],
    treatments: Sequence[Sequence[float]],
    level: float = 0.95,
    correction: str = "none",
    metric_type: str = MEAN_TYPES_UNDER_TEST[0],
) -> Dict[str, Any]:
    """``per_user_mean_metric_result`` with every user's value given."""
    variants = [(_CONTROL, len(control), sum(1 for y in control if y), list(control))]
    for arm, values in zip((_T1, _T2), treatments):
        variants.append((arm, len(values), sum(1 for y in values if y), list(values)))
    return per_user_mean_metric_result(
        variants, 1.0 - level, correction, metric=_metric(metric_type)
    )


def _treatments(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [v for v in result["variants"] if not v["is_control"]]


# ---------------------------------------------------------------------------
# Oracles
# ---------------------------------------------------------------------------


def _scipy_wilson(x: int, n: int, level: float) -> Tuple[float, float, float]:
    ci = stats.binomtest(x, n).proportion_ci(confidence_level=level, method="wilson")
    return x / n, float(ci.low), float(ci.high)


def _welch_limits(
    y_t: np.ndarray, y_c: np.ndarray, level: float
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """Each arm's mean +/- t(1 - alpha / 2, Welch df) * its standard error."""
    v_t = float(np.var(y_t, ddof=1)) / len(y_t)
    v_c = float(np.var(y_c, ddof=1)) / len(y_c)
    df = (v_t + v_c) ** 2 / (v_t**2 / (len(y_t) - 1) + v_c**2 / (len(y_c) - 1))
    c = float(stats.t.ppf(1.0 - (1.0 - level) / 2.0, df))
    m_t, m_c = float(np.mean(y_t)), float(np.mean(y_c))
    e_t, e_c = c * math.sqrt(v_t), c * math.sqrt(v_c)
    return (m_t, m_t - e_t, m_t + e_t), (m_c, m_c - e_c, m_c + e_c)


def _lower_equation(t, c):
    """The MOVER-R lower bound's defining equation, 0 at the bound."""
    (m_t, l_t, _), (m_c, _, u_c) = t, c
    return lambda th: (
        (m_t - th * m_c) ** 2 - (m_t - l_t) ** 2 - th**2 * (u_c - m_c) ** 2
    )


def _upper_equation(t, c):
    """The MOVER-R upper bound's defining equation, 0 at the bound."""
    (m_t, _, u_t), (m_c, l_c, _) = t, c
    return lambda th: (
        (th * m_c - m_t) ** 2 - (u_t - m_t) ** 2 - th**2 * (m_c - l_c) ** 2
    )


#: How far from the point estimate the oracle looks for a bound before it
#: calls the ratio unbounded.
_SEARCH_LIMIT = 1e15

#: The floor of the ratio for each analysis, set by the test from the analysis
#: it tests (never read from the code): two proportions have a ratio of 0 or
#: above; a ratio of two means can have either sign.
PROPORTION_FLOOR = 0.0
MEAN_FLOOR = None


def _lower_limit_of_d(theta: float, t, c) -> float:
    """MOVER's lower limit of ``D(theta) = m_t - theta * m_c`` from the arms'
    limits, for either sign of ``theta``: when ``theta < 0`` the limits of
    ``theta * m_c`` swap (its upper limit is ``theta * l_c``)."""
    (m_t, l_t, _), (m_c, l_c, u_c) = t, c
    top = theta * u_c if theta >= 0.0 else theta * l_c
    return (m_t - theta * m_c) - math.hypot(m_t - l_t, top - theta * m_c)


def _upper_limit_of_d(theta: float, t, c) -> float:
    """MOVER's upper limit of ``D(theta)``, sign-aware the same way."""
    (m_t, _, u_t), (m_c, l_c, u_c) = t, c
    bottom = theta * l_c if theta >= 0.0 else theta * u_c
    return (m_t - theta * m_c) + math.hypot(u_t - m_t, theta * m_c - bottom)


def _brentq(f, a: float, b: float) -> float:
    return brentq(f, a, b, xtol=1e-15, rtol=4 * np.finfo(float).eps, maxiter=1000)


def _first_past(test, theta0: float, direction: int, floor: Optional[float]):
    """The first point, stepping away from ``theta0`` by doubling steps, where
    ``test`` holds; None if it does not within ``_SEARCH_LIMIT`` (or above
    the floor, stepping down)."""
    step = max(1.0, abs(theta0))
    while step < _SEARCH_LIMIT:
        theta = theta0 + direction * step
        if floor is not None and direction < 0 and theta <= floor:
            return floor if test(floor) else None
        if test(theta):
            return theta
        step *= 2.0
    return None


def _oracle_ratio(t, c, floor: Optional[float]) -> Optional[Tuple[float, float]]:
    """Both bounds of ``m_t / m_c`` for ``m_c > 0``: the ratios ``theta``
    (``theta >= floor`` when there is a floor) whose ``D(theta)`` has a lower
    limit at most 0 and an upper limit at least 0, found by brentq on the
    sign-aware limits above.  None when either side has no bound within the
    search: the set of ratios the data allow is unbounded."""
    theta0 = t[0] / c[0]

    def lower(theta):
        return _lower_limit_of_d(theta, t, c)

    def upper(theta):
        return _upper_limit_of_d(theta, t, c)

    if floor is not None and lower(floor) <= 0.0:
        low = floor
    else:
        far = _first_past(lambda th: lower(th) > 0.0, theta0, -1, floor)
        if far is None:
            return None
        low = _brentq(lower, far, theta0)
    far = _first_past(lambda th: upper(th) < 0.0, theta0, +1, None)
    if far is None:
        return None
    return low, _brentq(upper, theta0, far)


def _oracle_relative(t, c, floor: Optional[float]) -> Optional[Tuple[float, float]]:
    """The relative change in percent, ``100 (m_t - m_c) / |m_c|``.

    For ``m_c > 0`` it is ``100 (ratio - 1)``.  For ``m_c < 0``, negating
    every value negates the relative change while the control's mean becomes
    positive, so the interval is the mirrored data's, negated and reversed.
    ``floor`` is the analysis's (``PROPORTION_FLOOR`` or ``MEAN_FLOOR``).
    """
    if c[0] == 0.0:
        return None
    if c[0] < 0.0:
        mirrored = _oracle_relative((-t[0], -t[2], -t[1]), (-c[0], -c[2], -c[1]), floor)
        return None if mirrored is None else (-mirrored[1], -mirrored[0])
    ratio = _oracle_ratio(t, c, floor)
    if ratio is None:
        return None
    return 100.0 * (ratio[0] - 1.0), 100.0 * (ratio[1] - 1.0)


def _residuals(rel: Tuple[float, float], t, c) -> Tuple[float, float]:
    """Each of the code's bounds put back into its defining equation, over
    the size of the equation's terms (0 at an exact root)."""
    if c[0] < 0.0:
        t, c = (-t[0], -t[2], -t[1]), (-c[0], -c[2], -c[1])
        thetas = (1.0 - rel[1] / 100.0, 1.0 - rel[0] / 100.0)
    else:
        thetas = (1.0 + rel[0] / 100.0, 1.0 + rel[1] / 100.0)
    out = []
    for equation, theta in zip((_lower_equation(t, c), _upper_equation(t, c)), thetas):
        (m_t, l_t, u_t), (m_c, l_c, u_c) = t, c
        scale = (
            m_t**2
            + (theta * m_c) ** 2
            + max(m_t - l_t, u_t - m_t) ** 2
            + theta**2 * max(u_c - m_c, m_c - l_c) ** 2
        )
        out.append(abs(equation(theta)) / scale)
    return out[0], out[1]


def _assert_close(actual, expected, rel=1e-9, abs_=1e-9):
    assert actual == pytest.approx(expected, rel=rel, abs=abs_), (actual, expected)


# ---------------------------------------------------------------------------
# S2.1: a proportion's difference, and Newcombe's interval
# ---------------------------------------------------------------------------

#: (x_t, n_t, x_c, n_c): balanced, unbalanced both ways, the boundary tables.
PROPORTION_TABLES = [
    (150, 1000, 120, 1000),
    (9, 150, 40, 2000),
    (690, 30000, 40, 2000),
    (40, 2000, 9, 150),
    (12, 400, 0, 500),
    (0, 20, 0, 20),
    (20, 20, 20, 20),
    (3, 37, 0, 100),
    (6, 20, 1, 20),
    (5, 50, 0, 50),
    (3, 50, 18, 100),
    (0, 20, 33, 200),
    (128, 1000, 100, 1000),
    (1, 1, 0, 1),
]


@pytest.mark.parametrize("table", PROPORTION_TABLES, ids=str)
@pytest.mark.parametrize("metric_type", PROPORTION_TYPES_UNDER_TEST)
def test_a_proportion_difference_is_the_difference_of_the_shares(table, metric_type):
    """``x_t / n_t - x_c / n_c`` from the counts, not from the rounded
    percentages ``mean`` is made from (PS-8)."""
    x_t, n_t, x_c, n_c = table
    treatment = _proportion(x_t, n_t, x_c, n_c, metric_type=metric_type)
    assert treatment["absolute_difference"] == x_t / n_t - x_c / n_c
    assert treatment["absolute_difference"] == pytest.approx(
        float(Fraction(x_t, n_t) - Fraction(x_c, n_c)), abs=1e-15
    )


@pytest.mark.parametrize("table", PROPORTION_TABLES, ids=str)
@pytest.mark.parametrize("level", [0.80, 0.90, 0.95, 0.99])
def test_the_proportion_interval_is_newcombes(table, level):
    x_t, n_t, x_c, n_c = table
    treatment = _proportion(x_t, n_t, x_c, n_c, level=level)
    low, high = confint_proportions_2indep(
        x_t, n_t, x_c, n_c, method="newcomb", compare="diff", alpha=1.0 - level
    )
    assert treatment["absolute_difference_ci"] == pytest.approx(
        (float(low), float(high)), abs=1e-12
    )


def test_two_named_intervals():
    """The plan's numbers: 0/20 against 0/20 is not degenerate, and the
    control at 0 of 500 against 12 of 400."""
    _assert_close(
        _proportion(0, 20, 0, 20)["absolute_difference_ci"],
        (-0.161125, 0.161125),
        abs_=5e-7,
    )
    _assert_close(
        _proportion(12, 400, 0, 500)["absolute_difference_ci"],
        (0.015138, 0.051699),
        abs_=5e-7,
    )


# ---------------------------------------------------------------------------
# S2.2: a proportion's interval against is_significant, counted exactly
# ---------------------------------------------------------------------------

#: (n_t, n_c, level) -> ("significant, interval covers 0", "interval excludes
#: 0, not significant").  Fisher's exact test decides; the interval is
#: Newcombe's.  Treatment 20 against control 200 and 50 against 100 hold the
#: first kind at both levels (PS-3); on the square grids it does not occur.
GRID_COUNTS = {
    (20, 20, 0.95): (0, 24),
    (50, 50, 0.95): (0, 82),
    (100, 37, 0.95): (0, 64),
    (37, 100, 0.95): (0, 64),
    (20, 200, 0.95): (2, 116),
    (20, 200, 0.99): (18, 90),
    (50, 100, 0.95): (2, 76),
    (50, 100, 0.99): (10, 44),
}


@lru_cache(maxsize=None)
def _grid(n_t: int, n_c: int, level: float) -> Tuple[Tuple[int, int, Dict], ...]:
    """Every table of the grid through ``binomial_metric_result``, no
    correction: ``(x_t, x_c, treatment row)``."""
    rows = []
    for x_c in range(n_c + 1):
        for x_t in range(n_t + 1):
            rows.append((x_t, x_c, _proportion(x_t, n_t, x_c, n_c, level=level)))
    return tuple(rows)


def _excludes_0(interval: Tuple[float, float]) -> bool:
    return interval[0] > 0.0 or interval[1] < 0.0


@pytest.mark.parametrize("grid", sorted(GRID_COUNTS), ids=str)
def test_the_proportion_interval_against_significance(grid):
    n_t, n_c, level = grid
    significant_covering = excluding_not_significant = 0
    nearest_bound = nearest_p = math.inf
    for _, _, row in _grid(n_t, n_c, level):
        interval = row["absolute_difference_ci"]
        significant_covering += row["is_significant"] and not _excludes_0(interval)
        excluding_not_significant += _excludes_0(interval) and not row["is_significant"]
        nearest_bound = min(nearest_bound, abs(interval[0]), abs(interval[1]))
        nearest_p = min(nearest_p, abs(row["p_value"] - (1.0 - level)))
    assert (significant_covering, excluding_not_significant) == GRID_COUNTS[grid]
    # No table sits so near a boundary that a last-bit difference could
    # move it from one count to the other.
    assert nearest_bound > 1e-9 and nearest_p > 1e-9


#: Named tables: (x_t, n_t, x_c, n_c, level, is_significant, interval).
BOUNDARY_TABLES = {
    # QA's B1, the repository's "three" fixture: not significant, Newcombe
    # excludes 0.
    "B1": (150, 1000, 120, 1000, 0.95, False, (0.000014, 0.060008)),
    "B2": (3, 37, 0, 100, 0.95, True, (0.016348, 0.213007)),
    "B3": (6, 20, 1, 20, 0.95, False, (0.008087, 0.472800)),
    "B4": (5, 50, 0, 50, 0.95, False, (0.008975, 0.213602)),
    # The dashboard's "significant, although the interval reaches 0" (PS-3).
    "C16": (3, 50, 18, 100, 0.95, True, (-0.215202, 0.000032)),
    "0/20 vs 33/200": (0, 20, 33, 200, 0.95, True, (-0.222658, 0.002300)),
    "128/1000": (128, 1000, 100, 1000, 0.95, False, (0.000102, 0.055969)),
    "129/1000": (129, 1000, 100, 1000, 0.95, True, (0.001052, 0.057020)),
}


@pytest.mark.parametrize("name", sorted(BOUNDARY_TABLES))
def test_named_boundary_tables(name):
    x_t, n_t, x_c, n_c, level, significant, interval = BOUNDARY_TABLES[name]
    row = _proportion(x_t, n_t, x_c, n_c, level=level)
    assert row["is_significant"] is significant
    _assert_close(row["absolute_difference_ci"], interval, abs_=5e-7)


def test_b5_an_adjusted_treatment_is_not_significant_although_its_interval_excludes_0():
    """Two treatments under Benjamini-Hochberg: t1's raw p is below 0.05, its
    adjusted p is not, and the unadjusted interval excludes 0."""
    result = binomial_metric_result(
        [(_CONTROL, 1000, 100), (_T1, 1000, 130), (_T2, 1000, 101)],
        0.05,
        "benjamini_hochberg",
        metric=_metric(),
    )
    t1 = next(v for v in result["variants"] if v["variant_name"] == "t1")
    assert t1["p_value"] < 0.05 < t1["adjusted_p_value"]
    assert t1["is_significant"] is False
    assert result["winning_variant_id"] is None
    _assert_close(t1["absolute_difference_ci"], (0.00200, 0.05807), abs_=5e-6)


# ---------------------------------------------------------------------------
# S2.3: a mean's interval against is_significant (Welch, unchanged)
# ---------------------------------------------------------------------------

MEAN_SEED = 1_496_003
MEAN_EXPERIMENTS = 400


@lru_cache(maxsize=None)
def _mean_datasets() -> Tuple[Tuple[np.ndarray, np.ndarray, np.ndarray], ...]:
    """Seeded experiments of a control and two treatments, per-user values of
    either sign, with effects near the detection threshold."""
    rng = np.random.default_rng(MEAN_SEED)
    out = []
    for _ in range(MEAN_EXPERIMENTS):
        sizes = rng.integers(12, 90, size=3)
        base = rng.uniform(-4.0, 4.0)
        scale = rng.uniform(0.5, 3.0)
        arms = []
        for i, n in enumerate(sizes):
            shift = 0.0 if i == 0 else rng.normal(0.0, 2.2) * scale / math.sqrt(n)
            arms.append(np.round(rng.normal(base + shift, scale, size=n), 6))
        out.append(tuple(arms))
    return tuple(out)


@lru_cache(maxsize=None)
def _mean_rows(correction: str, level: float = 0.95) -> Tuple[Tuple[Any, ...], ...]:
    """``(treatment row, y_t, y_c)`` for every treatment of the datasets."""
    rows = []
    for y_c, y_1, y_2 in _mean_datasets():
        result = _mean_result(y_c, [y_1, y_2], level=level, correction=correction)
        for row, y_t in zip(_treatments(result), (y_1, y_2)):
            rows.append((row, y_t, y_c))
    return tuple(rows)


#: correction -> "interval excludes 0, not significant" over the treatments
#: of ``_mean_datasets`` at 0.95.  "Significant, interval covers 0" is 0 for
#: every correction: an adjusted p-value is never below the raw one.
MEAN_EXCLUDING_NOT_SIGNIFICANT = {
    "none": 0,
    "bonferroni": 51,
    "benjamini_hochberg": 38,
}


@pytest.mark.parametrize("correction", sorted(MEAN_EXCLUDING_NOT_SIGNIFICANT))
def test_the_mean_interval_against_significance(correction):
    significant_covering = excluding_not_significant = 0
    for row, _, _ in _mean_rows(correction):
        interval = row["absolute_difference_ci"]
        significant_covering += row["is_significant"] and not _excludes_0(interval)
        excluding_not_significant += _excludes_0(interval) and not row["is_significant"]
    assert significant_covering == 0
    assert excluding_not_significant == MEAN_EXCLUDING_NOT_SIGNIFICANT[correction]


def test_the_mean_interval_is_welchs():
    """R6: the mean's difference interval is scipy's Welch interval."""
    for row, y_t, y_c in _mean_rows("none")[:200]:
        expected = stats.ttest_ind(y_t, y_c, equal_var=False).confidence_interval(0.95)
        _assert_close(row["absolute_difference_ci"], tuple(expected), rel=1e-7)


# ---------------------------------------------------------------------------
# S2.4: the relative change's interval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", PROPORTION_TABLES, ids=str)
@pytest.mark.parametrize("level", [0.80, 0.95, 0.99])
def test_the_proportion_relative_interval_solves_its_defining_equations(table, level):
    x_t, n_t, x_c, n_c = table
    row = _proportion(x_t, n_t, x_c, n_c, level=level)
    t, c = _scipy_wilson(x_t, n_t, level), _scipy_wilson(x_c, n_c, level)
    expected = _oracle_relative(t, c, PROPORTION_FLOOR)
    if expected is None:
        assert row["relative_improvement_pct_ci"] is None
        return
    _assert_close(row["relative_improvement_pct_ci"], expected, rel=1e-9, abs_=1e-7)
    assert max(_residuals(row["relative_improvement_pct_ci"], t, c)) < 1e-12


@pytest.mark.parametrize("grid", [(20, 200, 0.95), (50, 100, 0.99)], ids=str)
def test_the_relative_interval_matches_the_oracle_over_a_grid(grid):
    """Every table: the code's relative interval is None exactly when the
    oracle finds no bound, and otherwise equals it, with residuals near 0."""
    n_t, n_c, level = grid
    checked = 0
    wilson_c = {x: _scipy_wilson(x, n_c, level) for x in range(n_c + 1)}
    wilson_t = {x: _scipy_wilson(x, n_t, level) for x in range(n_t + 1)}
    for x_t, x_c, row in _grid(n_t, n_c, level):
        expected = _oracle_relative(wilson_t[x_t], wilson_c[x_c], PROPORTION_FLOOR)
        actual = row["relative_improvement_pct_ci"]
        assert (actual is None) == (expected is None), (x_t, x_c, actual, expected)
        if actual is None:
            continue
        _assert_close(actual, expected, rel=1e-9, abs_=1e-7)
        assert max(_residuals(actual, wilson_t[x_t], wilson_c[x_c])) < 1e-12
        checked += 1
    assert checked > 3000


#: Per-user values with each sign of the means: (name, control, treatment).
MEAN_FIXTURES = [
    (
        "positive",
        [8.0, 12.5, 9.0, 11.0, 10.5, 7.5, 13.0, 9.5, 10.0, 11.5],
        [12.0, 14.5, 11.0, 16.0, 13.5, 12.5, 15.0, 10.5, 14.0, 13.0, 12.0],
    ),
    (
        "negative control",
        [-3.0, -5.5, -4.0, -6.0, -2.5, -4.5, -5.0, -3.5],
        [-2.0, -3.5, -1.0, -4.0, -2.5, -3.0, -1.5, -2.0],
    ),
    (
        "negative treatment",
        [2.0, 3.5, 1.0, 4.0, 2.5, 3.0, 1.5, 2.0],
        [-1.0, -0.5, 0.5, -2.0, -1.5, 0.0, -1.0, -0.5],
    ),
    (
        "both negative, treatment lower",
        [-1.0, -1.5, -0.5, -2.0, -1.0, -1.25],
        [-3.0, -2.5, -4.0, -3.5, -2.0, -3.0, -2.75],
    ),
    (
        "mixed values",
        [5.0, -2.0, 3.0, 4.0, -1.0, 6.0, 2.0, 3.5, 4.5, 2.5],
        [1.0, -3.0, 2.0, 0.5, -1.0, 1.5, 0.0, 2.5],
    ),
]


def _relative_oracle_for_means(y_t, y_c, level):
    t, c = _welch_limits(np.asarray(y_t), np.asarray(y_c), level)
    return t, c, _oracle_relative(t, c, MEAN_FLOOR)


@pytest.mark.parametrize("name", [f[0] for f in MEAN_FIXTURES])
@pytest.mark.parametrize("level", [0.80, 0.95, 0.99])
@pytest.mark.parametrize("metric_type", MEAN_TYPES_UNDER_TEST)
def test_the_mean_relative_interval_solves_its_defining_equations(
    name, level, metric_type
):
    _, y_c, y_t = next(f for f in MEAN_FIXTURES if f[0] == name)
    [row] = _treatments(_mean_result(y_c, [y_t], level=level, metric_type=metric_type))
    t, c, expected = _relative_oracle_for_means(y_t, y_c, level)
    assert expected is not None, "each fixture keeps its control away from 0"
    actual = row["relative_improvement_pct_ci"]
    _assert_close(actual, expected, rel=1e-9, abs_=1e-7)
    assert max(_residuals(actual, t, c)) < 1e-12
    assert actual[0] <= row["relative_improvement_pct"] <= actual[1]


def test_the_relative_interval_of_negated_values_is_the_reflection():
    """Negating every value negates the relative change: the interval is
    the original's, negated and reversed."""
    _, y_c, y_t = MEAN_FIXTURES[0]
    [row] = _treatments(_mean_result(y_c, [y_t]))
    [negated] = _treatments(_mean_result([-y for y in y_c], [[-y for y in y_t]]))
    low, high = row["relative_improvement_pct_ci"]
    _assert_close(negated["relative_improvement_pct_ci"], (-high, -low), rel=1e-12)
    assert negated["relative_improvement_pct"] == pytest.approx(
        -row["relative_improvement_pct"], rel=1e-12
    )


def test_seeded_mean_rows_match_the_oracle_and_hold_r1_and_r2():
    """Every treatment of the seeded datasets, controls of either sign: the
    relative interval is the oracle's (None exactly when the oracle finds no
    bound), contains its own point (R1) and agrees with the difference's
    interval on 0 (R2).  Measured: 681 of the 800 have a relative interval,
    348 of them with a negative control."""
    negative_controls = compared = 0
    for row, y_t, y_c in _mean_rows("none"):
        t, c, expected = _relative_oracle_for_means(y_t, y_c, 0.95)
        actual = row["relative_improvement_pct_ci"]
        # No control sits within 1e-9 of the edge of the guard.
        assert abs(abs(c[0]) - (c[2] - c[0])) > 1e-9 * abs(c[0])
        assert (actual is None) == (expected is None), (actual, expected)
        if actual is None:
            continue
        compared += 1
        negative_controls += c[0] < 0
        _assert_close(actual, expected, rel=1e-9, abs_=1e-7)
        point = row["relative_improvement_pct"]
        assert actual[0] - 1e-9 <= point <= actual[1] + 1e-9
        difference = row["absolute_difference_ci"]
        assert (difference[0] > 0) == (actual[0] > 0)
        assert (difference[1] < 0) == (actual[1] < 0)
    assert (compared, negative_controls) == (681, 348)


def test_the_proportion_grids_hold_r1_and_r2():
    """R1: the point is inside its interval; R2: the difference's, the
    relative change's and the impact's intervals agree on 0."""
    rows = 0
    for n_t, n_c, level in GRID_COUNTS:
        for _, _, row in _grid(n_t, n_c, level):
            difference = row["absolute_difference_ci"]
            impact = row["incremental_impact_ci"]
            assert difference[0] <= row["absolute_difference"] <= difference[1]
            assert impact[0] <= row["incremental_impact"] <= impact[1]
            assert (impact[0] > 0, impact[1] < 0) == (
                difference[0] > 0,
                difference[1] < 0,
            )
            relative = row["relative_improvement_pct_ci"]
            if relative is None:
                continue
            rows += 1
            point = row["relative_improvement_pct"]
            assert relative[0] - 1e-9 <= point <= relative[1] + 1e-9
            # A ratio of two proportions is never below 0.
            assert relative[0] >= -100.0
            assert (relative[0] > 0, relative[1] < 0) == (
                difference[0] > 0,
                difference[1] < 0,
            )
    assert rows > 20000


def test_katz_disagrees_with_mover_r_on_b1():
    """Why Katz is not the oracle: on B1 MOVER-R's ratio interval excludes 1,
    as Newcombe's difference interval excludes 0, and Katz's covers 1."""
    row = _proportion(150, 1000, 120, 1000)
    low, high = row["relative_improvement_pct_ci"]
    _assert_close((low, high), (0.010, 56.229), abs_=5e-4)
    assert low > 0
    katz = stats.contingency.relative_risk(150, 1000, 120, 1000).confidence_interval(
        0.95
    )
    assert katz.low < 1 < katz.high


def test_a_control_at_0_has_no_relative_interval():
    assert _proportion(12, 400, 0, 500)["relative_improvement_pct_ci"] is None
    assert _proportion(0, 400, 0, 500)["relative_improvement_pct_ci"] is None
    [row] = _treatments(_mean_result([0.0, 1.0, -1.0, 0.0], [[2.0, 3.0, 1.0, 2.0]]))
    assert row["relative_improvement_pct"] is None
    assert row["relative_improvement_pct_ci"] is None


#: Proportion tables whose control's Wilson upper limit is above twice its
#: rate (a 2/200 control at 0.95: 0.0357 against 0.02), and the relative
#: interval each has (PS-1 reopened, T234): bounded, wide, never below -100%.
LOW_COUNT_RELATIVE = {
    (6, 200, 2, 200): (-34.18048952850553, 1206.3102447884623),
    (6, 200, 4, 200): (-55.392957656015426, 397.4833386943205),
}


@pytest.mark.parametrize("table", sorted(LOW_COUNT_RELATIVE), ids=str)
def test_a_low_count_control_has_a_bounded_relative_interval(table, caplog):
    """A control of a few conversions: its Wilson upper limit reaches past
    twice its rate, and the ratio of the two proportions is still bounded
    over ratios of 0 and above.  The pinned interval is the oracle's, and
    nothing is logged."""
    x_t, n_t, x_c, n_c = table
    _, l_c, u_c = _scipy_wilson(x_c, n_c, 0.95)
    assert l_c > 0 and u_c >= 2 * x_c / n_c
    with caplog.at_level(logging.WARNING):
        row = _proportion(x_t, n_t, x_c, n_c)
    expected = _oracle_relative(
        _scipy_wilson(x_t, n_t, 0.95), (x_c / n_c, l_c, u_c), PROPORTION_FLOOR
    )
    assert row["relative_improvement_pct_ci"] == pytest.approx(
        LOW_COUNT_RELATIVE[table], rel=1e-12
    )
    _assert_close(row["relative_improvement_pct_ci"], expected, rel=1e-9, abs_=1e-7)
    assert row["relative_improvement_pct_ci"][0] >= -100.0
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_treatment_with_no_conversions_at_the_exact_edge():
    """Constructed limits where ``u_c`` is exactly ``2 m_c`` and the treatment
    has no conversions (``b == 0``): the lower bound is 0, so the change's is
    -100%, with no division by ``u_c (2 m_c - u_c)``, which is 0 here.  Not a
    confidence level that lands on the edge: that hit depends on the last bit
    of ``norm.ppf``."""
    treatment = ArmLimits(0.0, 0.0, 0.3)
    control = ArmLimits(0.25, 0.0625, 0.5)
    assert control.upper == 2 * control.mean
    interval = relative_change_interval(treatment, control, nonnegative=True)
    assert interval[0] == -100.0
    assert interval[1] == pytest.approx(81.42294704442907, rel=1e-12)
    expected = _oracle_relative(treatment, control, PROPORTION_FLOOR)
    _assert_close(interval, expected, rel=1e-9, abs_=1e-7)


@pytest.mark.parametrize("mean", [0.5, -0.5])
def test_a_mean_control_at_the_float_edge_has_no_relative_interval(mean):
    """A mean control whose upper limit rounds to exactly twice its mean while
    its lower limit is a hair above 0: the mean's guard keeps both halves, so
    the answer is None, not a huge or failing bound."""
    treatment = ArmLimits(mean, mean - 0.25, mean + 0.25)
    control = ArmLimits(1.0, 2.0**-53, 2.0)
    assert control.lower > 0 and control.upper == 2 * control.mean
    assert relative_change_interval(treatment, control) is None


def test_a_mean_control_whose_interval_reaches_0_has_no_relative_interval():
    control = [-1.0, 2.0, 0.5, -0.5, 1.5, 0.0]
    [row] = _treatments(_mean_result(control, [[3.0, 4.0, 2.5, 3.5, 5.0, 4.0]]))
    _, c, expected = _relative_oracle_for_means(
        [3.0, 4.0, 2.5, 3.5, 5.0, 4.0], control, 0.95
    )
    assert c[1] <= 0 and expected is None
    assert row["relative_improvement_pct"] is not None
    assert row["relative_improvement_pct_ci"] is None


def test_a_limit_that_is_not_finite_is_logged_and_gives_none(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            relative_change_interval(
                ArmLimits(1.0, 0.5, math.inf), ArmLimits(1.0, 0.5, 1.5)
            )
            is None
        )
    assert any("not finite" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# S2.6: the change adds fields and moves no other number
# ---------------------------------------------------------------------------


def _without(output: Any, fields: Sequence[str]) -> Any:
    """``output`` with ``fields`` removed from every variant of every metric
    result in it, after checking that each variant carried them all."""
    stripped = copy.deepcopy(output)

    def strip(metric: Dict[str, Any]) -> None:
        for variant in metric["variants"]:
            for field in fields:
                assert field in variant, field
                del variant[field]

    if isinstance(stripped, dict) and "variants" in stripped:
        strip(stripped)
    elif isinstance(stripped, dict) and "metrics" in stripped:
        for metric in stripped["metrics"]:
            strip(metric)
    else:
        for metric in stripped.values():
            strip(metric)
    return stripped


def _sha(output: Any) -> str:
    payload = json.dumps(
        characterisation._canonical(output), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_without_the_new_fields_the_binomial_output_is_the_one_before_them():
    outputs = sufficient_stats._binomial_outputs()
    assert (
        _sha(_without(outputs, DIFFERENCE_FIELDS))
        == sufficient_stats.SUFFICIENT_STATS_FINGERPRINTS[BEFORE_DIFFERENCE_FIELDS][
            "binomial_metric_result"
        ]
    )


@pytest.mark.parametrize("case", sorted(characterisation.CHARACTERISATION))
def test_without_the_new_fields_results_are_the_ones_before_them(case, monkeypatch):
    fixture_name, confidence, correction = case.split("-")
    output = characterisation.results_output(
        fixture_name, float(confidence), correction, monkeypatch
    )
    actual, _ = characterisation._fingerprint(_without(output, DIFFERENCE_FIELDS))
    assert actual == characterisation.CHARACTERISATIONS[BEFORE_DIFFERENCE_FIELDS][case]


def test_without_the_new_fields_the_revenue_results_are_the_ones_before_them(
    monkeypatch,
):
    """R6: a mean metric's difference and its interval do not move."""
    output = routing._results(
        [
            routing._metric(MetricType.REVENUE, is_primary=True),
            routing._metric(MetricType.CONVERSION),
        ],
        monkeypatch,
    )
    for metric in output["metrics"]:
        fields = (
            NEW_MEAN_FIELDS if metric.get("analysis") == "mean" else DIFFERENCE_FIELDS
        )
        stripped = _without(metric, fields)
        metric.clear()
        metric.update(stripped)
    assert (
        routing._fingerprint(output)
        == routing.REVENUE_RESULTS_FINGERPRINTS[BEFORE_DIFFERENCE_FIELDS]
    )


def test_without_the_new_fields_the_value_metric_results_are_the_ones_before_them(
    monkeypatch,
):
    """R6 for #439's mean types: a count, duration or custom metric's
    difference and its interval do not move either."""
    output = value_metrics._value_metrics_output(monkeypatch)
    assert {m["analysis"] for m in output["metrics"]} == {"mean"}
    for metric in output["metrics"]:
        stripped = _without(metric, NEW_MEAN_FIELDS)
        metric.clear()
        metric.update(stripped)
    assert (
        routing._fingerprint(output)
        == value_metrics.VALUE_RESULTS_FINGERPRINTS[BEFORE_DIFFERENCE_FIELDS]
    )


def test_the_engine_before_the_new_fields_is_frozen_and_this_one_is_later():
    """The fields change the results' output, so they cannot be pinned under
    a version frozen on main without them."""
    assert BEFORE_DIFFERENCE_FIELDS in sufficient_stats.RELEASED_ENGINE_VERSIONS
    assert sufficient_stats._key(ENGINE_VERSION) > sufficient_stats._key(
        BEFORE_DIFFERENCE_FIELDS
    )


def test_a_warehouse_mean_result_carries_none_of_the_new_fields():
    """``mean_metric_result`` (warehouse mean runs) is unchanged: the
    narrowing limitations.md states."""
    for output in sufficient_stats._mean_outputs().values():
        for variant in output["variants"]:
            assert not set(DIFFERENCE_FIELDS) & set(variant)


# ---------------------------------------------------------------------------
# S2.7: generic over the analysis, not the metric type
# ---------------------------------------------------------------------------


def test_the_mean_cases_follow_the_module_tuple():
    assert MEAN_TYPES_UNDER_TEST is ssa.MEAN_METRIC_TYPES
    assert set(PROPORTION_TYPES_UNDER_TEST) | set(MEAN_TYPES_UNDER_TEST) == {
        t.value for t in MetricType
    }


def test_every_metric_type_carries_the_fields_through_the_results(monkeypatch):
    """Through ``get_experiment_results`` with only the database replaced:
    every treatment of every metric type carries all five, the control none,
    and the schema keeps them."""
    metrics = [
        routing._metric(t, is_primary=t is MetricType.CONVERSION) for t in MetricType
    ]
    output = routing._results(metrics, monkeypatch)
    analyses = {}
    for result in output["metrics"]:
        analyses[result["metric_type"]] = result.get("analysis", "proportion")
        control = next(v for v in result["variants"] if v["is_control"])
        [treatment] = _treatments(result)
        assert all(control[field] is None for field in DIFFERENCE_FIELDS)
        assert all(treatment[field] is not None for field in DIFFERENCE_FIELDS)
        validated = MetricResult.model_validate(result)
        schema_treatment = next(v for v in validated.variants if not v.is_control)
        for field in DIFFERENCE_FIELDS:
            value = getattr(schema_treatment, field)
            assert value == pytest.approx(treatment[field], rel=1e-15)
    assert {t for t, a in analyses.items() if a == "mean"} == set(MEAN_TYPES_UNDER_TEST)


# ---------------------------------------------------------------------------
# S2.8 to S2.10: the impact
# ---------------------------------------------------------------------------


def test_the_proportion_impact_is_the_difference_times_the_treatments_users():
    """Unbalanced arms, so the treatment's, the control's and both arms'
    users give different answers."""
    result = binomial_metric_result(
        [(_CONTROL, 150, 9), (_T1, 2000, 160), (_T2, 30000, 2100)],
        0.05,
        "benjamini_hochberg",
        metric=_metric(),
    )
    for row in _treatments(result):
        n = row["sample_size"]
        low, high = row["absolute_difference_ci"]
        assert row["incremental_impact"] == row["absolute_difference"] * n
        assert row["incremental_impact_ci"] == (low * n, high * n)
    assert [row["sample_size"] for row in _treatments(result)] == [2000, 30000]


@pytest.mark.parametrize("metric_type", MEAN_TYPES_UNDER_TEST)
def test_the_mean_impact_is_the_difference_times_the_treatments_users(metric_type):
    rng = np.random.default_rng(1_496_008)
    control = list(np.round(rng.exponential(10.0, size=40), 2))
    treatment = list(np.round(rng.exponential(12.0, size=300), 2))
    [row] = _treatments(_mean_result(control, [treatment], metric_type=metric_type))
    low, high = row["absolute_difference_ci"]
    assert row["sample_size"] == 300
    assert row["incremental_impact"] == row["absolute_difference"] * 300
    assert row["incremental_impact_ci"] == (low * 300, high * 300)


def test_a_mean_with_no_variation_has_an_impact_but_no_interval():
    """Neither arm varies: the difference is known, its interval is not."""
    [row] = _treatments(_mean_result([0.0] * 5, [[2.0] * 4]))
    assert row["absolute_difference"] == pytest.approx(2.0)
    assert row["incremental_impact"] == row["absolute_difference"] * 4
    assert row["absolute_difference_ci"] is None
    assert row["incremental_impact_ci"] is None
    assert row["relative_improvement_pct_ci"] is None


def test_the_impact_is_a_plain_number():
    """A number, with no currency and no unit, in the schema and in its
    description."""
    field = VariantResult.model_fields["incremental_impact"]
    assert field.annotation == Optional[float]
    assert "no currency or unit is read or added" in field.description
    interval = VariantResult.model_fields["incremental_impact_ci"]
    assert interval.annotation == Optional[tuple[float, float]]


def test_on_a_lower_is_better_metric_the_signs_are_the_raw_change():
    """A significant decrease that wins: the impact and both intervals are
    negative, as the difference is; nothing is flipped for the direction."""
    result = binomial_metric_result(
        [(_T1, 2000, 125), (_CONTROL, 2000, 200)],
        0.05,
        "none",
        metric=_metric(lower_is_better=True),
    )
    [row] = _treatments(result)
    assert result["winning_variant_id"] == row["variant_id"]
    assert row["incremental_impact"] < 0
    assert row["incremental_impact_ci"][1] < 0
    assert row["relative_improvement_pct_ci"][1] < 0


# ---------------------------------------------------------------------------
# S2.11 to S2.13: edges, JSON, and every None counted
# ---------------------------------------------------------------------------


def test_every_new_field_is_finite_json():
    for n_t, n_c, level in GRID_COUNTS:
        for _, _, row in _grid(n_t, n_c, level):
            json.dumps({f: row[f] for f in DIFFERENCE_FIELDS}, allow_nan=False)
    for row, _, _ in _mean_rows("none"):
        json.dumps({f: row[f] for f in DIFFERENCE_FIELDS}, allow_nan=False)


def test_an_arm_with_no_users_has_no_difference():
    result = binomial_metric_result(
        [(_T1, 0, 0), (_CONTROL, 500, 0), (_T2, 400, 12)],
        0.05,
        "none",
        metric=_metric(),
    )
    rows = {v["variant_name"]: v for v in result["variants"]}
    assert all(rows["t1"][f] is None for f in DIFFERENCE_FIELDS)
    assert all(rows["control"][f] is None for f in DIFFERENCE_FIELDS)
    assert rows["t2"]["incremental_impact"] == 12.0
    assert rows["t2"]["relative_improvement_pct_ci"] is None
    no_control_users = _proportion(5, 50, 0, 0)
    assert all(no_control_users[f] is None for f in DIFFERENCE_FIELDS)


def test_one_user_per_arm():
    """Per path (PS-8): a proportion with 1 user per arm is computed; a mean
    with fewer than 2 users has no difference."""
    row = _proportion(1, 1, 0, 1)
    assert row["absolute_difference"] == 1.0
    low, high = row["absolute_difference_ci"]
    assert -1.0 <= low < 0.0 < high <= 1.0
    [mean_row] = _treatments(_mean_result([1.0, 2.0, 3.0], [[4.0]]))
    assert all(mean_row[f] is None for f in DIFFERENCE_FIELDS)
    [small_control] = _treatments(_mean_result([1.0], [[4.0, 5.0, 6.0]]))
    assert all(small_control[f] is None for f in DIFFERENCE_FIELDS)


def test_everything_converted_in_both_arms():
    row = _proportion(20, 20, 20, 20)
    low, high = row["absolute_difference_ci"]
    assert low < 0.0 < high and low == pytest.approx(-high, abs=1e-15)
    rel_low, rel_high = row["relative_improvement_pct_ci"]
    assert rel_low < 0.0 < rel_high


def test_a_treatment_whose_users_all_have_0_reads_minus_100_percent():
    """PS-8: the relative interval of a treatment that does not vary at 0,
    against a control that does, is (-100%, -100%) as computed."""
    rng = np.random.default_rng(1_496_011)
    control = list(rng.poisson(2.0, size=200).astype(float))
    [row] = _treatments(_mean_result(control, [[0.0] * 200]))
    assert row["relative_improvement_pct_ci"] == (-100.0, -100.0)
    low, high = row["absolute_difference_ci"]
    assert high < 0


#: The architect's grid: arm sizes 5 to 200, every count, the control's at
#: least 1: 175,130 tables.  Relative intervals that are None, per level:
#: none, because a proportion's relative interval is None only when the
#: control has no conversions (PS-1 reopened, T234).  The count is also
#: recomputed below from scipy's Wilson limits.
ARCHITECT_SIZES = (5, 10, 20, 30, 50, 100, 200)
ARCHITECT_RELATIVE_NONE = {0.80: 0, 0.95: 0, 0.99: 0}
#: The same for the grids above, through ``binomial_metric_result``: the
#: tables whose control has no conversions, ``n_t + 1`` per grid.
GRID_RELATIVE_NONE = {
    (20, 20, 0.95): 21,
    (50, 50, 0.95): 51,
    (100, 37, 0.95): 101,
    (37, 100, 0.95): 38,
    (20, 200, 0.95): 21,
    (20, 200, 0.99): 21,
    (50, 100, 0.95): 51,
    (50, 100, 0.99): 51,
}


def _scipy_controls_with_no_bound(sizes: Sequence[int], level: float, start: int):
    """``(x_c, n_c)`` whose scipy Wilson lower limit is at or below 0, where
    a ratio of proportions has no upper bound: a control with no conversions.
    The test's own predicate, never the code's."""
    out = set()
    for n_c in sizes:
        for x_c in range(start, n_c + 1):
            _, low, _ = _scipy_wilson(x_c, n_c, level)
            if low <= 0.0:
                out.add((x_c, n_c))
    return out


@pytest.mark.parametrize("grid", sorted(GRID_RELATIVE_NONE), ids=str)
def test_every_none_on_the_grids_is_counted(grid):
    """S2.13 per field: the difference's interval is never None with users
    in both arms; the relative interval is None exactly as often as the
    control has no upper bound by scipy's Wilson limits (no conversions)."""
    n_t, n_c, level = grid
    rows = _grid(n_t, n_c, level)
    absolute_none = sum(row["absolute_difference_ci"] is None for _, _, row in rows)
    relative_none = sum(
        row["relative_improvement_pct_ci"] is None for _, _, row in rows
    )
    impact_none = sum(row["incremental_impact_ci"] is None for _, _, row in rows)
    unbounded = _scipy_controls_with_no_bound((n_c,), level, start=0)
    assert (absolute_none, impact_none) == (0, 0)
    assert relative_none == GRID_RELATIVE_NONE[grid] == len(unbounded) * (n_t + 1)


@pytest.mark.parametrize("level", sorted(ARCHITECT_RELATIVE_NONE))
def test_every_none_on_the_architects_grid_is_counted(level):
    """The fields come from ``_proportion_difference``, which
    ``binomial_metric_result`` calls for each treatment: here directly, as
    the grid is 175,130 tables."""
    z = ssa.two_sided_z(level)
    tables = absolute_none = relative_none = below_minus_100 = 0
    for n_c in ARCHITECT_SIZES:
        for x_c in range(1, n_c + 1):
            for n_t in ARCHITECT_SIZES:
                for x_t in range(n_t + 1):
                    fields = ssa._proportion_difference(n_t, x_t, n_c, x_c, z)
                    tables += 1
                    absolute_none += fields["absolute_difference_ci"] is None
                    relative = fields["relative_improvement_pct_ci"]
                    relative_none += relative is None
                    below_minus_100 += relative is not None and relative[0] < -100.0
    unbounded = _scipy_controls_with_no_bound(ARCHITECT_SIZES, level, start=1)
    per_control = sum(n + 1 for n in ARCHITECT_SIZES)
    assert tables == 175_130
    assert absolute_none == 0
    assert below_minus_100 == 0
    assert (
        relative_none == ARCHITECT_RELATIVE_NONE[level] == len(unbounded) * per_control
    )


# ---------------------------------------------------------------------------
# S2.15: the holdout reads three keys and is unchanged
# ---------------------------------------------------------------------------


def test_the_holdout_comparison_keeps_its_keys():
    assert set(holdout_results.compare(1000, 100, 9000, 1000)) == {
        "absolute",
        "relative_pct",
        "ci_lower",
        "ci_upper",
        "p_value",
        "is_significant",
        "always_valid_ci_lower",
        "always_valid_ci_upper",
    }


def test_the_export_has_no_interval_or_impact_column():
    """The export's rows are unchanged: no #1496 field, no interval."""
    for row in (ExperimentExportRow, VariantExportRow):
        columns = set(row.model_fields)
        assert not columns & set(DIFFERENCE_FIELDS), row.__name__
        assert not [c for c in columns if c.endswith("_ci") or "impact" in c]


def test_the_warehouse_mean_estimator_is_unchanged():
    """``mean_metric_result``'s variants keep their keys (no #1496 field)."""
    result = mean_metric_result(
        2.0,
        [(_CONTROL, 50, 10.0, 120.0), (_T1, 50, -10.0, 110.0)],
        0.05,
        "none",
        metric=_metric("mean"),
    )
    for variant in result["variants"]:
        assert not set(DIFFERENCE_FIELDS) & set(variant)

"""How often the heterogeneous-effects banner fires, with and without a real
difference between segments (#1969).

A breakdown's ``has_heterogeneous_effects`` (the banner) comes from
``DimensionalAnalysisService.detect_hte``: a test that the first treatment's
difference from the control, in percentage points, is the same in every
segment that passes the floor (``hte_tested_segments``), at the experiment's
alpha.  Until engine 1.12.0 it also fired whenever the lifts' spread was large
or their signs differed, which on the case reported in #1969 (4 segments of 90
users per arm, 10% -> 18% in every segment) was about 60% of experiments.

Each shape here is 2,000 seeded tables: ``numpy.random.RandomState(1969)``,
the control's converting users drawn first (``binomial(users, rate,
size=(2000, segments))``), then the treatment's.  RandomState's stream is
frozen (NEP 19), so every count is the same on every run.  Each count is pinned
exactly, after a band that a change in the method would leave: a band alone
misses a plant that moves a count a little, an exact pin alone reads a numpy
change as a method change.

The 2,000-table counts come from a batched replica of the test
(``interaction_analysis.fit_additive_batch`` and ``pearson_statistic_batch``
on every table at once), which calls the production floor,
``hte_tested_segments``, not a copy of it.  A defect planted in
``detect_hte`` itself never reaches the replica, so the pins cannot see it.
``test_the_replica_agrees_with_detect_hte`` can: it sends the first 200 tables
of four shapes through ``compute_segment_results`` and the real ``detect_hte``
at alpha 0.01, 0.05 and 0.10 and requires the same 2,400 decisions as the
replica.

Measured at this seed and draw order (flagged of 2,000 at alpha .05 unless
noted):

* RW-1's null (the reported case): 24, 108 and 219 at .01, .05 and .10; the
  0.47.0 rule on the same tables: 1216;
* a strong heterogeneity at the same size (30/30/5/5% against 10%): 1967;
  a moderate one at 500 per arm (18/18/10/10% against 10%): 1835;
* six nulls the floor tests: 117, 99, 108 and 124 on four ordinary shapes,
  121 and 139 on the two in the liberal corner (many equal segments of 50
  users at a high base rate; 5.7% and 5.5% at 20,000 tables);
* two nulls the floor never tests (0 of 2,000): the documented "too little
  data to tell" false.

Bands: the central 99.9% of Binomial(2,000, alpha) at .01, .05 and .10 is
[7, 36], [69, 133] and [157, 245]; the corner's bound, 147, is the 99.9%
upper limit of Binomial(2,000, .0568).
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Dict, List, NamedTuple, Tuple

import numpy as np
import pytest
from scipy import stats

from backend.app.services import dimensional_analysis_service as das
from backend.app.services import interaction_analysis as ia

pytestmark = pytest.mark.unit

SIMS = 2_000
SEED = 1969
ALPHAS = (0.01, 0.05, 0.10)
#: The first tables of a shape that go through the real path.
CROSS_CHECK_TABLES = 200

#: The central 99.9% of Binomial(SIMS, alpha): its 0.05% and 99.95% points.
BANDS = {0.01: (7, 36), 0.05: (69, 133), 0.10: (157, 245)}
#: The 99.9% upper limit of Binomial(SIMS, .0568), the liberal corner's rate.
CORNER_BOUND = 147


class Shape(NamedTuple):
    """Users per segment and conversion rate, for the control and the treatment."""

    control_users: Tuple[int, ...]
    control_rate: Any
    treatment_users: Tuple[int, ...]
    treatment_rate: Any


SHAPES: Dict[str, Shape] = {
    # The case reported in #1969 (RW-1): the same lift in every segment.
    "rw1_null": Shape((90,) * 4, 0.10, (90,) * 4, 0.18),
    "strong_heterogeneity": Shape((90,) * 4, 0.10, (90,) * 4, (0.30, 0.30, 0.05, 0.05)),
    "moderate_heterogeneity": Shape(
        (500,) * 4, 0.10, (500,) * 4, (0.18, 0.18, 0.10, 0.10)
    ),
    "tiny": Shape((300, 90, 30, 10), 0.10, (300, 90, 30, 10), 0.18),
    "two_large_two_tiny": Shape((500, 500, 5, 2), 0.10, (500, 500, 5, 2), 0.18),
    "sparse_4x500": Shape((500,) * 4, 0.02, (500,) * 4, 0.04),
    # The control and the treatment split 600/200 and 200/600 by turns.
    "unequal_split": Shape((600, 200, 600, 200), 0.10, (200, 600, 200, 600), 0.15),
    "corner_22x50_at_50": Shape((50,) * 22, 0.50, (50,) * 22, 0.50),
    "corner_21x50_at_30": Shape((50,) * 21, 0.30, (50,) * 21, 0.38),
    "untested_21x50": Shape((50,) * 21, 0.10, (50,) * 21, 0.18),
    "untested_sparse_4x90": Shape((90,) * 4, 0.02, (90,) * 4, 0.04),
    # Two segments always pass the floor, the 60 and 40 only sometimes.
    "floor_binding": Shape((300, 90, 60, 40), 0.10, (300, 90, 60, 40), 0.18),
}

#: Flagged of SIMS on RW-1's null at each alpha.
RW1_NULL_PINS = {0.01: 24, 0.05: 108, 0.10: 219}
#: Flagged of SIMS by the 0.47.0 rule on RW-1's null.
RW1_NULL_0_47_0_PIN = 1216
STRONG_PIN = 1967
MODERATE_PIN = 1835
#: (flagged at .05, tables with at least two tested segments).
ORDINARY_NULL_PINS = {
    "tiny": (117, 1988),
    "two_large_two_tiny": (99, 2000),
    "sparse_4x500": (108, 2000),
    "unequal_split": (124, 2000),
}
CORNER_NULL_PINS = {"corner_22x50_at_50": 121, "corner_21x50_at_30": 139}
UNTESTED_NULLS = ("untested_21x50", "untested_sparse_4x90")
CROSS_CHECKED = ("rw1_null", "unequal_split", "moderate_heterogeneity", "floor_binding")


@lru_cache(maxsize=None)
def _draws(name: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Users and converting users of every table: the control's, then the treatment's."""
    shape = SHAPES[name]
    control_users = np.asarray(shape.control_users)
    treatment_users = np.asarray(shape.treatment_users)
    rs = np.random.RandomState(SEED)
    segments = len(control_users)
    control_conv = rs.binomial(control_users, shape.control_rate, size=(SIMS, segments))
    treatment_conv = rs.binomial(
        treatment_users, shape.treatment_rate, size=(SIMS, segments)
    )
    return control_users, control_conv, treatment_users, treatment_conv


@lru_cache(maxsize=None)
def _replica(name: str) -> Tuple[np.ndarray, np.ndarray]:
    """Whether each table is tested, and its p-value (nan when it is not).

    The production floor picks each table's segments; the tables with the
    same number of tested segments are fitted together.
    """
    control_users, control_conv, treatment_users, treatment_conv = _draws(name)
    tested = np.zeros(SIMS, dtype=bool)
    p_values = np.full(SIMS, np.nan)
    by_count: Dict[int, List[Tuple[int, List[int]]]] = {}
    for table in range(SIMS):
        rows = zip(
            control_users, control_conv[table], treatment_users, treatment_conv[table]
        )
        columns = das.hte_tested_segments(list(rows))
        if len(columns) >= 2:
            by_count.setdefault(len(columns), []).append((table, columns))
    for count, items in by_count.items():
        tables = np.array([table for table, _ in items])
        columns = np.array([cols for _, cols in items])
        n = np.stack([control_users[columns], treatment_users[columns]], axis=1)
        x = np.stack(
            [
                control_conv[tables[:, None], columns],
                treatment_conv[tables[:, None], columns],
            ],
            axis=1,
        )
        n, x = n.astype(float), x.astype(float)
        statistic = ia.pearson_statistic_batch(n, x, ia.fit_additive_batch(n, x))
        assert np.isfinite(statistic).all(), name
        p_values[tables] = stats.chi2.sf(statistic, count - 1)
        tested[tables] = True
    return tested, p_values


def _flagged(name: str, alpha: float) -> int:
    tested, p_values = _replica(name)
    return int((tested & (p_values < alpha)).sum())


def _tested(name: str) -> int:
    return int(_replica(name)[0].sum())


def _segments(name: str, table: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """One table as the breakdown hands it to compute_segment_results."""
    control_users, control_conv, treatment_users, treatment_conv = _draws(name)
    return {
        f"segment-{j}": {
            "control": {
                "variant_name": "control",
                "is_control": True,
                "total": int(control_users[j]),
                "conversions": int(control_conv[table, j]),
            },
            "treatment": {
                "variant_name": "treatment",
                "is_control": False,
                "total": int(treatment_users[j]),
                "conversions": int(treatment_conv[table, j]),
            },
        }
        for j in range(len(control_users))
    }


def _rule_of_0_47_0(segment_results) -> bool:
    """detect_hte as released in 0.47.0, kept to measure the "before".

    A chi-squared test of the converting users alone at a fixed 0.05, then,
    when it did not reject or failed, a flag for a coefficient of variation of
    the lifts above 0.5 or for lifts of both signs.
    """
    if len(segment_results) < 2:
        return False
    data = []
    for seg in segment_results:
        ctrl = next((v for v in seg.variants if v.is_control), None)
        treat = next((v for v in seg.variants if not v.is_control), None)
        if ctrl is None or treat is None:
            continue
        data.append(
            (
                ctrl.sample_size,
                ctrl.conversions or 0,
                treat.sample_size,
                treat.conversions or 0,
            )
        )
    if len(data) < 2:
        return False
    ctrl_conv = np.array([d[1] for d in data], dtype=float)
    ctrl_total = np.array([d[0] for d in data], dtype=float)
    treat_conv = np.array([d[3] for d in data], dtype=float)
    treat_total = np.array([d[2] for d in data], dtype=float)
    ctrl_rate = np.divide(
        ctrl_conv, ctrl_total, out=np.zeros_like(ctrl_conv), where=ctrl_total > 0
    )
    treat_rate = np.divide(
        treat_conv, treat_total, out=np.zeros_like(treat_conv), where=treat_total > 0
    )
    lift = treat_rate - ctrl_rate
    contingency = np.array([[int(c) for c in treat_conv], [int(c) for c in ctrl_conv]])
    if treat_total.sum() == 0 or ctrl_total.sum() == 0:
        return False
    try:
        _, p_value, dof, _ = stats.chi2_contingency(contingency)
        if p_value < 0.05 and dof > 0:
            return True
    except Exception:
        pass
    lift_std = float(np.std(lift, ddof=1))
    lift_mean = float(np.abs(np.mean(lift)))
    if lift_mean > 0 and lift_std / lift_mean > 0.5:
        return True
    return bool(any(v > 0 for v in lift) and any(v < 0 for v in lift))


# ---------------------------------------------------------------------------
# The reported case, before and after
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alpha", ALPHAS)
def test_the_rw1_null_is_flagged_about_alpha_of_the_time(alpha):
    """RW-1's null: the same 8-point lift in 4 segments of 90 users per arm.
    At .05 this is the "after" of #1969; at .01 and .10 it shows the test
    uses the alpha it is given, not a fixed one."""
    flagged = _flagged("rw1_null", alpha)
    low, high = BANDS[alpha]
    assert low <= flagged <= high, f"alpha {alpha}: {flagged} of {SIMS}"
    assert flagged == RW1_NULL_PINS[alpha]


def test_the_harness_is_rw1s_case_under_the_0_47_0_rule():
    """The "before": on the same tables the 0.47.0 rule flags about 60%, so the
    harness is the reported case.  Every built segment has one control and one
    treatment of 90 users, so the control is really flagged."""
    service = das.DimensionalAnalysisService()
    flagged = 0
    for table in range(SIMS):
        results = service.compute_segment_results(_segments("rw1_null", table), 0.05)
        assert len(results) == 4
        for seg in results:
            assert sorted((v.is_control, v.sample_size) for v in seg.variants) == [
                (False, 90),
                (True, 90),
            ]
        flagged += _rule_of_0_47_0(results)
    assert flagged == RW1_NULL_0_47_0_PIN


# ---------------------------------------------------------------------------
# A real difference between segments is still found
# ---------------------------------------------------------------------------


def test_a_strong_heterogeneity_at_the_issues_size_is_detected():
    """4 x 90 per arm, control 10%, treatment 30/30/5/5%: at least 95%."""
    flagged = _flagged("strong_heterogeneity", 0.05)
    assert flagged >= 0.95 * SIMS, flagged
    assert flagged == STRONG_PIN


def test_a_moderate_heterogeneity_at_a_realistic_size_is_detected():
    """4 x 500 per arm, control 10%, treatment 18/18/10/10%: at least 80% (the
    platform's default power), and within 3 standard errors of the test's
    theoretical power, a noncentral chi-squared with the statistic of the
    expected table as its noncentrality."""
    shape = SHAPES["moderate_heterogeneity"]
    n = np.array([shape.control_users, shape.treatment_users], dtype=float)
    expected = n * np.array([[shape.control_rate] * 4, list(shape.treatment_rate)])
    noncentrality = float(
        ia.pearson_statistic_batch(
            n[None], expected[None], ia.fit_additive_batch(n[None], expected[None])
        )[0]
    )
    df = n.shape[1] - 1
    power = float(stats.ncx2.sf(stats.chi2.isf(0.05, df), df, noncentrality))
    se = (SIMS * power * (1 - power)) ** 0.5

    flagged = _flagged("moderate_heterogeneity", 0.05)
    assert flagged >= 0.80 * SIMS, flagged
    assert abs(flagged - SIMS * power) <= 3 * se, (flagged, SIMS * power, se)
    assert flagged == MODERATE_PIN


# ---------------------------------------------------------------------------
# No anti-conservative drift, on shapes the floor tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ORDINARY_NULL_PINS))
def test_an_ordinary_null_is_flagged_at_most_alpha(name):
    pinned, pinned_tested = ORDINARY_NULL_PINS[name]
    flagged, tested = _flagged(name, 0.05), _tested(name)
    assert flagged <= BANDS[0.05][1], f"{name}: {flagged} of {SIMS}"
    assert tested >= 1900, f"{name}: {tested} of {SIMS} tested"
    assert (flagged, tested) == (pinned, pinned_tested)


@pytest.mark.parametrize("name", sorted(CORNER_NULL_PINS))
def test_a_null_in_the_liberal_corner_stays_below_its_bound(name):
    """Many equal segments of 50 users at a high base rate: the test is mildly
    liberal there (5.5% to 5.7% at alpha .05), which 133 would fail on honest
    seeds; the bound is derived from that rate, not from these counts."""
    flagged, tested = _flagged(name, 0.05), _tested(name)
    assert flagged <= CORNER_BOUND, f"{name}: {flagged} of {SIMS}"
    assert tested == SIMS, f"{name}: {tested} of {SIMS} tested"
    assert flagged == CORNER_NULL_PINS[name]


@pytest.mark.parametrize("name", UNTESTED_NULLS)
def test_a_shape_below_the_floor_is_never_tested(name):
    """50 users at 14% and 90 users at 3% leave fewer than 10 expected
    converters per arm: never tested, so never flagged."""
    assert _tested(name) == 0


# ---------------------------------------------------------------------------
# The replica is the real path
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", CROSS_CHECKED)
def test_the_replica_agrees_with_detect_hte(name, monkeypatch):
    """The first 200 tables through compute_segment_results and the real
    detect_hte, at each alpha, give the replica's decisions: so the pins above
    are the product's counts, and a defect in detect_hte that the replica does
    not share fails here.  interaction_test is memoised per table inside this
    test only (each table is tested at three alphas)."""
    real_test = ia.interaction_test
    memo: Dict[Any, Any] = {}

    def memoised(n, x):
        key = (repr(n), repr(x))
        if key not in memo:
            memo[key] = real_test(n, x)
        return memo[key]

    monkeypatch.setattr(ia, "interaction_test", memoised)
    service = das.DimensionalAnalysisService()
    started = time.perf_counter()
    decisions = {}
    for table in range(CROSS_CHECK_TABLES):
        for alpha in ALPHAS:
            results = service.compute_segment_results(_segments(name, table), alpha)
            decisions[table, alpha] = service.detect_hte(results, alpha=alpha)
    seconds = time.perf_counter() - started

    tested, p_values = _replica(name)
    disagreements = [
        (table, alpha, decision, bool(tested[table] and p_values[table] < alpha))
        for (table, alpha), decision in decisions.items()
        if decision != bool(tested[table] and p_values[table] < alpha)
    ]
    print(f"{name}: {len(decisions)} decisions through detect_hte in {seconds:.1f} s")
    assert len(decisions) == CROSS_CHECK_TABLES * len(ALPHAS)
    assert all(type(decision) is bool for decision in decisions.values())
    assert disagreements == [], (
        f"{name}: {len(disagreements)} of {len(decisions)} decisions differ "
        f"from the replica (table, alpha, detect_hte, replica): {disagreements[:10]}"
    )

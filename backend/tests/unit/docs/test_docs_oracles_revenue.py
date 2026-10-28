"""The revenue oracles, and the results journey's revenue, guardrail and planned-sample steps.

The results journey (``experiment-results.yaml``) ends with two experiments
whose primary metric is a revenue metric, each sent a known population whose
purchases carry values from the traffic step's ramp (``value``), the second
with every value doubled; the first also lists a lower-is-better metric and
reads its planned sample. Their numbers are held to oracles written from the
docs and textbook formulas (``docs_runner/oracles.py``). What decides whether
those checks can fail is pure, and is tested here, in the unit job:

* each oracle is its formula: the ramp's sums are the values the traffic step
  sends; a mean is over every ASSIGNED user, 0 for a non-buyer; Welch's p-value
  and interval are scipy's on the users' values written out one by one, and
  the textbook formula's by hand;
* the journey's exact numbers are binary fractions a double holds whatever the
  order of the additions, so ``rel: 0`` can hold, and the nearest wrong answers
  fall outside every tolerance;
* doubling every value doubles the means, the difference and the interval,
  leaves the buyers and the p-value as they were, and the journey's two
  experiments differ in nothing else;
* each oracle refuses what no step can send (a decimal ramp, a value past
  10^12, a variant of one user, no variation);
* each read's oracle is given its traffic's counts and ramp; the
  lower-is-better metric is a listing whose rise leaves the recommendation;
  the planned sample is the formula's for the share of buyers, and the results
  pages' "of 57,763" is the formula's too;
* the runner fails each step on the answer each defect would give.

No network, no browser, no product code.
"""

from __future__ import annotations

import copy
import importlib
import math
import statistics
import sys
import types
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pytest
import yaml
from scipy import stats

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import log, oracles, traffic

JOURNEY = RUNNER_ROOT / "journeys" / "experiment-results.yaml"

#: Each revenue read, and the traffic step that sends its experiment's users.
REVENUE_READS = {
    "revenue-results": "revenue-traffic",
    "doubled-results": "doubled-traffic",
}
COUNT_ARGS = (
    "control_users",
    "control_converted",
    "treatment_users",
    "treatment_converted",
)
RAMP_ARGS = ("base", "step", "modulo")
#: The tolerance of the numbers that are not exact (the p-value, the interval).
REL = 1e-9
#: An experiment's confidence level when it is created without one, and the
#: Sample Size tab's defaults ("statistics/power-analysis.md": 5% relative
#: effect when none was saved, 80% power).
DEFAULT_CONFIDENCE = 0.95
DEFAULT_MDE = 0.05
DEFAULT_POWER = 0.8
#: ``getting-started/limitations.md``: a value larger than this adds nothing.
VALUE_LIMIT = 10**12


@pytest.fixture(scope="module")
def steps() -> List[Dict[str, Any]]:
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))["steps"]


def _step(steps: List[Dict[str, Any]], step_id: str) -> Dict[str, Any]:
    return next(step for step in steps if step["id"] == step_id)


def _index(steps: List[Dict[str, Any]], step_id: str) -> int:
    return next(i for i, step in enumerate(steps) if step["id"] == step_id)


def _sent(traffic_step: Dict[str, Any]) -> Dict[str, int]:
    """The traffic step's counts, under the oracles' argument names."""
    variants = traffic_step["do"]["traffic"]["variants"]
    return {
        "control_users": variants["Control"]["assigned"],
        "control_converted": variants["Control"]["converted"],
        "treatment_users": variants["Treatment"]["assigned"],
        "treatment_converted": variants["Treatment"]["converted"],
    }


def _ramp(traffic_step: Dict[str, Any]) -> Dict[str, int]:
    value = traffic_step["do"]["traffic"]["value"]
    assert value["mode"] == "ramp"
    return {name: value[name] for name in RAMP_ARGS}


def _args(steps, read_id: str) -> Dict[str, int]:
    """Counts and ramp of the traffic behind *read_id*."""
    behind = _step(steps, REVENUE_READS[read_id])
    return {**_sent(behind), **_ramp(behind)}


def _values(users: int, converted: int, base: int, step: int, modulo: int) -> List[int]:
    """Every assigned user's value, written out: the buyers' from the runner's
    own ramp, then a 0 for every user who bought nothing."""

    class _Value:
        mode = "ramp"

    ramp = _Value()
    ramp.base, ramp.step, ramp.modulo = base, step, modulo
    return list(traffic.ramp_values(ramp, converted)) + [0] * (users - converted)


def _arrays(args: Dict[str, int]) -> Tuple[np.ndarray, np.ndarray]:
    ramp = (args["base"], args["step"], args["modulo"])
    control = _values(args["control_users"], args["control_converted"], *ramp)
    treatment = _values(args["treatment_users"], args["treatment_converted"], *ramp)
    return np.array(control, dtype=float), np.array(treatment, dtype=float)


def _counts(args: Dict[str, int]) -> Tuple[int, ...]:
    return tuple(args[name] for name in COUNT_ARGS + RAMP_ARGS)


def _oracle_calls(node: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        if "name" in node and "args" in node and node["name"] in oracles.ORACLES:
            found.append(node)
        for value in node.values():
            found.extend(_oracle_calls(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_oracle_calls(value))
    return found


#: The journey's populations, written here as literals as well: the oracles'
#: answers below are checked against them, and the journey against both.
REVENUE = {
    "control_users": 256,
    "control_converted": 64,
    "treatment_users": 256,
    "treatment_converted": 112,
    "base": -20,
    "step": 20,
    "modulo": 6,
}
DOUBLED = {**REVENUE, "base": -40, "step": 40}

#: Other populations the oracles must also agree with scipy on: unequal users,
#: a ramp that wraps late, a single value, mostly refunds.
CASES = [
    REVENUE,
    DOUBLED,
    {**REVENUE, "control_users": 300, "treatment_users": 200},
    {**REVENUE, "modulo": 50, "base": 5, "step": 3},
    {**REVENUE, "modulo": 1, "base": 49},
    {**REVENUE, "base": -90, "step": 25, "modulo": 5},
    {
        "control_users": 3,
        "control_converted": 1,
        "treatment_users": 2,
        "treatment_converted": 2,
        "base": 7,
        "step": 1,
        "modulo": 2,
    },
]


# ---------------------------------------------------------------------------
# The oracles are their formulas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "converted, base, step, modulo",
    [
        (0, 10, 1, 7),
        (9, 10, 1, 7),
        (64, -20, 20, 6),
        (112, -20, 20, 6),
        (5, 10, 1, 7),
        (13, -5, 0, 1),
        (13, 3, 4, 1),
        (40, 100, -7, 9),
    ],
)
def test_the_ramp_sums_are_what_the_traffic_step_sends(converted, base, step, modulo):
    sent = _values(converted, converted, base, step, modulo)
    total, squares = oracles._ramp_sums(converted, base, step, modulo)
    assert total == math.fsum(sent) and isinstance(total, int)
    assert squares == math.fsum(v * v for v in sent) and isinstance(squares, int)


def test_the_traffic_steps_docstring_example_gives_its_total():
    """``model.TrafficValue``: 10/1/7, 9 converters, 112 in all."""
    assert oracles._ramp_sums(9, 10, 1, 7)[0] == 112


def test_the_journeys_means_and_difference_are_the_totals_over_every_user():
    """-20, 0, 20, 40, 60, 80 sum to 180: Control's 64 buyers are 10 rounds and
    -20, 0, 20, 40 (1840); Treatment's 112 are 18 rounds and -20, 0, 20, 40
    (3280). Over 256 users each, buyers or not."""
    assert oracles._ramp_sums(64, -20, 20, 6)[0] == 1840
    assert oracles._ramp_sums(112, -20, 20, 6)[0] == 3280
    counts = _counts(REVENUE)
    assert oracles.revenue_mean(*counts, variant="control") == 1840 / 256 == 7.1875
    assert oracles.revenue_mean(*counts, variant="treatment") == 3280 / 256 == 12.8125
    assert oracles.revenue_difference(*counts) == 5.625
    control, treatment = _arrays(REVENUE)
    assert oracles.revenue_mean(*counts, variant="control") == control.mean()
    assert oracles.revenue_mean(*counts) == treatment.mean()


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(map(str, c.values())))
def test_welch_is_scipys_on_the_values_written_out(case):
    control, treatment = _arrays(case)
    counts = _counts(case)
    welch = stats.ttest_ind(treatment, control, equal_var=False)
    assert oracles.revenue_welch_p(*counts) == pytest.approx(welch.pvalue, rel=1e-12)
    for level in (0.9, 0.95, 0.99):
        interval = welch.confidence_interval(level)
        for bound, expected in (("lower", interval.low), ("upper", interval.high)):
            got = oracles.revenue_welch_interval(*counts, level, bound)
            assert got == pytest.approx(expected, rel=1e-12, abs=1e-12), (level, bound)
    exact = Fraction(int(treatment.sum()), len(treatment)) - Fraction(
        int(control.sum()), len(control)
    )
    assert oracles.revenue_difference(*counts) == float(exact)


@pytest.mark.parametrize(
    "case", CASES[:4], ids=["revenue", "doubled", "300-200", "late"]
)
def test_welch_is_the_textbook_formula_by_hand(case):
    """NIST/SEMATECH 1.3.5.3: t on the unpooled standard error, Satterthwaite's df."""
    control, treatment = (list(map(int, a)) for a in _arrays(case))
    v_t = statistics.variance(treatment) / len(treatment)
    v_c = statistics.variance(control) / len(control)
    se = math.sqrt(v_t + v_c)
    t = (statistics.fmean(treatment) - statistics.fmean(control)) / se
    df = (v_t + v_c) ** 2 / (
        v_t**2 / (len(treatment) - 1) + v_c**2 / (len(control) - 1)
    )
    counts = _counts(case)
    assert oracles.revenue_welch_p(*counts) == pytest.approx(
        2 * stats.t.sf(abs(t), df), rel=1e-10
    )
    upper = statistics.fmean(treatment) - statistics.fmean(control)
    upper += stats.t.ppf(0.975, df) * se
    assert oracles.revenue_welch_interval(*counts, 0.95, "upper") == pytest.approx(
        upper, rel=1e-10
    )


def test_the_exact_numbers_are_exact_however_they_are_added():
    """``rel: 0`` holds: with 256 users a variant (a power of two) and whole
    values, each mean, the difference, the grand mean and every partial sum
    on the way is a binary fraction a double holds exactly, so a plain running
    sum in either order, an exact sum, and sums centred on the grand mean all
    give the oracle's number. At 200 users a variant they would not be."""
    for case in (REVENUE, DOUBLED):
        counts = _counts(case)
        control, treatment = _arrays(case)
        everyone = np.concatenate([control, treatment])
        k = math.fsum(everyone) / len(everyone)
        assert Fraction(k) == Fraction(int(everyone.sum()), len(everyone))
        centred = {}
        for name, arm in (("control", control), ("treatment", treatment)):
            running = 0.0
            for value in arm:
                running += value
            backwards = 0.0
            for value in arm[::-1]:
                backwards += value
            wanted = oracles.revenue_mean(*counts, variant=name)
            assert Fraction(wanted) == Fraction(int(arm.sum()), len(arm))
            assert running / len(arm) == backwards / len(arm) == wanted
            assert math.fsum(arm) / len(arm) == wanted
            centred[name] = math.fsum(v - k for v in arm) / len(arm)
            assert k + centred[name] == wanted
        assert centred["treatment"] - centred["control"] == oracles.revenue_difference(
            *counts
        )
    # Why the power of two: 1840 / 200 is no binary fraction.
    assert Fraction(1840 / 200) != Fraction(1840, 200)


def test_doubling_every_value_doubles_the_means_and_leaves_the_p_value():
    """Welch's t is unchanged by a scale: the difference and its standard
    error both double. The buyers do not depend on the values at all."""
    original, doubled = _counts(REVENUE), _counts(DOUBLED)
    for variant in ("control", "treatment"):
        assert oracles.revenue_mean(
            *doubled, variant=variant
        ) == 2 * oracles.revenue_mean(*original, variant=variant)
    assert oracles.revenue_difference(*doubled) == 2 * oracles.revenue_difference(
        *original
    )
    assert oracles.revenue_welch_p(*doubled) == pytest.approx(
        oracles.revenue_welch_p(*original), rel=1e-13
    )
    for bound in ("lower", "upper"):
        assert oracles.revenue_welch_interval(*doubled, 0.95, bound) == pytest.approx(
            2 * oracles.revenue_welch_interval(*original, 0.95, bound), rel=1e-13
        )
    for converted in (64, 112):
        total, squares = oracles._ramp_sums(converted, -20, 20, 6)
        assert oracles._ramp_sums(converted, -40, 40, 6) == (2 * total, 4 * squares)


def _defects(case: Dict[str, int]) -> Dict[str, Dict[str, float]]:
    """What each defect would answer on *case*, by the numbers it moves."""
    control, treatment = _arrays(case)
    buyers_c = control[: case["control_converted"]]
    buyers_t = treatment[: case["treatment_converted"]]
    pooled = stats.ttest_ind(treatment, control, equal_var=True)
    welch = stats.ttest_ind(treatment, control, equal_var=False)
    se = math.sqrt(
        treatment.var(ddof=1) / len(treatment) + control.var(ddof=1) / len(control)
    )
    diff = treatment.mean() - control.mean()
    kept_c = control[control >= 0]
    kept_t = treatment[treatment >= 0]
    refunds_dropped_c = kept_c.sum() / len(control)
    refunds_dropped_t = kept_t.sum() / len(treatment)
    return {
        "the mean over the buyers only": {
            "control_mean": buyers_c.mean(),
            "treatment_mean": buyers_t.mean(),
            "difference": buyers_t.mean() - buyers_c.mean(),
            "p": stats.ttest_ind(buyers_t, buyers_c, equal_var=False).pvalue,
        },
        "the refunds dropped": {
            "control_mean": refunds_dropped_c,
            "treatment_mean": refunds_dropped_t,
            "difference": refunds_dropped_t - refunds_dropped_c,
        },
        "the total, not the mean": {
            "control_mean": control.sum(),
            "treatment_mean": treatment.sum(),
            "difference": treatment.sum() - control.sum(),
        },
        "Student's t with a pooled variance": {
            "p": pooled.pvalue,
            "lower": pooled.confidence_interval(0.95).low,
            "upper": pooled.confidence_interval(0.95).high,
        },
        "a normal tail in place of Student's": {
            "p": 2 * stats.norm.sf(abs(float(welch.statistic))),
            "lower": diff - stats.norm.ppf(0.975) * se,
            "upper": diff + stats.norm.ppf(0.975) * se,
        },
        "one-sided": {"p": float(welch.pvalue) / 2},
        "the smaller arm's n - 1 as the degrees of freedom": {
            "p": 2 * stats.t.sf(abs(float(welch.statistic)), len(control) - 1),
            "lower": diff - stats.t.ppf(0.975, len(control) - 1) * se,
            "upper": diff + stats.t.ppf(0.975, len(control) - 1) * se,
        },
        "an interval at 90%": {
            "lower": welch.confidence_interval(0.9).low,
            "upper": welch.confidence_interval(0.9).high,
        },
        "an interval at 97.5%": {
            "lower": welch.confidence_interval(0.975).low,
            "upper": welch.confidence_interval(0.975).high,
        },
    }


def _right(case: Dict[str, int]) -> Dict[str, float]:
    counts = _counts(case)
    return {
        "control_mean": oracles.revenue_mean(*counts, variant="control"),
        "treatment_mean": oracles.revenue_mean(*counts),
        "difference": oracles.revenue_difference(*counts),
        "p": oracles.revenue_welch_p(*counts),
        "lower": oracles.revenue_welch_interval(*counts, 0.95, "lower"),
        "upper": oracles.revenue_welch_interval(*counts, 0.95, "upper"),
    }


#: The tolerance each number is held to in the journey.
TOLERANCE = {
    "control_mean": 0.0,
    "treatment_mean": 0.0,
    "difference": 0.0,
    "p": REL,
    "lower": REL,
    "upper": REL,
}


@pytest.mark.parametrize("case", [REVENUE, DOUBLED], ids=["revenue", "doubled"])
def test_each_defects_answer_is_far_outside_its_tolerance(case):
    """Each wrong number lies more than 1,000 times its tolerance away (or,
    for an exact number, differs at all and by more than 0.1%)."""
    right = _right(case)
    for defect, numbers in _defects(case).items():
        for name, wrong in numbers.items():
            moved = abs(wrong - right[name]) / abs(right[name])
            floor = 1000 * TOLERANCE[name] if TOLERANCE[name] else 1e-3
            assert moved > floor, (defect, name, moved)


def test_the_pooled_t_moves_only_through_its_degrees_of_freedom():
    """With the same users in each variant Student's statistic is Welch's;
    only the degrees of freedom tell them apart (0.17% on the p-value)."""
    control, treatment = _arrays(REVENUE)
    pooled = stats.ttest_ind(treatment, control, equal_var=True)
    welch = stats.ttest_ind(treatment, control, equal_var=False)
    assert float(pooled.statistic) == pytest.approx(float(welch.statistic), rel=1e-12)
    assert pooled.df == 510 and 480 < welch.df < 485
    moved = abs(pooled.pvalue - welch.pvalue) / welch.pvalue
    assert 1e-3 < moved < 2e-3


# ---------------------------------------------------------------------------
# What the oracles refuse
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "change, message",
    [
        ({"base": 10.0}, "base must be a whole number"),
        ({"base": True}, "base must be a whole number"),
        ({"base": "10"}, "base must be a whole number"),
        ({"step": 0.5}, "step must be a whole number"),
        ({"step": False}, "step must be a whole number"),
        ({"modulo": 0}, "modulo must be at least 1"),
        ({"modulo": -6}, "modulo must be at least 1"),
        ({"modulo": 6.0}, "modulo must be a whole number"),
        ({"modulo": True}, "modulo must be a whole number"),
        ({"base": VALUE_LIMIT + 1}, "larger than 10"),
        ({"base": -(VALUE_LIMIT + 1)}, "larger than 10"),
        ({"base": VALUE_LIMIT - 99, "step": 20}, "larger than 10"),
        ({"control_users": 1, "control_converted": 1}, "at least 2 users"),
        ({"treatment_users": 1, "treatment_converted": 0}, "at least 2 users"),
        ({"control_converted": 0, "treatment_converted": 0}, "neither variant"),
        ({"base": 0, "step": 0}, "neither variant"),
        ({"control_converted": 257}, "more control users converted"),
        ({"treatment_users": 0, "treatment_converted": 0}, "has no users"),
        ({"control_users": -1}, "cannot be negative"),
        ({"control_users": 256.0}, "whole number of users"),
        ({"treatment_converted": True}, "whole number of users"),
    ],
)
def test_what_no_traffic_step_can_send_is_refused(change, message):
    counts = _counts({**REVENUE, **change})
    for call in (
        lambda: oracles.revenue_welch_p(*counts),
        lambda: oracles.revenue_welch_interval(*counts),
    ):
        with pytest.raises(ValueError, match=message):
            call()
    if message in ("at least 2 users", "neither variant"):
        # A mean and a difference need no variance.
        oracles.revenue_mean(*counts)
        oracles.revenue_difference(*counts)
    else:
        for call in (
            lambda: oracles.revenue_mean(*counts),
            lambda: oracles.revenue_difference(*counts),
        ):
            with pytest.raises(ValueError, match=message):
                call()


def test_a_value_of_exactly_ten_to_the_twelfth_is_taken():
    """ "larger than 10^12 in size" adds nothing: 10^12 itself is counted."""
    for base in (VALUE_LIMIT, -VALUE_LIMIT):
        counts = _counts({**REVENUE, "base": base, "step": 0})
        assert oracles.revenue_mean(*counts, variant="control") == 64 * base / 256
    counts = _counts({**REVENUE, "base": VALUE_LIMIT - 100, "step": 20})
    assert oracles.revenue_difference(*counts) > 0


@pytest.mark.parametrize(
    "keyword, value, message",
    [
        ("variant", "both", "variant must be"),
        ("confidence_level", 0, "confidence_level must be above 0"),
        ("confidence_level", 1, "confidence_level must be above 0"),
        ("confidence_level", 1.5, "confidence_level must be above 0"),
        ("confidence_level", True, "confidence_level must be a number"),
        ("confidence_level", "0.95", "confidence_level must be a number"),
        ("confidence_level", float("nan"), "confidence_level must be finite"),
        ("bound", "middle", "bound must be"),
    ],
)
def test_an_option_no_read_can_mean_is_refused(keyword, value, message):
    function = (
        oracles.revenue_mean if keyword == "variant" else oracles.revenue_welch_interval
    )
    with pytest.raises(ValueError, match=message):
        function(*_counts(REVENUE), **{keyword: value})


def test_the_revenue_oracles_are_registered():
    for name in (
        "revenue_mean",
        "revenue_difference",
        "revenue_welch_p",
        "revenue_welch_interval",
    ):
        assert oracles.ORACLES[name] is getattr(oracles, name)


# ---------------------------------------------------------------------------
# The journey's steps
# ---------------------------------------------------------------------------
def test_the_journeys_populations_are_the_literals_here(steps):
    assert _args(steps, "revenue-results") == REVENUE
    assert _args(steps, "doubled-results") == DOUBLED


@pytest.mark.parametrize("read_id", REVENUE_READS)
def test_each_revenue_reads_oracles_get_its_traffics_counts_and_ramp(steps, read_id):
    wanted = _args(steps, read_id)
    calls = _oracle_calls(_step(steps, read_id))
    names = {call["name"] for call in calls}
    assert names == {
        "revenue_mean",
        "revenue_difference",
        "revenue_welch_p",
        "revenue_welch_interval",
    }
    for call in calls:
        for name in COUNT_ARGS + RAMP_ARGS:
            assert call["args"][name] == wanted[name], (call["name"], name)
        if call["name"] == "revenue_welch_interval":
            # The experiment is created with no confidence level: its default.
            assert call["args"]["confidence_level"] == DEFAULT_CONFIDENCE


@pytest.mark.parametrize("read_id", REVENUE_READS)
def test_each_revenue_read_holds_the_exact_numbers_exactly(steps, read_id):
    computed = _step(steps, read_id)["expect"]["computed"]
    wanted = _args(steps, read_id)
    sent = _sent(_step(steps, REVENUE_READS[read_id]))
    for path, check in computed.items():
        name = check["oracle"]["name"]
        answer = oracles.ORACLES[name](**check["oracle"]["args"])
        if name in ("revenue_mean", "revenue_difference"):
            assert check["rel"] == 0, path
            # A whole number of 256ths: a double holds the exact answer.
            assert (Fraction(answer) * 256).denominator == 1, path
        else:
            assert check["rel"] == REL, path
    expected = _step(steps, read_id)["expect"]["json"]
    # The buyers are the users who sent the event, whatever its value.
    assert expected["metrics.0.variants.0.sample_size"] == sent["control_users"]
    assert expected["metrics.0.variants.0.conversions"] == sent["control_converted"]
    assert expected["metrics.0.variants.1.sample_size"] == sent["treatment_users"]
    assert expected["metrics.0.variants.1.conversions"] == sent["treatment_converted"]
    assert expected["metrics.0.analysis"] == "mean"
    assert expected["metrics.0.variants.1.statistical_test_used"] == "welch_t_test"
    assert set(wanted) == set(COUNT_ARGS + RAMP_ARGS)


def test_the_doubled_experiment_differs_only_in_its_values(steps):
    first = _step(steps, "revenue-experiment")["do"]["api"]["body"]
    second = _step(steps, "doubled-experiment")["do"]["api"]["body"]
    assert second["metrics"] == first["metrics"][:1]
    assert {k: v for k, v in second.items() if k not in ("name", "key", "metrics")} == {
        k: v for k, v in first.items() if k not in ("name", "key", "metrics")
    }
    sent = _step(steps, "revenue-traffic")["do"]["traffic"]
    doubled = _step(steps, "doubled-traffic")["do"]["traffic"]
    assert doubled["variants"] == sent["variants"]
    assert doubled["event"] == sent["event"] and doubled["users"] != sent["users"]
    assert doubled["value"] == {
        **sent["value"],
        "base": 2 * sent["value"]["base"],
        "step": 2 * sent["value"]["step"],
    }
    # The same buyers are expected in both reads.
    first_read = _step(steps, "revenue-results")["expect"]["json"]
    second_read = _step(steps, "doubled-results")["expect"]["json"]
    for path, value in second_read.items():
        if ".variants." in path:
            assert first_read[path] == value, path


def test_each_revenue_step_comes_after_its_experiment_is_made_and_sent(steps):
    order = [
        "revenue-experiment",
        "revenue-start",
        "revenue-traffic",
        "revenue-results",
        "guardrail-listing",
        "revenue-sample-size",
        "doubled-experiment",
        "doubled-start",
        "doubled-traffic",
        "doubled-results",
    ]
    at = [_index(steps, step_id) for step_id in order]
    assert at == sorted(at)


def test_the_ramp_is_whole_and_inside_the_limit_and_no_user_is_sent_twice(steps):
    """Whole numbers only (the oracles add them as integers), every value at
    most 10^12 in size, and no two traffic steps share an experiment and a
    user prefix: each ramp counts its buyers from 0, so a user sent twice
    would buy twice."""
    pairs = set()
    valued = []
    for step in steps:
        plan = (step.get("do") or {}).get("traffic")
        if plan is None:
            continue
        pair = (plan["experiment"], plan["users"])
        assert pair not in pairs, pair
        pairs.add(pair)
        value = plan.get("value")
        if value is None:
            continue
        valued.append(step["id"])
        assert all(
            isinstance(value[name], int) and not isinstance(value[name], bool)
            for name in RAMP_ARGS
        )
        bought = max(v["converted"] for v in plan["variants"].values())
        sent = _values(bought, bought, value["base"], value["step"], value["modulo"])
        assert max(abs(v) for v in sent) <= VALUE_LIMIT
        # A refund and an order of 0 are among the values sent.
        assert min(sent) < 0 and 0 in sent
    assert valued == list(REVENUE_READS.values())


def test_the_revenue_metric_is_the_primary_on_the_events_sent(steps):
    body = _step(steps, "revenue-experiment")["do"]["api"]["body"]
    event = _step(steps, "revenue-traffic")["do"]["traffic"]["event"]
    revenue, listed = body["metrics"]
    assert revenue["metric_type"] == "revenue" and revenue["is_primary"] is True
    assert revenue["event_name"] == event
    assert "lower_is_better" not in revenue
    # The listed metric counts the same event, so its buyers are known exactly.
    assert listed["event_name"] == event
    assert listed["lower_is_better"] is True and listed["is_primary"] is False
    assert listed["metric_type"] == "conversion"


def test_the_lower_is_better_metric_is_listed_and_decides_nothing(steps):
    """A listing: its name, its direction and its users' counts. Its rise is
    significant, while the primary metric's rise is too, so a guardrail
    verdict would have had a reason to stop SHIP VARIANT and none does."""
    expected = _step(steps, "guardrail-listing")["expect"]["json"]
    sent = _sent(_step(steps, "revenue-traffic"))
    listed = _step(steps, "revenue-experiment")["do"]["api"]["body"]["metrics"][1]
    assert expected["metrics.1.metric_name"] == listed["name"]
    assert expected["metrics.1.lower_is_better"] is True
    assert expected["metrics.1.is_primary"] is False
    assert expected["metrics.1.analysis"] == "proportion"
    assert expected["metrics.0.lower_is_better"] is False
    assert expected["metrics.1.variants.0.sample_size"] == sent["control_users"]
    assert expected["metrics.1.variants.0.conversions"] == sent["control_converted"]
    assert expected["metrics.1.variants.1.sample_size"] == sent["treatment_users"]
    assert expected["metrics.1.variants.1.conversions"] == sent["treatment_converted"]
    assert expected["summary.recommendation"] == "SHIP_VARIANT"
    rise = oracles.fisher_exact_p(**sent)
    assert rise < 1 - DEFAULT_CONFIDENCE and rise == pytest.approx(
        1.1220526e-05, rel=1e-6
    )
    assert sent["treatment_converted"] > sent["control_converted"]
    counts = _counts(REVENUE)
    assert oracles.revenue_welch_p(*counts) < 1 - DEFAULT_CONFIDENCE
    assert oracles.revenue_difference(*counts) > 0
    # Nothing but a listing is expected of the listed metric.
    keys = {path.split(".", 2)[2] for path in expected if path.startswith("metrics.1.")}
    assert keys == {
        "metric_name",
        "is_primary",
        "lower_is_better",
        "analysis",
        "variants.0.variant_name",
        "variants.0.sample_size",
        "variants.0.conversions",
        "variants.1.variant_name",
        "variants.1.sample_size",
        "variants.1.conversions",
    }


def test_the_planned_sample_is_the_formulas_for_the_share_of_buyers(steps):
    """ "a revenue metric's plan is for the share of users who buy": the
    control's 64 buyers of 256, at the 5% default effect, the experiment's 95%
    (alpha 0.05) and 80% power: 19,147 per variant."""
    step = _step(steps, "revenue-sample-size")
    sent = _sent(_step(steps, "revenue-traffic"))
    expected = step["expect"]["json"]
    share = sent["control_converted"] / sent["control_users"]
    assert expected["baseline_rate"] == share == 0.25
    assert expected["mde"] == DEFAULT_MDE and expected["mde_source"] == "default"
    assert expected["power_target"] == DEFAULT_POWER
    assert expected["analysed_as"] == "conversion"
    assert expected["metric_type"] == "revenue"
    assert expected["current_sample_size_per_variant"] == min(
        sent["control_users"], sent["treatment_users"]
    )
    (call,) = _oracle_calls(step)
    assert call["name"] == "sample_size_proportions"
    assert call["args"] == {
        "baseline_rate": share,
        "minimum_detectable_effect": DEFAULT_MDE,
        "alpha": round(1 - DEFAULT_CONFIDENCE, 12),
        "power": DEFAULT_POWER,
    }
    assert step["expect"]["computed"]["required_sample_size_per_variant"]["rel"] == 0
    required = oracles.sample_size_proportions(**call["args"])
    assert required == 19147
    assert expected["is_adequate"] is (
        expected["current_sample_size_per_variant"] >= required
    )
    # The metric names no effect, so the plan is for the default.
    revenue = _step(steps, "revenue-experiment")["do"]["api"]["body"]["metrics"][0]
    assert "expected_effect" not in revenue


@pytest.mark.parametrize(
    "page, traffic_id",
    [
        ("results-page", "traffic"),
        ("ratio-results-page", "ratio-traffic"),
        ("floor-results-page", "floor-traffic"),
    ],
)
def test_each_results_pages_planned_sample_is_the_formulas(steps, page, traffic_id):
    """ "N of 57,763 per variant": the smallest variant's users, against the
    formula at the control's rate (10% on each), the 5% default and 80%."""
    sent = _sent(_step(steps, traffic_id))
    rate = sent["control_converted"] / sent["control_users"]
    required = oracles.sample_size_proportions(
        rate, DEFAULT_MDE, round(1 - DEFAULT_CONFIDENCE, 12), DEFAULT_POWER
    )
    smallest = min(sent["control_users"], sent["treatment_users"])
    line = f'"{smallest} of {int(required):,} per variant"'
    assert line in _step(steps, page)["expect"]["aria"]


# ---------------------------------------------------------------------------
# The runner fails each step on a defect's answer
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute with a stand-in for Playwright, which the unit job lacks."""
    sync_api = types.ModuleType("playwright.sync_api")

    class _Expect:
        def __call__(self, *args, **kwargs):
            raise AssertionError("no browser here")

        def set_options(self, **kwargs):
            return None

    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = _Expect()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


class _Answer:
    def __init__(self, body: Dict[str, Any]):
        self.status = 200
        self._body = body

    def json(self):
        return self._body


class _Api:
    def __init__(self, body: Dict[str, Any]):
        self.body = body
        self.paths: List[str] = []

    def fetch(self, path, method, headers, data):
        self.paths.append(path)
        return _Answer(self.body)


def _run(execute_module, tmp_path, steps, step_id: str, body: Dict[str, Any]):
    from docs_runner.model import Step
    from docs_runner.stacks import Running

    step = Step.model_validate(_step(steps, step_id))
    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    runner.tokens[(running.api_url, "admin")] = "a-token-for-the-test"
    runner.values = {"revenue-experiment-id": "e4", "doubled-experiment-id": "e5"}
    api = _Api(body)
    observed, _ = runner._api_step(step, running, api, "experiment-results", 1)
    return observed, api.paths


def _variant(name, users, buyers, numbers=None, control=False) -> Dict[str, Any]:
    entry = {
        "variant_name": name,
        "is_control": control,
        "sample_size": users,
        "conversions": buyers,
    }
    entry.update(numbers or {})
    return entry


def _results_body(name: str, case: Dict[str, int], numbers: Dict[str, float]):
    """A /results answer with *numbers* in the revenue row (``_right``'s keys)."""
    nc, cc, nt, ct = (case[arg] for arg in COUNT_ARGS)
    return {
        "experiment_name": name,
        "metrics": [
            {
                "metric_name": "Order value",
                "metric_type": "revenue",
                "analysis": "mean",
                "is_primary": True,
                "lower_is_better": False,
                "variants": [
                    _variant(
                        "Control", nc, cc, {"mean": numbers["control_mean"]}, True
                    ),
                    _variant(
                        "Treatment",
                        nt,
                        ct,
                        {
                            "mean": numbers["treatment_mean"],
                            "absolute_difference": numbers["difference"],
                            "absolute_difference_ci": [
                                numbers["lower"],
                                numbers["upper"],
                            ],
                            "p_value": numbers["p"],
                            "adjusted_p_value": numbers["p"],
                            "statistical_test_used": "welch_t_test",
                            "is_significant": numbers["p"] < 0.05,
                        },
                    ),
                ],
            },
            {
                "metric_name": "Purchases, lower is better",
                "metric_type": "conversion",
                "analysis": "proportion",
                "is_primary": False,
                "lower_is_better": True,
                "variants": [
                    _variant("Control", nc, cc, control=True),
                    _variant("Treatment", nt, ct),
                ],
            },
        ],
        "summary": {"total_users": nc + nt, "recommendation": "SHIP_VARIANT"},
        "srm": {"warning": False, "p_value": 1.0},
    }


@pytest.mark.parametrize(
    "read_id, case, name",
    [
        ("revenue-results", REVENUE, "Docs journey revenue"),
        ("doubled-results", DOUBLED, "Docs journey revenue doubled"),
    ],
)
def test_each_revenue_step_passes_the_formulas_answer_and_fails_a_defects(
    execute_module, tmp_path, steps, read_id, case, name
):
    right = _right(case)
    observed, paths = _run(
        execute_module, tmp_path, steps, read_id, _results_body(name, case, right)
    )
    assert observed == "status 200"
    assert paths == [
        f"/api/v1/results/{'e4' if read_id == 'revenue-results' else 'e5'}"
    ]
    failed = []
    for defect, numbers in _defects(case).items():
        body = _results_body(name, case, {**right, **numbers})
        body["metrics"][0]["variants"][1]["is_significant"] = True
        with pytest.raises(execute_module._Failed) as raised:
            _run(execute_module, tmp_path, steps, read_id, body)
        assert " gives " in raised.value.observed, defect
        failed.append(defect)
    assert len(failed) == len(_defects(case))
    # The buyers counted from the values (a buyer whose total is 0 or less
    # left out), or a variant's users missing: the counts fail on their own.
    for variant, field, wrong in (
        (0, "conversions", 64 - 11 - 11),
        (1, "conversions", 112 - 19 - 19),
        (0, "sample_size", 64),
    ):
        body = copy.deepcopy(_results_body(name, case, right))
        body["metrics"][0]["variants"][variant][field] = wrong
        with pytest.raises(execute_module._Failed):
            _run(execute_module, tmp_path, steps, read_id, body)


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b["metrics"][1].update(lower_is_better=False),
        lambda b: b["metrics"][1]["variants"][1].update(conversions=111),
        lambda b: b["metrics"][1]["variants"][0].update(sample_size=255),
        lambda b: b["metrics"].pop(1),
        lambda b: b["summary"].update(recommendation="KEEP_CONTROL"),
        lambda b: b["summary"].update(recommendation="CONTINUE_TESTING"),
        lambda b: b["metrics"][1].update(analysis="mean"),
    ],
    ids=[
        "direction-dropped",
        "a-buyer-missing",
        "a-user-missing",
        "the-metric-not-listed",
        "a-verdict-keeps-control",
        "a-verdict-waits",
        "analysed-as-a-mean",
    ],
)
def test_the_guardrail_listing_fails_a_defects_answer(
    execute_module, tmp_path, steps, change
):
    right = _results_body("Docs journey revenue", REVENUE, _right(REVENUE))
    observed, _ = _run(execute_module, tmp_path, steps, "guardrail-listing", right)
    assert observed == "status 200"
    wrong = copy.deepcopy(right)
    change(wrong)
    with pytest.raises(execute_module._Failed):
        _run(execute_module, tmp_path, steps, "guardrail-listing", wrong)


def _plan_body(required: int, baseline: float = 0.25) -> Dict[str, Any]:
    return {
        "metric_name": "Order value",
        "metric_type": "revenue",
        "analysed_as": "conversion",
        "baseline_rate": baseline,
        "baseline_source": "observed",
        "mde": DEFAULT_MDE,
        "mde_source": "default",
        "power_target": DEFAULT_POWER,
        "current_sample_size_per_variant": 256,
        "is_adequate": False,
        "required_sample_size_per_variant": required,
    }


def test_the_planned_sample_step_fails_a_defects_answer(
    execute_module, tmp_path, steps
):
    observed, paths = _run(
        execute_module, tmp_path, steps, "revenue-sample-size", _plan_body(19147)
    )
    assert observed == "status 200"
    assert paths == ["/api/v1/results/e4/sample-size"]
    wrong_plans = {
        "one more user": 19148,
        "one fewer user": 19146,
        "rounded down": int(oracles.sample_size_proportions(0.25, 0.05, 0.05, 0.8)) - 1,
        "planned at the treatment's share": oracles.sample_size_proportions(
            112 / 256, 0.05, 0.05, 0.8
        ),
        "planned at 90% power": oracles.sample_size_proportions(0.25, 0.05, 0.05, 0.9),
        "planned one-sided": oracles.sample_size_proportions(0.25, 0.05, 0.1, 0.8),
    }
    for defect, required in wrong_plans.items():
        assert required != 19147, defect
        with pytest.raises(execute_module._Failed):
            _run(
                execute_module,
                tmp_path,
                steps,
                "revenue-sample-size",
                _plan_body(int(required)),
            )
    # A plan from the mean value per user is no share of users at all.
    with pytest.raises(execute_module._Failed):
        _run(
            execute_module,
            tmp_path,
            steps,
            "revenue-sample-size",
            _plan_body(19147, baseline=7.1875),
        )

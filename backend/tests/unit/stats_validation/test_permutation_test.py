"""The permutation test of Welch's t behind a mean metric's p-value (#2041).

``backend/app/services/permutation_test.py`` carries the procedure.  These
tests hold its rules exactly, each against arithmetic of its own:

* the number of permutations ``B`` follows the integer rule, including at
  ``alpha = 1 - level`` where a float formula gives a different ``B``;
* a zero standard error gives an infinite or zero statistic;
* the tie rule and the equal-tailed p-value, against an exact replay of the
  documented draws with every statistic computed in rational arithmetic;
* the statistic is Welch's t, not a raw difference of means, against an
  exact enumeration of every assignment of a small sample;
* the seed: a comparison without a UUID has no p-value, and the key is
  pinned;
* determinism: two processes with different hash seeds, input reordered
  with explicit zeros, every value moved one ulp, two days, the global numpy
  seed, and a user whose total is 0 in one summation order and a rounding
  residue in another all give the same p-value.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import List, Sequence, Tuple

import numpy as np
import pytest
from freezegun import freeze_time

from backend.app.core.stats_engine import derive_test_seed
from backend.app.services.mean_arms import prepared_for_tests
from backend.app.services.permutation_test import (
    ComparisonIdentityError,
    comparison_seed,
    listed_values,
    permutation_t_p_value,
    permutations_for,
    welch_t_from_sums,
)
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    PermutationPValueError,
    mean_metric_result,
    per_user_mean_metric_result,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]

METRIC_ID = "5e3c1f00-2041-4000-8000-0000000000a1"
CONTROL_ID = "5e3c1f00-2041-4000-8000-0000000000c0"
TREATMENT_ID = "5e3c1f00-2041-4000-8000-0000000000e1"
SECOND_ID = "5e3c1f00-2041-4000-8000-0000000000e2"


def _metric(metric_id=METRIC_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=metric_id, name="revenue", metric_type="revenue", is_primary=True
    )


def _result(
    control: Sequence[float],
    n_control: int,
    treatment: Sequence[float],
    n_treatment: int,
    *,
    metric_id=METRIC_ID,
    control_id=CONTROL_ID,
    treatment_id=TREATMENT_ID,
) -> dict:
    """``per_user_mean_metric_result`` on one control and one treatment."""
    return per_user_mean_metric_result(
        prepared_for_tests(
            [
                (
                    BinomialVariant(control_id, "Control", True),
                    n_control,
                    len(control),
                    list(control),
                ),
                (
                    BinomialVariant(treatment_id, "Treatment", False),
                    n_treatment,
                    len(treatment),
                    list(treatment),
                ),
            ]
        ),
        0.05,
        "benjamini_hochberg",
        metric=_metric(metric_id),
    )


def _p_value(
    control: Sequence[float],
    n_control: int,
    treatment: Sequence[float],
    n_treatment: int,
    *,
    treatments: int = 1,
    alpha: float = 0.05,
) -> float:
    """The product's p-value for one comparison, from raw per-user totals:
    listed values, seed and ``B`` as ``per_user_mean_metric_result`` derives
    them for these ids."""
    vc, vt = listed_values([control, treatment])
    permutations = permutations_for(treatments, alpha)
    seed = comparison_seed(
        METRIC_ID,
        CONTROL_ID,
        TREATMENT_ID,
        n_control,
        n_treatment,
        vc.size,
        vt.size,
        permutations,
    )
    return permutation_t_p_value(
        vc, n_control, vt, n_treatment, seed=seed, permutations=permutations
    )


# ---------------------------------------------------------------------------
# B, the number of permutations
# ---------------------------------------------------------------------------

#: (compared treatments, alpha) -> B.
B_RULE = {
    (1, 0.05): 1_999,
    (2, 0.05): 1_999,
    (25, 0.01): 9_999,
    (50, 0.05): 3_999,
    (99, 0.05): 7_999,
    (99, 0.01): 39_999,
    (50, 1 - 0.9): 1_999,
    (100, 1 - 0.8): 1_999,
    (2, 0.01): 1_999,
    (6, 0.01): 3_999,
}


def test_the_number_of_permutations_follows_the_integer_rule():
    """``B + 1 = max(2000, 2000 * ceil(4k / (2000 * alpha)))``, in integer
    arithmetic on ``round(alpha * 10**9)``.  The smallest adjusted p-value,
    ``k * 2 / (B + 1)``, is then at most alpha / 2.

    At ``alpha = 1 - 0.9`` with 50 treatments, and ``1 - 0.8`` with 100, the
    exact answer is 1,999; the same formula on the float alpha gives 3,999,
    so these two rows fail a float implementation."""
    measured = {key: permutations_for(*key) for key in B_RULE}
    assert measured == B_RULE
    for (k, alpha), b in B_RULE.items():
        assert k * 2 / (b + 1) <= alpha / 2 + 1e-12, (k, alpha, b)
    for k, alpha in ((50, 1 - 0.9), (100, 1 - 0.8)):
        float_b = max(2000, 2000 * math.ceil(4 * k / (2000 * alpha))) - 1
        assert float_b == 3_999, (k, alpha, float_b)


@pytest.mark.parametrize(
    "treatments, alpha", [(0, 0.05), (True, 0.05), (1.0, 0.05), (1, 0.0), (1, 1.0)]
)
def test_the_number_of_permutations_refuses_what_is_not_a_comparison(treatments, alpha):
    with pytest.raises(ValueError):
        permutations_for(treatments, alpha)


# ---------------------------------------------------------------------------
# The statistic and the p-value
# ---------------------------------------------------------------------------


def test_a_zero_standard_error_gives_an_infinite_or_zero_statistic():
    """Two arms that do not vary: ``+inf`` when the treatment's mean is
    higher, ``-inf`` when lower, 0 when they are equal."""
    # Treatment: 4 users at 2.0; control: 5 users at 1.0 (centred sums).
    t = welch_t_from_sums(
        [8.0, 4.0, 5.0], [16.0, 4.0, 6.25], 4, [5.0, 10.0, 6.25], [5.0, 20.0, 7.8125], 5
    )
    assert t[0] == math.inf
    assert t[1] == -math.inf
    assert t[2] == 0.0
    # A non-zero standard error: Welch's t.
    finite = welch_t_from_sums([8.0], [20.0], 4, [5.0], [7.0], 5)[0]
    var_t = (20.0 - 64.0 / 4) / 3
    var_c = (7.0 - 25.0 / 5) / 4
    assert finite == pytest.approx((2.0 - 1.0) / math.sqrt(var_t / 4 + var_c / 5))


def _exact_t(
    small: Sequence[Fraction],
    large: Sequence[Fraction],
    n_small: int,
    n_large: int,
    small_is_treatment: bool,
) -> Tuple[int, Fraction]:
    """Welch's t in exact arithmetic, as (sign, t squared), for an assignment
    with these listed values in each arm and zeros for the rest."""

    def arm(values: Sequence[Fraction], n: int) -> Tuple[Fraction, Fraction]:
        total = sum(values, Fraction(0))
        mean = total / n
        ss = (
            sum(((v - mean) ** 2 for v in values), Fraction(0))
            + (n - len(values)) * mean * mean
        )
        return mean, ss / (n - 1)

    mean_s, var_s = arm(small, n_small)
    mean_l, var_l = arm(large, n_large)
    if small_is_treatment:
        d, se2 = mean_s - mean_l, var_s / n_small + var_l / n_large
    else:
        d, se2 = mean_l - mean_s, var_l / n_large + var_s / n_small
    sign = (d > 0) - (d < 0)
    if se2 == 0:
        return sign, Fraction(-1)  # infinite (or 0 when sign is 0)
    return sign, d * d / se2


def _signed_key(t: Tuple[int, Fraction]) -> Tuple[int, Fraction]:
    """An exact t as a key that orders like t itself."""
    sign, square = t
    if square < 0:  # zero standard error: +inf, -inf or 0
        return (2 * sign, Fraction(0))
    return (sign, sign * square)


def _absolute_key(t: Tuple[int, Fraction]) -> Tuple[int, Fraction]:
    """An exact t as a key that orders like ``|t|``."""
    sign, square = t
    if square < 0:
        return (1 if sign else 0, Fraction(0))
    return (0, square)


def _fractions(values: Sequence[float]) -> List[Fraction]:
    return [Fraction(v) for v in values]


def _exact_replay(
    control: List[float],
    n_control: int,
    treatment: List[float],
    n_treatment: int,
    seed: int,
    permutations: int,
) -> Tuple[float, float]:
    """The equal-tailed p-value, and the two-sided ``|T|`` one, from the
    documented draws replayed here, with each permutation's t computed
    exactly from the listed values it puts in each arm."""
    pooled = sorted(control + treatment)
    small_is_treatment = n_treatment <= n_control
    if small_is_treatment:
        n_small, n_large = n_treatment, n_control
        observed_small, observed_large = treatment, control
    else:
        n_small, n_large = n_control, n_treatment
        observed_small, observed_large = control, treatment
    observed = _exact_t(
        _fractions(observed_small),
        _fractions(observed_large),
        n_small,
        n_large,
        small_is_treatment,
    )
    rng = np.random.default_rng(seed)
    m = rng.hypergeometric(n_small, n_large, len(pooled), size=permutations)
    at_least = at_most = beyond = 0
    for b in range(permutations):
        drawn = rng.choice(len(pooled), size=int(m[b]), replace=False, shuffle=False)
        chosen = set(drawn.tolist())
        small = [pooled[i] for i in sorted(chosen)]
        large = [pooled[i] for i in range(len(pooled)) if i not in chosen]
        t = _exact_t(
            _fractions(small), _fractions(large), n_small, n_large, small_is_treatment
        )
        at_least += _signed_key(t) >= _signed_key(observed)
        at_most += _signed_key(t) <= _signed_key(observed)
        beyond += _absolute_key(t) >= _absolute_key(observed)
    equal_tailed = min(1.0, 2.0 * min(1 + at_least, 1 + at_most) / (permutations + 1))
    two_sided = min(1.0, (1 + beyond) / (permutations + 1))
    return equal_tailed, two_sided


@pytest.mark.parametrize(
    "control, n_control, treatment, n_treatment",
    [
        ([0.4, 1.3], 25, [0.1, 0.2, 0.3], 15),
        ([0.83], 15, [0.83, 0.17, 0.41, 0.08, 0.86], 11),
    ],
)
def test_ties_and_the_equal_tailed_rule_hold_in_exact_arithmetic(
    control, n_control, treatment, n_treatment
):
    """Small samples whose observed assignment recurs: a few listed values
    in the smaller arm (in the second, one of them equal to the control's
    only value).  A permutation that puts the same values in the treatment
    has exactly the observed t, but sums them in another order, so in floats
    it can land a bit either side; it must count as a tie, in both tails.

    The product's p-value equals the one replayed here from the documented
    draws with every t computed in rational arithmetic, where a recurrence is
    an exact tie.  The samples are asymmetric: twice the smaller tail differs
    from the two-sided ``|T|`` p-value, which the product must not give."""
    vc, vt = listed_values([control, treatment])
    permutations = permutations_for(1, 0.05)
    seed = comparison_seed(
        METRIC_ID,
        CONTROL_ID,
        TREATMENT_ID,
        n_control,
        n_treatment,
        vc.size,
        vt.size,
        permutations,
    )
    measured = permutation_t_p_value(
        vc, n_control, vt, n_treatment, seed=seed, permutations=permutations
    )
    equal_tailed, two_sided = _exact_replay(
        control, n_control, treatment, n_treatment, seed, permutations
    )
    assert equal_tailed != two_sided, (equal_tailed, two_sided)
    assert measured == equal_tailed, (
        f"p = {measured}; the exact replay gives {equal_tailed} (|T|: {two_sided})"
    )


@pytest.mark.parametrize(
    "control, treatment",
    [
        ([], [100.0]),
        ([100.0], []),
        ([100.0], [100.0]),
        ([2e-13, -3e-13, 1e-13], [4e-13, -1e-13]),
    ],
    ids=[
        "one buyer in the treatment",
        "one buyer in the control",
        "one buyer in each arm, the same value",
        "residues only, no listed value",
    ],
)
def test_the_p_value_is_capped_at_1(control, treatment):
    """``min(1, ...)``: when both tails hold more than half of the
    permutations, the p-value is exactly 1, never more.  One buyer at
    100/100 puts the observed t at the edge of almost every permutation, and
    with no listed value at all (totals that are rounding residues) every
    permutation ties the observed t, where twice the smaller tail is 2.

    Checked through the procedure and through the product, whose schema
    refuses a p-value above 1: without the cap an early experiment with one
    buyer would answer an error."""
    assert _p_value(control, 100, treatment, 100) == 1.0
    product = _result(control, 100, treatment, 100)
    assert product["variants"][1]["statistical_test_used"] == "permutation_t_test"
    assert product["variants"][1]["p_value"] == 1.0


def _enumerated_p(
    control: Sequence[float], treatment: Sequence[float], statistic
) -> float:
    """The exact equal-tailed permutation p-value over every assignment."""
    pooled = list(control) + list(treatment)
    n, n_t = len(pooled), len(treatment)
    observed = statistic(list(treatment), list(control))
    at_least = at_most = total = 0
    for chosen in itertools.combinations(range(n), n_t):
        picked = set(chosen)
        t = statistic(
            [pooled[i] for i in chosen],
            [pooled[i] for i in range(n) if i not in picked],
        )
        tie = abs(t - observed) <= 1e-9 * max(1.0, abs(observed))
        at_least += t > observed or tie
        at_most += t < observed or tie
        total += 1
    return min(1.0, 2.0 * min(at_least, at_most) / total)


def _welch(treatment: List[float], control: List[float]) -> float:
    def mean_var(values):
        mean = math.fsum(values) / len(values)
        return mean, math.fsum((v - mean) ** 2 for v in values) / (len(values) - 1)

    mt, vt = mean_var(treatment)
    mc, vc = mean_var(control)
    return (mt - mc) / math.sqrt(vt / len(treatment) + vc / len(control))


def _raw_difference(treatment: List[float], control: List[float]) -> float:
    return math.fsum(treatment) / len(treatment) - math.fsum(control) / len(control)


def test_the_statistic_is_welchs_t_not_a_raw_difference():
    """Three treatment users with a wide spread against twelve control users
    with a narrow one.  Over all 455 assignments, the exact permutation
    p-value of Welch's t is 0.536 and that of the raw difference of means
    0.193.  The product's p-value (1,999 permutations) is the studentized
    one: a raw difference would land near 0.19."""
    control = [10.4, 9.3, 7.6, 10.8, 10.2, 9.5, 12.1, 10.1, 9.2, 10.1, 9.0, 9.2]
    treatment = [14.6, 3.3, 4.6]
    studentized = _enumerated_p(control, treatment, _welch)
    raw = _enumerated_p(control, treatment, _raw_difference)
    assert abs(studentized - raw) >= 0.2, (studentized, raw)
    measured = _p_value(control, len(control), treatment, len(treatment))
    assert abs(measured - studentized) < abs(measured - raw), (
        f"p = {measured}; the exact studentized p-value is {studentized:.4f} "
        f"and the raw difference's {raw:.4f}"
    )


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def test_the_seed_key_is_pinned():
    """Changing the key's construction changes every mean metric's p-value,
    so it bumps the engine version: these values are pinned."""
    assert derive_test_seed("perm-t|a|b") == derive_test_seed("perm-t|a|b")
    assert derive_test_seed("perm-t|a|b") != derive_test_seed("perm-t|a|c")
    assert derive_test_seed("perm-t|a|b") == 4_096_842_751_812_167_728
    seed = comparison_seed(
        METRIC_ID, CONTROL_ID, TREATMENT_ID, 600, 5_400, 30, 270, 1_999
    )
    assert seed == derive_test_seed(
        f"perm-t|{METRIC_ID}|{CONTROL_ID}|{TREATMENT_ID}|600|5400|30|270|1999"
    )
    assert 0 <= seed < 2**63


def test_the_ids_in_the_seed_are_canonical():
    """Uppercase, braces and the ``urn:uuid:`` form name the same UUID, so
    they give the same seed; so does a ``uuid.UUID``."""
    import uuid

    canonical = comparison_seed(
        METRIC_ID, CONTROL_ID, TREATMENT_ID, 10, 10, 2, 3, 1_999
    )
    for metric_id in (
        METRIC_ID.upper(),
        "{" + METRIC_ID + "}",
        "urn:uuid:" + METRIC_ID,
        uuid.UUID(METRIC_ID),
    ):
        assert (
            comparison_seed(metric_id, CONTROL_ID, TREATMENT_ID, 10, 10, 2, 3, 1_999)
            == canonical
        )


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("bad", [None, "", "control", 7])
def test_a_comparison_without_a_uuid_has_no_p_value(position, bad):
    """The ids are part of the seed; there is no fallback."""
    ids = [METRIC_ID, CONTROL_ID, TREATMENT_ID]
    ids[position] = bad
    with pytest.raises(ComparisonIdentityError):
        comparison_seed(*ids, 10, 10, 2, 3, 1_999)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

#: A lopsided, zero-inflated comparison whose p-value is in the middle of the
#: range, so a different seed or a different draw would very likely move it.
DET_CONTROL = [12.5, 3.25, 40.0, 7.75, 19.0, 0.5, 66.0, 2.0]
DET_N_CONTROL = 60
DET_TREATMENT = [
    5.0,
    31.0,
    8.5,
    1.25,
    14.0,
    22.75,
    3.5,
    90.0,
    6.0,
    11.0,
    2.5,
    48.0,
    9.75,
    0.75,
    27.0,
    4.25,
    17.5,
    35.0,
]
DET_N_TREATMENT = 200


def _det_p() -> float:
    return _p_value(DET_CONTROL, DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT)


def test_the_determinism_fixture_is_not_at_the_floor_or_the_ceiling():
    p = _det_p()
    assert 0.1 < p < 0.9, p


_SUBPROCESS = """
import json, sys
sys.path.insert(0, {root!r})
from backend.tests.unit.stats_validation import test_permutation_test as t
print(json.dumps(
    {{"p": t._det_p(), "result": t._det_json(), "hash": hash("controlvar")}},
    sort_keys=True,
))
"""


def _det_json() -> str:
    """``per_user_mean_metric_result`` on the fixture, as canonical JSON."""
    result = _result(DET_CONTROL, DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT)
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def test_two_processes_with_different_hash_seeds_give_the_same_p_value():
    """``PYTHONHASHSEED`` 1 and 2 (``-I`` would ignore it, so ``-s -P``): the
    p-value and the whole ``per_user_mean_metric_result``, as canonical
    JSON, are identical, and the treatment's p-value is the permutation
    test's."""
    outputs = []
    for hash_seed in ("1", "2"):
        result = subprocess.run(
            [sys.executable, "-s", "-P", "-c", _SUBPROCESS.format(root=str(REPO_ROOT))],
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert outputs[0]["hash"] != outputs[1]["hash"], "the hash seeds did not differ"
    assert outputs[0]["p"] == outputs[1]["p"] == _det_p(), (
        f"p-values differ between processes: {outputs[0]['p']} and {outputs[1]['p']}"
    )
    assert outputs[0]["result"] == outputs[1]["result"] == _det_json()
    treatment = json.loads(_det_json())["variants"][1]
    assert treatment["statistical_test_used"] == "permutation_t_test"
    assert treatment["p_value"] == _det_p()


def test_reordered_input_with_explicit_zeros_gives_the_same_p_value():
    """SQL's ``GROUP BY`` returns users in no fixed order, and a user can be
    listed with a total of exactly 0."""
    reordered = _p_value(
        list(reversed(DET_CONTROL)) + [0.0, 0.0],
        DET_N_CONTROL,
        DET_TREATMENT[::2] + [0.0] + DET_TREATMENT[1::2],
        DET_N_TREATMENT,
    )
    assert reordered == _det_p()


def test_moving_every_value_one_ulp_gives_the_same_p_value():
    """A ``SUM`` under another query plan can move a total's last bit; the
    values are not in the seed."""
    up = _p_value(
        [float(np.nextafter(v, math.inf)) for v in DET_CONTROL],
        DET_N_CONTROL,
        [float(np.nextafter(v, math.inf)) for v in DET_TREATMENT],
        DET_N_TREATMENT,
    )
    down = _p_value(
        [float(np.nextafter(v, -math.inf)) for v in DET_CONTROL],
        DET_N_CONTROL,
        [float(np.nextafter(v, -math.inf)) for v in DET_TREATMENT],
        DET_N_TREATMENT,
    )
    assert up == down == _det_p()


def test_the_same_values_on_two_days_give_the_same_p_value():
    """The day is not in the seed."""
    with freeze_time("2026-11-20 23:59:59"):
        before = _det_p()
    with freeze_time("2026-11-21 00:00:01"):
        after = _det_p()
    assert before == after


def test_the_global_numpy_seed_does_not_change_the_p_value():
    state = np.random.get_state()
    try:
        np.random.seed(1)
        first = _det_p()
        np.random.seed(2)
        second = _det_p()
    finally:
        np.random.set_state(state)
    assert first == second == _det_p()


def test_a_net_zero_user_counts_as_zero_in_every_summation_order():
    """A user with orders of 10.10 and 5.20 and a refund of 15.30 totals 0.0
    summed in that order and -1.78e-15 summed as 5.20, -15.30, 10.10.  A total
    within 1e-9 * max(1, largest |total|) of 0 counts as 0, so both give the
    same listed values, seed and p-value."""
    in_order = 0.0
    for value in (10.10, 5.20, -15.30):
        in_order += value
    other_order = 0.0
    for value in (5.20, -15.30, 10.10):
        other_order += value
    assert in_order == 0.0
    assert other_order == pytest.approx(-1.7763568394002505e-15, abs=0)
    first = _p_value(
        DET_CONTROL + [in_order], DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT
    )
    second = _p_value(
        DET_CONTROL + [other_order], DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT
    )
    assert first == second == _det_p()
    in_product = _result(
        DET_CONTROL + [in_order], DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT
    )
    other_in_product = _result(
        DET_CONTROL + [other_order], DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT
    )
    assert in_product["variants"][1]["p_value"] == _det_p()
    assert other_in_product["variants"][1]["p_value"] == _det_p()


def test_the_zero_tolerance_scales_with_the_largest_total():
    """``1e-9 * max(1, M)``: below 1, the tolerance is 1e-9."""
    small = listed_values([[1e-9, 2e-9, -1e-10], [0.5]])
    assert small[0].tolist() == [2e-9]
    large = listed_values([[1e-3, 3e-3], [2e6]])
    assert large[0].tolist() == [3e-3]
    assert large[1].tolist() == [2e6]


def test_a_total_of_1e9_makes_totals_of_1_or_less_count_as_0_in_the_test_only():
    """One total of 1e9: totals at or below 1e-9 * 1e9 = 1 count as 0 in the
    test, so 0.5 and 1.0 are not listed and 1.5 is.  The p-value is the one
    of the same data with 0.5 and 1.0 set to exactly 0 (the same listed
    counts, so the same seed); the reported means use every total."""
    control = [1e9, 0.5, 2.0, 7.25, 1.0, 3.5]
    treatment = [1.5, 0.5, 4.0, 1.0, 12.0, 6.0, 2.5]

    def zeroed(values):
        return [0.0 if v in (0.5, 1.0) else v for v in values]

    vc, vt = listed_values([control, treatment])
    for listed in (vc, vt):
        assert 0.5 not in listed.tolist() and 1.0 not in listed.tolist(), listed
    assert 1.5 in vt.tolist(), vt
    assert (vc.size, vt.size) == (4, 5)
    assert _p_value(control, 40, treatment, 300) == _p_value(
        zeroed(control), 40, zeroed(treatment), 300
    )
    as_given = _result(control, 40, treatment, 300)
    with_zeros = _result(zeroed(control), 40, zeroed(treatment), 300)
    assert as_given["variants"][1]["p_value"] == with_zeros["variants"][1]["p_value"]
    assert as_given["variants"][1]["mean"] == math.fsum(treatment) / 300
    assert as_given["variants"][1]["mean"] != with_zeros["variants"][1]["mean"]


# ---------------------------------------------------------------------------
# Fail closed: no fallback to Welch
# ---------------------------------------------------------------------------


def _two_treatment_sums():
    control = BinomialVariant(CONTROL_ID, "Control", True)
    first = BinomialVariant(TREATMENT_ID, "T1", False)
    second = BinomialVariant(SECOND_ID, "T2", False)
    return [
        (control, 100, -5.0, 900.0),
        (first, 100, 3.0, 950.0),
        (second, 100, 2.0, 870.0),
    ]


def test_a_missing_permutation_p_value_raises():
    with pytest.raises(PermutationPValueError, match=SECOND_ID):
        mean_metric_result(
            0.0,
            _two_treatment_sums(),
            0.05,
            "benjamini_hochberg",
            metric=_metric(),
            p_values={TREATMENT_ID: 0.2},
        )


def test_a_permutation_p_value_for_a_treatment_that_is_not_compared_raises():
    control = BinomialVariant(CONTROL_ID, "Control", True)
    flat = BinomialVariant(TREATMENT_ID, "T1", False)
    with pytest.raises(PermutationPValueError, match=TREATMENT_ID):
        mean_metric_result(
            1.0,
            [(control, 10, 0.0, 0.0), (flat, 10, 0.0, 0.0)],
            0.05,
            "none",
            metric=_metric(),
            p_values={TREATMENT_ID: 0.5},
        )


def test_given_permutation_p_values_are_used_and_named():
    result = mean_metric_result(
        0.0,
        _two_treatment_sums(),
        0.05,
        "bonferroni",
        metric=_metric(),
        p_values={TREATMENT_ID: 0.02, SECOND_ID: 0.5},
    )
    treatments = result["variants"][1:]
    assert [v["p_value"] for v in treatments] == [0.02, 0.5]
    assert [v["adjusted_p_value"] for v in treatments] == [0.04, 1.0]
    assert [v["statistical_test_used"] for v in treatments] == [
        "permutation_t_test"
    ] * 2


@pytest.mark.parametrize(
    "ids",
    [
        {"metric_id": None},
        {"metric_id": "revenue"},
        {"control_id": "control"},
        {"treatment_id": None},
    ],
)
def test_a_mean_metric_without_uuids_has_no_p_value(ids):
    """The metric's and the variants' ids are in the seed; the comparison
    raises rather than fall back to Welch's p-value."""
    with pytest.raises(ComparisonIdentityError):
        _result(DET_CONTROL, DET_N_CONTROL, DET_TREATMENT, DET_N_TREATMENT, **ids)


# ---------------------------------------------------------------------------
# What does not change
# ---------------------------------------------------------------------------

#: The fields whose values the permutation test does not change: every
#: estimate and interval stays Welch's or Fieller's.
UNCHANGED_FIELDS = (
    "variant_id",
    "variant_name",
    "is_control",
    "sample_size",
    "conversions",
    "mean",
    "std_dev",
    "confidence_interval",
    "effect_size",
    "effect_size_label",
    "power",
    "relative_improvement_pct",
    "absolute_difference",
    "absolute_difference_ci",
    "relative_improvement_pct_ci",
    "incremental_impact",
    "incremental_impact_ci",
)


def _unchanged_datasets():
    rng = np.random.default_rng(2_041_022_000)
    buyers_c = rng.lognormal(3.0, 1.0, size=30)
    buyers_t = rng.lognormal(3.1, 1.0, size=36)
    return {
        "balanced revenue": (buyers_c.tolist(), 400, buyers_t.tolist(), 400),
        "normal": (
            rng.normal(50.0, 10.0, size=60).tolist(),
            60,
            rng.normal(52.0, 10.0, size=540).tolist(),
            540,
        ),
    }


@pytest.mark.parametrize("name", ["balanced revenue", "normal"])
def test_every_estimate_and_interval_is_what_welch_gave(name, monkeypatch):
    """A balanced revenue experiment and a metric that is not skewed: every
    field but the p-values, significance, the test's name and the winner is
    exactly what the engine gave before #2041, when the p-value was Welch's
    (``mean_metric_result`` without permutation p-values, which its own
    fingerprint holds to 1.12.0's output byte for byte).  A conversion
    metric's whole result is unchanged too: the binomial pins under 1.13.0
    are 1.12.0's."""
    control, n_control, treatment, n_treatment = _unchanged_datasets()[name]
    product = _result(control, n_control, treatment, n_treatment)
    monkeypatch.setattr(
        "backend.app.services.sufficient_stats_analysis._permutation_p_values",
        lambda *args, **kwargs: None,
    )
    welch = _result(control, n_control, treatment, n_treatment)
    assert product["variants"][1]["statistical_test_used"] == "permutation_t_test"
    assert welch["variants"][1]["statistical_test_used"] == "welch_t_test"
    for ours, theirs in zip(product["variants"], welch["variants"]):
        for field in UNCHANGED_FIELDS:
            assert ours[field] == theirs[field], (name, ours["variant_name"], field)
    for key in ("metric_id", "metric_name", "metric_type", "analysis"):
        assert product[key] == welch[key]

"""
The sufficient-statistics estimators' output, pinned per ``ENGINE_VERSION``
and per function.

``backend/app/services/sufficient_stats_analysis.py`` computes metric results
from per-variant counts or sums, for ``/results`` and for callers with no
Controlvar database rows (warehouse analysis, #312).  This test runs fixed
counts through each estimator, hashes the outputs and compares the hash with
the one pinned for the current ``ENGINE_VERSION`` **and that function**.

It is separate from ``ENGINE_FINGERPRINTS`` in ``test_engine_fingerprint.py``,
which is not edited for these functions.  The rule: an engine-changing PR
bumps ``ENGINE_VERSION`` to the next minor and appends it to
``RELEASED_ENGINE_VERSIONS`` ("frozen on main") in the same change.  A frozen
version's entries are never edited, and no key is ever added under one, so a
new estimator is pinned under a new version, never under a frozen one.  A PR
is engine-changing when, for the same database rows and request, it adds,
removes or changes any number, decision (winner, recommendation, Bayesian
decision, ``can_stop``) or allocation weight that a path stamping
``engine_version`` returns, caches or persists: results and its snapshots,
Bayesian, CUPED, sequential, and the bandit's state, history and status.  A
revert of an engine-changing PR bumps again.
"""

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.services.mean_arms import prepared_for_tests
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
    cuped_metric_result,
    mean_metric_result,
    per_user_mean_metric_result,
)
from backend.tests.unit.services.test_binomial_metric_result_characterisation import (
    _FIXTURES,
    _canonical,
    _experiment,
    results_output,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: sha256 of the canonical outputs below, per engine version, per function.
SUFFICIENT_STATS_FINGERPRINTS: Dict[str, Dict[str, str]] = {
    "1.1.0": {
        "binomial_metric_result": "cd5de7d0dd07d9e53e847cd8bb56a2b7e1c1164b590e2370c50f35aee74e0411",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #454: the proportion interval uses norm.ppf at 1 - alpha instead of a
    # fixed 1.96.  The mean path already used t.ppf at alpha: 1.1.0's hash.
    "1.2.0": {
        "binomial_metric_result": "c65e109491f6e0968e0f5f8e248fcad9f571826960095622cf76e813516eba8a",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #217: CUPED from per-arm sums, new in 1.3.0.  The other two are 1.2.0's.
    "1.3.0": {
        "binomial_metric_result": "c65e109491f6e0968e0f5f8e248fcad9f571826960095622cf76e813516eba8a",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #854: the sequential route only.  These three are 1.3.0's.
    "1.4.0": {
        "binomial_metric_result": "c65e109491f6e0968e0f5f8e248fcad9f571826960095622cf76e813516eba8a",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "ad119bc6bfff1ec62f70b7499e7c7eb29341f3a12df69587935128964e03125c",
    },
    # #1377: the winner reads lower_is_better, and the binomial and mean
    # results carry it (False for these fixtures).  Without that field they
    # are 1.4.0's (test_lower_is_better_fingerprint.py); CUPED is 1.4.0's.
    "1.5.0": {
        "binomial_metric_result": "77ad64a045ff7cac8ca009488eac45d1db6e0216f8bb5f13b86315519187a9c1",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # #1385: a lower-is-better bandit's weights, regret and history
    # alpha/beta.  These estimators are unchanged: 1.5.0's.
    "1.6.0": {
        "binomial_metric_result": "77ad64a045ff7cac8ca009488eac45d1db6e0216f8bb5f13b86315519187a9c1",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # #439: count, duration and custom are routed to the mean in /results.
    # These estimators are unchanged: 1.6.0's.
    "1.7.0": {
        "binomial_metric_result": "77ad64a045ff7cac8ca009488eac45d1db6e0216f8bb5f13b86315519187a9c1",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # #1496: each binomial treatment carries its difference from the control,
    # the intervals and the impact; without them it is 1.7.0's
    # (test_difference_intervals.py).  CUPED and the mean estimator (warehouse
    # mean runs) are unchanged: 1.7.0's.
    "1.8.0": {
        "binomial_metric_result": "e0e24aebc40aab2a12c60e9537057ec53f1aa554192f0ab189c7c547e485e7ad",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # 1.9.0 (#1776) changes the segment breakdown only: 1.8.0's.
    "1.9.0": {
        "binomial_metric_result": "e0e24aebc40aab2a12c60e9537057ec53f1aa554192f0ab189c7c547e485e7ad",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # 1.10.0 (#448) adds the balanced-groups estimate only: 1.9.0's.
    "1.10.0": {
        "binomial_metric_result": "e0e24aebc40aab2a12c60e9537057ec53f1aa554192f0ab189c7c547e485e7ad",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # 1.11.0: a significant treatment against a control with no conversions
    # is the winner.  The binomial "edges" set has one (t2, 12 of 400 against
    # 0 of 500), so the binomial hash moves; the other two are 1.10.0's.
    "1.11.0": {
        "binomial_metric_result": "b634c5af2edc11d68b03498763799e730ec25c850a049e9678b2ce62fda87b8f",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # 1.12.0 (#1969) changes the segment breakdown's heterogeneity flag only:
    # 1.11.0's.
    "1.12.0": {
        "binomial_metric_result": "b634c5af2edc11d68b03498763799e730ec25c850a049e9678b2ce62fda87b8f",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
    },
    # 1.13.0 (#2041): a mean metric's p-value in /results comes from a
    # permutation test of Welch's t; per_user_mean_metric_result is pinned
    # here for the first time (its cases: PER_USER_MEAN_CASE_DIGESTS).  The
    # other three are 1.12.0's: mean_metric_result on sums alone (warehouse
    # mean runs) keeps Welch's t-test, byte for byte.
    "1.13.0": {
        "binomial_metric_result": "b634c5af2edc11d68b03498763799e730ec25c850a049e9678b2ce62fda87b8f",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
        "per_user_mean_metric_result": "847120695dd4f39121b3d11e40874d2f7374453328e712cd1efdb31d9a942cdb",
    },
    # 1.14.0 (#1934): a metric's conversion window; no fixture here has one,
    # so 1.13.0's entries.
    "1.14.0": {
        "binomial_metric_result": "b634c5af2edc11d68b03498763799e730ec25c850a049e9678b2ce62fda87b8f",
        "cuped_metric_result": "29e33befe849a6cc36fd462b5d4e1f2a361efcff2f695a38c612653c11363f67",
        "mean_metric_result": "dbd894429d89e81cb669f5063a4e275452432696c904a180ca3d6e64ae6cbd41",
        "per_user_mean_metric_result": "847120695dd4f39121b3d11e40874d2f7374453328e712cd1efdb31d9a942cdb",
    },
}

#: The engine versions frozen on main, oldest first.  An engine-changing PR
#: appends its new ``ENGINE_VERSION`` here in the same change; nothing is ever
#: removed or reordered.  A function new in a version is pinned under that new
#: version only, never added under an earlier one (a new key under a frozen
#: version passes every hash, which is how a changed engine could keep a
#: frozen version number).  ``test_engine_versions_frozen.py`` holds the
#: digests that enforce it.
RELEASED_ENGINE_VERSIONS = (
    "1.1.0",
    "1.2.0",
    "1.3.0",
    "1.4.0",
    "1.5.0",
    "1.6.0",
    "1.7.0",
    "1.8.0",
    "1.9.0",
    "1.10.0",
    "1.11.0",
    "1.12.0",
    "1.13.0",
    "1.14.0",
)

_CONTROL = "ffffffff-0000-4000-8000-000000000002"
_TREATMENT_1 = "00000000-0000-4000-8000-000000000001"
_TREATMENT_2 = "77777777-0000-4000-8000-000000000003"

_METRIC = SimpleNamespace(
    id="33333333-0000-4000-8000-000000000003",
    name="Converted",
    metric_type="conversion",
    is_primary=True,
)


def _binomial_outputs() -> Dict[str, Any]:
    """Fixed counts through ``binomial_metric_result``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    t2 = BinomialVariant(_TREATMENT_2, "t2", False)
    count_sets = {
        # Treatment first: the control is found by flag, not position.
        "three": [(t1, 1000, 150), (control, 1000, 120), (t2, 1000, 131)],
        "unbalanced": [(control, 2000, 40), (t1, 150, 9), (t2, 30000, 690)],
        "edges": [(t1, 0, 0), (control, 500, 0), (t2, 400, 12)],
    }
    outputs: Dict[str, Any] = {}
    for name, counts in count_sets.items():
        for alpha in (0.05, 0.10):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                outputs[f"{name}/{alpha}/{correction}"] = binomial_metric_result(
                    counts, alpha, correction, metric=_METRIC
                )
    return outputs


_MEAN_METRIC = SimpleNamespace(
    id="44444444-0000-4000-8000-000000000004",
    name="Revenue per user",
    metric_type="revenue",
    is_primary=False,
)


def _mean_outputs() -> Dict[str, Any]:
    """Fixed centred sums through ``mean_metric_result``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    t2 = BinomialVariant(_TREATMENT_2, "t2", False)
    sum_sets = {
        # (k, [(variant, n, sum_d, sum_d2), ...]); treatment first again.
        "three": (
            42.5,
            [
                (t1, 1000, 180.0, 16300.0),
                (control, 1000, -150.0, 15800.0),
                (t2, 1000, -30.0, 16100.0),
            ],
        ),
        "unbalanced": (
            1e6,
            [
                (control, 2000, -12.0, 8000.5),
                (t1, 150, 30.0, 700.0),
                (t2, 30000, -18.0, 121000.0),
            ],
        ),
        "no_variation": (
            3.0,
            [(control, 10, 0.0, 0.0), (t1, 12, 12.0, 12.0), (t2, 5, 5.0, 9.0)],
        ),
    }
    outputs: Dict[str, Any] = {}
    for name, (k, sums) in sum_sets.items():
        for alpha in (0.05, 0.10):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                outputs[f"{name}/{alpha}/{correction}"] = mean_metric_result(
                    k, sums, alpha, correction, metric=_MEAN_METRIC
                )
    return outputs


def _cuped_outputs() -> Dict[str, Any]:
    """Fixed per-arm sums through ``cuped_metric_result``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    t2 = BinomialVariant(_TREATMENT_2, "t2", False)
    sum_sets = {
        # (variant, n, sum_y, sum_x, sum_y2, sum_x2, sum_xy); treatment first.
        "three": [
            (t1, 1000, 150.0, 300.0, 150.0, 300.0, 80.0),
            (control, 1000, 120.0, 310.0, 120.0, 310.0, 70.0),
            (t2, 1000, 131.0, 290.0, 131.0, 290.0, 66.0),
        ],
        "unbalanced": [
            (control, 2000, 40.0, 500.0, 40.0, 500.0, 25.0),
            (t1, 150, 9.0, 30.0, 9.0, 30.0, 5.0),
            (t2, 30000, 690.0, 7000.0, 690.0, 7000.0, 400.0),
        ],
        "edges": [
            (t1, 0, 0.0, 0.0, 0.0, 0.0, 0.0),
            (control, 500, 10.0, 0.0, 10.0, 0.0, 0.0),
            (t2, 400, 12.0, 0.0, 12.0, 0.0, 0.0),
        ],
    }
    outputs: Dict[str, Any] = {}
    for name, sums in sum_sets.items():
        for level in (0.95, 0.90):
            for correction in ("none", "bonferroni", "benjamini_hochberg"):
                for adjust in (True, False):
                    outputs[f"{name}/{level}/{correction}/{adjust}"] = (
                        cuped_metric_result(
                            sums, level, correction, metric=_METRIC, adjust=adjust
                        )
                    )
    return outputs


def _values(count: int, step: int, scale: float, offset: float = 0.5) -> List[float]:
    """``count`` positive per-user totals from a fixed formula, no RNG."""
    return [round(((i * step) % 997) * scale + offset, 2) for i in range(count)]


def _per_user_mean_cases() -> Dict[str, Any]:
    """The inputs of ``per_user_mean_metric_result``'s pinned cases (#2041),
    one per path of the permutation test's random draws: (alpha, arms), each
    arm ``(variant, users, converted, totals)``."""
    control = BinomialVariant(_CONTROL, "control", True)
    t1 = BinomialVariant(_TREATMENT_1, "t1", False)
    many = [
        BinomialVariant(f"5e3c1f00-2041-4000-8000-00000000000{i}", f"t{i}", False)
        for i in range(1, 7)
    ]
    # A user with orders of 10.10 and 5.20 and a refund of 15.30, summed as
    # 5.20, -15.30, 10.10: a residue of -1.78e-15, which counts as 0.
    net_zero = 5.20 + -15.30 + 10.10
    return {
        # (a) K = 7 listed users: the hypergeometric draw's urn method (< 10).
        "a_urn": (
            0.05,
            [
                (control, 50, 3, [12.0, 3.5, 40.25]),
                (t1, 60, 4, [7.0, 19.5, 2.25, 66.0]),
            ],
        ),
        # (b) 10 <= K <= 10,000, a lopsided split.
        "b_hrua": (
            0.05,
            [
                (control, 400, 30, _values(30, 7919, 0.07)),
                (t1, 1_600, 120, _values(120, 104729, 0.07)),
            ],
        ),
        # (c) K = 30,000 at 10/90: about 3,000 values fall in the smaller arm,
        # more than K // 20, so the choice takes its tail-shuffle path.
        "c_tail_shuffle": (
            0.05,
            [
                (control, 6_000, 3_000, _values(3_000, 7919, 0.05)),
                (t1, 54_000, 27_000, _values(27_000, 104729, 0.05)),
            ],
        ),
        # (d) six treatments at alpha 0.01: B + 1 = 4,000.
        "d_six_treatments": (
            0.01,
            [(control, 80, 8, _values(8, 7919, 0.1))]
            + [
                (v, 80 + 10 * i, 6 + i, _values(6 + i, 104729 + i, 0.1))
                for i, v in enumerate(many)
            ],
        ),
        # (e) a net-zero user among the listed totals.
        "e_net_zero": (
            0.05,
            [
                (
                    control,
                    60,
                    9,
                    [12.5, 3.25, 40.0, 7.75, 19.0, 0.5, 66.0, 2.0, net_zero],
                ),
                (t1, 200, 18, _values(18, 7919, 0.05)),
            ],
        ),
    }


def _per_user_mean_outputs() -> Dict[str, Any]:
    """Each case of ``_per_user_mean_cases`` through
    ``per_user_mean_metric_result``."""
    return {
        name: per_user_mean_metric_result(
            prepared_for_tests(arms), alpha, "benjamini_hochberg", metric=_MEAN_METRIC
        )
        for name, (alpha, arms) in _per_user_mean_cases().items()
    }


#: Every function this file pins, by the key it is pinned under.
_ESTIMATORS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "binomial_metric_result": _binomial_outputs,
    "cuped_metric_result": _cuped_outputs,
    "mean_metric_result": _mean_outputs,
    "per_user_mean_metric_result": _per_user_mean_outputs,
}

#: ``per_user_mean_metric_result``'s pinned outputs case by case, per engine
#: version: its entry in ``SUFFICIENT_STATS_FINGERPRINTS`` is the digest of
#: all of them, and a failure names each case that moved.
PER_USER_MEAN_CASE_DIGESTS: Dict[str, Dict[str, str]] = {
    "1.13.0": {
        "a_urn": "26952f980791896923f36740b73d05ed1dd5a9d1396a3915c399345efcbdd0be",
        "b_hrua": "611f1908605e1da8b33da86fe9543e544e9e9b12f639fce491329f0f7f9f91d7",
        "c_tail_shuffle": "85540e2d27dca76fc4d9c963b240b72712b9637609df2f27f51eb073fa000586",
        "d_six_treatments": "4464504e26007e36dd6a227b4f48eadbba49a0fbb43f842d02c3af89933b0814",
        "e_net_zero": "5fb85a2186e3772af263af77ca3cb9e77e435531a45b5a96620352683ef3c3b3",
    },
    # 1.14.0 (#1934): a metric's conversion window; no fixture here has one,
    # so 1.13.0's entries.
    "1.14.0": {
        "a_urn": "26952f980791896923f36740b73d05ed1dd5a9d1396a3915c399345efcbdd0be",
        "b_hrua": "611f1908605e1da8b33da86fe9543e544e9e9b12f639fce491329f0f7f9f91d7",
        "c_tail_shuffle": "85540e2d27dca76fc4d9c963b240b72712b9637609df2f27f51eb073fa000586",
        "d_six_treatments": "4464504e26007e36dd6a227b4f48eadbba49a0fbb43f842d02c3af89933b0814",
        "e_net_zero": "5fb85a2186e3772af263af77ca3cb9e77e435531a45b5a96620352683ef3c3b3",
    },
}


def _digest(value: Any) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint(outputs: Dict[str, Any]) -> str:
    return _digest(outputs)


def _moved_cases(function: str, outputs: Dict[str, Any]) -> str:
    """For ``per_user_mean_metric_result``: each case whose digest differs
    from the pinned one, with both digests."""
    if function != "per_user_mean_metric_result":
        return ""
    pinned = PER_USER_MEAN_CASE_DIGESTS.get(ENGINE_VERSION, {})
    lines = [
        f"  {case}: pinned {pinned.get(case)}, measured {_digest(output)}"
        for case, output in sorted(outputs.items())
        if pinned.get(case) != _digest(output)
    ]
    return " Cases that moved:\n" + "\n".join(lines) if lines else ""


@pytest.mark.parametrize("function", sorted(_ESTIMATORS))
def test_sufficient_stats_fingerprint(function):
    pinned = SUFFICIENT_STATS_FINGERPRINTS.get(ENGINE_VERSION, {})
    outputs = _ESTIMATORS[function]()
    actual = _fingerprint(outputs)
    assert function in pinned, (
        f"{function} has no pinned fingerprint for ENGINE_VERSION "
        f"{ENGINE_VERSION}; add {actual!r} (see this module's docstring)."
        f"{_moved_cases(function, outputs)}"
    )
    assert actual == pinned[function], (
        f"{function}'s output changed under ENGINE_VERSION {ENGINE_VERSION} "
        f"(fingerprint {actual}). See this module's docstring."
        f"{_moved_cases(function, outputs)}"
    )


def test_each_per_user_mean_case_is_pinned():
    """The case digests are the ones the function's fingerprint covers: the
    same cases, and together the same outputs."""
    outputs = _per_user_mean_outputs()
    pinned = PER_USER_MEAN_CASE_DIGESTS[ENGINE_VERSION]
    assert set(pinned) == set(outputs)
    moved = _moved_cases("per_user_mean_metric_result", outputs)
    assert not moved, f"per_user_mean_metric_result under {ENGINE_VERSION}.{moved}"


def test_the_per_user_mean_cases_take_every_path_they_name():
    """(a) fewer than 10 listed users; (b) 10 to 10,000; (c) 30,000 listed
    with more than K // 20 in the smaller arm on average; (d) six compared
    treatments at alpha 0.01, so B + 1 = 4,000; (e) a user whose total is a
    rounding residue of 0."""
    from backend.app.services.permutation_test import permutations_for

    cases = _per_user_mean_cases()
    listed = {
        name: sum(len(arm[3]) for arm in arms[:2]) for name, (_, arms) in cases.items()
    }
    assert listed["a_urn"] < 10
    assert 10 <= listed["b_hrua"] <= 10_000
    control, treatment = cases["c_tail_shuffle"][1]
    k = listed["c_tail_shuffle"]
    assert k == 30_000 and k * control[1] / (control[1] + treatment[1]) > k // 20
    alpha, arms = cases["d_six_treatments"]
    assert len(arms) - 1 == 6 and permutations_for(6, alpha) + 1 == 4_000
    residue = cases["e_net_zero"][1][0][3][-1]
    assert residue != 0.0 and abs(residue) < 1e-14
    outputs = _per_user_mean_outputs()
    for name, output in outputs.items():
        treatments = [v for v in output["variants"] if not v["is_control"]]
        assert all(
            v["statistical_test_used"] == "permutation_t_test" for v in treatments
        ), name


#: The release that introduced ``cuped_metric_result`` (#217, v0.21.0).
CUPED_FIRST_VERSION = "1.3.0"


def _key(version):
    return tuple(int(part) for part in version.split("."))


def test_cuped_metric_result_is_pinned_only_from_the_version_that_added_it():
    """#217 changed CUPED's numbers after 1.2.0 shipped, so it bumped the
    engine to 1.3.0 and pinned ``cuped_metric_result`` there.

    The rule: CUPED is absent from every version released before #217 (1.1.0
    and 1.2.0), and present under 1.3.0 and under the running version.  1.3.0
    shipped in v0.21.0, so it is in ``RELEASED_ENGINE_VERSIONS``; its CUPED
    entry is the one that shipped, not a key added afterwards.
    """
    assert CUPED_FIRST_VERSION in RELEASED_ENGINE_VERSIONS
    for version in RELEASED_ENGINE_VERSIONS:
        pinned = "cuped_metric_result" in SUFFICIENT_STATS_FINGERPRINTS[version]
        if _key(version) < _key(CUPED_FIRST_VERSION):
            assert not pinned, (
                f"cuped_metric_result is pinned under {version}, which shipped "
                f"before #217"
            )
        else:
            assert pinned, f"cuped_metric_result is missing from {version}"
    assert "cuped_metric_result" in SUFFICIENT_STATS_FINGERPRINTS[ENGINE_VERSION]


#: The release that introduced ``per_user_mean_metric_result``'s pin (#2041).
PER_USER_MEAN_FIRST_VERSION = "1.13.0"


def test_per_user_mean_metric_result_is_pinned_only_from_the_version_that_added_it():
    """#2041 changed a mean metric's p-value after 1.12.0 shipped, so it
    bumped the engine to 1.13.0 and pinned ``per_user_mean_metric_result``
    there, with a case for each path of the permutation test's draws.

    The rule, as for CUPED: absent from every version released before it,
    present under 1.13.0 and under the running version.  A key added under a
    frozen version would pass every hash."""
    assert PER_USER_MEAN_FIRST_VERSION in RELEASED_ENGINE_VERSIONS
    for version in RELEASED_ENGINE_VERSIONS:
        pinned = "per_user_mean_metric_result" in SUFFICIENT_STATS_FINGERPRINTS[version]
        if _key(version) < _key(PER_USER_MEAN_FIRST_VERSION):
            assert not pinned, (
                f"per_user_mean_metric_result is pinned under {version}, which "
                f"shipped before #2041"
            )
        else:
            assert pinned, f"per_user_mean_metric_result is missing from {version}"
    assert set(PER_USER_MEAN_CASE_DIGESTS) <= {
        v
        for v in RELEASED_ENGINE_VERSIONS
        if _key(v) >= _key(PER_USER_MEAN_FIRST_VERSION)
    }
    assert ENGINE_VERSION in PER_USER_MEAN_CASE_DIGESTS


def test_every_pinned_function_is_exercised():
    """A pinned key with no estimator behind it would pass by being skipped."""
    assert set(SUFFICIENT_STATS_FINGERPRINTS[ENGINE_VERSION]) == set(_ESTIMATORS)


@pytest.mark.parametrize("fixture_name", sorted(_FIXTURES))
@pytest.mark.parametrize(
    "confidence, correction",
    [(0.95, "none"), (0.95, "bonferroni"), (0.90, "benjamini_hochberg")],
)
def test_binomial_metric_result_is_what_results_returns(
    fixture_name, confidence, correction, monkeypatch
):
    """Same counts in, the same objects out: ``==``, not a rounded hash."""
    fixture = _FIXTURES[fixture_name]
    experiment = _experiment()
    output = results_output(fixture_name, confidence, correction, monkeypatch)
    for metric, from_results in zip(
        sorted(experiment.metric_definitions, key=lambda m: not m.is_primary),
        output["metrics"],
    ):
        assert from_results["metric_id"] == metric.id
        counts = [
            (
                v,
                fixture.units[v.id],
                fixture.converted[(metric.event_name, v.id)],
            )
            for v in experiment.variants
        ]
        assert (
            binomial_metric_result(counts, 1.0 - confidence, correction, metric=metric)
            == from_results
        )


def test_binomial_metric_result_needs_a_control():
    with pytest.raises(ValueError, match="control"):
        binomial_metric_result(
            [(BinomialVariant(_TREATMENT_1, "t1", False), 10, 1)],
            0.05,
            "none",
            metric=_METRIC,
        )

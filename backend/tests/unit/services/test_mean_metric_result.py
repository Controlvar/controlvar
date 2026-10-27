"""
``mean_metric_result``: a mean metric's result from sums centred on the grand
mean (#312).

* **Exactness.**  The fixture F-MEAN-1 (200,000 units per arm around 1e4, and
  the same values moved by 1e9) goes through the estimator as a warehouse
  would send it: ``k`` is the grand mean, and ``sum(y - k)``,
  ``sum((y - k) ** 2)`` are float64 sums in stored order.  Every number the
  result carries is compared with an exact reference: ``fractions.Fraction``
  sums over the stored float64 values, 60-digit ``Decimal`` square roots, the
  exact Welch-Satterthwaite degrees of freedom.  The tolerances are the
  measured error with headroom: rel 1e-12 at 1e4 and 2e-12 at 1e9.  Sums of
  ``y`` and ``y ** 2`` (no centring) are off by about 4e-6 at 1e4 and give a
  negative variance at 1e9; passing ``k + c`` into Welch instead of ``c`` is
  off by about 3e-10 and 3e-6.  Both fail here.
* **Definitions.**  On small raw arrays, the interval, Cohen's d, power and
  the p-value equal scipy's ``t.ppf`` and ``ttest_ind``,
  ``AnalysisService.cohens_d`` and ``NormalIndPower``: the variance is the
  sample variance (ddof 1).
* **Refusals.**  Sums with a negative variance are refused, not computed.  A
  rounding residue below 0 (an arm with no buyers, centred on the grand mean)
  is read as 0, within the tolerance the CUPED sums use.
"""

import math
from decimal import Decimal, getcontext
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pytest
from scipy import stats
from statsmodels.stats.power import NormalIndPower

from backend.app.schemas.results import MetricResult
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    SufficientStatsNotComputed,
    SufficientStatsRefused,
    adjusted_p_values,
    mean_metric_result,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_CONTROL = BinomialVariant("ffffffff-0000-4000-8000-000000000002", "control", True)
_TREATMENT = BinomialVariant("00000000-0000-4000-8000-000000000001", "t1", False)
_TREATMENT_2 = BinomialVariant("77777777-0000-4000-8000-000000000003", "t2", False)
_METRIC = SimpleNamespace(
    id="44444444-0000-4000-8000-000000000004",
    name="Revenue per user",
    metric_type="revenue",
    is_primary=True,
)
_ALPHA = 0.05

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _centred_sums(values: np.ndarray, k: float) -> Tuple[float, float]:
    """``sum(y - k)`` and ``sum((y - k) ** 2)`` in float64, one value at a
    time in stored order, as a SQL ``SUM`` over the rows would."""
    s = 0.0
    s2 = 0.0
    for y in values.tolist():
        d = y - k
        s += d
        s2 += d * d
    return s, s2


def _run(arrays: Dict[Any, np.ndarray], alpha: float = _ALPHA) -> Dict[str, Any]:
    k = float(np.concatenate(list(arrays.values())).mean())
    rows = []
    for variant, values in arrays.items():
        sum_d, sum_d2 = _centred_sums(values, k)
        rows.append((variant, len(values), sum_d, sum_d2))
    return mean_metric_result(k, rows, alpha, "none", metric=_METRIC)


def _by_name(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {v["variant_name"]: v for v in result["variants"]}


def _rel(got: float, want: float) -> float:
    return abs(got - want) / abs(want) if want != 0 else abs(got)


# ---------------------------------------------------------------------------
# Exactness against a Fraction/Decimal reference
# ---------------------------------------------------------------------------


def _f_mean_1(extra_offset: float) -> Tuple[np.ndarray, np.ndarray]:
    """F-MEAN-1: 200,000 units per arm around 1e4; ``extra_offset`` is added to
    both arms in float64 (the +1e9 copy)."""
    rng = np.random.default_rng(20260927)
    control = 1e4 + rng.normal(0.0, 1.0, 200_000)
    treatment = 1e4 + rng.normal(0.005, 1.0, 200_000)
    if extra_offset:
        control = control + extra_offset
        treatment = treatment + extra_offset
    return control, treatment


def _dec(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _exact_reference(
    control: np.ndarray, treatment: np.ndarray, alpha: float
) -> Dict[str, float]:
    """Every output quantity, computed exactly from the stored float64 values
    and rounded to float64 once, at the end."""
    getcontext().prec = 60

    def arm(values: np.ndarray) -> Tuple[int, Fraction, Fraction]:
        s = Fraction(0)
        s2 = Fraction(0)
        for y in values.tolist():
            f = Fraction(y)
            s += f
            s2 += f * f
        n = len(values)
        return n, s / n, (s2 - s * s / n) / (n - 1)

    nc, mc, vc = arm(control)
    nt, mt, vt = arm(treatment)
    se2 = vt / nt + vc / nc
    diff = mt - mc
    t_ref = _dec(diff) / _dec(se2).sqrt()
    df_ref = se2 * se2 / ((vt / nt) ** 2 / (nt - 1) + (vc / nc) ** 2 / (nc - 1))
    d_ref = _dec(diff) / _dec((vc + vt) / 2).sqrt()
    ref = {
        "mean_c": float(mc),
        "mean_t": float(mt),
        "std_c": float(_dec(vc).sqrt()),
        "std_t": float(_dec(vt).sqrt()),
        "p": float(2 * stats.t.sf(abs(float(t_ref)), float(df_ref))),
        "effect": float(d_ref),
        "improvement": float(_dec(diff / abs(mc) * 100)),
        "power": float(
            NormalIndPower().power(
                effect_size=abs(float(d_ref)),
                nobs1=nt,
                alpha=alpha,
                ratio=nc / nt,
            )
        ),
    }
    for name, n, mean, var in (("c", nc, mc, vc), ("t", nt, mt, vt)):
        q = Decimal(repr(float(stats.t.ppf(1 - alpha / 2, n - 1))))
        half = q * _dec(var / n).sqrt()
        ref[f"ci_low_{name}"] = float(_dec(mean) - half)
        ref[f"ci_high_{name}"] = float(_dec(mean) + half)
    return ref


def _got(result: Dict[str, Any]) -> Dict[str, float]:
    v = _by_name(result)
    c, t = v["control"], v["t1"]
    return {
        "mean_c": c["mean"],
        "mean_t": t["mean"],
        "std_c": c["std_dev"],
        "std_t": t["std_dev"],
        "p": t["p_value"],
        "effect": t["effect_size"],
        "improvement": t["relative_improvement_pct"],
        "power": t["power"],
        "ci_low_c": c["confidence_interval"][0],
        "ci_high_c": c["confidence_interval"][1],
        "ci_low_t": t["confidence_interval"][0],
        "ci_high_t": t["confidence_interval"][1],
    }


@pytest.mark.parametrize(
    "extra_offset, tolerance",
    [
        pytest.param(0.0, 1e-12, id="f_mean_1"),
        pytest.param(1e9, 2e-12, id="f_mean_1_plus_1e9"),
    ],
)
def test_mean_metric_result_matches_exact_reference(extra_offset, tolerance):
    control, treatment = _f_mean_1(extra_offset)
    result = _run({_CONTROL: control, _TREATMENT: treatment})
    got = _got(result)
    ref = _exact_reference(control, treatment, _ALPHA)
    errors = {name: _rel(got[name], ref[name]) for name in ref}
    worst = max(errors, key=errors.get)
    assert errors[worst] <= tolerance, (
        f"{worst} is off by rel {errors[worst]:.2e} (tolerance {tolerance:.0e}); "
        + ", ".join(f"{k}={e:.1e}" for k, e in sorted(errors.items()))
    )
    assert result["variants"][1]["statistical_test_used"] == "welch_t_test"


# ---------------------------------------------------------------------------
# Definitions, against the raw arrays
# ---------------------------------------------------------------------------


def _small_arrays() -> Dict[Any, np.ndarray]:
    rng = np.random.default_rng(312)
    return {
        _CONTROL: 250.0 + rng.normal(0.0, 3.0, 40),
        _TREATMENT: 250.0 + rng.normal(1.5, 4.0, 55),
    }


@pytest.mark.parametrize("alpha", [0.05, 0.10])
def test_mean_ci_d_power_match_raw_arrays(alpha):
    arrays = _small_arrays()
    control, treatment = arrays[_CONTROL], arrays[_TREATMENT]
    v = _by_name(_run(arrays, alpha))

    for name, values in (("control", control), ("t1", treatment)):
        n = len(values)
        mean = float(np.mean(values))
        sd = float(np.std(values, ddof=1))
        half = float(stats.t.ppf(1 - alpha / 2, n - 1)) * sd / math.sqrt(n)
        assert v[name]["sample_size"] == n
        assert v[name]["conversions"] is None
        assert v[name]["mean"] == pytest.approx(mean, rel=1e-12)
        assert v[name]["std_dev"] == pytest.approx(sd, rel=1e-9)
        low, high = v[name]["confidence_interval"]
        assert low == pytest.approx(mean - half, rel=1e-12)
        assert high == pytest.approx(mean + half, rel=1e-12)

    d, label = AnalysisService(db=None).cohens_d(list(control), list(treatment))
    power = NormalIndPower().power(
        effect_size=abs(d),
        nobs1=len(treatment),
        alpha=alpha,
        ratio=len(control) / len(treatment),
    )
    welch = stats.ttest_ind(treatment, control, equal_var=False)
    t1 = v["t1"]
    assert t1["effect_size"] == pytest.approx(d, rel=1e-9)
    assert t1["effect_size_label"] == label
    assert t1["power"] == pytest.approx(power, rel=1e-9)
    assert t1["p_value"] == pytest.approx(float(welch.pvalue), rel=1e-9)
    assert t1["relative_improvement_pct"] == pytest.approx(
        (np.mean(treatment) - np.mean(control)) / abs(np.mean(control)) * 100,
        rel=1e-9,
    )
    assert t1["is_significant"] == (float(welch.pvalue) < alpha)
    assert v["control"]["p_value"] is None
    assert v["control"]["effect_size"] is None


def test_the_result_is_a_valid_metric_result():
    result = _run(_small_arrays())
    parsed = MetricResult.model_validate(result)
    assert parsed.metric_type == "revenue"
    assert [v.statistical_test_used for v in parsed.variants] == [
        None,
        "welch_t_test",
    ]


# ---------------------------------------------------------------------------
# Refusals and what is not computed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("negative_arm", ["control", "treatment"])
def test_negative_variance_refused(negative_arm):
    """sum_d2 < sum_d ** 2 / n: no sample has these sums."""
    good = (100, 3.0, 250.0)
    bad = (100, 50.0, 24.0)  # 24 < 50 ** 2 / 100 = 25
    control, treatment = (bad, good) if negative_arm == "control" else (good, bad)
    with pytest.raises(SufficientStatsRefused) as refused:
        mean_metric_result(
            10.0,
            [(_CONTROL, *control), (_TREATMENT, *treatment)],
            _ALPHA,
            "none",
            metric=_METRIC,
        )
    assert refused.value.code == "result_invalid"


@pytest.mark.parametrize("no_buyer_arm", ["control", "treatment"])
def test_a_no_buyer_arm_gives_a_result(no_buyer_arm):
    """Every value 0 in an arm of 3, centred on k = 0.1 (the other arm has the
    buyers).  The true variance is 0, but the float64 sums leave
    ``sum_d2 - sum_d ** 2 / n`` at about -3.5e-18: rounding, read as 0, not a
    negative variance."""
    k = 0.1
    no_buyer = _centred_sums(np.zeros(3), k)
    buyers = _centred_sums(np.array([0.0, 0.0, 0.6]), k)
    # The residue is there, or this would not test the tolerance.
    assert no_buyer[1] - no_buyer[0] ** 2 / 3 < 0
    control, treatment = (
        (no_buyer, buyers) if no_buyer_arm == "control" else (buyers, no_buyer)
    )
    result = mean_metric_result(
        k,
        [(_CONTROL, 3, *control), (_TREATMENT, 3, *treatment)],
        _ALPHA,
        "none",
        metric=_METRIC,
    )
    v = _by_name(result)
    empty = v["control" if no_buyer_arm == "control" else "t1"]
    assert empty["mean"] == pytest.approx(0.0, abs=1e-15)
    assert empty["std_dev"] == 0.0
    # One arm varies, so the treatment is compared.
    assert v["t1"]["p_value"] is not None
    assert v["t1"]["note"] is None
    MetricResult.model_validate(result)


@pytest.mark.parametrize(
    "row",
    [
        (-1, 0.0, 0.0),
        (2.0, 0.0, 1.0),
        (True, 0.0, 1.0),
        (10, float("nan"), 1.0),
        (10, 0.0, float("inf")),
    ],
)
def test_malformed_sums_refused(row):
    with pytest.raises(SufficientStatsRefused):
        mean_metric_result(
            0.0,
            [(_CONTROL, 10, 0.0, 9.0), (_TREATMENT, *row)],
            _ALPHA,
            "none",
            metric=_METRIC,
        )


def test_non_finite_k_refused():
    with pytest.raises(SufficientStatsRefused):
        mean_metric_result(
            float("inf"),
            [(_CONTROL, 10, 0.0, 9.0), (_TREATMENT, 10, 0.0, 9.0)],
            _ALPHA,
            "none",
            metric=_METRIC,
        )


@pytest.mark.parametrize("n", [0, 1])
def test_fewer_than_two_units_not_computed(n):
    with pytest.raises(SufficientStatsNotComputed) as not_computed:
        mean_metric_result(
            5.0,
            [(_CONTROL, 10, 0.0, 9.0), (_TREATMENT, n, 0.0, 0.0)],
            _ALPHA,
            "none",
            metric=_METRIC,
        )
    assert not_computed.value.code == "fewer_than_2_units"
    assert not_computed.value.message == "Not computed: fewer than 2 units"


def test_no_variation_in_either_arm_has_no_p_value():
    result = mean_metric_result(
        5.0,
        [(_CONTROL, 10, 0.0, 0.0), (_TREATMENT, 12, 12.0, 12.0)],
        _ALPHA,
        "none",
        metric=_METRIC,
    )
    v = _by_name(result)
    t1 = v["t1"]
    assert t1["mean"] == 6.0
    assert t1["std_dev"] == 0.0
    assert t1["p_value"] is None
    assert t1["effect_size"] is None
    assert t1["power"] is None
    assert t1["statistical_test_used"] is None
    assert t1["is_significant"] is False
    assert t1["note"] == "Not computed: no variation"
    assert t1["relative_improvement_pct"] == pytest.approx(20.0)
    assert result["winning_variant_id"] is None


def test_no_variation_in_one_arm_is_still_compared():
    result = mean_metric_result(
        5.0,
        [(_CONTROL, 10, 0.0, 0.0), (_TREATMENT, 12, 12.0, 30.0)],
        _ALPHA,
        "none",
        metric=_METRIC,
    )
    t1 = _by_name(result)["t1"]
    assert t1["p_value"] is not None
    assert t1["note"] is None


def test_relative_improvement_is_none_when_the_control_mean_is_zero():
    # Control values average exactly 0 (k = 1, centred mean -1).
    result = mean_metric_result(
        1.0,
        [(_CONTROL, 10, -10.0, 20.0), (_TREATMENT, 10, 5.0, 30.0)],
        _ALPHA,
        "none",
        metric=_METRIC,
    )
    v = _by_name(result)
    assert v["control"]["mean"] == 0.0
    assert v["t1"]["relative_improvement_pct"] is None
    assert v["t1"]["p_value"] is not None


def test_mean_metric_result_needs_a_control():
    with pytest.raises(ValueError, match="control"):
        mean_metric_result(
            0.0,
            [(_TREATMENT, 10, 0.0, 9.0)],
            _ALPHA,
            "none",
            metric=_METRIC,
        )


# ---------------------------------------------------------------------------
# Several treatments: correction and the winner
# ---------------------------------------------------------------------------


def _three_arms() -> List[Tuple[Any, int, float, float]]:
    # k = 100; centred means 0, +0.5, +0.05; variance 4 in every arm.
    rows: List[Tuple[Any, int, float, float]] = []
    for variant, n, c in ((_CONTROL, 2000, 0.0), (_TREATMENT, 2000, 0.5)):
        rows.append((variant, n, c * n, 4.0 * (n - 1) + c * c * n))
    n, c = 2000, 0.05
    rows.append((_TREATMENT_2, n, c * n, 4.0 * (n - 1) + c * c * n))
    return rows


@pytest.mark.parametrize("method", ["none", "bonferroni", "benjamini_hochberg"])
def test_correction_is_applied_across_treatments(method):
    rows: Sequence[Tuple[Any, int, float, float]] = _three_arms()
    raw = _by_name(mean_metric_result(100.0, rows, _ALPHA, "none", metric=_METRIC))
    p = [raw["t1"]["p_value"], raw["t2"]["p_value"]]
    expected = adjusted_p_values(p, method)
    result = mean_metric_result(100.0, rows, _ALPHA, method, metric=_METRIC)
    v = _by_name(result)
    assert [v["t1"]["adjusted_p_value"], v["t2"]["adjusted_p_value"]] == expected
    assert [v["t1"]["p_value"], v["t2"]["p_value"]] == p
    assert v["t1"]["is_significant"] is True
    assert result["has_significant_result"] is True
    assert result["winning_variant_id"] == _TREATMENT.id

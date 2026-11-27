"""
Unit tests for PowerCalculatorService (EP-056).

Coverage:
- Sample size computation (proportions, means, Bonferroni, one/two-tailed)
- MDE computation (binary search, round-trip consistency)
- Runtime estimation (days, weeks, CI)
- Power curve generation (monotonicity, target marking, defaults)
- Edge-case validation (invalid inputs raise ValueError)

No network calls are made — all tests are pure CPU.
"""

import math

import pytest
from scipy.stats import norm

from backend.app.services.power_calculator_service import (
    MDEResult,
    PowerCalculatorService,
    PowerCurvePoint,
    RuntimeEstimate,
    SampleSizeResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def calc() -> PowerCalculatorService:
    return PowerCalculatorService()


# ---------------------------------------------------------------------------
# Reference values
# ---------------------------------------------------------------------------
# All expected values verified against scipy.stats.norm manually.
#
# Formula (Fleiss 2003, two-proportions z-test):
#   n = [z_alpha * sqrt(2*p_bar*(1-p_bar)) + z_power * sqrt(p1*(1-p1)+p2*(1-p2))]^2
#       / (p2 - p1)^2
#
# For baseline=0.05, mde_relative=0.10, p2=0.055, alpha=0.05, power=0.80:
#   z_alpha = 1.9600, z_power = 0.8416
#   → n ≈ 31234 per variant (verified independently)
#
# The spec mentioned "~3842" but that does not match any standard formula for
# these exact parameters. The implementation uses the correct Fleiss (2003)
# formula and the tests assert against the actual scipy-derived values.

REF_BASELINE = 0.05
REF_MDE_REL = 0.10  # 10% relative lift → p2 = 0.055
REF_N_PER_VARIANT = 31234  # verified via scipy.stats.norm

HIGHER_BASELINE = 0.10
HIGHER_MDE_REL = 0.05  # 5% relative lift → p2 = 0.105
HIGHER_BASELINE_N = 57763  # verified via scipy.stats.norm (Fleiss formula)


# ===========================================================================
# TestSampleSizeComputation
# ===========================================================================


class TestSampleSizeComputation:
    """Tests for compute_sample_size."""

    def test_returns_sample_size_result_type(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=REF_BASELINE,
            minimum_detectable_effect=REF_MDE_REL,
        )
        assert isinstance(result, SampleSizeResult)

    def test_reference_value_baseline_005_mde_010(self, calc):
        """Reference case: baseline=5%, mde=10% relative → ~31234 per variant."""
        result = calc.compute_sample_size(
            baseline_rate=REF_BASELINE,
            minimum_detectable_effect=REF_MDE_REL,
            alpha=0.05,
            power=0.80,
        )
        # Allow ±5 for ceiling differences
        assert abs(result.per_variant - REF_N_PER_VARIANT) <= 5, (
            f"Expected ~{REF_N_PER_VARIANT}, got {result.per_variant}"
        )

    def test_total_equals_per_variant_times_n_variants(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
        )
        assert result.total == result.per_variant * result.n_variants

    def test_higher_baseline_smaller_relative_mde(self, calc):
        """Higher baseline with smaller relative MDE."""
        result = calc.compute_sample_size(
            baseline_rate=HIGHER_BASELINE,
            minimum_detectable_effect=HIGHER_MDE_REL,
        )
        # Approximately 14776 per variant for baseline=0.10, mde=5%
        assert abs(result.per_variant - HIGHER_BASELINE_N) <= 10, (
            f"Expected ~{HIGHER_BASELINE_N}, got {result.per_variant}"
        )

    def test_smaller_alpha_requires_larger_sample(self, calc):
        """Stricter alpha (0.01) requires more samples than alpha=0.05."""
        n_005 = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, alpha=0.05
        )
        n_001 = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, alpha=0.01
        )
        assert n_001.per_variant > n_005.per_variant

    def test_higher_power_requires_larger_sample(self, calc):
        """Power=0.90 requires more samples than power=0.80."""
        n_080 = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, power=0.80
        )
        n_090 = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, power=0.90
        )
        assert n_090.per_variant > n_080.per_variant

    def test_three_variants_bonferroni_larger_than_two(self, calc):
        """Bonferroni correction for 3 variants increases required n."""
        n_2v = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, n_variants=2
        )
        n_3v = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, n_variants=3
        )
        assert n_3v.per_variant > n_2v.per_variant

    def test_a_third_variant_plans_each_comparison_at_alpha_over_two(self, calc):
        """The page: corrected_alpha = alpha / (n_variants - 1), 0.05 / 2 = 0.025."""
        three = calc.compute_sample_size(
            baseline_rate=REF_BASELINE,
            minimum_detectable_effect=REF_MDE_REL,
            alpha=0.05,
            n_variants=3,
        )
        two_at_half_the_alpha = calc.compute_sample_size(
            baseline_rate=REF_BASELINE,
            minimum_detectable_effect=REF_MDE_REL,
            alpha=0.025,
            n_variants=2,
        )
        assert three.per_variant == two_at_half_the_alpha.per_variant
        assert three.per_variant > REF_N_PER_VARIANT

    def test_the_size_is_the_formula_the_page_writes_out(self, calc):
        """p1, p2, p_bar, z_alpha, z_power and n of the page's verification example."""
        p1 = REF_BASELINE
        p2 = p1 + p1 * REF_MDE_REL
        p_bar = (p1 + p2) / 2
        z_alpha = norm.ppf(1 - 0.05 / 2)
        z_power = norm.ppf(0.80)
        assert (p1, p2, p_bar) == pytest.approx((0.05, 0.055, 0.0525))
        assert round(z_alpha, 4) == 1.96
        assert round(z_power, 3) == 0.842
        assert round(z_power, 4) == 0.8416
        n = (
            z_alpha * math.sqrt(2 * p_bar * (1 - p_bar))
            + z_power * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
        ) ** 2 / (p2 - p1) ** 2

        result = calc.compute_sample_size(
            baseline_rate=p1,
            minimum_detectable_effect=REF_MDE_REL,
            alpha=0.05,
            power=0.80,
        )
        assert result.per_variant == math.ceil(n) == 31234
        assert result.total == 62468

    def test_one_tailed_requires_fewer_samples_than_two_tailed(self, calc):
        """One-tailed test needs fewer samples than two-tailed."""
        n_two = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, two_tailed=True
        )
        n_one = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, two_tailed=False
        )
        assert n_one.per_variant < n_two.per_variant

    def test_larger_mde_requires_smaller_sample(self, calc):
        """Larger MDE → easier to detect → fewer samples required."""
        n_small_mde = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.05
        )
        n_large_mde = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.20
        )
        assert n_large_mde.per_variant < n_small_mde.per_variant

    def test_confidence_level_is_one_minus_alpha(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, alpha=0.05
        )
        assert abs(result.confidence_level - 0.95) < 1e-9

    def test_mde_absolute_equals_baseline_times_relative(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10, minimum_detectable_effect=0.20
        )
        expected_abs = 0.10 * 0.20
        assert abs(result.mde_absolute - expected_abs) < 1e-9

    def test_metric_type_proportion_returns_result(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
            metric_type="proportion",
        )
        assert result.metric_type == "proportion"
        assert result.per_variant > 0

    def test_metric_type_mean_with_std_returns_result(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.50,
            minimum_detectable_effect=0.10,
            metric_type="mean",
            baseline_std=0.10,
        )
        assert result.metric_type == "mean"
        assert result.per_variant > 0

    def test_metric_type_mean_without_std_raises_value_error(self, calc):
        with pytest.raises(ValueError, match="baseline_std"):
            calc.compute_sample_size(
                baseline_rate=0.50,
                minimum_detectable_effect=0.10,
                metric_type="mean",
                baseline_std=None,
            )

    def test_runtime_returned_when_daily_traffic_provided(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
            daily_traffic=5000,
            traffic_allocation=1.0,
        )
        assert result.runtime_days is not None
        assert result.runtime_days > 0

    def test_runtime_none_when_no_daily_traffic(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
        )
        assert result.runtime_days is None

    def test_higher_traffic_gives_shorter_runtime(self, calc):
        r_low = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
            daily_traffic=100,
        )
        r_high = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
            daily_traffic=10000,
        )
        assert r_high.runtime_days < r_low.runtime_days

    def test_n_variants_stored_in_result(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
            n_variants=4,
        )
        assert result.n_variants == 4

    def test_two_tailed_flag_stored_in_result(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.10,
            minimum_detectable_effect=0.10,
            two_tailed=False,
        )
        assert result.two_tailed is False

    def test_per_variant_is_positive_integer(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.30, minimum_detectable_effect=0.10
        )
        assert isinstance(result.per_variant, int)
        assert result.per_variant > 0


# ===========================================================================
# TestSampleSizeValidation  (edge cases / invalid inputs)
# ===========================================================================


class TestSampleSizeValidation:
    """Tests for input validation in compute_sample_size."""

    def test_baseline_rate_zero_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=0.0, minimum_detectable_effect=0.10)

    def test_baseline_rate_one_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=1.0, minimum_detectable_effect=0.10)

    def test_baseline_rate_negative_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=-0.1, minimum_detectable_effect=0.10)

    def test_mde_zero_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=0.10, minimum_detectable_effect=0.0)

    def test_mde_negative_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10, minimum_detectable_effect=-0.05
            )

    def test_mde_one_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=0.10, minimum_detectable_effect=1.0)

    def test_mde_above_one_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=0.10, minimum_detectable_effect=1.5)

    def test_alpha_zero_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10, minimum_detectable_effect=0.10, alpha=0.0
            )

    def test_alpha_05_or_above_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10, minimum_detectable_effect=0.10, alpha=0.5
            )

    def test_alpha_above_05_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10, minimum_detectable_effect=0.10, alpha=0.8
            )

    def test_power_zero_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10, minimum_detectable_effect=0.10, power=0.0
            )

    def test_power_one_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10, minimum_detectable_effect=0.10, power=1.0
            )

    def test_treatment_rate_above_one_raises_value_error(self, calc):
        """baseline_rate=0.95, mde=0.10 → p2=1.045 which is invalid."""
        with pytest.raises(ValueError):
            calc.compute_sample_size(baseline_rate=0.95, minimum_detectable_effect=0.10)

    def test_invalid_metric_type_raises_value_error(self, calc):
        with pytest.raises(ValueError):
            calc.compute_sample_size(
                baseline_rate=0.10,
                minimum_detectable_effect=0.10,
                metric_type="invalid_type",
            )


# ===========================================================================
# TestMDEComputation
# ===========================================================================


class TestMDEComputation:
    """Tests for compute_mde."""

    def test_returns_mde_result_type(self, calc):
        result = calc.compute_mde(
            sample_size_per_variant=31234,
            baseline_rate=0.05,
        )
        assert isinstance(result, MDEResult)

    def test_mde_approximate_round_trip(self, calc):
        """compute_mde should recover approximately the MDE used in compute_sample_size."""
        n = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10
        ).per_variant

        mde_result = calc.compute_mde(
            sample_size_per_variant=n,
            baseline_rate=0.20,
            alpha=0.05,
            power=0.80,
        )
        # The recovered MDE should be within 5% of 0.10
        assert abs(mde_result.mde_relative - 0.10) < 0.005, (
            f"Expected MDE ≈ 0.10, got {mde_result.mde_relative:.4f}"
        )

    def test_larger_sample_gives_smaller_mde(self, calc):
        """More samples → can detect smaller effects."""
        mde_small = calc.compute_mde(sample_size_per_variant=1000, baseline_rate=0.10)
        mde_large = calc.compute_mde(sample_size_per_variant=50000, baseline_rate=0.10)
        assert mde_large.mde_relative < mde_small.mde_relative

    def test_mde_absolute_equals_baseline_times_relative(self, calc):
        result = calc.compute_mde(sample_size_per_variant=5000, baseline_rate=0.20)
        expected_abs = (
            result.baseline_rate * result.mde_relative
            if hasattr(result, "baseline_rate")
            else 0.20 * result.mde_relative
        )
        # mde_absolute ≈ 0.20 * mde_relative
        assert abs(result.mde_absolute - 0.20 * result.mde_relative) < 0.001

    def test_total_sample_is_per_variant_times_n_variants(self, calc):
        result = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, n_variants=3
        )
        assert result.total_sample == 5000 * 3

    def test_n_variants_stored(self, calc):
        result = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, n_variants=3
        )
        assert result.n_variants == 3

    def test_alpha_stored(self, calc):
        result = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, alpha=0.01
        )
        assert result.alpha == 0.01

    def test_power_stored(self, calc):
        result = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, power=0.90
        )
        assert result.power == 0.90

    def test_mde_relative_is_positive(self, calc):
        result = calc.compute_mde(sample_size_per_variant=5000, baseline_rate=0.10)
        assert result.mde_relative > 0

    def test_mde_absolute_is_positive(self, calc):
        result = calc.compute_mde(sample_size_per_variant=5000, baseline_rate=0.10)
        assert result.mde_absolute > 0

    def test_mde_relative_decreases_with_power(self, calc):
        """Lower power → can detect smaller effects with same n."""
        mde_080 = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, power=0.80
        )
        mde_090 = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, power=0.90
        )
        assert mde_090.mde_relative > mde_080.mde_relative

    def test_mde_two_tailed_flag_stored(self, calc):
        result = calc.compute_mde(
            sample_size_per_variant=5000, baseline_rate=0.10, two_tailed=False
        )
        assert result.two_tailed is False


# ===========================================================================
# TestRuntimeEstimation
# ===========================================================================


class TestRuntimeEstimation:
    """Tests for compute_runtime_estimate."""

    def test_returns_runtime_estimate_type(self, calc):
        result = calc.compute_runtime_estimate(
            required_sample_size=1000, daily_traffic=500, traffic_allocation=1.0
        )
        assert isinstance(result, RuntimeEstimate)

    def test_basic_runtime_calculation(self, calc):
        """1000 users needed, 500/day at 100% allocation, 2 variants → 4 days."""
        result = calc.compute_runtime_estimate(
            required_sample_size=1000,
            daily_traffic=500,
            traffic_allocation=1.0,
            n_variants=2,
        )
        # daily_per_variant = 500 * 1.0 / 2 = 250
        # days = 1000 / 250 = 4.0
        assert abs(result.days_to_significance - 4.0) < 0.01

    def test_runtime_reference_case(self, calc):
        """31234 samples, 1000/day, 50% allocation, 2 variants → ~124.9 days."""
        result = calc.compute_runtime_estimate(
            required_sample_size=31234,
            daily_traffic=1000,
            traffic_allocation=0.5,
            n_variants=2,
        )
        # daily_per_variant = 1000 * 0.5 / 2 = 250
        # days = 31234 / 250 = 124.9
        assert abs(result.days_to_significance - 124.9) < 1.0

    def test_higher_traffic_gives_fewer_days(self, calc):
        r_low = calc.compute_runtime_estimate(
            required_sample_size=10000, daily_traffic=100, traffic_allocation=1.0
        )
        r_high = calc.compute_runtime_estimate(
            required_sample_size=10000, daily_traffic=10000, traffic_allocation=1.0
        )
        assert r_high.days_to_significance < r_low.days_to_significance

    def test_lower_allocation_gives_more_days(self, calc):
        r_full = calc.compute_runtime_estimate(
            required_sample_size=5000, daily_traffic=1000, traffic_allocation=1.0
        )
        r_half = calc.compute_runtime_estimate(
            required_sample_size=5000, daily_traffic=1000, traffic_allocation=0.5
        )
        assert r_half.days_to_significance > r_full.days_to_significance

    def test_weeks_equals_days_over_seven(self, calc):
        result = calc.compute_runtime_estimate(
            required_sample_size=7000, daily_traffic=1000, traffic_allocation=1.0
        )
        assert (
            abs(result.weeks_to_significance - result.days_to_significance / 7) < 1e-6
        )

    def test_confidence_interval_is_tuple_of_two(self, calc):
        result = calc.compute_runtime_estimate(
            required_sample_size=5000, daily_traffic=1000, traffic_allocation=1.0
        )
        assert len(result.confidence_interval_days) == 2

    def test_confidence_interval_lower_lt_days_lt_upper(self, calc):
        result = calc.compute_runtime_estimate(
            required_sample_size=5000, daily_traffic=1000, traffic_allocation=1.0
        )
        lo, hi = result.confidence_interval_days
        assert lo <= result.days_to_significance <= hi

    def test_daily_traffic_per_variant_correct(self, calc):
        result = calc.compute_runtime_estimate(
            required_sample_size=5000,
            daily_traffic=2000,
            traffic_allocation=0.5,
            n_variants=4,
        )
        # 2000 * 0.5 / 4 = 250
        assert result.daily_traffic_per_variant == 250

    def test_zero_daily_traffic_raises(self, calc):
        with pytest.raises(ValueError):
            calc.compute_runtime_estimate(
                required_sample_size=5000, daily_traffic=0, traffic_allocation=1.0
            )

    def test_invalid_allocation_zero_raises(self, calc):
        with pytest.raises(ValueError):
            calc.compute_runtime_estimate(
                required_sample_size=5000, daily_traffic=1000, traffic_allocation=0.0
            )

    def test_invalid_sample_size_raises(self, calc):
        with pytest.raises(ValueError):
            calc.compute_runtime_estimate(
                required_sample_size=0, daily_traffic=1000, traffic_allocation=1.0
            )


@pytest.mark.regression
class TestRuntimeUsesUnroundedDailyTraffic:
    """#1109: the days divide by the unrounded users per variant per day,
    ``daily_traffic * traffic_allocation / n_variants``, the dashboard's formula
    (frontend/src/utils/power.ts). Rounding that down to a whole number (at
    least 1) reported 31,235 days for 31,235 per variant at both 1 and 3 users
    a day."""

    @pytest.mark.parametrize(
        "daily_traffic, expected_days",
        [
            # 0.5 users per variant a day: 31,235 / 0.5
            pytest.param(1, 62_470.0, id="1-user-a-day"),
            # 1.5 users per variant a day: 31,235 / 1.5 = 20,823.3
            pytest.param(3, 31_235 / 1.5, id="3-users-a-day"),
        ],
    )
    def test_low_traffic_days(self, calc, daily_traffic, expected_days):
        result = calc.compute_runtime_estimate(
            required_sample_size=31_235,
            daily_traffic=daily_traffic,
            traffic_allocation=1.0,
            n_variants=2,
        )
        assert result.days_to_significance == pytest.approx(expected_days, rel=1e-12)
        assert result.weeks_to_significance == pytest.approx(
            expected_days / 7, rel=1e-12
        )
        lo, hi = result.confidence_interval_days
        assert lo <= result.days_to_significance <= hi
        # The whole-number figure keeps its type and its value.
        assert result.daily_traffic_per_variant == 1

    def test_fraction_above_one_user_a_day(self, calc):
        """101 users a day over 2 variants is 50.5 each, not 50."""
        result = calc.compute_runtime_estimate(
            required_sample_size=10_100,
            daily_traffic=101,
            traffic_allocation=1.0,
            n_variants=2,
        )
        assert result.days_to_significance == pytest.approx(200.0, rel=1e-12)
        assert result.daily_traffic_per_variant == 50

    def test_sample_size_runtime_days_at_one_user_a_day(self, calc):
        result = calc.compute_sample_size(
            baseline_rate=0.05,
            minimum_detectable_effect=0.10,
            daily_traffic=1,
            traffic_allocation=1.0,
        )
        assert result.runtime_days == pytest.approx(2 * result.per_variant, rel=1e-12)

    @pytest.mark.parametrize(
        "daily_traffic, allocation, expected_days, per_variant_per_day",
        [
            # docs/statistics/power-analysis.md, "Runtime Estimation"
            pytest.param(10_000, 0.5, 12.4936, 2_500, id="docs-50pct-allocation"),
            # frontend/src/tests/utils/power.test.ts
            pytest.param(10_000, 1.0, 6.2468, 5_000, id="dashboard-full-allocation"),
        ],
    )
    def test_documented_examples_unchanged(
        self, calc, daily_traffic, allocation, expected_days, per_variant_per_day
    ):
        result = calc.compute_runtime_estimate(
            required_sample_size=31_234,
            daily_traffic=daily_traffic,
            traffic_allocation=allocation,
            n_variants=2,
        )
        assert result.days_to_significance == pytest.approx(expected_days, rel=1e-12)
        assert result.daily_traffic_per_variant == per_variant_per_day

    @pytest.mark.parametrize("allocation", [5e-324, 1e-310])
    def test_allocation_too_small_to_calculate_raises_overflow_error(
        self, calc, allocation
    ):
        """The users per variant per day underflows to zero (5e-324) or the
        days leave float range (1e-310): OverflowError, which the routes answer
        with a 422, rather than a division by zero or an infinite day count."""
        with pytest.raises(OverflowError):
            calc.compute_runtime_estimate(
                required_sample_size=31_235,
                daily_traffic=1,
                traffic_allocation=allocation,
                n_variants=2,
            )

    @pytest.mark.parametrize(
        "allocation, n_variants",
        [
            pytest.param(0.0, 2, id="allocation-0"),
            pytest.param(1.0, 0, id="n_variants-0"),
            pytest.param(1.0, 1, id="n_variants-1"),
        ],
    )
    def test_zero_allocation_and_too_few_variants_still_refused(
        self, calc, allocation, n_variants
    ):
        with pytest.raises(ValueError):
            calc.compute_runtime_estimate(
                required_sample_size=31_235,
                daily_traffic=1,
                traffic_allocation=allocation,
                n_variants=n_variants,
            )


# ===========================================================================
# TestPowerCurve
# ===========================================================================


class TestPowerCurve:
    """Tests for compute_power_curve."""

    def test_returns_list_of_power_curve_points(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10)
        assert isinstance(points, list)
        assert all(isinstance(p, PowerCurvePoint) for p in points)

    def test_power_curve_non_empty(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10)
        assert len(points) > 0

    def test_points_are_monotonically_decreasing_in_sample_size(self, calc):
        """Larger effect size requires smaller sample size."""
        points = calc.compute_power_curve(baseline_rate=0.10)
        sizes = [p.sample_size_per_variant for p in points]
        # Sample size should decrease (or stay the same) as effect size increases
        for i in range(1, len(sizes)):
            assert sizes[i] <= sizes[i - 1], (
                f"Non-monotonic at index {i}: {sizes[i - 1]} → {sizes[i]}"
            )

    def test_default_effect_sizes_cover_1_to_50_percent(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10)
        effect_sizes = [p.effect_size_relative for p in points]
        assert min(effect_sizes) <= 0.05  # at least 5% covered
        assert max(effect_sizes) >= 0.40  # up to at least 40%

    def test_all_sample_sizes_positive(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10)
        for p in points:
            assert p.sample_size_per_variant > 0

    def test_at_most_one_current_target_marked(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10, mde_target=0.10)
        targets = [p for p in points if p.is_current_target]
        assert len(targets) <= 1

    def test_current_target_closest_to_mde(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10, mde_target=0.10)
        targets = [p for p in points if p.is_current_target]
        if targets:
            target = targets[0]
            non_targets = [p for p in points if not p.is_current_target]
            if non_targets:
                closest_other = min(
                    non_targets,
                    key=lambda p: abs(p.effect_size_relative - 0.10),
                )
                assert (
                    abs(target.effect_size_relative - 0.10)
                    <= abs(closest_other.effect_size_relative - 0.10) + 1e-9
                )

    def test_no_target_marked_when_mde_target_none(self, calc):
        points = calc.compute_power_curve(baseline_rate=0.10, mde_target=None)
        targets = [p for p in points if p.is_current_target]
        assert len(targets) == 0

    def test_custom_effect_sizes_respected(self, calc):
        custom_sizes = [0.05, 0.10, 0.20]
        points = calc.compute_power_curve(baseline_rate=0.10, effect_sizes=custom_sizes)
        returned_sizes = [p.effect_size_relative for p in points]
        for s in custom_sizes:
            assert s in returned_sizes

    def test_curve_with_different_alpha(self, calc):
        pts_005 = calc.compute_power_curve(baseline_rate=0.10, alpha=0.05)
        pts_001 = calc.compute_power_curve(baseline_rate=0.10, alpha=0.01)
        # Stricter alpha → more samples at same effect size
        assert pts_001[0].sample_size_per_variant > pts_005[0].sample_size_per_variant

    def test_curve_with_higher_power_target(self, calc):
        pts_080 = calc.compute_power_curve(baseline_rate=0.10, power_target=0.80)
        pts_090 = calc.compute_power_curve(baseline_rate=0.10, power_target=0.90)
        # Higher power → more samples
        assert pts_090[0].sample_size_per_variant > pts_080[0].sample_size_per_variant


# ===========================================================================
# TestRoundTripConsistency
# ===========================================================================


class TestRoundTripConsistency:
    """Round-trip tests between sample size and MDE."""

    def test_sample_size_mde_round_trip_baseline_020(self, calc):
        """compute_sample_size(0.20, mde=0.15) → n; compute_mde(n, 0.20) ≈ 0.15."""
        target_mde = 0.15
        n = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=target_mde
        ).per_variant

        recovered = calc.compute_mde(sample_size_per_variant=n, baseline_rate=0.20)
        assert abs(recovered.mde_relative - target_mde) < 0.01, (
            f"Round-trip mismatch: expected ~{target_mde}, got {recovered.mde_relative:.4f}"
        )

    def test_sample_size_mde_round_trip_baseline_030(self, calc):
        target_mde = 0.20
        n = calc.compute_sample_size(
            baseline_rate=0.30, minimum_detectable_effect=target_mde
        ).per_variant

        recovered = calc.compute_mde(sample_size_per_variant=n, baseline_rate=0.30)
        assert abs(recovered.mde_relative - target_mde) < 0.01, (
            f"Round-trip mismatch: expected ~{target_mde}, got {recovered.mde_relative:.4f}"
        )


# ===========================================================================
# TestEdgeCases
# ===========================================================================


class TestEdgeCases:
    """Miscellaneous edge-case tests."""

    def test_very_small_mde_valid_baseline(self, calc):
        """Very small MDE with small baseline is valid (just produces large n)."""
        result = calc.compute_sample_size(
            baseline_rate=0.05, minimum_detectable_effect=0.01
        )
        assert result.per_variant > 1_000_000  # very large sample expected

    def test_very_large_mde_small_sample(self, calc):
        """Very large MDE (e.g. 50% relative) requires few samples."""
        result = calc.compute_sample_size(
            baseline_rate=0.10, minimum_detectable_effect=0.50
        )
        assert result.per_variant < 1000

    def test_ratio_metric_type_works(self, calc):
        """metric_type='ratio' falls through to means formula with baseline_std."""
        result = calc.compute_sample_size(
            baseline_rate=0.50,
            minimum_detectable_effect=0.10,
            metric_type="ratio",
            baseline_std=0.20,
        )
        assert result.metric_type == "ratio"
        assert result.per_variant > 0

    def test_four_variants_bonferroni(self, calc):
        """Four variants applies Bonferroni for 3 comparisons."""
        n_2v = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, n_variants=2
        )
        n_4v = calc.compute_sample_size(
            baseline_rate=0.20, minimum_detectable_effect=0.10, n_variants=4
        )
        assert n_4v.per_variant > n_2v.per_variant

    def test_power_curve_skips_invalid_points(self, calc):
        """Power curve skips effect sizes where p2 >= 1."""
        # High baseline near 1 means some large effect sizes would produce p2>=1
        # baseline=0.90, mde=0.20 → p2=1.08 (invalid, should be skipped)
        points = calc.compute_power_curve(baseline_rate=0.90)
        # All returned points should have valid (positive) sample sizes
        for p in points:
            assert p.sample_size_per_variant > 0

    def test_full_traffic_allocation_runtime(self, calc):
        result = calc.compute_runtime_estimate(
            required_sample_size=10000,
            daily_traffic=10000,
            traffic_allocation=1.0,
            n_variants=2,
        )
        # daily_per_variant = 10000 / 2 = 5000
        # days = 10000 / 5000 = 2.0
        assert abs(result.days_to_significance - 2.0) < 0.01

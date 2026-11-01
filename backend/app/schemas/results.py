"""
Pydantic v2 schemas for the experiment results API (EP-016).

This module defines all request/response schemas for the Analytics &
Experiment Results Engine, including per-variant statistics, metric
aggregations, time-series data, sample-size calculations, and the
top-level experiment results response.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.schemas.bayesian import BayesianResultsResponse
from backend.app.schemas.dimensional import DimensionalBreakdownResponse
from backend.app.schemas.sequential import SequentialTestingResponse

# ---------------------------------------------------------------------------
# SRMResult (sample-ratio mismatch)
# ---------------------------------------------------------------------------


class SRMResult(BaseModel):
    """Sample-ratio-mismatch check of assignment counts vs. traffic allocation.

    A Pearson chi-square goodness-of-fit test of the observed per-variant
    assignment counts against the counts implied by each variant's
    ``traffic_allocation``.  ``warning`` is ``True`` when ``p_value`` is below
    0.001, in which case the randomisation is suspect and the per-metric
    results should not be trusted.

    Only reported for experiments with a fixed allocation.  A multi-armed
    bandit reallocates traffic deliberately (from ``BanditState`` weights,
    which are never written back to ``traffic_allocation``), so the whole
    block is ``null`` there rather than a permanent false alarm.

    Caveat: an allocation edited while the experiment was running is still
    tested against its *current* split, so the counts accumulated under the
    old split can trip the warning.
    """

    model_config = ConfigDict(from_attributes=True)

    chi2: float = Field(..., ge=0.0, description="Pearson chi-square statistic.")
    p_value: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Upper-tail probability with (variants - 1) degrees of freedom.",
    )
    warning: bool = Field(
        ...,
        description="True when p_value < 0.001 (the observed split does not match the allocation).",
    )
    expected: Dict[str, float] = Field(
        default_factory=dict,
        description="Expected assignment count per variant id, from traffic_allocation.",
    )
    observed: Dict[str, int] = Field(
        default_factory=dict,
        description="Observed assignment count per variant id.",
    )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RecommendationAction(str, Enum):
    """Decision recommendation for an experiment after analysis.

    SHIP_VARIANT    - A treatment variant has won; ship it.
    KEEP_CONTROL    - No variant beat the control; keep the status quo.
    CONTINUE_TESTING - Results are directional but not yet conclusive.
    INCONCLUSIVE    - Results are mixed or otherwise uninterpretable.
    """

    SHIP_VARIANT = "SHIP_VARIANT"
    KEEP_CONTROL = "KEEP_CONTROL"
    CONTINUE_TESTING = "CONTINUE_TESTING"
    INCONCLUSIVE = "INCONCLUSIVE"


class CorrectionMethod(str, Enum):
    """Multiple-comparison correction method applied to p-values.

    NONE               - No correction applied (raw p-values).
    BONFERRONI         - Conservative family-wise error rate control.
    BENJAMINI_HOCHBERG - False discovery rate (FDR) control; less conservative.
    """

    NONE = "none"
    BONFERRONI = "bonferroni"
    BENJAMINI_HOCHBERG = "benjamini_hochberg"


class EffectSizeLabel(str, Enum):
    """Qualitative label for the magnitude of an effect size.

    Based on Cohen's conventional thresholds (h or d):
      NEGLIGIBLE - |d| < 0.2
      SMALL      - 0.2 <= |d| < 0.5
      MEDIUM     - 0.5 <= |d| < 0.8
      LARGE      - |d| >= 0.8
    """

    NEGLIGIBLE = "negligible"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class StatisticalTest(str, Enum):
    """Statistical test used when computing significance for a variant result.

    FISHER_EXACT       - Fisher's exact test (small-sample conversion data).
    Z_TEST_PROPORTIONS - Two-proportion z-test (large-sample conversions).
    WELCH_T_TEST       - Welch's t-test (continuous/revenue metrics).
    """

    FISHER_EXACT = "fisher_exact"
    Z_TEST_PROPORTIONS = "z_test_proportions"
    WELCH_T_TEST = "welch_t_test"


# ---------------------------------------------------------------------------
# VariantResult
# ---------------------------------------------------------------------------


class VariantResult(BaseModel):
    """Statistical results for a single variant on a single metric.

    Contains all per-variant statistics produced by the analysis engine,
    including sample counts, point estimates, confidence intervals,
    significance tests, and effect-size characterisation.

    For the control variant, fields that represent a comparison vs. the
    control (``p_value``, ``adjusted_p_value``, ``relative_improvement_pct``,
    ``effect_size``, ``effect_size_label``, ``power``, and
    ``statistical_test_used``) are ``None``.

    The metric's ``analysis`` says what ``mean`` is.  For a metric analysed
    as a proportion (a conversion metric) ``mean`` is the share of the
    variant's users who converted, in [0, 1].  For a revenue, count, duration
    or custom metric (``analysis`` ``"mean"``) it is the mean per assigned
    user of each user's value, 0 for a user with none: for a count metric the
    number of the metric's events they sent, otherwise the total of those
    events' value.  ``conversions`` is the number of users who converted
    either way.
    """

    model_config = ConfigDict(from_attributes=True)

    variant_id: UUID = Field(..., description="Unique identifier of the variant.")
    variant_name: str = Field(..., description="Human-readable name of the variant.")
    is_control: bool = Field(
        ...,
        description="True when this is the control (baseline) variant.",
    )
    sample_size: Annotated[int, Field(ge=0)] = Field(
        ...,
        description="Number of unique users assigned to this variant.",
    )
    conversions: Optional[int] = Field(
        None,
        description=(
            "Number of assigned users with at least one conversion event; a "
            "user who converts several times counts once, so this never "
            "exceeds sample_size.  For a metric analysed as a mean too: the "
            "users who sent the metric's event, whatever its value."
        ),
    )
    mean: float = Field(
        ...,
        description=(
            "When the metric's analysis is 'proportion': the share of the "
            "variant's users who converted, in [0, 1].  When it is 'mean' (a "
            "revenue, count, duration or custom metric): the mean per "
            "assigned user of each user's value, 0 for a user with none: for "
            "a count metric the number of the metric's events they sent, "
            "otherwise the total of those events' value."
        ),
    )
    std_dev: Optional[float] = Field(
        None,
        description=(
            "Standard deviation of one user's outcome: sqrt(mean * (1 - mean)) "
            "for a proportion, the sample standard deviation of the per-user "
            "values for a mean.  None when the variant has no users, or for "
            "a mean when it has fewer than 2."
        ),
    )
    confidence_interval: tuple[float, float] = Field(
        ...,
        description=(
            "Two-sided confidence interval as (lower, upper).  "
            "Its level is the response's confidence_level: the one the "
            "request sent, otherwise the experiment's stored level.  A "
            "variant with too few users for one (none, or fewer than 2 for a "
            "mean) has (mean, mean)."
        ),
    )
    p_value: Optional[float] = Field(
        None,
        description=(
            "Unadjusted p-value from the chosen statistical test.  "
            "None for the control variant."
        ),
    )
    adjusted_p_value: Optional[float] = Field(
        None,
        description=(
            "p-value after applying the experiment-level multiple-comparison "
            "correction method.  None for the control variant or when "
            "correction_method is NONE."
        ),
    )
    is_significant: bool = Field(
        ...,
        description=(
            "True when the adjusted (or raw, if no correction) p-value is "
            "below the significance threshold 1 - confidence_level, the same "
            "level as confidence_interval."
        ),
    )
    effect_size: Optional[float] = Field(
        None,
        description=(
            "Standardised effect size: Cohen's h for conversion metrics, "
            "Cohen's d for continuous metrics.  None for the control variant."
        ),
    )
    effect_size_label: Optional[EffectSizeLabel] = Field(
        None,
        description=(
            "Qualitative label derived from the Cohen's convention thresholds.  "
            "None for the control variant."
        ),
    )
    relative_improvement_pct: Optional[float] = Field(
        None,
        description=(
            "Relative change in the metric mean compared with the control, "
            "expressed as a percentage: ((variant_mean - control_mean) / "
            "control_mean) * 100.  The sign is the raw change whatever the "
            "metric's direction: when the metric's lower_is_better is true, a "
            "negative value is the good direction.  None for the control "
            "variant, and for a mean when the control's mean is 0."
        ),
    )
    power: Optional[float] = Field(
        None,
        description=(
            "Achieved statistical power (1 - beta) for detecting the observed "
            "effect at the configured significance level.  None for the control "
            "variant."
        ),
    )
    statistical_test_used: Optional[StatisticalTest] = Field(
        None,
        description=(
            "The statistical test that produced p_value.  None for the control variant."
        ),
    )
    absolute_difference: Optional[float] = Field(
        None,
        description=(
            "When the metric's analysis is 'mean': this variant's mean minus "
            "the control's, per assigned user.  None for the control, for a "
            "proportion metric, and when either variant has fewer than 2 users."
        ),
    )
    absolute_difference_ci: Optional[tuple[float, float]] = Field(
        None,
        description=(
            "Two-sided Welch-Satterthwaite confidence interval of "
            "absolute_difference as (lower, upper), at the response's "
            "confidence_level and not adjusted for multiple comparisons.  "
            "None when absolute_difference is None, and when neither variant's "
            "per-user values vary."
        ),
    )

    @field_validator("p_value", "adjusted_p_value", mode="before")
    @classmethod
    def validate_p_value_range(cls, v: Optional[float]) -> Optional[float]:
        """Validate that p-values are either None or in [0.0, 1.0]."""
        if v is None:
            return v
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"p-value must be in the range [0.0, 1.0], got {v!r}.")
        return v


# ---------------------------------------------------------------------------
# MetricResult
# ---------------------------------------------------------------------------


class MetricResult(BaseModel):
    """Aggregated results for one metric across all experiment variants.

    Holds the per-variant ``VariantResult`` objects together with summary
    flags that indicate whether the experiment produced a statistically
    significant winner for this metric.
    """

    model_config = ConfigDict(from_attributes=True)

    metric_id: UUID = Field(
        ..., description="Unique identifier of the metric definition."
    )
    metric_name: str = Field(..., description="Human-readable name of the metric.")
    metric_type: str = Field(
        ...,
        description=(
            "Metric category: one of 'conversion', 'revenue', 'count', "
            "'duration', or 'custom'."
        ),
    )
    is_primary: bool = Field(
        ...,
        description=(
            "True when this is the primary (decision) metric for the experiment."
        ),
    )
    lower_is_better: bool = Field(
        False,
        description=(
            "The metric definition's lower_is_better: true when a decrease is "
            "the good direction (errors, latency, uninstalls).  "
            "winning_variant_id, and for the primary metric the summary's "
            "recommendation and the Bayesian decision, follow it.  False when "
            "the definition does not set it."
        ),
    )
    analysis: Literal["proportion", "mean"] = Field(
        "proportion",
        description=(
            "How the variants are compared.  'mean' for a revenue, count, "
            "duration or custom metric: the mean per assigned user (see "
            "VariantResult.mean), compared with Welch's t-test.  'proportion' "
            "for a conversion metric: the share of users who converted, "
            "compared with Fisher's exact test."
        ),
    )
    variants: List[VariantResult] = Field(
        ...,
        description="Per-variant statistical results, one entry per variant.",
    )
    has_significant_result: bool = Field(
        ...,
        description=(
            "True when at least one non-control variant has is_significant=True "
            "for this metric."
        ),
    )
    winning_variant_id: Optional[UUID] = Field(
        None,
        description=(
            "ID of the variant with the best significant result in the "
            "metric's direction (the lowest when lower_is_better is true, "
            "else the highest), or None if no variant is significantly better "
            "than the control."
        ),
    )

    @property
    def control_variant(self) -> Optional[VariantResult]:
        """Return the control VariantResult, or None if not present."""
        for variant in self.variants:
            if variant.is_control:
                return variant
        return None


# ---------------------------------------------------------------------------
# ExperimentSummary
# ---------------------------------------------------------------------------


class ExperimentSummary(BaseModel):
    """High-level summary of an experiment's overall performance.

    Provides aggregate counts and the engine's recommendation, allowing
    consumers to present a quick decision overview without inspecting
    individual metric results.
    """

    model_config = ConfigDict(from_attributes=True)

    total_users: int = Field(
        ...,
        description="Total unique users enrolled across all variants.",
    )
    total_events: int = Field(
        ...,
        description="Total metric-relevant events recorded across all variants.",
    )
    total_conversions: Optional[int] = Field(
        None,
        description=(
            "Number of assigned users, across all variants, with at least one "
            "conversion event on any metric; each user counts once.  None "
            "when the experiment has no conversion metric."
        ),
    )
    duration_days: Optional[int] = Field(
        None,
        description=(
            "Number of calendar days the experiment has been (or was) running.  "
            "None when start_date is not yet known."
        ),
    )
    has_winner: bool = Field(
        ...,
        description=(
            "True when at least one variant achieved significance on the primary "
            "metric."
        ),
    )
    winning_variant_id: Optional[UUID] = Field(
        None,
        description=(
            "ID of the winning variant on the primary metric, or None if no "
            "winner was found."
        ),
    )
    recommendation: RecommendationAction = Field(
        ...,
        description="The engine's recommended action for this experiment.",
    )
    recommendation_reason: str = Field(
        ...,
        description=("Human-readable explanation of why this recommendation was made."),
    )


# ---------------------------------------------------------------------------
# ExperimentResultsResponse
# ---------------------------------------------------------------------------


class ExperimentResultsResponse(BaseModel):
    """Top-level response schema for the experiment results endpoint.

    Returned by ``GET /api/v1/experiments/{experiment_id}/results``.
    Contains experiment metadata, analysis configuration, a high-level
    summary, and the full per-metric breakdown.
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: UUID = Field(
        ...,
        description="Unique identifier of the experiment.",
    )
    experiment_name: str = Field(
        ...,
        description="Human-readable name of the experiment.",
    )
    status: str = Field(
        ...,
        description=(
            "Current lifecycle status of the experiment: 'draft', 'active', "
            "'paused', 'completed', or 'archived'."
        ),
    )
    start_date: Optional[datetime] = Field(
        None,
        description="UTC timestamp when the experiment started, or None if not yet started.",
    )
    end_date: Optional[datetime] = Field(
        None,
        description="UTC timestamp when the experiment ended, or None if still running.",
    )
    confidence_level: Annotated[float, Field(ge=0.80, le=0.99)] = Field(
        default=0.95,
        description=(
            "Statistical confidence level used for significance testing and "
            "confidence interval construction.  Must be in [0.80, 0.99]."
        ),
    )
    correction_method: CorrectionMethod = Field(
        default=CorrectionMethod.NONE,
        description=(
            "Multiple-comparison correction method applied when there are "
            "multiple metrics or variants."
        ),
    )
    sample_size_adequate: bool = Field(
        ...,
        description=(
            "True when every variant has reached the minimum required sample "
            "size for the configured power and effect size targets."
        ),
    )
    computed_at: datetime = Field(
        ...,
        description="UTC timestamp at which these results were computed.",
    )
    summary: ExperimentSummary = Field(
        ...,
        description="High-level experiment summary and recommendation.",
    )
    metrics: List[MetricResult] = Field(
        ...,
        description="Full per-metric statistical results.",
    )

    # EP-021: Sequential testing (backward compatible — null for non-sequential)
    sequential_testing: Optional[SequentialTestingResponse] = Field(
        None,
        description=(
            "Sequential testing analysis data (mSPRT, confidence sequences, "
            "evidence trajectory). Null when sequential testing is not enabled."
        ),
    )

    # Issue #28: Dimensional breakdown (backward compatible — null when not requested)
    breakdown: Optional[DimensionalBreakdownResponse] = Field(
        None,
        description=(
            "Dimensional breakdown of results by user segment. "
            "Populated only when the ?breakdown=<dimension> query parameter is supplied."
        ),
    )

    # EP-035 Batch 2: Bayesian results (backward compatible — null when not enabled)
    bayesian_results: Optional[BayesianResultsResponse] = Field(
        None,
        description=(
            "Bayesian inference results including posterior distributions, "
            "probability to be best, expected loss, and stopping decision. "
            "Null when bayesian_enabled=False on the experiment."
        ),
    )

    # P0 statistical credibility: sample-ratio mismatch (null when undefined)
    srm: Optional[SRMResult] = Field(
        None,
        description=(
            "Sample-ratio-mismatch chi-square test of assignment counts against "
            "the variants' traffic allocation. Null when the experiment has fewer "
            "than two allocated variants, has no assignments yet, or allocates "
            "traffic adaptively (optimization_type other than 'fixed'), where the "
            "bandit's own weights — not traffic_allocation — decide the split."
        ),
    )

    @field_validator("confidence_level", mode="before")
    @classmethod
    def validate_confidence_level(cls, v: float) -> float:
        """Validate that confidence_level is in the range [0.80, 0.99]."""
        if not (0.80 <= v <= 0.99):
            raise ValueError(
                f"confidence_level must be between 0.80 and 0.99 inclusive, got {v!r}."
            )
        return v


# ---------------------------------------------------------------------------
# DailyDataPoint
# ---------------------------------------------------------------------------


class DailyDataPoint(BaseModel):
    """A single day's worth of metric observations for one variant.

    Used both for raw daily snapshots (``VariantTimeSeries.values``) and
    for running cumulative totals (``VariantTimeSeries.cumulative``).
    """

    model_config = ConfigDict(from_attributes=True)

    date: str = Field(
        ...,
        description=(
            "Calendar date of this data point in ISO 8601 format: 'YYYY-MM-DD'."
        ),
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    sample_size: int = Field(
        ...,
        description="Number of users included in this data point.",
    )
    conversions: Optional[int] = Field(
        None,
        description=(
            "Number of users whose first conversion falls in this data point "
            "(daily), or users converted so far (cumulative).  None for "
            "non-conversion metric types."
        ),
    )
    mean: float = Field(
        ...,
        description=(
            "For conversion metrics: conversion rate for this data point.  "
            "For continuous metrics: arithmetic mean of observed values."
        ),
    )


# ---------------------------------------------------------------------------
# VariantTimeSeries
# ---------------------------------------------------------------------------


class VariantTimeSeries(BaseModel):
    """Time-series data for a single variant on a single metric.

    Provides both a daily view (``values``) and a running cumulative view
    (``cumulative``) so that consumers can render both chart types without
    additional computation.
    """

    model_config = ConfigDict(from_attributes=True)

    variant_id: UUID = Field(..., description="Unique identifier of the variant.")
    variant_name: str = Field(..., description="Human-readable name of the variant.")
    is_control: bool = Field(
        ...,
        description="True when this is the control (baseline) variant.",
    )
    values: List[DailyDataPoint] = Field(
        ...,
        description=(
            "Ordered list of daily metric observations, one entry per calendar "
            "day the experiment was active."
        ),
    )
    cumulative: List[DailyDataPoint] = Field(
        ...,
        description=(
            "Ordered list of cumulative metric totals, one entry per calendar "
            "day.  Each entry accumulates all observations from experiment start "
            "through that day."
        ),
    )


# ---------------------------------------------------------------------------
# DailyResultsResponse
# ---------------------------------------------------------------------------


class DailyResultsResponse(BaseModel):
    """Response schema for the experiment time-series endpoint.

    Returned by
    ``GET /api/v1/experiments/{experiment_id}/results/daily``.

    When ``metric_id`` is supplied, ``series`` contains data for that
    single metric; when omitted, the endpoint returns data for the primary
    metric (behaviour defined by the endpoint implementation).
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: UUID = Field(
        ...,
        description="Unique identifier of the experiment.",
    )
    metric_id: Optional[UUID] = Field(
        None,
        description=(
            "Metric for which time-series data is returned.  None when the "
            "response covers the primary metric and the client did not filter "
            "by metric."
        ),
    )
    series: List[VariantTimeSeries] = Field(
        ...,
        description="Per-variant time-series data, one entry per variant.",
    )


# ---------------------------------------------------------------------------
# SampleSizeResult
# ---------------------------------------------------------------------------


#: Why no required sample size could be computed (``unavailable_reason``).
SampleSizeUnavailableReason = Literal[
    "no_metric",
    "no_control_data",
    "no_control_conversions",
    "rate_at_boundary",
    "effect_out_of_range",
    "effect_too_small",
]

#: Why a fixed sample size is only a guide for this experiment.
SampleSizeGuideOnlyReason = Literal[
    "adaptive_allocation",
    "unequal_allocation",
    "sequential_testing",
    "bayesian",
]


class SampleSizeResult(BaseModel):
    """The planned sample size for an experiment, and how far it has got.

    Plans a two-sided two-proportion test on the primary metric: the users
    each variant needs to detect a relative lift of ``mde`` over the baseline
    rate.  The MDE is the request's ``mde`` or, without one, the effect saved
    with the experiment (the primary metric's ``expected_effect``), or 0.05
    when none was saved.  The baseline is the request's
    ``baseline_conversion_rate`` or, without one, the rate observed in the
    control variant so far.  Every input used is returned with where it came
    from.

    When there is nothing to plan from yet (no metric, no control users, no
    control conversions) the answer is still 200: the required size and the
    achieved power are ``null`` and ``unavailable_reason`` says why.
    """

    model_config = ConfigDict(from_attributes=True)

    required_sample_size_per_variant: Optional[Annotated[int, Field(ge=1)]] = Field(
        ...,
        description=(
            "Users each variant needs to reach the target power at this MDE and "
            "confidence level. null when it cannot be computed; "
            "unavailable_reason says why."
        ),
    )
    current_sample_size_per_variant: int = Field(
        ...,
        description=(
            "Users assigned so far to the smallest variant (the conservative "
            "reference: every variant has at least this many)."
        ),
    )
    is_adequate: bool = Field(
        ...,
        description=(
            "True when current_sample_size_per_variant >= "
            "required_sample_size_per_variant. False when the required size is "
            "null."
        ),
    )
    achieved_power: Optional[float] = Field(
        ...,
        description=(
            "Power (1 - beta) the smallest variant has reached to detect the "
            "planned MDE (not the observed effect). 0.0 with no users; null when "
            "the required size is null."
        ),
    )
    days_to_significance: Optional[int] = Field(
        None,
        description="Not estimated yet: always null.",
    )
    projected_completion_date: Optional[datetime] = Field(
        None,
        description="Not estimated yet: always null.",
    )
    baseline_rate: Optional[float] = Field(
        ...,
        description=(
            "Baseline conversion rate the plan starts from: the request's "
            "baseline_conversion_rate, or the control variant's observed rate. "
            "null when neither is available."
        ),
    )
    mde: float = Field(
        ...,
        description=(
            "Minimum detectable effect planned for, relative to the baseline "
            "rate: 0.05 means 12% -> 12.6%. mde_source says where it came from."
        ),
    )
    mde_source: Literal["request", "experiment", "default"] = Field(
        "default",
        description=(
            "Where mde came from: 'request' (mde was sent), 'experiment' (the "
            "primary metric's expected_effect, saved with the experiment and "
            "above 0 and below 1) or 'default' (0.05: no usable effect was "
            "saved)."
        ),
    )
    confidence_level: float = Field(
        ...,
        description="Confidence level of the test being planned, in [0, 1].",
    )
    power_target: float = Field(
        ...,
        description="Target power (1 - beta) the required size is computed for.",
    )
    baseline_source: Optional[Literal["observed", "request"]] = Field(
        None,
        description=(
            "Where baseline_rate came from: 'request' (baseline_conversion_rate "
            "was sent) or 'observed' (the control variant's rate so far). null "
            "when there is no baseline."
        ),
    )
    baseline_users: Optional[int] = Field(
        None,
        description=(
            "Control users behind an observed baseline rate. null unless "
            "baseline_source is 'observed'."
        ),
    )
    metric_id: Optional[UUID] = Field(
        None, description="The primary metric planned for. null when there is none."
    )
    metric_name: Optional[str] = Field(None, description="Name of that metric.")
    metric_type: Optional[str] = Field(
        None, description="The metric's configured type, e.g. 'conversion'."
    )
    analysed_as: Literal["conversion"] = Field(
        "conversion",
        description=(
            "The test planned for: always 'conversion', whatever the metric's "
            "type. For a revenue metric, which the results analyse as a mean "
            "per user, the plan is for the share of users who send its event."
        ),
    )
    alpha: float = Field(
        ...,
        description=(
            "Significance level of each comparison: 1 - confidence_level, "
            "divided by comparisons when a correction is requested."
        ),
    )
    comparisons: int = Field(
        ...,
        ge=1,
        description="Treatment variants compared with the control (variants - 1, at least 1).",
    )
    correction_method: Literal["none", "bonferroni", "benjamini_hochberg"] = Field(
        "none",
        description=(
            "Correction for several comparisons. 'bonferroni' and "
            "'benjamini_hochberg' both plan at alpha / comparisons (Bonferroni is "
            "an upper bound for Benjamini-Hochberg)."
        ),
    )
    mde_absolute: Optional[float] = Field(
        None,
        description="baseline_rate * mde, in rate units. null when there is no baseline.",
    )
    unavailable_reason: Optional[SampleSizeUnavailableReason] = Field(
        None,
        description=(
            "Why required_sample_size_per_variant is null: no_metric, "
            "no_control_data (no control variant or no control users yet), "
            "no_control_conversions, rate_at_boundary (every control user "
            "converted), effect_out_of_range (the observed rate raised by the "
            "MDE reaches 100%), effect_too_small (the observed rate raised by "
            "the MDE changes too little for the size to be a finite number). "
            "null when a size was computed."
        ),
    )
    guide_only_reasons: List[SampleSizeGuideOnlyReason] = Field(
        default_factory=list,
        description=(
            "Why a fixed sample size is only a guide for this experiment: "
            "adaptive_allocation (a bandit moves traffic), unequal_allocation, "
            "sequential_testing, bayesian. Empty when none applies."
        ),
    )

    @field_validator("required_sample_size_per_variant", mode="before")
    @classmethod
    def validate_required_sample_size(cls, v: Optional[int]) -> Optional[int]:
        """Validate that required_sample_size_per_variant is null or at least 1."""
        if v is None:
            return v
        if v < 1:
            raise ValueError(
                f"required_sample_size_per_variant must be >= 1, got {v!r}."
            )
        return v

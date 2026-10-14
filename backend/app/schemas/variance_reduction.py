"""
Variance Reduction schemas — Issue #21.

Pydantic v2 schemas for CUPED variance-reduction configuration and results.
Supports CUPED, CUPED+, and Winsorization methods.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core import analysis_status as analysis_table
from backend.app.core.analysis_status import AnalysisStatusValue
from backend.app.core.stats_engine import ENGINE_VERSION

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VarianceReductionMethod(str, Enum):
    """Method used for variance reduction in experiment analysis."""

    NONE = "none"
    CUPED = "cuped"
    CUPED_PLUS = "cuped_plus"
    WINSORIZATION = "winsorization"


# ---------------------------------------------------------------------------
# Configuration schema
# ---------------------------------------------------------------------------


class VarianceReductionConfig(BaseModel):
    """Configuration for variance reduction on an experiment.

    This config is stored as JSONB in the experiments table and controls
    which variance-reduction method is applied when computing results.

    Attributes:
        method: Which variance-reduction method to use.
        covariate_metric_id: ID of one of the experiment's metrics whose event is
            the history read as the covariate (CUPED and CUPED_PLUS only). Not
            required: without it, each metric's own event before the user's
            assignment is the history.
        covariate_lookback_days: Number of days of pre-experiment data to use
            when building the covariate (1–90, default 7).
        winsorization_percentile: Upper percentile for Winsorization clipping
            (50–100, default 99.0).
    """

    model_config = ConfigDict(from_attributes=True)

    method: VarianceReductionMethod = Field(
        default=VarianceReductionMethod.NONE,
        description="Variance-reduction method to apply.",
    )
    covariate_metric_id: Optional[str] = Field(
        default=None,
        description=(
            "ID of one of the experiment's metrics whose event is the history "
            "read as the CUPED covariate: whether the user sent it in the "
            "covariate_lookback_days before their assignment. Used by 'cuped' "
            "and 'cuped_plus'. Not required: without it, each metric's own "
            "event before the user's assignment is the history."
        ),
    )
    covariate_lookback_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description=(
            "Number of days of pre-experiment history to use when building the "
            "covariate. Must be between 1 and 90."
        ),
    )
    winsorization_percentile: float = Field(
        default=99.0,
        ge=50.0,
        le=100.0,
        description=(
            "Upper percentile threshold for Winsorization (50–100). "
            "Values above this percentile are clipped to the threshold value."
        ),
    )


# ---------------------------------------------------------------------------
# Per-metric CUPED result
# ---------------------------------------------------------------------------


class CupedMetricResult(BaseModel):
    """CUPED-adjusted comparison of one treatment with the control, on one metric.

    The CUPED results endpoint returns one of these per (metric, treatment).
    A comparison that cannot be computed carries ``unavailable_reason`` and
    null numbers; it is listed, never left out.
    """

    model_config = ConfigDict(from_attributes=True)

    metric_id: str = Field(..., description="Unique identifier of the metric.")
    metric_name: str = Field(..., description="Human-readable name of the metric.")
    variant_id: Optional[str] = Field(
        None,
        description=(
            "The treatment compared with the control. Null only when the "
            "experiment has no control or no treatment."
        ),
    )
    variant_name: Optional[str] = Field(None, description="The treatment's name.")
    control_variant_id: Optional[str] = Field(
        None, description="The control variant the treatment is compared with."
    )
    control_sample_size: Optional[int] = Field(
        None, description="Users assigned to the control."
    )
    treatment_sample_size: Optional[int] = Field(
        None, description="Users assigned to this treatment."
    )
    adjusted_control_mean: Optional[float] = Field(
        None, description="The control's conversion rate, adjusted for the covariate."
    )
    adjusted_treatment_mean: Optional[float] = Field(
        None,
        description="The treatment's conversion rate, adjusted for the covariate.",
    )
    adjusted_effect: Optional[float] = Field(
        None,
        description="adjusted_treatment_mean - adjusted_control_mean.",
    )
    adjusted_se: Optional[float] = Field(
        None, description="Standard error of the adjusted effect."
    )
    adjusted_p_value: Optional[float] = Field(
        None,
        description=(
            "Two-sided p-value of a z-test on the adjusted effect, before any "
            "multiple-comparison correction (in /results, adjusted_p_value is "
            "the corrected one; here that is corrected_p_value)."
        ),
    )
    adjusted_ci_lower: Optional[float] = Field(
        None,
        description="Lower bound of the adjusted effect's interval, at confidence_level.",
    )
    adjusted_ci_upper: Optional[float] = Field(
        None,
        description="Upper bound of the adjusted effect's interval, at confidence_level.",
    )
    unadjusted_effect: Optional[float] = Field(
        None,
        description="The treatment's conversion rate minus the control's, unadjusted.",
    )
    unadjusted_se: Optional[float] = Field(
        None, description="Standard error of the unadjusted effect."
    )
    corrected_p_value: Optional[float] = Field(
        None,
        description=(
            "adjusted_p_value after the experiment's correction_method, applied "
            "across this metric's treatments. Null for the method 'none'."
        ),
    )
    is_significant: bool = Field(
        False,
        description=(
            "corrected_p_value (adjusted_p_value when there is none) is below "
            "1 - confidence_level."
        ),
    )
    variance_reduction_pct: Optional[float] = Field(
        None,
        description=(
            "100 * (1 - variance of the adjusted effect / variance of the "
            "unadjusted effect). Can be below 0: the slope theta is pooled over "
            "every arm, and an arm whose own relation differs can end up noisier."
        ),
    )
    theta: Optional[float] = Field(
        None,
        description=(
            "The slope of the outcome on the covariate, pooled within arms; 0 when "
            "no user has covariate events (or every user has), and for 'none'."
        ),
    )
    covariate_event_name: Optional[str] = Field(
        None,
        description=(
            "The event the covariate counts: the metric's own event_name, or that "
            "of covariate_metric_id. Null when no covariate is read."
        ),
    )
    covariate_coverage_pct: Optional[float] = Field(
        None,
        description=(
            "Share of this comparison's users (control and this treatment), 0-100, "
            "with at least one covariate event in their window before assignment."
        ),
    )
    unavailable_reason: Optional[str] = Field(
        None,
        description=(
            "Null, or why this comparison was not computed: not_a_proportion_metric, "
            "winsorization_needs_mean_metric, fewer_than_2_units, no_variation, "
            "no_residual_variation, "
            "no_control_variant, no_treatment_variant, covariate_metric_not_found, "
            "metric_has_no_event_name or result_invalid."
        ),
    )
    method: VarianceReductionMethod = Field(
        ...,
        description=(
            "The method actually computed: 'cuped' when 'cuped_plus' is configured."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level CUPED results response
# ---------------------------------------------------------------------------


class CupedResultsResponse(BaseModel):
    """Response schema for the CUPED variance-reduced results endpoint.

    Returned by ``GET /api/v1/results/{experiment_id}/cuped``.
    Contains per-metric CUPED-adjusted results for an experiment.
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: str = Field(..., description="Unique identifier of the experiment.")
    method: VarianceReductionMethod = Field(
        ..., description="The experiment's configured variance-reduction method."
    )
    confidence_level: Optional[float] = Field(
        None,
        description="The experiment's stored confidence level, used for every interval.",
    )
    correction_method: Optional[str] = Field(
        None,
        description=(
            "The experiment's stored multiple-comparison correction, behind "
            "corrected_p_value."
        ),
    )
    covariate_lookback_days: Optional[int] = Field(
        None,
        description="Days before each user's assignment the covariate reads.",
    )
    metrics: List[CupedMetricResult] = Field(
        ...,
        description="One comparison per metric and treatment, against the control.",
    )
    computed_at: str = Field(
        ..., description="ISO 8601 UTC timestamp of when the results were computed."
    )
    # CUPED is closed-form (OLS theta); there is no Monte Carlo draw, so the
    # seed and sample count are always null.  They are kept on the schema so
    # every analysis response shares the same provenance block.
    seed: Optional[int] = Field(
        None,
        description="Always null: CUPED is closed-form and draws no random samples.",
    )
    n_samples: Optional[int] = Field(
        None,
        description="Always null: CUPED is closed-form and draws no random samples.",
    )
    engine_version: str = Field(
        ENGINE_VERSION,
        description="Statistics engine version that produced these results.",
    )
    analysis_status: AnalysisStatusValue = Field(
        default_factory=lambda: analysis_table.analysis_status("cuped"),
        description=(
            "'ga' when these numbers are what their names say; 'beta' when part "
            "of the analysis is not computed as described yet (see "
            "analysis_notice).  About the numbers, not the response shape."
        ),
    )
    analysis_notice: Optional[str] = Field(
        default_factory=lambda: analysis_table.analysis_notice("cuped"),
        description=(
            "Present exactly when analysis_status is 'beta': what is not "
            "computed yet, with the issue that tracks it."
        ),
    )

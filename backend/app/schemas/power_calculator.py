"""
Pydantic v2 schemas for the Power Calculator endpoints (EP-056).

Covers:
- SampleSizeRequest / SampleSizeResponse
- MDERequest / MDEResponse
- RuntimeRequest / RuntimeResponse
- PowerCurveRequest / PowerCurveResponse
- PlanRequest / PlanResponse (planning advice)
"""

from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums / literals
# ---------------------------------------------------------------------------

VALID_METRIC_TYPES = ("proportion", "mean", "ratio")

# Upper bounds on the integer inputs of the sample-size, MDE and runtime
# requests (#1003). The calculations convert these integers to floats, and an
# integer above about 1.8e308 cannot be converted (OverflowError), so before
# these bounds such a value answered 500. Both bounds sit far above anything a
# real experiment sends:
#
# * MAX_VARIANTS: an experiment's variants take whole percentages of its
#   traffic that sum to 100, so at most 100 of them receive any; the
#   dashboard's power calculator offers 2 to 5.
# * MAX_COUNT: a sample size or a daily traffic of 10**12 is more than a
#   hundred times the world's population. The documented examples use 31,234
#   users per variant and 10,000 a day. Every integer up to it is exact as a
#   float (it is below 2**53).
MAX_VARIANTS = 100
MAX_COUNT = 10**12


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class SampleSizeRequest(BaseModel):
    """Request body for POST /power/sample-size."""

    baseline_rate: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        description=(
            "The metric before the change. For metric_type 'proportion' (the "
            "default), a rate above 0 and below 1: 0.05 is 5%. For 'mean' or "
            "'ratio', the baseline mean in the metric's own units, such as "
            "45.0 for an average order value of 45: any finite number above 0."
        ),
    )
    minimum_detectable_effect: float = Field(
        ...,
        gt=0,
        description="Minimum detectable effect as a relative lift (e.g. 0.10 = 10%).",
    )
    alpha: float = Field(
        default=0.05,
        gt=0,
        lt=0.5,
        description="Type I error rate (significance level). Must be in (0, 0.5).",
    )
    power: float = Field(
        default=0.80,
        gt=0,
        lt=1,
        description="Desired statistical power (1 - Type II error rate). Must be in (0, 1).",
    )
    n_variants: int = Field(
        default=2,
        ge=2,
        le=MAX_VARIANTS,
        description="Number of variants including control (minimum 2).",
    )
    two_tailed: bool = Field(
        default=True,
        description="Whether to use a two-tailed test (recommended).",
    )
    metric_type: str = Field(
        default="proportion",
        description="Type of metric: 'proportion', 'mean', or 'ratio'.",
    )
    baseline_std: Optional[float] = Field(
        default=None,
        gt=0,
        description="Standard deviation of the baseline metric (required for metric_type='mean' or 'ratio').",
    )
    daily_traffic: Optional[int] = Field(
        default=None,
        gt=0,
        le=MAX_COUNT,
        description="Daily users exposed to the experiment (used to compute runtime).",
    )
    traffic_allocation: float = Field(
        default=1.0,
        gt=0,
        le=1.0,
        description="Fraction of total traffic assigned to the experiment (0–1].",
    )

    @field_validator("metric_type")
    @classmethod
    def validate_metric_type(cls, v: str) -> str:
        if v not in VALID_METRIC_TYPES:
            raise ValueError(f"metric_type must be one of {VALID_METRIC_TYPES}")
        return v

    @field_validator("minimum_detectable_effect")
    @classmethod
    def validate_mde(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("minimum_detectable_effect must be > 0")
        if v >= 1.0:
            raise ValueError(
                "minimum_detectable_effect must be < 1.0 (a relative lift >= 100% is not supported)"
            )
        return v

    @model_validator(mode="after")
    def validate_mean_requires_std(self) -> "SampleSizeRequest":
        if self.metric_type in ("mean", "ratio") and self.baseline_std is None:
            raise ValueError(
                f"baseline_std is required when metric_type='{self.metric_type}'"
            )
        return self

    @model_validator(mode="after")
    def validate_proportion_bounds(self) -> "SampleSizeRequest":
        """A proportion is a rate: it, and the rate it is raised to, stay below 1.

        Only a ``proportion`` is bounded. A ``mean`` or ``ratio`` baseline is in
        the metric's own units (#1034), so any finite value above 0 is planned.
        """
        if self.metric_type != "proportion":
            return self
        if self.baseline_rate >= 1.0:
            raise ValueError(
                f"baseline_rate is {self.baseline_rate}. With metric_type "
                "'proportion' (the default) it is a rate and must be below 1: "
                "0.05 means 5%. For a mean, such as an average order value, "
                "send metric_type 'mean' and baseline_std."
            )
        p2 = self.baseline_rate + self.baseline_rate * self.minimum_detectable_effect
        if p2 >= 1.0:
            raise ValueError(
                f"baseline_rate ({self.baseline_rate}) raised by "
                f"minimum_detectable_effect ({self.minimum_detectable_effect}) "
                f"is {p2:.4f}, and a proportion must stay below 1. Lower "
                "baseline_rate or minimum_detectable_effect. For a mean, such "
                "as an average order value, send metric_type 'mean' and "
                "baseline_std."
            )
        return self


class MDERequest(BaseModel):
    """Request body for POST /power/mde."""

    sample_size_per_variant: int = Field(
        ...,
        gt=0,
        le=MAX_COUNT,
        description="Fixed sample size per variant.",
    )
    baseline_rate: float = Field(
        ...,
        gt=0,
        lt=1,
        description="Current baseline conversion/success rate (exclusive 0–1).",
    )
    alpha: float = Field(
        default=0.05,
        gt=0,
        lt=0.5,
        description="Type I error rate. Must be in (0, 0.5).",
    )
    power: float = Field(
        default=0.80,
        gt=0,
        lt=1,
        description="Desired statistical power. Must be in (0, 1).",
    )
    n_variants: int = Field(
        default=2,
        ge=2,
        le=MAX_VARIANTS,
        description="Number of variants including control.",
    )
    two_tailed: bool = Field(
        default=True,
        description="Whether to use a two-tailed test.",
    )


class RuntimeRequest(BaseModel):
    """Request body for POST /power/runtime."""

    required_sample_size: int = Field(
        ...,
        gt=0,
        le=MAX_COUNT,
        description="Required sample size per variant.",
    )
    daily_traffic: int = Field(
        ...,
        gt=0,
        le=MAX_COUNT,
        description="Total daily users exposed to the experiment.",
    )
    traffic_allocation: float = Field(
        ...,
        gt=0,
        le=1.0,
        description="Fraction of traffic in the experiment (0–1].",
    )
    n_variants: int = Field(
        default=2,
        ge=2,
        le=MAX_VARIANTS,
        description="Number of variants including control.",
    )


class PowerCurveRequest(BaseModel):
    """Query parameters for GET /power/curve."""

    baseline_rate: float = Field(
        ...,
        gt=0,
        lt=1,
        description="Current baseline conversion rate.",
    )
    alpha: float = Field(
        default=0.05,
        gt=0,
        lt=0.5,
        description="Type I error rate.",
    )
    power_target: float = Field(
        default=0.80,
        gt=0,
        lt=1,
        description="Target statistical power.",
    )
    mde_target: Optional[float] = Field(
        default=None,
        description="The currently selected MDE (marks a point on the curve).",
    )


class PlanRequest(BaseModel):
    """Request body for POST /power/plan (planning advice)."""

    experiment_name: str = Field(
        ...,
        min_length=3,
        max_length=200,
        description="Name of the experiment being planned.",
    )
    metric_description: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Plain-English description of the primary metric.",
    )
    baseline_rate: float = Field(
        ...,
        gt=0,
        lt=1,
        description="Baseline metric rate (0–1).",
    )
    mde: float = Field(
        ...,
        gt=0,
        lt=1,
        description="Minimum detectable effect (relative, e.g. 0.10 = 10%).",
    )
    runtime_days: float = Field(
        ...,
        gt=0,
        description="Estimated runtime in days.",
    )
    business_context: str = Field(
        default="",
        max_length=1000,
        description="Optional business context for the experiment.",
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class SampleSizeResponse(BaseModel):
    """Response for POST /power/sample-size."""

    model_config = ConfigDict(from_attributes=True)

    per_variant: int = Field(description="Sample size needed per variant.")
    total: int = Field(description="Total sample size across all variants.")
    alpha: float
    power: float
    baseline_rate: float
    mde_absolute: float = Field(description="Minimum detectable absolute effect size.")
    mde_relative: float = Field(
        description="Minimum detectable relative lift (e.g. 0.10 = 10%)."
    )
    confidence_level: float = Field(
        description="Statistical confidence level (1 - alpha)."
    )
    runtime_days: Optional[float] = Field(
        default=None,
        description="Estimated days to reach significance (None if daily_traffic not provided).",
    )
    n_variants: int
    two_tailed: bool
    metric_type: str


class MDEResponse(BaseModel):
    """Response for POST /power/mde."""

    model_config = ConfigDict(from_attributes=True)

    mde_absolute: float = Field(description="Smallest detectable absolute effect.")
    mde_relative: float = Field(description="Smallest detectable relative lift.")
    per_variant_sample: int
    total_sample: int
    alpha: float
    power: float
    n_variants: int
    two_tailed: bool


class RuntimeResponse(BaseModel):
    """Response for POST /power/runtime."""

    model_config = ConfigDict(from_attributes=True)

    days_to_significance: float
    weeks_to_significance: float
    daily_traffic_per_variant: int
    confidence_interval_days: Tuple[float, float] = Field(
        description="90% CI accounting for traffic variance (lower, upper).",
    )


class PowerCurvePoint(BaseModel):
    """A single point on the power curve."""

    model_config = ConfigDict(from_attributes=True)

    effect_size_relative: float = Field(
        description="Relative effect size (e.g. 0.05 = 5% lift)."
    )
    sample_size_per_variant: int
    is_current_target: bool = Field(
        description="True for the point closest to the selected MDE.",
    )


class PowerCurveResponse(BaseModel):
    """Response for GET /power/curve."""

    model_config = ConfigDict(from_attributes=True)

    points: List[PowerCurvePoint]
    baseline_rate: float
    alpha: float
    power_target: float


class PlanResponse(BaseModel):
    """Response for POST /power/plan (the built-in planning advice)."""

    model_config = ConfigDict(from_attributes=True)

    advice: str = Field(description="Plain-English planning advice.")
    generated_by: str = Field(description="Always 'template'.")
    experiment_name: str
    baseline_rate: float
    mde: float
    runtime_days: float

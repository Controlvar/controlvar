"""
Pydantic v2 schemas for dimensional analysis / segment breakdown — Issue #28.

Used by GET /api/v1/results/{experiment_id}?breakdown=<dimension>.
"""

from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


class SegmentVariantResult(BaseModel):
    """Per-variant statistics within a single segment."""

    model_config = ConfigDict(from_attributes=True)

    variant_id: str = Field(..., description="Unique identifier of the variant.")
    variant_name: str = Field(..., description="Human-readable name of the variant.")
    is_control: bool = Field(..., description="True when this is the control variant.")
    sample_size: int = Field(
        ...,
        ge=0,
        description="Number of users in this variant within the segment.",
    )
    conversions: Optional[int] = Field(
        None,
        description=(
            "Number of users in this variant within the segment with at least "
            "one conversion event; a user who converts several times counts once."
        ),
    )
    mean: float = Field(
        ...,
        description=(
            "Observed conversion rate in [0, 1] for conversion metrics, "
            "or arithmetic mean for continuous metrics."
        ),
    )
    confidence_interval: Optional[Tuple[float, float]] = Field(
        None,
        description="Two-sided confidence interval as (lower, upper).",
    )
    p_value: Optional[float] = Field(
        None,
        description=(
            "Two-sided p-value of Fisher's exact test against the control, the "
            "test the headline uses for a conversion rate. None for the control "
            "variant itself."
        ),
    )
    is_significant: bool = Field(
        ...,
        description=(
            "True when p_value < adjusted_alpha (Bonferroni-corrected significance "
            "threshold for this breakdown)."
        ),
    )


class SegmentBreakdown(BaseModel):
    """Aggregated statistics for one segment value (e.g. 'ios', 'US', 'premium')."""

    model_config = ConfigDict(from_attributes=True)

    segment_value: str = Field(
        ...,
        description="The value of the breakdown dimension for this segment.",
    )
    sample_size: int = Field(
        ...,
        ge=0,
        description="Total number of users across all variants in this segment.",
    )
    variants: List[SegmentVariantResult] = Field(
        ...,
        description="Per-variant statistics for this segment.",
    )


class DimensionalBreakdownResponse(BaseModel):
    """
    Top-level response for a dimensional breakdown of experiment results.

    Returned as the ``breakdown`` field on ExperimentResultsResponse when the
    ``breakdown`` query parameter is supplied.
    """

    model_config = ConfigDict(from_attributes=True)

    dimension: str = Field(
        ...,
        description=(
            "The dimension used for segmentation (e.g. 'platform', 'country', "
            "'user_tier')."
        ),
    )
    is_exploratory: bool = Field(
        default=True,
        description=(
            "Always True.  Dimensional breakdowns are exploratory analyses and "
            "should not be used as primary decision criteria."
        ),
    )
    adjusted_alpha: float = Field(
        ...,
        gt=0.0,
        le=1.0,
        description=(
            "Bonferroni-corrected significance threshold: base_alpha / num_segments. "
            "Used when evaluating is_significant within each segment."
        ),
    )
    has_heterogeneous_effects: bool = Field(
        ...,
        description=(
            "True when a chi-squared test detects significant heterogeneity "
            "in treatment effects across segments."
        ),
    )
    hte_warning: Optional[str] = Field(
        None,
        description=(
            "Human-readable warning message displayed when has_heterogeneous_effects "
            "is True.  None when no HTE is detected."
        ),
    )
    segments: List[SegmentBreakdown] = Field(
        ...,
        description=(
            "Per-segment results: the 20 values with the most users, then "
            "`other` and `unknown`, each listed only when it has users."
        ),
    )

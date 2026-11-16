"""
Post-Stratification & FDR Correction schemas — EP-043.

Pydantic v2 schemas for:
- PostStratificationRequest / PostStratResult (response)
- FDRCorrectionRequest / FDRResult (response)
"""

from typing import Dict, List, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.schemas.finite_number import FiniteFloat

# ---------------------------------------------------------------------------
# Post-Stratification schemas
# ---------------------------------------------------------------------------


class PostStratificationRequest(BaseModel):
    """Request body for the post-stratification endpoint.

    Attributes:
        stratum_cols: Column names in the experiment data that define strata
            (e.g. ["country", "device_type"]). Must be non-empty.
        metric_col: Name of the numeric outcome column in the data.
            Defaults to "metric_value".
        alpha: Significance level for confidence interval construction (0–1).
            Defaults to 0.05.
    """

    model_config = ConfigDict(from_attributes=True)

    stratum_cols: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "Column name(s) that define strata "
            "(e.g. ['country', 'device_type']). Must be non-empty."
        ),
    )
    metric_col: str = Field(
        default="metric_value",
        description="Name of the numeric outcome column in the experiment data.",
    )
    alpha: FiniteFloat = Field(
        default=0.05,
        gt=0.0,
        lt=1.0,
        description="Significance level for confidence interval construction (exclusive: 0–1).",
    )

    @field_validator("stratum_cols")
    @classmethod
    def stratum_cols_non_empty(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("stratum_cols must contain at least one column name")
        for col in v:
            if not col or not col.strip():
                raise ValueError("stratum_cols entries must be non-empty strings")
        return v


class PostStratResultResponse(BaseModel):
    """Response schema for a post-stratification analysis result.

    Returned by ``POST /api/v1/results/{experiment_id}/post-stratification``.
    """

    model_config = ConfigDict(from_attributes=True)

    metric_name: str = Field(..., description="Name of the metric analysed.")
    control_mean: float = Field(
        ..., description="Post-stratification-weighted control mean."
    )
    treatment_mean: float = Field(
        ..., description="Post-stratification-weighted treatment mean."
    )
    effect_size: float = Field(
        ..., description="Absolute treatment effect: treatment_mean - control_mean."
    )
    effect_size_relative: float = Field(
        ..., description="Relative lift: effect_size / |control_mean|."
    )
    variance_reduction: float = Field(
        ...,
        description=(
            "Percentage variance reduction vs. the naive (unstratified) estimator. "
            "Positive means reduction; negative means the stratification inflates variance."
        ),
    )
    adjusted_se: float = Field(
        ..., description="Standard error of the post-stratified effect estimate."
    )
    p_value: float = Field(..., description="Two-tailed p-value from a z-test.")
    confidence_interval: Tuple[float, float] = Field(
        ..., description="(lower, upper) confidence interval at the requested alpha."
    )
    n_strata: int = Field(..., description="Number of unique strata used.")
    strata_sizes: Dict[str, int] = Field(
        ..., description="Mapping of stratum label → total count across both groups."
    )


# ---------------------------------------------------------------------------
# FDR Correction schemas
# ---------------------------------------------------------------------------


class FDRCorrectionRequest(BaseModel):
    """Request body for the BH FDR correction endpoint.

    Attributes:
        p_values: Mapping of metric_name → raw p-value. All values must be
            in [0, 1]. Must be non-empty.
        fdr_threshold: Desired FDR control level (α). Defaults to 0.05.
    """

    model_config = ConfigDict(from_attributes=True)

    p_values: Dict[str, FiniteFloat] = Field(
        ...,
        min_length=1,
        description=(
            "Mapping of metric_name → raw p-value. "
            "All values must be in [0, 1]. Must be non-empty."
        ),
    )
    fdr_threshold: FiniteFloat = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="FDR control level α (0–1, inclusive). Default 0.05.",
    )

    @field_validator("p_values")
    @classmethod
    def p_values_in_valid_range(cls, v: Dict[str, float]) -> Dict[str, float]:
        if not v:
            raise ValueError("p_values must be non-empty")
        for name, p in v.items():
            if p < 0.0 or p > 1.0:
                raise ValueError(
                    f"p-value for '{name}' is {p}, which is outside [0, 1]"
                )
        return v


class FDRResultResponse(BaseModel):
    """Response schema for a single metric after BH FDR correction.

    Returned (in a list) by ``POST /api/v1/results/{experiment_id}/fdr-correction``.
    """

    model_config = ConfigDict(from_attributes=True)

    metric_name: str = Field(..., description="Name of the metric / hypothesis tested.")
    raw_p_value: float = Field(..., description="Original uncorrected p-value.")
    adjusted_p_value: float = Field(
        ...,
        description=(
            "BH-adjusted p-value. Monotonically non-decreasing when items are sorted "
            "by rank ascending."
        ),
    )
    rank: int = Field(
        ...,
        description="Rank of this metric when p-values are sorted ascending (1 = smallest).",
    )
    is_significant: bool = Field(
        ..., description="True if this metric is rejected after BH correction."
    )

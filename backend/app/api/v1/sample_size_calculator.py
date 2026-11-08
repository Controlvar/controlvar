"""
Sample size calculator endpoint.

This module provides an endpoint for calculating the required sample size
for experiments based on expected effect size, baseline conversion rate,
and desired statistical power.
"""

import math
from typing import Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.models.user import User
from backend.app.services.power_calculator_service import (
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
    SampleSizeNotFiniteError,
    sample_size_two_proportions,
)

# Create router
router = APIRouter(prefix="/utils", route_class=OffLoopRoute)


class SampleSizeRequest(BaseModel):
    """Schema for sample size calculation request."""

    baseline_rate: float = Field(
        ..., ge=0, le=1, description="Baseline conversion rate (0-1)"
    )
    minimum_detectable_effect: float = Field(
        ...,
        gt=0,
        description="Minimum detectable effect (relative change, e.g. 0.1 for 10%)",
    )
    statistical_power: float = Field(
        0.8, ge=0.5, le=0.99, description="Statistical power (0.5-0.99)"
    )
    significance_level: float = Field(
        0.05, ge=0.01, le=0.1, description="Significance level (0.01-0.1)"
    )
    is_one_sided: bool = Field(
        False, description="Whether the test is one-sided (default: two-sided)"
    )

    @field_validator("baseline_rate")
    @classmethod
    def validate_baseline_rate(cls, v):
        """Validate baseline conversion rate."""
        if v <= 0 or v >= 1:
            raise ValueError("Baseline rate must be between 0 and 1")
        return v

    @field_validator("minimum_detectable_effect")
    @classmethod
    def validate_minimum_detectable_effect(cls, v):
        """Validate minimum detectable effect."""
        if v <= 0:
            raise ValueError("Minimum detectable effect must be greater than 0")
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "baseline_rate": 0.1,
                "minimum_detectable_effect": 0.15,
                "statistical_power": 0.8,
                "significance_level": 0.05,
                "is_one_sided": False,
            }
        }
    )


class SampleSizeResponse(BaseModel):
    """Schema for sample size calculation response."""

    baseline_rate: float
    minimum_detectable_effect: float
    statistical_power: float
    significance_level: float
    is_one_sided: bool
    samples_per_variant: int
    total_samples: int
    estimated_duration_days: Optional[Dict[str, int]] = None
    notes: Optional[str] = None


@router.post(
    "/sample-size",
    response_model=SampleSizeResponse,
    summary="Calculate required sample size",
    response_description="Returns the required sample size for an experiment",
)
async def calculate_sample_size(
    request: SampleSizeRequest = Body(
        ..., description="Sample size calculation parameters"
    ),
    daily_traffic: Optional[int] = Query(
        None, gt=0, description="Daily traffic to the experiment (optional)"
    ),
    traffic_allocation: Optional[float] = Query(
        None,
        gt=0,
        le=1,
        description="Fraction of traffic allocated to the experiment (0-1)",
    ),
    variant_count: int = Query(
        2, ge=2, description="Number of variants (including control)"
    ),
    current_user: User = Depends(deps.get_current_active_user),
) -> SampleSizeResponse:
    """
    Calculate the required sample size for an experiment.

    This endpoint calculates the number of samples needed per variant and in total
    to achieve the desired statistical power for detecting the minimum effect size.

    Optionally, if daily traffic and traffic allocation are provided, it also
    estimates how long the experiment will need to run to collect the required sample.

    Returns:
        SampleSizeResponse: Sample size calculation results
    """
    # Extract parameters from request
    baseline_rate = request.baseline_rate
    mde = request.minimum_detectable_effect
    power = request.statistical_power
    alpha = request.significance_level
    is_one_sided = request.is_one_sided

    # The treatment rate has to be a probability. Past 100% the formula takes
    # the square root of a negative variance.
    treatment_rate = baseline_rate + baseline_rate * mde
    if baseline_rate * (1 + mde) >= 1 or treatment_rate >= 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=TREATMENT_RATE_CEILING_MESSAGE,
        )

    # Two arms through the same formula as the results page's Sample Size tab
    # and the guided setup's estimate (pooled variance under H0, unpooled under
    # H1), with no multiple-comparison correction; every variant needs this
    # many users.
    try:
        samples_per_variant = sample_size_two_proportions(
            p1=baseline_rate,
            p2=treatment_rate,
            alpha=alpha,
            power=power,
            two_tailed=not is_one_sided,
        )
    except SampleSizeNotFiniteError:
        # The treatment rate is too close to the baseline for the size to be a
        # finite number (an effect or a baseline near zero).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=SAMPLE_SIZE_NOT_FINITE_MESSAGE,
        )

    # Calculate total sample size
    total_samples = samples_per_variant * variant_count

    # Calculate estimated duration if daily traffic and allocation are provided
    estimated_duration_days = None
    notes = None

    if daily_traffic is not None and traffic_allocation is not None:
        if traffic_allocation <= 0 or traffic_allocation > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Traffic allocation must be between 0 and 1",
            )

        # Calculate daily experiment traffic
        daily_experiment_traffic = daily_traffic * traffic_allocation

        # Calculate daily traffic per variant
        daily_variant_traffic = daily_experiment_traffic / variant_count

        # Calculate days needed
        days_needed = math.ceil(samples_per_variant / daily_variant_traffic)

        # Add estimates for different time periods
        estimated_duration_days = {
            "days": days_needed,
            "weeks": math.ceil(days_needed / 7),
            "months": math.ceil(days_needed / 30),
        }

        # Add notes for very long or very short experiments
        if days_needed < 7:
            notes = "This experiment is very short. Consider implementing proper guardrails to avoid data quality issues."
        elif days_needed > 90:
            notes = "This experiment will take a long time to complete. Consider increasing traffic allocation, reducing the number of variants, or adjusting the minimum detectable effect."

    # Return response
    return SampleSizeResponse(
        baseline_rate=baseline_rate,
        minimum_detectable_effect=mde,
        statistical_power=power,
        significance_level=alpha,
        is_one_sided=is_one_sided,
        samples_per_variant=samples_per_variant,
        total_samples=total_samples,
        estimated_duration_days=estimated_duration_days,
        notes=notes,
    )

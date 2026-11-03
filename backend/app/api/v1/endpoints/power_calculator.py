"""
Pre-Experiment Power Calculator API endpoints (EP-056).

Routes (no authentication required — useful as a pre-login planning tool):
    POST /api/v1/power/sample-size  — compute required sample size
    POST /api/v1/power/mde          — compute MDE for a fixed sample
    POST /api/v1/power/runtime      — estimate experiment runtime
    GET  /api/v1/power/curve        — power curve (effect size vs. sample)
    POST /api/v1/power/plan         — built-in planning advice
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from backend.app.core.logger import unexpected_failure
from backend.app.schemas.power_calculator import (
    MDERequest,
    MDEResponse,
    PlanRequest,
    PlanResponse,
    PowerCurvePoint,
    PowerCurveResponse,
    RuntimeRequest,
    RuntimeResponse,
    SampleSizeRequest,
    SampleSizeResponse,
)
from backend.app.services.ai_experiment_planner_service import (
    AIExperimentPlannerService,
)
from backend.app.services.power_calculator_service import PowerCalculatorService

logger = logging.getLogger(__name__)

router = APIRouter()
_calculator = PowerCalculatorService()
_planner = AIExperimentPlannerService()

# What the calculation routes (sample size, MDE, runtime, curve) answer, with
# a 422, when a number in the calculation leaves floating-point range and
# Python raises OverflowError (#1003), and GET /experiments/analysis/sample-size
# when the days needed cannot be calculated (a traffic share so small that
# users per variant per day come to 0, or the day count is not finite) (#820).
# The request schemas' upper bounds keep
# the integers in range; what is left is the sample size of a ``mean`` or
# ``ratio`` metric with a very large ``baseline_std``, or with a baseline so
# small that the difference squared underflows to zero. Those used to answer
# 500. The curve skips a point it cannot compute, so it has no known case.
OUT_OF_RANGE_MESSAGE = (
    "These values are too large or too small to calculate with. "
    "Use values nearer those of a real experiment."
)


# ---------------------------------------------------------------------------
# POST /sample-size
# ---------------------------------------------------------------------------


@router.post(
    "/sample-size",
    response_model=SampleSizeResponse,
    status_code=status.HTTP_200_OK,
    summary="Compute Required Sample Size",
    description=(
        "Compute the required sample size per variant (and total) for a given "
        "baseline rate and minimum detectable effect. "
        "Optionally estimates experiment runtime when daily_traffic is provided."
    ),
)
def compute_sample_size(body: SampleSizeRequest) -> SampleSizeResponse:
    """Compute the required sample size per variant."""
    try:
        result = _calculator.compute_sample_size(
            baseline_rate=body.baseline_rate,
            minimum_detectable_effect=body.minimum_detectable_effect,
            alpha=body.alpha,
            power=body.power,
            n_variants=body.n_variants,
            two_tailed=body.two_tailed,
            metric_type=body.metric_type,
            baseline_std=body.baseline_std,
            daily_traffic=body.daily_traffic,
            traffic_allocation=body.traffic_allocation,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OverflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=OUT_OF_RANGE_MESSAGE,
        ) from exc

    return SampleSizeResponse(
        per_variant=result.per_variant,
        total=result.total,
        alpha=result.alpha,
        power=result.power,
        baseline_rate=result.baseline_rate,
        mde_absolute=result.mde_absolute,
        mde_relative=result.mde_relative,
        confidence_level=result.confidence_level,
        runtime_days=result.runtime_days,
        n_variants=result.n_variants,
        two_tailed=result.two_tailed,
        metric_type=result.metric_type,
    )


# ---------------------------------------------------------------------------
# POST /mde
# ---------------------------------------------------------------------------


@router.post(
    "/mde",
    response_model=MDEResponse,
    status_code=status.HTTP_200_OK,
    summary="Compute Minimum Detectable Effect",
    description=(
        "Given a fixed sample size per variant, compute the smallest relative "
        "effect that can be detected with the specified power and alpha."
    ),
)
def compute_mde(body: MDERequest) -> MDEResponse:
    """Compute the MDE for a fixed sample size."""
    try:
        result = _calculator.compute_mde(
            sample_size_per_variant=body.sample_size_per_variant,
            baseline_rate=body.baseline_rate,
            alpha=body.alpha,
            power=body.power,
            n_variants=body.n_variants,
            two_tailed=body.two_tailed,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OverflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=OUT_OF_RANGE_MESSAGE,
        ) from exc

    return MDEResponse(
        mde_absolute=result.mde_absolute,
        mde_relative=result.mde_relative,
        per_variant_sample=result.per_variant_sample,
        total_sample=result.total_sample,
        alpha=result.alpha,
        power=result.power,
        n_variants=result.n_variants,
        two_tailed=result.two_tailed,
    )


# ---------------------------------------------------------------------------
# POST /runtime
# ---------------------------------------------------------------------------


@router.post(
    "/runtime",
    response_model=RuntimeResponse,
    status_code=status.HTTP_200_OK,
    summary="Estimate Experiment Runtime",
    description=(
        "Estimate how many days it will take to collect the required sample "
        "size given the daily traffic and traffic allocation."
    ),
)
def compute_runtime(body: RuntimeRequest) -> RuntimeResponse:
    """Estimate experiment runtime."""
    try:
        result = _calculator.compute_runtime_estimate(
            required_sample_size=body.required_sample_size,
            daily_traffic=body.daily_traffic,
            traffic_allocation=body.traffic_allocation,
            n_variants=body.n_variants,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OverflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=OUT_OF_RANGE_MESSAGE,
        ) from exc

    return RuntimeResponse(
        days_to_significance=result.days_to_significance,
        weeks_to_significance=result.weeks_to_significance,
        daily_traffic_per_variant=result.daily_traffic_per_variant,
        confidence_interval_days=result.confidence_interval_days,
    )


# ---------------------------------------------------------------------------
# GET /curve
# ---------------------------------------------------------------------------


@router.get(
    "/curve",
    response_model=PowerCurveResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Power Curve",
    description=(
        "Return the power curve: for each relative effect size, the required "
        "sample size per variant. Used to render the power curve chart."
    ),
)
def get_power_curve(
    baseline: float = Query(
        ...,
        gt=0,
        lt=1,
        description="Baseline conversion/success rate (0–1, exclusive).",
    ),
    alpha: float = Query(
        default=0.05,
        gt=0,
        lt=0.5,
        description="Type I error rate.",
    ),
    power: float = Query(
        default=0.80,
        gt=0,
        lt=1,
        description="Target statistical power.",
    ),
    mde: Optional[float] = Query(
        default=None,
        description="Currently selected MDE (marks a point on the curve).",
    ),
) -> PowerCurveResponse:
    """Return the power curve data for charting."""
    try:
        points = _calculator.compute_power_curve(
            baseline_rate=baseline,
            alpha=alpha,
            power_target=power,
            mde_target=mde,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OverflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=OUT_OF_RANGE_MESSAGE,
        ) from exc

    return PowerCurveResponse(
        points=[
            PowerCurvePoint(
                effect_size_relative=p.effect_size_relative,
                sample_size_per_variant=p.sample_size_per_variant,
                is_current_target=p.is_current_target,
            )
            for p in points
        ],
        baseline_rate=baseline,
        alpha=alpha,
        power_target=power,
    )


# ---------------------------------------------------------------------------
# POST /plan
# ---------------------------------------------------------------------------


@router.post(
    "/plan",
    response_model=PlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Planning Advice",
    description=(
        "Generate plain-English planning advice for a power analysis result. "
        "The advice is the built-in planning advice, and generated_by is "
        "always 'template'."
    ),
)
async def get_planning_advice(body: PlanRequest) -> PlanResponse:
    """Generate the built-in planning advice."""
    try:
        result = await _planner.get_planning_advice(
            experiment_name=body.experiment_name,
            metric_description=body.metric_description,
            baseline_rate=body.baseline_rate,
            mde=body.mde,
            runtime_days=body.runtime_days,
            business_context=body.business_context,
        )
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Planning advice",
            "Could not generate the planning advice",
            logger=logger,
        ) from exc

    return PlanResponse(
        advice=result["advice"],
        generated_by=result["generated_by"],
        experiment_name=body.experiment_name,
        baseline_rate=body.baseline_rate,
        mde=body.mde,
        runtime_days=body.runtime_days,
    )

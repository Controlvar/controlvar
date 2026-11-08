"""
AI-Powered Experiment Design API endpoints (Issue #23).

Routes:
    POST /api/v1/ai/design                  — AI experiment design assistant
    POST /api/v1/ai/interpret/{experiment_id} — AI results interpreter
    GET  /api/v1/ai/sample-size             — Sample size calculator
    GET  /api/v1/ai/templates               — List experiment templates
    GET  /api/v1/ai/templates/{template_id} — Get single template

All endpoints require an authenticated user (DEVELOPER or higher recommended).
The design and interpret endpoints degrade gracefully when the Claude API is
unavailable — template-based responses are returned instead.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.models.user import User
from backend.app.schemas.ai_design import (
    DesignRequest,
    ExperimentDesignSuggestionResponse,
    ExperimentTemplateResponse,
    InterpretRequest,
    ResultsInterpretationResponse,
    SampleSizeEstimateResponse,
)
from backend.app.schemas.power_calculator import MAX_COUNT
from backend.app.services.ai_design_service import AIDesignService
from backend.app.services.experiment_template_service import ExperimentTemplateService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)


# ---------------------------------------------------------------------------
# POST /design
# ---------------------------------------------------------------------------


@router.post(
    "/design",
    response_model=ExperimentDesignSuggestionResponse,
    summary="AI Experiment Design Assistant",
    description=(
        "Generate an AI-powered experiment design suggestion. "
        "Falls back to template-based suggestions when the Claude API is unavailable."
    ),
)
def suggest_design(
    body: DesignRequest,
    current_user: User = Depends(deps.get_current_active_user),
) -> ExperimentDesignSuggestionResponse:
    """Generate an experiment design suggestion for the given description."""
    suggestion = AIDesignService.suggest_experiment_design(
        description=body.description,
        experiment_type=body.experiment_type,
    )
    return ExperimentDesignSuggestionResponse(**suggestion.__dict__)


# ---------------------------------------------------------------------------
# POST /interpret/{experiment_id}
# ---------------------------------------------------------------------------


@router.post(
    "/interpret/{experiment_id}",
    response_model=ResultsInterpretationResponse,
    summary="AI Results Interpreter",
    description=(
        "Generate a plain-English interpretation of experiment results. "
        "Falls back to a template-based interpretation when AI is unavailable."
    ),
)
def interpret_results(
    experiment_id: str,
    body: InterpretRequest,
    current_user: User = Depends(deps.get_current_active_user),
) -> ResultsInterpretationResponse:
    """Interpret experiment results and return a recommendation."""
    results = {
        "p_value": body.p_value,
        "relative_improvement_pct": body.relative_improvement_pct,
        "variant_name": body.variant_name,
    }
    interp = AIDesignService.interpret_results(results)
    return ResultsInterpretationResponse(**interp.__dict__)


# ---------------------------------------------------------------------------
# GET /sample-size
# ---------------------------------------------------------------------------


@router.get(
    "/sample-size",
    response_model=SampleSizeEstimateResponse,
    summary="Sample Size Calculator",
    description="Calculate the required sample size for an experiment using statistical formulas.",
)
def estimate_sample_size(
    baseline_rate: float = Query(
        ..., description="Current baseline conversion rate (0–1)"
    ),
    mde: float = Query(
        ..., description="Minimum detectable effect — absolute change (0–1)"
    ),
    confidence: float = Query(
        default=0.95, description="Desired confidence level (default 0.95)"
    ),
    power: float = Query(
        default=0.80, description="Desired statistical power (default 0.80)"
    ),
    daily_traffic: Optional[int] = Query(
        default=None,
        gt=0,
        le=MAX_COUNT,
        description=(
            "Users a day who enter the experiment, split evenly between the two "
            "variants; used to compute days_to_significance"
        ),
    ),
    current_user: User = Depends(deps.get_current_active_user),
) -> SampleSizeEstimateResponse:
    """Estimate the required sample size per variant."""
    estimate = AIDesignService.estimate_sample_size(
        baseline_rate=baseline_rate,
        mde=mde,
        confidence=confidence,
        power=power,
        daily_traffic=daily_traffic,
    )
    return SampleSizeEstimateResponse(**estimate.__dict__)


# ---------------------------------------------------------------------------
# GET /templates
# ---------------------------------------------------------------------------


@router.get(
    "/templates",
    response_model=List[ExperimentTemplateResponse],
    summary="List Experiment Templates",
    description="Return all pre-built experiment templates, optionally filtered by type.",
)
def list_templates(
    type: Optional[str] = Query(default=None, description="Filter by experiment type"),
    current_user: User = Depends(deps.get_current_active_user),
) -> List[ExperimentTemplateResponse]:
    """List available experiment templates."""
    templates = ExperimentTemplateService.list_templates(experiment_type=type)
    return [ExperimentTemplateResponse(**t.__dict__) for t in templates]


# ---------------------------------------------------------------------------
# GET /templates/{template_id}
# ---------------------------------------------------------------------------


@router.get(
    "/templates/{template_id}",
    response_model=ExperimentTemplateResponse,
    summary="Get Experiment Template",
    description="Retrieve a single experiment template by its ID.",
)
def get_template(
    template_id: str,
    current_user: User = Depends(deps.get_current_active_user),
) -> ExperimentTemplateResponse:
    """Retrieve a single experiment template by ID."""
    template = ExperimentTemplateService.get_template(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found",
        )
    return ExperimentTemplateResponse(**template.__dict__)

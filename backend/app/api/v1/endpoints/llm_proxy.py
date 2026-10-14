"""
LLM Proxy endpoints (EP-046).

Routes completion requests, collects evaluation scores, returns analytics,
and runs LLM-as-judge scoring.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.logger import unexpected_failure
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.llm_experiment import LLMEvaluation, LLMExperimentStatus
from backend.app.models.user import User
from backend.app.schemas.llm_experiments import (
    LLMCompleteRequest,
    LLMCompleteResponse,
    LLMEvaluationResponse,
    LLMExperimentResults,
    LLMJudgeRequest,
    SubmitEvaluationRequest,
)
from backend.app.services.llm_analytics_service import LLMEvaluationAnalyticsService
from backend.app.services.llm_experiment_service import LLMExperimentService
from backend.app.services.llm_proxy_service import GeminiError, LLMProxyService

router = APIRouter(tags=["LLM Proxy"])
logger = logging.getLogger(__name__)
_experiment_service = LLMExperimentService()
_proxy_service = LLMProxyService()
_analytics_service = LLMEvaluationAnalyticsService()


def _eval_to_response(ev: LLMEvaluation) -> LLMEvaluationResponse:
    return LLMEvaluationResponse(
        id=str(ev.id),
        llm_experiment_id=str(ev.llm_experiment_id),
        variant_id=str(ev.variant_id),
        user_id=ev.user_id,
        input_variables=ev.input_variables or {},
        rendered_prompt=ev.rendered_prompt or "",
        model_response=ev.model_response or "",
        latency_ms=ev.latency_ms or 0,
        input_tokens=ev.input_tokens or 0,
        output_tokens=ev.output_tokens or 0,
        estimated_cost_usd=ev.estimated_cost_usd or 0.0,
        human_rating=ev.human_rating,
        auto_eval_score=ev.auto_eval_score,
        business_metric_value=ev.business_metric_value,
        created_at=ev.created_at,
    )


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


@router.post(
    "/{experiment_id}/complete",
    response_model=LLMCompleteResponse,
    summary="Get LLM completion for assigned variant",
)
async def llm_complete(
    experiment_id: UUID,
    body: LLMCompleteRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Assign the user to a variant and return an LLM completion.

    The variant is chosen via consistent hash (same user always gets the same
    variant).  The call is logged as an LLMEvaluation record.
    """
    # Any authenticated user can call this endpoint (READ permission)
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )

    experiment = _experiment_service.get_experiment(db, experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )
    if experiment.status != LLMExperimentStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Experiment is not ACTIVE (status={experiment.status.value})",
        )

    try:
        evaluation = await _proxy_service.complete(
            db=db,
            experiment_id=experiment_id,
            user_id=body.user_id,
            input_variables=body.input_variables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except GeminiError as exc:
        # The Gemini client's own sentences (HTTP status, finish reason, a
        # missing key): written by llm_proxy_service, kept for the caller.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM provider error: {exc}",
        )
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "LLM completion",
            "The LLM provider could not complete the request",
            db=db,
            logger=logger,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    # Load the variant for the response
    variant = _experiment_service.get_variant(db, evaluation.variant_id)

    return LLMCompleteResponse(
        variant_id=str(evaluation.variant_id),
        variant_name=variant.name if variant else "",
        provider=variant.provider.value
        if variant and hasattr(variant.provider, "value")
        else "",
        model_name=variant.model_name if variant else "",
        response=evaluation.model_response or "",
        latency_ms=evaluation.latency_ms or 0,
        cost_usd=evaluation.estimated_cost_usd or 0.0,
        input_tokens=evaluation.input_tokens or 0,
        output_tokens=evaluation.output_tokens or 0,
        evaluation_id=str(evaluation.id),
    )


# ---------------------------------------------------------------------------
# Evaluation / rating
# ---------------------------------------------------------------------------


@router.post(
    "/{experiment_id}/evaluate",
    response_model=LLMEvaluationResponse,
    summary="Submit evaluation scores",
)
def submit_evaluation(
    experiment_id: UUID,
    body: SubmitEvaluationRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Submit human rating and/or business metric value for a prior completion.
    """
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )

    evaluation = (
        db.query(LLMEvaluation)
        .filter(LLMEvaluation.id == UUID(body.evaluation_id))
        .first()
    )
    if evaluation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation not found"
        )
    if str(evaluation.llm_experiment_id) != str(experiment_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evaluation does not belong to this experiment",
        )

    if body.human_rating is not None:
        evaluation = _analytics_service.submit_human_rating(
            db, UUID(body.evaluation_id), body.human_rating
        )
    if body.business_metric_value is not None:
        evaluation = _analytics_service.submit_business_metric(
            db, UUID(body.evaluation_id), body.business_metric_value
        )

    return _eval_to_response(evaluation)


# ---------------------------------------------------------------------------
# Results / analytics
# ---------------------------------------------------------------------------


@router.get(
    "/{experiment_id}/results",
    response_model=LLMExperimentResults,
    summary="Get LLM experiment analytics results",
)
def get_llm_results(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Return per-variant statistics, CIs, p-values, and winner determination."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    try:
        return _analytics_service.get_experiment_results(db, experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------


@router.post(
    "/{experiment_id}/judge",
    response_model=list,
    summary="Run LLM-as-judge scoring",
)
async def run_judge(
    experiment_id: UUID,
    body: LLMJudgeRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Use a judge LLM (default: LLM_DEFAULT_JUDGE_MODEL) to score all responses in
    this experiment on the specified criteria (e.g. 'helpfulness', 'accuracy').

    Returns one result per evaluation: its score from 0 to 1, stored as the
    evaluation's auto_eval_score. When the judge call fails or the reply has no
    usable score, no score is stored: the result's score is null, error is
    "call_failed" or "no_score", and an earlier score is replaced by null.
    """
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )

    experiment = _experiment_service.get_experiment(db, experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="LLM experiment not found"
        )

    eval_uuids = None
    if body.evaluation_ids:
        try:
            eval_uuids = [UUID(eid) for eid in body.evaluation_ids]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid evaluation_id format",
            )

    results = await _analytics_service.run_llm_as_judge(
        db=db,
        experiment_id=experiment_id,
        judge_criteria=body.criteria,
        judge_model=body.judge_model,
        evaluation_ids=eval_uuids,
    )
    return [r.model_dump() for r in results]

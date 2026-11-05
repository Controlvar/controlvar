"""
Cross-experiment interaction detection endpoints (Issue #25).

GET /api/v1/interactions/scan                     — scan all active experiments
GET /api/v1/interactions/{exp_a_id}/{exp_b_id}    — analyze a specific pair (beta)

``/scan`` reports overlap only: its interaction, novelty and SUTVA sub-results
are null and the risk level comes from the overlap.  The pair route (beta,
#219) reports the real overlap and tests whether each treatment's lift differs
across the other experiment's arms (``InteractionPairResponse``).

Access: ANALYST, DEVELOPER or ADMIN (VIEWER gets 403).
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_active_user, get_db
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.logger import unexpected_failure
from backend.app.models.user import User, UserRole
from backend.app.schemas.interaction import (
    ActiveInteractionScanResponse,
    InteractionAnalysisResponse,
    InteractionPairResponse,
    RiskLevel,
)
from backend.app.services.interaction_detection_service import (
    InteractionAnalysis,
    InteractionDetectionService,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

#: The pair route is beta in the OpenAPI document (docs/api/stability.md) as
#: well as in the response's analysis_status (#219).
_BETA = {"x-stability": "beta"}

#: The fixed answers of the pair route.
SAME_EXPERIMENT = "The two experiments must be different."
EXPERIMENT_NOT_FOUND = "{} does not match an experiment."


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------


def _require_developer(current_user: User) -> None:
    """Raise 403 when the user is VIEWER (read-only role).

    DEVELOPER, ANALYST, and ADMIN may access interaction analysis endpoints.
    """
    role = getattr(current_user, "role", None)
    if current_user.is_superuser:
        return
    if role in (UserRole.VIEWER,):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role.",
        )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _to_response(analysis: InteractionAnalysis) -> InteractionAnalysisResponse:
    """Convert an InteractionAnalysis dataclass to ``/scan``'s item schema."""
    from backend.app.schemas.interaction import (
        InteractionResultSchema,
        NoveltyResultSchema,
        SUTVAResultSchema,
    )

    interaction_result = None
    if analysis.interaction_result is not None:
        interaction_result = InteractionResultSchema(
            has_interaction=analysis.interaction_result.has_interaction,
            p_value=analysis.interaction_result.p_value,
            interaction_effect_size=analysis.interaction_result.interaction_effect_size,
            warning_message=analysis.interaction_result.warning_message,
        )

    novelty_result = None
    if analysis.novelty_result is not None:
        novelty_result = NoveltyResultSchema(
            has_novelty=analysis.novelty_result.has_novelty,
            decline_rate=analysis.novelty_result.decline_rate,
            recommendation=analysis.novelty_result.recommendation,
        )

    sutva_result = None
    if analysis.sutva_result is not None:
        sutva_result = SUTVAResultSchema(
            has_violation=analysis.sutva_result.has_violation,
            contamination_rate=analysis.sutva_result.contamination_rate,
            warning_message=analysis.sutva_result.warning_message,
        )

    return InteractionAnalysisResponse(
        experiment_a_id=analysis.experiment_a_id,
        experiment_b_id=analysis.experiment_b_id,
        overlap_coefficient=analysis.overlap_coefficient,
        has_significant_overlap=analysis.has_significant_overlap,
        interaction_result=interaction_result,
        novelty_result=novelty_result,
        sutva_result=sutva_result,
        overall_risk=RiskLevel(analysis.overall_risk),
        recommendations=analysis.recommendations,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/scan", response_model=ActiveInteractionScanResponse)
def scan_interactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Scan all active experiments for pairwise interactions.

    Requires DEVELOPER role or higher.
    """
    try:
        _require_developer(current_user)

        service = InteractionDetectionService()
        analyses = service.scan_active_experiments(db)
        active_ids = service._get_active_experiment_ids(db)

        response_analyses = [_to_response(a) for a in analyses]
        high_risk_count = sum(
            1 for a in response_analyses if a.overall_risk == RiskLevel.HIGH
        )

        return ActiveInteractionScanResponse(
            total_active_experiments=len(active_ids),
            pairs_analyzed=len(response_analyses),
            high_risk_pairs=high_risk_count,
            analyses=response_analyses,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # A failed read is a 500, never "no active experiments" (#853).
        raise unexpected_failure(
            exc,
            "Interaction scan",
            "Could not scan the active experiments for interactions",
            db=db,
            logger=logger,
        )


@router.get(
    "/{exp_a_id}/{exp_b_id}",
    response_model=InteractionPairResponse,
    summary="Beta: do two experiments' effects interact?",
    openapi_extra=_BETA,
)
def analyze_pair(
    exp_a_id: UUID,
    exp_b_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """The overlap of two experiments, and whether their effects interact.

    Beta (#219).  For each treatment of each experiment: does its lift, in
    percentage points, on the experiment's primary conversion metric differ
    across the other experiment's arms, among the users in both?  Any two
    experiments, in any status.  A row that cannot be tested says why in
    ``unavailable_reason``.
    """
    _require_developer(current_user)
    if exp_a_id == exp_b_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=SAME_EXPERIMENT
        )

    service = InteractionDetectionService()
    try:
        service.bound_statement_time(db)
        experiment_a = service.load_experiment(db, exp_a_id)
        if experiment_a is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=EXPERIMENT_NOT_FOUND.format("experiment_a_id"),
            )
        experiment_b = service.load_experiment(db, exp_b_id)
        if experiment_b is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=EXPERIMENT_NOT_FOUND.format("experiment_b_id"),
            )
        return service.analyze_pair_interactions(experiment_a, experiment_b, db)
    except HTTPException:
        raise
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Interaction analysis",
            "Could not analyse the two experiments",
            db=db,
            logger=logger,
        )

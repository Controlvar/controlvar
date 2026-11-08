"""
Post-Stratification & BH FDR Correction endpoints — EP-043.

Provides:
  POST /results/{experiment_id}/post-stratification
      Not available yet: answers 501 (no per-user stratum data exists).

  POST /results/{experiment_id}/fdr-correction
      Apply Benjamini-Hochberg FDR correction to a set of per-metric p-values.
"""

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_active_user, get_db
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.logger import unexpected_failure
from backend.app.models.experiment import Experiment
from backend.app.models.user import User
from backend.app.schemas.post_stratification import (
    FDRCorrectionRequest,
    FDRResultResponse,
    PostStratificationRequest,
    PostStratResultResponse,
)
from backend.app.services.fdr_correction_service import BenjaminiHochbergService

router = APIRouter(route_class=OffLoopRoute)
logger = logging.getLogger(__name__)

POST_STRAT_UNAVAILABLE_DETAIL = (
    "Post-stratification is not available yet: it is not computed from an "
    "experiment's recorded data."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_experiment_or_404(experiment_id: UUID, db: Session) -> Experiment:
    """Fetch an Experiment by ID or raise HTTP 404."""
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if experiment is None:
        raise HTTPException(
            status_code=404,
            detail=f"Experiment '{experiment_id}' not found",
        )
    return experiment


# ---------------------------------------------------------------------------
# Endpoint 1 — POST /results/{experiment_id}/post-stratification
# ---------------------------------------------------------------------------


@router.post(
    "/{experiment_id}/post-stratification",
    response_model=PostStratResultResponse,
    summary="Post-stratification variance reduction (not available yet)",
    description=(
        "Not available yet: this route answers 501 for every existing "
        "experiment. Post-stratification needs one metric value and the "
        "stratum attributes per assigned user, and nothing builds those rows "
        "from an experiment's recorded assignments and events yet. The route "
        "reports no numbers until it can compute them from that data."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No experiment with this ID"},
        status.HTTP_501_NOT_IMPLEMENTED: {
            "description": "Post-stratification is not available yet",
        },
    },
    tags=["Results"],
    openapi_extra={"x-stability": "beta"},
)
def compute_post_stratification(
    experiment_id: UUID,
    request: PostStratificationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PostStratResultResponse:
    """
    Refuse with 501: post-stratification is not available yet.

    ``PostStratificationService`` computes the Horvitz-Thompson estimate from
    per-user rows (one metric value and the stratum attributes per user).
    Nothing builds those rows from an experiment's assignments and events
    yet (which metric, which aggregation, and where a stratum attribute comes
    from are all undecided), and this route used to fill the gap with
    randomly generated values -- a result that looked like an analysis of the
    experiment and was not.  Until a real data source exists the route says
    so instead.
    """
    _get_experiment_or_404(experiment_id, db)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=POST_STRAT_UNAVAILABLE_DETAIL,
    )


# ---------------------------------------------------------------------------
# Endpoint 2 — POST /results/{experiment_id}/fdr-correction
# ---------------------------------------------------------------------------


@router.post(
    "/{experiment_id}/fdr-correction",
    response_model=List[FDRResultResponse],
    summary="Benjamini-Hochberg FDR correction",
    description=(
        "Apply the Benjamini-Hochberg (1995) step-up procedure to control the "
        "False Discovery Rate (FDR) across multiple per-metric p-values. "
        "Less conservative than Bonferroni for ≥5 metrics."
    ),
    tags=["Results"],
)
def compute_fdr_correction(
    experiment_id: UUID,
    request: FDRCorrectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> List[FDRResultResponse]:
    """
    Apply Benjamini-Hochberg FDR correction to multiple p-values.

    The BH procedure:
    1. Sort m p-values ascending.
    2. For rank k: BH threshold = k/m × α.
    3. Find the largest k where p(k) ≤ k/m × α.
    4. Reject all hypotheses with rank ≤ that k.

    Body parameters:
    - **p_values**: Dict mapping metric_name → raw p-value (all in [0, 1]).
    - **fdr_threshold**: FDR control level α (default 0.05).

    Returns a list of FDRResultResponse items sorted by rank ascending,
    each with raw_p_value, adjusted_p_value, rank, and is_significant.
    """
    # Verify experiment exists
    _get_experiment_or_404(experiment_id, db)

    # Run BH correction
    service = BenjaminiHochbergService()
    try:
        results = service.correct(
            p_values=request.p_values,
            fdr_threshold=request.fdr_threshold,
        )
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "FDR correction",
            "Could not apply the FDR correction",
            db=db,
            logger=logger,
        )

    return [
        FDRResultResponse(
            metric_name=r.metric_name,
            raw_p_value=r.raw_p_value,
            adjusted_p_value=r.adjusted_p_value,
            rank=r.rank,
            is_significant=r.is_significant,
        )
        for r in results
    ]

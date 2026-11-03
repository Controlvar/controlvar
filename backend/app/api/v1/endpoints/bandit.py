"""
Bandit API endpoints — Issue #22 Batch C.

Endpoints
---------
GET  /api/v1/bandit/{experiment_id}
    Returns the current BanditStatusResponse for a MAB experiment.

POST /api/v1/bandit/{experiment_id}/update
    Triggers an immediate weight recalculation (DEVELOPER+ role required).

PUT  /api/v1/bandit/{experiment_id}/weights
    Manually override variant weights (ADMIN role required).
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.bandit_state import BanditState
from backend.app.models.experiment import Experiment
from backend.app.models.user import User, UserRole
from backend.app.schemas.bandit import (
    BanditStatusResponse,
    BanditUpdateRequest,
    BanditVariantWeight,
    OptimizationType,
)
from backend.app.services.audit_service import (
    audit_snapshot,
    record_experiment_change,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: The 403 of ``POST /bandit/{experiment_id}/update`` (#891): it names the
#: permission, then the built-in roles that have it, so it stays true when a
#: custom role or direct grant gives the permission instead.
TRIGGER_UPDATE_FORBIDDEN = (
    "Triggering a bandit update needs permission to change experiments, which "
    "the DEVELOPER and ADMIN roles have."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_mab_experiment(experiment_id: UUID, db: Session) -> Experiment:
    """
    Fetch a MAB experiment by UUID or raise an appropriate HTTPException.

    Raises 404 if not found.
    Raises 404 if optimization_type is 'fixed' (not a MAB experiment).
    """
    experiment: Optional[Experiment] = (
        db.query(Experiment).filter(Experiment.id == experiment_id).first()
    )
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Experiment {experiment_id} not found",
        )
    if experiment.optimization_type == "fixed":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Experiment {experiment_id} uses fixed allocation; "
                "bandit endpoints are only available for MAB experiments"
            ),
        )
    return experiment


def _int_or_none(value: object) -> Optional[int]:
    """Coerce a persisted provenance column to ``int`` (``None`` when unset)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _build_status_response(
    experiment: Experiment,
    bandit_state: Optional[BanditState],
    scheduler: BanditScheduler,
) -> BanditStatusResponse:
    """
    Build a BanditStatusResponse from an experiment and its persisted state.

    If no state exists yet, returns equal weights for all variants.
    """
    variant_id_to_name = {str(v.id): v.name for v in experiment.variants}
    seed: Optional[int] = None
    n_samples: Optional[int] = None
    engine_version = ENGINE_VERSION

    if bandit_state is None or not bandit_state.variant_weights:
        # No state yet — return equal weights
        n = len(experiment.variants)
        equal_weight = 1.0 / n if n > 0 else 1.0
        current_weights = [
            BanditVariantWeight(
                variant_id=str(v.id),
                variant_name=v.name,
                current_weight=equal_weight,
                successes=0,
                pulls=0,
                conversion_rate=0.0,
            )
            for v in experiment.variants
        ]
        weight_map = {str(v.id): equal_weight for v in experiment.variants}
        total_pulls = 0
        regret_pct: Optional[float] = None
        last_updated: Optional[str] = None
    else:
        seed = _int_or_none(getattr(bandit_state, "seed", None))
        n_samples = _int_or_none(getattr(bandit_state, "n_samples", None))
        stored_version = getattr(bandit_state, "engine_version", None)
        if isinstance(stored_version, str) and stored_version:
            engine_version = stored_version
        current_weights = []
        weight_map: dict = {}

        for vid, vdata in bandit_state.variant_weights.items():
            weight = float(vdata.get("weight", 0.0))
            pulls = int(vdata.get("pulls", 0))
            successes = int(vdata.get("successes", 0))
            conv_rate = successes / pulls if pulls > 0 else 0.0
            weight_map[vid] = weight
            current_weights.append(
                BanditVariantWeight(
                    variant_id=vid,
                    variant_name=variant_id_to_name.get(vid, vid),
                    current_weight=weight,
                    successes=successes,
                    pulls=pulls,
                    conversion_rate=conv_rate,
                )
            )

        total_pulls = bandit_state.total_pulls
        regret_pct = bandit_state.regret_reduction_pct
        last_updated = bandit_state.last_computed_at

    recommendation = scheduler.get_recommendation(weight_map, variant_id_to_name)

    return BanditStatusResponse(
        experiment_id=str(experiment.id),
        algorithm=OptimizationType(experiment.optimization_type),
        current_weights=current_weights,
        total_pulls=total_pulls,
        regret_reduction_pct=regret_pct,
        recommendation=recommendation,
        last_updated=last_updated,
        seed=seed,
        n_samples=n_samples,
        engine_version=engine_version,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{experiment_id}", response_model=BanditStatusResponse)
def get_bandit_status(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> BanditStatusResponse:
    """
    Return the current bandit allocation weights for a MAB experiment.

    Requires any authenticated user (VIEWER+).
    Returns equal weights if no bandit state has been computed yet.
    """
    experiment = _get_mab_experiment(experiment_id, db)

    bandit_state: Optional[BanditState] = (
        db.query(BanditState).filter(BanditState.experiment_id == experiment_id).first()
    )

    scheduler = BanditScheduler(db=db)
    return _build_status_response(experiment, bandit_state, scheduler)


@router.post("/{experiment_id}/update", response_model=BanditStatusResponse)
def trigger_bandit_update(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> BanditStatusResponse:
    """
    Trigger an immediate weight recalculation for a MAB experiment.

    Requires permission to change experiments: DEVELOPER or ADMIN, or in the
    full profile a custom role or direct grant giving experiment (update).
    """
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=TRIGGER_UPDATE_FORBIDDEN,
        )

    experiment = _get_mab_experiment(experiment_id, db)
    scheduler = BanditScheduler(db=db)

    try:
        scheduler.update_experiment(experiment)
    except Exception as exc:
        logger.error("Bandit update failed for %s: %s", experiment_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bandit weight update failed",
        ) from exc

    # Reload the freshly persisted state
    bandit_state: Optional[BanditState] = (
        db.query(BanditState).filter(BanditState.experiment_id == experiment_id).first()
    )
    return _build_status_response(experiment, bandit_state, scheduler)


@router.put("/{experiment_id}/weights", response_model=BanditStatusResponse)
def override_bandit_weights(
    experiment_id: UUID,
    body: BanditUpdateRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> BanditStatusResponse:
    """
    Manually override variant weights for a MAB experiment.

    Requires ADMIN role.
    """
    # Require ADMIN role (superuser or explicit ADMIN role)
    if not current_user.is_superuser and current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN role required to override bandit weights",
        )

    experiment = _get_mab_experiment(experiment_id, db)

    # Build the weights_payload in the format expected by BanditState
    weights_payload = {
        vid: {
            "weight": weight,
            "successes": 0,
            "failures": 0,
            "pulls": 0,
        }
        for vid, weight in body.weights.items()
    }

    before_audit = audit_snapshot(EntityType.EXPERIMENT, experiment)

    # Upsert BanditState
    bandit_state: Optional[BanditState] = (
        db.query(BanditState).filter(BanditState.experiment_id == experiment_id).first()
    )

    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()

    if bandit_state is None:
        bandit_state = BanditState(
            experiment_id=experiment_id,
            algorithm=experiment.optimization_type,
            variant_weights=weights_payload,
            total_pulls=0,
            regret_reduction_pct=None,
            last_computed_at=now_iso,
            seed=None,
            n_samples=None,
            engine_version=ENGINE_VERSION,
        )
        db.add(bandit_state)
    else:
        bandit_state.variant_weights = weights_payload
        bandit_state.last_computed_at = now_iso
        # A manual override draws no samples: clear the sampling provenance.
        bandit_state.seed = None
        bandit_state.n_samples = None
        bandit_state.engine_version = ENGINE_VERSION

    db.commit()
    db.refresh(bandit_state)

    record_experiment_change(
        db,
        current_user,
        ActionType.EXPERIMENT_UPDATE,
        experiment_id,
        before_audit,
        reason="bandit weights",
    )

    scheduler = BanditScheduler(db=db)
    return _build_status_response(experiment, bandit_state, scheduler)

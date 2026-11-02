"""
The body of the split URL preview (#393).

MODULE: split_url. This module holds the *body* of
``GET /api/v1/experiments/{experiment_id}/split-url/preview``. The route
itself is declared in the core experiments router
(``api/v1/endpoints/experiments.py``), which reaches this handler through
``core/optional_modules.split_url_preview_handler()`` and answers HTTP 501
when the module is not installed.

The preview answers what ``POST /api/v1/tracking/split-url/assign`` would
give a visitor id now, without recording anything
(``services/split_url_service.stored_or_new_variant``). The texts below are
shared with that route, so a condition reads the same on both.
"""

from typing import Any, Dict
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from backend.app.models.experiment import Experiment
from backend.app.models.user import User, UserRole
from modules.backend.app.services import split_url_service

#: The answer to a preview of a stored ``split_url_config`` that no longer
#: validates. Fixed text: it carries nothing from the stored config, and
#: ``{id}`` is the route's own placeholder, not a value.
SPLIT_URL_CONFIG_NOT_VALID_DETAIL = (
    "split_url_config is not valid; update it with PUT /api/v1/experiments/{id}"
)

#: The 400 for an experiment of another type.
NOT_SPLIT_URL_DETAIL = "Experiment is not a split_url type"

#: The 400 for a split URL experiment without a configuration.
NO_SPLIT_URL_CONFIG_DETAIL = "split_url_config is not set for this experiment"


def is_split_url(experiment: Experiment) -> bool:
    """Whether the experiment's type is ``split_url``."""
    value = getattr(experiment.experiment_type, "value", experiment.experiment_type)
    return value == "split_url"


async def preview_split_url_assignment(
    experiment_id: UUID,
    user_id: str,
    db: Session,
    current_user: User,
) -> Dict[str, Any]:
    """
    The URL a visitor id gets now, without recording anything.

    Their stored variant, if they have one; otherwise the variant a new
    visitor with that id would get (the bandit weights for a bandit
    experiment, else the traffic allocation). It checks no eligibility rule:
    a visitor the global holdout, mutual exclusion or targeting would keep
    out is served the control page by the assign route instead.

    This endpoint requires DEVELOPER or ADMIN role.

    Returns:
        Dict containing experiment_id, user_id, variant_name, url and
        traffic_allocation

    Raises:
        HTTPException 403: If user does not have DEVELOPER or ADMIN role
        HTTPException 404: If experiment not found
        HTTPException 400: If experiment is not a split_url type or has no config
        HTTPException 409: If the stored config breaks the rules (one saved
            before a rule, say); it is answered, not served
    """
    # A superuser, or the ADMIN or DEVELOPER role: the same rule as the core
    # route that calls this handler (experiments.preview_split_url_assignment).
    if not (
        getattr(current_user, "is_superuser", False)
        or getattr(current_user, "role", None) in (UserRole.ADMIN, UserRole.DEVELOPER)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only DEVELOPER or ADMIN users can access the split URL preview endpoint",
        )

    experiment = (
        db.query(Experiment)
        .options(joinedload(Experiment.variants))
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Experiment not found",
        )
    if not is_split_url(experiment):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=NOT_SPLIT_URL_DETAIL,
        )
    if not experiment.split_url_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=NO_SPLIT_URL_CONFIG_DETAIL,
        )
    # Stored JSONB is not re-validated on read: a config saved before a rule
    # can be one no request could store now. The caller can fix it with a
    # PUT, so it is a conflict, not a server error. Nothing is echoed.
    if split_url_service.entries_of(experiment) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
        )

    variant = split_url_service.stored_or_new_variant(db, experiment, user_id)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
        )
    config = split_url_service.parsed_config(experiment)
    url_variant = next(v for v in config.variants if v.name == variant.name)
    return {
        "experiment_id": str(experiment.id),
        "user_id": user_id,
        "variant_name": url_variant.name,
        "url": url_variant.url,
        "traffic_allocation": url_variant.traffic_allocation,
    }

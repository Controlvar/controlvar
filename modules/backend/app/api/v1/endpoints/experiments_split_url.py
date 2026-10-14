"""
Split URL experiment routing — EP-036.

MODULE: split_url. This module holds the *body* of
``GET /api/v1/experiments/{experiment_id}/split-url/preview``. The route
itself is declared in the core experiments router
(``api/v1/endpoints/experiments.py``), which reaches this handler through
``core/optional_modules.split_url_preview_handler()`` and answers HTTP 501
when the module is not installed.

The core keeps the *validation* of ``split_url_config``
(``schemas/split_url_config.py``); only the deterministic URL routing —
``services/split_url_service`` and this preview handler — is the module's.
"""

from typing import Any, Dict
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.models.user import User
from backend.app.schemas.split_url_config import SplitUrlConfig
from backend.app.services.experiment_service import ExperimentService
from modules.backend.app.services import split_url_service

#: The answer to a preview of a stored ``split_url_config`` that no longer
#: validates. Fixed text: it carries nothing from the stored config, and
#: ``{id}`` is the route's own placeholder, not a value.
SPLIT_URL_CONFIG_NOT_VALID_DETAIL = (
    "split_url_config is not valid; update it with PUT /api/v1/experiments/{id}"
)


async def preview_split_url_assignment(
    experiment_id: UUID,
    user_id: str,
    db: Session,
    current_user: User,
) -> Dict[str, Any]:
    """
    Preview the split URL variant assignment for a given user.

    Returns the URL variant that an MD5 hash of ``{user_id}:{experiment_id}``
    picks. It does not predict the Split URL router Lambda, which hashes the
    viewer's IP address and User-Agent with its configuration's
    ``experiment_key`` (#393).

    This endpoint requires DEVELOPER or ADMIN role.

    Returns:
        Dict containing variant_name, url, traffic_allocation, and experiment_id

    Raises:
        HTTPException 403: If user does not have DEVELOPER or ADMIN role
        HTTPException 404: If experiment not found
        HTTPException 400: If experiment is not a split_url type or has no config
        HTTPException 409: If the stored config is not valid (one saved before
            each allocation was held to 0-100, say); it is answered, not served
    """
    # Only ADMIN and DEVELOPER roles may use the preview endpoint
    is_admin_or_developer = (
        current_user.is_superuser
        or getattr(current_user, "role", None) in ("admin", "developer")
        or (
            hasattr(current_user, "username")
            and "developer" in (current_user.username or "").lower()
        )
    )
    if not is_admin_or_developer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only DEVELOPER or ADMIN users can access the split URL preview endpoint",
        )

    # Retrieve the experiment
    experiment_service = ExperimentService(db)
    experiment = experiment_service.get_experiment_by_id(experiment_id)
    if not experiment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Experiment not found",
        )

    # Validate it's a split_url experiment
    exp_type = (
        experiment.get("experiment_type")
        if isinstance(experiment, dict)
        else getattr(experiment, "experiment_type", None)
    )
    if hasattr(exp_type, "value"):
        exp_type = exp_type.value
    if exp_type != "split_url":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Experiment is not a split_url type",
        )

    # Validate split_url_config is present
    raw_config = (
        experiment.get("split_url_config")
        if isinstance(experiment, dict)
        else getattr(experiment, "split_url_config", None)
    )
    if not raw_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="split_url_config is not set for this experiment",
        )

    # Parse the config — it may be a dict (from DB JSONB) or already a SplitUrlConfig
    if isinstance(raw_config, dict):
        try:
            config = SplitUrlConfig(**raw_config)
        except ValidationError:
            # Stored JSONB is not re-validated on read: a config saved before
            # the range check (#1194) can be one no request could store now.
            # The caller can fix it with a PUT, so it is a conflict, not a
            # server error. No part of the config is echoed.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
            ) from None
    else:
        config = raw_config

    # Derive experiment key: use the experiment id string as the key
    exp_id_str = (
        experiment.get("id")
        if isinstance(experiment, dict)
        else str(getattr(experiment, "id", experiment_id))
    )
    experiment_key = str(exp_id_str)

    # Get deterministic variant assignment
    variant = split_url_service.get_url_variant(
        user_id=user_id,
        experiment_key=experiment_key,
        config=config,
    )

    return {
        "experiment_id": experiment_key,
        "user_id": user_id,
        "variant_name": variant.name,
        "url": variant.url,
        "traffic_allocation": variant.traffic_allocation,
    }

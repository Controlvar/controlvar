"""
Preview links: one variant of a visual experiment, on the site, for anyone
who has the link (#1545, S4).

Mounted under ``/experiments`` (``backend/app/api/api.py``):

* ``POST /api/v1/experiments/{experiment_id}/visual/preview-links`` makes one
  for a variant, the control included, valid for 1 to 168 hours (24 if not
  given). It answers the link, ``<editor_url>#controlvar-preview=<token>``,
  once: only the SHA-256 of the token is stored (a ``visual_tokens`` row of
  kind ``preview``, naming its variant by name, so a variants save that
  re-creates the variants keeps the link working).
* ``DELETE`` on the same path ends every live preview link of the experiment.

Both need a caller who may change the experiment
(``deps.get_experiment_change_access``: READ and UPDATE on experiments), and
both write an audit entry. Making a link also checks the experiment's page
origins (``backend/app/services/visual_origins.py``), as opening the editor
does, so a link is never made for a site this server does not accept
requests from.

Opening a link (``POST /api/v1/tracking/visual/preview``) records nothing;
see ``tracking_visual.py``.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.logger import unexpected_failure
from backend.app.core.logging import logger
from backend.app.core.security import hash_api_key
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.experiment import Experiment
from backend.app.models.user import User
from backend.app.models.visual_token import PREVIEW, VisualToken
from backend.app.schemas.storable_text import StorableTextModel
from backend.app.services.audit_service import AuditService
from backend.app.services.visual import is_visual
from backend.app.services.visual_origins import visual_origin_problem

router = APIRouter(route_class=OffLoopRoute)

#: The fragment a preview link carries its token in.
PREVIEW_FRAGMENT = "controlvar-preview"
#: How long a link lives, in hours.
DEFAULT_PREVIEW_HOURS = 24
MAX_PREVIEW_HOURS = 168

NOT_VISUAL = (
    "This experiment has no visual settings, so it has no preview links: a "
    "preview link shows a visual experiment's variant on its page."
)
VARIANT_NOT_FOUND = "This experiment has no variant with that variant_id."
EXPERIMENT_NOT_FOUND = "Experiment not found"


class PreviewLinkCreate(StorableTextModel):
    """The variant to preview, and for how long."""

    model_config = ConfigDict(extra="forbid")

    variant_id: UUID = Field(
        ..., description="One of the experiment's variants, the control included."
    )
    expires_in_hours: StrictInt = Field(
        DEFAULT_PREVIEW_HOURS,
        ge=1,
        le=MAX_PREVIEW_HOURS,
        description="How long the link works: 1 to 168 hours. 24 if not given.",
    )


class PreviewLinkCreated(BaseModel):
    """The link, shown once."""

    url: str = Field(
        ...,
        description=(
            "The experiment's editor page with `#controlvar-preview=<token>`. "
            "Anyone who opens it on a page with the Controlvar script sees the "
            "variant, until it expires or is revoked; opening it records nothing."
        ),
    )
    expires_at: datetime


def _experiment_to_change(db: Session, experiment_id: UUID, user: User) -> Experiment:
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=EXPERIMENT_NOT_FOUND
        )
    return deps.get_experiment_change_access(experiment, user)


@router.post(
    "/{experiment_id}/visual/preview-links",
    status_code=status.HTTP_201_CREATED,
    response_model=PreviewLinkCreated,
    summary="Make a preview link for a visual experiment's variant (beta)",
    response_description="The link, shown once",
    openapi_extra={"x-stability": "beta"},
    responses={
        400: {"description": "The experiment has no visual settings"},
        403: {"description": "The caller may not change experiments"},
        404: {"description": "No such experiment, or no such variant of it"},
        422: {
            "description": (
                "The request is not in the documented form, or a page of the "
                "experiment is on a site this server does not accept"
            )
        },
    },
)
async def create_preview_link(
    response: Response,
    experiment_id: UUID = Path(..., description="The visual experiment"),
    body: PreviewLinkCreate = Body(...),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> PreviewLinkCreated:
    """
    Make a link that shows one variant of a visual experiment on its page.

    Anyone who opens the link on a page with the Controlvar script sees the
    variant there, whatever the experiment's audience and status, until the
    link expires (1 to 168 hours, 24 by default) or the experiment's links are
    revoked. Opening it records nothing. The link is answered once; only a
    hash of its token is stored.

    Needs a caller who may change experiments (READ and UPDATE). Refused with
    422 when one of the experiment's pages, or its editor page, is on a site
    this server does not accept requests from (CORS_ORIGINS), or on the
    dashboard's own address, or when the server does not know the dashboard's
    address (DASHBOARD_ORIGINS or PUBLIC_BASE_URL): the same checks as opening
    the visual editor. Sent with `Cache-Control: no-store`.
    """
    experiment = _experiment_to_change(db, experiment_id, current_user)
    if not is_visual(experiment):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_VISUAL)
    variant = next((v for v in experiment.variants if v.id == body.variant_id), None)
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=VARIANT_NOT_FOUND
        )
    refusal = visual_origin_problem(experiment.visual_config)
    if refusal is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refusal
        )

    # Plain values: the commit below expires the rows.
    name = experiment.name
    editor_url = experiment.visual_config["editor_url"]
    variant_id = variant.id
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours)
    try:
        db.add(
            VisualToken(
                kind=PREVIEW,
                token_hash=hash_api_key(token),
                experiment_id=experiment_id,
                variant_name=variant.name,
                user_id=current_user.id,
                expires_at=expires_at,
            )
        )
        db.commit()
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Preview link create",
            "Could not make the preview link",
            db=db,
            logger=logger,
        )
    # The link is committed; a failed audit write does not undo it. The entry
    # holds no part of the token and no variant name.
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.EXPERIMENT_PREVIEW_LINK_CREATE,
        entity_type=EntityType.EXPERIMENT,
        entity_id=experiment_id,
        entity_name=name,
        after={"variant_id": str(variant_id), "expires_at": expires_at.isoformat()},
    )
    response.headers["Cache-Control"] = "no-store"
    return PreviewLinkCreated(
        url=f"{editor_url}#{PREVIEW_FRAGMENT}={token}", expires_at=expires_at
    )


@router.delete(
    "/{experiment_id}/visual/preview-links",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End every preview link of an experiment (beta)",
    openapi_extra={"x-stability": "beta"},
    responses={
        403: {"description": "The caller may not change experiments"},
        404: {"description": "No such experiment"},
    },
)
async def revoke_preview_links(
    experiment_id: UUID = Path(..., description="The experiment"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """
    End every live preview link of the experiment at once: each answers 404
    from then on. Needs a caller who may change experiments (READ and
    UPDATE). The audit entry records how many links were ended.
    """
    experiment = _experiment_to_change(db, experiment_id, current_user)
    name = experiment.name
    now = datetime.now(timezone.utc)
    try:
        ended = db.execute(
            update(VisualToken)
            .where(
                VisualToken.experiment_id == experiment_id,
                VisualToken.kind == PREVIEW,
                VisualToken.revoked_at.is_(None),
                VisualToken.expires_at > now,
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Preview links revoke",
            "Could not revoke the preview links",
            db=db,
            logger=logger,
        )
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.EXPERIMENT_PREVIEW_LINKS_REVOKE,
        entity_type=EntityType.EXPERIMENT,
        entity_id=experiment_id,
        entity_name=name,
        after={"revoked": ended},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

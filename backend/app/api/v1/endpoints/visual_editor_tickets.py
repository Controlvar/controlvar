"""
Opening a visual draft in the visual editor, from the dashboard (#1545).

Mounted under ``/experiments`` (``backend/app/api/api.py``), beside the
preview links:

* ``POST /api/v1/experiments/{experiment_id}/visual/editor-tickets`` answers
  an editor link, ``<editor_url>#controlvar-editor=<ticket>``, once. The
  ticket works once, for 120 seconds; only its SHA-256 is stored (a
  ``visual_tokens`` row of kind ``editor_ticket``). Making it first ends every
  live editor credential of the experiment, whoever holds it, so the editor is
  open in one place at a time; each editor session that ends writes a
  ``visual_editor_session_closed`` entry, by the caller.

What the link does next is in ``visual_editor.py`` and
``backend/app/services/visual_editor.py``.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.logger import unexpected_failure
from backend.app.core.logging import logger
from backend.app.core.security import hash_api_key
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User
from backend.app.models.visual_token import EDITOR_TICKET, REOPENED, VisualToken
from backend.app.schemas.visual_editor import EditorLinkOpened
from backend.app.services import visual_editor
from backend.app.services.audit_service import AuditActor
from backend.app.services.visual import is_visual
from backend.app.services.visual_origins import (
    editor_link_origin_refusal,
    visual_origin_problem,
)

router = APIRouter(route_class=OffLoopRoute)


@router.post(
    "/{experiment_id}/visual/editor-tickets",
    status_code=status.HTTP_201_CREATED,
    response_model=EditorLinkOpened,
    summary="Open a visual draft in the visual editor (beta)",
    response_description="The editor link, shown once",
    openapi_extra={"x-stability": "beta"},
    responses={
        400: {
            "description": (
                "The experiment has no visual settings, is not a draft, or has a "
                "start date"
            )
        },
        403: {
            "description": (
                "The request does not come from the dashboard, or the caller may "
                "not change experiments"
            )
        },
        404: {"description": "No such experiment"},
        422: {
            "description": (
                "The server does not know the dashboard's address, or a page of "
                "the experiment is on a site this server does not accept"
            )
        },
    },
)
def open_visual_editor(
    request: Request,
    response: Response,
    experiment_id: UUID = Path(..., description="The visual draft"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> EditorLinkOpened:
    """
    Make the link that opens a visual draft in the visual editor, on the
    experiment's own page.

    The link is the experiment's editor page with
    `#controlvar-editor=<ticket>`; it works once, within 120 seconds, and is
    answered once (only a hash of the ticket is stored). Making it ends every
    editor session of the experiment that is still open, whoever opened it.

    Accepted only from the dashboard: the request's `Origin` must be one of
    the server's dashboard origins (DASHBOARD_ORIGINS, or PUBLIC_BASE_URL's).
    Refused, in this order: 422 when the server knows no dashboard origin;
    403 for any other `Origin`, or none; 404; 403 for a caller who may not
    change experiments (READ and UPDATE); 400 when the experiment has no
    visual settings, is not a draft, or has a start date; 422 when one of its
    pages, or its editor page, is on a site this server does not accept
    requests from (CORS_ORIGINS), or on the dashboard's own address, or when
    CORS_ORIGINS is `*`. Sent with `Cache-Control: no-store`.
    """
    refusal = editor_link_origin_refusal(request.headers.get("origin"))
    if refusal is not None:
        raise HTTPException(status_code=refusal[0], detail=refusal[1])
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=visual_editor.EXPERIMENT_NOT_FOUND,
        )
    deps.get_experiment_change_access(experiment, current_user)
    if not is_visual(experiment):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=visual_editor.NOT_VISUAL
        )
    if experiment.status != ExperimentStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=visual_editor.NOT_A_DRAFT
        )
    if experiment.start_date is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=visual_editor.DATED_DRAFT
        )
    problem = visual_origin_problem(experiment.visual_config)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem
        )

    # Plain values: the commit below expires the rows.
    name = experiment.name
    editor_url = experiment.visual_config["editor_url"]
    actor = AuditActor.of(current_user)
    now = visual_editor.utcnow()
    ticket = secrets.token_urlsafe(32)
    expires_at = now + visual_editor.TICKET_LIFETIME
    try:
        visual_editor.hold_editor(db, experiment_id)
        ended = visual_editor.reopen(db, experiment_id, now)
        db.add(
            VisualToken(
                kind=EDITOR_TICKET,
                token_hash=hash_api_key(ticket),
                experiment_id=experiment_id,
                user_id=actor.id,
                expires_at=expires_at,
            )
        )
        db.commit()
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Visual editor open",
            "Could not open the visual editor",
            db=db,
            logger=logger,
        )
    # The ticket is committed; a failed audit write does not undo it.
    visual_editor.audit_sessions_closed(
        db,
        ended,
        actor=actor,
        reason=REOPENED,
        experiment_id=experiment_id,
        experiment_name=name,
    )
    response.headers["Cache-Control"] = "no-store"
    return EditorLinkOpened(
        open_url=f"{editor_url}#{visual_editor.EDITOR_FRAGMENT}={ticket}",
        expires_at=expires_at,
    )

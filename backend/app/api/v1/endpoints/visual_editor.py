"""
The visual editor's sign-in, after the dashboard made the editor link
(#1545). Mounted under ``/visual-editor`` (``backend/app/api/api.py``); no
route here takes a dashboard sign-in or an API key.

* ``POST /api/v1/visual-editor/tickets/claim`` ``{ticket, claim}``: sent by
  the Controlvar script on the page the editor link opened. It spends the
  ticket and stores the claim, a value of the page's own, for 120 seconds.
  The request's ``Origin`` must be one of the experiment's sites.
* ``POST /api/v1/visual-editor/sessions`` ``{claim, page_origin}``: sent by
  the visual editor's panel, from the dashboard. It spends the claim and
  answers an editor session, ``cves_...``, for 4 hours, with the draft's
  design.

Each spends its credential with one statement and commits at once, so a
refusal that comes after the spend leaves the credential spent. A spent
ticket or claim presented again ends the editor that link opened (409); see
``backend/app/services/visual_editor.py``. Both routes have an exact rate
limit of their own (``RATE_LIMIT_CONFIG``).
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.logger import unexpected_failure
from backend.app.core.logging import logger
from backend.app.core.security import hash_api_key
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.experiment import Experiment
from backend.app.models.user import User
from backend.app.models.visual_token import (
    EDITOR_CLAIM,
    EDITOR_SESSION,
    EDITOR_TICKET,
    VisualToken,
)
from backend.app.schemas.visual_editor import (
    ClaimExchange,
    EditorSessionOpened,
    TicketClaim,
    TicketClaimed,
)
from backend.app.services import visual_editor
from backend.app.services.audit_service import AuditActor, AuditService
from backend.app.services.visual_origins import (
    SITE_NOT_LISTED,
    dashboard_origin_refusal,
    request_origin,
    site_origin_refusal,
    site_origins,
)

router = APIRouter(route_class=OffLoopRoute)


async def require_dashboard_origin(request: Request) -> None:
    """403 unless the request may come from the dashboard: an ``Origin`` that
    is a dashboard origin, or, for a ``GET`` or ``HEAD`` only, no ``Origin``.
    Reads one header and nothing else."""
    refusal = dashboard_origin_refusal(request.headers.get("origin"), request.method)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)


def refuse_unless_allowed(experiment: Experiment, user: User | None) -> None:
    """403 with the editor's own text unless *user* is active and may still
    change *experiment* (READ and UPDATE on experiments, or a superuser)."""
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=visual_editor.CANNOT_CHANGE
        )
    try:
        deps.get_experiment_change_access(experiment, user)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=visual_editor.CANNOT_CHANGE
        ) from None


@router.post(
    "/tickets/claim",
    status_code=status.HTTP_201_CREATED,
    response_model=TicketClaimed,
    summary="Claim an editor link, from the page it opened (beta)",
    response_description="The claim is stored; no credential comes back",
    openapi_extra={"x-stability": "beta"},
    responses={
        401: {"description": "The editor link is unknown, expired or ended"},
        403: {
            "description": "The request does not come from one of the experiment's sites"
        },
        409: {
            "description": "The editor link was already used: the editor it opened is closed"
        },
        422: {
            "description": (
                "The request is not in the documented form, or the value sent "
                "with the link is already in use"
            )
        },
    },
)
def claim_editor_link(
    request: Request,
    response: Response,
    body: TicketClaim = Body(...),
    db: Session = Depends(deps.get_db),
) -> TicketClaimed:
    """
    Spend an editor link's ticket and store the page's value in its place.

    Sent by the Controlvar script on the page the editor link opened, with no
    API key and no cookie. `claim` is 43 base64url characters the page drew;
    the editor's panel then exchanges it for a session
    (`POST /api/v1/visual-editor/sessions`) within 120 seconds. The answer holds
    no credential.

    Refused, in this order: 422 when `claim` is not 43 base64url characters,
    or is already in use (the ticket is not spent); 409 when the ticket was
    already used, which also closes the editor it opened; 401 when it is
    unknown, expired or ended; 403 when the request's `Origin` is not one of
    the experiment's sites (the ticket stays spent). Sent with
    `Cache-Control: no-store`.
    """
    claim_hash = hash_api_key(body.claim)
    ticket_hash = hash_api_key(body.ticket)
    try:
        if visual_editor.hash_is_stored(db, claim_hash):
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=visual_editor.CLAIM_IN_USE,
            )
        now = visual_editor.utcnow()
        ticket = visual_editor.spend(db, EDITOR_TICKET, ticket_hash, now)
        if ticket is None:
            used = visual_editor.used_row(db, EDITOR_TICKET, ticket_hash)
            if used is not None:
                raise visual_editor.end_reused_link(
                    db, used.id, used.user_id, used.experiment_id
                )
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=visual_editor.LINK_NOT_VALID,
            )
        # Spent, whatever is refused next.
        db.commit()
        config = db.execute(
            select(Experiment.visual_config).where(
                Experiment.id == ticket.experiment_id
            )
        ).scalar()
        refusal = site_origin_refusal(
            request.headers.get("origin"), site_origins(config)
        )
        if refusal is not None:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
        expires_at = now + visual_editor.CLAIM_LIFETIME
        db.add(
            VisualToken(
                kind=EDITOR_CLAIM,
                token_hash=claim_hash,
                experiment_id=ticket.experiment_id,
                user_id=ticket.user_id,
                parent_id=ticket.id,
                expires_at=expires_at,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # The same value was stored since the check above; the ticket
            # stays spent.
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=visual_editor.CLAIM_IN_USE,
            ) from None
    except HTTPException:
        raise
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Editor link claim",
            "Could not claim the editor link",
            db=db,
            logger=logger,
        )
    response.headers["Cache-Control"] = "no-store"
    return TicketClaimed(expires_at=expires_at)


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=EditorSessionOpened,
    dependencies=[Depends(require_dashboard_origin)],
    summary="Start a visual editor session (beta)",
    response_description="The session's token, shown once, and the draft",
    openapi_extra={"x-stability": "beta"},
    responses={
        400: {
            "description": (
                "The experiment is no longer a draft, has a start date, or has no "
                "visual settings"
            )
        },
        401: {"description": "The value is unknown, expired or ended"},
        403: {
            "description": (
                "The request does not come from the dashboard, the page is not one "
                "of the experiment's sites, or the user may no longer change the "
                "experiment"
            )
        },
        409: {
            "description": "The editor link was already used: the editor it opened is closed"
        },
    },
)
def open_editor_session(
    response: Response,
    body: ClaimExchange = Body(...),
    db: Session = Depends(deps.get_db),
) -> EditorSessionOpened:
    """
    Exchange the page's value for a visual editor session.

    Sent by the visual editor's panel, from the dashboard, with the value the
    page handed it and that page's origin. The session belongs to the user
    who made the editor link, lasts 4 hours and cannot be extended; its token
    works only on the visual editor's session routes. The answer carries the
    token once, the experiment's sites the editor may run on, and the
    draft's design.

    Refused, in this order: 403 when the request's `Origin` is not one of the
    dashboard's; 422 when the request is not in the documented form; 409 when
    the value was already used, which also closes the editor it opened; 401
    when it is unknown, expired or ended; 403 when `page_origin` is not one of
    the experiment's sites; 403 when the user is no longer active or may no
    longer change experiments; 400 when the experiment is no longer a draft,
    has a start date, or has no visual settings. After the 401 every refusal
    leaves the value spent. Sent with `Cache-Control: no-store`.
    """
    claim_hash = hash_api_key(body.claim)
    try:
        now = visual_editor.utcnow()
        claim = visual_editor.spend(db, EDITOR_CLAIM, claim_hash, now)
        if claim is None:
            used = visual_editor.used_row(db, EDITOR_CLAIM, claim_hash)
            if used is not None and used.parent_id is not None:
                raise visual_editor.end_reused_link(
                    db, used.parent_id, used.user_id, used.experiment_id
                )
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=visual_editor.LINK_NOT_VALID,
            )
        # Spent, whatever is refused next.
        db.commit()
        experiment = (
            db.query(Experiment).filter(Experiment.id == claim.experiment_id).first()
        )
        if experiment is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=visual_editor.LINK_NOT_VALID,
            )
        page = request_origin(body.page_origin)
        if page is None or page not in site_origins(experiment.visual_config):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=SITE_NOT_LISTED
            )
        user = db.get(User, claim.user_id) if claim.user_id else None
        refuse_unless_allowed(experiment, user)
        visual_editor.refuse_unless_open_to_edit(experiment)

        actor = AuditActor.of(user)
        token = visual_editor.SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires_at = now + visual_editor.SESSION_LIFETIME
        session_id = uuid.uuid4()
        state = visual_editor.session_state(experiment, expires_at)
        name = experiment.name
        experiment_id = experiment.id
        db.add(
            VisualToken(
                id=session_id,
                kind=EDITOR_SESSION,
                token_hash=hash_api_key(token),
                experiment_id=experiment_id,
                user_id=claim.user_id,
                parent_id=claim.id,
                expires_at=expires_at,
            )
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Visual editor session",
            "Could not start the visual editor session",
            db=db,
            logger=logger,
        )
    AuditService.record_after_commit(
        db,
        actor=actor,
        action=ActionType.VISUAL_EDITOR_SESSION_OPENED,
        entity_type=EntityType.EXPERIMENT,
        entity_id=experiment_id,
        entity_name=name,
        after={"session_id": str(session_id)},
    )
    response.headers["Cache-Control"] = "no-store"
    return EditorSessionOpened(token=token, **state)

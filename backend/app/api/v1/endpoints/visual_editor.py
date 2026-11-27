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
refusal that comes after the spend leaves the credential spent. It then
writes the next credential under ``visual_editor.hold_editor``, and only
while the one it spent is still live. A spent ticket or claim presented
again ends the editor that link opened (409); see
``backend/app/services/visual_editor.py``. Both routes have an exact rate
limit of their own (``RATE_LIMIT_CONFIG``).

The session's own routes take ``Authorization: Bearer cves_...`` and nothing
else; no other route takes that token:

* ``GET /api/v1/visual-editor/sessions/current``: the session and the draft.
* ``DELETE /api/v1/visual-editor/sessions/current``: signs out.
* ``PUT /api/v1/visual-editor/sessions/current/variants/{variant_id}/changes``:
  saves one variant's page changes, through the experiment update's own
  steps (``experiments._apply_experiment_update``), as the session's user.
* ``PATCH /api/v1/visual-editor/sessions/current/experiment``: saves the
  draft's audience, split, metrics, goals and exclusion group, the design
  through the same experiment update and the group through the exclusion
  group service, refusing a request made from a version of the design that
  has changed since.
* ``GET /api/v1/visual-editor/sessions/current/segments`` and
  ``GET /api/v1/visual-editor/sessions/current/exclusion-groups``: the active
  segments and exclusion groups the editor may choose from, needing what
  the dashboard's lists of them need.

Each runs :func:`current_editor_session` first (signing out,
:func:`signing_out_editor_session`). A session that cannot be used answers 401
with ``{"detail", "reason"}`` (:class:`EditorSessionClosed`), so the editor can
say why it closed.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api import deps, experiment_cache
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.v1.endpoints import experiments as experiments_endpoints
from backend.app.api.v1.endpoints import (
    mutual_exclusion_groups as exclusion_groups_endpoints,
)
from backend.app.api.v1.endpoints import segments as segments_endpoints
from backend.app.core.logger import unexpected_failure
from backend.app.core.logging import logger
from backend.app.core.permissions import Action
from backend.app.core.security import hash_api_key
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    validate_segment_rules,
)
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.segment import Segment, SegmentKind, SegmentStatus
from backend.app.models.user import User
from backend.app.models.visual_token import (
    EDITOR_CLAIM,
    EDITOR_SESSION,
    EDITOR_TICKET,
    SIGNED_OUT,
    VisualToken,
)
from backend.app.schemas.experiment import ExperimentUpdate, VariantBase
from backend.app.schemas.visual import VisualChanges
from backend.app.schemas.visual_editor import (
    ClaimExchange,
    EditorDesignChange,
    EditorExclusionGroup,
    EditorSegment,
    EditorSessionOpened,
    EditorSessionState,
    TicketClaim,
    TicketClaimed,
)
from backend.app.services import visual_editor
from backend.app.services.audit_service import AuditActor, AuditService
from backend.app.services.mutual_exclusion_service import MutualExclusionService
from backend.app.services.visual import design_etag
from backend.app.services.visual_origins import (
    SITE_NOT_LISTED,
    dashboard_origin_refusal,
    request_origin,
    site_origin_refusal,
    site_origins,
    visual_origin_problem,
)

router = APIRouter(route_class=OffLoopRoute)


class EditorSessionRefusal(BaseModel):
    """An editor session's 401."""

    detail: str = Field(..., description="The sentence the editor shows.")
    reason: str = Field(
        ...,
        description=(
            "Why: reopened, ticket_reused, started, signed_out, scheduled, "
            "expired or not_recognised."
        ),
    )


class EditorSessionClosed(HTTPException):
    """The 401 of an editor session that cannot be used: ``detail`` is the
    text for *reason*, and the answer carries both (see
    :func:`editor_session_closed_handler`)."""

    def __init__(self, reason: str | None) -> None:
        self.reason = visual_editor.closed_reason(reason)
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=visual_editor.CLOSED_TEXTS[self.reason],
            headers={"WWW-Authenticate": "Bearer"},
        )


async def editor_session_closed_handler(
    request: Request, exc: EditorSessionClosed
) -> JSONResponse:
    """``{"detail": <text>, "reason": <code>}`` with ``WWW-Authenticate``."""
    return JSONResponse(
        {"detail": exc.detail, "reason": exc.reason},
        status_code=exc.status_code,
        headers=exc.headers,
    )


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
    the experiment's sites (the ticket stays spent); then the same 409 or 401
    if the link has been used again, ended or has expired meanwhile. Sent
    with `Cache-Control: no-store`.
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
        visual_editor.hold_editor(db, ticket.experiment_id)
        refusal = visual_editor.parent_refusal(db, ticket.id)
        if refusal is not None:
            db.rollback()
            raise refusal
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
    the experiment's sites; 422, as opening the editor answers, when
    CORS_ORIGINS contains `*` or one of the experiment's pages is no longer
    on a site this server accepts; 403 when the user is no longer active or
    may no longer change experiments; 400 when the experiment is no longer a
    draft, has a start date, or has no visual settings; then the same 409 or
    401 if the value has been ended or has expired meanwhile. After the 401
    every refusal leaves the value spent. Sent with `Cache-Control: no-store`.
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
        problem = visual_origin_problem(experiment.visual_config)
        if problem is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem
            )
        user = db.get(User, claim.user_id) if claim.user_id else None
        refuse_unless_allowed(experiment, user)
        visual_editor.refuse_unless_open_to_edit(experiment)

        actor = AuditActor.of(user)
        token = visual_editor.SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires_at = now + visual_editor.SESSION_LIFETIME
        session_id = uuid.uuid4()
        state = visual_editor.session_state(db, experiment, expires_at, user)
        name = experiment.name
        experiment_id = experiment.id
        visual_editor.hold_editor(db, experiment_id)
        refusal = visual_editor.parent_refusal(db, claim.id)
        if refusal is not None:
            raise refusal
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


# --- the session's routes ----------------------------------------------------------


@dataclass
class EditorSession:
    """The session a request is made in, as :func:`current_editor_session`
    found it."""

    id: uuid.UUID
    expires_at: datetime
    user: User
    experiment: Experiment


def current_editor_session(
    request: Request, db: Session = Depends(deps.get_db)
) -> EditorSession:
    """The editor session of the request, or the refusal, checked in this
    order on every call:

    1. 403 unless the ``Origin`` is a dashboard origin; a ``GET`` or ``HEAD``
       may have none.
    2. 401 ``not_recognised`` unless ``Authorization: Bearer cves_...`` names
       an editor session.
    3. 401 with the session's ``revoked_reason`` once it has been ended.
    4. 401 ``expired`` once its 4 hours are over.
    5. 403 unless its user is active and may still change experiments.
    6. 404 when the experiment no longer exists.
    7. 400 once the experiment is no longer a draft, or has a start date.

    It writes nothing.
    """
    row = _live_session_row(request, db)
    user = db.get(User, row.user_id) if row.user_id else None
    experiment = db.query(Experiment).filter(Experiment.id == row.experiment_id).first()
    refuse_unless_allowed(experiment, user)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=visual_editor.EXPERIMENT_GONE,
        )
    if experiment.status != ExperimentStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=visual_editor.NOT_A_DRAFT
        )
    if experiment.start_date is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=visual_editor.DATED_DRAFT
        )
    return EditorSession(
        id=row.id, expires_at=row.expires_at, user=user, experiment=experiment
    )


@dataclass
class SessionToSignOut:
    """The session a sign-out ends, as :func:`signing_out_editor_session`
    found it."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    experiment_id: uuid.UUID


def signing_out_editor_session(
    request: Request, db: Session = Depends(deps.get_db)
) -> SessionToSignOut:
    """The session to sign out: :func:`current_editor_session`'s steps 1-4
    only. A session signs out whatever has changed since it was opened: its
    user's status or role, the experiment's status or start date."""
    row = _live_session_row(request, db)
    return SessionToSignOut(
        id=row.id, user_id=row.user_id, experiment_id=row.experiment_id
    )


def _live_session_row(request: Request, db: Session) -> VisualToken:
    """Steps 1-4 of :func:`current_editor_session`: the row of the request's
    editor session, neither ended nor expired."""
    refusal = dashboard_origin_refusal(request.headers.get("origin"), request.method)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.startswith(
        visual_editor.SESSION_TOKEN_PREFIX
    ):
        raise EditorSessionClosed(visual_editor.NOT_RECOGNISED)
    row = (
        db.query(VisualToken)
        .filter(
            VisualToken.token_hash == hash_api_key(token),
            VisualToken.kind == EDITOR_SESSION,
        )
        .first()
    )
    if row is None:
        raise EditorSessionClosed(visual_editor.NOT_RECOGNISED)
    if row.revoked_at is not None:
        raise EditorSessionClosed(row.revoked_reason)
    if row.expires_at <= visual_editor.utcnow():
        raise EditorSessionClosed(visual_editor.EXPIRED)
    return row


async def editor_cache_control() -> deps.CacheControl:
    """The experiment cache as the experiment routes use it, without their
    ``skip_cache`` query parameter: the session's write always drops it."""
    return await deps.get_cache_control(skip_cache=False)


_SESSION_REFUSALS = {
    400: {"description": "The experiment is no longer a draft, or has a start date"},
    401: {
        "model": EditorSessionRefusal,
        "description": "The session cannot be used; reason says why",
    },
    403: {
        "description": (
            "The request does not come from the dashboard, or the user may no "
            "longer change the experiment"
        )
    },
    404: {"description": "The experiment no longer exists"},
}


@router.get(
    "/sessions/current",
    response_model=EditorSessionState,
    summary="The visual editor session and its draft (beta)",
    openapi_extra={"x-stability": "beta"},
    responses=_SESSION_REFUSALS,
)
def read_editor_session(
    response: Response,
    session: EditorSession = Depends(current_editor_session),
    db: Session = Depends(deps.get_db),
) -> EditorSessionState:
    """
    The session's end, the experiment's sites the editor may run on, and the
    draft's design as stored now: the session's answer without its token.

    The design is the draft's page changes, pages and editor page, its
    audience (`targeting_rules`), each variant's share of the traffic, its
    metrics (each as the experiment's own answer gives it), its goals, and
    its exclusion group. `holdout` is the active global holdout, shown only
    to a user who may create experiments. `capabilities` says which other
    settings the editor may offer: none yet. Sent with
    `Cache-Control: no-store`.
    """
    response.headers["Cache-Control"] = "no-store"
    return EditorSessionState(
        **visual_editor.session_state(
            db, session.experiment, session.expires_at, session.user
        )
    )


@router.get(
    "/sessions/current/segments",
    response_model=List[EditorSegment],
    summary="The segments the visual editor may choose from (beta)",
    openapi_extra={"x-stability": "beta"},
    responses={
        **_SESSION_REFUSALS,
        403: {
            "description": (
                "The request does not come from the dashboard, or the user may "
                "not list experiments or may no longer change the experiment"
            )
        },
    },
)
def list_editor_segments(
    response: Response,
    session: EditorSession = Depends(current_editor_session),
    db: Session = Depends(deps.get_db),
) -> List[EditorSegment]:
    """
    Every active segment, by id, name and kind, for the editor's audience
    step. `rules_valid` says whether a segment can be used in an audience:
    true for an id_list segment, and for a rules segment whose stored rules
    are in the targeting rule format. Neither a segment's rules nor its
    members are sent. Needs permission to list experiments, as
    `GET /api/v1/segments` does. Sent with `Cache-Control: no-store`.
    """
    segments_endpoints._require_permission(session.user, Action.LIST)
    rows = (
        db.query(Segment)
        .filter(Segment.status == SegmentStatus.ACTIVE)
        .order_by(Segment.created_at, Segment.id)
        .all()
    )
    response.headers["Cache-Control"] = "no-store"
    return [
        EditorSegment(
            id=row.id,
            name=row.name,
            kind=row.kind,
            rules_valid=_rules_valid(row),
        )
        for row in rows
    ]


def _rules_valid(segment: Segment) -> bool:
    """Whether *segment* can be used in an audience: an id list always, a
    rules segment when its stored rules pass ``validate_segment_rules``."""
    if segment.kind == SegmentKind.ID_LIST.value:
        return True
    try:
        validate_segment_rules(segment.rules)
    except TargetingRulesError:
        return False
    return True


@router.get(
    "/sessions/current/exclusion-groups",
    response_model=List[EditorExclusionGroup],
    summary="The exclusion groups the visual editor may choose from (beta)",
    openapi_extra={"x-stability": "beta"},
    responses={
        **_SESSION_REFUSALS,
        403: {
            "description": (
                "The request does not come from the dashboard, or the user may "
                "not create experiments or may no longer change the experiment"
            )
        },
    },
)
def list_editor_exclusion_groups(
    response: Response,
    session: EditorSession = Depends(current_editor_session),
    db: Session = Depends(deps.get_db),
) -> List[EditorExclusionGroup]:
    """
    Every active mutual exclusion group, by id and name, for the editor's
    split step. Needs permission to create experiments, as
    `GET /api/v1/mutual-exclusion-groups` does. Sent with
    `Cache-Control: no-store`.
    """
    exclusion_groups_endpoints._require_developer(session.user)
    rows = (
        db.query(MutualExclusionGroup)
        .filter(MutualExclusionGroup.status == MutualExclusionGroupStatus.ACTIVE)
        .order_by(MutualExclusionGroup.created_at, MutualExclusionGroup.id)
        .all()
    )
    response.headers["Cache-Control"] = "no-store"
    return [EditorExclusionGroup(id=row.id, name=row.name) for row in rows]


@router.delete(
    "/sessions/current",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of the visual editor (beta)",
    openapi_extra={"x-stability": "beta"},
    responses={
        401: _SESSION_REFUSALS[401],
        403: {"description": "The request does not come from the dashboard"},
    },
)
def sign_out_of_editor(
    session: SessionToSignOut = Depends(signing_out_editor_session),
    db: Session = Depends(deps.get_db),
) -> Response:
    """
    End the session: its token answers 401 `signed_out` from then on. The
    saved design is kept. Only the first four checks of every session call
    apply (the `Origin`, the token, the session's end and its expiry), so a
    session signs out even once its user may no longer change experiments,
    and of a draft that has since been given a start date, or is no longer a
    draft.
    """
    user = db.get(User, session.user_id) if session.user_id else None
    actor = AuditActor.of(user)
    experiment_id = session.experiment_id
    name = db.query(Experiment.name).filter(Experiment.id == experiment_id).scalar()
    try:
        ended = [
            visual_editor.Ended(*row)
            for row in db.execute(
                update(VisualToken)
                .where(
                    VisualToken.id == session.id,
                    VisualToken.revoked_at.is_(None),
                )
                .values(revoked_at=visual_editor.utcnow(), revoked_reason=SIGNED_OUT)
                .returning(VisualToken.id, VisualToken.kind, VisualToken.user_id)
                .execution_options(synchronize_session=False)
            ).all()
        ]
        db.commit()
    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Visual editor sign-out",
            "Could not sign out of the visual editor",
            db=db,
            logger=logger,
        )
    visual_editor.audit_sessions_closed(
        db,
        ended,
        actor=actor,
        reason=SIGNED_OUT,
        experiment_id=experiment_id,
        experiment_name=name,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _variant_body(variant, changes: VisualChanges | None) -> VariantBase:
    """A stored variant as an update sends it. Its page changes are sent only
    for the variant being saved; every other variant keeps the ones stored
    for its name."""
    fields = {
        "name": variant.name,
        "description": variant.description,
        "is_control": bool(variant.is_control),
        "traffic_allocation": variant.traffic_allocation,
        "configuration": variant.configuration,
    }
    if changes is not None:
        fields["visual_changes"] = changes
    return VariantBase(**fields)


@router.put(
    "/sessions/current/variants/{variant_id}/changes",
    response_model=EditorSessionState,
    summary="Save one variant's page changes from the visual editor (beta)",
    openapi_extra={"x-stability": "beta"},
    responses={
        **_SESSION_REFUSALS,
        404: {
            "description": (
                "The experiment no longer exists, or has no variant with that id"
            )
        },
        422: {
            "description": (
                "The page changes are refused, as an experiment update refuses them"
            )
        },
    },
)
async def save_variant_changes(
    response: Response,
    variant_id: uuid.UUID = Path(..., description="One of the draft's variants"),
    changes: VisualChanges = Body(...),
    session: EditorSession = Depends(current_editor_session),
    db: Session = Depends(deps.get_db),
    cache_control: deps.CacheControl = Depends(editor_cache_control),
) -> EditorSessionState:
    """
    Replace one variant's page changes, keeping every other variant and its
    changes as stored. It is the experiment update's own write, so it refuses
    what an update refuses (the control's changes among them), records one
    "Experiment changed" entry by the session's user with the reason
    `visual editor session <first 8 characters of the session's id>`, and
    drops the cached experiment. The variants are saved again, so each gets a
    new id; the answer is the session's, with the new ids. Sent with
    `Cache-Control: no-store`.
    """
    experiment = session.experiment
    stored = list(experiment.variants)
    if not any(variant.id == variant_id for variant in stored):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=visual_editor.VARIANT_NOT_FOUND,
        )
    update_in = ExperimentUpdate(
        variants=[
            _variant_body(variant, changes if variant.id == variant_id else None)
            for variant in stored
        ]
    )
    experiment_id = experiment.id
    await experiments_endpoints._apply_experiment_update(
        db,
        experiment_id,
        update_in,
        session.user,
        cache_control,
        audit_reason=f"visual editor session {session.id.hex[:8]}",
    )
    # The variants were saved again: read the row as it is now.
    db.expire_all()
    saved = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    response.headers["Cache-Control"] = "no-store"
    return EditorSessionState(
        **visual_editor.session_state(db, saved, session.expires_at, session.user)
    )


def _design_update(
    experiment: Experiment, change: EditorDesignChange, shares
) -> ExperimentUpdate | None:
    """The experiment update for the parts of *change* that were sent, built
    as a dashboard save would send them, or ``None`` when none was. Every
    variant is sent with all it stores, only its share taken from *shares*,
    and without its page changes, so it keeps the ones stored for its name;
    the goals are sent inside the stored visual settings. Anything the
    update refuses here is a 422 in the request validation's own shape."""
    sent = change.model_fields_set
    fields = {}
    if "targeting_rules" in sent:
        fields["targeting_rules"] = change.targeting_rules
    if "metrics" in sent:
        fields["metrics"] = change.metrics
    try:
        if "variants" in sent:
            fields["variants"] = [
                _variant_body(variant, None).model_copy(
                    update={"traffic_allocation": shares[variant.id]}
                )
                for variant in experiment.variants
            ]
        if "goals" in sent:
            fields["visual_config"] = {
                **(experiment.visual_config or {}),
                "goals": [goal.model_dump(mode="json") for goal in change.goals],
            }
        if not fields:
            return None
        return ExperimentUpdate(**fields)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", *error["loc"])}
                for error in exc.errors(include_url=False)
            ]
        ) from None


_DESIGN_REFUSALS = {
    **_SESSION_REFUSALS,
    403: {
        "description": (
            "The request does not come from the dashboard, the user may no "
            "longer change the experiment, or the exclusion group changes and "
            "the user may not create experiments"
        )
    },
    409: {
        "description": (
            "The design changed after the editor loaded it; nothing was saved"
        )
    },
    422: {
        "description": (
            "The request is not in the documented form, the split does not "
            "name each variant once, the exclusion group does not exist or is "
            "archived, or the change is refused as an experiment update "
            "refuses it"
        )
    },
}


@router.patch(
    "/sessions/current/experiment",
    response_model=EditorSessionState,
    summary="Save a draft's audience, split, metrics and goals from the visual editor (beta)",
    openapi_extra={"x-stability": "beta"},
    responses=_DESIGN_REFUSALS,
)
async def save_experiment_design(
    response: Response,
    change: EditorDesignChange = Body(...),
    session: EditorSession = Depends(current_editor_session),
    db: Session = Depends(deps.get_db),
    cache_control: deps.CacheControl = Depends(editor_cache_control),
) -> EditorSessionState:
    """
    Save the parts of the draft's design the request sends: who can join
    (`targeting_rules`), the split (`variants`), the metrics, the goals and
    the exclusion group. A part left out is not changed, and neither is
    anything else: each variant's name, description, configuration and page
    changes, the visual settings' pages and editor page, the split's balance
    and the analysis settings.

    Checked before anything is written, in this order, after the session's
    own checks: 422 when the request is not in the documented form (any
    other key, `start_date` and `balance_within` included) or the split
    does not name each of the draft's variants exactly once; 409 when
    `design_etag` is not the design's current version; when the exclusion
    group changes, 403 unless the user may create experiments, then 422
    unless the group exists and is active; then 422 for anything an
    experiment update refuses, with its `detail`.

    The design is saved as the experiment update's own write: one
    "Experiment changed" entry by the session's user with the reason
    `visual editor session <first 8 characters of the session's id>`, and
    the cached experiment is dropped. A changed exclusion group is then
    saved as the exclusion group routes save it, with their entry and the
    same reason. A request with only `design_etag` saves nothing. The answer
    is the session's, with the new `design_etag` (and, when the split was
    saved, the variants' new ids). Sent with `Cache-Control: no-store`.
    """
    experiment = session.experiment
    sent = change.model_fields_set
    shares = None
    if "variants" in sent:
        shares = visual_editor.shares_by_variant(experiment.variants, change.variants)
    if change.design_etag != design_etag(experiment):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=visual_editor.DESIGN_CHANGED
        )
    group_change = None
    if "mutual_exclusion_group_id" in sent and (
        change.mutual_exclusion_group_id != experiment.mutual_exclusion_group_id
    ):
        exclusion_groups_endpoints._require_developer(session.user)
        wanted = change.mutual_exclusion_group_id
        if wanted is not None:
            group = db.get(MutualExclusionGroup, wanted)
            if group is None or group.status != MutualExclusionGroupStatus.ACTIVE:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=visual_editor.GROUP_NOT_AVAILABLE,
                )
        group_change = (experiment.mutual_exclusion_group_id, wanted)
    update_in = _design_update(experiment, change, shares)

    experiment_id = experiment.id
    reason = f"visual editor session {session.id.hex[:8]}"
    if update_in is not None:
        await experiments_endpoints._apply_experiment_update(
            db,
            experiment_id,
            update_in,
            session.user,
            cache_control,
            audit_reason=reason,
        )
    if group_change is not None:
        await _change_group(db, session.user, experiment_id, *group_change, reason)

    db.expire_all()
    saved = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    response.headers["Cache-Control"] = "no-store"
    return EditorSessionState(
        **visual_editor.session_state(db, saved, session.expires_at, session.user)
    )


async def _change_group(
    db: Session, user: User, experiment_id, leaving, joining, reason: str
) -> None:
    """Move the experiment from the group *leaving* to the group *joining*
    (either may be ``None``) as the exclusion group routes do: through
    ``MutualExclusionService``, one entry per group changed with *reason*,
    and the cached experiment dropped once anything is committed."""
    service = MutualExclusionService(db)
    moved = False
    failure = None
    try:
        if leaving is not None:
            service.remove_experiment_from_group(leaving, experiment_id)
            moved = True
            exclusion_groups_endpoints._record(
                db,
                user,
                ActionType.MUTUAL_EXCLUSION_GROUP_UPDATE,
                leaving,
                exclusion_groups_endpoints._group_name(db, leaving),
                before={"experiment_id": str(experiment_id)},
                reason=reason,
            )
        if joining is not None:
            service.add_experiment_to_group(joining, experiment_id)
            moved = True
            exclusion_groups_endpoints._record(
                db,
                user,
                ActionType.MUTUAL_EXCLUSION_GROUP_UPDATE,
                joining,
                exclusion_groups_endpoints._group_name(db, joining),
                after={"experiment_id": str(experiment_id)},
                reason=reason,
            )
    except Exception as exc:
        failure = unexpected_failure(
            exc,
            "Visual editor exclusion group change",
            "Could not change the experiment's exclusion group",
            db=db,
            logger=logger,
        )
    if moved:
        await experiment_cache.drop_cached_experiment_details([experiment_id])
    if failure is not None:
        raise failure

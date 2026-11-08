"""
Workspace API endpoints for EP-057: Multi-Tenant Team Workspaces.

Provides REST endpoints for workspace CRUD, member management and the
invite lifecycle. Workspaces have no plan and no limits, and issue no API
keys: SDKs and integrations use platform API keys (``/api/v1/api-keys``).
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_active_user, get_current_user, get_db
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.security import oauth2_scheme
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User
from backend.app.services.audit_service import AuditActor, AuditService
from modules.backend.app.models.workspace import (
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceMemberRole,
)
from modules.backend.app.schemas.workspaces import (
    AddMemberRequest,
    CreateInviteRequest,
    CreateWorkspaceRequest,
    InviteEmailMismatchResponse,
    UpdateMemberRoleRequest,
    UpdateWorkspaceRequest,
    WorkspaceInviteResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
    WorkspaceWithStatsResponse,
)
from modules.backend.app.services.workspace_service import (
    INVITE_EMAIL_MISMATCH_CODE,
    INVITE_EMAIL_MISMATCH_MESSAGE,
    AlreadyMember,
    CannotDemoteLastOwner,
    CannotRemoveLastOwner,
    InviteAlreadyAccepted,
    InviteEmailMismatch,
    InviteExpired,
    InviteNotFound,
    WorkspaceMemberNotFound,
    WorkspaceNotFound,
    WorkspaceSlugInvalid,
    WorkspaceSlugTaken,
    invite_email_for_viewer,
    invite_email_matches,
    workspace_service,
)

router = APIRouter(route_class=OffLoopRoute)

#: The invite preview: a recipient who may not have an account yet looks the
#: invitation up by its token before accepting.  Sign-in is optional (it only
#: decides whether the invited address is shown in full), so the registration
#: mounts it behind nothing.
public_router = APIRouter(route_class=OffLoopRoute)

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_workspace_or_404(db: Session, workspace_id: uuid.UUID) -> Workspace:
    """Return workspace or raise 404."""
    try:
        return workspace_service.get_workspace(db, workspace_id)
    except WorkspaceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _require_member(
    db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember:
    """Return the member record or raise 403."""
    member = workspace_service.get_member(db, workspace_id, user_id)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this workspace.",
        )
    return member


def _require_role(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    minimum_role: str,
) -> None:
    """Raise 403 unless the user holds at least minimum_role."""
    if not workspace_service.check_member_permission(
        db, workspace_id, user_id, minimum_role
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {minimum_role} role or above.",
        )


def _require_owner_for_owner_role(
    db: Session,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Raise 403 unless the user is an OWNER of the workspace.

    Called before any change that grants the OWNER role, or that changes or
    removes the membership of a member who holds it: per the role table, only
    an OWNER may do those.
    """
    if not workspace_service.check_member_permission(
        db, workspace_id, user_id, "OWNER"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an OWNER can grant the OWNER role or change an OWNER's membership.",
        )


def _member_to_response(member: WorkspaceMember) -> WorkspaceMemberResponse:
    """Serialize a WorkspaceMember, hydrating user fields if loaded."""
    username = None
    email = None
    if member.user is not None:
        username = member.user.username
        email = member.user.email
    return WorkspaceMemberResponse(
        workspace_id=str(member.workspace_id),
        user_id=str(member.user_id),
        role=member.role.value,
        joined_at=member.joined_at,
        username=username,
        email=email,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workspace CRUD
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: CreateWorkspaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a new workspace. The authenticated user becomes the OWNER."""
    try:
        workspace = workspace_service.create_workspace(
            db=db,
            name=payload.name,
            slug=payload.slug,
            owner_id=current_user.id,
            description=payload.description,
        )
    except WorkspaceSlugInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except WorkspaceSlugTaken as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return WorkspaceResponse(
        id=str(workspace.id),
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        is_active=workspace.is_active,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


@router.get("/", response_model=List[WorkspaceWithStatsResponse])
def list_my_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all workspaces the current user belongs to, each with its member count."""
    workspaces = workspace_service.list_user_workspaces(db, current_user.id)
    counts = workspace_service.member_counts(db, [w.id for w in workspaces])
    return [
        WorkspaceWithStatsResponse(
            id=str(w.id),
            name=w.name,
            slug=w.slug,
            description=w.description,
            is_active=w.is_active,
            created_at=w.created_at,
            updated_at=w.updated_at,
            member_count=counts.get(w.id, 0),
        )
        for w in workspaces
    ]


@router.get("/{workspace_id}", response_model=WorkspaceWithStatsResponse)
def get_workspace(
    workspace_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get workspace details and stats. Caller must be a member."""
    workspace = _get_workspace_or_404(db, workspace_id)
    _require_member(db, workspace_id, current_user.id)

    try:
        stats = workspace_service.get_workspace_stats(db, workspace_id)
    except Exception:
        stats = None

    return WorkspaceWithStatsResponse(
        id=str(workspace.id),
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        is_active=workspace.is_active,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        member_count=stats.member_count if stats else 0,
    )


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
def update_workspace(
    workspace_id: uuid.UUID = Path(...),
    payload: UpdateWorkspaceRequest = ...,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update workspace. Requires ADMIN role or above."""
    _get_workspace_or_404(db, workspace_id)
    _require_role(db, workspace_id, current_user.id, "ADMIN")

    workspace = workspace_service.update_workspace(
        db,
        workspace_id,
        payload.model_dump(exclude_none=True),
    )
    return WorkspaceResponse(
        id=str(workspace.id),
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        is_active=workspace.is_active,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(
    workspace_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete workspace. Requires OWNER role."""
    _get_workspace_or_404(db, workspace_id)
    _require_role(db, workspace_id, current_user.id, "OWNER")
    workspace_service.delete_workspace(db, workspace_id)


# ─────────────────────────────────────────────────────────────────────────────
# Members
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/{workspace_id}/members", response_model=List[WorkspaceMemberResponse])
def list_members(
    workspace_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List workspace members. Any member may call this."""
    _get_workspace_or_404(db, workspace_id)
    _require_member(db, workspace_id, current_user.id)
    members = workspace_service.list_members(db, workspace_id)
    return [_member_to_response(m) for m in members]


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    workspace_id: uuid.UUID = Path(...),
    payload: AddMemberRequest = ...,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Add an existing user as a workspace member. Requires ADMIN role."""
    _get_workspace_or_404(db, workspace_id)
    _require_role(db, workspace_id, current_user.id, "ADMIN")
    if payload.role == "OWNER":
        _require_owner_for_owner_role(db, workspace_id, current_user.id)

    try:
        user_uuid = uuid.UUID(payload.user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid user_id UUID format.",
        )

    try:
        member = workspace_service.add_member(
            db,
            workspace_id=workspace_id,
            user_id=user_uuid,
            role=payload.role,
            invited_by=current_user.id,
        )
    except AlreadyMember as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return _member_to_response(member)


@router.put(
    "/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberResponse,
)
def update_member_role(
    workspace_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
    payload: UpdateMemberRoleRequest = ...,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Change a member's role. Requires ADMIN role."""
    _get_workspace_or_404(db, workspace_id)
    _require_role(db, workspace_id, current_user.id, "ADMIN")
    # Granting OWNER, or changing the role of a member who holds it, is for
    # an OWNER only.
    target = workspace_service.get_member(db, workspace_id, user_id)
    target_is_owner = target is not None and target.role == WorkspaceMemberRole.OWNER
    if payload.role == "OWNER" or target_is_owner:
        _require_owner_for_owner_role(db, workspace_id, current_user.id)

    actor = AuditActor.of(current_user)
    old_role = target.role.value if target is not None else None
    try:
        member = workspace_service.update_member_role(
            db, workspace_id, user_id, payload.role
        )
    except WorkspaceMemberNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except CannotDemoteLastOwner as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    response = _member_to_response(member)
    if old_role is not None and old_role != member.role.value:
        # After the change has committed: a failed entry is logged and does
        # not undo it. Only an actual change is recorded.
        AuditService.record_after_commit(
            db,
            actor=actor,
            action=ActionType.ROLE_ASSIGN,
            entity_type=EntityType.USER,
            entity_id=user_id,
            entity_name=response.username or str(user_id),
            before={"workspace_id": str(workspace_id), "workspace_role": old_role},
            after={
                "workspace_id": str(workspace_id),
                "workspace_role": member.role.value,
            },
            reason="workspace role changed",
        )
    return response


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    workspace_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Remove a member. ADMIN+ can remove anyone; members can remove themselves."""
    _get_workspace_or_404(db, workspace_id)

    is_self = current_user.id == user_id
    is_admin = workspace_service.check_member_permission(
        db, workspace_id, current_user.id, "ADMIN"
    )
    if not is_self and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires ADMIN role or above to remove other members.",
        )
    # Removing an OWNER other than yourself is for an OWNER only.
    if not is_self:
        target = workspace_service.get_member(db, workspace_id, user_id)
        if target is not None and target.role == WorkspaceMemberRole.OWNER:
            _require_owner_for_owner_role(db, workspace_id, current_user.id)

    try:
        workspace_service.remove_member(db, workspace_id, user_id)
    except WorkspaceMemberNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except CannotRemoveLastOwner as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invites
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{workspace_id}/invites",
    response_model=WorkspaceInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invite(
    workspace_id: uuid.UUID = Path(...),
    payload: CreateInviteRequest = ...,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a workspace invite for an email address and return its token.

    No email is sent. Requires ADMIN role.
    """
    _get_workspace_or_404(db, workspace_id)
    _require_role(db, workspace_id, current_user.id, "ADMIN")

    invite = workspace_service.create_invite(
        db,
        workspace_id=workspace_id,
        email=payload.email,
        role=payload.role,
        invited_by=current_user.id,
    )
    return _invite_to_response(
        invite, email=invite.email, inviter_username=_inviter_username(invite)
    )


def _inviter_username(invite: WorkspaceInvite) -> Optional[str]:
    """The inviting account's username, or ``None`` when it no longer exists."""
    inviter = invite.inviter
    return inviter.username if inviter is not None else None


def _invite_to_response(
    invite: WorkspaceInvite, *, email: str, inviter_username: Optional[str]
) -> WorkspaceInviteResponse:
    """The API shape of an invite, with ``email`` and ``inviter_username``
    as the caller decided the viewer may see them."""
    return WorkspaceInviteResponse(
        id=str(invite.id),
        workspace_id=str(invite.workspace_id),
        email=email,
        role=invite.role.value,
        token=invite.token,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        workspace_name=invite.workspace.name,
        inviter_username=inviter_username,
    )


def _invite_viewer(
    bearer: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """The signed-in account looking at an invitation, or ``None``.

    Sign-in is optional here: no credentials, credentials that do not resolve
    to an account, and an inactive account are all ``None`` rather than an
    error, so the preview answers the same way with or without them.
    """
    try:
        user = get_current_user(token=bearer, db=db)
    except HTTPException:
        # This request's session is the one get_current_user used, and it can
        # write (a first Cognito sign-in creates the user row). If that write
        # failed, the session has to be rolled back before the invite lookup
        # uses it.
        db.rollback()
        return None
    return user if user.is_active else None


@public_router.get(
    "/invites/{token}",
    response_model=WorkspaceInviteResponse,
    # Sign-in is optional: the empty entry beside the bearer scheme says so.
    openapi_extra={"security": [{}]},
)
def get_invite(
    response: Response,
    token: str = Path(...),
    db: Session = Depends(get_db),
    viewer: Optional[User] = Depends(_invite_viewer),
):
    """Get invite info by token. No sign-in needed.

    `email` is returned in full only to the account it was sent to (compared
    without regard to case); anyone else, signed in or not, gets it masked,
    e.g. `a•••@example.com`.
    """
    try:
        invite = workspace_service.get_invite_by_token(db, token)
    except InviteNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    # The body depends on who is asking, so no shared cache may keep it.
    response.headers["Cache-Control"] = "private, no-store"
    viewer_email = viewer.email if viewer is not None else None
    # The inviter's username goes only to the invitee, by the same rule that
    # decides whether `email` is shown in full.
    is_invitee = invite_email_matches(invite.email, viewer_email)
    return _invite_to_response(
        invite,
        email=invite_email_for_viewer(invite.email, viewer_email),
        inviter_username=_inviter_username(invite) if is_invitee else None,
    )


@router.post(
    "/invites/{token}/accept",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {
            "model": InviteEmailMismatchResponse,
            "description": (
                "The signed-in account is not the invited address "
                '(`detail.code` is `"invite_email_mismatch"`).'
            ),
        }
    },
)
def accept_invite(
    token: str = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Accept a workspace invite. The current user is added as a member.

    Only the account whose email is the invited address may accept (compared
    without regard to case); any other account gets 403 with code
    ``invite_email_mismatch``.
    """
    try:
        member = workspace_service.accept_invite(
            db, token, current_user.id, current_user.email
        )
    except InviteNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InviteEmailMismatch:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": INVITE_EMAIL_MISMATCH_CODE,
                "message": INVITE_EMAIL_MISMATCH_MESSAGE,
            },
        )
    except InviteExpired as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except InviteAlreadyAccepted as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except AlreadyMember as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    return _member_to_response(member)

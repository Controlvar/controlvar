"""
WorkspaceService for EP-057: Multi-Tenant Team Workspaces.

Handles all business logic for workspace CRUD, membership management and
the invite lifecycle. Workspaces have no plan and no limits, and issue no API
keys.
"""

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from modules.backend.app.models.workspace import (
    ROLE_HIERARCHY,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceMemberRole,
)

# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────


class WorkspaceError(Exception):
    """Base exception for workspace service errors."""


class WorkspaceNotFound(WorkspaceError):
    """Raised when a workspace cannot be found."""


class WorkspaceMemberNotFound(WorkspaceError):
    """Raised when a workspace member record cannot be found."""


class WorkspacePermissionError(WorkspaceError):
    """Raised when the caller lacks the required workspace role."""


class WorkspaceSlugTaken(WorkspaceError):
    """Raised when the requested slug is already in use."""


class WorkspaceSlugInvalid(WorkspaceError):
    """Raised when the slug does not match the allowed character pattern."""


class AlreadyMember(WorkspaceError):
    """Raised when a user is already a member of the workspace."""


class CannotRemoveLastOwner(WorkspaceError):
    """Raised when attempting to remove the only owner of a workspace."""


class CannotDemoteLastOwner(WorkspaceError):
    """Raised when an owner attempts to remove their own OWNER role."""


class InviteNotFound(WorkspaceError):
    """Raised when an invite token cannot be found."""


class InviteExpired(WorkspaceError):
    """Raised when attempting to accept an expired invite."""


class InviteAlreadyAccepted(WorkspaceError):
    """Raised when attempting to accept an already-accepted invite."""


#: The typed code and fixed message of a refused accept. The message names
#: neither address.
INVITE_EMAIL_MISMATCH_CODE = "invite_email_mismatch"
INVITE_EMAIL_MISMATCH_MESSAGE = "This invite was sent to a different email address."


class InviteEmailMismatch(WorkspaceError):
    """Raised when the accepting account's email is not the invited address."""

    def __init__(self) -> None:
        super().__init__(INVITE_EMAIL_MISMATCH_MESSAGE)


def _comparable_email(value: Optional[str]) -> Optional[str]:
    """An address as invites compare it: trimmed, ASCII, lower-cased.

    ``None`` when there is no address or it is not ASCII. ASCII is checked
    before lower-casing, on the value as stored: ``str.lower`` folds some
    non-ASCII characters into ASCII ones (U+212A KELVIN SIGN becomes ``k``), so
    lower-casing first would make a different address compare equal. The same
    rule as ``_sso_email`` in the SSO service.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or not trimmed.isascii():
        return None
    return trimmed.lower()


def invite_email_matches(
    invite_email: Optional[str], user_email: Optional[str]
) -> bool:
    """Whether ``user_email`` is the address ``invite_email`` was sent to.

    Both must be present and ASCII after trimming, and equal after
    lower-casing. No alias rules: ``a+x@`` and ``a@`` are different addresses.
    """
    invited = _comparable_email(invite_email)
    accepting = _comparable_email(user_email)
    return invited is not None and accepting is not None and invited == accepting


#: What stands in for the hidden part of a masked address.
INVITE_EMAIL_MASK = "•••"


def mask_invite_email(email: Optional[str]) -> str:
    """``alice@example.com`` -> ``a•••@example.com``.

    The first character and everything from the last ``@`` are kept; an
    address with no ``@``, or nothing before it, becomes ``•••``. Masking a
    masked address returns it unchanged. The invite page's ``maskEmail`` does
    the same.
    """
    trimmed = email.strip() if isinstance(email, str) else ""
    at = trimmed.rfind("@")
    if at <= 0:
        return INVITE_EMAIL_MASK
    return trimmed[0] + INVITE_EMAIL_MASK + trimmed[at:]


def invite_email_for_viewer(invite_email: str, viewer_email: Optional[str]) -> str:
    """The invited address as the invitation preview returns it to a viewer.

    In full, as stored, when ``viewer_email`` is the invited address under
    :func:`invite_email_matches` (the rule accepting uses); masked with
    :func:`mask_invite_email` for anyone else, including no viewer at all.
    """
    if invite_email_matches(invite_email, viewer_email):
        return invite_email
    return mask_invite_email(invite_email)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class WorkspaceStats:
    """Aggregated statistics for a workspace."""

    member_count: int


# ─────────────────────────────────────────────────────────────────────────────
# Slug helpers
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$")


def _validate_slug(slug: str) -> None:
    """Raise WorkspaceSlugInvalid if slug is not URL-safe."""
    # fullmatch, not match: "$" also matches before a final newline.
    if not _SLUG_RE.fullmatch(slug):
        raise WorkspaceSlugInvalid(
            "Slug must be 3–50 characters, lowercase alphanumeric with hyphens, "
            "and must not start or end with a hyphen."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────


class WorkspaceService:
    """Business logic layer for workspace management."""

    # ── Workspace CRUD ────────────────────────────────────────────────────────

    def create_workspace(
        self,
        db: Session,
        name: str,
        slug: str,
        owner_id: uuid.UUID,
        description: str = "",
    ) -> Workspace:
        """Create a new workspace and add the creator as OWNER."""
        _validate_slug(slug)

        # Check slug uniqueness
        existing = db.query(Workspace).filter(Workspace.slug == slug).first()
        if existing:
            raise WorkspaceSlugTaken(f"Slug '{slug}' is already in use.")

        workspace = Workspace(
            name=name,
            slug=slug,
            description=description,
            is_active=True,
        )
        db.add(workspace)
        db.flush()  # get workspace.id before adding member

        # Add owner as first member
        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=owner_id,
            role=WorkspaceMemberRole.OWNER,
            invited_by=None,
            joined_at=datetime.utcnow(),
        )
        db.add(member)
        db.commit()
        db.refresh(workspace)
        return workspace

    def get_workspace(self, db: Session, workspace_id: uuid.UUID) -> Workspace:
        """Return a workspace by ID or raise WorkspaceNotFound."""
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            raise WorkspaceNotFound(f"Workspace {workspace_id} not found.")
        return workspace

    def get_workspace_by_slug(self, db: Session, slug: str) -> Workspace:
        """Return a workspace by slug or raise WorkspaceNotFound."""
        workspace = db.query(Workspace).filter(Workspace.slug == slug).first()
        if not workspace:
            raise WorkspaceNotFound(f"Workspace with slug '{slug}' not found.")
        return workspace

    def list_user_workspaces(self, db: Session, user_id: uuid.UUID) -> List[Workspace]:
        """Return all workspaces the user is a member of."""
        members = (
            db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user_id).all()
        )
        workspace_ids = [m.workspace_id for m in members]
        if not workspace_ids:
            return []
        return db.query(Workspace).filter(Workspace.id.in_(workspace_ids)).all()

    def update_workspace(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        data: dict,
    ) -> Workspace:
        """Update workspace fields. Only name and description are mutable."""
        workspace = self.get_workspace(db, workspace_id)

        allowed = {"name", "description"}
        for key, value in data.items():
            if key not in allowed:
                continue
            setattr(workspace, key, value)

        db.commit()
        db.refresh(workspace)
        return workspace

    def delete_workspace(self, db: Session, workspace_id: uuid.UUID) -> None:
        """Delete a workspace, its members, invites and API keys.

        Experiments and feature flags are **not** deleted: they are core rows
        that merely carried this workspace's id, and they return to the
        unscoped pool.

        That used to happen by itself, through the database's
        ``ON DELETE SET NULL`` on ``experiments.workspace_id`` and
        ``feature_flags.workspace_id``.  The seam dropped those two
        constraints (migration ``a7b8c9d0e1f2``) because they were the entire
        ORM coupling between a core table and a module's, so nothing clears
        the column any more unless this does: a deleted workspace would
        otherwise leave rows pointing at an id that no longer exists.  The
        members, invites and API keys still cascade -- those constraints are
        between the module's own tables and were never touched.
        """
        workspace = self.get_workspace(db, workspace_id)

        # Imported here rather than at module scope: these are the core
        # models, and the import direction matters more than the microsecond.
        from backend.app.models.experiment import Experiment
        from backend.app.models.feature_flag import FeatureFlag

        for model in (Experiment, FeatureFlag):
            db.query(model).filter(model.workspace_id == workspace_id).update(
                {"workspace_id": None}, synchronize_session=False
            )

        db.delete(workspace)
        db.commit()

    # ── Members ───────────────────────────────────────────────────────────────

    def add_member(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        invited_by: uuid.UUID,
    ) -> WorkspaceMember:
        """Add an existing user as a member of the workspace.

        Workspaces have no member limit.
        """
        self.get_workspace(db, workspace_id)  # existence check

        # Check for existing membership
        existing = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )
        if existing:
            raise AlreadyMember(
                f"User {user_id} is already a member of this workspace."
            )

        try:
            role_enum = WorkspaceMemberRole[role.upper()]
        except KeyError:
            role_enum = WorkspaceMemberRole.VIEWER

        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role_enum,
            invited_by=invited_by,
            joined_at=datetime.utcnow(),
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return member

    def remove_member(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Remove a user from the workspace."""
        member = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )
        if not member:
            raise WorkspaceMemberNotFound(
                f"User {user_id} is not a member of workspace {workspace_id}."
            )

        # Prevent removing the last owner
        if member.role == WorkspaceMemberRole.OWNER:
            owner_count = (
                db.query(WorkspaceMember)
                .filter(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == WorkspaceMemberRole.OWNER,
                )
                .count()
            )
            if owner_count <= 1:
                raise CannotRemoveLastOwner(
                    "Cannot remove the last owner of a workspace."
                )

        db.delete(member)
        db.commit()

    def update_member_role(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        new_role: str,
    ) -> WorkspaceMember:
        """Change a member's role within the workspace."""
        member = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )
        if not member:
            raise WorkspaceMemberNotFound(
                f"User {user_id} is not a member of workspace {workspace_id}."
            )

        # Prevent an owner from demoting themselves if they are the last owner
        if member.role == WorkspaceMemberRole.OWNER and new_role.upper() != "OWNER":
            owner_count = (
                db.query(WorkspaceMember)
                .filter(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == WorkspaceMemberRole.OWNER,
                )
                .count()
            )
            if owner_count <= 1:
                raise CannotDemoteLastOwner(
                    "Cannot demote the last owner of a workspace."
                )

        try:
            member.role = WorkspaceMemberRole[new_role.upper()]
        except KeyError:
            member.role = WorkspaceMemberRole.VIEWER

        db.commit()
        db.refresh(member)
        return member

    def list_members(
        self, db: Session, workspace_id: uuid.UUID
    ) -> List[WorkspaceMember]:
        """Return all members of a workspace."""
        return (
            db.query(WorkspaceMember)
            .filter(WorkspaceMember.workspace_id == workspace_id)
            .all()
        )

    # ── Invites ───────────────────────────────────────────────────────────────

    def create_invite(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        email: str,
        role: str,
        invited_by: uuid.UUID,
    ) -> WorkspaceInvite:
        """Create a 7-day invite token for the given email address."""
        self.get_workspace(db, workspace_id)  # existence check

        try:
            role_enum = WorkspaceMemberRole[role.upper()]
        except KeyError:
            role_enum = WorkspaceMemberRole.VIEWER

        token = secrets.token_hex(32)
        expires_at = datetime.utcnow() + timedelta(days=7)

        invite = WorkspaceInvite(
            workspace_id=workspace_id,
            email=email,
            role=role_enum,
            token=token,
            invited_by=invited_by,
            expires_at=expires_at,
        )
        db.add(invite)
        db.commit()
        db.refresh(invite)
        return invite

    def get_invite_by_token(self, db: Session, token: str) -> WorkspaceInvite:
        """Return an invite by its token or raise InviteNotFound."""
        invite = (
            db.query(WorkspaceInvite).filter(WorkspaceInvite.token == token).first()
        )
        if not invite:
            raise InviteNotFound("Invite token not found.")
        return invite

    def accept_invite(
        self,
        db: Session,
        token: str,
        user_id: uuid.UUID,
        user_email: Optional[str],
    ) -> WorkspaceMember:
        """Accept an invite and add the user to the workspace.

        Only the account the invite was sent to may accept it: ``user_email``
        (the accepting account's stored email) must match the invite's address
        under :func:`invite_email_matches`, or :class:`InviteEmailMismatch` is
        raised. That applies to every account, superusers included, and is
        checked before expiry and prior acceptance.
        """
        invite = self.get_invite_by_token(db, token)

        if not invite_email_matches(invite.email, user_email):
            raise InviteEmailMismatch()
        if invite.is_expired:
            raise InviteExpired("This invite has expired.")
        if invite.is_accepted:
            raise InviteAlreadyAccepted("This invite has already been accepted.")

        # Mark accepted
        invite.accepted_at = datetime.utcnow()
        db.flush()

        # Add as member (may raise AlreadyMember)
        member = self.add_member(
            db,
            workspace_id=invite.workspace_id,
            user_id=user_id,
            role=invite.role.value,
            invited_by=invite.invited_by,
        )
        return member

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_workspace_stats(
        self, db: Session, workspace_id: uuid.UUID
    ) -> WorkspaceStats:
        """Return the workspace's member count."""
        self.get_workspace(db, workspace_id)  # existence check

        member_count = (
            db.query(WorkspaceMember)
            .filter(WorkspaceMember.workspace_id == workspace_id)
            .count()
        )
        return WorkspaceStats(member_count=member_count)

    def member_counts(
        self, db: Session, workspace_ids: Iterable[uuid.UUID]
    ) -> Dict[uuid.UUID, int]:
        """Return each workspace's member count, in one grouped query.

        A workspace with no members is absent from the result; callers read a
        missing id as 0.
        """
        ids = list(workspace_ids)
        if not ids:
            return {}
        rows = (
            db.query(WorkspaceMember.workspace_id, func.count())
            .filter(WorkspaceMember.workspace_id.in_(ids))
            .group_by(WorkspaceMember.workspace_id)
            .all()
        )
        return dict(rows)

    # ── Permissions ───────────────────────────────────────────────────────────

    def check_member_permission(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        required_role: str,
    ) -> bool:
        """
        Return True if the user holds at least the required_role in the workspace.

        Role hierarchy: VIEWER < ANALYST < DEVELOPER < ADMIN < OWNER
        """
        member = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )
        if not member:
            return False

        try:
            member_index = ROLE_HIERARCHY.index(member.role.value)
            required_index = ROLE_HIERARCHY.index(required_role.upper())
        except ValueError:
            return False

        return member_index >= required_index

    def get_member(
        self,
        db: Session,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Optional[WorkspaceMember]:
        """Return the WorkspaceMember record or None."""
        return (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )


# Module-level singleton
workspace_service = WorkspaceService()

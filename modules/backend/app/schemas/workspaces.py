"""
Pydantic schemas for EP-057: Multi-Tenant Team Workspaces.
"""

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from backend.app.schemas.storable_text import (
    contains_unstorable_text,
    unstorable_text_message,
)

# ─────────────────────────────────────────────────────────────────────────────
# Workspace
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$")


def _refuse_unstorable_text(value: Any, info: ValidationInfo) -> Any:
    """Refuse NUL and unpaired surrogates in a workspace's name or
    description: the database cannot store them, and the save answered 500
    (#2181). A ``mode="before"`` validator, so the field's own checks do not
    read the value first; the message names the field, never the value.
    ``slug`` refuses both already."""
    if contains_unstorable_text(value):
        raise ValueError(unstorable_text_message(info.field_name))
    return value


class CreateWorkspaceRequest(BaseModel):
    """Payload for creating a new workspace.

    Workspaces have no plan and no limits, so ``plan`` (or any other field not
    listed here) is refused with 422 rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    description: str = ""

    @field_validator("name", "description", mode="before")
    @classmethod
    def refuse_unstorable_text(cls, value: Any, info: ValidationInfo) -> Any:
        """Text the database cannot store answers 422 at the field."""
        return _refuse_unstorable_text(value, info)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        # fullmatch, not match: "$" also matches before a final newline.
        if not _SLUG_RE.fullmatch(v):
            raise ValueError(
                "Slug must be 3–50 characters, lowercase alphanumeric with hyphens, "
                "and must not start or end with a hyphen."
            )
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        if len(v) > 200:
            raise ValueError("name must be at most 200 characters")
        return v


class UpdateWorkspaceRequest(BaseModel):
    """Payload for updating an existing workspace (all fields optional).

    Any other field, ``plan`` included, is refused with 422.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None

    @field_validator("name", "description", mode="before")
    @classmethod
    def refuse_unstorable_text(cls, value: Any, info: ValidationInfo) -> Any:
        """As on create."""
        return _refuse_unstorable_text(value, info)


class WorkspaceResponse(BaseModel):
    """Response schema for a workspace."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    description: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id(cls, v):
        return str(v)


class WorkspaceWithStatsResponse(WorkspaceResponse):
    """Workspace response with its member count."""

    member_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Members
# ─────────────────────────────────────────────────────────────────────────────


class AddMemberRequest(BaseModel):
    """Payload for adding an existing user to a workspace."""

    user_id: str
    role: str = "VIEWER"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"OWNER", "ADMIN", "DEVELOPER", "ANALYST", "VIEWER"}
        if v.upper() not in allowed:
            raise ValueError(f"role must be one of {sorted(allowed)}")
        return v.upper()


class UpdateMemberRoleRequest(BaseModel):
    """Payload for changing a member's role."""

    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"OWNER", "ADMIN", "DEVELOPER", "ANALYST", "VIEWER"}
        if v.upper() not in allowed:
            raise ValueError(f"role must be one of {sorted(allowed)}")
        return v.upper()


class WorkspaceMemberResponse(BaseModel):
    """Response schema for a workspace member."""

    model_config = ConfigDict(from_attributes=True)

    workspace_id: str
    user_id: str
    role: str
    joined_at: datetime

    # Denormalised user fields (populated manually in endpoints)
    username: Optional[str] = None
    email: Optional[str] = None

    @field_validator("workspace_id", "user_id", mode="before")
    @classmethod
    def coerce_uuid(cls, v):
        return str(v)

    @field_validator("role", mode="before")
    @classmethod
    def coerce_role(cls, v):
        if hasattr(v, "value"):
            return v.value
        return str(v)


# ─────────────────────────────────────────────────────────────────────────────
# Invites
# ─────────────────────────────────────────────────────────────────────────────


#: Longest address an invite accepts: the width of ``workspace_invites.email``
#: (``String(255)``), so a longer one is a 422 rather than a database error.
#: Checked before the shape pattern runs.
INVITE_EMAIL_MAX_LENGTH = 255
_INVITE_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+$")


class CreateInviteRequest(BaseModel):
    """Payload for sending a workspace invite.

    ``email`` is trimmed and must be ASCII, at most 255 characters (the column width) and shaped
    ``local@domain`` (exactly one ``@``, no whitespace). Deliberately not
    ``EmailStr``: that refuses special-use domains such as ``corp.local`` and
    ``int.test``, which accounts can legitimately have. Who may accept the
    invite is decided when it is accepted, not here.
    """

    email: str
    role: str = "VIEWER"

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, v: object) -> str:
        if not isinstance(v, str):
            raise ValueError("email must be a string")
        trimmed = v.strip()
        # ASCII as sent: some non-ASCII characters lower-case to ASCII ones,
        # and an address that is not ASCII can never accept an invite.
        if not trimmed.isascii():
            raise ValueError("email must contain only ASCII characters")
        if len(trimmed) > INVITE_EMAIL_MAX_LENGTH:
            raise ValueError(
                f"email must be at most {INVITE_EMAIL_MAX_LENGTH} characters"
            )
        if not _INVITE_EMAIL_SHAPE.match(trimmed):
            raise ValueError("email must be an address of the form name@domain")
        return trimmed

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        # OWNER cannot be granted via invite
        allowed = {"ADMIN", "DEVELOPER", "ANALYST", "VIEWER"}
        if v.upper() not in allowed:
            raise ValueError(
                f"role must be one of {sorted(allowed)} (OWNER cannot be invited)"
            )
        return v.upper()


class InviteEmailMismatchDetail(BaseModel):
    """The ``detail`` of a refused invite accept."""

    code: str
    message: str


class InviteEmailMismatchResponse(BaseModel):
    """403 body: the signed-in account is not the invited address."""

    detail: InviteEmailMismatchDetail


class WorkspaceInviteResponse(BaseModel):
    """Response schema for a workspace invite."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    email: str
    role: str
    token: str
    expires_at: datetime
    accepted_at: Optional[datetime]
    workspace_name: str = Field(
        description="Name of the workspace the invitation is for."
    )
    inviter_username: Optional[str] = Field(
        description=(
            "Username of the account that created the invitation; null when "
            "that account no longer exists. The invitation preview "
            "(`GET /workspaces/invites/{token}`) returns it only to the "
            "account the invitation was sent to, as it does `email` in full; "
            "anyone else gets null."
        )
    )

    @field_validator("id", "workspace_id", mode="before")
    @classmethod
    def coerce_uuid(cls, v):
        return str(v)

    @field_validator("role", mode="before")
    @classmethod
    def coerce_role(cls, v):
        if hasattr(v, "value"):
            return v.value
        return str(v)

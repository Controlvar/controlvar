"""The bodies of the visual editor's sign-in routes (#1545).

``backend/app/services/visual_editor.py`` holds what each route does with
them. A request body refuses any key it does not name (``extra="forbid"``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from backend.app.schemas.storable_text import StorableTextModel

#: The value the page sends with an editor link's ticket: exactly 43
#: base64url characters, no padding.
CLAIM_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
#: The 422 for any other value.
CLAIM_FORMAT_REFUSED = "Send exactly 43 characters, each a letter, a digit, - or _."


def _claim(value: str) -> str:
    if CLAIM_PATTERN.fullmatch(value) is None:
        raise PydanticCustomError("claim_format", CLAIM_FORMAT_REFUSED)
    return value


class EditorLinkOpened(BaseModel):
    """The editor link, shown once."""

    open_url: str = Field(
        ...,
        description=(
            "The experiment's editor page with `#controlvar-editor=<43 "
            "characters>`. It opens the visual editor once, within 120 seconds, "
            "on a page with the Controlvar script."
        ),
    )
    expires_at: datetime = Field(..., description="When the link stops working.")


class TicketClaim(StorableTextModel):
    """What the page an editor link opened sends."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The value after `#controlvar-editor=` in the editor link.",
    )
    claim: str = Field(
        ...,
        description=(
            "43 base64url characters (A-Z, a-z, 0-9, - and _, no padding), "
            "drawn at random by the page."
        ),
    )

    @field_validator("claim")
    @classmethod
    def _claim_format(cls, value: str) -> str:
        return _claim(value)


class TicketClaimed(BaseModel):
    """The claim was stored. No credential comes back."""

    expires_at: datetime = Field(
        ...,
        description="When the claim stops working if the editor has not used it.",
    )


class ClaimExchange(StorableTextModel):
    """What the visual editor's panel sends to start its session."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(
        ...,
        description=(
            "The value the page handed the panel: the 43 base64url characters "
            "it sent with the editor link."
        ),
    )
    page_origin: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description=(
            "The origin of the page that handed the panel the value: the "
            "`origin` of that message event. It must be one of the "
            "experiment's sites."
        ),
    )

    @field_validator("claim")
    @classmethod
    def _claim_format(cls, value: str) -> str:
        return _claim(value)


class EditorVariant(BaseModel):
    id: UUID
    name: str
    is_control: bool
    visual_changes: Optional[Dict[str, Any]] = Field(
        None, description="The variant's page changes as stored, or null."
    )


class EditorExperiment(BaseModel):
    id: UUID
    key: Optional[str] = None
    name: str
    editor_url: Optional[str] = None
    pages: List[Dict[str, Any]]
    design_etag: Optional[str] = Field(
        None,
        description="The design's version, as the experiment's own answer gives it.",
    )
    variants: List[EditorVariant]


class EditorSessionState(BaseModel):
    """An editor session: when it ends, where it may run, and the draft."""

    expires_at: datetime = Field(
        ..., description="When the session ends: 4 hours after it began."
    )
    site_origins: List[str] = Field(
        ...,
        description=(
            "The experiment's sites the editor may run on: its pages' origins "
            "that this server accepts requests from."
        ),
    )
    experiment: EditorExperiment


class EditorSessionOpened(EditorSessionState):
    """A new editor session, with its token, shown once."""

    token: str = Field(
        ...,
        description=(
            "`cves_` and 43 base64url characters: the session's bearer token "
            "for the visual editor's session routes and no other."
        ),
    )

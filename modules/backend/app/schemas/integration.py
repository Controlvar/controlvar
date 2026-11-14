"""
Pydantic v2 schemas for third-party integration configuration — EP-034 Batch 1.
"""

import enum
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.backend.app.models.integration_config import LAST_ERROR_MAX_LENGTH


class IntegrationType(str, enum.Enum):
    SALESFORCE = "salesforce"
    JIRA = "jira"
    GITHUB = "github"


# ---------------------------------------------------------------------------
# Integration Config schemas
# ---------------------------------------------------------------------------


class IntegrationConfigCreate(BaseModel):
    """Payload for creating a new integration config."""

    integration_type: IntegrationType
    is_active: bool = False
    encrypted_config: Optional[Dict[str, Any]] = None


class IntegrationConfigUpdate(BaseModel):
    """Payload for updating an existing integration config."""

    is_active: Optional[bool] = None
    encrypted_config: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None

    @field_validator("last_error")
    @classmethod
    def _last_error_fits_the_column(cls, value: Optional[str]) -> Optional[str]:
        """Refuse a ``last_error`` the column cannot hold.

        A validator, not ``Field(max_length=...)``, so the OpenAPI document
        does not change.  Without it the database refuses the value and the
        route answers 500.
        """
        if value is not None and len(value) > LAST_ERROR_MAX_LENGTH:
            raise ValueError(
                f"last_error must be at most {LAST_ERROR_MAX_LENGTH} characters"
            )
        return value


#: The keys of ``encrypted_config`` a response shows, with their values: the
#: connection settings.  Every other key is a stored secret, and a response
#: names it in ``stored_secrets`` without its value.  This is a list of what to
#: show, not of what to hide, because ``encrypted_config`` accepts any key: a
#: key nobody listed here is withheld.
SHOWN_CONFIG_KEYS = frozenset(
    {
        "base_url",
        "email",
        "project_key",
        "instance_url",
        "client_id",
        "repo_owner",
        "repo_name",
    }
)


# Every route that answers with a configuration answers with this model,
# whoever the caller is.  ``encrypted_config`` carries only SHOWN_CONFIG_KEYS;
# the other stored keys are named, sorted, in ``stored_secrets``, and their
# values are never returned, not even as a placeholder.  (The class docstring
# is the schema's description in the OpenAPI document, so this stays here.)
class IntegrationConfigResponse(BaseModel):
    """Response schema for an integration config record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    integration_type: IntegrationType
    is_active: bool
    encrypted_config: Optional[Dict[str, Any]] = None
    stored_secrets: List[str] = Field(
        default_factory=list,
        description=(
            "The names, sorted, of the keys of the stored configuration whose "
            "values are not returned: every key except base_url, email, "
            "project_key, instance_url, client_id, repo_owner and repo_name. "
            "encrypted_config carries only those seven."
        ),
    )
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _withhold_stored_secrets(self) -> "IntegrationConfigResponse":
        """Keep the shown keys in ``encrypted_config`` and name the rest.

        Builds new objects and never edits the stored one: validation has
        already copied the configuration, and the copy is replaced, not
        changed.  Running it twice gives the same answer, so a response that
        is validated again (FastAPI does that to a returned model) keeps its
        ``stored_secrets``.
        """
        stored = self.encrypted_config
        if stored is None:
            return self
        self.encrypted_config = {
            key: value for key, value in stored.items() if key in SHOWN_CONFIG_KEYS
        }
        withheld = {key for key in stored if key not in SHOWN_CONFIG_KEYS}
        self.stored_secrets = sorted(withheld | set(self.stored_secrets))
        return self


# ---------------------------------------------------------------------------
# Jira-specific schemas
# ---------------------------------------------------------------------------


class JiraIssueCreateRequest(BaseModel):
    """Request body for creating a Jira issue linked to an experiment."""

    project_key: str
    experiment_name: str
    experiment_id: str
    hypothesis: str
    start_date: datetime
    issue_type: str = "Task"
    platform_url: Optional[str] = None


class JiraWebhookEvent(BaseModel):
    """Parsed Jira webhook event."""

    event_type: str
    issue_key: str
    new_status: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None

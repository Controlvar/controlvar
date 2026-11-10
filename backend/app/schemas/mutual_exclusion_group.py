"""
Pydantic v2 schemas for Mutual Exclusion Groups (EP-022).

Defines request/response models for creating, updating, and querying
mutual exclusion groups that prevent users from being in conflicting experiments.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MutualExclusionGroupStatus(str, Enum):
    """Status of a mutual exclusion group."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class MutualExclusionGroupCreate(BaseModel):
    """Schema for creating a new mutual exclusion group."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Unique name for the mutual exclusion group.",
    )
    description: Optional[str] = Field(
        None,
        max_length=2000,
        description="Description of the group's purpose.",
    )
    traffic_allocation: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of traffic eligible for experiments in this group (0.0-1.0).",
    )

    @field_validator("traffic_allocation", mode="after")
    @classmethod
    def validate_traffic_allocation(cls, v: float) -> float:
        """Runs after pydantic's type check and the field's ge/le, so a value
        of the wrong type answers pydantic's own 422; as a ``before`` validator
        it compared the raw value and raised ``TypeError``, a 500 (#957)."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("traffic_allocation must be between 0.0 and 1.0")
        return v


class MutualExclusionGroupUpdate(BaseModel):
    """Schema for updating a mutual exclusion group."""

    name: Optional[str] = Field(
        None,
        min_length=1,
        max_length=200,
        description="Updated name.",
    )
    description: Optional[str] = Field(
        None,
        max_length=2000,
        description="Updated description.",
    )
    traffic_allocation: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Updated traffic allocation (0.0-1.0).",
    )
    status: Optional[MutualExclusionGroupStatus] = Field(
        None,
        description="Updated status.",
    )

    @field_validator("traffic_allocation", mode="after")
    @classmethod
    def validate_traffic_allocation(cls, v: Optional[float]) -> Optional[float]:
        """``after``, as on create (#957)."""
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("traffic_allocation must be between 0.0 and 1.0")
        return v


class ExperimentSummary(BaseModel):
    """Minimal experiment info for embedding in group responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: str


class MutualExclusionGroupResponse(BaseModel):
    """Response schema for a mutual exclusion group."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: Optional[str] = None
    traffic_allocation: float
    status: str
    owner_id: Optional[UUID] = None
    experiments: List[ExperimentSummary] = []
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def convert_status_enum(cls, v):
        if hasattr(v, "value"):
            return v.value
        return v


class MutualExclusionGroupListResponse(BaseModel):
    """Paginated list of mutual exclusion groups."""

    items: List[MutualExclusionGroupResponse]
    total: int


class AddExperimentToGroupRequest(BaseModel):
    """Request to add an experiment to a mutual exclusion group."""

    experiment_id: UUID = Field(
        ..., description="ID of the experiment to add to the group."
    )


class UserExperimentSelection(BaseModel):
    """Result of selecting which experiment a user should see in a group."""

    user_id: str = Field(..., description="User identifier.")
    group_id: UUID = Field(..., description="Mutual exclusion group ID.")
    selected_experiment_id: Optional[UUID] = Field(
        None,
        description="ID of the selected experiment, or null if user is excluded.",
    )
    is_excluded: bool = Field(
        ...,
        description="True if user falls outside the group's traffic allocation.",
    )

"""
Pydantic v2 schemas for Global Holdout (EP-022).

Defines request/response models for creating, updating, and querying
global holdout configurations that reserve a percentage of users from
all experiments.
"""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.core.analysis_status import AnalysisStatusValue


class GlobalHoldoutCreate(BaseModel):
    """Schema for creating a new global holdout."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Unique name for the global holdout.",
    )
    description: Optional[str] = Field(
        None,
        max_length=2000,
        description="Description of the holdout's purpose.",
    )
    holdout_percentage: int = Field(
        default=10,
        ge=1,
        le=20,
        description=(
            "Percentage of users to hold out, 1-20. Recommended: 1-5%, up to 10% "
            "when traffic is low; run for 1-3 months. Cannot change once the "
            "holdout has been active."
        ),
    )
    is_active: bool = Field(
        default=False,
        description=(
            "Whether the holdout is active. True makes it the active holdout "
            "and deactivates the one that was active."
        ),
    )

    @field_validator("holdout_percentage", mode="after")
    @classmethod
    def validate_holdout_percentage(cls, v: int) -> int:
        """Runs after pydantic's type check and the field's ge/le, so a value
        of the wrong type answers pydantic's own 422; as a ``before`` validator
        it compared the raw value and raised ``TypeError``, a 500 (#957)."""
        if not (1 <= v <= 20):
            raise ValueError("holdout_percentage must be between 1 and 20")
        return v


class GlobalHoldoutUpdate(BaseModel):
    """Schema for updating a global holdout."""

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
    holdout_percentage: Optional[int] = Field(
        None,
        ge=1,
        le=20,
        description=(
            "Percentage of users to hold out, 1-20. Recommended: 1-5%, up to 10% "
            "when traffic is low; run for 1-3 months. Cannot change once the "
            "holdout has been active."
        ),
    )
    is_active: Optional[bool] = Field(
        None,
        description=(
            "Updated active status. True deactivates the active holdout; a "
            "holdout that has ended cannot restart."
        ),
    )

    @field_validator("holdout_percentage", mode="after")
    @classmethod
    def validate_holdout_percentage(cls, v: Optional[int]) -> Optional[int]:
        """``after``, as on create (#957)."""
        if v is not None and not (1 <= v <= 20):
            raise ValueError("holdout_percentage must be between 1 and 20")
        return v


class GlobalHoldoutResponse(BaseModel):
    """Response schema for a global holdout."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: Optional[str] = None
    holdout_percentage: int
    is_active: bool
    owner_id: Optional[UUID] = None
    activated_at: Optional[datetime] = Field(
        None,
        description=(
            "When the holdout was activated (UTC). Null for a holdout never "
            "activated, and for one active since before holdout membership "
            "was recorded."
        ),
    )
    deactivated_at: Optional[datetime] = Field(
        None,
        description="When the holdout was deactivated (UTC); it cannot restart.",
    )
    created_at: datetime
    updated_at: datetime


class GlobalHoldoutListResponse(BaseModel):
    """List of global holdouts."""

    items: List[GlobalHoldoutResponse]
    total: int


class HoldoutCheckResponse(BaseModel):
    """Result of checking if a user is in the global holdout."""

    user_id: str = Field(..., description="User identifier.")
    is_in_holdout: bool = Field(
        ..., description="True if the user is in the global holdout."
    )
    holdout_percentage: int = Field(..., description="Current holdout percentage.")
    bucket: int = Field(
        ...,
        ge=0,
        le=99,
        description="User's holdout bucket (0-99).",
    )


# ---------------------------------------------------------------------------
# Results (beta, #445)
# ---------------------------------------------------------------------------

HoldoutResultsUnavailableReason = Literal[
    "activated_before_measurement", "not_activated", "too_few_users", "no_events"
]

HoldoutGroupName = Literal["in_holdout", "not_in_holdout"]


class HoldoutGroup(BaseModel):
    """One arm of a holdout's results."""

    group: HoldoutGroupName = Field(
        ...,
        description=(
            "in_holdout: users the holdout kept out of every experiment. "
            "not_in_holdout: everyone else first seen while it was active."
        ),
    )
    label: str = Field(..., description="'In holdout' or 'Not in holdout'.")
    users: int = Field(
        ...,
        ge=0,
        description=(
            "Users analysed in this group: first seen while the holdout was "
            "active, less the excluded ones. Users refused by targeting or "
            "mutual exclusion are in their group."
        ),
    )
    conversions: int = Field(
        ...,
        ge=0,
        description="Users who sent at least one matching event in their window.",
    )
    conversion_rate: Optional[float] = Field(
        None,
        description="conversions / users, from 0 to 1; null when users is 0.",
    )
    excluded_users: int = Field(
        ...,
        ge=0,
        description=(
            "Users of this group left out of both groups because they had an "
            "experiment assignment before they were first seen. About the "
            "holdout percentage of excluded_users is expected here."
        ),
    )


class HoldoutDifference(BaseModel):
    """Not in holdout minus in holdout: positive means the experiments raised it."""

    absolute: float = Field(
        ...,
        description=(
            "not_in_holdout conversion_rate minus in_holdout conversion_rate. "
            "Positive means the running experiments raised the metric."
        ),
    )
    relative_pct: Optional[float] = Field(
        None,
        description=(
            "absolute as a percentage of the in_holdout rate; null when that rate is 0."
        ),
    )
    ci_lower: float = Field(
        ...,
        description=(
            "Lower bound of the 95% interval for absolute (Agresti-Caffo), for "
            "reading once. It is computed separately from p_value and can "
            "disagree with is_significant near the boundary."
        ),
    )
    ci_upper: float = Field(
        ...,
        description=(
            "Upper bound of the 95% interval for absolute (Agresti-Caffo), for "
            "reading once. It is computed separately from p_value and can "
            "disagree with is_significant near the boundary."
        ),
    )
    p_value: Optional[float] = Field(
        None,
        description=(
            "Fisher's exact test, the test /results uses. The interval is "
            "computed separately and can disagree with it near the boundary."
        ),
    )
    is_significant: bool = Field(
        ...,
        description=(
            "p_value < 0.05, from Fisher's exact test. It follows p_value, not "
            "the interval: near the boundary the interval can include 0 while "
            "this is true, or exclude 0 while it is false."
        ),
    )
    always_valid_ci_lower: float = Field(
        ...,
        description=(
            "Lower bound of the 95% confidence sequence for absolute (mSPRT, "
            "tau squared 0.001); it stays valid however often you check."
        ),
    )
    always_valid_ci_upper: float = Field(
        ...,
        description=(
            "Upper bound of the 95% confidence sequence for absolute (mSPRT, "
            "tau squared 0.001); it stays valid however often you check."
        ),
    )


class HoldoutResultsResponse(BaseModel):
    """A holdout's users against everyone else, for one metric (beta)."""

    holdout_id: UUID
    holdout_name: str
    holdout_percentage: int
    activated_at: Optional[datetime] = Field(None, description="UTC.")
    deactivated_at: Optional[datetime] = Field(None, description="UTC.")
    window_end: Optional[datetime] = Field(
        None,
        description=(
            "End of every user's window (UTC): deactivated_at, or the time of "
            "the request while the holdout is active. Null when the holdout "
            "cannot be measured."
        ),
    )
    metric: str = Field(..., description="The event name, as sent.")
    unavailable_reason: Optional[HoldoutResultsUnavailableReason] = Field(
        None,
        description=(
            "Why difference is null; null exactly when difference is set. "
            "activated_before_measurement: the holdout was active before its "
            "users were recorded. not_activated: it has not been activated "
            "since. too_few_users: a group has fewer than 100 users. "
            "no_events: neither group sent a matching event."
        ),
    )
    message: Optional[str] = Field(
        None, description="Present exactly when unavailable_reason is set."
    )
    excluded_users: Optional[int] = Field(
        None,
        description=(
            "Users left out of both groups because they had an experiment "
            "assignment before they were first seen; null when the holdout "
            "cannot be measured."
        ),
    )
    holdout_users_with_assignments: Optional[int] = Field(
        None,
        description=(
            "Users in the holdout group assigned to an experiment inside their "
            "window. 0 normally; more after a rollback to a release that does "
            "not record holdouts, and analysis_notice then says so. Null when "
            "the holdout cannot be measured."
        ),
    )
    groups: List[HoldoutGroup] = Field(
        default_factory=list,
        description=(
            "Empty when the holdout cannot be measured; otherwise in_holdout, "
            "then not_in_holdout."
        ),
    )
    difference: Optional[HoldoutDifference] = None
    analysis_status: AnalysisStatusValue = Field(
        ..., description="Whether the numbers can be relied on: 'beta'."
    )
    analysis_notice: Optional[str] = Field(
        None, description="What the beta numbers do and do not measure."
    )

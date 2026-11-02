"""
Segment schemas for Pydantic validation and serialization.

This module defines Pydantic models for audience segmentation data structures
used in the P3-C: Audience Segmentation API.
"""

from enum import Enum
from typing import Annotated, Any, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    check_segment_vocabulary,
    validate_segment_rules,
)
from backend.app.schemas.storable_text import StorableTextModel

#: At most this many user ids in one add or remove request (#440).
MAX_MEMBER_IDS_PER_REQUEST = 10_000
#: A user id in an id-list segment is 1 to this many characters.
MAX_MEMBER_ID_LENGTH = 255

#: The description of a segment's ``rules`` in the OpenAPI document.
SEGMENT_RULES_DESCRIPTION = (
    "Targeting rules in the format flag and experiment targeting use: "
    '{"logical_operator": "AND"|"OR"|"NOT" (optional), "groups": '
    '[{"logical_operator"?, "conditions": [{"attribute", "operator", "value"}]}]}, '
    "with operators such as equals, in, regex and semver_gte. At least one group, "
    "each with at least one condition; at most 20 groups, 50 conditions, 10 regex "
    "conditions and 1,000 list values. Any other shape is refused with 422."
)


def _checked_segment_rules(value: Any) -> Any:
    """Refuse segment rules membership would not evaluate as written (#440).

    The value is returned as given, never rewritten. The message is fixed
    text naming a place and a reason, never the submitted rules (see
    ``validate_segment_rules``). A word membership never matches, such as an
    unknown distance unit, is refused here too (#1204): on the write path
    only, because ``validate_segment_rules`` also decides whether a stored
    segment can be evaluated.
    """
    try:
        check_segment_vocabulary(validate_segment_rules(value))
    except TargetingRulesError as err:
        raise ValueError(str(err)) from None
    return value


class SegmentStatus(str, Enum):
    """Segment lifecycle status."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class SegmentKind(str, Enum):
    """What decides a segment's members. Fixed when the segment is created."""

    RULES = "rules"
    ID_LIST = "id_list"


#: The description of ``kind`` in the OpenAPI document.
SEGMENT_KIND_DESCRIPTION = (
    "rules: users whose attributes match `rules`. id_list: users whose user_id "
    "is in the segment's list, added with POST /segments/{segment_id}/members; "
    "an id_list segment has no rules. Set when the segment is created and never "
    "changed."
)


class SegmentCreate(BaseModel):
    """Schema for creating a new audience segment."""

    name: str = Field(..., min_length=2, max_length=128)
    description: Optional[str] = Field(None, max_length=512)
    kind: SegmentKind = Field(SegmentKind.RULES, description=SEGMENT_KIND_DESCRIPTION)
    rules: Optional[dict] = Field(
        None,
        description=SEGMENT_RULES_DESCRIPTION
        + " Required for a rules segment; refused for an id_list segment.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "US Premium Users",
                "description": "Premium subscribers in the United States",
                "rules": {
                    "logical_operator": "AND",
                    "groups": [
                        {
                            "logical_operator": "AND",
                            "conditions": [
                                {
                                    "attribute": "country",
                                    "operator": "equals",
                                    "value": "US",
                                },
                                {
                                    "attribute": "plan",
                                    "operator": "equals",
                                    "value": "premium",
                                },
                            ],
                        }
                    ],
                },
            }
        }
    )

    @field_validator("rules", mode="before")
    @classmethod
    def checked_rules(cls, value: Any) -> Any:
        if value is None:
            return None
        return _checked_segment_rules(value)

    @model_validator(mode="after")
    def rules_match_the_kind(self) -> "SegmentCreate":
        if self.kind == SegmentKind.RULES and self.rules is None:
            raise ValueError("rules: required for a rules segment")
        if self.kind == SegmentKind.ID_LIST and self.rules is not None:
            raise ValueError("rules: an id_list segment has no rules")
        return self


class SegmentUpdate(BaseModel):
    """Schema for updating an existing audience segment.  All fields are optional."""

    name: Optional[str] = Field(None, min_length=2, max_length=128)
    description: Optional[str] = None
    rules: Optional[dict] = Field(
        None,
        description=SEGMENT_RULES_DESCRIPTION
        + " Leave it out to keep the stored rules.",
    )
    status: Optional[SegmentStatus] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Updated Segment Name",
                "status": "inactive",
            }
        }
    )

    @model_validator(mode="before")
    @classmethod
    def kind_is_not_changed(cls, data: Any) -> Any:
        if isinstance(data, dict) and "kind" in data:
            raise ValueError("kind: a segment's kind cannot be changed")
        return data

    @field_validator("rules", mode="before")
    @classmethod
    def checked_rules(cls, value: Any) -> Any:
        if value is None:
            return None
        return _checked_segment_rules(value)


class SegmentResponse(BaseModel):
    """Schema for returning a segment in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    kind: SegmentKind = Field(SegmentKind.RULES, description=SEGMENT_KIND_DESCRIPTION)
    rules: Optional[dict] = Field(
        None, description="The segment's rules; null for an id_list segment."
    )
    status: SegmentStatus
    created_at: str
    updated_at: str
    member_count: Optional[int] = Field(
        None,
        description="How many user ids an id_list segment holds, on GET "
        "/segments/{segment_id}. Null for a rules segment and in the list.",
    )


#: One user id in a member request: 1 to 255 characters, matched exactly.
MemberId = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_MEMBER_ID_LENGTH)
]


def _checked_member_ids(field: str, value: Any) -> Any:
    """Refuse a member list outside the limits, with fixed text (#440).

    The message names the field and, for one id, its position; never an id.
    """
    if not isinstance(value, list):
        raise ValueError(f"{field}: a list of IDs is required")
    if not value:
        raise ValueError(f"{field}: at least 1 ID is required")
    if len(value) > MAX_MEMBER_IDS_PER_REQUEST:
        raise ValueError(
            f"{field}: at most {MAX_MEMBER_IDS_PER_REQUEST:,} IDs per request"
        )
    for index in range(len(value)):
        item = value[index]
        if not isinstance(item, str) or not 1 <= len(item) <= MAX_MEMBER_ID_LENGTH:
            raise ValueError(
                f"{field}[{index}]: an ID is 1 to {MAX_MEMBER_ID_LENGTH} characters"
            )
    return value


_MEMBER_IDS_DESCRIPTION = (
    "User ids, 1 to 10,000 per request, each 1 to 255 characters and matched "
    "exactly (not trimmed, not case-folded). Repeats count once."
)


class SegmentMembersAdd(StorableTextModel):
    """User ids to add to an id-list segment."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"add": ["user-123", "user-456"]}},
    )

    add: List[MemberId] = Field(
        ...,
        min_length=1,
        max_length=MAX_MEMBER_IDS_PER_REQUEST,
        description=_MEMBER_IDS_DESCRIPTION
        + " An id already in the segment is left as it is.",
    )

    @field_validator("add", mode="before")
    @classmethod
    def checked_ids(cls, value: Any) -> Any:
        return _checked_member_ids("add", value)


class SegmentMembersRemove(StorableTextModel):
    """User ids to remove from an id-list segment."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"remove": ["user-123"]}},
    )

    remove: List[MemberId] = Field(
        ...,
        min_length=1,
        max_length=MAX_MEMBER_IDS_PER_REQUEST,
        description=_MEMBER_IDS_DESCRIPTION
        + " An id that is not in the segment is ignored.",
    )

    @field_validator("remove", mode="before")
    @classmethod
    def checked_ids(cls, value: Any) -> Any:
        return _checked_member_ids("remove", value)


class SegmentMembersAddResponse(BaseModel):
    """What an add changed. ``added + already_members`` is the number of
    distinct ids sent."""

    added: int = Field(..., description="Ids that were not members and now are.")
    already_members: int = Field(
        ..., description="Ids that were members already; nothing changed for them."
    )
    member_count: int = Field(..., description="Members of the segment now.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"added": 4190, "already_members": 11, "member_count": 4190}
        }
    )


class SegmentMembersRemoveResponse(BaseModel):
    """What a remove changed. ``removed + not_members`` is the number of
    distinct ids sent."""

    removed: int = Field(..., description="Ids that were members and no longer are.")
    not_members: int = Field(..., description="Ids that were not members.")
    member_count: int = Field(..., description="Members of the segment now.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"removed": 120, "not_members": 0, "member_count": 4070}
        }
    )


class SegmentMembershipRequest(BaseModel):
    """Check if a user context matches a segment."""

    user_context: dict = Field(
        ...,
        description="User attributes to evaluate against segment rules. "
        'e.g. {"user_id": "123", "country": "US", "plan": "pro", "age": 25}',
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_context": {
                    "user_id": "123",
                    "country": "US",
                    "plan": "pro",
                    "age": 25,
                }
            }
        }
    )


class SegmentMembershipResponse(BaseModel):
    """Result of evaluating a user context against a segment."""

    segment_id: str
    segment_name: str
    is_member: bool
    matched_rules: list[str] = Field(
        default=[],
        description="Each condition, in any group, that the context satisfies "
        'on its own, as "<attribute> <operator> <value>". Empty when the user '
        "is not a member.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "segment_id": "abc123",
                "segment_name": "US Premium Users",
                "is_member": True,
                "matched_rules": ["country equals US", "plan equals premium"],
            }
        }
    )


class BulkSegmentMembershipRequest(BaseModel):
    """Check membership for one user across multiple segments."""

    user_context: dict
    segment_ids: list[str] = Field(..., min_length=1, max_length=50)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_context": {"country": "US", "plan": "pro"},
                "segment_ids": ["seg-1", "seg-2"],
            }
        }
    )

    @field_validator("segment_ids")
    @classmethod
    def validate_segment_ids(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("segment_ids must contain at least 1 element")
        if len(v) > 50:
            raise ValueError("segment_ids cannot contain more than 50 elements")
        return v


class BulkSegmentMembershipResponse(BaseModel):
    """Result of evaluating one user against multiple segments."""

    user_context: dict
    memberships: dict[str, bool]  # {segment_id: is_member}
    evaluation_time_ms: float

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_context": {"country": "US", "plan": "pro"},
                "memberships": {"seg-1": True, "seg-2": False},
                "evaluation_time_ms": 3.42,
            }
        }
    )


class ExperimentSegmentLink(BaseModel):
    """Link an experiment to a required audience segment."""

    experiment_id: str
    segment_id: str
    is_required: bool = True  # if True, only segment members can be assigned


class SegmentExperimentResponse(BaseModel):
    """Which experiments and feature flags use this segment."""

    segment_id: str
    experiments: list[dict]  # [{id, name, status}]
    feature_flags: list[dict]  # [{id, name}]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "segment_id": "abc123",
                "experiments": [
                    {"id": "exp-1", "name": "Homepage Test", "status": "active"}
                ],
                "feature_flags": [{"id": "ff-1", "name": "dark-mode"}],
            }
        }
    )


class AudiencePreviewResponse(BaseModel):
    """Estimated audience size from preview evaluation."""

    estimated_percentage: float
    sample_size: int
    matched: int

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "estimated_percentage": 35.5,
                "sample_size": 1000,
                "matched": 355,
            }
        }
    )

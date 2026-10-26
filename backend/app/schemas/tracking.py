"""
Tracking schema models for validation and serialization.

This module defines Pydantic models for tracking-related data structures.
These models are used for experiment assignment and event tracking.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from backend.app.schemas.storable_text import StorableTextModel


class EventFields(BaseModel):
    """The fields of an event, with no rule about which keys it names.

    ``EventBase`` adds the rule that an event names an experiment or a flag;
    ``UntaggedEventCreate`` and ``EventResponse`` do not carry it (#217).
    """

    event_name: str = Field(..., min_length=1, max_length=100)
    event_type: str = Field("custom", min_length=1, max_length=100)
    user_id: Optional[str] = Field(None, max_length=255)
    session_id: Optional[str] = Field(None, max_length=255)
    experiment_id: Optional[str] = Field(None, max_length=255)
    feature_flag_id: Optional[str] = Field(None, max_length=255)
    variant_id: Optional[str] = Field(None, max_length=255)
    value: Optional[float] = None
    # Free-form event attributes; ``properties`` is the name used by the
    # tracking endpoints and services, ``metadata`` is kept for callers
    # that already send it.  EventService.build_event accepts either.
    properties: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("properties", "metadata", mode="before")
    @classmethod
    def parse_json_string(cls, v: Any) -> Any:
        """Accept a JSON-encoded string for the free-form attribute fields."""
        if isinstance(v, str):
            import json

            try:
                return json.loads(v) if v.strip() else None
            except json.JSONDecodeError as exc:
                raise ValueError(f"must be a JSON object: {exc}") from exc
        return v


class EventBase(EventFields):
    """Base model for event data."""

    @model_validator(mode="after")
    def validate_experiment_or_feature_flag(self) -> "EventBase":
        """Validate that either experiment_id or feature_flag_id is provided."""
        if not self.experiment_id and not self.feature_flag_id:
            raise ValueError("Either experiment_id or feature_flag_id must be provided")
        return self


class EventCreate(StorableTextModel, EventBase):
    """Model for creating a new event."""


class UntaggedEventCreate(StorableTextModel, EventFields):
    """An event that names no experiment and no flag (#217).

    ``/tracking/track`` and ``/tracking/batch`` build this one when the
    request gives neither key. The event is stored as history and counts in
    no experiment's results. Every other caller builds ``EventCreate``, which
    still refuses an event with neither id.
    """


class EventResponse(EventFields):
    """Model for event responses."""

    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssignmentBase(BaseModel):
    """Base model for assignment data."""

    user_id: Optional[str] = Field(None, max_length=255)
    session_id: Optional[str] = Field(None, max_length=255)
    experiment_id: Optional[str] = Field(None, max_length=255)
    feature_flag_id: Optional[str] = Field(None, max_length=255)
    variant_id: Optional[str] = Field(None, max_length=255)
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_experiment_or_feature_flag(self) -> "AssignmentBase":
        """Validate that either experiment_id or feature_flag_id is provided."""
        if not self.experiment_id and not self.feature_flag_id:
            raise ValueError("Either experiment_id or feature_flag_id must be provided")
        return self


class AssignmentCreate(AssignmentBase):
    """Model for creating a new assignment."""


class AssignmentResponse(AssignmentBase):
    """Model for assignment responses."""

    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExperimentMetrics(BaseModel):
    """Model for experiment metrics."""

    experiment_id: str
    start_date: datetime
    end_date: datetime
    metrics: List[str]

    model_config = ConfigDict(from_attributes=True)

    @field_validator("end_date")
    @classmethod
    def validate_end_date(cls, v: datetime, values: Dict[str, Any]) -> datetime:
        """Validate that end_date is after start_date."""
        if "start_date" in values and v <= values["start_date"]:
            raise ValueError("end_date must be after start_date")
        return v


class MetricResult(BaseModel):
    """Model for metric results."""

    metric_name: str
    variant_id: str
    value: float
    confidence_interval: Optional[List[float]] = None
    p_value: Optional[float] = None
    sample_size: int

    model_config = ConfigDict(from_attributes=True)


class ExperimentResults(BaseModel):
    """Model for experiment results."""

    experiment_id: str
    start_date: datetime
    end_date: datetime
    metrics: List[MetricResult]

    model_config = ConfigDict(from_attributes=True)


class AssignmentRequest(StorableTextModel):
    """Model for requesting a variant assignment."""

    experiment_key: str = Field(
        ..., min_length=1, max_length=100, description="Experiment identifier"
    )
    user_id: str = Field(
        ..., min_length=1, max_length=255, description="User identifier"
    )
    context: Optional[Dict[str, Any]] = Field(
        None, description="User context for targeting"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "experiment_key": "button-color-test",
                "user_id": "user-123",
                "context": {
                    "country": "US",
                    "device": "mobile",
                    "browser": "chrome",
                    "is_returning": True,
                },
            }
        }
    )


class VariantAssignmentResponse(BaseModel):
    """Response for POST /tracking/assign: the variant a user was assigned to.

    Users that are not eligible (global holdout, mutual exclusion group or
    targeting rules), and every user of an experiment that is not ACTIVE,
    still get HTTP 200 with the experiment's control variant so SDKs render
    the default experience; ``assigned`` is ``False`` and ``reason`` says
    why.  No assignment or exposure is recorded for them.
    """

    experiment_key: str
    user_id: str
    variant_id: str
    variant_name: str
    is_control: bool = False
    configuration: Optional[Dict[str, Any]] = None
    assigned: bool = Field(
        True,
        description="False when the user was not enrolled; the control variant is returned",
    )
    reason: str = Field(
        "assigned",
        description=(
            "assigned | holdout | mutual_exclusion | targeting, or the status "
            "of an experiment that is not ACTIVE: draft | paused | completed "
            "| archived"
        ),
    )

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "experiment_key": "button-color-test",
                "user_id": "user-123",
                "variant_id": "7b0a1a2e-4c3d-4f5e-8a9b-0c1d2e3f4a5b",
                "variant_name": "control",
                "is_control": True,
                "configuration": {"color": "blue"},
                "assigned": False,
                "reason": "holdout",
            }
        },
    )


#: The most users one ``POST /api/v1/tracking/assign/batch`` request may carry.
MAX_BATCH_ASSIGN_USERS = 1000

#: The 422 message for a list longer than :data:`MAX_BATCH_ASSIGN_USERS`.
BATCH_TOO_LONG_MESSAGE = "at most 1,000 users per request; split the list"

#: The 422 message for a repeated ``user_id``; it names positions, never the id.
BATCH_DUPLICATE_MESSAGE = (
    "duplicate user_id; each user may appear once per request "
    "(first seen at users[{first}])"
)


class BatchAssignmentUser(StorableTextModel):
    """One user of ``POST /api/v1/tracking/assign/batch``."""

    user_id: str = Field(
        ..., min_length=1, max_length=255, description="User identifier"
    )
    context: Optional[Dict[str, Any]] = Field(
        None,
        description="This user's attributes, matched against the experiment's "
        "targeting rules as on POST /api/v1/tracking/assign",
    )

    model_config = ConfigDict(extra="forbid")


class AssignmentBatchRequest(StorableTextModel):
    """Assign up to 1,000 users to one ACTIVE experiment.

    ``users`` holds 1 to 1,000 entries with distinct ``user_id`` values.
    Unknown fields are refused, here and in each user, so a misspelt
    ``context`` is a 422 rather than a user with no attributes.
    """

    experiment_key: str = Field(
        ..., min_length=1, max_length=100, description="Experiment identifier"
    )
    users: List[BatchAssignmentUser] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_ASSIGN_USERS,
        description="1 to 1,000 users, each user_id once",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "experiment_key": "spring-email",
                "users": [
                    {"user_id": "cust-001", "context": {"country": "US"}},
                    {"user_id": "cust-002", "context": {"country": "DE"}},
                ],
            }
        },
    )

    @field_validator("users", mode="before")
    @classmethod
    def _at_most_the_cap(cls, value: Any) -> Any:
        # Before the items are validated, so an oversized list is refused
        # without building a model for every entry.
        if isinstance(value, list) and len(value) > MAX_BATCH_ASSIGN_USERS:
            raise PydanticCustomError("too_long", BATCH_TOO_LONG_MESSAGE)
        return value

    @field_validator("users", mode="after")
    @classmethod
    def _each_user_once(
        cls, value: List[BatchAssignmentUser]
    ) -> List[BatchAssignmentUser]:
        first_seen: Dict[str, int] = {}
        for index, user in enumerate(value):
            first = first_seen.setdefault(user.user_id, index)
            if first != index:
                # The error's location is the repeated entry; ``input`` is
                # dropped from every 422 body (validation_errors.py) and the
                # message carries positions only.
                raise ValidationError.from_exception_data(
                    cls.__name__,
                    [
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "duplicate_user_id",
                                BATCH_DUPLICATE_MESSAGE,
                                {"first": first},
                            ),
                            loc=(index, "user_id"),
                            input=None,
                        )
                    ],
                )
        return value


class BatchVariant(BaseModel):
    """A variant of the experiment, keyed by its id in ``variants``."""

    name: str
    is_control: bool = False
    configuration: Optional[Dict[str, Any]] = None


class BatchAssignment(BaseModel):
    """One user's answer, as ``POST /api/v1/tracking/assign`` would give it."""

    user_id: str
    variant_id: str = Field(
        ..., description="A key of variants; the control when assigned is false"
    )
    assigned: bool = Field(
        ..., description="False when the user was not enrolled; nothing was recorded"
    )
    reason: str = Field(
        ..., description="assigned | holdout | mutual_exclusion | targeting"
    )


class BatchAssignmentCounts(BaseModel):
    """How many users of the request ended with each ``reason``."""

    assigned: int = 0
    holdout: int = 0
    mutual_exclusion: int = 0
    targeting: int = 0


class AssignmentBatchResponse(BaseModel):
    """Response for ``POST /api/v1/tracking/assign/batch``.

    ``assignments`` is in request order, one entry per user.
    """

    experiment_key: str
    variants: Dict[str, BatchVariant]
    assignments: List[BatchAssignment]
    counts: BatchAssignmentCounts


class EventRequest(StorableTextModel):
    """
    Model for tracking an event through the public tracking API.

    Experiments and feature flags are referenced by their human-readable keys
    (the SDKs do not know internal ids); the endpoint resolves them and picks
    up the user's variant assignment automatically.
    """

    event_type: str = Field(..., min_length=1, max_length=100)
    event_name: Optional[str] = Field(None, max_length=255)
    user_id: str = Field(..., min_length=1, max_length=255)
    experiment_key: Optional[str] = Field(None, max_length=100)
    feature_flag_key: Optional[str] = Field(None, max_length=100)
    value: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[datetime] = None

    # Both keys are optional (#217). With neither, the endpoint stores the
    # event as history; a key that is given but not found answers 404.

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_type": "purchase",
                "user_id": "user-123",
                "experiment_key": "button-color-test",
                "value": 49.99,
                "metadata": {
                    "product_id": "prod-456",
                    "category": "electronics",
                    "currency": "USD",
                },
            }
        }
    )


class EventBatchRequest(BaseModel):
    """Model for batch tracking multiple events."""

    events: List[EventRequest] = Field(
        ..., min_length=1, max_length=100, description="List of events to track"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [
                    {
                        "event_type": "page_view",
                        "user_id": "user-123",
                        "experiment_key": "button-color-test",
                        "metadata": {
                            "page": "/product/456",
                            "referrer": "/category/electronics",
                        },
                    },
                    {
                        "event_type": "add_to_cart",
                        "user_id": "user-123",
                        "experiment_key": "button-color-test",
                        "value": 49.99,
                        "metadata": {"product_id": "prod-456", "quantity": 1},
                    },
                ]
            }
        }
    )


class EventBatchResponse(BaseModel):
    """Model for batch event tracking response."""

    success_count: int = Field(..., description="Number of events successfully tracked")
    failure_count: int = Field(..., description="Number of events that failed to track")
    errors: Optional[List[Dict[str, Any]]] = Field(
        None, description="Details of failed events"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success_count": 1,
                "failure_count": 1,
                "errors": [
                    {
                        "index": 1,
                        "event_type": "add_to_cart",
                        "user_id": "user-123",
                        "error": "Invalid experiment key",
                    }
                ],
            }
        }
    )


class EventQueryParams(BaseModel):
    """Model for querying events."""

    experiment_id: Optional[UUID4] = Field(None, description="Filter by experiment ID")
    feature_flag_id: Optional[UUID4] = Field(
        None, description="Filter by feature flag ID"
    )
    user_id: Optional[str] = Field(
        None, max_length=255, description="Filter by user ID"
    )
    event_type: Optional[str] = Field(
        None, max_length=100, description="Filter by event type"
    )
    start_date: Optional[datetime] = Field(
        None, description="Filter events after this date"
    )
    end_date: Optional[datetime] = Field(
        None, description="Filter events before this date"
    )
    limit: int = Field(
        100, ge=1, le=1000, description="Maximum number of events to return"
    )
    offset: int = Field(0, ge=0, description="Number of events to skip")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "experiment_id": "123e4567-e89b-12d3-a456-426614174000",
                "event_type": "purchase",
                "start_date": "2023-01-01T00:00:00Z",
                "end_date": "2023-01-31T23:59:59Z",
                "limit": 100,
                "offset": 0,
            }
        }
    )

    @field_validator("end_date")
    def validate_date_range(cls, v, values):
        """Validate that end_date is after start_date."""
        start_date = values.get("start_date")
        if start_date and v and v < start_date:
            raise ValueError("end_date must be after start_date")
        return v

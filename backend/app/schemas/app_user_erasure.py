"""Request and response of ``POST /api/v1/admin/app-users/erase`` (#2042, beta).

The rules of the erase are in ``backend/app/services/app_user_erasure.py``.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.storable_text import StorableTextModel


class AppUserEraseRequest(StorableTextModel):
    """The app user to erase. Any other field is refused with 422."""

    # Exactly the ids tracking can store: 1 to 255 characters with no NUL and
    # no lone surrogate (StorableTextModel), as AssignmentRequest.user_id.
    # Matched exactly: no trimming, no case folding, no pattern.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"user_id": "user-to-erase"}},
    )

    user_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "The id your app sent as user_id. Matched exactly, case and "
            "spaces included."
        ),
    )


class AppUserEraseCounts(BaseModel):
    """Rows this call deleted, per table."""

    events: int = Field(..., description="Events, exposures included.")
    raw_metrics: int = Field(..., description="Flag evaluations and latencies.")
    error_logs: int = Field(..., description="Client errors.")
    llm_evaluations: int = Field(..., description="LLM experiment evaluations.")
    event_dedup_keys: int = Field(..., description="Keys of events sent with an id.")
    assignments: int = Field(..., description="Experiment assignments.")
    holdout_population: int = Field(..., description="Global-holdout rows.")
    segment_members: int = Field(..., description="Id-list segment memberships.")
    feature_flag_overrides: int = Field(..., description="Per-user flag overrides.")


class AppUserEraseResponse(BaseModel):
    """What one call did. Counts only: never the id."""

    receipt_id: UUID = Field(
        ...,
        description=(
            "The entity_id of this call's two audit-log entries: "
            "GET /api/v1/audit-logs/?entity_id=<receipt_id>."
        ),
    )
    complete: bool = Field(
        ...,
        description=(
            "True when no row of the id is left. False when time ran out or "
            "a statement timed out: send the same request again."
        ),
    )
    ids_erased: int = Field(..., description="How many ids the erase covered.")
    deleted: AppUserEraseCounts

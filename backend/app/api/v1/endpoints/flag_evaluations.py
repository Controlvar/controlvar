"""
Locally evaluated flag counts (``POST /api/v1/tracking/evaluations``, beta).

A server-side SDK that evaluates flags in-process reports how many evaluations
it made, per flag, so that safety monitoring still has a denominator for the
errors reported through ``POST /api/v1/tracking/errors`` with a server-side
key.  See
``backend/app/services/sdk_evaluation_service.py`` for how the counts are
stored and capped.

The route requires an API key carrying the ``sdk:ruleset`` scope, the same
scope that grants the local-evaluation ruleset: only a server evaluating flags
locally has anything to report here.  It is mounted under the ``/tracking``
prefix, so it shares the SDK rate limit.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Literal

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.sdk_scope import require_sdk_ruleset_key
from backend.app.models.api_key import APIKey
from backend.app.schemas.storable_text import StorableTextModel
from backend.app.services.sdk_evaluation_service import (
    EvaluationReport,
    record_local_evaluations,
)

router = APIRouter()

#: Most entries accepted in one report.
MAX_ENTRIES = 1000
#: Largest ``count`` accepted in one entry.
MAX_COUNT = 1_000_000
#: Longest window one entry may describe.
MAX_WINDOW = timedelta(minutes=10)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class FlagEvaluationCount(StorableTextModel):
    """How many times one flag was evaluated locally in one window."""

    flag_key: str = Field(..., min_length=1, max_length=100)
    count: int = Field(..., ge=1, le=MAX_COUNT, description="Evaluations made")
    enabled_count: int = Field(
        ..., ge=0, description="How many of them returned enabled"
    )
    window_start: datetime = Field(..., description="Start of the counting window")
    window_end: datetime = Field(..., description="End of the counting window")

    @model_validator(mode="after")
    def _consistent(self) -> "FlagEvaluationCount":
        if self.enabled_count > self.count:
            raise ValueError("enabled_count must not exceed count")
        start, end = _naive_utc(self.window_start), _naive_utc(self.window_end)
        if end < start:
            raise ValueError("window_end must not be before window_start")
        if end - start > MAX_WINDOW:
            raise ValueError("a window must not be longer than 10 minutes")
        return self


class FlagEvaluationReport(BaseModel):
    """A batch of per-flag evaluation counts."""

    evaluations: List[FlagEvaluationCount] = Field(
        ..., min_length=1, max_length=MAX_ENTRIES
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "evaluations": [
                    {
                        "flag_key": "new-checkout",
                        "count": 1200,
                        "enabled_count": 300,
                        "window_start": "2026-09-27T12:00:00Z",
                        "window_end": "2026-09-27T12:01:00Z",
                    }
                ]
            }
        }
    )


class FlagEvaluationError(BaseModel):
    """A flag whose count was not, or not fully, recorded."""

    flag_key: str
    code: Literal["unknown_flag", "ceiling"]
    message: str
    requested: int
    accepted: int


class FlagEvaluationReportResponse(BaseModel):
    """What was recorded."""

    accepted: int = Field(..., description="Evaluations recorded, over all flags")
    errors: List[FlagEvaluationError] = Field(default_factory=list)


@router.post(
    "/evaluations",
    response_model=FlagEvaluationReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Report flags evaluated locally by a server-side SDK (beta)",
    response_description="How many evaluations were recorded, and any not recorded",
    openapi_extra={"x-stability": "beta"},
)
def report_flag_evaluations(
    report: FlagEvaluationReport = Body(...),
    db: Session = Depends(deps.get_db),
    api_key: APIKey = Depends(require_sdk_ruleset_key),
) -> FlagEvaluationReportResponse:
    """
    Record per-flag evaluation counts from an SDK that evaluates flags locally.

    Each accepted count is stored as flag evaluations stamped with the time the
    server received the report (the windows are kept as metadata), so safety
    monitoring counts them towards the flag's error-rate denominator at once.

    Counts are capped per API key, flag and minute (100,000) and per flag and
    minute across all keys (1,000,000); the excess is not recorded and is
    listed in ``errors`` with code ``ceiling``. Unknown flag keys are skipped
    and listed with code ``unknown_flag``. Entries for the same flag are merged.

    **Authentication**: an API key with the ``sdk:ruleset`` scope in the
    X-API-Key header.

    Raises:
        HTTPException 401: If the API key is missing or invalid
        HTTPException 403: If the API key does not have the ``sdk:ruleset`` scope
        HTTPException 422: If the report is malformed
    """
    received_at = datetime.utcnow()
    result = record_local_evaluations(
        db,
        api_key.id,
        (
            EvaluationReport(
                flag_key=entry.flag_key,
                count=entry.count,
                enabled_count=entry.enabled_count,
                window_start=_naive_utc(entry.window_start),
                window_end=_naive_utc(entry.window_end),
            )
            for entry in report.evaluations
        ),
        received_at=received_at,
    )
    return FlagEvaluationReportResponse(
        accepted=result.accepted,
        errors=[FlagEvaluationError(**error) for error in result.errors],
    )

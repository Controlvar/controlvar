"""
One experiment's results counted for the members of one ID-list segment
(#1933).

Used by ``GET /api/v1/results/{experiment_id}/segments/{segment_id}`` (beta).
The fields shared with ``ExperimentResultsResponse`` have the same types and
reuse its components unchanged; the response has no ``sequential_testing``,
``bayesian_results`` or ``breakdown``, so no whole-experiment block can be
read as the segment's.

``SegmentKind`` and ``SegmentStatus`` are the segment schemas' enums, never the
models': the model's ``SegmentKind`` would add a second component of the same
name and rename the one every segment operation uses.
"""

from datetime import datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.results import (
    CorrectionMethod,
    ExperimentSummary,
    MetricResult,
    SRMResult,
)
from backend.app.schemas.segment import SegmentKind, SegmentStatus


class ResultsSegment(BaseModel):
    """The segment the results were counted for, and how its members were read."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="The segment's id.")
    name: str = Field(..., description="The segment's name.")
    kind: SegmentKind = Field(
        ..., description="Always id_list: only an ID-list segment is accepted."
    )
    status: SegmentStatus = Field(
        ...,
        description=(
            "The segment's status when the results were computed. A segment "
            "in any status is accepted."
        ),
    )
    membership: Literal["current"] = Field(
        ...,
        description=(
            "Always current: the list's user ids when the request was made, "
            "not when each user was assigned."
        ),
    )
    member_count: int = Field(
        ...,
        ge=0,
        description=(
            "How many user ids the list held when it was read, members not "
            "assigned to the experiment included."
        ),
    )
    experiment_users: int = Field(
        ...,
        ge=0,
        description=(
            "Distinct users assigned to the experiment, members of the list or not."
        ),
    )
    experiment_srm_warning: Optional[bool] = Field(
        None,
        description=(
            "The warning of the whole experiment's sample ratio check. Null "
            "when that check is not defined (a bandit, fewer than two "
            "allocated variants, no assignments)."
        ),
    )


class SegmentResultsResponse(BaseModel):
    """An experiment's main results, counted only for its assigned users whose
    user_id is in one ID-list segment."""

    model_config = ConfigDict(from_attributes=True)

    experiment_id: UUID = Field(..., description="Unique identifier of the experiment.")
    experiment_name: str = Field(
        ..., description="Human-readable name of the experiment."
    )
    status: str = Field(
        ...,
        description=(
            "Current lifecycle status of the experiment: 'draft', 'active', "
            "'paused', 'completed', or 'archived'."
        ),
    )
    start_date: Optional[datetime] = Field(
        None,
        description="UTC timestamp when the experiment started, or None if not yet started.",
    )
    end_date: Optional[datetime] = Field(
        None,
        description="UTC timestamp when the experiment ended, or None if still running.",
    )
    confidence_level: Annotated[float, Field(ge=0.80, le=0.99)] = Field(
        default=0.95,
        description=(
            "Confidence level the results were computed at: the request's, or "
            "the experiment's stored one."
        ),
    )
    correction_method: CorrectionMethod = Field(
        default=CorrectionMethod.NONE,
        description=(
            "Multiple-comparison correction applied across each metric's "
            "treatments within the segment: the request's, or the "
            "experiment's stored one. It does not cover the choice of segment."
        ),
    )
    sample_size_adequate: bool = Field(
        ...,
        description=(
            "True when every variant's users in the segment reach the primary "
            "metric's minimum sample size."
        ),
    )
    computed_at: datetime = Field(
        ..., description="UTC timestamp at which these results were computed."
    )
    summary: ExperimentSummary = Field(
        ...,
        description=(
            "The summary for the segment's users. Its recommendation is "
            "INCONCLUSIVE whenever their sample ratio check or the whole "
            "experiment's warns. Exploratory: do not automate a decision from it."
        ),
    )
    metrics: List[MetricResult] = Field(
        ..., description="Every metric, primary first, for the segment's users."
    )
    srm: Optional[SRMResult] = Field(
        None,
        description=(
            "The sample ratio check of the segment's users alone. Null for a "
            "bandit, for fewer than two allocated variants, and when no "
            "member is assigned."
        ),
    )
    segment: ResultsSegment = Field(
        ..., description="The segment these results were counted for."
    )
    is_exploratory: bool = Field(
        default=True,
        description=(
            "Always true. The correction covers the comparisons within the "
            "segment, not the choice of segment."
        ),
    )

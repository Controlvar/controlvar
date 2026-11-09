"""
The balanced-groups results of an experiment that balances its split within
groups (#448).

Used by ``GET /api/v1/results/{experiment_id}/balanced-groups`` (beta).
"""

from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.schemas.dimensional import DimensionalBreakdownResponse

#: Why ``overall`` is null.
NoEstimateReason = Literal["mean_metric", "sequential_testing", "too_few_users"]


class BalancedGroupsEstimate(BaseModel):
    """One treatment against the control, over every group."""

    model_config = ConfigDict(from_attributes=True)

    variant_id: str = Field(..., description="The treatment variant's id.")
    variant_name: str = Field(..., description="The treatment variant's name.")
    absolute_difference: Optional[float] = Field(
        None,
        description=(
            "The treatment's conversion rate minus the control's in each group, "
            "weighted by the group's share of the experiment's users (every "
            "variant's), as a proportion (0.02 is 2 percentage points). Null "
            "when the treatment has no users."
        ),
    )
    absolute_difference_ci: Optional[Tuple[float, float]] = Field(
        None,
        description=(
            "The normal interval of absolute_difference at the experiment's "
            "confidence_level. Null when standard_error is 0 or null."
        ),
    )
    standard_error: Optional[float] = Field(
        None,
        description="The standard error of absolute_difference.",
    )
    relative_improvement_pct: Optional[float] = Field(
        None,
        description=(
            "absolute_difference over the control's conversion rate weighted "
            "the same way, in percent. Null when that rate is 0."
        ),
    )
    p_value: Optional[float] = Field(
        None,
        description=(
            "Two-sided normal p-value of absolute_difference. Null when "
            "standard_error is 0 or null, never 0.0."
        ),
    )
    adjusted_p_value: Optional[float] = Field(
        None,
        description=(
            "p_value corrected across the treatments with the experiment's "
            "correction_method; null with no correction."
        ),
    )
    is_significant: bool = Field(
        ...,
        description=(
            "Whether adjusted_p_value (p_value with no correction) is below "
            "1 - confidence_level. Not a verdict: the experiment's results "
            "carry its decision."
        ),
    )


class MergedGroup(BaseModel):
    """A group with too few users to estimate, counted with a larger one."""

    group: str = Field(..., description="The group with too few users.")
    into: str = Field(..., description="The group it was counted with.")


class BalancedGroupsResponse(BaseModel):
    """
    The results of an experiment that balances its split within groups, by
    the group each user was balanced in, and one estimate over the groups.
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: str = Field(..., description="The experiment's id.")
    confidence_level: float = Field(
        ...,
        description="The experiment's confidence level, which the estimate uses.",
    )
    correction_method: str = Field(
        ...,
        description="The experiment's multiple-comparison correction.",
    )
    groups: DimensionalBreakdownResponse = Field(
        ...,
        description=(
            "One row per group: the breakdown's rows, with each user in the "
            "group stored when they were assigned. Exploratory, as every "
            "breakdown is: each row's significance uses adjusted_alpha, the "
            "Bonferroni threshold over the rows returned."
        ),
    )
    overall: Optional[List[BalancedGroupsEstimate]] = Field(
        None,
        description=(
            "One estimate per treatment of the primary metric, over every "
            "group; null with a reason. No CUPED adjustment is applied to it."
        ),
    )
    reason: Optional[NoEstimateReason] = Field(
        None,
        description=(
            "Why overall is null: mean_metric (the primary metric is not a "
            "conversion), sequential_testing (the experiment uses sequential "
            "testing) or too_few_users (no group has 2 users in every "
            "variant). Null when overall is given."
        ),
    )
    merged_groups: List[MergedGroup] = Field(
        default_factory=list,
        description=(
            "Groups with fewer than 2 users in a variant, each counted in "
            "overall with the largest group that has 2 in every variant."
        ),
    )
    users_outside_blocks: int = Field(
        ...,
        ge=0,
        description=(
            "Users split without balancing: assigned by the hash because the "
            "server was busy balancing others of their group, or by an older "
            "server version (whose rows stored no group and are counted under "
            "unknown)."
        ),
    )
    unknown_share: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description=(
            "Of the users whose group was stored, the share in unknown "
            "(assigned without the attribute). Users whose row stored no group "
            "are not counted. Null when no user's group was stored."
        ),
    )
    engine_version: str = Field(
        ENGINE_VERSION,
        description="Statistics engine version that produced these results.",
    )

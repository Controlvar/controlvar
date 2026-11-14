"""
Bandit schemas for Multi-Armed Bandit experiment optimization.

Defines the Pydantic v2 models used for:
- Reporting current bandit state and allocation weights (BanditStatusResponse)
- Describing per-variant statistics (BanditVariantWeight)
- Manual weight overrides by administrators (BanditUpdateRequest)
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.schemas.finite_number import FiniteFloat

# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class OptimizationType(str, Enum):
    """Traffic-optimization algorithm for an experiment."""

    FIXED = "fixed"
    THOMPSON_SAMPLING = "thompson_sampling"
    UCB1 = "ucb1"
    EPSILON_GREEDY = "epsilon_greedy"


# ---------------------------------------------------------------------------
# Component schemas
# ---------------------------------------------------------------------------


class BanditVariantWeight(BaseModel):
    """
    Current bandit allocation state for a single variant.

    Attributes
    ----------
    variant_id:
        Unique identifier of the variant.
    variant_name:
        Human-readable name of the variant.
    current_weight:
        Current traffic-allocation weight in [0.0, 1.0].
    successes:
        Cumulative success/conversion count.
    pulls:
        Total number of times this variant was shown.
    conversion_rate:
        Empirical conversion rate (successes / pulls).
    """

    model_config = ConfigDict(from_attributes=True)

    variant_id: str = Field(..., description="Unique variant identifier")
    variant_name: str = Field(..., description="Display name for the variant")
    current_weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Current traffic allocation weight [0.0, 1.0]",
    )
    successes: int = Field(
        ..., ge=0, description="Cumulative success / conversion count"
    )
    pulls: int = Field(
        ..., ge=0, description="Total impressions / pulls for this variant"
    )
    conversion_rate: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Empirical conversion rate (successes / pulls)",
    )


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class BanditStatusResponse(BaseModel):
    """
    Full bandit status snapshot for an experiment.

    Attributes
    ----------
    experiment_id:
        Identifier of the parent experiment.
    algorithm:
        Algorithm currently driving traffic allocation.
    current_weights:
        Per-variant allocation weights.
    total_pulls:
        Total pulls across all variants.
    regret_reduction_pct:
        Estimated regret reduction vs. uniform allocation (may be None early on).
    recommendation:
        Narrative recommendation: one of ``"EXPLORING"``, ``"CONVERGING"``,
        or ``"DEPLOYING_<VariantName>"``.
    last_updated:
        ISO-8601 timestamp of the last weight update (None if never updated).
    seed:
        Deterministic RNG seed used by the last Thompson-sampling tick
        (``blake2b(experiment_id | tick day | n_samples)``); ``None`` for
        deterministic algorithms or before the first tick.
    n_samples:
        Monte Carlo draws per variant used by the last Thompson-sampling
        tick; ``None`` for deterministic algorithms or before the first tick.
    engine_version:
        Version of the statistics engine that computed the current weights.
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: str = Field(..., description="Parent experiment identifier")
    algorithm: OptimizationType = Field(
        ..., description="Active optimization algorithm"
    )
    current_weights: List[BanditVariantWeight] = Field(
        ...,
        description="Per-variant allocation weights",
    )
    total_pulls: int = Field(..., ge=0, description="Total pulls across all variants")
    regret_reduction_pct: Optional[float] = Field(
        None,
        description="Estimated regret reduction vs. uniform allocation (%)",
    )
    recommendation: str = Field(
        ...,
        description=(
            "Narrative recommendation: 'EXPLORING', 'CONVERGING', "
            "or 'DEPLOYING_<VariantName>'"
        ),
    )
    last_updated: Optional[str] = Field(
        None,
        description="ISO-8601 timestamp of the last weight update",
    )
    seed: Optional[int] = Field(
        None,
        ge=0,
        description="Deterministic RNG seed used by the last Thompson-sampling tick",
    )
    n_samples: Optional[int] = Field(
        None,
        ge=1,
        description="Monte Carlo draws per variant used by the last Thompson-sampling tick",
    )
    engine_version: str = Field(
        ENGINE_VERSION,
        description="Statistics engine version that computed the current weights",
    )


# ---------------------------------------------------------------------------
# Update request schema
# ---------------------------------------------------------------------------


class BanditUpdateRequest(BaseModel):
    """
    Manual weight override for bandit allocation (admin / testing use).

    Weights must:
    * all be >= 0.0
    * sum to approximately 1.0 (within a tolerance of 1e-6 to allow
      floating-point rounding across many variants)
    """

    weights: Dict[str, FiniteFloat] = Field(
        ...,
        description="Mapping of variant_id → allocation weight; must sum to ~1.0",
    )

    @field_validator("weights")
    @classmethod
    def validate_weights_non_negative(cls, v: Dict[str, float]) -> Dict[str, float]:
        """All individual weights must be >= 0."""
        for variant_id, weight in v.items():
            if weight < 0.0:
                raise ValueError(
                    f"Weight for variant '{variant_id}' is negative ({weight}); "
                    "all weights must be >= 0.0"
                )
        return v

    @model_validator(mode="after")
    def validate_weights_sum_to_one(self) -> "BanditUpdateRequest":
        """Weights must sum to approximately 1.0 (tolerance: 1e-6)."""
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Weights must sum to 1.0 (got {total:.8f}); "
                "adjust variant weights so they total exactly 1.0"
            )
        return self

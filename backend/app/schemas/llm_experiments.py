"""
Pydantic v2 schemas for LLM Experiment API endpoints (EP-046).
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.core.config import settings

# ---------------------------------------------------------------------------
# Variant schemas
# ---------------------------------------------------------------------------


class CreateLLMVariantRequest(BaseModel):
    """Request schema for creating an LLM variant."""

    name: str
    is_control: bool = False
    traffic_split: float  # 0.0 – 1.0

    provider: str
    model_name: str
    system_prompt: str = ""
    prompt_template: str
    temperature: float = 0.7
    max_tokens: int = 1000
    additional_params: Dict[str, Any] = {}

    @field_validator("traffic_split")
    @classmethod
    def validate_traffic_split(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("traffic_split must be between 0.0 and 1.0")
        return v

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0")
        return v

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_tokens must be positive")
        return v


class UpdateLLMVariantRequest(BaseModel):
    """Request schema for updating an LLM variant."""

    name: Optional[str] = None
    traffic_split: Optional[float] = None
    system_prompt: Optional[str] = None
    prompt_template: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    additional_params: Optional[Dict[str, Any]] = None


class LLMVariantResponse(BaseModel):
    """Response schema for an LLM variant."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    llm_experiment_id: str
    name: str
    is_control: bool
    traffic_split: float
    provider: str
    model_name: str
    system_prompt: str
    prompt_template: str
    temperature: float
    max_tokens: int
    additional_params: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @field_validator("id", "llm_experiment_id", mode="before")
    @classmethod
    def coerce_uuid(cls, v: Any) -> str:
        return str(v)

    @field_validator("provider", mode="before")
    @classmethod
    def coerce_provider_enum(cls, v: Any) -> str:
        if hasattr(v, "value"):
            return v.value
        return str(v)


# ---------------------------------------------------------------------------
# Experiment schemas
# ---------------------------------------------------------------------------


class CreateLLMExperimentRequest(BaseModel):
    """Request schema for creating an LLM experiment."""

    name: str
    description: str = ""
    task_type: str
    evaluation_metric: str
    variants: List[CreateLLMVariantRequest]

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, v: str) -> str:
        valid = {
            "chat_completion",
            "text_generation",
            "classification",
            "summarization",
            "code_generation",
            "embedding",
        }
        if v not in valid:
            raise ValueError(f"task_type must be one of: {', '.join(sorted(valid))}")
        return v

    @field_validator("evaluation_metric")
    @classmethod
    def validate_evaluation_metric(cls, v: str) -> str:
        valid = {
            "human_rating",
            "latency",
            "cost",
            "accuracy",
            "relevance",
            "fluency",
            "business_metric",
        }
        if v not in valid:
            raise ValueError(
                f"evaluation_metric must be one of: {', '.join(sorted(valid))}"
            )
        return v

    @model_validator(mode="after")
    def validate_variants(self) -> "CreateLLMExperimentRequest":
        if len(self.variants) < 2:
            raise ValueError("Must have at least 2 variants (control + treatment)")
        control_count = sum(1 for v in self.variants if v.is_control)
        if control_count != 1:
            raise ValueError("Exactly 1 variant must be marked as control")
        total_split = sum(v.traffic_split for v in self.variants)
        if abs(total_split - 1.0) > 0.01:
            raise ValueError(
                f"traffic_split values must sum to 1.0 (got {total_split:.4f})"
            )
        return self


class UpdateLLMExperimentRequest(BaseModel):
    """Request schema for updating an LLM experiment."""

    name: Optional[str] = None
    description: Optional[str] = None
    task_type: Optional[str] = None
    evaluation_metric: Optional[str] = None


class LLMExperimentResponse(BaseModel):
    """Response schema for an LLM experiment."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    status: str
    task_type: str
    evaluation_metric: str
    experiment_id: Optional[str]
    created_by: Optional[str]
    variants: List[LLMVariantResponse]
    created_at: datetime
    updated_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def coerce_uuid(cls, v: Any) -> str:
        return str(v)

    @field_validator("experiment_id", "created_by", mode="before")
    @classmethod
    def coerce_optional_uuid(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)

    @field_validator("status", mode="before")
    @classmethod
    def coerce_status_enum(cls, v: Any) -> str:
        if hasattr(v, "value"):
            return v.value
        return str(v)

    @field_validator("task_type", mode="before")
    @classmethod
    def coerce_task_type_enum(cls, v: Any) -> str:
        if hasattr(v, "value"):
            return v.value
        return str(v)

    @field_validator("evaluation_metric", mode="before")
    @classmethod
    def coerce_eval_metric_enum(cls, v: Any) -> str:
        if hasattr(v, "value"):
            return v.value
        return str(v)


class LLMExperimentListResponse(BaseModel):
    """Paginated list response for LLM experiments."""

    items: List[LLMExperimentResponse]
    total: int
    page: int = 1
    page_size: int = 20


# ---------------------------------------------------------------------------
# Completion / evaluation schemas
# ---------------------------------------------------------------------------


class LLMCompleteRequest(BaseModel):
    """Request to get a completion from the assigned variant."""

    user_id: str
    input_variables: Dict[str, Any] = {}


class LLMCompleteResponse(BaseModel):
    """Response from a completion request."""

    variant_id: str
    variant_name: str
    provider: str
    model_name: str
    response: str
    latency_ms: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    evaluation_id: str


class SubmitEvaluationRequest(BaseModel):
    """Request to submit evaluation scores for a prior completion."""

    evaluation_id: str
    human_rating: Optional[float] = None  # 1–5
    business_metric_value: Optional[float] = None

    @field_validator("human_rating")
    @classmethod
    def validate_rating(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not 1.0 <= v <= 5.0:
            raise ValueError("human_rating must be between 1.0 and 5.0")
        return v


class LLMEvaluationResponse(BaseModel):
    """Response schema for an individual LLM evaluation."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    llm_experiment_id: str
    variant_id: str
    user_id: str
    input_variables: Dict[str, Any]
    rendered_prompt: str
    model_response: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    human_rating: Optional[float]
    auto_eval_score: Optional[float]
    business_metric_value: Optional[float]
    created_at: datetime

    @field_validator("id", "llm_experiment_id", "variant_id", mode="before")
    @classmethod
    def coerce_uuid(cls, v: Any) -> str:
        return str(v)


# ---------------------------------------------------------------------------
# Analytics / results schemas
# ---------------------------------------------------------------------------


class VariantStats(BaseModel):
    """Per-variant statistics for an LLM experiment."""

    variant_id: str
    variant_name: str
    is_control: bool
    n_evaluations: int
    n_auto_eval_unscored: int = Field(
        ...,
        ge=0,
        description=(
            "Evaluations with no judge score: not judged yet, or the call failed "
            "or gave no usable score; left out of the auto-eval statistics."
        ),
    )
    mean_latency_ms: Optional[float]
    latency_ci_lower: Optional[float]
    latency_ci_upper: Optional[float]
    mean_cost_usd: Optional[float]
    cost_ci_lower: Optional[float]
    cost_ci_upper: Optional[float]
    mean_auto_eval_score: Optional[float]
    auto_eval_ci_lower: Optional[float]
    auto_eval_ci_upper: Optional[float]
    mean_human_rating: Optional[float]
    mean_business_metric: Optional[float]
    business_metric_ci_lower: Optional[float]
    business_metric_ci_upper: Optional[float]
    # vs control; None when either variant has fewer than two values, or
    # neither variant's values vary
    p_value: Optional[float]
    effect_size: Optional[float]  # Cohen's d vs control; None in the same cases


class LLMExperimentResults(BaseModel):
    """Full analytics results for an LLM experiment."""

    experiment_id: str
    experiment_name: str
    status: str
    evaluation_metric: str
    variant_stats: List[VariantStats]
    winner_variant_id: Optional[str]
    winner_variant_name: Optional[str]
    winner_metric: Optional[Literal["business_metric", "auto_eval"]] = Field(
        default=None,
        description=(
            "The one metric every variant is compared on: business_metric when "
            "every variant has at least one business-metric value, otherwise "
            "auto_eval (the judge score), compared across the variants that "
            "have one. The winner and every variant's p_value and effect_size "
            "are on this metric. A variant with no evaluations has no "
            "business-metric value. Null when no variant has a value for the "
            "chosen metric."
        ),
    )
    total_evaluations: int
    total_auto_eval_unscored: int = Field(
        ...,
        ge=0,
        description=(
            "Evaluations in the experiment with no judge score: not judged yet, "
            "or the call failed or gave no usable score."
        ),
    )


class LLMJudgeRequest(BaseModel):
    """Request to run LLM-as-judge scoring."""

    criteria: str = "helpfulness"
    judge_model: str = Field(
        default_factory=lambda: settings.LLM_DEFAULT_JUDGE_MODEL,
        description="Model used to score responses; defaults to LLM_DEFAULT_JUDGE_MODEL.",
    )
    evaluation_ids: Optional[List[str]] = None  # None = score all


class LLMJudgeResult(BaseModel):
    """Result from LLM-as-judge scoring for a single evaluation.

    ``score`` is from 0 to 1, or None when no score was stored: then ``error``
    says why, ``"call_failed"`` (the judge call failed) or ``"no_score"`` (the
    reply had no JSON ``score`` from 0 to 1).
    """

    evaluation_id: str
    score: Optional[float]  # 0–1, or None when no score was stored
    reasoning: str
    judge_model: str
    error: Optional[Literal["call_failed", "no_score"]] = None

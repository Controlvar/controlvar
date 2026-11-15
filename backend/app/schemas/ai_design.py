"""
Pydantic schemas for the AI Design endpoints.

Covers:
- ExperimentDesignSuggestionResponse
- ResultsInterpretationResponse
- SampleSizeEstimateResponse
- DesignRequest / InterpretRequest
- ExperimentTemplateResponse
- MCP server manifest schemas
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.finite_number import FiniteFloat

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class DesignRequest(BaseModel):
    """Request body for POST /ai/design."""

    description: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Natural language description of the experiment goal (10–1000 chars).",
    )
    experiment_type: str = Field(
        default="default",
        description="Type of experiment: checkout, onboarding, pricing, email, landing_page, or default.",
    )


class InterpretRequest(BaseModel):
    """Request body for POST /ai/interpret/{experiment_id}."""

    experiment_id: str = Field(..., description="Experiment identifier.")
    variant_name: str = Field(..., description="Name of the variant being interpreted.")
    p_value: FiniteFloat = Field(
        ..., description="Statistical p-value from the experiment."
    )
    relative_improvement_pct: FiniteFloat = Field(
        ..., description="Relative improvement percentage (positive = better)."
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ExperimentDesignSuggestionResponse(BaseModel):
    """Response for POST /ai/design."""

    model_config = ConfigDict(from_attributes=True)

    hypothesis: str
    primary_metric: str
    guardrail_metrics: List[str]
    recommended_sample_size: int
    recommended_duration_days: int
    variant_descriptions: List[str]
    confidence: str  # "ai_generated" | "template_based"
    reasoning: Optional[str] = None


class ResultsInterpretationResponse(BaseModel):
    """Response for POST /ai/interpret/{experiment_id}."""

    model_config = ConfigDict(from_attributes=True)

    summary: str
    recommendation: str  # "ship" | "continue_testing" | "stop_futility"
    confidence_statement: str
    key_findings: List[str]
    generated_by: str  # "ai" | "template"


class SampleSizeEstimateResponse(BaseModel):
    """Response for GET /ai/sample-size."""

    model_config = ConfigDict(from_attributes=True)

    required_per_variant: int
    total_required: int
    days_to_significance: Optional[int] = None
    assumptions: Dict[str, Any]


class ExperimentTemplateResponse(BaseModel):
    """Response for GET /ai/templates and GET /ai/templates/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    experiment_type: str
    primary_metric: str
    guardrail_metrics: List[str]
    recommended_duration_days: int
    minimum_sample_size: int
    expected_effect_size: float
    variant_descriptions: List[str]
    tags: List[str]


# ---------------------------------------------------------------------------
# MCP schemas
# ---------------------------------------------------------------------------


class MCPToolSchema(BaseModel):
    """Definition of a single MCP tool exposed to AI agents."""

    name: str
    description: str
    parameters: Dict[str, Any]


class MCPManifestResponse(BaseModel):
    """MCP server manifest listing all available tools."""

    name: str
    version: str
    tools: List[MCPToolSchema]

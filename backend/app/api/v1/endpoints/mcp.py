"""
MCP (Model Context Protocol) server endpoint.

Exposes the experimentation platform APIs to AI coding assistants and agents
via a standardised tool manifest. No authentication is required for manifest
discovery — this follows the MCP convention of open discovery.

Routes:
    GET /api/v1/mcp/manifest — Return MCP tool manifest (public)
"""

import logging

from fastapi import APIRouter

from backend.app.schemas.ai_design import MCPManifestResponse, MCPToolSchema

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Available MCP tools
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    MCPToolSchema(
        name="create_experiment",
        description="Create a new A/B experiment on the experimentation platform",
        parameters={
            "name": {"type": "string", "description": "Experiment name"},
            "hypothesis": {"type": "string", "description": "Experiment hypothesis"},
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of metric names to track",
            },
        },
    ),
    MCPToolSchema(
        name="get_results",
        description="Retrieve the statistical results for a specific experiment",
        parameters={
            "experiment_id": {
                "type": "string",
                "description": "UUID or key of the experiment",
            },
        },
    ),
    MCPToolSchema(
        name="toggle_feature_flag",
        description="Enable or disable a feature flag by key",
        parameters={
            "flag_key": {"type": "string", "description": "Feature flag key"},
            "enabled": {
                "type": "boolean",
                "description": "True to enable, False to disable",
            },
        },
    ),
    MCPToolSchema(
        name="suggest_experiment",
        description="Get AI-powered experiment design suggestions based on a natural language description",
        parameters={
            "description": {
                "type": "string",
                "description": "Natural language description of the experiment goal",
            },
            "experiment_type": {
                "type": "string",
                "description": "Type of experiment (checkout, onboarding, pricing, email, landing_page)",
            },
        },
    ),
    MCPToolSchema(
        name="interpret_results",
        description="Get a plain-English interpretation and recommendation for experiment results",
        parameters={
            "experiment_id": {
                "type": "string",
                "description": "UUID or key of the experiment",
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/manifest",
    response_model=MCPManifestResponse,
    summary="MCP Server Manifest",
    description=(
        "Return the MCP server manifest listing all available tools. "
        "No authentication is required for manifest discovery."
    ),
)
def get_mcp_manifest() -> MCPManifestResponse:
    """Return the MCP tool manifest for AI agent discovery."""
    return MCPManifestResponse(
        name="controlvar",
        version="1.0.0",
        tools=MCP_TOOLS,
    )

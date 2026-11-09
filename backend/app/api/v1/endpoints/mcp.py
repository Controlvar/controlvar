"""
The tool list of the Controlvar MCP server.

The MCP server is a separate program (``sdk/mcp``, ``@controlvar/mcp-server``)
that an AI assistant starts on the user's computer and talks to over stdio. It
calls this API with the user's own API key. Nothing in this API speaks MCP: the
manifest below only *lists* the server's tools, in the vocabulary of the Model
Context Protocol, so that a person or a tool can see what the server offers
without installing it. No authentication is required to read the list.

``MCP_TOOLS`` equals ``sdk/mcp/tools.json``, which is the server's own answer to
``tools/list``; a unit test fails when the two differ.

Routes:
    GET /api/v1/mcp/manifest — Return the MCP server's tool list (public)
"""

import logging

from fastapi import APIRouter

from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.schemas.ai_design import MCPManifestResponse, MCPToolSchema

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

# ---------------------------------------------------------------------------
# The six tools the MCP server offers (sdk/mcp/tools.json)
# ---------------------------------------------------------------------------

_FLAG_KEY = {
    "type": "string",
    "description": "The flag's key, as list_feature_flags shows it.",
}
_EXPERIMENT_ID = {
    "type": "string",
    "description": "The experiment's id, from list_experiments.",
}
_LIMIT = {
    "type": "integer",
    "description": "How many to return, from 1 to 100. Default 50.",
}
_SKIP = {
    "type": "integer",
    "description": "How many to skip, to read the next page. Default 0.",
}

MCP_TOOLS = [
    MCPToolSchema(
        name="list_feature_flags",
        description=(
            "Lists the feature flags on the Controlvar instance. Each item has its "
            "`key`, `name`, `status` (ACTIVE, INACTIVE or ARCHIVED), `value_type`, "
            "`rollout_percentage`, `tags`, `updated_at`, `stale` and `stale_reasons`, "
            "and the answer has `stale_days` and `evaluations_seen`. `stale` is true "
            "when the flag looks ready to remove, and `stale_reasons` says why "
            "(not_evaluated, on_for_everyone, off_for_everyone); this is advice only, "
            "and nothing is archived or changed. `stale_days` is the number of days "
            "those reasons look back, and `evaluations_seen` is false when the "
            "instance recorded no flag evaluation in that time, so not_evaluated is "
            "not reported. `search` matches part of a flag's name or key; `status` "
            "keeps one status; `stale` keeps the stale flags (true) or the others "
            "(false). Returns at most `limit` flags (default 50, at most 100) and "
            "`total`, the number that match. When `total` is larger, call again with "
            "`skip`. The list is ordered by creation time, oldest first, so paging "
            "with `skip` reads each flag once unless flags are created or deleted "
            "meanwhile; to read one flag by its key, use get_feature_flag."
        ),
        parameters={
            "search": {
                "type": "string",
                "description": "Part of a flag's name or key, 1 to 100 characters.",
            },
            "status": {"type": "string", "description": "Only flags with this status."},
            "stale": {
                "type": "boolean",
                "description": (
                    "true returns only stale flags, false the rest; omit for all. "
                    "Archived flags are never stale."
                ),
            },
            "limit": dict(_LIMIT),
            "skip": dict(_SKIP),
        },
    ),
    MCPToolSchema(
        name="get_feature_flag",
        description=(
            "Reads one feature flag by its key: status, rollout percentage, targeting "
            "rules, the values it serves when on and off, tags and timestamps. A flag "
            "that is not ACTIVE serves its off value to every user."
        ),
        parameters={"flag_key": dict(_FLAG_KEY)},
    ),
    MCPToolSchema(
        name="evaluate_feature_flag",
        description=(
            "Asks the instance what a flag serves one user, the same answer an SDK "
            "gets: `enabled`, the `value` served, and `reason`. The reasons are: "
            "targeting_rule (a targeting rule matched); rollout (the rollout "
            "percentage decided); inactive (the flag is not ACTIVE); error (the rules "
            "could not be evaluated, so the flag is off). Put the user's attributes "
            'in `context`, for example {"country": "DE", "plan": "pro"}, so targeting '
            "rules can match them. Each call is recorded as one evaluation of the "
            "flag, with this user_id and context, as an SDK's is."
        ),
        parameters={
            "flag_key": dict(_FLAG_KEY),
            "user_id": {
                "type": "string",
                "description": "The user to evaluate the flag for, as your app identifies them.",
            },
            "context": {
                "type": "object",
                "description": "The user's attributes, as a JSON object, for the flag's targeting rules.",
            },
        },
    ),
    MCPToolSchema(
        name="list_experiments",
        description=(
            "Lists experiments. Each item has its `id` (which get_experiment and "
            "get_experiment_results take), `key`, `name`, `status` (draft, active, "
            "paused, completed or archived), `experiment_type`, `start_date`, "
            "`end_date` and `updated_at`. `search` matches part of a name or "
            "description, or a whole tag, but not the key; `status` keeps one status. "
            "Paging is the same as list_feature_flags: at most `limit` items, `total`, "
            "and `skip` for the next page."
        ),
        parameters={
            "search": {
                "type": "string",
                "description": "Part of an experiment's name or description, or a whole tag, 1 to 100 characters.",
            },
            "status": {
                "type": "string",
                "description": "Only experiments with this status.",
            },
            "limit": dict(_LIMIT),
            "skip": dict(_SKIP),
        },
    ),
    MCPToolSchema(
        name="get_experiment",
        description=(
            "Reads one experiment by its id (from list_experiments): hypothesis, "
            "status, variants and their traffic allocation, metrics (one is primary), "
            "targeting rules and dates."
        ),
        parameters={"experiment_id": dict(_EXPERIMENT_ID)},
    ),
    MCPToolSchema(
        name="get_experiment_results",
        description=(
            "Reads the statistical results of an experiment that has started (a draft "
            "has none). For each metric and variant: the sample size, the conversion "
            "rate or mean, the difference from the control with its confidence "
            "interval, the p-value and whether it is significant, plus a summary and "
            "recommendation. Before stating a conclusion, check `srm` (a sample-ratio "
            "mismatch: users were not split as configured, so the comparison cannot "
            "be trusted) and `sample_size_adequate` (false: too few users yet)."
        ),
        parameters={"experiment_id": dict(_EXPERIMENT_ID)},
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
        "Return the tool list of the Controlvar MCP server, which runs on your "
        "computer and calls this API with your API key. Nothing on this API answers "
        "an MCP client. No authentication is required to read the list."
    ),
)
def get_mcp_manifest() -> MCPManifestResponse:
    """Return the MCP server's tool list."""
    return MCPManifestResponse(
        name="controlvar",
        version="1.0.0",
        tools=MCP_TOOLS,
    )

"""The MCP tool's user_id limit is the API's.

``evaluate_feature_flag`` takes a ``user_id`` the API then stores in a
String(255) column. A tool that allows one more character than the API turns
a refused input into a 422 from the API. ``sdk/mcp/tools.json`` is what the
server answers to ``tools/list``. Reads files only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.api.v1.endpoints.feature_flags import USER_ID_MAX_LENGTH

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOLS_JSON = REPO_ROOT / "sdk" / "mcp" / "tools.json"


@pytest.mark.regression
def test_evaluate_feature_flag_user_id_limit_equals_the_apis() -> None:
    tools = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    tool = next(t for t in tools if t["name"] == "evaluate_feature_flag")
    assert (
        tool["inputSchema"]["properties"]["user_id"]["maxLength"] == USER_ID_MAX_LENGTH
    )

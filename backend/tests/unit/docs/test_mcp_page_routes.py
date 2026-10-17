"""Every request ``docs/mcp-server.md`` documents is a route the API serves.

The page used to tell a reader to point an MCP client at
``/api/v1/mcp/manifest``. An MCP client opens with a POST (``initialize``), and
that route answers only GET, so the client got ``405``. A check that every
documented PATH exists passed throughout: the path was real, the method was
not. So this compares (method, path) pairs with the OpenAPI snapshot:

* each ``curl`` in a ``bash`` block gives one pair: the method from ``-X`` /
  ``--request``, else POST when it sends a body, else GET; the path from its
  ``localhost`` URL, without the query string;
* each ``url`` given to an MCP client (a block that holds ``mcpServers``) is a
  POST, because that is the first request an MCP client makes to it.

A pair matches an operation in ``docs/api/openapi-v1.full.json`` when the
method is the same and the path is the same, a ``{parameter}`` segment
matching any one segment. A ``curl`` with no ``localhost`` URL is refused
rather than skipped. Standard library only: no backend module is imported.
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "mcp-server.md"
SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"

_FENCE = re.compile(
    r"^(?P<fence>`{3,})(?P<info>[^\n]*)\n(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_URL = re.compile(
    r"^(?:https?://)?(?:localhost|127\.0\.0\.1)(?::\d+)?(?P<path>/[^?#]*)"
)
_MCP_URL = re.compile(r'"url"\s*:\s*"(?P<url>[^"]+)"')
_BODY_FLAGS = {
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--json",
    "-F",
    "--form",
}
_ENDS_COMMAND = {";", "|", "||", "&", "&&", "(", ")", "$("}
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _is_bash(info: str) -> bool:
    info = info.strip()
    return info == "bash" or info.startswith("{.bash")


def _commands(body: str) -> str:
    """*body* with every unquoted line break made a `;`, continuations joined."""
    body = body.replace("\\\n", " ")
    out, quote, escaped = [], None, False
    for ch in body:
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            ch = ";"
        out.append(ch)
    return "".join(out)


def _tokens(body: str) -> list[str]:
    lexer = shlex.shlex(_commands(body), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def curl_pairs(body: str) -> list[tuple[str, str]]:
    """The (METHOD, path) of every ``curl`` in a shell block."""
    tokens = _tokens(body)
    pairs = []
    for i, token in enumerate(tokens):
        if token != "curl":
            continue
        args = []
        for arg in tokens[i + 1 :]:
            if arg in _ENDS_COMMAND or arg == "curl":
                break
            args.append(arg)
        method, sends_body, get, path = None, False, False, None
        for j, arg in enumerate(args):
            if arg in ("-X", "--request") and j + 1 < len(args):
                method = args[j + 1].upper()
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:].upper()
            elif arg in _BODY_FLAGS:
                sends_body = True
            elif arg in ("-G", "--get"):
                get = True
            elif path is None:
                match = _URL.match(arg)
                if match:
                    path = match.group("path")
        assert path is not None, f"a curl with no localhost URL: curl {' '.join(args)}"
        if method is None:
            method = "POST" if sends_body and not get else "GET"
        pairs.append((method, path))
    return pairs


def mcp_client_pairs(body: str) -> list[tuple[str, str]]:
    """The URLs an MCP client is told to connect to; its first request is a POST."""
    if "mcpServers" not in body:
        return []
    pairs = []
    for match in _MCP_URL.finditer(body):
        url = _URL.match(match.group("url").split("://", 1)[-1])
        assert url, f"an MCP client URL that is not this API: {match.group('url')}"
        pairs.append(("POST", url.group("path")))
    return pairs


def documented_pairs(text: str) -> list[tuple[str, str]]:
    pairs = []
    for fence in _FENCE.finditer(text):
        body = fence.group("body")
        if _is_bash(fence.group("info")):
            pairs.extend(curl_pairs(body))
        pairs.extend(mcp_client_pairs(body))
    return pairs


def _operations() -> list[tuple[str, re.Pattern]]:
    spec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    operations = []
    for path, item in spec["paths"].items():
        pattern = re.compile(
            "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(path)) + "$"
        )
        for method in item:
            if method in _METHODS:
                operations.append((method.upper(), pattern))
    return operations


def unserved(pairs: list[tuple[str, str]]) -> list[str]:
    """Each pair no operation in the snapshot serves, as ``METHOD path``."""
    operations = _operations()
    return [
        f"{method} {path}"
        for method, path in pairs
        if not any(m == method and p.match(path) for m, p in operations)
    ]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_every_request_the_page_documents_is_served():
    pairs = documented_pairs(PAGE.read_text(encoding="utf-8"))
    missing = unserved(pairs)
    assert missing == [], (
        f"docs/mcp-server.md documents {missing}, which {SNAPSHOT.name} does not "
        "serve (no such method on that path)"
    )
    assert ("GET", "/api/v1/mcp/manifest") in pairs
    assert len(pairs) >= 10, f"the page documents too few requests to check: {pairs}"


def test_the_page_tells_no_one_to_set_a_claude_api_key():
    """The Quick Start stack never passes it to the API (docker-compose.yml)."""
    assert "ANTHROPIC_API_KEY" not in PAGE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The check refuses what the page used to say
# ---------------------------------------------------------------------------

OLD_CLIENT_CONFIG = """
```json
{
  "mcpServers": {
    "controlvar": {
      "url": "http://localhost:8000/api/v1/mcp/manifest"
    }
  }
}
```
"""

MCP_INITIALIZE = """
```{.bash exec}
curl -s -X POST localhost:8000/api/v1/mcp/manifest \\
  -H 'content-type: application/json' \\
  -d '{"jsonrpc": "2.0", "id": 1, "method": "initialize"}'
```
"""


@pytest.mark.parametrize(
    "planted",
    [OLD_CLIENT_CONFIG, MCP_INITIALIZE],
    ids=["the old client config", "an initialize POST"],
)
def test_an_mcp_client_connecting_to_the_manifest_is_refused(planted):
    pairs = documented_pairs(planted)
    assert pairs == [("POST", "/api/v1/mcp/manifest")]
    assert unserved(pairs) == ["POST /api/v1/mcp/manifest"]


def test_a_wrong_method_on_a_real_path_is_refused():
    pairs = documented_pairs(
        "```bash\ncurl -s -X DELETE localhost:8000/api/v1/ai/templates\n```\n"
    )
    assert unserved(pairs) == ["DELETE /api/v1/ai/templates"]


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "curl -s localhost:8000/api/v1/mcp/manifest | jq .",
            [("GET", "/api/v1/mcp/manifest")],
        ),
        (
            'curl -s "localhost:8000/api/v1/ai/sample-size?baseline_rate=0.08&mde=0.004"',
            [("GET", "/api/v1/ai/sample-size")],
        ),
        (
            "curl -s http://localhost:8000/api/v1/ai/design -H 'a: b' -d '{\n  \"x\": 1\n}'",
            [("POST", "/api/v1/ai/design")],
        ),
        ("curl -s -XPUT localhost:8000/api/v1/x", [("PUT", "/api/v1/x")]),
        ("curl -s --request patch localhost:8000/api/v1/x", [("PATCH", "/api/v1/x")]),
        ("curl -s -G localhost:8000/api/v1/x -d a=1", [("GET", "/api/v1/x")]),
        (
            "TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \\\n"
            "  -d '{}' | jq -r .access_token)\n"
            'curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN"',
            [("POST", "/api/v1/auth/login"), ("GET", "/api/v1/auth/me")],
        ),
        (
            "curl -s -o /dev/null -w '%{http_code}\\n' localhost:8000/api/v1/ai/templates",
            [("GET", "/api/v1/ai/templates")],
        ),
    ],
    ids=[
        "get",
        "query string",
        "body",
        "-XPUT",
        "--request",
        "-G",
        "captured, two lines",
        "status code only",
    ],
)
def test_the_parser_reads_method_and_path(body, expected):
    assert curl_pairs(body) == expected


def test_a_curl_to_anything_but_this_api_is_refused_not_skipped():
    with pytest.raises(AssertionError, match="no localhost URL"):
        curl_pairs("curl -s https://example.com/api/v1/x")


def test_a_parameter_matches_one_segment_only():
    assert unserved([("GET", "/api/v1/ai/templates/checkout-cta")]) == []
    assert unserved([("GET", "/api/v1/ai/templates/a/b")]) == [
        "GET /api/v1/ai/templates/a/b"
    ]

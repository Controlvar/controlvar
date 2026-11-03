"""Every client snippet on the MCP page starts the real server with the real settings (Q21, C8).

The page shows how to add the server to Claude Code (a ``claude mcp add`` line)
and to Claude Desktop and Cursor (an ``mcpServers`` JSON block). Each is checked
against what ships: the environment variables the server reads (``env`` in
``sdk/mcp/tools.json``), and how it is started. While the page says "Not yet
published" that is the clone-path form, ``node`` on the built ``dist/index.js``
(``bin`` in ``sdk/mcp/package.json``); once published it is ``npx -y
@controlvar/mcp-server@<version>``, pinned, because a bare ``npx -y`` runs
whatever was published last. A block that gives a server a ``url`` is refused:
the server is started by command, and nothing on the API answers an MCP client.

Until the package is on npm the page names no ``npx`` command at all.

Reads files only; no git. ``mechanism:mcp-page-snippets`` and
``mechanism:mcp-page-state`` in ``scripts/doc_claims.py``.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "mcp-server.md"
PACKAGE = REPO_ROOT / "sdk" / "mcp" / "package.json"
TOOLS = REPO_ROOT / "sdk" / "mcp" / "tools.json"

_FENCE = re.compile(
    r"^(?P<fence>`{3,})(?P<info>[^\n]*)\n(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_UNPUBLISHED = re.compile(r"Not yet published", re.I)
_NPX = re.compile(r"\bnpx\b")

#: What a snippet starts the server with: (command, arguments, environment names).
Launch = Tuple[str, List[str], List[str]]


def shipped() -> Dict[str, Any]:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    env = json.loads(TOOLS.read_text(encoding="utf-8"))["env"]
    return {
        "name": package["name"],
        "version": package["version"],
        "bin": package["bin"]["controlvar-mcp"],
        "env": env,
    }


def claude_code_launches(body: str) -> List[Launch]:
    """The ``claude mcp add`` commands in a shell block."""
    launches = []
    for command in body.replace("\\\n", " ").splitlines():
        if "claude mcp add" not in command:
            continue
        tokens = shlex.split(command)
        env = [
            tokens[i + 1].split("=", 1)[0]
            for i, token in enumerate(tokens[:-1])
            if token in ("-e", "--env")
        ]
        rest = tokens[tokens.index("--") + 1 :] if "--" in tokens else []
        launches.append((rest[0] if rest else "", rest[1:], env))
    return launches


def client_config_launches(body: str) -> Tuple[List[Launch], List[str]]:
    """The servers in an ``mcpServers`` block, and a problem for each one with a ``url``."""
    launches, problems = [], []
    servers = json.loads(body)["mcpServers"]
    for name, entry in servers.items():
        if "url" in entry:
            problems.append(
                f"server {name!r} is given a url; add it by command, not by URL"
            )
        launches.append(
            (
                entry.get("command", ""),
                entry.get("args", []),
                list(entry.get("env", {})),
            )
        )
    return launches, problems


def problems(text: str, ship: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    unpublished = bool(_UNPUBLISHED.search(text))
    launches: List[Launch] = []
    for fence in _FENCE.finditer(text):
        body = fence.group("body")
        if "claude mcp add" in body:
            launches += claude_code_launches(body)
        if "mcpServers" in body:
            more, bad = client_config_launches(body)
            launches += more
            found += bad
    if not launches:
        found.append("the page shows no client snippet to check")
    for command, args, env in launches:
        for name in env:
            if name not in ship["env"]:
                found.append(
                    f"the page sets {name}; the server reads {', '.join(ship['env'])}"
                )
        for required in ("CONTROLVAR_API_URL", "CONTROLVAR_API_KEY"):
            if required not in env:
                found.append(f"a snippet does not set {required}")
        if unpublished:
            tail = "/" + ship["bin"]
            ok = command == "node" and len(args) == 1 and args[0].endswith(tail)
            if not ok:
                found.append(
                    f"a snippet starts the server with {command} {args}; before "
                    f"publication that is `node <clone>/sdk/mcp/{ship['bin']}`"
                )
        else:
            pinned = ["-y", f"{ship['name']}@{ship['version']}"]
            if (command, args) != ("npx", pinned):
                found.append(
                    f"a snippet starts the server with {command} {args}; the "
                    f"published form is `npx {' '.join(pinned)}`"
                )
    if unpublished and _NPX.search(text):
        found.append("the page says 'Not yet published' and still names an npx command")
    return found


def test_every_client_snippet_names_the_real_server():
    found = problems(PAGE.read_text(encoding="utf-8"), shipped())
    assert not found, "\n".join(found)


def test_an_unpublished_page_names_no_install_by_npx():
    text = PAGE.read_text(encoding="utf-8")
    if not _UNPUBLISHED.search(text):
        pytest.skip("the page no longer says the package is unpublished")
    assert not _NPX.search(text)
    assert f"npm install {shipped()['name']}" not in text


def test_the_page_has_one_claude_code_command_and_one_config_block():
    text = PAGE.read_text(encoding="utf-8")
    assert len(re.findall(r"claude mcp add", text)) == 1
    assert len(re.findall(r'"mcpServers"', text)) == 1


# ---------------------------------------------------------------------------
# The check refuses what it exists to refuse
# ---------------------------------------------------------------------------


def _planted(old: str, new: str) -> str:
    text = PAGE.read_text(encoding="utf-8")
    assert old in text, old
    return text.replace(old, new)


def test_a_misspelled_variable_is_refused():
    found = problems(
        _planted('"CONTROLVAR_API_KEY": "paste', '"CONTROLVAR_API_TOKN": "paste'),
        shipped(),
    )
    assert any("sets CONTROLVAR_API_TOKN; the server reads" in p for p in found), found
    assert any("does not set CONTROLVAR_API_KEY" in p for p in found), found


def test_a_misspelled_variable_on_the_claude_code_line_is_refused():
    found = problems(
        _planted("-e CONTROLVAR_API_KEY=", "-e CONTROLVAR_TOKEN="), shipped()
    )
    assert any("sets CONTROLVAR_TOKEN; the server reads" in p for p in found), found


def test_a_url_in_a_server_entry_is_refused():
    found = problems(
        _planted(
            '"command": "node",',
            '"url": "http://localhost:8000/api/v1/mcp/manifest",\n      "command": "node",',
        ),
        shipped(),
    )
    assert any("is given a url; add it by command, not by URL" in p for p in found), (
        found
    )


def test_an_npx_command_before_publication_is_refused():
    found = problems(_planted('"command": "node",', '"command": "npx",'), shipped())
    assert any("starts the server with npx" in p for p in found), found
    assert any("still names an npx command" in p for p in found), found


def test_a_wrong_bin_path_is_refused():
    found = problems(
        _planted("/sdk/mcp/dist/index.js", "/sdk/mcp/dist/main.js"), shipped()
    )
    assert any("a snippet starts the server with node" in p for p in found), found


def test_once_published_an_unpinned_npx_is_refused_and_a_pinned_one_passes():
    ship = shipped()
    published = (
        "```json\n"
        '{"mcpServers": {"controlvar": {"command": "npx", "args": ["-y", "%s"], '
        '"env": {"CONTROLVAR_API_URL": "http://localhost:8000", '
        '"CONTROLVAR_API_KEY": "k"}}}}\n'
        "```\n"
    )
    pinned = published % f"{ship['name']}@{ship['version']}"
    assert problems(pinned, ship) == []
    unpinned = published % ship["name"]
    assert any("the published form is" in p for p in problems(unpinned, ship))

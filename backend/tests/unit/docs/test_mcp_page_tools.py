"""The MCP page lists the tools the MCP server offers, and no page denies the server (Q22, Q23).

``sdk/mcp/tools.json`` is what the server answers to ``tools/list``. The page's
tool table must name exactly those tools, in that order, each with the route it
calls. And the sentence the page used to open with, that the platform runs no
MCP server, was true of the API and false of the product once the server
existed: it survived in three files after the manifest changed. The sweep below
finds that sentence, in any of the three forms it had, under ``docs/`` and
``tests/acceptance/docs/``; the page now says it differently ("Nothing on your
instance answers an MCP client; add the server by command, not by URL."), which
``test_nothing_answers_an_mcp_client`` pins.

Reads files only, with no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is ``mechanism:mcp-page-tools`` in ``scripts/doc_claims.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "mcp-server.md"
TOOLS = REPO_ROOT / "sdk" / "mcp" / "tools.json"

#: The route each tool calls, as the page's table spells it.
ROUTES: Dict[str, str] = {
    "list_feature_flags": "GET /api/v1/feature-flags/",
    "get_feature_flag": "GET /api/v1/feature-flags/",
    "evaluate_feature_flag": "POST /api/v1/feature-flags/evaluate/{flag_key}",
    "list_experiments": "GET /api/v1/experiments/",
    "get_experiment": "GET /api/v1/experiments/{experiment_id}",
    "get_experiment_results": "GET /api/v1/experiments/{experiment_id}/results",
}

#: The three forms of the denied sentence (Q23). Matched across line breaks and
#: ignoring case, so a rewrapped copy is still found.
PHRASES = ("runs no MCP server", "does not run an MCP server", "serves no MCP server")
SWEPT_DIRS = ("docs", "tests/acceptance/docs")
SWEPT_SUFFIXES = {".md", ".toml", ".yml", ".yaml", ".txt", ".json", ".py"}


def served_tools() -> List[str]:
    return [t["name"] for t in json.loads(TOOLS.read_text(encoding="utf-8"))["tools"]]


def table_rows(text: str) -> List[List[str]]:
    """The cells of each body row of the page's ``Tool | What it answers | API route`` table."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[:1] == ["Tool"] and "API route" in cells:
            rows = []
            for row in lines[i + 2 :]:
                if not row.strip().startswith("|"):
                    break
                rows.append(
                    [
                        c.strip().replace("`", "")
                        for c in row.strip().strip("|").split("|")
                    ]
                )
            return rows
    raise AssertionError("the page has no `Tool | What it answers | API route` table")


def table_problems(text: str, served: List[str]) -> List[str]:
    rows = table_rows(text)
    listed = [row[0] for row in rows]
    found = []
    if listed != served:
        found.append(f"the page's table lists {listed}; the MCP server serves {served}")
    for row in rows:
        if ROUTES.get(row[0]) != row[-1]:
            found.append(
                f"the page says {row[0]} calls {row[-1]!r}; it calls {ROUTES.get(row[0])!r}"
            )
    return found


def sweep(root: Path) -> List[str]:
    """Each place under *root* that still denies the MCP server, as ``path:line says phrase``."""
    found = []
    for directory in SWEPT_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SWEPT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for phrase in PHRASES:
                pattern = re.compile(
                    r"\s+".join(re.escape(word) for word in phrase.split()), re.I
                )
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    where = path.relative_to(root).as_posix()
                    found.append(f"{where}:{line} says {phrase!r}")
    return found


def test_the_pages_tool_table_is_the_servers_tools():
    found = table_problems(PAGE.read_text(encoding="utf-8"), served_tools())
    assert not found, "\n".join(found)


def test_the_table_check_fires_on_a_dropped_and_a_misrouted_tool():
    text = PAGE.read_text(encoding="utf-8")
    dropped = text.replace(
        "| `get_experiment` | One experiment, by its id | "
        "`GET /api/v1/experiments/{experiment_id}` |\n",
        "",
    )
    assert dropped != text
    assert table_problems(dropped, served_tools()) == [
        "the page's table lists ['list_feature_flags', 'get_feature_flag', "
        "'evaluate_feature_flag', 'list_experiments', 'get_experiment_results']; "
        "the MCP server serves ['list_feature_flags', 'get_feature_flag', "
        "'evaluate_feature_flag', 'list_experiments', 'get_experiment', "
        "'get_experiment_results']"
    ]
    misrouted = text.replace("`GET /api/v1/experiments/`", "`GET /api/v1/experiment/`")
    assert misrouted != text
    assert any(
        "calls 'GET /api/v1/experiment/'" in problem
        for problem in table_problems(misrouted, served_tools())
    )


def test_no_page_says_the_platform_has_no_mcp_server():
    found = sweep(REPO_ROOT)
    assert found == [], "\n".join(found)


@pytest.mark.parametrize("phrase", PHRASES)
def test_the_sweep_fires_on_a_planted_phrase(phrase, tmp_path):
    """Plant each form, once on one line and once rewrapped, under each swept directory."""
    wrapped = phrase.replace(" ", "\n", 2)
    for directory in SWEPT_DIRS:
        target = tmp_path / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / "page.md").write_text(
            f"Intro.\n\nThe platform {phrase} at all.\n\nAnd the platform {wrapped}.\n",
            encoding="utf-8",
        )
    found = sweep(tmp_path)
    assert sorted(found) == sorted(
        f"{directory}/page.md:{line} says {phrase!r}"
        for directory in SWEPT_DIRS
        for line in (3, 5)
    )


def test_the_sweep_passes_on_the_kept_sentence(tmp_path):
    target = tmp_path / "docs"
    target.mkdir()
    (target / "page.md").write_text(
        "Nothing on your instance answers an MCP client; add the server by command, "
        "not by URL.\n",
        encoding="utf-8",
    )
    assert sweep(tmp_path) == []

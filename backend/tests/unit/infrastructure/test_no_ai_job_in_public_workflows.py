"""No public workflow runs an AI job, and no AI check is a required check.

Two pins, both reading ``.github`` and ``scripts/configure-repo.sh`` only:

* no workflow or composite action names an AI provider's secret, sets an
  ``env:`` or ``with:`` key for one, uses an AI agent action, or runs an agent
  command line;
* no name in ``REQUIRED_CHECKS`` is an AI agent's result.

The match is on structure, not on text. A text search for ``anthropic`` is red
on main: ``pr-qa-gate.yml`` checks, in a ``run:`` block, that the API images
import the provider SDKs. That is a product test, not an agent, so only
secret references, ``env:``/``with:`` keys, ``uses:`` and command words count.

The checks below are functions of a parsed mapping so that each has a planted
case that must be refused.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit, pytest.mark.regression]

if not wg.CONFIGURE_REPO.is_file() or not wg.WORKFLOWS.is_dir():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

NAME = r"anthropic|claude|qa_agent"
#: A secret reference: ``secrets.X`` or ``secrets['X']``.
SECRET_REF = re.compile(r"secrets\s*(?:\.|\[\s*['\"])\s*(\w+)", re.I)
#: The agent command as a word: ``claude -p``, ``./node_modules/.bin/claude``,
#: or ``claude`` behind any package runner or wrapper, with flags between
#: (``npx -y claude``, ``bunx claude``, ``pnpm dlx claude``, ``yarn dlx claude``,
#: ``npm exec -- claude``), and any use of the ``@anthropic-ai/`` packages. A
#: quoted package name in an assertion is not a command.
RUNNER = (
    r"(?:npx|bunx|pnpx|pnpm\s+(?:dlx|exec)|yarn\s+(?:dlx|exec)|npm\s+(?:exec|x)"
    r"|exec|sudo|xargs|command|env|nohup|time)"
)
AGENT_COMMAND = re.compile(
    r"(?:^|[;&|(`/]\s*|\b" + RUNNER + r"(?:\s+(?:-\S+|\w+=\S*))*\s+)claude(?:\s|$)"
    r"|@anthropic-ai/",
    re.I | re.M,
)
#: Names that mark a required check as an agent's result.
AGENT_CHECK = re.compile(r"agent|explorer|claude|anthropic|\bAI\b", re.I)


def action_files() -> List[Path]:
    actions = wg.REPO_ROOT / ".github" / "actions"
    return sorted(
        p for p in actions.rglob("*") if p.name in ("action.yml", "action.yaml")
    )


def public_files() -> List[Path]:
    return (
        sorted(wg.WORKFLOWS.glob("*.yml"))
        + sorted(wg.WORKFLOWS.glob("*.yaml"))
        + action_files()
    )


def _walk(node: Any, path: str = "") -> Iterator[tuple]:
    """Yield (path, key, value) for every mapping entry, and (path, None, s) for a
    string in a list."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if isinstance(value, str):
                yield path, None, value
            yield from _walk(value, f"{path}[{i}]")


def agent_findings(doc: Any) -> List[str]:
    """Every place a parsed workflow or action runs, or can run, an AI agent."""
    found: List[str] = []
    for path, key, value in _walk(doc):
        if isinstance(value, str):
            for ref in SECRET_REF.findall(value):
                if re.search(NAME, ref, re.I):
                    found.append(f"{path}: secret {ref}")
            if key == "uses" and re.search(NAME, value, re.I):
                found.append(f"{path}: uses {value}")
            if key == "run" and AGENT_COMMAND.search(value):
                found.append(f"{path}: run calls the agent command")
        if (
            isinstance(key, str)
            and path.rsplit("/", 1)[-1] in ("env", "with")
            and re.search(NAME, key, re.I)
        ):
            found.append(f"{path}: key {key}")
    return found


def agent_message(file: Any, finding: str) -> str:
    """``agent workflow in public repo: <file>:<job>``, then what was found."""
    parts = finding.split(":", 1)[0].split("/")
    job = parts[2] if len(parts) > 2 and parts[1] == "jobs" else "(action)"
    return f"agent workflow in public repo: {file}:{job} ({finding})"


def agent_required_checks(names: List[str]) -> List[str]:
    return [n for n in names if AGENT_CHECK.search(n)]


def test_the_workflow_files_are_found():
    """A glob that matches nothing would make the next test pass for ever."""
    files = public_files()
    assert len(files) > 20, files
    assert any(p.parent.name == "workflows" for p in files)
    assert action_files(), "no composite action found"


def test_no_workflow_or_action_runs_an_ai_job():
    problems = []
    for path in public_files():
        for finding in agent_findings(wg.load(path)):
            problems.append(agent_message(path.relative_to(wg.REPO_ROOT), finding))
    assert not problems, "\n".join(problems)


def test_no_required_check_is_an_ai_result():
    assert agent_required_checks(wg.required_checks()) == []


# --------------------------------------------------------------------------
# The checks refuse what they exist to refuse
# --------------------------------------------------------------------------

PLANTS = {
    "secret in job env": {
        "jobs": {"j": {"env": {"KEY": "${{ secrets.ANTHROPIC_API_KEY }}"}}}
    },
    "secret by bracket": {
        "jobs": {
            "j": {
                "steps": [{"run": "x", "env": {"K": "${{ secrets['QA_AGENT_KEY'] }}"}}]
            }
        }
    },
    "env key under an arbitrary secret": {
        "jobs": {"j": {"env": {"ANTHROPIC_API_KEY": "${{ secrets.MY_KEY }}"}}}
    },
    "with key in a composite": {
        "runs": {"steps": [{"uses": "./x", "with": {"claude_token": "t"}}]}
    },
    "agent action": {
        "jobs": {"j": {"steps": [{"uses": "anthropics/claude-code-action@v1"}]}}
    },
    "agent command": {"jobs": {"j": {"steps": [{"run": "set -e\nclaude -p hi"}]}}},
    "agent install": {
        "jobs": {"j": {"steps": [{"run": "npm i -g @anthropic-ai/claude-code"}]}}
    },
}
for _runner in (
    "npx claude",
    "npx -y claude",
    "npx --yes claude -p hi",
    "bunx claude",
    "pnpx claude",
    "pnpm dlx claude",
    "pnpm dlx @anthropic-ai/claude-code",
    "pnpm exec claude",
    "yarn dlx claude",
    "yarn dlx -q claude",
    "npm exec -- claude",
    "npm x claude",
    "./node_modules/.bin/claude -p hi",
    "env CI=1 claude",
    "sudo claude",
    "echo hi | xargs claude",
):
    PLANTS[f"runner: {_runner}"] = {"jobs": {"j": {"steps": [{"run": _runner}]}}}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_each_plant_is_refused(plant):
    assert agent_findings(PLANTS[plant]), plant


def test_a_quoted_package_name_in_a_run_block_is_not_an_agent():
    """The shape on main today (pr-qa-gate.yml): an image check names the SDKs."""
    doc = {
        "jobs": {
            "j": {
                "steps": [
                    {"run": 'assert {"anthropic", "openai"} <= set(names), names'},
                    {"run": "# `anthropic` nor `openai`"},
                ]
            }
        }
    }
    assert agent_findings(doc) == []


def test_an_agent_check_name_is_refused():
    assert agent_required_checks(["lint", "qa-agent/explorer"]) == ["qa-agent/explorer"]
    assert agent_required_checks(["lint", "Claude review"]) == ["Claude review"]
    assert agent_required_checks(["lint", "DCO"]) == []


def test_the_message_names_the_file_and_the_job():
    found = agent_findings(PLANTS["secret in job env"])
    assert agent_message("a.yml", found[0]).startswith(
        "agent workflow in public repo: a.yml:j "
    )

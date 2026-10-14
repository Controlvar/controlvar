"""The two migration pages and the load-testing page, as Doc Examples runs them (#1075).

``docs/self-hosting/migrations.md`` is the operator's page: its reader has the
image, not a checkout, so every command it runs goes into the API container
(``docker compose exec api ...``) and runs in the ``stack`` environment.
``docs/development/database/migrations.md`` and
``docs/performance/load-testing.md`` are the contributor's pages, run from a
development virtual environment in ``stack-dev``, which carries those two
only. What a run cannot see is pinned here, without a stack:

* the operator page runs nothing from a venv, and its AWS block runs the
  migration task the way the Deploy workflow does: a registered revision's
  ARN, never the bare family, with ``MIGRATION_COMMAND``;
* no shell block on either migration page autogenerates a revision without
  naming the head it extends, names the singular ``head``, or runs
  ``alembic merge``: a full checkout has two heads, kept apart on purpose (the
  contributor page once told a reader to autogenerate an "Initial migration" on
  an empty database, with neither, and the operator page once showed a merge).
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
OPERATOR = "docs/self-hosting/migrations.md"
CONTRIBUTOR = "docs/development/database/migrations.md"
LOAD_TESTING = "docs/performance/load-testing.md"
ENROLMENT = REPO_ROOT / "scripts" / "doc_examples.toml"
STACK = REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "migration_task_stack.py"

#: An opening shell fence and its info string, then the body up to the close.
_BLOCK = re.compile(r"^```\{\.bash (?P<info>[^}]*)\}\n(?P<body>.*?)^```", re.M | re.S)


def shell_blocks(rel: str) -> list[tuple[str, str]]:
    """(info, body) of every tagged shell block of a page, continuations joined."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    return [
        (m.group("info"), m.group("body").replace("\\\n", " "))
        for m in _BLOCK.finditer(text)
    ]


def environments() -> dict[str, str]:
    data = tomllib.loads(ENROLMENT.read_text(encoding="utf-8"))
    return {d["path"]: d["environment"] for d in data["document"]}


def test_stack_dev_carries_the_two_contributor_pages_and_the_operator_page_is_stack():
    enrolled = environments()
    assert sorted(p for p, e in enrolled.items() if e == "stack-dev") == sorted(
        [CONTRIBUTOR, LOAD_TESTING]
    )
    assert enrolled[OPERATOR] == "stack"


def test_the_operator_page_runs_its_commands_in_the_api_container():
    blocks = shell_blocks(OPERATOR)
    run = [body for info, body in blocks if info.startswith("exec")]
    assert run, "the operator page runs nothing"
    for body in run:
        for line in filter(None, (line.strip() for line in body.splitlines())):
            assert line.startswith("docker compose exec "), line
    for _, body in blocks:
        assert "venv/bin/activate" not in body, body
    alembic = [b for _, b in blocks if "alembic" in b and "docker compose" in b]
    assert any(
        "docker compose exec api python -m alembic -c backend/app/db/alembic.ini" in b
        for b in alembic
    )


#: A command line that runs alembic or the bootstrap, and the API container's
#: prefix every such line on the operator page carries.
_RUNS_ALEMBIC = re.compile(r"\bpython -m (?:alembic|backend\.app\.db\.bootstrap)\b")
_IN_THE_API = re.compile(
    r"^docker compose exec (?:-e [A-Z_]{1,40}=\S{1,60} ){0,4}api python -m "
)


def test_every_alembic_and_bootstrap_command_runs_in_the_api_container():
    """Every shell line of the operator page that runs alembic or the bootstrap
    runs it in the API container (``docker compose exec api``); the AWS block
    hands the bootstrap to the migration task as JSON, which runs nothing here."""
    lines = [
        line.strip()
        for _, body in shell_blocks(OPERATOR)
        for line in body.splitlines()
        if _RUNS_ALEMBIC.search(line)
    ]
    assert len(lines) >= 10, lines
    outside = [line for line in lines if not _IN_THE_API.match(line)]
    assert outside == [], outside


def _migration_command() -> list[str]:
    for node in ast.parse(STACK.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "MIGRATION_COMMAND"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no MIGRATION_COMMAND in {STACK.name}")


@pytest.mark.skipif(not STACK.is_file(), reason="this tree has no infrastructure/cdk")
def test_the_operator_page_runs_the_migration_task_as_the_deploy_does():
    """A registered revision's ARN, never the bare family (which resolves to
    whichever revision registered last), running MIGRATION_COMMAND."""
    (aws,) = [b for info, b in shell_blocks(OPERATOR) if 'reason="aws:' in info]
    register = re.search(
        r"^ARN=\$\(bash scripts/register_task_definition\.sh "
        r"experimentation-migrate-(?P<env>[a-z]+) \"\$IMAGE\"\)$",
        aws,
        re.M,
    )
    assert register, aws
    assert re.search(
        r"^bash scripts/run_migration_task\.sh experimentation-"
        + register.group("env")
        + r" \"\$ARN\" ",
        aws,
        re.M,
    ), aws
    assert json.dumps(_migration_command(), separators=(",", ":")) in aws
    assert "run-task" not in aws and "--task-definition" not in aws


@pytest.mark.parametrize("rel", [OPERATOR, CONTRIBUTOR])
def test_no_block_autogenerates_without_a_head_or_names_the_singular_head(rel):
    for info, body in shell_blocks(rel):
        for command in body.splitlines():
            if "revision" in command and "--autogenerate" in command:
                assert "--head " in command, f"{rel}: {command}"
            assert not re.search(r"\b(upgrade|downgrade|stamp) head\b", command), (
                f"{rel}: {command}"
            )
        assert " merge " not in f" {body} ", f"{rel}: a block merges heads"

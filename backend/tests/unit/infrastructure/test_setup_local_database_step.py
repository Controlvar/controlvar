"""``demo/setup-local.sh`` prepares a new database with the bootstrap (#1142).

The demo guide tells a new reader to run ``./demo/setup-local.sh`` on a machine
whose Postgres has an empty database. The script used to prepare it with a bare
``alembic upgrade heads``, which stops at the second revision of the historical
chain (``relation "permissions" already exists``): that chain was written
against databases that already had tables and cannot be replayed from zero.
``python -m backend.app.db.bootstrap`` builds a new database from the models,
stamps the heads, and runs ``alembic upgrade heads`` itself when the database
already has an ``alembic_version`` table, so one command serves both.

The script is read here without Docker or a database, so this runs in the unit
job. The checks are functions of the script's text, each planted against a
small offender below so that a check that stops firing fails here and not only
when the real file regresses.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SETUP_LOCAL = REPO_ROOT / "demo" / "setup-local.sh"

BOOTSTRAP = re.compile(r"(?:^|\s)-m\s+backend\.app\.db\.bootstrap(?:\s|$)")
#: Any alembic command that upgrades: ``python -m alembic ... upgrade heads``,
#: ``alembic upgrade head``, ``alembic -c <ini> upgrade <rev>``.
RAW_UPGRADE = re.compile(r"\balembic\b.*\bupgrade\b")
#: The seed scripts, which need the schema to exist.
SEED = re.compile(r"\bbackend/scripts/seed_\w+\.py\b")


def commands(script: str) -> List[str]:
    """The script's commands, with ``\\`` continuations joined and comments gone."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def first_index(lines: List[str], pattern: "re.Pattern[str]") -> Optional[int]:
    for index in range(len(lines)):
        if pattern.search(lines[index]):
            return index
    return None


def database_step_problems(script: str) -> List[str]:
    """One line for each way the script can meet an empty database and fail."""
    lines = commands(script)
    bootstrap = first_index(lines, BOOTSTRAP)
    problems: List[str] = []
    if bootstrap is None:
        problems.append("never runs python -m backend.app.db.bootstrap")
    for index in range(len(lines)):
        if RAW_UPGRADE.search(lines[index]) and (
            bootstrap is None or index < bootstrap
        ):
            problems.append(
                f"runs a raw alembic upgrade before the bootstrap: {lines[index]}"
            )
    seed = first_index(lines, SEED)
    if bootstrap is not None and seed is not None and seed < bootstrap:
        problems.append(f"seeds the database before the bootstrap: {lines[seed]}")
    # The bootstrap creates the administrator the demo guide signs in with; left
    # to its defaults it creates a second one (admin@example.com).
    if bootstrap is not None:
        before = "\n".join(lines[:bootstrap])
        for name in ("FIRST_SUPERUSER", "FIRST_SUPERUSER_PASSWORD"):
            if not re.search(rf"^export {name}=", before, re.MULTILINE):
                problems.append(f"does not export {name} before the bootstrap")
    return problems


def test_setup_local_prepares_the_database_with_the_bootstrap():
    script = SETUP_LOCAL.read_text(encoding="utf-8")
    lines = commands(script)
    # The scan has to still see the steps it orders the bootstrap against.
    assert first_index(lines, SEED) is not None, "seed lines not found"
    assert database_step_problems(script) == []


PLANTED_SCRIPT = {
    "the old line: a raw alembic upgrade and no bootstrap": (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "python -m alembic -c backend/app/db/alembic.ini upgrade heads\n"
        "python backend/scripts/seed_demo_data.py\n",
        "never runs python -m backend.app.db.bootstrap",
    ),
    "a raw alembic upgrade ahead of the bootstrap": (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "python -m alembic -c backend/app/db/alembic.ini upgrade heads\n"
        "python -m backend.app.db.bootstrap\n"
        "python backend/scripts/seed_demo_data.py\n",
        "runs a raw alembic upgrade before the bootstrap",
    ),
    "the short alembic spelling": (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "alembic upgrade head\n"
        "python -m backend.app.db.bootstrap\n",
        "runs a raw alembic upgrade before the bootstrap",
    ),
    "the upgrade continued over two lines": (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "python -m alembic \\\n    -c backend/app/db/alembic.ini \\\n    upgrade heads\n"
        "python -m backend.app.db.bootstrap\n",
        "runs a raw alembic upgrade before the bootstrap",
    ),
    "a bootstrap that only appears in a comment": (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "# python -m backend.app.db.bootstrap\n"
        "python -m alembic -c backend/app/db/alembic.ini upgrade heads\n",
        "never runs python -m backend.app.db.bootstrap",
    ),
    "seeds ahead of the bootstrap": (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "python backend/scripts/seed_demo_data.py\n"
        "python -m backend.app.db.bootstrap\n",
        "seeds the database before the bootstrap",
    ),
    "no administrator set for the bootstrap": (
        "python -m backend.app.db.bootstrap\n"
        "python backend/scripts/seed_demo_data.py\n",
        "does not export FIRST_SUPERUSER before the bootstrap",
    ),
}


@pytest.mark.parametrize(
    "script, expected", PLANTED_SCRIPT.values(), ids=list(PLANTED_SCRIPT)
)
def test_a_planted_database_step_is_named(script, expected):
    problems = database_step_problems(script)
    assert any(expected in p for p in problems), problems


def test_a_raw_upgrade_after_the_bootstrap_is_not_a_problem():
    script = (
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n"
        "python -m backend.app.db.bootstrap\n"
        "python -m alembic -c backend/app/db/alembic.ini upgrade heads\n"
        "python backend/scripts/seed_demo_data.py\n"
    )
    assert database_step_problems(script) == []

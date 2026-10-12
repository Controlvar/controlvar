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
#: The administrator the demo guide (``demo/DEMO_GUIDE.md``) signs in with and
#: the seeds (``backend/scripts/seed_demo_data.py``) create.
DEMO_ADMIN = "admin@demo.com"


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


def last_assignment(lines: List[str], name: str) -> Optional[str]:
    """The value *name* last gets in *lines*, quotes removed, or None.

    Only what a child process inherits counts: ``export NAME=value`` lines, and
    *lines* may also carry the ``NAME=value`` prefix of a command. A bare
    ``NAME=value`` line does not (the bootstrap would not see it), and neither
    does a ``NAME=value`` line of a here-document, which is data: the script's
    ``cat > .env <<'EOF'`` block lists ENVIRONMENT, FIRST_SUPERUSER and the
    password, and the backend does not read that ``.env``. The name must stand
    alone, so ``APP_ENV=...`` is not ``ENVIRONMENT`` and
    ``FIRST_SUPERUSER_PASSWORD=...`` is not ``FIRST_SUPERUSER``.
    """
    value: Optional[str] = None
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){name}=(\"[^\"]*\"|'[^']*'|\S*)")
    for line in lines:
        for match in pattern.finditer(line):
            value = match.group(1).strip("\"'")
    return value


def inherited_settings(lines: List[str], bootstrap: int) -> List[str]:
    """What the bootstrap inherits: the ``export`` lines before it, then the
    assignments written in front of the bootstrap command itself."""
    exports = [line for line in lines[:bootstrap] if line.startswith("export ")]
    match = BOOTSTRAP.search(lines[bootstrap])
    assert match is not None
    return exports + [lines[bootstrap][: match.start()]]


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
    if bootstrap is not None:
        upto = inherited_settings(lines, bootstrap)
        # The bootstrap creates the first administrator with the demo password
        # only when ENVIRONMENT was set to development: ENVIRONMENT is the
        # supported spelling, APP_ENV the deprecated fallback.
        environment = last_assignment(upto, "ENVIRONMENT")
        if environment != "development":
            problems.append(
                "does not set ENVIRONMENT=development before the bootstrap "
                f"(it is {environment!r})"
            )
        # The bootstrap creates the administrator the demo guide signs in with;
        # left to its defaults it creates a second one (admin@example.com).
        administrator = last_assignment(upto, "FIRST_SUPERUSER")
        if administrator is None:
            problems.append("does not set FIRST_SUPERUSER before the bootstrap")
        elif administrator != DEMO_ADMIN:
            problems.append(
                f"FIRST_SUPERUSER is {administrator}, not {DEMO_ADMIN}: the "
                "demo guide and the seeds use that account"
            )
        if last_assignment(upto, "FIRST_SUPERUSER_PASSWORD") is None:
            problems.append(
                "does not set FIRST_SUPERUSER_PASSWORD before the bootstrap"
            )
    return problems


def test_setup_local_prepares_the_database_with_the_bootstrap():
    script = SETUP_LOCAL.read_text(encoding="utf-8")
    lines = commands(script)
    # The scan has to still see the steps it orders the bootstrap against.
    assert first_index(lines, SEED) is not None, "seed lines not found"
    assert database_step_problems(script) == []


#: What the script sets before the bootstrap: each plant below is wrong in one
#: way only, so it is the one check that names it.
SETTINGS = (
    "export ENVIRONMENT=development\n"
    "export FIRST_SUPERUSER=admin@demo.com\n"
    "export FIRST_SUPERUSER_PASSWORD=x\n"
)
RAW = "python -m alembic -c backend/app/db/alembic.ini upgrade heads\n"
BOOT = "python -m backend.app.db.bootstrap\n"
SEED_LINE = "python backend/scripts/seed_demo_data.py\n"

PLANTED_SCRIPT = {
    "the old line: a raw alembic upgrade and no bootstrap": (
        SETTINGS + RAW + SEED_LINE,
        "never runs python -m backend.app.db.bootstrap",
    ),
    "a raw alembic upgrade ahead of the bootstrap": (
        SETTINGS + RAW + BOOT + SEED_LINE,
        "runs a raw alembic upgrade before the bootstrap",
    ),
    "the short alembic spelling": (
        SETTINGS + "alembic upgrade head\n" + BOOT,
        "runs a raw alembic upgrade before the bootstrap",
    ),
    "the upgrade continued over two lines": (
        SETTINGS
        + "python -m alembic \\\n    -c backend/app/db/alembic.ini \\\n    upgrade heads\n"
        + BOOT,
        "runs a raw alembic upgrade before the bootstrap",
    ),
    "a bootstrap that only appears in a comment": (
        SETTINGS + "# " + BOOT + RAW,
        "never runs python -m backend.app.db.bootstrap",
    ),
    "seeds ahead of the bootstrap": (
        SETTINGS + SEED_LINE + BOOT,
        "seeds the database before the bootstrap",
    ),
    "no administrator set for the bootstrap": (
        "export ENVIRONMENT=development\n" + BOOT + SEED_LINE,
        "does not set FIRST_SUPERUSER before the bootstrap",
    ),
    "no administrator password set for the bootstrap": (
        "export ENVIRONMENT=development\nexport FIRST_SUPERUSER=admin@demo.com\n"
        + BOOT
        + SEED_LINE,
        "does not set FIRST_SUPERUSER_PASSWORD before the bootstrap",
    ),
    "ENVIRONMENT never set": (
        SETTINGS.replace("export ENVIRONMENT=development\n", "") + BOOT + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is None)",
    ),
    "only the deprecated APP_ENV set": (
        "export APP_ENV=development\n"
        + SETTINGS.replace("export ENVIRONMENT=development\n", "")
        + BOOT
        + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is None)",
    ),
    "ENVIRONMENT set to something else": (
        SETTINGS.replace("=development", "=production") + BOOT + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is 'production')",
    ),
    "ENVIRONMENT changed after it was set": (
        SETTINGS + "export ENVIRONMENT=staging\n" + BOOT + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is 'staging')",
    ),
    "ENVIRONMENT set only after the bootstrap": (
        SETTINGS.replace("export ENVIRONMENT=development\n", "")
        + BOOT
        + "export ENVIRONMENT=development\n"
        + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is None)",
    ),
    "a .env here-document is data, not settings": (
        "cat > .env <<'EOF'\n"
        "ENVIRONMENT=development\n"
        "FIRST_SUPERUSER=admin@demo.com\n"
        "FIRST_SUPERUSER_PASSWORD=x\n"
        "EOF\n" + BOOT + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is None)",
    ),
    "a bare assignment is not exported": (
        "ENVIRONMENT=development\n"
        "export FIRST_SUPERUSER=admin@demo.com\n"
        "export FIRST_SUPERUSER_PASSWORD=x\n" + BOOT + SEED_LINE,
        "does not set ENVIRONMENT=development before the bootstrap (it is None)",
    ),
    "the bootstrap's default administrator": (
        SETTINGS.replace("admin@demo.com", "admin@example.com") + BOOT + SEED_LINE,
        "FIRST_SUPERUSER is admin@example.com, not admin@demo.com",
    ),
    "another administrator, quoted": (
        SETTINGS.replace("=admin@demo.com", '="ops@demo.com"') + BOOT + SEED_LINE,
        "FIRST_SUPERUSER is ops@demo.com, not admin@demo.com",
    ),
    "the administrator changed after it was set": (
        SETTINGS + "export FIRST_SUPERUSER=admin@example.com\n" + BOOT + SEED_LINE,
        "FIRST_SUPERUSER is admin@example.com, not admin@demo.com",
    ),
    "an administrator that only begins admin@demo.com": (
        SETTINGS.replace("admin@demo.com", "admin@demo.com.evil.test")
        + BOOT
        + SEED_LINE,
        "FIRST_SUPERUSER is admin@demo.com.evil.test, not admin@demo.com",
    ),
}


@pytest.mark.parametrize(
    "script, expected", PLANTED_SCRIPT.values(), ids=list(PLANTED_SCRIPT)
)
def test_a_planted_database_step_is_named(script, expected):
    problems = database_step_problems(script)
    assert any(expected in p for p in problems), problems


ACCEPTED_SCRIPT = {
    "the settings exported, then the bootstrap": SETTINGS + BOOT + SEED_LINE,
    "a raw upgrade after the bootstrap": SETTINGS + BOOT + RAW + SEED_LINE,
    "ENVIRONMENT on the bootstrap's own line": (
        SETTINGS.replace("export ENVIRONMENT=development\n", "")
        + "ENVIRONMENT=development python -m backend.app.db.bootstrap\n"
        + SEED_LINE
    ),
    "quoted values": (
        'export ENVIRONMENT="development"\n'
        "export FIRST_SUPERUSER='admin@demo.com'\n"
        "export FIRST_SUPERUSER_PASSWORD='Demo1234!'\n" + BOOT + SEED_LINE
    ),
    "APP_ENV beside ENVIRONMENT": "export APP_ENV=development\n" + SETTINGS + BOOT,
}


@pytest.mark.parametrize("script", ACCEPTED_SCRIPT.values(), ids=list(ACCEPTED_SCRIPT))
def test_an_acceptable_database_step_is_not_a_problem(script):
    assert database_step_problems(script) == []

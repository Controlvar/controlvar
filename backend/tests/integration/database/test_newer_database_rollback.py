"""An older build meeting a database a newer release migrated: a rollback.

The rehearsal from the self-hosting plan's review (#238), as a test.  Release
N+1 is this tree plus one more core revision -- ``down_revision`` = this tree's
core head -- that creates a table, exactly what the next release to carry a
migration will look like.  The database is built by N, upgraded by N+1, and
then N starts against it again, which is what ``helm rollback`` or a re-pinned
image tag does.

N cannot resolve N+1's revision, so it must refuse -- exit non-zero, before
alembic touches anything -- and say what happened: *a newer release migrated
this database; run that release or restore the pre-upgrade backup*.  It used to
say "a database built by the full profile being opened by a core build ...
delete those rows from alembic_version", which was the wrong diagnosis and
harmful advice: deleting the row leaves N+1's table in place and unrecorded,
the next N run re-applies migrations over it, and rolling forward to N+1 again
dies on ``DuplicateTable``.

Both documented paths are exercised, because both reach the same guard: the
bootstrap the container runs on start-up, and the raw ``alembic upgrade
heads`` that ``deploy.yml`` and the ECS migration task run.  In-process
subprocesses rather than ``docker run``: the property is the bootstrap's, the
trees are built the way ``tree_profiles`` builds them for the other transition
tests, and a subprocess is what a container is to this code.

The refusal belongs to the bootstrap and to ``alembic upgrade heads``.  The
second test is the other half (#298): a deployment whose containers start with
``RUN_MIGRATIONS=false`` runs neither, so rolling back to N means N's **API**
serving the database N+1 migrated.  With a backward-compatible N+1 -- the only
kind a rollback without a restore can rely on -- N must come up ready and
answer a database-backed route as it normally does, not with a 500.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This tree's core head.  A literal, for the reason ``tree_profiles.CORE_HEAD``
#: gives; ``backend/tests/unit/db/test_alembic_plan.py`` pins it to the files.
CORE_HEAD = tree_profiles.CORE_HEAD
#: The modules branch's HEAD -- what a modules revision of release N+1 would
#: extend.  Not its first revision (``modules_0001_rbac``): the branch has two.
MODULES_HEAD = "modules_0002_warehouse_analysis"

#: The revision release N+1 adds, and the table it creates.  On the core chain
#: its id has no ``modules_`` prefix; on the modules branch it has, which is
#: what a build reads as "the other profile" -- and must not, from a full build.
NEXT_REVISION = "zz_next_release_0001"
NEXT_MODULES_REVISION = "modules_9999_next_release_probe"
NEXT_TABLE = "next_release_probe"

#: Where N+1's revision goes: (revision id, the head it extends).
CORE_CHAIN = "core_chain"
MODULES_BRANCH = "modules_branch"
_NEXT = {
    CORE_CHAIN: (NEXT_REVISION, CORE_HEAD),
    MODULES_BRANCH: (NEXT_MODULES_REVISION, MODULES_HEAD),
}

_NEXT_RELEASE_REVISION = '''"""A migration the next release carries (test fixture).

Revision ID: {revision}
Revises: {down_revision}
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "{revision}"
down_revision = "{down_revision}"
branch_labels = None
depends_on = None

SCHEMA = os.environ["POSTGRES_SCHEMA"]


def upgrade() -> None:
    op.create_table(
        "{NEXT_TABLE}",
        sa.Column("id", sa.Integer(), primary_key=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("{NEXT_TABLE}", schema=SCHEMA)
'''

#: Any advice to remove rows from the version table, however it is phrased.
_REMOVE_ROWS_ADVICE = re.compile(r"\b(delete|remove|truncate)\b", re.IGNORECASE)


@pytest.fixture
def scratch_schema(test_db):
    schema = f"rollback_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _this_release(profile: str, tmp_path):
    """Release N: this tree, as *profile* builds it, with its ``VERSION``."""
    if profile == FULL:
        return tree_profiles.REPO_ROOT
    tree = tree_profiles.core_tree(tmp_path / "release-n")
    (tree / "VERSION").write_bytes((tree_profiles.REPO_ROOT / "VERSION").read_bytes())
    return tree


def _next_release(profile: str, tmp_path, chain: str = CORE_CHAIN):
    """Release N+1: a copy of this tree plus one revision on *chain*."""
    destination = tmp_path / "release-n-plus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    revision, down_revision = _NEXT[chain]
    if chain == MODULES_BRANCH:
        # Built from parts: see tree_profiles._ALEMBIC_INI_PARTS.
        versions = tree.joinpath("modules", "backend", "app", "db", "migrations")
    else:
        versions = tree / "backend" / "app" / "db" / "migrations"
    (versions / "versions" / f"{revision}.py").write_text(
        _NEXT_RELEASE_REVISION.format(
            revision=revision, down_revision=down_revision, NEXT_TABLE=NEXT_TABLE
        ),
        encoding="utf-8",
    )
    return tree


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


@pytest.mark.regression
@pytest.mark.parametrize(
    ("profile", "chain"),
    [
        pytest.param(CORE, CORE_CHAIN, id="core"),
        pytest.param(FULL, CORE_CHAIN, marks=pytest.mark.modules, id="full"),
        # A newer *modules* revision met by a full build: the prefix says
        # "modules", the build is full, so it is a newer release -- which is
        # what the previous full image says to a database modules_0002 migrated.
        pytest.param(
            FULL, MODULES_BRANCH, marks=pytest.mark.modules, id=MODULES_BRANCH
        ),
    ],
)
def test_an_older_release_refuses_a_database_a_newer_release_migrated(
    test_db, scratch_schema, tmp_path, profile, chain
):
    next_revision = _NEXT[chain][0]
    release_n = _this_release(profile, tmp_path)
    release_n_plus_1 = _next_release(profile, tmp_path, chain)
    version = (tree_profiles.REPO_ROOT / "VERSION").read_text("utf-8").strip()

    # N builds the database; N+1 upgrades it and applies its migration.
    built = tree_profiles.bootstrap_schema(release_n, scratch_schema)
    assert built.returncode == 0, built.stderr[-3000:]
    upgraded = tree_profiles.bootstrap_schema(release_n_plus_1, scratch_schema)
    assert upgraded.returncode == 0, upgraded.stderr[-3000:]
    assert NEXT_TABLE in _tables(test_db, scratch_schema)
    after_upgrade = _rows(test_db, scratch_schema)
    assert next_revision in after_upgrade
    tables_after_upgrade = _tables(test_db, scratch_schema)

    # The rollback: N again, by both documented paths.
    rolled_back = tree_profiles.bootstrap_schema(release_n, scratch_schema)
    raw_upgrade = tree_profiles.alembic(release_n, scratch_schema, "upgrade", "heads")

    for result in (rolled_back, raw_upgrade):
        assert result.returncode != 0, result.stdout[-2000:]
        assert "migrated by a newer Controlvar release" in result.stderr, result.stderr[
            -3000:
        ]
        assert next_revision in result.stderr
        assert f"({version})" in result.stderr
        assert "restore the backup taken before that upgrade" in result.stderr
        assert "full profile being opened by a core build" not in result.stderr
        # The refusal's own sentence, not the traceback around it.
        refusal = result.stderr[result.stderr.rindex("RuntimeError") :]
        advice = _REMOVE_ROWS_ADVICE.search(refusal)
        assert advice is None, f"removal advice {advice.group(0)!r}: {refusal}"

    # Nothing recorded, nothing applied, nothing dropped.
    assert _rows(test_db, scratch_schema) == after_upgrade
    assert _tables(test_db, scratch_schema) == tables_after_upgrade


# ---------------------------------------------------------------------------
# Release N's API, started without migrations, on N+1's database (#298)
# ---------------------------------------------------------------------------
#: How long the API may take to start answering ``/health/ready``.  A ceiling
#: on the wait, never an assertion on how long it took.
READY_DEADLINE_SECONDS = 180


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _production_environment(port: int) -> dict:
    """What a production task sets, minus ``RUN_MIGRATIONS`` (nothing reads it
    but the entrypoint, which is not run here).  Fresh secrets per test; Redis
    at a port nothing listens on, since readiness does not require it."""
    return {
        "ENVIRONMENT": "production",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "FIRST_SUPERUSER_PASSWORD": secrets.token_urlsafe(24),
        "AUDIT_HMAC_KEY": secrets.token_urlsafe(48),
        "SSO_STATE_SECRET": secrets.token_urlsafe(48),
        "ALLOWED_HOSTS": "127.0.0.1",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        # The preflight refuses these in production; the task sets POSTGRES_*.
        "DATABASE_URI": None,
        "DATABASE_URL": None,
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "1",
        "REDIS_URI": None,
        "REDIS_URL": None,
    }


def _get(url: str, headers: dict | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def _wait_until_ready(server, base: str, log) -> tuple[int, str]:
    """Poll ``/health/ready`` until it answers 200, the server exits, or the
    deadline passes; return the last answer (``0`` for no answer at all)."""
    deadline = time.monotonic() + READY_DEADLINE_SECONDS
    last = (0, "no answer")
    while time.monotonic() < deadline:
        if server.poll() is not None:
            pytest.fail(
                f"the API exited {server.returncode} before it was ready:\n"
                + log.read_text("utf-8", "replace")[-4000:]
            )
        try:
            last = _get(f"{base}/health/ready")
        except OSError as error:  # not listening yet
            last = (0, repr(error))
        if last[0] == 200:
            return last
        time.sleep(0.5)
    return last


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param(CORE, id="core"),
        pytest.param(FULL, marks=pytest.mark.modules, id="full"),
    ],
)
def test_an_older_api_serves_a_database_a_newer_release_migrated(
    test_db, scratch_schema, tmp_path, profile
):
    release_n = _this_release(profile, tmp_path)
    release_n_plus_1 = _next_release(profile, tmp_path)

    # N builds the database; N+1 upgrades it and applies its migration.
    built = tree_profiles.bootstrap_schema(release_n, scratch_schema)
    assert built.returncode == 0, built.stderr[-3000:]
    upgraded = tree_profiles.bootstrap_schema(release_n_plus_1, scratch_schema)
    assert upgraded.returncode == 0, upgraded.stderr[-3000:]
    assert NEXT_REVISION in _rows(test_db, scratch_schema)
    rows_after_upgrade = _rows(test_db, scratch_schema)
    tables_after_upgrade = _tables(test_db, scratch_schema)

    # The rollback: N's container with RUN_MIGRATIONS=false -- the entrypoint's
    # settings preflight, then the server, and no bootstrap in between.
    port = _free_port()
    environment = _production_environment(port)
    preflight = tree_profiles.run(
        release_n, ["-m", "backend.app.core.preflight"], scratch_schema, environment
    )
    assert preflight.returncode == 0, preflight.stdout + preflight.stderr

    log = tmp_path / "api.log"
    server = tree_profiles.serve(release_n, scratch_schema, port, log, environment)
    base = f"http://127.0.0.1:{port}"
    try:
        ready = _wait_until_ready(server, base, log)
        assert ready[0] == 200, (
            f"/health/ready: {ready}\n" + log.read_text("utf-8", "replace")[-4000:]
        )
        # The profile N was built as, so "full" is not a core build in disguise.
        assert json.loads(ready[1])["profile"] == profile, ready

        # A route that reads N's models from the database: the SDK evaluation
        # looks the key up in api_keys (and its owner in users) before
        # anything else.  An unknown key is its normal 401 -- a 500 here is N's
        # code meeting a schema it cannot read.
        status, body = _get(
            f"{base}/api/v1/feature-flags/evaluate/rollback-probe?user_id=u1",
            {"X-API-Key": "not-a-key-this-database-issued"},
        )
        assert status == 401, (
            f"{status} {body[:500]}\n" + log.read_text("utf-8", "replace")[-4000:]
        )
        assert "Invalid API Key" in body
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=30)

    # Serving it recorded and changed nothing: N+1's revision and table remain.
    assert _rows(test_db, scratch_schema) == rows_after_upgrade
    assert _tables(test_db, scratch_schema) == tables_after_upgrade

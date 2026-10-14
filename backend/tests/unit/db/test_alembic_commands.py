"""Alembic as this repository configures it, read through alembic itself.

The migration scripts are read with alembic's ``ScriptDirectory`` from
``backend/app/db/alembic.ini``, and the commands an operator types are run
with ``python -m alembic`` where alembic answers without a database (``heads``,
``history``, and the refusal of the singular ``head``). The URL alembic and the
bootstrap connect with is read from what each builds. No database.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = REPO_ROOT / "backend" / "app" / "db" / "alembic.ini"
CORE_VERSIONS = REPO_ROOT / "backend" / "app" / "db" / "migrations" / "versions"
ENV_PY = REPO_ROOT / "backend" / "app" / "db" / "migrations" / "env.py"

CORE_HEAD = "37dcb2969766"
MODULES_HEAD = "modules_0002_warehouse_analysis"
#: The revision 1ab99332f0ba's re-run recipe downgrades to, and what lies past it.
EVENTS_UTC = "1ab99332f0ba"
BEFORE_EVENTS_UTC = "a89544fb1075"

#: A full checkout: the modules branch's version location exists.
FULL = (REPO_ROOT / "modules").is_dir()
full_only = pytest.mark.skipif(not FULL, reason="a core tree has one head")


def _script(version_locations: str | None = None) -> ScriptDirectory:
    config = Config(str(ALEMBIC_INI))
    if version_locations is not None:
        config.set_main_option("version_locations", version_locations)
    return ScriptDirectory.from_config(config)


def _alembic(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.regression
def test_alembic_manages_the_postgresql_schema():
    """alembic.ini's scripts are this repository's migrations, and the URL
    env.py gives alembic is a PostgreSQL one: libpq's own refusal below."""
    script = _script()
    assert Path(script.dir) == ENV_PY.parent
    assert len(list(script.walk_revisions())) >= 33, "alembic finds the migrations"
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABASE_")}
    env.update(POSTGRES_SERVER="127.0.0.1", POSTGRES_PORT="1")
    result = _alembic("current", **env)
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "psycopg2.OperationalError" in out, out[-2000:]
    assert 'connection to server at "127.0.0.1", port 1 failed' in out, out[-2000:]


@pytest.mark.regression
def test_migration_files_live_in_the_core_versions_directory_one_revision_each():
    script = _script(version_locations=str(CORE_VERSIONS))
    revisions = list(script.walk_revisions())
    files = sorted(p for p in CORE_VERSIONS.glob("*.py") if p.name != "__init__.py")
    assert {Path(r.path).parent for r in revisions} == {CORE_VERSIONS}
    assert sorted(Path(r.path) for r in revisions) == files, (
        "a file is not one revision"
    )
    ids = [r.revision for r in revisions]
    assert len(ids) == len(set(ids)) == len(files)


@pytest.mark.regression
def test_every_migration_has_an_upgrade_and_a_downgrade():
    """Each revision module, as alembic loads it, has both functions."""
    script = _script()
    revisions = list(script.walk_revisions())
    assert revisions
    missing = [
        f"{revision.revision}: no {name}()"
        for revision in revisions
        for name in ("upgrade", "downgrade")
        if not callable(getattr(revision.module, name, None))
    ]
    assert not missing, missing


@pytest.mark.regression
def test_heads_prints_the_core_head_and_in_a_full_tree_the_modules_head_labelled():
    result = _alembic("heads")
    assert result.returncode == 0, result.stderr
    lines = sorted(line for line in result.stdout.splitlines() if line.strip())
    expected = [f"{CORE_HEAD} (head)"]
    if FULL:
        expected.append(f"{MODULES_HEAD} (modules) (head)")
    assert lines == sorted(expected)


def _history() -> list[str]:
    """The lines ``alembic history`` prints, formatted as its command formats
    them (in-process: alembic.ini's ``revision_environment`` would otherwise
    open a database first)."""
    return [
        sc.cmd_format(
            verbose=False, include_branches=True, include_doc=True, include_parents=True
        )
        for sc in _script().walk_revisions(base="base", head="heads")
    ]


@pytest.mark.regression
def test_history_marks_the_newest_revision_of_each_branch_and_labels_the_modules_one():
    lines = _history()
    marked = [line for line in lines if "(head)" in line]
    assert sorted(line.split(" -> ")[1].split(" ")[0] for line in marked) == sorted(
        [CORE_HEAD, MODULES_HEAD] if FULL else [CORE_HEAD]
    ), marked
    labelled = [line for line in lines if "(modules)" in line]
    if FULL:
        assert len(labelled) == 2, labelled
        assert all(" -> modules_" in line for line in labelled), labelled
    else:
        assert labelled == []


@full_only
@pytest.mark.regression
def test_a_command_that_names_the_singular_head_fails_with_two_heads():
    result = _alembic("upgrade", "head", "--sql")
    assert result.returncode != 0
    assert (
        "FAILED: Multiple head revisions are present for given argument 'head'"
        in result.stdout + result.stderr
    )


@full_only
@pytest.mark.regression
def test_a_bare_minus_one_with_two_heads_warns_and_picks_one_branch():
    script = _script()
    heads = tuple(script.revision_map.heads)
    assert sorted(heads) == sorted([CORE_HEAD, MODULES_HEAD])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = [s.revision.revision for s in script._downgrade_revs("-1", heads)]
    assert len(plan) == 1 and plan[0] in heads, plan
    assert any("ambiguous" in str(w.message) for w in caught), caught


@pytest.mark.regression
def test_past_1ab99332f0ba_its_rerun_downgrade_also_undoes_the_later_revisions():
    """Downgrading to a89544fb1075 undoes 1ab99332f0ba alone only while it is
    the core head; from today's head it also runs every revision after it."""
    script = _script(version_locations=str(CORE_VERSIONS))
    alone = [
        s.revision.revision
        for s in script._downgrade_revs(BEFORE_EVENTS_UTC, (EVENTS_UTC,))
    ]
    assert alone == [EVENTS_UTC]
    from_head = [
        s.revision.revision
        for s in script._downgrade_revs(BEFORE_EVENTS_UTC, (CORE_HEAD,))
    ]
    assert from_head[-1] == EVENTS_UTC and len(from_head) > 1, from_head
    assert "806901fb7735" in from_head


def _bootstrap_url(env: dict[str, str]) -> str:
    probe = "from backend.app.db.bootstrap import database_url\nprint(database_url())\n"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.regression
def test_alembic_and_the_bootstrap_connect_with_the_postgres_settings():
    settings = {
        "POSTGRES_SERVER": "db.example.com",
        "POSTGRES_PORT": "6543",
        "POSTGRES_USER": "probe_user",
        "POSTGRES_PASSWORD": "probe-pass",
        "POSTGRES_DB": "probe_db",
        "POSTGRES_SCHEMA": "probe_schema",
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABASE_")}
    env.update(settings)
    url = _bootstrap_url(env)
    assert url == "postgresql://probe_user:probe-pass@db.example.com:6543/probe_db"

    # env.py: alembic's own connection attempt names the host and port it was
    # given. Nothing listens on port 1.
    env.update(POSTGRES_SERVER="127.0.0.1", POSTGRES_PORT="1")
    result = _alembic("current", **env)
    assert result.returncode != 0
    assert '"127.0.0.1", port 1' in result.stdout + result.stderr, result.stderr[-2000:]

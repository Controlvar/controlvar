"""The Database Migration workflow's target rule, checked against the real graph (#726).

``test_deploy_workflows.py`` runs the workflow's refusal step over fixed
lists. This file asks the revision graph whether those lists are the right
ones, with alembic's own planner and no database:

* every revision id on disk passes the workflow's allowlist, so the rule
  refuses no real migration;
* each downgrade the help text gives as an example unapplies exactly the
  revisions listed for it, on one branch, with no ambiguity warning -- a bare
  ``-1`` fails this, because alembic warns that it is ambiguous from two heads;
* the warning in the help text holds: a downgrade to the branch point
  ``a7b8c9d0e1f2`` also unapplies the whole modules branch.

The graph has two heads only in a full checkout, so the tests that need the
modules branch carry ``@pytest.mark.modules`` and are skipped in a core build
(``backend/tests/conftest.py``); in the full checkout CI runs, the zero-skip
check over this directory (``scripts/check_junit_skips.py``) holds them to
running.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = REPO_ROOT / "backend" / "app" / "db" / "alembic.ini"
DB_MIGRATE = REPO_ROOT / ".github" / "workflows" / "db-migrate.yml"
STEP = "Refuse a target alembic cannot take"

pytestmark = pytest.mark.skipif(
    not DB_MIGRATE.is_file(), reason="this tree has no .github/workflows"
)

#: The examples the help text and the runbook give, and the revisions each
#: unapplies from both heads, newest first.  The core example is the down
#: revision of the newest core migration: ``4c2c09a2b3e8``, which undoes
#: ``607c51a59bf5`` (#1497), since that revision landed; ``37dcb2969766``
#: (#1206), ``d29a479daafe`` (#440) and ``a89544fb1075`` before it.  A core
#: revision added later moves the example to its own down revision or joins this
#: list, and the documents that name the example must say what the downgrades
#: it runs drop (``test_documented_downgrade_targets.py``).
NAMED_DOWNGRADES = {
    "4c2c09a2b3e8": ["607c51a59bf5"],
    # docs/deployment/rollback-runbook.md, Step 2: "Going one further",
    # "Going further", "Going further still" and "Further again".
    "37dcb2969766": ["607c51a59bf5", "4c2c09a2b3e8"],
    "d29a479daafe": ["607c51a59bf5", "4c2c09a2b3e8", "37dcb2969766"],
    "806901fb7735": ["607c51a59bf5", "4c2c09a2b3e8", "37dcb2969766", "d29a479daafe"],
    "1ab99332f0ba": [
        "607c51a59bf5",
        "4c2c09a2b3e8",
        "37dcb2969766",
        "d29a479daafe",
        "806901fb7735",
    ],
    # docs/deployment/rollback-runbook.md, Step 2, and the operator page's
    # Rolling Back: the modules branch, back to a named revision (#1386).
    "modules_0002_warehouse_analysis": ["modules_0003_lower_is_better"],
    "modules_0001_rbac": [
        "modules_0003_lower_is_better",
        "modules_0002_warehouse_analysis",
    ],
}
BRANCH_POINT = "a7b8c9d0e1f2"
MODULES_PREFIX = "modules_"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def _downgrade_plan(script: ScriptDirectory, target: str) -> list[str]:
    heads = tuple(script.revision_map.heads)
    with warnings.catch_warnings():
        # "downgrade -1 from multiple heads is ambiguous" is only a warning in
        # alembic, and the command exits 0; here it is a failure.
        warnings.simplefilter("error")
        return [s.revision.revision for s in script._downgrade_revs(target, heads)]


def _allowlist() -> re.Pattern[str]:
    """The id pattern the workflow's step tests a target against, read from it."""
    document = yaml.safe_load(DB_MIGRATE.read_text(encoding="utf-8"))
    (job,) = [j for j in document["jobs"].values() if "environment" in j]
    (step,) = [s for s in job["steps"] if s.get("name") == STEP]
    patterns = re.findall(r'\[\[ "\$TARGET" =~ (\S+) \]\]', step["run"])
    # The last test is the id allowlist; the first only decides whether the
    # target is safe to print in the refusal.
    assert len(patterns) == 2, patterns
    return re.compile(patterns[-1])


@pytest.mark.regression
def test_every_revision_id_passes_the_workflow_allowlist():
    allowlist = _allowlist()
    ids = [r.revision for r in _script().walk_revisions()]
    assert len(ids) >= 30, f"found only {len(ids)} revisions: the scan is not reading"
    assert [i for i in ids if not allowlist.fullmatch(i)] == []
    assert {"head", "base", "heads"}.isdisjoint(ids)


def _version_num_width() -> int:
    """The width of ``alembic_version.version_num``, as alembic creates it.

    Read from alembic's own PostgreSQL implementation, not typed here: the
    column is ``String(32)`` in alembic 1.20.0.
    """
    from alembic.ddl.impl import DefaultImpl
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    impl = DefaultImpl.get_by_dialect(dialect)(dialect, None, True, None, None, {})
    table = impl.version_table_impl(
        version_table="alembic_version",
        version_table_schema=None,
        version_table_pk=True,
    )
    return table.c.version_num.type.length


@pytest.mark.regression
def test_every_revision_id_fits_the_version_table():
    """A revision id longer than ``version_num`` passes every check without a
    database -- the allowlist above takes 64 characters -- and then fails the
    stamp of every fresh install and the migration task's ``upgrade heads``
    with ``value too long for type character varying(32)`` (#1386: a 45-character
    id was planned and caught only by a database run)."""
    width = _version_num_width()
    assert width, "alembic's version table declares no width"
    ids = [r.revision for r in _script().walk_revisions()]
    assert len(ids) >= 30, f"found only {len(ids)} revisions: the scan is not reading"
    too_long = [(i, len(i)) for i in ids if len(i) > width]
    assert too_long == [], f"width {width} too long: {too_long}"


@pytest.mark.regression
@pytest.mark.modules
@pytest.mark.parametrize("target", sorted(NAMED_DOWNGRADES))
def test_each_named_downgrade_unapplies_its_listed_revisions_on_one_branch(target):
    script = _script()
    assert len(script.revision_map.heads) == 2
    plan = _downgrade_plan(script, target)
    assert plan == NAMED_DOWNGRADES[target]
    branches = {rev.startswith(MODULES_PREFIX) for rev in plan}
    assert len(branches) == 1


@pytest.mark.regression
@pytest.mark.modules
def test_the_branch_point_also_unapplies_the_modules_branch():
    """Backs the help text: "a core id at or below a7b8c9d0e1f2 also unapplies
    the modules branch"."""
    plan = _downgrade_plan(_script(), BRANCH_POINT)
    # The eleven core revisions above it (through 607c51a59bf5, #1497) and the
    # modules branch's three (through modules_0003_lower_is_better, #1386).
    assert len(plan) == 14, plan
    assert [r for r in plan if r.startswith(MODULES_PREFIX)] == [
        "modules_0003_lower_is_better",
        "modules_0002_warehouse_analysis",
        "modules_0001_rbac",
    ]

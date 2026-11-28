"""The lock budget of each release's upgrade, as a literal map (#449).

Every revision a release's upgrade runs declares, in seconds, how long it can
make the upgrade wait for a lock: the sum of the ``lock_timeout`` bounds it
sets, one around each block of statements that can wait (PostgreSQL applies a
bound to each lock it waits for, so a revision with two blocks can wait for
both), or 0 when nothing it does can wait. The upgrade runs all of its
revisions in one transaction (``env.py`` sets no
``transaction_per_migration``), so the waits add up, and their sum is at most
``CAP_SECONDS``. A lock timeout in any revision rolls back every revision of
that run.

**One map per release, each with its start heads.** A release's upgrade starts
at the previous release's heads, which are already applied and are in neither
the walk nor the map. Its walk is the ancestors of where it ends (the next
release's start heads, or the current heads for the newest release) minus the
ancestors of its start heads that exist in this profile: a core tree has no
modules branch, so ``modules_0005_redshift`` is simply absent there. Never walk
from one start head: from ``t1812_user_totp`` alone the walk takes in every
modules revision. A later release adds its own entry to ``RELEASES`` beside the
earlier ones and changes none of them.

Every revision in a walk must be in its release's map, and each release's sum
is at most ``CAP_SECONDS``. No database and no ``.git``: it runs the same in
``scripts/core_build.sh``'s tree.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = REPO_ROOT / "backend" / "app" / "db" / "alembic.ini"

#: The most an upgrade of one release may wait for locks, all its revisions
#: together, in seconds.
CAP_SECONDS = 4.0


@dataclass(frozen=True)
class Release:
    """One release's upgrade: where it starts, and what each revision declares."""

    #: The release this upgrade installs.
    name: str
    #: The previous release's heads. Already applied: in neither the walk nor
    #: the map.
    start_heads: Tuple[str, ...]
    #: Each revision the upgrade runs, and its declared lock bound in seconds.
    declared: Dict[str, float]


#: Oldest first; a release's walk ends where the next one's starts. A revision
#: declares the sum of its blocks' bounds, leaving out a block on a table that
#: an earlier revision of the same run creates: no other session can hold a
#: lock on it, so that block waits for nothing. 0.48.0's upgrade starts at
#: 0.47.0's heads and runs ``t449_identity_aliases``, whose ``SET LOCAL
#: lock_timeout = '250ms'`` bounds its one wait (behind a concurrent creation of
#: the same table). 0.49.0's starts at 0.48.0's released heads and runs
#: ``t2011_flag_environments``, which has two blocks: ``'500ms'`` for the lock
#: its new table's foreign key takes on ``feature_flags``, then ``'1500ms'``
#: for ``api_keys``, 2 s in all; then ``t2012_flag_split``, which has two blocks
#: of ``'1s'``, one for ``feature_flags`` and one for the table
#: ``t2011_flag_environments`` created, 1 s in all; then
#: ``t1934_metric_window``, which has one block of ``'500ms'``, for
#: ``metrics``, 0.5 s; and, where the modules branch exists,
#: ``modules_0006_saml_requests``, whose one block of ``'250ms'`` bounds the
#: same wait as ``t449_identity_aliases``'s, 0.25 s. 0.49.0's sum is 3.75 s
#: (+0.25 s for ``modules_0006_saml_requests``, from 3.5 s). From a database
#: already at
#: ``t2011_flag_environments`` (an install from ``main``, which no release
#: left) both of ``t2012_flag_split``'s blocks can wait, 2 s in all. A tuple of
#: records, not a dict keyed by release:
#: ``test_engine_versions_frozen.py`` reads a dict keyed ``N.N.N`` as a table of
#: stats-engine pins, and these are product releases.
RELEASES: Tuple[Release, ...] = (
    Release(
        name="0.48.0",
        start_heads=("t1812_user_totp", "modules_0005_redshift"),
        declared={"t449_identity_aliases": 0.25},
    ),
    # t2011: 500 ms on feature_flags, then 1500 ms on api_keys. t2012: 1 s on
    # feature_flags. t1934: 500 ms on metrics. modules_0006: 250 ms on the
    # creation of its own table.
    Release(
        name="0.49.0",
        start_heads=("t449_identity_aliases", "modules_0005_redshift"),
        declared={
            "t2011_flag_environments": 2,
            "t2012_flag_split": 1,
            "t1934_metric_window": 0.5,
            "modules_0006_saml_requests": 0.25,
        },
    ),
)


def release(name: str) -> Release:
    """The entry of ``RELEASES`` for *name*."""
    (found,) = [entry for entry in RELEASES if entry.name == name]
    return found


def end_heads(name: str) -> Optional[Tuple[str, ...]]:
    """Where *name*'s walk ends: the next release's start heads, or ``None``
    (the current heads) for the newest release."""
    names = [entry.name for entry in RELEASES]
    index = names.index(name)
    return RELEASES[index + 1].start_heads if index + 1 < len(RELEASES) else None


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def _known(script: ScriptDirectory) -> Set[str]:
    return {revision.revision for revision in script.walk_revisions()}


def _parents(script: ScriptDirectory, revision: str) -> Tuple[str, ...]:
    found = script.get_revision(revision)
    down = found.down_revision
    if down is None:
        parents: Tuple[str, ...] = ()
    elif isinstance(down, str):
        parents = (down,)
    else:
        parents = tuple(down)
    depends = found.dependencies
    if isinstance(depends, str):
        parents += (depends,)
    elif depends:
        parents += tuple(depends)
    return parents


def ancestors(script: ScriptDirectory, heads: Iterable[str]) -> Set[str]:
    """*heads* and every revision below them."""
    seen: Set[str] = set()
    stack = list(heads)
    while stack:
        revision = stack.pop()
        if revision in seen:
            continue
        seen.add(revision)
        stack.extend(_parents(script, revision))
    return seen


def walk(
    script: ScriptDirectory,
    release: Release,
    end_heads: Optional[Iterable[str]] = None,
) -> List[str]:
    """The revisions *release*'s upgrade runs in this profile, sorted.

    Ancestors of *end_heads* (the current heads when ``None``) minus the
    ancestors of the start heads this profile has.
    """
    known = _known(script)
    start = [head for head in release.start_heads if head in known]
    assert start, (
        f"none of the start heads {release.start_heads} exists in this tree: a "
        "walk from nowhere would take in every revision"
    )
    end = (
        script.get_heads()
        if end_heads is None
        else [head for head in end_heads if head in known]
    )
    return sorted(ancestors(script, end) - ancestors(script, start))


def problems(
    script: ScriptDirectory, releases: Tuple[Release, ...]
) -> Dict[str, List[str]]:
    """What each release's walk refuses, as ``{release: [problem, ...]}``."""
    found: Dict[str, List[str]] = {}
    for index, entry in enumerate(releases):
        following = releases[index + 1] if index + 1 < len(releases) else None
        end = None if following is None else following.start_heads
        revisions = walk(script, entry, end)
        missing = [revision for revision in revisions if revision not in entry.declared]
        if missing:
            found.setdefault(entry.name, []).append(
                f"revisions with no declared lock bound: {missing}"
            )
        total = sum(entry.declared.get(revision, 0.0) for revision in revisions)
        if total > CAP_SECONDS:
            found.setdefault(entry.name, []).append(
                f"declared lock sum {total:g} s exceeds {CAP_SECONDS:g} s"
            )
    return found


def test_every_revision_each_release_runs_is_declared_and_the_sum_fits():
    script = _script()
    for entry in RELEASES:
        print(f"{entry.name}: walk {walk(script, entry, end_heads(entry.name))}")
    assert problems(script, RELEASES) == {}


def test_the_releases_are_named_once_each():
    names = [entry.name for entry in RELEASES]
    assert names and len(names) == len(set(names)), names


def test_0_48_0_runs_t449_alone_from_0_47_0_and_declares_a_quarter_second():
    """The walk excludes 0.47.0's heads in both profiles: from
    ``t1812_user_totp`` (with ``modules_0005_redshift`` where it exists) the
    upgrade runs ``t449_identity_aliases`` and nothing else."""
    entry = release("0.48.0")
    assert walk(_script(), entry, release("0.49.0").start_heads) == [
        "t449_identity_aliases"
    ]
    assert entry.declared == {"t449_identity_aliases": 0.25}


def test_0_49_0_starts_at_0_48_0s_released_heads_and_declares_three_and_three_quarter_seconds():
    """0.48.0's walk now ends at 0.48.0's released heads, which shipped in
    v0.48.0 (``t449_identity_aliases`` and, where the modules branch exists,
    ``modules_0005_redshift``), and 0.49.0's starts there. In both profiles
    0.49.0's upgrade runs ``t2011_flag_environments``, which declares 2 s,
    ``t2012_flag_split``, which declares 1 s, and ``t1934_metric_window``, which
    declares 0.5 s; where the modules branch exists it also runs
    ``modules_0006_saml_requests``, which declares 0.25 s: 3.75 s, 0.25 s more
    than the 3.5 s before it, under the 4 s cap. alembic runs that revision
    first, before ``t2011_flag_environments`` takes the ``api_keys`` lock, so
    no SDK request queues through its wait (the migrations page says so)."""
    entry = release("0.49.0")
    assert entry.start_heads == ("t449_identity_aliases", "modules_0005_redshift")
    assert end_heads("0.48.0") == entry.start_heads
    core = ["t1934_metric_window", "t2011_flag_environments", "t2012_flag_split"]
    full = (REPO_ROOT / "modules").is_dir()
    expected = sorted(core + ["modules_0006_saml_requests"]) if full else core
    assert walk(_script(), entry, end_heads("0.49.0")) == expected
    assert entry.declared == {
        "t2011_flag_environments": 2,
        "t2012_flag_split": 1,
        "t1934_metric_window": 0.5,
        "modules_0006_saml_requests": 0.25,
    }
    assert sum(entry.declared.values()) == 3.75
    assert CAP_SECONDS == 4.0
    if full:
        order = [
            step.revision.revision
            for step in _script()._upgrade_revs("heads", entry.start_heads)
        ]
        assert order[0] == "modules_0006_saml_requests", order


def test_a_declared_bound_is_a_positive_number_or_zero():
    for entry in RELEASES:
        for revision, seconds in entry.declared.items():
            assert isinstance(seconds, (int, float)), (entry.name, revision)
            assert 0 <= seconds <= CAP_SECONDS, (entry.name, revision, seconds)


# ---------------------------------------------------------------------------
# 0.47.0's revisions, which an upgrade from 0.46.0 or earlier also runs
# ---------------------------------------------------------------------------
#: The revisions v0.47.0 added. None bounds its locks; the self-hosting
#: migrations page says so for an upgrade that skips 0.47.0.
V0_47_0_REVISIONS = ("4955571d232c", "t1812_user_totp", "modules_0005_redshift")
#: The ``op`` calls that alter an existing table, and the position of the
#: table among each call's arguments.
_ALTERING_OPS = {
    "add_column": 0,
    "alter_column": 0,
    "drop_column": 0,
    "drop_constraint": 1,
    "create_check_constraint": 1,
    "create_unique_constraint": 1,
    "create_foreign_key": 1,
}


def _revision_paths(script: ScriptDirectory) -> Dict[str, Path]:
    """The files of 0.47.0's revisions this tree has.

    A core tree (``scripts/core_build.sh`` deletes ``modules/``) has no modules
    branch, and must not have ``modules_0005_redshift`` either.
    """
    known = _known(script)
    full = (REPO_ROOT / "modules").is_dir()
    expected = {
        revision
        for revision in V0_47_0_REVISIONS
        if full or not revision.startswith("modules_")
    }
    present = {revision for revision in V0_47_0_REVISIONS if revision in known}
    assert present == expected, (sorted(present), sorted(expected))
    return {revision: Path(script.get_revision(revision).path) for revision in present}


def _altered_tables(path: Path) -> Set[str]:
    """The tables ``upgrade()`` alters with an ``op`` call, constants resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: Dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            names[node.targets[0].id] = node.value.value
    tables: Set[str] = set()
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "upgrade"):
            continue
        for call in ast.walk(node):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "op"
                and call.func.attr in _ALTERING_OPS
                and len(call.args) > _ALTERING_OPS[call.func.attr]
            ):
                continue
            table = call.args[_ALTERING_OPS[call.func.attr]]
            if isinstance(table, ast.Constant) and isinstance(table.value, str):
                tables.add(table.value)
            elif isinstance(table, ast.Name) and table.id in names:
                tables.add(names[table.id])
    return tables


def test_0_47_0_revisions_set_no_lock_timeout_and_4955571d232c_alters_experiments_and_variants():
    """The page's last sentence about an upgrade from 0.46.0 or earlier: none of
    0.47.0's revisions sets a lock timeout, so the waits behind them are
    unbounded, and 4955571d232c alters ``experiments`` and ``variants``, which
    SDK requests read."""
    paths = _revision_paths(_script())
    for revision, path in sorted(paths.items()):
        source = path.read_text(encoding="utf-8")
        assert "lock_timeout" not in source.lower(), f"{revision} sets lock_timeout"
    altered = _altered_tables(paths["4955571d232c"])
    assert {"experiments", "variants"} <= altered, sorted(altered)

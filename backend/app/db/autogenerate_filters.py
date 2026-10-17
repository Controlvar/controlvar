"""
What alembic's autogenerate must leave alone in the core migration chain.

Two kinds of object, for the same reason: they belong to the optional modules,
so the core chain may neither create them nor drop them.

**The two cross-boundary foreign keys.**  The workspaces module's model
(``modules/backend/app/models/workspace.py``) attaches two foreign keys to core
tables -- ``experiments.workspace_id`` and ``feature_flags.workspace_id`` ->
``workspaces.id`` -- whenever the modules' models are loaded.  Those
constraints are managed by the modules' alembic branch
(``modules/backend/app/db/migrations``, issue #89), never by this chain: core
migration ``a7b8c9d0e1f2`` drops them on every database, and the modules
branch's base revision (``modules_0001_rbac``) puts them back.  A full checkout
whose database has not run the branch yet therefore differs from its metadata
by exactly these two, and an autogenerate that emitted them would put a
reference to ``workspaces`` into a core migration, which then fails ``upgrade
heads`` on every core database and silently re-couples the profiles.

**The fourteen module tables.**  Nine of them are created by revisions that
sit in the *core* chain for historical reasons (``ep057_workspaces``,
``ep037``, ``ep050``, ``ep034``, ``e181583b4b24``) and the other five by the
modules branch (``modules_0002_warehouse_analysis`` also replaces the
``warehouse_connections`` that ``e181583b4b24`` made), so a database can
perfectly well hold all fourteen while this build's ``Base.metadata`` holds
none of them -- a core checkout, or a full checkout
whose registration was asked for core models only.  Since the move under
``modules/`` turned ``include_schemas=True`` on in ``migrations/env.py`` (main
reflects only ``public`` and so never saw them), autogenerate reflects the
application schema and sees them; without this filter it proposes one
``op.drop_table`` call per module table (and their indexes), and applying that revision to a
full-profile database destroys every workspace, SSO configuration, BAA and PHI
audit row.  Reproduced in review round 3: a core checkout, a database with the
module tables, one ``alembic revision --autogenerate``, twelve drops (there
were twelve module tables then).

The alternative -- rely on the modules being loaded so the tables are in the
metadata -- is what already failed: the loaded profile is a property of the
*process*, and the case that matters is the process that has no modules at all.
A name list is the only thing a core build can answer the question with, so
:data:`MODULE_TABLES` is that list, and
``backend/tests/unit/db/test_autogenerate_filters.py`` pins it against the
MODULE TABLES section of ``modules-manifest.txt`` -- the manifest stays the
source of truth, and the copy here cannot drift from it silently.  (The
manifest itself cannot be read at runtime: ``backend/Dockerfile`` copies
``backend/`` and ``modules/backend/`` into the image and nothing else.)

The filter applies to the core chain only.  A revision generated for the
modules branch -- ``alembic revision --head modules@head`` (or
``--branch-label modules`` for a new base, or a ``--version-path`` under
``modules/``) -- must see the constraints and the tables, because that branch
is exactly where they are managed.  The decision is made from the ``alembic
revision`` command line, never from the profile that happens to be loaded: a
developer on the full profile generating a *core* migration is the common case,
and one that must stay filtered.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable, Optional

#: Constraint names the workspaces module's model owns.
MODULE_MANAGED_CONSTRAINTS = frozenset(
    {"experiments_workspace_id_fkey", "feature_flags_workspace_id_fkey"}
)

#: Tables the optional modules own, whatever profile this process is running.
#:
#: The source of truth is the MODULE TABLES section of
#: ``modules-manifest.txt``; this is the copy core code can read with no
#: repository around it, and ``test_autogenerate_filters.py`` fails when the
#: two disagree.  Adding a module model means adding its table here *and*
#: there.
MODULE_TABLES = frozenset(
    {
        "workspaces",
        "workspace_members",
        "workspace_invites",
        "workspace_api_keys",
        "sso_configs",
        "custom_roles",
        "user_custom_roles",
        "direct_permission_grants",
        "baa_configs",
        "phi_audit_logs",
        "warehouse_connections",
        "warehouse_sources",
        "warehouse_analysis_runs",
        "integration_configs",
    }
)

#: The modules branch label (``branch_labels`` of ``modules_0001_rbac``).
MODULES_BRANCH = "modules"

#: The modules branch's versions directory, resolved from this file rather
#: than matched by name.  A bare ``"modules" in path.parts`` test answers True
#: for a *core* versions directory that merely sits under some parent called
#: ``modules`` -- a checkout in ``~/modules/controlvar`` -- and
#: would then hand a core revision no filter at all.
MODULES_VERSIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[3]
    / "modules"
    / "backend"
    / "app"
    / "db"
    / "migrations"
    / "versions"
)

IncludeObject = Callable[[Any, Optional[str], str, bool, Any], bool]


def owning_table_name(obj: Any, name: str | None, type_: str) -> Optional[str]:
    """The table *obj* belongs to, for any object type autogenerate reports.

    For ``type_ == "table"`` that is *name* itself; for a column, index or
    constraint it is ``obj.table.name``.  ``None`` when the object has no
    table -- a schema, or something a future alembic reports that has none.
    """
    if type_ == "table":
        return name
    table = getattr(obj, "table", None)
    return getattr(table, "name", None)


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """alembic ``include_object`` hook for the **core** chain.

    Skips the module-managed constraints and everything belonging to a module
    table -- the table itself and its columns, indexes and constraints -- so
    that a core autogenerate neither creates nor drops what the modules branch
    owns.  ``reflected`` is deliberately not consulted: the answer is the same
    whichever side of the comparison the object came from, which is what makes
    "the core chain never writes DDL for a module table" true rather than
    "true for the databases we happened to try".
    """
    if type_ == "foreign_key_constraint" and name in MODULE_MANAGED_CONSTRAINTS:
        return False
    if owning_table_name(obj, name, type_) in MODULE_TABLES:
        return False
    return True


def targets_modules_branch(cmd_opts: Any) -> bool:
    """Whether an ``alembic revision`` command line generates for the ``modules`` branch.

    *cmd_opts* is alembic's parsed command line (``config.cmd_opts``); it is
    ``None`` when alembic is driven from Python (``db/bootstrap.py``), which
    never generates a revision.
    """
    if cmd_opts is None:
        return False
    heads = getattr(cmd_opts, "head", None) or ""
    if isinstance(heads, str):
        heads = [heads]
    if any(h == MODULES_BRANCH or h.startswith(f"{MODULES_BRANCH}@") for h in heads):
        return True
    if (getattr(cmd_opts, "branch_label", None) or "") == MODULES_BRANCH:
        return True
    version_path = getattr(cmd_opts, "version_path", None) or ""
    if not version_path:
        return False
    # Compare resolved paths: a name match would fire for a core versions
    # directory under any parent called "modules" (see MODULES_VERSIONS_DIR).
    try:
        target = pathlib.Path(str(version_path)).resolve()
    except OSError:  # pragma: no cover -- an unresolvable path is not ours
        return False
    return target == MODULES_VERSIONS_DIR


def include_object_for(cmd_opts: Any) -> Optional[IncludeObject]:
    """The ``include_object`` hook env.py should configure for this command.

    ``None`` -- no filter -- for a revision aimed at the modules branch; the
    core filter for everything else.
    """
    if targets_modules_branch(cmd_opts):
        return None
    return include_object

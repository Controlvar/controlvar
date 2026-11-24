"""Flag environments: each flag's staging and development settings, and each key's environment

Adds, for #2011, and nothing else:

* the table ``flag_environment_settings``: a flag's configuration in staging
  or development, at most one row per flag and environment. The BaseModel
  columns ``id`` (``uuid``, the primary key), ``created_at`` and ``updated_at``
  (``timestamp``, NOT NULL, with ``ix_<schema>_flag_environment_settings_created_at``),
  and

  - ``feature_flag_id`` (``uuid``, NOT NULL, referencing ``feature_flags.id``
    ON DELETE CASCADE);
  - ``environment`` (``varchar(32)``, NOT NULL), with
    ``check_flag_environment_name``: ``staging`` or ``development`` only, since
    production's configuration is the flag row;
  - ``is_active`` (``boolean``, NOT NULL DEFAULT ``false``),
    ``targeting_rules`` (``jsonb``, nullable) and ``rollout_percentage``
    (``integer``, NOT NULL DEFAULT ``0``, with ``check_flag_environment_rollout``,
    0 to 100);
  - ``uq_flag_environment``: UNIQUE ``(feature_flag_id, environment)``;

* ``api_keys.environment`` (``varchar(32)``, NOT NULL DEFAULT ``'production'``)
  with ``check_api_key_environment``: ``production``, ``staging`` or
  ``development``.

The same names, types and defaults as the models
(``FlagEnvironmentSetting`` in ``backend/app/models/feature_flag.py``,
``APIKey.environment`` in ``backend/app/models/api_key.py``).

**Expand only.** ``feature_flags`` is not changed and there is no data step:
every flag's configuration is its production configuration, staging and
development are off for every flag, and every key already stored is a
production key. The previous release reads and writes exactly what this
release treats as production, and a key it inserts without naming the column
gets ``production`` from the default. Adding a column with a constant default
rewrites no row (PostgreSQL 11 and later). Each step runs only when its object
is absent: a schema built by ``db/bootstrap.py``'s ``create_all`` already has
every one, and the transition tests rewind such a schema's ``alembic_version``
and replay the chain from there. The existence checks read ``pg_catalog``
only.

**Two bounded waits, the api_keys one last.** The upgrade locks two existing
tables, each in its own block of ``SET LOCAL lock_timeout`` ... ``SET LOCAL
lock_timeout TO DEFAULT``, so no later revision inherits a bound:

1. ``_FLAGS_LOCK_TIMEOUT`` (500 ms): creating the table takes SHARE ROW
   EXCLUSIVE on ``feature_flags`` (the foreign key). It waits only behind a
   session writing to ``feature_flags``; SDK requests only read it and are
   not blocked.
2. ``_KEYS_LOCK_TIMEOUT`` (1500 ms): the column and the check take ACCESS
   EXCLUSIVE on ``api_keys``, which every SDK request reads, so new key
   lookups queue behind this step while it waits and until the upgrade
   commits. It runs last for that reason; the bound is above PostgreSQL's
   default ``deadlock_timeout`` (1 s), after which an autovacuum in the way is
   cancelled.

The declared bound, 2 s, is their sum: this revision's entry in
``backend/tests/unit/db/test_upgrade_lock_budget.py``. A lock timeout
(SQLSTATE 55P03) or a deadlock (40P01) in either block refuses the upgrade
with ``_lock_refusal()``. ``upgrade heads`` runs every pending revision in one
transaction (``env.py`` sets no ``transaction_per_migration``), so the refusal
rolls back every revision of that run: nothing is applied, and running the
upgrade again is the whole remedy.

**The downgrade loses every staging and development setting and key
binding**, and takes the same two locks, ``api_keys`` first: every key whose
environment is not ``production`` is made inactive (so it answers 401, and is
never served production's configuration by the release rolled back to), then
``check_api_key_environment`` and ``api_keys.environment`` are dropped by name
(so ``test_documented_downgrade_targets.py``, which reads ``drop_column``
calls, sees this downgrade), all within ``_KEYS_LOCK_TIMEOUT``; then the table
within ``_FLAGS_LOCK_TIMEOUT`` (dropping its foreign key takes ACCESS
EXCLUSIVE on ``feature_flags``). Each step runs only when its object is
present. Production flags and keys are unaffected. It does not refuse over
data, and nothing restores what it drops: an upgrade afterwards starts every
flag off in staging and development, and the deactivated keys come back as
inactive production keys. A lock timeout or a deadlock refuses it with
``_downgrade_lock_refusal()``: nothing is changed, and it is run again.

**Offline mode is refused** in both directions: each reads the catalog
before it changes anything, so neither can be emitted as SQL.

No application import: a migration stays what it was the day it was written.

Core chain, extending the core head ``t449_identity_aliases`` (#449), 0.48.0's.
Nothing here touches a module table, so a full checkout still has exactly two
heads: this revision and the modules branch's head (``modules_0005_redshift``).

Revision ID: t2011_flag_environments
Revises: t449_identity_aliases
Create Date: 2026-11-24
"""

import os
from typing import Callable, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "t2011_flag_environments"
down_revision: Union[str, None] = "t449_identity_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_SETTINGS = "flag_environment_settings"
_FLAGS = "feature_flags"
_SETTINGS_CREATED_INDEX = f"ix_{_SCHEMA}_flag_environment_settings_created_at"
_NAME_CHECK = "check_flag_environment_name"
_NAME_CHECK_SQL = "environment IN ('staging', 'development')"
_ROLLOUT_CHECK = "check_flag_environment_rollout"
_ROLLOUT_CHECK_SQL = "rollout_percentage >= 0 AND rollout_percentage <= 100"
_UNIQUE = "uq_flag_environment"

_KEYS = "api_keys"
_KEY_COLUMN = "environment"
_KEY_DEFAULT = "production"
_KEY_CHECK = "check_api_key_environment"
_KEY_CHECK_SQL = "environment IN ('production', 'staging', 'development')"

#: The bound on each wait, per table. Never "0": PostgreSQL reads 0 as no
#: timeout at all. Their sum is the 2 s the budget map declares.
_FLAGS_LOCK_TIMEOUT = "500ms"
_KEYS_LOCK_TIMEOUT = "1500ms"

#: What ends a wait for a lock, in every block of both directions: a lock
#: timeout (55P03) or a deadlock (40P01).
_LOCK_REFUSED = ("55P03", "40P01")

_OFFLINE_REFUSAL = (
    "t2011_flag_environments reads the catalog before it changes anything, so "
    "it cannot be emitted as SQL; run it online (without --sql)."
)

#: (bound, what the lock is for) per table and direction.
_UPGRADE_WAITS = {
    _FLAGS: (
        _FLAGS_LOCK_TIMEOUT,
        f"to create {_SETTINGS}, which references it",
    ),
    _KEYS: (_KEYS_LOCK_TIMEOUT, f"to add the column {_KEY_COLUMN}"),
}
_DOWNGRADE_WAITS = {
    _KEYS: (
        _KEYS_LOCK_TIMEOUT,
        "to deactivate the staging and development keys and drop the column "
        f"{_KEY_COLUMN}",
    ),
    _FLAGS: (_FLAGS_LOCK_TIMEOUT, f"to drop {_SETTINGS}, which references it"),
}


def _pgcode(exc: BaseException) -> Union[str, None]:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _lock_refusal(table: str) -> str:
    bound, purpose = _UPGRADE_WAITS[table]
    return "\n".join(
        [
            f"Refusing to upgrade schema {_SCHEMA}: t2011_flag_environments could "
            f"not take the lock it needs on {_SCHEMA}.{table} {purpose}, because "
            f"another session held it (this step's bound is {bound}).",
            "Nothing was applied: every revision of this upgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the upgrade again.",
        ]
    )


def _downgrade_lock_refusal(table: str) -> str:
    bound, purpose = _DOWNGRADE_WAITS[table]
    return "\n".join(
        [
            f"Refusing to downgrade schema {_SCHEMA}: t2011_flag_environments "
            f"could not take the lock it needs on {_SCHEMA}.{table} {purpose}, "
            f"because another session held it (this step's bound is {bound}).",
            "Nothing was changed: every revision of this downgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the downgrade again.",
        ]
    )


def _relation_exists(name: str, kind: str) -> bool:
    """Whether *name* is a relation of *kind* in the schema, from the catalog."""
    found = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :name AND c.relkind = :kind"
        ),
        {"schema": _SCHEMA, "name": name, "kind": kind},
    )
    return found.first() is not None


def _column_exists(table: str, column: str) -> bool:
    found = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table "
            "AND a.attname = :column AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"schema": _SCHEMA, "table": table, "column": column},
    )
    return found.first() is not None


def _constraint_exists(table: str, name: str) -> bool:
    found = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_constraint k "
            "JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table AND k.conname = :name"
        ),
        {"schema": _SCHEMA, "table": table, "name": name},
    )
    return found.first() is not None


def _bounded(bound: str, steps: Callable[[], None], refusal: str) -> None:
    """Run *steps* with ``lock_timeout`` at *bound*, then put it back.

    A lock timeout or a deadlock becomes *refusal*; any other error is raised
    as it was.
    """
    bind = op.get_bind()
    bind.execute(sa.text(f"SET LOCAL lock_timeout = '{bound}'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    try:
        steps()
    except sa.exc.OperationalError as exc:
        if _pgcode(exc) in _LOCK_REFUSED:
            raise RuntimeError(refusal) from None
        raise
    bind.execute(sa.text("SET LOCAL lock_timeout TO DEFAULT"))


def _create_absent_settings() -> None:
    """The side table and its index."""
    if not _relation_exists(_SETTINGS, "r"):
        op.create_table(
            _SETTINGS,
            sa.Column("feature_flag_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("environment", sa.String(length=32), nullable=False),
            sa.Column(
                "is_active",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column(
                "targeting_rules",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column(
                "rollout_percentage",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(_NAME_CHECK_SQL, name=_NAME_CHECK),
            sa.CheckConstraint(_ROLLOUT_CHECK_SQL, name=_ROLLOUT_CHECK),
            sa.ForeignKeyConstraint(
                ["feature_flag_id"], [f"{_SCHEMA}.{_FLAGS}.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("feature_flag_id", "environment", name=_UNIQUE),
            schema=_SCHEMA,
        )
    if not _relation_exists(_SETTINGS_CREATED_INDEX, "i"):
        op.create_index(
            _SETTINGS_CREATED_INDEX,
            _SETTINGS,
            ["created_at"],
            unique=False,
            schema=_SCHEMA,
        )


def _add_absent_key_environment() -> None:
    """``api_keys.environment`` and its check."""
    if not _column_exists(_KEYS, _KEY_COLUMN):
        op.add_column(
            _KEYS,
            sa.Column(
                _KEY_COLUMN,
                sa.String(length=32),
                server_default=sa.text(f"'{_KEY_DEFAULT}'"),
                nullable=False,
            ),
            schema=_SCHEMA,
        )
    if not _constraint_exists(_KEYS, _KEY_CHECK):
        op.create_check_constraint(_KEY_CHECK, _KEYS, _KEY_CHECK_SQL, schema=_SCHEMA)


def _qualified(table: str) -> str:
    preparer = op.get_bind().dialect.identifier_preparer
    return f"{preparer.quote_schema(_SCHEMA)}.{preparer.quote(table)}"


def _deactivate_non_production_keys() -> None:
    op.get_bind().execute(
        sa.text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"UPDATE {_qualified(_KEYS)} SET is_active = false "  # nosec B608 - the table name is the dialect-quoted module constant, not user input
            f"WHERE {_KEY_COLUMN} <> '{_KEY_DEFAULT}'"
        )
    )


def _drop_present_settings() -> None:
    """The side table, with its index and every row, when present."""
    if _relation_exists(_SETTINGS, "r"):
        op.drop_table(_SETTINGS, schema=_SCHEMA)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    _bounded(_FLAGS_LOCK_TIMEOUT, _create_absent_settings, _lock_refusal(_FLAGS))
    _bounded(_KEYS_LOCK_TIMEOUT, _add_absent_key_environment, _lock_refusal(_KEYS))


def downgrade() -> None:
    """Deactivates every staging and development key, then drops
    ``check_api_key_environment``, ``api_keys.environment`` and
    ``flag_environment_settings`` with every staging and development setting.
    Production flags and keys are unaffected. Does not refuse over data;
    nothing restores what it drops. A lock it cannot take refuses it, with
    nothing changed."""
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    def drop_present_key_environment() -> None:
        # Deactivate every staging and development key, then drop the check
        # and the column, each only when present. Defined inside downgrade()
        # so that test_documented_downgrade_targets.py, which reads
        # downgrade()'s body for drop_column calls, sees this one.
        if _column_exists(_KEYS, _KEY_COLUMN):
            _deactivate_non_production_keys()
        if _constraint_exists(_KEYS, _KEY_CHECK):
            op.drop_constraint(_KEY_CHECK, _KEYS, schema=_SCHEMA, type_="check")
        if _column_exists(_KEYS, _KEY_COLUMN):
            op.drop_column(_KEYS, _KEY_COLUMN, schema=_SCHEMA)

    _bounded(
        _KEYS_LOCK_TIMEOUT,
        drop_present_key_environment,
        _downgrade_lock_refusal(_KEYS),
    )
    _bounded(
        _FLAGS_LOCK_TIMEOUT,
        _drop_present_settings,
        _downgrade_lock_refusal(_FLAGS),
    )

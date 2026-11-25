"""Flag split: a nullable split on each feature flag and each flag environment setting

Downgrade refuses while any flag has a split or a rule value.

Adds, for #2012, and nothing else:

* ``feature_flags.split`` (``jsonb``, nullable): a flag's split in production;
* ``flag_environment_settings.split`` (``jsonb``, nullable): a flag's split in
  staging or development.

The same name and type as the models (``FeatureFlag.split`` and
``FlagEnvironmentSetting.split`` in ``backend/app/models/feature_flag.py``).

**Storage only, and expand only.** No default, no check and no data step: the
column is NULL on every row, and adding a nullable column with no default
rewrites no row. No request sets a split and no evaluation reads one. The
previous release names the column in no statement, so its inserts store NULL
and its updates keep what is stored. ``feature_flags.variants`` is not touched.
Each step runs only when its column is absent: a schema built by
``db/bootstrap.py``'s ``create_all`` already has both, and the transition tests
rewind such a schema's ``alembic_version`` and replay the chain from there. The
existence checks read ``pg_catalog`` only.

**Each wait is bounded.** Each ``ALTER TABLE`` takes ACCESS EXCLUSIVE on its
table, with ``SET LOCAL lock_timeout = '1s'`` (``_LOCK_TIMEOUT``) straight
before it and ``SET LOCAL lock_timeout TO DEFAULT`` straight after, so no later
revision inherits the bound. ``feature_flags`` comes first, as a reader that
finds a flag and then its setting takes them. Upgrading from 0.48.0, the same
run creates ``flag_environment_settings`` (``t2011_flag_environments``), so
only the ``feature_flags`` step can wait: the declared bound, 1 s, is this
revision's entry in ``backend/tests/unit/db/test_upgrade_lock_budget.py``. From
a database already at ``t2011_flag_environments`` both steps can wait, 1 s
each. In the run from 0.48.0 the lock ``t2011_flag_environments`` took on
``api_keys`` is held while this one waits, so new key lookups queue behind it.

A lock timeout (SQLSTATE 55P03) or a deadlock (40P01) refuses the upgrade with
``_lock_refusal()``: the bound equals PostgreSQL's default
``deadlock_timeout`` (1 s), so a session that holds one of these tables and
then waits for a lock this run holds can end it either way. ``upgrade heads``
runs every pending revision in one transaction (``env.py`` sets no
``transaction_per_migration``), so the refusal rolls back every revision of
that run: nothing is applied, and running the upgrade again is the whole
remedy.

**The downgrade drops both columns, so it refuses while one holds anything**:
while any row of either table has a split, or a rule of a string, number or
json flag carries a value of its own, in production (the flag row's rules) or
in staging or development (the setting's). The release rolled back to would
serve neither, and nothing could rebuild a dropped split. The refusal names
each flag and the environments it was found in, and changes nothing. A rule's
value stays in the stored rules and is not dropped; it refuses because the
release rolled back to ignores it. A boolean flag's rule value does not refuse.

It takes ACCESS EXCLUSIVE on ``feature_flags`` and then on
``flag_environment_settings`` before it looks, under the same bound each: a
session that is writing a split when the downgrade starts is waited for, and
what it commits is seen. The dropped columns are named in ``downgrade()``
itself, so ``test_documented_downgrade_targets.py``, which reads
``drop_column`` calls there, sees this downgrade. A lock timeout or a deadlock
refuses it with ``_downgrade_lock_refusal()``: nothing is changed, and it is
run again. A downgrade past ``t2011_flag_environments`` in the same run goes on
to that revision's locks, ``api_keys`` first, with these held.

**The rule values are found by shape** (``_HAS_RULE_VALUE``), because a
condition of any rule has a ``value`` of its own: the dashboard shape's
top-level ``value`` (beside ``groups``), and in the native shape a ``value`` on
an entry of ``rules`` or on ``default_rule``. It answers exactly where
``backend.app.core.flag_values.rule_values`` finds one;
``test_flag_split_migration.py`` compares the two over stored rules of every
shape.

**Offline mode is refused** in both directions: the upgrade reads the catalog
and the downgrade reads the flags before either changes anything, so neither
can be emitted as SQL.

No application import: a migration stays what it was the day it was written.

Core chain, extending the core head ``t2011_flag_environments`` (#2011).
Nothing here touches a module table, so a full checkout still has exactly two
heads: this revision and the modules branch's head (``modules_0005_redshift``).

Revision ID: t2012_flag_split
Revises: t2011_flag_environments
Create Date: 2026-11-24
"""

import os
from typing import Callable, Dict, List, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "t2012_flag_split"
down_revision: Union[str, None] = "t2011_flag_environments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_FLAGS = "feature_flags"
_SETTINGS = "flag_environment_settings"
_COLUMN = "split"

#: The bound on each wait, in both directions. Never "0": PostgreSQL reads 0 as
#: no timeout at all.
_LOCK_TIMEOUT = "1s"

#: What ends a wait for a lock, in every block of both directions: a lock
#: timeout (55P03) or a deadlock (40P01).
_LOCK_REFUSED = ("55P03", "40P01")

#: How many flags the downgrade's refusal names, at most.
_NAMED_FLAGS = 20

_OFFLINE_REFUSAL = (
    "t2012_flag_split reads the catalog before it adds anything, and its "
    "downgrade looks for flags that have a split or a rule value before it "
    "drops anything, so it cannot be emitted as SQL; run it online (without "
    "--sql)."
)

#: Whether the stored rules ``{rules}`` carry a rule value, as
#: ``flag_values.rule_values`` reads them. ``->`` answers SQL NULL for a key
#: that is absent and for anything but an object, and JSON ``null`` for a key
#: that holds null, so ``IS NOT NULL`` asks whether the key is there. The CASE
#: is what keeps ``jsonb_array_elements`` off anything but an array.
_HAS_RULE_VALUE = """
    CASE
      WHEN jsonb_typeof({rules}) IS DISTINCT FROM 'object' THEN false
      WHEN ({rules} -> 'groups') IS NOT NULL THEN ({rules} -> 'value') IS NOT NULL
      WHEN ({rules} -> 'rules') IS NULL THEN false
      ELSE
        ({rules} -> 'default_rule' -> 'value') IS NOT NULL
        OR EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
            CASE jsonb_typeof({rules} -> 'rules')
              WHEN 'array' THEN {rules} -> 'rules'
              ELSE CAST('[]' AS jsonb)
            END
          ) AS entry(rule)
          WHERE (entry.rule -> 'value') IS NOT NULL
        )
    END
"""


def _pgcode(exc: BaseException) -> Union[str, None]:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _lock_refusal(table: str) -> str:
    return "\n".join(
        [
            f"Refusing to upgrade schema {_SCHEMA}: t2012_flag_split could not "
            f"take the lock it needs on {_SCHEMA}.{table} to add the column "
            f"{_COLUMN}, because another session held it (this step's bound is "
            f"{_LOCK_TIMEOUT}).",
            "Nothing was applied: every revision of this upgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the upgrade again.",
        ]
    )


def _downgrade_lock_refusal(table: str) -> str:
    return "\n".join(
        [
            f"Refusing to downgrade schema {_SCHEMA}: t2012_flag_split could not "
            f"take the lock it needs on {_SCHEMA}.{table} to drop the column "
            f"{_COLUMN}, because another session held it (this step's bound is "
            f"{_LOCK_TIMEOUT}).",
            "Nothing was changed: every revision of this downgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the downgrade again.",
        ]
    )


def _in_use_refusal(flags: Dict[str, List[str]]) -> str:
    shown = [
        f"{key} ({', '.join(environments)})"
        for key, environments in list(flags.items())[:_NAMED_FLAGS]
    ]
    more = len(flags) - len(shown)
    named = ", ".join(shown) + (f" and {more} more" if more else "")
    return (
        f"Refusing to downgrade schema {_SCHEMA}: t2012_flag_split found "
        f"{len(flags)} feature flag(s) with a split or a rule value: {named}. "
        f"This downgrade drops {_FLAGS}.{_COLUMN} and {_SETTINGS}.{_COLUMN}, and "
        "the release it rolls back to serves neither a split nor a rule value. "
        "Clear them while the release that wrote them is still serving, or "
        "restore the backup taken before the upgrade to it. Nothing was changed."
    )


def _column_exists(table: str) -> bool:
    """Whether *table* has the column, from the catalog."""
    found = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table "
            "AND a.attname = :column AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"schema": _SCHEMA, "table": table, "column": _COLUMN},
    )
    return found.first() is not None


def _bounded(steps: Callable[[], None], refusal: str) -> None:
    """Run *steps* with ``lock_timeout`` at the bound, then put it back.

    A lock timeout or a deadlock becomes *refusal*; any other error is raised
    as it was.
    """
    bind = op.get_bind()
    bind.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    try:
        steps()
    except sa.exc.OperationalError as exc:
        if _pgcode(exc) in _LOCK_REFUSED:
            raise RuntimeError(refusal) from None
        raise
    bind.execute(sa.text("SET LOCAL lock_timeout TO DEFAULT"))


def _qualified(table: str) -> str:
    preparer = op.get_bind().dialect.identifier_preparer
    return f"{preparer.quote_schema(_SCHEMA)}.{preparer.quote(table)}"


def _lock(table: str) -> None:
    op.get_bind().execute(
        sa.text(f"LOCK TABLE {_qualified(table)} IN ACCESS EXCLUSIVE MODE")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    )


def _flags_in_use() -> Dict[str, List[str]]:
    """Each flag with a split or a rule value, by key: the environments it was
    found in. A column already gone holds nothing."""

    def in_use(row: str, table: str) -> str:
        split = f"{row}.{_COLUMN} IS NOT NULL" if _column_exists(table) else "false"
        rules = _HAS_RULE_VALUE.format(rules=f"{row}.targeting_rules")
        return f"({split} OR (flag.value_type <> 'boolean' AND {rules}))"

    rows = op.get_bind().execute(
        sa.text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            "SELECT flag.key AS key, 'production' AS environment "  # nosec B608 - table and column names are dialect-quoted module constants, not user input
            f"FROM {_qualified(_FLAGS)} AS flag "
            f"WHERE {in_use('flag', _FLAGS)} "
            "UNION ALL "
            "SELECT flag.key, setting.environment "
            f"FROM {_qualified(_SETTINGS)} AS setting "
            f"JOIN {_qualified(_FLAGS)} AS flag ON flag.id = setting.feature_flag_id "
            f"WHERE {in_use('setting', _SETTINGS)} "
            "ORDER BY 1, 2"
        )
    )
    found: Dict[str, List[str]] = {}
    for row in rows:
        found.setdefault(row.key, []).append(row.environment)
    return found


def _add_split(table: str) -> None:
    if _column_exists(table):
        return
    _bounded(
        lambda: op.add_column(
            table,
            sa.Column(
                _COLUMN, postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
            schema=_SCHEMA,
        ),
        _lock_refusal(table),
    )


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    _add_split(_FLAGS)
    _add_split(_SETTINGS)


def downgrade() -> None:
    """Drops ``feature_flags.split`` and ``flag_environment_settings.split``.

    Refused while any row of either has a split, or a rule of a string, number
    or json flag carries a value: the refusal names the flags and changes
    nothing. A lock it cannot take refuses it too, with nothing changed."""
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    def drop_unused() -> None:
        # Both tables are locked here, so what is read is what is dropped.
        # Defined inside downgrade() so that
        # test_documented_downgrade_targets.py, which reads downgrade()'s body
        # for drop_column calls, sees these.
        _lock(_SETTINGS)
        in_use = _flags_in_use()
        if in_use:
            raise RuntimeError(_in_use_refusal(in_use))
        for table in (_FLAGS, _SETTINGS):
            if _column_exists(table):
                op.drop_column(table, _COLUMN, schema=_SCHEMA)

    _bounded(lambda: _lock(_FLAGS), _downgrade_lock_refusal(_FLAGS))
    _bounded(drop_unused, _downgrade_lock_refusal(_SETTINGS))

"""Metric window: a nullable conversion window, in hours, on each metric

The downgrade drops the column and every window stored in it, and does not
refuse.

Adds, for #1934, and nothing else:

* ``metrics.conversion_window_hours`` (``integer``, nullable, no default): how
  many hours after a user's assignment the metric counts that user's events;
  NULL counts every event, as before;
* the check ``check_metric_conversion_window``: the column is NULL, or a whole
  number of hours from 1 to 8760 (365 days), the range of the warehouse
  analysis's ``conversion_window_hours``.

The same name, type and check as the model (``Metric.conversion_window_hours``
in ``backend/app/models/experiment.py``), so autogenerate stays empty.

**Storage only, and expand only.** No default and no data step: the column is
NULL on every row, and adding a nullable column with no default rewrites no
row, so no answer changes: nothing sets a window yet. The previous release
names the column in no statement, so its inserts store NULL and its updates
keep what is stored. Each step runs only when its object is absent: a schema
built by ``db/bootstrap.py``'s ``create_all`` already has both, and the
transition tests rewind such a schema's ``alembic_version`` and replay the
chain from there. The existence checks read ``pg_catalog`` only, before the
bound is set.

**The wait is bounded.** Adding the column takes ACCESS EXCLUSIVE on
``metrics``, and adding the check runs under the same lock, so the block has
one wait: ``SET LOCAL lock_timeout = '500ms'`` (``_LOCK_TIMEOUT``) straight
before it and ``SET LOCAL lock_timeout TO DEFAULT`` straight after, so no later
revision inherits the bound. 0.5 s is this revision's entry in
``backend/tests/unit/db/test_upgrade_lock_budget.py``. Upgrading from 0.48.0,
the same run holds the locks ``t2011_flag_environments`` took on ``api_keys``
and ``t2012_flag_split`` took on ``feature_flags`` while this one waits, so new
key lookups and flag reads queue behind it too.

A lock timeout (SQLSTATE 55P03) or a deadlock (40P01) refuses the upgrade with
``_lock_refusal()``. ``upgrade heads`` runs every pending revision in one
transaction (``env.py`` sets no ``transaction_per_migration``), so the refusal
rolls back every revision of that run: nothing is applied, and running the
upgrade again is the whole remedy.

**The downgrade drops the column, and every window with it.** It does not
refuse: undoing a release is the operator's decision, and nothing can rebuild
a dropped window. Dropping the column drops the check with it. It waits under
the same bound, and a lock timeout or a deadlock refuses it with
``_downgrade_lock_refusal()``: nothing is changed, and it is run again. The
dropped column is named in ``downgrade()`` itself, so
``test_documented_downgrade_targets.py``, which reads ``drop_column`` calls
there, sees this downgrade.

**Offline mode is refused** in both directions: each reads the catalog before
it changes anything, so neither can be emitted as SQL.

No application import: a migration stays what it was the day it was written.

Core chain, extending the core head ``t2012_flag_split`` (#2012). Nothing here
touches a module table, so a full checkout still has exactly two heads: this
revision and the modules branch's head (``modules_0005_redshift``).

Revision ID: t1934_metric_window
Revises: t2012_flag_split
Create Date: 2026-11-26
"""

import os
from typing import Callable, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t1934_metric_window"
down_revision: Union[str, None] = "t2012_flag_split"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "metrics"
_COLUMN = "conversion_window_hours"
_CHECK = "check_metric_conversion_window"
#: The check, as the model writes it. 8760 hours is 365 days.
_CHECK_CONDITION = (
    "conversion_window_hours IS NULL OR conversion_window_hours BETWEEN 1 AND 8760"
)

#: The bound on the wait, in both directions. Never "0": PostgreSQL reads 0 as
#: no timeout at all.
_LOCK_TIMEOUT = "500ms"

#: What ends a wait for a lock, in both directions: a lock timeout (55P03) or a
#: deadlock (40P01).
_LOCK_REFUSED = ("55P03", "40P01")

_OFFLINE_REFUSAL = (
    "t1934_metric_window reads the catalog before it adds or drops anything, so "
    "it cannot be emitted as SQL; run it online (without --sql)."
)


def _pgcode(exc: BaseException) -> Union[str, None]:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _lock_refusal() -> str:
    return "\n".join(
        [
            f"Refusing to upgrade schema {_SCHEMA}: t1934_metric_window could not "
            f"take the lock it needs on {_SCHEMA}.{_TABLE} to add the column "
            f"{_COLUMN}, because another session held it (this step's bound is "
            f"{_LOCK_TIMEOUT}).",
            "Nothing was applied: every revision of this upgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the upgrade again.",
        ]
    )


def _downgrade_lock_refusal() -> str:
    return "\n".join(
        [
            f"Refusing to downgrade schema {_SCHEMA}: t1934_metric_window could "
            f"not take the lock it needs on {_SCHEMA}.{_TABLE} to drop the column "
            f"{_COLUMN}, because another session held it (this step's bound is "
            f"{_LOCK_TIMEOUT}).",
            "Nothing was changed: every revision of this downgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the downgrade again.",
        ]
    )


def _column_exists() -> bool:
    """Whether ``metrics`` has the column, from the catalog."""
    found = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table "
            "AND a.attname = :column AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"schema": _SCHEMA, "table": _TABLE, "column": _COLUMN},
    )
    return found.first() is not None


def _check_exists() -> bool:
    """Whether ``metrics`` has the check, from the catalog."""
    found = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_constraint k "
            "JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table "
            "AND k.conname = :check AND k.contype = 'c'"
        ),
        {"schema": _SCHEMA, "table": _TABLE, "check": _CHECK},
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


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    add_column = not _column_exists()
    add_check = not _check_exists()
    if not (add_column or add_check):
        return

    def add() -> None:
        if add_column:
            op.add_column(
                _TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True), schema=_SCHEMA
            )
        if add_check:
            op.create_check_constraint(_CHECK, _TABLE, _CHECK_CONDITION, schema=_SCHEMA)

    _bounded(add, _lock_refusal())


def downgrade() -> None:
    """Drops ``metrics.conversion_window_hours``, every metric's conversion
    window, and with it ``check_metric_conversion_window``. Does not refuse
    over data. A lock it cannot take refuses it, with nothing changed."""
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    if not _column_exists():
        return
    _bounded(
        lambda: op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA),
        _downgrade_lock_refusal(),
    )

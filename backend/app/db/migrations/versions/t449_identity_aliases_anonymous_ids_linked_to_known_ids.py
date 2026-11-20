"""Identity aliases: the table that links an anonymous id to a known id

Adds the table ``identity_aliases`` (#449), and nothing else:

* ``anonymous_id`` (``varchar(255)``, the primary key): an anonymous id is
  linked at most once;
* ``user_id`` (``varchar(255)``, NOT NULL): the known id it is linked to;
* ``created_at`` (``timestamptz``, NOT NULL DEFAULT ``now()``);
* ``check_identity_aliases_ids_differ``: ``CHECK (anonymous_id <> user_id)``;
* ``ix_identity_aliases_user_id`` on ``(user_id)``.

The same names, types and defaults as the model
(``backend/app/models/identity_alias.py``).

**Expand only, and no lock on an existing table.** No foreign key, no ALTER,
no index on an existing table and no backfill: the table starts empty, and the
previous release never reads it, so it starts against this schema and serves
as before. Each step runs only when its object is absent: a schema built by
``db/bootstrap.py``'s ``create_all`` already has both, and the transition
tests rewind such a schema's ``alembic_version`` and replay the chain from
there. The existence checks read ``pg_catalog`` only.

**The one wait is bounded.** A ``CREATE TABLE`` that references no existing
table takes no lock an SDK request holds, but it still waits behind another
session's uncommitted creation of a table of the same name, or an uncommitted
``DROP SCHEMA``. So the revision opens with ``SET LOCAL lock_timeout =
'250ms'`` (``_LOCK_TIMEOUT``; never 0, which PostgreSQL reads as no timeout)
and ends with ``SET LOCAL lock_timeout TO DEFAULT``, so no later revision
inherits it. A lock timeout (SQLSTATE 55P03) refuses the upgrade with
``_lock_refusal()``. ``upgrade heads`` runs every pending revision in one
transaction (``env.py`` sets no ``transaction_per_migration``), so the refusal
rolls back every revision of that run: nothing is applied, and running the
upgrade again is the whole remedy. The declared bound, 0.25 s, is this
revision's entry in ``backend/tests/unit/db/test_upgrade_lock_budget.py``.

**Offline mode is refused** in both directions: the upgrade reads the catalog
before it creates anything, so it cannot be emitted as SQL.

**The downgrade drops the table, and with it every link.** Assignments and
events are untouched. It does not refuse, and nothing restores the links: an
upgrade afterwards creates the table empty, and the links must be sent again.
A downgrade to ``t1812_user_totp`` or below runs it: the self-hosting
migrations page, the rollback runbook and the Database Migration workflow say
so where they name such a target. ``test_documented_downgrade_targets.py``
reads only ``drop_column`` calls, so
``test_identity_aliases_downgrade_docs.py`` holds that by this revision's id.

No application import: a migration stays what it was the day it was written.

Core chain, extending the core head ``t1812_user_totp`` (#1812). Nothing here
touches a module table, so a full checkout still has exactly two heads: this
revision and the modules branch's head (``modules_0005_redshift``).

Revision ID: t449_identity_aliases
Revises: t1812_user_totp
Create Date: 2026-11-19
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t449_identity_aliases"
down_revision: Union[str, None] = "t1812_user_totp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "identity_aliases"
_CHECK = "check_identity_aliases_ids_differ"
_CHECK_SQL = "anonymous_id <> user_id"
_INDEX = "ix_identity_aliases_user_id"

#: The bound on this revision's one wait. Never "0": PostgreSQL reads 0 as no
#: timeout at all.
_LOCK_TIMEOUT = "250ms"

_LOCK_NOT_AVAILABLE = "55P03"

_OFFLINE_REFUSAL = (
    "t449_identity_aliases reads the catalog before it creates anything, so it "
    "cannot be emitted as SQL; run it online (without --sql)."
)


def _pgcode(exc: BaseException) -> Union[str, None]:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _lock_refusal() -> str:
    return "\n".join(
        [
            f"Refusing to upgrade schema {_SCHEMA}: t449_identity_aliases waited "
            f"longer than {_LOCK_TIMEOUT} for a lock it needs to create "
            f"{_SCHEMA}.{_TABLE} (another session is creating a table of that "
            "name, or dropping the schema, and has not committed).",
            "Nothing was applied: every revision of this upgrade was rolled "
            "back, and the database is still at the revisions it had before. "
            "Run the upgrade again.",
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


def _create_absent_objects() -> None:
    if not _relation_exists(_TABLE, "r"):
        op.create_table(
            _TABLE,
            sa.Column("anonymous_id", sa.String(length=255), nullable=False),
            sa.Column("user_id", sa.String(length=255), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(_CHECK_SQL, name=_CHECK),
            sa.PrimaryKeyConstraint("anonymous_id"),
            schema=_SCHEMA,
        )
    if not _relation_exists(_INDEX, "i"):
        op.create_index(_INDEX, _TABLE, ["user_id"], unique=False, schema=_SCHEMA)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    bind = op.get_bind()
    bind.execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    try:
        _create_absent_objects()
    except sa.exc.OperationalError as exc:
        if _pgcode(exc) == _LOCK_NOT_AVAILABLE:
            raise RuntimeError(_lock_refusal()) from None
        raise
    bind.execute(sa.text("SET LOCAL lock_timeout TO DEFAULT"))


def downgrade() -> None:
    """Drops identity_aliases: every link between an anonymous id and a known
    id. Assignments and events are untouched. Does not refuse; nothing can
    restore the links, and an upgrade afterwards creates the table empty."""
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    op.drop_index(_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)

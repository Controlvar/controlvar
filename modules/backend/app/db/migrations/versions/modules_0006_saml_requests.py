"""The modules branch, sixth revision: the SAML sign-ins in progress (#123).

Adds the table ``sso_saml_requests``, and nothing else. One row per SAML
sign-in the API has started and not finished:

* ``id`` (``varchar(64)``, the primary key): the id of the request sent to the
  identity provider;
* ``config_id`` (``uuid``, NOT NULL): the single sign-on configuration the
  sign-in started from; **no foreign key**;
* ``return_to`` (``varchar(512)``, NOT NULL): where the sign-in returns;
* ``handoff_hash`` (``char(43)``, NOT NULL): the hash of the hand-off secret
  the starting browser holds;
* ``created_at`` (``timestamptz``, NOT NULL DEFAULT ``now()``);
* ``expires_at`` (``timestamptz``, NOT NULL), set by the application;
* ``used_at`` (``timestamptz``, NULL): set once, when the sign-in completes;
* ``ix_sso_saml_requests_expires_at`` on ``(expires_at)``.

The same names, types and defaults as the model
(``modules/backend/app/models/sso_saml_request.py``). Nothing reads or writes
the table yet: it is the storage SAML sign-in will use, added on its own so
that the change which first writes it needs no migration.

**Expand only, and no lock on an existing table.** No foreign key, no ALTER,
no index on an existing table and no backfill: the table starts empty and the
previous release never reads it, so it starts against this schema and serves
as before. Each step runs only when its object is absent: a schema built by
``db/bootstrap.py``'s ``create_all`` already has both, and the transition
tests rewind such a schema's ``alembic_version`` and replay the branch from
there. The existence checks read ``pg_catalog`` only.

**The revision marks what it creates.** When it creates the table it writes a
COMMENT on the table's primary key (:data:`_CREATED_BY`), as
``modules_0002_warehouse_analysis`` does. ``alembic upgrade heads`` that
reaches the heads also creates any module table the models declare and the
schema lacks, untagged; the COMMENT is how a test tells the two apart.

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
before it creates anything, so it cannot be emitted as SQL, and the downgrade,
which could be, is refused too (``_OFFLINE_DOWNGRADE_REFUSAL``), so the
revision is applied and undone the same way, online.

**The downgrade drops the table**, on every database, however it was made: the
sign-ins in progress are lost and must be started again. No other table
changes. ``alembic downgrade modules_0005_redshift`` runs it alone.

No application import: a migration stays what it was the day it was written.

Modules branch, extending ``modules_0005_redshift`` (written by hand for
``revision --head modules@head``). Nothing here touches a core table, so a
full checkout still has exactly two heads: this revision and the core head.

Revision ID: modules_0006_saml_requests
Revises: modules_0005_redshift
Create Date: 2026-11-27
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "modules_0006_saml_requests"
down_revision: Union[str, None] = "modules_0005_redshift"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "sso_saml_requests"
#: PostgreSQL's own name for the table's primary key, which the model's
#: ``create_all`` also gives it.
_PRIMARY_KEY = "sso_saml_requests_pkey"
_INDEX = "ix_sso_saml_requests_expires_at"

#: COMMENT written on the primary key when this revision creates the table.
_CREATED_BY = "created by alembic revision modules_0006_saml_requests"

#: The bound on this revision's one wait. Never "0": PostgreSQL reads 0 as no
#: timeout at all.
_LOCK_TIMEOUT = "250ms"

_LOCK_NOT_AVAILABLE = "55P03"

_OFFLINE_REFUSAL = (
    "modules_0006_saml_requests reads the catalog before it creates anything, "
    "so it cannot be emitted as SQL; run it online (without --sql)."
)
_OFFLINE_DOWNGRADE_REFUSAL = (
    "modules_0006_saml_requests is undone online, as it is applied: its upgrade "
    "cannot be emitted as SQL, and its downgrade is refused as SQL to match; "
    "run the downgrade online (without --sql)."
)


def _pgcode(exc: BaseException) -> Union[str, None]:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _lock_refusal() -> str:
    return "\n".join(
        [
            f"Refusing to upgrade schema {_SCHEMA}: modules_0006_saml_requests "
            f"waited longer than {_LOCK_TIMEOUT} for a lock it needs to create "
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
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("config_id", UUID(as_uuid=True), nullable=False),
            sa.Column("return_to", sa.String(length=512), nullable=False),
            sa.Column("handoff_hash", sa.CHAR(length=43), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name=_PRIMARY_KEY),
            schema=_SCHEMA,
        )
        # _SCHEMA comes from the deployment's environment; the other names
        # and the comment are module constants.
        op.execute(
            f'COMMENT ON CONSTRAINT "{_PRIMARY_KEY}" ON "{_SCHEMA}"."{_TABLE}" '
            f"IS '{_CREATED_BY}'"
        )
    if not _relation_exists(_INDEX, "i"):
        op.create_index(_INDEX, _TABLE, ["expires_at"], unique=False, schema=_SCHEMA)


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
    """Drops sso_saml_requests: the SAML sign-ins in progress, which must be
    started again. No other table changes. Does not refuse."""
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_DOWNGRADE_REFUSAL)
    op.drop_table(_TABLE, schema=_SCHEMA)

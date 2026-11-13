"""Two-step sign-in: each local account's authenticator secret and recovery codes

Adds the table ``user_totp`` (#1812): one row per account that has started
setting two-step sign-in up, keyed by ``user_id`` (the primary key, referencing
``users.id`` ON DELETE CASCADE), with

* ``secret_enc`` (``text``, NOT NULL): the authenticator secret, sealed with a
  key derived from ``SECRET_KEY``;
* ``confirmed_at`` (``timestamptz``, nullable): NULL until a first code proves
  the account's app has the secret;
* ``last_used_step`` (``bigint``, NOT NULL DEFAULT 0): the newest 30-second
  step a code was accepted for;
* ``failed_attempts`` (``integer``, NOT NULL DEFAULT 0): wrong codes since the
  last accepted one;
* ``recovery_hashes`` (``jsonb``, NOT NULL DEFAULT ``'[]'``): a bcrypt hash per
  unused recovery code;
* ``created_at`` (``timestamptz``, NOT NULL DEFAULT ``now()``).

The same columns, types and defaults as the model
(``backend/app/models/user_totp.py``).

Pure expand: a new table, and ``users`` is not touched, so the previous
release starts against this schema and signs everyone in as before. No data
step.

The table is created only when absent, as ``4955571d232c`` does: a schema
built by ``db/bootstrap.py``'s ``create_all`` already has it, and the
transition tests rewind such a schema's ``alembic_version`` and replay the
chain from there.

**The downgrade drops the table, and with it every account's two-step sign-in
secret, recovery codes and wrong-code count.** It does not refuse, and nothing
can restore them: those accounts then sign in with the password alone and must
set two-step sign-in up again. It drops ``secret_enc`` and ``recovery_hashes``
by name before the table, so ``test_documented_downgrade_targets.py`` (which
reads ``drop_column`` calls) sees this downgrade. A downgrade to
``4955571d232c`` or below runs it: the self-hosting migrations page, the
rollback runbook and the Database Migration workflow say so where they name
such a target.

Core chain, extending the core head ``4955571d232c`` (#1545). Nothing here
touches a module table, so a full checkout still has exactly two heads: this
revision and ``modules_0004_rbac_activation``.

Revision ID: t1812_user_totp
Revises: 4955571d232c
Create Date: 2026-11-13
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "t1812_user_totp"
down_revision: Union[str, None] = "4955571d232c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "user_totp"


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE, schema=_SCHEMA)


def upgrade() -> None:
    if _has_table():
        return
    op.create_table(
        _TABLE,
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_used_step",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "failed_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "recovery_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{_SCHEMA}.users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    """Drops user_totp: every account's two-step sign-in secret, recovery
    codes and wrong-code count. Does not refuse; nothing can restore them.

    The two columns that hold the secrets are dropped by name first, so the
    downgrade-documentation test (which reads ``drop_column`` calls) sees
    this revision; the table goes with the rest."""
    op.drop_column(_TABLE, "recovery_hashes", schema=_SCHEMA)
    op.drop_column(_TABLE, "secret_enc", schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)

"""Visual experiments: page changes, pages and goals, and the editor's tokens

Adds what a visual experiment stores (#1545):

* ``experiments.visual_config`` (``jsonb``, nullable): the pages a visual
  experiment runs on, the page its editor opens, and its goals;
* ``variants.visual_changes`` (``jsonb``, nullable): a variant's page changes;
* the table ``visual_tokens``: one row per editor ticket, editor session,
  editor claim or preview link, by the SHA-256 of its token (``token_hash``,
  unique), with ``experiment_id`` referencing ``experiments.id`` ON DELETE
  CASCADE, ``user_id`` referencing ``users.id`` ON DELETE CASCADE and
  ``parent_id`` referencing ``visual_tokens.id`` ON DELETE CASCADE; the checks
  ``ck_visual_tokens_kind`` (``kind`` one of ``editor_ticket``,
  ``editor_session``, ``editor_claim``, ``preview``) and
  ``ck_visual_tokens_revoked_reason`` (null, or one of ``reopened``,
  ``started``, ``ticket_reused``, ``signed_out``, ``scheduled``); and the
  indexes ``ix_visual_tokens_experiment_id`` and ``ix_visual_tokens_parent_id``.

The checks admit every kind and reason the design names, so a later release
that writes ``editor_claim`` or ``scheduled`` needs no migration. They have the
same names and SQL as the model (``backend/app/models/visual_token.py``).

Pure expand: both columns are nullable with no default and the table is new,
so the previous release, which never reads or writes any of them, starts
against this schema and inserts experiments and variants as before. No data
step: no row is changed.

Each step runs only when its object is absent, as ``806901fb7735`` does: a
schema built by ``db/bootstrap.py``'s ``create_all`` already has them (they are
on the models), and the transition tests rewind such a schema's
``alembic_version`` and replay the chain from there.

**The downgrade drops the table and both columns, and with them every visual
experiment's pages, goals and page changes, and every editor ticket, session
and preview link.** It does not refuse, and nothing can restore them. The
experiments stay, as ordinary experiments with no page changes. A downgrade
to ``c448_balance_within`` or below runs it: the self-hosting migrations page,
the rollback runbook and the Database Migration workflow say so where they
name such a target.

Core chain, extending the core head ``c448_balance_within`` (#448). Written
for ``revision --head 7b6b4b4fe246`` and re-pointed onto
``c448_balance_within`` when #448's first migration reached main before it.
Nothing here touches a module table, so a full checkout still has exactly two
heads: this revision and ``modules_0004_rbac_activation``.

Revision ID: 4955571d232c
Revises: c448_balance_within
Create Date: 2026-11-06
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4955571d232c"
down_revision: Union[str, None] = "c448_balance_within"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_EXPERIMENTS = "experiments"
_VARIANTS = "variants"
_TOKENS = "visual_tokens"
_CONFIG = "visual_config"
_CHANGES = "visual_changes"

_KIND_CHECK = "ck_visual_tokens_kind"
_KIND_CHECK_SQL = (
    "kind IN ('editor_ticket', 'editor_session', 'editor_claim', 'preview')"
)
_REASON_CHECK = "ck_visual_tokens_revoked_reason"
_REASON_CHECK_SQL = (
    "revoked_reason IS NULL OR revoked_reason IN "
    "('reopened', 'started', 'ticket_reused', 'signed_out', 'scheduled')"
)
_EXPERIMENT_INDEX = "ix_visual_tokens_experiment_id"
_PARENT_INDEX = "ix_visual_tokens_parent_id"


def _has_column(table: str, column: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table, schema=_SCHEMA)
    return any(found["name"] == column for found in columns)


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TOKENS, schema=_SCHEMA)


def _indexes() -> set:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(_TOKENS, schema=_SCHEMA)
    }


def upgrade() -> None:
    if not _has_column(_EXPERIMENTS, _CONFIG):
        op.add_column(
            _EXPERIMENTS,
            sa.Column(_CONFIG, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            schema=_SCHEMA,
        )
    if not _has_column(_VARIANTS, _CHANGES):
        op.add_column(
            _VARIANTS,
            sa.Column(_CHANGES, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            schema=_SCHEMA,
        )
    if not _has_table():
        op.create_table(
            _TOKENS,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("variant_name", sa.String(length=100), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_reason", sa.String(length=20), nullable=True),
            sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.CheckConstraint(_KIND_CHECK_SQL, name=_KIND_CHECK),
            sa.CheckConstraint(_REASON_CHECK_SQL, name=_REASON_CHECK),
            sa.ForeignKeyConstraint(
                ["experiment_id"],
                [f"{_SCHEMA}.{_EXPERIMENTS}.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], [f"{_SCHEMA}.users.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["parent_id"], [f"{_SCHEMA}.{_TOKENS}.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash"),
            schema=_SCHEMA,
        )
    indexes = _indexes()
    if _EXPERIMENT_INDEX not in indexes:
        op.create_index(
            _EXPERIMENT_INDEX, _TOKENS, ["experiment_id"], unique=False, schema=_SCHEMA
        )
    if _PARENT_INDEX not in indexes:
        op.create_index(
            _PARENT_INDEX, _TOKENS, ["parent_id"], unique=False, schema=_SCHEMA
        )


def downgrade() -> None:
    """Drops visual_tokens (every editor ticket, session and preview link),
    variants.visual_changes (every page change) and
    experiments.visual_config (every visual experiment's pages and goals).
    Does not refuse; nothing can restore them."""
    op.drop_table(_TOKENS, schema=_SCHEMA)
    op.drop_column(_VARIANTS, _CHANGES, schema=_SCHEMA)
    op.drop_column(_EXPERIMENTS, _CONFIG, schema=_SCHEMA)

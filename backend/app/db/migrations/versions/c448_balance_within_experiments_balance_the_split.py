"""Experiments: balance the split within groups as users arrive

c448_balance_within adds the balance setting, each user's group and place in
it, and the per-group counters. Its downgrade drops every experiment's balance
setting, every user's group and every counter; they cannot be recovered. Run
it only with a human go at that moment.

Expand only (#448). Five steps, each only when its object is absent:

1. ``experiments.balance_within`` (``jsonb``, nullable): the attribute and the
   values an experiment balances its split within;
2. ``experiments.balance_seed`` (``varchar(64)``, nullable): the key of the
   experiment's block order, made by the server when ``balance_within`` is
   first set;
3. ``assignments.balance_group`` (``varchar(100)``, nullable): the group a
   user was balanced in;
4. ``assignments.balance_seq`` (``bigint``, nullable): the user's place in
   their group (NULL for a user assigned outside the blocks);
5. the table ``balance_group_counters``: ``experiment_id`` (``uuid``, a
   foreign key to ``experiments`` ON DELETE CASCADE), ``balance_group``
   (``varchar(100)``), ``next_seq`` (``bigint`` NOT NULL), primary key
   ``(experiment_id, balance_group)``.

No backfill and nothing dropped: every column is nullable with no default, so
adding one rewrites no row, and every existing experiment balances nothing.

The previous release runs against this schema unchanged: its models do not map
the columns or the table, so its INSERTs leave the columns NULL and its
SELECTs do not name them. An experiment that balances is assigned by it with
the hash, with a NULL group, for as long as it serves.

A schema built by ``db/bootstrap.py``'s ``create_all`` already has every
object (they are on the models), and the transition tests rewind such a
schema's ``alembic_version`` and replay the chain from there, so each step is
skipped when its object exists. The upgrade reads the catalog, so it cannot be
emitted as SQL (``--sql``).

``test_balance_within_migration.py`` pins that an upgraded database and a
bootstrapped one end with the same columns and table, that autogenerate is
empty on the upgraded database, that the previous release's code assigns users
against this schema, and the downgrade.

Core chain, extending the core head ``7b6b4b4fe246`` (#1528). Nothing here
touches a module table, so a full checkout still has exactly two heads: this
revision and the modules branch's head.

Revision ID: c448_balance_within
Revises: 7b6b4b4fe246
Create Date: 2026-11-06
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c448_balance_within"
down_revision: Union[str, None] = "7b6b4b4fe246"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_COUNTERS = "balance_group_counters"

#: (table, column, type) for each column this revision adds, in order.
_COLUMNS = (
    ("experiments", "balance_within", postgresql.JSONB(astext_type=sa.Text())),
    ("experiments", "balance_seed", sa.String(length=64)),
    ("assignments", "balance_group", sa.String(length=100)),
    ("assignments", "balance_seq", sa.BigInteger()),
)

_OFFLINE_REFUSAL = (
    "c448_balance_within reads the catalog before it adds anything, so it "
    "cannot be emitted as SQL; run it online (without --sql)."
)


def _has_column(table: str, column: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table, schema=_SCHEMA)
    return any(found["name"] == column for found in columns)


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=_SCHEMA)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    for table, column, type_ in _COLUMNS:
        if not _has_column(table, column):
            op.add_column(
                table, sa.Column(column, type_, nullable=True), schema=_SCHEMA
            )
    if not _has_table(_COUNTERS):
        op.create_table(
            _COUNTERS,
            sa.Column("experiment_id", sa.UUID(), nullable=False),
            sa.Column("balance_group", sa.String(length=100), nullable=False),
            sa.Column("next_seq", sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint(
                ["experiment_id"],
                [f"{_SCHEMA}.experiments.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("experiment_id", "balance_group"),
            schema=_SCHEMA,
        )


def downgrade() -> None:
    """Drops ``balance_group_counters`` and the four columns.

    Every experiment's balance setting and block-order key, every user's group
    and place in it, and every group's counter are lost; nothing can recover
    them. It does not refuse. An upgrade afterwards starts every experiment
    with no balance setting.
    """
    op.drop_table(_COUNTERS, schema=_SCHEMA)
    for table, column, _type in reversed(_COLUMNS):
        op.drop_column(table, column, schema=_SCHEMA)

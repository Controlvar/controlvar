"""Events: an event_id is stored once per user, experiment, flag and event name

Adds the table ``event_dedup_keys`` (#1543):

* ``user_id`` (``varchar(255)``, the type of ``events.user_id``);
* ``dedup_key`` (``varchar(64)``): ``backend/app/services/event_dedup.py``'s
  SHA-256 hex over the event's experiment, flag, event name and
  ``event_id``;
* ``stored_event_id`` (``uuid``): the ``events.id`` that claimed the key;
* ``created_at`` (``timestamptz``, default ``now()``);
* ``PRIMARY KEY (user_id, dedup_key)``, and no other index.

``EventService.track_event_once`` claims a key with ``INSERT ... ON CONFLICT
DO NOTHING RETURNING`` in the same transaction as the event's insert, so an
event sent again with the same ``event_id`` is stored once.

**No foreign key to ``events``.** ``CREATE TABLE ... REFERENCES events`` takes
a lock on ``events`` and waits behind every open transaction writing to it;
without one, this revision takes no lock on ``events`` at all and completes
while the previous release keeps writing events
(``test_event_dedup_keys_migration.py`` pins that). Nothing here changes
``events``.

Pure expand: the previous release never reads or writes the table, so it
starts against this schema and stores events as before (an event it stores
during the deploy window is not deduplicated). Downgrade drops the table and
the keys in it; an upgrade afterwards starts from no keys.

The table is created only when it is absent, as ``8fd44fb483a2`` does: a
schema built by ``db/bootstrap.py``'s ``create_all`` already has it (it is on
the model), and the transition tests rewind such a schema's
``alembic_version`` and replay the chain from there.

Core chain, extending the core head ``607c51a59bf5`` (generated with
``revision --autogenerate --head 607c51a59bf5``).  Nothing here touches a
module table, so a full checkout still has exactly two heads: this revision
and ``modules_0004_rbac_activation``.

Revision ID: 85dd4796bb8b
Revises: 607c51a59bf5
Create Date: 2026-11-02
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "85dd4796bb8b"
down_revision: Union[str, None] = "607c51a59bf5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "event_dedup_keys"


def _exists() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE, schema=_SCHEMA)


def upgrade() -> None:
    if _exists():
        return
    op.create_table(
        _TABLE,
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("stored_event_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "dedup_key"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    """Drops the table, and with it every key: nothing is deduplicated until
    an upgrade creates it again."""
    op.drop_table(_TABLE, schema=_SCHEMA)

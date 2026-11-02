"""Rollout stages: record when each stage started

Downgrade drops every recorded start, and nothing restores the exact values.

Adds ``rollout_stages.started_at`` (``timestamp without time zone``, nullable,
no default), the type ``RolloutStage.started_at`` declares (``UTCDateTime``,
whose database type is a plain ``DateTime``).  The scheduler and
``POST /api/v1/rollout-schedules/stages/{stage_id}/advance`` write it once,
when a stage goes from pending to in progress, and the scheduler measures a
time-based stage's duration and the schedule's ``min_stage_duration`` from it.
Before this revision the scheduler measured from ``updated_at``, which every
edit of the stage moves, so a rename restarted a stage's clock (#1528).

Two steps, each only when it has something to do:

1. add the column, when it is absent;
2. set ``started_at = updated_at`` on every ``IN_PROGRESS`` stage whose
   ``started_at`` is NULL: exactly the instant the scheduler measured from
   before this revision, so the upgrade moves no stage's completion time.
   Pending stages have not started, and a stage that completed or failed
   before the upgrade has no recorded start; both stay NULL.  The status enum
   is stored by its member NAME, which is why the literal is upper case.

A schema built by ``db/bootstrap.py``'s ``create_all`` already has the column
(it is on the model), and the transition tests rewind such a schema's
``alembic_version`` and replay the chain from there: step 1 is skipped and step
2 sets only rows that are still NULL.  The upgrade reads the catalog, so it
cannot be emitted as SQL (``--sql``).

The previous release runs against this schema unchanged: its model does not
map the column, so its INSERTs leave it NULL and its SELECTs do not name it.  A
stage it starts during a rolling deploy has a NULL ``started_at``; this
release then measures from that stage's ``updated_at``, as before.

**The downgrade drops the column and does not refuse.**  Every recorded start
is lost.  A later upgrade sets each in-progress stage's start from its
``updated_at`` again, which an edit made in the meantime may have moved.

``test_rollout_stage_started_at_migration.py`` pins that an upgraded database
and a bootstrapped one end with the same column, the backfill, that autogenerate
is empty on the upgraded database, that the previous release's code creates,
starts and advances stages against this schema, and the downgrade.

Core chain, extending the core head ``607c51a59bf5`` (created with
``revision --head 607c51a59bf5``).  Nothing here touches a module table, so a
full checkout still has exactly two heads: this revision and the modules
branch's head.

Revision ID: 7b6b4b4fe246
Revises: 607c51a59bf5
Create Date: 2026-11-02
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b6b4b4fe246"
down_revision: Union[str, None] = "607c51a59bf5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "rollout_stages"
_COLUMN = "started_at"

_OFFLINE_REFUSAL = (
    "7b6b4b4fe246 reads the catalog before it adds anything, so it cannot be "
    "emitted as SQL; run it online (without --sql)."
)


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_TABLE, schema=_SCHEMA)
    return any(column["name"] == _COLUMN for column in columns)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.DateTime(), nullable=True),
            schema=_SCHEMA,
        )
    # Built with SQLAlchemy Core rather than as SQL text: the table and the
    # schema are identifiers, never string-formatted into a statement.
    stages = sa.table(
        _TABLE,
        sa.column(_COLUMN),
        sa.column("updated_at"),
        sa.column("status"),
        schema=_SCHEMA,
    )
    op.execute(
        stages.update()
        .where(stages.c.status == "IN_PROGRESS", stages.c.started_at.is_(None))
        .values(started_at=stages.c.updated_at)
    )


def downgrade() -> None:
    """Drops ``rollout_stages.started_at``: every stage's recorded start.

    It does not refuse.  Nothing restores the exact values: a later upgrade
    sets each in-progress stage's start from its ``updated_at``, which an edit
    may have moved since the stage started.
    """
    op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA)

"""The modules branch, third revision: a direction for each warehouse metric source (#1386).

Adds ``warehouse_sources.lower_is_better`` (``boolean``, ``NOT NULL``,
``DEFAULT false``): whether a decrease is the improvement for that source's
metric.  The source API sets it and the analysis run path reads it (#1386);
this revision only stores it.

``ADD COLUMN ... NOT NULL DEFAULT false`` fills every existing row with false
in the same statement (PostgreSQL 11 and later store the default in the
catalogue rather than rewriting the table), which is how every warehouse
analysis has read a metric so far: higher is better.  There is no separate
backfill.

The server default is load-bearing beyond that first fill.  The previous
release's image does not know the column, so its INSERTs do not name it;
during a deploy, and after an image-only rollback, the database supplies
false.  Without the default those INSERTs fail on ``NOT NULL``.

The column is added only when it is absent, as core ``a89544fb1075`` does: a
schema built by ``db/bootstrap.py``'s ``create_all`` already has it (it is on
the model), and the transition tests rewind such a schema's
``alembic_version`` and replay the chain from there.

Downgrade drops the column on every database, however it was made -- unlike
``modules_0001_rbac`` and ``modules_0002_warehouse_analysis``, which drop only
the objects their own upgrade created.  The previous release's model has no
such column, so a database that kept it would differ from that release's
models.  Dropping it deletes every source's setting; an upgrade afterwards
sets each back to false, not to what was stored.

``backend/tests/integration/database/test_modules_0003_transitions.py`` pins
the upgrade from the previous release's database, the server default on a
migrated and a bootstrapped database, and the downgrade.

Modules branch, extending ``modules_0002_warehouse_analysis`` (written by hand
for ``revision --head modules@head``).  Nothing here touches a core table, so a
full checkout still has exactly two heads: this revision and the core head.

Revision ID: modules_0003_lower_is_better
Revises: modules_0002_warehouse_analysis
Create Date: 2026-11-01
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "modules_0003_lower_is_better"
down_revision: Union[str, None] = "modules_0002_warehouse_analysis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "warehouse_sources"
_COLUMN = "lower_is_better"


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_TABLE, schema=_SCHEMA)
    return any(column["name"] == _COLUMN for column in columns)


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            schema=_SCHEMA,
        )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN, schema=_SCHEMA)

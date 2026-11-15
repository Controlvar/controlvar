"""The modules branch, fifth revision: Amazon Redshift connections (#826).

Downgrade refuses while any Amazon Redshift connection is stored.

Widens three CHECK constraints of ``warehouse_connections`` so a connection
may name ``redshift``, a connection type that, like ``athena``, authenticates
by assuming a role in the customer's AWS account with an external id the
platform generates, and stores no secret:

* ``ck_warehouse_connections_type`` accepts ``redshift`` beside
  ``bigquery``, ``snowflake`` and ``athena``;
* ``ck_warehouse_connections_external_id``: a row has an external id exactly
  when it is an ``athena`` or a ``redshift`` connection;
* ``ck_warehouse_connections_athena_no_secret`` (the name is kept): an
  ``athena`` or a ``redshift`` row stores no credential and no pending one.

``ck_warehouse_connections_byte_cap_type`` is not touched: a ``redshift`` row
has no per-query byte cap, as a ``snowflake`` row has none.  No column, index
or row changes, and the parameters stay JSONB, so a Redshift connection's
workgroup or cluster needs no column.

Every row stored before the upgrade still meets each widened constraint, and
every row the previous release writes (``bigquery``, ``snowflake`` or
``athena``) is still accepted: the previous release keeps working against the
upgraded schema.  ``backend/tests/integration/database/test_modules_0005_transitions.py``
pins both, on a migrated and on a bootstrapped database.

Each constraint is dropped and created again with its new text, in the
revision's one transaction.  A schema built by ``db/bootstrap.py``'s
``create_all`` already has the new text (it is on the model); replaying this
revision on it, as the transition tests do, gives the same constraints.

**Downgrade** puts the three constraints back as the previous release's
model has them.  It refuses while any ``redshift`` row is stored, naming how
many and their ids, and changes nothing: the narrowed constraints could not
hold such a row, and dropping it would lose a connection and its sources.
Delete those connections first, or restore the backup taken before the
upgrade.  The downgrade counts rows, so it cannot be emitted as SQL
(``--sql``); the upgrade can.

Modules branch, extending ``modules_0004_rbac_activation`` (written by hand
for ``revision --head modules@head``).  Nothing here touches a core table, so
a full checkout still has exactly two heads: this revision and the core head.

Revision ID: modules_0005_redshift
Revises: modules_0004_rbac_activation
Create Date: 2026-11-13
"""

import os
from typing import List, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "modules_0005_redshift"
down_revision: Union[str, None] = "modules_0004_rbac_activation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "warehouse_connections"

#: Each constraint this revision changes: its name, the text it has after the
#: upgrade, and the text the previous release's model gives it.  Literal
#: text, so a later model change cannot move what this revision wrote.
_CONSTRAINTS = (
    (
        "ck_warehouse_connections_type",
        "warehouse_type IN ('bigquery', 'snowflake', 'athena', 'redshift')",
        "warehouse_type IN ('bigquery', 'snowflake', 'athena')",
    ),
    (
        "ck_warehouse_connections_external_id",
        "(warehouse_type IN ('athena', 'redshift')) = (external_id IS NOT NULL)",
        "(warehouse_type = 'athena') = (external_id IS NOT NULL)",
    ),
    (
        "ck_warehouse_connections_athena_no_secret",
        "warehouse_type NOT IN ('athena', 'redshift') OR "
        "(credentials_ciphertext IS NULL AND pending_credentials_ciphertext IS NULL)",
        "warehouse_type <> 'athena' OR (credentials_ciphertext IS NULL "
        "AND pending_credentials_ciphertext IS NULL)",
    ),
)

#: How many connection ids the downgrade's refusal names, at most.
_NAMED_IDS = 20

_OFFLINE_REFUSAL = (
    "modules_0005_redshift's downgrade counts the Amazon Redshift connections "
    "before it changes anything, so it cannot be emitted as SQL; run it online "
    "(without --sql)."
)


def _replace(name: str, condition: str) -> None:
    op.drop_constraint(name, _TABLE, type_="check", schema=_SCHEMA)
    op.create_check_constraint(name, _TABLE, condition, schema=_SCHEMA)


def _redshift_connections() -> List[str]:
    """The id of every ``redshift`` connection, in id order.

    Built with SQLAlchemy Core: the table and the schema are identifiers,
    never string-formatted into a statement.
    """
    connections = sa.table(
        _TABLE, sa.column("id"), sa.column("warehouse_type"), schema=_SCHEMA
    )
    rows = op.get_bind().execute(
        sa.select(connections.c.id)
        .where(connections.c.warehouse_type == "redshift")
        .order_by(connections.c.id)
    )
    return [str(row.id) for row in rows]


def _refusal(ids: List[str]) -> str:
    named = ", ".join(ids[:_NAMED_IDS])
    more = f" and {len(ids) - _NAMED_IDS} more" if len(ids) > _NAMED_IDS else ""
    return (
        f"modules_0005_redshift: refusing to downgrade: {len(ids)} warehouse "
        f"connection(s) are Amazon Redshift connections ({named}{more}). The "
        "previous release's warehouse_connections constraints refuse that type. "
        "Delete those connections, or restore the backup taken before the "
        "upgrade to this release. Nothing was changed."
    )


def upgrade() -> None:
    for name, widened, _previous in _CONSTRAINTS:
        _replace(name, widened)


def downgrade() -> None:
    """Puts the previous release's three constraints back.

    Refused while any ``redshift`` connection is stored: the narrowed
    constraints could not hold it.  The refusal names the connections and
    changes nothing.
    """
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    ids = _redshift_connections()
    if ids:
        raise RuntimeError(_refusal(ids))
    for name, _widened, previous in _CONSTRAINTS:
        _replace(name, previous)

"""Safety rollback records: safety_config_id nullable, its foreign key ON DELETE SET NULL

Downgrade changes nothing: the column stays nullable and the key stays SET NULL.

A rollback no longer gives a flag that has no safety configuration of its own
an empty one, which took the flag off the global default thresholds (#1206).
The rollback record of such a flag has no configuration to point at, so:

* ``safety_rollback_records.safety_config_id`` drops NOT NULL;
* its foreign key to ``feature_flag_safety_configs.id`` becomes ON DELETE SET
  NULL (it was ON DELETE CASCADE), so deleting a configuration keeps the
  flag's rollback records and clears their link instead of deleting them.

Schema only: no row of any table is changed or deleted. The empty configurations
earlier releases added at a rollback stay where they are.

Each step runs only when it is needed, as ``37dcb2969766``'s do: a schema built
by ``db/bootstrap.py``'s ``create_all`` already has both (they are on the
model), and the transition tests rewind such a schema's ``alembic_version`` and
replay the chain from there. The foreign key is found by its column, not by its
name, and is created again under PostgreSQL's default name,
``safety_rollback_records_safety_config_id_fkey``, the name both
``create_safety_tables`` and ``create_all`` give it. The upgrade reads the
catalog, so it cannot be emitted as SQL (``--sql``).

The previous release runs against this schema unchanged: it writes a
configuration id on every record, which a nullable column accepts, and it
reads records whose id is NULL.

Core chain, extending the core head ``37dcb2969766`` (#440). Nothing here
touches a module table, so a full checkout still has exactly two heads: this
revision and ``modules_0002_warehouse_analysis``.

Revision ID: 4c2c09a2b3e8
Revises: 37dcb2969766
Create Date: 2026-10-13
"""

import os
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4c2c09a2b3e8"
down_revision: Union[str, None] = "37dcb2969766"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_RECORDS = "safety_rollback_records"
_CONFIGS = "feature_flag_safety_configs"
_COLUMN = "safety_config_id"
#: PostgreSQL's default name for the key, which every schema so far has.
_FOREIGN_KEY = "safety_rollback_records_safety_config_id_fkey"
_SET_NULL = "SET NULL"

_OFFLINE_REFUSAL = (
    "4c2c09a2b3e8 reads the foreign key's current rule before it changes it "
    "and cannot be emitted as SQL; run it online (without --sql)."
)


def _column_is_nullable() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_RECORDS, schema=_SCHEMA)
    (column,) = [c for c in columns if c["name"] == _COLUMN]
    return bool(column["nullable"])


def _config_foreign_key() -> Optional[dict]:
    """The foreign key on ``safety_config_id``, as the inspector reports it."""
    keys = sa.inspect(op.get_bind()).get_foreign_keys(_RECORDS, schema=_SCHEMA)
    matching = [k for k in keys if k["constrained_columns"] == [_COLUMN]]
    return matching[0] if matching else None


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    if not _column_is_nullable():
        op.alter_column(
            _RECORDS,
            _COLUMN,
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
            schema=_SCHEMA,
        )

    key = _config_foreign_key()
    ondelete = ((key or {}).get("options") or {}).get("ondelete") or ""
    if key is not None and ondelete.upper() == _SET_NULL:
        return
    if key is not None:
        op.drop_constraint(key["name"], _RECORDS, type_="foreignkey", schema=_SCHEMA)
    op.create_foreign_key(
        _FOREIGN_KEY,
        _RECORDS,
        _CONFIGS,
        [_COLUMN],
        ["id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
        ondelete=_SET_NULL,
    )


def downgrade() -> None:
    """Changes nothing: ``safety_config_id`` stays nullable and its key ON DELETE SET NULL.

    Only ``alembic_version`` moves back. The rollback runbook undoes a
    migration while the release that contains it is still serving ("Undo the
    migration before you roll the API back"), and that release writes a NULL
    ``safety_config_id`` for every rollback of a flag on the global defaults.
    NOT NULL again would make each of those rollbacks fail ("Rollback
    failed.", the flag left serving) until the API itself is rolled back,
    which is during an incident, when a rollback is most needed. ON DELETE
    CASCADE again would let a deleted configuration take the flag's rollback
    history with it. The previous release runs against the relaxed schema
    unchanged, so nothing needs to be put back.
    """

"""Feature flags: a value type and two values, held by one check

Downgrade refuses while any flag is a string, number or json flag.

Adds what a flag that serves a string, number or JSON value needs (#1497):

* ``feature_flags.value_type`` (``varchar(16) NOT NULL``, server default
  ``'boolean'``): ``boolean``, ``string``, ``number`` or ``json``.  Every
  existing flag becomes a boolean flag, which is what it is;
* ``feature_flags.on_value`` and ``feature_flags.off_value`` (``jsonb``,
  nullable): what a string, number or json flag serves to the users it is on
  for, and to everyone else.  A boolean flag stores neither: both stay NULL;
* the check ``check_flag_values``, one branch per type:

  - ``boolean``: both values NULL;
  - ``string`` and ``number``: both values present, both of that JSON type, and
    not equal;
  - ``json``: both values present, each an object or an array, and not equal.

  Any other ``value_type`` matches no branch and is refused.

Storage only.  No route, schema, evaluation or ruleset reads or writes these
columns in this release; ``default_value`` is not touched.

**The check states IS NOT NULL in every non-boolean branch.**  PostgreSQL
treats a check whose expression is NULL as satisfied, and ``jsonb_typeof(NULL)``
is NULL, so a branch without it would accept a string flag with no values.
Equality is JSONB's: ``3`` equals ``3.0`` and ``{"a": 1}`` equals ``{"a":
1.0}``, ``true`` never equals ``1``, and arrays compare in order.  JSON
``null`` is a value, not SQL NULL, and its type is ``null``, so every branch
refuses it.

The server default is load-bearing.  The previous release's image does not know
the columns, so its INSERTs name none of them; during a deploy, and after an
image-only rollback, the database supplies ``'boolean'`` and leaves both values
NULL, which the boolean branch accepts.  Without the default those INSERTs fail
on ``NOT NULL``.  That image's SELECTs name only the columns it maps, so it
reads a string, number or json flag as an ordinary on/off flag; the stored
values stay where they are.

Each step runs only when its object is absent, as ``a89544fb1075`` and
``271f03a31742`` do: a schema built by ``db/bootstrap.py``'s ``create_all``
already has all four (they are on the model), and the transition tests rewind
such a schema's ``alembic_version`` and replay the chain from there.  The
upgrade reads the catalog and the downgrade counts rows, so neither can be
emitted as SQL (``--sql``).

``test_feature_flag_value_columns_migration.py`` pins that an upgraded database
and a bootstrapped one end with the same columns, server defaults and check,
what the check refuses and accepts, that the previous release's code still
creates, updates and lists flags against this schema, and the downgrade.

Core chain, extending the core head ``4c2c09a2b3e8`` (#1206).  Nothing here
touches a module table, so a full checkout still has exactly two heads: this
revision and ``modules_0002_warehouse_analysis``.

Revision ID: 607c51a59bf5
Revises: 4c2c09a2b3e8
Create Date: 2026-10-31
"""

import os
from typing import List, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "607c51a59bf5"
down_revision: Union[str, None] = "4c2c09a2b3e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_TABLE = "feature_flags"
_VALUE_TYPE = "value_type"
_ON_VALUE = "on_value"
_OFF_VALUE = "off_value"
_CHECK = "check_flag_values"
#: The same text as ``FLAG_VALUES_CHECK_SQL`` in ``models/feature_flag.py``;
#: the migration test compares the two as PostgreSQL stores them.
_CHECK_SQL = (
    "(value_type = 'boolean' AND on_value IS NULL AND off_value IS NULL)"
    " OR (value_type = 'string'"
    " AND on_value IS NOT NULL AND off_value IS NOT NULL"
    " AND jsonb_typeof(on_value) = 'string' AND jsonb_typeof(off_value) = 'string'"
    " AND on_value <> off_value)"
    " OR (value_type = 'number'"
    " AND on_value IS NOT NULL AND off_value IS NOT NULL"
    " AND jsonb_typeof(on_value) = 'number' AND jsonb_typeof(off_value) = 'number'"
    " AND on_value <> off_value)"
    " OR (value_type = 'json'"
    " AND on_value IS NOT NULL AND off_value IS NOT NULL"
    " AND jsonb_typeof(on_value) IN ('object', 'array')"
    " AND jsonb_typeof(off_value) IN ('object', 'array')"
    " AND on_value <> off_value)"
)

#: How many flag keys the downgrade's refusal names, at most.
_NAMED_KEYS = 20

_OFFLINE_REFUSAL = (
    "607c51a59bf5 reads the catalog before it adds anything, and its downgrade "
    "counts the string, number and json flags before it drops anything, so it "
    "cannot be emitted as SQL; run it online (without --sql)."
)


def _columns() -> set:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(_TABLE, schema=_SCHEMA)}


def _has_check() -> bool:
    checks = sa.inspect(op.get_bind()).get_check_constraints(_TABLE, schema=_SCHEMA)
    return any(check["name"] == _CHECK for check in checks)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    present = _columns()
    if _VALUE_TYPE not in present:
        op.add_column(
            _TABLE,
            sa.Column(
                _VALUE_TYPE,
                sa.String(length=16),
                server_default=sa.text("'boolean'"),
                nullable=False,
            ),
            schema=_SCHEMA,
        )
    for column in (_ON_VALUE, _OFF_VALUE):
        if column not in present:
            op.add_column(
                _TABLE,
                sa.Column(column, postgresql.JSONB(), nullable=True),
                schema=_SCHEMA,
            )
    if not _has_check():
        op.create_check_constraint(_CHECK, _TABLE, _CHECK_SQL, schema=_SCHEMA)


def _value_flags() -> List[str]:
    """The keys of every flag that is not a boolean flag, in key order.

    Built with SQLAlchemy Core: the table and the schema are identifiers,
    never string-formatted into a statement.
    """
    flags = sa.table(_TABLE, sa.column("key"), sa.column(_VALUE_TYPE), schema=_SCHEMA)
    rows = op.get_bind().execute(
        sa.select(flags.c.key)
        .where(flags.c.value_type != "boolean")
        .order_by(flags.c.key)
    )
    return [row.key for row in rows]


def _refusal(keys: List[str]) -> str:
    named = ", ".join(keys[:_NAMED_KEYS])
    more = f" and {len(keys) - _NAMED_KEYS} more" if len(keys) > _NAMED_KEYS else ""
    return (
        f"607c51a59bf5: refusing to downgrade: {len(keys)} feature flag(s) serve a "
        f"string, number or json value ({named}{more}). This downgrade drops "
        "value_type, on_value and off_value, which hold each such flag's type and "
        "both of its values, and nothing could rebuild them. Delete those flags, "
        "or convert each to a boolean flag (value_type 'boolean', on_value and "
        "off_value NULL), or restore the backup taken before the upgrade to this "
        "release. Nothing was changed."
    )


def downgrade() -> None:
    """Drops the check and the three columns: every flag's value type and values.

    Refused while any flag is a string, number or json flag
    (``value_type <> 'boolean'``): dropping the columns would delete that
    flag's type and both of its values, and nothing could rebuild them.  The
    refusal names the flags and changes nothing.  With only boolean flags
    stored, the columns hold nothing a previous release reads: ``value_type``
    is ``'boolean'`` and both values are NULL on every row.
    """
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)
    keys = _value_flags()
    if keys:
        raise RuntimeError(_refusal(keys))
    op.drop_constraint(_CHECK, _TABLE, type_="check", schema=_SCHEMA)
    op.drop_column(_TABLE, _OFF_VALUE, schema=_SCHEMA)
    op.drop_column(_TABLE, _ON_VALUE, schema=_SCHEMA)
    op.drop_column(_TABLE, _VALUE_TYPE, schema=_SCHEMA)

"""The modules branch, fourth revision: an activation time for custom role assignments and direct grants (#891).

Adds ``activated_at`` (``timestamp with time zone``, nullable, no default) to
``user_custom_roles`` and ``direct_permission_grants``.  When this revision
was written, nothing wrote the column or read it: every assignment and grant
stored before the upgrade has none, and so has every one made by code that
does not set it.

There is no default and no backfill, on purpose.  ``ADD COLUMN ... DEFAULT
now()`` would give every row already stored an activation time it was never
given.  The previous release's image does not know the column, so its
INSERTs do not name it; a nullable column with no default takes them
unchanged, and those rows have none either.

The upgrade then logs the rows without an activation time, so an operator
has a record of what existed when it ran: one WARNING that counts them and
lists one line per row (at most :data:`LISTED`, then ``… and N more``), or
one INFO line when there are none.  Expired grants and grants with
``is_active`` false are neither counted nor listed.  Each line carries a user
id and the role name, or the resource, the actions and the expiry; never a
``reason``.  ``docs/self-hosting/migrations.md`` ("modules_0004_rbac_activation")
quotes the text.

Each column is added only when it is absent, as ``modules_0003_lower_is_better``
does: a schema built by ``db/bootstrap.py``'s ``create_all`` already has it (it
is on the models), and the transition tests rewind such a schema's
``alembic_version`` and replay the chain from there.

Downgrade drops each column when it is there, on every database however it was
made, as ``modules_0003_lower_is_better`` does: the previous release's models
have no such column, so a database that kept it would differ from them.  The
rows stay; any activation time stored in the column goes with it, and an
upgrade afterwards adds the column empty.

``backend/tests/integration/database/test_modules_0004_transitions.py`` pins
the upgrade from the previous release's database and its log, the column's
shape on a migrated and a bootstrapped database, the previous release's
INSERT, and the downgrade.

Modules branch, extending ``modules_0003_lower_is_better`` (written by hand for
``revision --head modules@head``).  Nothing here touches a core table, so a
full checkout still has exactly two heads: this revision and the core head.

Revision ID: modules_0004_rbac_activation
Revises: modules_0003_lower_is_better
Create Date: 2026-11-02
"""

import json
import logging
import os
from datetime import timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "modules_0004_rbac_activation"
down_revision: Union[str, None] = "modules_0003_lower_is_better"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# alembic.ini sets the ``alembic`` logger to INFO, so both lines reach stderr.
logger = logging.getLogger("alembic.runtime.migration")

# The schema name comes from the deployment's own environment, never from a
# request.
_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")

_ASSIGNMENTS = "user_custom_roles"
_GRANTS = "direct_permission_grants"
_TABLES = (_ASSIGNMENTS, _GRANTS)
_COLUMN = "activated_at"

#: The most rows the upgrade lists one by one; the rest are counted.
LISTED = 200


def _has_column(table: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table, schema=_SCHEMA)
    return any(column["name"] == _COLUMN for column in columns)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _rows_without_an_activation_time(bind) -> tuple[int, int, list[str]]:
    """``(assignments, grants, lines)``: both counts, and at most
    :data:`LISTED` lines, assignments first.  Built with SQLAlchemy Core, so
    the dialect quotes the schema and the tables."""
    roles = sa.table("custom_roles", sa.column("id"), sa.column("name"), schema=_SCHEMA)
    assignments = sa.table(
        _ASSIGNMENTS,
        sa.column("id"),
        sa.column("user_id"),
        sa.column("role_id"),
        sa.column(_COLUMN),
        schema=_SCHEMA,
    )
    grants = sa.table(
        _GRANTS,
        sa.column("id"),
        sa.column("user_id"),
        sa.column("resource"),
        sa.column("actions"),
        sa.column("expires_at"),
        sa.column("is_active"),
        sa.column(_COLUMN),
        schema=_SCHEMA,
    )
    assignment_rows = assignments.join(roles, roles.c.id == assignments.c.role_id)
    assignment_filter = assignments.c[_COLUMN].is_(None)
    # Expired grants and grants with is_active false are neither counted nor
    # listed; expiry is read by the database's clock.
    grant_filter = sa.and_(
        grants.c[_COLUMN].is_(None),
        grants.c.is_active.is_(True),
        sa.or_(grants.c.expires_at.is_(None), grants.c.expires_at > sa.func.now()),
    )

    assignment_count = bind.execute(
        sa.select(sa.func.count())
        .select_from(assignment_rows)
        .where(assignment_filter)
    ).scalar_one()
    grant_count = bind.execute(
        sa.select(sa.func.count()).select_from(grants).where(grant_filter)
    ).scalar_one()

    lines = [
        f"assignment user_id={user_id} role_name={role_name}"
        for user_id, role_name in bind.execute(
            sa.select(assignments.c.user_id, roles.c.name)
            .select_from(assignment_rows)
            .where(assignment_filter)
            .order_by(assignments.c.user_id, roles.c.name, assignments.c.id)
            .limit(LISTED)
        )
    ]
    room = LISTED - len(lines)
    if room > 0:
        for user_id, resource, actions, expires_at in bind.execute(
            sa.select(
                grants.c.user_id,
                grants.c.resource,
                grants.c.actions,
                grants.c.expires_at,
            )
            .where(grant_filter)
            .order_by(grants.c.user_id, grants.c.resource, grants.c.id)
            .limit(room)
        ):
            expiry = (
                "none"
                if expires_at is None
                else expires_at.astimezone(timezone.utc).isoformat()
            )
            lines.append(
                f"grant user_id={user_id} resource={resource} "
                f"actions={json.dumps(actions)} expires_at={expiry}"
            )
    return assignment_count, grant_count, lines


def _log_rows_without_an_activation_time() -> None:
    assignment_count, grant_count, lines = _rows_without_an_activation_time(
        op.get_bind()
    )
    total = assignment_count + grant_count
    if total == 0:
        logger.info(
            "%s: no custom role assignments or direct permission grants "
            "without an activation time",
            revision,
        )
        return
    parts = []
    if assignment_count:
        parts.append(_counted(assignment_count, "custom role assignment"))
    if grant_count:
        parts.append(_counted(grant_count, "direct permission grant"))
    header = (
        f"{revision}: {' and '.join(parts)} {'has' if total == 1 else 'have'} "
        "no activation time (activated_at is empty; expired grants and grants "
        "with is_active false are not counted or listed). See "
        f'docs/self-hosting/migrations.md, "{revision}":'
    )
    if total > len(lines):
        lines.append(f"… and {total - len(lines)} more")
    logger.warning("\n".join([header, *lines]))


def upgrade() -> None:
    for table in _TABLES:
        if not _has_column(table):
            op.add_column(
                table,
                sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True),
                schema=_SCHEMA,
            )
    _log_rows_without_an_activation_time()


def downgrade() -> None:
    for table in _TABLES:
        if _has_column(table):
            op.drop_column(table, _COLUMN, schema=_SCHEMA)

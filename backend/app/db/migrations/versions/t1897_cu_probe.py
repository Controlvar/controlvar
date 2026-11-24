"""Compose upgrade probe

A throwaway revision for one Compose Upgrade run, never merged: one revision
on the core head, so that the run's delta is not empty.

Revision ID: t1897_cu_probe
Revises: t449_identity_aliases
Create Date: 2026-11-24
"""

import os
from typing import Sequence, Union

from alembic import op

revision: str = "t1897_cu_probe"
down_revision: Union[str, None] = "t449_identity_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")


def upgrade() -> None:
    op.execute(
        f"UPDATE {_SCHEMA}.feature_flags"
        " SET targeting_rules = jsonb_build_object('rules', jsonb_build_array())"
        " WHERE targeting_rules IS NOT NULL"
    )


def downgrade() -> None:
    pass

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
    op.execute(f"SELECT 1 FROM {_SCHEMA}.alembic_version LIMIT 0")


def downgrade() -> None:
    pass

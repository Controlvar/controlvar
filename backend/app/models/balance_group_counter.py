"""Each group's next place in a balanced experiment's blocks (#448).

One row per experiment and group: ``next_seq`` is the seq the group's next new
user takes. ``backend.app.services.balanced_allocation.take_seq`` takes it with
one ``INSERT ... ON CONFLICT DO UPDATE SET next_seq = next_seq + 1 RETURNING
next_seq - 1`` in the assignment's own transaction, so the increment commits
or rolls back with the assignment row, and the stored seqs of a group are
exactly ``0 .. next_seq - 1``.

Deleting the experiment deletes its counters (``ON DELETE CASCADE``).
"""

from sqlalchemy import BigInteger, Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from backend.app.core.database_config import get_schema_name
from backend.app.models.base import Base


class BalanceGroupCounter(Base):
    """The next seq of one group of one balanced experiment."""

    __tablename__ = "balance_group_counters"

    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.experiments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: The group, as ``assignments.balance_group`` stores it.
    balance_group = Column(String(100), primary_key=True)
    #: The seq the group's next new user takes; the first user takes 0.
    next_seq = Column(BigInteger, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<BalanceGroupCounter {self.experiment_id} {self.balance_group} "
            f"next_seq={self.next_seq}>"
        )

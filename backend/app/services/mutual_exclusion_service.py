"""
Mutual Exclusion Service for managing experiment groups (EP-022).

Ensures that users can only be assigned to one experiment within a group,
using consistent hashing for deterministic, stable user-to-experiment mapping.
"""

import hashlib
import logging
import struct
from typing import List, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)

logger = logging.getLogger(__name__)

#: SQLSTATE ``unique_violation``.
_UNIQUE_VIOLATION = "23505"

#: The unique constraint on ``mutual_exclusion_groups.name``. It is unnamed in
#: the model and the migration, so PostgreSQL names it ``<table>_<column>_key``,
#: the same in every schema. It compares names exactly: "Checkout" and
#: "checkout" are two names.
NAME_UNIQUE_CONSTRAINT = "mutual_exclusion_groups_name_key"


def group_name_taken_detail(name: str) -> str:
    """The 409 detail for a name another group has (#1019)."""
    return f"A mutual exclusion group with the name '{name}' already exists."


class GroupNameTaken(Exception):
    """Another group already has this name (#1019)."""

    def __init__(self, name: str) -> None:
        super().__init__(group_name_taken_detail(name))


def _is_name_violation(error: IntegrityError) -> bool:
    """True when *error* is the unique constraint on the name refusing a write.

    Decided from the driver's diagnostics (SQLSTATE, table, constraint), never
    from the message text; any other integrity error is not this one.
    """
    orig = getattr(error, "orig", None)
    if getattr(orig, "pgcode", None) != _UNIQUE_VIOLATION:
        return False
    diag = getattr(orig, "diag", None)
    return (
        getattr(diag, "table_name", None) == MutualExclusionGroup.__tablename__
        and getattr(diag, "constraint_name", None) == NAME_UNIQUE_CONSTRAINT
    )


class MutualExclusionService:
    """Service for managing mutual exclusion groups and user-to-experiment selection."""

    MAX_HASH_VALUE = 0xFFFFFFFF

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_group(
        self,
        name: str,
        description: Optional[str] = None,
        traffic_allocation: float = 1.0,
        owner_id: Optional[UUID] = None,
    ) -> MutualExclusionGroup:
        """Create a new mutual exclusion group.

        Raises ``GroupNameTaken`` when another group has ``name``.
        """
        if self._name_taken(name):
            raise GroupNameTaken(name)
        group = MutualExclusionGroup(
            name=name,
            description=description,
            traffic_allocation=traffic_allocation,
            status=MutualExclusionGroupStatus.ACTIVE,
            owner_id=owner_id,
        )
        self.db.add(group)
        self._commit(name)
        self.db.refresh(group)
        return group

    def get_group(self, group_id: UUID) -> Optional[MutualExclusionGroup]:
        """Get a mutual exclusion group by ID."""
        return (
            self.db.query(MutualExclusionGroup)
            .filter(MutualExclusionGroup.id == group_id)
            .first()
        )

    def list_groups(
        self, status: Optional[str] = None, skip: int = 0, limit: int = 100
    ) -> List[MutualExclusionGroup]:
        """List mutual exclusion groups with optional status filter."""
        query = self.db.query(MutualExclusionGroup)
        if status:
            query = query.filter(
                MutualExclusionGroup.status == MutualExclusionGroupStatus(status)
            )
        return query.offset(skip).limit(limit).all()

    def count_groups(self, status: Optional[str] = None) -> int:
        """Count mutual exclusion groups with optional status filter."""
        query = self.db.query(MutualExclusionGroup)
        if status:
            query = query.filter(
                MutualExclusionGroup.status == MutualExclusionGroupStatus(status)
            )
        return query.count()

    def update_group(
        self,
        group_id: UUID,
        name: Optional[str] = None,
        description: Optional[str] = None,
        traffic_allocation: Optional[float] = None,
        status: Optional[str] = None,
    ) -> Optional[MutualExclusionGroup]:
        """Update a mutual exclusion group.

        Raises ``GroupNameTaken`` when ``name`` is another group's; nothing is
        changed then.
        """
        group = self.get_group(group_id)
        if not group:
            return None

        if name is not None and name != group.name and self._name_taken(name, group.id):
            raise GroupNameTaken(name)
        if name is not None:
            group.name = name
        if description is not None:
            group.description = description
        if traffic_allocation is not None:
            group.traffic_allocation = traffic_allocation
        if status is not None:
            group.status = MutualExclusionGroupStatus(status)

        self._commit(name)
        self.db.refresh(group)
        return group

    def _name_taken(self, name: str, exclude_id: Optional[UUID] = None) -> bool:
        """Whether a group other than ``exclude_id`` has ``name``, compared as the
        unique constraint compares it (exactly). A check before the write; the
        constraint itself is the guarantee (``_commit``)."""
        query = self.db.query(MutualExclusionGroup.id).filter(
            MutualExclusionGroup.name == name
        )
        if exclude_id is not None:
            query = query.filter(MutualExclusionGroup.id != exclude_id)
        return query.first() is not None

    def _commit(self, name: Optional[str]) -> None:
        """Commit; the name's unique constraint refusing it is ``GroupNameTaken``.

        A request that wrote the same name after this one's ``_name_taken``
        reaches here. The session is rolled back first, so it stays usable;
        any other integrity error is raised as it was.
        """
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            if name is not None and _is_name_violation(error):
                raise GroupNameTaken(name) from error
            raise

    def archive_group(self, group_id: UUID) -> Optional[MutualExclusionGroup]:
        """Archive (soft-delete) a mutual exclusion group."""
        return self.update_group(group_id, status="archived")

    # ------------------------------------------------------------------
    # Experiment Management
    # ------------------------------------------------------------------

    def add_experiment_to_group(
        self, group_id: UUID, experiment_id: UUID
    ) -> Optional[Experiment]:
        """Add an experiment to a mutual exclusion group."""
        group = self.get_group(group_id)
        if not group:
            raise ValueError(f"Group {group_id} not found")

        experiment = (
            self.db.query(Experiment).filter(Experiment.id == experiment_id).first()
        )
        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        if experiment.mutual_exclusion_group_id is not None:
            if experiment.mutual_exclusion_group_id == group_id:
                return experiment  # Already in this group
            raise ValueError(f"Experiment {experiment_id} is already in another group")

        experiment.mutual_exclusion_group_id = group_id
        self.db.commit()
        self.db.refresh(experiment)
        return experiment

    def remove_experiment_from_group(
        self, group_id: UUID, experiment_id: UUID
    ) -> Optional[Experiment]:
        """Remove an experiment from a mutual exclusion group."""
        experiment = (
            self.db.query(Experiment)
            .filter(
                Experiment.id == experiment_id,
                Experiment.mutual_exclusion_group_id == group_id,
            )
            .first()
        )
        if not experiment:
            raise ValueError(
                f"Experiment {experiment_id} not found in group {group_id}"
            )

        experiment.mutual_exclusion_group_id = None
        self.db.commit()
        self.db.refresh(experiment)
        return experiment

    def get_group_experiments(self, group_id: UUID) -> List[Experiment]:
        """Get all experiments in a mutual exclusion group."""
        return (
            self.db.query(Experiment)
            .filter(Experiment.mutual_exclusion_group_id == group_id)
            .all()
        )

    def get_active_group_experiments(self, group_id: UUID) -> List[Experiment]:
        """Get active experiments in a mutual exclusion group."""
        return (
            self.db.query(Experiment)
            .filter(
                Experiment.mutual_exclusion_group_id == group_id,
                Experiment.status == ExperimentStatus.ACTIVE,
            )
            .all()
        )

    # ------------------------------------------------------------------
    # User-to-Experiment Selection
    # ------------------------------------------------------------------

    def select_experiment_for_user(
        self, user_id: str, group_id: UUID
    ) -> Optional[UUID]:
        """
        Select which experiment a user should participate in within a group.

        Algorithm:
        1. Hash (user_id, group_id) → normalize to [0, 1)
        2. If value >= group.traffic_allocation → exclude from all experiments
        3. Divide [0, traffic_allocation) proportionally among active experiments
        4. Return the experiment whose sub-range contains the hash value

        Returns:
            Experiment ID the user is assigned to, or None if excluded.
        """
        group = self.get_group(group_id)
        if not group or group.status != MutualExclusionGroupStatus.ACTIVE:
            return None

        # Get active experiments in the group
        active_experiments = self.get_active_group_experiments(group_id)
        if not active_experiments:
            return None

        # Hash user into [0, 1)
        hash_value = self._normalized_hash(user_id, str(group_id))

        # Check traffic allocation
        if hash_value >= group.traffic_allocation:
            return None

        # Distribute within traffic allocation proportionally
        num_experiments = len(active_experiments)
        slot_size = group.traffic_allocation / num_experiments
        cumulative = 0.0

        for experiment in sorted(active_experiments, key=lambda e: str(e.id)):
            cumulative += slot_size
            if hash_value < cumulative:
                return experiment.id

        # Fallback to last experiment
        return active_experiments[-1].id

    def _normalized_hash(self, user_id: str, salt: str) -> float:
        """Hash user_id with salt and normalize to [0, 1)."""
        combined = f"{user_id}:{salt}".encode("utf-8")
        hash_bytes = hashlib.md5(combined, usedforsecurity=False).digest()[:4]
        hash_int = struct.unpack("<I", hash_bytes)[0]
        return hash_int / (self.MAX_HASH_VALUE + 1)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def get_user_experiment_group(
        self, experiment_id: UUID
    ) -> Optional[MutualExclusionGroup]:
        """Get the mutual exclusion group for an experiment, if any."""
        experiment = (
            self.db.query(Experiment).filter(Experiment.id == experiment_id).first()
        )
        if not experiment or not experiment.mutual_exclusion_group_id:
            return None
        return self.get_group(experiment.mutual_exclusion_group_id)

    def is_user_eligible_for_experiment(
        self, user_id: str, experiment_id: UUID
    ) -> bool:
        """Check if a user is eligible for an experiment given mutual exclusion rules.

        Experiments without a group, or whose group has been archived, carry no
        constraint.  Otherwise the user is eligible only when the group's
        consistent hashing selects this experiment for them AND they hold no
        assignment in another experiment of the group that is still live
        (see ``has_sibling_assignment``).  The second condition is what keeps
        a user in one experiment when the group's active set changes: the hash
        divides users among the ACTIVE experiments only, so activating,
        pausing or resuming a sibling moves users between slots.
        """
        group = self.get_user_experiment_group(experiment_id)
        if not group:
            return True  # No group constraint
        if group.status == MutualExclusionGroupStatus.ARCHIVED:
            return True  # Archived groups no longer constrain their experiments

        selected = self.select_experiment_for_user(user_id, group.id)
        if selected != experiment_id:
            return False

        # Only reached once the hash has picked this experiment, so the extra
        # query runs for users who would otherwise be enrolled.
        if self.has_sibling_assignment(user_id, group.id, experiment_id):
            logger.debug(
                "Mutual exclusion group %s: user already enrolled in another "
                "experiment of the group; not enrolling in %s",
                group.id,
                experiment_id,
            )
            return False
        return True

    def has_sibling_assignment(
        self, user_id: str, group_id: UUID, experiment_id: UUID
    ) -> bool:
        """Whether the user holds an assignment in another live experiment of the group.

        A sibling is any experiment other than ``experiment_id`` whose CURRENT
        ``mutual_exclusion_group_id`` is ``group_id`` and whose status is not
        COMPLETED or ARCHIVED -- so ACTIVE, PAUSED and DRAFT siblings all hold
        their users.  Completing a sibling, or removing it from the group,
        releases them.
        """
        row = (
            self.db.query(Assignment.id)
            .join(Experiment, Experiment.id == Assignment.experiment_id)
            .filter(
                Assignment.user_id == user_id,
                Experiment.mutual_exclusion_group_id == group_id,
                Experiment.id != experiment_id,
                Experiment.status.notin_(
                    [ExperimentStatus.COMPLETED, ExperimentStatus.ARCHIVED]
                ),
            )
            .limit(1)
            .first()
        )
        return row is not None

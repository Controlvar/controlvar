"""
LLM Experiment Service (EP-046).

Handles CRUD operations for LLM experiments and variants, plus consistent-hash
variant assignment (same algorithm used by the JS/Python/Java SDKs).
"""

import hashlib
import logging
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from backend.app.models.llm_experiment import (
    LLMEvaluationMetric,
    LLMExperiment,
    LLMExperimentStatus,
    LLMProvider,
    LLMTaskType,
    LLMVariant,
)
from backend.app.schemas.llm_experiments import (
    CreateLLMExperimentRequest,
    CreateLLMVariantRequest,
    UpdateLLMExperimentRequest,
    UpdateLLMVariantRequest,
)

logger = logging.getLogger(__name__)


class LLMExperimentService:
    """
    Core service for LLM experiment lifecycle management.

    Provides:
    - CRUD for LLMExperiment and LLMVariant records
    - Consistent-hash variant assignment (MD5, same as SDK pattern)
    - Status transitions (start / pause)
    """

    # ------------------------------------------------------------------
    # Experiment CRUD
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        db: Session,
        data: CreateLLMExperimentRequest,
        created_by: Optional[UUID] = None,
    ) -> LLMExperiment:
        """Create a new LLM experiment with variants."""
        experiment = LLMExperiment(
            name=data.name,
            description=data.description,
            status=LLMExperimentStatus.DRAFT,
            task_type=LLMTaskType(data.task_type),
            evaluation_metric=LLMEvaluationMetric(data.evaluation_metric),
            created_by=created_by,
        )
        db.add(experiment)
        db.flush()  # Get the ID before adding variants

        for variant_data in data.variants:
            variant = self._build_variant(experiment.id, variant_data)
            db.add(variant)

        db.commit()
        db.refresh(experiment)
        return experiment

    def get_experiment(
        self, db: Session, experiment_id: UUID
    ) -> Optional[LLMExperiment]:
        """Retrieve a single LLM experiment with its variants."""
        return db.query(LLMExperiment).filter(LLMExperiment.id == experiment_id).first()

    def list_experiments(
        self,
        db: Session,
        status: Optional[LLMExperimentStatus] = None,
        task_type: Optional[LLMTaskType] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[List[LLMExperiment], int]:
        """List LLM experiments with optional filters, returns (items, total).

        The filters are members, never request text: the route turns the
        query's text into a member, in any case, or answers 422 (#955).
        """
        query = db.query(LLMExperiment)
        if status is not None:
            query = query.filter(LLMExperiment.status == status)
        if task_type is not None:
            query = query.filter(LLMExperiment.task_type == task_type)
        total = query.count()
        items = (
            query.order_by(LLMExperiment.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def update_experiment(
        self,
        db: Session,
        experiment_id: UUID,
        data: UpdateLLMExperimentRequest,
    ) -> Optional[LLMExperiment]:
        """Update a LLM experiment. Returns None if not found."""
        experiment = self.get_experiment(db, experiment_id)
        if experiment is None:
            return None
        update_data = data.model_dump(exclude_unset=True)
        if "task_type" in update_data:
            update_data["task_type"] = LLMTaskType(update_data["task_type"])
        if "evaluation_metric" in update_data:
            update_data["evaluation_metric"] = LLMEvaluationMetric(
                update_data["evaluation_metric"]
            )
        for field, value in update_data.items():
            setattr(experiment, field, value)
        db.commit()
        db.refresh(experiment)
        return experiment

    def start_experiment(
        self, db: Session, experiment_id: UUID
    ) -> Optional[LLMExperiment]:
        """
        Transition an experiment from DRAFT/PAUSED → ACTIVE.

        Validates that there is at least one control variant before starting.
        """
        experiment = self.get_experiment(db, experiment_id)
        if experiment is None:
            return None
        if experiment.status not in (
            LLMExperimentStatus.DRAFT,
            LLMExperimentStatus.PAUSED,
        ):
            raise ValueError(
                f"Cannot start experiment with status {experiment.status.value}. "
                "Only DRAFT or PAUSED experiments can be started."
            )
        if not experiment.variants:
            raise ValueError("Cannot start experiment without variants")
        controls = [v for v in experiment.variants if v.is_control]
        if not controls:
            raise ValueError("Cannot start experiment without a control variant")
        experiment.status = LLMExperimentStatus.ACTIVE
        db.commit()
        db.refresh(experiment)
        return experiment

    def pause_experiment(
        self, db: Session, experiment_id: UUID
    ) -> Optional[LLMExperiment]:
        """Transition an experiment from ACTIVE → PAUSED."""
        experiment = self.get_experiment(db, experiment_id)
        if experiment is None:
            return None
        if experiment.status != LLMExperimentStatus.ACTIVE:
            raise ValueError(
                f"Cannot pause experiment with status {experiment.status.value}. "
                "Only ACTIVE experiments can be paused."
            )
        experiment.status = LLMExperimentStatus.PAUSED
        db.commit()
        db.refresh(experiment)
        return experiment

    def complete_experiment(
        self, db: Session, experiment_id: UUID
    ) -> Optional[LLMExperiment]:
        """Transition an experiment to COMPLETED."""
        experiment = self.get_experiment(db, experiment_id)
        if experiment is None:
            return None
        experiment.status = LLMExperimentStatus.COMPLETED
        db.commit()
        db.refresh(experiment)
        return experiment

    # ------------------------------------------------------------------
    # Variant CRUD
    # ------------------------------------------------------------------

    def add_variant(
        self,
        db: Session,
        experiment_id: UUID,
        data: CreateLLMVariantRequest,
    ) -> LLMVariant:
        """Add a variant to an existing experiment."""
        variant = self._build_variant(experiment_id, data)
        db.add(variant)
        db.commit()
        db.refresh(variant)
        return variant

    def update_variant(
        self,
        db: Session,
        variant_id: UUID,
        data: UpdateLLMVariantRequest,
    ) -> Optional[LLMVariant]:
        """Update an LLM variant. Returns None if not found."""
        variant = db.query(LLMVariant).filter(LLMVariant.id == variant_id).first()
        if variant is None:
            return None
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(variant, field, value)
        db.commit()
        db.refresh(variant)
        return variant

    def get_variant(self, db: Session, variant_id: UUID) -> Optional[LLMVariant]:
        """Get a variant by ID."""
        return db.query(LLMVariant).filter(LLMVariant.id == variant_id).first()

    # ------------------------------------------------------------------
    # Variant assignment
    # ------------------------------------------------------------------

    def assign_variant(
        self, db: Session, experiment_id: UUID, user_id: str
    ) -> LLMVariant:
        """
        Deterministically assign a user to a variant using consistent hashing.

        Uses MD5 of ``{experiment_id}:{user_id}`` modulo 10000 to produce a
        bucket 0–9999, then walks the sorted variant list accumulating traffic
        until the bucket falls in the variant's range.  Same algorithm as the
        JS/Python/Java SDKs.

        Raises:
            ValueError: if the experiment is not ACTIVE or has no variants.
        """
        experiment = self.get_experiment(db, experiment_id)
        if experiment is None:
            raise ValueError(f"Experiment {experiment_id} not found")
        if experiment.status != LLMExperimentStatus.ACTIVE:
            raise ValueError(
                f"Experiment is not ACTIVE (status={experiment.status.value})"
            )
        variants = list(experiment.variants)
        if not variants:
            raise ValueError("Experiment has no variants")

        # Sort variants deterministically so bucket boundaries are stable
        variants_sorted = sorted(variants, key=lambda v: str(v.id))

        bucket = self._hash_bucket(str(experiment_id), user_id)
        cumulative = 0.0
        for variant in variants_sorted:
            cumulative += variant.traffic_split
            if bucket < cumulative:
                return variant
        # Fallback to last variant (handles floating-point imprecision)
        return variants_sorted[-1]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_bucket(experiment_id: str, user_id: str) -> float:
        """
        Map ``experiment_id:user_id`` to a float in [0, 1).

        Reduces the full 128-bit MD5 digest modulo 10000 and divides by
        10000, matching the other assignment hashers in the platform.

        Note: an earlier version reduced only the first 16 bits of the digest
        modulo 10000.  Because 65536 is not a multiple of 10000, buckets
        0-5535 were 7/6 as likely as buckets 5536-9999, which skewed a
        nominal 33/33/34 split to roughly 36/34/30.  Using the whole digest
        makes the bias negligible (2**128 mod 10000 is a rounding error).
        """
        key = f"{experiment_id}:{user_id}"
        digest = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
        bucket = int(digest, 16) % 10000
        return bucket / 10000.0

    @staticmethod
    def _build_variant(
        experiment_id: UUID, data: CreateLLMVariantRequest
    ) -> LLMVariant:
        """Construct an LLMVariant ORM object from a request schema."""
        return LLMVariant(
            llm_experiment_id=experiment_id,
            name=data.name,
            is_control=data.is_control,
            traffic_split=data.traffic_split,
            provider=LLMProvider(data.provider),
            model_name=data.model_name,
            system_prompt=data.system_prompt,
            prompt_template=data.prompt_template,
            temperature=data.temperature,
            max_tokens=data.max_tokens,
            additional_params=data.additional_params,
        )

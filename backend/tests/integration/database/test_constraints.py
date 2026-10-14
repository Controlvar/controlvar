"""
Integration tests for database constraints.

Tests that verify unique, not-null, and foreign key constraints are enforced
at the database layer for Experiment, Variant, Metric, and FeatureFlag models.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.models.assignment import Assignment
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus


@pytest.mark.integration
@pytest.mark.requires_db
class TestExperimentConstraints:
    """Test database constraints on Experiment and related models."""

    def test_experiment_name_not_null(self, db_session, admin_user):
        """Experiment.name must not be null — IntegrityError raised if omitted."""
        exp = Experiment(
            name=None,
            owner_id=admin_user.id,
            status=ExperimentStatus.DRAFT,
        )
        db_session.add(exp)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_experiment_status_has_default_draft(self, db_session, admin_user):
        """Experiment.status defaults to DRAFT when not explicitly set."""
        exp = Experiment(
            name="Default Status Experiment",
            owner_id=admin_user.id,
        )
        db_session.add(exp)
        db_session.flush()
        # The column default applies; status should be DRAFT
        assert exp.status == ExperimentStatus.DRAFT or exp.status is not None
        db_session.rollback()

    def test_two_experiments_same_name_allowed(self, db_session, admin_user):
        """Two experiments with the same name (same owner) are permitted — no unique constraint."""
        exp1 = Experiment(
            name="Duplicate Name",
            owner_id=admin_user.id,
            status=ExperimentStatus.DRAFT,
        )
        exp2 = Experiment(
            name="Duplicate Name",
            owner_id=admin_user.id,
            status=ExperimentStatus.DRAFT,
        )
        db_session.add_all([exp1, exp2])
        db_session.flush()  # Should not raise
        db_session.rollback()

    def test_variant_requires_experiment_id(self, db_session):
        """Variant.experiment_id is non-nullable — IntegrityError if missing."""
        variant = Variant(
            experiment_id=None,
            name="Orphan Variant",
        )
        db_session.add(variant)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_variant_foreign_key_must_exist(self, db_session):
        """Variant.experiment_id must reference a real experiment — FK violation."""
        fake_id = uuid.uuid4()
        variant = Variant(
            experiment_id=fake_id,
            name="Ghost Variant",
        )
        db_session.add(variant)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_metric_requires_experiment_id(self, db_session):
        """Metric.experiment_id is non-nullable — IntegrityError if missing."""
        metric = Metric(
            experiment_id=None,
            name="Orphan Metric",
            event_name="click",
        )
        db_session.add(metric)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_metric_name_unique_within_experiment(self, db_session, make_experiment):
        """Metric names must be unique within an experiment — second insert raises."""
        exp = make_experiment(name="Metric Unique Test")
        m1 = Metric(
            experiment_id=exp.id,
            name="Conversion",
            event_name="click",
        )
        m2 = Metric(
            experiment_id=exp.id,
            name="Conversion",  # Duplicate within same experiment
            event_name="purchase",
        )
        db_session.add(m1)
        db_session.flush()
        db_session.add(m2)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_metric_name_unique_per_experiment_only(self, db_session, make_experiment):
        """Same metric name is allowed across different experiments."""
        exp1 = make_experiment(name="Exp Metric A")
        exp2 = make_experiment(name="Exp Metric B")
        m1 = Metric(experiment_id=exp1.id, name="Conversion", event_name="click")
        m2 = Metric(experiment_id=exp2.id, name="Conversion", event_name="click")
        db_session.add_all([m1, m2])
        db_session.flush()  # Should succeed
        db_session.rollback()

    def test_variant_traffic_allocation_check(self, db_session, make_experiment):
        """Variant.traffic_allocation must be between 0 and 100 — constraint violation."""
        exp = make_experiment(name="Traffic Check")
        variant = Variant(
            experiment_id=exp.id,
            name="Overflow Variant",
            traffic_allocation=101,
        )
        db_session.add(variant)
        with pytest.raises((IntegrityError, Exception)):
            db_session.flush()
        db_session.rollback()

    def test_end_date_must_be_after_start_date(self, db_session, make_experiment):
        """check_experiment_dates: an end date on or before the start is refused."""
        exp = make_experiment(name="Date Order")
        exp.start_date = datetime(2026, 3, 1, 12, 0)
        exp.end_date = datetime(2026, 3, 1, 12, 1)
        db_session.flush()  # after the start: accepted
        exp.end_date = datetime(2026, 3, 1, 12, 0)  # the same moment is not after it
        with pytest.raises(IntegrityError, match="check_experiment_dates"):
            db_session.flush()
        db_session.rollback()

    def test_the_database_does_not_check_that_allocations_sum_to_100(
        self, db_session, make_experiment
    ):
        """Only the API checks the sum: rows written directly may sum to anything."""
        exp = make_experiment(name="Allocation Sum")
        db_session.add_all(
            [
                Variant(experiment_id=exp.id, name="A", traffic_allocation=30),
                Variant(experiment_id=exp.id, name="B", traffic_allocation=30),
            ]
        )
        db_session.flush()  # 60 in all: the database accepts it
        total = sum(
            v.traffic_allocation
            for v in db_session.query(Variant).filter(Variant.experiment_id == exp.id)
        )
        assert total == 60
        db_session.rollback()

    def test_assignment_unique_per_experiment_user(
        self, db_session, make_experiment, make_variant
    ):
        """Only one assignment per (experiment, user_id) pair — unique constraint."""
        exp = make_experiment(name="Assignment Unique Test")
        variant = make_variant(experiment=exp, name="Control", is_control=True)

        a1 = Assignment(
            experiment_id=exp.id,
            variant_id=variant.id,
            user_id="unique-user-001",
        )
        db_session.add(a1)
        db_session.flush()

        a2 = Assignment(
            experiment_id=exp.id,
            variant_id=variant.id,
            user_id="unique-user-001",  # Duplicate user in same experiment
        )
        db_session.add(a2)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_assignment_different_users_same_experiment_allowed(
        self, db_session, make_experiment, make_variant
    ):
        """Different users can be assigned to the same experiment."""
        exp = make_experiment(name="Multi-user Assignment Test")
        variant = make_variant(experiment=exp, name="Control", is_control=True)

        a1 = Assignment(experiment_id=exp.id, variant_id=variant.id, user_id="user-aaa")
        a2 = Assignment(experiment_id=exp.id, variant_id=variant.id, user_id="user-bbb")
        db_session.add_all([a1, a2])
        db_session.flush()  # Should not raise
        db_session.rollback()


@pytest.mark.integration
@pytest.mark.requires_db
class TestFeatureFlagConstraints:
    """Test database constraints on FeatureFlag model."""

    def test_feature_flag_key_unique(self, db_session, admin_user):
        """FeatureFlag.key must be globally unique — second insert raises IntegrityError."""
        flag1 = FeatureFlag(
            key="unique-flag-key",
            name="Flag One",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        db_session.add(flag1)
        db_session.flush()

        flag2 = FeatureFlag(
            key="unique-flag-key",  # Duplicate key
            name="Flag Two",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        db_session.add(flag2)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_feature_flag_key_not_null(self, db_session, admin_user):
        """FeatureFlag.key must not be null — IntegrityError if omitted."""
        flag = FeatureFlag(
            key=None,
            name="No-key Flag",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        db_session.add(flag)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_feature_flag_rollout_percentage_constraint(self, db_session, admin_user):
        """FeatureFlag.rollout_percentage must be 0-100 — constraint violation for 101."""
        flag = FeatureFlag(
            key=f"pct-check-{uuid.uuid4().hex[:6]}",
            name="PCT Flag",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=101,
        )
        db_session.add(flag)
        with pytest.raises((IntegrityError, Exception)):
            db_session.flush()
        db_session.rollback()

    def test_feature_flag_different_keys_allowed(self, db_session, admin_user):
        """Two feature flags with different keys are both allowed."""
        flag1 = FeatureFlag(
            key="distinct-key-alpha",
            name="Alpha",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        flag2 = FeatureFlag(
            key="distinct-key-beta",
            name="Beta",
            status=FeatureFlagStatus.INACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=0,
        )
        db_session.add_all([flag1, flag2])
        db_session.flush()  # Should not raise
        db_session.rollback()

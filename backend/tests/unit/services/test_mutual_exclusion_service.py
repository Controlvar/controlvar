"""Unit tests for MutualExclusionService (EP-022)."""

from unittest.mock import MagicMock, PropertyMock, call, patch
from uuid import UUID, uuid4

import pytest

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.services.mutual_exclusion_service import MutualExclusionService


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def service(mock_db):
    return MutualExclusionService(mock_db)


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------


def _no_other_group_has_the_name(mock_db):
    """The name check before a write (#1019) finds no other group."""
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None


class TestCreateGroup:
    def test_creates_group_with_defaults(self, service, mock_db):
        _no_other_group_has_the_name(mock_db)
        result = service.create_group(name="Test Group")
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once()
        added = mock_db.add.call_args[0][0]
        assert added.name == "Test Group"
        assert added.traffic_allocation == 1.0
        assert added.status == MutualExclusionGroupStatus.ACTIVE

    def test_creates_group_with_custom_fields(self, service, mock_db):
        _no_other_group_has_the_name(mock_db)
        owner = uuid4()
        service.create_group(
            name="Custom",
            description="A description",
            traffic_allocation=0.5,
            owner_id=owner,
        )
        added = mock_db.add.call_args[0][0]
        assert added.description == "A description"
        assert added.traffic_allocation == 0.5
        assert added.owner_id == owner


class TestGetGroup:
    def test_returns_group_when_found(self, service, mock_db):
        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_group
        result = service.get_group(uuid4())
        assert result is mock_group

    def test_returns_none_when_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.get_group(uuid4())
        assert result is None


class TestListGroups:
    def test_list_without_filter(self, service, mock_db):
        groups = [MagicMock(), MagicMock()]
        mock_db.query.return_value.offset.return_value.limit.return_value.all.return_value = groups
        result = service.list_groups()
        assert result == groups

    def test_list_with_status_filter(self, service, mock_db):
        groups = [MagicMock()]
        mock_db.query.return_value.filter.return_value.offset.return_value.limit.return_value.all.return_value = groups
        result = service.list_groups(status=MutualExclusionGroupStatus.ACTIVE)
        assert result == groups

    def test_list_with_pagination(self, service, mock_db):
        mock_db.query.return_value.offset.return_value.limit.return_value.all.return_value = []
        service.list_groups(skip=10, limit=5)
        mock_db.query.return_value.offset.assert_called_with(10)
        mock_db.query.return_value.offset.return_value.limit.assert_called_with(5)


class TestUpdateGroup:
    def test_returns_none_when_group_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.update_group(uuid4(), name="New Name")
        assert result is None

    def test_updates_name(self, service, mock_db):
        mock_group = MagicMock(spec=MutualExclusionGroup)
        _no_other_group_has_the_name(mock_db)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_group
        service.update_group(uuid4(), name="Updated")
        assert mock_group.name == "Updated"
        mock_db.commit.assert_called_once()

    def test_updates_traffic_allocation(self, service, mock_db):
        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_group
        service.update_group(uuid4(), traffic_allocation=0.75)
        assert mock_group.traffic_allocation == 0.75

    def test_updates_status(self, service, mock_db):
        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_group
        service.update_group(uuid4(), status="archived")
        assert mock_group.status == MutualExclusionGroupStatus.ARCHIVED


class TestArchiveGroup:
    def test_archive_sets_status_archived(self, service, mock_db):
        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_group
        service.archive_group(uuid4())
        assert mock_group.status == MutualExclusionGroupStatus.ARCHIVED
        mock_db.commit.assert_called_once()


# ------------------------------------------------------------------
# Experiment Management
# ------------------------------------------------------------------


class TestAddExperimentToGroup:
    def _setup_mocks(self, mock_db, group=None, experiment=None):
        """Helper to set up chained query mocks for group and experiment lookups."""
        # get_group is called first via self.db.query(MutualExclusionGroup)
        # then add_experiment_to_group calls self.db.query(Experiment)
        # We need to handle two separate query() calls
        call_count = [0]
        original_query = mock_db.query

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if model is MutualExclusionGroup or call_count[0] == 1:
                result.filter.return_value.first.return_value = group
            else:
                result.filter.return_value.first.return_value = experiment
            return result

        mock_db.query = MagicMock(side_effect=side_effect)

    def test_adds_experiment_to_group(self, service, mock_db):
        group_id = uuid4()
        exp_id = uuid4()
        mock_group = MagicMock(spec=MutualExclusionGroup, id=group_id)
        mock_exp = MagicMock(spec=Experiment, id=exp_id)
        mock_exp.mutual_exclusion_group_id = None

        self._setup_mocks(mock_db, group=mock_group, experiment=mock_exp)
        result = service.add_experiment_to_group(group_id, exp_id)
        assert result is mock_exp
        assert mock_exp.mutual_exclusion_group_id == group_id

    def test_raises_if_group_not_found(self, service, mock_db):
        self._setup_mocks(mock_db, group=None, experiment=MagicMock())
        with pytest.raises(ValueError, match="not found"):
            service.add_experiment_to_group(uuid4(), uuid4())

    def test_raises_if_experiment_not_found(self, service, mock_db):
        self._setup_mocks(mock_db, group=MagicMock(), experiment=None)
        with pytest.raises(ValueError, match="not found"):
            service.add_experiment_to_group(uuid4(), uuid4())

    def test_raises_if_experiment_in_another_group(self, service, mock_db):
        group_id = uuid4()
        other_group_id = uuid4()
        mock_group = MagicMock(spec=MutualExclusionGroup, id=group_id)
        mock_exp = MagicMock(spec=Experiment)
        mock_exp.mutual_exclusion_group_id = other_group_id

        self._setup_mocks(mock_db, group=mock_group, experiment=mock_exp)
        with pytest.raises(ValueError, match="already in another group"):
            service.add_experiment_to_group(group_id, uuid4())

    def test_returns_experiment_if_already_in_same_group(self, service, mock_db):
        group_id = uuid4()
        mock_group = MagicMock(spec=MutualExclusionGroup, id=group_id)
        mock_exp = MagicMock(spec=Experiment)
        mock_exp.mutual_exclusion_group_id = group_id

        self._setup_mocks(mock_db, group=mock_group, experiment=mock_exp)
        result = service.add_experiment_to_group(group_id, uuid4())
        assert result is mock_exp
        # Should not have committed since no change was made
        mock_db.commit.assert_not_called()


class TestRemoveExperimentFromGroup:
    def test_removes_experiment(self, service, mock_db):
        mock_exp = MagicMock(spec=Experiment)
        mock_exp.mutual_exclusion_group_id = uuid4()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_exp

        result = service.remove_experiment_from_group(uuid4(), uuid4())
        assert result is mock_exp
        assert mock_exp.mutual_exclusion_group_id is None
        mock_db.commit.assert_called_once()

    def test_raises_if_experiment_not_in_group(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="not found in group"):
            service.remove_experiment_from_group(uuid4(), uuid4())


# ------------------------------------------------------------------
# User-to-Experiment Selection
# ------------------------------------------------------------------


class TestSelectExperimentForUser:
    def _make_experiment(self, exp_id=None, status=ExperimentStatus.ACTIVE):
        exp = MagicMock(spec=Experiment)
        exp.id = exp_id or uuid4()
        exp.status = status
        return exp

    def _make_group(
        self,
        group_id=None,
        traffic_allocation=1.0,
        status=MutualExclusionGroupStatus.ACTIVE,
    ):
        group = MagicMock(spec=MutualExclusionGroup)
        group.id = group_id or uuid4()
        group.traffic_allocation = traffic_allocation
        group.status = status
        return group

    def test_returns_none_when_group_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.select_experiment_for_user("user1", uuid4())
        assert result is None

    def test_returns_none_when_group_is_archived(self, service, mock_db):
        group = self._make_group(status=MutualExclusionGroupStatus.ARCHIVED)
        mock_db.query.return_value.filter.return_value.first.return_value = group
        result = service.select_experiment_for_user("user1", group.id)
        assert result is None

    def test_returns_none_when_no_active_experiments(self, service, mock_db):
        group = self._make_group()
        # First query returns group, second query returns empty list
        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] <= 1:
                result.filter.return_value.first.return_value = group
            else:
                result.filter.return_value.all.return_value = []
            return result

        mock_db.query = MagicMock(side_effect=side_effect)
        result = service.select_experiment_for_user("user1", group.id)
        assert result is None

    @patch.object(MutualExclusionService, "_normalized_hash")
    def test_excluded_by_traffic_allocation(self, mock_hash, service, mock_db):
        """User with hash >= traffic_allocation should be excluded."""
        group = self._make_group(traffic_allocation=0.5)
        exp = self._make_experiment()
        # Hash of 0.8 is >= 0.5, so user is excluded
        mock_hash.return_value = 0.8

        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] <= 1:
                result.filter.return_value.first.return_value = group
            else:
                result.filter.return_value.all.return_value = [exp]
            return result

        mock_db.query = MagicMock(side_effect=side_effect)
        result = service.select_experiment_for_user("user1", group.id)
        assert result is None

    @patch.object(MutualExclusionService, "_normalized_hash")
    def test_selects_single_experiment(self, mock_hash, service, mock_db):
        """With one experiment and hash within traffic, selects that experiment."""
        group = self._make_group(traffic_allocation=1.0)
        exp = self._make_experiment()
        mock_hash.return_value = 0.3

        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] <= 1:
                result.filter.return_value.first.return_value = group
            else:
                result.filter.return_value.all.return_value = [exp]
            return result

        mock_db.query = MagicMock(side_effect=side_effect)
        result = service.select_experiment_for_user("user1", group.id)
        assert result == exp.id

    @patch.object(MutualExclusionService, "_normalized_hash")
    def test_even_distribution_among_experiments(self, mock_hash, service, mock_db):
        """With two experiments and traffic=1.0, hash < 0.5 goes to first, >= 0.5 to second."""
        group = self._make_group(traffic_allocation=1.0)
        # Create experiments with deterministic sorted IDs
        exp_a = self._make_experiment(
            exp_id=UUID("00000000-0000-0000-0000-000000000001")
        )
        exp_b = self._make_experiment(
            exp_id=UUID("00000000-0000-0000-0000-000000000002")
        )

        def setup_db(hash_val):
            mock_hash.return_value = hash_val
            call_count = [0]

            def side_effect(model):
                call_count[0] += 1
                result = MagicMock()
                if call_count[0] <= 1:
                    result.filter.return_value.first.return_value = group
                else:
                    result.filter.return_value.all.return_value = [exp_a, exp_b]
                return result

            mock_db.query = MagicMock(side_effect=side_effect)

        # Hash 0.2 should land in first experiment's slot (0.0 to 0.5)
        setup_db(0.2)
        result = service.select_experiment_for_user("user_low", group.id)
        assert result == exp_a.id

        # Hash 0.7 should land in second experiment's slot (0.5 to 1.0)
        setup_db(0.7)
        result = service.select_experiment_for_user("user_high", group.id)
        assert result == exp_b.id

    def test_deterministic_same_input(self, service, mock_db):
        """Same user_id and group_id always produce the same hash."""
        group_id = uuid4()
        hash1 = service._normalized_hash("user_abc", str(group_id))
        hash2 = service._normalized_hash("user_abc", str(group_id))
        assert hash1 == hash2

    def test_different_users_get_different_hashes(self, service, mock_db):
        """Different user_ids produce different hashes (with very high probability)."""
        salt = str(uuid4())
        hash1 = service._normalized_hash("user_1", salt)
        hash2 = service._normalized_hash("user_2", salt)
        assert hash1 != hash2

    def test_normalized_hash_range(self, service, mock_db):
        """Hash values should be in [0, 1)."""
        for i in range(200):
            h = service._normalized_hash(f"user_{i}", "some_salt")
            assert 0.0 <= h < 1.0


# ------------------------------------------------------------------
# Eligibility
# ------------------------------------------------------------------


class TestIsUserEligibleForExperiment:
    def test_eligible_when_no_group(self, service, mock_db):
        """Experiment without a mutual exclusion group => always eligible."""
        mock_exp = MagicMock(spec=Experiment)
        mock_exp.mutual_exclusion_group_id = None
        mock_db.query.return_value.filter.return_value.first.return_value = mock_exp
        result = service.is_user_eligible_for_experiment("user1", uuid4())
        assert result is True

    @patch.object(MutualExclusionService, "has_sibling_assignment", return_value=False)
    @patch.object(MutualExclusionService, "select_experiment_for_user")
    def test_eligible_when_selected(self, mock_select, mock_sibling, service, mock_db):
        """User is eligible if select_experiment_for_user returns this experiment
        and they hold no assignment in a live sibling."""
        exp_id = uuid4()
        group_id = uuid4()

        mock_exp = MagicMock(spec=Experiment)
        mock_exp.id = exp_id
        mock_exp.mutual_exclusion_group_id = group_id

        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_group.id = group_id

        # First query for experiment, second query for group
        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if model is Experiment or call_count[0] == 1:
                result.filter.return_value.first.return_value = mock_exp
            else:
                result.filter.return_value.first.return_value = mock_group
            return result

        mock_db.query = MagicMock(side_effect=side_effect)
        mock_select.return_value = exp_id

        result = service.is_user_eligible_for_experiment("user1", exp_id)
        assert result is True
        mock_sibling.assert_called_once_with("user1", group_id, exp_id)

    @patch.object(MutualExclusionService, "has_sibling_assignment", return_value=True)
    @patch.object(MutualExclusionService, "select_experiment_for_user")
    def test_not_eligible_when_enrolled_in_a_sibling(
        self, mock_select, mock_sibling, service, mock_db
    ):
        """Selected by the hash, but already enrolled in a live sibling (#446)."""
        exp_id = uuid4()
        group_id = uuid4()

        mock_exp = MagicMock(spec=Experiment)
        mock_exp.id = exp_id
        mock_exp.mutual_exclusion_group_id = group_id

        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_group.id = group_id

        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if model is Experiment or call_count[0] == 1:
                result.filter.return_value.first.return_value = mock_exp
            else:
                result.filter.return_value.first.return_value = mock_group
            return result

        mock_db.query = MagicMock(side_effect=side_effect)
        mock_select.return_value = exp_id

        assert service.is_user_eligible_for_experiment("user1", exp_id) is False
        mock_sibling.assert_called_once_with("user1", group_id, exp_id)

    @patch.object(MutualExclusionService, "has_sibling_assignment", return_value=False)
    @patch.object(MutualExclusionService, "select_experiment_for_user")
    def test_not_eligible_when_different_experiment_selected(
        self, mock_select, mock_sibling, service, mock_db
    ):
        """User is NOT eligible if select_experiment_for_user returns a different experiment."""
        exp_id = uuid4()
        other_exp_id = uuid4()
        group_id = uuid4()

        mock_exp = MagicMock(spec=Experiment)
        mock_exp.id = exp_id
        mock_exp.mutual_exclusion_group_id = group_id

        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_group.id = group_id

        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if model is Experiment or call_count[0] == 1:
                result.filter.return_value.first.return_value = mock_exp
            else:
                result.filter.return_value.first.return_value = mock_group
            return result

        mock_db.query = MagicMock(side_effect=side_effect)
        mock_select.return_value = other_exp_id

        result = service.is_user_eligible_for_experiment("user1", exp_id)
        assert result is False
        # The sibling lookup runs only after the hash has picked this experiment.
        mock_sibling.assert_not_called()

    @patch.object(MutualExclusionService, "has_sibling_assignment", return_value=False)
    @patch.object(MutualExclusionService, "select_experiment_for_user")
    def test_archived_group_imposes_no_constraint(
        self, mock_select, mock_sibling, service, mock_db
    ):
        """An archived (soft-deleted) group must not lock users out of its experiments."""
        exp_id = uuid4()
        group_id = uuid4()

        mock_exp = MagicMock(spec=Experiment)
        mock_exp.id = exp_id
        mock_exp.mutual_exclusion_group_id = group_id

        mock_group = MagicMock(spec=MutualExclusionGroup)
        mock_group.id = group_id
        mock_group.status = MutualExclusionGroupStatus.ARCHIVED

        call_count = [0]

        def side_effect(model):
            call_count[0] += 1
            result = MagicMock()
            if model is Experiment or call_count[0] == 1:
                result.filter.return_value.first.return_value = mock_exp
            else:
                result.filter.return_value.first.return_value = mock_group
            return result

        mock_db.query = MagicMock(side_effect=side_effect)

        assert service.is_user_eligible_for_experiment("user1", exp_id) is True
        mock_select.assert_not_called()
        mock_sibling.assert_not_called()

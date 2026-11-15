"""Unit tests for GlobalHoldoutService (EP-022)."""

from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

from backend.app.models.global_holdout import GlobalHoldout
from backend.app.services.global_holdout_service import (
    HOLDOUT_SALT,
    GlobalHoldoutService,
)


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def service(mock_db):
    return GlobalHoldoutService(mock_db)


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------


def _no_other_holdout_has_the_name(mock_db):
    """The name check before a write (#1019) finds no other holdout."""
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None


class TestCreateHoldout:
    def test_creates_holdout_with_defaults(self, service, mock_db):
        _no_other_holdout_has_the_name(mock_db)
        result = service.create_holdout(name="Main Holdout")
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once()
        added = mock_db.add.call_args[0][0]
        assert added.name == "Main Holdout"
        assert added.holdout_percentage == 10
        assert added.is_active is False

    def test_creates_holdout_with_custom_fields(self, service, mock_db):
        _no_other_holdout_has_the_name(mock_db)
        owner = uuid4()
        service.create_holdout(
            name="Custom Holdout",
            description="Custom description",
            holdout_percentage=5,
            is_active=True,
            owner_id=owner,
        )
        added = mock_db.add.call_args[0][0]
        assert added.description == "Custom description"
        assert added.holdout_percentage == 5
        assert added.is_active is True
        assert added.owner_id == owner


class TestGetHoldout:
    def test_returns_holdout_when_found(self, service, mock_db):
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        result = service.get_holdout(uuid4())
        assert result is mock_holdout

    def test_returns_none_when_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.get_holdout(uuid4())
        assert result is None


class TestGetActiveHoldout:
    def test_returns_active_holdout(self, service, mock_db):
        mock_holdout = MagicMock(spec=GlobalHoldout, is_active=True)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        result = service.get_active_holdout()
        assert result is mock_holdout

    def test_returns_none_when_no_active(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.get_active_holdout()
        assert result is None


class TestListHoldouts:
    def test_lists_all_holdouts(self, service, mock_db):
        holdouts = [MagicMock(), MagicMock()]
        mock_db.query.return_value.offset.return_value.limit.return_value.all.return_value = holdouts
        result = service.list_holdouts()
        assert result == holdouts

    def test_lists_with_pagination(self, service, mock_db):
        mock_db.query.return_value.offset.return_value.limit.return_value.all.return_value = []
        service.list_holdouts(skip=5, limit=10)
        mock_db.query.return_value.offset.assert_called_with(5)
        mock_db.query.return_value.offset.return_value.limit.assert_called_with(10)


class TestUpdateHoldout:
    def test_returns_none_when_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.update_holdout(uuid4(), name="New")
        assert result is None

    def test_updates_name(self, service, mock_db):
        mock_holdout = MagicMock(spec=GlobalHoldout)
        _no_other_holdout_has_the_name(mock_db)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        service.update_holdout(uuid4(), name="Updated")
        assert mock_holdout.name == "Updated"
        mock_db.commit.assert_called_once()

    def test_updates_holdout_percentage(self, service, mock_db):
        # Never active: the percentage can still change (#445).
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_holdout.is_active = False
        mock_holdout.activated_at = None
        mock_holdout.deactivated_at = None
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        service.update_holdout(uuid4(), holdout_percentage=15)
        assert mock_holdout.holdout_percentage == 15

    def test_updates_description(self, service, mock_db):
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        service.update_holdout(uuid4(), description="new desc")
        assert mock_holdout.description == "new desc"


# ------------------------------------------------------------------
# Activation / Deactivation
# ------------------------------------------------------------------


class TestActivateHoldout:
    def test_activating_deactivates_others(self, service, mock_db):
        """Activating a holdout should deactivate all other active holdouts first."""
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_holdout.is_active = False
        mock_holdout.activated_at = None
        mock_holdout.deactivated_at = None
        mock_holdout.hash_salt = "holdout:unit"
        other = MagicMock(spec=GlobalHoldout)
        other.is_active = True
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        mock_db.query.return_value.filter.return_value.all.return_value = [other]

        service.activate_holdout(uuid4())

        assert other.is_active is False
        assert other.deactivated_at is not None
        assert mock_holdout.is_active is True
        assert mock_holdout.activated_at == other.deactivated_at
        mock_db.flush.assert_called()
        mock_db.commit.assert_called()

    def test_activate_returns_none_when_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.activate_holdout(uuid4())
        assert result is None


class TestDeactivateHoldout:
    def test_deactivates_holdout(self, service, mock_db):
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_holdout.is_active = True
        mock_holdout.deactivated_at = None
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout
        service.deactivate_holdout(uuid4())
        assert mock_holdout.is_active is False
        assert mock_holdout.deactivated_at is not None
        mock_db.commit.assert_called()

    def test_deactivate_returns_none_when_not_found(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        result = service.deactivate_holdout(uuid4())
        assert result is None


# ------------------------------------------------------------------
# User Holdout Check
# ------------------------------------------------------------------


class TestIsUserInHoldout:
    def test_returns_false_when_no_active_holdout(self, service, mock_db):
        mock_db.query.return_value.filter.return_value.first.return_value = None
        is_in, pct, bucket = service.is_user_in_holdout("user_123")
        assert is_in is False
        assert pct == 0
        assert bucket == 0

    @patch("backend.app.services.global_holdout_service.holdout_bucket")
    def test_user_in_holdout(self, mock_bucket, service, mock_db):
        """User with bucket < holdout_percentage should be in holdout."""
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_holdout.holdout_percentage = 10
        mock_holdout.is_active = True
        mock_holdout.hash_salt = "holdout:own"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout

        mock_bucket.return_value = 5  # 5 < 10 => in holdout
        is_in, pct, bucket = service.is_user_in_holdout("user_123")
        # The holdout's own salt, not the legacy constant (#445).
        mock_bucket.assert_called_once_with("user_123", "holdout:own")
        assert is_in is True
        assert pct == 10
        assert bucket == 5

    @patch("backend.app.services.global_holdout_service.holdout_bucket")
    def test_user_not_in_holdout(self, mock_bucket, service, mock_db):
        """User with bucket >= holdout_percentage should NOT be in holdout."""
        mock_holdout = MagicMock(spec=GlobalHoldout)
        mock_holdout.holdout_percentage = 10
        mock_holdout.is_active = True
        mock_holdout.hash_salt = "holdout:own"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_holdout

        mock_bucket.return_value = 50  # 50 >= 10 => not in holdout
        is_in, pct, bucket = service.is_user_in_holdout("user_456")
        assert is_in is False
        assert pct == 10
        assert bucket == 50


class TestIsUserInHoldoutStatic:
    def test_user_in_holdout_static(self):
        """Static method: bucket < percentage => in holdout."""
        # Find a user that hashes to a low bucket
        bucket = GlobalHoldoutService._get_holdout_bucket_static("user_123")
        is_in, returned_bucket = GlobalHoldoutService.is_user_in_holdout_static(
            "user_123", 100
        )
        assert is_in is True
        assert returned_bucket == bucket

    def test_user_not_in_holdout_static(self):
        """Static method: bucket >= percentage => not in holdout."""
        is_in, bucket = GlobalHoldoutService.is_user_in_holdout_static("user_123", 0)
        assert is_in is False


# ------------------------------------------------------------------
# Determinism & Distribution
# ------------------------------------------------------------------


class TestDeterministicBucketing:
    def test_same_user_same_bucket(self):
        """Same user_id always produces the same bucket."""
        bucket1 = GlobalHoldoutService._get_holdout_bucket_static("user_123")
        bucket2 = GlobalHoldoutService._get_holdout_bucket_static("user_123")
        assert bucket1 == bucket2

    def test_different_users_can_get_different_buckets(self):
        """Different user_ids should (generally) produce different buckets."""
        buckets = set()
        for i in range(50):
            b = GlobalHoldoutService._get_holdout_bucket_static(f"user_{i}")
            buckets.add(b)
        # With 50 users, we should see more than 1 distinct bucket
        assert len(buckets) > 1

    def test_bucket_range(self):
        """All buckets should be in [0, 99]."""
        for i in range(200):
            bucket = GlobalHoldoutService._get_holdout_bucket_static(f"user_{i}")
            assert 0 <= bucket <= 99

    def test_uniform_distribution(self):
        """Buckets should be roughly uniformly distributed across 0-99."""
        bucket_counts = [0] * 100
        num_users = 10000
        for i in range(num_users):
            bucket = GlobalHoldoutService._get_holdout_bucket_static(f"test_user_{i}")
            bucket_counts[bucket] += 1

        # Each bucket should get roughly num_users/100 = 100 users
        expected = num_users / 100
        for count in bucket_counts:
            # Allow +/- 50% deviation (generous tolerance for hash uniformity)
            assert count > expected * 0.5, f"Bucket underrepresented: {count}"
            assert count < expected * 1.5, f"Bucket overrepresented: {count}"

    def test_holdout_salt_is_used(self):
        """Bucket computation uses the global holdout salt, not arbitrary salt."""
        import hashlib
        import struct

        user_id = "salt_test_user"
        combined = f"{user_id}:{HOLDOUT_SALT}".encode("utf-8")
        hash_bytes = hashlib.md5(combined).digest()[:4]
        hash_int = struct.unpack("<I", hash_bytes)[0]
        expected_bucket = hash_int % 100

        actual_bucket = GlobalHoldoutService._get_holdout_bucket_static(user_id)
        assert actual_bucket == expected_bucket


class TestCountHoldouts:
    def test_count_returns_value(self, service, mock_db):
        mock_db.query.return_value.count.return_value = 3
        result = service.count_holdouts()
        assert result == 3


class TestSaltIndependence:
    """Each holdout's own salt makes its membership independent of the last
    one's (#445): with one shared salt the next holdout would hold out the
    same users again."""

    def test_two_holdouts_overlap_as_independent_draws(self):
        """Two FIXED salts, so the overlap is one number and the test cannot
        fail by chance (#1025).

        With random salts the overlap is a random draw: over 600 random pairs
        it had mean 0.04002 and sd 0.00143 (binomial: 0.00139), so the
        0.034-0.046 bound was about 4.2 sd and a run failed about once in
        37,000. These two give 801 of 20,000 users, 0.04005. A holdout that
        ignored its salt would give 0.20, so the bound still bites.
        """
        from backend.app.services.global_holdout_service import holdout_bucket

        first, second = "holdout:" + "1" * 32, "holdout:" + "2" * 32
        users = [f"independence_{i}" for i in range(20000)]
        in_first = {u for u in users if holdout_bucket(u, first) < 20}
        in_second = {u for u in users if holdout_bucket(u, second) < 20}

        # Independent draws at 20% each overlap on about 4% of users
        # (sd 0.14 points at n=20000); one shared salt would overlap on 20%.
        overlap = len(in_first & in_second) / len(users)
        assert 0.034 < overlap < 0.046, overlap

    def test_new_salts_are_distinct_and_never_the_legacy_one(self):
        from backend.app.models.global_holdout import (
            LEGACY_HOLDOUT_SALT,
            new_holdout_salt,
        )

        salts = {new_holdout_salt() for _ in range(1000)}
        assert len(salts) == 1000
        assert LEGACY_HOLDOUT_SALT not in salts


class TestMeasurable:
    """A legacy-salt holdout is never measurable, whatever ``activated_at``
    says (PE C1): its buckets are every earlier holdout's buckets."""

    def test_legacy_salt_is_never_measurable(self):
        from datetime import datetime

        from backend.app.models.global_holdout import LEGACY_HOLDOUT_SALT

        stamped = datetime(2026, 10, 1)
        assert not GlobalHoldout(
            hash_salt=LEGACY_HOLDOUT_SALT, activated_at=stamped
        ).is_measurable
        assert not GlobalHoldout(hash_salt="holdout:x", activated_at=None).is_measurable
        assert GlobalHoldout(hash_salt="holdout:x", activated_at=stamped).is_measurable

    def test_activation_never_stamps_a_legacy_row(self, service, mock_db):
        from backend.app.models.global_holdout import LEGACY_HOLDOUT_SALT

        holdout = MagicMock(spec=GlobalHoldout)
        holdout.is_active = False
        holdout.activated_at = None
        holdout.deactivated_at = None
        holdout.hash_salt = LEGACY_HOLDOUT_SALT
        mock_db.query.return_value.filter.return_value.first.return_value = holdout
        mock_db.query.return_value.filter.return_value.all.return_value = []

        service.activate_holdout(uuid4())

        assert holdout.is_active is True
        assert holdout.activated_at is None

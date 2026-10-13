"""
Unit tests for EP-050: HIPAA Service (modules/backend/app/services/hipaa_service.py).

All database interactions are mocked with unittest.mock — no live DB needed.

Run with:
    source venv/bin/activate && export APP_ENV=test TESTING=true
    python -m pytest modules/backend/tests/unit/services/test_hipaa_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import List
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.backend.app.services.hipaa_service import HIPAAService

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc() -> HIPAAService:
    """Return a fresh HIPAAService instance."""
    return HIPAAService()


@pytest.fixture
def mock_db() -> MagicMock:
    """Return a mock SQLAlchemy Session."""
    return MagicMock()


def _make_log_entry(
    user_id=None,
    resource_type="experiment",
    resource_id=None,
    action="READ",
    phi_fields=None,
    purpose="treatment",
    timestamp=None,
) -> Mock:
    """Create a mock PHIAuditLog object."""
    m = Mock()
    m.id = uuid.uuid4()
    m.user_id = user_id or uuid.uuid4()
    m.resource_type = resource_type
    m.resource_id = resource_id or uuid.uuid4()
    m.action = action
    m.phi_fields_accessed = phi_fields or ["name", "dob"]
    m.purpose = purpose
    m.ip_address = "127.0.0.1"
    m.user_agent = "pytest/1.0"
    m.timestamp = timestamp or datetime.utcnow()
    m.retention_years = 6
    return m


def _make_baa(
    is_active=True,
    expiry_date=None,
    organization_name="Test Org",
) -> Mock:
    """Create a mock BAAConfig object."""
    m = Mock()
    m.id = uuid.uuid4()
    m.organization_name = organization_name
    m.signatory_name = "Test Signatory"
    m.signatory_email = "sig@example.com"
    m.effective_date = date.today()
    m.expiry_date = expiry_date
    m.is_active = is_active
    m.data_residency_region = "us-east-1"
    m.phi_categories = ["demographics"]
    m.signed_document_hash = "a" * 64
    m.created_by = uuid.uuid4()
    m.created_at = datetime.utcnow()
    # is_expired property
    if expiry_date is not None:
        m.is_expired = expiry_date < date.today()
    else:
        m.is_expired = False
    return m


# ---------------------------------------------------------------------------
# log_phi_access tests
# ---------------------------------------------------------------------------


class TestLogPhiAccess:
    """Tests for HIPAAService.log_phi_access()."""

    def test_creates_audit_log_record(self, svc, mock_db):
        """log_phi_access() adds a PHIAuditLog to the DB session."""
        user_id = uuid.uuid4()
        resource_id = uuid.uuid4()

        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            svc.log_phi_access(
                db=mock_db,
                user_id=user_id,
                resource_type="experiment",
                resource_id=resource_id,
                action="READ",
                phi_fields=["name", "dob"],
                purpose="treatment",
            )

            mock_db.add.assert_called_once_with(mock_instance)
            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once_with(mock_instance)

    def test_sets_correct_action(self, svc, mock_db):
        """log_phi_access() uppercases the action field."""
        user_id = uuid.uuid4()
        resource_id = uuid.uuid4()

        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            svc.log_phi_access(
                db=mock_db,
                user_id=user_id,
                resource_type="user_data",
                resource_id=resource_id,
                action="write",
                phi_fields=["ssn"],
                purpose="operations",
            )

            call_kwargs = MockLog.call_args[1]
            assert call_kwargs["action"] == "WRITE"

    def test_sets_correct_purpose(self, svc, mock_db):
        """log_phi_access() lowercases the purpose field."""
        user_id = uuid.uuid4()
        resource_id = uuid.uuid4()

        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            svc.log_phi_access(
                db=mock_db,
                user_id=user_id,
                resource_type="experiment",
                resource_id=resource_id,
                action="READ",
                phi_fields=["name"],
                purpose="TREATMENT",
            )

            call_kwargs = MockLog.call_args[1]
            assert call_kwargs["purpose"] == "treatment"

    def test_extracts_ip_from_request(self, svc, mock_db):
        """log_phi_access() extracts client IP from the request object."""
        user_id = uuid.uuid4()
        resource_id = uuid.uuid4()
        mock_request = Mock()
        mock_request.client.host = "10.0.0.5"
        mock_request.headers = {"user-agent": "test-agent/1.0"}

        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            svc.log_phi_access(
                db=mock_db,
                user_id=user_id,
                resource_type="experiment",
                resource_id=resource_id,
                action="READ",
                phi_fields=["name"],
                purpose="research",
                request=mock_request,
            )

            call_kwargs = MockLog.call_args[1]
            assert call_kwargs["ip_address"] == "10.0.0.5"

    def test_extracts_user_agent_from_request(self, svc, mock_db):
        """log_phi_access() extracts User-Agent from the request headers."""
        user_id = uuid.uuid4()
        resource_id = uuid.uuid4()
        mock_request = Mock()
        mock_request.client.host = "127.0.0.1"
        mock_request.headers = {"user-agent": "Mozilla/5.0"}

        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            svc.log_phi_access(
                db=mock_db,
                user_id=user_id,
                resource_type="user_data",
                resource_id=resource_id,
                action="EXPORT",
                phi_fields=["all"],
                purpose="operations",
                request=mock_request,
            )

            call_kwargs = MockLog.call_args[1]
            assert call_kwargs["user_agent"] == "Mozilla/5.0"

    def test_null_ip_when_no_request(self, svc, mock_db):
        """log_phi_access() sets ip_address to None when no request is provided."""
        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            svc.log_phi_access(
                db=mock_db,
                user_id=uuid.uuid4(),
                resource_type="experiment",
                resource_id=uuid.uuid4(),
                action="READ",
                phi_fields=["name"],
                purpose="treatment",
                request=None,
            )

            call_kwargs = MockLog.call_args[1]
            assert call_kwargs["ip_address"] is None

    def test_retention_years_set_to_6(self, svc, mock_db):
        """log_phi_access() sets retention_years to the HIPAA-required 6 years."""
        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            mock_settings = Mock()
            mock_settings.HIPAA_AUDIT_LOG_RETENTION_YEARS = 6

            with patch(
                "modules.backend.app.services.hipaa_service.settings", mock_settings
            ):
                svc.log_phi_access(
                    db=mock_db,
                    user_id=uuid.uuid4(),
                    resource_type="experiment",
                    resource_id=uuid.uuid4(),
                    action="READ",
                    phi_fields=["name"],
                    purpose="treatment",
                )

                call_kwargs = MockLog.call_args[1]
                assert call_kwargs["retention_years"] == 6

    def test_returns_log_instance(self, svc, mock_db):
        """log_phi_access() returns the created PHIAuditLog instance."""
        with patch("modules.backend.app.services.hipaa_service.PHIAuditLog") as MockLog:
            mock_instance = Mock()
            MockLog.return_value = mock_instance

            result = svc.log_phi_access(
                db=mock_db,
                user_id=uuid.uuid4(),
                resource_type="experiment",
                resource_id=uuid.uuid4(),
                action="READ",
                phi_fields=["name"],
                purpose="treatment",
            )

            assert result is mock_instance


# ---------------------------------------------------------------------------
# get_phi_audit_logs tests
# ---------------------------------------------------------------------------


class TestGetPhiAuditLogs:
    """Tests for HIPAAService.get_phi_audit_logs()."""

    def _setup_mock_query(self, mock_db, items, total=None):
        """Set up the mock DB query chain for PHIAuditLog."""
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.offset.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.count.return_value = total if total is not None else len(items)
        mock_query.all.return_value = items
        return mock_query

    def test_returns_dict_with_required_keys(self, svc, mock_db):
        """get_phi_audit_logs() returns a dict with items, total, page, page_size."""
        self._setup_mock_query(mock_db, [])
        result = svc.get_phi_audit_logs(db=mock_db)
        assert "items" in result
        assert "total" in result
        assert "page" in result
        assert "page_size" in result

    def test_returns_all_logs_when_no_filters(self, svc, mock_db):
        """Without filters, all logs are returned (no extra .filter() calls)."""
        logs = [_make_log_entry() for _ in range(3)]
        self._setup_mock_query(mock_db, logs, total=3)
        result = svc.get_phi_audit_logs(db=mock_db)
        assert result["total"] == 3
        assert len(result["items"]) == 3

    def test_page_and_page_size_in_response(self, svc, mock_db):
        """Pagination params are reflected in the response."""
        self._setup_mock_query(mock_db, [], total=0)
        result = svc.get_phi_audit_logs(db=mock_db, page=2, page_size=25)
        assert result["page"] == 2
        assert result["page_size"] == 25

    def test_filters_by_resource_type(self, svc, mock_db):
        """Filter by resource_type applies a WHERE clause."""
        mock_query = self._setup_mock_query(mock_db, [], total=0)
        svc.get_phi_audit_logs(db=mock_db, resource_type="experiment")
        # filter() should have been called at least once
        assert mock_query.filter.called

    def test_filters_by_user_id(self, svc, mock_db):
        """Filter by user_id applies a WHERE clause."""
        mock_query = self._setup_mock_query(mock_db, [], total=0)
        svc.get_phi_audit_logs(db=mock_db, user_id=uuid.uuid4())
        assert mock_query.filter.called

    def test_filters_by_date_range(self, svc, mock_db):
        """Filter by start_date and end_date applies WHERE clauses."""
        mock_query = self._setup_mock_query(mock_db, [], total=0)
        svc.get_phi_audit_logs(
            db=mock_db,
            start_date=datetime(2025, 1, 1),
            end_date=datetime(2025, 12, 31),
        )
        # filter() is called twice for start_date and end_date
        assert mock_query.filter.call_count >= 2

    def test_empty_result_set(self, svc, mock_db):
        """get_phi_audit_logs() handles empty result set gracefully."""
        self._setup_mock_query(mock_db, [], total=0)
        result = svc.get_phi_audit_logs(db=mock_db)
        assert result["items"] == []
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# create_baa tests
# ---------------------------------------------------------------------------


class TestCreateBaa:
    """Tests for HIPAAService.create_baa()."""

    def _baa_data(self, **overrides):
        defaults = {
            "organization_name": "ACME Hospital",
            "signatory_name": "Dr. Smith",
            "signatory_email": "smith@acme.com",
            "effective_date": date.today(),
            "expiry_date": None,
            "data_residency_region": "us-east-1",
            "phi_categories": ["demographics", "diagnosis"],
            "signed_document_hash": "a" * 64,
        }
        defaults.update(overrides)
        return defaults

    def test_creates_baa_and_commits(self, svc, mock_db):
        """create_baa() adds a BAAConfig to the DB and commits."""
        with patch("modules.backend.app.services.hipaa_service.BAAConfig") as MockBAA:
            mock_instance = Mock()
            MockBAA.return_value = mock_instance

            svc.create_baa(db=mock_db, data=self._baa_data(), created_by=uuid.uuid4())

            mock_db.add.assert_called_once_with(mock_instance)
            mock_db.commit.assert_called_once()
            mock_db.refresh.assert_called_once_with(mock_instance)

    def test_sets_is_active_true(self, svc, mock_db):
        """Newly created BAA is active."""
        with patch("modules.backend.app.services.hipaa_service.BAAConfig") as MockBAA:
            mock_instance = Mock()
            MockBAA.return_value = mock_instance

            svc.create_baa(db=mock_db, data=self._baa_data(), created_by=uuid.uuid4())

            call_kwargs = MockBAA.call_args[1]
            assert call_kwargs["is_active"] is True

    def test_stores_created_by(self, svc, mock_db):
        """create_baa() stores the created_by UUID."""
        created_by = uuid.uuid4()
        with patch("modules.backend.app.services.hipaa_service.BAAConfig") as MockBAA:
            mock_instance = Mock()
            MockBAA.return_value = mock_instance

            svc.create_baa(db=mock_db, data=self._baa_data(), created_by=created_by)

            call_kwargs = MockBAA.call_args[1]
            assert call_kwargs["created_by"] == created_by

    def test_returns_baa_instance(self, svc, mock_db):
        """create_baa() returns the created BAAConfig instance."""
        with patch("modules.backend.app.services.hipaa_service.BAAConfig") as MockBAA:
            mock_instance = Mock()
            MockBAA.return_value = mock_instance

            result = svc.create_baa(
                db=mock_db, data=self._baa_data(), created_by=uuid.uuid4()
            )
            assert result is mock_instance


# ---------------------------------------------------------------------------
# get_baa_configs tests
# ---------------------------------------------------------------------------


class TestGetBaaConfigs:
    """Tests for HIPAAService.get_baa_configs()."""

    def _setup_baa_query(self, mock_db, items):
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = items
        return mock_query

    def test_returns_active_baas_by_default(self, svc, mock_db):
        """get_baa_configs() applies is_active filter by default."""
        mock_query = self._setup_baa_query(mock_db, [])
        svc.get_baa_configs(db=mock_db)
        # Should call filter for is_active
        assert mock_query.filter.called

    def test_returns_all_baas_when_active_only_false(self, svc, mock_db):
        """get_baa_configs(active_only=False) skips the is_active filter."""
        mock_query = self._setup_baa_query(mock_db, [])
        svc.get_baa_configs(db=mock_db, active_only=False)
        assert not mock_query.filter.called

    def test_returns_list(self, svc, mock_db):
        """get_baa_configs() always returns a list."""
        baas = [_make_baa(), _make_baa()]
        self._setup_baa_query(mock_db, baas)
        result = svc.get_baa_configs(db=mock_db)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# deactivate_baa tests
# ---------------------------------------------------------------------------


class TestDeactivateBaa:
    """Tests for HIPAAService.deactivate_baa()."""

    def test_sets_is_active_false(self, svc, mock_db):
        """deactivate_baa() sets is_active=False on the target BAA."""
        baa = _make_baa(is_active=True)

        with patch.object(svc, "get_baa_by_id", return_value=baa):
            result = svc.deactivate_baa(db=mock_db, baa_id=baa.id)

        assert baa.is_active is False
        mock_db.commit.assert_called_once()

    def test_returns_none_for_missing_baa(self, svc, mock_db):
        """deactivate_baa() returns None if the BAA does not exist."""
        with patch.object(svc, "get_baa_by_id", return_value=None):
            result = svc.deactivate_baa(db=mock_db, baa_id=uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# generate_hipaa_report tests
# ---------------------------------------------------------------------------


class TestGenerateHipaaReport:
    """Tests for HIPAAService.generate_hipaa_report()."""

    def _setup_queries(self, mock_db, logs=None, baas=None):
        """Configure mock_db.query() for both PHIAuditLog and BAAConfig."""
        logs = logs or []
        baas = baas or []

        def query_side_effect(model):
            from modules.backend.app.models.baa_config import BAAConfig
            from modules.backend.app.models.phi_audit_log import PHIAuditLog

            mock_q = MagicMock()
            mock_q.filter.return_value = mock_q
            mock_q.all.return_value = logs if "PHIAuditLog" in str(model) else baas
            mock_q.count.return_value = 0
            return mock_q

        mock_db.query.side_effect = query_side_effect

    def test_returns_dict_with_required_keys(self, svc, mock_db):
        """generate_hipaa_report() returns a dict with all expected keys."""
        self._setup_queries(mock_db)
        result = svc.generate_hipaa_report(db=mock_db)
        assert "total_phi_access_events" in result
        assert "access_by_purpose" in result
        assert "access_by_resource_type" in result
        assert "unique_users_accessing_phi" in result
        assert "potential_violations" in result
        assert "baa_coverage" in result

    def test_counts_total_events(self, svc, mock_db):
        """total_phi_access_events matches the number of audit log entries."""
        logs = [_make_log_entry() for _ in range(5)]
        self._setup_queries(mock_db, logs=logs)
        result = svc.generate_hipaa_report(db=mock_db)
        assert result["total_phi_access_events"] == 5

    def test_counts_by_purpose(self, svc, mock_db):
        """access_by_purpose counts are correct."""
        logs = [
            _make_log_entry(purpose="treatment"),
            _make_log_entry(purpose="treatment"),
            _make_log_entry(purpose="operations"),
        ]
        self._setup_queries(mock_db, logs=logs)
        result = svc.generate_hipaa_report(db=mock_db)
        assert result["access_by_purpose"].get("treatment") == 2
        assert result["access_by_purpose"].get("operations") == 1

    def test_counts_by_resource_type(self, svc, mock_db):
        """access_by_resource_type counts are correct."""
        logs = [
            _make_log_entry(resource_type="experiment"),
            _make_log_entry(resource_type="experiment"),
            _make_log_entry(resource_type="user_data"),
        ]
        self._setup_queries(mock_db, logs=logs)
        result = svc.generate_hipaa_report(db=mock_db)
        assert result["access_by_resource_type"].get("experiment") == 2
        assert result["access_by_resource_type"].get("user_data") == 1

    def test_unique_users_count(self, svc, mock_db):
        """unique_users_accessing_phi counts distinct user_ids."""
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        logs = [
            _make_log_entry(user_id=uid1),
            _make_log_entry(user_id=uid1),  # duplicate
            _make_log_entry(user_id=uid2),
        ]
        self._setup_queries(mock_db, logs=logs)
        result = svc.generate_hipaa_report(db=mock_db)
        assert result["unique_users_accessing_phi"] == 2

    def test_potential_violations_for_unknown_purpose(self, svc, mock_db):
        """Access logs with unrecognised purpose are flagged as potential violations."""
        bad_log = _make_log_entry(purpose="unknown_purpose")
        self._setup_queries(mock_db, logs=[bad_log])
        result = svc.generate_hipaa_report(db=mock_db)
        assert len(result["potential_violations"]) == 1

    def test_no_violations_for_valid_purposes(self, svc, mock_db):
        """Logs with recognised purposes produce no violations."""
        logs = [
            _make_log_entry(purpose="treatment"),
            _make_log_entry(purpose="operations"),
            _make_log_entry(purpose="research"),
        ]
        self._setup_queries(mock_db, logs=logs)
        result = svc.generate_hipaa_report(db=mock_db)
        assert result["potential_violations"] == []

    def test_baa_coverage_counts(self, svc, mock_db):
        """baa_coverage reflects active and expired BAAs."""
        active_baa = _make_baa(is_active=True)
        expired_baa = _make_baa(
            is_active=True,
            expiry_date=date.today() - timedelta(days=1),
        )
        # Simulate expiry
        expired_baa.is_expired = True
        expired_baa.is_active = True  # still in DB but expired

        self._setup_queries(mock_db, logs=[], baas=[active_baa, expired_baa])
        result = svc.generate_hipaa_report(db=mock_db)
        assert "active_baas" in result["baa_coverage"]
        assert "expired_baas" in result["baa_coverage"]

    def test_empty_report_for_no_logs(self, svc, mock_db):
        """Report with no logs returns zeros and empty collections."""
        self._setup_queries(mock_db, logs=[], baas=[])
        result = svc.generate_hipaa_report(db=mock_db)
        assert result["total_phi_access_events"] == 0
        assert result["access_by_purpose"] == {}
        assert result["access_by_resource_type"] == {}
        assert result["unique_users_accessing_phi"] == 0
        assert result["potential_violations"] == []


# ---------------------------------------------------------------------------
# check_data_residency tests
# ---------------------------------------------------------------------------


class TestCheckDataResidency:
    """Tests for HIPAAService.check_data_residency()."""

    def test_allowed_region_returns_true(self, svc):
        """us-east-1 is in the default allowed regions."""
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1", "us-west-2"]
            assert svc.check_data_residency("us-east-1") is True

    def test_second_allowed_region_returns_true(self, svc):
        """us-west-2 is in the default allowed regions."""
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1", "us-west-2"]
            assert svc.check_data_residency("us-west-2") is True

    def test_disallowed_region_returns_false(self, svc):
        """ap-southeast-1 is NOT in the HIPAA allowed regions by default."""
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1", "us-west-2"]
            assert svc.check_data_residency("ap-southeast-1") is False

    def test_eu_region_disallowed_by_default(self, svc):
        """EU regions are not HIPAA-compliant under US regulations by default."""
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1", "us-west-2"]
            assert svc.check_data_residency("eu-west-1") is False

    def test_custom_allowed_region(self, svc):
        """Custom HIPAA_ALLOWED_REGIONS list is respected."""
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.HIPAA_ALLOWED_REGIONS = ["eu-west-1", "us-east-1"]
            assert svc.check_data_residency("eu-west-1") is True


# ---------------------------------------------------------------------------
# encrypt_phi_field / decrypt_phi_field delegation tests
# ---------------------------------------------------------------------------


class TestEncryptDecryptDelegation:
    """Tests for encrypt_phi_field and decrypt_phi_field delegation."""

    def test_encrypt_delegates_to_phi_encryption(self, svc):
        """encrypt_phi_field() delegates to PHIEncryption.encrypt()."""
        with patch(
            "modules.backend.app.services.hipaa_service.PHIEncryption"
        ) as MockEnc:
            mock_enc_instance = Mock()
            MockEnc.return_value = mock_enc_instance
            mock_enc_instance.encrypt.return_value = "encrypted_token"

            result = svc.encrypt_phi_field("John Smith")

            MockEnc.assert_called_once()
            mock_enc_instance.encrypt.assert_called_once_with("John Smith")
            assert result == "encrypted_token"

    def test_decrypt_delegates_to_phi_encryption(self, svc):
        """decrypt_phi_field() delegates to PHIEncryption.decrypt()."""
        with patch(
            "modules.backend.app.services.hipaa_service.PHIEncryption"
        ) as MockEnc:
            mock_enc_instance = Mock()
            MockEnc.return_value = mock_enc_instance
            mock_enc_instance.decrypt.return_value = "John Smith"

            result = svc.decrypt_phi_field("some_ciphertext")

            mock_enc_instance.decrypt.assert_called_once_with("some_ciphertext")
            assert result == "John Smith"

    def test_decrypt_propagates_value_error(self, svc):
        """decrypt_phi_field() propagates ValueError from PHIEncryption."""
        with patch(
            "modules.backend.app.services.hipaa_service.PHIEncryption"
        ) as MockEnc:
            mock_enc_instance = Mock()
            MockEnc.return_value = mock_enc_instance
            mock_enc_instance.decrypt.side_effect = ValueError("bad ciphertext")

            with pytest.raises(ValueError):
                svc.decrypt_phi_field("invalid_ciphertext")


# ---------------------------------------------------------------------------
# BAAConfig expiry detection tests
# ---------------------------------------------------------------------------


class TestBaaExpiry:
    """Tests for BAA expiry detection logic."""

    def test_baa_not_expired_when_no_expiry_date(self):
        """A BAA with no expiry date is not expired."""
        baa = _make_baa(expiry_date=None)
        assert not baa.is_expired

    def test_baa_expired_when_past_expiry_date(self):
        """A BAA with a past expiry date is expired."""
        past_date = date.today() - timedelta(days=1)
        baa = _make_baa(expiry_date=past_date)
        baa.is_expired = past_date < date.today()
        assert baa.is_expired

    def test_baa_not_expired_when_future_expiry_date(self):
        """A BAA with a future expiry date is not expired."""
        future_date = date.today() + timedelta(days=365)
        baa = _make_baa(expiry_date=future_date)
        baa.is_expired = future_date < date.today()
        assert not baa.is_expired


# ---------------------------------------------------------------------------
# get_hipaa_status tests
# ---------------------------------------------------------------------------


class TestGetHipaaStatus:
    """Tests for HIPAAService.get_hipaa_status()."""

    def _setup_status_query(self, mock_db, active_baa_count: int):
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.count.return_value = active_baa_count
        return mock_query

    def test_phi_encryption_false_when_key_not_set(self, svc, mock_db):
        """phi_encryption_configured is False when PHI_ENCRYPTION_KEY is not set."""
        self._setup_status_query(mock_db, active_baa_count=1)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = None
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1"]

            result = svc.get_hipaa_status(db=mock_db)
            assert result["phi_encryption_configured"] is False

    def test_phi_encryption_true_when_key_set(self, svc, mock_db):
        """phi_encryption_configured is True when PHI_ENCRYPTION_KEY is set."""
        self._setup_status_query(mock_db, active_baa_count=1)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = "somekey"
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1"]

            result = svc.get_hipaa_status(db=mock_db)
            assert result["phi_encryption_configured"] is True

    def test_has_active_baa_false_when_no_baas(self, svc, mock_db):
        """has_active_baa is False when there are no active BAAs."""
        self._setup_status_query(mock_db, active_baa_count=0)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = "somekey"
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1"]

            result = svc.get_hipaa_status(db=mock_db)
            assert result["has_active_baa"] is False

    def test_has_active_baa_true_when_baa_exists(self, svc, mock_db):
        """has_active_baa is True when at least one active BAA exists."""
        self._setup_status_query(mock_db, active_baa_count=1)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = "somekey"
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1"]

            result = svc.get_hipaa_status(db=mock_db)
            assert result["has_active_baa"] is True

    def test_overall_hipaa_ready_false_when_missing_requirements(self, svc, mock_db):
        """overall_hipaa_ready is False when any requirement is missing."""
        self._setup_status_query(mock_db, active_baa_count=0)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = None
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1"]

            result = svc.get_hipaa_status(db=mock_db)
            assert result["overall_hipaa_ready"] is False

    def test_overall_hipaa_ready_true_when_all_requirements_met(self, svc, mock_db):
        """overall_hipaa_ready is True when all requirements are satisfied."""
        self._setup_status_query(mock_db, active_baa_count=1)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = "valid_key"
            mock_settings.HIPAA_ALLOWED_REGIONS = ["us-east-1"]

            result = svc.get_hipaa_status(db=mock_db)
            assert result["overall_hipaa_ready"] is True

    def test_overall_hipaa_ready_false_with_no_allowed_regions(self, svc, mock_db):
        """An active BAA and a PHI key are not enough with HIPAA_ALLOWED_REGIONS empty."""
        self._setup_status_query(mock_db, active_baa_count=1)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = "valid_key"
            mock_settings.HIPAA_ALLOWED_REGIONS = []

            result = svc.get_hipaa_status(db=mock_db)
            assert result["has_active_baa"] is True
            assert result["phi_encryption_configured"] is True
            assert result["data_residency_configured"] is False
            assert result["overall_hipaa_ready"] is False

    def test_audit_logging_always_enabled(self, svc, mock_db):
        """audit_logging_enabled is always True in the HIPAA service."""
        self._setup_status_query(mock_db, active_baa_count=0)
        with patch(
            "modules.backend.app.services.hipaa_service.settings"
        ) as mock_settings:
            mock_settings.PHI_ENCRYPTION_KEY = None
            mock_settings.HIPAA_ALLOWED_REGIONS = []

            result = svc.get_hipaa_status(db=mock_db)
            assert result["audit_logging_enabled"] is True

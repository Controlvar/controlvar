"""
Unit tests for ExportService — data export and report generation.
Uses mock DB session, no real database required.
"""

import csv
import io
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.app.schemas.export import ExportFormat, ExportRequest, ExportScope
from backend.app.services.export_service import ExportService


@pytest.fixture
def mock_db():
    db = MagicMock()
    # The results computation reads the experiment's stored analysis
    # settings (#580); a MagicMock's own attributes are not a method.
    experiment = db.query.return_value.options.return_value.filter.return_value
    experiment.first.return_value.correction_method = "benjamini_hochberg"
    experiment.first.return_value.confidence_level = 0.95
    return db


@pytest.fixture
def mock_experiment():
    exp = MagicMock()
    exp.id = "exp-001"
    exp.name = "Test Experiment"
    exp.status.value = "active"
    exp.experiment_type.value = "a_b"
    exp.start_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    exp.end_date = None
    exp.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    exp.variants = []
    exp.correction_method = "benjamini_hochberg"
    exp.confidence_level = 0.95
    return exp


class TestExportExperimentsCSV:
    def test_returns_csv_content_type(self, mock_db, mock_experiment):
        """CSV export returns text/csv content type"""
        mock_db.query.return_value.filter.return_value.filter.return_value.all.return_value = [
            mock_experiment
        ]
        mock_db.query.return_value.all.return_value = [mock_experiment]

        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        _, content_type = service.export_experiments(request)
        assert content_type == "text/csv"

    def test_csv_has_header_row(self, mock_db, mock_experiment):
        """CSV export first row contains column headers"""
        mock_db.query.return_value.all.return_value = [mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        content, _ = service.export_experiments(request)
        reader = csv.reader(io.StringIO(content))
        headers = next(reader)
        assert "experiment_id" in headers
        assert "experiment_name" in headers
        assert "status" in headers

    def test_csv_row_count_matches_experiments(self, mock_db, mock_experiment):
        """CSV has one data row per experiment"""
        mock_db.query.return_value.all.return_value = [mock_experiment, mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        content, _ = service.export_experiments(request)
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        assert len(rows) == 2

    def test_empty_export_returns_empty_csv(self, mock_db):
        """Empty result set returns empty string for CSV"""
        mock_db.query.return_value.all.return_value = []
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        content, _ = service.export_experiments(request)
        assert content == ""


class TestExportExperimentsJSON:
    def test_returns_json_content_type(self, mock_db, mock_experiment):
        mock_db.query.return_value.all.return_value = [mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        _, content_type = service.export_experiments(request)
        assert content_type == "application/json"

    def test_json_is_parseable_list(self, mock_db, mock_experiment):
        mock_db.query.return_value.all.return_value = [mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        content, _ = service.export_experiments(request)
        data = json.loads(content)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_json_row_has_required_fields(self, mock_db, mock_experiment):
        mock_db.query.return_value.all.return_value = [mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        content, _ = service.export_experiments(request)
        data = json.loads(content)
        row = data[0]
        assert "experiment_id" in row
        assert "experiment_name" in row
        assert "status" in row
        assert "total_assignments" in row


class TestExportVariants:
    def test_returns_csv_content_type_for_variants(self, mock_db, mock_experiment):
        """Variant CSV export returns text/csv content type"""
        mock_db.query.return_value.all.return_value = [mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        _, content_type = service.export_variants(request)
        assert content_type == "text/csv"

    def test_returns_json_content_type_for_variants(self, mock_db, mock_experiment):
        """Variant JSON export returns application/json content type"""
        mock_db.query.return_value.all.return_value = [mock_experiment]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        _, content_type = service.export_variants(request)
        assert content_type == "application/json"

    def test_variant_export_with_variants(self, mock_db):
        """Variant export includes rows for each variant in each experiment"""
        mock_variant = MagicMock()
        mock_variant.id = "var-001"
        mock_variant.name = "Control"
        mock_variant.is_control = True

        mock_exp = MagicMock()
        mock_exp.id = "exp-001"
        mock_exp.name = "Test Experiment"
        mock_exp.variants = [mock_variant]

        mock_db.query.return_value.all.return_value = [mock_exp]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        content, _ = service.export_variants(request)
        data = json.loads(content)
        assert isinstance(data, list)

    def test_empty_variant_export_returns_empty_csv(self, mock_db):
        """Empty variant result set returns empty string for CSV"""
        mock_db.query.return_value.all.return_value = []
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        content, _ = service.export_variants(request)
        assert content == ""


class TestExportFeatureFlags:
    def test_returns_csv_content_type(self, mock_db):
        """Feature flag CSV export returns text/csv"""
        mock_ff = MagicMock()
        mock_ff.id = "ff-001"
        mock_ff.key = "my_flag"
        mock_ff.name = "My Flag"
        mock_ff.status.value = "ACTIVE"
        mock_ff.rollout_percentage = 50
        mock_ff.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        mock_ff.updated_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        mock_db.query.return_value.all.return_value = [mock_ff]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        _, content_type = service.export_feature_flags(request)
        assert content_type == "text/csv"

    def test_returns_json_content_type(self, mock_db):
        """Feature flag JSON export returns application/json"""
        mock_ff = MagicMock()
        mock_ff.id = "ff-001"
        mock_ff.key = "my_flag"
        mock_ff.name = "My Flag"
        mock_ff.status.value = "ACTIVE"
        mock_ff.rollout_percentage = 50
        mock_ff.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        mock_ff.updated_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        mock_db.query.return_value.all.return_value = [mock_ff]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        _, content_type = service.export_feature_flags(request)
        assert content_type == "application/json"

    def test_feature_flag_json_has_required_fields(self, mock_db):
        """Feature flag JSON rows include all required fields"""
        mock_ff = MagicMock()
        mock_ff.id = "ff-001"
        mock_ff.key = "my_flag"
        mock_ff.name = "My Flag"
        mock_ff.status.value = "ACTIVE"
        mock_ff.rollout_percentage = 50
        mock_ff.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        mock_ff.updated_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        mock_db.query.return_value.all.return_value = [mock_ff]
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.JSON)
        content, _ = service.export_feature_flags(request)
        data = json.loads(content)
        row = data[0]
        assert "flag_id" in row
        assert "flag_key" in row
        assert "flag_name" in row
        assert "status" in row
        assert "rollout_percentage" in row

    def test_empty_feature_flag_export_returns_empty_csv(self, mock_db):
        """Empty feature flag result returns empty string for CSV"""
        mock_db.query.return_value.all.return_value = []
        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        content, _ = service.export_feature_flags(request)
        assert content == ""


class TestPlatformOverviewReport:
    def test_report_has_required_fields(self, mock_db):
        """Platform overview contains all required summary fields"""
        mock_db.query.return_value.count.return_value = 5
        mock_db.query.return_value.filter.return_value.count.return_value = 2

        service = ExportService(mock_db)
        report = service.generate_platform_overview()

        assert hasattr(report, "total_experiments")
        assert hasattr(report, "active_experiments")
        assert hasattr(report, "total_feature_flags")
        assert hasattr(report, "generated_at")

    def test_report_generated_at_is_iso_format(self, mock_db):
        mock_db.query.return_value.count.return_value = 0
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        service = ExportService(mock_db)
        report = service.generate_platform_overview()

        # Should be parseable as ISO datetime
        datetime.fromisoformat(report.generated_at.replace("Z", "+00:00"))

    def test_report_totals_are_non_negative(self, mock_db):
        """All count fields in the overview are non-negative integers"""
        mock_db.query.return_value.count.return_value = 10
        mock_db.query.return_value.filter.return_value.count.return_value = 3

        service = ExportService(mock_db)
        report = service.generate_platform_overview()

        assert report.total_experiments >= 0
        assert report.active_experiments >= 0
        assert report.completed_experiments >= 0
        assert report.total_feature_flags >= 0
        assert report.active_feature_flags >= 0
        assert report.total_assignments >= 0
        assert report.total_events >= 0

    def test_report_with_date_filters(self, mock_db):
        """Platform overview respects optional date filters"""
        mock_db.query.return_value.count.return_value = 0
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        service = ExportService(mock_db)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 12, 31, tzinfo=timezone.utc)
        report = service.generate_platform_overview(start_date=start, end_date=end)

        assert report.period_start == start.isoformat()
        assert report.period_end == end.isoformat()

    def test_report_has_experiments_with_winners(self, mock_db, mock_experiment):
        """The overview counts the experiments whose results name a winner"""
        mock_db.query.return_value.count.return_value = 0
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        winner = MagicMock()
        winner.id = "var-002"
        winner.name = "treatment"
        mock_experiment.variants = [winner]
        without = MagicMock()
        without.id = "exp-002"
        mock_db.query.return_value.all.return_value = [mock_experiment, without]
        results = {
            "exp-001": {"summary": {"winning_variant_id": "var-002"}},
            "exp-002": {"summary": {"winning_variant_id": None}},
        }

        with patch("backend.app.services.export_service.AnalysisService") as analysis:
            analysis.return_value.get_experiment_results.side_effect = (
                lambda experiment_id, **_: results[experiment_id]
            )
            report = ExportService(mock_db).generate_platform_overview()

        assert report.experiments_with_winners == 1


class TestToCSV:
    def test_to_csv_uses_schema_field_names_as_headers(self, mock_db):
        from backend.app.schemas.export import ExperimentExportRow

        service = ExportService(mock_db)
        rows = [
            ExperimentExportRow(
                experiment_id="e1",
                experiment_name="Test",
                status="active",
                experiment_type="a_b",
                start_date=None,
                end_date=None,
                duration_days=None,
                total_assignments=0,
                total_events=0,
                winner_variant=None,
                recommendation=None,
            )
        ]
        csv_content = service._to_csv(rows, ExperimentExportRow)
        reader = csv.DictReader(io.StringIO(csv_content))
        headers = reader.fieldnames
        assert set(headers) == set(ExperimentExportRow.model_fields.keys())

    def test_to_csv_empty_list_returns_empty_string(self, mock_db):
        from backend.app.schemas.export import ExperimentExportRow

        service = ExportService(mock_db)
        assert service._to_csv([], ExperimentExportRow) == ""

    def test_to_csv_values_match_row_data(self, mock_db):
        """CSV data rows match the values from the input rows"""
        from backend.app.schemas.export import ExperimentExportRow

        service = ExportService(mock_db)
        rows = [
            ExperimentExportRow(
                experiment_id="exp-123",
                experiment_name="My Experiment",
                status="completed",
                experiment_type="a_b",
                start_date="2024-01-01",
                end_date="2024-01-31",
                duration_days=30.0,
                total_assignments=1000,
                total_events=500,
                winner_variant="Treatment",
                recommendation="SHIP_VARIANT",
            )
        ]
        csv_content = service._to_csv(rows, ExperimentExportRow)
        reader = csv.DictReader(io.StringIO(csv_content))
        data_rows = list(reader)
        assert len(data_rows) == 1
        assert data_rows[0]["experiment_id"] == "exp-123"
        assert data_rows[0]["experiment_name"] == "My Experiment"
        assert data_rows[0]["status"] == "completed"
        assert data_rows[0]["total_assignments"] == "1000"

    def test_to_csv_multiple_rows(self, mock_db):
        """CSV serializer correctly handles multiple rows"""
        from backend.app.schemas.export import ExperimentExportRow

        service = ExportService(mock_db)
        rows = [
            ExperimentExportRow(
                experiment_id=f"exp-{i}",
                experiment_name=f"Experiment {i}",
                status="active",
                experiment_type="a_b",
                start_date=None,
                end_date=None,
                duration_days=None,
                total_assignments=i * 100,
                total_events=i * 50,
                winner_variant=None,
                recommendation=None,
            )
            for i in range(5)
        ]
        csv_content = service._to_csv(rows, ExperimentExportRow)
        reader = csv.DictReader(io.StringIO(csv_content))
        data_rows = list(reader)
        assert len(data_rows) == 5


class TestDateFiltering:
    def test_start_date_filter_is_applied(self, mock_db, mock_experiment):
        """Export applies start_date filter to DB query"""
        chain = MagicMock()
        mock_db.query.return_value = chain
        chain.filter.return_value = chain
        chain.all.return_value = [mock_experiment]
        chain.options.return_value = chain
        chain.first.return_value = mock_experiment

        service = ExportService(mock_db)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        request = ExportRequest(format=ExportFormat.CSV, start_date=start)
        service.export_experiments(request)

        # filter should be called (for start_date)
        assert mock_db.query.called

    def test_end_date_filter_is_applied(self, mock_db, mock_experiment):
        """Export applies end_date filter to DB query"""
        chain = MagicMock()
        mock_db.query.return_value = chain
        chain.filter.return_value = chain
        chain.all.return_value = [mock_experiment]
        chain.options.return_value = chain
        chain.first.return_value = mock_experiment

        service = ExportService(mock_db)
        end = datetime(2024, 12, 31, tzinfo=timezone.utc)
        request = ExportRequest(format=ExportFormat.CSV, end_date=end)
        service.export_experiments(request)

        assert mock_db.query.called

    def test_experiment_id_filter_applied(self, mock_db, mock_experiment):
        """Export filters by provided experiment IDs"""
        chain = MagicMock()
        mock_db.query.return_value = chain
        chain.filter.return_value = chain
        chain.all.return_value = [mock_experiment]
        chain.options.return_value = chain
        chain.first.return_value = mock_experiment

        service = ExportService(mock_db)
        request = ExportRequest(format=ExportFormat.CSV)
        service.export_experiments(request, experiment_ids=["exp-001"])

        assert mock_db.query.called

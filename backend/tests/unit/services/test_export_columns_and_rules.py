"""The data export's columns, numbers and refusals (#1142).

``ExportService`` builds the rows of ``/api/v1/export/*``. Its numbers come
from the results computation called with no options, so they follow the
experiment's stored correction method and confidence level; the counts and
times below are read from the rows it builds, with the database mocked.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.experiment import (
    DEFAULT_CONFIDENCE_LEVEL,
    DEFAULT_CORRECTION_METHOD,
    Experiment,
    ExperimentStatus,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.user import UserRole
from backend.app.schemas.export import (
    ExportFormat,
    ExportRequest,
    PlatformOverviewReport,
)
from backend.app.services.export_service import ExportService
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
)

EXPORTS = (
    "/api/v1/export/experiments",
    "/api/v1/export/variants",
    "/api/v1/export/feature-flags",
    "/api/v1/export/reports/overview",
)


@pytest.fixture
def signed_in():
    def sign_in(role):
        user = SimpleNamespace(
            id=uuid.uuid4(), role=role, is_active=True, is_superuser=False
        )
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        app.dependency_overrides[deps.get_db] = lambda: MagicMock()

    yield sign_in
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    app.dependency_overrides.pop(deps.get_db, None)


def _empty_service():
    service = MagicMock()
    for name in ("export_experiments", "export_variants", "export_feature_flags"):
        getattr(service, name).return_value = ("", "text/csv")
    service.generate_platform_overview.return_value = PlatformOverviewReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        period_start=None,
        period_end=None,
        average_experiment_duration_days=None,
        total_experiments=0,
        active_experiments=0,
        completed_experiments=0,
        total_feature_flags=0,
        active_feature_flags=0,
        total_assignments=0,
        total_events=0,
        experiments_with_winners=0,
    )
    return service


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_every_role_may_use_the_exports(signed_in, role):
    signed_in(role)
    with patch(
        "backend.app.api.v1.endpoints.export.ExportService",
        return_value=_empty_service(),
    ):
        client = TestClient(app)
        for path in EXPORTS:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)


def test_the_export_urls_have_no_trailing_slash(signed_in):
    signed_in(UserRole.ADMIN)
    client = TestClient(app, follow_redirects=False)
    for path in EXPORTS:
        response = client.get(path + "/")
        assert response.status_code == 307, path
        assert response.headers["location"].endswith(path)


# ---------------------------------------------------------------------------
# The result columns
# ---------------------------------------------------------------------------

METRIC = SimpleNamespace(
    id="m", name="conversion", metric_type="conversion", is_primary=True
)


def _counts(treatment_a_conversions=131):
    return [
        (BinomialVariant("control", "Control", True), 1000, 100),
        (BinomialVariant("a", "A", False), 1000, treatment_a_conversions),
        (BinomialVariant("b", "B", False), 1000, 101),
    ]


def test_the_variant_columns_are_the_results_numbers_with_the_stored_settings():
    assert (DEFAULT_CORRECTION_METHOD, DEFAULT_CONFIDENCE_LEVEL) == (
        "benjamini_hochberg",
        0.95,
    )
    metric = binomial_metric_result(
        _counts(), 0.05, "benjamini_hochberg", metric=METRIC
    )
    variants = [
        SimpleNamespace(
            id=v["variant_id"], name=v["variant_name"], is_control=v["is_control"]
        )
        for v in metric["variants"]
    ]
    experiment = SimpleNamespace(id=uuid.uuid4(), name="Checkout", variants=variants)
    results = {"metrics": [metric], "summary": {}}

    with patch("backend.app.services.export_service.AnalysisService") as analysis:
        analysis.return_value.get_experiment_results.return_value = results
        rows = ExportService(MagicMock())._variants_to_rows([experiment])

    # No correction method or confidence level is passed: the results
    # computation uses the experiment's stored ones.
    analysis.return_value.get_experiment_results.assert_called_once_with(
        experiment.id, include_bayesian=False
    )
    by_id = {v["variant_id"]: v for v in metric["variants"]}
    for row in rows:
        source = by_id[row.variant_id]
        assert (row.assignments, row.conversions, row.conversion_rate) == (
            source["sample_size"],
            source["conversions"],
            source["mean"],
        )
        assert row.p_value == source["p_value"]
        assert row.is_significant == source["is_significant"]


def test_p_value_is_unadjusted_and_significance_is_decided_on_the_corrected_one():
    corrected = binomial_metric_result(
        _counts(), 0.05, "benjamini_hochberg", metric=METRIC
    )
    uncorrected = binomial_metric_result(_counts(), 0.05, "none", metric=METRIC)
    a = {v["variant_id"]: v for v in corrected["variants"]}["a"]
    a_none = {v["variant_id"]: v for v in uncorrected["variants"]}["a"]

    # The example: p 0.035 is not significant once Benjamini-Hochberg makes
    # it 0.071, and is significant with no correction.
    assert round(a["p_value"], 3) == 0.036 and round(a["adjusted_p_value"], 3) == 0.071
    assert a["is_significant"] is False and a_none["is_significant"] is True

    # Another correction changes significance only: the counts, rates and
    # unadjusted p-values are the same.
    for key in ("sample_size", "conversions", "mean", "p_value"):
        assert a[key] == a_none[key], key


# ---------------------------------------------------------------------------
# The feature flag columns
# ---------------------------------------------------------------------------


def _flag(created=None, updated=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        key="checkout_v2",
        name="Checkout v2",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=40,
        created_at=created or datetime(2026, 1, 2, 3, 4, 5),
        updated_at=updated or datetime(2026, 1, 3, 3, 4, 5),
    )


def test_the_feature_flag_columns():
    flag = _flag()
    db = MagicMock()
    db.query.return_value.all.return_value = [flag]
    service = ExportService(db)
    with patch.object(service, "_count_by", return_value={flag.id: 12}) as count_by:
        (row,) = service._build_feature_flag_rows(
            ExportRequest(format=ExportFormat.CSV)
        )

    # total_evaluations counts the metric records stored for the flag.
    (column, ids), _ = count_by.call_args
    assert str(column) == "RawMetric.feature_flag_id" and ids == [flag.id]
    assert row.total_evaluations == 12
    assert row.enabled_evaluations == 0
    assert row.enabled_rate == 0.0
    assert (row.flag_id, row.flag_name) == (str(flag.id), "Checkout v2")
    assert isinstance(row.flag_id, str)
    uuid.UUID(row.flag_id)
    assert row.created_at == "2026-01-02T03:04:05"
    assert row.updated_at == "2026-01-03T03:04:05"


# ---------------------------------------------------------------------------
# The overview
# ---------------------------------------------------------------------------


class _Query:
    """A query that remembers its filters and counts what matches them."""

    def __init__(self, counts, filters=()):
        self.counts = counts
        self.filters = filters

    def filter(self, condition):
        return _Query(self.counts, self.filters + (str(condition),))

    def count(self):
        return self.counts.get(self.filters, 0)

    def all(self):
        return self.counts.get(("all",) + self.filters, [])


def test_the_overview_fields():
    active = str(FeatureFlag.status == FeatureFlagStatus.ACTIVE)
    completed_with_dates = (
        str(Experiment.status == ExperimentStatus.COMPLETED),
        str(Experiment.start_date.isnot(None)),
        str(Experiment.end_date.isnot(None)),
    )
    start = datetime(2026, 1, 1)
    finished = [
        SimpleNamespace(start_date=start, end_date=start + timedelta(days=10)),
        SimpleNamespace(start_date=start, end_date=start + timedelta(days=21)),
    ]
    tables = {
        "Experiment": {(): 3, ("all",) + completed_with_dates: finished},
        "FeatureFlag": {(): 15, (active,): 8},
        "Assignment": {(): 125},
        "Event": {(): 340},
        "Report": {(): 5},
    }
    db = MagicMock()
    db.query.side_effect = lambda model: _Query(tables[model.__name__])

    report = ExportService(db).generate_platform_overview()

    assert report.period_start is None and report.period_end is None
    assert report.total_feature_flags == 15
    assert report.active_feature_flags == 8
    assert report.total_assignments == 125
    assert report.total_events == 340
    # Five stored reports, but none has a summary to count.
    assert report.experiments_with_winners == 0
    assert report.average_experiment_duration_days == 15.5
    assert datetime.fromisoformat(report.generated_at).utcoffset() == timedelta(0)

    tables["Experiment"] = {(): 3}
    assert (
        ExportService(db).generate_platform_overview().average_experiment_duration_days
        is None
    )


def test_times_and_ids_in_the_rows():
    stored = datetime(2026, 2, 1, 9, 30)
    experiment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Checkout",
        status=ExperimentStatus.COMPLETED,
        experiment_type=SimpleNamespace(value="a_b"),
        start_date=stored,
        end_date=stored + timedelta(days=7),
        variants=[],
    )
    row = ExportService(MagicMock())._experiment_to_row(experiment)
    # Written as stored, with no UTC offset added.
    assert (row.start_date, row.end_date) == (
        "2026-02-01T09:30:00",
        "2026-02-08T09:30:00",
    )
    assert row.experiment_id == str(experiment.id)

    with_offset = ExportService(MagicMock()).generate_platform_overview(
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
    )
    for value in (
        with_offset.generated_at,
        with_offset.period_start,
        with_offset.period_end,
    ):
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0), value


def test_csv_is_written_by_dictwriter_with_crlf_line_endings():
    flag = _flag()
    db = MagicMock()
    db.query.return_value.all.return_value = [flag]
    service = ExportService(db)
    with (
        patch.object(service, "_count_by", return_value={}),
        patch(
            "backend.app.services.export_service.csv.DictWriter",
            wraps=__import__("csv").DictWriter,
        ) as writer,
    ):
        content, content_type = service.export_feature_flags(
            ExportRequest(format=ExportFormat.CSV)
        )
    writer.assert_called_once()
    assert content_type == "text/csv"
    lines = content.split("\r\n")
    assert lines[0].startswith("flag_id,flag_key,flag_name,")
    assert lines[1].startswith(str(flag.id))
    assert "\n" not in content.replace("\r\n", "")


# ---------------------------------------------------------------------------
# Refusals and failures
# ---------------------------------------------------------------------------


def test_an_unaccepted_value_lists_each_invalid_parameter(signed_in):
    signed_in(UserRole.ADMIN)
    response = TestClient(app).get(
        "/api/v1/export/experiments", params={"format": "xml", "start_date": "soon"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert {tuple(e["loc"]) for e in detail} == {
        ("query", "format"),
        ("query", "start_date"),
    }
    assert "xml" not in response.text and "soon" not in response.text


def test_an_unexpected_failure_is_a_500_and_reaches_the_server_log(signed_in):
    signed_in(UserRole.ADMIN)
    service = _empty_service()
    service.export_feature_flags.side_effect = RuntimeError("database went away")
    with patch(
        "backend.app.api.v1.endpoints.export.ExportService", return_value=service
    ):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/v1/export/feature-flags"
        )
        assert response.status_code == 500
        assert response.text.endswith("Server Error")

        # The exception is raised on out of the application, so the server
        # running it logs the traceback.
        with pytest.raises(RuntimeError, match="database went away"):
            TestClient(app).get("/api/v1/export/feature-flags")

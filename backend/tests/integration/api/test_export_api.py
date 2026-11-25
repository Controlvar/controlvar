"""
DB-backed tests for the data export (#220).

* ``/export/variants`` and ``/export/experiments`` take their numbers from the
  computation behind ``GET /api/v1/results/{id}``, so each variant's
  assignments, conversions, rate, p-value and significance, and each
  experiment's winner and recommendation, equal what ``/results`` reports.
* ``/export/reports/experiments/{id}`` honours ``format=csv``, answers 404
  for an unknown id and 422 for one that is not a UUID.
* ``scope`` other than ``summary`` is refused with 422.
* An experiment whose results cannot be computed keeps its rows, with the
  result columns empty, and does not fail the export.
* The totals are counted in SQL: as events, assignments and raw metrics
  grow, the number of statements the export runs stays the same and it loads
  none of those rows.
* The results of several experiments are read together (#1817): the number
  of statements does not grow with the experiments of a group, and every
  result and every body is the one each experiment read alone gives.
* CSV cells are written spreadsheet-safe.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import csv
import io
import json
import uuid
import weakref
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import null
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.metrics.metric import RawMetric
from backend.app.schemas.export import (
    ExportFormat,
    ExportRequest,
    VariantExportRow,
)
from backend.app.schemas.tracking import VALUE_BOUND
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.export_service import ExportService

VARIANT_HEADER = ",".join(VariantExportRow.model_fields.keys())


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _delete_rows(db_session, experiment_ids=(), flag_ids=()):
    db_session.rollback()
    for model in (Event, Assignment):
        db_session.query(model).filter(model.experiment_id.in_(experiment_ids)).delete(
            synchronize_session=False
        )
    if flag_ids:
        db_session.query(RawMetric).filter(
            RawMetric.feature_flag_id.in_(flag_ids)
        ).delete(synchronize_session=False)
    db_session.commit()


@pytest.fixture
def make_ab_experiment(db_session, make_experiment):
    """Factory: an ACTIVE A/B experiment with a primary and a secondary metric."""
    created = []

    def _make(name=None, control_name="control", treatment_name="treatment"):
        suffix = uuid.uuid4().hex[:8]
        experiment = make_experiment(
            name=name or f"Export API {suffix}",
            key=f"export-api-{suffix}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        for variant_name, is_control in ((control_name, True), (treatment_name, False)):
            db_session.add(
                Variant(
                    experiment_id=experiment.id,
                    name=variant_name,
                    is_control=is_control,
                    traffic_allocation=50,
                )
            )
        # The secondary metric is created first, so that a builder picking the
        # first metric rather than the primary one would read the wrong numbers.
        db_session.add(
            Metric(
                experiment_id=experiment.id,
                name="Signup",
                event_name="signup",
                metric_type=MetricType.CONVERSION,
                is_primary=False,
            )
        )
        db_session.add(
            Metric(
                experiment_id=experiment.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            )
        )
        db_session.commit()
        db_session.refresh(experiment)
        created.append(experiment.id)
        return experiment

    yield _make
    _delete_rows(db_session, experiment_ids=created)


def _variants(experiment):
    control = next(v for v in experiment.variants if v.is_control)
    treatment = next(v for v in experiment.variants if not v.is_control)
    return control, treatment


def _seed(db_session, experiment, variant, n_users, n_purchases, n_signups=0):
    """n_users assignments, each with an exposure event; some purchase or sign up."""
    now_iso = datetime.now(timezone.utc).isoformat()
    prefix = f"{variant.name}-{uuid.uuid4().hex[:6]}"
    rows = []
    for i in range(n_users):
        user_id = f"{prefix}-{i:05d}"
        rows.append(
            Assignment(
                experiment_id=experiment.id, variant_id=variant.id, user_id=user_id
            )
        )
        names = ["exposure"]
        if i < n_purchases:
            names.append("purchase")
        if i < n_signups:
            names.append("signup")
        for name in names:
            rows.append(
                Event(
                    event_type=name,
                    event_name=name,
                    user_id=user_id,
                    experiment_id=experiment.id,
                    variant_id=variant.id,
                    value=1.0,
                    created_at=now_iso,
                )
            )
    db_session.add_all(rows)
    db_session.commit()


def _since(experiment):
    """A start_date that keeps the export to experiments made from this one on."""
    return (experiment.created_at - timedelta(seconds=1)).isoformat()


def _results(admin_client, experiment):
    response = admin_client.get(
        f"/api/v1/results/{experiment.id}", params={"use_cache": "false"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _primary(results):
    return next(m for m in results["metrics"] if m["is_primary"])


def _export(admin_client, route, experiment, fmt="json"):
    response = admin_client.get(
        f"/api/v1/export/{route}",
        params={"format": fmt, "start_date": _since(experiment)},
    )
    assert response.status_code == 200, response.text
    return response


@pytest.fixture
def significant_experiment(db_session, make_ab_experiment):
    """Treatment converts 70/400 against control's 40/400 on the primary metric."""
    experiment = make_ab_experiment()
    control, treatment = _variants(experiment)
    _seed(db_session, experiment, control, 400, 40, n_signups=200)
    _seed(db_session, experiment, treatment, 400, 70, n_signups=10)
    return experiment


# ---------------------------------------------------------------------------
# The export carries the results API's numbers
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestExportEqualsResults:
    @pytest.mark.regression
    def test_variant_rows_equal_results(self, admin_client, significant_experiment):
        experiment = significant_experiment
        expected = {
            v["variant_id"]: v
            for v in _primary(_results(admin_client, experiment))["variants"]
        }

        rows = [
            r
            for r in _export(admin_client, "variants", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]

        assert {r["variant_id"] for r in rows} == set(expected)
        for row in rows:
            want = expected[row["variant_id"]]
            assert row["variant_name"] == want["variant_name"]
            assert row["is_control"] == want["is_control"]
            assert row["assignments"] == want["sample_size"] == 400
            assert row["conversions"] == want["conversions"]
            assert row["conversion_rate"] == pytest.approx(want["mean"], abs=1e-12)
            if want["p_value"] is None:
                assert row["p_value"] is None
            else:
                assert row["p_value"] == pytest.approx(want["p_value"], abs=1e-12)
            assert row["is_significant"] == want["is_significant"]
            if want["relative_improvement_pct"] is None:
                assert row["relative_improvement_pct"] is None
            else:
                assert row["relative_improvement_pct"] == pytest.approx(
                    want["relative_improvement_pct"], abs=1e-12
                )
        # The primary metric's counts, not the secondary one's.
        by_name = {r["variant_name"]: r for r in rows}
        assert by_name["control"]["conversions"] == 40
        assert by_name["treatment"]["conversions"] == 70
        assert by_name["treatment"]["is_significant"] is True

    @pytest.mark.regression
    def test_experiment_row_winner_and_recommendation_equal_results(
        self, admin_client, significant_experiment
    ):
        experiment = significant_experiment
        summary = _results(admin_client, experiment)["summary"]
        assert summary["recommendation"] == "SHIP_VARIANT"
        _, treatment = _variants(experiment)
        assert summary["winning_variant_id"] == str(treatment.id)

        rows = [
            r
            for r in _export(admin_client, "experiments", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]

        assert len(rows) == 1
        assert rows[0]["winner_variant"] == "treatment"
        assert rows[0]["recommendation"] == summary["recommendation"]
        assert rows[0]["total_assignments"] == 800
        # 800 exposures, 110 purchases and 210 sign-ups.
        assert rows[0]["total_events"] == 800 + 110 + 210

    @pytest.mark.regression
    def test_failed_sample_ratio_check_is_inconclusive_in_results_and_export(
        self, admin_client, db_session, make_ab_experiment
    ):
        """600/400 on a 50/50 split fails the sample-ratio check (#880).

        The numbers alone would recommend SHIP_VARIANT: treatment converts
        70/400 against control's 60/600.  ``/results``, the experiment export
        and the report all answer INCONCLUSIVE, and the winner is still
        reported.  The check runs in ``AnalysisService``, which all three
        read; run only in the results route, the export would still say
        SHIP_VARIANT.
        """
        experiment = make_ab_experiment()
        control, treatment = _variants(experiment)
        _seed(db_session, experiment, control, 600, 60)
        _seed(db_session, experiment, treatment, 400, 70)

        body = _results(admin_client, experiment)
        summary = body["summary"]
        assert body["srm"]["warning"] is True
        assert summary["recommendation"] == "INCONCLUSIVE"
        assert summary["recommendation_reason"] == (
            "Sample-ratio mismatch: the observed assignment split does not match "
            f"the configured traffic allocation (p = {body['srm']['p_value']:.2g}), "
            "so these results cannot be trusted until the cause is found."
        )
        assert summary["has_winner"] is True
        assert summary["winning_variant_id"] == str(treatment.id)

        rows = [
            r
            for r in _export(admin_client, "experiments", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]
        assert len(rows) == 1
        assert rows[0]["recommendation"] == "INCONCLUSIVE"
        assert rows[0]["winner_variant"] == "treatment"

        report = admin_client.get(f"/api/v1/export/reports/experiments/{experiment.id}")
        assert report.status_code == 200, report.text
        assert report.json()["experiments"][0]["recommendation"] == "INCONCLUSIVE"

    @pytest.mark.regression
    def test_a_bandit_with_the_same_split_keeps_its_recommendation(
        self, admin_client, db_session, make_ab_experiment
    ):
        """A bandit moves traffic on purpose, so it has no sample-ratio check
        (``srm`` is null), and a null check changes nothing (#880)."""
        experiment = make_ab_experiment()
        experiment.optimization_type = "thompson_sampling"
        db_session.commit()
        control, treatment = _variants(experiment)
        _seed(db_session, experiment, control, 600, 60)
        _seed(db_session, experiment, treatment, 400, 70)

        body = _results(admin_client, experiment)
        assert body["srm"] is None
        assert body["summary"]["recommendation"] == "SHIP_VARIANT"

        rows = [
            r
            for r in _export(admin_client, "experiments", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]
        assert rows[0]["recommendation"] == "SHIP_VARIANT"

    def test_no_winner_is_empty_and_recommendation_still_equals_results(
        self, admin_client, db_session, make_ab_experiment
    ):
        experiment = make_ab_experiment()
        control, treatment = _variants(experiment)
        _seed(db_session, experiment, control, 200, 20)
        _seed(db_session, experiment, treatment, 200, 21)
        summary = _results(admin_client, experiment)["summary"]

        rows = [
            r
            for r in _export(admin_client, "experiments", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]

        assert summary["winning_variant_id"] is None
        assert rows[0]["winner_variant"] is None
        assert rows[0]["recommendation"] == summary["recommendation"]

    def test_report_json_equals_the_exports(self, admin_client, significant_experiment):
        experiment = significant_experiment
        variants = [
            r
            for r in _export(admin_client, "variants", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]
        experiments = [
            r
            for r in _export(admin_client, "experiments", experiment).json()
            if r["experiment_id"] == str(experiment.id)
        ]

        response = admin_client.get(
            f"/api/v1/export/reports/experiments/{experiment.id}"
        )

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        report = response.json()
        assert report["experiment_id"] == str(experiment.id)
        assert report["experiments"] == experiments
        assert report["variants"] == variants

    @pytest.mark.regression
    def test_experiment_without_control_keeps_rows_with_empty_results(
        self, admin_client, db_session, make_experiment, significant_experiment
    ):
        """One experiment whose results cannot be computed does not fail the export."""
        broken = make_experiment(
            name=f"Export no control {uuid.uuid4().hex[:8]}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
        )
        db_session.add(Variant(experiment_id=broken.id, name="only", is_control=False))
        # With a metric and no control variant, get_experiment_results raises
        # ValueError, which is what /results answers 404 for.
        db_session.add(
            Metric(
                experiment_id=broken.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            )
        )
        db_session.commit()
        db_session.refresh(broken)
        assert (
            admin_client.get(
                f"/api/v1/results/{broken.id}", params={"use_cache": "false"}
            ).status_code
            == 404
        )

        variants = _export(admin_client, "variants", significant_experiment).json()
        experiments = _export(
            admin_client, "experiments", significant_experiment
        ).json()

        broken_variant = next(
            r for r in variants if r["experiment_id"] == str(broken.id)
        )
        for column in (
            "assignments",
            "conversions",
            "conversion_rate",
            "p_value",
            "is_significant",
            "relative_improvement_pct",
        ):
            assert broken_variant[column] is None, column
        broken_row = next(
            r for r in experiments if r["experiment_id"] == str(broken.id)
        )
        assert broken_row["winner_variant"] is None
        assert broken_row["recommendation"] is None
        # The healthy experiment in the same export still carries its numbers.
        healthy = next(
            r
            for r in experiments
            if r["experiment_id"] == str(significant_experiment.id)
        )
        assert healthy["recommendation"] == "SHIP_VARIANT"


# ---------------------------------------------------------------------------
# The single-experiment report
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestExperimentReport:
    @pytest.mark.regression
    def test_format_csv_is_the_variant_rows_as_csv(
        self, admin_client, significant_experiment
    ):
        experiment = significant_experiment
        response = admin_client.get(
            f"/api/v1/export/reports/experiments/{experiment.id}",
            params={"format": "csv"},
        )

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        lines = response.text.splitlines()
        assert lines[0] == VARIANT_HEADER
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 2
        for row in rows:
            for cell in row.values():
                assert not cell.startswith(("{", "[")), cell
        expected = {
            v["variant_id"]: v
            for v in _primary(_results(admin_client, experiment))["variants"]
        }
        for row in rows:
            want = expected[row["variant_id"]]
            assert int(row["assignments"]) == want["sample_size"]
            assert int(row["conversions"]) == want["conversions"]
            assert float(row["conversion_rate"]) == pytest.approx(
                want["mean"], abs=1e-12
            )

    @pytest.mark.regression
    def test_unknown_experiment_is_404(self, admin_client):
        response = admin_client.get(
            f"/api/v1/export/reports/experiments/{uuid.uuid4()}"
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Experiment not found"

    @pytest.mark.regression
    def test_non_uuid_id_is_422(self, admin_client):
        response = admin_client.get("/api/v1/export/reports/experiments/not-a-uuid")
        assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
@pytest.mark.parametrize("route", ["experiments", "variants"])
def test_scope_other_than_summary_is_422(admin_client, route):
    response = admin_client.get(f"/api/v1/export/{route}", params={"scope": "events"})
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "scope=events is not supported; the export is available with scope=summary only"
    )


# ---------------------------------------------------------------------------
# Spreadsheet-safe CSV cells
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestSpreadsheetSafeCells:
    @pytest.mark.regression
    def test_leading_characters_are_written_as_text(
        self, admin_client, db_session, make_ab_experiment, make_feature_flag
    ):
        experiment = make_ab_experiment(
            name=f"=1+2 {uuid.uuid4().hex[:6]}",
            control_name="-10% off",
            treatment_name="@home",
        )
        flag = make_feature_flag(key=f"flag-{uuid.uuid4().hex[:8]}", name="+plus\tname")

        experiments = list(
            csv.DictReader(
                io.StringIO(
                    _export(admin_client, "experiments", experiment, "csv").text
                )
            )
        )
        variants = list(
            csv.DictReader(
                io.StringIO(_export(admin_client, "variants", experiment, "csv").text)
            )
        )
        flags_csv = admin_client.get(
            "/api/v1/export/feature-flags",
            params={"format": "csv", "start_date": _since(flag)},
        ).text
        flags = list(csv.DictReader(io.StringIO(flags_csv)))

        exp_row = next(
            r for r in experiments if r["experiment_id"] == str(experiment.id)
        )
        assert exp_row["experiment_name"] == "'" + experiment.name
        names = {
            r["variant_name"]
            for r in variants
            if r["experiment_id"] == str(experiment.id)
        }
        assert names == {"'-10% off", "'@home"}
        flag_row = next(r for r in flags if r["flag_id"] == str(flag.id))
        assert flag_row["flag_name"] == "'+plus\tname"
        # Ordinary text and numbers are unchanged.
        assert flag_row["flag_key"] == flag.key
        assert exp_row["experiment_type"] == "a_b"

        # JSON carries the values as they are.
        json_rows = _export(admin_client, "variants", experiment).json()
        assert {
            r["variant_name"]
            for r in json_rows
            if r["experiment_id"] == str(experiment.id)
        } == {"-10% off", "@home"}

    def test_every_leading_character(self):
        from backend.app.services.export_service import spreadsheet_safe

        for text in ("=A1", "+1", "-1", "@A1", "\tA1", "\rA1"):
            assert spreadsheet_safe(text) == "'" + text
        for value in ("A1", "", "1-2", " =A1", -1, -0.5, None, True):
            assert spreadsheet_safe(value) == value


# ---------------------------------------------------------------------------
# Growth invariance: totals are counted in SQL
# ---------------------------------------------------------------------------


class _Probe:
    """Counts the statements a session runs and the rows of each model it loads."""

    MODELS = (Event, Assignment, RawMetric)

    def __init__(self, engine):
        self.engine = engine
        self.statements = 0
        self.loaded = Counter()

    def _on_execute(self, *args, **kwargs):
        self.statements += 1

    def _loader(self, model):
        def _on_load(target, context):
            self.loaded[model.__name__] += 1

        return _on_load

    def __enter__(self):
        sa_event.listen(self.engine, "before_cursor_execute", self._on_execute)
        self._listeners = [(m, self._loader(m)) for m in self.MODELS]
        for model, fn in self._listeners:
            sa_event.listen(model, "load", fn)
        return self

    def __exit__(self, *exc):
        sa_event.remove(self.engine, "before_cursor_execute", self._on_execute)
        for model, fn in self._listeners:
            sa_event.remove(model, "load", fn)


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_export_work_does_not_grow_with_events_and_raw_metrics(
    db_session, make_ab_experiment, make_feature_flag
):
    """
    The experiment and flag exports count assignments, events and evaluations
    in SQL: adding rows changes the totals, not the number of statements run,
    and no Event, Assignment or RawMetric row is loaded.
    """
    experiment = make_ab_experiment()
    control, treatment = _variants(experiment)
    flag = make_feature_flag(key=f"flag-{uuid.uuid4().hex[:8]}")
    flag_since = flag.created_at - timedelta(seconds=1)

    def add_rows(n):
        _seed(db_session, experiment, control, n, n // 5)
        _seed(db_session, experiment, treatment, n, n // 4)
        db_session.add_all(
            RawMetric(
                metric_type="flag_evaluation", feature_flag_id=flag.id, user_id=f"u{i}"
            )
            for i in range(n)
        )
        db_session.commit()

    def measure():
        session = Session(bind=db_session.get_bind())
        try:
            with _Probe(session.get_bind()) as probe:
                service = ExportService(session)
                experiments = json.loads(
                    service.export_experiments(
                        ExportRequest(format=ExportFormat.JSON),
                        experiment_ids=[str(experiment.id)],
                    )[0]
                )
                flags = json.loads(
                    service.export_feature_flags(
                        ExportRequest(format=ExportFormat.JSON, start_date=flag_since)
                    )[0]
                )
            flag_row = next(r for r in flags if r["flag_id"] == str(flag.id))
            return probe, experiments[0], flag_row
        finally:
            session.close()

    try:
        add_rows(20)
        small, small_exp, small_flag = measure()
        add_rows(200)
        large, large_exp, large_flag = measure()
    finally:
        _delete_rows(db_session, flag_ids=[flag.id])

    # The totals are live ...
    assert small_exp["total_assignments"] == 40
    assert large_exp["total_assignments"] == 440
    assert large_exp["total_events"] > small_exp["total_events"]
    assert small_flag["total_evaluations"] == 20
    assert large_flag["total_evaluations"] == 220
    # ... and the work to produce them does not grow with the rows.
    assert large.statements == small.statements, (small.statements, large.statements)
    assert dict(small.loaded) == {}, small.loaded
    assert dict(large.loaded) == {}, large.loaded


# ---------------------------------------------------------------------------
# Growth invariance: the number of experiments (#1817)
# ---------------------------------------------------------------------------


def _statements_for_export(db_session, experiments):
    """The statements ``export_experiments`` runs for exactly these experiments."""
    session = Session(bind=db_session.get_bind())
    try:
        with _Probe(session.get_bind()) as probe:
            body = ExportService(session).export_experiments(
                ExportRequest(format=ExportFormat.JSON),
                experiment_ids=[str(e.id) for e in experiments],
            )[0]
        return probe.statements, json.loads(body)
    finally:
        session.close()


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_experiment_export_statements_do_not_grow_with_experiment_count(
    db_session, make_ab_experiment
):
    """#1817: ten experiments cost the statements one does."""
    made = [make_ab_experiment() for _ in range(10)]
    for experiment in made:
        control, treatment = _variants(experiment)
        _seed(db_session, experiment, control, 20, 2)
        _seed(db_session, experiment, treatment, 20, 6)

    one, one_rows = _statements_for_export(db_session, made[:1])
    ten, ten_rows = _statements_for_export(db_session, made)

    assert len(one_rows) == 1 and len(ten_rows) == 10
    assert ten == one, (one, ten)


def _seeded(db_session, make_ab_experiment, n):
    """``n`` A/B experiments, each with assignments and primary conversions."""
    made = [make_ab_experiment() for _ in range(n)]
    for experiment in made:
        control, treatment = _variants(experiment)
        _seed(db_session, experiment, control, 20, 2)
        _seed(db_session, experiment, treatment, 20, 6)
    return made


def _sql(db_session, call):
    """``call``'s answer on a session of its own, and the statements it runs."""
    session = Session(bind=db_session.get_bind())
    statements = []

    def on_execute(conn, cursor, statement, *_args):
        statements.append(statement)

    engine = session.get_bind()
    sa_event.listen(engine, "before_cursor_execute", on_execute)
    try:
        return call(session), statements
    finally:
        sa_event.remove(engine, "before_cursor_execute", on_execute)
        session.close()


def _period_of_their_own(db_session, experiment_ids):
    """Move the experiments to a period that holds no other; its first moment.

    They are one second apart, in the order given. The shared test database
    is not truncated, so the period is in the past and differs in every run.
    """
    start = datetime(1980, 1, 1) + timedelta(
        seconds=uuid.uuid4().int % (30 * 365 * 24 * 3600)
    )
    for i, experiment_id in enumerate(experiment_ids):
        db_session.query(Experiment).filter(Experiment.id == experiment_id).update(
            {"created_at": start + timedelta(seconds=i)}, synchronize_session=False
        )
    db_session.commit()
    return start


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_variant_export_statements_do_not_grow_with_experiment_count(
    db_session, make_ab_experiment
):
    """#1817: the variant export of ten experiments costs what one's does."""
    made = _seeded(db_session, make_ab_experiment, 10)

    def export(experiments):
        return _sql(
            db_session,
            lambda session: json.loads(
                ExportService(session).export_variants(
                    ExportRequest(format=ExportFormat.JSON),
                    experiment_ids=[str(e.id) for e in experiments],
                )[0]
            ),
        )

    one_rows, one = export(made[:1])
    ten_rows, ten = export(made)

    assert len(one_rows) == 2 and len(ten_rows) == 20
    assert len(ten) == len(one), (len(one), len(ten))


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_overview_statements_do_not_grow_with_experiment_count(
    db_session, make_ab_experiment
):
    """#1817: the overview of ten experiments costs what one's does."""
    made = _seeded(db_session, make_ab_experiment, 10)
    start = _period_of_their_own(db_session, [e.id for e in made])

    def overview(n):
        return _sql(
            db_session,
            lambda session: ExportService(session).generate_platform_overview(
                start, start + timedelta(seconds=n - 1)
            ),
        )

    one_report, one = overview(1)
    ten_report, ten = overview(10)

    assert one_report.total_experiments == 1
    assert ten_report.total_experiments == 10
    assert len(ten) == len(one), (len(one), len(ten))


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_export_statements_grow_once_per_group_of_experiments(
    db_session, make_ab_experiment, monkeypatch
):
    """A group of experiments costs what one does, and two groups twice that."""
    monkeypatch.setattr("backend.app.services.results_batch.RESULTS_BATCH_SIZE", 4)
    made = _seeded(db_session, make_ab_experiment, 8)

    one, _ = _statements_for_export(db_session, made[:1])
    four, _ = _statements_for_export(db_session, made[:4])
    eight, rows = _statements_for_export(db_session, made)

    # The export's own reads: the experiments and the two totals.
    own = 3
    assert len(rows) == 8
    assert four == one, (one, four)
    assert eight == four + (four - own), (four, eight)


# ---------------------------------------------------------------------------
# The results read together are the results read one at a time (#1817)
# ---------------------------------------------------------------------------

_AB = (("control", True, 50), ("treatment", False, 50))
_ABC = (("control", True, 34), ("a", False, 33), ("b", False, 33))

_PURCHASE = ("Purchase", "purchase", MetricType.CONVERSION, {"is_primary": True})
_SIGNUP = ("Signup", "signup", MetricType.CONVERSION, {})


class _Battery:
    """Experiments the results computation answers differently, each by name.

    ``ids`` is ``{shape: experiment id}`` in the order the shapes were made.
    """

    def __init__(self, db_session, make_experiment):
        self.db = db_session
        self._make = make_experiment
        self.ids = {}
        self._arms = {}

    def experiment(self, shape, metrics, variants=_AB, **fields):
        suffix = uuid.uuid4().hex[:8]
        experiment = self._make(
            name=f"{shape} {suffix}",
            key=f"results-batch-{suffix}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 9, 30, tzinfo=timezone.utc),
            **fields,
        )
        self.ids[shape] = experiment.id
        arms = {
            name: Variant(
                experiment_id=experiment.id,
                name=name,
                is_control=is_control,
                traffic_allocation=allocation,
            )
            for name, is_control, allocation in variants
        }
        self.db.add_all(arms.values())
        self.db.add_all(
            Metric(
                experiment_id=experiment.id,
                name=name,
                event_name=event_name,
                metric_type=metric_type,
                **extra,
            )
            for name, event_name, metric_type, extra in metrics
        )
        self.db.flush()
        self._arms[shape] = {name: arm.id for name, arm in arms.items()}

    def arm(self, shape, name):
        """The id of one of the shape's variants."""
        return self._arms[shape][name]

    def assign(self, shape, arm, user_ids):
        self.db.add_all(
            Assignment(
                experiment_id=self.ids[shape],
                variant_id=self.arm(shape, arm),
                user_id=user_id,
            )
            for user_id in user_ids
        )

    def users(self, shape, **arms):
        """Assign ``n`` new users to each arm: ``{arm: [user ids]}``."""
        made = {
            arm: [f"{uuid.uuid4().hex[:12]}-{i:03d}" for i in range(n)]
            for arm, n in arms.items()
        }
        for arm, user_ids in made.items():
            self.assign(shape, arm, user_ids)
        return made

    def send(self, shape, arm, user_ids, name, *values, event_type=None):
        """Each user sends one event per value (one worth 1.0 when none is given).

        ``arm`` is one of the shape's variants by name, a variant id, or None
        for an event with no variant.
        """
        variant_id = self.arm(shape, arm) if isinstance(arm, str) else arm
        self.db.add_all(
            Event(
                event_type=event_type or name,
                event_name=name,
                user_id=user_id,
                experiment_id=self.ids[shape],
                variant_id=variant_id,
                value=value,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            for user_id in user_ids
            for value in (values or (1.0,))
        )


def _fill(b):
    """Make every shape.

    The order is the one the sub-group test states. A shape's weight there is
    its assigned users times the event names its metrics read, the empty name
    (or no metric) counting as one more.
    """
    b.experiment("no assignments", [_PURCHASE])

    b.experiment("significant", [_SIGNUP, _PURCHASE])
    u = b.users("significant", control=100, treatment=100)
    b.send("significant", "control", u["control"][:10], "purchase", 1.0, 1.0)
    b.send("significant", "treatment", u["treatment"][:35], "purchase")
    b.send("significant", "control", u["control"][5:25], "signup")
    b.send("significant", "treatment", u["treatment"][30:52], "signup")

    shape = "revenue and count"
    b.experiment(
        shape,
        [
            ("Clicks", "click", MetricType.COUNT, {}),
            ("Revenue", "purchase", MetricType.REVENUE, {"is_primary": True}),
        ],
    )
    u = b.users(shape, control=30, treatment=30)
    for i in range(10):
        b.send(shape, "control", [u["control"][i]], "purchase", 0.25 * i, 0.5)
    b.send(shape, "control", u["control"][:1], "purchase", None)
    b.send(shape, "control", u["control"][10:11], "purchase", None)
    b.send(shape, "control", u["control"][11:12], "purchase", 0.0)
    b.send(shape, "control", u["control"][12:13], "purchase", -3.5)
    for i in range(20):
        b.send(shape, "treatment", [u["treatment"][i]], "purchase", 2.0 + 0.5 * i)
    b.send(shape, "control", u["control"][:5], "click", None, None, None)
    b.send(shape, "control", u["control"][5:6], "click", 1e300)
    b.send(shape, "treatment", u["treatment"][:12], "click", 1.0, 1.0)

    b.experiment("no conversions", [_PURCHASE])
    u = b.users("no conversions", control=25, treatment=25)
    b.send("no conversions", "control", u["control"], "exposure")

    shape = "no control"
    b.experiment(shape, [_PURCHASE], (("a", False, 50), ("b", False, 50)))
    u = b.users(shape, a=5, b=5)
    b.send(shape, "a", u["a"][:2], "purchase")

    # The legacy convention: with no metric, a conversion is event_type.
    shape = "no metrics"
    b.experiment(shape, [])
    u = b.users(shape, control=10, treatment=10)
    b.send(shape, "control", u["control"][:1], None, 1.0, 1.0, event_type="conversion")
    b.send(shape, "control", u["control"][1:2], "x", event_type="conversion")
    b.send(shape, "treatment", u["treatment"][:4], "y", event_type="conversion")
    b.send(shape, "treatment", u["treatment"][5:6], "purchase")
    b.send(shape, "control", ["nobody-1"], "x", event_type="conversion")

    for shape, fields in (
        ("sample ratio", {}),
        ("bandit", {"optimization_type": "thompson_sampling"}),
    ):
        b.experiment(shape, [_PURCHASE], **fields)
        u = b.users(shape, control=120, treatment=40)
        b.send(shape, "control", u["control"][:6], "purchase")
        b.send(shape, "treatment", u["treatment"][:20], "purchase")

    shape = "three variants"
    b.experiment(shape, [_PURCHASE], _ABC)
    u = b.users(shape, control=40, a=40, b=40)
    b.send(shape, "control", u["control"][:6], "purchase")
    b.send(shape, "a", u["a"][:12], "purchase")
    b.send(shape, "b", u["b"][:5], "purchase")
    # None of these is a conversion: a user with no assignment, another arm's
    # tag, no tag, the two exposure types, and a name no metric has.
    b.send(shape, "control", ["nobody-1", "nobody-2"], "purchase")
    b.send(shape, "a", u["control"][6:8], "purchase")
    b.send(shape, None, u["b"][5:9], "purchase")
    b.send(shape, "control", u["control"][8:12], "purchase", event_type="exposure")
    b.send(
        shape,
        "control",
        u["control"][12:14],
        "purchase",
        event_type="experiment_exposure",
    )
    b.send(shape, "a", u["a"][12:15], "retired")

    shape = "lower is better"
    b.experiment(
        shape,
        [
            (
                "Refund",
                "refund",
                MetricType.CONVERSION,
                {"is_primary": True, "lower_is_better": True},
            )
        ],
    )
    u = b.users(shape, control=100, treatment=100)
    b.send(shape, "control", u["control"][:30], "refund")
    b.send(shape, "treatment", u["treatment"][:8], "refund")

    shape = "beyond the bound"
    b.experiment(
        shape, [("Revenue", "purchase", MetricType.REVENUE, {"is_primary": True})]
    )
    u = b.users(shape, control=20, treatment=20)
    b.send(shape, "control", u["control"][:1], "purchase", 5e12)
    b.send(shape, "control", u["control"][1:2], "purchase", 5e12, 2.5)
    b.send(shape, "control", u["control"][2:3], "purchase", -5e12)
    b.send(shape, "control", u["control"][3:4], "purchase", 4.0)
    b.send(shape, "treatment", u["treatment"][:1], "purchase", VALUE_BOUND)
    b.send(shape, "treatment", u["treatment"][1:2], "purchase", 1.0)

    # The same users in two experiments, and one event in each that carries
    # the other's variant, from a user assigned to that variant there.
    shared = [f"shared-{uuid.uuid4().hex[:8]}-{i:02d}" for i in range(20)]
    revenue = ("Revenue", "purchase", MetricType.REVENUE, {})
    b.experiment("shared users, first", [_PURCHASE, revenue])
    b.assign("shared users, first", "control", shared[0::2])
    b.assign("shared users, first", "treatment", shared[1::2])
    b.experiment("shared users, second", [_PURCHASE, _SIGNUP])
    b.assign("shared users, second", "control", shared[:10])
    b.assign("shared users, second", "treatment", shared[10:])
    shape = "shared users, first"
    b.send(shape, "control", shared[0:6:2], "purchase", 2.0)
    b.send(shape, "treatment", shared[1:9:2], "purchase", 4.0)
    b.send(shape, b.arm("shared users, second", "treatment"), shared[18:19], "purchase")
    shape = "shared users, second"
    b.send(shape, "control", shared[:3], "purchase")
    b.send(shape, "treatment", shared[10:16], "purchase")
    b.send(shape, "control", [shared[0], shared[5]], "signup")
    b.send(shape, b.arm("shared users, first", "treatment"), shared[7:8], "signup")

    # An empty event name reads the legacy events, whatever their names are.
    shape = "legacy count and revenue"
    b.experiment(
        shape,
        [
            ("Conversions", "", MetricType.COUNT, {"is_primary": True}),
            ("Value", "", MetricType.REVENUE, {}),
        ],
    )
    u = b.users(shape, control=20, treatment=20)
    for arm, user_ids, name, value in (
        ("control", u["control"][:1], "x", 1.5),
        ("control", u["control"][:1], "y", 2.5),
        ("control", u["control"][:1], None, None),
        ("control", u["control"][1:2], "x", 5e12),
        ("control", u["control"][2:3], None, 0.25),
        ("control", u["control"][2:3], "y", 5e12),
        ("control", u["control"][3:4], None, None),
        ("treatment", u["treatment"][:10], "x", 1.0),
        ("treatment", u["treatment"][:10], "y", 2.0),
        ("treatment", u["treatment"][10:11], None, -4.0),
    ):
        b.send(shape, arm, user_ids, name, value, event_type="conversion")
    b.send(shape, "control", u["control"][4:5], "x", event_type="purchase")

    shape = "legacy beside named"
    b.experiment(shape, [_PURCHASE, ("Any", "", MetricType.COUNT, {})])
    u = b.users(shape, control=20, treatment=20)
    b.send(shape, "control", u["control"][:3], "purchase")
    b.send(shape, "treatment", u["treatment"][:7], "purchase")
    b.send(shape, "control", u["control"][10:14], "x", event_type="conversion")
    b.send(shape, "treatment", u["treatment"][10:12], None, event_type="conversion")
    b.send(
        shape, "treatment", u["treatment"][12:13], "purchase", event_type="conversion"
    )

    shape = "deleted metric"
    b.experiment(shape, [_PURCHASE, ("Old", "old", MetricType.CONVERSION, {})])
    u = b.users(shape, control=15, treatment=15)
    b.send(shape, "control", u["control"][:2], "purchase")
    b.send(shape, "treatment", u["treatment"][:6], "purchase")
    b.send(shape, "control", u["control"][5:9], "old")
    b.send(shape, "treatment", u["treatment"][7:9], "old")
    b.db.flush()
    b.db.query(Metric).filter(
        Metric.experiment_id == b.ids[shape], Metric.name == "Old"
    ).delete(synchronize_session=False)

    shape = "stored settings"
    b.experiment(
        shape,
        [_PURCHASE],
        _ABC,
        correction_method="bonferroni",
        confidence_level=0.90,
    )
    u = b.users(shape, control=60, a=60, b=60)
    for arm, n in (("control", 10), ("a", 21), ("b", 17)):
        b.send(shape, arm, u[arm][:n], "purchase")

    shape = "bayesian"
    b.experiment(
        shape,
        [_PURCHASE],
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
    )
    u = b.users(shape, control=50, treatment=50)
    b.send(shape, "control", u["control"][:10], "purchase")
    b.send(shape, "treatment", u["treatment"][:18], "purchase")

    shape = "three arms, sample ratio"
    b.experiment(
        shape,
        [_PURCHASE],
        (("control", True, 50), ("a", False, 25), ("b", False, 25)),
    )
    u = b.users(shape, control=60, a=60, b=60)
    for arm, n in (("control", 6), ("a", 9), ("b", 3)):
        b.send(shape, arm, u[arm][:n], "purchase")

    shape = "no allocation"
    b.experiment(
        shape,
        [_PURCHASE],
        (("control", True, 50), ("a", False, 50), ("b", False, null())),
    )
    u = b.users(shape, control=20, a=20, b=20)
    for arm, n in (("control", 2), ("a", 4), ("b", 1)):
        b.send(shape, arm, u[arm][:n], "purchase")

    # Another experiment's metric reads the name; this one's does not.
    shape = "another metric's event"
    b.experiment(shape, [_SIGNUP])
    u = b.users(shape, control=10, treatment=10)
    b.send(shape, "control", u["control"][:2], "signup")
    b.send(shape, "treatment", u["treatment"][:4], "signup")
    b.send(shape, "control", u["control"][2:6], "purchase")
    b.send(shape, "treatment", u["treatment"][4:8], "purchase")

    shape = "only a control"
    b.experiment(shape, [_PURCHASE], (("control", True, 100),))
    u = b.users(shape, control=10)
    b.send(shape, "control", u["control"][:2], "purchase")

    b.experiment("nothing", [], ())

    shape = "duration and custom"
    b.experiment(
        shape,
        [
            (
                "Load",
                "loaded",
                MetricType.DURATION,
                {"is_primary": True, "lower_is_better": True},
            ),
            ("Score", "score", MetricType.CUSTOM, {}),
        ],
    )
    u = b.users(shape, control=20, treatment=20)
    b.send(shape, "control", u["control"][:15], "loaded", 2.0, 4.0)
    b.send(shape, "treatment", u["treatment"][:12], "loaded", 1.0)
    b.send(shape, "control", u["control"][:4], "score", 0.5)
    b.send(shape, "treatment", u["treatment"][:8], "score", 1.5)

    b.db.commit()


#: The battery's experiments and one id no experiment has.
_SHAPES = 25
_UNKNOWN = "unknown id"
#: The shapes with no metric that names its event.
_NO_EVENT_NAME = {"no metrics", "legacy count and revenue", "nothing", _UNKNOWN}

#: At a budget of 300: each sub-group of the battery, and its statements but
#: for the one that counts its assigned users.
_SUB_GROUPS = [
    (["no assignments"], 1),
    (["significant"], 2),
    (["revenue and count", "no conversions", "no control", "no metrics"], 3),
    (["sample ratio"], 2),
    (["bandit", "three variants"], 2),
    (
        [
            "lower is better",
            "beyond the bound",
            "shared users, first",
            "shared users, second",
        ],
        2,
    ),
    (["legacy count and revenue", "legacy beside named", "deleted metric"], 3),
    (["stored settings", "bayesian"], 2),
    (
        [
            "three arms, sample ratio",
            "no allocation",
            "another metric's event",
            "only a control",
            "nothing",
        ],
        3,
    ),
    (["duration and custom"], 2),
]


def _delete_experiments(db_session, ids):
    """Delete a battery's experiments, with their variants and metrics (ON
    DELETE CASCADE): no route makes a variant with no allocation, and the
    experiment list does not answer one."""
    _delete_rows(db_session, experiment_ids=ids)
    db_session.query(Experiment).filter(Experiment.id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.commit()


@pytest.fixture
def battery(db_session, make_experiment):
    """``{shape: experiment id}`` for every shape, and an id no experiment has."""
    made = _Battery(db_session, make_experiment)
    _fill(made)
    yield {**made.ids, _UNKNOWN: uuid.uuid4()}
    _delete_experiments(db_session, list(made.ids.values()))


class _SubGroups:
    """Records the shapes of each sub-group that is read, in order, and checks
    that nothing is held from one sub-group to the next.

    ``counted`` is the shapes whose assigned users were held.
    """

    def __init__(self, monkeypatch, battery):
        from backend.app.services import results_batch

        self.read = []
        self.counted = set()
        self.rows = []
        shapes = {str(i): shape for shape, i in battery.items()}
        load_rows = results_batch.load_rows
        service = results_batch._BatchedAnalysisService
        load_experiment = service._load_experiment

        def reading(db, experiments):
            # The last sub-group's rows and counts are gone before the next are
            # read.
            assert [ref() for ref in self.rows] == [None] * len(self.rows)
            self.read.append([shapes[str(e.id)] for e in experiments])
            loaded = load_rows(db, experiments)
            self.rows.append(weakref.ref(loaded))
            return loaded

        def computing(batched, experiment_id):
            # What is held is the current sub-group's.
            rows = batched._reads.rows
            for held in (
                {shapes[e] for e, _, _ in rows.users},
                {shapes[e] for e in rows.converted},
                {shapes[e] for e in rows.assignments},
            ):
                assert held <= set(self.read[-1]), held
            self.counted.update(shapes[e] for e in rows.assignments)
            return load_experiment(batched, experiment_id)

        monkeypatch.setattr(results_batch, "load_rows", reading)
        monkeypatch.setattr(service, "_load_experiment", computing)


def _many(db_session, experiment_ids):
    """The batched results of these ids, and the statements they ran."""
    return _sql(
        db_session,
        lambda session: AnalysisService(session).get_experiment_results_many(
            list(experiment_ids)
        ),
    )


def _single(session, experiment_id):
    """What the export computed one experiment at a time, or None (a ValueError)."""
    try:
        return AnalysisService(session).get_experiment_results(
            experiment_id, include_bayesian=False
        )
    except ValueError:
        return None


def _comparable(results):
    """The results less the moment they were computed, as type-sensitive text."""
    if results is None:
        return None
    return json.dumps(
        {k: v for k, v in results.items() if k != "computed_at"},
        sort_keys=True,
        default=str,
    )


def _shapes_that_differ(db_session, battery, results):
    """The shapes whose batched results are not their results read alone."""
    differ = []
    for shape, experiment_id in battery.items():
        alone, _ = _sql(db_session, lambda session: _single(session, experiment_id))
        if _comparable(results[str(experiment_id)]) != _comparable(alone):
            differ.append(shape)
    return differ


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
class TestResultsReadTogether:
    """``get_experiment_results_many`` answers what ``get_experiment_results`` does."""

    @pytest.mark.parametrize("size", [None, _SHAPES, 1])
    def test_each_result_is_the_result_read_alone(
        self, db_session, battery, monkeypatch, size
    ):
        """In groups of the shipped size, in one group, and one at a time."""
        if size is not None:
            monkeypatch.setattr(
                "backend.app.services.results_batch.RESULTS_BATCH_SIZE", size
            )
        results, _ = _many(db_session, battery.values())

        assert len(battery) == _SHAPES
        assert list(results) == [str(i) for i in battery.values()]
        assert _shapes_that_differ(db_session, battery, results) == []

    def test_the_shapes_reach_what_they_are_named_for(self, db_session, battery):
        """Worked out by hand from ``_fill``, so that two results that agree
        agree on the right numbers."""
        results, _ = _many(db_session, battery.values())
        by_shape = {shape: results[str(i)] for shape, i in battery.items()}

        def summary(shape):
            return by_shape[shape]["summary"]

        def arms(shape, metric, column):
            (found,) = [
                m for m in by_shape[shape]["metrics"] if m["metric_name"] == metric
            ]
            return [v[column] for v in found["variants"]]

        assert by_shape["no control"] is None
        assert by_shape[_UNKNOWN] is None
        assert summary("significant")["recommendation"] == "SHIP_VARIANT"
        assert summary("significant")["total_conversions"] == 25 + 52
        assert arms("significant", "Purchase", "conversions") == [10, 35]
        assert arms("significant", "Signup", "conversions") == [20, 22]
        assert summary("sample ratio")["recommendation"] == "INCONCLUSIVE"
        assert by_shape["bandit"]["srm"] is None
        assert by_shape["three arms, sample ratio"]["srm"]["warning"] is True
        assert len(by_shape["no allocation"]["srm"]["expected"]) == 2
        assert by_shape["only a control"]["srm"] is None
        assert by_shape["nothing"]["metrics"] == []
        assert by_shape["no metrics"]["metrics"] == []
        assert summary("no metrics")["total_conversions"] == 6
        assert summary("no metrics")["total_events"] == 9
        assert arms("three variants", "Purchase", "conversions") == [6, 12, 5]
        assert summary("three variants")["total_conversions"] == 23
        assert summary("three variants")["total_users"] == 120
        assert arms("lower is better", "Refund", "conversions") == [30, 8]
        assert arms("beyond the bound", "Revenue", "conversions") == [4, 2]
        assert arms("beyond the bound", "Revenue", "mean") == [
            6.5 / 20,
            (VALUE_BOUND + 1.0) / 20,
        ]
        assert arms("shared users, first", "Purchase", "conversions") == [3, 4]
        assert arms("shared users, first", "Revenue", "mean") == [0.6, 1.6]
        assert summary("shared users, first")["total_conversions"] == 7
        assert summary("shared users, second")["total_conversions"] == 10
        shape = "legacy count and revenue"
        assert arms(shape, "Conversions", "conversions") == [4, 11]
        assert arms(shape, "Conversions", "mean") == [7 / 20, 21 / 20]
        assert arms(shape, "Value", "mean") == [4.25 / 20, 26 / 20]
        assert summary(shape)["total_conversions"] == 15
        shape = "legacy beside named"
        assert arms(shape, "Purchase", "conversions") == [3, 8]
        assert arms(shape, "Any", "conversions") == [4, 3]
        assert summary(shape)["total_conversions"] == 11
        assert summary("deleted metric")["total_conversions"] == 8
        assert [
            by_shape["stored settings"][key]
            for key in ("correction_method", "confidence_level")
        ] == ["bonferroni", 0.9]
        assert by_shape["bayesian"]["bayesian_results"] is None
        assert summary("another metric's event")["total_conversions"] == 6
        assert arms("duration and custom", "Load", "mean") == [4.5, 0.6]
        assert arms("duration and custom", "Score", "mean") == [0.1, 0.6]

    def test_the_statements_do_not_depend_on_what_the_group_holds(
        self, db_session, battery, monkeypatch
    ):
        """One group of every shape: the experiments, the assigned users, the
        events, the metric events, the legacy events, the assignment lookup and
        the assigned users per variant. A read that does not go through the
        batch runs once per experiment, and fails here.
        """
        at_the_shipped_size = _many(db_session, battery.values())[1]
        assert len(at_the_shipped_size) == 7 * 3

        monkeypatch.setattr(
            "backend.app.services.results_batch.RESULTS_BATCH_SIZE", _SHAPES
        )
        in_one_group = _many(db_session, battery.values())[1]
        assert len(in_one_group) == 7, in_one_group
        # Nothing is written: no Bayesian decision, no snapshot.
        assert {s.split(None, 1)[0] for s in in_one_group} == {"SELECT"}

    @pytest.mark.parametrize(
        "shapes, statements",
        [
            ((), 0),
            ((_UNKNOWN,), 1),
            # The three reads of the group, the metric events (none matches) and
            # the assigned users per variant.
            (("no conversions", _UNKNOWN), 3 + 2),
            # ... and the assignments of those who sent one.
            (("significant", "sample ratio"), 3 + 3),
            # No metric event is read when no metric names one.
            (("no metrics", "legacy count and revenue", "nothing"), 3 + 3),
            (("nothing",), 3 + 2),
            # A metric that names its event beside one that does not.
            (("legacy beside named",), 3 + 4),
        ],
    )
    def test_the_statements_of_one_group(self, db_session, battery, shapes, statements):
        ids = [battery[shape] for shape in shapes]
        results, ran = _many(db_session, ids)

        assert list(results) == [str(i) for i in ids]
        assert len(ran) == statements, ran
        named = [shape for shape in shapes if shape not in _NO_EVENT_NAME]
        assert any("events.event_name" in s for s in ran) is bool(named)

    def test_one_assignment_lookup_for_each_chunk_of_users(
        self, db_session, battery, monkeypatch
    ):
        """77 and 26 users sent a metric event in these two experiments: three
        lookups of 40 users at most, and the second holds users of both."""
        monkeypatch.setattr(
            "backend.app.services.results_batch.ASSIGNMENT_LOOKUP_CHUNK", 40
        )
        shapes = {s: battery[s] for s in ("significant", "sample ratio")}
        results, ran = _many(db_session, shapes.values())

        assert len(ran) == 3 + 1 + 3 + 1, ran
        assert _shapes_that_differ(db_session, shapes, results) == []

    def test_the_rows_are_read_in_sub_groups(self, db_session, battery, monkeypatch):
        """At a budget of 300 the battery is read in 10 sub-groups, in order:
        each takes experiments while their weights add up to 300 or less, and
        "significant" (200 users, 2 event names) is over it and read alone.
        """
        sub_groups = _SubGroups(monkeypatch, battery)
        monkeypatch.setattr(
            "backend.app.services.results_batch.RESULTS_BATCH_SIZE", _SHAPES
        )
        monkeypatch.setattr(
            "backend.app.services.results_batch.RESULTS_ROW_BUDGET", 300
        )

        results, ran = _many(db_session, battery.values())

        assert sub_groups.read == [shapes for shapes, _ in _SUB_GROUPS]
        assert len(sub_groups.read) == 10
        assert len(ran) == 3 + sum(1 + n for _, n in _SUB_GROUPS) == 35
        assert sub_groups.counted == set(battery) - {
            "no assignments",
            "nothing",
            _UNKNOWN,
        }
        assert _shapes_that_differ(db_session, battery, results) == []

        # The one over the budget, on its own. (pytest keeps the unknown id's
        # log record, and with it the last call's frames and last rows.)
        sub_groups.rows.clear()
        alone, ran = _many(db_session, [battery["significant"]])
        assert len(ran) == 3 + 3
        only = {"significant": battery["significant"]}
        assert _shapes_that_differ(db_session, only, alone) == []

    @pytest.mark.parametrize("limit, statements", [(399, 35 - 3 + 20), (400, 35)])
    def test_an_experiment_over_the_limit_is_computed_with_its_own_reads(
        self, db_session, battery, monkeypatch, limit, statements
    ):
        """The shape "significant" is expected to read 400 rows. Over a limit of
        399 the batch reads no row for it, and it costs the 20 statements
        ``/results`` makes instead of its sub-group's 3; at 400 it is a
        sub-group.
        """
        from backend.app.services import results_batch

        assert results_batch.RESULTS_ROW_BUDGET < results_batch.RESULTS_BATCH_ROW_LIMIT
        sub_groups = _SubGroups(monkeypatch, battery)
        monkeypatch.setattr(results_batch, "RESULTS_BATCH_SIZE", _SHAPES)
        monkeypatch.setattr(results_batch, "RESULTS_ROW_BUDGET", 300)
        monkeypatch.setattr(results_batch, "RESULTS_BATCH_ROW_LIMIT", limit)

        results, ran = _many(db_session, battery.values())

        over = ["significant"] if limit < 400 else []
        assert sub_groups.read == [s for s, _ in _SUB_GROUPS if s != over]
        assert len(ran) == statements
        assert _shapes_that_differ(db_session, battery, results) == []
        _, own = _sql(
            db_session, lambda session: _single(session, battery["significant"])
        )
        assert len(own) == 20

    def test_an_event_name_is_matched_as_it_is_stored(
        self, db_session, make_experiment
    ):
        """Names that differ only by case or by a space are different names."""
        names = ("purchase", "Purchase", " purchase", "purchase ")
        made = _Battery(db_session, make_experiment)
        try:
            made.experiment(
                "names",
                [
                    (f"Metric {i}", name, MetricType.CONVERSION, {"is_primary": i == 0})
                    for i, name in enumerate(names)
                ],
            )
            u = made.users("names", control=20, treatment=20)
            for i, name in enumerate(names):
                made.send("names", "control", u["control"][5 * i : 6 * i + 1], name)
                made.send("names", "treatment", u["treatment"][: 2 * i + 2], name)
            made.send("names", "control", u["control"][19:], "PURCHASE")
            db_session.commit()

            results, _ = _many(db_session, made.ids.values())

            found = results[str(made.ids["names"])]
            assert {
                m["metric_name"]: [v["conversions"] for v in m["variants"]]
                for m in found["metrics"]
            } == {f"Metric {i}": [i + 1, 2 * i + 2] for i in range(4)}
            assert found["summary"]["total_conversions"] == 10 + 8
            assert _shapes_that_differ(db_session, made.ids, results) == []
        finally:
            _delete_experiments(db_session, list(made.ids.values()))

    def test_a_repeated_id_is_answered_once_in_the_first_one_s_place(
        self, db_session, battery
    ):
        ids = [battery[s] for s in ("bandit", _UNKNOWN, "significant")]
        results, _ = _many(db_session, [*ids, ids[0], ids[1]])
        assert list(results) == [str(i) for i in ids]


def _one_by_one(service, experiment_ids):
    """``get_experiment_results_many`` as the export used to compute it."""
    return {
        str(experiment_id): _single(service.db, experiment_id)
        for experiment_id in experiment_ids
    }


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
def test_the_bodies_are_the_bodies_of_one_experiment_at_a_time(
    admin_client, db_session, battery, monkeypatch
):
    """Every export and report of the battery, as JSON and CSV, byte for byte."""
    ids = [i for shape, i in battery.items() if shape != _UNKNOWN]
    start = _period_of_their_own(db_session, ids)
    period = {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(seconds=len(ids))).isoformat(),
    }

    requests = [
        (path, {**period, "format": fmt})
        for path in ("experiments", "variants")
        for fmt in ("json", "csv")
    ] + [
        (f"reports/experiments/{experiment_id}", {"format": fmt})
        for experiment_id in ids
        for fmt in ("json", "csv")
    ]

    def get(path, params):
        db_session.expire_all()
        response = admin_client.get(f"/api/v1/export/{path}", params=params)
        assert response.status_code == 200, response.text
        return response

    def bodies():
        found = {(path, p["format"]): get(path, p).content for path, p in requests}
        # The overview says when it was made.
        overview = get("reports/overview", period).json()
        overview.pop("generated_at")
        found["reports/overview", "json"] = json.dumps(overview).encode()
        return found

    batched = bodies()
    monkeypatch.setattr(AnalysisService, "get_experiment_results_many", _one_by_one)
    one_by_one = bodies()

    assert len(json.loads(batched["experiments", "json"])) == len(ids)
    overview = json.loads(batched["reports/overview", "json"])
    assert overview["total_experiments"] == len(ids)
    assert [k for k in batched if batched[k] != one_by_one[k]] == []


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.regression
@pytest.mark.parametrize("moment", ["load_rows", "metric_rows", "assigned_pairs"])
def test_a_user_stored_while_the_export_runs_is_never_converted_but_unassigned(
    db_session, make_ab_experiment, monkeypatch, moment
):
    """The second experiment is in a later sub-group, every user of it has
    converted, and one more arrives while the first is computed, just before
    its event rows are read, or (a user whose event came first) just before its
    assignments are looked up: its variant still has no more converting users
    than assigned ones, so it has results.
    """
    from backend.app.services import results_batch

    first, second = _seeded(db_session, make_ab_experiment, 2)
    for variant in _variants(second):
        for user_id in (f"{variant.name}-late-{i}" for i in range(5)):
            db_session.add(
                Assignment(
                    experiment_id=second.id, variant_id=variant.id, user_id=user_id
                )
            )
    db_session.commit()
    ids = [first.id, second.id]
    everyone = (
        db_session.query(Assignment.user_id, Assignment.variant_id)
        .filter(Assignment.experiment_id == second.id)
        .all()
    )

    def convert(session, user_id, variant_id):
        session.add(
            Event(
                event_type="purchase",
                event_name="purchase",
                user_id=user_id,
                experiment_id=second.id,
                variant_id=variant_id,
                value=1.0,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    for user_id, variant_id in everyone:
        convert(db_session, user_id, variant_id)
    arm = _variants(second)[0].id
    event_first = moment == "assigned_pairs"
    if event_first:
        convert(db_session, "arrives", arm)
    db_session.commit()

    read = getattr(results_batch, moment)
    calls = []

    def arriving(db, wanted):
        calls.append(len(wanted))
        if len(calls) == 2:
            with Session(bind=db_session.get_bind()) as other:
                other.add(
                    Assignment(experiment_id=ids[1], variant_id=arm, user_id="arrives")
                )
                if not event_first:
                    convert(other, "arrives", arm)
                other.commit()
        return read(db, wanted)

    monkeypatch.setattr(results_batch, moment, arriving)
    monkeypatch.setattr(results_batch, "RESULTS_ROW_BUDGET", 1)
    results, _ = _many(db_session, ids)

    assert calls == [1, 1]
    found = results[str(second.id)]
    assert found is not None
    for metric in found["metrics"]:
        for variant in metric["variants"]:
            assert variant["conversions"] <= variant["sample_size"], variant

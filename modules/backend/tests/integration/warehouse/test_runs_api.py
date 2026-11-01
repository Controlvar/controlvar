"""Warehouse analysis runs end to end: the API, the runner and a DuckDB warehouse."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Metric, MetricType
from backend.app.services.analysis_service import AnalysisService
from modules.backend.app.models.warehouse_analysis_run import WarehouseAnalysisRun
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.tests.integration.warehouse import recording_estimator
from modules.backend.tests.integration.warehouse.conftest import WA, ts

pytestmark = [pytest.mark.integration, pytest.mark.modules]


def _exposure(user, key, variant, when):
    return (user, key, variant, when)


def _standard_data(wh, key: str) -> None:
    """Six units: three per variant; two control and one treatment convert."""
    wh.exposures(
        [
            _exposure("u1", key, "control", ts(2)),
            _exposure("u2", key, "control", ts(2)),
            _exposure("u3", key, "control", ts(3)),
            _exposure("u4", key, "treatment", ts(2)),
            _exposure("u5", key, "treatment", ts(3)),
            _exposure("u6", key, "treatment", ts(3)),
            _exposure("x1", "another-experiment", "control", ts(2)),
        ]
    )
    wh.events(
        [
            ("u1", ts(2, 5), None),
            ("u1", ts(2, 6), None),
            ("u2", ts(4), None),
            ("u4", ts(3), None),
        ]
    )


def test_proportion_run_end_to_end(wh):
    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("DEVELOPER")
    response = wh.start_run(client, experiment, ids)
    assert response.status_code == 202, response.text
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["status"] == "succeeded", run

    # The estimator is given the counts, control first, and the run's alpha.
    assert len(recording_estimator.CALLS) == 1
    call = recording_estimator.CALLS[0]
    assert call["alpha"] == pytest.approx(0.05)
    assert call["correction_method"] == "none"
    assert call["metric"] == {
        "id": ids["metric_source_id"],
        "name": "Purchases",
        "metric_type": "conversion",
        "is_primary": True,
        "lower_is_better": False,
    }
    assert [
        (v["variant_name"], v["n"], v["n_converted"]) for v in call["variants"]
    ] == [
        ("control", 3, 2),
        ("treatment", 3, 1),
    ]
    results = run["results"]
    metric = results["metrics"][0]
    assert metric["computed"] is True and metric["is_primary"] is True
    assert metric["result"]["metric_id"] == ids["metric_source_id"]
    assert [v["sample_size"] for v in metric["result"]["variants"]] == [3, 3]
    assert metric["result"]["variants"][1]["statistical_test_used"] == "fisher_exact"
    assert results["srm"]["observed"] == {str(v.id): 3 for v in experiment.variants}
    assert results["diagnostics"]["units"] == 6
    # View SQL: every statement sent, with its hash.
    assert [s["kind"] for s in run["statements"]] == ["diagnostics", "metric"]
    assert all(len(s["sha256"]) == 64 for s in run["statements"])
    assert run["window_start"].startswith("2026-09-01T00:00:00")
    assert run["window_end"].startswith("2026-09-10T00:00:00")


def _seed_results(wh, experiment, rows, *, is_primary=False, lower_is_better=None):
    """The same units in the platform's own tables, as /results reads them."""
    variants = {v.name: v for v in experiment.variants}
    metric = Metric(
        experiment_id=experiment.id,
        name="Purchases",
        event_name="purchase",
        metric_type=MetricType.CONVERSION,
        is_primary=is_primary,
        lower_is_better=lower_is_better,
    )
    wh.db.add(metric)
    for user, variant, conversions in rows:
        wh.db.add(
            Assignment(
                experiment_id=experiment.id,
                variant_id=variants[variant].id,
                user_id=user,
            )
        )
        for when in conversions:
            wh.db.add(
                Event(
                    event_type="track",
                    event_name="purchase",
                    user_id=user,
                    experiment_id=experiment.id,
                    variant_id=variants[variant].id,
                    created_at=when.isoformat(),
                    value=1.0,
                )
            )
    wh.db.commit()
    wh.db.refresh(metric)
    return metric


def _results_counts(wh, experiment, metric):
    raw = AnalysisService(wh.db).calculate_metric_results(experiment, metric)
    return {
        v["variant_name"]: (v["sample_size"], v["conversions"])
        for v in raw["variant_results"]
    }


def _warehouse_counts():
    return {
        v["variant_name"]: (v["n"], v["n_converted"])
        for v in recording_estimator.CALLS[-1]["variants"]
    }


def _run(wh, experiment, rows):
    wh.exposures([(u, experiment.key, variant, ts(2)) for u, variant, _ in rows])
    wh.events([(u, when, None) for u, _, whens in rows for when in whens])
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("DEVELOPER")
    response = wh.start_run(client, experiment, ids)
    assert response.status_code == 202, response.text
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["status"] == "succeeded", run


def test_proportion_counts_match_results_on_restricted_fixture(wh):
    """With no pre-exposure event, no unit in two variants and no event outside
    the window, the warehouse counts equal /results' counts."""
    experiment = wh.experiment(end=ts(10))
    rows = [
        ("r1", "control", [ts(3), ts(4)]),
        ("r2", "control", []),
        ("r3", "control", [ts(5)]),
        ("r4", "treatment", [ts(3)]),
        ("r5", "treatment", []),
    ]
    metric = _seed_results(wh, experiment, rows)
    _run(wh, experiment, rows)
    assert (
        _warehouse_counts()
        == _results_counts(wh, experiment, metric)
        == {
            "control": (3, 2),
            "treatment": (2, 1),
        }
    )


#: A significant drop (Fisher p = 0.0011, below the run's alpha of 0.1): nine
#: of ten control units convert and one of ten treatment units.  A fixture
#: whose change is not significant names no winner in either direction, so
#: the winner would agree for the wrong reason.
SIGNIFICANT_DROP = [
    (f"c{i}", "control", [ts(3), ts(4)] if i < 9 else []) for i in range(10)
] + [(f"t{i}", "treatment", [ts(5)] if i < 1 else []) for i in range(10)]


@pytest.mark.parametrize("lower", [False, True], ids=["higher", "lower"])
def test_proportion_results_equal_results_on_restricted_fixture(wh, lower):
    """On the counts /results also computes, the warehouse result is /results'
    result: every variant's numbers, significance and winner (only the metric's
    id differs -- a warehouse source is not a platform metric), in either
    direction, the source and the platform metric marked alike (#1386)."""
    import json

    experiment = wh.experiment(end=ts(10))
    rows = SIGNIFICANT_DROP
    _seed_results(wh, experiment, rows, is_primary=True, lower_is_better=lower)
    wh.exposures([(u, experiment.key, v, ts(2)) for u, v, _ in rows])
    wh.events([(u, w, None) for u, _, ws in rows for w in ws])
    ids = wh.ready(wh.as_("ADMIN"), lower_is_better=lower)
    client = wh.as_("ADMIN")
    response = wh.start_run(
        client, experiment, ids, confidence_level=0.9, correction_method="bonferroni"
    )
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["status"] == "succeeded", run
    warehouse = dict(run["results"]["metrics"][0]["result"])
    treatment = next(v for v in experiment.variants if not v.is_control)
    assert warehouse["lower_is_better"] is lower
    assert warehouse["winning_variant_id"] == (str(treatment.id) if lower else None)
    results = AnalysisService(wh.db).get_experiment_results(
        experiment.id, confidence_level=0.9, correction_method="bonferroni"
    )["metrics"][0]
    results = json.loads(json.dumps(results, default=str))
    assert warehouse.pop("metric_id") == ids["metric_source_id"]
    results.pop("metric_id")
    assert warehouse == results


def test_documented_difference_from_results(wh):
    """An event before the unit's first exposure converts it in /results but not
    in the warehouse: the stated definitional difference."""
    experiment = wh.experiment(end=ts(10))
    rows = [
        ("d1", "control", [ts(1, 12)]),  # before its exposure on the 2nd
        ("d2", "treatment", [ts(3)]),
    ]
    metric = _seed_results(wh, experiment, rows)
    _run(wh, experiment, rows)
    assert _results_counts(wh, experiment, metric)["control"] == (1, 1)
    assert _warehouse_counts()["control"] == (1, 0)
    assert (
        _warehouse_counts()["treatment"]
        == _results_counts(wh, experiment, metric)["treatment"]
    )


def test_run_stores_resolved_utc_window(wh):
    """A naive start_date is UTC: the stored window starts at that instant."""
    experiment = wh.experiment(start=datetime(2026, 9, 1, 6, 30), end=None)
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    response = wh.start_run(client, experiment, ids)
    assert response.status_code == 202, response.text
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["window_start"] in ("2026-09-01T06:30:00Z", "2026-09-01T06:30:00+00:00")
    assert run["window_end"].startswith("2026-09-20T12:00:00")  # the harness clock
    assert "'2026-09-01 06:30:00+00:00'" in run["statements"][0]["sql"]


def test_explicit_window_with_no_offset_is_read_as_utc(wh):
    experiment = wh.experiment()
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    response = wh.start_run(
        client,
        experiment,
        ids,
        window_start="2026-09-02T00:00:00",
        window_end="2026-09-03T00:00:00+02:00",
    )
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["window_start"].startswith("2026-09-02T00:00:00")
    assert run["window_end"].startswith("2026-09-02T22:00:00")


def test_experiment_without_window_422(wh):
    experiment = wh.experiment()
    experiment.start_date = None
    wh.db.commit()
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    response = wh.start_run(wh.as_("ADMIN"), experiment, ids)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "experiment_has_no_window"
    assert (
        wh.db.query(WarehouseAnalysisRun)
        .filter(WarehouseAnalysisRun.experiment_id == experiment.id)
        .count()
        == 0
    )


def test_srm_uses_traffic_allocation(wh):
    experiment = wh.experiment(end=ts(10), allocations=(90, 10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    run = wh.wait_for_run(
        client, wh.start_run(client, experiment, ids).json()["run_id"]
    )
    srm = run["results"]["srm"]
    control = next(v for v in experiment.variants if v.is_control)
    assert srm["expected"][str(control.id)] == pytest.approx(6 * 0.9)
    assert run["results"]["srm_skipped"] is None


def test_srm_none_for_bandit(wh):
    experiment = wh.experiment(end=ts(10), optimization_type="thompson_sampling")
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    run = wh.wait_for_run(
        client, wh.start_run(client, experiment, ids).json()["run_id"]
    )
    assert run["results"]["srm"] is None
    assert run["results"]["srm_skipped"] == "adaptive_allocation"


def test_variant_labels_truncated_in_python(wh):
    """A variant named with more than 64 characters matches the label the SQL
    cut to 64, and is reported as the experiment's variant."""
    experiment = wh.experiment(end=ts(10))
    long_name = "treatment-" + "x" * 80
    treatment = next(v for v in experiment.variants if not v.is_control)
    treatment.name = long_name[:100]
    wh.db.commit()
    wh.exposures(
        [
            ("a", experiment.key, "control", ts(2)),
            ("b", experiment.key, long_name, ts(2)),
        ]
    )
    wh.events([("b", ts(3), None)])
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    run = wh.wait_for_run(
        client, wh.start_run(client, experiment, ids).json()["run_id"]
    )
    by_name = {v["variant_name"]: v for v in run["results"]["variants"]}
    assert by_name[long_name[:100]]["labels"] == [long_name[:64]]
    assert by_name[long_name[:100]]["units"] == 1
    assert run["results"]["unmapped_labels"] == []


def test_variant_map_names_the_variant_of_a_label(wh):
    experiment = wh.experiment(end=ts(10))
    wh.exposures([("a", experiment.key, "A", ts(2)), ("b", experiment.key, "B", ts(2))])
    wh.events([("b", ts(3), None)])
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    variants = {v.name: str(v.id) for v in experiment.variants}
    response = wh.start_run(
        client,
        experiment,
        ids,
        variant_map={"A": variants["control"], "B": variants["treatment"]},
    )
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert _warehouse_counts() == {"control": (1, 0), "treatment": (1, 1)}
    assert run["request"]["variant_map"] == {
        "A": variants["control"],
        "B": variants["treatment"],
    }


def test_unmapped_labels_are_reported_not_counted(wh):
    experiment = wh.experiment(end=ts(10))
    wh.exposures(
        [
            ("a", experiment.key, "control", ts(2)),
            ("b", experiment.key, "treatment", ts(2)),
            ("c", experiment.key, "holdout", ts(2)),
        ]
    )
    wh.events([])
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    run = wh.wait_for_run(
        client, wh.start_run(client, experiment, ids).json()["run_id"]
    )
    assert run["results"]["unmapped_labels"] == [{"label": "holdout", "units": 1}]
    assert _warehouse_counts() == {"control": (1, 0), "treatment": (1, 0)}


def test_a_metric_that_cannot_be_computed_says_why_never_zero(wh):
    """Metric rows in the window that match no exposed unit: that metric is
    "Not computed" with its reason; the run itself succeeds."""
    experiment = wh.experiment(end=ts(10))
    wh.exposures([("u1", experiment.key, "control", ts(2))])
    wh.events([("someone-else", ts(3), None)])
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    run = wh.wait_for_run(
        client, wh.start_run(client, experiment, ids).json()["run_id"]
    )
    assert run["status"] == "succeeded"
    metric = run["results"]["metrics"][0]
    assert metric["computed"] is False
    assert metric["not_computed_reason"] == "join_key_mismatch"
    assert metric["message"].startswith("Not computed: ")
    assert metric["result"] is None
    # Nothing was analysed, so nothing states a direction: the direction is
    # the computed result's ``lower_is_better``, never the wrapper's (#1386).
    assert "lower_is_better" not in metric
    assert recording_estimator.CALLS == []


def test_a_failed_run_reports_its_code_and_no_numbers(wh):
    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    wh.fail_with = WarehouseError(
        WarehouseErrorCode.PERMISSION_DENIED, warehouse="athena"
    )
    response = wh.start_run(wh.as_("ADMIN"), experiment, ids)
    # Authentication is the app-global override, so the reader's client is
    # made after the ADMIN call; made before it, it would act as ADMIN.
    client = wh.as_("ANALYST")
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["status"] == "failed"
    assert run["error_code"] == "permission_denied"
    assert run["error_message"] == (
        "The warehouse role does not have permission for this query."
    )
    assert run["results"] is None
    row = wh.db.get(WarehouseAnalysisRun, run["id"])
    wh.db.refresh(row)
    assert row.results is None and row.sufficient_statistics is None


def _mean_source(wh, admin, ids, **overrides):
    mean = wh.metric_source(
        admin,
        ids["connection_id"],
        name="Revenue",
        metric_type="mean",
        columns={"unit_id": "user_id", "event_at": "event_at", "value": "amount"},
        **overrides,
    )
    wh.validate(admin, mean["id"])
    return mean


def _run_mean(wh, experiment, admin, ids, mean):
    response = admin.post(
        f"{WA}/experiments/{experiment.id}/runs",
        json={
            "connection_id": ids["connection_id"],
            "assignment_source_id": ids["assignment_source_id"],
            "metric_source_ids": [mean["id"]],
        },
    )
    assert response.status_code == 202, response.text
    # ANALYST is the lowest role that reads a run (D50).
    run = wh.wait_for_run(wh.as_("ANALYST"), response.json()["run_id"])
    assert run["status"] == "succeeded", run
    (metric,) = run["results"]["metrics"]
    return run, metric


def _revenue_data(wh, key: str) -> None:
    """Control units spend 15, 20 and 0; treatment units 7.5, 2.5 and 0."""
    wh.exposures(
        [
            _exposure("u1", key, "control", ts(2)),
            _exposure("u2", key, "control", ts(2)),
            _exposure("u3", key, "control", ts(3)),
            _exposure("u4", key, "treatment", ts(2)),
            _exposure("u5", key, "treatment", ts(3)),
            _exposure("u6", key, "treatment", ts(3)),
        ]
    )
    wh.events(
        [
            ("u1", ts(2, 5), 10.0),
            ("u1", ts(2, 6), 5.0),
            ("u2", ts(4), 20.0),
            ("u4", ts(3), 7.5),
            ("u5", ts(4), 2.5),
            # A NULL value is ignored (and counted), not read as 0 or refused.
            ("u6", ts(4), None),
            # Before the unit's exposure: not counted.
            ("u3", ts(1), 99.0),
        ]
    )


def test_mean_run_end_to_end(wh):
    """A mean metric runs like a proportion one: its centred sums come back,
    the core estimator computes Welch's test from them, and the numbers agree
    with the same test on the units' values."""
    from scipy import stats

    admin = wh.as_("ADMIN")
    experiment = wh.experiment(end=ts(10))
    _revenue_data(wh, experiment.key)
    ids = wh.ready(admin)
    mean = _mean_source(wh, admin, ids)
    run, metric = _run_mean(wh, experiment, admin, ids, mean)

    assert metric["computed"] is True and metric["metric_type"] == "mean"
    result = metric["result"]
    assert result["metric_type"] == "mean"
    control, treatment = result["variants"]
    assert (control["variant_name"], treatment["variant_name"]) == (
        "control",
        "treatment",
    )
    assert (control["sample_size"], treatment["sample_size"]) == (3, 3)
    assert control["mean"] == pytest.approx(35 / 3, rel=1e-12)
    assert treatment["mean"] == pytest.approx(10 / 3, rel=1e-12)
    control_y, treatment_y = [15.0, 20.0, 0.0], [7.5, 2.5, 0.0]
    welch = stats.ttest_ind(treatment_y, control_y, equal_var=False)
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["note"] is None and control["note"] is None
    assert control["conversions"] is None
    stored = run["results"]
    assert stored["srm"]["observed"] == {str(v.id): 3 for v in experiment.variants}
    row = wh.db.get(WarehouseAnalysisRun, run["id"])
    wh.db.refresh(row)
    sums = row.sufficient_statistics["metrics"][mean["id"]]
    assert sums["null_value_rows"] == 1
    # The exact strings the warehouse returned are what is stored.
    assert all(isinstance(v["sum_d"], str) for v in sums["variants"])


def test_mean_run_caps_each_units_sum(wh):
    admin = wh.as_("ADMIN")
    experiment = wh.experiment(end=ts(10))
    _revenue_data(wh, experiment.key)
    ids = wh.ready(admin)
    mean = _mean_source(wh, admin, ids, cap_value=12.0)
    _, metric = _run_mean(wh, experiment, admin, ids, mean)
    control, treatment = metric["result"]["variants"]
    assert control["mean"] == pytest.approx(24 / 3, rel=1e-12)
    assert treatment["mean"] == pytest.approx(10 / 3, rel=1e-12)


def test_mean_no_variation_note_reaches_the_response(wh):
    """Every unit's value is 0: the treatment's row says why there is no
    p-value, in the run the API returns."""
    admin = wh.as_("ADMIN")
    experiment = wh.experiment(end=ts(10))
    wh.exposures(
        [
            ("u1", experiment.key, "control", ts(2)),
            ("u2", experiment.key, "control", ts(2)),
            ("u3", experiment.key, "treatment", ts(2)),
            ("u4", experiment.key, "treatment", ts(2)),
        ]
    )
    wh.events([])
    ids = wh.ready(admin)
    mean = _mean_source(wh, admin, ids)
    _, metric = _run_mean(wh, experiment, admin, ids, mean)
    assert metric["computed"] is True
    control, treatment = metric["result"]["variants"]
    assert treatment["note"] == "Not computed: no variation"
    assert treatment["p_value"] is None
    assert control["note"] is None


def test_mean_with_fewer_than_2_units_says_so(wh):
    admin = wh.as_("ADMIN")
    experiment = wh.experiment(end=ts(10))
    wh.exposures(
        [
            ("u1", experiment.key, "control", ts(2)),
            ("u2", experiment.key, "control", ts(2)),
            ("u3", experiment.key, "treatment", ts(2)),
        ]
    )
    wh.events([("u1", ts(3), 4.0)])
    ids = wh.ready(admin)
    mean = _mean_source(wh, admin, ids)
    _, metric = _run_mean(wh, experiment, admin, ids, mean)
    assert metric["computed"] is False and metric["result"] is None
    assert metric["not_computed_reason"] == "fewer_than_2_units"
    assert metric["message"] == "Not computed: fewer than 2 units"


def test_unvalidated_sources_are_refused(wh):
    admin = wh.as_("ADMIN")
    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    connection = wh.athena_connection(admin)
    assignment = wh.assignment_source(admin, connection["id"])
    metric = wh.metric_source(admin, connection["id"])
    response = wh.start_run(
        admin,
        experiment,
        {
            "connection_id": connection["id"],
            "assignment_source_id": assignment["id"],
            "metric_source_id": metric["id"],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "source_not_validated"


def test_runs_list_newest_first_for_every_role(wh):
    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    first = wh.start_run(client, experiment, ids).json()["run_id"]
    wh.wait_for_run(client, first)
    wh.now = wh.now.replace(minute=5)
    second = wh.start_run(client, experiment, ids).json()["run_id"]
    wh.wait_for_run(client, second)
    for role in ("ADMIN", "DEVELOPER", "ANALYST"):
        listed = wh.as_(role).get(f"{WA}/experiments/{experiment.id}/runs")
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()["runs"]] == [second, first]
    # D50: VIEWER does not read warehouse analyses.
    listed = wh.as_("VIEWER").get(f"{WA}/experiments/{experiment.id}/runs")
    assert listed.status_code == 403, listed.text
    assert listed.json()["detail"]["code"] == "role_required"
    assert "runs" not in listed.json()


def test_the_run_response_never_carries_rows(wh):
    """Results are aggregates: no unit id from the warehouse appears."""
    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    response = client.get(
        f"{WA}/runs/{wh.start_run(client, experiment, ids).json()['run_id']}"
    )
    run = wh.wait_for_run(client, response.json()["id"])
    text = str(run)
    for unit in ("u1", "u2", "u3", "u4", "u5", "u6", "x1"):
        assert f"'{unit}'" not in text


def test_completed_runs_record_when(wh):
    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("ADMIN")
    run = wh.wait_for_run(
        client, wh.start_run(client, experiment, ids).json()["run_id"]
    )
    assert run["finished_at"] is not None and run["started_at"] is not None
    assert (
        datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00")).tzinfo
        == timezone.utc
    )


def test_starting_a_run_is_audited(wh):
    from sqlalchemy import text

    experiment = wh.experiment(end=ts(10))
    _standard_data(wh, experiment.key)
    ids = wh.ready(wh.as_("ADMIN"))
    client = wh.as_("DEVELOPER")
    run_id = wh.start_run(client, experiment, ids).json()["run_id"]
    wh.wait_for_run(client, run_id)
    rows = wh.db.execute(
        text(
            "SELECT action, actor_id, new_value FROM test_experimentation.audit_events_v2 "
            "WHERE resource_type = 'warehouse_analysis_run' AND resource_id = :id"
        ),
        {"id": run_id},
    ).all()
    assert len(rows) == 1
    assert str(rows[0].actor_id) == str(wh.user("DEVELOPER").id)
    assert rows[0].new_value["metric_source_ids"] == [ids["metric_source_id"]]
    assert "total_seconds" not in rows[0].new_value


# ---------------------------------------------------------------------------
# A metric source's direction (#1386)
# ---------------------------------------------------------------------------

#: Per unit, the summed value of a mean metric: the treatment's mean is about
#: half the control's, a significant drop with ten units a variant.
CONTROL_VALUES = [10.0, 11.0, 9.0, 10.0, 12.0, 8.0, 10.0, 11.0, 9.0, 10.0]
TREATMENT_VALUES = [5.0, 6.0, 4.0, 5.0, 7.0, 3.0, 5.0, 6.0, 4.0, 5.0]


def _a_significant_drop(wh, key: str, kind: str) -> None:
    if kind == "proportion":
        rows = SIGNIFICANT_DROP
        wh.exposures([(u, key, variant, ts(2)) for u, variant, _ in rows])
        wh.events([(u, when, None) for u, _, whens in rows for when in whens])
        return
    units = [(f"c{i}", "control", y) for i, y in enumerate(CONTROL_VALUES)] + [
        (f"t{i}", "treatment", y) for i, y in enumerate(TREATMENT_VALUES)
    ]
    wh.exposures([(u, key, variant, ts(2)) for u, variant, _ in units])
    wh.events([(u, ts(3), y) for u, _, y in units])


def _metric_body(kind: str) -> dict:
    if kind == "proportion":
        return {"name": "Checkout errors"}
    return {
        "name": "Support minutes",
        "metric_type": "mean",
        "columns": {"unit_id": "user_id", "event_at": "event_at", "value": "amount"},
    }


def _treatment(experiment):
    return next(v for v in experiment.variants if not v.is_control)


def _finished(wh, client, response):
    assert response.status_code == 202, response.text
    run = wh.wait_for_run(client, response.json()["run_id"])
    assert run["status"] == "succeeded", run
    return run


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["proportion", "mean"])
@pytest.mark.parametrize("lower", [True, False], ids=["lower", "higher"])
def test_a_lower_is_better_source_names_a_significant_drop_the_winner(wh, kind, lower):
    """Through the API: the source stores its direction, and a run names the
    treatment with a significant drop the winner only when the source is
    lower is better.  Before #1386 the source API refused the field, and every
    run read every metric as higher is better."""
    experiment = wh.experiment(end=ts(10))
    _a_significant_drop(wh, experiment.key, kind)
    admin = wh.as_("ADMIN")
    ids = wh.ready(admin, lower_is_better=lower, **_metric_body(kind))
    stored = admin.get(f"{WA}/sources/{ids['metric_source_id']}")
    assert stored.json()["lower_is_better"] is lower

    run = _finished(wh, admin, wh.start_run(admin, experiment, ids))

    (metric,) = run["results"]["metrics"]
    result = metric["result"]
    treatment = _treatment(experiment)
    row = next(v for v in result["variants"] if v["variant_id"] == str(treatment.id))
    assert row["is_significant"] is True and row["relative_improvement_pct"] < 0
    assert result["lower_is_better"] is lower
    assert result["winning_variant_id"] == (str(treatment.id) if lower else None)


def test_each_metric_takes_the_direction_of_its_own_source(wh):
    """Two metrics with the same significant drop, one source marked lower is
    better and one not: each result follows its own source."""
    experiment = wh.experiment(end=ts(10))
    _a_significant_drop(wh, experiment.key, "proportion")
    admin = wh.as_("ADMIN")
    ids = wh.ready(admin, name="Checkout errors", lower_is_better=True)
    second = wh.metric_source(
        admin, ids["connection_id"], name="Purchases", lower_is_better=False
    )
    wh.validate(admin, second["id"])

    run = _finished(
        wh,
        admin,
        wh.start_run(
            admin,
            experiment,
            ids,
            metric_source_ids=[ids["metric_source_id"], second["id"]],
        ),
    )

    primary, secondary = (m["result"] for m in run["results"]["metrics"])
    treatment = str(_treatment(experiment).id)
    assert (primary["lower_is_better"], primary["winning_variant_id"]) == (
        True,
        treatment,
    )
    assert (secondary["lower_is_better"], secondary["winning_variant_id"]) == (
        False,
        None,
    )


def test_an_edit_after_a_run_is_admitted_does_not_change_its_direction(wh):
    """The direction is read when the run is admitted and frozen in its plan:
    a source edited while the run is waiting on the warehouse does not
    change it."""
    experiment = wh.experiment(end=ts(10))
    _a_significant_drop(wh, experiment.key, "proportion")
    admin = wh.as_("ADMIN")
    ids = wh.ready(admin, name="Checkout errors", lower_is_better=True)
    wh.gate = threading.Event()
    response = wh.start_run(admin, experiment, ids)
    assert response.status_code == 202, response.text
    assert wh.entered.acquire(timeout=10), "the run never reached the warehouse"

    # A PUT without the field sets it to false.
    edited = admin.put(
        f"{WA}/sources/{ids['metric_source_id']}",
        json={
            "kind": "metric",
            "name": "Checkout errors",
            "table": "main.events",
            "columns": {"unit_id": "user_id", "event_at": "event_at"},
            "metric_type": "proportion",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["lower_is_better"] is False
    wh.gate.set()

    run = wh.wait_for_run(admin, response.json()["run_id"])
    assert run["status"] == "succeeded", run
    result = run["results"]["metrics"][0]["result"]
    assert result["lower_is_better"] is True
    assert result["winning_variant_id"] == str(_treatment(experiment).id)


def test_the_direction_is_the_sources_not_the_experiments_metric(wh):
    """An experiment metric of the same name marked lower is better does not
    change a warehouse analysis: a source has one direction, its own, in
    every experiment that analyses it."""
    experiment = wh.experiment(end=ts(10))
    _seed_results(
        wh, experiment, SIGNIFICANT_DROP, is_primary=True, lower_is_better=True
    )
    _a_significant_drop(wh, experiment.key, "proportion")
    admin = wh.as_("ADMIN")
    ids = wh.ready(admin)  # a source named Purchases, not marked

    run = _finished(wh, admin, wh.start_run(admin, experiment, ids))

    result = run["results"]["metrics"][0]["result"]
    assert result["lower_is_better"] is False
    assert result["winning_variant_id"] is None

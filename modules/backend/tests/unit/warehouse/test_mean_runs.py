"""Mean metrics in a warehouse run, from the rows a warehouse returns.

The rows are written by hand here, in the wire format (counts and 17-digit
strings), so each case isolates one thing the runner does with a mean metric:
map labels to variants, hand the centred sums to the core estimator, and turn
the estimator's refusals into "Not computed" with words a reader can act on.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from backend.app.services.sufficient_stats_analysis import mean_metric_result
from modules.backend.app.services import warehouse_runner as runner
from modules.backend.app.services.warehouse_clients import WarehouseClient
from modules.backend.app.services.warehouse_query_builder import BuiltQuery
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.errors import (
    MESSAGES,
    WarehouseErrorCode,
)
from modules.backend.tests.unit.warehouse.duckdb_adapter import DUCKDB_SQL

pytestmark = pytest.mark.unit

CONTROL = runner.VariantInfo(uuid.UUID(int=1), "control", True, 50.0)
TREATMENT = runner.VariantInfo(uuid.UUID(int=2), "treatment", False, 50.0)
DIAGNOSTICS = BuiltQuery("diagnostics", "duckdb", "SELECT 'diagnostics'")


def _metric_query(name: str) -> BuiltQuery:
    return BuiltQuery("metric", "duckdb", f"SELECT '{name}'")


def _row(variant: str, n: int, sum_d: str, sum_d2: str, *, k: str, converted=0):
    return {
        "variant": variant,
        "n": str(n),
        "n_converted": str(converted),
        "k": k,
        "sum_d": sum_d,
        "sum_d2": sum_d2,
        "metric_rows_in_window": "10",
        "metric_rows_matched": "10",
        "null_value_rows": "0",
    }


def _diagnostics(units: int) -> Dict[str, str]:
    return {
        "exposure_rows": str(units),
        "null_key_rows": "0",
        "units": str(units),
        "multi_variant_units": "0",
        "variant_values": "2",
    }


class _Adapter:
    def __init__(self, answers: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self.answers = answers

    def run_query(self, built: BuiltQuery, deadline: Deadline):
        return SimpleNamespace(rows=tuple(self.answers[built.sql]))


def _run(
    monkeypatch,
    metrics: Sequence[runner.MetricPlan],
    answers: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    variant_map: Optional[Mapping[str, uuid.UUID]] = None,
    mean_estimator=mean_metric_result,
) -> Dict[str, Any]:
    finished: List[Dict[str, Any]] = []
    monkeypatch.setattr(runner, "_start", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        runner,
        "_finish",
        lambda factory, run_id, now, **values: finished.append(values),
    )
    plan = runner.RunPlan(
        client=WarehouseClient("athena", _Adapter(answers), DUCKDB_SQL),
        diagnostics=DIAGNOSTICS,
        metrics=tuple(metrics),
        variants=(TREATMENT, CONTROL),
        variant_map=variant_map or {},
        fixed_allocation=True,
        alpha=0.05,
        correction_method="none",
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        mean_estimator=mean_estimator,
    )
    assert runner.execute_run(plan, Deadline(60), uuid.uuid4()) is None, finished
    (values,) = finished
    return values["results"]


def _mean_plan(name: str = "Revenue", *, primary: bool = True) -> runner.MetricPlan:
    return runner.MetricPlan(
        source_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        name=name,
        metric_type="mean",
        is_primary=primary,
        lower_is_better=False,
        query=_metric_query(name),
    )


def test_the_core_estimator_gets_k_and_the_sums_control_first(monkeypatch):
    """Two labels mapped to one variant add their sums (both are centred on
    the same k); the estimator gets k and every variant, the control first."""
    calls: List[Dict[str, Any]] = []

    def recording(k, variants, alpha, correction_method, *, metric):
        calls.append(
            {
                "k": k,
                "variants": [(v.name, n, d, d2) for v, n, d, d2 in variants],
                "alpha": alpha,
                "metric_type": metric.metric_type,
            }
        )
        return mean_metric_result(k, variants, alpha, correction_method, metric=metric)

    results = _run(
        monkeypatch,
        [_mean_plan()],
        {
            "SELECT 'diagnostics'": [_diagnostics(12)],
            "SELECT 'Revenue'": [
                _row("treatment", 5, "2.5", "10.25", k="100.5"),
                _row("ctl-a", 4, "-1.0", "3.0", k="100.5"),
                _row("ctl-b", 3, "-1.5", "4.75", k="100.5"),
            ],
        },
        variant_map={"ctl-a": CONTROL.id, "ctl-b": CONTROL.id},
        mean_estimator=recording,
    )
    assert calls == [
        {
            "k": 100.5,
            "variants": [("control", 7, -2.5, 7.75), ("treatment", 5, 2.5, 10.25)],
            "alpha": 0.05,
            "metric_type": "mean",
        }
    ]
    (metric,) = results["metrics"]
    assert metric["computed"] is True and metric["metric_type"] == "mean"
    control, treatment = metric["result"]["variants"]
    assert control["mean"] == pytest.approx(100.5 - 2.5 / 7)
    assert treatment["mean"] == pytest.approx(100.5 + 2.5 / 5)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert results["srm"]["observed"] == {str(CONTROL.id): 7, str(TREATMENT.id): 5}


def test_fewer_than_2_units_says_so_not_a_generic_error(monkeypatch):
    results = _run(
        monkeypatch,
        [_mean_plan()],
        {
            "SELECT 'diagnostics'": [_diagnostics(4)],
            "SELECT 'Revenue'": [
                _row("control", 3, "0.5", "2.0", k="7.25"),
                _row("treatment", 1, "-0.5", "0.25", k="7.25"),
            ],
        },
    )
    (metric,) = results["metrics"]
    assert metric["computed"] is False and metric["result"] is None
    assert metric["not_computed_reason"] == "fewer_than_2_units"
    assert metric["message"] == "Not computed: fewer than 2 units"
    # The stored code has its own copy wherever it is looked up by code, not
    # the generic text of a code the fixed set does not know.
    copy = runner.run_message("fewer_than_2_units")
    assert copy != runner.run_message("a_code_not_in_the_set")
    assert copy == "A variant has fewer than 2 units, so its mean can't be compared."


def test_sums_no_sample_can_have_are_result_invalid(monkeypatch):
    """sum_d2 far below sum_d**2 / n (1 against 50) is a negative variance no
    rounding explains: refused, not clamped."""
    results = _run(
        monkeypatch,
        [_mean_plan()],
        {
            "SELECT 'diagnostics'": [_diagnostics(4)],
            "SELECT 'Revenue'": [
                _row("control", 2, "10.0", "1.0", k="3.0"),
                _row("treatment", 2, "-10.0", "1.0", k="3.0"),
            ],
        },
    )
    (metric,) = results["metrics"]
    assert metric["computed"] is False
    assert metric["not_computed_reason"] == "result_invalid"
    assert metric["message"] == (
        "Not computed: " + MESSAGES[WarehouseErrorCode.RESULT_INVALID]
    )


@pytest.mark.regression
def test_a_no_buyer_arm_is_a_result_not_result_invalid(monkeypatch):
    """Every value 0 in the control, centred on k = 0.1: its sums leave a
    rounding residue below 0 (about -3.5e-18), which is read as 0.  Before,
    the run reported this metric "not computed" as result_invalid."""
    sum_d, sum_d2 = "-0.30000000000000004", "0.030000000000000006"
    assert float(sum_d2) - float(sum_d) ** 2 / 3 < 0
    results = _run(
        monkeypatch,
        [_mean_plan()],
        {
            "SELECT 'diagnostics'": [_diagnostics(6)],
            "SELECT 'Revenue'": [
                _row("control", 3, sum_d, sum_d2, k="0.1"),
                _row("treatment", 3, "0.3", "0.27", k="0.1"),
            ],
        },
    )
    (metric,) = results["metrics"]
    assert metric["computed"] is True, metric
    control, treatment = metric["result"]["variants"]
    assert control["mean"] == pytest.approx(0.0, abs=1e-15)
    assert control["std_dev"] == 0.0
    assert treatment["statistical_test_used"] == "welch_t_test"


def test_no_variation_is_a_note_on_the_variant_not_a_number(monkeypatch):
    results = _run(
        monkeypatch,
        [_mean_plan()],
        {
            "SELECT 'diagnostics'": [_diagnostics(5)],
            "SELECT 'Revenue'": [
                _row("control", 3, "0", "0", k="4"),
                _row("treatment", 2, "0", "0", k="4"),
            ],
        },
    )
    control, treatment = results["metrics"][0]["result"]["variants"]
    assert treatment["note"] == "Not computed: no variation"
    assert treatment["p_value"] is None and treatment["is_significant"] is False
    assert control["note"] is None


def test_a_proportion_and_a_mean_metric_in_one_run(monkeypatch):
    proportion = runner.MetricPlan(
        source_id=uuid.uuid4(),
        name="Purchased",
        metric_type="proportion",
        is_primary=True,
        lower_is_better=False,
        query=_metric_query("Purchased"),
    )
    results = _run(
        monkeypatch,
        [proportion, _mean_plan(primary=False)],
        {
            "SELECT 'diagnostics'": [_diagnostics(8)],
            "SELECT 'Purchased'": [
                _row("control", 4, "0", "0", k="0.5", converted=1),
                _row("treatment", 4, "0", "0", k="0.5", converted=3),
            ],
            "SELECT 'Revenue'": [
                _row("control", 4, "-2.0", "5.0", k="10.0"),
                _row("treatment", 4, "2.0", "6.0", k="10.0"),
            ],
        },
    )
    purchased, revenue = results["metrics"]
    assert purchased["result"]["metric_type"] == "conversion"
    assert purchased["result"]["variants"][1]["statistical_test_used"] == (
        "fisher_exact"
    )
    assert revenue["result"]["metric_type"] == "mean"
    assert revenue["result"]["variants"][1]["statistical_test_used"] == ("welch_t_test")
    assert revenue["is_primary"] is False

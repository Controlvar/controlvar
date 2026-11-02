"""A warehouse run analyses each metric in its source's direction (#1386).

The rows are written by hand, in the wire format, and the run goes through
``execute_run`` -- the hops that carry a source's direction to the estimator
(``MetricPlan`` to ``MetricRef``, in the proportion and the mean branch) --
with no database and no DuckDB.  The estimators themselves already follow a
direction they are handed; what was missing was a run that handed one over.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from modules.backend.app.services import warehouse_runner as runner
from modules.backend.app.services.warehouse_clients import WarehouseClient
from modules.backend.app.services.warehouse_query_builder import ATHENA_SQL, BuiltQuery
from modules.backend.app.warehouse.deadlines import Deadline

pytestmark = pytest.mark.unit

CONTROL = runner.VariantInfo(uuid.UUID(int=1), "control", True, 50.0)
TREATMENT = runner.VariantInfo(uuid.UUID(int=2), "treatment", False, 50.0)
DIAGNOSTICS = BuiltQuery("diagnostics", "athena", "SELECT 'diagnostics'")

#: Each variant's wire row, by (kind, change): 2000 units a variant for a
#: proportion (200 against 125 converting, -37.5% or +60%), and for a mean
#: k = 2.0 with the control's mean 2.2 and the treatment's 1.7 or 2.5.
ROWS: Dict[tuple, Dict[str, Sequence[Any]]] = {
    ("proportion", "drop"): {"control": (2000, 200), "treatment": (2000, 125)},
    ("proportion", "rise"): {"control": (2000, 125), "treatment": (2000, 200)},
    ("mean", "drop"): {
        "control": (500, "100.0", "1000.0"),
        "treatment": (500, "-150.0", "1100.0"),
    },
    ("mean", "rise"): {
        "control": (500, "100.0", "1000.0"),
        "treatment": (500, "250.0", "1100.0"),
    },
}


def _wire(kind: str, label: str, values: Sequence[Any]) -> Dict[str, str]:
    if kind == "proportion":
        n, converted = values
        k, sum_d, sum_d2 = "0", "0", "0"
    else:
        n, sum_d, sum_d2 = values
        converted, k = 0, "2.0"
    return {
        "variant": label,
        "n": str(n),
        "n_converted": str(converted),
        "k": k,
        "sum_d": sum_d,
        "sum_d2": sum_d2,
        "metric_rows_in_window": str(n),
        "metric_rows_matched": str(n),
        "null_value_rows": "0",
    }


class _Adapter:
    def __init__(self, answers: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self.answers = answers

    def run_query(self, built: BuiltQuery, deadline: Deadline):
        return SimpleNamespace(rows=tuple(self.answers[built.sql]))


def _run(monkeypatch, kind: str, change: str, *, lower_is_better: bool) -> Dict:
    """The stored result of one run with one metric of ``kind``."""
    finished: List[Dict[str, Any]] = []
    monkeypatch.setattr(runner, "_start", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        runner,
        "_finish",
        lambda factory, run_id, now, **values: finished.append(values),
    )
    rows = ROWS[(kind, change)]
    units = sum(values[0] for values in rows.values())
    metric = runner.MetricPlan(
        source_id=uuid.UUID(int=10),
        name="Checkout errors",
        metric_type=kind,
        is_primary=True,
        lower_is_better=lower_is_better,
        query=BuiltQuery("metric", "athena", "SELECT 'metric'"),
    )
    answers = {
        DIAGNOSTICS.sql: [
            {
                "exposure_rows": str(units),
                "null_key_rows": "0",
                "units": str(units),
                "multi_variant_units": "0",
                "variant_values": "2",
            }
        ],
        metric.query.sql: [_wire(kind, label, v) for label, v in rows.items()],
    }
    plan = runner.RunPlan(
        client=WarehouseClient("athena", _Adapter(answers), ATHENA_SQL),
        diagnostics=DIAGNOSTICS,
        metrics=(metric,),
        variants=(CONTROL, TREATMENT),
        variant_map={},
        fixed_allocation=True,
        alpha=0.05,
        correction_method="none",
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
    )
    assert runner.execute_run(plan, Deadline(60), uuid.uuid4()) is None, finished
    (values,) = finished
    (entry,) = values["results"]["metrics"]
    assert entry["computed"] is True, entry
    return entry["result"]


CASES = [
    (kind, direction, change)
    for kind in ("proportion", "mean")
    for direction in ("lower", "higher")
    for change in ("drop", "rise")
]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("kind", "direction", "change"),
    CASES,
    ids=["-".join(case) for case in CASES],
)
def test_a_run_names_the_winner_in_the_direction_of_its_source(
    monkeypatch, kind, direction, change
):
    """A significant drop wins only when the source is lower is better, a
    significant rise only when it is not, for both metric types; the result
    says which direction it used.  Before #1386 a run had no direction to
    carry: every metric was read as higher is better."""
    lower = direction == "lower"
    result = _run(monkeypatch, kind, change, lower_is_better=lower)

    treatment = next(
        v for v in result["variants"] if v["variant_id"] == str(TREATMENT.id)
    )
    assert treatment["is_significant"] is True, treatment
    assert (treatment["relative_improvement_pct"] < 0) is (change == "drop")
    assert result["lower_is_better"] is lower
    wins = lower is (change == "drop")
    assert result["winning_variant_id"] == (str(TREATMENT.id) if wins else None)


@pytest.mark.parametrize(
    ("kind", "change"),
    [(kind, change) for kind in ("proportion", "mean") for change in ("drop", "rise")],
)
def test_the_direction_changes_only_the_winner_and_the_flag(monkeypatch, kind, change):
    """p-values, intervals and the signed relative change are the same in
    either direction: only ``lower_is_better`` and the winner move."""
    lower = _run(monkeypatch, kind, change, lower_is_better=True)
    higher = _run(monkeypatch, kind, change, lower_is_better=False)
    assert (lower.pop("lower_is_better"), higher.pop("lower_is_better")) == (
        True,
        False,
    )
    assert lower.pop("winning_variant_id") != higher.pop("winning_variant_id")
    assert lower == higher

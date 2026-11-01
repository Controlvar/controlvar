"""Mean metrics end to end: DuckDB SQL -> 17-digit strings -> runner -> estimator.

A warehouse run of a mean metric is checked against an exact reference, on
each fixture of ``numeric_fixtures`` and at that fixture's own tolerance
(1e-12 for F-MEAN-1, 2e-12 for its +1e9 copy, 1e-10 for the zero-inflated
revenue fixture F-MEAN-REV).  Nothing between the SQL and the result is
stubbed except the database session the run's row is written with: the
statements are the ones the builder generates for DuckDB, their rows go
through the wire format and the parser, and :func:`execute_run` maps the
labels and calls the core estimator, ``mean_metric_result``.

The reference is exact where it can be: the per-arm mean and variance are
rational sums of the stored float64 values; square roots, the t statistic and
the Welch-Satterthwaite degrees of freedom are taken in 60-digit decimal.
The p-value is ``2 * t.sf(|t|, df)`` of the reference t and df, and the
interval's quantile is ``t.ppf`` of the reference n: those two scipy calls are
the only floating-point steps the reference shares with what it checks.

``test_mean_parity_statement_is_the_duckdb_golden`` pins the statement
itself.  DuckDB serialises each float with ``format('{:.17g}', x)``, the fmt
spelling of ``printf('%.17g', x)`` (sqlglot reads ``printf`` as an unknown
function, which the re-parse refuses).  With fewer digits on the wire the
parity test fails.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pandas as pd
import pytest
from scipy import stats
from statsmodels.stats.power import NormalIndPower

from modules.backend.app.services import warehouse_runner as runner
from modules.backend.app.services.warehouse_clients import WarehouseClient
from modules.backend.app.services.warehouse_query_builder import (
    AnalysisWindow,
    AssignmentMapping,
    MetricMapping,
    build_diagnostics_query,
    build_metric_query,
)
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.tests.unit.warehouse import numeric_fixtures as nf
from modules.backend.tests.unit.warehouse.duckdb_adapter import (
    DUCKDB_SQL,
    DuckDBWarehouse,
)

pytestmark = pytest.mark.unit

KEY = "mean-parity"
ALPHA = 0.05
EXPOSED = pd.Timestamp("2026-09-02 00:00:00", tz="UTC")
EVENT = pd.Timestamp("2026-09-02 01:00:00", tz="UTC")
WINDOW = AnalysisWindow(
    datetime(2026, 9, 1, tzinfo=timezone.utc),
    datetime(2026, 9, 10, tzinfo=timezone.utc),
)
ASSIGNMENT = AssignmentMapping(
    ("main", "exposures"), "user_id", "experiment_key", "variant", "exposed_at"
)
METRIC = MetricMapping(("main", "events"), "user_id", "event_at", "mean", "amount")
GOLDEN = Path(__file__).parent / "parity" / "duckdb_metric_mean.sql"

CONTROL = runner.VariantInfo(uuid.UUID(int=1), "control", True, 50.0)
TREATMENT = runner.VariantInfo(uuid.UUID(int=2), "treatment", False, 50.0)
DIGITS = 60


@lru_cache(maxsize=None)
def _fixture(name: str):
    fixture = nf.load(name)
    return fixture, nf.exact_arm(fixture.control), nf.exact_arm(fixture.treatment)


def _warehouse(fixture: nf.MeanFixture, threads: int) -> DuckDBWarehouse:
    wh = DuckDBWarehouse(threads=threads)
    arms = [("control", fixture.control), ("treatment", fixture.treatment)]
    ids = [f"{label}-{i}" for label, values in arms for i in range(len(values))]
    wh.create_from_frame(
        "exposures",
        pd.DataFrame(
            {
                "user_id": ids,
                "experiment_key": KEY,
                "variant": [
                    label for label, values in arms for _ in range(len(values))
                ],
                "exposed_at": EXPOSED,
            }
        ),
    )
    wh.create_from_frame(
        "events",
        pd.DataFrame(
            {
                "user_id": ids,
                "event_at": EVENT,
                "amount": [v for _, values in arms for v in values.tolist()],
            }
        ),
    )
    return wh


class _Adapter:
    def __init__(self, warehouse: DuckDBWarehouse) -> None:
        self._warehouse = warehouse

    def run_query(self, built, deadline):
        return SimpleNamespace(rows=tuple(self._warehouse.fetch(built)))


def run_mean_metric(warehouse: DuckDBWarehouse, monkeypatch) -> Dict[str, Any]:
    """One warehouse run with one mean metric; the stored ``results``."""
    finished: List[Dict[str, Any]] = []
    monkeypatch.setattr(runner, "_start", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        runner,
        "_finish",
        lambda factory, run_id, now, **values: finished.append(values),
    )
    client = WarehouseClient("athena", _Adapter(warehouse), DUCKDB_SQL)
    plan = runner.RunPlan(
        client=client,
        diagnostics=build_diagnostics_query(DUCKDB_SQL, ASSIGNMENT, KEY, WINDOW),
        metrics=(
            runner.MetricPlan(
                source_id=uuid.UUID(int=10),
                name="Revenue",
                metric_type="mean",
                is_primary=True,
                lower_is_better=False,
                query=build_metric_query(DUCKDB_SQL, ASSIGNMENT, METRIC, KEY, WINDOW),
            ),
        ),
        variants=(TREATMENT, CONTROL),
        variant_map={},
        fixed_allocation=True,
        alpha=ALPHA,
        correction_method="none",
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
    )
    error = runner.execute_run(plan, Deadline(600), uuid.uuid4())
    assert error is None, finished
    (values,) = finished
    assert values.get("error_code") is None, values
    return values["results"]


def _dec(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def exact_expectations(exact_c: nf.ExactArm, exact_t: nf.ExactArm) -> Dict[str, Any]:
    """Every number the result reports, from the exact per-arm moments."""
    with localcontext() as ctx:
        ctx.prec = DIGITS
        diff = exact_t.mean - exact_c.mean
        se2_c = exact_c.variance / exact_c.n
        se2_t = exact_t.variance / exact_t.n
        se = (_dec(se2_c) + _dec(se2_t)).sqrt()
        t_ref = _dec(diff) / se
        df_ref = (se2_c + se2_t) ** 2 / (
            se2_c**2 / (exact_c.n - 1) + se2_t**2 / (exact_t.n - 1)
        )
        pooled = _dec((exact_c.variance + exact_t.variance) / 2).sqrt()
        d_ref = _dec(diff) / pooled

        def arm(exact: nf.ExactArm) -> Dict[str, Decimal]:
            q = Decimal(repr(float(stats.t.ppf(1 - ALPHA / 2, exact.n - 1))))
            half = q * _dec(exact.variance / exact.n).sqrt()
            mean = _dec(exact.mean)
            return {
                "mean": mean,
                "std_dev": _dec(exact.variance).sqrt(),
                "ci_low": mean - half,
                "ci_high": mean + half,
            }

        control, treatment = arm(exact_c), arm(exact_t)
        return {
            "control": control,
            "treatment": treatment,
            "p_value": 2 * stats.t.sf(abs(float(t_ref)), float(df_ref)),
            "effect_size": d_ref,
            "relative_improvement_pct": _dec(diff) / abs(_dec(exact_c.mean)) * 100,
            "power": NormalIndPower().power(
                effect_size=abs(float(d_ref)),
                nobs1=exact_t.n,
                alpha=ALPHA,
                ratio=exact_c.n / exact_t.n,
            ),
        }


def _relative(value: float, reference: Any) -> float:
    reference = Fraction(reference)
    if reference == 0:
        return abs(value)
    return float(abs(Fraction(value) - reference) / abs(reference))


@pytest.mark.parametrize("threads", [1, 8])
@pytest.mark.parametrize("name", sorted(nf.FIXTURES))
def test_mean_parity_duckdb_sql(name: str, threads: int, monkeypatch) -> None:
    fixture, exact_c, exact_t = _fixture(name)
    results = run_mean_metric(_warehouse(fixture, threads), monkeypatch)

    (metric,) = results["metrics"]
    assert metric["computed"] is True, metric
    result = metric["result"]
    control, treatment = result["variants"]
    assert (control["variant_name"], treatment["variant_name"]) == (
        "control",
        "treatment",
    )
    assert (control["sample_size"], treatment["sample_size"]) == (
        exact_c.n,
        exact_t.n,
    )
    assert control["conversions"] is None and treatment["conversions"] is None
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["note"] is None

    expected = exact_expectations(exact_c, exact_t)
    errors: Dict[str, float] = {}
    for label, got in (("control", control), ("treatment", treatment)):
        want = expected[label]
        errors[f"{label}.mean"] = _relative(got["mean"], want["mean"])
        errors[f"{label}.std_dev"] = _relative(got["std_dev"], want["std_dev"])
        low, high = got["confidence_interval"]
        errors[f"{label}.ci_low"] = _relative(low, want["ci_low"])
        errors[f"{label}.ci_high"] = _relative(high, want["ci_high"])
    for field in ("p_value", "effect_size", "relative_improvement_pct", "power"):
        errors[field] = _relative(treatment[field], expected[field])

    worst = max(errors, key=errors.__getitem__)
    assert errors[worst] <= fixture.tolerance, (name, threads, worst, errors)
    assert all(math.isfinite(e) for e in errors.values())


def test_mean_parity_statement_is_the_duckdb_golden() -> None:
    """The statement the parity test runs, byte for byte, with every float
    serialised to 17 significant digits."""
    built = build_metric_query(DUCKDB_SQL, ASSIGNMENT, METRIC, KEY, WINDOW)
    assert built.sql == GOLDEN.read_text(encoding="utf-8")
    for column, expression in (
        ("k", "MIN(k.k)"),
        ("sum_d", "SUM(y.y - k.k)"),
        ("sum_d2", "SUM((y.y - k.k) * (y.y - k.k))"),
    ):
        assert f"format('{{:.17g}}', {expression}) AS {column}," in built.sql

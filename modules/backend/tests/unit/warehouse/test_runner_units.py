"""The runner's and the source service's pure parts, without a database."""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from modules.backend.app.services import warehouse_estimators as estimators
from modules.backend.app.services import warehouse_runner as runner
from modules.backend.app.services import warehouse_source_service as sources
from modules.backend.app.services.warehouse_clients import (
    NUMERIC_TYPES,
    TIME_TYPES,
    WarehouseClient,
    worst_case_per_day,
)
from modules.backend.app.services.warehouse_query_builder import (
    BIGQUERY_SQL,
    SNOWFLAKE_SQL,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    WarehouseResultRefused,
)
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.app.warehouse.executor import JobKind, WarehouseExecutor

pytestmark = pytest.mark.unit


def _source(mapping, filters=()):
    return SimpleNamespace(
        column_mapping={
            role: {"name": name, "type": None} for role, name in mapping.items()
        },
        filters=[{"column": c, "operator": "eq", "value": "x"} for c in filters],
    )


def _client(dialect=SNOWFLAKE_SQL, kind="snowflake"):
    return WarehouseClient(kind, None, dialect, TIME_TYPES[kind], NUMERIC_TYPES[kind])


def test_columns_resolve_to_the_canonical_case_the_metadata_reports():
    source = _source(
        {"unit_id": "user_id", "event_at": "EVENT_AT"}, filters=("status",)
    )
    mapping, filters = sources.resolve_columns(
        source,
        _client(),
        [("USER_ID", "TEXT"), ("EVENT_AT", "TIMESTAMP_NTZ"), ("STATUS", "TEXT")],
    )
    assert mapping == {
        "unit_id": {"name": "USER_ID", "type": "TEXT"},
        "event_at": {"name": "EVENT_AT", "type": "TIMESTAMP_NTZ"},
    }
    assert filters == [{"column": "STATUS", "operator": "eq", "value": "x"}]


def test_an_ambiguous_case_match_is_not_guessed():
    source = _source({"unit_id": "user_id", "event_at": "EVENT_AT"})
    with pytest.raises(sources.SourceRefused) as refused:
        sources.resolve_columns(
            source,
            _client(),
            [("User_Id", "TEXT"), ("USER_ID", "TEXT"), ("EVENT_AT", "TIMESTAMP_TZ")],
        )
    assert (refused.value.code, refused.value.field) == (
        "identifier_not_found",
        "columns.unit_id",
    )


def test_a_metadata_name_the_gate_refuses_is_never_stored():
    """A column the warehouse reports with a quote in its name cannot be mapped."""
    source = _source({"unit_id": 'user"id', "event_at": "event_at"})
    with pytest.raises(sources.SourceRefused):
        sources.resolve_columns(
            source,
            _client(BIGQUERY_SQL, "bigquery"),
            [('user"id', "STRING"), ("event_at", "TIMESTAMP")],
        )


@pytest.mark.parametrize(
    ("kind", "time_type", "accepted"),
    [
        ("bigquery", "TIMESTAMP", True),
        ("bigquery", "DATETIME", False),
        ("bigquery", "DATE", False),
        ("snowflake", "TIMESTAMP_LTZ", True),
        ("snowflake", "TEXT", False),
    ],
)
def test_time_columns_must_be_timestamps(kind, time_type, accepted):
    dialect = BIGQUERY_SQL if kind == "bigquery" else SNOWFLAKE_SQL
    source = _source({"unit_id": "u", "event_at": "t"})
    call = lambda: sources.resolve_columns(  # noqa: E731
        source, _client(dialect, kind), [("u", "STRING"), ("t", time_type)]
    )
    if accepted:
        call()
    else:
        with pytest.raises(sources.SourceRefused) as refused:
            call()
        assert refused.value.code == "unsupported_column_type"


def test_the_estimator_gets_the_counts_control_first():
    seen = {}

    def estimator(variants, alpha, correction_method, *, metric):
        seen.update(
            variants=variants, alpha=alpha, method=correction_method, metric=metric
        )
        return {"ok": True, "interval": (0.1, 0.2), "infinite": float("inf")}

    control = estimators.VariantRef(uuid.uuid4(), "control", True)
    treatment = estimators.VariantRef(uuid.uuid4(), "treatment", False)
    metric = estimators.MetricRef(uuid.uuid4(), "Purchases", True)
    result = estimators.proportion_result(
        [
            estimators.VariantCounts(control, 10, 3),
            estimators.VariantCounts(treatment, 12, 5),
        ],
        alpha=0.05,
        correction_method="bonferroni",
        metric=metric,
        estimator=estimator,
    )
    # Stored as JSON: a tuple becomes a list, a non-finite number null.
    assert result == {"ok": True, "interval": [0.1, 0.2], "infinite": None}
    assert seen == {
        "variants": [(control, 10, 3), (treatment, 12, 5)],
        "alpha": 0.05,
        "method": "bonferroni",
        "metric": metric,
    }
    assert metric.metric_type == "conversion"


def test_preview_rows_keep_aggregates_only():
    rows = [
        {
            "total_rows": "5",
            "null_unit_rows": "1",
            "null_variant_rows": "0",
            "earliest": "1788307200",
            "latest": "-5",
            "variant": "control" + "x" * 80,
            "units": "3",
            "unit_id": "a-unit-value",
        }
    ]
    summary = runner.parse_preview_rows("assignment", rows)
    assert set(summary) == {
        "total_rows",
        "null_unit_rows",
        "null_variant_rows",
        "earliest",
        "latest",
        "variants",
    }
    assert summary["variants"] == [{"label": ("control" + "x" * 80)[:64], "units": 3}]
    assert summary["latest"].year == 1969


@pytest.mark.parametrize(
    "rows",
    [
        [{"total_rows": "-1", "null_unit_rows": "0", "null_value_rows": "0"}],
        [{"total_rows": "1.5", "null_unit_rows": "0", "null_value_rows": "0"}],
        [{"total_rows": True, "null_unit_rows": "0", "null_value_rows": "0"}],
        [],
    ],
)
def test_preview_rows_that_cannot_be_right_are_refused(rows):
    with pytest.raises(WarehouseResultRefused):
        runner.parse_preview_rows("metric", rows, is_mean=True)


def test_a_preview_not_in_utc_is_refused():
    row = {"total_rows": "1", "null_unit_rows": "0", "null_value_rows": "0"}
    with pytest.raises(WarehouseResultRefused) as refused:
        runner.parse_preview_rows(
            "metric", [{**row, "session_offset": "-07:00"}], expect_session_offset=True
        )
    assert refused.value.code == "timezone_not_utc"
    runner.parse_preview_rows(
        "metric", [{**row, "session_offset": "+00:00"}], expect_session_offset=True
    )


@pytest.mark.regression
def test_a_preview_from_a_real_snowflake_utc_session_is_accepted():
    """Snowflake writes a UTC offset as ``Z``; ``Z `` or ``-00:00`` is still refused."""
    row = {"total_rows": "1", "null_unit_rows": "0", "null_value_rows": "0"}
    runner.parse_preview_rows(
        "metric", [{**row, "session_offset": "Z"}], expect_session_offset=True
    )
    for offset in ("Z ", "z", "-00:00", None):
        with pytest.raises(WarehouseResultRefused) as refused:
            runner.parse_preview_rows(
                "metric",
                [{**row, "session_offset": offset}],
                expect_session_offset=True,
            )
        assert refused.value.code == "timezone_not_utc"


@pytest.mark.regression
@pytest.mark.parametrize("zero", ["0", 0, " 0 ", "00"])
def test_a_preview_of_no_rows_may_have_a_null_offset(zero):
    row = {"total_rows": zero, "null_unit_rows": "0", "null_value_rows": "0"}
    summary = runner.parse_preview_rows(
        "metric", [{**row, "session_offset": None}], expect_session_offset=True
    )
    assert summary["total_rows"] == 0


@pytest.mark.parametrize("count", ["1", 1, "10", None, "", "x", True, Decimal("0")])
def test_a_preview_with_rows_or_no_usable_count_needs_a_utc_offset(count):
    row = {"total_rows": count, "null_unit_rows": "0", "null_value_rows": "0"}
    with pytest.raises(WarehouseResultRefused) as refused:
        runner.parse_preview_rows(
            "metric", [{**row, "session_offset": None}], expect_session_offset=True
        )
    assert refused.value.code in ("timezone_not_utc", "result_invalid")


def test_a_preview_without_a_count_does_not_excuse_a_null_offset():
    with pytest.raises(WarehouseResultRefused) as refused:
        runner.parse_preview_rows(
            "metric",
            [{"null_unit_rows": "0", "null_value_rows": "0", "session_offset": None}],
            expect_session_offset=True,
        )
    assert refused.value.code == "timezone_not_utc"


@pytest.mark.parametrize("offset", ["+01:00", "UTC", "", "z", "Z "])
def test_a_preview_of_no_rows_not_in_utc_is_refused(offset):
    row = {"total_rows": "0", "null_unit_rows": "0", "null_value_rows": "0"}
    with pytest.raises(WarehouseResultRefused) as refused:
        runner.parse_preview_rows(
            "metric", [{**row, "session_offset": offset}], expect_session_offset=True
        )
    assert refused.value.code == "timezone_not_utc"


def test_failure_codes_come_from_the_fixed_set():
    assert (
        runner.failure_code(WarehouseError(WarehouseErrorCode.BYTES_LIMIT))
        == "bytes_limit"
    )
    assert runner.failure_code(WarehouseResultRefused("no_units", "x")) == "no_units"
    assert runner.failure_code(RuntimeError("upstream text")) == "internal"
    assert runner.run_message("time_limit") == (
        "The warehouse did not finish within the time limit."
    )
    assert runner.run_message("not-a-code") == runner.run_message("internal")


def test_a_cancelled_reservation_frees_its_slot_without_running():
    executor = WarehouseExecutor(1, per_organisation=1)
    ran = []
    try:
        reservation, future = runner.reserve(
            executor,
            lambda deadline, run_id: ran.append(run_id),
            kind=JobKind.ANALYSIS,
            connection_id=uuid.uuid4(),
            total_seconds=5,
        )
        with pytest.raises(runner.AdmissionRefused) as busy:
            runner.reserve(
                executor,
                lambda deadline, run_id: None,
                kind=JobKind.ANALYSIS,
                connection_id=uuid.uuid4(),
                total_seconds=5,
            )
        assert busy.value.code == "warehouse_busy"
        reservation.cancel()
        assert future.result(timeout=10) is None
        assert ran == []
        assert executor.admitted == 0
    finally:
        executor.shutdown(wait=True)


def test_a_released_reservation_runs_with_its_run_id():
    executor = WarehouseExecutor(1, per_organisation=1)
    run_id = uuid.uuid4()
    started = threading.Event()
    try:
        reservation, future = runner.reserve(
            executor,
            lambda deadline, rid: (started.set(), rid)[1],
            kind=JobKind.PREVIEW,
            connection_id=uuid.uuid4(),
            total_seconds=5,
        )
        reservation.release(run_id)
        assert future.result(timeout=10) == run_id
    finally:
        executor.shutdown(wait=True)


def test_the_worst_case_is_runs_times_eleven_times_the_cap():
    bytes_capped = SimpleNamespace(
        max_runs_per_day=20, max_bytes_per_query=10**9, query_timeout_seconds=300
    )
    time_capped = SimpleNamespace(
        max_runs_per_day=20, max_bytes_per_query=None, query_timeout_seconds=300
    )
    assert worst_case_per_day(bytes_capped) == {
        "worst_case_bytes_per_day": 20 * 11 * 10**9,
        "worst_case_seconds_per_day": None,
    }
    assert worst_case_per_day(time_capped) == {
        "worst_case_bytes_per_day": None,
        "worst_case_seconds_per_day": 20 * 11 * 300,
    }


def test_the_daily_limit_copy():
    from datetime import datetime, timezone

    refusal = runner.daily_limit_refusal(
        "Prod analytics", 20, datetime(2026, 9, 27, 18, 48, tzinfo=timezone.utc)
    )
    assert refusal.status_code == 429
    assert refusal.headers == {"Retry-After": str(5 * 3600 + 12 * 60)}
    assert refusal.detail() == {
        "code": "daily_run_limit_reached",
        "message": (
            "Not run: Prod analytics has reached its limit of 20 analyses per day "
            "(UTC, previews included). The count resets at 00:00 UTC, in 5 h 12 min. "
            "An admin can change the limit in Warehouse › Connections › Prod analytics."
        ),
        "limit": 20,
        "resets_at": "2026-09-28T00:00:00Z",
    }


def test_the_default_estimator_is_the_one_results_uses():
    from backend.app.services import sufficient_stats_analysis

    assert estimators.binomial_metric_result is (
        sufficient_stats_analysis.binomial_metric_result
    )
    control = estimators.VariantRef(uuid.uuid4(), "control", True)
    treatment = estimators.VariantRef(uuid.uuid4(), "treatment", False)
    result = estimators.proportion_result(
        [
            estimators.VariantCounts(control, 1000, 100),
            estimators.VariantCounts(treatment, 1000, 130),
        ],
        alpha=0.05,
        correction_method="none",
        metric=estimators.MetricRef(uuid.uuid4(), "Purchases", True),
    )
    assert result["metric_type"] == "conversion"
    assert result["variants"][1]["statistical_test_used"] == "fisher_exact"
    assert isinstance(result["variants"][0]["confidence_interval"], list)

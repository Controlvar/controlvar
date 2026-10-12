"""
Unit tests for ResultsStreamingService (EP-058: Real-time WebSocket Streaming Results).

Tests cover:
- get_live_snapshot returns dict with correct shape
- snapshot includes all required fields
- is_significant logic
- handles missing experiment gracefully
- handles DB errors gracefully
- compute_and_broadcast delegates correctly
- p-value computation
- days_running calculation
"""

import asyncio
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from backend.app.services.results_streaming_service import (
    ResultsStreamingService,
    _norm_cdf,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_variant(
    vid: str = "var-1",
    name: str = "Control",
    is_control: bool = True,
) -> MagicMock:
    v = MagicMock()
    v.id = vid
    v.name = name
    v.is_control = is_control
    return v


def _make_experiment(
    exp_id: str = "exp-1",
    status: str = "active",
    variants=None,
    start_date=None,
    metric_definitions=None,
) -> MagicMock:
    exp = MagicMock()
    exp.id = exp_id
    exp.status = status
    exp.variants = variants or []
    exp.start_date = start_date
    exp.metric_definitions = metric_definitions or []
    return exp


def _make_db_session(experiment=None) -> MagicMock:
    """Create a mock DB session that returns the given experiment."""
    db = MagicMock()
    query_mock = MagicMock()
    options_mock = MagicMock()
    filter_mock = MagicMock()

    db.query.return_value = query_mock
    query_mock.options.return_value = options_mock
    options_mock.filter.return_value = filter_mock
    filter_mock.first.return_value = experiment

    # Mock assignment/event queries (return empty results by default)
    query_mock.filter.return_value = query_mock
    query_mock.group_by.return_value = query_mock
    query_mock.all.return_value = []
    query_mock.scalar.return_value = 0

    return db


def _make_service_with_experiment(experiment=None):
    """Create service + mock DB factory returning the given experiment."""
    db = _make_db_session(experiment)
    factory = MagicMock(return_value=db)
    service = ResultsStreamingService(factory)
    return service, db


# ---------------------------------------------------------------------------
# get_live_snapshot — schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_contains_event_field():
    """Snapshot must have event='results_update'."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert snapshot["event"] == "results_update"


@pytest.mark.asyncio
async def test_snapshot_contains_experiment_id():
    """Snapshot must contain experiment_id."""
    exp = _make_experiment(exp_id="exp-abc")
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-abc")
    assert snapshot["experiment_id"] == "exp-abc"


@pytest.mark.asyncio
async def test_snapshot_contains_timestamp():
    """Snapshot must contain a timestamp field (ISO 8601 string)."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert "timestamp" in snapshot
    assert isinstance(snapshot["timestamp"], str)
    # Should be parseable as ISO
    datetime.fromisoformat(snapshot["timestamp"])


@pytest.mark.asyncio
async def test_snapshot_contains_status():
    """Snapshot must contain status field."""
    exp = _make_experiment(status="active")
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert "status" in snapshot
    assert snapshot["status"] == "active"


@pytest.mark.asyncio
async def test_snapshot_contains_variants_list():
    """Snapshot must contain a variants list."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert "variants" in snapshot
    assert isinstance(snapshot["variants"], list)


@pytest.mark.asyncio
async def test_snapshot_contains_total_participants():
    """Snapshot must contain total_participants."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert "total_participants" in snapshot
    assert isinstance(snapshot["total_participants"], int)


@pytest.mark.asyncio
async def test_snapshot_contains_days_running():
    """Snapshot must contain days_running (int or None)."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert "days_running" in snapshot


@pytest.mark.asyncio
async def test_snapshot_contains_is_significant():
    """Snapshot must contain is_significant boolean."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert "is_significant" in snapshot
    assert isinstance(snapshot["is_significant"], bool)


# ---------------------------------------------------------------------------
# days_running logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_days_running_is_none_when_no_start_date():
    """days_running is None when experiment has no start_date."""
    exp = _make_experiment(start_date=None)
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert snapshot["days_running"] is None


@pytest.mark.asyncio
async def test_days_running_calculated_from_start_date():
    """days_running reflects how long the experiment has been running."""
    start = datetime.now(timezone.utc) - timedelta(days=10)
    exp = _make_experiment(start_date=start)
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert snapshot["days_running"] == 10


# ---------------------------------------------------------------------------
# Experiment not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_for_missing_experiment_has_not_found_status():
    """get_live_snapshot returns not_found status when experiment doesn't exist."""
    service, _ = _make_service_with_experiment(experiment=None)
    snapshot = await service.get_live_snapshot("exp-missing")
    assert snapshot["status"] == "not_found"


@pytest.mark.asyncio
async def test_snapshot_for_missing_experiment_has_empty_variants():
    """Snapshot for missing experiment has empty variants list."""
    service, _ = _make_service_with_experiment(experiment=None)
    snapshot = await service.get_live_snapshot("exp-missing")
    assert snapshot["variants"] == []


@pytest.mark.asyncio
async def test_snapshot_for_missing_experiment_has_zero_participants():
    """Snapshot for missing experiment has total_participants=0."""
    service, _ = _make_service_with_experiment(experiment=None)
    snapshot = await service.get_live_snapshot("exp-missing")
    assert snapshot["total_participants"] == 0


@pytest.mark.asyncio
async def test_snapshot_for_missing_experiment_has_error_field():
    """Snapshot for missing experiment includes an error message."""
    service, _ = _make_service_with_experiment(experiment=None)
    snapshot = await service.get_live_snapshot("exp-missing")
    assert "error" in snapshot


# ---------------------------------------------------------------------------
# DB error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_on_db_error_returns_error_snapshot():
    """get_live_snapshot returns an error snapshot when DB call raises."""
    factory = MagicMock(side_effect=Exception("DB connection refused"))
    service = ResultsStreamingService(factory)
    snapshot = await service.get_live_snapshot("exp-1")
    assert snapshot["event"] == "results_update"
    assert snapshot["status"] == "error"
    assert "error" in snapshot


@pytest.mark.asyncio
async def test_snapshot_on_db_error_has_correct_experiment_id():
    """Error snapshot preserves the experiment_id."""
    factory = MagicMock(side_effect=Exception("DB down"))
    service = ResultsStreamingService(factory)
    snapshot = await service.get_live_snapshot("exp-xyz")
    assert snapshot["experiment_id"] == "exp-xyz"


# ---------------------------------------------------------------------------
# is_significant logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_significant_false_when_no_data():
    """is_significant is False when there are no variants with data."""
    exp = _make_experiment(variants=[_make_variant()])
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert snapshot["is_significant"] is False


@pytest.mark.asyncio
async def test_is_significant_false_for_control_only():
    """is_significant is False when only control variant exists."""
    ctrl = _make_variant(vid="ctrl", name="Control", is_control=True)
    exp = _make_experiment(variants=[ctrl])
    service, _ = _make_service_with_experiment(exp)
    snapshot = await service.get_live_snapshot("exp-1")
    assert snapshot["is_significant"] is False


# ---------------------------------------------------------------------------
# compute_and_broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_and_broadcast_calls_manager_broadcast():
    """compute_and_broadcast() calls manager.broadcast with the snapshot."""
    exp = _make_experiment()
    service, _ = _make_service_with_experiment(exp)

    manager = AsyncMock()
    await service.compute_and_broadcast(manager, "exp-1")

    manager.broadcast.assert_called_once()
    call_args = manager.broadcast.call_args
    assert call_args[0][0] == "exp-1"  # experiment_id
    snapshot = call_args[0][1]
    assert snapshot["event"] == "results_update"


@pytest.mark.asyncio
async def test_compute_and_broadcast_propagates_snapshot():
    """compute_and_broadcast() passes the actual snapshot to broadcast."""
    exp = _make_experiment(exp_id="exp-broadcast")
    service, _ = _make_service_with_experiment(exp)

    manager = AsyncMock()
    await service.compute_and_broadcast(manager, "exp-broadcast")

    snapshot = manager.broadcast.call_args[0][1]
    assert snapshot["experiment_id"] == "exp-broadcast"


# ---------------------------------------------------------------------------
# _norm_cdf helper
# ---------------------------------------------------------------------------


def test_norm_cdf_at_zero():
    """Normal CDF at z=0 should be 0.5."""
    result = _norm_cdf(0.0)
    assert abs(result - 0.5) < 1e-6


def test_norm_cdf_at_1_96():
    """Normal CDF at z=1.96 should be approximately 0.975."""
    result = _norm_cdf(1.96)
    assert abs(result - 0.975) < 0.001


def test_norm_cdf_monotone():
    """Normal CDF should be monotonically increasing."""
    values = [_norm_cdf(float(z)) for z in range(-3, 4)]
    assert all(values[i] < values[i + 1] for i in range(len(values) - 1))


# ---------------------------------------------------------------------------
# The Live p-value pools each arm's own counts (#1146)
# ---------------------------------------------------------------------------


def _reference_p_value(
    control_conv: int, control_n: int, treat_conv: int, treat_n: int
) -> float:
    """
    Two-sided pooled two-proportion z-test, from first principles.

    p_pool = (x1 + x2) / (n1 + n2)
    se     = sqrt(p_pool (1 - p_pool) (1/n1 + 1/n2))
    z      = |x2/n2 - x1/n1| / se
    p      = erfc(z / sqrt(2))
    """
    pooled = (control_conv + treat_conv) / (control_n + treat_n)
    se = math.sqrt(pooled * (1 - pooled) * (1 / control_n + 1 / treat_n))
    z = abs(treat_conv / treat_n - control_conv / control_n) / se
    return math.erfc(z / math.sqrt(2))


def _live_variant_rows(arms: dict, monkeypatch):
    """
    Run ``_compute_variant_results`` for a control and a treatment whose
    (participants, conversions) are given as ``arms``; return the two rows
    keyed by variant name.
    """
    ctrl = _make_variant(vid="ctrl", name="Control", is_control=True)
    treat = _make_variant(vid="treat", name="Treatment", is_control=False)
    exp = _make_experiment(variants=[ctrl, treat])
    db = _make_db_session(exp)
    db.query.return_value.all.return_value = [
        MagicMock(variant_id="ctrl", cnt=arms["Control"][0]),
        MagicMock(variant_id="treat", cnt=arms["Treatment"][0]),
    ]
    conversions = {"ctrl": arms["Control"][1], "treat": arms["Treatment"][1]}
    monkeypatch.setattr(
        "backend.app.services.event_matching.count_converting_users",
        lambda _db, _experiment_id, variant_id, _event_name: conversions[
            str(variant_id)
        ],
    )
    service = ResultsStreamingService(MagicMock(return_value=db))
    rows = service._compute_variant_results("exp-1", exp, db)
    return {row["name"]: row for row in rows}


@pytest.mark.regression
@pytest.mark.parametrize(
    ("control", "treatment", "expected"),
    [
        # The three cases of #1146.  main answered 0.506, 0.035 and 0.0513.
        ((1000, 100), (100, 13), 0.346116),
        ((100, 10), (1000, 130), 0.390750),
        ((5000, 500), (5000, 560), 0.051286),
    ],
)
def test_live_p_value_pools_each_arms_own_counts(
    control, treatment, expected, monkeypatch
):
    """
    The pooled estimate uses the control's own n and conversions, and the
    treatment's own: unequal arms (a bandit, a 90/10 split) used to be pooled
    with the treatment's n for both.
    """
    rows = _live_variant_rows({"Control": control, "Treatment": treatment}, monkeypatch)

    p_value = rows["Treatment"]["p_value"]
    # Hard-coded from an independent implementation (the issue's table) ...
    assert p_value == pytest.approx(expected, abs=1e-6)
    # ... and from the closed form written out above.
    reference = _reference_p_value(control[1], control[0], treatment[1], treatment[0])
    assert p_value == pytest.approx(reference, abs=1e-6)
    assert rows["Control"]["p_value"] is None


@pytest.mark.regression
@pytest.mark.asyncio
async def test_live_is_significant_follows_the_correct_p_value(monkeypatch):
    """
    10/100 against 130/1000 is p = 0.39: the Live tab said 0.035 and ticked
    "Live z-test p < 0.05" in green.  The payload keeps its shape.
    """
    ctrl = _make_variant(vid="ctrl", name="Control", is_control=True)
    treat = _make_variant(vid="treat", name="Treatment", is_control=False)
    exp = _make_experiment(variants=[ctrl, treat])
    db = _make_db_session(exp)
    db.query.return_value.all.return_value = [
        MagicMock(variant_id="ctrl", cnt=100),
        MagicMock(variant_id="treat", cnt=1000),
    ]
    conversions = {"ctrl": 10, "treat": 130}
    monkeypatch.setattr(
        "backend.app.services.event_matching.count_converting_users",
        lambda _db, _experiment_id, variant_id, _event_name: conversions[
            str(variant_id)
        ],
    )
    service = ResultsStreamingService(MagicMock(return_value=db))

    snapshot = await service.get_live_snapshot("exp-1")

    assert snapshot["is_significant"] is False
    assert snapshot["total_participants"] == 1100
    treatment_row = next(v for v in snapshot["variants"] if not v["is_control"])
    assert set(treatment_row) == {
        "key",
        "name",
        "participant_count",
        "conversion_count",
        "conversion_rate",
        "relative_lift",
        "p_value",
        "is_control",
    }
    assert treatment_row["p_value"] == pytest.approx(0.390750, abs=1e-6)


@pytest.mark.parametrize(
    ("control", "treatment"),
    [
        ((100, 0), (100, 0)),  # no conversions anywhere: pooled rate 0
        ((100, 100), (100, 100)),  # every user converted: pooled rate 1
        ((100, 10), (0, 0)),  # an empty treatment arm
        ((100, 10), (4, 2)),  # fewer than 5 treatment users
    ],
)
def test_live_p_value_is_none_where_there_is_nothing_to_test(
    control, treatment, monkeypatch
):
    """Degenerate arms answer None, as they did before, and never raise."""
    rows = _live_variant_rows({"Control": control, "Treatment": treatment}, monkeypatch)

    assert rows["Treatment"]["p_value"] is None
    assert rows["Control"]["p_value"] is None


@pytest.mark.regression
def test_live_p_value_is_none_when_the_control_has_no_participants(monkeypatch):
    """
    An empty control arm has no rate to compare with.  main read it as a
    control rate of 0 and pooled it with the treatment's n, so 10/100 against
    nothing came out as p = 0.001; the pooled estimate cannot take an empty arm.
    """
    rows = _live_variant_rows({"Control": (0, 0), "Treatment": (100, 10)}, monkeypatch)

    assert rows["Treatment"]["p_value"] is None


@pytest.mark.regression
def test_live_p_value_one_sided_zero_conversions_still_computes(monkeypatch):
    """0/200 against 10/100 has a defined pooled rate, so it has a p-value."""
    rows = _live_variant_rows(
        {"Control": (200, 0), "Treatment": (100, 10)}, monkeypatch
    )

    assert rows["Treatment"]["p_value"] == pytest.approx(
        _reference_p_value(0, 200, 10, 100), abs=1e-6
    )

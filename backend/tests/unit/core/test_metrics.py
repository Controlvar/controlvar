"""
Unit tests for backend/app/core/metrics.py

Tests cover:
- record_request increments the http_requests_total counter
- record_request observes the http_request_duration_seconds histogram
- All helper recording functions delegate to the correct metric objects
- monitoring_config registry entries are well-formed
"""

import pytest


def test_record_request_increments_counter(mocker):
    mock_counter = mocker.patch("backend.app.core.metrics.http_requests_total")
    from backend.app.core.metrics import record_request

    record_request("GET", "/health", 200, 0.01)

    mock_counter.labels.assert_called_with(
        method="GET", endpoint="/health", status_code=200
    )
    mock_counter.labels.return_value.inc.assert_called_once()


def test_record_request_observes_histogram(mocker):
    mock_hist = mocker.patch("backend.app.core.metrics.http_request_duration_seconds")
    from backend.app.core.metrics import record_request

    record_request("POST", "/api/v1/tracking/assign", 200, 0.045)

    mock_hist.labels.assert_called_with(
        method="POST",
        endpoint="/api/v1/tracking/assign",
        status_code=200,
    )
    mock_hist.labels.return_value.observe.assert_called_with(0.045)


def test_request_duration_histogram_buckets_cover_5ms_to_10s():
    """Verify the histogram buckets span the SLA-relevant range."""
    from backend.app.core.metrics import http_request_duration_seconds

    buckets = list(http_request_duration_seconds._upper_bounds)
    # Buckets include +Inf as the last entry; check explicit boundaries
    explicit = [b for b in buckets if b != float("inf")]
    assert min(explicit) <= 0.005, "Lower bound must reach 5ms for fast endpoints"
    assert max(explicit) >= 10.0, (
        "Upper bound must reach 10s for slow background queries"
    )


def test_record_experiment_assignment(mocker):
    mock_counter = mocker.patch("backend.app.core.metrics.experiment_assignments_total")
    from backend.app.core.metrics import record_experiment_assignment

    record_experiment_assignment("exp-123", "variant-A")

    mock_counter.labels.assert_called_with(
        experiment_id="exp-123", variant_id="variant-A"
    )
    mock_counter.labels.return_value.inc.assert_called_once()


def test_record_event_tracked(mocker):
    mock_counter = mocker.patch("backend.app.core.metrics.events_tracked_total")
    from backend.app.core.metrics import record_event_tracked

    record_event_tracked("page_view")

    mock_counter.labels.assert_called_with(event_type="page_view")
    mock_counter.labels.return_value.inc.assert_called_once()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("purchase", "custom"),
        ("purchase_9f3c1b2a-unique", "custom"),
        ("Conversion", "conversion"),
        ("exposure", "exposure"),
        ("", "custom"),
    ],
)
def test_record_event_tracked_bounds_label_cardinality(mocker, raw, expected):
    """Client-supplied event names must never become new label values."""
    mock_counter = mocker.patch("backend.app.core.metrics.events_tracked_total")
    from backend.app.core.metrics import record_event_tracked

    record_event_tracked(raw)

    mock_counter.labels.assert_called_with(event_type=expected)


@pytest.mark.parametrize("route", ["track", "batch", "events"])
def test_record_event_duplicate(mocker, route):
    """A duplicate (#1543) is counted by route, never by event type."""
    mock_counter = mocker.patch("backend.app.core.metrics.events_duplicate_total")
    from backend.app.core.metrics import record_event_duplicate

    record_event_duplicate(route)

    mock_counter.labels.assert_called_once_with(route=route)
    mock_counter.labels.return_value.inc.assert_called_once()


@pytest.mark.parametrize("route", ["purchase", "/api/v1/tracking/track", ""])
def test_record_event_duplicate_refuses_a_label_it_does_not_know(mocker, route):
    mock_counter = mocker.patch("backend.app.core.metrics.events_duplicate_total")
    from backend.app.core.metrics import record_event_duplicate

    with pytest.raises(ValueError):
        record_event_duplicate(route)
    mock_counter.labels.assert_not_called()


def test_record_flag_evaluation(mocker):
    mock_counter = mocker.patch(
        "backend.app.core.metrics.feature_flag_evaluations_total"
    )
    from backend.app.core.metrics import record_flag_evaluation

    record_flag_evaluation("my-flag", "enabled")

    mock_counter.labels.assert_called_with(flag_key="my-flag", result="enabled")
    mock_counter.labels.return_value.inc.assert_called_once()


def test_record_cache_hit(mocker):
    mock_counter = mocker.patch("backend.app.core.metrics.cache_hits_total")
    from backend.app.core.metrics import record_cache_hit

    record_cache_hit("redis")

    mock_counter.labels.assert_called_with(cache_type="redis")
    mock_counter.labels.return_value.inc.assert_called_once()


def test_record_cache_miss(mocker):
    mock_counter = mocker.patch("backend.app.core.metrics.cache_misses_total")
    from backend.app.core.metrics import record_cache_miss

    record_cache_miss("redis")

    mock_counter.labels.assert_called_with(cache_type="redis")
    mock_counter.labels.return_value.inc.assert_called_once()


def test_update_active_experiments(mocker):
    mock_gauge = mocker.patch("backend.app.core.metrics.active_experiments_gauge")
    from backend.app.core.metrics import update_active_experiments

    update_active_experiments(42)

    mock_gauge.set.assert_called_with(42)


def test_monitoring_config_all_metrics_valid():
    from backend.app.core.monitoring_config import METRICS_REGISTRY

    assert len(METRICS_REGISTRY) > 0, "METRICS_REGISTRY must not be empty"

    for name, spec in METRICS_REGISTRY.items():
        assert spec.name == name, (
            f"Spec name '{spec.name}' must match registry key '{name}'"
        )
        assert len(spec.description) > 0, (
            f"Spec '{name}' must have a non-empty description"
        )
        assert isinstance(spec.labels, list), f"Spec '{name}' labels must be a list"


def test_monitoring_config_cloudwatch_alarms_valid():
    from backend.app.core.monitoring_config import CLOUDWATCH_ALARMS

    assert len(CLOUDWATCH_ALARMS) > 0, "CLOUDWATCH_ALARMS must not be empty"

    for alarm_name, alarm_cfg in CLOUDWATCH_ALARMS.items():
        assert "metric" in alarm_cfg, f"Alarm '{alarm_name}' must have 'metric'"
        assert "threshold" in alarm_cfg, f"Alarm '{alarm_name}' must have 'threshold'"
        assert "comparison" in alarm_cfg, f"Alarm '{alarm_name}' must have 'comparison'"


def test_record_request_status_code_as_int(mocker):
    """Ensures status_code label is passed as an integer, not a string."""
    mock_counter = mocker.patch("backend.app.core.metrics.http_requests_total")
    mock_hist = mocker.patch("backend.app.core.metrics.http_request_duration_seconds")
    from backend.app.core.metrics import record_request

    record_request("DELETE", "/api/v1/experiments/123", 204, 0.005)

    call_kwargs = mock_counter.labels.call_args
    assert call_kwargs.kwargs["status_code"] == 204
    assert isinstance(call_kwargs.kwargs["status_code"], int)

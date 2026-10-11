"""
Unit tests for SafetyScheduler automatic rollbacks.

The scheduler must roll an unhealthy flag back to the percentage configured
in the flag's safety config (``rollback_percentage``) rather than always to 0,
and must read its cadence from ``settings.SAFETY_CHECK_INTERVAL_MINUTES``.
The reason it records rounds the measured value and the threshold (#1068),
and the Slack alert and webhook it sends carry that value and threshold with
the metric's unit (#1076). The reason words the breach by the threshold's
comparison (above, below, equal to) and writes it in the metric's unit, the
same in the scheduler and in ``SafetyService.should_rollback`` (#1127).
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from backend.app.core.safety_scheduler import (
    SafetyScheduler,
    _rollback_target_percentage,
)
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.safety import RollbackTriggerType
from backend.app.schemas.safety import MetricStatus, SafetyCheckResponse
from backend.app.services.notification_service import NotificationService
from backend.app.services.safety_service import (
    SafetyService,
    format_metric_reading,
    format_metric_value,
)
from backend.app.services.slack_notifier import SlackNotifier


def _unhealthy_scheduler_env(
    rollback_percentage, enable_automatic_rollbacks=True, is_healthy=False
):
    """Build the mocked db + SafetyService for one unhealthy flag."""
    flag = MagicMock()
    flag.id = "flag-uuid-1"
    flag.key = "streampulse_player_v2"
    flag.rollout_percentage = 25
    flag.status = FeatureFlagStatus.ACTIVE

    metric = MagicMock()
    metric.is_healthy = is_healthy
    metric.name = "error_rate"
    metric.current_value = 0.12
    metric.threshold = 0.05

    safety_check = MagicMock()
    safety_check.is_healthy = is_healthy
    safety_check.details = "error_rate too high"
    safety_check.metrics = [metric]

    config = MagicMock()
    config.enabled = True
    config.rollback_percentage = rollback_percentage

    global_settings = MagicMock()
    global_settings.enable_automatic_rollbacks = enable_automatic_rollbacks

    rollback_result = MagicMock()
    rollback_result.success = True
    rollback_result.message = f"Rolled back to {rollback_percentage}%"

    service = MagicMock()
    service.async_get_feature_flag_safety_config = AsyncMock(return_value=config)
    service.check_feature_flag_safety = AsyncMock(return_value=safety_check)
    service.async_get_safety_settings = AsyncMock(return_value=global_settings)
    service.async_rollback_feature_flag = AsyncMock(return_value=rollback_result)

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [flag]
    return db, service, flag


@pytest.mark.asyncio
async def test_rollback_uses_config_rollback_percentage():
    db, service, flag = _unhealthy_scheduler_env(rollback_percentage=5)

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()

    service.async_rollback_feature_flag.assert_awaited_once()
    kwargs = service.async_rollback_feature_flag.await_args.kwargs
    assert kwargs["feature_flag_id"] == flag.id
    assert kwargs["percentage"] == 5
    assert kwargs["trigger_type"] == RollbackTriggerType.AUTOMATIC
    assert "error_rate" in kwargs["reason"]

    scheduler._notification_service.notify_safety_rollback.assert_called_once()
    assert (
        scheduler._notification_service.notify_safety_rollback.call_args.kwargs[
            "feature_flag_name"
        ]
        == flag.key
    )


@pytest.mark.asyncio
async def test_rollback_defaults_to_zero_when_config_has_no_percentage():
    db, service, _ = _unhealthy_scheduler_env(rollback_percentage=None)

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()

    assert service.async_rollback_feature_flag.await_args.kwargs["percentage"] == 0


@pytest.mark.asyncio
async def test_no_rollback_when_automatic_rollbacks_disabled():
    db, service, _ = _unhealthy_scheduler_env(
        rollback_percentage=5, enable_automatic_rollbacks=False
    )

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()

    service.async_rollback_feature_flag.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rollback_when_healthy():
    db, service, _ = _unhealthy_scheduler_env(rollback_percentage=5, is_healthy=True)

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        await scheduler.check_feature_flags_safety()

    service.async_rollback_feature_flag.assert_not_awaited()


@pytest.mark.parametrize(
    "value, expected",
    [
        (5, 5),
        (0, 0),
        (100, 100),
        (150, 100),
        (-3, 0),
        (7.9, 7),
        (None, 0),
        ("5", 0),
        (True, 0),
        (MagicMock(), 0),
    ],
)
def test_rollback_target_percentage_clamps_and_defaults(value, expected):
    config = MagicMock()
    config.rollback_percentage = value
    assert _rollback_target_percentage(config) == expected


def test_interval_defaults_come_from_settings(monkeypatch):
    from backend.app.core import rollout_scheduler as rollout_module
    from backend.app.core import safety_scheduler as safety_module

    monkeypatch.setattr(safety_module.app_settings, "SAFETY_CHECK_INTERVAL_MINUTES", 1)
    monkeypatch.setattr(
        rollout_module.app_settings, "ROLLOUT_CHECK_INTERVAL_MINUTES", 2
    )

    assert safety_module.SafetyScheduler().interval_minutes == 1
    assert rollout_module.RolloutScheduler().interval_minutes == 2
    # explicit argument still wins
    assert safety_module.SafetyScheduler(interval_minutes=9).interval_minutes == 9
    assert rollout_module.RolloutScheduler(interval_minutes=9).interval_minutes == 9


def test_settings_have_scheduler_interval_defaults():
    from backend.app.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.SAFETY_CHECK_INTERVAL_MINUTES == 5
    assert settings.ROLLOUT_CHECK_INTERVAL_MINUTES == 15


@pytest.mark.asyncio
async def test_no_repeat_rollback_when_flag_is_already_at_target():
    """An unhealthy flag that already sits at its rollback percentage is left
    alone: no new rollback call, record or notification each cycle."""
    db, service, flag = _unhealthy_scheduler_env(rollback_percentage=5)
    flag.rollout_percentage = 5

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()

    service.async_rollback_feature_flag.assert_not_awaited()
    scheduler._notification_service.notify_safety_rollback.assert_not_called()


# --- the rollback reason reads as a person would write it (#1068) -----------


def _one_breaching_metric(
    current_value, threshold, name="error_rate", comparison_type=None
):
    """A real unhealthy check holding one breaching metric.

    ``comparison_type`` goes into ``details`` as ``check_feature_flag_safety``
    puts it there; left out, the metric carries no details at all.
    """
    return SafetyCheckResponse(
        feature_flag_id=uuid4(),
        is_healthy=False,
        metrics=[
            MetricStatus(
                name=name,
                current_value=current_value,
                threshold=threshold,
                is_healthy=False,
                details=(
                    {"comparison_type": comparison_type} if comparison_type else None
                ),
            )
        ],
    )


@pytest.mark.regression
@pytest.mark.asyncio
async def test_automatic_rollback_reason_rounds_the_measured_value():
    """1 error in 14 evaluations: the audit entry, the rollback record and the
    notification all read ``7.143%``, not ``0.07142857142857142``."""
    db, service, flag = _unhealthy_scheduler_env(rollback_percentage=0)
    service.check_feature_flag_safety = AsyncMock(
        return_value=_one_breaching_metric(1 / 14, 0.05)
    )

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()

    expected = "Automatic rollback due to error_rate above threshold (7.143% > 5%)"
    assert service.async_rollback_feature_flag.await_args.kwargs["reason"] == expected
    notify = scheduler._notification_service.notify_safety_rollback
    assert notify.call_args.kwargs["reason"] == expected


@pytest.mark.regression
@pytest.mark.asyncio
async def test_should_rollback_reason_rounds_the_measured_value():
    """The same sentence shape in ``SafetyService.should_rollback``."""
    flag = MagicMock()
    flag.status = FeatureFlagStatus.ACTIVE
    flag.rollout_percentage = 50
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = flag
    config = MagicMock()
    config.enabled = True
    config.rollback_percentage = 0
    global_settings = MagicMock()
    global_settings.enable_automatic_rollbacks = True

    service = SafetyService(db)
    with (
        patch.object(
            service, "get_feature_flag_safety_config", AsyncMock(return_value=config)
        ),
        patch.object(
            service, "get_safety_settings", AsyncMock(return_value=global_settings)
        ),
        patch.object(
            service,
            "check_feature_flag_safety",
            AsyncMock(return_value=_one_breaching_metric(1 / 14, 0.05)),
        ),
    ):
        should, reason, details = await service.should_rollback(db, uuid4())

    assert should is True
    assert reason == "Metric 'error_rate' is above threshold (7.143% > 5%)"
    # The raw value is still there for anything that computes with it.
    assert details["trigger_value"] == 1 / 14


# --- the reason is worded by the comparison, in the metric's unit (#1127) -----


async def _scheduler_reason(check):
    """The reason the scheduler records for ``check``."""
    db, service, _ = _unhealthy_scheduler_env(rollback_percentage=0)
    service.check_feature_flag_safety = AsyncMock(return_value=check)
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()
    return service.async_rollback_feature_flag.await_args.kwargs["reason"]


async def _service_reason(check):
    """The reason ``SafetyService.should_rollback`` gives for ``check``."""
    flag = MagicMock()
    flag.status = FeatureFlagStatus.ACTIVE
    flag.rollout_percentage = 50
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = flag
    config = MagicMock()
    config.enabled = True
    config.rollback_percentage = 0
    global_settings = MagicMock()
    global_settings.enable_automatic_rollbacks = True

    service = SafetyService(db)
    with (
        patch.object(
            service, "get_feature_flag_safety_config", AsyncMock(return_value=config)
        ),
        patch.object(
            service, "get_safety_settings", AsyncMock(return_value=global_settings)
        ),
        patch.object(
            service, "check_feature_flag_safety", AsyncMock(return_value=check)
        ),
    ):
        should, reason, _ = await service.should_rollback(db, uuid4())
    assert should is True
    return reason


# (comparison_type, name, value, threshold, the breach as the reason words it)
_BREACHES = [
    ("greater_than", "error_rate", 1 / 14, 0.05, "above threshold (7.143% > 5%)"),
    ("less_than", "error_rate", 0.01, 0.05, "below threshold (1% < 5%)"),
    ("equal_to", "error_rate", 0.05, 0.05, "equal to threshold (5% = 5%)"),
    (
        "greater_than",
        "avg_latency",
        523.4567,
        500.0,
        "above threshold (523.5 ms > 500 ms)",
    ),
    ("less_than", "avg_latency", 40.0, 100.0, "below threshold (40 ms < 100 ms)"),
    ("less_than", "checkout_success", 0.9, 0.95, "below threshold (0.9 < 0.95)"),
    ("equal_to", "checkout_success", 3.0, 3.0, "equal to threshold (3 = 3)"),
    # No comparison_type in the details reads as the default, greater_than.
    (None, "checkout_success", 0.5, 0.25, "above threshold (0.5 > 0.25)"),
]


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("comparison, name, value, limit, breach", _BREACHES)
async def test_automatic_rollback_reason_words_the_breach_in_the_scheduler(
    comparison, name, value, limit, breach
):
    """A ``less_than`` metric fell below its threshold; the reason used to say
    it was ``exceeding`` it, with ``>``, and printed the raw fraction."""
    check = _one_breaching_metric(value, limit, name=name, comparison_type=comparison)
    assert await _scheduler_reason(check) == (
        f"Automatic rollback due to {name} {breach}"
    )


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("comparison, name, value, limit, breach", _BREACHES)
async def test_should_rollback_reason_words_the_breach(
    comparison, name, value, limit, breach
):
    """The same breach, worded the same, in ``SafetyService.should_rollback``."""
    check = _one_breaching_metric(value, limit, name=name, comparison_type=comparison)
    assert await _service_reason(check) == f"Metric '{name}' is {breach}"


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("comparison, name, value, limit, breach", _BREACHES)
async def test_service_and_scheduler_word_the_same_breach_alike(
    comparison, name, value, limit, breach
):
    check = _one_breaching_metric(value, limit, name=name, comparison_type=comparison)
    from_scheduler = await _scheduler_reason(check)
    from_service = await _service_reason(check)
    assert from_scheduler.removeprefix(f"Automatic rollback due to {name} ") == (
        from_service.removeprefix(f"Metric '{name}' is ")
    )


@pytest.mark.regression
@pytest.mark.asyncio
async def test_error_rate_reason_is_a_percentage_not_a_fraction():
    """The alert beside the reason reads ``7.143% / 5%``; the reason read
    ``0.07143 > 0.05``."""
    check = _one_breaching_metric(1 / 14, 0.05, comparison_type="greater_than")
    for reason in (await _scheduler_reason(check), await _service_reason(check)):
        assert "7.143%" in reason and "5%" in reason
        assert "0.07143" not in reason and "0.05" not in reason


@pytest.mark.regression
@pytest.mark.asyncio
async def test_less_than_threshold_from_the_safety_config_reaches_the_reason():
    """End to end through ``check_feature_flag_safety``: the comparison the
    operator configured is what the reason says, not the default."""
    flag = MagicMock()
    flag.id = uuid4()
    flag.key = "checkout_v2"
    flag.status = FeatureFlagStatus.ACTIVE
    flag.rollout_percentage = 50
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = flag

    config = MagicMock()
    config.enabled = True
    config.rollback_percentage = 0
    config.metrics = {
        "checkout_success": {
            "critical_threshold": 0.95,
            "comparison_type": "less_than",
        }
    }
    global_settings = MagicMock()
    global_settings.enable_automatic_rollbacks = True

    service = SafetyService(db)
    with (
        patch.object(service, "_require_flag", return_value=flag),
        patch.object(service, "_error_window", return_value={}),
        patch.object(service, "_get_metric_value", return_value=0.9),
        patch.object(
            service, "get_feature_flag_safety_config", AsyncMock(return_value=config)
        ),
        patch.object(
            service, "get_safety_settings", AsyncMock(return_value=global_settings)
        ),
    ):
        should, reason, details = await service.should_rollback(db, flag.id)

    assert should is True
    assert reason == "Metric 'checkout_success' is below threshold (0.9 < 0.95)"
    assert details["trigger_value"] == 0.9
    assert details["threshold_value"] == 0.95


@pytest.mark.parametrize(
    "details",
    [None, {}, {"comparison_type": None}, {"comparison_type": "nonsense"}, "text"],
)
def test_describe_breach_reads_an_unknown_comparison_as_greater_than(details):
    """``_breaches`` treats anything but ``less_than`` and ``equal_to`` as
    ``greater_than``; the wording follows it."""
    from backend.app.services.safety_service import describe_breach

    status = MetricStatus(
        name="error_rate",
        current_value=0.2,
        threshold=0.1,
        is_healthy=False,
        details=None,
    )
    status.details = details
    assert describe_breach(status) == "above threshold (20% > 10%)"


@pytest.mark.regression
@pytest.mark.parametrize(
    "value, expected",
    [
        (1 / 14, "0.07143"),
        (1 / 3, "0.3333"),
        (0.1, "0.1"),
        (0.05, "0.05"),
        (0.0, "0"),
        (500.0, "500"),
        (523.4567, "523.5"),
        (30000.123, "30000"),
        (0.00001, "0.00001"),
    ],
)
def test_format_metric_value_four_significant_figures_no_exponent(value, expected):
    assert format_metric_value(value) == expected


# --- the alert carries the measured value and threshold (#1076) --------------


def _capturing_notification_service():
    """The real NotificationService and SlackNotifier, with Slack's client and
    the webhook POST replaced by recorders.

    Returns the service and the two lists the Slack messages and the webhook
    payloads are appended to.
    """
    service = NotificationService()
    service._webhook_url = "https://hooks.example.com/rollback"
    slack = SlackNotifier()
    slack._enabled = True
    slack._token = "xoxb-test-token"
    slack._sdk_available = True
    service._slack = slack
    service._email = MagicMock()
    slack_messages: list = []
    webhook_payloads: list = []
    return service, slack_messages, webhook_payloads


async def _run_rollback_with_real_alert(
    metric_name, current_value, threshold, comparison_type=None
):
    """Drive the scheduler's rollback path for one breaching metric and return
    what Slack and the webhook were sent."""
    db, safety, _ = _unhealthy_scheduler_env(rollback_percentage=0)
    safety.check_feature_flag_safety = AsyncMock(
        return_value=_one_breaching_metric(
            current_value, threshold, name=metric_name, comparison_type=comparison_type
        )
    )
    notifications, slack_messages, webhook_payloads = _capturing_notification_service()

    slack_client = MagicMock()
    slack_client.chat_postMessage.side_effect = lambda **kw: slack_messages.append(kw)

    def fake_post(url, json=None, timeout=None):
        webhook_payloads.append(json)
        return MagicMock(status_code=200)

    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=safety),
        patch(
            "backend.app.services.slack_notifier.WebClient", return_value=slack_client
        ),
        patch(
            "backend.app.services.notification_service.httpx.post",
            side_effect=fake_post,
        ),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = notifications
        await scheduler.check_feature_flags_safety()

    assert len(slack_messages) == 1
    assert len(webhook_payloads) == 1
    # An automatic rollback sends no email.
    assert notifications._email.method_calls == []
    message = slack_messages[0]
    fields = [f["text"] for f in message["blocks"][1]["fields"]]
    return message["text"], fields, webhook_payloads[0]


@pytest.mark.regression
@pytest.mark.asyncio
async def test_automatic_rollback_alert_carries_the_measured_error_rate():
    """1 error in 14 evaluations against 0.05: Slack and the webhook show
    7.143% and 5%, not 0.0% and 0.0%."""
    text, fields, payload = await _run_rollback_with_real_alert(
        "error_rate", 1 / 14, 0.05
    )

    assert text == (
        "Safety Rollback: 'streampulse_player_v2' rolled back — "
        "error rate 7.143% above threshold 5%."
    )
    assert "*Error Rate:*\n7.143%" in fields
    assert "*Threshold:*\n5%" in fields
    assert "0.0%" not in text + "".join(fields)

    assert payload["event_type"] == "safety_rollback"
    assert "error rate 7.143% above threshold 5%" in payload["message"]
    assert payload["metadata"]["metric"] == "error_rate"
    assert payload["metadata"]["value"] == 1 / 14
    assert payload["metadata"]["threshold"] == 0.05
    assert payload["metadata"]["error_rate"] == 1 / 14


@pytest.mark.regression
@pytest.mark.asyncio
async def test_automatic_rollback_alert_shows_a_latency_in_ms():
    """An average latency is labelled as one and reads in ms, not as a
    percentage; the webhook's ``error_rate`` is empty rather than a latency."""
    text, fields, payload = await _run_rollback_with_real_alert(
        "avg_latency", 523.4567, 500.0
    )

    assert text == (
        "Safety Rollback: 'streampulse_player_v2' rolled back — "
        "avg latency 523.5 ms above threshold 500 ms."
    )
    assert "*Avg Latency:*\n523.5 ms" in fields
    assert "*Threshold:*\n500 ms" in fields
    assert not any("Error Rate" in f for f in fields)
    assert "%" not in text + "".join(fields)

    assert "avg latency 523.5 ms above threshold 500 ms" in payload["message"]
    assert payload["metadata"]["metric"] == "avg_latency"
    assert payload["metadata"]["value"] == 523.4567
    assert payload["metadata"]["threshold"] == 500.0
    assert payload["metadata"]["error_rate"] is None


# (comparison_type, metric, value, threshold, the alert's line)
_ALERT_LINES = [
    ("less_than", "error_rate", 0.01, 0.05, "error rate 1% below threshold 5%"),
    ("equal_to", "error_rate", 0.05, 0.05, "error rate 5% equal to threshold 5%"),
    (
        "less_than",
        "avg_latency",
        40.0,
        100.0,
        "avg latency 40 ms below threshold 100 ms",
    ),
    (
        "less_than",
        "checkout_success",
        0.9,
        0.95,
        "checkout success 0.9 below threshold 0.95",
    ),
    ("greater_than", "error_rate", 0.1, 0.05, "error rate 10% above threshold 5%"),
]


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("comparison, name, value, limit, line", _ALERT_LINES)
async def test_automatic_rollback_alert_words_the_breach_by_the_comparison(
    comparison, name, value, limit, line
):
    """A ``less_than`` metric that fell below its floor was announced as
    having ``exceeded`` it, in the Slack text and in the webhook message, next
    to a reason that said ``below``."""
    text, _, payload = await _run_rollback_with_real_alert(
        name, value, limit, comparison_type=comparison
    )

    assert text == f"Safety Rollback: 'streampulse_player_v2' rolled back — {line}."
    assert f"rolled back: {line}. Reason: " in payload["message"]
    assert "exceeded" not in text + payload["message"]
    # The alert's line and the reason word the breach alike.
    word = {"less_than": "below", "equal_to": "equal to", "greater_than": "above"}
    assert f" {word[comparison]} threshold (" in payload["metadata"]["reason"]


@pytest.mark.regression
@pytest.mark.asyncio
async def test_scheduler_hands_the_comparison_to_the_notification():
    db, service, _ = _unhealthy_scheduler_env(rollback_percentage=0)
    service.check_feature_flag_safety = AsyncMock(
        return_value=_one_breaching_metric(
            0.9, 0.95, name="checkout_success", comparison_type="less_than"
        )
    )
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", return_value=db),
        patch("backend.app.core.safety_scheduler.SafetyService", return_value=service),
    ):
        scheduler = SafetyScheduler()
        scheduler._notification_service = MagicMock()
        await scheduler.check_feature_flags_safety()

    kwargs = scheduler._notification_service.notify_safety_rollback.call_args.kwargs
    assert kwargs["comparison"] == "less_than"
    assert (kwargs["value"], kwargs["threshold"]) == (0.9, 0.95)


def test_alert_without_a_comparison_reads_as_greater_than():
    """A caller that does not pass ``comparison`` (every caller before #1127)
    gets the ``greater_than`` wording, in the notification and in Slack."""
    notifications, slack_messages, webhook_payloads = _capturing_notification_service()
    slack_client = MagicMock()
    slack_client.chat_postMessage.side_effect = lambda **kw: slack_messages.append(kw)

    def fake_post(url, json=None, timeout=None):
        webhook_payloads.append(json)
        return MagicMock(status_code=200)

    with (
        patch(
            "backend.app.services.slack_notifier.WebClient", return_value=slack_client
        ),
        patch(
            "backend.app.services.notification_service.httpx.post",
            side_effect=fake_post,
        ),
    ):
        notifications.notify_safety_rollback(
            "flag-id",
            "my-flag",
            "reason",
            metric_name="error_rate",
            value=0.1,
            threshold=0.05,
        )
        # The Slack notifier on its own, with no ``relation``.
        notifications._slack.send_safety_rollback_alert(
            flag_name="my-flag",
            reason="reason",
            metric_label="error rate",
            value="10%",
            threshold="5%",
        )

    assert "error rate 10% above threshold 5%" in webhook_payloads[0]["message"]
    assert slack_messages[0]["text"].endswith("error rate 10% above threshold 5%.")
    assert slack_messages[1]["text"].endswith("error rate 10% above threshold 5%.")


@pytest.mark.regression
def test_rollback_alert_without_a_metric_shows_no_numbers():
    """No breaching metric known: the alert gives the reason and no invented
    0.0% value or threshold."""
    notifications, slack_messages, webhook_payloads = _capturing_notification_service()
    slack_client = MagicMock()
    slack_client.chat_postMessage.side_effect = lambda **kw: slack_messages.append(kw)

    def fake_post(url, json=None, timeout=None):
        webhook_payloads.append(json)
        return MagicMock(status_code=200)

    with (
        patch(
            "backend.app.services.slack_notifier.WebClient", return_value=slack_client
        ),
        patch(
            "backend.app.services.notification_service.httpx.post",
            side_effect=fake_post,
        ),
    ):
        notifications.notify_safety_rollback(
            feature_flag_id="flag-1",
            feature_flag_name="checkout_v2",
            reason="Automatic rollback due to safety issues",
        )

    text = slack_messages[0]["text"]
    fields = [f["text"] for f in slack_messages[0]["blocks"][1]["fields"]]
    assert text == (
        "Safety Rollback: 'checkout_v2' rolled back. "
        "Reason: Automatic rollback due to safety issues"
    )
    assert fields == [
        "*Flag:*\ncheckout_v2",
        "*Reason:*\nAutomatic rollback due to safety issues",
    ]
    metadata = webhook_payloads[0]["metadata"]
    assert metadata["value"] is None
    assert metadata["threshold"] is None
    assert metadata["error_rate"] is None


@pytest.mark.regression
@pytest.mark.parametrize(
    "name, value, expected",
    [
        ("error_rate", 1 / 14, "7.143%"),
        ("error_rate", 0.05, "5%"),
        ("error_rate", 0.07, "7%"),
        ("error_rate", 0.0, "0%"),
        ("avg_latency", 523.4567, "523.5 ms"),
        ("latency", 250.0, "250 ms"),
        ("p95_latency", 30000.123, "30000 ms"),
        ("error_count", 12.0, "12"),
    ],
)
def test_format_metric_reading_gives_the_unit(name, value, expected):
    assert format_metric_reading(name, value) == expected

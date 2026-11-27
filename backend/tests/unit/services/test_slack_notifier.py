"""
Unit tests for SlackNotifier (EP-030 Batch 1A TDD).

Tests cover:
- Initialization with settings
- Disabled / no-token short-circuits return False
- _send_message success and error paths
- Custom vs default channel routing
- All public notification methods (safety rollback, experiment started/completed,
  rollout advanced/completed, generic alert)
- Block Kit structure for safety and experiment blocks
- Exception containment — no exception must ever propagate out
- All methods return False when Slack is disabled
"""

import json
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notifier(
    slack_enabled: bool = True,
    slack_bot_token: str = "xoxb-test-token",
    slack_default_channel: str = "#platform-alerts",
    sdk_available: bool = True,
):
    """
    Return a SlackNotifier whose settings and SDK availability can be
    controlled by the caller.
    """
    with (
        patch("backend.app.services.slack_notifier.settings") as mock_settings,
        patch("backend.app.services.slack_notifier.SLACK_SDK_AVAILABLE", sdk_available),
    ):
        mock_settings.SLACK_ENABLED = slack_enabled
        mock_settings.SLACK_BOT_TOKEN = slack_bot_token
        mock_settings.SLACK_DEFAULT_CHANNEL = slack_default_channel

        from backend.app.services.slack_notifier import SlackNotifier

        notifier = SlackNotifier()
        # Override instance attributes directly so they don't re-read settings
        notifier._enabled = slack_enabled
        notifier._token = slack_bot_token
        notifier._default_channel = slack_default_channel
        notifier._sdk_available = sdk_available
    return notifier


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_with_settings(self):
        """SlackNotifier reads SLACK_* settings on construction."""
        with patch("backend.app.services.slack_notifier.settings") as mock_settings:
            mock_settings.SLACK_ENABLED = True
            mock_settings.SLACK_BOT_TOKEN = "xoxb-my-token"
            mock_settings.SLACK_DEFAULT_CHANNEL = "#eng-alerts"

            from backend.app.services.slack_notifier import SlackNotifier

            notifier = SlackNotifier()

        assert notifier._enabled is True
        assert notifier._token == "xoxb-my-token"
        assert notifier._default_channel == "#eng-alerts"


# ---------------------------------------------------------------------------
# Disabled / no-token guards
# ---------------------------------------------------------------------------


class TestDisabledGuards:
    def test_send_when_disabled_returns_false(self):
        """All sends return False immediately when SLACK_ENABLED is False."""
        notifier = _make_notifier(slack_enabled=False)
        result = notifier._send_message(channel="#test", blocks=[], text="hello")
        assert result is False

    def test_send_when_no_token_returns_false(self):
        """All sends return False when the bot token is empty."""
        notifier = _make_notifier(slack_bot_token="")
        result = notifier._send_message(channel="#test", blocks=[], text="hello")
        assert result is False


# ---------------------------------------------------------------------------
# _send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    def test_send_message_success(self):
        """Returns True when WebClient.chat_postMessage succeeds."""
        notifier = _make_notifier()

        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch(
            "backend.app.services.slack_notifier.WebClient", return_value=mock_client
        ):
            result = notifier._send_message(channel="#test", blocks=[], text="hello")

        assert result is True
        mock_client.chat_postMessage.assert_called_once()

    def test_send_message_slack_api_error_returns_false(self):
        """SlackApiError is swallowed and False is returned."""
        from slack_sdk.errors import SlackApiError

        notifier = _make_notifier()

        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = SlackApiError(
            message="channel_not_found",
            response={"ok": False, "error": "channel_not_found"},
        )

        with patch(
            "backend.app.services.slack_notifier.WebClient", return_value=mock_client
        ):
            result = notifier._send_message(channel="#missing", blocks=[], text="hello")

        assert result is False

    def test_send_message_connection_error_returns_false(self):
        """Generic connection exceptions are swallowed and False is returned."""
        notifier = _make_notifier()

        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = ConnectionError("network error")

        with patch(
            "backend.app.services.slack_notifier.WebClient", return_value=mock_client
        ):
            result = notifier._send_message(channel="#test", blocks=[], text="hello")

        assert result is False

    def test_send_message_uses_custom_channel(self):
        """_send_message passes the provided channel to chat_postMessage."""
        notifier = _make_notifier()

        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch(
            "backend.app.services.slack_notifier.WebClient", return_value=mock_client
        ):
            notifier._send_message(channel="#custom-alerts", blocks=[], text="hello")

        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert call_kwargs.get("channel") == "#custom-alerts"

    def test_send_message_uses_default_channel(self):
        """When channel=None _send_message falls back to _default_channel."""
        notifier = _make_notifier(slack_default_channel="#platform-alerts")

        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        with patch(
            "backend.app.services.slack_notifier.WebClient", return_value=mock_client
        ):
            notifier._send_message(channel=None, blocks=[], text="hello")

        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert call_kwargs.get("channel") == "#platform-alerts"


# ---------------------------------------------------------------------------
# send_safety_rollback_alert
# ---------------------------------------------------------------------------


class TestSendSafetyRollbackAlert:
    def test_send_safety_rollback_alert_success(self):
        """Returns True on a successful safety rollback alert."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True):
            result = notifier.send_safety_rollback_alert(
                flag_name="dark_mode",
                metric_label="error rate",
                value="12%",
                threshold="5%",
                reason="Error rate exceeded threshold",
            )

        assert result is True

    def test_send_safety_rollback_alert_calls_send_message(self):
        """Verifies that _send_message is invoked when sending a rollback alert."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True) as mock_send:
            notifier.send_safety_rollback_alert(
                flag_name="dark_mode",
                metric_label="error rate",
                value="12%",
                threshold="5%",
                reason="Error rate exceeded threshold",
            )

        mock_send.assert_called_once()

    def test_send_safety_rollback_alert_block_contains_flag_name(self):
        """The generated blocks include the feature flag name."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(channel, blocks, text):
            captured["blocks"] = blocks
            return True

        with patch.object(notifier, "_send_message", side_effect=capture_send):
            notifier.send_safety_rollback_alert(
                flag_name="payments_v2",
                metric_label="error rate",
                value="10%",
                threshold="5%",
                reason="Latency spike",
            )

        block_text = str(captured.get("blocks", ""))
        assert "payments_v2" in block_text

    def test_send_safety_rollback_alert_block_contains_error_rate(self):
        """The generated blocks include the error rate value."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(channel, blocks, text):
            captured["blocks"] = blocks
            return True

        with patch.object(notifier, "_send_message", side_effect=capture_send):
            notifier.send_safety_rollback_alert(
                flag_name="payments_v2",
                metric_label="error rate",
                value="12.3%",
                threshold="5%",
                reason="Latency spike",
            )

        fields = [f["text"] for f in captured["blocks"][1]["fields"]]
        assert "*Error Rate:*\n12.3%" in fields
        assert "*Threshold:*\n5%" in fields


# ---------------------------------------------------------------------------
# send_experiment_started
# ---------------------------------------------------------------------------


class TestSendExperimentStarted:
    def test_send_experiment_started_success(self):
        """Returns True on a successful experiment started notification."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True):
            result = notifier.send_experiment_started(
                experiment_name="Homepage CTA Test",
            )

        assert result is True

    def test_send_experiment_started_with_owner_email(self):
        """Owner email is included in the generated blocks when provided."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(channel, blocks, text):
            captured["blocks"] = blocks
            return True

        with patch.object(notifier, "_send_message", side_effect=capture_send):
            notifier.send_experiment_started(
                experiment_name="Homepage CTA Test",
                owner_email="alice@example.com",
            )

        block_text = str(captured.get("blocks", ""))
        assert "alice@example.com" in block_text


# ---------------------------------------------------------------------------
# send_experiment_completed
# ---------------------------------------------------------------------------


class TestSendExperimentCompleted:
    def test_send_experiment_completed_with_winner(self):
        """Winner variant name is included in the blocks when provided."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(channel, blocks, text):
            captured["blocks"] = blocks
            return True

        with patch.object(notifier, "_send_message", side_effect=capture_send):
            notifier.send_experiment_completed(
                experiment_name="Homepage CTA Test",
                winner="variant_b",
            )

        block_text = str(captured.get("blocks", ""))
        assert "variant_b" in block_text

    def test_send_experiment_completed_no_winner(self):
        """Completes successfully even when no winner is specified."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True):
            result = notifier.send_experiment_completed(
                experiment_name="Homepage CTA Test",
            )

        assert result is True


# ---------------------------------------------------------------------------
# send_rollout_advanced
# ---------------------------------------------------------------------------


class TestSendRolloutAdvanced:
    def test_send_rollout_advanced_success(self):
        """Returns True on a successful rollout advanced notification."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True):
            result = notifier.send_rollout_advanced(
                flag_name="new_checkout",
                from_pct=10,
                to_pct=50,
                stage_name="Phase 2",
            )

        assert result is True

    def test_send_rollout_advanced_shows_percentages(self):
        """Both from_pct and to_pct values appear in the generated blocks."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(channel, blocks, text):
            captured["blocks"] = blocks
            return True

        with patch.object(notifier, "_send_message", side_effect=capture_send):
            notifier.send_rollout_advanced(
                flag_name="new_checkout",
                from_pct=10,
                to_pct=50,
            )

        block_text = str(captured.get("blocks", ""))
        assert "10" in block_text
        assert "50" in block_text


# ---------------------------------------------------------------------------
# send_rollout_completed
# ---------------------------------------------------------------------------


class TestSendRolloutCompleted:
    def test_send_rollout_completed_success(self):
        """Returns True on a successful rollout completed notification."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True):
            result = notifier.send_rollout_completed(
                flag_name="new_checkout",
                final_pct=100,
            )

        assert result is True


# ---------------------------------------------------------------------------
# send_generic_alert
# ---------------------------------------------------------------------------


class TestSendGenericAlert:
    def test_send_generic_alert_info_severity(self):
        """Info-severity alert is sent successfully."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_message", return_value=True):
            result = notifier.send_generic_alert(
                title="Deployment Notice",
                message="Backend was redeployed.",
                severity="info",
            )

        assert result is True

    def test_send_generic_alert_critical_severity(self):
        """Critical-severity alert is sent successfully."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(channel, blocks, text):
            captured["blocks"] = blocks
            return True

        with patch.object(notifier, "_send_message", side_effect=capture_send):
            notifier.send_generic_alert(
                title="DB Down",
                message="Database connection pool exhausted.",
                severity="critical",
            )

        block_text = str(captured.get("blocks", ""))
        # Critical alerts should be visually distinct — check title or severity label
        assert "DB Down" in block_text or "critical" in block_text.lower()


# ---------------------------------------------------------------------------
# Block Kit structure
# ---------------------------------------------------------------------------


class TestBuildSafetyBlock:
    def test_build_safety_block_structure(self):
        """_build_safety_block returns a list of dicts with a header block."""
        notifier = _make_notifier()

        blocks = notifier._build_safety_block(
            flag_name="feature_x",
            metric_label="error rate",
            value="8%",
            threshold="5%",
            reason="High error rate",
        )

        assert isinstance(blocks, list)
        assert len(blocks) > 0
        # First block must be a header
        assert blocks[0]["type"] == "header"
        # At least one section block with fields
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        assert len(section_blocks) >= 1


class TestBuildExperimentBlock:
    def test_build_experiment_block_structure(self):
        """_build_experiment_block returns a list of dicts with a header block."""
        notifier = _make_notifier()

        blocks = notifier._build_experiment_block(
            name="CTA Button Test",
            status="started",
            details={"owner": "bob@example.com"},
        )

        assert isinstance(blocks, list)
        assert len(blocks) > 0
        assert blocks[0]["type"] == "header"


# ---------------------------------------------------------------------------
# Exception containment
# ---------------------------------------------------------------------------


class TestExceptionContainment:
    def test_exception_never_propagates(self):
        """An exception thrown deep inside _send_message must not propagate."""
        notifier = _make_notifier()

        # Patch _send_message to raise unexpectedly
        with patch.object(
            notifier,
            "_send_message",
            side_effect=RuntimeError("unexpected crash"),
        ):
            # None of these should raise
            try:
                notifier.send_safety_rollback_alert(flag_name="flag", reason="test")
                notifier.send_experiment_started(experiment_name="Exp")
                notifier.send_experiment_completed(experiment_name="Exp")
                notifier.send_rollout_advanced(flag_name="flag", from_pct=0, to_pct=10)
                notifier.send_rollout_completed(flag_name="flag")
                notifier.send_generic_alert(title="T", message="M")
            except Exception as exc:
                pytest.fail(f"Exception propagated unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Slack disabled — all public methods return False
# ---------------------------------------------------------------------------


class TestSlackDisabledAllMethodsReturnFalse:
    def test_slack_disabled_all_methods_return_false(self):
        """When SLACK_ENABLED=False every public method returns False."""
        notifier = _make_notifier(slack_enabled=False)

        assert notifier.send_safety_rollback_alert(flag_name="f", reason="r") is False
        assert notifier.send_experiment_started(experiment_name="E") is False
        assert notifier.send_experiment_completed(experiment_name="E") is False
        assert (
            notifier.send_rollout_advanced(flag_name="f", from_pct=0, to_pct=10)
            is False
        )
        assert notifier.send_rollout_completed(flag_name="f") is False
        assert notifier.send_generic_alert(title="T", message="M") is False


# ---------------------------------------------------------------------------
# Free text is shown as typed
# ---------------------------------------------------------------------------

TYPED = "R&D <beta> <!channel>"
SHOWN = "R&amp;D &lt;beta&gt; &lt;!channel&gt;"


def _posted(method: str, **kwargs) -> dict:
    """The keyword arguments ``method`` passes to ``chat_postMessage``."""
    client = MagicMock()
    with patch("backend.app.services.slack_notifier.WebClient", return_value=client):
        assert getattr(_make_notifier(), method)(**kwargs) is True
    return client.chat_postMessage.call_args.kwargs


def _field_texts(blocks: list) -> list:
    return [field["text"] for block in blocks for field in block.get("fields", [])]


@pytest.mark.regression
@pytest.mark.parametrize(
    "method,kwargs,label",
    [
        ("send_safety_rollback_alert", {"flag_name": TYPED, "reason": "r"}, "Flag"),
        ("send_safety_rollback_alert", {"flag_name": "f", "reason": TYPED}, "Reason"),
        (
            "send_safety_rollback_alert",
            {
                "flag_name": TYPED,
                "reason": "r",
                "metric_label": "error rate",
                "value": "7.1%",
                "threshold": "5%",
            },
            "Flag",
        ),
        ("send_experiment_started", {"experiment_name": TYPED}, "Experiment"),
        (
            "send_experiment_started",
            {"experiment_name": "E", "owner_email": TYPED},
            "Owner",
        ),
        (
            "send_experiment_completed",
            {"experiment_name": "E", "winner": TYPED},
            "Winner",
        ),
        (
            "send_rollout_advanced",
            {"flag_name": TYPED, "from_pct": 0, "to_pct": 10},
            "Flag",
        ),
        (
            "send_rollout_advanced",
            {"flag_name": "f", "from_pct": 0, "to_pct": 10, "stage_name": TYPED},
            "Stage",
        ),
        ("send_rollout_completed", {"flag_name": TYPED}, "Flag"),
    ],
)
def test_free_text_is_shown_as_typed(method, kwargs, label):
    """``&``, ``<`` and ``>`` in a name or a reason reach Slack as entities,
    so the message shows the text as it was typed."""
    posted = _posted(method, **kwargs)
    assert f"*{label}:*\n{SHOWN}" in _field_texts(posted["blocks"])
    # Nothing typed reaches the message unescaped, the fallback text included.
    assert "<" not in json.dumps(posted["blocks"]) + posted["text"]


@pytest.mark.regression
def test_the_fallback_text_shows_free_text_as_typed():
    posted = _posted("send_experiment_completed", experiment_name=TYPED, winner=TYPED)
    assert posted["text"] == f"Experiment '{SHOWN}' has completed. Winner: {SHOWN}."


def test_the_notifiers_own_markup_is_sent_unchanged():
    """Its bold labels, and a generic alert's message, which its caller
    writes as mrkdwn: a link there still renders as a link."""
    link = "<https://example.com/flags/1|the flag> & *its rollout*"
    posted = _posted("send_generic_alert", title="Check", message=link)
    assert posted["blocks"][1]["text"]["text"] == link
    started = _posted("send_experiment_started", experiment_name="Checkout")
    assert _field_texts(started["blocks"])[0] == "*Experiment:*\nCheckout"

"""What the platform sends, where it goes, and the settings it reads (#1142).

Slack carries one message per event: a scheduled experiment start, resume or
end, a rollout stage the rollout scheduler starts, and a safety rollback. Each
goes to ``SLACK_DEFAULT_CHANNEL``: nothing reads a user's stored preferences
when sending. Email is sent only by ``POST /api/v1/notifications/test``; no
event path passes an owner's address, and ``NOTIFICATION_ADMIN_EMAILS`` is
read into the email notifier and never sent to.

The source checks walk ``backend/app`` (and ``modules/backend/app`` when the
modules are present), so a new caller anywhere fails here until this file
names it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic_settings import SettingsError

from backend.app.api import deps
from backend.app.core.config import Settings
from backend.app.main import app
from backend.app.models.user import UserRole
from backend.app.schemas.notification import NotificationPreferenceUpdate
from backend.app.services.email_notifier import EmailNotifier
from backend.app.services.notification_service import NotificationService
from backend.app.services.slack_notifier import SlackNotifier

REPO = pathlib.Path(__file__).resolve().parents[4]
APP_TREES = [
    REPO / "backend" / "app",
    REPO / "modules" / "backend" / "app",
]
SERVICE_MODULE = "backend.app.services.notification_service"


def _sources():
    for tree in APP_TREES:
        if not tree.is_dir():
            continue
        for path in sorted(tree.rglob("*.py")):
            yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text())


def _calls(tree, predicate):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and predicate(node.func.attr)
        ):
            yield node


def _is_email_send(name: str) -> bool:
    return name == "_send_email" or (
        name.startswith("send_") and name.endswith("_email")
    )


def _service_with_mock_channels():
    service = NotificationService()
    service._slack = MagicMock()
    service._email = MagicMock()
    service._webhook_url = ""
    return service


def _notify_like_the_schedulers(service):
    # The keyword arguments core/scheduler.py, core/rollout_scheduler.py and
    # core/safety_scheduler.py pass: none of them passes an owner's address.
    flag_id = str(uuid.uuid4())
    service.notify_experiment_started(
        experiment_id=str(uuid.uuid4()), experiment_name="checkout", resumed=False
    )
    service.notify_experiment_started(
        experiment_id=str(uuid.uuid4()), experiment_name="checkout", resumed=True
    )
    service.notify_experiment_ended(
        experiment_id=str(uuid.uuid4()), experiment_name="checkout"
    )
    service.notify_rollout_advanced(
        feature_flag_id=flag_id, stage_name="Stage 2", new_percentage=50
    )
    service.notify_safety_rollback(
        feature_flag_id=flag_id, feature_flag_name="new-checkout", reason="threshold"
    )


# ---------------------------------------------------------------------------
# Which events send what
# ---------------------------------------------------------------------------


def test_only_the_test_route_sends_email():
    callers = set()
    for path, tree in _sources():
        if path.endswith("services/email_notifier.py"):
            continue
        if any(True for _ in _calls(tree, _is_email_send)):
            callers.add(path)
        for call in _calls(
            tree,
            lambda n: n in {"notify_experiment_started", "notify_experiment_ended"},
        ):
            names = {k.arg for k in call.keywords}
            assert "owner_email" not in names and len(call.args) <= 2, (
                f"{path}:{call.lineno} passes an owner's address, so an event "
                "now sends email"
            )
    assert callers == {
        "backend/app/services/notification_service.py",
        "backend/app/api/v1/endpoints/notifications.py",
    }

    service = _service_with_mock_channels()
    _notify_like_the_schedulers(service)
    assert service._email.method_calls == []
    assert service._slack.method_calls, "the events sent no Slack message"


def test_scheduled_changes_go_to_slack_and_no_route_sends_a_notification():
    service = _service_with_mock_channels()
    _notify_like_the_schedulers(service)
    sent = [c[0] for c in service._slack.method_calls]
    assert sent == [
        "send_experiment_started",
        "send_experiment_started",
        "send_experiment_completed",
        "send_rollout_advanced",
        "send_safety_rollback_alert",
    ]

    importers = set()
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == SERVICE_MODULE:
                importers.add(path)
            elif isinstance(node, ast.Import) and any(
                a.name == SERVICE_MODULE for a in node.names
            ):
                importers.add(path)
    # The three schedulers send the events; the two routes are the test
    # routes. The experiment start, pause and complete routes are not here.
    assert importers == {
        "backend/app/core/scheduler.py",
        "backend/app/core/rollout_scheduler.py",
        "backend/app/core/safety_scheduler.py",
        "backend/app/api/v1/endpoints/notifications.py",
        "backend/app/api/v1/endpoints/scheduler_health.py",
    }


# ---------------------------------------------------------------------------
# The settings
# ---------------------------------------------------------------------------

_ENVIRONMENT = {
    "SLACK_ENABLED": ("true", True),
    "SLACK_BOT_TOKEN": ("xoxb-1", "xoxb-1"),
    "SLACK_DEFAULT_CHANNEL": ("#team", "#team"),
    "EMAIL_ENABLED": ("true", True),
    "SENDGRID_API_KEY": ("SG.k", "SG.k"),
    "SMTP_HOST": ("smtp.example.org", "smtp.example.org"),
    "SMTP_PORT": ("2525", 2525),
    "SMTP_USERNAME": ("mailer", "mailer"),
    "SMTP_PASSWORD": ("pw", "pw"),
    "EMAIL_FROM_ADDRESS": ("ops@example.org", "ops@example.org"),
    "EMAIL_FROM_NAME": ("Ops", "Ops"),
    "NOTIFICATION_ADMIN_EMAILS": ('["a@example.org"]', ["a@example.org"]),
}


def test_the_settings_are_read_from_the_api_process_environment(monkeypatch):
    for name, (raw, _) in _ENVIRONMENT.items():
        monkeypatch.setenv(name, raw)
    built = Settings(_env_file=None)
    assert {n: getattr(built, n) for n in _ENVIRONMENT} == {
        n: parsed for n, (_, parsed) in _ENVIRONMENT.items()
    }

    # The notifiers copy the process's settings when they are built, so a
    # change takes effect when the API is restarted.
    with patch("backend.app.services.slack_notifier.settings", built):
        slack = SlackNotifier()
    assert (slack._enabled, slack._token, slack._default_channel) == (
        True,
        "xoxb-1",
        "#team",
    )

    # On the Quick Start stack the API is the compose file's ``api`` service,
    # and its settings are that service's ``environment:`` mapping.
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    assert isinstance(compose["services"]["api"]["environment"], dict)


def test_every_slack_message_goes_to_the_default_channel(monkeypatch):
    for name in ("SLACK_DEFAULT_CHANNEL", "SMTP_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert Settings(_env_file=None).SLACK_DEFAULT_CHANNEL == "#platform-alerts"

    configured = SimpleNamespace(
        SLACK_ENABLED=True,
        SLACK_BOT_TOKEN="xoxb-1",
        SLACK_DEFAULT_CHANNEL="#platform-alerts",
    )
    client = MagicMock()
    with (
        patch("backend.app.services.slack_notifier.settings", configured),
        patch("backend.app.services.slack_notifier.WebClient", return_value=client),
    ):
        service = NotificationService()
        service._slack = SlackNotifier()
        service._email = MagicMock()
        service._webhook_url = ""
        _notify_like_the_schedulers(service)

    channels = [c.kwargs["channel"] for c in client.chat_postMessage.call_args_list]
    assert len(channels) == 5
    assert set(channels) == {"#platform-alerts"}


def test_preferences_are_not_read_when_sending():
    readers = set()
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "NotificationPreference":
                readers.add(path)
            elif isinstance(node, ast.alias) and node.name == "NotificationPreference":
                readers.add(path)
            elif (
                isinstance(node, ast.ClassDef) and node.name == "NotificationPreference"
            ):
                readers.add(path)
    # The model, the model registry and the preferences routes; no sender.
    assert readers == {
        "backend/app/models/notification.py",
        "backend/app/models/__init__.py",
        "backend/app/api/v1/endpoints/notifications.py",
    }


def test_smtp_uses_port_587_by_default(monkeypatch):
    monkeypatch.delenv("SMTP_PORT", raising=False)
    assert Settings(_env_file=None).SMTP_PORT == 587

    configured = SimpleNamespace(
        EMAIL_ENABLED=True,
        SENDGRID_API_KEY="",
        SMTP_HOST="smtp.example.org",
        SMTP_PORT=587,
        SMTP_USERNAME="",
        SMTP_PASSWORD="",
        EMAIL_FROM_ADDRESS="ops@example.org",
        EMAIL_FROM_NAME="Ops",
        NOTIFICATION_ADMIN_EMAILS=[],
    )
    with patch("backend.app.services.email_notifier.settings", configured):
        notifier = EmailNotifier()
    with patch("backend.app.services.email_notifier.smtplib.SMTP") as smtp:
        assert notifier._send_email(["to@example.org"], "s", "<p>b</p>")
    smtp.assert_called_once_with("smtp.example.org", 587)


def test_every_email_is_sent_from_the_configured_sender():
    configured = SimpleNamespace(
        EMAIL_ENABLED=True,
        SENDGRID_API_KEY="SG.k",
        SMTP_HOST="smtp.example.org",
        SMTP_PORT=587,
        SMTP_USERNAME="",
        SMTP_PASSWORD="",
        EMAIL_FROM_ADDRESS="ops@example.org",
        EMAIL_FROM_NAME="Ops",
        NOTIFICATION_ADMIN_EMAILS=[],
    )
    with patch("backend.app.services.email_notifier.settings", configured):
        notifier = EmailNotifier()

    # SMTP: the From header and the envelope sender.
    with patch("backend.app.services.email_notifier.smtplib.SMTP") as smtp:
        assert notifier._send_via_smtp(["to@example.org"], "s", "<p>b</p>")
    server = smtp.return_value.__enter__.return_value
    sender, recipients, message = server.sendmail.call_args.args
    assert sender == "ops@example.org"
    assert "From: Ops <ops@example.org>" in message

    # SendGrid: the message's from address and name.
    client = MagicMock()
    client.send.return_value.status_code = 202
    with patch(
        "backend.app.services.email_notifier.SendGridAPIClient", return_value=client
    ):
        assert notifier._send_via_sendgrid(["to@example.org"], "s", "<p>b</p>")
    mail = client.send.call_args.args[0]
    assert (mail.from_email.email, mail.from_email.name) == ("ops@example.org", "Ops")


def test_admin_emails_are_a_json_array_and_are_never_sent_to(monkeypatch):
    monkeypatch.setenv(
        "NOTIFICATION_ADMIN_EMAILS", '["a@example.org", "b@example.org"]'
    )
    assert Settings(_env_file=None).NOTIFICATION_ADMIN_EMAILS == [
        "a@example.org",
        "b@example.org",
    ]

    readers = set()
    for path, tree in _sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value == "NOTIFICATION_ADMIN_EMAILS"
            ):
                readers.add(path)
            elif isinstance(node, ast.Attribute) and node.attr in {
                "NOTIFICATION_ADMIN_EMAILS",
                "_admin_emails",
            }:
                assert isinstance(node.ctx, ast.Store), (
                    f"{path}:{node.lineno} reads the admin addresses"
                )
                readers.add(path)
    # Declared in the settings, copied into the email notifier, used by nobody.
    assert readers == {"backend/app/services/email_notifier.py"}


def test_a_comma_separated_admin_email_list_stops_the_api_from_starting():
    environment = dict(os.environ)
    environment["NOTIFICATION_ADMIN_EMAILS"] = "a@example.org,b@example.org"
    result = subprocess.run(
        [sys.executable, "-c", "import backend.app.core.config"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert 'error parsing value for field "NOTIFICATION_ADMIN_EMAILS"' in result.stderr

    with pytest.raises(SettingsError, match="NOTIFICATION_ADMIN_EMAILS"):
        with patch.dict(os.environ, environment):
            Settings(_env_file=None)


# ---------------------------------------------------------------------------
# The preferences API
# ---------------------------------------------------------------------------

_FIELDS = {
    "notify_experiment_started",
    "notify_experiment_completed",
    "notify_safety_rollback",
    "notify_rollout_advanced",
    "slack_channel",
    "email_override",
}


class _Stored(SimpleNamespace):
    """A preferences row: only the attributes set on it exist."""


def _stored(user_id):
    now = datetime.now(timezone.utc)
    return _Stored(
        id=uuid.uuid4(),
        user_id=user_id,
        notify_experiment_started=True,
        notify_experiment_completed=True,
        notify_safety_rollback=True,
        notify_rollout_advanced=False,
        slack_channel=None,
        email_override=None,
        created_at=now,
        updated_at=now,
    )


def _user(role):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.role = role
    user.is_active = True
    user.is_superuser = False
    return user


@pytest.fixture
def signed_in():
    def sign_in(role):
        user = _user(role)
        row = _stored(user.id)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = row
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        app.dependency_overrides[deps.get_db] = lambda: db
        return row

    yield sign_in
    app.dependency_overrides.pop(deps.get_current_active_user, None)
    app.dependency_overrides.pop(deps.get_db, None)


def test_the_preference_fields_and_any_other_field_is_ignored(signed_in):
    assert set(NotificationPreferenceUpdate.model_fields) == _FIELDS

    row = signed_in(UserRole.ADMIN)
    response = TestClient(app).put(
        "/api/v1/notifications/preferences",
        json={"notify_rollout_advanced": True, "quiet_hours": "22-07", "user_id": "x"},
    )
    assert response.status_code == 200, response.text
    assert row.notify_rollout_advanced is True
    assert not hasattr(row, "quiet_hours")
    assert row.user_id != "x"


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_any_signed_in_user_reads_and_updates_their_own_preferences(signed_in, role):
    row = signed_in(role)
    client = TestClient(app)

    read = client.get("/api/v1/notifications/preferences")
    assert read.status_code == 200, read.text
    assert read.json()["user_id"] == str(row.user_id)

    update = client.put(
        "/api/v1/notifications/preferences", json={"slack_channel": "#mine"}
    )
    assert update.status_code == 200, update.text
    assert update.json()["slack_channel"] == "#mine"

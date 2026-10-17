# Slack & Email Alerting

The platform sends a Slack message for each event in the table below. Email is sent only by the test route described on this page.

Notification failure is always non-fatal — if a Slack message or email cannot be sent, the platform operation that triggered it continues normally and the failure is logged.

---

## Supported Channels

| Channel | Provider | Configuration |
|---------|----------|--------------|
| Slack | Slack Block Kit via `slack_sdk` | `SLACK_BOT_TOKEN`, `SLACK_DEFAULT_CHANNEL` |
| Email | SendGrid (preferred) or SMTP fallback | `SENDGRID_API_KEY` or SMTP settings |

---

## Event Types

| Event | Slack | Email | Description |
|-------|-------|-------|-------------|
| Safety rollback | ✅ | ❌ | Feature flag auto-rolled back due to error rate or latency threshold |
| Scheduled experiment change | ✅ | ❌ | The scheduler started, resumed or completed an experiment; the start, pause and complete routes send nothing |
| Rollout stage advance | ✅ | ❌ | Rollout schedule progressed to next stage |

---

## Configuration

These are settings of the API process: put them in its environment (on the Quick Start
stack, the `api` service's `environment:` in `docker-compose.yml`), then restart it.
Both channels are off until you turn them on.

| Setting | What it does |
|---------|--------------|
| `SLACK_ENABLED` | `true` turns Slack messages on; the default is `false`. |
| `SLACK_BOT_TOKEN` | The bot token messages are sent with. |
| `SLACK_DEFAULT_CHANNEL` | The channel every event's message goes to (default `#platform-alerts`). |
| `EMAIL_ENABLED` | `true` turns email on; the default is `false`. |
| `SENDGRID_API_KEY` | Email goes through SendGrid with this key; when it is empty, through SMTP. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | The SMTP server (port 587 by default). |
| `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME` | The sender of every email. |
| `NOTIFICATION_ADMIN_EMAILS` | A JSON array of addresses. No notification is sent to them. |

`NOTIFICATION_ADMIN_EMAILS` is a JSON array. A plain comma-separated list stops the API
from starting, with `error parsing value for field "NOTIFICATION_ADMIN_EMAILS"`.

---

## Notification Preferences API

Each user can store which events they want to be notified about, a Slack channel of their
own and another email address. The preferences are stored and returned; sending does not
read them, so every event's Slack message goes to `SLACK_DEFAULT_CHANNEL`. Only the test
notification below can name another channel.

Run the commands on this page in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by the
ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

### GET /api/v1/notifications/preferences

Returns the current user's preferences, creating the defaults the first time:

```{.bash exec}
curl -s localhost:8000/api/v1/notifications/preferences \
  -H "Authorization: Bearer $TOKEN" | jq '{notify_experiment_started, notify_rollout_advanced, slack_channel}'
```
<!-- expect: "notify_experiment_started": true -->
<!-- expect: "notify_rollout_advanced": false -->
<!-- expect: "slack_channel": null -->

It prints the defaults: notified when an experiment starts, not when a rollout advances,
and no channel of your own. The full response:

```json
{
  "notify_experiment_started": true,
  "notify_experiment_completed": true,
  "notify_safety_rollback": true,
  "notify_rollout_advanced": false,
  "slack_channel": null,
  "email_override": null,
  "id": "e99a2406-2fb5-455d-8082-47024f676fee",
  "user_id": "2658dd18-4167-4803-9aff-a3ffb1f603ce",
  "created_at": "2026-09-26T22:51:27.531187",
  "updated_at": "2026-09-26T22:51:27.531187"
}
```

---

### PUT /api/v1/notifications/preferences

Changes the fields you send and leaves the others as they are. This turns on rollout
notifications and sends your Slack alerts to `#my-team-alerts`:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/notifications/preferences \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"notify_rollout_advanced": true, "slack_channel": "#my-team-alerts"}' \
  | jq '{notify_rollout_advanced, slack_channel}'
```
<!-- expect: "notify_rollout_advanced": true -->
<!-- expect: "slack_channel": "#my-team-alerts" -->

It prints the two changed values. The fields are `notify_experiment_started`,
`notify_experiment_completed`, `notify_safety_rollback`, `notify_rollout_advanced`,
`slack_channel` and `email_override`; any other field is ignored.

---

### GET /api/v1/notifications/admin/preferences

Lists every user's preferences. Requires the ADMIN role.

---

### GET /api/v1/notifications/delivery-log

Lists the notification delivery log, most recent first. Requires the ADMIN role. No notification is written to it, so it is empty.

**Query Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `page` | int | Page number (default: 1) |
| `limit` | int | Results per page (default: 20, max: 100) |
| `event_type` | string | Filter by event type |
| `status` | string | Filter by delivery status: `sent`, `failed`, `skipped` |

```{.bash exec}
curl -s "localhost:8000/api/v1/notifications/delivery-log?status=failed" \
  -H "Authorization: Bearer $TOKEN" | jq '{total, page, limit}'
```
<!-- expect: "total": 0 -->

It prints `"total": 0`.

---

### POST /api/v1/notifications/test

Sends a test notification, to check a channel's settings. Requires the DEVELOPER or
ADMIN role. `channel` is `slack`, `email` or `webhook`; `recipient` is optional (a Slack
channel or an email address) and `message` defaults to "Test notification from
Controlvar":

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/notifications/test \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"channel": "slack"}' | jq .success
```
<!-- expect: false -->

On the Quick Start stack it prints `false`, because Slack is not configured there.

---

## Permissions

| Action | Minimum Role |
|--------|-------------|
| View own preferences | Any authenticated user |
| Update own preferences | Any authenticated user |
| Send test notification | DEVELOPER |
| View all preferences / delivery log | ADMIN |

---

## Troubleshooting

**Slack messages not arriving**
1. Confirm that `SLACK_ENABLED` is `true` in the API's environment
2. Ensure the bot is invited to `SLACK_DEFAULT_CHANNEL` (`/invite @your-bot`)

**Emails not arriving**
1. Confirm that `EMAIL_ENABLED` is `true`
2. Check the spam folder, and add the `EMAIL_FROM_ADDRESS` domain to the allowlist

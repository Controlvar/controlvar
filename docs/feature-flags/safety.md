# Safety Monitoring

Safety monitoring checks each active feature flag's error rate and latency against thresholds you
set. When a flag breaches one and automatic rollbacks are turned on, it rolls the flag back. Its
endpoints are under `/api/v1/safety`.

---

## What Safety Monitoring Does

The safety monitor runs every 5 minutes by default (`SAFETY_CHECK_INTERVAL_MINUTES`). For every
`ACTIVE` flag, whatever its `rollout_percentage` (a flag at 0% still serves the users its targeting
rules match), it:

1. Loads the flag's safety configuration (or the platform default when the flag has none) and skips the flag
   if monitoring is not `enabled`.
2. Reads the current value of each configured metric over the last 15 minutes: error metrics come from
   `error_logs` (relative to the flag's evaluations in `raw_metrics`), latency metrics from `raw_metrics`.
3. Compares each value with the metric's `critical_threshold` using its `comparison_type`. A breach marks the
   flag unhealthy; a `warning_threshold` breach is only reported.
4. If the flag is unhealthy **and** the global setting `enable_automatic_rollbacks` is on, rolls the flag back
   to the configuration's `rollback_percentage`. A target of `0` (the default) turns the flag off
   (`status: inactive`) and sets its percentage to 0; any other target lowers only the global percentage
   (see [What a rollback changes](#what-a-rollback-changes)). It also pauses the flag's active rollout
   schedules, records a `SafetyRollbackRecord`, and sends the Slack alert and webhook event.

You can run the same check on demand with `GET /api/v1/safety/feature-flags/{flag_id}/check`.

### Which errors count

A flag's error metrics, and so automatic rollback, count two kinds of error: flag evaluations that
fail on the server, and error reports sent to `POST /api/v1/tracking/errors` or `/errors/batch` with
a server-side key, an API key with the `sdk:ruleset` scope whose owner can change feature flags
(ADMIN, DEVELOPER or a superuser). Error reports sent with any other key, such as the key in a
browser or mobile app, are stored and counted separately in the check
(`details.uncounted_error_count`), but do not count toward the error rate or a rollback. The same
goes for an `sdk:ruleset` key whose owner has since lost the role. To have app errors count, send
them to your server and forward them from there with a server-side key; see
[Reporting client-side errors](create.md#reporting-client-side-errors).

---

## Metrics You Can Monitor

| Metric name | Source | Meaning |
|-------------|--------|---------|
| `error_rate` | `error_logs` ÷ flag evaluations | Fraction of evaluations that logged an error that counts (0.0–1.0; see [Which errors count](#which-errors-count)) |
| `error_count` | `error_logs` | Number of errors that count, in the window |
| `total_evaluations` | `raw_metrics` | Number of flag evaluations in the window |
| `latency`, `avg_latency` | `raw_metrics` (`metric_type = latency`) | Average latency in ms |
| `p95_latency` | `raw_metrics` | 95th percentile latency in ms |
| `max_latency`, `min_latency` | `raw_metrics` | Extremes in ms |

Each metric takes a `MetricThreshold`:

| Field | Type | Description |
|-------|------|-------------|
| `critical_threshold` | float | Breaching it makes the flag unhealthy (rollback candidate) |
| `warning_threshold` | float | Reported in the check details; never triggers a rollback |
| `comparison_type` | `"greater_than"` (default), `"less_than"`, `"equal_to"` | How the current value is compared with the thresholds |

Metric names outside this list are accepted but have no data source; they are reported as healthy.

---

## Configuring Per-Flag Safety

Run the commands on the rest of this page in one terminal, in order, against the stack from
the [Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by
the ones before it.

### Log in and create a flag

Changing safety settings takes a superuser, such as the demo administrator:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq '{is_superuser}'
```
<!-- expect: "is_superuser": true -->

It prints `"is_superuser": true`.

The examples watch a flag that is on for half of all users. This creates it and saves its id
in `$FLAG_ID` (see [Creating Feature Flags](create.md)):

```{.bash exec}
FLAG_ID=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "new-checkout-flow", "name": "New Checkout Flow", "rollout_percentage": 50, "is_active": true}' \
  | jq -r .id)

curl -s localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{status, rollout_percentage}'
```
<!-- expect: "status": "active" -->
<!-- expect: "rollout_percentage": 50 -->

It prints `"status": "active"` and `"rollout_percentage": 50`.

### A flag without a configuration

`GET /api/v1/safety/feature-flags/{flag_id}/config` never answers `404` for a flag without a
configuration. It returns a default built from the global settings, with the nil UUID as
`id`. Nothing is written until you `POST`:

```{.bash exec}
curl -s localhost:8000/api/v1/safety/feature-flags/$FLAG_ID/config \
  -H "Authorization: Bearer $TOKEN" | jq '{id, enabled}'
```
<!-- expect: "id": "00000000-0000-0000-0000-000000000000" -->
<!-- expect: "enabled": true -->

```json
{
  "id": "00000000-0000-0000-0000-000000000000",
  "enabled": true
}
```

### Create or Update a Flag's Configuration

Requires a superuser. The call creates the configuration, or replaces the fields you send:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/safety/feature-flags/$FLAG_ID/config \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "enabled": true,
    "metrics": {
      "error_rate":  {"warning_threshold": 0.02, "critical_threshold": 0.05, "comparison_type": "greater_than"},
      "p95_latency": {"warning_threshold": 300,  "critical_threshold": 500,  "comparison_type": "greater_than"}
    },
    "rollback_percentage": 0
  }' | jq -c '.metrics'
```
<!-- expect: "error_rate":{"warning_threshold":0.02,"critical_threshold":0.05,"comparison_type":"greater_than"} -->
<!-- expect: "p95_latency":{"warning_threshold":300.0,"critical_threshold":500.0,"comparison_type":"greater_than"} -->

It prints the stored thresholds:

```json
{"error_rate":{"warning_threshold":0.02,"critical_threshold":0.05,"comparison_type":"greater_than"},"p95_latency":{"warning_threshold":300.0,"critical_threshold":500.0,"comparison_type":"greater_than"}}
```

**Request Fields**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `enabled` | boolean | No | Whether the monitor checks this flag (default `true`) |
| `metrics` | object | No | Map of metric name → threshold (see above). An empty map means nothing is checked |
| `rollback_percentage` | int | No | Target of the automatic rollback, 0–100 (default `0`); any other value answers 422. A value stored before that bound is clamped to 0–100 when the monitor uses it. `0` turns the flag off for every user, including users matched by a targeting rule. A value from 1 to 100 lowers the global rollout, for example `5` to keep a small slice of users on the flag; users matched by a targeting rule keep their rule's percentage (see [What a rollback changes](#what-a-rollback-changes)). Manual rollbacks take the percentage as a query parameter |

A field you leave out keeps its stored value; on the first `POST` it takes its default. No
field accepts `null`: a `null` answers 422 with `"type": "null_not_allowed"` and nothing is
written. A configuration saved with `"metrics": null` by an earlier version reads back with
`"metrics": {}`, so nothing is checked for that flag until you `POST` its thresholds again.

The response (the same shape as `GET .../config`) also carries the configuration's `id`,
`feature_flag_id`, `enabled`, `rollback_percentage`, `created_at` and `updated_at`.

### Example: Checkout Flag with Tight Thresholds

```json
{
  "enabled": true,
  "metrics": {
    "error_rate":  {"critical_threshold": 0.02, "comparison_type": "greater_than"},
    "p95_latency": {"critical_threshold": 300,  "comparison_type": "greater_than"}
  }
}
```

### Example: Non-Critical UI Flag, Alerts Only

Set only `warning_threshold`s (no `critical_threshold`): the check reports warnings but the flag never becomes
unhealthy, so it is never rolled back.

```json
{
  "enabled": true,
  "metrics": {
    "error_rate": {"warning_threshold": 0.10, "comparison_type": "greater_than"}
  }
}
```

---

## Global Safety Settings

Global settings hold the automatic-rollback switch and the default metric thresholds applied to flags that have
no configuration of their own.

### View Global Settings

Any logged-in user can read them:

```{.bash exec}
curl -s localhost:8000/api/v1/safety/settings \
  -H "Authorization: Bearer $TOKEN" | jq '{enable_automatic_rollbacks}'
```
<!-- expect: "enable_automatic_rollbacks": true -->

It prints `"enable_automatic_rollbacks": true`: the Quick Start's demo data turns automatic
rollbacks on. The response also
carries `default_metrics`, `id`, `created_at` and `updated_at`.

On a database where no settings have been stored, the first read creates them with
`enable_automatic_rollbacks: false` and no defaults. **Automatic rollbacks are then off until
a superuser turns them on.**

### Update Global Settings

Requires a superuser:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/safety/settings \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "enable_automatic_rollbacks": true,
    "default_metrics": {
      "error_rate": {"warning_threshold": 0.05, "critical_threshold": 0.10, "comparison_type": "greater_than"}
    }
  }' | jq -c .default_metrics
```
<!-- expect: {"error_rate":{"warning_threshold":0.05,"critical_threshold":0.1,"comparison_type":"greater_than"}} -->

It prints the stored defaults:

```json
{"error_rate":{"warning_threshold":0.05,"critical_threshold":0.1,"comparison_type":"greater_than"}}
```

A flag with a configuration of its own is checked against its own thresholds only; the
defaults apply to a flag that has none.

---

## Running a Check

```{.bash exec}
curl -s localhost:8000/api/v1/safety/feature-flags/$FLAG_ID/check \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{is_healthy, metrics: [.metrics[] | {name, current_value, threshold, is_healthy}]}'
```
<!-- expect: "is_healthy": true -->
<!-- expect: "name": "error_rate" -->

The new flag has no traffic, so every value is `0.0` and the flag is healthy:

```json
{
  "is_healthy": true,
  "metrics": [
    {
      "name": "error_rate",
      "current_value": 0.0,
      "threshold": 0.05,
      "is_healthy": true
    },
    {
      "name": "p95_latency",
      "current_value": 0.0,
      "threshold": 500.0,
      "is_healthy": true
    }
  ]
}
```

Each metric's `details` holds its `warning_threshold`, `critical_threshold`, `comparison_type`,
`warning` (whether the warning threshold is breached) and `measured` (`false` for a metric name
with no data source). The response also carries
`feature_flag_id`, `last_checked` and `details`, with the window's `error_count`,
`total_evaluations` and `uncounted_error_count`: the error reports that were stored but do not
count (see [Which errors count](#which-errors-count)). When a metric breaches its critical
threshold, that metric and the whole response read `"is_healthy": false`.

The dashboard shows the same check. The flag's page has a **Safety check** section that reads
*Healthy* or *Unhealthy* (or *Error rate unknown* when errors were reported but no evaluations
fall in the last 15 minutes), with each metric's value beside its threshold, and a line saying how
many error reports in the window were sent with keys that do not count. **Admin → Safety** (for
superusers) lists the first 100 flags under **Flag Safety Status** as *healthy*, *warning* or
*critical*, with its error rate and how many reports were not counted.

---

## Auto-Rollback

When the monitor finds a flag unhealthy and `enable_automatic_rollbacks` is on, it:

1. Locks the flag row and rolls the flag back to its configured `rollback_percentage` (default `0`, which
   turns the flag off), pausing the flag's active rollout schedules in the same transaction
2. Records a `SafetyRollbackRecord` with trigger type `automatic`, the previous and target
   percentages, and the reason (`trigger_reason`, the same text as the audit entry's `reason`
   below: the record has no columns of its own for the metric value or the threshold)
3. Writes a `safety_rollback` entry to the flag's audit history
   (`GET /api/v1/audit-logs/entity/feature_flag/{flag_id}`), with `system:safety-monitor` as its
   `user_email` and no `user_id`. Its `reason` is the rollback's: the first breaching metric with
   its value and threshold, in the metric's unit and each to four significant figures (an error rate
   as a percentage, so 1 error in 14 evaluations reads `7.143%`; a latency in milliseconds), worded by
   the threshold's `comparison_type`: `above` and `>` for `greater_than`, `below` and `<` for
   `less_than`, `equal to` and `=` for `equal_to`. Such as
   `Automatic rollback due to error_rate above threshold (10% > 5%)`, or, for a `less_than`
   threshold of 100 on `total_evaluations` and a flag evaluated 40 times,
   `Automatic rollback due to total_evaluations below threshold (40 < 100)`. Its `new_value` holds
   `trigger_type` (`automatic`), `previous_percentage`, `new_percentage`, `deactivated` and
   `paused_schedules` (see [Audit Logging](../api/audit-logging.md#changes-the-platform-makes-on-its-own))
4. Sends a Slack alert to `SLACK_DEFAULT_CHANNEL` and a `safety_rollback` event to
   `NOTIFICATION_WEBHOOK_URL`, each when configured. Both name the first breaching metric with its
   measured value and threshold in the metric's unit: an error rate as a percentage (1 error in 14
   evaluations against `0.05` reads `error rate 7.143% above threshold 5%`), a latency in
   milliseconds (`avg latency 523.5 ms above threshold 500 ms`). The line says `above`, `below` or
   `equal to` by the threshold's `comparison_type`, as the reason does. The webhook's `metadata` carries
   the raw numbers as `metric`, `value` and `threshold`, and `error_rate` holds the value only when the
   metric is `error_rate` (otherwise `null`). An automatic rollback sends no email

Investigate the root cause before re-enabling.

### What a rollback changes

A rollback, automatic or manual, does one of two things, depending on its target:

- **A rollback to `0%` turns the flag off.** Its status becomes `inactive` and its global
  `rollout_percentage` becomes 0. Every user, including users matched by a targeting rule, gets
  `enabled: false` with `reason: "inactive"`, exactly as when the flag is turned off by hand (see
  [Creating Feature Flags](create.md#when-a-flag-evaluates-to-off)). The targeting rules are left
  as written.
- **A rollback to 1–100% lowers only the global `rollout_percentage`.** A user who matches no
  targeting rule is bucketed by the new percentage. A user who matches a rule is bucketed by that
  rule's own `rollout_percentage` (100 unless the rule sets one), so the rollback does not change
  whether that user gets the flag.

Either way, the flag's active rollout schedules are paused, so a later stage cannot raise the
percentage again; they stay paused until you resume them.

So the flag's default value is served to every user after a rollback to `0%`, and after a partial
rollback to the users it removes, while users matched by a targeting rule keep their rule's
percentage.

A partial rollback does not help when the harm is only among users matched by a targeting rule,
and a flag that serves only rule-matched users (global `0%`) is never rolled back to a partial
target; use a target of `0` for such a flag.

### What Triggers a Rollback

| Condition | Trigger |
|-----------|---------|
| Any metric's current value breaches its `critical_threshold` | Automatic rollback (first breaching metric is named in the reason) |
| Only `warning_threshold`s are breached | Reported in the check; no rollback |
| `enable_automatic_rollbacks` is off | Check reports unhealthy; no rollback |

---

## Rollback History

Every rollback (automatic or manual) is stored in the `safety_rollback_records` table
(`feature_flag_id`, `safety_config_id`, `trigger_type`, `trigger_reason`, `previous_percentage`,
`target_percentage`, `success`, `executed_by_user_id`, `created_at`). The rollback response includes the
record's id as `rollback_record_id`. No endpoint lists the records, and the dashboard lists only the
manual rollbacks made in the current browser session, so query the table for automatic ones.

---

## Manual Rollback

Requires a superuser. Roll a flag down to a percentage from 0 to 100 (default `0`; any
other value answers 422), with a reason. The
reason is a query parameter, so its spaces are written `%20`:

```{.bash exec}
curl -s -X POST "localhost:8000/api/v1/safety/feature-flags/$FLAG_ID/rollback?percentage=0&reason=Checkout%20errors%20observed%20in%20Datadog" \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{success, message, previous_percentage, new_percentage, deactivated: .details.deactivated}'
```
<!-- expect: "success": true -->
<!-- expect: "message": "Feature flag 'new-checkout-flow' turned off and rolled back from 50% to 0%" -->
<!-- expect: "deactivated": true -->

```json
{
  "success": true,
  "message": "Feature flag 'new-checkout-flow' turned off and rolled back from 50% to 0%",
  "previous_percentage": 50,
  "new_percentage": 0,
  "deactivated": true
}
```

The response also carries `feature_flag_id`, `trigger_type` (`manual`), `rollback_record_id`,
`timestamp` and `details`: the reason, `deactivated`, and `paused_schedules` (the ids of the
rollout schedules the rollback paused). A rollback that would change nothing returns
`success: false`, writes nothing, and says why: the flag is archived, the flag is already off,
or, for a target above 0, its global percentage is already at or below the target.

A manual rollback changes the same thing as an automatic one (see
[What a rollback changes](#what-a-rollback-changes)). This one was to `0%`, so the flag is now off:

```{.bash exec}
curl -s localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{status, rollout_percentage}'
```
<!-- expect: "status": "inactive" -->
<!-- expect: "rollout_percentage": 0 -->

It prints `"status": "inactive"` and `"rollout_percentage": 0`.

---

## Re-Enabling After Rollback

After investigating and resolving the root cause, bring the flag back in this order.

The percentage the flag had before the rollback is the rollback response's `previous_percentage`
(50 above). For an automatic rollback, read it from the flag's latest `safety_rollback` audit
entry (`GET /api/v1/audit-logs/entity/feature_flag/{flag_id}`, for superusers, ADMIN and
ANALYST): that entry's `new_value` is a JSON string holding `previous_percentage`. The flag's
latest row in the `safety_rollback_records` table holds it too.

**After a rollback to `0%`, turn the flag on first.** It comes back at 0%, so at first only users
matched by a targeting rule are served:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/feature-flags/$FLAG_ID/activate \
  -H "Authorization: Bearer $TOKEN" | jq '{status, rollout_percentage}'
```
<!-- expect: "status": "active" -->
<!-- expect: "rollout_percentage": 0 -->

It prints `"status": "active"` and `"rollout_percentage": 0`.

**Then raise the rollout**, back to `previous_percentage` or, more cautiously, below it. After a
partial rollback (1–100%) the flag is still on, so this is where you start:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"rollout_percentage": 5}' | jq '{status, rollout_percentage}'
```
<!-- expect: "status": "active" -->
<!-- expect: "rollout_percentage": 5 -->

It prints `"status": "active"` and `"rollout_percentage": 5`.

**Finally, resume the rollout schedule.** The rollback paused the flag's active schedule, and it
stays paused until you resume it explicitly with `POST /api/v1/rollout-schedules/{schedule_id}/activate`
(see [Rollout Schedules](rollouts.md#pausing-a-schedule)). The ids are in the rollback response's
`details.paused_schedules`.

**Check the flag's thresholds.** A rollback leaves the flag with a safety configuration of its
own: a flag that had none gets one with no thresholds, so from then on it is not checked against
the global defaults. If it should stay monitored, `POST` its thresholds (see
[Create or Update a Flag's Configuration](#create-or-update-a-flags-configuration)).

Watch the safety check for at least one full 15-minute window before expanding further. With
automatic rollbacks on, the monitor rolls the flag back again on its next pass while one of its
critical thresholds is still breached.

---

## Best Practices

### Set thresholds relative to baseline

Measure your baseline error rate and latency before enabling safety monitoring. If your API normally returns
2% errors, a 1% threshold causes false positives. A reasonable critical threshold is **2× your baseline**:
baseline error rate 2% → `critical_threshold: 0.04`; baseline p95 latency 200 ms → `critical_threshold: 400`.

### Use a warning threshold as an early signal

Pair a lower `warning_threshold` with the critical one; warnings show up in the check details and in the
dashboard without touching traffic.

### Use tight thresholds for critical paths

For features that touch payments, authentication or core user flows: `error_rate` critical `0.02`,
`p95_latency` critical `300`.

### Combine with gradual rollouts

Safety monitoring is most effective with gradual rollouts. A problem affecting 5% of users is much easier to
contain than one affecting 100%. See [Rollout Schedules](rollouts.md).

### Flags with large targeting rules

For a flag with large targeting rules, either set no threshold on `latency`, `avg_latency`,
`p95_latency` or `max_latency`, or leave automatic rollback off. A flag without a safety
configuration of its own uses the global default thresholds, so to take a latency threshold off one
flag, give that flag its own configuration without one.

### Test the rollback path

Before a major launch, trigger a manual rollback in staging to confirm that the flag turns off as
expected. A manual rollback sends no Slack alert or webhook event; only an automatic rollback does.

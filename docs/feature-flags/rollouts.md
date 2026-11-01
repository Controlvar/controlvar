# Gradual Rollouts

A gradual rollout raises a feature flag's rollout percentage over time, in controlled stages.
Instead of flipping a flag from 0% to 100% in one step, you roll out to 10%, watch for
problems, expand to 50%, confirm everything is healthy, then go to 100%.

---

## What a Rollout Schedule Is

A rollout schedule is a plan attached to a feature flag. It defines:

- a sequence of **stages**, each with a target rollout percentage;
- a **trigger type** for each stage: time-based (the scheduler starts it at its
  `start_date`, once the stages before it are complete) or manual (it starts only when you
  advance it);
- optional start and end dates for the whole schedule.

The platform's background scheduler processes active schedules every 15 minutes
(`ROLLOUT_CHECK_INTERVAL_MINUTES`) and starts a time-based stage once its `start_date` has
passed and every stage before it is complete. Once the schedule's `end_date` has passed, the
scheduler passes the schedule over: no stage starts or completes by itself, and the schedule
stays `active`.

---

## In the Dashboard

A flag's page has a **Rollout schedule** section. ADMIN and DEVELOPER create and run a
schedule there; ANALYST and VIEWER see the schedule and its stages, and change nothing.

- **Create a rollout schedule** is offered when the flag has no schedule that is a draft,
  active or paused. It takes a name and the stages, in order: each has a name, a target
  percentage and how it starts, when you advance it (manual) or at a set time (time-based).
- A time-based stage's start time is optional. You pick it in your browser's time zone, the
  form shows it back in UTC, and it is sent in UTC.
- Before it sends anything, the form checks the whole list of stages: each has a name, each
  target is a whole number from 0 to 100, and no target is below the one before it.
- A new schedule is a draft: nothing changes for users until you activate it.
- **Activate** starts a draft, **Pause** stops an active schedule and **Resume** starts it
  again, and **Cancel schedule** ends it for good. The page asks before each of these but
  Pause.
- While the schedule is a draft or paused, **Edit stages** changes, adds or removes its
  pending stages. A stage that has started cannot be changed there.
- On an active schedule, **Start stage** starts the first pending stage when it is manual and
  no stage is in progress, and **Complete stage** completes the manual stage in progress and
  starts the next one at once (see [Manual Advancement](#manual-advancement)). No other stage
  is offered.
- A schedule made in the dashboard has no start or end date of its own, a maximum of 100% and
  no minimum stage duration. Those are set through the API only.

---

## Creating a Rollout Schedule via API

Run these in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each step uses the shell variables set by
the ones before it.

**URLs.** The collection URL ends with a slash (`/api/v1/rollout-schedules/`). Without it
the API answers `307 Temporary Redirect`, which `curl` doesn't follow, so nothing happens
and nothing is printed.

### Step 1: Log in

Changing a flag's rollout takes a user with the ADMIN or DEVELOPER role:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

### Step 2: Create the flag

A schedule belongs to a flag. This creates one, switched on at 0%, and saves its id in
`$FLAG_ID` (see [Creating Feature Flags](create.md) for the details):

```{.bash exec}
FLAG=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "new-checkout-flow", "name": "New Checkout Flow", "rollout_percentage": 0, "is_active": true}')
FLAG_ID=$(jq -r .id <<<"$FLAG")

jq '{key, status, rollout_percentage}' <<<"$FLAG"
```
<!-- expect: "status": "active" -->
<!-- expect: "rollout_percentage": 0 -->

It prints `"status": "active"` and `"rollout_percentage": 0`.

### Step 3: Create the rollout schedule

This schedule has three stages:

1. **Initial Canary**, 10%, manual: you start it when you're ready.
2. **Expanded Rollout**, 50%, time-based, with a `start_date` a week from now. It comes after
   a manual stage, so it does not start by itself (see [Trigger Types](#trigger-types)).
3. **Full Rollout**, 100%, manual: a human approval gate before everyone gets the feature.

Dates are UTC, in ISO 8601 form. The first two lines work them out from today with `jq`, so
the example always uses dates in the future. In your own schedule you can write them out
(`2027-01-08T00:00:00Z`). The command saves the schedule's id in `$SCHEDULE_ID`, and the
first stage's in `$CANARY_ID`:

```{.bash exec}
WEEK1=$(jq -nr 'now + 7*86400 | todate')
WEEK4=$(jq -nr 'now + 28*86400 | todate')

SCHEDULE=$(curl -s -X POST localhost:8000/api/v1/rollout-schedules/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  --data @- <<EOF
{
  "name": "New Checkout Flow Gradual Rollout",
  "description": "Roll out the new checkout to all users over 4 weeks",
  "feature_flag_id": "$FLAG_ID",
  "end_date": "$WEEK4",
  "max_percentage": 100,
  "min_stage_duration": 24,
  "stages": [
    {
      "name": "Initial Canary",
      "description": "First 10%: watch error rates",
      "stage_order": 1,
      "target_percentage": 10,
      "trigger_type": "manual"
    },
    {
      "name": "Expanded Rollout",
      "description": "Expand to 50%: confirm stability",
      "stage_order": 2,
      "target_percentage": 50,
      "trigger_type": "time_based",
      "start_date": "$WEEK1"
    },
    {
      "name": "Full Rollout",
      "description": "100%: manual gate for final approval",
      "stage_order": 3,
      "target_percentage": 100,
      "trigger_type": "manual"
    }
  ]
}
EOF
)
SCHEDULE_ID=$(jq -r .id <<<"$SCHEDULE")
CANARY_ID=$(jq -r '.stages[] | select(.stage_order == 1) | .id' <<<"$SCHEDULE")

jq -c '{status, stages: [.stages[] | {stage_order, target_percentage, trigger_type, status}]}' <<<"$SCHEDULE"
```
<!-- expect: "status":"draft" -->
<!-- expect: "trigger_type":"manual","status":"pending" -->

The API answers `201 Created`. A new schedule is a `draft`, and every stage is `pending`:

```json
{"status":"draft","stages":[{"stage_order":1,"target_percentage":10,"trigger_type":"manual","status":"pending"},{"stage_order":2,"target_percentage":50,"trigger_type":"time_based","status":"pending"},{"stage_order":3,"target_percentage":100,"trigger_type":"manual","status":"pending"}]}
```

The response also carries the schedule's `id`, `feature_flag_id`, `owner_id`, dates and
settings, and each stage's `id`. A schedule is refused with `422` when its target
percentages decrease, when two stages have the same stage order or the orders leave a
gap, or when a stage order is above 1000. The orders need not start at 1: 5, 6, 7 is
accepted.

### Step 4: Activate the schedule

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/rollout-schedules/$SCHEDULE_ID/activate \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "active" -->

It prints `"active"`. From now until the schedule's `end_date`, the scheduler starts a
time-based stage once its `start_date` has passed and every stage before it is complete.
Activating a schedule doesn't change the flag: it is still at 0%.

---

## Rollout Stages

### Target Percentage

Each stage has a `target_percentage`: the rollout percentage the flag is set to when that
stage starts. Percentages must not decrease from one stage to the next.

- Valid: 10 → 50 → 100.
- Invalid: 10 → 50 → 25.

### Trigger Types

| Trigger Type | Description |
|--------------|-------------|
| `time_based` | Before the schedule's `end_date`, the stage starts by itself once its `start_date` has passed and every stage before it is complete; with no `start_date`, on the scheduler's first pass after that. The scheduler completes it no sooner than 24 hours after it starts, or the hours its `trigger_configuration` gives as `duration`. |
| `manual` | The stage starts only when you advance it. |
| `metric_based` | Accepted, but not implemented in this release: such a stage never starts by itself. |

The scheduler never completes a manual stage, so **a time-based stage after a manual stage
never starts by itself**. Advance the manual stage a second time to move on (see
[Manual Advancement](#manual-advancement)).

### Stage Lifecycle

```text
pending → in_progress → completed
```

When a stage starts, it becomes `in_progress` and the flag's rollout percentage is set to
the stage's target. When the scheduler starts the next stage, the previous one becomes
`completed`.

---

## Time-Based Triggers

For `time_based` stages, set a `start_date` in UTC, in ISO 8601 form:

```json
{
  "trigger_type": "time_based",
  "start_date": "2027-01-08T00:00:00Z"
}
```

The scheduler checks every 15 minutes, so a stage set for 09:00 UTC may not start until
09:15. Plan your schedule with this buffer in mind.

All dates are in UTC. If your business runs on a specific timezone, convert before setting
dates.

---

## Manual Advancement

Advance a manual stage to start it. This starts the canary:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/rollout-schedules/stages/$CANARY_ID/advance \
  -H "Authorization: Bearer $TOKEN" | jq '{name, status}'

curl -s localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" | jq .rollout_percentage
```
<!-- expect: "status": "in_progress" -->
<!-- expect: 10 -->

The stage is `in_progress`, and the flag is now at `10`%.

Advancing a manual stage that is in progress completes it and starts the next stage at once,
whatever that stage's trigger and `start_date`. The advance takes effect at once, and the
schedule must be active. Only a manual stage can be advanced; advancing a time-based one
answers `400`. A manual advance is not held back by the earlier stages or by
`min_stage_duration`: advancing the 100% stage first would put the flag at 100% at once.
`min_stage_duration` (in hours) applies only to the stages the scheduler starts.

Manual stages are recommended for the final rollout to 100%: they serve as a human approval
gate before full deployment.

---

## Pausing a Schedule

If you observe problems during a rollout, pause the schedule to stop the automatic stages:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/rollout-schedules/$SCHEDULE_ID/pause \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "paused" -->

It prints `"paused"`. Pausing does not change the flag's current rollout percentage; it only
stops future stage transitions. A [safety rollback](safety.md#what-a-rollback-changes) pauses
the flag's active schedule the same way, and it stays paused until you resume it. When you have investigated and resolved the issue, resume the
schedule by activating it again:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/rollout-schedules/$SCHEDULE_ID/activate \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "active" -->

It prints `"active"`.

---

## Monitoring Progress

### View Schedule Status

```{.bash exec}
curl -s localhost:8000/api/v1/rollout-schedules/$SCHEDULE_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '{status, stages: [.stages[] | {name, target_percentage, status}]}'
```
<!-- expect: "name":"Initial Canary","target_percentage":10,"status":"in_progress" -->

```json
{"status":"active","stages":[{"name":"Initial Canary","target_percentage":10,"status":"in_progress"},{"name":"Expanded Rollout","target_percentage":50,"status":"pending"},{"name":"Full Rollout","target_percentage":100,"status":"pending"}]}
```

In the dashboard, the flag's page shows the schedule under **Rollout schedule**: its
status, its name, and each stage with its trigger, target percentage and status. The
**Rollout percentage** below it is the flag's percentage now.

To list every schedule for a flag, filter the collection by `feature_flag_id`:

```{.bash exec}
curl -s "localhost:8000/api/v1/rollout-schedules/?feature_flag_id=$FLAG_ID" \
  -H "Authorization: Bearer $TOKEN" | jq .total
```
<!-- expect: 1 -->

It prints `1`. The response is `{"items": [...], "total", "skip", "limit"}`.

---

## Cancelling a Schedule

To stop a rollout entirely:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/rollout-schedules/$SCHEDULE_ID/cancel \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "cancelled" -->

It prints `"cancelled"`. The flag's current rollout percentage is not changed. To roll back,
change the flag itself (see [Creating Feature Flags](create.md)), or use a
[safety rollback](safety.md#manual-rollback): a rollback to 0% turns the flag off and pauses
its schedule. For a string, number or json flag, every user then gets its off value.

---

## Common Gotchas

- Rollout percentages must not decrease across stages.
- You cannot delete stages from an active schedule: cancel the schedule first.
- Stage orders must be sequential (1, 2, 3, with no gaps), and no higher than 1000.
- Time-based `start_date` values are in UTC. The dashboard's picker takes your time zone and
  sends UTC.
- Manual stages must be advanced explicitly: they never start by themselves, even after the
  schedule's `end_date`.
- A manual advance takes effect at once, whatever the earlier stages and
  `min_stage_duration` say.

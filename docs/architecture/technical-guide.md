# Technical Guide

Implementation reference for Controlvar: authentication, targeting rules, assignment,
results, the background schedulers and caching. The components, and where each request
goes, are in the [Architecture Overview](../getting-started/architecture.md), and the
data models in [Data Models](models.md).

---

## Authentication System

### Two Authentication Methods

**1. Bearer Token (JWT)**
- Used by human users in the dashboard
- Obtained via `POST /api/v1/auth/login`
- Validated by the API against its own accounts (`AUTH_PROVIDER=local`, the default); the other provider is described in [AWS Cognito Integration](../cognito_integration.md)
- Expires per `ACCESS_TOKEN_EXPIRE_MINUTES`

Get a token. The login takes the account's `email`, not a username; on the Quick Start
stack the administrator is `admin@demo.com`:

```{.bash exec}
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' \
  -H "Content-Type: application/json" | jq -r '.access_token')
```

Use the token. The experiments collection's URL ends with a slash: without it the API
answers `307 Temporary Redirect`, which `curl` does not follow, so nothing is printed.

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/experiments/ | jq -r '.items[].name'
```
<!-- expect: Checkout Button Color -->

On the Quick Start stack it prints the names of its three demo experiments, among them
`Checkout Button Color`. If it prints an error from `jq` instead, the login failed and
`$TOKEN` holds no token.

**2. API Key**
- Used by SDKs and server-to-server tracking calls
- Static key, stored hashed in DB
- Passed via `X-API-Key` header
- Required for all `/api/v1/tracking/*` endpoints

Any signed-in user, whatever their role, can create a key, and an API key writes tracking
data. This creates one with the token above (see [API Keys](../security/api-keys.md)); the
full key is shown only in this response:

```{.bash exec}
KEY=$(curl -s -X POST http://localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Server-side tracking"}' | jq -r .key)

printf '%s\n' "${KEY:0:5}"
```
<!-- expect: eptk_ -->

It prints `eptk_`, the start of every key. Then assign a user to the Quick Start stack's
active `checkout_button_color` experiment with it. The body is JSON, so the request says so
with `Content-Type`:

```{.bash exec}
curl -s -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/v1/tracking/assign \
  -d '{"user_id":"u1","experiment_key":"checkout_button_color","context":{}}' | jq '{variant_name, assigned}'
```
<!-- expect: "variant_name": "green_button" -->
<!-- expect: "assigned": true -->

User `u1` is assigned the `green_button` variant (`"assigned": true`), and keeps it on
every later call.

### Role-Based Access Control (RBAC)

Each role's permissions are listed in [RBAC](../api/rbac.md).

Experiments and feature flags: what a user can do depends on their role; the owner is
recorded but does not decide access.

Permission checks in code (both return a bool; the caller raises the 403):

```python
from backend.app.core.permissions import (
    Action,
    ResourceType,
    can_act_on_feature_flag,
    check_permission,
)

# a role's permission on a resource type
if not check_permission(current_user, ResourceType.EXPERIMENT, Action.UPDATE):
    raise HTTPException(status_code=403, detail="Not enough permissions")

# a particular feature flag
if not can_act_on_feature_flag(current_user, flag.owner_id, Action.DELETE):
    raise HTTPException(status_code=403, detail="Not enough permissions")
```

---

## Rules Engine

The rules engine evaluates targeting rules to determine if a user should be included in an experiment or feature flag.

### Rule Structure

Rules are stored in the shape the dashboard's rule builder writes: a top-level
`logical_operator` combining groups, each group combining its conditions. An
experiment's `targeting_rules` must use this shape; any other is refused with
`422` on create and update.

```json
{
  "logical_operator": "AND",
  "groups": [
    {
      "logical_operator": "AND",
      "conditions": [
        {"attribute": "country", "operator": "in", "value": ["US", "CA", "GB"]},
        {"attribute": "user_age", "operator": "greater_than", "value": 18},
        {"attribute": "plan", "operator": "equals", "value": "premium"}
      ]
    }
  ]
}
```

The operators a stored rule can use are listed under "Targeting Rules" in the
[API endpoints reference](../api/endpoints.md).

---

## Assignment Algorithm

When a user calls `/api/v1/tracking/assign`:

1. **Stored assignment**: a user who already has one gets it back, unchanged
2. **Eligibility** (new users only): the global holdout, then the experiment's mutual exclusion
   group, then its targeting rules. An ineligible user gets the control variant with
   `assigned: false`, and no assignment is stored
3. **Traffic split**: hash the user with the experiment's `key` (the first four bytes of
   `MD5("{user_id}:{key}")`, little-endian, divided by 2^32: the hash every SDK implements,
   `backend/app/core/consistent_hash.py`) → bucket 0-99
4. **Variant selection**: the bucket picks a variant by the cumulative traffic allocations. A
   bandit experiment routes a new user by its current weights instead
5. **Persistence**: save the assignment to the `assignments` table

```text
User "user-123" + Experiment key "homepage-test"
     ↓
Hash → bucket 84
     ↓
Traffic allocation: Control 0-49 (50%), Treatment 50-99 (50%)
     ↓
bucket 84 → Treatment variant
```

This means:
- Assignment is **sticky**: the stored row is returned on every later call, even after the
  traffic allocation changes
- Consistent across devices (same user_id = same variant)
- No session dependency

---

## Results Engine

The results engine computes:

### Statistical Methods

**Conversion metrics (Fisher's exact test):**
- Every metric in the results is analysed as a conversion today
- For each treatment against the control: a 2×2 table of users who converted and users who did
  not, from the users assigned to each variant
- Two-sided Fisher's exact test (`scipy.stats.fisher_exact`); the response's
  `statistical_test_used` is `fisher_exact`
- Effect size: Cohen's h

**Mean metrics (Welch's t-test):**
- Used by warehouse analysis (beta, the `warehouse` module) for a metric that is a mean
- Welch's t-test for unequal variances; `statistical_test_used` is `welch_t_test`
- Effect size: Cohen's d

**Multiple comparisons correction:**
- Each experiment stores a correction method and a confidence level:
  Benjamini-Hochberg (FDR) at 0.95 by default, Bonferroni or none on request.
  The results, the sample-size plan, the export and the report use them; a
  results request may name another for itself. Locked once the experiment
  leaves draft.
- CUPED follows the stored level and correction; sequential testing does not
  follow the stored level.

### REST Endpoints

```text
GET  /api/v1/results/{id}             # Full results with p-values, CIs, effect sizes
GET  /api/v1/results/{id}/daily       # Daily time-series per variant
GET  /api/v1/results/{id}/sample-size # Sample size adequacy + power
POST /api/v1/results/{id}/invalidate-cache
```

### Sample Size

How a sample size is computed, and the defaults, are in
[Power Analysis](../statistics/power-analysis.md).

---

## Background Schedulers

Five background jobs start with the API process (`lifespan` in `backend/app/main.py`):

| Scheduler | Default cycle | Responsibility |
|-----------|---------------|----------------|
| Experiment scheduler | `EXPERIMENT_SCHEDULER_INTERVAL_MINUTES` (15) | Starts draft experiments at `start_date`, resumes paused ones at a scheduled `resume_at`, stops experiments at `end_date` |
| Rollout scheduler | `ROLLOUT_CHECK_INTERVAL_MINUTES` (15) | Advances feature-flag rollout stages |
| Metrics scheduler | 15 min | Aggregates raw metrics |
| Safety monitor | `SAFETY_CHECK_INTERVAL_MINUTES` (5) | Checks each active feature flag against its safety thresholds; rolls one back when automatic rollbacks are on |
| Bandit scheduler | `BANDIT_UPDATE_INTERVAL_MINUTES` (5) | Recomputes multi-armed bandit weights (DynamoDB counters when their pulls and successes are at least PostgreSQL's, else PostgreSQL) |

### Experiment Scheduler (every `EXPERIMENT_SCHEDULER_INTERVAL_MINUTES`, default 15)

Transitions experiments based on `start_date`, `resume_at` and `end_date`:
- `DRAFT` + `start_date ≤ now` → `ACTIVE`
- `PAUSED` + `resume_at ≤ now` → `ACTIVE` (the notification says "resumed automatically")
- `ACTIVE` + `end_date ≤ now` → `COMPLETED`

A paused experiment's `start_date` is when it first started and never resumes it: a
`PAUSED` experiment with no `resume_at` stays paused, across ticks and restarts, until
someone starts it. `PUT /schedule` on a `PAUSED` experiment stores its `start_date` as
`resume_at` and leaves the experiment's own `start_date` alone. Any change of status clears
`resume_at` (an ORM listener on `Experiment.status`), and a database constraint
(`ck_experiments_resume_only_when_paused`) refuses a `resume_at` on any status but `PAUSED`.

```python
# API: schedule an experiment
PUT /api/v1/experiments/{id}/schedule
{
  "start_date": "2026-04-01T00:00:00Z",
  "end_date": "2026-04-15T23:59:59Z"
}
```

### Rollout Scheduler (every `ROLLOUT_CHECK_INTERVAL_MINUTES`, default 15)

Advances feature flag rollout stages:
- Activates `TIME_BASED` stages once their `start_date` has passed (a stage never starts early, even
  when the previous stage has completed)
- Completes an `IN_PROGRESS` stage after its `trigger_configuration.duration` hours (default 24)
- Updates `rollout_percentage` on FeatureFlag
- Transitions stage status: `PENDING → IN_PROGRESS → COMPLETED`

### Safety Monitor (every `SAFETY_CHECK_INTERVAL_MINUTES`, default 5)

Checks every active feature flag against its thresholds: those of its own safety configuration
or, when it has none, the global defaults. A flag whose configuration has `enabled: false` is
skipped. For each other flag it:
- reads error metrics from `error_logs` and latency metrics from `raw_metrics`, over the last 15
  minutes;
- compares each configured metric against its thresholds;
- when automatic rollbacks are on and a critical threshold is breached, rolls the flag back to
  its `rollback_percentage` and records a `SafetyRollbackRecord`.

How to configure it, which errors count and what a rollback changes are in
[Safety Monitoring](../feature-flags/safety.md).

### Bandit Scheduler (every `BANDIT_UPDATE_INTERVAL_MINUTES`)

Recomputes variant weights for multi-armed bandit experiments and persists them in `bandit_state`;
`POST /api/v1/tracking/assign` routes new users by those weights. See
[Multi-Armed Bandit](../api/multi-armed-bandit.md).

---

## Caching Architecture

What the API keeps in Redis is listed under "Redis (ElastiCache)" in the
[Architecture Overview](../getting-started/architecture.md).

### Cache Invalidation

Invalidate results cache for an experiment:

```text
POST /api/v1/results/{id}/invalidate-cache
```

Feature flags need no invalidation: the API reads the flag list, a flag's
detail and its evaluation from the database on every request.

---

## The Database and its Migrations

The schema, the models and the constraints the database enforces are in
[Data Models](models.md). A new migration is written as
[Creating New Migrations](../development/database/migrations.md#creating-new-migrations)
says.

---

## API Conventions

Each route's request, response and status codes are in the API reference at
`/api/v1/docs`.

---

## Use Cases

### Targeting High-Value Users

**Scenario:** Only run an experiment for users on a paid plan, in the US/EU, who have been active in the last 30 days.

An experiment's groups do not nest, so "and either of these two" is written as two
groups joined by `OR`, each carrying the shared conditions:

```json
{
  "targeting_rules": {
    "logical_operator": "OR",
    "groups": [
      {
        "logical_operator": "AND",
        "conditions": [
          {"attribute": "plan", "operator": "in", "value": ["pro", "enterprise"]},
          {"attribute": "country", "operator": "in", "value": ["US", "GB", "DE", "FR"]},
          {"attribute": "days_since_last_login", "operator": "less_than", "value": 30},
          {"attribute": "lifetime_value", "operator": "greater_than", "value": 100}
        ]
      },
      {
        "logical_operator": "AND",
        "conditions": [
          {"attribute": "plan", "operator": "in", "value": ["pro", "enterprise"]},
          {"attribute": "country", "operator": "in", "value": ["US", "GB", "DE", "FR"]},
          {"attribute": "days_since_last_login", "operator": "less_than", "value": 30},
          {"attribute": "subscription_months", "operator": "greater_than", "value": 6}
        ]
      }
    ]
  }
}
```

Pass the user's attributes as the `context` of `POST /api/v1/tracking/assign`:

```json
{
  "user_id": "user-123",
  "experiment_key": "premium-feature-test",
  "context": {
    "plan": "pro",
    "country": "US",
    "days_since_last_login": 3,
    "lifetime_value": 250,
    "subscription_months": 8
  }
}
```

---

## Performance Characteristics

What decides throughput today:

- Each API container runs one uvicorn worker process (`WEB_CONCURRENCY`, which defaults to `1`
  in the image, both compose files and the Helm chart; the AWS task definition does not set it).
- In that process, most SDK routes (a single assignment, event tracking and flag evaluation)
  do their database work one request at a time
  ([#823](https://github.com/Controlvar/controlvar/issues/823)).
- So throughput grows with the number of API tasks or replicas, not with the number of
  requests sent at once to one of them: on AWS the service's task count (its floor is
  `API_DESIRED_COUNT` in `infrastructure/cdk/app.py`), with the Helm chart `api.replicaCount`.

---

## Infrastructure (AWS)

What the AWS stacks create, and how a request reaches the API and the dashboard, is in
[Architecture on AWS](overview.md).

---

## Monitoring & Observability

### Metrics

The metrics the API exports, and the CloudWatch dashboards and alarms, are in
[Monitoring and Observability](../self-hosting/monitoring.md).

### Log Structure

The fields on each log line are in [Logging Standards](../logging_standards.md).

### Dashboards

- Scheduler health: http://localhost:8000/api/v1/docs#/Scheduler%20Health
- CloudWatch: the API's log group, `/ecs/experimentation-backend-<env>`
- Health: http://localhost:8000/health

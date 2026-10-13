# Technical Guide

Detailed implementation reference for Experimently. Covers architecture, data models, key subsystems, and real-world use cases.

---

## System Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                        Client Layer                             │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ Next.js UI  │  │ JavaScript   │  │ Python SDK /         │  │
│  │ (Port 3000) │  │ SDK          │  │ Direct API calls     │  │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬───────────┘  │
└─────────│────────────────│─────────────────────│───────────────┘
          │                │                     │
          ▼                ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Application (Port 8000)            │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Middleware: Auth, CORS, Rate Limiting, Request Logging  │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ /api/v1/   │  │ /api/v1/     │  │ /api/v1/              │  │
│  │ experiments│  │ feature-flags│  │ tracking / results    │  │
│  └──────┬─────┘  └──────┬───────┘  └───────────┬───────────┘  │
│         │               │                       │               │
│  ┌──────▼───────────────▼───────────────────────▼───────────┐  │
│  │                   Services Layer                          │  │
│  │  ExperimentService  FeatureFlagService  AssignmentService │  │
│  │  EventService       ResultsEngine       RulesEngine       │  │
│  └──────────────────────────────┬────────────────────────────┘  │
└─────────────────────────────────│───────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
            ┌──────────────┐          ┌──────────────────┐
            │ PostgreSQL   │          │ Redis (Cache)    │
            │ (Aurora)     │          │ (ElastiCache)    │
            └──────────────┘          └──────────────────┘
```

---

## Data Models

### Core Entities

```text
Experiment
├── id (UUID PK)
├── key (unique string) ← used in tracking API
├── name, description, hypothesis
├── status: draft | active | paused | completed | archived
├── experiment_type: a_b | mv | split_url | bandit
├── owner_id → User
├── start_date, end_date (for scheduling)
├── targeting_rules (JSON)
├── traffic_allocation (0-100)
│
├── variants → [Variant]
│   ├── id, name
│   ├── is_control (bool)
│   ├── traffic_allocation (0-100, must sum to ≤100)
│   └── configuration (JSON — variant-specific config)
│
├── metrics → [Metric]
│   ├── id, name
│   ├── event_name ← matches Event.event_name
│   ├── metric_type: CONVERSION | REVENUE | COUNT | DURATION
│   └── is_primary (bool)
│
└── assignments → [Assignment]
    ├── user_id (string — your app's user identifier)
    ├── variant_id → Variant
    └── created_at

Event
├── id (UUID PK)
├── user_id (string)
├── event_type (string)
├── event_name (string) ← matched to Metric.event_name
├── experiment_id → Experiment (nullable)
├── feature_flag_id → FeatureFlag (nullable)
├── variant_id → Variant (nullable)
├── value (float — revenue, count, duration)
└── properties (JSON — arbitrary metadata)

FeatureFlag
├── id (UUID PK)
├── key (unique string) ← used in evaluation API
├── name, description
├── status: ACTIVE | INACTIVE | ARCHIVED
├── owner_id → User
├── rollout_percentage (0-100)
├── targeting_rules (JSON)
└── rollout_schedules → [RolloutSchedule]
    ├── stages → [RolloutStage]
    │   ├── stage_order (int)
    │   ├── target_percentage (int)
    │   ├── trigger_type: TIME_BASED | METRIC_BASED | MANUAL
    │   └── start_date

User
├── id (UUID PK)
├── username, email, full_name
├── role: ADMIN | DEVELOPER | ANALYST | VIEWER
├── is_superuser (bool)
└── is_active (bool)
```

---

## Authentication System

### Two Authentication Methods

**1. Bearer Token (JWT)**
- Used by human users in the dashboard
- Obtained via `POST /api/v1/auth/login`
- Validated against AWS Cognito (production) or local DB (development)
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

| Role | Create | Update | Delete | View |
|------|--------|--------|--------|------|
| ADMIN | All | All | All | All |
| DEVELOPER | Experiments, Flags | Any experiment; any flag | Any DRAFT experiment; any flag | All |
| ANALYST | None | None | None | All |
| VIEWER | None | None | None | Approved experiments; all flags |

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

The table below lists the engine's own operator names. In stored rules they are
written in lower case as the dashboard names them (`in`, `greater_than`,
`semver_gte`, ...); the full list is under "Targeting Rules" in the
[API endpoints reference](../api/endpoints.md).

### Supported Operators (20+)

| Category | Operators |
|----------|-----------|
| Equality | `EQUALS`, `NOT_EQUALS` |
| Comparison | `GREATER_THAN`, `LESS_THAN`, `GREATER_THAN_OR_EQUAL`, `LESS_THAN_OR_EQUAL` |
| String | `CONTAINS`, `NOT_CONTAINS`, `STARTS_WITH`, `ENDS_WITH`, `REGEX` |
| Array | `IN`, `NOT_IN`, `CONTAINS_ANY`, `CONTAINS_ALL` |
| Existence | `EXISTS`, `NOT_EXISTS` |
| Versioning | `SEMVER_GT`, `SEMVER_LT`, `SEMVER_EQ` |
| Geo | `GEO_DISTANCE` |
| Time | `TIME_IN_WINDOW` |
| JSON | `JSON_PATH` |
| Logic | `AND`, `OR`, `NOT` |

### Performance

- Compilation cache: LRU cache, 10,000 entries
- Evaluation cache: thread-safe LRU with TTL (default: 300s)
- Throughput: 125,000+ simple evaluations/second
- Batch evaluation: 1,000 users in <5 seconds

```python
from backend.app.services.rules_evaluation_service import RulesEvaluationService

service = RulesEvaluationService(
    cache_size=1000,
    cache_ttl_seconds=300,
    metrics_window=1000,
)

# Single evaluation
result = await service.evaluate_rules_cached(
    rules=targeting_rules,
    context={"country": "US", "user_age": 25, "plan": "premium"},
)

# Batch evaluation
results = await service.batch_evaluate(
    users=[{"user_id": "u1", "country": "US"}, ...],
    rules=targeting_rules,
)
```

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

The analytics results engine (`backend/app/services/analysis_service.py`) computes:

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

### Sample Size Calculation

Required sample size per variant:

```text
n = 2 * ((z_α/2 + z_β)² * p(1-p)) / δ²

Where:
  z_α/2 = z-score for confidence level (1.96 for 95%)
  z_β   = z-score for power (0.84 for 80%)
  p     = baseline conversion rate
  δ     = minimum detectable effect (MDE)
```

Default: 95% confidence, 80% power, 5% MDE.

---

## Lambda Functions

**No stack deploys the code under `backend/lambda/`.** It holds two functions and the
package they share, each with its own tests, which CI runs:

- `backend/lambda/event_processor/`: event-stream processing code (validation,
  enrichment, aggregation, S3 archiving). No stack deploys it or connects a stream to it.
- `backend/lambda/feature_flag_evaluation/`: flag-evaluation code with a local cache. No
  stack deploys it; SDKs evaluate flags through the API
  (`GET /api/v1/feature-flags/evaluate/{key}`).
- `backend/lambda/shared/`: the hashing, models and helpers both of them use.

Experiment assignment happens only in the API: `POST /api/v1/tracking/assign` calls
`AssignmentService`, which applies the global holdout, mutual exclusion groups and
targeting. The functions the stacks do deploy (two placeholders and the Glue ETL
trigger) are listed in [AWS Integration](../integrations/aws.md#lambda-functions).

---

## Background Schedulers

Five background jobs start with the API process (`lifespan` in `backend/app/main.py`):

| Scheduler | Default cycle | Responsibility |
|-----------|---------------|----------------|
| Experiment scheduler | `EXPERIMENT_SCHEDULER_INTERVAL_MINUTES` (15) | Starts draft experiments at `start_date`, resumes paused ones at a scheduled `resume_at`, stops experiments at `end_date` |
| Rollout scheduler | 15 min | Advances feature-flag rollout stages |
| Metrics scheduler | 15 min | Aggregates raw metrics |
| Safety monitor | 5 min | Checks per-flag safety thresholds, triggers rollbacks |
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

### Rollout Scheduler (every 15 min)

Advances feature flag rollout stages:
- Activates `TIME_BASED` stages once their `start_date` has passed (a stage never starts early, even
  when the previous stage has completed)
- Completes an `IN_PROGRESS` stage after its `trigger_configuration.duration` hours (default 24)
- Updates `rollout_percentage` on FeatureFlag
- Transitions stage status: `PENDING → IN_PROGRESS → COMPLETED`

### Safety Monitor (every 5 min)

Monitors active feature flags that have an enabled safety configuration:
- Reads error metrics from `error_logs` and latency metrics from `raw_metrics`
- Compares each configured metric against its thresholds
- Rolls the flag back to its `rollback_percentage` when a critical threshold is breached
- Records a `SafetyRollbackRecord`

```python
# Configure safety for a feature flag
POST /api/v1/safety/feature-flags/{feature_flag_id}/config
{
  "enabled": true,
  "metrics": {
    "error_rate": {"warning_threshold": 0.02, "critical_threshold": 0.05, "comparison_type": "greater_than"},
    "p95_latency": {"warning_threshold": 300, "critical_threshold": 500, "comparison_type": "greater_than"}
  },
  "rollback_percentage": 0
}
```

### Bandit Scheduler (every `BANDIT_UPDATE_INTERVAL_MINUTES`)

Recomputes variant weights for multi-armed bandit experiments and persists them in `bandit_state`;
`POST /api/v1/tracking/assign` routes new users by those weights. See
[Multi-Armed Bandit](../api/multi-armed-bandit.md).

---

## Caching Architecture

### Two-Level Cache

**Level 1: Application Cache (in-process)**
- Rules compilation: LRU(10,000) — compiled rule ASTs
- Evaluation results: LRU(1,000) with 300s TTL

**Level 2: Redis Cache**
- Feature flag state: 60s TTL
- User assignments: 3600s TTL
- Session tokens: per `ACCESS_TOKEN_EXPIRE_MINUTES`

### Cache Invalidation

Invalidate results cache for an experiment:

```text
POST /api/v1/results/{id}/invalidate-cache
```

Feature flags need no invalidation: the API reads the flag list, a flag's
detail and its evaluation from the database on every request.

---

## Database Schema

Schema: `experimentation` (set via `POSTGRES_SCHEMA` env var)

### Key Tables

```sql
-- All tables are in the `experimentation` schema
experimentation.users
experimentation.experiments
experimentation.variants
experimentation.metrics
experimentation.assignments      -- User-variant assignment log
experimentation.events           -- Tracking events
experimentation.feature_flags
experimentation.rollout_schedules
experimentation.rollout_stages
experimentation.safety_settings
experimentation.safety_rollback_records
experimentation.audit_logs       -- Change audit trail
```

### Unique Constraints

- `experiments.key` — unique per tenant
- `feature_flags.key` — unique per tenant
- `assignments(experiment_id, user_id)` — one variant per user per experiment
- `variants` traffic allocation: 0 ≤ x ≤ 100

---

## Migration Workflow

Step 1: Modify the model in `backend/app/models/`.

Step 2: Generate the migration, from the repository root of a development checkout. A
full checkout has two heads, the core chain and the `modules` branch, so first list them:

```{.bash skip reason="dev: runs alembic in a development checkout with its venv"}
source venv/bin/activate
export POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation
python -m alembic -c backend/app/db/alembic.ini heads
```

Then name the head the new revision extends: the core head's id for a change to a model in
`backend/app/models/`, or `modules@head` for a module's. Without `--head` the command fails
with "Multiple heads are present".

```{.bash skip reason="fragment: replace <core head id> with the core head that alembic heads printed"}
python -m alembic -c backend/app/db/alembic.ini revision --autogenerate --head <core head id> -m "add feature column"
```

Step 3: Review the generated file, `backend/app/db/migrations/versions/xxxx_add_feature_column.py`. Verify its `down_revision`, the column types and the schema prefix.

Step 4: Apply:

```{.bash skip reason="dev: runs alembic in a development checkout with its venv"}
python -m alembic -c backend/app/db/alembic.ini upgrade heads
```

Step 5: Verify:

```{.bash skip reason="dev: runs alembic in a development checkout with its venv"}
python -m alembic -c backend/app/db/alembic.ini current
```

---

## API Design Conventions

### Versioning

All API routes are prefixed with `/api/v1/`. Future versions will use `/api/v2/`.

### Response Envelope

Success responses return the resource directly (no envelope):
```json
{"id": "...", "name": "...", "status": "ACTIVE"}
```

Error responses follow RFC 7807:
```json
{"detail": "Not enough permissions"}
```

Paginated list responses:
```json
{
  "items": [...],
  "total": 100,
  "page": 1,
  "size": 20
}
```

### Status Codes

| Code | When |
|------|------|
| 200 | GET, PUT success |
| 201 | POST (create) success |
| 204 | DELETE success |
| 400 | Validation error |
| 401 | Missing/invalid authentication |
| 403 | Authenticated but insufficient permissions |
| 404 | Resource not found |
| 409 | Conflict (duplicate key, invalid state transition) |
| 422 | Unprocessable entity (Pydantic validation failure) |

---

## Use Cases

### Use Case 1: A/B Test a Checkout Flow

**Scenario:** Engineering wants to test a simplified 2-step checkout vs the current 4-step flow.

**Implementation:**

```python
# Step 1: Create experiment
POST /api/v1/experiments
{
  "name": "Simplified Checkout Test",
  "key": "checkout-simplified",
  "experiment_type": "a_b",
  "targeting_rules": {
    "logical_operator": "AND",
    "groups": [
      {
        "logical_operator": "AND",
        "conditions": [
          {"attribute": "country", "operator": "in", "value": ["US"]},
          {"attribute": "account_age_days", "operator": "greater_than", "value": 7}
        ]
      }
    ]
  },
  "variants": [
    {"name": "Control (4-step)", "is_control": true, "traffic_allocation": 50},
    {"name": "Treatment (2-step)", "is_control": false, "traffic_allocation": 50}
  ],
  "metrics": [
    {"name": "Purchase Complete", "event_name": "purchase_complete", "metric_type": "conversion", "is_primary": true},
    {"name": "Revenue", "event_name": "purchase_complete", "metric_type": "revenue", "is_primary": false}
  ]
}
```

`targeting_rules` uses the shape the dashboard's rule builder writes: groups of
conditions, each an `attribute`, an `operator` and a `value`. Rules in any other
shape are refused with `422` on create and update; the shape and the operators
are listed under "Targeting Rules" in the [API endpoints reference](../api/endpoints.md).

```javascript
// Step 2: In your frontend checkout page
const { variant_name } = await assignUser('user-123', 'checkout-simplified', {
  country: 'US',
  account_age_days: user.daysOnPlatform,
});

if (variant_name === 'Treatment (2-step)') {
  renderSimplifiedCheckout();
} else {
  renderCurrentCheckout();
}

// Step 3: Track conversion
await trackEvent('purchase_complete', 'user-123', 'checkout-simplified', {
  value: orderTotal,
  metadata: { order_id: orderId },
});
```

---

### Use Case 2: Gradual Feature Flag Rollout

**Scenario:** Ship a new recommendation engine to 5% → 25% → 100% over 3 weeks.

```python
# Step 1: Create flag
POST /api/v1/feature-flags
{"key": "new-recommendations", "name": "New Recommendation Engine", "rollout_percentage": 0}

# Step 2: Create rollout schedule
POST /api/v1/rollout-schedules
{
  "feature_flag_id": "FLAG_ID",
  "name": "Recommendation Engine Rollout",
  "stages": [
    {"stage_order": 1, "target_percentage": 5,   "trigger_type": "TIME_BASED", "start_date": "2026-04-01T00:00:00Z"},
    {"stage_order": 2, "target_percentage": 25,  "trigger_type": "TIME_BASED", "start_date": "2026-04-08T00:00:00Z"},
    {"stage_order": 3, "target_percentage": 100, "trigger_type": "MANUAL"}
  ]
}

# Step 3: Activate the schedule
POST /api/v1/rollout-schedules/SCHEDULE_ID/activate
```

The background scheduler automatically advances stages at the specified times. Stage 3 requires manual advancement:

```text
POST /api/v1/rollout-schedules/stages/STAGE3_ID/advance
```

---

### Use Case 3: Targeting High-Value Users

**Scenario:** Only run an experiment for users on a paid plan, in the US/EU, who have been active in the last 30 days.

Groups do not nest, so "and either of these two" is written as two groups joined
by `OR`, each carrying the shared conditions:

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

Pass user attributes in the context:
```javascript
await assignUser(userId, 'premium-feature-test', {
  plan: user.subscriptionPlan,
  country: user.countryCode,
  days_since_last_login: user.daysSinceLastLogin,
  lifetime_value: user.ltv,
  subscription_months: user.subscriptionMonths,
});
```

---

### Use Case 4: Safety-Gated Feature Flag

**Scenario:** New payment provider integration with auto-rollback if error rate > 2%.

```python
# Create flag
POST /api/v1/feature-flags
{"key": "new-payment-provider", "rollout_percentage": 5}

# Configure safety monitoring
POST /api/v1/safety/feature-flags/{feature_flag_id}/config
{
  "feature_flag_id": "FLAG_ID",
  "max_error_rate": 0.02,     # 2% error rate triggers rollback
  "max_latency_p99": 2000,    # 2s p99 latency threshold
  "window_seconds": 300,      # Monitor over 5-minute windows
  "auto_rollback": true,
  "rollback_percentage": 0    # Roll back to 0% on trigger
}

# Track errors in your payment service
POST /api/v1/tracking/track
{
  "event_type": "payment_error",
  "user_id": "user-123",
  "feature_flag_key": "new-payment-provider",
  "value": 1
}
```

If the error threshold is breached, the safety monitor rolls the flag back to its configured
`rollback_percentage` and creates a rollback record. A target of `0` (the default) turns the flag off for
every user, including users matched by a targeting rule; a target from 1 to 100 lowers only the global
rollout percentage. Either way the flag's active rollout schedule is paused. See
[What a rollback changes](../feature-flags/safety.md#what-a-rollback-changes).

---

## Performance Characteristics

No throughput or latency figure is published yet: none has been measured on a deployment, and
none will be given here until one has. What decides throughput today:

- Each API container runs one uvicorn worker process (`WEB_CONCURRENCY`, which defaults to `1`
  in the image, both compose files and the Helm chart; the AWS task definition does not set it).
- In that process, most SDK routes (a single assignment, event tracking and flag evaluation)
  do their database work one request at a time
  ([#823](https://github.com/getexperimently/experimently/issues/823)).
- So throughput grows with the number of API tasks or replicas, not with the number of
  requests sent at once to one of them: on AWS the service's task count (its floor is
  `API_DESIRED_COUNT` in `infrastructure/cdk/app.py`), with the Helm chart `api.replicaCount`.

---

## Infrastructure (AWS)

| Service | Usage |
|---------|-------|
| ECS Fargate | API container hosting |
| Aurora PostgreSQL | Primary database |
| ElastiCache Redis | Caching layer |
| Kinesis Data Streams | Full profile only: a stream feeding Firehose and the S3 data lake; the API does not write to it |
| Lambda | Two placeholder functions and the Glue ETL trigger; the code under `backend/lambda/` is not deployed |
| Cognito | User authentication |
| CloudWatch | Monitoring + alerting |
| Secrets Manager | Credentials storage |

The dashboard runs as its own ECS service behind the API's load balancer: the
HTTPS listener sends `/api/*`, `/health`, `/health/*` and `/metrics` to the API
and everything else to the dashboard. `cdk deploy` starts it on the
`experimentation-platform/web:bootstrap` image; the Deploy workflow rolls
each release onto it by digest, after the API. Nothing creates CloudFront or
an S3 bucket for it. It is the `frontend/Dockerfile` image, a static export
served by nginx, which proxies `/api/` to the API in Docker Compose and proxies
nothing in AWS.

---

## Monitoring & Observability

### Key Metrics (CloudWatch)

- `api.request.duration` — p50, p95, p99 per endpoint
- `rules.evaluation.duration` — rules engine latency
- `assignment.throughput` — assignments per second
- `event.ingestion.rate` — events per second
- `feature_flag.evaluation.rate` — evaluations per second
- `db.query.duration` — query latency
- `cache.hit_rate` — cache efficiency

### Log Structure

All logs follow structured JSON format:

```json
{
  "timestamp": "2026-03-01T10:00:00Z",
  "level": "INFO",
  "service": "experimentation-api",
  "request_id": "req-abc123",
  "user_id": "user-456",
  "endpoint": "/api/v1/tracking/assign",
  "duration_ms": 12,
  "experiment_key": "homepage-test",
  "variant": "Treatment"
}
```

### Dashboards

- Scheduler health: http://localhost:8000/api/v1/docs#/Scheduler%20Health
- CloudWatch: `/experimentation-platform/api` log group
- Health: http://localhost:8000/health

# Architecture Overview

This document describes the high-level technical architecture of the platform: how the components fit together, how data flows, and how the system is deployed on AWS.

---

## High-Level Components

```text
┌──────────────────────────────────────────────────────────────────┐
│                        Client Applications                        │
│           (web, mobile, server — via SDK or REST API)            │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                Application Load Balancer (HTTPS)                 │
│  (no CloudFront; /api/* → API, everything else → the dashboard)  │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                    API Layer (FastAPI)                            │
│        Experiments / Feature Flags / Analytics / Auth            │
└──────┬───────────────┬───────────────┬───────────────────────────┘
       │               │               │
       ▼               ▼               ▼
  PostgreSQL        Redis           DynamoDB
  (Aurora)      (ElastiCache)    (real-time counters,
                                  counters module)
```

A full-profile deployment also creates a Kinesis stream, Firehose, an S3 data lake, an
OpenSearch domain and Glue ETL jobs. The API does not write to them today; see
[Analytics Pipeline](#analytics-pipeline).

---

## API Layer

### FastAPI Application

The core of the platform is a **FastAPI** application that exposes a REST API. It is deployed on **ECS Fargate** and scales horizontally behind an Application Load Balancer.

The API is organized into resource-specific endpoint groups:

| Endpoint Group | Description |
|----------------|-------------|
| `/api/v1/experiments` | Create, update, start, pause, stop, and query experiments |
| `/api/v1/feature-flags` | Manage feature flags, targeting rules, and rollout percentages |
| `/api/v1/results` | Read statistical results, with CUPED, Bayesian and sequential analyses, FDR correction, daily series and sample sizes |
| `/api/v1/tracking` | Ingest assignment and event data from client applications |
| `/api/v1/users` | User management and authentication |
| `/api/v1/compliance` | Audit event log and compliance reports |
| `/api/v1/integrations` | Jira, Salesforce, and GitHub integrations |
| `/api/v1/notifications` | Slack and email alert preferences |
| `/api/v1/rbac` | Role management and permission grants |
| `/api/v1/bandit` | Multi-armed bandit state and weight management |

Interactive API documentation is available at `/api/v1/docs` (Swagger UI) and `/api/v1/redoc` (ReDoc).

### Background Schedulers

Several background tasks run on a configurable cycle alongside the API process:

| Scheduler | Default Cycle | Responsibility |
|-----------|---------------|----------------|
| Experiment scheduler | 15 minutes (`EXPERIMENT_SCHEDULER_INTERVAL_MINUTES`) | Auto-starts draft experiments at `start_date`, resumes a paused experiment only at a `resume_at` scheduled after the pause, auto-stops at `end_date` |
| Rollout scheduler | 15 minutes | Advances rollout schedule stages based on time-based triggers |
| Safety monitor | 5 minutes | Checks error rate and latency thresholds; triggers auto-rollback if breached |
| Bandit scheduler | 5 minutes (`BANDIT_UPDATE_INTERVAL_MINUTES`) | Recomputes variant weights for active multi-armed bandit experiments from DynamoDB counters when every variant has at least PostgreSQL's pulls and successes there, otherwise from PostgreSQL assignments + converting users (counted as `/results` counts them); `/tracking/assign` routes new users by these weights |

---

## Database Layer

### PostgreSQL (Aurora)

Amazon Aurora PostgreSQL is the primary relational database. It stores all persistent application data:

- Experiments, variants, metrics definitions
- Feature flags, targeting rules, rollout schedules
- User accounts, roles, permissions
- Audit events and compliance records
- Integration configurations
- Results aggregations

All schema changes are managed with **Alembic** migrations. The schema is namespaced under the `experimentation` PostgreSQL schema.

The production Aurora cluster is configured with a read replica. The API is given the writer endpoint only, so every query goes to the writer.

### Redis (ElastiCache)

Redis holds the rate limiter's counters and the API's caches:

1. **Rate-limit counters**: the request counts the rate limiter keeps per client and route group.

2. **Application cache**: experiments, for an hour, and results. The API does not cache feature flags: it reads them from the database on every request, so a flag change is served on the next one.

---

## Lambda Functions

**No Lambda function serves requests or processes events.** SDKs call the API for assignment
(`POST /api/v1/tracking/assign`) and flag evaluation (`GET /api/v1/feature-flags/evaluate/{key}`).
The stacks deploy three functions: `DatabaseAccessLambda` (compute stack) and
`AnalyticsLambda` (analytics stack, full profile) are placeholders whose inline code returns
200 and does nothing, and `ETLTriggerLambda` (Glue ETL stack, full profile) starts the Glue
jobs daily. The event-processor and flag-evaluation code under `backend/lambda/`
is not deployed by any stack. See [AWS Integration](../integrations/aws.md#lambda-functions).

---

## Counters (DynamoDB, `counters` module)

The `counters` module keeps assignment and conversion counts in DynamoDB, incremented with atomic `ADD` operations through its own routes (`POST /api/v1/counters/{experiment_id}/increment` and `POST /api/v1/counters/bulk`). Assignment and tracking do not write them.

### Counter Schema

The table is `experiment-counters-<env>`, keyed by `pk` and `sk`, with an
`experiment-id-index` global secondary index
(`modules/infrastructure/cdk/stacks/dynamodb_counters_stack.py`).

The bandit scheduler reads these counters first when it updates a bandit's weights, and falls
back to PostgreSQL when they are unavailable (`backend/app/core/bandit_scheduler.py`).

---

## Analytics Pipeline

Tracked events are stored in PostgreSQL: `POST /api/v1/tracking/track` and
`/tracking/batch` write rows to the `events` table, and every analysis reads them from there.

The full profile's analytics stack creates a pipeline beside that, which the API does not feed
today:

```text
Kinesis Data Stream ──> Firehose ──> S3 data lake ──> Glue ETL (daily)
        │
        └──> AnalyticsLambda (placeholder: returns 200, processes nothing)

OpenSearch domain: created; nothing writes to it
```

---

## Split URL Testing (Lambda@Edge)

A split URL experiment gives each variant its own URL. The split-URL module ships
a CloudFront construct (`modules/infrastructure/constructs/split_url_distribution.py`) with a
Lambda@Edge `viewer-request` router. **No stack uses it**: `infrastructure/cdk/app.py` creates
no CloudFront distribution, so a deployment has it only if you add it to a stack yourself.

The router (`modules/lambda/split_url_router/handler.py`):

```text
Viewer request
    |
    +-- Read the experiment config from the X-Split-URL-Config request header
    |     - Missing, invalid, or fewer than two variants: pass the request through
    |
    +-- Read the assignment cookie
    |     - Holds a known variant URL: pass through
    |     - Absent or unknown: hash the client fingerprint (IP + User-Agent) to pick a variant
    |
    +-- Return 302 to the variant URL
    |
    +-- Set-Cookie with Max-Age of cookie_ttl_days (default 30 days)
```

The construct does not set `X-Split-URL-Config`, so how the configuration reaches the router is
left to you. Lambda@Edge functions must be deployed in `us-east-1`. See
[Split URL testing](../api/split-url.md).

---

## Authentication

### Sign-in

Users sign in with a local account by default (`AUTH_PROVIDER=local`): the API checks the password and issues a JWT access token, valid for 12 hours by default (`LOCAL_AUTH_TOKEN_TTL_MINUTES`). The token is passed in the `Authorization: Bearer <token>` header. [Cognito Integration](../cognito_integration.md) describes the other provider, an AWS Cognito user pool.

### API Key Authentication

SDK clients and server-to-server integrations use **API keys** instead of JWT tokens. API keys are:

- Created by any signed-in user, for themselves, via `POST /api/v1/api-keys`; whatever its creator's role, a key writes tracking data (assignments and events)
- Passed in the `X-API-Key: <key>` header
- Not limited by their `scopes`, with one exception: any active key authenticates every API-key route as the user who created it, except the server-side SDK routes (`GET /api/v1/sdk/ruleset`, `POST /api/v1/tracking/evaluations`, `POST /api/v1/tracking/assign/batch`), which require the `sdk:ruleset` scope (see [API Key Management](../security/api-keys.md#scopes))
- Revocable without affecting user accounts

### Role-Based Access Control

Every user has one of four roles, each with progressively broader permissions:

| Role | Description |
|------|-------------|
| **VIEWER** | Reads every experiment, flag and result and changes none; can create an API key, which writes tracking data |
| **ANALYST** | View all experiments, results, audit logs, and reports |
| **DEVELOPER** | Create and manage experiments and feature flags |
| **ADMIN** | Full access: user management, global settings, compliance exports |

Custom roles and direct permission grants are stored and shown alongside the base role, but no permission check reads them yet: routes check the base role ([#891](https://github.com/Controlvar/controlvar/issues/891)).

---

## CDK Infrastructure

The AWS infrastructure is defined as code using **AWS CDK v2**, in Python (`infrastructure/cdk/app.py`), which defines the stacks below; `cdk deploy --all` from `infrastructure/cdk` deploys them.

The Fargate stack runs the dashboard as its own ECS service behind the API's load
balancer: `/api/*`, `/health`, `/health/*` and `/metrics` go to the API, everything
else to the dashboard. `cdk deploy` starts it on `experimentation-platform/web:bootstrap`;
the Deploy workflow rolls each release onto it by digest, after the API. No
stack creates CloudFront. The split-URL module's CloudFront construct
exists but `app.py` does not use it. See [AWS CDK Deployment](../self-hosting/cdk.md).

### Stacks

| Stack (`<env>` is `dev`, `staging` or `prod`) | Contents |
|---|---|
| `experimentation-vpc-<env>` | VPC, public/private/isolated subnets, NAT, route tables, network ACLs, gateway endpoints, security groups |
| `experimentation-auth-<env>` | Cognito user pool and app client |
| `experimentation-database-<env>` | Aurora PostgreSQL cluster, parameter group, KMS key, security group |
| `experimentation-redis-<env>` | ElastiCache Redis replication group, subnet group, security group |
| `experimentation-dynamodb-<env>` | Three DynamoDB tables (events, feature flags, overrides) |
| `experimentation-compute-<env>` | ECS cluster, task security group, a placeholder Lambda (`DatabaseAccessLambda`) |
| `experimentation-fargate-<env>` | ALB, HTTPS + test listeners, blue/green target groups, the API's Fargate service, CodeDeploy application and deployment group, auto-scaling; the dashboard's ECS service and target group |
| `experimentation-migrations-<env>` | One-off ECS task definition that runs the alembic upgrade |
| `experimentation-monitoring-<env>` | CloudWatch dashboards and alarms, and an SNS topic |
| `experimentation-dynamodb-counters-<env>` | **Full profile only** — the real-time experiment-counters table |
| `experimentation-analytics-<env>` | **Full profile only** — Kinesis stream, Firehose, S3 data lake, OpenSearch domain, a placeholder consumer Lambda |
| `experimentation-glue-etl-<env>` | **Full profile only** — Glue database and crawler, two ETL jobs, Athena results bucket, daily trigger |

A core deployment builds the first nine; a full one builds all twelve. The
names are the stack **ids** `cdk deploy` takes, not class names -- run
`cdk list` to see them for your environment.

### Deployment Model

The API service uses **blue/green deployment** via AWS CodeDeploy:

1. A new task definition is registered (the "green" environment)
2. CodeDeploy gradually shifts traffic from the old (blue) to the new (green) deployment
3. If health checks fail, traffic is automatically shifted back to blue
4. The canary sends 10% of traffic to the new tasks for 15 minutes, then shifts the rest

---

## Health and Observability

| Endpoint / Resource | Purpose |
|---------------------|---------|
| `GET /health` | The same as `GET /health/ready`: `{"status": "healthy"}` with 200 when the API can serve, `"unhealthy"` with 503 when it cannot |
| `GET /metrics` | Prometheus-format metrics (request count, latency histograms, error rates) |
| CloudWatch (CDK deployments) | The dashboards and alarms the monitoring and Fargate stacks create; see [Monitoring](../self-hosting/monitoring.md) |
| Structured JSON logs | Each request's log lines carry its `request_id`, `path` and `method` |

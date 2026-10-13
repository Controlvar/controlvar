# High-Level Architecture: Experimently for AWS

Based on the detailed requirements for an experimentation platform with A/B testing and feature flag capabilities, this document outlines a comprehensive architecture leveraging AWS services to build a robust, scalable solution.

## Architecture Overview

The architecture is designed to provide high-performance experiment evaluation, real-time event collection, advanced statistical analysis, and flexible targeting capabilities.

!!! warning "This is the design, not what the CDK deploys today"
    The diagram below is the target design. What `infrastructure/cdk` deploys
    today is the API and the dashboard on ECS Fargate behind one Application
    Load Balancer, with
    Aurora PostgreSQL, ElastiCache Redis and DynamoDB tables; see
    [AWS CDK Deployment](../self-hosting/cdk.md).

    **The dashboard is its own ECS service behind the same load balancer**: the
    HTTPS listener sends `/api/*`, `/health`, `/health/*` and `/metrics` to the
    API and everything else to the dashboard. `cdk deploy` starts it on the
    `experimentation-platform/web:bootstrap` image; the Deploy workflow rolls
    each release onto it by digest, after the API. No stack creates CloudFront
    or API Gateway. The dashboard is a static Next.js export served by nginx; in
    Docker Compose that nginx also proxies `/api/` to the API, and in AWS it
    proxies nothing.

```text
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                  Client Applications                             │
└───────────────────┬─────────────────────────────────────┬─────────────────────┬─┘
                    │                                     │                     │
                    ▼                                     ▼                     ▼
┌───────────────────────────┐     ┌───────────────────────────┐     ┌─────────────────────────┐
│  Amazon CloudFront        │     │    Application Load       │     │  Amazon API Gateway     │
│  (not created by the CDK) │     │       Balancer            │     │  (not created by CDK)   │
└─────────────┬─────────────┘     └──────────────┬────────────┘     └────────────┬────────────┘
              │                                  │                                │
              │                                  │                                │
              ▼                                  ▼                                ▼
┌───────────────────────────┐     ┌───────────────────────────┐     ┌─────────────────────────┐
│  Next.js dashboard        │     │   ECS/Fargate Containers  │     │   Lambda Functions      │
│  (ECS, behind the ALB)    │     │   (Core Backend Services) │     │   (not deployed)        │
└─────────────┬─────────────┘     └──────────────┬────────────┘     └────────────┬────────────┘
              │                                  │                                │
              │                                  │                                │
              │                                  ▼                                │
              │                   ┌───────────────────────────┐                  │
              └────────────────▶ │     Amazon RDS Aurora     │ ◀────────────────┘
                                 │     (Primary Database)     │
                                 └──────────────┬────────────┘
                                                │
                                                │
                                                ▼
          ┌────────────────────────────────────────────────────────────────┐
          │                                                                │
┌─────────┴─────────┐    ┌────────────────────┐    ┌─────────────────────┐│
│ Amazon DynamoDB   │    │ Amazon ElastiCache │    │ Amazon OpenSearch   ││
│ (High-throughput  │    │ (Redis for caching)│    │ (Analytics Engine)  ││
│  event storage)   │    │                    │    │                     ││
└─────────┬─────────┘    └────────────────────┘    └─────────────────────┘│
          │                                                                │
          └────────────────────────────────────────────────────────────────┘
               ▲                     ▲                          ▲
               │                     │                          │
               │                     │                          │
┌──────────────┴─────────┐  ┌───────┴───────────┐  ┌───────────┴───────────┐
│  Amazon Kinesis        │  │  AWS EventBridge  │  │  AWS Glue / Athena    │
│  (Event Stream)        │  │  (Event Bus)      │  │  (Data Processing)    │
└──────────────┬─────────┘  └───────────────────┘  └───────────────────────┘
               │
               │
               ▼
┌────────────────────────┐
│  Amazon S3             │
│  (Data Lake)           │
└────────────────────────┘
```

## Core Components

### 1. Client-Facing Layer

-   **Application Load Balancer**: For routing traffic to backend services. This is the only entry point the CDK creates: HTTPS, with HTTP redirected to HTTPS, in front of the API.
-   **Amazon CloudFront** (design only, not created by the CDK): Global CDN for delivering static assets and caching API responses
-   **Amazon API Gateway** (design only, not created by the CDK): Manages APIs for experiment evaluation, event tracking, and admin functions

### 2. Application Layer

-   **Next.js dashboard**: Web UI for experiment management, feature flag configuration, and dashboards. It runs as the nginx image built from `frontend/Dockerfile`: in AWS as its own ECS service behind the load balancer (started on `web:bootstrap`, then rolled onto each release by the Deploy workflow), and in Docker Compose
-   **Core Backend Services (ECS/Fargate)**:
    -   Experiment Management Service
    -   Feature Flag Service
    -   Segmentation Service
    -   Analysis Service
    -   User Management Service
    -   Assignment Service: assigns users to experiments (`POST /api/v1/tracking/assign`),
        applying the global holdout, mutual exclusion groups and targeting
-   **Lambda code (not deployed)**: `backend/lambda/` holds flag-evaluation and
    event-processing code that no stack deploys. Flags are evaluated by the API
    (`GET /api/v1/feature-flags/evaluate/{key}`).

### 3. Data Storage Layer

-   **Amazon Aurora PostgreSQL**: Primary database for:
    -   Experiment definitions
    -   Feature flag configurations
    -   User accounts and permissions
    -   Segmentation rules
-   **Amazon DynamoDB**: High-throughput NoSQL database for:
    -   Experiment assignments (user to variant mappings)
    -   Feature flag evaluations
    -   Real-time event tracking
-   **Amazon ElastiCache (Redis)**: In-memory caching for:
    -   Feature flag configurations
    -   Experiment assignments
    -   Segment membership
-   **Amazon S3**: Data lake for:
    -   Raw event data
    -   Analytics exports
    -   Audit logs

### 4. Analytics & Processing Layer

-   **Amazon Kinesis** (full profile): a stream that Firehose delivers to the S3 data lake; the API does not write to it today
-   **AWS EventBridge**: Event bus for internal service communication
-   **Amazon OpenSearch Service** (full profile): a domain is created; nothing writes to it today, and results are computed by the API from PostgreSQL
-   **AWS Glue & Athena**: ETL jobs, partitions and crawlers for the data lake

### 5. Supporting Services

-   **Amazon Cognito**: User authentication and authorization
-   **AWS Lambda**: Two placeholder functions and the Glue ETL trigger; the code under `backend/lambda/` is not deployed by any stack (see [AWS Integration](../integrations/aws.md#lambda-functions))
-   **Amazon CloudWatch**: Monitoring, logging, and alerting
-   **AWS WAF**: Web application firewall for security
-   **AWS Secrets Manager**: Secure credential storage

## Key Service Implementations

### 1. Experiment Management & Feature Flags System

```text
┌───────────────────────────────────────┐
│      Experiment Management UI         │
└─────────────────┬─────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────┐
│   Experiment Management Service       │
│   - Create/Edit/Delete experiments    │
│   - Manage variants                   │
│   - Configure targeting rules         │
└─────────────────┬─────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────┐
│   Feature Flag Service                │
│   - Feature flag definitions          │
│   - Targeting rules                   │
│   - Rollout configurations            │
└─────────────────┬─────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────┐
│   API: /tracking/assign, /evaluate    │
│   - User-to-experiment assignment     │
│   - Feature flag evaluation           │
│   - Holdout, exclusion, traffic       │
└─────────────────┬─────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────┐
│   PostgreSQL + ElastiCache            │
│   - Assignment storage                │
│   - Configuration cache               │
└───────────────────────────────────────┘
```

### 2. Real-Time Event Collection & Analysis

```text
┌───────────────────────────────────────┐
│      Client SDK                       │
│      - Experiment exposure tracking   │
│      - Conversion events              │
└─────────────────┬─────────────────────┘
                  │  POST /api/v1/tracking/track
                  │  POST /api/v1/tracking/batch
                  ▼
┌───────────────────────────────────────┐
│   API (ECS service)                   │
│   - X-API-Key authentication          │
│   - Event validation                  │
└─────────────────┬─────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────┐
│   PostgreSQL                          │
│   - Events and assignments            │
└─────────────────┬─────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────┐
│   Results, computed by the API        │
│   (/api/v1/results/...)               │
└───────────────────────────────────────┘
```

Kinesis, Lambda and OpenSearch are not on this path: see
[Kinesis and OpenSearch](../integrations/aws.md#kinesis-and-opensearch).

## Key Technical Capabilities

### 1. Real-Time Feature Flag & Experiment Evaluation

-   **API evaluation endpoint** (`GET /api/v1/feature-flags/evaluate/{key}`), served by the ECS service
-   **Consistent hashing algorithm** for stable user assignment
-   **Redis caching layer** for configuration data
-   **SDK implementations** for web, mobile, and server environments

### 2. High-Performance Event Collection

-   **Event tracking through the API** (`POST /api/v1/tracking/track` and `/tracking/batch`), stored in PostgreSQL
-   **Batched event processing** to optimize throughput
-   **Event schema validation** and enrichment
-   **Real-time metrics aggregation** using DynamoDB counters

### 3. Statistical Analysis System

-   **Significance testing** computed by the API from the events in PostgreSQL
-   **Multi-metric analysis** with correction for multiple testing
-   **Bayesian analysis** for more nuanced experiment interpretation
-   **Automated experiment stopping** based on significance thresholds

### 4. Segmentation & Targeting Engine

-   **Rich targeting rule evaluation** supporting complex logical operations
-   **User attribute-based targeting** (device, location, behavior)
-   **Sticky bucket assignment** for consistent user experience
-   **Gradual rollout control** with percentage-based targeting

## Scalability & Reliability Considerations

1. **High Availability**

    - Multi-AZ deployments for all services
    - Database read replicas for read scaling
    - Service auto-scaling based on load

2. **Performance Optimization**

    - Redis caching for experiment configurations (feature flags are read from the database on every request)
    - DynamoDB for high-throughput event ingestion
    - API response caching at CloudFront edge locations

3. **Operational Excellence**
    - Infrastructure as Code using AWS CDK or CloudFormation
    - Automated CI/CD pipelines for deployment
    - Comprehensive monitoring and alerting

## Implementation Requirements Fulfillment

This architecture addresses all core requirements from the project specification:

1. **Experiment Creation and Management**

    - Supports all experiment types (A/B, multivariate, split URL, multi-armed bandit)
    - Provides variant management with traffic distribution control
    - Enables advanced segmentation and scheduling capabilities
    - Allows real-time experiment control

2. **Metrics and Events Tracking**

    - Supports custom metrics definition
    - Provides comprehensive event logging
    - Offers real-time metrics reporting
    - Includes statistical analysis capabilities

3. **Randomization and Traffic Allocation**

    - Implements random user assignment
    - Supports controlled traffic allocation
    - Enables real-time allocation adjustments

4. **Feature Flags Integration**

    - Provides complete feature flag management
    - Implements targeting rules
    - Enables real-time feature control
    - Supports multivariate flag configuration
    - Allows gradual rollouts and immediate rollbacks

5. **User Access Control and Permissions**

    - Implements role-based access control
    - Provides experiment-level permissions
    - Maintains comprehensive audit logs

6. **Results and Analysis**
    - Offers robust statistical analysis
    - Provides visualizations and dashboards
    - Implements automated significance calculations

---

## Post-MVP Components

The following capabilities were added after the initial platform release. See the linked docs for full details.

### Advanced Statistical Methods

| Component | Description | Doc |
|-----------|-------------|-----|
| Sequential Testing (EP-021) | mSPRT continuous monitoring, always-valid CIs, alpha spending (O'Brien-Fleming, Pocock) | [sequential-testing.md](../api/sequential-testing.md) |
| CUPED Variance Reduction (#21, #217) | Covariate adjustment (pooled OLS θ) for each user's own events before assignment, every treatment against the control | [cuped.md](../api/cuped.md) |
| Multi-Armed Bandit (#22) | Thompson Sampling, UCB1, Epsilon-Greedy; background MAB scheduler | [multi-armed-bandit.md](../api/multi-armed-bandit.md) |
| Dimensional Analysis (#28) | Per-segment breakdowns with Bonferroni correction, HTE detection | [dimensional-analysis.md](../api/dimensional-analysis.md) |
| Interaction Detection (#25), beta | Jaccard overlap between experiments; (beta) whether one experiment's lift on its primary conversion metric differs across another's arms | [interaction-detection.md](../api/interaction-detection.md) |

### Traffic Management

| Component | Description | Doc |
|-----------|-------------|-----|
| Mutual Exclusion Groups (EP-022) | Consistent-hashing traffic partitioning to prevent cross-experiment contamination | [mutual-exclusion-groups.md](../api/mutual-exclusion-groups.md) |
| Global Holdout (EP-022) | Keeps a percentage of new users out of every experiment; `GET /api/v1/holdout/{id}/results` (beta) compares them with everyone else first seen while it was active. Feature flags and split-URL experiments ignore it. | [mutual-exclusion-groups.md](../api/mutual-exclusion-groups.md) |

### Data & Integrations

| Component | Description | Doc |
|-----------|-------------|-----|
| Warehouse analysis (#312) | Beta; Snowflake and BigQuery are available | [warehouse-analytics.md](../api/warehouse-analytics.md) |
| Real-time DynamoDB Counters (P2-B) | Atomic ADD operations, bulk counter updates, Lambda integration | [dynamodb-readme.md](../infrastructure/dynamodb-readme.md) |

### Platform Experience

| Component | Description | Doc |
|-----------|-------------|-----|
| Guided experiment builder (#27, #376) | Guided setup in the dashboard (browser-only, 5 steps, same create request as the form); a separate 5-step draft-and-submit API | [experiment-wizard.md](../guides/experiment-wizard.md) |
| AI Experiment Design (#23) | Design suggestions, results interpretation, sample sizes and templates over REST; a manifest that describes them as MCP tools, with no MCP server behind it | [mcp-server.md](../mcp-server.md) |
| Audit Logging & Bulk Toggle (P1-B) | Immutable audit trail, SSE real-time stream, bulk flag operations | [audit-logging.md](../api/audit-logging.md) |
| RBAC Post-MVP (P2-A) | Custom roles, direct permission grants, effective permissions view (stored and shown; no permission check reads them yet, #891) | [rbac.md](../api/rbac.md) |
| Slack / Email Alerting (EP-030) | Event-driven notifications via Slack Block Kit and SendGrid/SMTP | [alerting.md](../api/alerting.md) |
| Admin UI (EP-029) | React admin panel for users, roles, audit logs, notifications, and resource management | — |

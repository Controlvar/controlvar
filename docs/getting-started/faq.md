# Frequently Asked Questions

---

## General

### What is Controlvar?

Controlvar is a self-hosted experimentation platform for running A/B tests, feature flags, and controlled rollouts. It provides a complete solution for data-driven product development: design experiments, assign users to variants, track conversion events, run statistical analysis, and make shipping decisions — all in one platform.

The platform includes a REST API backend (FastAPI), a React management dashboard, client SDKs (JavaScript, Python, Java, React), and AWS CDK infrastructure code.

---

### Is it open source and self-hostable?

Yes. The code is Apache-2.0, with the client SDKs MIT, and you run it yourself: Docker Compose on one host, the Helm chart on Kubernetes, or the AWS CDK app in your AWS account. Your experiment data is stored in your own PostgreSQL database. Features you configure send data where you point them: LLM evaluation to the model provider you choose, alerting to Slack or a mail server, and warehouse analysis to your warehouse.

On AWS, the CDK app in `infrastructure/cdk/` defines the environment: the API and the dashboard as ECS Fargate services behind one Application Load Balancer, the Aurora PostgreSQL database and the Redis cache. The dashboard starts on the `web:bootstrap` image you push first. After that, each run of the Deploy workflow rolls the release onto the dashboard too: it builds the release's dashboard image (or reuses the one already in ECR for that version), registers a new dashboard task definition with it, rolls the dashboard service onto it once the API is serving the same release, and smoke-tests the dashboard through the public URL ([Deployment Guide](../deployment/deployment-guide.md)). The CDK does not create a CloudFront distribution. A checkout that also has `modules/` gets the real-time DynamoDB counters table and the Kinesis/OpenSearch/Glue data lake alongside them — see [AWS CDK Deployment](../self-hosting/cdk.md) for which stacks each profile deploys.

---

### What AWS services does it require?

A full production deployment uses the following AWS services:

| Service | Purpose |
|---------|---------|
| ECS Fargate | Hosts the FastAPI application |
| Aurora PostgreSQL | Primary relational database |
| ElastiCache Redis | Rate-limit counters and the API's caches |
| DynamoDB | Full profile: the `counters` module's counters, which only its own routes write |
| Lambda | No request or event processing: placeholder functions that do nothing, and a daily Glue ETL trigger in the full profile. Assignment and flag evaluation are API calls ([AWS Integration](../integrations/aws.md#lambda-functions)) |
| CloudFront | Not created by the CDK app. The split URL edge router, a CDK app of its own, creates a distribution and a Lambda@Edge function only when you deploy it; AWS bills both ([AWS Integration](../integrations/aws.md#the-split-url-edge-router)) |
| Kinesis | Full profile only: a stream feeding Firehose and the S3 data lake. The API does not write to it today |
| OpenSearch | Full profile only: a domain is created; nothing writes to it today |
| Cognito | A user pool the CDK creates; sign-in uses local accounts by default ([Cognito Integration](../cognito_integration.md)) |
| S3 | Full profile only: the analytics data lake, Athena results and Glue scripts. The dashboard is an ECS service, not files in S3 |
| CloudWatch | Logging, metrics, and dashboards |
| Secrets Manager | The JWT signing secret, the first administrator's password, and the database user and its password; in the full profile, also the audit signing key |
| CodeDeploy | Blue/green deployments for the API service |

For local development, Docker Compose provides PostgreSQL and Redis, and LocalStack can emulate most AWS services.

---

### How is user assignment consistent across requests?

User-to-variant assignment uses a **deterministic consistent hash** of `experiment_key + user_id`. Because the hash is deterministic, the same user always receives the same variant for a given experiment — regardless of which server processes the request, whether the cache is warm, or whether the SDK has been restarted.

The hash decides a new user's variant from the experiment's traffic weights. The API then stores the assignment, and a user who already has one gets it back unchanged, even if the weights have changed since.

---

### What statistical methods are supported?

The platform supports a range of statistical approaches:

| Method | Description |
|--------|-------------|
| Frequentist (Fisher's exact test) | Each treatment against the control: a two-sided Fisher's exact test on the users who converted and the users who did not. A revenue, count, duration or custom metric is compared on its mean per assigned user with Welch's t-test instead; warehouse analysis (beta) compares a mean metric with Welch's t-test |
| Sequential testing (mSPRT) | Continuous monitoring with valid p-values at any sample size |
| Always-valid confidence intervals | Confidence sequences that are valid at every look |
| Alpha spending (O'Brien-Fleming, Pocock) | Not computed yet: the response's `alpha_spending` is empty; use the mSPRT, which is valid under continuous monitoring |
| CUPED | Variance reduction from each user's own events before assignment (API only) |
| Bayesian (Beta-Binomial) | Posterior credible intervals, Bayes factors, probability of superiority, ROPE (the priors, loss threshold and credible level are set on the new-experiment form or through the API; the ROPE through the API only) |
| Multi-armed bandit | Thompson Sampling, UCB1, and Epsilon-Greedy adaptive traffic allocation |
| Dimensional analysis | Segment-level breakdowns with Bonferroni correction and heterogeneous treatment effect detection |
| Interaction detection (beta) | Jaccard overlap between experiments; whether one experiment's lift on its primary conversion metric differs across another's arms (API only) |

[What the dashboard does, and what is API only](../guides/dashboard-and-api.md) says which of these the
dashboard shows.

---

### What SDKs are available?

The JavaScript SDK is on npm (`npm install @controlvar/js-sdk`, 0.3.0, beta) and the
Python SDK is on PyPI (`pip install controlvar`, 0.3.0, beta). So are the React SDK
(`npm install @controlvar/react-sdk`, 0.1.0, beta), the React Native SDK
(`npm install @controlvar/react-native-sdk`, 0.1.0, beta) and the two OpenFeature providers
(`npm install @controlvar/openfeature-provider`, 0.1.0, beta; `pip install controlvar-openfeature`,
0.1.0, beta). The Java SDK is on Maven Central and the .NET SDK on NuGet. The Go SDK installs by
module path, and the other SDKs from a clone of this repository: each SDK's page
([Go](../sdk/go.md), [Ruby](../sdk/ruby.md)) says how.

| SDK | Package |
|-----|---------|
| JavaScript / TypeScript | `@controlvar/js-sdk` (npm) |
| Python | `controlvar` (PyPI) |
| Java / JVM | `com.controlvar:controlvar-sdk` (Maven Central) |
| React | `@controlvar/react-sdk` (npm) |
| React Native | `@controlvar/react-native-sdk` (npm) |
| OpenFeature (Node) | `@controlvar/openfeature-provider` (npm) |
| OpenFeature (Python) | `controlvar-openfeature` (PyPI) |
| .NET | `Controlvar.SDK` (NuGet) |

13 of the 16 SDKs run experiment variant assignment, feature flag evaluation and event tracking against a live API in CI, in the SDK Live Contract job of `pr-qa-gate.yml`. The Java SDK includes Spring Boot auto-configuration. The React SDK includes hooks, an HOC, and SSR support for Next.js.

---

## Getting Started

### How do I get started?

Follow the [Quick Start Guide](quick-start.md). It walks you through:

1. Starting the local stack with Docker Compose: Postgres, Redis, the API and the dashboard
2. Signing in
3. Creating and starting an experiment, in the dashboard or through the API
4. Creating an API key
5. Assigning a user and tracking an event
6. Reading the results, then creating and evaluating a feature flag

For a full AWS deployment, see [CDK Deployment](../self-hosting/cdk.md).

---

### What is the difference between a feature flag and an experiment?

**Feature flags** control whether a feature is visible to a user, or which value they get. They are primarily operational tools — used for gradual rollouts, kill switches, and beta programs. A flag has no statistical analysis attached to it. A flag does not measure which value does better; an experiment does.

**Experiments** are scientific tools for causal measurement. They split users into variants, track defined metrics, and apply statistical tests to determine whether a variant is better than the control. Experiments have a defined start and end date and produce a ship/no-ship recommendation.

You can combine both: run a feature behind a flag during an experiment, then graduate the flag to 100% after the experiment concludes.

---

## Data and Analytics

### Can I use my own data warehouse?

Yes, if it is Snowflake or BigQuery. Warehouse analysis (beta, full profile) runs an experiment's analysis on your own tables, and each warehouse becomes available once its connector has been checked against a real account: Snowflake and BigQuery are available, and Amazon Athena is not yet. See [Warehouse analysis](../api/warehouse-analytics.md).

---

### How does CUPED variance reduction work?

CUPED (Controlled-experiment Using Pre-Experiment Data) reduces result noise by adjusting each conversion rate for whether users sent the metric's event in the days before they were assigned. The adjustment uses a regression slope (`theta`) pooled within the experiment's arms.

When that history predicts the outcome, lower variance means you reach statistical significance with fewer users, or detect smaller effects with the same sample size. History counts only if the server received it before the user was assigned, so send it before you start the experiment. `GET /api/v1/results/{experiment_id}/cuped` reports it; see [CUPED documentation](../api/cuped.md).

---

### What is Bayesian experimentation vs frequentist?

**Frequentist** testing (the default) computes a p-value: the probability of seeing results as extreme as these if there were no true effect. You make a binary decision: reject the null hypothesis (p < 0.05) or fail to reject it.

**Bayesian** testing maintains a probability distribution over the possible true values of the metric. It gives you:

- A **credible interval**: the range where the true effect likely falls, with a specified probability
- **Probability of superiority**: P(treatment > control), a natural way to communicate results
- A **Bayes factor**: how much more likely the data is under the alternative hypothesis than the null

Bayesian methods are more flexible for continuous monitoring and provide richer output than a binary p-value. Frequentist methods are more familiar, have established reporting conventions, and are often required for regulatory or compliance reporting.

See [Bayesian Experimentation API](../api/bayesian.md) to enable Bayesian analysis on your experiments.

---

### How does Split URL testing work?

Split URL testing sends different visitors to different URLs — for example, `/checkout` vs `/checkout-v2`. Your server or edge asks the API where to send each visitor who opens the control's URL, and redirects them; the API assigns the visitor as `POST /api/v1/tracking/assign` does. Conversions count for the variant the visitor saw when they are tracked with the same visitor id and name the experiment. On AWS, an optional CloudFront edge router (beta) can do the asking and the redirect.

See [Split URL testing](../api/split-url.md) to set it up, with a reference integration in the Python standard library.

---

## Compliance and Security

### What compliance certifications are supported?

On 2026-10-13 the project held no certification. The platform provides audit-trail controls that customers use as evidence in their own **SOC 2** or **ISO/IEC 27001** programs; the `compliance` module adds tamper-evident signing and report packs.

Creating, changing and deleting experiments and feature flags (and, in the full profile, warehouse connections, sources and analysis runs) is recorded in the compliance audit trail. With the `compliance` module each event is signed with HMAC-SHA256 under the `AUDIT_HMAC_KEY` setting; without it events are recorded unsigned. Logins, role changes and exports are not recorded in this trail; logins and role changes are recorded in the other audit log, `GET /api/v1/audit-logs`.

Pre-built compliance reports are available at:
- `GET /api/v1/compliance/reports/soc2` — SOC 2 report, by default over the last 365 days
- `GET /api/v1/compliance/reports/iso27001` — ISO 27001 report, by default over the last 730 days

The compliance trail exports as JSON or CSV at `GET /api/v1/compliance/export` (compliance module; a core deployment answers `501`): flag and experiment creates, changes and deletes, without their old and new values. Toggles, rollbacks, sign-ins and role changes are in the audit log, which exports at `GET /api/v1/audit-logs/export` (beta) in every profile. For a SIEM, take both.

See [Compliance Audit Trail API](../api/compliance.md) for full documentation.

---

### Can I integrate with Jira, Salesforce, or GitHub?

In one direction. The platform stores one configuration for each and receives their webhooks, authenticating every delivery against the integration's webhook secret. Nothing in the platform calls Jira, Salesforce or GitHub yet, and a delivery changes nothing in the platform:

- **Jira**: receives issue events, signed by Jira Cloud or carrying the secret from Jira Server
- **Salesforce**: receives JSON from a Flow HTTP Callout, an Apex callout or a relay
- **GitHub**: receives push, pull_request and issues events, each signed with HMAC-SHA256

Integrations are created by an ADMIN at `POST /api/v1/integrations`, are addressed by their type afterwards (`GET /api/v1/integrations/github`; there is one of each type), and have per-service webhook endpoints at `POST /api/v1/integrations/webhooks/github   (also /jira, /salesforce)`.

See [Integrations API](../api/integrations.md), [Salesforce Integration](../integrations/salesforce.md), and [GitHub Integration](../integrations/github.md) for detailed setup guides.

---

## Access Control

### What roles and permissions are available?

The platform uses a layered role-based access control system with four built-in roles:

| Role | What They Can Do |
|------|-----------------|
| **VIEWER** | Reads every experiment, flag and result and changes none; can create an API key, which writes tracking data |
| **ANALYST** | View all experiments, results, audit logs, and reports |
| **DEVELOPER** | Create and manage experiments and feature flags; read integration settings, but not change them |
| **ADMIN** | Manage experiments, flags, integrations and compliance exports; user management needs a superuser account, which the Admin role alone is not |

Beyond the four base roles, admins can create **custom roles** with specific per-resource and per-action permissions. They can also grant **direct permission** to a user for a specific resource, with an optional expiry timestamp for temporary access. Both add to what the user's base role allows, from their next request; they never remove a permission and never give administration.

See [RBAC API Reference](../api/rbac.md) for the full role and permission management API.

---

### How do API keys work?

API keys are used for SDK authentication and server-to-server calls. Unlike JWT tokens, which sign a user in, API keys do not expire unless they are created with an expiry (`expires_at`). Any active key authenticates every SDK route (flag evaluation, tracking) as the user who created it; its `scopes` do not narrow that. The one exception is the server-side SDK routes (`GET /api/v1/sdk/ruleset`, `POST /api/v1/tracking/evaluations`, `POST /api/v1/tracking/assign/batch`), which require the `sdk:ruleset` scope. A key that carries the `mcp:read` scope can also list feature flags and experiments and read an experiment and its results, as the user who created it.

Create an API key via `POST /api/v1/api-keys` with a name. The full key value is shown only once at creation time — store it securely. To use it, pass the key in the `X-API-Key: <key>` header on all SDK requests.

Keys can be listed (without exposing the secret) and revoked at any time. See [API Key Management](../security/api-keys.md) for best practices.

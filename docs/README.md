# Experimently Documentation

## Start Here

| Guide | Audience | Description |
|-------|----------|-------------|
| [Quick Start](getting-started/quick-start.md) | Everyone | Zero to first experiment in 30 minutes |
| [User Guide](guides/user-guide.md) | Product / Analysts | Designing, running, and interpreting experiments |
| [Technical Guide](architecture/technical-guide.md) | Engineers | Architecture, data models, implementation details |
| [SDK Guide](sdk-guide.md) | Engineers | Endpoint contract for SDKs and per-SDK status |
| [ShopLab Demo](https://github.com/getexperimently/experimently/blob/main/demo/shoplab/README.md) | Everyone | A storefront running five live experiments through the React SDK (`./demo/setup-local.sh`) |
| [Testing Guide](development/testing-guide.md) | Engineers | Writing and running tests |

---

## API Reference

| Doc | Description |
|-----|-------------|
| [Endpoints](api/endpoints.md) | Complete REST API reference |
| [Using the API Docs](api/api-docs-guide.md) | How to read the API documentation the server serves |
| [RBAC](api/rbac.md) | Role-based access control API |
| [Data Export](api/data-export.md) | CSV/JSON streaming export endpoints |

### Advanced Analytics APIs
| Doc | Description |
|-----|-------------|
| [Sequential Testing](api/sequential-testing.md) | mSPRT early stopping, always-valid confidence intervals |
| [CUPED](api/cuped.md) | Variance reduction from each user's own events before assignment |
| [Multi-Armed Bandit](api/multi-armed-bandit.md) | Thompson Sampling, UCB1, Epsilon-Greedy algorithms |
| [Dimensional Analysis](api/dimensional-analysis.md) | Segment breakdowns with Bonferroni correction |
| [Interaction Detection](api/interaction-detection.md) | Cross-experiment user overlap, and (beta) whether two experiments' effects interact |

### Platform Capabilities
| Doc | Description |
|-----|-------------|
| [Mutual Exclusion & Holdout](api/mutual-exclusion-groups.md) | Prevent experiment conflicts, global holdout configuration |
| [Warehouse analysis](api/warehouse-analytics.md) | Beta; Snowflake and BigQuery are available |
| [Audit Logging & Bulk Toggle](api/audit-logging.md) | Audit trail, SSE stream, bulk feature flag operations |
| [Alerting](api/alerting.md) | Slack and email notifications for platform events |

### Developer & Integration Guides
| Doc | Description |
|-----|-------------|
| [AI endpoints and the MCP manifest](mcp-server.md) | Experiment design suggestions, results interpretation, sample sizes and templates over REST, and a manifest that describes them as MCP tools (the platform runs no MCP server) |
| [Guided experiment builder](guides/experiment-wizard.md) | Guided setup in the dashboard, and the separate 5-step wizard API |

---

## SDKs

| SDK | Description |
|-----|-------------|
| [SDK Guide](sdk-guide.md) | Python and JavaScript SDK overview |
| [Java SDK](sdk/java.md) | JVM SDK with Spring Boot auto-configuration, LRU cache, MD5 hashing |
| [React SDK](sdk/react.md) | React hooks, context provider, HOC, SSR/Next.js support |
| [Go SDK](sdk/go.md) | Go SDK with local evaluation, context cancellation, goroutine safety |
| [iOS SDK](sdk/ios.md) | Beta. Swift SDK with async/await, offline fallback, SwiftUI integration; flag evaluation does not send user attributes yet |
| [Android SDK](sdk/android.md) | Beta. Kotlin SDK with Coroutines, OkHttp, Compose integration; flag evaluation does not send user attributes yet |
| [Flutter SDK](sdk/flutter.md) | Beta. Dart SDK for Flutter and plain Dart; flag evaluation does not send user attributes yet |
| [React Native SDK](sdk/react-native.md) | Beta. Provider, hooks and client for React Native; flag evaluation does not send user attributes yet |

---

## Getting Started

- [Quick Start](getting-started/quick-start.md) — full setup in 30 minutes
- [Quick start](getting-started/quick-start.md) — prerequisites (Docker, Python, Node) and first run
- [Environment Setup](getting-started/environment-setup.md) — `.env` configuration
- [Docker Guide](getting-started/docker-guide.md) — local Postgres, Redis, LocalStack
- [Python Virtual Environment](getting-started/python-virtual-env-setup.md)
- [Pre-commit Hooks](getting-started/pre-commit-hooks.md)
- [VSCode Settings](getting-started/vscode-settings.md)

---

## Architecture

- [Overview](architecture/overview.md) — AWS architecture, component diagram
- [Technical Guide](architecture/technical-guide.md) — implementation details, subsystems
- [Models](architecture/models.md) — every model, their relationships, and the migration chain

---

## Authentication

- [Auth Flow Diagrams](auth/flow.md) — registration, login, password reset, token refresh
- [Auth User Guide](auth/auth-user-guide.md) — registration, login, password policies
- [Cognito Testing](auth/cognito-auth-testing.md) — testing auth locally
- [SSO](auth/sso.md) — OIDC setup for Okta, Google and GitHub (the `sso` module). SAML 2.0 sign-in is not available yet; use OIDC

---

## Development

- [Guidelines](development/guidelines.md) — coding standards, git workflow, TDD
- [Testing Guide](development/testing-guide.md) — unit, integration, E2E, contract tests
- [Dependency Injection](development/dependency-injection.md) — FastAPI deps.py patterns
- [Workflow Explanation](development/workflow-explanation.md) — GitHub Actions CI
- [Database Migrations](development/database/migrations.md) — Alembic workflow
- [Database Usage](development/database/usage.md) — Aurora, DynamoDB, Redis patterns
- [Database Backup](development/database/backup.md) — backup and recovery procedures

---

## Deployment & Operations

- [Deployment Guide](deployment/deployment-guide.md) — ECS Fargate, Blue/Green deployment
- [Rollback Runbook](deployment/rollback-runbook.md) — rollback decision tree and procedures
- [Disaster Recovery](deployment/disaster-recovery.md) — RTO/RPO, failure scenarios
- [Secrets Management](deployment/secrets-management.md) — AWS Secrets Manager, rotation

---

## Security

- [Hardening Changes](security/hardening-changes.md) — WAF, headers, rate limiting
- [Incident Response](security/incident-response.md) — reporting a vulnerability, and rolling back your deployment

---

## Monitoring

- [Monitoring](self-hosting/monitoring.md) — CloudWatch dashboards, Prometheus metrics
- [Logging Standards](logging_standards.md) — logging conventions

---

## Infrastructure

- [DynamoDB](infrastructure/dynamodb-readme.md) — table schemas, access patterns
- [CloudWatch Setup](infrastructure/cloudwatch-setup.md) — log groups, agent config
- [IAM Setup](infrastructure/aws-iam-setup.md) — roles and permissions
- [Network Security](infrastructure/network-security-readme.md) — subnets, NAT, endpoints, security groups, ACLs
- [VPC Stack](infrastructure/vpc-stack-implementation.md) — CDK networking stack

---

## Profiles

- [Modules](getting-started/modules.md) — the core and full profiles, and what each module adds

The platform is fully open source under Apache-2.0, with the SDKs MIT. Both
build profiles are the same licence and the same price. An earlier commercial
strategy was superseded by that decision on 2026-09-12; the records of it are
internal and are not published.

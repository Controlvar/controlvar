# Experimently

> **Open-source experimentation platform for A/B testing and feature flags — Apache-2.0, all of it**

A production-ready, scalable platform for A/B testing and feature management with real-time evaluation, automated safety monitoring, and comprehensive analytics. Every line in this repository is open source: the core, the optional modules and the SDKs.

---

## 🎯 Overview

An experimentation platform that enables teams to make data-driven decisions through robust A/B testing and feature flag management. Built on AWS, self-hosted in your own account.

### Key Capabilities

- **A/B Testing & Multivariate Experiments**: Frequentist, sequential (mSPRT) and Bayesian analysis
  in the API; [which of them the dashboard shows](docs/guides/dashboard-and-api.md)
- **Advanced Feature Flags**: Targeting, gradual rollouts, and automated safety monitoring
- **Enhanced Rules Engine**: 20+ operators including semantic versioning, geo-distance, time windows
- **Real-time Analytics**: High-throughput event collection and comprehensive metrics
- **RBAC**: Role-based access control with local or AWS Cognito authentication; custom roles with the `rbac` module (recorded, but they do not change access yet: [#891](https://github.com/getexperimently/experimently/issues/891))
- **Audit Trail**: creating, changing and deleting flags and experiments, and flag toggles, are
  recorded in every profile; the full profile adds warehouse changes, HMAC-SHA256 signing, SOC 2 /
  ISO 27001 reports and a JSON or CSV export for your compliance program
- **Automated Safety**: Real-time monitoring with automatic rollback capabilities

### Measured, not marketed

Numbers in this README come from the repository's own test and benchmark suites, not from a
production deployment we cannot show you:

- **Rules engine**: >100k simple-operator evaluations/second on a quiet
  developer machine — the floor `backend/tests/unit/core/test_performance_benchmarks.py`
  asserts. It is **skipped in CI** (`skipif CI=true`): shared runners are
  several times slower and made it flaky, so the number is reproducible
  locally, not enforced on every merge
- **Statistical engine**: sequential testing (mSPRT), CUPED, Bayesian and multi-armed bandits, each
  with a DB-backed test that drives the public API. CUPED adjusts for each user's own events
  before assignment ([CUPED](docs/api/cuped.md))
- **The backend and dashboard test suites** run in `pr-qa-gate.yml` on every pull request that
  changes code, and 13 of the 16 SDKs run against a live backend in
  CI (the `SDK Live Contract` job of `pr-qa-gate.yml`); Flutter, Elixir
  and iOS are covered by their unit tests, nightly and whenever they change

---

## 🎬 See it running

Two demo applications ship in the repo and drive the platform through the public SDK path, exactly as
a customer app would. `./demo/setup-local.sh` starts everything.

- **ShopLab** (`demo/shoplab`, http://localhost:3200): an e-commerce storefront running an A/B test,
  a multi-armed bandit, a multivariate test, a CUPED checkout test and a gradual-rollout flag.
- **StreamPulse** (`demo/streampulse`, http://localhost:3300): a simulated mobile app with a device
  picker, targeting by OS version/region/tier, a kill switch, mutual exclusion and a scripted rollout
  story in which a crash spike triggers an automatic safety rollback.

Walkthrough: [demo/DEMO_GUIDE.md](demo/DEMO_GUIDE.md).

---

## 🏗️ Architecture

Built using modern, scalable architecture leveraging AWS services:

- **Frontend**: Next.js React application with TypeScript
- **Backend**: FastAPI Python services (ECS/Fargate, auto-scaling)
- **Request path**: the API serves assignment, flag evaluation and event tracking; no Lambda function is on it
- **Data**: Aurora PostgreSQL Multi-AZ, ElastiCache Redis, DynamoDB
- **Analytics**: events are stored in PostgreSQL and the API computes results from them
- **Infrastructure**: AWS CDK for infrastructure as code

**Technology Stack**: Python 3.11+, FastAPI, SQLAlchemy, Pydantic v2, Next.js, React, TypeScript, PostgreSQL, Redis

[View Architecture Details →](docs/architecture/overview.md)

---

## ✨ Key Features

### Advanced Targeting & Rules Engine
- **20+ Operators**: Basic (equals, in), String (regex, contains), Advanced (semver, geo-distance, time windows, JSON path)
- **Throughput floors** (`test_performance_benchmarks.py`, run locally —
  skipped in CI, see above): simple equality >100k/sec, string contains
  >20k/sec, semver >1k/sec, time windows >1k/sec, geo-distance >500/sec
- **Smart Caching**: Multi-layer caching — compiled rules (LRU) and evaluation
  results (LRU with TTL)
- **Batch Processing**: Efficient evaluation of 1000+ users

### Automated Safety Monitoring
- **Real-time Metrics**: Error rate, latency, custom metrics tracking
- **Auto-Rollback**: Automatic rollback on threshold violations
- **Configurable Thresholds**: Per-feature flag safety configuration
- **Complete Audit Trail**: All rollbacks logged with reasoning

### Statistical Rigor
- **Bayesian & Frequentist**: Dual statistical approaches
- **Sequential Testing**: Early stopping with O'Brien-Fleming bounds
- **Multiple-comparison correction**: Benjamini-Hochberg by default, or Bonferroni, across each experiment's variants; stored with the experiment alongside its confidence level
- **Sample Size Calculations**: Automatic power analysis

### Security controls
- **Audit trail**: append-only log of every change (the `compliance` module adds HMAC signing and
  report packs to support a SOC 2 or ISO 27001 program)
- **Data controls**: retention settings and export endpoints (right-to-erasure tooling is on the roadmap)
- **Complete Audit Logs**: record of all actions
- **RBAC**: 4-tier role system (Admin, Developer, Analyst, Viewer)
- **Encryption**: At rest (KMS) and in transit (HTTPS at the load balancer)

---

## 🚀 Getting Started

### Run the whole thing

You need Docker with Compose v2, `curl` and `jq`. Clone the repository:

```{.bash skip reason="checkout: the runner starts inside a checkout"}
git clone https://github.com/getexperimently/experimently.git
cd experimently
```

Then start the stack from the repository root:

```{.bash exec timeout=1200}
docker compose up -d --wait
```

Check that you can sign in:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

Open **http://localhost:3000** and sign in with **admin@demo.com / Demo1234!**.
The API is on http://localhost:8000, its docs at http://localhost:8000/api/v1/docs.
Four containers come up — Postgres, Redis, the API and the dashboard — and the
database is created and seeded on the way.

The first start builds both images from source, so it is the slow one:
**measured at 2 m 32 s** on an 8-core Docker with a cold build cache (115 s
build, 37 s to healthy), plus roughly a minute of base-image pulls on a machine
that has never run it. Afterwards `docker compose up -d` takes seconds, and
`docker compose down -v` removes it all again, volumes included.

Change `FIRST_SUPERUSER_PASSWORD`, `SECRET_KEY` and `POSTGRES_PASSWORD` in a
`.env` file before letting anyone else reach it.
[**Quick start**](docs/getting-started/quick-start.md) has the rest: the
service table, the verification commands, and the `demo`, `tools` and `aws`
profiles.

### For Evaluation

Interested in using this platform for your organization?

1. **Run the demos**: see [demo/DEMO_GUIDE.md](demo/DEMO_GUIDE.md)
2. **Run the demo**: `./demo/setup-local.sh` starts Postgres and Redis, seeds demo data, and launches the
   dashboard (http://localhost:3100), the API (http://localhost:8000) and the **ShopLab** storefront
   (http://localhost:3200) — a small e-commerce site running five live experiments through the React SDK,
   with a traffic simulator keeping the dashboards moving. Walkthrough: [demo/DEMO_GUIDE.md](demo/DEMO_GUIDE.md)
3. **Review the Documentation**: Check the [docs](docs/) directory for comprehensive guides
4. **Run Locally**: Follow the setup instructions in CONTRIBUTING.md for local development

### For Deployment

Refer to the documentation for:
- Deployment configuration
- Infrastructure setup with AWS CDK
- SDK integration guides
- Configuration options

---

## 📖 Documentation

- **[Architecture Overview](docs/architecture/overview.md)**: System design
- **[Targeting rules](docs/feature-flags/create.md)**: operators, attribute aliases, examples
- **[Contributing](CONTRIBUTING.md)**: development setup, the test suites, the core/modules boundary

---

## 🔧 Technology Highlights

### Backend Services
- **FastAPI Application**: Comprehensive REST API, with the unit, integration,
  smoke and module suites required to pass on every pull request — see the
  checks on any PR for what actually ran and what it covered
- **Background Schedulers**: Automated experiment lifecycle, rollout management, metrics aggregation
- **Enhanced Rules Engine**: Advanced targeting with 20+ operators

### Database & Caching
- **Aurora PostgreSQL**: Multi-AZ with automatic failover
- **ElastiCache Redis**: Session and evaluation caching
- **Data Retention**: Configurable policies (90 days to 7 years)

### Lambda, Kinesis and OpenSearch
- **Lambda code**: event processing and flag evaluation under `backend/lambda/`, not deployed by any stack (assignment and evaluation run in the API)
- **Kinesis and OpenSearch**: created by the full profile's analytics stack; the API writes nothing to the stream and nothing writes to the domain

---

## 🛡️ Security

- **Controls that support your compliance program**: append-only audit log, role-based access,
  per-flag safety monitoring with automatic rollback; the `compliance` and `hipaa` modules add
  tamper-evident audit signing, compliance report packs and PHI encryption. We do not hold SOC 2,
  ISO 27001 or HIPAA attestations and do not claim them.
- **Encryption**: AES-256 at rest (KMS) and HTTPS in transit at the load balancer when deployed with the provided CDK
- **Network Security**: VPC isolation, security groups, and an HTTPS-only load balancer (HTTP redirects to HTTPS) in front of the API and the dashboard (CDK deployment). The CDK does not deploy a WAF or CloudFront

---

## 💼 Use Cases

### Product Teams
- A/B test new features and UI changes
- Gradual rollouts with safety monitoring
- Data-driven product decisions

### Engineering Teams
- Feature flags for deployment control
- Canary deployments and kill switches
- Configuration management

### Data Science Teams
- Statistical experiment analysis
- Metric tracking and attribution
- Segmentation analysis

### Compliance Teams
- Complete audit trails
- Role-based access control
- Retention policy management

---

## 📞 Support

For questions and issues:
- Check the [documentation](docs/) directory
- Review [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines
- Run the demos (`./demo/setup-local.sh`)

---

## 🧩 Profiles and modules

One codebase, two profiles. The **core profile** is `backend/` and `frontend/`: experiments and
feature flags end to end, targeting with 20+ operators, gradual rollouts, safety monitoring with
automatic rollback, scheduling, the full statistics (frequentist and Bayesian, sequential testing,
CUPED, multi-armed bandits, mutual exclusion groups and global holdouts, dimensional
breakdowns, experiment overlap and interaction detection (beta),
live results), the four built-in roles, audit logging, API keys, alerting
and every SDK. The **full profile** adds the optional modules under `modules/`, which plug into
the core through the registration hooks in `backend/app/core/`:

| Module | What it adds |
|---|---|
| `workspaces` | Group users into teams, with workspace roles and email invites. Grouping only: access to experiments and flags is by platform role |
| `rbac` | Roles beyond the built-in four, and permissions granted directly to a user. Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides ([#891](https://github.com/getexperimently/experimently/issues/891)) |
| `sso` | OIDC identity providers with just-in-time provisioning and role mapping. SAML 2.0 sign-in is not available yet; use OIDC |
| `hipaa` | PHI encryption, six-year PHI audit retention, BAA records |
| `compliance` | SOC 2 / ISO 27001 reports, signed audit exports |
| `warehouse` | Warehouse analysis (beta): run an experiment's analysis on tables in your own warehouse, under `/api/v1/warehouse/analysis` (connections, sources, runs). Snowflake and BigQuery are available; `GET /api/v1/warehouse/analysis/connectors` lists which connectors are. See [docs/api/warehouse-analytics.md](docs/api/warehouse-analytics.md) |
| `integrations` | Jira, Salesforce and GitHub |
| `counters` | DynamoDB-backed live assignment and conversion counters |
| `etl` | Glue crawlers, Athena partitions and scheduled jobs |
| `split_url` | Server-side URL splitting at the edge. The edge router has no configuration source in this release, so it does not split traffic yet; experiments, their URL configuration and the preview work ([#393](https://github.com/getexperimently/experimently/issues/393)) |

`GET /api/v1/modules` reports the running profile and the installed modules — ask it first, rather
than inferring the profile from a status code. A core deployment is the repository with `modules/`
deleted (`make core-build` proves it builds, boots and passes its tests that way), so a module's
routes are not mounted and its URLs **404** like any other unknown path. Three URLs are declared by
a core router but implemented by a module — `/api/v1/compliance/reports/{standard}`,
`/api/v1/compliance/export` and `/api/v1/experiments/{id}/split-url/preview` — and those answer
**501**, as does creating an experiment with `experiment_type=split_url`: the route exists in both
profiles, so it says "not installed" rather than leaving you to wonder about a typo. Both profiles
are the same licence and the same price: none. Details in
[docs/getting-started/modules.md](docs/getting-started/modules.md).

---

## 📄 License

| Part of the repository | Licence |
|---|---|
| Everything except `sdk/` — the core, the modules, the dashboard, the docs | [Apache-2.0](LICENSE) |
| `sdk/` (all client SDKs and OpenFeature providers) | [MIT](sdk/LICENSE) |

Third-party dependency licences are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md);
attribution is in [NOTICE](NOTICE). Security reports go to [SECURITY.md](SECURITY.md), not to
public issues. Contributions are accepted under the DCO — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 🌟 Key Differentiators

| Feature | This Platform | Typical Alternatives |
|---------|---------------|---------------------|
| **Advanced Targeting** | 20+ operators (semver, geo, time, JSON path) | Basic operators only |
| **Safety Monitoring** | Automated rollback with configurable thresholds | Manual monitoring |
| **Statistical Methods** | Bayesian + Frequentist analysis | Single method |
| **Audit Logging** | Append-only trail; tamper-evident signing with the `compliance` module | Limited or none |
| **Rules evaluation** | >100k simple evaluations/sec locally (`test_performance_benchmarks.py`; skipped on CI runners) | Varies widely |
| **RBAC** | 4 roles; local auth or AWS Cognito | Basic or none |
| **Deployment** | Self-hosted on your AWS account | SaaS only |
| **Customization** | Full platform access | Limited APIs |

---

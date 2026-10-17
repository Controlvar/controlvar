# Controlvar

> **Open-source A/B testing and feature flags that you run yourself.**

The core, the optional modules and the dashboard are Apache-2.0; the client SDKs under `sdk/`
are MIT. Controlvar is pre-1.0: see the
[releases](https://github.com/Controlvar/controlvar/releases).
Controlvar was called Experimently before 0.35.0;
[Upgrading to Controlvar](docs/self-hosting/upgrading-to-controlvar.md) says what
that changes for an install you already run.

---

## Try it on your machine

You need Docker with Compose v2, `curl` and `jq`. Clone the repository:

```{.bash skip reason="checkout: the runner starts inside a checkout"}
git clone https://github.com/Controlvar/controlvar.git
cd controlvar
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

The first start builds the API and dashboard images from source.
`docker compose down -v` removes it all again, volumes included.

This stack is for your own machine. It runs with development settings and publishes its ports
on `127.0.0.1` only, and the demo account's password is on this page. To run Controlvar
where others can reach it, see [Self-host it](#self-host-it).
[**Quick start**](docs/getting-started/quick-start.md) has the rest: the
service table, the verification commands, and the `demo`, `tools` and `aws`
profiles.

---

## See it in an app

Two demo applications ship in the repo and drive the platform through the public SDK path, exactly as
a customer app would. `./demo/setup-local.sh` starts everything.

- **ShopLab** (`demo/shoplab`, http://localhost:3200): an e-commerce storefront running an A/B test,
  a multi-armed bandit, a multivariate test, a CUPED checkout test and a gradual-rollout flag.
- **StreamPulse** (`demo/streampulse`, http://localhost:3300): a simulated mobile app with a device
  picker, targeting by OS version/region/tier, a kill switch, mutual exclusion and a scripted rollout
  story in which a crash spike triggers an automatic safety rollback.

Walkthrough: [demo/DEMO_GUIDE.md](demo/DEMO_GUIDE.md).

---

## What it does

- **A/B Testing & Multivariate Experiments**: Frequentist, sequential (mSPRT) and Bayesian analysis
  in the API; [which of them the dashboard shows](docs/guides/dashboard-and-api.md)
- **Sequential testing**: mSPRT with always-valid confidence intervals, so you can read results at
  any time. There are no O'Brien-Fleming or Pocock boundaries.
- **Multiple-comparison correction**: Benjamini-Hochberg by default, or Bonferroni, across each experiment's variants; stored with the experiment alongside its confidence level
- **Sample Size Calculations**: Automatic power analysis
- **Results**: events are stored in PostgreSQL and the API computes results from them
- **Advanced Feature Flags**: Targeting, gradual rollouts, and automated safety monitoring
- **Enhanced Rules Engine**: 20+ operators including semantic versioning, geo-distance, time windows
- **RBAC**: 4-tier role system (Admin, Developer, Analyst, Viewer)
- **Audit Trail**: creating, changing and deleting flags and experiments, and flag toggles, are
  recorded in every profile; the full profile adds warehouse changes, HMAC-SHA256 signing, SOC 2 /
  ISO 27001 reports and a JSON or CSV export for your compliance program
- **Automated Safety**: monitoring every 5 minutes by default, with automatic rollback capabilities

### Automated Safety Monitoring
- **Real-time Metrics**: Error rate, latency, custom metrics tracking
- **Auto-Rollback**: Automatic rollback on threshold violations
- **Configurable Thresholds**: Per-feature flag safety configuration
- **Complete Audit Trail**: All rollbacks logged with reasoning

---

## What it does not do (yet)

- A flag is on or off for each user: it carries no variants or values. To serve one of several
  experiences, run a multivariate experiment.
- Every metric is analysed as a conversion: whether each user sent the metric's event.
- Variant weights must add up to exactly 100%; there is no separate experiment-level traffic
  allocation.
- One deployment is one environment: run staging and production as separate deployments, each
  with its own API keys.
- Sign-in has no multi-factor step, and SAML sign-in is not available (OIDC is, with the `sso`
  module).
- Nothing deletes events, assignments or audit records by age, and no tool erases one person's
  data. The platform's own analysis history is purged after `ANALYSIS_HISTORY_RETENTION_DAYS`
  (90 days by default).

[Limitations](docs/getting-started/limitations.md) lists these and the rest, with what happens
instead.

---

## Self-host it

| Option | What you run | What CI runs |
|---|---|---|
| Docker Compose on one host | [`deploy/compose/compose.yml`](deploy/compose/compose.yml), on the published images | starts it and checks the API answers |
| Helm on Kubernetes | [`charts/experimently`](charts/experimently) | installs it on a kind cluster |
| AWS CDK | [`infrastructure/cdk`](infrastructure/cdk) | synthesises every stack; no CI job deploys it |

[AWS CDK Deployment](docs/self-hosting/cdk.md) and [Kubernetes](docs/self-hosting/kubernetes.md)
have the steps.

---

## SDKs

There are 16 SDKs under [`sdk/`](sdk/), and 13 of them run assignment, flag evaluation and event
tracking against a live API in CI (the SDK Live Contract job of `pr-qa-gate.yml`).

| SDK | Install |
|---|---|
| JavaScript | `npm install @getexperimently/js-sdk` |
| Python | `pip install experimently` (it imports as `experimentation`) |
| Go | `go get github.com/getexperimently/experimently/sdk/go` |

Every other SDK installs from a clone of this repository. The [SDK guide](docs/sdk-guide.md) says
how, and what CI runs for each one.

---

## Documentation

- [Documentation home](docs/README.md)
- [Architecture overview](docs/getting-started/architecture.md)
- [Targeting rules](docs/feature-flags/create.md): operators, attribute aliases and examples

---

## 🛡️ Security

- **Controls that support your compliance program**: append-only audit log, role-based access,
  per-flag safety monitoring with automatic rollback; the `compliance` and `hipaa` modules add
  tamper-evident audit signing, compliance report packs and PHI encryption. On 2026-10-13 the
  project held no SOC 2, ISO 27001 or HIPAA attestation, and it claims none.
- **Passwords and API keys**: stored hashed; an API key is shown once, when it is created
- **API keys**: any signed-in user can create one for themselves, and a key acts as its creator
  on the tracking and flag-evaluation routes, so a VIEWER's key can send assignments and events
- **Encryption**: AES-256 at rest (KMS) and HTTPS in transit at the load balancer when deployed with the provided CDK
- **Network Security**: VPC isolation, security groups, and an HTTPS-only load balancer (HTTP redirects to HTTPS) in front of the API and the dashboard (CDK deployment). The CDK does not deploy a WAF or CloudFront

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
| `rbac` | Roles beyond the built-in four, and permissions granted directly to a user. Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)) |
| `sso` | OIDC identity providers with just-in-time provisioning and role mapping. SAML 2.0 sign-in is not available yet; use OIDC |
| `hipaa` | PHI encryption, PHI access records stamped with a six-year retention (nothing deletes them), BAA records |
| `compliance` | SOC 2 / ISO 27001 reports, signed audit exports |
| `warehouse` | Warehouse analysis (beta): run an experiment's analysis on tables in your own warehouse, under `/api/v1/warehouse/analysis` (connections, sources, runs). Snowflake and BigQuery are available; `GET /api/v1/warehouse/analysis/connectors` lists which connectors are. See [docs/api/warehouse-analytics.md](docs/api/warehouse-analytics.md) |
| `integrations` | Jira, Salesforce and GitHub |
| `counters` | Routes to increment and read assignment and conversion counters in DynamoDB; assignment and tracking do not write them |
| `etl` | Glue crawlers, Athena partitions and scheduled jobs |
| `split_url` | Server-side URL splitting at the edge. The edge router has no configuration source in this release, so it does not split traffic yet; experiments, their URL configuration and the preview work ([#393](https://github.com/Controlvar/controlvar/issues/393)) |

`GET /api/v1/modules` reports the running profile and the installed modules — ask it first, rather
than inferring the profile from a status code. A core deployment is the repository with `modules/`
deleted (`make core-build` proves it builds, boots and passes its tests that way), so a module's
routes are not mounted and its URLs **404** like any other unknown path. Three URLs are declared by
a core router but implemented by a module — `/api/v1/compliance/reports/{standard}`,
`/api/v1/compliance/export` and `/api/v1/experiments/{id}/split-url/preview` — and those answer
**501**, as does creating an experiment with `experiment_type=split_url`: the route exists in both
profiles, so it says "not installed" rather than leaving you to wonder about a typo.
The two profiles are under the same licence. Details in
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

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers the development setup, the test suites and the
core/modules boundary.

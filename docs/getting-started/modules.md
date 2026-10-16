# Modules and profiles

Controlvar is one codebase, open source (Apache-2.0, with the client SDKs
MIT), and it runs in one of two **profiles**:

- **core** — the product without the optional modules. This is what a
  checkout without a `modules/` directory runs, and what the `core` container
  images ship.
- **full** — the product with the optional **modules** installed. This is what
  a complete checkout runs, and what the `full` container images ship.

There is nothing else to choose: no tier, no key, no state that expires. The
modules are part of the same repository, under the same licence, and every
one of them is on by default in the full profile. The only question a
deployment answers is whether the `modules/` package is present.

`GET /api/v1/modules` reports the answer, unauthenticated, with exactly three
keys:

```{.bash exec}
curl -s http://localhost:8000/api/v1/modules
```
<!-- expect: "profile":"full" -->
<!-- expect: "rbac" -->

A full deployment prints something like:

```json
{"profile": "full", "modules": ["workspaces", "hipaa", "compliance", "sso", "rbac", "warehouse", "integrations", "counters", "etl", "split_url"], "version": "1.0.0"}
```

A core deployment answers `{"profile": "core", "modules": [], "version": "..."}`.

## The core profile

The core profile includes experiments and feature flags end to
end — targeting rules with 20+ operators, gradual rollouts, safety monitoring
with automatic rollback, scheduling — and the full statistics: frequentist and
Bayesian analysis, sequential testing with early stopping, CUPED variance
reduction, multi-armed bandits, mutual exclusion groups and global holdouts,
dimensional breakdowns, interaction detection, and live results over
WebSocket. The four built-in roles (Admin, Developer, Analyst, Viewer), audit
logging, API keys, alerting and every SDK are core too.

## The modules

The modules are the things an organisation needs once more than one team
shares an instance, or once a compliance regime or an external system is
involved. Each has a name, which is what `GET /api/v1/modules` lists and what
the dashboard checks:

| Module | Name | What it adds |
| --- | --- | --- |
| Team workspaces | `workspaces` | Group users into teams, with workspace roles and email invites. Grouping only: access to experiments and flags is by platform role |
| Custom roles | `rbac` | Roles beyond the built-in four, and permissions granted directly to a user. Both are recorded, but they do not change what anyone can do yet: each user's built-in role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)) |
| SSO / SAML / OIDC | `sso` | OIDC identity providers with just-in-time provisioning and role mapping. SAML 2.0 sign-in is not available yet; use OIDC |
| HIPAA | `hipaa` | PHI encryption, PHI access records stamped with a six-year retention (nothing deletes them), BAA records |
| Compliance reporting | `compliance` | SOC 2 / ISO 27001 reports, signed audit exports |
| Warehouse analytics | `warehouse` | Warehouse analysis (beta): run an experiment's analysis on tables in your own warehouse, under `/api/v1/warehouse/analysis`. Snowflake and BigQuery are available ([Warehouse analytics](../api/warehouse-analytics.md)) |
| Third-party integrations | `integrations` | Jira, Salesforce and GitHub |
| Counters | `counters` | Routes to increment and read assignment and conversion counters in DynamoDB; assignment and tracking do not write them |
| ETL | `etl` | Glue crawlers, Athena partitions and scheduled jobs |
| Split URL testing | `split_url` | Server-side URL splitting at the edge. The edge router has no configuration source in this release, so it does not split traffic yet; experiments, their URL configuration and the preview work ([#393](https://github.com/Controlvar/controlvar/issues/393)) |

The split is along "one team running the product" versus "an organisation
administering it".

## What the core profile leaves out

A core deployment does not carry the modules' code at all, so:

- The modules' API routes are not mounted, so their URLs **404** like any other
  unknown path. That is what "not installed" looks like almost everywhere:
  `/api/v1/workspaces/`, `/api/v1/rbac/roles`, `/api/v1/hipaa/*`,
  `/api/v1/sso/*`, `/api/v1/integrations/*` and the rest simply are not there.
- Three URLs are the exception, because a *core* router declares them while a
  module implements them — `/api/v1/compliance/reports/{standard}`,
  `/api/v1/compliance/export` and
  `/api/v1/experiments/{id}/split-url/preview`. Those answer **HTTP 501 Not
  Implemented**: the route exists in both profiles, so it says "not installed"
  rather than leaving you to wonder about a typo.
- Creating an experiment with `experiment_type=split_url` is refused with 501
  for the same reason, so a core instance cannot store an experiment nothing
  routes.
- Do not infer the profile from a status code. `GET /api/v1/modules` reports
  the running profile and the installed modules; ask it.
- The dashboard's module pages (`/admin/roles`, `/workspaces/**`) render a
  notice saying the module is not installed in this deployment, with a link
  back here, instead of a 404. The admin sidebar and the "More" navigation
  group list only the pages the instance can serve.
- The fourteen module tables (`workspaces`, `workspace_members`,
  `workspace_invites`, `workspace_api_keys`, `sso_configs`, `custom_roles`,
  `user_custom_roles`, `direct_permission_grants`, `baa_configs`,
  `phi_audit_logs`, `warehouse_connections`, `warehouse_sources`,
  `warehouse_analysis_runs`, `integration_configs`) are not created.

Everything else — experiments, flags, assignment, tracking, results — is
identical in both profiles.

## Running the full profile

The backend runs the full profile whenever the `modules` package is
importable: a complete checkout has it, so `uvicorn backend.app.main:app` from
the repository root is already the full profile. The core profile is a tree
with no `modules/` directory; `scripts/core_build.sh` produces one from a
checkout and proves it builds, boots and passes its tests. `GET
/api/v1/modules` reports which one is running — nothing in the backend reads
`EXPERIMENTLY_PROFILE`, so that variable does not select a backend profile;
the presence of `modules/` does.

The modules have dependencies of their own, in `modules/requirements.txt`
(locked in `modules/requirements.lock`): the SAML and OIDC libraries, and the
SQL parser the warehouse module checks every query with. `backend/requirements.txt`
deliberately carries none of them. A full profile without them starts and serves
every route, and then refuses every warehouse query — install them alongside the
backend pins:

```{.bash skip reason="dev: installs the backend's and the modules' Python dependencies into a development checkout"}
pip install -r backend/requirements.txt -r modules/requirements.txt
```

The dashboard follows `EXPERIMENTLY_PROFILE`. A plain `npm run build` is the full
profile, where `@modules/*` resolves to `../modules/frontend/src`; with
`EXPERIMENTLY_PROFILE=core` it resolves to `src/modules-stub` instead:

```{.bash skip reason="dev: builds the dashboard in a development checkout, once per profile"}
cd frontend
npm run build
EXPERIMENTLY_PROFILE=core npm run build
```

`EXPERIMENTLY_PROFILE=core` forces the core resolution on a complete
checkout, which is how the seam is tested without deleting anything:
`EXPERIMENTLY_PROFILE=core npm test`, `EXPERIMENTLY_PROFILE=core npm run build`
and `npx tsc --noEmit -p tsconfig.core.json` (from `frontend/`).

The container images select the profile by build target and build argument.

For the API, the `core` target is the default. `full` additionally copies
`modules/backend/` and installs `modules/requirements.lock`, so it needs a
complete checkout: built from a core tree it does not fail, it produces an
image with an empty `/app/modules`, which `abort_if_modules_broken()` refuses
to start outside development:

```{.bash skip reason="dev: builds both API images from a development checkout, several minutes each"}
docker build -f backend/Dockerfile -t experimently-api:core .
docker build -f backend/Dockerfile --target full -t experimently-api:full .
```

For the dashboard, `EXPERIMENTLY_PROFILE=core` is the default, like the API's
`core` target, so a plain `docker build` of either produces a matching pair.
`full` copies `modules/frontend/`, so it too needs a complete checkout: built
from a core tree the build stops at its own profile assertion:

```{.bash skip reason="dev: builds both dashboard images from a development checkout, several minutes each"}
docker build -f frontend/Dockerfile -t experimently-web:core .
docker build -f frontend/Dockerfile -t experimently-web:full --build-arg EXPERIMENTLY_PROFILE=full .
```

The full dashboard build fails if `modules/frontend/` is missing; the core
build never uses it, so it works from a checkout that has no `modules/`
directory at all — under a classic builder (`DOCKER_BUILDKIT=0`, Compose v1,
Kaniko), which builds every stage rather than only the ones the profile
selects, as well as under BuildKit. Both images refuse to carry a bundle that
does not match the profile (a core image carries no chunk naming a module
route; a full image carries them).

`docker compose` takes the same variable for both images, and without it starts
the core profile. This starts the full one and waits until every service is
healthy:

```{.bash exec timeout=1200}
EXPERIMENTLY_PROFILE=full docker compose up -d --wait
```

## Where the code lives

The split is by directory. Everything under `modules/` is the optional part;
everything else is core. `modules-manifest.txt` at the repository root lists
every module path, one group per module, and a core build is the repository
with those paths deleted — it must build, boot and pass its tests that way,
and `backend/tests/smoke/test_core_boundary.py` fails on any core file that
imports a module.

For the dashboard that means `modules/frontend/src/`, which mirrors
`frontend/src/` (`pages/`, `components/`, `services/`, `contexts/`, `tests/`)
and holds the module pages (`/admin/roles`, `/sso/complete`, `/warehouse/**`,
`/workspaces/**`), the
custom-roles components and the workspace service. The core tree reaches it
only through the `@modules/*` alias, defined once in
`frontend/modules-alias.js` and applied to webpack, jest and TypeScript alike:

- With `modules/frontend/src/` present, `@modules/x` is
  `modules/frontend/src/x`.
- Without it — a core checkout — `@modules/x` is
  `frontend/src/modules-stub/x`, whose pages render the "module not
  installed" notice. The route files under `frontend/src/pages/` are one-line
  re-exports and are the same in both profiles.

Contributions to `modules/` are welcome exactly like contributions anywhere
else in the repository; see `CONTRIBUTING.md`.

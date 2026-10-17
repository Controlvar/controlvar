# The Docker Compose stack

The repository ships one development compose file, `docker-compose.yml` at the repository root.
This page lists its services, profiles and the settings you are most likely to change.
Every host port below is published on `127.0.0.1` only. For a server, use
[`deploy/compose/compose.yml`](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/compose.yml):
the [README](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/README.md)
beside it says how to run it and lists its settings; see
[Releases, images and verification](../self-hosting/releases.md) for the versions and
how to verify the images.

## Services

| Service | Image | Host port | Profile | Notes |
|---------|-------|-----------|---------|-------|
| `postgres` | `postgres:16-alpine` | 5432 | default | data in the `postgres_data` volume |
| `redis` | `redis:7-alpine` | 6379 | default | caching and rate-limit counters |
| `api` | `experimently-api:core` (built from `backend/Dockerfile`; `EXPERIMENTLY_PROFILE=full` builds the `full` target with the modules) | 8000 | default | runs bootstrap and seeds on first start, then uvicorn |
| `frontend` | `experimently-web:core` (built from `frontend/Dockerfile`; `full` likewise) | 3000 | default | nginx serving the static dashboard and proxying `/api` to `api` |
| `shoplab` | built from `demo/shoplab` | 3200 | `demo` | sample e-commerce app |
| `shoplab-simulator` | `python:3.11-slim`, running `demo/shoplab/simulator` | none | `demo` | ShopLab's traffic generator |
| `streampulse` | built from `demo/streampulse` | 3300 | `demo` | sample streaming app |
| `streampulse-simulator` | `python:3.11-slim`, running `demo/streampulse/simulator` | none | `demo` | StreamPulse's device simulator |
| `pgadmin` | `dpage/pgadmin4` | 5050 | `tools` | database browser |
| `localstack` | `localstack/localstack:3` | 4566 | `aws` | only for developing the optional AWS integrations |

Start the core stack, the four default services:

```{.bash exec timeout=1200}
docker compose up -d --wait
```

Check that those four are the ones running:

```{.bash exec}
docker compose ps --services --status running | sort
```
<!-- expect: api -->
<!-- expect: frontend -->
<!-- expect: postgres -->
<!-- expect: redis -->

It prints `api`, `frontend`, `postgres` and `redis`, one per line.

The `demo` profile adds ShopLab, StreamPulse and their traffic simulators. Start it
with the seeds the apps need:

```{.bash skip reason="demo: builds both demo app images and starts their simulators"}
SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait
```

`make demo` runs the same command with `--build`. Without the `shoplab` and
`streampulse` seeds the API key built into the apps does not exist: they load, but
every flag shows its fallback, and both simulators exit at start. If you already started the profile without them, run the
command above once: it applies the missing seeds to the existing database, with no
`down -v`. To run the apps outside Docker, use `./demo/setup-local.sh`, which the
[Demo guide](https://github.com/Controlvar/controlvar/blob/main/demo/DEMO_GUIDE.md)
walks through.

Add pgAdmin and LocalStack with the `tools` and `aws` profiles:

```{.bash skip reason="server: starts pgAdmin and LocalStack, which pull their own images"}
docker compose --profile tools --profile aws up -d
```

Stop everything and drop the volumes, which deletes the database:

```{.bash exec}
docker compose down -v
```

## First start

The `api` container's entrypoint (`backend/docker-entrypoint.sh`) waits for Postgres,
runs `python -m backend.app.db.bootstrap` when `RUN_MIGRATIONS=true` (creates the schema
on a fresh database, upgrades an existing one, and creates `FIRST_SUPERUSER`), then
applies each seed listed in `SEED` once per database. Applied seeds are recorded in the
`seed_markers` table; set `SEED_FORCE=true` to re-run them.

| Seed | What it adds |
|------|--------------|
| `demo` (default) | `admin@demo.com / Demo1234!`, three experiments, two flags |
| `shoplab`, `streampulse` | the demo applications' experiments, flags and API keys |
| `sdk-contract` | the fixtures the SDK live-contract suite expects |

The seeds (`demo`, `shoplab`, `streampulse`, `sdk-contract`) are development-only. They run when
`ENVIRONMENT` is `development` (the default) or `test`. With `ENVIRONMENT=staging` or
`production` the container exits before it waits for Postgres, printing
`SEED=<value> is for development and is refused when ENVIRONMENT=<env>. Remove SEED.`
Set `SEED=` (empty) in `.env` there: an empty value applies no seeds, while leaving
`SEED` out applies `demo`. The seed scripts make the same check when they are run by hand.

## Variables you should set before sharing the stack

Put them in a `.env` file next to `docker-compose.yml`:

```env
SECRET_KEY=<at least 32 random characters>
FIRST_SUPERUSER=you@example.com
FIRST_SUPERUSER_PASSWORD=<8 characters to 72 bytes>
POSTGRES_PASSWORD=<random>
```

Other useful knobs: `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `API_HOST_PORT`,
`FRONTEND_HOST_PORT` when a port is taken; `SEED`; `LOG_FORMAT=json`; `METRICS_TOKEN` to
require a token on `/metrics`; `WEB_CONCURRENCY` for more uvicorn workers. `AUTH_PROVIDER=cognito`
switches authentication to Amazon Cognito; add `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`
and the AWS credentials to the `api` service's environment yourself, the compose file
does not pass them through. The dashboard does not yet sign in with Cognito. Sign in through
the API with `POST /api/v1/auth/token`. SSO sign-in needs `AUTH_PROVIDER=local`. See
[Signing in under Cognito](../cognito_integration.md#signing-in-under-cognito).

`DEV_AUTH_BYPASS` defaults to `false` in the compose file and is refused by the API unless
`ENVIRONMENT` is `development` or `test`.

## Running the API or dashboard outside Docker

Start only the data services and follow "Running outside Docker" in the
[Quick Start](quick-start.md):

```{.bash exec}
docker compose up -d --wait postgres redis
```

Then check that they are the two running:

```{.bash exec}
docker compose ps --services --status running | sort | paste -sd ' ' -
```
<!-- expect: postgres redis -->

It prints `postgres redis`.

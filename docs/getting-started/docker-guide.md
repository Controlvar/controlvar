# Docker Local Development

**This guide runs the local development stack, for your own machine. It is not for a server.** The stack publishes its ports on `127.0.0.1` only, `ENVIRONMENT=development` accepts a short `SECRET_KEY`, and the first start creates the demo administrator `admin@demo.com` with the demo password shown in this guide. To run Controlvar on a server, use the published images in [`deploy/compose/compose.yml`](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/compose.yml): the [README](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/README.md) beside it says how to run it and lists its settings, and [Releases, images and verification](../self-hosting/releases.md) covers the versions and how to verify the images.

## Overview

This guide runs Controlvar locally with Docker Compose and shows how to reach each
service. By default the compose file at the repository root starts PostgreSQL, Redis, the
API and the dashboard. Three optional profiles add more: `tools` (pgAdmin), `aws`
(LocalStack, for developing the optional AWS integrations) and `demo` (the ShopLab and
StreamPulse demo applications). [The Docker Compose stack](docker-compose-file.md) lists
every service with its host port and profile.

## Prerequisites

- Docker with Compose v2 (`docker compose`, not the retired `docker-compose`)
- `curl` and `jq`
- The AWS CLI, only for the LocalStack section below

## Project Structure

The commands below run from the root of a clone of the repository, which contains:

```text
controlvar/
├── backend/
├── frontend/
├── localstack/
│   └── init-aws.sh
├── docker-compose.yml
└── .env              (optional; your own settings)
```

## Docker Compose File Overview

`docker-compose.yml` defines these services:

- **postgres**: the database (PostgreSQL 16)
- **redis**: caching and rate-limit counters (Redis 7)
- **api**: the backend, built from `backend/Dockerfile`
- **frontend**: the dashboard, built from `frontend/Dockerfile`
- **pgadmin** (profile `tools`): a database browser
- **localstack** (profile `aws`): local emulation of AWS services
- **shoplab**, **streampulse** and their simulators (profile `demo`)

## Getting Started

### Step 1: Start Services

From the repository root, start the default services:

```{.bash exec timeout=1200}
docker compose up -d --wait
```

The first start builds the API and dashboard images, which takes a few minutes. `--wait`
returns once every service is healthy.

### Step 2: Verify Services

List the services that are running:

```{.bash exec}
docker compose ps --services --status running | sort
```
<!-- expect: api -->
<!-- expect: frontend -->
<!-- expect: postgres -->
<!-- expect: redis -->

It prints `api`, `frontend`, `postgres` and `redis`, one per line.

### Step 3: Connect to PostgreSQL

Open a `psql` session inside the `postgres` container:

```{.bash skip reason="server: opens an interactive psql session"}
docker compose exec postgres psql -U postgres -d experimentation
```

The user and password are both `postgres`, and the database is `experimentation`. The
tables live in the `experimentation` schema. To run a single query instead, pass it with
`-c`; this one lists the administrators the first start created:

```{.bash exec}
docker compose exec postgres psql -U postgres -d experimentation -tAc "select email from experimentation.users where is_superuser"
```
<!-- expect: admin@demo.com -->

It prints `admin@demo.com`.

PostgreSQL is also published on `localhost:5432` for tools on your machine, on `127.0.0.1` only. With the
`tools` profile, pgAdmin runs at http://localhost:5050:

```{.bash skip reason="server: starts pgAdmin, which pulls its own image"}
docker compose --profile tools up -d --wait pgadmin
```

### Step 4: Interact with Redis

Check that Redis answers:

```{.bash exec}
docker compose exec redis redis-cli ping
```
<!-- expect: PONG -->

It prints `PONG`.

### Step 5: Working with LocalStack

LocalStack runs only with the `aws` profile. Start it:

```{.bash skip reason="server: starts LocalStack, which pulls a large image"}
docker compose --profile aws up -d --wait localstack
```

It listens on http://localhost:4566.

#### AWS CLI Configuration for LocalStack

LocalStack accepts any credentials. Set dummy ones for this shell only, so that nothing is
written to your `~/.aws` and no real profile is used:

```{.bash skip reason="aws: sets dummy AWS CLI credentials for LocalStack"}
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-west-2
```

Every command below passes `--endpoint-url=http://localhost:4566`, which sends it to
LocalStack on your machine, not to an AWS account.

#### Creating and Listing DynamoDB Tables

Create a table:

```{.bash skip reason="aws: uses the AWS CLI against LocalStack"}
aws --endpoint-url=http://localhost:4566 dynamodb create-table --table-name testTable --attribute-definitions AttributeName=id,AttributeType=S --key-schema AttributeName=id,KeyType=HASH --billing-mode PAY_PER_REQUEST
```

List tables:

```{.bash skip reason="aws: uses the AWS CLI against LocalStack"}
aws --endpoint-url=http://localhost:4566 dynamodb list-tables
```

#### Working with S3

Create a bucket:

```{.bash skip reason="aws: uses the AWS CLI against LocalStack"}
aws --endpoint-url=http://localhost:4566 s3 mb s3://my-bucket
```

List buckets:

```{.bash skip reason="aws: uses the AWS CLI against LocalStack"}
aws --endpoint-url=http://localhost:4566 s3 ls
```

### Step 6: Rebuild After a Change

The `api` and `frontend` containers run images built from your working tree; they do not
reload when you edit the code. Rebuild and restart them after a change:

```{.bash skip reason="dev: rebuilds the images from your working tree"}
docker compose up -d --build --wait
```

The API is at http://localhost:8000 and the dashboard at http://localhost:3000.

### Step 7: Sign in to the dashboard

Open http://localhost:3000 and choose **Sign in**. Enter `admin@demo.com` as the **Email** and
`Demo1234!` as the **Password**, then choose **Sign in**; you land on **Experiments**.

The first start creates that administrator from `FIRST_SUPERUSER` and
`FIRST_SUPERUSER_PASSWORD` in `docker-compose.yml`, whose defaults these are. The default
`demo` seed adds `dev@demo.com`, `analyst@demo.com` and `viewer@demo.com` with the same
password, three experiments and two flags. These accounts are for a stack on your own machine.

## Development Workflow

For hot reloading, run the API or the dashboard outside Docker and keep only PostgreSQL
and Redis in it: see "Running outside Docker" in the [Quick Start](quick-start.md) and
"Running the stack" in `CONTRIBUTING.md`.

## Stopping

Stop everything and drop the volumes, which deletes the database:

```{.bash exec}
docker compose down -v
```

The next `docker compose up -d --wait` creates the database and applies the seeds again.

# Quick Start

This guide takes you from a fresh clone to a running experiment and a feature flag. Everything
below runs locally with Docker; no AWS account and no external identity provider is needed.

---

## Prerequisites

| Tool | Check |
|------|-------|
| Docker with Compose v2 | `docker compose version` |
| `curl` and `jq` | `jq --version` |

Only needed if you want to run the backend or dashboard outside Docker: Python 3.11+, Node.js 22+.

---

## Step 1: Start the stack

Clone the repository:

```{.bash skip reason="checkout: the runner starts inside a checkout"}
git clone https://github.com/Controlvar/controlvar.git
cd controlvar
```

Then start everything from the repository root:

```{.bash exec timeout=1200}
docker compose up -d --wait
```

The first start builds two images (`experimently-api:core`, `experimently-web:core`), starts
Postgres 16 and Redis 7, creates the database schema, creates the first administrator and
applies the `demo` seed (three experiments, two flags). Seeds run once per database.

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:3000 |
| API and interactive docs | http://localhost:8000 and http://localhost:8000/api/v1/docs |
| Health | http://localhost:8000/health/ready |

Default login: **admin@demo.com / Demo1234!**. This is the development stack: it
publishes its ports on `127.0.0.1` only. Change `FIRST_SUPERUSER_PASSWORD`, `SECRET_KEY`
and `POSTGRES_PASSWORD` in a `.env` file next to `docker-compose.yml` before anyone else
uses it, and run a server from the published images in
[`deploy/compose/compose.yml`](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/compose.yml)
instead. The header comment of `docker-compose.yml` describes the optional `demo`, `tools`
and `aws` profiles and the host-port overrides.

Verify:

```{.bash exec}
curl -s localhost:8000/health/ready | jq .status
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/experiments/
```
<!-- expect: "healthy" -->
<!-- expect: 401 -->

The first prints `"healthy"`. The second prints `401`, because the API requires a login.

---

## Step 2: Log in

In the browser, open http://localhost:3000 and sign in. From the shell:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

Tokens last 12 hours (`LOCAL_AUTH_TOKEN_TTL_MINUTES`). Ten failed logins lock an account
for 15 minutes.

---

## Step 3: Create an experiment

Through the dashboard: **Experiments → + New Experiment** walks you through guided setup
(type, details, variants, an optional sample-size estimate, review); create it, then
**Start**. Through the API:

```{.bash exec}
EXPERIMENT=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{
    "name": "Homepage button colour",
    "key": "homepage_button_colour",
    "hypothesis": "A green call-to-action raises click-through",
    "experiment_type": "a_b",
    "variants": [
      {"name": "control",   "is_control": true,  "traffic_allocation": 50},
      {"name": "green_cta", "is_control": false, "traffic_allocation": 50}
    ],
    "metrics": [
      {"name": "CTA click", "event_name": "cta_click", "metric_type": "conversion", "is_primary": true}
    ]
  }')
ID=$(jq -r .id <<<"$EXPERIMENT")

curl -s -X POST localhost:8000/api/v1/experiments/$ID/start -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "active" -->

The last command prints `"active"`.

Conversions are matched to a metric by `event_name`, so the events your app sends in
Step 5 must use the same name.

---

## Step 4: Create an API key

SDKs and your application authenticate with an API key, not a user token. The key is
shown once.

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"my-app"}' | jq -r .key)
```

---

## Step 5: Assign users and track events

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/assign \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"experiment_key":"homepage_button_colour","user_id":"user-123","context":{"country":"DE"}}' | jq
```
<!-- expect: "assigned": true -->

The response carries `variant_name`, `variant_id`, `is_control`, `configuration`, and
`assigned` with a `reason` (`assigned`, `holdout`, `mutual_exclusion` or `targeting`).
Assignments are sticky: the same `user_id` always gets the same variant.

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"event_type":"conversion","event_name":"cta_click","user_id":"user-123","experiment_key":"homepage_button_colour","value":1}' | jq .event_name
```
<!-- expect: "cta_click" -->

It prints `"cta_click"`, the event it stored.

The SDKs make the same two calls; see the [SDK guide](../sdk-guide.md).

---

## Step 6: Read the results

Dashboard: **Experiments → your experiment → View results**. API:

```{.bash exec}
curl -s localhost:8000/api/v1/results/$ID -H "Authorization: Bearer $TOKEN" | jq .summary
```
<!-- expect: "total_conversions": 1 -->

Results include per-variant conversion rates, p-values, confidence intervals and a
sample-size check.

---

## Step 7: Feature flags

```{.bash exec}
FLAG=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"key":"new_checkout","name":"New checkout","rollout_percentage":10,"is_active":false}' | jq -r .id)

curl -s -X POST localhost:8000/api/v1/feature-flags/$FLAG/activate -H "Authorization: Bearer $TOKEN" | jq .status

curl -s "localhost:8000/api/v1/feature-flags/evaluate/new_checkout?user_id=user-2" -H "X-API-Key: $KEY" | jq
```
<!-- expect: "active" -->
<!-- expect: "enabled": true -->
<!-- expect: "reason": "rollout" -->

The flag is created switched off (`"is_active": false`, which is also what leaving it out
does; a field the API does not read answers 422).
The second command turns it on for 10% of users and prints `"active"`. The third
evaluates it for `user-2` and prints `"enabled": true` with `"reason": "rollout"`:
`user-2` is inside the 10%, `user-123` is not, and a user always gets the same answer.

Evaluation returns `{key, enabled, config, reason}`. An inactive flag evaluates to
`enabled: false` with reason `inactive`; only an unknown key is a 404.

---

## Running outside Docker

```{.bash skip reason="server: starts long-running development servers"}
docker compose up -d --wait postgres redis
python3.11 -m venv venv && source venv/bin/activate
pip install -r backend/requirements.txt
export ENVIRONMENT=development
python -m backend.app.db.bootstrap
AUTH_PROVIDER=local uvicorn backend.app.main:app --reload --port 8000
```

The first line starts only the database and cache. The bootstrap creates the schema and
the first administrator, and is safe to run again. `ENVIRONMENT` must be set: the API
will not start without it, and the bootstrap will not create an administrator with the
default password unless it says `development` or `test`. In a second terminal, start the
dashboard on http://localhost:3100. The development server does not proxy `/api`: the
dashboard calls the API at http://localhost:8000 itself (set `NEXT_PUBLIC_API_URL` in
`frontend/.env.local` when the API runs elsewhere):

```{.bash skip reason="server: starts a long-running development server"}
cd frontend && npm ci && npm run dev
```

Run these from the repository root; the backend is imported as `backend.app.*`.

---

## Demo applications

To run the two sample apps, ShopLab (http://localhost:3200) and StreamPulse
(http://localhost:3300), and their traffic simulators in Docker as well:

```{.bash skip reason="demo: builds both demo app images and starts their simulators"}
SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait
```

The first start builds both apps' images. If you already started the `demo` profile
without the seeds and the apps answer 401, run the same command once: it applies the
missing seeds to the existing database, with no `down -v`. The
[demo guide](https://github.com/Controlvar/controlvar/blob/main/demo/DEMO_GUIDE.md)
walks through what to look at, and how to run the apps outside Docker.

---

## Troubleshooting

**`api` container never becomes healthy**

```{.bash exec}
docker compose logs api --tail 100
```
<!-- expect: [entrypoint] starting: -->

The entrypoint prints the bootstrap and seed steps. With `ENVIRONMENT=staging` or
`production`, a `SECRET_KEY` shorter than 32
characters, or `DEV_AUTH_BYPASS=true` with `ENVIRONMENT=production`, stops the API on
purpose. This stack runs with `ENVIRONMENT=development`, which accepts a short `SECRET_KEY`.

**Port already in use**

Set `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `API_HOST_PORT` or `FRONTEND_HOST_PORT` in `.env`.

**Start from scratch**

```{.bash exec}
docker compose down -v
```

This drops the database volume; the seeds run again on the next start.

**Events are not counted as conversions**

The event's `event_name` must equal the metric's `event_name`, the experiment must be
`active`, and the user must have been assigned first.

---

## What's next

- [Limitations](limitations.md) — what Controlvar does not do, and what happens instead
- [Docker Compose on one host](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/compose.yml) — run Controlvar where others can reach it
- [SDK guide](../sdk-guide.md) — endpoint contract and per-SDK status
- [Docker guide](docker-guide.md) — the local development stack: services, ports and profiles
- [User guide](../guides/user-guide.md) — for experiment designers and analysts
- [Testing guide](../development/testing-guide.md)

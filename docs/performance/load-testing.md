# Load Testing — Controlvar

## Overview

The load tests are [Locust](https://locust.io/) files in
`backend/tests/performance/locustfiles/`. `backend/tests/performance/run_load_tests.py`
runs one of them headless and judges the result against each endpoint's target: a p50,
p95 and p99 response time and a minimum number of requests per second. The targets are
defined in `backend/tests/performance/specs/performance_targets.py`.

The runner's `--profile` picks one of six locustfiles: `baseline`, `spike`, `endurance`,
`crud`, `db-stress` and `breakpoint`. Without it the runner runs the baseline,
`api_load_test.py`.

---

## What is measured, and where

This page publishes no response time or throughput. The CI run that measures them is the
Performance Tests workflow (`.github/workflows/performance-tests.yml`), which runs the
baseline every Monday and any of the six on a manual dispatch: it prints each endpoint's
p50, p95, p99 and requests per second beside its target, and uploads Locust's CSV files
as the run's artifacts.

Run with `--sla enforce` (the runner's default), a test **passes** when all of the
following hold simultaneously:
- no request failed (Locust exits 1 on any failed request, and the runner names the endpoints)
- every target the locustfile declares in its `TARGETS` tuple was recorded
- p50, p95, and p99 response times are at or below the target thresholds
- Achieved RPS meets or exceeds the minimum target

**The weekly Performance Tests workflow does not gate on latency or throughput.** It
passes `--sla report`: it fails when a request fails, when nothing was recorded, or when
a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and
requests per second beside its target without checking them.

---

## Running Load Tests Locally

CI runs this section and
[Running Unit Tests for the Load Test Framework](#running-unit-tests-for-the-load-test-framework)
as written, against the [Quick Start](../getting-started/quick-start.md) stack.

### Prerequisites

Run every command from the repository root, in the development virtual environment
that [Python Virtual Environment Setup](../getting-started/python-virtual-env-setup.md)
makes:

```{.bash exec}
source venv/bin/activate
```

Its requirements include Locust, at the version `backend/requirements.txt` pins:

```{.bash skip reason="registry: pip installs the backend's requirements from PyPI; CI builds this venv before the page runs"}
pip install -r backend/requirements.txt
```

The tests need PostgreSQL and Redis on `localhost`, and an API on port 8000 that uses
them. The Quick Start stack is all three. With `make db` instead (PostgreSQL and
Redis only), start the API from the checkout, in a separate terminal:

```{.bash skip reason="server: runs the API until you stop it"}
ENVIRONMENT=development uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Seed what the locustfiles call: the experiment `sdk_contract_ab`, the flag
`sdk_contract_flag`, an API key, and the developer account the management tasks sign in
as (`dev@demo.com`, not a superuser). The seed writes to the database the `POSTGRES_*`
settings name, whose defaults are that PostgreSQL, and writes the key to
`tests/sdk-contract/live/.api_key`:

```{.bash exec}
python -m backend.scripts.seed_sdk_contract
export LOAD_TEST_API_KEY_FILE=tests/sdk-contract/live/.api_key
```
<!-- expect: sdk_contract_ab -->
<!-- expect: Done. -->

It names the experiment, the flag and the key it created, or found from an earlier
run, and ends with `Done.`. Like the demo seeds, it runs only when `ENVIRONMENT` is
`development` (the default) or `test`; with `staging` or `production` it stops before it
touches the database.

### Rate limits

An API in the `development` environment, which is what the Quick Start stack and the
command above run, answers 429 to an address that passes a route's rate limit: 6000
requests a minute for each SDK route (`/api/v1/tracking/*`,
`/api/v1/feature-flags/evaluate/*`). Locust counts a 429 as a failed request, so the
run fails. So against a development API, keep a run small and short, as Option B
does. For a real load, run
the API in the `test` environment, where rate limiting is off, as Option C's
`--start-server` and the weekly workflow do.

### Option A: Locust Web UI (interactive)

Baseline test:

```{.bash skip reason="server: serves Locust's web UI until you stop it"}
locust -f backend/tests/performance/locustfiles/api_load_test.py \
       --host http://localhost:8000
```

Spike test:

```{.bash skip reason="server: serves Locust's web UI until you stop it"}
locust -f backend/tests/performance/locustfiles/spike_test.py \
       --host http://localhost:8000
```

Endurance test:

```{.bash skip reason="server: serves Locust's web UI until you stop it"}
locust -f backend/tests/performance/locustfiles/endurance_test.py \
       --host http://localhost:8000
```

Then open `http://localhost:8089` in your browser. For the baseline and endurance
tests, fill in **Number of users** and **Ramp up**, then click **Start**. The spike
test's shape sets its own users and ramp-up, so Locust disables those two fields
for it: click **Start**. Against a development API, the rate limits above apply.

### Option B: Headless mode (no browser)

Baseline, 10 users for 20 seconds, which stays under a development API's rate limits:

```{.bash exec timeout=180}
locust -f backend/tests/performance/locustfiles/api_load_test.py \
       --headless \
       --users 10 \
       --spawn-rate 10 \
       --run-time 20s \
       --host http://localhost:8000 \
       --csv /tmp/locust_baseline
```
<!-- expect: Total failures : 0 (0.00%) -->

It ends with the number of requests and of failures (`Total failures : 0 (0.00%)`),
and exits 1 if any request failed. Locust writes its CSV files under the
`/tmp/locust_baseline` prefix, `/tmp/locust_baseline_stats.csv` among them.

### Option C: the runner

`run_load_tests.py` runs Locust headless and judges the result. With `--start-server`
it first starts the API from the checkout in the `test` environment, where rate
limiting is off, on `--server-port`, runs against it, and stops it afterwards. That
API reads the same `POSTGRES_*` settings, with other defaults: in the `test`
environment its database is `experimentation_test` and its schema
`test_experimentation`. Name the ones the seed wrote to:

```{.bash exec}
export POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation
```

Then run it as the weekly workflow does, with `--sla report`, which prints latency and
throughput beside the targets instead of gating on them:

```{.bash exec timeout=300}
python backend/tests/performance/run_load_tests.py \
    --start-server \
    --server-port 8001 \
    --users 50 \
    --spawn-rate 10 \
    --duration 60s \
    --sla report
```
<!-- expect: Every declared target recorded and no request failed -->

It ends with `Every declared target recorded and no request failed; latency and
throughput reported, not gated. Exiting 0.`

It exits 1 when a request failed (`ERROR: Locust exited with code 1`, then one `FAIL:`
line per endpoint with failures), and when the run measured nothing or not what it
should: Locust recorded no requests (`FAIL: no stats`), no recorded endpoint matches a
target in `performance_targets.py` (`FAIL: no matched endpoint`), or a target the
locustfile declares in `TARGETS` was not recorded, or it declares none
(`FAIL: unmatched target`).
A recorded endpoint matches a target when its method and its Locust `name=` are the
target's `method` and `endpoint` exactly. Without `--sla report` it also exits 1 when an
endpoint misses a target.

Against an API that is already running, pass `--host` (for example
`--host http://localhost:8000`) instead of `--start-server` and `--server-port`. Against
a development API, keep the run small and short, as in Option B.

---

## Adding Load Tests for New Endpoints

1. Add a `PerformanceTarget` entry to `PERFORMANCE_TARGETS` in `performance_targets.py`,
   for a route the API serves (a unit test checks every target against the OpenAPI
   document)
2. Add its key to a `TargetGroup` in `TARGET_GROUPS`: the spec tests fail on a target in
   no group
3. Add a `@task` method to the appropriate user class in `api_load_test.py`, in the
   form every task uses: `with self.client.get(..., name=<the target's endpoint>,
   catch_response=True) as response:` and `expect(response, 200)` from
   `locustfiles/common.py`. Without the `with` block Locust sends the request and never
   records it
4. Add the target's key to the locustfile's `TARGETS` tuple, so a run that does not
   record it fails
5. Run the spec and validator unit tests (below)
6. Trigger a CI baseline run to check that the new endpoint is recorded

---

## Running Unit Tests for the Load Test Framework

In the development virtual environment, as above:

```{.bash exec}
source venv/bin/activate
export APP_ENV=test TESTING=true
```

Test the SLA spec definitions:

```{.bash exec}
python -m pytest backend/tests/performance/test_specs.py -v
```
<!-- expect: passed -->

Test the validators (percentile calculations, SLA checks, CSV parsing, reporting):

```{.bash exec}
python -m pytest backend/tests/performance/test_validators.py -v
```
<!-- expect: passed -->

Run both together:

```{.bash exec}
python -m pytest backend/tests/performance/test_specs.py \
                 backend/tests/performance/test_validators.py -v
```
<!-- expect: passed -->

They pass without a running database or API server.

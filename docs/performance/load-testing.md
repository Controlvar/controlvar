# Load Testing — Experimently

## Overview

The load testing framework uses [Locust](https://locust.io/) to validate that the
experimentation platform API meets its SLA (Service Level Agreement) performance targets
under realistic and extreme traffic conditions.

Three test types are provided:

| Test | Script | Purpose | Duration |
|------|--------|---------|----------|
| Baseline | `api_load_test.py` | Normal production-like traffic | 60–120s |
| Spike | `spike_test.py` | Sudden traffic burst (10 → 1000 users) | 120s |
| Endurance | `endurance_test.py` | Sustained load, memory/leak detection | 30 min |

---

## Performance SLA Targets

These targets are defined in `backend/tests/performance/specs/performance_targets.py`
and serve as the authoritative contracts for API performance.

| Endpoint | Method | p50 | p95 | p99 | Min RPS |
|----------|--------|-----|-----|-----|---------|
| `POST /api/v1/tracking/assign` | POST | < 50ms | < 200ms | < 500ms | > 500 |
| `POST /api/v1/tracking/track` | POST | < 30ms | < 100ms | < 300ms | > 1000 |
| `GET /api/v1/feature-flags/evaluate/{key}` | GET | < 20ms | < 80ms | < 200ms | > 2000 |
| `GET /api/v1/experiments` | GET | < 100ms | < 300ms | < 800ms | > 200 |
| `GET /health` | GET | < 10ms | < 30ms | < 100ms | > 5000 |

Run with `--sla enforce` (the runner's default), a test **passes** when all of the
following hold simultaneously:
- no request failed (Locust exits 1 on any failed request, and the runner names the endpoints)
- every target the locustfile declares in its `TARGETS` tuple was recorded
- p50, p95, and p99 response times are at or below the target thresholds
- Achieved RPS meets or exceeds the minimum target

**The weekly Performance Tests workflow does not gate on latency or throughput.** It
passes `--sla report`: it fails when a request fails, when nothing was recorded, or when
a declared target was not recorded, and it prints each endpoint's p50, p95, p99 and
requests per second beside its target without checking them. One run's timings on a
shared CI runner are not a stable statistic to fail on, and latency and throughput
budgets are Phase 2 work (QA L2). Nothing may cite that run as meeting a budget.

---

## File Structure

```text
backend/tests/performance/
├── __init__.py
├── specs/
│   ├── __init__.py
│   └── performance_targets.py    # SLA contract definitions
├── validators.py                  # Result parsing and SLA validation logic
├── test_specs.py                  # Unit tests for SLA specs
├── test_validators.py             # Unit tests for validators
├── run_load_tests.py              # CI runner script
└── locustfiles/
    ├── __init__.py
    ├── api_load_test.py           # Baseline load test
    ├── spike_test.py              # Spike test with LoadTestShape
    └── endurance_test.py          # 30-minute endurance test
```

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
ENVIRONMENT=development uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
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
run fails. Measured on a laptop, the baseline's 50 users sent about 100 tracking
requests a second, which reaches that limit within the minute. So against a
development API, keep a run small and short, as Option B does. For a real load, run
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

Then open `http://localhost:8089` in your browser, configure the number of users
and spawn rate, and click **Start swarming**. Against a development API, the rate
limits above apply.

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
and exits 1 if any request failed. Results are written to
`/tmp/locust_baseline_stats.csv`, `/tmp/locust_baseline_stats_history.csv` and
`/tmp/locust_baseline_failures.csv`.

### Option C: CI runner (validates SLAs automatically)

`run_load_tests.py` runs Locust headless and judges the result. With `--start-server`
it first starts the API from the checkout in the `test` environment, where rate
limiting is off, on `--server-port`, runs against it, and stops it afterwards. That
API reads the same `POSTGRES_*` settings, with other defaults: in the `test`
environment its database is `experimentation_test` and its schema
`test_experimentation`. Name the ones the seed wrote to:

```{.bash exec}
export POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation
```

Then:

```{.bash skip reason="timing: gates on p50, p95, p99 and requests per second, which depend on the machine; the block after next runs the same load"}
python backend/tests/performance/run_load_tests.py \
    --start-server \
    --server-port 8001 \
    --users 50 \
    --spawn-rate 10 \
    --duration 60s
```

It exits 0 when every SLA is met and 1 when any is violated. It also exits 1 when a
request failed (`ERROR: Locust exited with code 1`, then one `FAIL:` line per endpoint
with failures), and when the run measured nothing or not what it should: Locust recorded
no requests (`FAIL: no stats`), no recorded endpoint matches a target in
`performance_targets.py` (`FAIL: no matched endpoint`), or a target the locustfile
declares in `TARGETS` was not recorded, or it declares none (`FAIL: unmatched target`).
A recorded endpoint matches a target when its method and its Locust `name=` are the
target's `method` and `endpoint` exactly.

The weekly workflow adds `--sla report`, which prints latency and throughput beside the
targets instead of gating on them (see above):

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

Against an API that is already running, pass `--host` (for example
`--host http://localhost:8000`) instead of `--start-server` and `--server-port`. Against
a development API, keep the run small and short, as in Option B.

---

## Understanding Test Types

### Baseline (`api_load_test.py`)

Simulates normal production traffic with two user classes:

- **TrackingUser** (80% of load, weight=8): Calls `POST /tracking/track` (3x) and
  `POST /tracking/assign` (1x) with short waits (10–50ms between tasks)
- **APIUser** (20% of load, weight=2): Calls `GET /experiments`, `GET /feature-flags/evaluate/{key}`,
  and `GET /health` with longer waits (0.5–2s between tasks)

**Use for**: Verifying baseline SLA compliance before a release.

### Spike (`spike_test.py`)

Uses `LoadTestShape` to drive a four-stage traffic profile:

| Stage | Duration | Users | Spawn Rate |
|-------|----------|-------|------------|
| Baseline | 0–30s | 10 | 10/s |
| Spike ramp | 30–60s | 1000 | 100/s |
| Sustain peak | 60–90s | 1000 | 1/s |
| Recovery | 90–120s | 10 | 100/s |

**Use for**: Detecting connection pool exhaustion, circuit breaker triggers, and
latency spikes during sudden traffic bursts.

### Endurance (`endurance_test.py`)

Runs 100 concurrent users with realistic task weights for 30 minutes.
Logs a metrics snapshot every 5 minutes to detect gradual degradation.

**Use for**: Detecting memory leaks, DB connection pool leaks, and performance
regression that only manifests after extended operation.

---

## Reading the Results CSV

Locust produces three CSV files when run with `--csv <prefix>`:

| File | Content |
|------|---------|
| `<prefix>_stats.csv` | Aggregate per-endpoint statistics (p50, p95, p99, RPS) |
| `<prefix>_stats_history.csv` | Time-series stats sampled every 2 seconds |
| `<prefix>_failures.csv` | Details of any failed requests |

### Key columns in `_stats.csv`

| Column | Description |
|--------|-------------|
| `Type` | HTTP method (GET, POST, …) |
| `Name` | Endpoint path |
| `Request Count` | Total requests sent |
| `Failure Count` | Number of 4xx/5xx responses |
| `50%` | p50 response time in ms |
| `95%` | p95 response time in ms |
| `99%` | p99 response time in ms |
| `Requests/s` | Average RPS during the test |

The last row (`Aggregated`) is a summary across all endpoints — the validators
skip this row automatically.

---

## Interpreting CI Results

The `run_load_tests.py` runner produces a structured report after each run:

```text
============================================================
PERFORMANCE TEST REPORT
============================================================
Total endpoints: 5  |  Passed: 4  |  Failed: 1
------------------------------------------------------------
[PASS] /health
[PASS] /api/v1/tracking/track
[PASS] /api/v1/feature-flags/evaluate/{key}
[PASS] /api/v1/experiments
[FAIL] /api/v1/tracking/assign
       - p95 exceeded: 245.0ms > 200ms target
       - RPS below target: 420.3 < 500 required RPS
============================================================
1 endpoint(s) failed SLA targets.
============================================================
```

**If a test fails**:
1. Check the `_stats.csv` artifact uploaded by the CI workflow
2. Compare against the SLA targets in `performance_targets.py`
3. Look at the `_stats_history.csv` for time-series degradation
4. Check `_failures.csv` for error details

---

## Updating SLA Targets

SLA targets are defined in:
```text
backend/tests/performance/specs/performance_targets.py
```

To add a new endpoint target:

```python
PERFORMANCE_TARGETS["list_feature_flags"] = PerformanceTarget(
    endpoint="/api/v1/feature-flags",
    method="GET",
    p50_ms=100,
    p95_ms=300,
    p99_ms=800,
    min_rps=200,
    description="List feature flags",
)
```

After updating targets, run the spec unit tests to verify consistency:

```{.bash exec}
python -m pytest backend/tests/performance/test_specs.py -v
```
<!-- expect: passed -->

To update an existing target (e.g., after a performance improvement):

1. Update the values in `performance_targets.py`
2. Run the spec tests to verify `p50 < p95 < p99` still holds
3. Create a git commit documenting the new SLA
4. Trigger the CI workflow to validate the new targets against the live system

---

## Adding Load Tests for New Endpoints

1. Add a `PerformanceTarget` entry in `performance_targets.py`, for a route the API
   serves (a unit test checks every target against the OpenAPI document)
2. Write spec tests in `test_specs.py` covering the new target
3. Add a `@task` method to the appropriate user class in `api_load_test.py`, in the
   form every task uses: `with self.client.get(..., name=<the target's endpoint>,
   catch_response=True) as response:` and `expect(response, 200)` from
   `locustfiles/common.py`. Without the `with` block Locust sends the request and never
   records it
4. Add the target's key to the locustfile's `TARGETS` tuple, so a run that does not
   record it fails
5. Run the validator unit tests to confirm everything works
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

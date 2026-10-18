# Monitoring and Observability

The API exposes Prometheus metrics at `/metrics`, writes structured logs, and
answers health probes. On AWS, the CDK's monitoring stack adds the CloudWatch
dashboards, the alarms and the SNS topic that emails them: see
[What Gets Deployed](cdk.md#experimentation-monitoring).

---

## Prometheus Metrics

The API exposes a Prometheus-compatible metrics endpoint:

```{.bash skip reason="server: needs a running API and its METRICS_TOKEN"}
curl -s -H "Authorization: Bearer $METRICS_TOKEN" https://app.example.com/metrics
```

- With `METRICS_TOKEN` set, a scrape sends it as `Authorization: Bearer <token>`
  or as `?token=<token>`. Without it, or with another value, the answer is `401`.
- With no `METRICS_TOKEN`, the endpoint answers in development and test, and
  answers `403` in every other environment.
- `METRICS_ENABLED=false` turns the endpoint off: it answers `404`.

### Key Metrics Exported

| Metric | Type | Description |
|--------|------|-------------|
| `http_requests_total` | Counter | Total HTTP requests by method, endpoint and status code |
| `http_request_duration_seconds` | Histogram | Request latency (p50, p95, p99 percentiles) |
| `experiment_assignments_total` | Counter | Variant assignments by experiment and variant |
| `feature_flag_evaluations_total` | Counter | Flag evaluations by flag key and result |
| `events_tracked_total` | Counter | Tracked events by type |
| `rate_limit_redis_fallback_active` | Gauge | 1 while this process counts rate limits per process because Redis is unavailable; 0 while it counts them in Redis |
| `rate_limit_redis_fallbacks_total` | Counter | Times this process switched rate limiting from Redis to per-process counting |

Each API process exports its own values: with more than one worker per container, a scrape sees
the worker that answered it. While `rate_limit_redis_fallback_active` is 1, per-IP rate limits are
multiplied by the number of API processes; the limiter tries Redis again every 30 s.

### Sample Prometheus Scrape Config

A job for the `scrape_configs` list of `prometheus.yml`, with the token in a file:

```yaml
- job_name: 'experimentation-api'
  metrics_path: '/metrics'
  scheme: 'https'
  authorization:
    credentials_file: /etc/prometheus/controlvar-metrics-token
  static_configs:
    - targets: ['app.example.com']
  scrape_interval: 30s
```

---

## Structured Logging (structlog)

The API uses **structlog** for JSON-formatted structured logs.
[Logging Standards](../logging_standards.md) lists the fields each line carries.

### Standard Fields

| Field | Description |
|-------|-------------|
| `timestamp` | ISO 8601 UTC timestamp |
| `level` | `debug`, `info`, `warning`, `error`, `critical` |
| `logger` | Module path of the logging component |
| `request_id` | Unique identifier for the HTTP request (correlates all log entries for one request) |

---

## Health Check

The API answers `GET /health` without authentication, for load balancers and
monitoring. It is the readiness probe, the same as `GET /health/ready`:

```bash
curl -s localhost:8000/health
```

Outside production it reports each check with its detail:

```json
{
  "status": "healthy",
  "timestamp": "2026-10-07T19:42:43.499684+00:00",
  "profile": "core",
  "version": "0.26.3",
  "environment": "development",
  "checks": {
    "database": {"status": "healthy", "latency_ms": 2.13},
    "redis": {"status": "healthy", "latency_ms": 0.87},
    "disk": {"status": "healthy", "free_gb": 41.7},
    "modules": {"status": "healthy", "profile": "core"}
  }
}
```

With `ENVIRONMENT=production` it leaves out `version`, `environment` and every
detail except each check's `status`:

```json
{
  "status": "healthy",
  "timestamp": "2026-10-07T19:43:19.062038+00:00",
  "profile": "core",
  "checks": {
    "database": {"status": "healthy"},
    "redis": {"status": "healthy"},
    "disk": {"status": "healthy"},
    "modules": {"status": "healthy"}
  }
}
```

It answers `503 Service Unavailable` with `"status": "unhealthy"` when
PostgreSQL does not answer, when Redis does not answer and `REDIS_REQUIRED=true`,
or when the disk check fails. A failed check names the error's type outside
production:

```json
{
  "status": "unhealthy",
  "timestamp": "2026-10-07T19:42:54.171329+00:00",
  "profile": "core",
  "version": "0.26.3",
  "environment": "development",
  "checks": {
    "database": {"status": "unhealthy", "error": "OperationalError"},
    "redis": {"status": "unhealthy", "error": "ConnectionError"},
    "disk": {"status": "healthy", "free_gb": 41.7},
    "modules": {"status": "healthy", "profile": "core"}
  }
}
```

Without `REDIS_REQUIRED=true`, a Redis that does not answer is reported as
`unhealthy` under `checks` while the response stays `200` and `"healthy"`. Free
disk space of 1 GB or less is reported as `"low"` and does not fail the probe.
`GET /health/live` is the liveness probe: it answers `200` whenever the process
is up and checks neither PostgreSQL nor Redis.

The ALB health check targets `GET /health` with a 5-second timeout and a 2/3 healthy/unhealthy threshold. Tasks that fail health checks are automatically replaced by ECS.

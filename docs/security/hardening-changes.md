# Backend Security Hardening Changes

This page records how the API was hardened. What is in place now is on [Security model](model.md), and the rate limits are listed on [Using the API Docs](../api/api-docs-guide.md).

## 1. Security Response Headers (`backend/app/middleware/security_middleware.py`)

### What changed

The existing `SecurityHeadersMiddleware` was rewritten to:

- Apply a **strict Content-Security-Policy** (`default-src 'none'; frame-ancestors 'none'`) instead of the permissive policy that allowed `unsafe-inline` and `unsafe-eval`.  An API-only service has no need to serve scripts, styles, or frames. The one exception is the two API reference pages, `/api/v1/docs` and `/api/v1/redoc`, which allow the jsdelivr assets they load (see below).
- Emit **HSTS** (`max-age=31536000; includeSubDomains; preload`) on every response outside development, except a CORS preflight.
- **Remove server fingerprinting headers** (`server`, `x-powered-by`) to reduce the information available to an attacker.
- Expand **Permissions-Policy** to additionally restrict `usb` and `magnetometer`.

The API reference pages, `/api/v1/docs` (Swagger UI) and `/api/v1/redoc` (ReDoc), matched by exact path, get a different `Content-Security-Policy` instead (`DOCS_CSP` in the middleware): scripts and stylesheets from `https://cdn.jsdelivr.net`, ReDoc's Google Fonts and its logo image, Swagger UI's one inline script by its sha256 hash, and the OpenAPI document from the same origin. It allows no other inline script and no `eval`. The dashboard's nginx proxies these two paths without adding its own policy, so the API's is the only one the browser sees.

## 2. Rate Limiting (`backend/app/middleware/rate_limiter.py`)

### What changed

`RateLimitMiddleware` enforces per-IP, per-path sliding-window limits. Counters live in Redis
(`RedisRateLimiter`, shared across API instances); when Redis is unreachable the middleware falls back to
an in-process sliding window so requests are never rejected because the limiter is down.
Each process counts on its own while Redis is unavailable, so per-IP limits are multiplied by the number
of API processes (the running task count). The limiter tries Redis again every 30 s, with redis-py's client
retries turned off so a retry holds a request for at most one connect timeout (2 s), and returns to shared
counts within 30 s of Redis recovering. Each switch is logged once per process (a `rate_limiter` field), and
the state is on `/metrics` as `rate_limit_redis_fallback_active`.

Limits are resolved by `resolve_rate_limit(path)`: exact entries first, then the shared export prefix, then the AI interpretation prefix, then SDK path prefixes, then the default.

Rate-limited responses return HTTP 429 with `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Window` headers.  Successful responses also carry the `X-RateLimit-*` informational headers.

## 3. CORS Hardening (`backend/app/main.py`)

### What changed

The CORS middleware configuration was tightened:

- `allow_methods` changed from `["*"]` to an explicit list: `["GET", "POST", "PUT", "DELETE", "PATCH"]`.  `OPTIONS` is handled automatically by the CORS middleware; `HEAD` and `TRACE` are not needed.
- `allow_headers` changed from `["*"]` to an explicit list: `["Authorization", "Content-Type", "X-API-Key"]`.  Only headers the API actually uses are permitted.
- `expose_headers` lists `X-Request-ID`, `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Window` and `X-Total-Count`, so clients can read these response headers cross-origin.
- The allowed origins come from `BACKEND_CORS_ORIGINS`, else `CORS_ORIGINS`, plus any `DASHBOARD_ORIGINS`. With neither set, development and test fall back to the local apps' `http://localhost` origins, and staging and production allow no other origin (#130). Credentials are not allowed cross-origin.

## 4. Input Length Constraints (`backend/app/schemas/`)

### What changed

**`experiment.py`**

| Field | Before | After |
|---|---|---|
| `ExperimentBase.description` | no constraint | `max_length=2000` |
| `ExperimentBase.hypothesis` | no constraint | `max_length=2000` |
| `ExperimentUpdate.description` | no constraint | `max_length=2000` |
| `ExperimentUpdate.hypothesis` | no constraint | `max_length=2000` |

**`feature_flag.py`**

| Field | Before | After |
|---|---|---|
| `FeatureFlagBase.description` | no constraint | `max_length=2000` |
| `FeatureFlagBase.key` validator | lowercase + alnum check | stricter regex `^[a-z0-9][a-z0-9_-]*$` |

**`tracking.py`**

| Field | Before | After |
|---|---|---|
| `EventBase.user_id` | no constraint | `max_length=255` |
| `EventBase.session_id` | no constraint | `max_length=255` |
| `EventBase.experiment_id` | no constraint | `max_length=255` |
| `EventBase.feature_flag_id` | no constraint | `max_length=255` |
| `EventBase.variant_id` | no constraint | `max_length=255` |
| `AssignmentBase.*` | same as above | `max_length=255` |
| `AssignmentRequest.experiment_key` | `min_length=1` only | + `max_length=100` |
| `AssignmentRequest.user_id` | `min_length=1` only | + `max_length=255` |
| `EventRequest.event_type` | no constraint | `min_length=1, max_length=100` |
| `EventQueryParams.user_id` | no constraint | `max_length=255` |
| `EventQueryParams.event_type` | no constraint | `max_length=100` |

### Why it matters

Unbounded string fields can be exploited to:
- Cause excessively large database rows / index entries.
- Trigger O(n) processing in downstream logic (e.g., logging, serialisation).
- In the absence of a WAF, provide a vector for simple denial-of-service via large payloads.

Pydantic validates these limits before any database interaction occurs.

## 5. Configuration Hardening (`backend/app/core/config.py`)

### What changed

1. Added `CORS_ORIGINS: List[str]` as an alternative plain-string CORS origins config that can be set via env var as a comma-separated string.

2. Added a `validate_secret_key` field validator that raises a `ValueError` at application startup if:
   - `ENVIRONMENT` is `staging` or `production` (`TESTING` never relaxes this), **and**
   - `SECRET_KEY` is shorter than 32 characters **or** is one of the well-known weak defaults (`default-secret-key-for-testing`, `secret`, `changeme`, etc.)

   This prevents the application from starting in production with a weak key.

   Later, `TESTING` stopped being an exception: the settings now refuse
   `TESTING` together with `ENVIRONMENT=staging` or `production` outright, so
   the check applies in every staging and production start.

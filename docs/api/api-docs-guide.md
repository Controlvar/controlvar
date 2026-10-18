# Controlvar API Documentation Guide

This guide explains how to access and use the API documentation for the experimentation platform.

## Accessing the Documentation

The API documentation is available in two formats:

1. **Swagger UI**: Interactive documentation where you can try out API endpoints
   - URL: `/api/v1/docs`

2. **ReDoc**: Clean, responsive documentation with better navigation
   - URL: `/api/v1/redoc`

## Understanding the API Structure

The API is organized into the following sections:

1. **Authentication (`/api/v1/auth`)**: Endpoints for user authentication
2. **Experiments (`/api/v1/experiments`)**: Manage experiments and variants
3. **Tracking (`/api/v1/tracking`)**: Track user assignments and events
4. **Feature Flags (`/api/v1/feature-flags`)**: Manage feature flags
5. **Administration (`/api/v1/admin`)**: Admin-only operations
6. **Health (`/health`)**: System health check

> The `/api/v1/events` and `/api/v1/assignments` routers only expose an informational `GET /`;
> use the `/api/v1/tracking/*` endpoints above.

## Authentication

The API uses two authentication methods:

1. **OAuth2 Bearer Token**: For authenticated user endpoints
   - Obtain a token via `/api/v1/auth/token` endpoint
   - Include the token in the `Authorization: Bearer <token>` header

2. **API Key**: For tracking endpoints called from client applications
   - Include the API key in the `X-API-Key: <api-key>` header

## API Versioning

- Current version: v1 (accessed via `/api/v1/...`)

See [API stability](stability.md) for what may change within v1.

## Error Responses

An error answers with a `detail` field: a message such as `{"detail": "Not Found"}`,
or, when a request does not validate (`422`), a list of the problems, each with its
`type`, `loc` and `msg`:

```json
{
  "detail": [
    {"type": "missing", "loc": ["body", "email"], "msg": "Field required"}
  ]
}
```

## Pagination

List endpoints page in different ways: most take `skip` and `limit`, some take `page`
with `limit` or `page_size`, and a few take no paging parameter. The route's entry in
the API documentation names its parameters.

- `limit`: Maximum number of records to return (default: 100; the largest value allowed
  differs by route, and some lists have none: the route's OpenAPI entry gives it)

## Filtering and Sorting

Some list endpoints support filtering, and one supports sorting:

- Filtering: Use query parameters (e.g., `status_filter=active`)
- Sorting: `GET /api/v1/experiments/` takes `sort_by` and `sort_order` (e.g., `sort_by=created_at&sort_order=desc`)

## Common Workflows

Here are the key API workflows for the experimentation platform:

### Experiment Management Workflow

1. Create experiment (`POST /api/v1/experiments`)
2. Update experiment details (`PUT /api/v1/experiments/{experiment_id}`)
3. Start experiment (`POST /api/v1/experiments/{experiment_id}/start`)
4. Monitor results (`GET /api/v1/experiments/{experiment_id}/results`)
5. Complete experiment (`POST /api/v1/experiments/{experiment_id}/complete`)

### User Assignment and Tracking Workflow

1. Assign user to experiment (`POST /api/v1/tracking/assign`)
2. Track user events (`POST /api/v1/tracking/track` or `POST /api/v1/tracking/batch`)
3. Analyze results (`GET /api/v1/experiments/{experiment_id}/results`)

### Feature Flag Workflow

1. Create feature flag (`POST /api/v1/feature-flags`)
2. Update flag configurations (`PUT /api/v1/feature-flags/{feature_flag_id}`)
3. Evaluate a flag for a user
   (`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=...`, with an
   API key; user attributes go in `context` as URL-encoded JSON)

There is no per-user override endpoint. Target individual users with the
flag's `targeting_rules` instead — see
[Targeting & Rules](../Enhanced_Rules_Engine_Reference.md).

## Rate Limiting

The API implements rate limiting to ensure fair usage and system stability:

Limits are per IP, resolved in `backend/app/middleware/rate_limiter.py`:
an exact path match wins, then the export and AI interpretation prefixes, then
the SDK prefixes, then the default.

| paths | limit |
|---|---|
| `/api/v1/auth/token`, `/auth/login` | 10 / min |
| `/api/v1/auth/signup`, `/auth/forgot-password`, `/auth/reset-password` | 5 / min |
| `/api/v1/users/me/password` | 5 / min |
| `/api/v1/auth/sso/exchange` (full image) | 10 / min |
| `/api/v1/auth/sso/login` (full image) | 30 / min |
| `/api/v1/export/` (all export endpoints share one count) | 10 / min |
| `/api/v1/audit-logs/export` (its own count) | 10 / min |
| `/api/v1/ai/design` | 10 / min |
| `/api/v1/ai/interpret/` (every experiment id shares one count) | 10 / min |
| `/api/v1/tracking/assign/batch` (its own count) | 60 / min |
| `/api/v1/tracking/`, `/api/v1/feature-flags/evaluate/`, `/api/v1/feature-flags/user/`, `/api/v1/sdk/` | `SDK_RATE_LIMIT_PER_MINUTE`, default **6000 / min** |
| everything else | **300 / min** |

The SDK ceiling is high on purpose: one server-side SDK, or one office behind
a NAT, is a single IP sending thousands of requests a minute.

## Webhooks

The webhook endpoints are **inbound** — the platform receives callbacks from
third-party services:

```text
POST /api/v1/integrations/webhooks/github
POST /api/v1/integrations/webhooks/jira
POST /api/v1/integrations/webhooks/salesforce
```

There is no `POST /api/v1/webhooks`: no API registers a webhook for the platform to
call. For Slack messages about platform events, see
[Slack & Email Alerting](alerting.md).

## SDKs

The SDKs, and which of them are published, are listed in
[SDK contract & status](../sdk-guide.md).

Report bugs or request features using the GitHub repository.

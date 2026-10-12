# API Documentation

This directory contains the API documentation for Experimently.

## Files

1. [API Reference](endpoints.md)
   - Comprehensive guide to using the API
   - Authentication and authorization
   - Usage examples with curl commands
   - Rate limiting and constraints
   - SDK usage examples
   - Framework integrations

2. [API Development Guide](api-docs-guide.md)
   - Guidelines for API development
   - Best practices
   - Versioning strategy
   - Testing requirements
   - Documentation standards

3. [API stability and the OpenAPI snapshots](stability.md)
   - `openapi-v1.stable.json` (core profile) and `openapi-v1.full.json` (full profile),
     the checked-in contract the smoke suite compares against
   - Marking a route `x-stability: beta`
   - What to do when a stable route has to change

## Quick Start

The management API (experiments, flags, results) takes a user's access token; the SDK
endpoints (`/tracking/*`, flag evaluation) take an API key instead, in the `X-API-Key`
header. See [Authentication](auth.md) for both.

These commands run as written against the stack from the
[Quick Start](../getting-started/quick-start.md). On your own deployment, use its URL
wherever they say `localhost:8000`. Log in first; this saves the access token in
`$TOKEN` for the command after it:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. Then send the token as a bearer token. This lists the experiments'
names; the collection URL ends with a slash, `/api/v1/experiments/`, because without it
the API answers `307`, which `curl` doesn't follow:

```{.bash exec}
curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" | jq -r '.items[].name'
```
<!-- expect: Checkout Button Color -->

It prints the demo data's experiments, among them `Checkout Button Color`.

Then check the [API Reference](endpoints.md) for detailed usage examples.

## Common Tasks

1. **Authentication**
   - [OAuth2 Authentication](endpoints.md#oauth2-authentication)
   - [API Key Authentication](endpoints.md#api-key-authentication)

2. **Experiments**
   - [Creating and managing experiments](endpoints.md#2-managing-experiments)
   - [Analysing results](sequential-testing.md) — and
     [Bayesian](bayesian.md), [CUPED](cuped.md),
     [dimensional breakdowns](dimensional-analysis.md)

3. **Feature Flags**
   - [Creating flags and evaluating them](endpoints.md#3-feature-flag-management)
   - [Targeting rules](../Enhanced_Rules_Engine_Reference.md)

4. **Event Tracking**
   - [Tracking events](endpoints.md#4-tracking-events)

## Need Help?

- Check the [API Reference](endpoints.md) for usage examples
- Follow the [API Development Guide](api-docs-guide.md) for best practices
- Contact support for additional assistance

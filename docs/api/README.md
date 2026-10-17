# API Documentation

This directory contains the API documentation for Controlvar.

## Files

1. [API Reference](endpoints.md): the experiments, feature flags and tracking endpoints, with curl examples
2. [Using the API Docs](api-docs-guide.md): the API documentation the API serves, and its rate limits
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

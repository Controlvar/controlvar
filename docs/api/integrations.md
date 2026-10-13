# Third-Party Integrations API

!!! info "Part of the `integrations` module"
    Third-party integrations is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

This document describes the third-party integrations endpoints: one stored configuration each for Jira, Salesforce and GitHub, and a webhook endpoint for each that authenticates what the service sends. Nothing in the platform calls Jira, Salesforce or GitHub yet, and an authenticated delivery is acknowledged without changing anything in the platform (see [What a delivery does](#what-a-delivery-does)).

---

## Overview

Each integration is represented as a persisted configuration record containing the service type, credentials, and connection metadata. Incoming events from external services are handled via per-integration webhook endpoints.

**Base path**: `/api/v1/integrations` (an optional module — a core deployment does not serve these routes)

**One configuration per service.** An integration is identified by its *type*, not by an id: there is a unique constraint on `integration_type`, so the platform holds at most one Jira, one Salesforce and one GitHub configuration. Every path below addresses it by type.

**Authentication**: the CRUD endpoints require a Bearer token — **ADMIN or DEVELOPER** to read, **ADMIN** to create, update or delete. The three webhook endpoints take no platform credential at all; they authenticate the *sender* against the integration's own `webhook_secret` (see [Webhook Endpoints](#webhook-endpoints)).

**Stored secrets are never returned.** A response shows the connection settings and names the stored secrets without their values; see [What a response shows](#what-a-response-shows).

The examples on this page run against a local full-profile stack (see [Modules and profiles](../getting-started/modules.md)); on your own deployment, use its URL instead of `localhost:8000`. Sign in as the stack's administrator, which saves a token in `$TOKEN`:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. If it prints `null`, the sign-in failed and `$TOKEN` holds no token.

---

## `IntegrationType` Enum

The value is lower case, in the body and in the path alike.

| Value | Description |
|---|---|
| `jira` | Atlassian Jira (Cloud or Server) |
| `salesforce` | Salesforce |
| `github` | GitHub |

---

## CRUD Endpoints

### What a response shows

Every response that carries a configuration (create, list, get and update) has the same shape, whoever the caller is:

| Field | What it holds |
|---|---|
| `encrypted_config` | Only the connection settings, with their stored values: `base_url`, `email` and `project_key` (Jira), `instance_url` and `client_id` (Salesforce), `repo_owner` and `repo_name` (GitHub) |
| `stored_secrets` | The names, sorted, of every other key the configuration holds, whatever its value: `api_token`, `client_secret`, `token`, `webhook_secret`, and any key not in the list above |

The value of a stored secret is never returned, not as a placeholder and not in part. To change a secret, send it with [`PUT`](#update-integration); the keys you do not send are kept. A secret that is lost cannot be read back: generate a new one, `PUT` it, and set it at the provider.

`encrypted_config` is stored in the database as given. Despite its name, it is not encrypted.

---

### Create Integration

```text
POST /api/v1/integrations
```

Creates the configuration for one service. The `encrypted_config` object's structure varies by `integration_type` (see [Per-Service Config Schemas](#per-service-config-schemas) below).

**Authentication**: ADMIN.

**Request Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `integration_type` | `string` | Yes | `jira`, `salesforce` or `github` |
| `is_active` | `boolean` | No | Default `false`. Only an **active** configuration answers webhooks |
| `encrypted_config` | `object` | No | The per-service credentials below |

**Example**: this creates the Jira configuration with a new random `webhook_secret`, which stays in `$WEBHOOK_SECRET`. `jq -n` builds the request body and `curl` sends it. The Jira values are examples: use your own site, account and API token.

```{.bash exec}
WEBHOOK_SECRET=$(openssl rand -hex 32)

jq -n --arg secret "$WEBHOOK_SECRET" '{
    integration_type: "jira",
    is_active: true,
    encrypted_config: {
      base_url: "https://your-org.atlassian.net",
      email: "automation@your-org.com",
      api_token: "ATATT3x...",
      project_key: "EXP",
      webhook_secret: $secret
    }
  }' \
  | curl -s -X POST localhost:8000/api/v1/integrations \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      --data @- \
  | jq .
```
<!-- expect: "integration_type": "jira" -->
<!-- expect: "is_active": true -->
<!-- expect: "project_key": "EXP" -->
<!-- expect: "api_token", -->
<!-- expect: "webhook_secret" -->

**Response: 201 Created**: the settings it was sent, without the secrets, which are named in `stored_secrets`: here `"api_token"` and `"webhook_secret"`.

```json
{
  "id": "3f1a9c62-6f5e-4a3b-9a0c-6d2b8e7f1a45",
  "integration_type": "jira",
  "is_active": true,
  "encrypted_config": {
    "base_url": "https://your-org.atlassian.net",
    "email": "automation@your-org.com",
    "project_key": "EXP"
  },
  "stored_secrets": ["api_token", "webhook_secret"],
  "last_sync_at": null,
  "last_error": null,
  "created_at": "2026-01-15T09:00:00Z",
  "updated_at": "2026-01-15T09:00:00Z"
}
```

A second configuration of the same type is `409 Conflict`.

---

### List Integrations

```text
GET /api/v1/integrations
```

Returns every configured integration — at most three records. There are no query parameters and no pagination.

**Authentication**: ADMIN or DEVELOPER.

**Example Request**

```{.bash exec}
curl -s localhost:8000/api/v1/integrations \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[].integration_type'
```
<!-- expect: jira -->

It prints `jira`, the one configuration so far.

**Response: 200 OK** — an array of the object shown above.

---

### Get Integration

```text
GET /api/v1/integrations/{integration_type}
```

Returns the configuration for one service.

**Authentication**: ADMIN or DEVELOPER.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `integration_type` | `string` | `jira`, `salesforce` or `github` |

**Example Request**

```{.bash exec}
curl -s localhost:8000/api/v1/integrations/jira \
  -H "Authorization: Bearer $TOKEN" | jq -c '{is_active, stored_secrets}'
```
<!-- expect: {"is_active":true,"stored_secrets":["api_token","webhook_secret"]} -->

It prints `{"is_active":true,"stored_secrets":["api_token","webhook_secret"]}`: the configuration is active and holds an API token and a webhook secret, whose values are not shown.

**Response: 200 OK** — the object shown above. `404 Not Found` when that service has no configuration.

---

### Update Integration

```text
PUT /api/v1/integrations/{integration_type}
```

Updates the configuration. Every field is optional; omitted fields are left alone. `encrypted_config` is **merged key by key**:

- a key sent with a value replaces the stored one, or adds it;
- a key sent as `null` removes it;
- a stored key that is not sent is kept.

So send only the keys you are changing. Reading the configuration back first is not needed, and a response carries no secret to send back anyway.

**Authentication**: ADMIN.

**Request Body** (all fields optional)

| Field | Type | Description |
|---|---|---|
| `is_active` | `boolean` | Activate or deactivate the integration |
| `encrypted_config` | `object` | The keys to set, each with its new value, or `null` to remove it |
| `last_error` | `string` or `null` | Clear or set the last recorded error |

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/integrations/jira \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "is_active": true,
    "encrypted_config": {
      "email": "new-automation@your-org.com",
      "api_token": "ATATT3x-new-token...",
      "project_key": "NEWPROJ"
    }
  }' | jq '{encrypted_config, stored_secrets}'
```
<!-- expect: "email": "new-automation@your-org.com" -->
<!-- expect: "project_key": "NEWPROJ" -->
<!-- expect: "api_token", -->
<!-- expect: "webhook_secret" -->

This changes the email, the API token and the project, and keeps `base_url` and `webhook_secret` as they were: the response shows the new email and project beside the old `base_url`, and still names both secrets. To remove a key, send it as `null`:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/integrations/jira \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{ "encrypted_config": { "project_key": null } }' | jq -c '.encrypted_config | keys'
```
<!-- expect: ["base_url","email"] -->

It prints the settings left, `["base_url","email"]`: `project_key` is gone.

**Response: 200 OK** — the updated object, in the shape [described above](#what-a-response-shows). `404 Not Found` when that service has no configuration.

---

### Delete Integration

```text
DELETE /api/v1/integrations/{integration_type}
```

Deletes the record. This is a hard delete — nothing is retained; to stop an integration without losing its credentials, `PUT` it with `{"is_active": false}` instead.

**Authentication**: ADMIN.

**Example Request**

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8000/api/v1/integrations/jira \
  -H "Authorization: Bearer $TOKEN"
```
<!-- expect: 204 -->

It prints `204`, and the Jira configuration is gone.

**Response: 204 No Content**. `404 Not Found` when that service has no configuration.

---

## Webhook Endpoints

```text
POST /api/v1/integrations/webhooks/jira
POST /api/v1/integrations/webhooks/salesforce
POST /api/v1/integrations/webhooks/github
```

These three routes take no sign-in and no API key. Each one **authenticates the sender against the integration's `webhook_secret` before the payload is parsed**. There is no path parameter: the handler uses the single *active* configuration of that type.

**Authenticating a delivery.** A sender presents the secret one of two ways:

| Method | Header | Value |
|---|---|---|
| Signature (preferred) | `X-Hub-Signature-256`, or `X-Hub-Signature` (Jira and Salesforce only) | `sha256=` + HMAC-SHA256 of the **raw** request body, hex |
| Shared secret | `X-Experimently-Webhook-Secret` | The `webhook_secret` itself |

```text
expected = "sha256=" + HMAC-SHA256(webhook_secret, raw_body)
```

- **GitHub** signs every delivery, so only `X-Hub-Signature-256` is accepted. The legacy SHA-1 `X-Hub-Signature` GitHub also sends is ignored.
- **Jira** and **Salesforce** accept either. A delivery that carries a signature header is judged on the signature alone, and a SHA-1 value (`sha1=...`) is refused. If your sender cannot sign the body, have it send the shared-secret header instead; a Salesforce Flow HTTP Callout can set one. The body must be a JSON object: a SOAP/XML body is refused with `400` even with the right secret. The shared secret is replayable and puts the secret on the wire, so use it only over TLS, and prefer the signature where the sender can produce one.

Both comparisons are constant-time. An integration with **no `webhook_secret` configured cannot authenticate anybody** and every delivery to it is refused — an anonymous write path fails closed.

Only the secret is consulted. The *outbound* API credentials (`instance_url`/`client_id`/`client_secret` for Salesforce, `token`/`repo_owner`/`repo_name` for GitHub) are not read here and are not needed: a receive-only integration — one configured with nothing but a `webhook_secret` — authenticates its deliveries normally. It is answered `200` and the event is logged rather than parsed, because parsing is the service's and the service cannot be built without those credentials.

**Response: 401 Unauthorized** — with the same body whatever went wrong: a wrong secret, a missing header, no `webhook_secret` configured, no active integration of that type, or no such integration at all. The reply must not tell an anonymous caller which integrations this deployment has.

```json
{ "detail": "Webhook authentication failed" }
```

**Response: 200 OK** — for every authenticated delivery:

```json
{ "status": "received" }
```

That 200 is deliberate even when processing fails: a provider must not retry forever over something it cannot fix, so an error *inside* the platform is logged and still answered 200. Only a body that is not a JSON object is `400 Bad Request`, and that is checked after the sender is known.

### What a delivery does

An authenticated delivery is acknowledged, and nothing in the platform changes because of it yet: no experiment, flag or other record is created or updated from a Jira, Salesforce or GitHub event.

### Sending a signed delivery

A sender signs the exact bytes it sends. To try it, create a receive-only Jira integration, with nothing but a new `webhook_secret` (the configuration above was deleted):

```{.bash exec}
WEBHOOK_SECRET=$(openssl rand -hex 32)

jq -n --arg secret "$WEBHOOK_SECRET" \
    '{integration_type: "jira", is_active: true, encrypted_config: {webhook_secret: $secret}}' \
  | curl -s -X POST localhost:8000/api/v1/integrations \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      --data @- \
  | jq -c '{integration_type, is_active, stored_secrets}'
```
<!-- expect: {"integration_type":"jira","is_active":true,"stored_secrets":["webhook_secret"]} -->

It prints `{"integration_type":"jira","is_active":true,"stored_secrets":["webhook_secret"]}`.

Then sign a delivery the way the table above says: the HMAC-SHA256 of the raw body, keyed with the `webhook_secret`, in hex, after `sha256=`:

```{.bash exec}
BODY='{"webhookEvent": "jira:issue_updated", "issue": {"key": "EXP-123"}}'
SIGNATURE="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"

curl -s -X POST localhost:8000/api/v1/integrations/webhooks/jira \
  -H 'Content-Type: application/json' \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data-binary "$BODY" | jq -c .
```
<!-- expect: {"status":"received"} -->

It prints `{"status":"received"}`. `printf '%s'` adds no newline and `--data-binary` sends the body byte for byte, so the signature covers exactly what is sent. `openssl dgst` prints the digest as the last field of its line (after `SHA2-256(stdin)= ` with OpenSSL 3, on its own with LibreSSL), which `awk` keeps. 

Change one byte of the body and the same signature is refused:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/integrations/webhooks/jira \
  -H 'Content-Type: application/json' \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data-binary "${BODY/EXP-123/EXP-124}" | jq -c .
```
<!-- expect: {"detail":"Webhook authentication failed"} -->

It prints `{"detail":"Webhook authentication failed"}`.

### Upgrading an integration created before webhook authentication

An integration configured before webhook authentication has no `webhook_secret`, and **every delivery to it answers 401**. The 401 says nothing (by design), so the platform log names the integration instead, once per process:

```text
The active jira integration has no webhook_secret in its encrypted_config, so every
inbound delivery to /api/v1/integrations/webhooks/jira is refused with 401. Add one …
```

The remedy is one `PUT` that sends the new key. `PUT` merges `encrypted_config` key by key, so the stored credentials are kept and nothing is read back first. `$TOKEN` is an ADMIN bearer token (the sign-in above sets it); set `TYPE` to the integration: `jira` below, or `salesforce` or `github`. The recipe prints the new secret once, on a line that starts `New webhook_secret for jira:`, and only when the `PUT` is answered `200`. The response body is discarded (`-o /dev/null`) and only its status code is kept, so that line is the only thing the recipe writes to standard output. An error status prints nothing to standard output, shows curl's one-line error on standard error and exits non-zero: `404` for a type with no configuration (create it first), `403` for a token that is not an ADMIN's, `401` for a token that is missing, invalid or expired, `422` for a `TYPE` that is not `jira`, `salesforce` or `github`, and curl's own error when the API cannot be reached. A redirect also prints nothing and exits `1`: it means the URL is not the API's exact one (a trailing slash, or `http://` where the deployment redirects to `https://`), so use the exact URL. The recipe does not follow redirects, because that would send the secret on to the new address. A secret that was never stored is never shown.

```{.bash exec}
TYPE=jira
SECRET=$(openssl rand -hex 32)

CODE=$(jq -n --arg s "$SECRET" '{encrypted_config: {webhook_secret: $s}}' \
  | curl -sSf -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $TOKEN" \
         -H 'Content-Type: application/json' --data @- \
         "http://localhost:8000/api/v1/integrations/$TYPE") \
  && [ "$CODE" = 200 ] && printf 'New webhook_secret for %s: %s\n' "$TYPE" "$SECRET"
```
<!-- expect: New webhook_secret for jira: -->

A script written for the earlier version of this recipe, which read the configuration, added the key and sent the whole object back, still works: the stored secrets it cannot send are kept.

Then set the same value at the provider: GitHub's webhook *Secret* field, Jira's webhook secret (Jira Cloud) or the `X-Experimently-Webhook-Secret` header on the relay in front of it, and the same header on the Salesforce callout (or relay).

From now on the secret the integration held before is refused, and the new one is accepted. Here a delivery presents each in `X-Experimently-Webhook-Secret`, the header a Jira Server webhook sets because it cannot sign:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/integrations/webhooks/jira \
  -H 'Content-Type: application/json' \
  -H "X-Experimently-Webhook-Secret: $WEBHOOK_SECRET" \
  --data-binary "$BODY" | jq -c .

curl -s -X POST localhost:8000/api/v1/integrations/webhooks/jira \
  -H 'Content-Type: application/json' \
  -H "X-Experimently-Webhook-Secret: $SECRET" \
  --data-binary "$BODY" | jq -c .
```
<!-- expect: {"detail":"Webhook authentication failed"} -->
<!-- expect: {"status":"received"} -->

The old secret prints `{"detail":"Webhook authentication failed"}`, and the new one `{"status":"received"}`.

---

### Jira Webhook

Receives Jira issue events (created, updated, transitioned). Nothing in the platform acts on them yet (see [What a delivery does](#what-a-delivery-does)).

**Headers**: `Content-Type: application/json`, plus `X-Hub-Signature` (Jira Cloud) or `X-Experimently-Webhook-Secret` (Jira Server).

**Example Payload** (Jira issue transitioned to "Done"):

```json
{
  "webhookEvent": "jira:issue_updated",
  "issue": {
    "id": "10042",
    "key": "EXP-123",
    "fields": {
      "summary": "Checkout Button Color Experiment",
      "status": { "name": "Done" }
    }
  },
  "changelog": {
    "items": [
      { "field": "status", "fromString": "In Progress", "toString": "Done" }
    ]
  }
}
```

---

### Salesforce Webhook

Receives a JSON object from a Salesforce Flow HTTP Callout, Apex callout or relay (Campaign updated, Opportunity stage changed). A native Salesforce Outbound Message sends SOAP/XML with no custom headers and cannot call this route: without the header it is refused with `401`, and with the header added by a proxy the XML body is refused with `400`.

**Headers**: `Content-Type: application/json`, plus `X-Experimently-Webhook-Secret`, set by the callout or relay, or `X-Hub-Signature-256` from a callout that signs the body.

**Example Payload**:

```json
{
  "event_type": "campaign_updated",
  "campaign_id": "701xx000000001AAAQ",
  "campaign_name": "Q1 Checkout Optimization",
  "status": "Completed",
  "experiment_key": "checkout-button-color"
}
```

---

### GitHub Webhook

Receives GitHub events (push, pull_request, issues).

**Headers**:

| Header | What the platform does with it |
|---|---|
| `X-Hub-Signature-256` | HMAC-SHA256 signature of the raw request body, prefixed with `sha256=` — **required** |
| `X-GitHub-Event` | Read as the event type (e.g., `push`, `pull_request`) when it is sent |
| `X-GitHub-Delivery`, `Content-Type` | Not checked |

**Example Payload** (pull request opened):

```json
{
  "action": "opened",
  "number": 42,
  "pull_request": {
    "title": "feat: Add new checkout flow experiment",
    "html_url": "https://github.com/your-org/your-repo/pull/42",
    "head": { "ref": "feat/checkout-experiment" },
    "base": { "ref": "main" }
  },
  "repository": {
    "full_name": "your-org/your-repo"
  }
}
```

---

## Per-Service Config Schemas

These are the keys of `encrypted_config`. `webhook_secret` is required by all three: without it the service's webhook endpoint refuses every delivery. The other keys are the credentials for calls to the service, which nothing in the platform makes yet, so an integration that only receives needs nothing but `webhook_secret`. A response returns only `base_url`, `email`, `project_key`, `instance_url`, `client_id`, `repo_owner` and `repo_name` with their values, and names every other key in `stored_secrets` (see [What a response shows](#what-a-response-shows)).

### Jira Config

The Jira client is written for **HTTP Basic Auth** (`email:api_token`); nothing in the platform calls it yet.

| Field | Type | Required | Description |
|---|---|---|---|
| `base_url` | `string` | Yes | Jira instance base URL (e.g., `https://your-org.atlassian.net`) |
| `email` | `string` | Yes | Atlassian account email used for API access |
| `api_token` | `string` | Yes | Jira API token (generated at `id.atlassian.com/manage-profile/security/api-tokens`) |
| `project_key` | `string` | No | A Jira project key such as `EXP`, stored for calls to Jira, which nothing in the platform makes yet |
| `webhook_secret` | `string` | Yes | Authenticates inbound deliveries to `/webhooks/jira` |

```json
{
  "base_url": "https://your-org.atlassian.net",
  "email": "automation@your-org.com",
  "api_token": "ATATT3xFfGF0...",
  "project_key": "EXP",
  "webhook_secret": "a-random-strong-secret"
}
```

---

### Salesforce Config

The Salesforce client is written for **OAuth 2.0 Client Credentials** (`client_id` and `client_secret` for an access token at the Salesforce token endpoint); nothing in the platform calls it yet.

| Field | Type | Required | Description |
|---|---|---|---|
| `instance_url` | `string` | Yes | Salesforce instance URL (e.g., `https://your-org.my.salesforce.com`) |
| `client_id` | `string` | Yes | Connected App consumer key |
| `client_secret` | `string` | Yes | Connected App consumer secret |
| `webhook_secret` | `string` | Yes | Authenticates inbound deliveries to `/webhooks/salesforce` |

```json
{
  "instance_url": "https://your-org.my.salesforce.com",
  "client_id": "3MVG9...",
  "client_secret": "1234567890ABCDEF...",
  "webhook_secret": "a-random-strong-secret"
}
```

---

### GitHub Config

The GitHub client is written for a **Bearer Token** (a GitHub Personal Access Token or a GitHub App installation token in `Authorization: Bearer <token>`); nothing in the platform calls it yet.

| Field | Type | Required | Description |
|---|---|---|---|
| `token` | `string` | Yes | GitHub PAT or GitHub App installation token with appropriate repo scopes |
| `repo_owner` | `string` | Yes | Owner (user or organisation) of the repository |
| `repo_name` | `string` | Yes | Repository name |
| `webhook_secret` | `string` | Yes | Verifies the `X-Hub-Signature-256` of inbound deliveries to `/webhooks/github` |

```json
{
  "token": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "repo_owner": "your-org",
  "repo_name": "your-repo",
  "webhook_secret": "a-random-strong-secret"
}
```

`POST`/`PUT` do not validate these keys: a configuration missing one is stored as sent. A missing `webhook_secret` makes the webhook endpoint answer 401, and an integration that has only a `webhook_secret` receives normally.

---

## Error Responses

| Status | Meaning |
|---|---|
| `400 Bad Request` | Webhook body is not a JSON object |
| `401 Unauthorized` | CRUD: missing or invalid Bearer token. Webhooks: the sender did not present the integration's `webhook_secret`, or no active integration of that type is configured |
| `403 Forbidden` | Authenticated user lacks the required role (ADMIN, or ADMIN/DEVELOPER to read) |
| `404 Not Found` | No integration of that type is configured |
| `409 Conflict` | An integration of that type already exists |
| `422 Unprocessable Entity` | `integration_type` is not one of `jira`, `salesforce`, `github` |
| `500 Internal Server Error` | Unexpected server error |

```json
{
  "detail": "jira integration already configured"
}
```

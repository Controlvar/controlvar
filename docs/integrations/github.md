# GitHub Integration

!!! info "Part of the `integrations` module"
    Third-party integrations is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

The GitHub integration receives your repository's webhook deliveries and checks each one's signature. It also stores a token and a repository for calls to GitHub, but nothing in the platform calls GitHub yet, and a delivery is acknowledged without changing anything in the platform.

---

## What the Integration Does

- **Inbound (GitHub → Platform)**: Receive webhook events from GitHub (`push`, `pull_request`, `issues`). The platform checks each delivery's HMAC-SHA256 signature and answers it. Nothing in the platform acts on the event yet: no pull request is linked to an experiment, and no experiment changes.
- **Outbound (Platform → GitHub)**: not wired yet. Nothing in the platform calls GitHub, so the `token` and the repository are stored but not used.

---

## Prerequisites

Before creating the integration, you need:

1. A **GitHub Personal Access Token (PAT)** with appropriate repository scopes, or a GitHub App installation token
2. A strong **webhook secret** that you will configure both in the platform and in the GitHub webhook settings
3. A GitHub repository where you will configure the webhook

### The GitHub token

The token is stored for calls to GitHub, which nothing in the platform makes yet, so an integration that only receives webhooks can leave it out.


---

## Creating the Integration

Creating, changing and deleting an integration needs an **ADMIN** bearer token; an ADMIN or a DEVELOPER can read it. `is_active` defaults to `false`, and only an active integration answers webhook deliveries, so send `true`.

The examples on this page run against a local full-profile stack (see [Modules and profiles](../getting-started/modules.md)); on your own deployment, use its URL instead of `localhost:8000`. Sign in as the stack's administrator, which saves a token in `$TOKEN`:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. If it prints `null`, the sign-in failed and `$TOKEN` holds no token.

This generates a webhook secret, keeps it in `$WEBHOOK_SECRET`, and creates the integration with it. The token and the repository are examples: use your own.

```{.bash exec}
WEBHOOK_SECRET=$(openssl rand -hex 32)

jq -n --arg secret "$WEBHOOK_SECRET" '{
    integration_type: "github",
    is_active: true,
    encrypted_config: {
      token: "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      repo_owner: "your-org",
      repo_name: "your-repo",
      webhook_secret: $secret
    }
  }' \
  | curl -s -X POST http://localhost:8000/api/v1/integrations \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data @- \
  | jq .
```
<!-- expect: "integration_type": "github" -->
<!-- expect: "is_active": true -->
<!-- expect: "repo_owner": "your-org" -->
<!-- expect: "token", -->
<!-- expect: "webhook_secret" -->

**Response: 201 Created**

```json
{
  "id": "3f1a9c62-6f5e-4a3b-9a0c-6d2b8e7f1a45",
  "integration_type": "github",
  "is_active": true,
  "encrypted_config": {
    "repo_owner": "your-org",
    "repo_name": "your-repo"
  },
  "stored_secrets": ["token", "webhook_secret"],
  "last_sync_at": null,
  "last_error": null,
  "created_at": "2026-03-02T10:00:00Z",
  "updated_at": "2026-03-02T10:00:00Z"
}
```

There is one GitHub configuration, and it is addressed by its type, `github`, not by the `id`: `GET`, `PUT` and `DELETE` use `/api/v1/integrations/github`, and the webhook URL below has no id in it either. The request and response are described in [Create Integration](../api/integrations.md#create-integration).

---

## Configuration Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `token` | string | Yes | GitHub PAT or GitHub App installation token, stored for calls to GitHub, which nothing in the platform makes yet. |
| `repo_owner` | string | Yes | Owner (user or organisation) of the repository |
| `repo_name` | string | Yes | Repository name |
| `webhook_secret` | string | Yes | A secret string used to verify incoming webhook payloads. Must match what you set in GitHub's webhook configuration. |

Neither the `token` nor the `webhook_secret` is returned in any response: a response shows `repo_owner` and `repo_name` and lists the names of the other keys in `stored_secrets` (see [What a response shows](../api/integrations.md#what-a-response-shows)). Both are stored in the database as given; they are not encrypted.

---

## Configuring the Webhook in GitHub

1. Add a webhook to your GitHub repository (**Settings → Webhooks → Add webhook**)
2. Set the **Payload URL** to:
   ```text
   https://your-platform.example.com/api/v1/integrations/webhooks/github
   ```
3. Set **Content type** to `application/json`
4. Set the **Secret** to the same value you used as `webhook_secret` when creating the integration (`printf '%s\n' "$WEBHOOK_SECRET"` prints it)
5. Under **Which events would you like to trigger this webhook?**, select:
   - Individual events: `Push`, `Pull requests`, `Issues`
   - Or "Send me everything" if you want all events
6. Ensure **Active** is checked and click **Add webhook**

---

## Webhook Endpoint

```text
POST /api/v1/integrations/webhooks/github
```

### Headers

The platform reads these headers of a delivery:

| Header | What the platform does with it |
|--------|-------------|
| `X-Hub-Signature-256` | HMAC-SHA256 signature of the raw request body: **required** |
| `X-GitHub-Event` | The event type (e.g., `push`, `pull_request`, `issues`); optional |
| `X-GitHub-Delivery`, `Content-Type` | Not checked |

### HMAC-SHA256 Signature Validation

The platform validates every incoming webhook using the `X-Hub-Signature-256` header. The validation algorithm:

```text
expected_signature = "sha256=" + HMAC-SHA256(webhook_secret, raw_request_body)
provided_signature = X-Hub-Signature-256 header value

if not constant_time_compare(expected_signature, provided_signature):
    return 401 Unauthorized
```

Requests with a missing or invalid `X-Hub-Signature-256` header receive `401 Unauthorized` and are not processed. This prevents spoofed webhook deliveries.

Every refused delivery gets the same answer, whatever the reason: `401` with the body `{"detail": "Webhook authentication failed"}`. A wrong or missing signature, an integration that is not active, an integration with no `webhook_secret`, and no GitHub integration at all are not told apart.

### Sending a signed delivery

GitHub signs each delivery for you. To see the check pass, sign one the same way, with the secret created above: the HMAC-SHA256 of the raw body, keyed with the `webhook_secret`, in hex, after `sha256=`:

```{.bash exec}
BODY='{"action": "opened", "number": 42, "pull_request": {"title": "feat: Add new checkout flow experiment"}}'
SIGNATURE="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"

curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: 72d3162e-cc78-11e3-81ab-4c9367dc0958" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data-binary "$BODY" | jq -c .
```
<!-- expect: {"status":"received"} -->

It prints `{"status":"received"}`. `printf '%s'` adds no newline and `--data-binary` sends the body byte for byte, so the signature covers exactly what is sent. `openssl dgst` prints the digest as the last field of its line (after `SHA2-256(stdin)= ` with OpenSSL 3, on its own with LibreSSL), which `awk` keeps.

The same signature on a body with one byte changed (`43` for `42`) is refused, and so is the body with no signature:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data-binary "${BODY/42/43}" | jq -c .

curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  --data-binary "$BODY" | jq -c .
```
<!-- expect: {"detail":"Webhook authentication failed"} -->
<!-- expect: {"detail":"Webhook authentication failed"} -->

Each prints `{"detail":"Webhook authentication failed"}`.

---

## Supported Event Types

### Push Events

An example `push` delivery:

```json
{
  "ref": "refs/heads/main",
  "commits": [
    {
      "id": "abc123",
      "message": "feat: add new checkout flow",
      "author": {"name": "Jane Smith", "email": "jane@example.com"},
      "url": "https://github.com/your-org/your-repo/commit/abc123"
    }
  ],
  "repository": {
    "full_name": "your-org/your-repo"
  }
}
```

### Pull Request Events

An example `pull_request` delivery:

```json
{
  "action": "opened",
  "number": 42,
  "pull_request": {
    "title": "feat: Add new checkout flow experiment",
    "html_url": "https://github.com/your-org/your-repo/pull/42",
    "head": {"ref": "feat/checkout-experiment", "sha": "abc123"},
    "base": {"ref": "main"},
    "body": "experiment_key: checkout-flow-v2"
  },
  "repository": {
    "full_name": "your-org/your-repo"
  }
}
```

### Issues Events

An example `issues` delivery:

```json
{
  "action": "opened",
  "issue": {
    "number": 101,
    "title": "Track experiment: checkout-flow-v2",
    "html_url": "https://github.com/your-org/your-repo/issues/101",
    "body": "Track the progress of the checkout flow experiment.",
    "labels": [{"name": "experiment"}]
  }
}
```

**Response: 200 OK**

```json
{"status": "received"}
```

A delivery from an authenticated sender is answered `200` even when the platform could not process the event: the failure is logged, so the provider does not retry something it cannot fix. A body that is not a JSON object is `400 Bad Request`, and that is checked only after the sender is authenticated.

An answered delivery changes nothing in the platform yet: a pull request is not linked to an experiment, whatever its body says.

---

## Troubleshooting

### 401 Unauthorized

**Symptom**: GitHub's delivery log shows status `401` and the body `{"detail": "Webhook authentication failed"}`.

Every refused delivery is answered the same way, so the status does not say which of these it is:

1. **Secret mismatch**: The `webhook_secret` in the platform does not match the secret in GitHub's webhook settings. Set it again with the recipe in [Rotating the Webhook Secret](#rotating-the-webhook-secret) and use the same value in GitHub. As a last resort you can `DELETE /api/v1/integrations/github` and create the integration again, but that removes the stored `token` and repository settings too, so you send them again.
2. **No secret in GitHub**: a delivery without an `X-Hub-Signature-256` header is refused. Open the webhook's settings in GitHub and check its **Secret** field.
3. **Not active, or no `webhook_secret` stored**: `GET /api/v1/integrations/github` must show `"is_active": true` and list `webhook_secret` in `stored_secrets`. The platform log also names an active integration that has no secret, once per process.
4. **Encoding issue**: Ensure the secret does not contain leading or trailing whitespace. Copy-paste carefully.

### 400 Bad Request

**Symptom**: GitHub's delivery log shows status `400` with the body `{"detail": "Invalid JSON payload"}`.

The signature was accepted, but the body is not a JSON object. Confirm the webhook's **Content type** is `application/json`, not `application/x-www-form-urlencoded`.

### Webhook Deliveries Not Reaching the Platform

1. Confirm the webhook payload URL is reachable from the public internet
2. Check GitHub's webhook delivery logs: **Repository → Settings → Webhooks → [Your Webhook] → Recent Deliveries**
3. Verify the integration is active: `GET /api/v1/integrations/github` (an ADMIN or a DEVELOPER can call it) and check that `is_active` is `true`
4. Check the platform's application logs for any processing errors

### Rotating the Webhook Secret

If you need to rotate the webhook secret:

Step 1: Set a new secret in the platform. Use the recipe in [Upgrading an integration created before webhook authentication](../api/integrations.md#upgrading-an-integration-created-before-webhook-authentication) with `TYPE=github` and an ADMIN token. It sends one `PUT /api/v1/integrations/github` that carries only the new `webhook_secret`; the stored `token` and repository settings are kept, and it prints the new secret.

Step 2: Set the same secret in the GitHub webhook's settings at once: until you do, GitHub's deliveries are refused with `401`.

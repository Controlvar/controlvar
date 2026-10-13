# Salesforce Integration

!!! info "Part of the `integrations` module"
    Third-party integrations is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

The Salesforce integration receives the events a Salesforce Flow or Apex callout sends to the platform, and authenticates each one. It also stores your Connected App's credentials for calls to Salesforce, but nothing in the platform calls Salesforce yet, and a delivery is acknowledged without changing anything in the platform.

---

## What the Integration Does

- **Outbound (Platform → Salesforce)**: not wired yet. Nothing in the platform calls Salesforce, so no experiment's status or result reaches a Salesforce record; the Connected App's credentials are stored but not used.
- **Inbound (Salesforce → Platform)**: Receive events from Salesforce via webhook, posted as JSON by a Flow HTTP Callout, an Apex callout or a relay. The platform authenticates and answers each delivery; nothing in the platform acts on the event yet.

---

## Prerequisites


The Connected App's credentials are stored for calls to Salesforce, which nothing in the platform makes yet, so an integration that only receives webhooks needs nothing but a `webhook_secret`.

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

This generates a webhook secret, keeps it in `$WEBHOOK_SECRET`, and creates the integration with it. The instance URL and the Connected App's key and secret are examples: use your own.

```{.bash exec}
WEBHOOK_SECRET=$(openssl rand -hex 32)

jq -n --arg secret "$WEBHOOK_SECRET" '{
    integration_type: "salesforce",
    is_active: true,
    encrypted_config: {
      instance_url: "https://your-org.my.salesforce.com",
      client_id: "3MVG9...",
      client_secret: "1234567890ABCDEF...",
      webhook_secret: $secret
    }
  }' \
  | curl -s -X POST http://localhost:8000/api/v1/integrations \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data @- \
  | jq .
```
<!-- expect: "integration_type": "salesforce" -->
<!-- expect: "is_active": true -->
<!-- expect: "instance_url": "https://your-org.my.salesforce.com" -->
<!-- expect: "client_secret", -->
<!-- expect: "webhook_secret" -->

**Response: 201 Created**

```json
{
  "id": "3f1a9c62-6f5e-4a3b-9a0c-6d2b8e7f1a45",
  "integration_type": "salesforce",
  "is_active": true,
  "encrypted_config": {
    "instance_url": "https://your-org.my.salesforce.com",
    "client_id": "3MVG9..."
  },
  "stored_secrets": ["client_secret", "webhook_secret"],
  "last_sync_at": null,
  "last_error": null,
  "created_at": "2026-03-02T10:00:00Z",
  "updated_at": "2026-03-02T10:00:00Z"
}
```

There is one Salesforce configuration, and it is addressed by its type, `salesforce`, not by the `id`: `GET`, `PUT` and `DELETE` use `/api/v1/integrations/salesforce`, and the webhook URL below has no id in it either. The request and response are described in [Create Integration](../api/integrations.md#create-integration).

---

## Configuration Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `instance_url` | string | Yes | Your Salesforce instance URL, e.g., `https://your-org.my.salesforce.com` |
| `client_id` | string | Yes | OAuth 2.0 Consumer Key from the Connected App |
| `client_secret` | string | Yes | OAuth 2.0 Consumer Secret from the Connected App |
| `webhook_secret` | string | Yes | A secret you choose (for example `openssl rand -hex 32`). An inbound delivery to the webhook endpoint below must present it; see [Webhook Endpoints](../api/integrations.md#webhook-endpoints) |
| `access_token` | string | No | Not read by the platform; you do not need to supply it. |

The Salesforce client in the platform is written for the **OAuth 2.0 Client Credentials** flow, and nothing calls it yet. The credentials are stored in the database as given; they are not encrypted. The `client_secret`, an `access_token` and the `webhook_secret` are not returned in any response, not even masked: a response shows `instance_url` and `client_id` and lists the names of the other keys in `stored_secrets` (see [What a response shows](../api/integrations.md#what-a-response-shows)).

---

## Webhook Endpoint

To receive incoming events from Salesforce, configure a Salesforce Flow with an HTTP Callout (or an Apex callout, or a relay in front of the platform) to POST a JSON object to:

```text
POST /api/v1/integrations/webhooks/salesforce
```

### Configuring a Salesforce callout

Send the event from something that can post a JSON object and set a header: a Flow with an **HTTP Callout** action, an Apex callout (`HttpRequest.setHeader`), or a relay in front of the platform.

1. Build the sender in Salesforce, for example a Flow on the record change you care about with an **HTTP Callout** action, or set up your relay
2. Set the method to `POST` and the URL to your webhook URL:
   `https://your-platform.example.com/api/v1/integrations/webhooks/salesforce`
3. Set the `Content-Type` header to `application/json`
4. Set the body to a JSON object with the fields you want to include (see [Incoming Webhook Payload Format](#incoming-webhook-payload-format))
5. Add the integration's `webhook_secret` as a header on the callout, as described next

### Authenticating a delivery

The platform parses a delivery only after the sender has presented the integration's `webhook_secret`, in one of two forms. Without it the answer is `401` and the body is not parsed.

| How | Header | Value |
|-----|--------|-------|
| Shared secret | `X-Experimently-Webhook-Secret` | The `webhook_secret` itself |
| Signature, from a callout that can compute one | `X-Hub-Signature-256`, or `X-Hub-Signature` | `sha256=` followed by the hex HMAC-SHA256 of the raw request body, keyed with the `webhook_secret` (a SHA-1 value, `sha1=...`, is refused) |

Set the shared-secret header on the callout. The secret then travels with every delivery, so the endpoint must be HTTPS. If the callout can sign the raw body (in Apex, with `Crypto.generateMac`), send the signature instead. A delivery that carries either signature header is judged on the signature alone: a wrong signature is refused even when the shared-secret header is right. The body must be a JSON object: a SOAP/XML body is refused with `400` even when the secret is right.

Every refused delivery gets the same answer, `401` with the body `{"detail": "Webhook authentication failed"}`: a wrong or missing secret, an integration that is not active, an integration with no `webhook_secret`, and no Salesforce integration at all are not told apart. If every delivery is refused, check with `GET /api/v1/integrations/salesforce` that `is_active` is `true` and that `stored_secrets` lists `webhook_secret`. See [Webhook Endpoints](../api/integrations.md#webhook-endpoints) for the whole contract.

### Sending a delivery

To see both forms accepted, send a delivery with the secret created above. A Flow HTTP Callout sends the secret itself:

```{.bash exec}
BODY='{"event_type": "campaign_updated", "campaign_id": "701xx000000001AAAQ", "status": "Completed"}'

curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/salesforce \
  -H "Content-Type: application/json" \
  -H "X-Experimently-Webhook-Secret: $WEBHOOK_SECRET" \
  --data-binary "$BODY" | jq -c .
```
<!-- expect: {"status":"received"} -->

It prints `{"status":"received"}`. An Apex callout that signs sends the HMAC-SHA256 of the raw body, keyed with the `webhook_secret`, in hex, after `sha256=`:

```{.bash exec}
SIGNATURE="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"

curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/salesforce \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data-binary "$BODY" | jq -c .
```
<!-- expect: {"status":"received"} -->

It prints `{"status":"received"}`. `printf '%s'` adds no newline and `--data-binary` sends the body byte for byte, so the signature covers exactly what is sent. `openssl dgst` prints the digest as the last field of its line (after `SHA2-256(stdin)= ` with OpenSSL 3, on its own with LibreSSL), which `awk` keeps.

A wrong signature is refused even beside the right shared secret, and an XML body is refused with `400` even with the right secret:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/salesforce \
  -H "Content-Type: application/json" \
  -H "X-Experimently-Webhook-Secret: $WEBHOOK_SECRET" \
  -H "X-Hub-Signature-256: sha256=0000" \
  --data-binary "$BODY" | jq -c .

curl -s -X POST http://localhost:8000/api/v1/integrations/webhooks/salesforce \
  -H "Content-Type: text/xml" \
  -H "X-Experimently-Webhook-Secret: $WEBHOOK_SECRET" \
  --data-binary '<soapenv:Envelope/>' | jq -c .
```
<!-- expect: {"detail":"Webhook authentication failed"} -->
<!-- expect: {"detail":"Invalid JSON payload"} -->

They print `{"detail":"Webhook authentication failed"}` and `{"detail":"Invalid JSON payload"}`.

### Incoming Webhook Payload Format

The platform accepts JSON objects from a Salesforce Flow HTTP Callout, an Apex callout or a relay. Any JSON object is accepted, and nothing reads its fields yet. For example:

```json
{
  "event_type": "campaign_updated",
  "campaign_id": "701xx000000001AAAQ",
  "campaign_name": "Q1 Checkout Optimization",
  "status": "Completed",
  "experiment_key": "checkout-button-color"
}
```


**Response: 200 OK**

```json
{"status": "received"}
```

A delivery from an authenticated sender is answered `200` even when the platform could not process the event: the failure is logged, so the provider does not retry something it cannot fix. A body that is not a JSON object is `400 Bad Request`, and that is checked only after the sender is authenticated.

---

## Syncing to Salesforce

Nothing in the platform calls Salesforce yet: no experiment's status or result is written to a Salesforce record, and the `instance_url`, `client_id` and `client_secret` are stored for a sync that is not wired. The webhook above is the part of the integration that works today.

---

## Troubleshooting

### Webhook Not Receiving Events

1. Confirm the URL in the Salesforce callout (the Flow HTTP Callout, the Apex callout or the relay) matches your integration webhook URL exactly
2. Make sure the webhook URL is HTTPS and can be called from outside your network
3. Check the API's access log: it logs each delivery with its path and status, for example `"POST /api/v1/integrations/webhooks/salesforce HTTP/1.1" 401`
4. In Salesforce, check the callout's response in the Flow's debug details or the Apex debug log (**Setup → Debug Logs**): `401` means the secret header is missing or wrong, or the integration is not active; `400` means the body is not a JSON object

### Updating Integration Credentials

If you rotate your Salesforce Connected App credentials, send the new values with `PUT /api/v1/integrations/salesforce` and an ADMIN token. The integration is addressed by its type, not by an id. `PUT` merges `encrypted_config` key by key, so send only the keys that changed: the keys you leave out, such as `instance_url` and `webhook_secret`, are kept.

```{.bash exec}
curl -s -X PUT http://localhost:8000/api/v1/integrations/salesforce \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "encrypted_config": {
      "client_id": "3MVG9-new-key...",
      "client_secret": "new-secret..."
    }
  }' | jq '{encrypted_config, stored_secrets}'
```
<!-- expect: "client_id": "3MVG9-new-key..." -->
<!-- expect: "client_secret", -->
<!-- expect: "webhook_secret" -->

The response shows the new `client_id` beside the old `instance_url`, and still names `client_secret` and `webhook_secret`: the webhook keeps working with the secret it had.

The whole request is described in [Update Integration](../api/integrations.md#update-integration). To change the `webhook_secret` instead, use the recipe in [Upgrading an integration created before webhook authentication](../api/integrations.md#upgrading-an-integration-created-before-webhook-authentication) with `TYPE=salesforce`.

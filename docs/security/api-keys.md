# API Key Management

API keys give programs access to the platform: SDK clients, server-side applications and
scripts. Unlike a user's login token, which expires (after 12 hours by default), an API key
lasts until you delete it or it reaches its own `expires_at`. It is meant for
service-to-service calls.

The commands on this page run as written against the stack from the
[Quick Start](../getting-started/quick-start.md), in one terminal, top to bottom. Each one uses
the shell variables set by the ones before it.

---

## What API Keys Are Used For

- **SDK integration**: the SDKs authenticate with an API key to evaluate feature flags,
  assign users to experiments and track events.
- **Server-to-server calls**: backend services that track conversions or evaluate flags.
- **Event ingestion**: the tracking endpoints (`/api/v1/tracking/*`) take an API key.

An API key authenticates as the user who created it, and only on the endpoints an SDK
calls: tracking, flag evaluation and the flag ruleset. Creating or changing flags
and experiments, and managing users, take a user login; an API key there is refused with
`401` and `{"detail":"Not authenticated"}`, the answer to a request with no login at all.
API keys are not for signing people in: user-facing applications use the login
described in [Authentication](../api/auth.md).

---

## Creating an API Key

Creating a key takes a user login. Log in first; this saves the token in `$TOKEN`:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. Any role can create keys for itself.

Then create the key. The collection URL has no trailing slash: `/api/v1/api-keys/` answers
`307 Temporary Redirect`, which `curl` doesn't follow.

```{.bash exec}
CREATED=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Production Checkout Service",
    "description": "Used by the checkout microservice to evaluate feature flags"
  }')
KEY=$(jq -r .key <<<"$CREATED")
KEY_ID=$(jq -r .id <<<"$CREATED")

jq '{name, prefix}' <<<"$CREATED"
```
<!-- expect: "name": "Production Checkout Service" -->

The API answers `201 Created`. The response carries the key's `id`, `name`, `key`,
`prefix` (the key's first nine characters, `eptk_` and four more), `created_at` and
`expires_at`. This saves the key in `$KEY` and its id in `$KEY_ID`, and prints:

```json
{
  "name": "Production Checkout Service",
  "prefix": "eptk_af52"
}
```

**The `key` value is shown only once.** Only a hash of it is stored, so nobody can show it to
you again. Store it immediately in a secure location (a secrets manager, not a code
repository). If you lose it, create a new key.

The request takes:
- `name` (required, up to 100 characters);
- `description`;
- `scopes`, a list of names (see [Scopes](#scopes));
- `expires_at`, an ISO 8601 time after which the key stops working. Leave it out for a
  key that doesn't expire.

### Scopes

Scopes are stored with the key and returned when you list keys. **One scope is enforced:
`sdk:ruleset`.** Every endpoint that accepts an API key, except the three below, accepts
any active key, whatever its scopes. Names such as `read`, `write` or `admin` on existing
keys are labels only; nothing checks them.

`sdk:ruleset` is for a server: one that evaluates flags locally, or one that assigns lists
of users to an experiment. These routes answer `403` to a key that does not carry it:

- `GET /api/v1/sdk/ruleset` (beta), which returns every feature flag's targeting rules in the
  form a server-side SDK evaluates locally;
- `POST /api/v1/tracking/evaluations` (beta), where a server-side SDK reports how many times
  it evaluated each flag locally, so safety monitoring can still compute each flag's error
  rate;
- `POST /api/v1/tracking/assign/batch` (beta), which assigns up to 1,000 users to an
  experiment in one request (see
  [Assign a customer list to an experiment](../guides/assign-customer-list.md)).

The `403` says
`This API key does not have the 'sdk:ruleset' scope. Create a key with the 'sdk:ruleset' scope for server-side SDK use.`

A key with `sdk:ruleset` can therefore download every feature flag's targeting rules,
including the values in them, and the values string, number and JSON flags serve, so keep
such a key on a server and never ship it to a browser or a mobile app.

- In the dashboard (**Admin → API Keys → Create API Key**), tick **Server-side local
  evaluation (sdk:ruleset)**; the same checkbox is the one for assigning lists of users.
  Leave it unticked for any other key; the key is then created
  with no scopes. The dashboard's API Keys page needs a superuser account; any other user
  creates keys through the API.
- Through the API, send `"scopes": ["sdk:ruleset"]`.

**Only users who can change feature flags can create a key with the `sdk:ruleset` scope:**
the ADMIN and DEVELOPER roles, and superusers. For anyone else (ANALYST or VIEWER) the
request is refused with `403 Forbidden`, whose message names the roles that may create such
a key, and no key is created. Keys without that scope can be created by every role, as
before. Using such a key follows the same rule: both routes above accept a key with
`sdk:ruleset` only while its owner can change feature flags, checked on every request, so a
key whose owner is moved to ANALYST or VIEWER is refused with `403` from then on.

Scope names are matched exactly and case-sensitively: the stored list is split on commas and
each entry is trimmed, so `"read, sdk:ruleset "` carries `sdk:ruleset`, while `SDK:RULESET`,
`xsdk:ruleset` and `sdk:ruleset-ro` do not.

---

## Using an API Key

Pass the key in the `X-API-Key` header on every request. This evaluates the demo data's
`beta_features` flag for one user:

```{.bash exec}
curl -s -G localhost:8000/api/v1/feature-flags/evaluate/beta_features \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-123" \
  --data-urlencode 'context={"plan":"pro"}' | jq '{key, enabled}'
```
<!-- expect: "key": "beta_features" -->
<!-- expect: "enabled": true -->

It prints `"enabled": true`: the flag is on for everyone.

This tracks a purchase by the same user in the demo data's `checkout_button_color`
experiment. A tracked event names the experiment (`experiment_key`) or the flag
(`feature_flag_key`) it belongs to:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{
    "user_id": "user-123",
    "experiment_key": "checkout_button_color",
    "event_type": "purchase",
    "value": 49.99
  }' | jq '{event_type, value}'
```
<!-- expect: "event_type": "purchase" -->
<!-- expect: "value": 49.99 -->

It prints the stored event's `event_type` and `value`.

In an SDK, the key is a setting of the client. Load it from the environment rather than
writing it into the code:

```javascript
const client = new ExperimentationClient({
  apiUrl: 'https://your-platform.example.com',
  apiKey: process.env.EXPERIMENTATION_API_KEY,
});
```

```python
client = ExperimentationClient(
    api_url="https://your-platform.example.com",
    api_key=os.environ["EXPERIMENTATION_API_KEY"],
)
```

---

## Listing Keys

List your keys. The secret values are never shown:

```{.bash exec}
curl -s localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | {name, scopes, is_active}'
```
<!-- expect: "name": "Production Checkout Service" -->
<!-- expect: "is_active": true -->

It prints each of your keys, including the one created above:

```json
{
  "name": "Production Checkout Service",
  "scopes": [],
  "is_active": true
}
```

Each key also carries its `id`, `description`, `user_id`, `created_at`, `expires_at` and
`last_used_at`. The list holds only your own keys. An ADMIN can add `?all=true` to list
every user's keys. The list leaves out inactive keys unless you add
`?include_inactive=true`.

`last_used_at` is when the key last authenticated a request, and `null` for a key that was never
used. The API writes it at most once a minute per key, so it can trail the last request by up to
a minute. Use it to find keys that are no longer used.

---

## Deleting a Key

Delete a key when it is no longer needed, or if you suspect it has leaked. Only its owner
or an ADMIN can delete it:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8000/api/v1/api-keys/$KEY_ID \
  -H "Authorization: Bearer $TOKEN"
```
<!-- expect: 204 -->

It prints `204`: the key is deleted permanently, and can't be restored.

From then on, requests with the key are refused:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"user_id": "user-123", "experiment_key": "checkout_button_color", "event_type": "purchase"}'
```
<!-- expect: {"detail":"Invalid API Key"} -->

It prints `{"detail":"Invalid API Key"}`, with status `401 Unauthorized`. A service still
using the key starts failing at once, so delete a key only after its services use a new one.

---

## Key Rotation Best Practices

Rotate API keys on a regular schedule to limit the window of exposure if a key leaks.

### Recommended Rotation Schedule

| Environment | Recommended Rotation |
|-------------|---------------------|
| Production | Every 90 days |
| Staging / Development | Every 180 days |
| CI/CD pipelines | On every major deployment |

Setting `expires_at` when you create a key makes the schedule hard to forget: the key stops
working on that date.

### Zero-Downtime Rotation Procedure

1. Create a new API key with the same name and scopes as the old key.
2. Update your service's secrets (Secrets Manager, environment variables, and so on) to the
   new key.
3. Deploy or restart the service so it uses the new key.
4. Check that the service works with the new key.
5. Delete the old key.

The service always has a valid key during the rotation.

---

## Security Best Practices

### Never commit keys to source control

API keys in source code can be exposed in logs, error messages or git history. Load keys
from environment variables or a secrets manager:

```javascript
apiKey: process.env.EXPERIMENTATION_API_KEY
```

and never write the key itself into the code.

### Use a secrets manager

Store production API keys in a dedicated secrets manager:

- **AWS Secrets Manager**: integrates with ECS task definitions and Lambda environment variables
- **HashiCorp Vault**: for multi-cloud or on-premise setups
- **Kubernetes Secrets**: for Kubernetes-based deployments

### Separate keys per service

Use one API key per service or application, and name it after the service. That limits the
blast radius of a leaked key: you can delete that service's key without affecting the
others.

```text
checkout-service-prod    → eptk_aaaa…
recommendations-prod     → eptk_bbbb…
analytics-pipeline       → eptk_cccc…
```

### Grant `sdk:ruleset` only where it is needed

Give the `sdk:ruleset` scope only to a server that will evaluate flags locally or assign
lists of users. Every other key, including any key used in a browser or a mobile app, should be created without it.
Because no other scope limits a key, a separate key per service is what lets you revoke one
integration without touching the others.

### Remember that a key acts as its owner

A key authenticates as the user who created it. Create keys from an account whose role is
no broader than the services need, and delete the keys of a user who leaves.

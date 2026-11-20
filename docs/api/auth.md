# Authentication

The platform uses two authentication methods, depending on who is calling:

| Method | Used For |
|--------|---------|
| **API Key** (`X-API-Key` header) | SDK clients, server-to-server calls, event tracking; with the `mcp:read` scope, reading flags, experiments and results |
| **Bearer token** (`Authorization: Bearer` header) | People: the dashboard, and direct API calls made as a user |

The commands on this page run as written against the stack from the
[Quick Start](../getting-started/quick-start.md), in one terminal, top to bottom. Each one uses
the shell variables set by the ones before it.

---

## Bearer Token Authentication

A bearer token is issued when a user logs in, and authenticates later requests as that user.

Where the login is checked depends on `AUTH_PROVIDER`:

- **`local`** (the default, and what the Quick Start runs): the platform checks the email
  address and password against its own users. Its tokens last 12 hours
  (`LOCAL_AUTH_TOKEN_TTL_MINUTES`, default `720`). After 10 failed attempts within 15
  minutes, the address answers `423` until the 15 minutes are up.
  The email address is matched whatever its letter case (A–Z), and every casing of it counts toward the same failed attempts.
  An account with [two-step sign-in](#two-step-sign-in) on also gives a code from its
  authenticator app.
- **`cognito`**: the credentials are passed to an AWS Cognito user pool, which issues an
  access token and a refresh token. Users are created by an administrator
  ([Adding a user](../cognito_integration.md#adding-a-user)).
  The dashboard does not yet sign in with Cognito. Sign in through the API with
  `POST /api/v1/auth/token`. SSO sign-in needs `AUTH_PROVIDER=local`.

### Obtaining a Token

Log in with the account's email address and password. This saves the token in `$TOKEN`
and prints what else the response says:

```{.bash exec}
LOGIN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}')
TOKEN=$(jq -r .access_token <<<"$LOGIN")

jq '{token_type, expires_in}' <<<"$LOGIN"
```
<!-- expect: "token_type": "bearer" -->
<!-- expect: "expires_in": 43200 -->

It prints `"token_type": "bearer"` and `"expires_in": 43200`, the token's lifetime in
seconds (12 hours). The response also carries the user's profile as `user`.

A wrong email address or password answers `401` with `{"detail":"Invalid email or password"}`.
`POST /api/v1/auth/login` takes JSON with `email` and `password`.
`POST /api/v1/auth/token` takes the same two as a form, with the email address in the
`username` field. That's the OAuth2 form the API's interactive documentation at
`localhost:8000/api/v1/docs` uses for its **Authorize** button.

### Check Who You Are Logged In As

```{.bash exec}
curl -s localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer $TOKEN" | jq '{email, role}'
```
<!-- expect: "email": "admin@demo.com" -->
<!-- expect: "role": "ADMIN" -->

```json
{
  "email": "admin@demo.com",
  "role": "ADMIN"
}
```

The profile also carries `id`, `username`, `full_name`, `is_superuser`, `is_active` and
`auth_provider`.

### Using a Bearer Token

Pass the token in the `Authorization` header. This lists the experiments; the demo data
has three:

```{.bash exec}
curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" | jq -r '.items[].key'
```
<!-- expect: checkout_button_color -->

It prints each experiment's key, including `checkout_button_color`.

The collection URL ends with a slash, `/api/v1/experiments/`. Without the slash the API
answers `307 Temporary Redirect`, which `curl` doesn't follow, so nothing is printed.

### Token Expiry, Refresh and Logout

With the `local` provider there is no refresh token: when a token expires, requests answer
`401` and the user logs in again. `POST /api/v1/auth/logout` answers `204` so a client can
discard its token. The token itself stays valid until it expires, because nothing is
revoked on the server.

With `AUTH_PROVIDER=cognito`, the login also returns a refresh token, and
`POST /api/v1/auth/refresh` with `{"refresh_token": "..."}` exchanges it for a new access
token. If the refresh token has expired too, the user logs in again.

---

## API Key Authentication

API keys are long-lived credentials for programs. Use them when:

- integrating an SDK into your application;
- making server-to-server API calls from a backend service;
- tracking events from a client application.

### Creating an API Key

Creating a key takes a bearer token. This saves the new key in `$KEY`:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Production App Key", "description": "Used by the checkout service"}' \
  | jq -r .key)

echo "${KEY:0:5}"
```
<!-- expect: eptk_ -->

It prints `eptk_`, the start of every key. The key is shown only in this response, so store
it securely. If you lose it, create a new key and delete the old one. See
[API Key Management](../security/api-keys.md) for listing, deleting and rotating keys.

The request also accepts an optional `"scopes"` list. Two scopes are enforced.
`sdk:ruleset` is for server-side SDK use: `GET /api/v1/sdk/ruleset`,
`POST /api/v1/tracking/evaluations` and `POST /api/v1/tracking/assign/batch` (all beta)
refuse a key without it, or whose owner can no longer change feature flags; batch assignment
also refuses a key whose owner cannot change experiments. Only users who can change feature
flags (ADMIN, DEVELOPER, a superuser, or in the full profile anyone whose custom role or direct
grant gives `feature_flag (update)`) can create a key with it; anyone else gets `403` and no
key is created. `mcp:read` lets a key read flags, experiments and results on four read
operations, listed below; any role can create a key with it. Every other endpoint that takes an
API key accepts any active key, whatever its scopes; see
[Scopes](../security/api-keys.md#scopes).

### Using an API Key

Pass the key in the `X-API-Key` header on every request. This tracks a purchase in the demo
data's `checkout_button_color` experiment:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{
    "user_id": "user-123",
    "experiment_key": "checkout_button_color",
    "event_type": "purchase_completed",
    "value": 49.99
  }' | jq .event_type
```
<!-- expect: "purchase_completed" -->

It prints `"purchase_completed"`, the stored event's type.

And this evaluates the demo data's `beta_features` flag for the same user:

```{.bash exec}
curl -s -G localhost:8000/api/v1/feature-flags/evaluate/beta_features \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-123" \
  --data-urlencode 'context={"plan":"pro"}' | jq .enabled
```
<!-- expect: true -->

It prints `true`.

An API key is accepted by the endpoints an SDK calls: tracking, flag evaluation and the
flag ruleset. A key that carries the `mcp:read` scope is also accepted by four read
operations: `GET /api/v1/feature-flags/`, `GET /api/v1/experiments/`,
`GET /api/v1/experiments/{experiment_id}` and `GET /api/v1/experiments/{experiment_id}/results`.
There it reads as the user who created the key, under that user's role, and a key without
the scope is refused with `403`. Everything else takes a bearer token.

---

## Roles and Permissions

Every user has one of four built-in roles. The role decides which operations are available;
in the full profile a custom role or direct grant can add more.

| Role | Description | Typical Users |
|------|-------------|---------------|
| **ADMIN** | Every resource; managing user accounts also needs the separate superuser flag | Platform owners, engineering leads |
| **DEVELOPER** | Create and manage experiments and feature flags | Engineers, product developers |
| **ANALYST** | Read experiments, flags and results; create reports | Data analysts, product managers |
| **VIEWER** | Reads experiments, flags and results; like every role, can create an API key, and an API key writes tracking data | Stakeholders, executives |

### What Each Role Can Do

| Action | VIEWER | ANALYST | DEVELOPER | ADMIN |
|--------|--------|---------|-----------|-------|
| View experiments, flags and results | Yes | Yes | Yes | Yes |
| Create and change experiments | — | — | Yes | Yes |
| Create and change feature flags | — | — | Yes | Yes |
| Create their own API keys | Yes | Yes | Yes | Yes |
| View audit logs | Own | All | Own | All |
| Manage users | — | — | — | Only with the superuser flag |

Access to a feature flag is by permission, not by who created it: a DEVELOPER can change any
flag, and a VIEWER can change none, not even one they created, unless a custom role or direct
grant (full profile) gives them `feature_flag (update)`.

To see a refusal, create a VIEWER account as the administrator:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/users/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"email": "viewer@example.com", "username": "viewer", "password": "Viewer1234!", "role": "VIEWER"}' \
  | jq '{email, role}'
```
<!-- expect: "role": "VIEWER" -->

It prints the new account's `email` and `"role": "VIEWER"`. A password needs at least eight
characters and at most 72 bytes (UTF-8; most characters outside English take two or more
bytes), with an upper-case letter, a lower-case letter and a digit. A longer or weaker one
answers `422`.

Log in as the viewer, and try to create a flag:

```{.bash exec}
VIEWER_TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"viewer@example.com","password":"Viewer1234!"}' | jq -r .access_token)

curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "viewer-flag", "name": "Viewer Flag"}'
```
<!-- expect: {"detail":"You don't have permission to create feature flags"} -->

The API answers `403 Forbidden` with `{"detail":"You don't have permission to create feature flags"}`.

### Change Your Own Password

Every user changes their own password with `POST /api/v1/users/me/password`, which asks for
the current one. A wrong current password is refused:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/users/me/password \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"current_password": "Not-the-password1", "new_password": "Viewer5678!"}'
```
<!-- expect: {"detail":"The current password is incorrect."} -->

It answers `403` with `{"detail":"The current password is incorrect."}`. That is `403`, not
`401`: the session is still signed in. Wrong current passwords count toward the same limit as
failed logins, so after 10 of them within 15 minutes both answer `423`.

With the right one, the password changes and the response is an empty `204`:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/v1/users/me/password \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"current_password": "Viewer1234!", "new_password": "Viewer5678!"}'
```
<!-- expect: 204 -->

It prints `204`. The new password follows the same rules as a new account's. Changing it
doesn't sign out other sessions: a token issued before the change keeps working until it
expires. To end them straight away, an administrator deactivates the account
(`"is_active": false`).

Sending your own `password` to `PUT /api/v1/users/{id}` or `PUT /api/v1/admin/users/{id}` is
refused with `403`, for administrators too. Those routes set **another** account's password:
a superuser resets a password there, without knowing the old one.

With the modules installed, admins can also create custom roles with fine-grained
permissions, and grant temporary permissions to individual users. They add permissions to
the base role in every permission check, from the user's next request, and never remove any.
See the [RBAC API Reference](rbac.md).

### Two-step sign-in

With `AUTH_PROVIDER=local`, any account can turn on two-step sign-in (2FA, using TOTP codes):
after the password, sign-in also asks for the 6-digit code an authenticator app shows, or for
one of the account's recovery codes. Each account can turn it on for itself, and an operator
can require it for chosen roles: [Requiring it for a role](#requiring-it-for-a-role).

Turn it on as the viewer. Ask for a secret for the authenticator app. It asks for your current
password, as changing your password does:

```{.bash exec}
ENROL=$(curl -s -X POST localhost:8000/api/v1/users/me/2fa/enroll \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"current_password": "Viewer5678!"}')
SECRET=$(jq -r .secret <<<"$ENROL")

jq -r .otpauth_uri <<<"$ENROL" | cut -d '?' -f 1
```
<!-- expect: otpauth://totp/Controlvar:viewer@example.com -->

It prints the start of the `otpauth://` link a QR code carries. The whole link, or the
`secret` typed in, adds the account to an authenticator app. Nothing changes at sign-in yet.

Confirm with the app's current code. Here Python stands in for the app:

```{.bash exec}
CODE=$(python3 -c 'import base64,hmac,struct,sys,time; k=base64.b32decode(sys.argv[1]+"="*(-len(sys.argv[1])%8)); h=hmac.digest(k,struct.pack(">Q",int(time.time()//30)),"sha1"); o=h[19]&15; print(str((int.from_bytes(h[o:o+4],"big")&0x7FFFFFFF)%1000000).zfill(6))' "$SECRET")

CONFIRM=$(curl -s -X POST localhost:8000/api/v1/users/me/2fa/confirm \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"code\": \"$CODE\"}")

jq '.recovery_codes | length' <<<"$CONFIRM"
```
<!-- expect: 10 -->

It prints `10`: two-step sign-in is on, and the answer holds 10 recovery codes, shown this
once. Each signs in one time when the app is not at hand.

Sign in again. The right password now answers `mfa_required` and a short `mfa_token`, with no
`access_token`:

```{.bash exec}
STEP1=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"viewer@example.com","password":"Viewer5678!"}')
MFA_TOKEN=$(jq -r .mfa_token <<<"$STEP1")

jq '{mfa_required, expires_in, access_token}' <<<"$STEP1"
```
<!-- expect: "mfa_required": true -->
<!-- expect: "expires_in": 300 -->
<!-- expect: "access_token": null -->

Send the `mfa_token` back within `expires_in` seconds with a `code` from the app, or, as here,
a `recovery_code`:

```{.bash exec}
RECOVERY=$(jq -r '.recovery_codes[0]' <<<"$CONFIRM")

curl -s -X POST localhost:8000/api/v1/auth/mfa/verify \
  -H 'content-type: application/json' \
  -d "{\"mfa_token\": \"$MFA_TOKEN\", \"recovery_code\": \"$RECOVERY\"}" | jq -r .user.email
```
<!-- expect: viewer@example.com -->

It prints `viewer@example.com`: the answer is the one `POST /api/v1/auth/login` gives an account
without two-step sign-in, with `access_token`. A code or recovery code works once:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/auth/mfa/verify \
  -H 'content-type: application/json' \
  -d "{\"mfa_token\": \"$MFA_TOKEN\", \"recovery_code\": \"$RECOVERY\"}"
```
<!-- expect: {"detail":"That code is not correct."} -->

```{.bash exec}
curl -s localhost:8000/api/v1/users/me/2fa -H "Authorization: Bearer $VIEWER_TOKEN"
```
<!-- expect: {"enabled":true,"required":false,"recovery_codes_remaining":9} -->

Turn it off with a code or a recovery code. It answers an empty `204`:

```{.bash exec}
SECOND=$(jq -r '.recovery_codes[1]' <<<"$CONFIRM")

curl -s -X POST localhost:8000/api/v1/users/me/2fa/disable \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"recovery_code\": \"$SECOND\"}"

curl -s localhost:8000/api/v1/users/me/2fa -H "Authorization: Bearer $VIEWER_TOKEN"
```
<!-- expect: {"enabled":false,"required":false,"recovery_codes_remaining":0} -->

`$VIEWER_TOKEN` was issued before two-step sign-in was turned on, and it kept working
throughout: a token stays valid until it expires.

| Route | What it does |
|---|---|
| `GET /api/v1/users/me/2fa` | `enabled`, `required` (your role requires it) and `recovery_codes_remaining` |
| `POST /api/v1/users/me/2fa/enroll` | `{"current_password"}`: a new secret, waiting for its first code; a waiting one is replaced. `409` once on |
| `POST /api/v1/users/me/2fa/confirm` | `{"code"}`: turns it on and answers 10 recovery codes. `409` with no secret waiting |
| `POST /api/v1/users/me/2fa/recovery-codes` | `{"code"}`: 10 new recovery codes; the earlier ones stop working |
| `POST /api/v1/users/me/2fa/disable` | `{"code"}` or `{"recovery_code"}`: turns it off. `409` while off; `403` when your role requires it |
| `POST /api/v1/auth/mfa/verify` | `{"mfa_token"}` and `{"code"}` or `{"recovery_code"}`: the access token |
| `POST /api/v1/auth/mfa/enroll` | `{"mfa_token"}` of a password step that answered `enrol: true`: a new secret, waiting for its first code |
| `POST /api/v1/auth/mfa/enroll/confirm` | That `{"mfa_token"}` and `{"code"}`: turns it on and answers 10 recovery codes and the access token. `409` with no secret waiting |
| `DELETE /api/v1/admin/users/{user_id}/2fa` | A superuser turns off another account's two-step sign-in; your own answers `400` |

A code is 6 digits and a recovery code 10 characters, `A`-`Z` and `2`-`7`. Spaces and hyphens
are ignored, and so is a recovery code's letter case. A field longer than 64 characters, or
both fields or neither, answers `422`. Anything else that is not a right, unused code counts as
a wrong one.

| Answer | When |
|---|---|
| `401` `That code is not correct.` | A wrong code at `POST /api/v1/auth/mfa/verify`; `403` with the same text on the `/api/v1/users/me/2fa` routes |
| `401` `Sign-in expired. Start again.` | The `mfa_token` is expired or not one; or the account was deactivated, or its two-step sign-in turned off, since the password step. At the two enrol routes, also when its role no longer requires it or it was turned on meanwhile |
| `423` with `Retry-After` | Wrong passwords and wrong codes together: 10 within 15 minutes lock the address, as for sign-in |
| `423` `Two-step sign-in is locked after too many wrong codes. Ask an administrator to reset it.` | 100 wrong codes in a row; even the right code is refused until the reset |
| `503` `Two-step sign-in is unavailable for this account. Ask an administrator to reset it.` | The stored secret cannot be read, as after `SECRET_KEY` changes |
| `403` `Two-step sign-in is required for your role, so it cannot be turned off.` | `POST /api/v1/users/me/2fa/disable` for an account whose role requires it; no code is checked |

`POST /api/v1/users/me/2fa/enroll` answers as `POST /api/v1/users/me/password` does about the
current password, in this order:

| Answer | When |
|---|---|
| `423` with `Retry-After` | The address is locked by wrong passwords and codes, even for the right password |
| `403` `This account does not sign in with a password, so two-step sign-in does not apply to it.` | The account has no password of its own; not counted |
| `403` `Enter your current password (current_password) to set up two-step sign-in.` | No body, no `current_password` or an empty one; not counted |
| `403` `The current password is incorrect.` | A wrong password; it counts toward the same limit as failed logins |

`POST /api/v1/auth/mfa/verify`, `POST /api/v1/auth/mfa/enroll` and
`POST /api/v1/auth/mfa/enroll/confirm` each take 10 requests a minute per address, like
`POST /api/v1/auth/login`, and `POST /api/v1/users/me/2fa/enroll` 5, like
`POST /api/v1/users/me/password`. `POST /api/v1/auth/token` answers `mfa_required` too, so the
**Authorize** button of the interactive documentation cannot sign in such an account: sign in
with the two calls above and paste the access token. A script should use an
[API key](#api-key-authentication), not a person's password.

A superuser resets an account that lost its app and its recovery codes with
`DELETE /api/v1/admin/users/{user_id}/2fa`. An operator resets one, or every account, from the
command line: [Resetting two-step sign-in](../auth/auth-user-guide.md#resetting-two-step-sign-in).

Known limits:

- A token issued before two-step sign-in was turned on, off or reset keeps working until it
  expires.
- The count of wrong passwords and codes is kept in each API process's memory, so each process
  counts its own; the count of 100 wrong codes is stored with the account.

#### Requiring it for a role

`LOCAL_AUTH_2FA_REQUIRED_ROLES` names the roles whose accounts must use two-step sign-in:
one or more of `ADMIN`, `DEVELOPER`, `ANALYST` and `VIEWER`, in capitals and comma-separated,
such as `ADMIN,DEVELOPER`. Unset or empty, no role is required. Any other entry, such as
`admin`, `ADMNI` or the empty one in `ADMIN,`, stops the API starting with a message that names
it. It applies to every account of a listed role that signs in with a password, superusers
included.

Such an account that has not set two-step sign-in up gets `{"mfa_required": true,
"mfa_token": "...", "enrol": true, "expires_in": 600}` from the right password, with no
`access_token`. It sets it up before it is signed in: `POST /api/v1/auth/mfa/enroll` with that
`mfa_token` answers a new secret, as `POST /api/v1/users/me/2fa/enroll` does, and
`POST /api/v1/auth/mfa/enroll/confirm` with the same `mfa_token` and the app's first `code`
turns it on and answers `{"recovery_codes": [...], "login": {...}}`, where `login` is what
`POST /api/v1/auth/login` answers an account without two-step sign-in. The dashboard does both
as part of signing in. From then on the account signs in with a code, and
`POST /api/v1/users/me/2fa/disable` answers it `403`. `GET /api/v1/users/me/2fa` says
`required: true`, except for an account with no password of its own. After a superuser's reset,
the next sign-in asks for set-up again.

The Docker Compose file passes it from `.env`, and the Helm chart takes it in `api.extraEnv`
(the chart's `values.yaml` shows how). The AWS CDK app takes it as the context
`-c local_auth_2fa_required_roles=ADMIN,DEVELOPER` on `cdk deploy`, and refuses it together
with `-c cognito_hosted_ui=true`. Signing in with SSO
(the `sso` module) asks for no code, for these roles too.

#### Password sign-ins in this repository

These sign in with a password and read `access_token`. Each fails if its account turns two-step
sign-in on:

| Path | Its account, and what happens |
|---|---|
| `scripts/synthetic_check.py` | A dedicated ANALYST account, every 30 minutes: its sign-in step fails. Keep that account without two-step sign-in |
| `scripts/scale_stack.py` | The first superuser: its sign-in step fails |
| `deploy/aws-installer/lib/steps.sh` | The first administrator, straight after the install, before anyone can turn it on |
| `deploy/demo/probe.py` | The demo viewer; demo mode refuses turning two-step sign-in on |
| `.github/workflows/_platform.yml` | Seeded accounts that never turn it on |
| `.github/workflows/compose-production.yml` | Seeded accounts that never turn it on |
| `.github/workflows/pr-qa-gate.yml` | Seeded accounts that never turn it on |
| `scripts/compose_upgrade.sh` | Seeded accounts that never turn it on |
| `scripts/fuzz_local.sh` | Seeded accounts that never turn it on |
| `demo/streampulse/simulator/rollout_story.py` | Seeded accounts that never turn it on |
| `tests/ground_truth/driver.py` | Seeded accounts that never turn it on |
| `tests/sdk-contract/live/run_live_contract.py` | Seeded accounts that never turn it on |
| `tests/alb/rehearse.sh` | Seeded accounts that never turn it on |
| `tests/llm/smoke.sh` | Seeded accounts that never turn it on |
| `tests/acceptance/showcase/capture/run.py` | Seeded accounts that never turn it on |
| `tests/acceptance/docs/docs_runner/pacer.py` | Seeded accounts that never turn it on |
| `backend/tests/performance/locustfiles/common.py` | Seeded accounts that never turn it on |
| `modules/backend/tests/realistic/scenarios/multi_tenant_isolation.py` | Seeded accounts that never turn it on |

Each also fails if `LOCAL_AUTH_2FA_REQUIRED_ROLES` names its account's role on the stack it signs
in to: the password step then answers `enrol: true` and no `access_token`. Keep the seeded
accounts' roles unrequired on the stacks they sign in to. The others:

| Path | When its account's role is required |
|---|---|
| `scripts/synthetic_check.py` | Fails while ANALYST is required. Keep that role unrequired |
| `scripts/scale_stack.py` | Fails while ADMIN is required |
| `deploy/aws-installer/lib/steps.sh` | Fails if ADMIN is required during the install: require it after the install |
| `deploy/demo/probe.py` | A VIEWER requirement on a demo stops every demo sign-in |

---

## Example Authenticated Requests

### Create an Experiment

This takes permission to create experiments, such as the DEVELOPER or ADMIN role.

An experiment needs at least one variant and one metric:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Checkout Button Green",
    "hypothesis": "A green button increases checkout completion",
    "variants": [
      {"name": "Blue Button", "is_control": true, "traffic_allocation": 50},
      {"name": "Green Button", "traffic_allocation": 50}
    ],
    "metrics": [
      {"name": "Checkout completed", "event_name": "checkout_completed", "is_primary": true}
    ]
  }' | jq '{name, status}'
```
<!-- expect: "name": "Checkout Button Green" -->
<!-- expect: "status": "draft" -->

The experiment is created in `draft`, with a `key` made from its name, which SDKs use to
find it.

---

## Error Responses

### 401 Unauthorized

Returned when no credentials are sent, or they aren't valid:

```{.bash exec}
curl -s localhost:8000/api/v1/experiments/
```
<!-- expect: {"detail":"Not authenticated"} -->

It prints `{"detail":"Not authenticated"}`. An expired or malformed bearer token answers
`{"detail":"Could not validate credentials"}`, a missing API key `{"detail":"API key missing"}`
and an unknown or deleted one `{"detail":"Invalid API Key"}`.

### 403 Forbidden

Returned when the user is logged in but their role doesn't allow the operation, as in the
viewer's attempt above. The `detail` says what was refused.

---

## Security Notes

- Never commit API keys or access tokens to source control.
- Rotate API keys regularly: at least every 90 days for production keys.
- Use separate API keys for each service or environment.
- Access tokens expire; do not store them long-term.
- In production, send all API traffic over HTTPS.

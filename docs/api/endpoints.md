# API Endpoints Documentation

This document describes the API's main endpoints, with examples. Every route is listed in the API's own reference at `/api/v1/docs`.

## Authentication and Authorization

### Overview
The API uses two types of authentication:
1. OAuth2 Bearer tokens for user authentication
2. API keys for client applications

The examples on this page run in one terminal, in order, against the
[Quick Start](../getting-started/quick-start.md) stack. Each uses the shell variables set by
the ones before it (`$TOKEN`, `$KEY`, `$EXP_ID`).

### OAuth2 Authentication

**1. Obtain an access token.** `POST /api/v1/auth/token` takes a form. With
`AUTH_PROVIDER=local` (the default) its `username` is the account's email address. This
signs in as the Quick Start stack's administrator and saves the token in `$TOKEN`:

```{.bash exec}
TOKEN=$(curl -s -X POST "http://localhost:8000/api/v1/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'username=admin@demo.com&password=Demo1234!' | jq -r .access_token)
```

**2. Use the access token:**

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/users/me" \
  -H "Authorization: Bearer $TOKEN" | jq .email
```
<!-- expect: "admin@demo.com" -->

It prints `"admin@demo.com"`. If it prints `null`, the sign-in failed and `$TOKEN` holds
no token.

### API Key Authentication

API keys are used by client applications for the tracking and feature flag evaluation
endpoints.

**1. Obtain an API key.** Any signed-in user can create one, whatever their role (see
[API Keys](../security/api-keys.md)), and an API key writes tracking data. The full key is
shown only in this response. This
creates one and saves it in `$KEY`:

```{.bash exec}
KEY=$(curl -s -X POST "http://localhost:8000/api/v1/api-keys" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My application"}' | jq -r .key)

printf '%s\n' "${KEY:0:5}"
```
<!-- expect: eptk_ -->

It prints `eptk_`, the start of every key.

**2. Use the API key:**

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/feature-flags/user/123" \
  -H "X-API-Key: $KEY" | jq .
```
<!-- expect: "new_dashboard_ui": true -->
<!-- expect: "beta_features": true -->

It prints each flag's answer for user `123`; on the Quick Start stack both of its flags are
on for that user: `"new_dashboard_ui": true` and `"beta_features": true`.

### Authorization Levels
1. **Regular Users**:
   - Can access their own data
   - Cannot access admin endpoints

   - Feature flags are by role, not ownership: ADMIN and DEVELOPER may read, create,
     change and delete any flag; ANALYST and VIEWER may read any flag and change none
   - Experiments: reading an experiment and its results under
     `/api/v1/experiments/{experiment_id}` needs the role's READ on experiments (all four
     roles hold it); who owns the experiment is not considered. ADMIN and DEVELOPER may create
     experiments, clone any experiment, and update, start, pause, complete, archive or
     annotate (`metadata`) any experiment, schedule any experiment, and delete any
     experiment in DRAFT. ANALYST and VIEWER change no experiment, including one they
     own

2. **Superusers**:
   - Can access all user data
   - Can manage all experiments
   - Can access admin endpoints
   - Can create other users

## Usage Examples

### 1. User Registration and Authentication

!!! note "Cognito only"
    The password-reset and token-refresh endpoints (`POST /api/v1/auth/forgot-password`,
    `/reset-password` and `/refresh`) are available only when `AUTH_PROVIDER=cognito`.
    The sign-up and confirmation endpoints (`POST /api/v1/auth/signup` and `/confirm`)
    also need `COGNITO_SELF_SIGNUP_ENABLED=true`; without it they answer 404 and an
    administrator creates users ([Adding a user](../cognito_integration.md#adding-a-user)).
    With any other provider, including the default `local`, all five answer 404. With
    `local`, sign in with `POST /api/v1/auth/login`; an administrator creates accounts
    and resets passwords.

    The dashboard does not yet sign in with Cognito. Sign in through the API with
    `POST /api/v1/auth/token`. SSO sign-in needs `AUTH_PROVIDER=local`.

Step 1: Register a new user:

Only with `COGNITO_SELF_SIGNUP_ENABLED=true` and a user pool that allows self sign-up; otherwise see [Adding a user](../cognito_integration.md#adding-a-user).

```{.bash skip reason="idp: needs a Cognito user pool that allows self sign-up"}
curl -X POST "http://localhost:8000/api/v1/auth/signup" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john.doe",
    "email": "john@example.com",
    "password": "SecurePass123!",
    "given_name": "John",
    "family_name": "Doe"
  }'
```

Step 2: Confirm registration:

```{.bash skip reason="idp: needs the code Cognito sent to the new user"}
curl -X POST "http://localhost:8000/api/v1/auth/confirm" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "john.doe",
    "confirmation_code": "123456"
  }'
```

Step 3: Login to get access token:

```{.bash skip reason="idp: signs in the Cognito user the two steps above create"}
curl -X POST "http://localhost:8000/api/v1/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'username=john.doe&password=SecurePass123!'
```

With the default `local` provider, sign in as in [OAuth2 Authentication](#oauth2-authentication)
above.

### 2. Managing Experiments

Step 1: List experiments:

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/experiments/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | jq -r '.items[].name'
```
<!-- expect: Checkout Button Color -->

On the Quick Start stack it prints its three demo experiments, among them
`Checkout Button Color`.

Step 2: Create a new experiment. It needs at least one variant and one metric, and
`experiment_type` is `a_b`, `mv`, `split_url` or `bandit`. `key` is what the tracking API
and the SDKs call it by:

```{.bash exec}
EXP=$(curl -s -X POST "http://localhost:8000/api/v1/experiments/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Button Color Test",
    "key": "button_color_test",
    "description": "Testing different button colors",
    "hypothesis": "Red buttons will have higher click rates",
    "experiment_type": "a_b",
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 50, "configuration": {"color": "blue"}},
      {"name": "red", "traffic_allocation": 50, "configuration": {"color": "red"}}
    ],
    "metrics": [{"name": "Purchase", "event_name": "purchase", "is_primary": true}],
    "targeting_rules": {
      "logical_operator": "AND",
      "groups": [
        {
          "logical_operator": "AND",
          "conditions": [
            {"attribute": "country", "operator": "in", "value": ["US", "CA"]},
            {"attribute": "browser", "operator": "in", "value": ["chrome", "firefox"]}
          ]
        }
      ]
    }
  }')
EXP_ID=$(jq -r .id <<<"$EXP")

jq '{key, status, experiment_type}' <<<"$EXP"
```
<!-- expect: "key": "button_color_test" -->
<!-- expect: "status": "draft" -->
<!-- expect: "experiment_type": "a_b" -->

Each metric's `name` may appear once in an experiment, compared exactly: case and spaces
count, so `Revenue` and `revenue` are two metrics. A list that repeats a name answers 422 at
`["body", "metrics"]`, and nothing is written. The `msg` gives the 0-based positions of the
first repeated pair and never the name, for example `duplicate metric name; each name may
appear once per experiment (first seen at metrics[0], repeated at metrics[2])`.
`PUT /api/v1/experiments/{experiment_id}` gives the same answer, and refuses an empty
`metrics` list with 422 as a create does; leaving `metrics` out, or sending `null`, leaves the
metrics as they are.

A new experiment is a `draft`. Its dates are not part of the create request: schedule it
with `PUT /api/v1/experiments/{experiment_id}/schedule`, or start it now:

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/experiments/$EXP_ID/start" \
  -H "Authorization: Bearer $TOKEN" | jq -r .status
```
<!-- expect: active -->

It prints `active`.

Step 3: Get experiment results. The path takes the experiment's id, a UUID:

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/experiments/$EXP_ID/results" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | jq '{experiment_name, status, variants: [.metrics[0].variants[] | {variant_name, sample_size}]}'
```
<!-- expect: "experiment_name": "Button Color Test" -->
<!-- expect: "status": "active" -->
<!-- expect: "variant_name": "control" -->
<!-- expect: "variant_name": "red" -->
<!-- expect: "sample_size": 0 -->

Nobody is assigned yet, so each variant's `sample_size` is `0`.

### 3. Feature Flag Management

Step 1: List feature flags:

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/feature-flags/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | jq -r '.items[].key'
```
<!-- expect: beta_features -->

On the Quick Start stack it prints its two demo flags, `new_dashboard_ui` and
`beta_features`.

Step 2: Create a feature flag:

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/feature-flags/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "new_checkout_flow",
    "name": "New Checkout Flow",
    "description": "Rolling out new checkout experience",
    "is_active": true,
    "rollout_percentage": 50,
    "targeting_rules": {
      "logical_operator": "AND",
      "groups": [
        {
          "logical_operator": "AND",
          "conditions": [
            {"attribute": "country", "operator": "in", "value": ["US"]},
            {"attribute": "plan", "operator": "equals", "value": "premium"}
          ]
        }
      ]
    }
  }' | jq '{key, status, rollout_percentage}'
```
<!-- expect: "key": "new_checkout_flow" -->
<!-- expect: "status": "active" -->
<!-- expect: "rollout_percentage": 50 -->

Step 3: Get feature flags for a user (client-side):

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/feature-flags/user/123" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" | jq .
```
<!-- expect: "new_checkout_flow": true -->

The answer now includes the new flag, `"new_checkout_flow": true`. User `123` sends no
`country` or `plan`, so the flag's rule does not match and its 50% rollout decides, which
turns it on for this user.

### 4. Tracking Events

Step 1: Assign a user to an experiment (sticky; every call for an enrolled user records that the user saw the experiment):

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/tracking/assign" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"experiment_key": "button_color_test", "user_id": "123", "context": {"country": "US", "browser": "chrome"}}' \
  | jq '{variant_name, assigned, reason}'
```
<!-- expect: "variant_name": "control" -->
<!-- expect: "assigned": true -->
<!-- expect: "reason": "assigned" -->

The experiment created above targets `country` US or CA and `browser` chrome or firefox,
so the `context` says both. User `123` is enrolled in `control`. A user whose context does
not match gets the control with `"assigned": false` and `"reason": "targeting"`, and
nothing is recorded for them.

Step 2: Track a conversion for that experiment (variant is resolved from the assignment):

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/tracking/track" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "purchase",
    "user_id": "123",
    "experiment_key": "button_color_test",
    "value": 99.99,
    "metadata": {"product_id": "ABC123", "payment_method": "credit_card"}
  }' | jq '{event_name, user_id, value}'
```
<!-- expect: "event_name": "purchase" -->
<!-- expect: "value": 99.99 -->

It prints the stored event: `"event_name": "purchase"`, user `123`, `"value": 99.99`.

Step 3: Get user assignments:

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/tracking/assignments/123" \
  -H "X-API-Key: $KEY" | jq '.[] | {experiment_name, variant_name}'
```
<!-- expect: "experiment_name": "Button Color Test" -->
<!-- expect: "variant_name": "control" -->

It prints the one assignment made above: `Button Color Test`, `control`.

### 5. Admin Operations

Step 1: List all users (superuser only):

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/admin/users" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | jq -r '.items[].email'
```
<!-- expect: admin@demo.com -->

On the Quick Start stack it prints its four demo accounts, newest first, ending with
`admin@demo.com`.

Step 2: Create a new user (superuser only):

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/users/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "jane.smith",
    "email": "jane@example.com",
    "password": "SecurePass123!",
    "full_name": "Jane Smith",
    "is_active": true,
    "is_superuser": false
  }' | jq '{username, email, role}'
```
<!-- expect: "username": "jane.smith" -->
<!-- expect: "role": "VIEWER" -->

It prints the new account. Its `role` is `VIEWER`; change it with
`PATCH /api/v1/admin/users/{user_id}` ([below](#change-a-users-role-or-active-status-admin)).

## Rate Limiting and API Constraints

### Rate Limits
Limits are counted per client address and per path: most routes allow 300 requests a
minute to each path from one address, and a path with an id in it, such as
`/api/v1/experiments/{experiment_id}`, counts separately for each id.
AI design (`POST /api/v1/ai/design`) allows 10 a minute, and AI results
interpretation (`POST /api/v1/ai/interpret/{experiment_id}`) 10 a minute for every
experiment id together. For the other routes with a limit of their own, see
[Rate limiting](api-docs-guide.md#rate-limiting) in the API Documentation Guide. Over a limit
the API answers `429` with a
`Retry-After` header (see [Rate Limiting](#3-rate-limiting) below).

### Request Size Limits
- Maximum request body size: 5 MiB (5,242,880 bytes); a larger body is answered `413`, with or without a `Content-Length` header
- A targeting `context` has limits of its own, below

### Targeting Context Limits
The routes that take a targeting `context` hold it to two limits before it is matched against
any rule: `GET` and `POST /api/v1/feature-flags/evaluate/{flag_key}`,
`GET /api/v1/feature-flags/user/{user_id}`, `POST /api/v1/tracking/assign`, and each user's
`context` in `POST /api/v1/tracking/assign/batch`. `POST /api/v1/segments/{segment_id}/evaluate`
and `POST /api/v1/segments/bulk-evaluate` hold their `user_context` to the same limits.

| Limit | At most | `detail` of the 422 |
|---|---|---|
| Depth: every JSON object and array is a level, the context itself the first (`{"a": 1}` is 1 level, `{"a": [1]}` and `{"a": {"b": 1}}` are 2) | 10 levels | `Targeting context nested more than 10 levels deep` |
| Size: the context as compact JSON (no spaces) in UTF-8 | 8,192 bytes | `Targeting context larger than 8192 bytes` |

- Depth is checked first, so a context over both limits answers the depth `detail`.
- On a `GET`, the `context` parameter is held to 8,192 bytes as sent (its JSON text after URL
  decoding, spaces included) before it is parsed, and a parameter nested too deeply to parse
  answers the depth 422.
- In the batch, every user's context is checked before the first user is assigned. One over a limit
  refuses the whole request with 422, its `detail` prefixed with the user's position
  (`users[3]: Targeting context larger than 8192 bytes`), and nobody is assigned.
- A refused request is not evaluated or assigned, and nothing is recorded for it.
- A `POST` body nested too deeply to parse at all answers 400 (`There was an error parsing
  the body`), as before.

Every context in the SDKs' shared test vectors is a few hundred bytes at most and no more than
three levels deep.

### Pagination
- List endpoints that page take `skip` and `limit` parameters
- The default and the largest `limit` differ by route (the experiments list: 100, at most
  500; the feature flags list: 100, with no maximum). The API's OpenAPI document,
  `GET /api/v1/openapi.json`, gives each route's default `limit` and, where it has one, its
  maximum

### Caching
- `GET /api/v1/experiments/{experiment_id}` and its `daily-results` and `segmented-results` are cached for 3,600 seconds (an hour) when `CACHE_ENABLED` is on (it is off by default). A change to the experiment clears its cached detail; its cached daily and segmented results stay until they expire
- Feature flags are not cached: the flag list and a flag's detail are read from the database on every request, whatever `CACHE_ENABLED` says
- `GET /api/v1/results/{experiment_id}` and `GET /api/v1/experiments/{experiment_id}/results` are cached for 5 minutes while the experiment is running, and for 24 hours otherwise, whenever Redis is reachable

## Authentication Endpoints

!!! note "Cognito only"
    The password-reset and token-refresh endpoints (`POST /api/v1/auth/forgot-password`,
    `/reset-password` and `/refresh`) are available only when `AUTH_PROVIDER=cognito`.
    The sign-up and confirmation endpoints (`POST /api/v1/auth/signup` and `/confirm`)
    also need `COGNITO_SELF_SIGNUP_ENABLED=true`; without it they answer 404 and an
    administrator creates users ([Adding a user](../cognito_integration.md#adding-a-user)).
    With any other provider, including the default `local`, all five answer 404. With
    `local`, sign in with `POST /api/v1/auth/login`; an administrator creates accounts
    and resets passwords.

    The dashboard does not yet sign in with Cognito. Sign in through the API with
    `POST /api/v1/auth/token`. SSO sign-in needs `AUTH_PROVIDER=local`.

### Sign Up
- **Endpoint**: `POST /api/v1/auth/signup`
- **Description**: Register a new user
- **Request Body**:
  ```json
  {
    "username": "string",
    "password": "string",
    "email": "string",
    "given_name": "string",
    "family_name": "string"
  }
  ```
- **Response**: 201 Created
  ```json
  {
    "user_id": "string",
    "confirmed": false,
    "message": "string"
  }
  ```

### Confirm Sign Up
- **Endpoint**: `POST /api/v1/auth/confirm`
- **Description**: Confirm user registration with verification code
- **Request Body**:
  ```json
  {
    "username": "string",
    "confirmation_code": "string"
  }
  ```
- **Response**: 200 OK
  ```json
  {
    "message": "Account confirmed successfully. You can now sign in.",
    "confirmed": true
  }
  ```

### Login
- **Endpoint**: `POST /api/v1/auth/token`
- **Description**: OAuth2 compatible token login
- **Request Body**: Form data
  - username: string (with `AUTH_PROVIDER=local`, the user's email address)
  - password: string
- With `AUTH_PROVIDER=local`: The email address is matched whatever its letter case (A–Z), and every casing of it counts toward the same failed attempts.
- **Response**: 200 OK
  ```json
  {
    "access_token": "string",
    "token_type": "bearer",
    "expires_in": 43200,
    "user": {"id": "uuid", "email": "string", "username": "string", "role": "ADMIN"}
  }
  ```

### Get User Info
- **Endpoint**: `GET /api/v1/auth/me`
- **Description**: Get current user information
- **Headers**: Authorization: Bearer {token}
- **Response**: 200 OK
  ```json
  {
    "id": "uuid",
    "email": "string",
    "username": "string",
    "full_name": "string",
    "role": "ADMIN",
    "is_superuser": true,
    "is_active": true,
    "auth_provider": "local"
  }
  ```

## User Management Endpoints

### List Users
- **Endpoint**: `GET /api/v1/users/`
- **Description**: List users (all for superusers, self for regular users)
- **Headers**: Authorization: Bearer {token}
- **Query Parameters**:
  - skip: int (default: 0)
  - limit: int (default: 100, max: 100)
- **Response**: 200 OK
  ```json
  {
    "items": [
      {
        "id": "string",
        "username": "string",
        "email": "string",
        "full_name": "string",
        "is_active": true,
        "is_superuser": false,
        "role": "VIEWER",
        "created_at": "datetime",
        "updated_at": "datetime",
        "last_login": "datetime",
        "preferences": {}
      }
    ],
    "total": 100,
    "skip": 0,
    "limit": 100
  }
  ```
- **Fields**: `full_name` is the first and last name joined by a space (or
  whichever one is set), and `null` when neither is. `email`, `role`,
  `last_login` and `preferences` may be `null`.

### Create User
- **Endpoint**: `POST /api/v1/users/`
- **Description**: Create new user (superuser only)
- **Headers**: Authorization: Bearer {token}
- **Request Body**:
  ```json
  {
    "username": "string",
    "email": "string",
    "password": "string",
    "full_name": "string",
    "is_active": true,
    "is_superuser": false
  }
  ```
- **Password**: at least 8 characters and at most 72 bytes (UTF-8), with an
  upper-case letter, a lower-case letter and a digit. Anything else is a 422.
- **Response**: 201 Created
- **Errors**: 409 "Email already registered" when another account holds the
  email address in any letter case (`Pat.Lee@example.com` and
  `pat.lee@example.com` are the same address here). 409 "Username already
  registered" when another account has the username.

### Get User
- **Endpoint**: `GET /api/v1/users/{user_id}`
- **Description**: Get user by ID
- **Headers**: Authorization: Bearer {token}
- **Path Parameters**:
  - user_id: string (UUID)
- **Response**: 200 OK
  ```json
  {
    "id": "string",
    "username": "string",
    "email": "string",
    "full_name": "string",
    "is_active": true,
    "is_superuser": false,
    "created_at": "datetime",
    "updated_at": "datetime"
  }
  ```

### Update User
- **Endpoint**: `PUT /api/v1/users/{user_id}`
- **Description**: Update a user. A superuser may update any user and every
  field. Anyone else may update only their own account (another user's is a
  403), may not change its email address or username, and has `is_active`
  and `is_superuser` ignored.
- **Headers**: Authorization: Bearer {token}
- **Request Body**: `username` and `email` are required. A user who is not a
  superuser sends the account's current values; they are accepted only if the
  username is exactly the stored one and the email address is the stored one
  as `EmailStr` parses it (it lower-cases the domain, not the part before the
  `@`), and they are not written.
  ```json
  {
    "username": "string",
    "email": "user@example.com",
    "full_name": "string",
    "password": "string"
  }
  ```
- **Password**: `password` sets **another** account's password: a superuser's
  reset, which needs no old password. It follows the same rules as a new
  account's (see Create User); a weaker one, or `""`, is a 422. Leave it out,
  or send `null`, to keep the password as it is. Your own password is changed
  with `POST /api/v1/users/me/password` (below), which asks for the current
  one; sending your own `password` here is a 403 for every caller,
  superusers included. `PUT /api/v1/admin/users/{user_id}` treats `password`
  the same way.
- **Your own access**: a superuser cannot remove their own superuser access or
  deactivate their own account here; another superuser does it. The request is
  refused when it would change your own `is_superuser` or `is_active` from
  `true` to `false`, and nothing in it is written, other fields included.
  Resending the stored values (`true`) is accepted, so a client that reads the
  account and sends it back with an edit is not refused.
- **During a concurrent change**: a request that turns off another superuser's
  `is_superuser` or `is_active` first waits for any change in progress to an
  active superuser's account, then checks that you are still an active
  superuser. If another administrator has just deactivated or deleted your
  account, the answer is 400 "Inactive user"; if they have just removed your
  superuser access, 403 "Not enough permissions". Nothing is written either
  way. The wait is at most 5 seconds; a request that would wait longer answers
  500 and writes nothing.
- **Audit log**: a change to `is_superuser` is recorded as `role_assign`, with
  the role and superuser flag before and after, and a change to `is_active` as
  `user_deactivate` or `user_activate`, in the same transaction as the change.
  If the entry cannot be written, nothing is saved.
- **Response**: 200 OK, the user as in Get User
- **Errors**: 400 "You can't remove your own superuser access. Ask another
  administrator to do it." when a superuser's request would turn off their own
  `is_superuser`. 400 "You can't deactivate your own account." when it would
  turn off their own `is_active`; when both would change, the answer is the
  first. 403 "Only an administrator can change an account's email address
  or username." when a user who is not a superuser sends a different email
  address or username. Administrators change them with
  `PUT /api/v1/admin/users/{user_id}`. 403 "To change your own password, use
  POST /api/v1/users/me/password, which asks for your current password." when
  the request sets the caller's own password.
  409 "Email already registered" when a superuser sets an email address that
  another account holds in any letter case. Changing only the letter case of
  the account's own address is accepted, and resending the stored address
  unchanged is never refused. 409 "Username already registered" when a
  superuser sets a username another account has.

### Delete User
- **Endpoint**: `DELETE /api/v1/users/{user_id}`
- **Description**: A superuser deletes any account but their own. Anyone else
  deletes only their own account (another user's is a 403).
- **Headers**: Authorization: Bearer {token}
- **During a concurrent change**: deleting an account that is an active
  superuser first waits for any change in progress to an active superuser's
  account, then checks that you are still an active superuser. If another
  administrator has just deactivated or deleted your account, the answer is
  400 "Inactive user"; if they have just removed your superuser access, 403
  "Not enough permissions". Nothing is written either way. The wait is at most
  5 seconds; a request that would wait longer answers 500 and writes nothing.
- **Response**: 204 No Content
- **Errors**: 400 "Superusers cannot delete themselves" when a superuser
  deletes their own account. 403 "Not enough permissions" when anyone else
  deletes another account. 404 "User not found".

### Change Your Password
- **Endpoint**: `POST /api/v1/users/me/password`
- **Description**: Change your own password. Available when `AUTH_PROVIDER`
  is `local`; otherwise the route answers 404 before the body is validated
  against the schema. Changing the password does not end other sessions: a
  token issued before the change keeps working until it expires. To end them
  straight away, an administrator sets `is_active` to `false`.
- **Headers**: Authorization: Bearer {token}
- **Request Body**: `new_password` follows the same rules as a new account's.
  ```json
  {
    "current_password": "string",
    "new_password": "string"
  }
  ```
- **Response**: 204 No Content
- **Errors**: 403 when `current_password` is missing or incorrect ("The
  current password is incorrect."), or when the account has no password of
  its own; 422 when `new_password` breaks the rules; 423 with `Retry-After`
  after too many wrong current passwords (the same count as failed logins,
  10 within 15 minutes by default); 429 after 5 requests a minute from one
  address. A wrong current password is a 403, not a 401: the session is
  still signed in. The runnable example is in
  [Authentication](auth.md#change-your-own-password).

## Experiment Endpoints

### List Experiments
- **Endpoint**: `GET /api/v1/experiments/`
- **Description**: List experiments with filtering and pagination
- **Headers**: Authorization: Bearer {token}
- **Query Parameters**:
  - status_filter: string (optional)
  - skip: int (default: 0)
  - limit: int (default: 100, max: 500)
  - search: string (optional)
  - sort_by: string (default: "created_at")
  - sort_order: string (default: "desc")
- **Response**: 200 OK
  ```json
  {
    "items": [
      {
        "id": "string",
        "name": "string",
        "description": "string",
        "status": "string",
        "created_at": "datetime",
        "updated_at": "datetime"
      }
    ],
    "total": 100,
    "skip": 0,
    "limit": 100
  }
  ```

### Get Experiment
- **Endpoint**: `GET /api/v1/experiments/{experiment_id}`
- **Description**: Get experiment details
- **Headers**: Authorization: Bearer {token}
- **Path Parameters**:
  - experiment_id: string (UUID)
- **Response**: 200 OK
  ```json
  {
    "id": "string",
    "name": "string",
    "description": "string",
    "status": "string",
    "created_at": "datetime",
    "updated_at": "datetime"
  }
  ```

### Get Experiment Results
- **Endpoint**: `GET /api/v1/experiments/{experiment_id}/results`
- **Description**: Get experiment results and analysis. The same payload as
  `GET /api/v1/results/{experiment_id}` with no options: the experiment's
  stored `correction_method` and `confidence_level` (see
  [How results are judged](#how-results-are-judged)).
- **Headers**: Authorization: Bearer {token}
- **Path Parameters**:
  - experiment_id: string (UUID)
- **Response**: 200 OK. The top-level fields are `experiment_id`, `experiment_name`,
  `status`, `start_date`, `end_date`, `computed_at`, `correction_method`,
  `confidence_level`, `metrics` (one entry per metric, each with a `variants` list),
  `summary`, `sample_size_adequate`, `srm`, `sequential_testing`, `bayesian_results` and
  `breakdown`. An excerpt:
  ```json
  {
    "experiment_id": "string (UUID)",
    "experiment_name": "string",
    "status": "active",
    "metrics": [
      {
        "metric_name": "string",
        "variants": [
          {"variant_name": "control", "sample_size": 0}
        ]
      }
    ]
  }
  ```

### How results are judged

Each experiment stores a `correction_method` (`none`, `bonferroni` or
`benjamini_hochberg`) and a `confidence_level` (0.80 to 0.99). A new
experiment gets `benjamini_hochberg` at `0.95` unless `POST
/api/v1/experiments/` names others; experiments created before these fields
existed were given the same defaults. Both are returned by every experiment
response.

- `GET /api/v1/results/{experiment_id}`, its alias
  `GET /api/v1/experiments/{experiment_id}/results`,
  `GET /api/v1/results/{experiment_id}/sample-size`, the data export and the
  experiment report use the stored values, and so does the interaction test
  of `GET /api/v1/interactions/{exp_a_id}/{exp_b_id}` (beta): the correction
  across one experiment's treatments, and the level for `is_significant`.
  `?correction_method=` and
  `?confidence_level=` on the first and third apply to that request only; the
  response's `correction_method` and `confidence_level` say what the numbers
  were computed under. `?correction_method=none` shows the uncorrected
  numbers.
- With one treatment the correction changes no decision: `adjusted_p_value`
  equals `p_value`, and `is_significant`, the winner and the summary are the
  same as with `none`. With two or more treatments it can: a treatment with
  `p_value` 0.035 has `adjusted_p_value` 0.071 under `benjamini_hochberg`
  when the other treatment's p-value is higher, and is not significant at
  0.95.
- Both fields can be changed with `PUT /api/v1/experiments/{experiment_id}`
  only while the experiment is a `draft`. After that nobody can change them,
  a superuser included, and re-sending the stored value is refused as well
  (`Cannot update correction_method for experiments in active status`). An
  experiment that was already running when these fields were added keeps
  `benjamini_hochberg` for good; `?correction_method=none` on its results
  shows the numbers it showed before. A clone is a draft and starts with its
  source's values.
- Not affected: the breakdown (`?breakdown=`) applies its own Bonferroni
  correction over segments, and sequential testing uses its own `alpha`.
  CUPED follows the stored level and applies the stored correction.

### The difference from the control, its intervals and the impact

Each treatment of each metric in `metrics` carries its difference from the
control. `absolute_difference` is in the unit of `mean`: for a metric analysed
as a proportion, the difference of the shares of users who converted,
computed from `conversions` and `sample_size` (0.012 is 1.2 percentage
points); for one analysed as a mean, the difference per assigned user.
`absolute_difference_ci` is its interval, `relative_improvement_pct_ci` the
interval of `relative_improvement_pct`, and `incremental_impact` with
`incremental_impact_ci` the difference and its interval times the treatment's
own `sample_size`. The control's are all null.

The intervals are at the response's `confidence_level` and are not adjusted
for multiple comparisons. For a proportion the difference's interval is
Newcombe's, built from each variant's Wilson score interval; for a mean it is
Welch's. The relative change's interval is the MOVER-R interval on the same
per-variant limits (Fieller's interval, for a mean), so the three intervals
agree on whether 0 is inside.

`is_significant` is decided by the test, not by an interval. With two or more
treatments under a correction, an interval can exclude 0 on a treatment that
is not significant. For a proportion, near the threshold, the interval and
Fisher's exact test can disagree either way: 128 of 1,000 against 100 of
1,000 is not significant (p 0.057) while its interval runs from +0.0001
to +0.056, and 3 of 50 against 18 of 100 is significant (p 0.049) while its
interval reaches +0.00003.

For a proportion, `relative_improvement_pct_ci` is null only when an arm has no
users or the control has no conversions. The ratio is taken over values of 0
and above, so the lower end is never below -100%, and at low counts the
interval is wide: a
treatment with 6 converting users out of 200 against a control with 2 out of
200 gives -34.2% to +1,206.3% at 0.95. For a mean it is null when
`absolute_difference_ci` is, when the control's mean is 0, and when the
control's mean plus or minus its margin reaches 0, where Fieller's set of
ratios is unbounded. `absolute_difference_ci` is null only when
`absolute_difference` is, or for a mean when neither variant's values vary.

The impact is a plain number over the treatment's own assigned users:
converting users for a proportion, the total of the per-user values for a
mean, with no currency or unit and no projection to other users or a later
period.

### The planned effect

A metric's `expected_effect` is the minimum detectable effect to plan the
sample for, as a relative change: `0.10` means 5% → 5.5%. The primary
metric's is what `GET /api/v1/results/{experiment_id}/sample-size` and the
results page's Planned sample plan for when the request sends no `mde`; the
response's `mde_source` is then `experiment`. A value that is not above 0 and
below 1 (`0`, `1.5`, a negative number) is stored as sent but not used: the
plan is for `0.05` and `mde_source` is `default`. A secondary metric's
`expected_effect` is not read. A clone keeps it. A `PUT` that sends `metrics`
replaces them, so a metric resent without `expected_effect` no longer has one.

## Feature Flag Endpoints

### List Feature Flags
- **Endpoint**: `GET /api/v1/feature-flags/`
- **Description**: List feature flags with filtering and pagination. Every role can list
  every flag.
- **Headers**: Authorization: Bearer {token}
- **Query Parameters**:
  - status: string (optional): `ACTIVE`, `INACTIVE` or `ARCHIVED`, in upper case (each
    item's own `status` is lower case)
  - skip: int (default: 0)
  - limit: int (default: 100; the route sets no maximum)
  - search: string (optional): matches part of the name or the key, in any case
- **Response**: 200 OK. `total` counts every flag that matches `status` and `search`, not
  only the ones on this page.
  ```json
  {
    "items": [
      {
        "id": "string (UUID)",
        "key": "string",
        "name": "string",
        "description": "string or null",
        "status": "active",
        "is_active": true,
        "rollout_percentage": 0,
        "targeting_rules": null,
        "default_value": false,
        "value_type": "boolean",
        "on_value": true,
        "off_value": false,
        "tags": null,
        "owner_id": "string (UUID) or null",
        "created_at": "datetime",
        "updated_at": "datetime"
      }
    ],
    "total": 1,
    "skip": 0,
    "limit": 100
  }
  ```

### Get User Feature Flags
- **Endpoint**: `GET /api/v1/feature-flags/user/{user_id}`
- **Description**: Get all feature flags evaluated for a user
- **Headers**: X-API-Key: {api_key}
- **Path Parameters**:
  - user_id: string
- **Query Parameters**:
  - context: object (optional), URL-encoded JSON; at most 8,192 bytes and 10 levels deep
    ([Targeting Context Limits](#targeting-context-limits))
- **Response**: 200 OK
  ```json
  {
    "feature_flag_key": true,
    "another_flag": false
  }
  ```

### Archived Flags
An archived flag stays archived until it is unarchived:

| Request | On an archived flag |
|---|---|
| `PUT /api/v1/feature-flags/{id}` with `"is_active": true` | 400, flag unchanged |
| `POST /api/v1/feature-flags/{id}/activate` | 400, flag unchanged |
| `POST /api/v1/feature-flags/{id}/enable` | 400, flag unchanged |
| `POST /api/v1/feature-flags/{id}/toggle` | 400, flag unchanged |
| `POST /api/v1/feature-flags/bulk-toggle`, `"action": "enable"` | 200; that flag's result has `"success": false` and the same sentence in `error`; the other flags are processed |
| `PUT` with `"is_active": false`, `/deactivate`, `/disable`, bulk `disable` | 200, still archived |
| `POST /api/v1/feature-flags/{id}/unarchive` | 200, now inactive |

Each 400 carries the detail `This flag is archived. Unarchive it before turning it on.`

### Unarchive Feature Flag (beta)
- **Endpoint**: `POST /api/v1/feature-flags/{flag_id}/unarchive`
- **Description**: Move an archived flag to inactive. It is not turned on; do that
  separately. A flag that is not archived is returned unchanged. Written to the audit
  log as `feature_flag_update` (`ARCHIVED` to `INACTIVE`).
- **Headers**: Authorization: Bearer {token}
- **Permissions**: ADMIN and DEVELOPER; ANALYST and VIEWER get 403
- **Response**: 200 OK, the flag (the same shape as `GET /api/v1/feature-flags/{flag_id}`)
- **Errors**: 403 without permission to change flags, 404 for an unknown flag

## Tracking Endpoints

All tracking endpoints use API-key authentication (`X-API-Key`). They address experiments and flags by
their public `key`, except `POST /api/v1/tracking/events`, which takes ids. They use the same `SDK_RATE_LIMIT_PER_MINUTE` limit (default 6000/min), counted per client address and per path, so each flag key, user id or other id in a path is its own counter; the exception is
`POST /api/v1/tracking/assign/batch`, which has its own limit of 60 requests a minute.

### Assign User to Experiment
- **Endpoint**: `POST /api/v1/tracking/assign`
- **Description**: Return the user's variant for an ACTIVE experiment. Sticky per user; bandit experiments
  route new users by the current `BanditState` weights. Every call for an enrolled user records a view
  event (the user saw the experiment): the first call and every later sticky call alike.
- **Eligibility**: a new user is first checked against the global holdout, the experiment's mutual
  exclusion group and its targeting rules (evaluated against `context`). An ineligible user still gets
  200 OK with the control variant, `assigned: false` and `reason` set to `holdout`, `mutual_exclusion` or
  `targeting`; nothing is recorded for them. A user who already has an assignment keeps it.
- **An experiment that is not ACTIVE**: a draft, or a paused, completed or archived experiment, answers
  every user, enrolled or not, with 200 OK, its control variant, `assigned: false` and `reason` set to
  its status: `draft`, `paused`, `completed` or `archived`. Nothing is recorded. Users who were
  assigned keep their assignment, so when a paused experiment is started again each of them gets
  their variant back. A draft with no control variant answers 404.
- **Headers**: X-API-Key: {api_key}
- **Body**: `{"experiment_key": string, "user_id": string, "context": object?}`; `context` at most
  8,192 bytes and 10 levels deep ([Targeting Context Limits](#targeting-context-limits))
- **Response**: 200 OK
  ```json
  {
    "experiment_key": "string",
    "user_id": "string",
    "variant_id": "uuid",
    "variant_name": "string",
    "is_control": false,
    "configuration": {},
    "assigned": true,
    "reason": "assigned"
  }
  ```
  `assigned` is `false` when the user was not enrolled; `reason` is `assigned`, `holdout`,
  `mutual_exclusion` or `targeting`, or the status of an experiment that is not ACTIVE.
- **Errors**: 404 when no experiment has that key (`No experiment has the key 'checkout'.`), or for a
  draft with no control variant; 422 when `context` is over a [limit](#targeting-context-limits)

### Assign a List of Users to an Experiment (beta)
- **Endpoint**: `POST /api/v1/tracking/assign/batch`
- **Description**: Assign up to 1,000 users to an ACTIVE experiment in one request. Each user gets
  what `POST /api/v1/tracking/assign` would give them, with the bandit weights read at the start of
  the request: the same eligibility checks for a new user (global holdout, mutual exclusion group,
  targeting rules against that user's `context`) and the same sticky answer for a user already
  assigned. Users are processed in the order sent, and each new assignment is saved on its own.
  Only assignments are recorded: the user's first `POST /api/v1/tracking/assign` (from an SDK, when
  they actually see the experiment) records the view. Assigned users count in the experiment's
  results from the moment they are assigned, and for a bandit experiment they count as pulls.
  See [Assign a customer list to an experiment](../guides/assign-customer-list.md).
- **Headers**: `X-API-Key: {api_key}`, a key with the `sdk:ruleset` scope whose owner is an ADMIN,
  a DEVELOPER or a superuser. That scope also lets the key download every feature flag's targeting
  rules (`GET /api/v1/sdk/ruleset`), so keep the key on a server; see
  [Scopes](../security/api-keys.md#scopes).
- **Body**: `{"experiment_key": string, "users": [{"user_id": string, "context": object?}]}`
  - `users`: 1 to 1,000 entries; each `user_id` 1 to 255 characters and different from every other
    in the request.
  - Unknown fields are refused, in the body and in each user.
  - Each user's `context`: at most 8,192 bytes and 10 levels deep
    ([Targeting Context Limits](#targeting-context-limits)).
- **Response**: 200 OK
  ```json
  {
    "experiment_key": "spring-email",
    "variants": {
      "7b0a1a2e-4c3d-4f5e-8a9b-0c1d2e3f4a5b": {"name": "control", "is_control": true, "configuration": {"subject": "Spring sale"}},
      "c41f9e10-2b7a-4d8e-9f01-3a5b6c7d8e9f": {"name": "urgent", "is_control": false, "configuration": {"subject": "48 hours left"}}
    },
    "assignments": [
      {"user_id": "cust-001", "variant_id": "c41f9e10-2b7a-4d8e-9f01-3a5b6c7d8e9f", "assigned": true, "reason": "assigned"},
      {"user_id": "cust-002", "variant_id": "7b0a1a2e-4c3d-4f5e-8a9b-0c1d2e3f4a5b", "assigned": false, "reason": "holdout"}
    ],
    "counts": {"assigned": 1, "holdout": 1, "mutual_exclusion": 0, "targeting": 0}
  }
  ```
  `assignments` is in request order, one entry per user. `assigned: false` means the user was not
  enrolled and nothing was recorded for them; `variant_id` is then the control, the experience to
  show them, and `reason` says why: `holdout` (the user is in the global holdout),
  `mutual_exclusion` (the user is enrolled in, or hashed to, another experiment of the same mutual
  exclusion group) or `targeting` (the user's `context` does not match the experiment's targeting
  rules). `counts` always has all four keys.
- **Errors**: see the table below. A 409 or a 500 may follow partial assignment: the users before the
  failure are assigned. Send the same request again; that is safe, because assignments are sticky.
  Any other 4xx assigns nobody.
- **Rate limit**: 60 requests a minute per client address, counted separately from the other
  tracking routes (60 × 1,000 = 60,000 users a minute). Over it the API answers 429 with
  `Retry-After: 60`; wait that long before sending again.

| Status | When | `detail` |
|---|---|---|
| 401 | no key | `API key missing` |
| 401 | an unknown, expired or revoked key | `Invalid API Key` |
| 403 | the key lacks the scope | `This API key does not have the 'sdk:ruleset' scope. Create a key with the 'sdk:ruleset' scope for server-side SDK use.` |
| 403 | the key's owner is no longer an active ADMIN, DEVELOPER or superuser | `This key's owner can no longer change feature flags or experiments, so the key is refused for server-side SDK use.` |
| 404 | no ACTIVE experiment has that key | `Active experiment with key 'spring-email' not found` |
| 409 | the experiment was paused, completed or deleted (or its variants changed) during the request | `The experiment changed during the request. Users earlier in the list may already be assigned; resending the same request is safe.` |
| 422 | `users` is empty | loc `["body","users"]`, msg `List should have at least 1 item after validation, not 0` |
| 422 | more than 1,000 users | loc `["body","users"]`, msg `at most 1,000 users per request; split the list` |
| 422 | a `user_id` appears twice | loc `["body","users",4,"user_id"]`, msg `duplicate user_id; each user may appear once per request (first seen at users[1])` |
| 422 | a `user_id` empty or over 255 characters, an unknown field, or text the database cannot store | pydantic's message at the field's loc |
| 422 | a user's `context` over a [limit](#targeting-context-limits) | `users[3]: Targeting context nested more than 10 levels deep` or `users[3]: Targeting context larger than 8192 bytes`, with that user's position |
| 429 | over 60 requests a minute | `Too Many Requests. Please slow down and retry after a moment.`, with `Retry-After` |
| 500 | anything else | `Could not assign the users to the experiment (request ID: <id>).` |

A 422 names positions and fields, never the ids that were sent.

### Track Event
- **Endpoint**: `POST /api/v1/tracking/track`
- **Description**: Record one event. `experiment_key` and `feature_flag_key` are both optional.
  An event with no key is stored as history and counts in no experiment's results; tag outcome
  events. A key that is given but not found answers 404.
  While the experiment is running (ACTIVE), the event is tagged with the variant the user is
  assigned to. An event for a draft, paused, completed or archived experiment is stored with the
  experiment and no variant, so it counts in no variant's results.
  `event_type` is free text (SDKs send the event name); `event_name` defaults to `event_type`. Metrics count
  events by `event_name`: a metric with `event_name: "purchase"` counts every `purchase` event.
  Which metrics read `value`: a revenue, duration or custom metric adds it up per user; a count
  metric counts the event and does not read it; a conversion metric does not read it.
- **Headers**: X-API-Key: {api_key}
- **Body**: `{"event_type": string, "event_name": string?, "user_id": string, "experiment_key": string?, "feature_flag_key": string?, "value": number?, "metadata": object?, "timestamp": datetime?}`
- **Response**: 200 OK (the stored event); 404 a key that was given was not found; 422 invalid body

### Batch Track Events
- **Endpoint**: `POST /api/v1/tracking/batch`
- **Description**: Record up to 100 events (same shape as `/track`) in one request. An entry with no
  key is stored as history, not refused; an entry whose key is not found is reported in `errors`.
  Each entry is tagged with a variant as `/track` tags it: only while its experiment is running.
- **Response**: 200 OK `{"success_count": int, "failure_count": int, "errors": [...]|null}`; 422 above 100 events, and none is stored

### Track Event by Ids
- **Endpoint**: `POST /api/v1/tracking/events`
- **Description**: Same as `/track` but keyed by internal ids (`experiment_id`, `variant_id`, `feature_flag_id`);
  `event_name` is required. It stores the `variant_id` it is given, whatever the experiment's status.
- **Headers**: X-API-Key: {api_key}
- **Response**: 200 OK (the stored event); 404 an id names no stored row, and nothing is stored: the detail names
  each such field, for example `No experiment has that experiment_id.`; 422 invalid body, or an id that is not a UUID

### Get User Assignments
- **Endpoint**: `GET /api/v1/tracking/assignments/{user_id}`
- **Description**: Get user's experiment assignments
- **Headers**: X-API-Key: {api_key}
- **Path Parameters**:
  - user_id: string
- **Query Parameters**:
  - active_only: boolean (default: true)
- **Response**: 200 OK
  ```json
  [
    {
      "id": "string (UUID)",
      "user_id": "string",
      "experiment_id": "string (UUID)",
      "experiment_name": "string",
      "variant_id": "string (UUID)",
      "variant_name": "string",
      "is_control": true,
      "created_at": "datetime"
    }
  ]
  ```

## Admin Endpoints

### List Users (Admin)
- **Endpoint**: `GET /api/v1/admin/users`
- **Description**: List all users, newest first (superuser only)
- **Headers**: Authorization: Bearer {token}
- **Query Parameters**:
  - skip: int (default: 0)
  - limit: int (default: 100, max: 100)
  - search: string (optional, at most 100 characters). Lists only the users
    whose username, email, first name or last name contains the term, ignoring
    letter case. The term is matched literally: `%`, `_` and `\` are ordinary
    characters, not wildcards. Leading and trailing spaces are ignored, and an
    empty or all-space term lists every user. `total` counts the matching
    users, so `skip`/`limit` page through the matches. A term matching nothing
    answers 200 with `"items": []` and `"total": 0`. Each column is matched
    on its own, so `Jane Smith` does not match a first name `Jane` and last
    name `Smith`; search for either part.
- **Errors**: 422 when `search` is longer than 100 characters or contains a
  NUL character.
- **Response**: 200 OK
  ```json
  {
    "items": [
      {
        "id": "string",
        "username": "string",
        "email": "string",
        "full_name": "string",
        "is_active": true,
        "is_superuser": false,
        "role": "VIEWER",
        "created_at": "datetime",
        "updated_at": "datetime",
        "last_login": "datetime",
        "preferences": {}
      }
    ],
    "total": 100,
    "skip": 0,
    "limit": 100
  }
  ```
- **Fields**: `full_name` is the first and last name joined by a space (or
  whichever one is set), and `null` when neither is. `email`, `role`,
  `last_login` and `preferences` may be `null`.

### Update User (Admin)
- **Endpoint**: `PUT /api/v1/admin/users/{user_id}`
- **Description**: Update any user, including `is_superuser` (superuser only)
- **Headers**: Authorization: Bearer {token}
- **Request Body**: as Update User; `password` is treated the same way
- **Your own access**: a superuser cannot remove their own superuser access or
  deactivate their own account here; another superuser does it. The request is
  refused when it would change your own `is_superuser` or `is_active` from
  `true` to `false`, and nothing in it is written, other fields included.
  Resending the stored values (`true`) is accepted, so a client that reads the
  account and sends it back with an edit is not refused.
- **During a concurrent change**: a request that turns off another superuser's
  `is_superuser` or `is_active` first waits for any change in progress to an
  active superuser's account, then checks that you are still an active
  superuser. If another administrator has just deactivated or deleted your
  account, the answer is 400 "Inactive user"; if they have just removed your
  superuser access, 403 "Not enough permissions". Nothing is written either
  way. The wait is at most 5 seconds; a request that would wait longer answers
  500 and writes nothing.
- **Audit log**: a change to `is_superuser` is recorded as `role_assign`, with
  the role and superuser flag before and after, and a change to `is_active` as
  `user_deactivate` or `user_activate`, in the same transaction as the change.
  If the entry cannot be written, nothing is saved.
- **Response**: 200 OK, the user
- **Errors**: 400 "You can't remove your own superuser access. Ask another
  administrator to do it." when the request would turn off your own
  `is_superuser`. 400 "You can't deactivate your own account." when it would
  turn off your own `is_active`; when both would change, the answer is the
  first. 404 "User not found". 409 "Email already registered" when the
  request sets an email address that another account holds in any letter
  case. Changing only the letter case of the account's own address is
  accepted, and resending the stored address unchanged is never refused.
  409 "Username already registered" when the request sets a username another
  account has.

### Change a User's Role or Active Status (Admin)
- **Endpoint**: `PATCH /api/v1/admin/users/{user_id}`
- **Stability**: beta (`x-stability: beta`); the shape may still change.
- **Description**: Change another account's role, its active status, or both
  (superuser only). This is what the dashboard's Edit user dialog calls.
- **Headers**: Authorization: Bearer {token}
- **Request Body**: only the keys to change, at least one of them:
  ```json
  {
    "role": "ANALYST",
    "is_active": false
  }
  ```
  - `role`: `ADMIN`, `DEVELOPER`, `ANALYST` or `VIEWER`, in any letter case.
  - `is_active`: `false` deactivates the account, `true` reactivates it.
  - `null` is refused, and so is any other key (`is_superuser`, `username`,
    `password`, ...): 422. Use `PUT` above for those.
- **Response**: 200 OK, the user. Sending the values the account already has
  changes nothing and still answers 200.
- **Deactivating a user**: they can no longer sign in, requests with a token
  they already hold are refused, and **the API keys they created stop
  working, including keys your applications use**. Check which keys a user
  created before deactivating them.
- **During a concurrent change**: deactivating an account that is a superuser
  first waits for any change in progress to an active superuser's account,
  then checks that you are still an active superuser. If another administrator
  has just deactivated or deleted your account, the answer is 400 "Inactive
  user"; if they have just removed your superuser access, 403 "Not enough
  permissions". Nothing is written either way. The wait is at most 5 seconds;
  a request that would wait longer answers 500 and writes nothing.
- **Errors**:
  - 400 "You can't change your own role. Ask another administrator to do it."
  - 400 "You can't deactivate your own account."
  - 400 "Inactive user" when your own account was deactivated or deleted
    while the request was in flight.
  - 403 "Not enough permissions" for an account that is not a superuser,
    whatever its role, including one whose superuser access was removed
    while the request was in flight.
  - 404 "User not found".
  - 409 "Roles on this deployment come from Cognito groups and are updated on
    every request. Change this user's group in Cognito instead." when
    `AUTH_PROVIDER=cognito` and `SYNC_ROLES_ON_LOGIN` is on (the default) and
    the request changes `role`. Changing `is_active` is still accepted there.
  - 422 for a body that is empty, has an unknown key, a `null`, or a role
    outside the four above.
- Every change is recorded in the audit log, in the same transaction as the
  change: a role change as `role_assign`, with the role and superuser flag
  before and after, and an active-status change as `user_deactivate` or
  `user_activate`. If the entry cannot be written, nothing is saved.

### Delete User (Admin)
- **Endpoint**: `DELETE /api/v1/admin/users/{user_id}`
- **Description**: Delete user (superuser only)
- **Headers**: Authorization: Bearer {token}
- **Path Parameters**:
  - user_id: string (UUID)
- **During a concurrent change**: deleting an account that is an active
  superuser first waits for any change in progress to an active superuser's
  account, then checks that you are still an active superuser. If another
  administrator has just deactivated or deleted your account, the answer is
  400 "Inactive user"; if they have just removed your superuser access, 403
  "Not enough permissions". Nothing is written either way. The wait is at most
  5 seconds; a request that would wait longer answers 500 and writes nothing.
- **Response**: 204 No Content
- **Errors**: 400 "Cannot delete your own user account" when the account is
  your own. 404 "User not found".

## Error Responses

All endpoints may return the following error responses:

### 400 Bad Request
```json
{
  "detail": "Error message"
}
```

### 401 Unauthorized
```json
{
  "detail": "Could not validate credentials"
}
```

### 403 Forbidden
```json
{
  "detail": "Not enough permissions"
}
```

### 404 Not Found
```json
{
  "detail": "Experiment not found"
}
```

The detail names what was not found. A path the API does not have answers
`{"detail": "Not Found"}`.

### 500 Internal Server Error

The SDK routes that store data answer a failure to store it with JSON: a fixed sentence
followed by the request ID, never the error itself.

```json
{
  "detail": "Could not store the event (request ID: 3f2c9a7e-5b1d-4c8e-9f0a-6d2b1e4c7a90)."
}
```

| Route | `detail` |
|---|---|
| `POST /api/v1/tracking/assign` | `Could not assign the user to the experiment (request ID: <id>).` |
| `POST /api/v1/tracking/assign/batch` | `Could not assign the users to the experiment (request ID: <id>).` |
| `POST /api/v1/tracking/track`, `POST /api/v1/tracking/events` | `Could not store the event (request ID: <id>).` |
| `POST /api/v1/tracking/errors` | `Could not store the error report (request ID: <id>).` |
| `POST /api/v1/tracking/errors/batch` (the whole batch) | `Could not store the error reports (request ID: <id>).` |

The ID is the response's `X-Request-ID`. When that value is not 1 to 128 characters of
`A-Z a-z 0-9 . _ : -`, it is left out and the sentence simply ends: `Could not store the event.`

The two batch routes answer 200 and list each item they could not store in `errors`. An item
refused for its own content (an unknown key, an invalid event) keeps the message saying why; an
item the server failed to store has `error` set to `Could not store this event (request ID: <id>).`
(`/tracking/batch`) or `Could not store this error report (request ID: <id>).`
(`/tracking/errors/batch`).

Any other unhandled error answers with a plain-text body, not JSON:

```text
Internal Server Error
```

It carries the usual CORS headers, so a dashboard on another origin can read it, and an
`X-Request-ID` header: search the API log for that id to find the traceback. The body never
includes the error's details.

## Error Handling Examples

### 1. Authentication Errors

Invalid credentials:

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=wrong@example.com&password=wrong_password" | jq .
```
<!-- expect: "detail": "Invalid email or password" -->

Response (401 Unauthorized):

```json
{
  "detail": "Invalid email or password"
}
```

The same message answers an unknown address, a wrong password and an inactive account.

An expired or invalid token:

```{.bash exec}
curl -s -X GET "http://localhost:8000/api/v1/users/me" \
  -H "Authorization: Bearer expired_token" | jq .
```
<!-- expect: "detail": "Could not validate credentials" -->

Response (401 Unauthorized):

```json
{
  "detail": "Could not validate credentials"
}
```

### 2. Validation Errors

Invalid experiment creation:

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/experiments/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "",
    "experiment_type": "INVALID_TYPE"
  }' | jq -c '.detail[] | {loc, msg}'
```
<!-- expect: {"loc":["body","name"],"msg":"String should have at least 1 character"} -->
<!-- expect: {"loc":["body","experiment_type"],"msg":"Input should be 'a_b', 'mv', 'split_url' or 'bandit'"} -->
<!-- expect: {"loc":["body","variants"],"msg":"Field required"} -->
<!-- expect: {"loc":["body","metrics"],"msg":"Field required"} -->

Both fields are invalid on purpose: `name` is empty and `experiment_type` is not a type.
The request also leaves out `variants` and `metrics`, which are required, so the response
lists four errors, one per field.

Response (422 Unprocessable Entity), shortened to the first two errors:

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "name"],
      "msg": "String should have at least 1 character",
      "ctx": {"min_length": 1}
    },
    {
      "type": "enum",
      "loc": ["body", "experiment_type"],
      "msg": "Input should be 'a_b', 'mv', 'split_url' or 'bandit'",
      "ctx": {"expected": "'a_b', 'mv', 'split_url' or 'bandit'"}
    }
  ]
}
```

### 3. Rate Limiting

Too many requests. One experiment's URL, `/api/v1/experiments/{experiment_id}`, allows
300 requests a minute from one address, so the 301st inside a minute is refused. `curl`'s
`[1-301]` sends the request 301 times (the `n` parameter is ignored), and `uniq -c` counts
each status:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/experiments/$EXP_ID?n=[1-301]" | sort | uniq -c
```
<!-- expect: 200 -->
<!-- expect: 429 -->

It prints `300 200` and `1 429`. Until the minute is over, every request to that URL is
refused, with a `Retry-After` header giving the seconds to wait:

```{.bash exec}
curl -s -i -X GET "http://localhost:8000/api/v1/experiments/$EXP_ID" \
  -H "Authorization: Bearer $TOKEN"
```
<!-- expect: 429 Too Many Requests -->
<!-- expect: retry-after: 60 -->
<!-- expect: Too Many Requests. Please slow down and retry after a moment. -->

Response (429 Too Many Requests):

```json
{
  "detail": "Too Many Requests. Please slow down and retry after a moment."
}
```

## Targeting Rules and Experiment Types

### Targeting Rules

`targeting_rules` uses the shape the dashboard's rule builder writes: groups of
conditions, each condition an `attribute`, an `operator` and a `value`.

```json
{
  "logical_operator": "OR",
  "groups": [
    {
      "logical_operator": "AND",
      "conditions": [
        {"attribute": "country", "operator": "in", "value": ["US", "CA"]},
        {"attribute": "device_type", "operator": "equals", "value": "mobile"}
      ]
    },
    {
      "logical_operator": "AND",
      "conditions": [
        {"attribute": "plan", "operator": "in", "value": ["premium", "beta"]}
      ]
    }
  ]
}
```

- `logical_operator` is `AND`, `OR` or `NOT` (any case), at the top level and
  on each group. Left out, it is `AND`.
- The operators are `equals`, `not_equals`, `contains`, `not_contains`,
  `starts_with`, `ends_with`, `greater_than`, `less_than`,
  `greater_than_or_equal`, `less_than_or_equal`, `in`, `not_in`, `regex`,
  `is_null`, `is_not_null`, `semver_eq`, `semver_gt`, `semver_lt`,
  `semver_gte`, `semver_lte`, `geo_within_radius`, `time_window`,
  `array_contains`, `array_intersects`, `in_segment` and `not_in_segment`
  (below).
- An experiment's rules may carry a top-level `rollout_percentage`, a number
  from 0 to 100: that share of the users who match is admitted. Which users
  is decided by the rules' top-level `id` (text, at most 100 characters):
  two rules with the same `id` and percentage admit the same users.
- When rules with a `rollout_percentage` below 100 and no `id` are stored on
  an experiment that is started for the first time (from `draft`, by
  `POST /start` or by its scheduled `start_date`), the experiment's own id is
  stored as their `id`, so each experiment admits its own share of users.
  The same happens when such rules are saved on a `paused` experiment whose
  stored rules admitted everyone they matched. A `PUT` whose rules have no
  `id` keeps the stored one. An `id` you send is never replaced, and
  resuming a `paused` experiment changes nothing: an experiment started
  without an `id` before this behaviour existed keeps admitting the users it
  admitted then. Cloning an experiment drops an `id` equal to the source
  experiment's own id, so the clone is given its own when it first starts;
  any other `id` is copied.
- `null`, `{}` and `{"groups": []}` mean no targeting: every user is eligible.

**`in_segment`, `not_in_segment`.** `{"attribute": "segment", "operator": "in_segment", "value": "<segment id>"}`.
One segment per condition; to match any of several, put one condition per segment in an `OR`
group. A ruleset can use at most 10 different segments. A user is a member of an ID-list segment
when the user the flag or experiment is evaluated for (the request's `user_id`) is in its list,
and of a rules segment when the attributes sent with the request match its rules; in a segment's
rules, `user_id` is that same user, whatever the context says. Membership is decided by the
server from its own records: nothing in the context you send makes a user a member. A condition
on the attribute `segment` with any other operator, such as `equals`, compares the context value
as before. Saving rules that name a segment that is unknown, inactive or archived, or whose rules
are not valid, answers 422, and a segment condition in a native `default_rule` (returned without
its conditions being evaluated) is refused. When the server cannot decide a user's membership of
a segment the rules name (a flag unarchived after its segment was archived, a segment rule whose
pattern cannot be evaluated for this context), it does not guess: the flag answers
`enabled: false` with `reason: "error"` and the experiment does not enrol the user
(`reason: "targeting"`), whichever of the two operators the condition uses. Flags that use a
segment are always evaluated by the server, never by an SDK's local evaluation.
A segment condition requires nothing from the context. A user who lacks an attribute is not
refused outright: on experiments, as on flags, they fail only the conditions on that attribute
(`is_null` passes), so another `OR` branch can still enrol them, and they match a `NOT` group on
that attribute (a user with no `country` matches `NOT (country equals US)`, but not
`country not_equals US`). Users already assigned to an experiment keep their assignment.

See [Segments](../guides/segments.md#target-a-flag-or-an-experiment-at-a-segment).

`POST /api/v1/experiments/` and `PUT /api/v1/experiments/{experiment_id}` answer
422 for experiment rules that would not be applied as written: a list of rules, a flat object such as
`{"country": ["US"]}`, an unknown key, `groups` together with `rules`,
`logical_operator` without `groups`, a group with no conditions, an unknown
operator or logical operator, a value the operator cannot use, and a list
operator with more than 1,000 values. The message names the place, for example
`groups[0].conditions[1].operator: unknown operator`, and never repeats the
submitted value.

Cloning an experiment copies its stored rules as they are, without this check,
so an experiment created before the check keeps rules it would now refuse. The
one exception is a segment: a clone whose rules name a segment that is not
active, or whose rules are not valid, answers 409 and creates nothing.

`POST /api/v1/feature-flags/` and `PUT /api/v1/feature-flags/{flag_id}` answer
422 for flag rules on the same terms. A flag's rules are also refused for a
top-level `name`, and for native rules without `rules` (a `default_rule` on its
own, which the flag evaluator does not read). A problem with the rules as a
whole reads `targeting rules: <reason>`, for example `targeting rules: unknown
key`. A flag's stored rules are not re-checked and are evaluated as before, but
a `PUT` that sends back stored rules the API now refuses answers 422; leave
`targeting_rules` out of the request instead.
`python -m backend.scripts.check_targeting_rules` lists the flags whose stored
rules are refused. See
[Add targeting rules](../feature-flags/create.md#add-targeting-rules).

An experiment's targeting can be changed only while it is `draft` or `paused`,
whatever the caller's role, superusers included:

- `draft`: `targeting_rules` may be sent with any other field.
- `paused`: `targeting_rules` must be the only field in the request; together
  with anything else the request is refused with 400.
- `active`, `completed`, `archived`: any request that includes
  `targeting_rules` is refused with 400, even when the value equals the stored
  one. The detail names the state, for example `Targeting can be changed only
  while the experiment is draft or paused; it is active.` Pause the experiment,
  change the targeting, then start it again.

People already assigned keep their variant. The new rules decide for everyone
not yet in the experiment, including people turned away before the change,
once the experiment is started again.

### Updating an experiment

Every field of `PUT /api/v1/experiments/{experiment_id}` is optional: a field
left out keeps its value. `name`, `status`, `experiment_type`,
`sequential_testing_enabled`, `optimization_type`, `correction_method` and
`confidence_level` cannot be set to `null`;
such a request is refused with 422, nothing is changed, and the message names
the field, for example `name cannot be null`. `name` is at most 100
characters, as on create. Other fields, such as `description`, may still be
sent as `null`.

A refusal because of the experiment's state is a 400 whose detail names the
state; a 403 means the caller's role may not update experiments, and is
decided before the state is looked at. Outside `draft`, only a superuser may
change an experiment's other fields (`Cannot update experiments in active
status`), and `variants`, `metrics`, `start_date`, `end_date`,
`correction_method` and `confidence_level` cannot be changed by anyone
(`Cannot update variants for experiments in active status`).

`schedule` is not accepted by this endpoint. A request that contains it, with
any value including `null` or `{}`, is refused with 422, nothing is changed,
and the error is on `["body", "schedule"]` with the message `schedule is not
applied by this endpoint; use PUT /api/v1/experiments/{experiment_id}/schedule`.
Schedule an experiment with `PUT /api/v1/experiments/{experiment_id}/schedule`.

### An experiment's status

An experiment's status changes only through its lifecycle endpoints, whatever
the caller's role, superusers included:

- `POST /api/v1/experiments/{experiment_id}/start`: `draft` or `paused` to
  `active`. It checks for at least two variants, a control variant and at
  least one metric, and sets `start_date` to now when it has none. A draft
  started before its scheduled `start_date` starts now, and its `start_date`
  is set to now; a scheduled `end_date` is kept. An experiment with no
  `start_date` whose `end_date` has passed is refused with 400 and nothing is
  changed; set a later `end_date`, or clear it with `null`, through
  `PUT /api/v1/experiments/{experiment_id}/schedule` and start it again. A
  split URL experiment is also refused with 400 when its `split_url_config`
  is missing or cannot split; see
  [Starting a split URL experiment](split-url.md#starting-a-split-url-experiment).
- `POST /api/v1/experiments/{experiment_id}/pause`: `active` to `paused`.
- `POST /api/v1/experiments/{experiment_id}/complete`: `active` or `paused` to
  `completed`, and sets `end_date` to now. An experiment that an earlier
  version started before its scheduled start can still have a `start_date`
  after now; complete sets it to one microsecond before the new `end_date`.
- `POST /api/v1/experiments/{experiment_id}/archive`: any status but
  `archived` to `archived`.

`PUT /api/v1/experiments/{experiment_id}` does not change it. A `status` equal
to the current one is accepted and changes nothing, so an experiment fetched
with `GET` can be sent back as it is. Any other value is refused with 422,
nothing is changed, and the error is on `["body", "status"]` with the message
`status changes through POST /api/v1/experiments/{id}/start, /pause, /complete
or /archive; it cannot be set by an update.`

`POST /api/v1/experiments/` always creates a `draft`. `status` may be left out
or sent as `"draft"`; any other value is refused with 422 and nothing is
created.

### Experiment Types

`experiment_type` is one of these, in lower case; anything else is a 422:

| Value | What it is |
|---|---|
| `a_b` | An A/B test (the default) |
| `mv` | A multivariate test, with several variants |
| `split_url` | Each variant is a URL; see [Server-Side Split URL Testing](#server-side-split-url-testing) |
| `bandit` | A multi-armed bandit, which shifts traffic towards the better variants; see [Multi-Armed Bandit](multi-armed-bandit.md) |

Every type takes the same `variants`, each with a `name`, a `traffic_allocation` and an
optional `configuration`. The allocations must add up to 100, and one variant must be the
control (`"is_control": true`):

```json
{
  "experiment_type": "a_b",
  "variants": [
    {"name": "control", "is_control": true, "traffic_allocation": 50, "configuration": {"color": "blue"}},
    {"name": "red", "traffic_allocation": 50, "configuration": {"color": "red"}}
  ]
}
```

A feature flag is not an experiment type: flags have their own endpoints, under
[Feature Flag Endpoints](#feature-flag-endpoints).

## SDK Usage

The SDKs call the tracking and evaluation endpoints above for you. Their own pages show how
to install each one and the methods it has:

- [Python SDK](../sdk/python.md)
- [JavaScript SDK](../sdk/javascript.md), and [React SDK](../sdk/react.md) for its hooks
- Every other SDK is listed in the [SDK Integration Guide](../sdk-guide.md)

---

## Advanced Analytics Endpoints

### Sequential Testing

See [Sequential Testing Guide](sequential-testing.md) for full documentation.

```text
GET /api/v1/results/{experiment_id}/sequential
```
Returns mSPRT analysis, always-valid confidence intervals, an early stopping recommendation and the advisory `at_risk` flag. `alpha_spending` is always empty: no planned-looks table is computed yet.

**Query params:** `alpha` (above 0 and at most 0.2; overrides the experiment's stored `sequential_testing_config.alpha`, default `0.05`)

`GET /api/v1/results/{experiment_id}` and `GET /api/v1/experiments/{experiment_id}/results` embed the same analysis, at the stored `alpha`, as `sequential_testing`: `null` when sequential testing is off or the analysis could not be computed, and cached with the results ([In the results response](sequential-testing.md#in-the-results-response)).

---

### Sample Size

See [Statistical Power Analysis](../statistics/power-analysis.md#during-the-experiment-the-sample-size-tab) for the method.

```text
GET /api/v1/results/{experiment_id}/sample-size
```
Returns the users each variant needs to detect a relative lift of `mde` over the baseline on the
primary metric (a two-sided two-proportion test), the smallest variant's users so far, the power
they give at that MDE, and every input used with where it came from.

**Query params** (all optional; send only what you want to change):

| Parameter | Default | Range |
|---|---|---|
| `baseline_conversion_rate` | the control variant's observed rate so far | above 0, below 1 |
| `mde` | the primary metric's `expected_effect` when it is above 0 and below 1, else `0.05` (relative: 12% → 12.6%) | above 0, below 1 |
| `confidence_level` | the experiment's stored `confidence_level` | 0.80 to 0.99 |
| `power_target` | `0.80` | 0.50 to 0.99 |
| `correction_method` | the experiment's stored `correction_method` | `none`, `bonferroni`, `benjamini_hochberg`; the last two plan each comparison at `alpha / (variants - 1)` |

**Response** (`SampleSizeResult`): `required_sample_size_per_variant`, `current_sample_size_per_variant`
(the smallest variant), `is_adequate`, `achieved_power`, `baseline_rate`, `baseline_source`
(`observed` or `request`), `baseline_users`, `mde`, `mde_source` (`request`, `experiment` or
`default`), `mde_absolute`, `confidence_level`, `alpha`
(per comparison), `comparisons`, `correction_method`, `power_target`, `metric_id`, `metric_name`,
`metric_type`, `analysed_as` (always `conversion` today), `guide_only_reasons` (why a fixed sample
size is only a guide here: `adaptive_allocation`, `unequal_allocation`, `sequential_testing`,
`bayesian`) and `unavailable_reason`. `days_to_significance` and `projected_completion_date` are always `null`.

With nothing to plan from, the answer is still **200**: `required_sample_size_per_variant`,
`achieved_power` and `baseline_rate` are `null`, and `unavailable_reason` is one of
`no_metric`, `no_control_data`, `no_control_conversions`, `rate_at_boundary` (every control user
converted), `effect_out_of_range` (the observed rate raised by `mde` reaches 100%) or
`effect_too_small` (the observed rate raised by `mde` changes too little for the size to be a
finite number). A `baseline_conversion_rate` that `mde` raises to 100% or more answers **422**,
and so does one that `mde` changes too little for the size to be a finite number; an unknown
experiment answers **404**, whatever the other parameters.

---

### CUPED Variance Reduction

See [CUPED Guide](cuped.md) for full documentation.

```text
GET /api/v1/results/{experiment_id}/cuped
```
Returns, for each conversion metric and each treatment, the effect against the control adjusted for each user's own events before assignment, with the variance reduction and the share of users with history. History counts only if the server received it before the user was assigned.

It takes no query parameters: the method comes from the experiment's `variance_reduction_config`, the interval from its stored `confidence_level` and `corrected_p_value` from its stored `correction_method`. A comparison that cannot be computed is listed with `unavailable_reason`.

---

### Dimensional Analysis / Segment Breakdown

See [Dimensional Analysis Guide](dimensional-analysis.md) for full documentation.

```text
GET /api/v1/experiments/{experiment_id}/segmented-results/{segment_by}
```
Returns counts per segment of `segment_by`, variant and metric, with no significance test;
an experiment in `draft` answers `400`. The breakdown with a test per segment is
`GET /api/v1/results/{experiment_id}?breakdown=` (see the guide).

**Query params:** `metric_id` (one metric only), `skip_cache` (default `false`)

---

### Multi-Armed Bandit

See [Multi-Armed Bandit Guide](multi-armed-bandit.md) for full documentation.

```text
GET  /api/v1/bandit/{experiment_id}           — Current variant weights and stats
POST /api/v1/bandit/{experiment_id}/update    — Trigger weight recalculation (DEVELOPER+)
PUT  /api/v1/bandit/{experiment_id}/weights   — Override weights manually (ADMIN)
```

---

### Interaction Detection

See [Interaction Detection Guide](interaction-detection.md) for full documentation.

```text
GET /api/v1/interactions/scan                  — Overlap of every pair of active experiments
GET /api/v1/interactions/{exp_a_id}/{exp_b_id} — Pairwise analysis (beta: overlap, and an interaction test on each primary metric)
```

Access: ANALYST, DEVELOPER or ADMIN (VIEWER returns 403).

---

## Platform Management Endpoints

### Mutual Exclusion Groups

See [Mutual Exclusion Groups Guide](mutual-exclusion-groups.md) for full documentation.

```text
GET    /api/v1/mutual-exclusion-groups                              — List groups
POST   /api/v1/mutual-exclusion-groups                              — Create group (DEVELOPER+)
GET    /api/v1/mutual-exclusion-groups/{group_id}                   — Get group
PUT    /api/v1/mutual-exclusion-groups/{group_id}                   — Update group (DEVELOPER+)
DELETE /api/v1/mutual-exclusion-groups/{group_id}                   — Archive group (ADMIN)
POST   /api/v1/mutual-exclusion-groups/{group_id}/experiments       — Add experiment
DELETE /api/v1/mutual-exclusion-groups/{group_id}/experiments/{eid} — Remove experiment
```

---

### Global Holdout

See [Mutual Exclusion Groups Guide](mutual-exclusion-groups.md) for full documentation.

```text
GET  /api/v1/holdout              — Get active holdout
GET  /api/v1/holdout/all          — List all holdouts (ADMIN)
POST /api/v1/holdout              — Create holdout (ADMIN)
PUT  /api/v1/holdout/{id}         — Update holdout (ADMIN)
GET  /api/v1/holdout/check/{uid}  — Check if user is in holdout
GET  /api/v1/holdout/{id}/results — Holdout vs everyone else for one metric (beta)
```

---

### Warehouse analysis (beta)

The full profile serves the warehouse analysis routes under
`/api/v1/warehouse/analysis`; every other path under `/api/v1/warehouse`
answers 404. Which warehouses are available is on
[Warehouse analysis](warehouse-analytics.md).

---

### Experiment Wizard

See [Experiment Wizard Guide](../guides/experiment-wizard.md) for full documentation.

All six wizard operations are **deprecated**: the dashboard does not use them, and they
may be removed in a later release.

```text
POST /api/v1/wizard/drafts              — Create draft (deprecated)
GET  /api/v1/wizard/drafts              — List my drafts (deprecated)
GET  /api/v1/wizard/drafts/{id}         — Get draft (deprecated)
PUT  /api/v1/wizard/drafts/{id}/step    — Update draft step (deprecated)
POST /api/v1/wizard/validate            — Validate step data (deprecated)
POST /api/v1/wizard/drafts/{id}/submit  — Submit → create experiment (deprecated)
```

---

### Rollout Schedules

```text
POST   /api/v1/rollout-schedules/                     — Create schedule
GET    /api/v1/rollout-schedules/                     — List schedules
GET    /api/v1/rollout-schedules/{id}                 — Get schedule
PUT    /api/v1/rollout-schedules/{id}                 — Update schedule
DELETE /api/v1/rollout-schedules/{id}                 — Delete schedule
POST   /api/v1/rollout-schedules/{id}/activate        — Activate
POST   /api/v1/rollout-schedules/{id}/pause           — Pause
POST   /api/v1/rollout-schedules/{id}/cancel          — Cancel
POST   /api/v1/rollout-schedules/{id}/stages          — Add stage
PUT    /api/v1/rollout-schedules/stages/{stage_id}    — Update stage
DELETE /api/v1/rollout-schedules/stages/{stage_id}    — Delete stage
POST   /api/v1/rollout-schedules/stages/{stage_id}/advance — Manual advance
```

---

### Audit Logs & Bulk Toggle

See [Audit Logging Guide](audit-logging.md) for full documentation.

```text
GET  /api/v1/audit-logs/                  — Query audit logs
GET  /api/v1/audit-logs/entity/{entity_type}/{entity_id}
                                          — Entries for one entity (ADMIN, ANALYST)
GET  /api/v1/audit-logs/user/{user_id}    — Entries for one actor
GET  /api/v1/audit-logs/stats             — Aggregate stats (ADMIN, ANALYST)
GET  /api/v1/audit-logs/stream            — SSE real-time stream
POST /api/v1/feature-flags/bulk-toggle    — Bulk enable/disable/archive (DEVELOPER+);
                                            enable refuses archived flags one by one
GET  /api/v1/feature-flags/{id}/history   — Flag change history
```

ADMIN and ANALYST read every audit entry; DEVELOPER and VIEWER read only their
own entries on the list, user, stream and history routes. See
[Permissions](audit-logging.md#permissions).

---

### Safety Monitoring

See [Safety Monitoring](../feature-flags/safety.md).

```text
GET  /api/v1/safety/settings                              — Global settings (any signed-in user)
POST /api/v1/safety/settings                              — Create/update global settings (superuser)
GET  /api/v1/safety/feature-flags/{flag_id}/config        — Per-flag safety config (defaults when none)
POST /api/v1/safety/feature-flags/{flag_id}/config        — Create/update per-flag config (rollback_percentage 0–100, else 422; 0 = off)
GET  /api/v1/safety/feature-flags/{flag_id}/check         — Run the safety check now
POST /api/v1/safety/feature-flags/{flag_id}/rollback      — Manual rollback (?percentage=0&reason=...; percentage 0–100, else 422; 0 = off for every user, schedule paused) (superuser)
```

---

### Audience Segments

```text
POST   /api/v1/segments                  — Create segment (DEVELOPER+)
GET    /api/v1/segments                  — List segments (?status=active|inactive|archived)
GET    /api/v1/segments/{id}             — Get segment
PUT    /api/v1/segments/{id}             — Update segment (DEVELOPER+; 409: in use, below)
DELETE /api/v1/segments/{id}             — Archive segment: sets status archived (DEVELOPER+; 409: in use)
POST   /api/v1/segments/{id}/evaluate    — Is this user context a member? (409: stored rules not valid)
POST   /api/v1/segments/bulk-evaluate    — One user context against up to 50 segments
GET    /api/v1/segments/{id}/experiments — Experiments and flags whose rules mention the segment's id
POST   /api/v1/segments/{id}/preview     — Estimate the share of users the rules in the body match
POST   /api/v1/segments/{id}/members        — Add user IDs to an id_list segment (beta; DEVELOPER+)
POST   /api/v1/segments/{id}/members/remove — Remove user IDs from an id_list segment (beta; DEVELOPER+)
```

`{id}` is the segment's UUID; any other text answers 422.

A segment's `kind` is `rules` (the default: users whose attributes match its rules) or
`id_list` (users whose `user_id` is in its list; see
[below](#segments-made-from-a-list-of-user-ids-beta)). It is set on create and never
changed.

**Segment rules use the targeting rule format** that flag and experiment targeting use,
and are checked when saved:

```json
{"logical_operator": "AND",
 "groups": [{"logical_operator": "AND",
             "conditions": [{"attribute": "country", "operator": "equals", "value": "US"},
                            {"attribute": "plan", "operator": "in", "value": "pro, team"}]}]}
```

The operators are the flag operators listed under
[Add targeting rules](../feature-flags/create.md#add-targeting-rules) (`equals`, `in`,
`regex`, `semver_gte`, ...), except `in_segment` and `not_in_segment`: a segment cannot
refer to a segment. A segment needs at least one group, and every group at least
one condition. It holds at most 20 groups, 50 conditions, 10 `regex` conditions and 1,000
list values in total. `POST` and `PUT /api/v1/segments` answer 422 for anything else, with
`loc` `["body", "rules"]` and a fixed message naming the place and the reason, for example
`groups[0].conditions[0].operator: unknown operator` or `rules: unknown key`. The message
never repeats the submitted rules. Saved rules are returned exactly as sent.

A user is a member when the rules match the `user_context` sent to evaluate, evaluated as
a feature flag evaluates the same rules: `{"user": {"country": "US"}}` and
`{"user.country": "US"}` both answer a rule on `country`. `matched_rules` lists each
condition, in any group, that the context satisfies on its own, as
`"<attribute> <operator> <value>"`; it is empty when the user is not a member. A `regex`
condition that cannot be evaluated makes the user not a member.

Evaluate and bulk-evaluate hold `user_context` to the
[Targeting Context Limits](#targeting-context-limits): at most 8,192 bytes and 10 levels deep.
Over either, they answer 422 with the same `detail` as the flag routes, and no segment is
evaluated. Bulk-evaluate checks its one `user_context` once, whatever the number of segments.

`POST /api/v1/segments/{id}/preview` evaluates the rules in its body against the stored
contexts of up to `sample_size` assignments (10 to 10,000, default 1,000). Only assignments
that carry a context are counted, and `sample_size` in the answer is how many there were.
Assignment does not store a context today, so the answer is usually
`{"estimated_percentage": 0.0, "sample_size": 0, "matched": 0}`: there was nothing to
estimate from, which is not the same as 0%. The preview also refuses, before any query,
`regex` conditions times `sample_size` above 500 and conditions plus groups times
`sample_size` above 50,000, at `loc` `["query", "sample_size"]`.

**A segment in use cannot be archived or made inactive.** Flags and experiments target a
segment with `in_segment` / `not_in_segment` ([Targeting Rules](#targeting-rules)).
`DELETE /api/v1/segments/{id}`, and a `PUT` that sets `status` to `inactive` or `archived`,
answer 409 and change nothing while a flag that is not archived (a disabled one included), or
an experiment that is draft, active or paused, has a rule naming the segment:

```json
{"detail": {"code": "segment_in_use",
            "message": "This segment is used by 1 feature flag and 2 experiments. Remove it from their targeting rules first.",
            "feature_flags": [{"id": "...", "key": "checkout-v2", "name": "Checkout v2"}],
            "experiments": [{"id": "...", "key": "pricing-page", "name": "Pricing page", "status": "paused"}]}}
```

A stored row that mentions the segment's id but whose rules cannot be read is listed too.

#### Upgrading: segment rules

Releases before this check stored segment rules in a format of their own,
`{"operator": "and", "conditions": [{"attribute", "operator": "eq", "value"}]}`, which
nothing checked: a condition the engine did not understand was skipped, and a segment left
with no condition matched every user. That format is now refused when saved.

**Segments already stored are left as they are.** Their rules are kept and returned by
`GET` unchanged, but they are not evaluated: `POST /api/v1/segments/{id}/evaluate` answers
**409** with the detail `Segment rules not valid: ...`, and bulk-evaluate answers `false`
for that segment. The same holds for a stored segment with no groups, an empty group or an
unknown operator. A `PUT` that leaves `rules` out (to rename one, say) still succeeds.

To list them, run `python -m backend.scripts.check_targeting_rules` from the repository
root against the API's database settings. Beside its flag lines it prints one line per
segment that is not archived and whose rules are not valid,
`segment <id> <name> <place> rules not valid: <reason>`, writes nothing, and exits 0
(2 when it cannot read the database). Rewrite each listed segment's rules in the format
above with `PUT /api/v1/segments/{id}`.

A flag or an experiment whose targeting rules name such a segment (with `in_segment` or
`not_in_segment`) is not evaluated against it: the flag answers `enabled: false` with
`reason: "error"`, and a new user is not assigned (`reason: "targeting"`). Creating or
updating a flag or an experiment that names one answers `422`.

#### Segments made from a list of user IDs (beta)

Create the segment with `kind` `id_list` and no `rules`:

```json
{"name": "Enterprise pilot", "kind": "id_list"}
```

Sending `rules` with `id_list`, or leaving them out with `rules`, answers 422. The answer
carries `"kind": "id_list"` and `"rules": null`, and `GET /api/v1/segments/{id}` adds
`member_count`, how many IDs the list holds (it is `null` for a rules segment and in the
list route). A `PUT` that sends `kind`, or `rules` for an id list, answers 422; renaming one
works as for any segment.

Add and remove IDs with the member routes, which are beta (`x-stability: beta`):

```json
POST /api/v1/segments/{id}/members          {"add": ["user-123", "user-456"]}
→ 200 {"added": 2, "already_members": 0, "member_count": 2}

POST /api/v1/segments/{id}/members/remove   {"remove": ["user-123", "user-999"]}
→ 200 {"removed": 1, "not_members": 1, "member_count": 1}
```

- **Limits.** 1 to 10,000 IDs per request, each a string of 1 to 255 characters, matched
  exactly (not trimmed, not case-folded). A segment holds at most 1,000,000 IDs. Send a
  longer list in chunks of 10,000.
- **Sending the same IDs again is safe.** An ID already in the segment is counted in
  `already_members` and left alone; removing one that is not there is counted in
  `not_members`. An ID repeated within one request counts once, so `added +
  already_members` (or `removed + not_members`) is the number of distinct IDs sent.
- **Refusals.** 422 with `loc` `["body", "add"]` (or `"remove"`) and a fixed message that
  never repeats an ID: `add: at least 1 ID is required`, `add: at most 10,000 IDs per
  request`, `add[17]: an ID is 1 to 255 characters`. Any other field in the body answers
  422 (`{"ids": [...]}` is refused, not ignored). A request that would take the segment
  past 1,000,000 answers 422 `this segment would have 1,000,250 members; a segment holds at
  most 1,000,000` and adds nothing. A rules segment answers 409 `members can be added only
  to an id_list segment` (`removed only from` on remove), an archived one 409 `this
  segment is archived; its members cannot be changed`, an unknown one 404.
- **Who.** DEVELOPER and ADMIN; ANALYST and VIEWER get 403. Each change writes one
  `segment_update` audit entry with the counts, never the IDs.

`POST /api/v1/segments/{id}/evaluate` answers an id list on `user_context.user_id`: the user
is a member when it is a string in the list, and `matched_rules` is empty. A context with no
`user_id`, or a `user_id` that is not a string, is not a member. Bulk-evaluate answers the
same way. Preview takes rules, so it answers 422 for a body with none.

---

### Notifications & Alerting

See [Alerting Guide](alerting.md) for full documentation.

```text
GET  /api/v1/notifications/preferences         — Get my preferences
PUT  /api/v1/notifications/preferences         — Update my preferences
GET  /api/v1/notifications/admin/preferences   — List all (ADMIN)
GET  /api/v1/notifications/delivery-log        — Delivery history (ADMIN)
POST /api/v1/notifications/test                — Send test notification (DEVELOPER+)
```

---

### AI Endpoints and the MCP Manifest

See [AI Endpoints and the MCP Manifest](../mcp-server.md) for full documentation. The
manifest describes these operations as tools in the vocabulary of the Model Context
Protocol, but the platform runs no MCP server: an MCP client cannot connect to it or call a
tool through it. Call the endpoints over HTTP.

```text
POST /api/v1/ai/design                    — AI experiment design suggestion
POST /api/v1/ai/interpret/{experiment_id} — AI results interpretation
GET  /api/v1/ai/sample-size               — Sample size calculator
GET  /api/v1/ai/templates                 — List experiment templates
GET  /api/v1/ai/templates/{id}            — Get template
GET  /api/v1/mcp/manifest                 — Tool manifest in MCP's vocabulary (no sign-in)
```

---

### Scheduler Health

```text
GET  /api/v1/scheduler/health           — Health summary for every scheduler
GET  /api/v1/scheduler/health/{name}    — Health of one scheduler
GET  /api/v1/scheduler/{name}/history   — Recent run history for one scheduler
POST /api/v1/scheduler/notify/test      — Send a test notification
```

---

### ETL / Glue Jobs

```text
POST /api/v1/etl/jobs/run               — Trigger an ETL job run
GET  /api/v1/etl/jobs/{run_id}/status   — Status of one run
POST /api/v1/etl/crawler/run            — Trigger the Glue crawler
GET  /api/v1/etl/crawler/status         — Crawler status
POST /api/v1/etl/partitions/add         — Register a date's 24 hourly partitions
```

`POST /api/v1/etl/query` has been removed and answers 404.

These routes act only on the Glue names the deployment configured. Any other
name answers **404** with a fixed `detail`, and Glue is not called:

| Route | Parameter | Accepted value | Otherwise |
|---|---|---|---|
| `GET .../jobs/{run_id}/status` | `job_name` | `GLUE_ETL_JOB_NAME` or `GLUE_METRICS_JOB_NAME` | `"Unknown ETL job"` |
| `GET .../crawler/status`, `POST .../crawler/run` | `crawler_name` | `GLUE_CRAWLER_NAME`, the default when omitted | `"Unknown crawler"` |
| `POST .../partitions/add` | `database`, `table` | `GLUE_DATABASE` and `GLUE_EVENTS_TABLE` | `"Unknown Glue table"` |

`POST .../jobs/run` takes a `job_type`, not a name. A setting left empty
configures nothing, so with it empty its routes answer 404 for every name, as
does `jobs/run` for a job type whose job is unset. A run that Glue does not
have answers 404 `"Job run not found"`.

`POST .../partitions/add?database=...&table=...&date=YYYY-MM-DD` registers the
24 hourly partitions (`year`, `month`, `day`, `hour`) of that date under the
Glue table's own storage location, and answers **201** with the 24 partitions
only when Glue registered every one of them or already had it. Re-running a
date is safe: hours already registered are accepted. Otherwise:

| Status | `detail` | When |
|---|---|---|
| 404 | `"Unknown Glue table"` | `database` or `table` is not the configured pair. Glue is not called |
| 422 | `"date must be a calendar date in YYYY-MM-DD format"` | `date` is anything else, such as `2026-10`, `2026-1-01` or `2026-13-45`. Glue is not called |
| 404 | `"Glue table not found. Run the crawler first."` | the configured table is not in the Glue catalog yet |
| 500 | `"Could not read the Glue table (request ID: ...)."` | Glue refused to read the table, or the table has no storage location |
| 500 | `"Could not register the partitions (request ID: ...)."` | Glue refused to register at least one partition for a reason other than it already existing. Some hours may already be registered; fix the cause and re-run the date |

The cause of a 500 is in the server log under the request ID, not in the
response.

The API reaches Glue in the region named by `AWS_DEFAULT_REGION`, never
`AWS_REGION`. With it unset, the job, crawler and partitions routes answer
500. See [AWS integration: Glue](../integrations/aws.md#glue-the-etl-routes).

---

### Real-time DynamoDB Counters

```text
GET  /api/v1/counters/{experiment_id}              — Get experiment counters
POST /api/v1/counters/{experiment_id}/increment    — Increment counter
POST /api/v1/counters/{experiment_id}/reset        — Reset counters (ADMIN)
POST /api/v1/counters/bulk                         — Bulk counter update
```

On the two routes that take `{experiment_id}`, the path is what is written:
the body's `experiment_id` must equal it, and a request where they disagree is
answered **400** rather than written somewhere else (#95).

`bulk` takes no `{experiment_id}` **by design**: each of its up-to-100
increments names its own experiment, so one call may span several and there is
no single experiment for the URL to name.

---

## Additional Endpoints

The following endpoints extend the core API. See the dedicated reference pages linked below for full request/response schemas, authentication requirements, and code examples.

---

### Compliance Audit Logging

See [Compliance API Reference](compliance.md) for full documentation.

ADMIN or ANALYST for the listing and the reports; ADMIN for the export.

```text
GET /api/v1/compliance/audit-events                — List audit events (paginated, filterable; every profile)
GET /api/v1/compliance/reports/soc2                — SOC 2 report, default period 365 days (compliance module)
GET /api/v1/compliance/reports/iso27001            — ISO 27001 report, default period 730 days (compliance module)
GET /api/v1/compliance/export                      — Download as JSON or CSV (ADMIN; compliance module)
```

**Query parameters for `audit-events`**: `page`, `limit`, `action`, `resource_type`, `actor_id`, `start_time`, `end_time`

**Query parameters for `export`**: `format` (`json`|`csv`), `start_time`, `end_time`

With the compliance module, each event is signed with HMAC-SHA256 and the signature is in its `hmac_signature` field; the report checks them. See [Compliance API Reference](compliance.md) for what is recorded.

---

### Third-Party Integrations

See [Integrations API Reference](integrations.md) for full documentation.

Read (the list and the get) needs the **ADMIN** or **DEVELOPER** role; create, update and delete need **ADMIN**. An ANALYST or a VIEWER is refused with `403` on every one of them. A configuration is addressed by its type (`jira`, `salesforce` or `github`), not by an id.

**Integration CRUD**:

```text
POST   /api/v1/integrations                      — Create integration (ADMIN)
GET    /api/v1/integrations                      — List integrations (ADMIN or DEVELOPER)
GET    /api/v1/integrations/{integration_type}   — Get integration details (ADMIN or DEVELOPER)
PUT    /api/v1/integrations/{integration_type}   — Update integration config (ADMIN)
DELETE /api/v1/integrations/{integration_type}   — Delete integration (ADMIN)
```

**Webhook receivers**:

```text
POST /api/v1/integrations/webhooks/jira        — Receive Jira issue events
POST /api/v1/integrations/webhooks/salesforce  — Receive Salesforce JSON (Flow/Apex callout)
POST /api/v1/integrations/webhooks/github      — Receive GitHub events (HMAC-SHA256 validated)
```

Supported `IntegrationType` values: `jira`, `salesforce`, `github` (lower case, in the body and in the path).

---

### Bayesian Experimentation

See [Bayesian API Reference](bayesian.md) for full documentation.

Bayesian analysis is enabled per experiment with two fields on the standard experiment
create/update request body. The experiment response carries both, plus `bayesian_decision`:

| Field | Type | Description |
|---|---|---|
| `bayesian_enabled` | `boolean` | Computes Bayesian results for this experiment. Turning it on without a config stores the defaults |
| `bayesian_config` | `object` | `prior_family`, `alpha`, `beta`, `loss_threshold`, `rope`, `credible_level`; see [the `bayesian_config` object](bayesian.md#turning-bayesian-analysis-on). An invalid value is refused with `422` |
| `bayesian_decision` | `string` | Response only: the latest recommendation, `CONTINUE`, `STOP_WINNER`, `STOP_EQUIVALENT` or `STOP_FUTILE`. Nothing stops the experiment on it |

When `bayesian_enabled` is `true`, `GET /api/v1/results/{experiment_id}` embeds a
`bayesian_results` block, and `GET /api/v1/results/{experiment_id}/bayesian` returns it on
its own: a posterior, credible interval, probability to be best and expected loss for each
variant, and the decision.

---

### Server-Side Split URL Testing

See [Split URL API Reference](split-url.md) for full documentation.

Split URL experiments use `experiment_type: split_url` and carry a `split_url_config` in the request body. How visitors are meant to be redirected, and what does not work in this release, is on the [Split URL API Reference](split-url.md).

**Experiment management** uses the existing experiment CRUD endpoints with `experiment_type=split_url`:

```text
POST /api/v1/experiments/              — Create split URL experiment (set experiment_type=split_url)
PUT  /api/v1/experiments/{id}          — Update split URL config
GET  /api/v1/experiments/{id}          — Returns split_url_config in response
```

**Split URL-specific endpoint**:

```text
GET /api/v1/experiments/{experiment_id}/split-url/preview  — Preview variant assignment for a user (dev/QA)
```

Query parameters for preview: `user_id` (required). The preview hashes `{user_id}:{experiment_id}`;
it does not predict the edge router.

**`SplitUrlConfig` schema** (`split_url_config` field):

```json
{
  "variants": [
    { "name": "Control",   "url": "https://example.com/page-v1", "traffic_allocation": 50 },
    { "name": "Treatment", "url": "https://example.com/page-v2", "traffic_allocation": 50 }
  ]
}
```

At least two URL variants, with `traffic_allocation` values (0-100) that sum to 100.
In a `split_url` experiment the URL variants name the experiment's variants one to one,
with the same allocations, and each `url` is an absolute `http://` or `https://` URL; see
[Validation Rules](split-url.md#validation-rules).
`cookie_name` and `cookie_ttl_days` (default 30) are optional.

# RBAC API Reference

!!! info "Part of the `rbac` module"
    Custom roles is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

## Overview

A user may do what their built-in role allows, plus what their custom roles and unexpired direct grants hold. Roles and grants only add: none takes a permission away. A role or grant can hold only the eleven permissions below, and none gives administration: managing users, custom roles and grants, other users' API keys, holdouts and exclusion-group status, and safety settings depends on the built-in role or the superuser flag, never on a custom role or grant.

Three layers decide what a user may do:

1. **Built-in role** (`base_role` in the API) — One of four roles every user has (`admin`, `developer`, `analyst`, `viewer`). Defined as the static `ROLE_PERMISSIONS` dict in `backend/app/core/permissions.py`.
2. **Custom Roles** — DB-backed roles with configurable per-resource/action permission sets. An admin can create any number of custom roles and assign them to users.
3. **Direct Permission Grants** — Per-user, per-resource grants made without assigning a role. Supports optional expiry timestamps for temporary grants.

Roles are assigned and grants made through this API; the dashboard's Roles page defines roles.

### How a permission check decides

Every permission check (`check_permission()`, `can_act_on_feature_flag()` and `can_read_all_audit_logs()` in `backend/app/core/permissions.py`) answers in this order:

```text
allowed(user, resource, action) =
    user.is_superuser
    or ROLE_PERMISSIONS[user.base_role] holds (resource, action)
    or (resource, action) is one of the eleven, and an active custom role
       or an unexpired direct grant of the user holds it
```

- Permissions are **additive** — a permission held by any layer is held. There is no deny: a custom role or grant cannot take away what the built-in role gives.
- A custom role or direct grant of a `feature_flag` action admits any flag, whoever owns it: access to flags is by permission, not ownership.
- Changes apply at once: a role assigned, a grant made, revoked or expired, or a role changed or deleted is read on the user's next request.
- Expired or deactivated direct grants, and assignments and grants recorded before custom roles took effect (below), are left out.
- A permission a role or grant stores outside the eleven (from before they were checked) never takes effect.

Superusers (`is_superuser=True`) pass every permission check.

### What each permission allows

| Permission | What a custom role or direct grant of it allows |
|---|---|
| `experiment (create)` | Create and clone experiments, including through the experiment wizard and LLM experiments. Also: read the global holdout, and manage exclusion groups (list, create, view, change other than their status, add and remove experiments). Create segments. |
| `experiment (read)` | Every built-in role already has it. |
| `experiment (update)` | Change, schedule, start, pause, complete, archive and annotate any experiment; trigger a bandit update; change LLM experiments; change segments and their members; in the full profile, write the real-time counters. |
| `experiment (delete)` | Delete draft experiments, and archive segments. |
| `experiment (list)` | Every built-in role already has it. |
| `feature_flag (create)` | Create flags. |
| `feature_flag (read)` | Every built-in role already has it. |
| `feature_flag (update)` | Change any flag: on and off, targeting, values, rollout and rollout schedules, including bulk toggle. Also: create and use server-side SDK keys (API keys with the `sdk:ruleset` scope); assigning users in a batch with such a key also needs `experiment (update)`. |
| `feature_flag (delete)` | Delete any flag. |
| `feature_flag (list)` | Every built-in role already has it. |
| `audit_log (read)` | Read the audit log, including other users' entries, its statistics and each item's history. Without it, Developer and Viewer users read only their own entries. It does not give the compliance audit events, reports or exports. |

### What a role or grant cannot give

A custom role or direct grant holds only the eleven permissions above and only adds to the built-in role. Whatever a user's roles and grants hold, these stay with the built-in role or the superuser flag:

- **Administration:** managing users; defining custom roles, assigning them and making grants (ADMIN); other users' API keys; creating, activating and ending global holdouts, and archiving or unarchiving exclusion groups (ADMIN); safety settings; and the optional modules' administration (ETL, HIPAA, integrations, SSO, resetting counters).
- **The compliance audit trail:** the compliance audit events (`GET /api/v1/compliance/audit-events`) and reports (`GET /api/v1/compliance/reports/{standard}`) need ADMIN or ANALYST, and the compliance export (`GET /api/v1/compliance/export`) needs ADMIN; a superuser reads all three. `audit_log (read)` reads the audit log, not this trail.
- **Other built-in-role decisions:** the split-URL preview, interaction analysis, notifications and the warehouse analysis roles.

To restrict someone, give them a lower built-in role. A custom role or grant never takes a permission away.

### Assignments and grants made before custom roles took effect

**Assignments and grants made before custom roles took effect** grant nothing, and the API does not list them: `custom_roles` and `user_count` count only assignments that take effect. To keep one, assign the role or make the grant again. The migration `modules_0004_rbac_activation` logged those that existed when it ran; any made after it ran were not logged. Making a grant again adds a new grant beside the old one, so a later revoke counts both; to drop an old grant, revoke it (`DELETE /api/v1/rbac/users/{user_id}/grant/{resource}` deletes inactive grants too).

Downgrading the modules schema below `modules_0004_rbac_activation` after the release in which custom roles took effect drops every activation time: every assignment and grant is inactive again, and stays inactive after upgrading; assign the roles and make the grants again. See [Migrations](../self-hosting/migrations.md).

---

## Authentication

All RBAC endpoints require a valid bearer token in the `Authorization` header:

```text
Authorization: Bearer <token>
```

The examples on this page run in one terminal, in order, against the full profile
([Modules and profiles](../getting-started/modules.md)). Each uses the shell variables set
by the ones before it. Sign in as the stack's administrator, which saves a token in
`$TOKEN`:

```{.bash exec}
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. If it prints `null`, the sign-in failed and `$TOKEN` holds no token.

The examples give roles and grants to the stack's demo analyst, `analyst@demo.com`. This
saves the analyst's user id in `$ANALYST_ID`:

```{.bash exec}
ANALYST=$(curl -s "http://localhost:8000/api/v1/admin/users?search=analyst@demo.com" \
  -H "Authorization: Bearer $TOKEN" | jq '.items[0]')
ANALYST_ID=$(jq -r .id <<<"$ANALYST")

jq '{email, role}' <<<"$ANALYST"
```
<!-- expect: "email": "analyst@demo.com" -->
<!-- expect: "role": "ANALYST" -->

It prints the analyst's `email` and `"role": "ANALYST"`.

---

## Endpoints

### List Custom Roles

```text
GET /api/v1/rbac/roles
```

Returns all custom roles. Any authenticated user can call this.

**Query Parameters**

| Parameter       | Type    | Default | Description                                          |
|-----------------|---------|---------|------------------------------------------------------|
| `include_system`| boolean | `true`  | When `false`, leaves out roles marked `is_system_role`. Nothing in this release creates one, so both forms list the custom roles you created. |

**Response 200**

```json
[
  {
    "id": "a1b2c3d4-...",
    "name": "data-scientist",
    "description": "Reads experiments and creates them",
    "is_system_role": false,
    "permissions": [
      {"resource": "experiment", "actions": ["read", "list", "create"]}
    ],
    "created_at": "2026-03-01T10:00:00Z",
    "user_count": 5
  }
]
```

**Example**

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/roles?include_system=false"
```
<!-- expect: [] -->

On a new deployment it prints `[]`: there are no custom roles until you create one.

---

### Create Custom Role

```text
POST /api/v1/rbac/roles
```

Creates a new custom role. **ADMIN only.**

**Request Body**

```json
{
  "name": "read-only-experiments",
  "description": "Can view experiments and results but not create or modify",
  "permissions": [
    {"resource": "experiment", "actions": ["read", "list"]},
    {"resource": "feature_flag", "actions": ["read", "list"]}
  ]
}
```

**Name validation rules:**
- Minimum 2 characters, maximum 64 characters
- Must start with a lowercase letter (`a-z`)
- Only lowercase letters, digits, hyphens (`-`), and underscores (`_`) are allowed
- Examples of valid names: `data-scientist`, `read_only_experiments`, `analyst2`

**What a role or a grant can hold:** `experiment` and `feature_flag`, each with `create`,
`read`, `update`, `delete` and `list`, and `audit_log` with `read`. A request that gives a
custom role or a direct grant any other resource or action (`user`, `role`, `permission`,
`report`, `export`, or `audit_log` with anything but `read`) is refused with `422`, and
nothing is written. Roles and grants stored before this check read back unchanged; see
[Update Custom Role](#update-custom-role) and
[Revoke Direct Permission](#revoke-direct-permission) for removing what they hold.

**Response 201**

```json
{
  "id": "a1b2c3d4-...",
  "name": "read-only-experiments",
  "description": "Can view experiments and results but not create or modify",
  "is_system_role": false,
  "permissions": [
    {"resource": "experiment", "actions": ["read", "list"]},
    {"resource": "feature_flag", "actions": ["read", "list"]}
  ],
  "created_at": "2026-03-01T10:00:00Z",
  "user_count": 0
}
```

**Error responses:**
- `409 Conflict` — A role with this name already exists
- `403 Forbidden` — Caller is not an admin
- `422 Unprocessable Entity` — Invalid name format or empty actions list, or a permission
  a role cannot hold (`"type": "not_grantable"`, below)

A permission a role cannot hold is refused with `"type": "not_grantable"` at
`["body", "permissions"]`. The message names each refused resource once, with only its
refused actions, then what a role or a grant can hold:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "exporter",
    "permissions": [
      {"resource": "export", "actions": ["read"]},
      {"resource": "audit_log", "actions": ["read", "list"]}
    ]
  }' | jq '.detail[0] | {type, loc, msg}'
```
<!-- expect: "type": "not_grantable" -->
<!-- expect: "msg": "export (read) and audit_log (list) cannot be granted. A custom role or a direct grant -->
<!-- expect: can hold only experiment (create, read, update, delete, list), feature_flag (create, read, update, delete, list) and audit_log (read)" -->

It prints `"type": "not_grantable"`, the `loc` and the message
`export (read) and audit_log (list) cannot be granted. A custom role or a direct grant can hold only experiment (create, read, update, delete, list), feature_flag (create, read, update, delete, list) and audit_log (read)`.
No `exporter` role is created.

**Example**

This creates the `data-scientist` role that the examples below assign:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "data-scientist",
    "description": "Reads experiments and creates them",
    "permissions": [
      {"resource": "experiment", "actions": ["read", "list", "create"]}
    ]
  }' | jq '{name, is_system_role, user_count}'
```
<!-- expect: "name": "data-scientist" -->
<!-- expect: "is_system_role": false -->
<!-- expect: "user_count": 0 -->

It prints `"name": "data-scientist"`, `"is_system_role": false` and `"user_count": 0`.

---

### Get Custom Role

```text
GET /api/v1/rbac/roles/{role_name}
```

Get details of a specific role. Any authenticated user can call this.

**Path Parameters**

| Parameter   | Type   | Description       |
|-------------|--------|-------------------|
| `role_name` | string | The role's name.  |

**Response 200**

```json
{
  "id": "a1b2c3d4-...",
  "name": "data-scientist",
  "description": null,
  "is_system_role": false,
  "permissions": [...],
  "created_at": "2026-03-01T10:00:00Z",
  "user_count": 3
}
```

**Error responses:**
- `404 Not Found` — Role does not exist

---

### Update Custom Role

```text
PUT /api/v1/rbac/roles/{role_name}
```

Update a custom role's description or permission set. **ADMIN only.** System roles cannot be modified.

**Request Body** (all fields optional)

```json
{
  "description": "Updated description",
  "permissions": [
    {"resource": "experiment", "actions": ["read", "list", "create"]}
  ]
}
```

**Response 200** — Updated role object (same shape as GET)

`permissions`, when it is sent, replaces the role's whole set and is checked like
Create's: a permission a role cannot hold is refused with `422`
(`"type": "not_grantable"`), and nothing changes. A role stored before that check with
other permissions still reads back with them, and an update that sends only
`description` keeps them; to remove them, send the permissions the role should keep.

**Error responses:**
- `400 Bad Request` — Attempted to modify a system role
- `403 Forbidden` — Caller is not an admin
- `404 Not Found` — Role does not exist
- `422 Unprocessable Entity` — A permission a role cannot hold (`"type": "not_grantable"`)

---

### Delete Custom Role

```text
DELETE /api/v1/rbac/roles/{role_name}
```

Delete a custom role. **ADMIN only.** System roles cannot be deleted.

**Response 204** — No content

**Error responses:**
- `400 Bad Request` — Attempted to delete a system role
- `403 Forbidden` — Caller is not an admin
- `404 Not Found` — Role does not exist

---

### Assign Role to User

```text
POST /api/v1/rbac/roles/assign
```

Assign a custom role to a user. **ADMIN only.** The role takes effect from the user's next request. This operation is idempotent — assigning a role to a user who already has it is a no-op. Assigning a role whose assignment was recorded before custom roles took effect activates it; the response is the same `assigned`.

**Request Body**

```json
{
  "user_id": "user-uuid",
  "role_name": "data-scientist",
  "reason": "Promoted to data science team"
}
```

**Response 200**

```json
{
  "status": "assigned",
  "user_id": "user-uuid",
  "role": "data-scientist"
}
```

**Error responses:**
- `403 Forbidden` — Caller is not an admin
- `404 Not Found` — Role does not exist

**Example**

This assigns the `data-scientist` role created above to the analyst:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles/assign \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "'"$ANALYST_ID"'", "role_name": "data-scientist", "reason": "Promoted to data science team"}' | jq .
```
<!-- expect: "status": "assigned" -->
<!-- expect: "role": "data-scientist" -->

It prints `"status": "assigned"` and `"role": "data-scientist"`. From the analyst's next
request the role takes effect: the analyst can create experiments, which the `analyst`
built-in role alone does not allow. The end of this page revokes it.

---

### Revoke Role from User

```text
POST /api/v1/rbac/roles/revoke
```

Revoke a custom role from a user. **ADMIN only.**

**Request Body**

```json
{
  "user_id": "user-uuid",
  "role_name": "data-scientist",
  "reason": "Moved to different team"
}
```

**Response 200**

```json
{
  "status": "revoked",
  "user_id": "user-uuid",
  "role": "data-scientist"
}
```

---

### Get Effective Permissions

```text
GET /api/v1/rbac/users/{user_id}/permissions
```

Returns what the permission checks allow the user: for `experiment` and `feature_flag`, the actions the checks admit (from the built-in role, active custom roles and unexpired direct grants), and `audit_log` with `read` when the user may read other users' audit entries. Only these three resources are listed: no other can be held by a custom role or grant, and administration depends on the built-in role or the superuser flag. `custom_roles` lists only the roles that take effect.

**Access control:** Users can view their own permissions. Admins can view any user's permissions.

**Response 200**

The response carries `user_id`, `username`, `base_role`, `custom_roles`, `permissions`
(each resource with the actions allowed on it) and `is_superuser`. The example below prints
part of it for the analyst.

Note: actions within each resource are returned sorted alphabetically.

**Error responses:**
- `403 Forbidden` — Non-admin user trying to view another user's permissions
- `404 Not Found` — User does not exist

**Example**

View your own permissions. `GET /api/v1/auth/me` gives your user id:

```{.bash exec}
MY_ID=$(curl -s http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq -r .id)

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$MY_ID/permissions" | jq '{base_role, custom_roles, is_superuser}'
```
<!-- expect: "base_role": "admin" -->
<!-- expect: "custom_roles": [] -->
<!-- expect: "is_superuser": true -->

For the administrator it prints `"base_role": "admin"`, no custom roles and
`"is_superuser": true`.

An admin viewing another user's permissions, here the analyst's:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | jq -c '{base_role, custom_roles, experiment: .permissions.experiment}'
```
<!-- expect: "base_role":"analyst" -->
<!-- expect: "data-scientist" -->
<!-- expect: "experiment":["create","list","read"] -->

It prints the built-in role, `"analyst"`, the `data-scientist` role, and
`"experiment":["create","list","read"]`: `create` comes from that role, which the `analyst`
built-in role does not have, and the analyst can now create experiments.

---

### Grant Direct Permission

```text
POST /api/v1/rbac/users/{user_id}/grant
```

Grant a specific resource permission directly to a user, without assigning a role. The grant takes effect from the user's next request, until it expires or is revoked. Supports optional expiry for temporary grants. **ADMIN only.**

**Request Body**

```json
{
  "user_id": "user-uuid",
  "resource": "feature_flag",
  "actions": ["update"],
  "reason": "Covering the release rotation",
  "expires_at": "2026-04-01T00:00:00Z"
}
```

`expires_at` is optional. If omitted, the grant never expires. `resource` and `actions`
are checked as a role's permissions are (see
[What a role or a grant can hold](#create-custom-role)): any other pair is refused with
`422` (`"type": "not_grantable"`, at `["body"]`), and nothing is written.

**Response 201**

```json
{
  "status": "granted",
  "user_id": "user-uuid",
  "resource": "feature_flag",
  "actions": ["update"]
}
```

**Error responses:**
- `403 Forbidden` — Caller is not an admin
- `404 Not Found` — User does not exist
- `422 Unprocessable Entity` — A permission a grant cannot hold (`"type": "not_grantable"`)

**Example — Temporary permission with expiry**

`expires_at` must be in the future: a grant whose `expires_at` has already passed is
stored, but Get Effective Permissions leaves it out from the start. This grants the
analyst `feature_flag` `update` for a week from now:

```{.bash exec}
WEEK=$(jq -nr 'now + 7*86400 | todate')

curl -s -X POST "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "'"$ANALYST_ID"'",
    "resource": "feature_flag",
    "actions": ["update"],
    "reason": "One-week release rotation",
    "expires_at": "'"$WEEK"'"
  }' | jq .
```
<!-- expect: "status": "granted" -->
<!-- expect: "resource": "feature_flag" -->

It prints `"status": "granted"` and `"resource": "feature_flag"`. Until it expires, the
analyst can change any flag. The end of this page revokes it.

---

### Revoke Direct Permission

```text
DELETE /api/v1/rbac/users/{user_id}/grant/{resource}
```

Revoke all direct permission grants for a user+resource combination. **ADMIN only.**

**Path Parameters**

| Parameter  | Type   | Description                |
|------------|--------|----------------------------|
| `user_id`  | UUID   | Target user's ID           |
| `resource` | string | Resource name (e.g. `experiment`) |

**Response 200**

```json
{
  "status": "revoked",
  "count": 1
}
```

`count` is the number of grant records deleted, and includes inactive and expired grants.
`resource` is any text, not only a resource a grant can hold now, so a grant stored before
that check (of `report` or `export`, say) can still be deleted.

---

## How-To Examples

### Create a "Read-Only Experiments" Custom Role

This defines and assigns a role that holds only reading experiments and flags. Every built-in role already reads them, so for the analyst it adds nothing: a custom role only adds. To keep a user read-only, give them the `viewer` or `analyst` built-in role.

Step 1: Create the role:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "read-only-experiments",
    "description": "View-only access to experiments and flags",
    "permissions": [
      {"resource": "experiment", "actions": ["read", "list"]},
      {"resource": "feature_flag", "actions": ["read", "list"]}
    ]
  }' | jq -r .name
```
<!-- expect: read-only-experiments -->

It prints `read-only-experiments`.

Step 2: Assign the role to a user, here the analyst:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles/assign \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "'"$ANALYST_ID"'",
    "role_name": "read-only-experiments",
    "reason": "Stakeholder who needs to monitor experiments"
  }' | jq -r .status
```
<!-- expect: assigned -->

It prints `assigned`.

Step 3: Check the assignment in the user's effective permissions:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | jq -c .custom_roles
```
<!-- expect: "read-only-experiments" -->

It prints the analyst's custom roles, `["data-scientist","read-only-experiments"]` after
the examples above. The new role adds nothing the analyst could not already do.

### Grant a Temporary Permission

This lets a data analyst change experiments for a week without changing their role. The grant takes effect from their next request.

```{.bash exec}
WEEK=$(jq -nr 'now + 7*86400 | todate')

curl -s -X POST "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "'"$ANALYST_ID"'",
    "resource": "experiment",
    "actions": ["update"],
    "reason": "Covering the experiment review",
    "expires_at": "'"$WEEK"'"
  }' | jq -r .status
```
<!-- expect: granted -->

It prints `granted`. After `expires_at`, the grant no longer takes effect and Get Effective Permissions leaves it out, without manual cleanup.

To revoke early:

```{.bash exec}
curl -s -X DELETE \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant/experiment" \
  -H "Authorization: Bearer $TOKEN" | jq -c .
```
<!-- expect: {"status":"revoked","count":1} -->

It prints `{"status":"revoked","count":1}`: the one `experiment` grant is deleted, and from
the analyst's next request they can no longer change experiments.

### Let a Viewer Read the Audit Log

A viewer reads only their own audit entries. A grant of `audit_log (read)` lets them read
other users' entries too, and revoking it takes that away on their next request. Sign in as
the demo viewer, `viewer@demo.com`, and save the viewer's token and user id:

```{.bash exec}
VIEWER_TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"viewer@demo.com","password":"Demo1234!"}' | jq -r .access_token)
VIEWER_ID=$(curl -s http://localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer $VIEWER_TOKEN" | jq -r .id)

curl -s "http://localhost:8000/api/v1/audit-logs/?user_id=$MY_ID&action_type=user_login" \
  -H "Authorization: Bearer $VIEWER_TOKEN" | jq -c '[.items[].user_email] | unique'
```
<!-- expect: ["viewer@demo.com"] -->

It prints `["viewer@demo.com"]`: the viewer asked for the administrator's sign-ins and was
answered with their own, whatever they ask for.

The administrator grants the viewer `audit_log (read)`:

```{.bash exec}
curl -s -X POST "http://localhost:8000/api/v1/rbac/users/$VIEWER_ID/grant" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "'"$VIEWER_ID"'", "resource": "audit_log", "actions": ["read"], "reason": "Quarterly access review"}' | jq -c '{status, resource}'

curl -s "http://localhost:8000/api/v1/audit-logs/?user_id=$MY_ID&action_type=user_login" \
  -H "Authorization: Bearer $VIEWER_TOKEN" | jq -c '[.items[].user_email] | unique'
```
<!-- expect: {"status":"granted","resource":"audit_log"} -->
<!-- expect: ["admin@demo.com"] -->

It prints `{"status":"granted","resource":"audit_log"}`, then `["admin@demo.com"]`: with the
same token, the viewer now reads the administrator's entries. Revoke the grant:

```{.bash exec}
curl -s -X DELETE "http://localhost:8000/api/v1/rbac/users/$VIEWER_ID/grant/audit_log" \
  -H "Authorization: Bearer $TOKEN" | jq -c '{audit_log_grants_revoked: .count}'

curl -s "http://localhost:8000/api/v1/audit-logs/?user_id=$MY_ID&action_type=user_login" \
  -H "Authorization: Bearer $VIEWER_TOKEN" | jq -c '[.items[].user_email] | unique'
```
<!-- expect: {"audit_log_grants_revoked":1} -->
<!-- expect: ["viewer@demo.com"] -->

It prints `{"audit_log_grants_revoked":1}`, then `["viewer@demo.com"]` again.

### Show a User's Permissions

This shows what the permission checks allow the user (see [Overview](#overview)).

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | \
  python3 -m json.tool
```
<!-- expect: "base_role": "analyst" -->
<!-- expect: "custom_roles": [ -->

It prints the whole set, beginning with the analyst's `user_id`, `username` and
`"base_role": "analyst"`.

### Leave the Demo as It Was

No example on this page leaves a demo user with more than their built-in role allows. This
revokes the two roles and the flag grant the examples above gave the analyst:

```{.bash exec}
for ROLE in data-scientist read-only-experiments; do
  curl -s -X POST http://localhost:8000/api/v1/rbac/roles/revoke \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "'"$ANALYST_ID"'", "role_name": "'"$ROLE"'"}' | jq -r .status
done

curl -s -X DELETE "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant/feature_flag" \
  -H "Authorization: Bearer $TOKEN" | jq -r .status

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | \
  jq -c '{custom_roles, experiment: .permissions.experiment}'
```
<!-- expect: {"custom_roles":[],"experiment":["list","read"]} -->

It prints `revoked` three times, then `{"custom_roles":[],"experiment":["list","read"]}`:
the analyst is back to what the `analyst` built-in role allows.

---

## Service API

Every permission check in `backend/app/core/permissions.py` reads custom roles and direct grants through one capability, `rbac.granted_permissions`, which the module registers as `granted_permission_pairs` in `modules/backend/app/services/rbac_service.py`. The `RBACService` there builds what Get Effective Permissions returns from the same checks:

```python
from backend.app.core.permissions import Action, ResourceType, check_permission
from modules.backend.app.services.rbac_service import RBACService

# May this user (a row of the request's session) create experiments? The
# built-in role first, then their active custom roles and unexpired grants.
can_do_it = check_permission(user, ResourceType.EXPERIMENT, Action.CREATE)

# What the checks allow, for the three resources a role or grant can hold
effective = RBACService.get_effective_permissions(db, user)
print(effective.permissions)  # {"experiment": ["create", "list", "read"], ...}
```

`check_permission` reads a user's custom roles and grants only for a user that is a row of a database session (a request's own), once per request; a user object that is not one gets its built-in role alone.

---

## Data Models

### CustomRole

| Column          | Type       | Description                                      |
|-----------------|------------|--------------------------------------------------|
| `id`            | UUID       | Primary key                                      |
| `name`          | string(64) | Unique role name (lowercase, slugified)          |
| `description`   | text       | Optional human-readable description              |
| `is_system_role`| boolean    | `true` for built-in roles (protected from edits) |
| `permissions`   | JSONB      | List of `{resource, actions[]}` objects          |
| `created_by_id` | UUID FK    | User who created the role                        |
| `created_at`    | datetime   | Creation timestamp                               |
| `updated_at`    | datetime   | Last update timestamp                            |

### UserCustomRole

| Column           | Type     | Description                    |
|------------------|----------|--------------------------------|
| `id`             | UUID     | Primary key                    |
| `user_id`        | UUID FK  | Reference to user              |
| `role_id`        | UUID FK  | Reference to custom role       |
| `assigned_by_id` | UUID FK  | Admin who made the assignment  |
| `reason`         | text     | Optional audit reason          |
| `assigned_at`    | datetime | When the assignment was made   |
| `activated_at`   | datetime | When the assignment took effect; `null` for one recorded before custom roles took effect, which grants nothing |

### DirectPermissionGrant

| Column          | Type     | Description                                   |
|-----------------|----------|-----------------------------------------------|
| `id`            | UUID     | Primary key                                   |
| `user_id`       | UUID FK  | Target user                                   |
| `resource`      | string   | Resource name (e.g. `experiment`)             |
| `actions`       | JSONB    | List of action strings (e.g. `["read"]`)      |
| `granted_by_id` | UUID FK  | Admin who granted the permission              |
| `reason`        | text     | Optional audit reason                         |
| `expires_at`    | datetime | Optional expiry; `null` means never expires   |
| `is_active`     | boolean  | Can be deactivated without deleting           |
| `activated_at`  | datetime | When the grant took effect; `null` for one recorded before custom roles took effect, which grants nothing |

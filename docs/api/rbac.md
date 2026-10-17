# RBAC API Reference

!!! info "Part of the `rbac` module"
    Custom roles is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

## Overview

The RBAC (Role-Based Access Control) module stores three layers of permissions for a user:

1. **Base Role** — One of four built-in system roles assigned to every user (`admin`, `developer`, `analyst`, `viewer`). Defined as the static `ROLE_PERMISSIONS` dict in `backend/app/core/permissions.py`.
2. **Custom Roles** — DB-backed roles with configurable per-resource/action permission sets. An admin can create any number of custom roles and assign them to users.
3. **Direct Permission Grants** — Per-user, per-resource grants made without assigning a role. Supports optional expiry timestamps for temporary grants.

!!! warning "Only the base role decides what a user can do"
    Routes check the base role (through `check_permission()` in
    `backend/app/core/permissions.py`) or the superuser flag; none reads custom roles or
    direct grants.
    Custom roles and direct grants are stored, and shown by
    `GET /api/v1/rbac/users/{user_id}/permissions`, but no permission check reads them:
    assigning a custom role or granting a permission changes nothing a user can do.
    Whether to make them take effect or remove them is tracked in
    [#891](https://github.com/Controlvar/controlvar/issues/891).

### Effective Permissions Resolution

`GET /api/v1/rbac/users/{user_id}/permissions` merges all three layers to build the set it shows. No permission check uses this set (see the note above):

```text
effective_permissions(user) =
    ROLE_PERMISSIONS[user.base_role]
    ∪ union(custom_role.permissions for each custom_role assigned to user)
    ∪ union(grant.permissions for each valid direct grant for user)
```

Superusers (`is_superuser=True`) pass every permission check and have full access to every resource and action.

### Permission Resolution Precedence

These rules describe the set the endpoint shows:

- Permissions are **additive** — a permission granted by any layer is granted overall.
- There is no deny mechanism; a higher layer cannot revoke a permission given by a lower layer.
- Expired or deactivated direct grants are excluded from the union.

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
    "description": "Read access to experiments plus export",
    "is_system_role": false,
    "permissions": [
      {"resource": "experiment", "actions": ["read", "list"]},
      {"resource": "export", "actions": ["read"]}
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
    {"resource": "report", "actions": ["read", "list"]}
  ]
}
```

**Name validation rules:**
- Minimum 2 characters, maximum 64 characters
- Must start with a lowercase letter (`a-z`)
- Only lowercase letters, digits, hyphens (`-`), and underscores (`_`) are allowed
- Examples of valid names: `data-scientist`, `read_only_experiments`, `analyst2`

**Available resources:** `experiment`, `feature_flag`, `user`, `role`, `permission`, `report`, `audit_log`, `export`

**Available actions:** `create`, `read`, `update`, `delete`, `list`

**Response 201**

```json
{
  "id": "a1b2c3d4-...",
  "name": "read-only-experiments",
  "description": "Can view experiments and results but not create or modify",
  "is_system_role": false,
  "permissions": [
    {"resource": "experiment", "actions": ["read", "list"]},
    {"resource": "report", "actions": ["read", "list"]}
  ],
  "created_at": "2026-03-01T10:00:00Z",
  "user_count": 0
}
```

**Error responses:**
- `409 Conflict` — A role with this name already exists
- `403 Forbidden` — Caller is not an admin
- `422 Unprocessable Entity` — Invalid name format or empty actions list

**Example**

This creates the `data-scientist` role that the examples below assign:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "data-scientist",
    "description": "Read access to experiments plus export",
    "permissions": [
      {"resource": "experiment", "actions": ["read", "list"]},
      {"resource": "export", "actions": ["read"]}
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

**Error responses:**
- `400 Bad Request` — Attempted to modify a system role
- `403 Forbidden` — Caller is not an admin
- `404 Not Found` — Role does not exist

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

Assign a custom role to a user. **ADMIN only.** This operation is idempotent — assigning a role to a user who already has it is a no-op.

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

It prints `"status": "assigned"` and `"role": "data-scientist"`. The assignment is stored;
the analyst can still do only what the `analyst` base role allows.

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

Returns the fully resolved permission set for a user, merging base role + custom roles + valid direct grants. This is a display: routes check only the base role, so a custom role or direct grant listed here does not let the user do anything their base role does not.

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
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | jq '{base_role, custom_roles, export: .permissions.export}'
```
<!-- expect: "base_role": "analyst" -->
<!-- expect: "data-scientist" -->
<!-- expect: "export": [ -->
<!-- expect: "read" -->

It prints the base role, `"analyst"`, the stored `data-scientist` role, and `export: ["read"]`
from that role in the set shown. The `analyst` base role holds no export permission, and
it is still the only thing that decides what the analyst can do.

---

### Grant Direct Permission

```text
POST /api/v1/rbac/users/{user_id}/grant
```

Grant a specific resource permission directly to a user, without assigning a role. The grant is stored and shown by Get Effective Permissions; no permission check reads it yet ([#891](https://github.com/Controlvar/controlvar/issues/891)). Supports optional expiry for temporary grants. **ADMIN only.**

**Request Body**

```json
{
  "user_id": "user-uuid",
  "resource": "report",
  "actions": ["create", "read"],
  "reason": "Temporary access for Q1 reporting",
  "expires_at": "2026-04-01T00:00:00Z"
}
```

`expires_at` is optional. If omitted, the grant never expires.

**Response 201**

```json
{
  "status": "granted",
  "user_id": "user-uuid",
  "resource": "report",
  "actions": ["create", "read"]
}
```

**Error responses:**
- `403 Forbidden` — Caller is not an admin

**Example — Temporary permission with expiry**

`expires_at` must be in the future: a grant whose `expires_at` has already passed is
stored, but Get Effective Permissions leaves it out from the start. This grants the
analyst `report` access for a week from now:

```{.bash exec}
WEEK=$(jq -nr 'now + 7*86400 | todate')

curl -s -X POST "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "'"$ANALYST_ID"'",
    "resource": "report",
    "actions": ["create", "read"],
    "reason": "One-week access for reporting",
    "expires_at": "'"$WEEK"'"
  }' | jq .
```
<!-- expect: "status": "granted" -->
<!-- expect: "resource": "report" -->

It prints `"status": "granted"` and `"resource": "report"`. The grant is stored and shown;
it changes nothing the analyst can do.

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
| `resource` | string | Resource name (e.g. `export`) |

**Response 200**

```json
{
  "status": "revoked",
  "count": 1
}
```

`count` is the number of grant records deleted.

---

## How-To Examples

### Create a "Read-Only Experiments" Custom Role

This defines and assigns a role for a user who should see experiments and results but never create or modify them. The role is stored and shown; it does not change what the user can do, which their base role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)). Today, to keep a user read-only, give them the `viewer` or `analyst` base role.

Step 1: Create the role:

```{.bash exec}
curl -s -X POST http://localhost:8000/api/v1/rbac/roles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "read-only-experiments",
    "description": "View-only access to experiments and reports",
    "permissions": [
      {"resource": "experiment", "actions": ["read", "list"]},
      {"resource": "report", "actions": ["read", "list"]},
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

Step 3: Check that the assignment is stored, in the user's effective permissions:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | jq -c .custom_roles
```
<!-- expect: "read-only-experiments" -->

It prints the analyst's stored custom roles, `["data-scientist","read-only-experiments"]`
after the examples above. What the analyst can do is unchanged.

### Grant a Temporary Export Permission

This records a week-long export grant for a data analyst without changing their role. The grant is stored and shown by Get Effective Permissions, but no permission check reads it: it does **not** let the analyst export anything their base role does not already allow ([#891](https://github.com/Controlvar/controlvar/issues/891)).

```{.bash exec}
WEEK=$(jq -nr 'now + 7*86400 | todate')

curl -s -X POST "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "'"$ANALYST_ID"'",
    "resource": "export",
    "actions": ["read", "list"],
    "reason": "Q1 compliance audit access",
    "expires_at": "'"$WEEK"'"
  }' | jq -r .status
```
<!-- expect: granted -->

It prints `granted`. After `expires_at`, the direct grant is automatically excluded from the set Get Effective Permissions shows, without requiring manual cleanup.

To revoke early:

```{.bash exec}
curl -s -X DELETE \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/grant/export" \
  -H "Authorization: Bearer $TOKEN" | jq -c .
```
<!-- expect: {"status":"revoked","count":1} -->

It prints `{"status":"revoked","count":1}`: the one `export` grant is deleted.

### Show a User's Stored Permissions

This shows the merged set. What the user can actually do is decided by their base role alone (see [Overview](#overview)).

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/rbac/users/$ANALYST_ID/permissions" | \
  python3 -m json.tool
```
<!-- expect: "base_role": "analyst" -->
<!-- expect: "custom_roles": [ -->

It prints the whole set, beginning with the analyst's `user_id`, `username` and
`"base_role": "analyst"`.

---

## Service API

The `RBACService` in `modules/backend/app/services/rbac_service.py` provides these methods. `get_effective_permissions` builds the set the Get Effective Permissions endpoint returns. `check_effective_permission` is not called by any route ([#891](https://github.com/Controlvar/controlvar/issues/891)):

```python
from modules.backend.app.services.rbac_service import RBACService

# Would the merged set allow this action? No route calls this.
can_do_it = RBACService.check_effective_permission(
    db, user, resource="experiment", action="create"
)

# Get the full resolved permission set
effective = RBACService.get_effective_permissions(db, user)
print(effective.permissions)  # {"experiment": ["create", "list", "read", ...], ...}
```

`check_effective_permission` checks custom roles and direct grants in addition to the base role, but no route calls it. Routes call `check_permission()` from `backend/app/core/permissions.py`, which reads only the base role.

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

### DirectPermissionGrant

| Column          | Type     | Description                                   |
|-----------------|----------|-----------------------------------------------|
| `id`            | UUID     | Primary key                                   |
| `user_id`       | UUID FK  | Target user                                   |
| `resource`      | string   | Resource name (e.g. `export`)                 |
| `actions`       | JSONB    | List of action strings (e.g. `["read"]`)      |
| `granted_by_id` | UUID FK  | Admin who granted the permission              |
| `reason`        | text     | Optional audit reason                         |
| `expires_at`    | datetime | Optional expiry; `null` means never expires   |
| `is_active`     | boolean  | Can be deactivated without deleting           |

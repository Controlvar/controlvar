# Team Workspaces — Quickstart

!!! info "Part of the `workspaces` module"
    Team workspaces is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

This guide shows you how to create a workspace, add your team to it, and
connect an SDK.

A workspace groups members and their workspace roles. It does not limit which
experiments, feature flags or API keys anyone can see or change: that is decided
by each user's platform role across the whole installation (and, for an API key,
who created it). See the
[Workspace Overview](./overview.md#what-workspaces-do-not-do).

---

## 1. Create a Workspace

Any authenticated platform user can create a workspace.

```http
POST /api/v1/workspaces/
Authorization: Bearer <your-token>
Content-Type: application/json

{
  "name": "Acme Mobile Team",
  "slug": "acme-mobile",
  "description": "Experiments for the iOS and Android apps"
}
```

**Response (201 Created):**
```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "Acme Mobile Team",
  "slug": "acme-mobile",
  "description": "Experiments for the iOS and Android apps",
  "is_active": true,
  "created_at": "2026-03-07T00:00:00Z",
  "updated_at": "2026-03-07T00:00:00Z"
}
```

The user who creates the workspace is automatically assigned the **OWNER** role.

The dashboard's **Workspaces** page (`/workspaces`) then lists the workspace, with its name,
slug and description.

---

## 2. Invite Team Members

### Option A — Invite by email

```http
POST /api/v1/workspaces/{workspace_id}/invites
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "email": "alice@example.com",
  "role": "DEVELOPER"
}
```

Send the returned `token` to Alice.  Alice can accept the invite once she is
logged into the platform with `alice@example.com` (in any case); any other
account gets `403` with code `invite_email_mismatch`:

```http
POST /api/v1/workspaces/invites/{token}/accept
Authorization: Bearer <alice-token>
```

### Option B — Add existing user directly

If you already know the user's platform ID:

```http
POST /api/v1/workspaces/{workspace_id}/members
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "user_id": "alice-uuid-here",
  "role": "DEVELOPER"
}
```

---

## 3. Connect an SDK with a Platform API Key

Workspaces do not issue API keys. A superuser can create a platform key in the
dashboard under **Admin → API Keys**, and a key acts as the user who created it.

The SDKs authenticate with a **platform API key**, created with
`POST /api/v1/api-keys` -- not with a workspace key. See
[API Key Management](../security/api-keys.md) for how to create one, and the
[SDK guide](../sdk-guide.md) for how to pass it to each SDK.

---

## Next Steps

- [Workspace Overview](./overview.md) — workspace roles and invitations
- [API Reference](http://localhost:8000/api/v1/docs#/Workspaces) — interactive Swagger UI

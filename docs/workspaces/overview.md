# Team Workspaces — Overview

!!! info "Part of the `workspaces` module"
    Team workspaces is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

## What Are Workspaces?

A **workspace** groups platform users into a team, with a workspace role for
each member. It represents a team, project, or product area — for example:

- `acme-mobile` for the mobile engineering team
- `acme-web` for the web product team
- `acme-growth` for the growth squad

A workspace holds its members, their workspace roles and its pending
invitations. The workspace routes (`/api/v1/workspaces/...`) check membership
and workspace role: only a member can read a workspace or its member list, and
only an ADMIN or OWNER of that workspace can change its settings, members or
invites (any member can leave).

### What workspaces do not do

Workspaces do **not** decide which experiments, feature flags or API keys a
user can see or change. Experiments and feature flags are not created inside a
workspace, and workspace membership plays no part when they are listed, read
or changed. That is decided by the user's **platform role** (ADMIN, DEVELOPER,
ANALYST or VIEWER) across the whole installation — see [Role-based access
control](../rbac/README.md).

If you need teams to be unable to see each other's experiments and flags, run a
separate installation for each team.

---

## Role Hierarchy

Every workspace member has exactly one workspace role. The roles form a
hierarchy, and a workspace role applies only to the workspace routes:

| Role          | On the workspace routes |
|---------------|-------------------------|
| **OWNER**     | Everything an ADMIN can do, and delete the workspace |
| **ADMIN**     | Update workspace settings, add and remove members, change member roles, send invites |
| **DEVELOPER** | Read the workspace and its member list; leave the workspace |
| **ANALYST**   | Read the workspace and its member list; leave the workspace |
| **VIEWER**    | Read the workspace and its member list; leave the workspace |

DEVELOPER, ANALYST and VIEWER are recorded for each member but currently grant
the same thing on the workspace routes.

### Changing Roles

- An ADMIN or OWNER can change a member's role among VIEWER, ANALYST,
  DEVELOPER and ADMIN. The last remaining OWNER cannot be demoted or removed.
- A member can remove themselves; an OWNER can do so only while there is at
  least one other OWNER.

---

## Plans and Limits

Workspaces have no plan and no limits: a workspace can have any number of
members. A create or update request that sends `plan` is refused with `422`.

---

## API Keys

Workspaces do not issue API keys. To connect an SDK, send events or evaluate
flags, use a **platform API key**. Any signed-in user can create one for themselves
with `POST /api/v1/api-keys`; step 3 of the [quickstart](./quickstart.md)
links to the full guide.

---

## Invitations

Workspace members can be added in two ways:

1. **Direct add** (ADMIN+): Look up an existing platform user by their UUID and
   add them immediately with the desired role.
2. **Email invite** (ADMIN+): Create an invite for an email address. No email
   is sent. The dashboard shows the invitation link after you create it, with
   a **Copy link** button; the API response contains the token (the link is
   `/workspaces/invites/<token>` on the dashboard's address). Send the link to
   the recipient. The token is valid for 7 days. When the recipient accepts the
   invite (while authenticated), they are added as a member with the invited
   role.
   Anyone with the link can look the invitation up through the API, but only
   the invited account sees the address in full; everyone else sees it
   masked, for example `a•••@example.com`.

Only a user signed in with the invited address (compared without regard to
case) can accept it. Anyone else gets `403` with code `invite_email_mismatch`.
The address must be ASCII, and an alias is a different address:
`alice+work@example.com` does not accept an invite sent to `alice@example.com`.

> An invitation cannot grant the `OWNER` role.

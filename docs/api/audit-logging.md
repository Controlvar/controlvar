# Audit Logging & Bulk Toggle

The audit log is a record of who changed what. No API edits or deletes an entry. It
records the changes people make through the API: creating, changing and deleting feature
flags and experiments, turning flags on and off, starting, pausing and completing
experiments, rollout schedule changes, API keys, holdouts, mutual exclusion groups,
segments, creating and deleting users, changes to a user's role, superuser flag or active
status, signing in with a password or through Cognito (`POST /auth/token`; a sign-in
through the Cognito Hosted UI does not pass through the API and is not recorded), and
safety rollbacks. In the full profile it also records custom roles assigned and revoked, a
workspace member's role changes, and single sign-on (SSO) sign-ins.
[Action Types](#action-types) lists every action and when it is written.

It also records the changes the platform makes on its own: scheduled experiment starts and
ends, rollout stages the rollout scheduler starts, rollbacks the safety monitor makes, a
user's first Cognito or SSO sign-in, and role changes Cognito group sync and SSO sign-in
make. Those role changes are written by a
[system actor](#changes-the-platform-makes-on-its-own). The Quick Start's demo data
includes a history written by the seed script, in the form the platform writes it.

Creating, changing and deleting a feature flag or an experiment is also recorded in a
separate table, the compliance audit trail, which `GET /api/v1/compliance/audit-events`
lists in every profile; see [Compliance Audit Trail](compliance.md#what-is-recorded) for
what it holds.

Run the commands on this page in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by the
ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

---

## Bulk Feature Flag Toggle

Toggle several feature flags in one call. Each flag succeeds or fails on its own: the
call answers `200` with a result per flag, even when some of them fail.

These two flags give the call something to change. They are created on
(`"is_active": true`; a new flag is off otherwise). This saves their ids in `$DARK_ID`
and `$CHECKOUT_ID`:

```{.bash exec}
DARK_ID=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "dark-mode", "name": "Dark mode", "is_active": true}' | jq -r .id)
CHECKOUT_ID=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "new-checkout", "name": "New checkout", "is_active": true}' | jq -r .id)

echo "$DARK_ID $CHECKOUT_ID" | wc -w
```
<!-- expect: 2 -->

It prints `2`, one id per flag.

### POST /api/v1/feature-flags/bulk-toggle

`action` is `enable`, `disable` or `archive`, and `reason` is optional. This turns off
both flags, and a third id that doesn't exist:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/feature-flags/bulk-toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "flag_ids": ["'"$DARK_ID"'", "'"$CHECKOUT_ID"'", "00000000-0000-0000-0000-000000000000"],
    "action": "disable",
    "reason": "Checkout incident"
  }' | jq '{total, succeeded, failed, errors: [.results[].error]}'
```
<!-- expect: "total": 3 -->
<!-- expect: "succeeded": 2 -->
<!-- expect: "failed": 1 -->
<!-- expect: "Feature flag not found" -->

It prints `"succeeded": 2` and `"failed": 1`, with the error `"Feature flag not found"`
for the third id. The full response lists each flag's result, and the id of the audit
entry written for each flag that changed:

```json
{
  "total": 3,
  "succeeded": 2,
  "failed": 1,
  "results": [
    {"flag_id": "851811d2-…", "flag_key": "dark-mode", "success": true, "error": null, "old_status": "ACTIVE", "new_status": "INACTIVE"},
    {"flag_id": "392fd547-…", "flag_key": "new-checkout", "success": true, "error": null, "old_status": "ACTIVE", "new_status": "INACTIVE"},
    {"flag_id": "00000000-0000-0000-0000-000000000000", "flag_key": "unknown", "success": false, "error": "Feature flag not found", "old_status": null, "new_status": null}
  ],
  "audit_log_ids": ["9d0a668c-…", "b0322b63-…"]
}
```

A flag the caller may not change (an ANALYST or VIEWER changes none) fails with
`"Not enough permissions to change this feature flag"` and is left as it was.

---

## Audit Log API

### GET /api/v1/audit-logs/

Query the audit log with filters. The URL ends with a slash; without it the API answers
`307`.

**Query Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `entity_type` | string | `feature_flag`, `experiment`, `user`, `role`, `permission`, `safety_config`, `rollout_schedule`, `api_key`, `holdout`, `mutual_exclusion_group`, `segment` |
| `entity_id` | UUID | Filter by specific entity ID |
| `action_type` | string | Filter by action (see Action Types below) |
| `user_id` | UUID | Filter by the user who performed the action |
| `from_date` | datetime | Entries at or after this time (ISO 8601) |
| `to_date` | datetime | Entries at or before this time (ISO 8601) |
| `page` | int | Page number, from 1 (default: 1) |
| `limit` | int | Page size (default: 50, max: 1000) |

An unknown `entity_type` or `action_type` answers `400`. This lists the two entries the
bulk toggle wrote:

```{.bash exec}
curl -s "localhost:8000/api/v1/audit-logs/?entity_type=feature_flag&action_type=toggle_disable" \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{total, entries: [.items[] | {entity_name, old_value, new_value, reason}]}'
```
<!-- expect: "total": 2 -->
<!-- expect: "old_value": "ACTIVE" -->
<!-- expect: "new_value": "INACTIVE" -->
<!-- expect: "reason": "Checkout incident" -->

It prints `"total": 2`, and for each flag its old and new status and the reason given.
One entry in full:

```json
{
  "user_email": "admin@demo.com",
  "action_type": "toggle_disable",
  "entity_type": "feature_flag",
  "entity_id": "851811d2-58aa-464b-9ebc-8cc3650f19ae",
  "entity_name": "Dark mode",
  "old_value": "ACTIVE",
  "new_value": "INACTIVE",
  "reason": "Checkout incident",
  "id": "9d0a668c-8ef3-4046-8fd5-49053afddb0a",
  "user_id": "2658dd18-4167-4803-9aff-a3ffb1f603ce",
  "timestamp": "2026-09-26T22:52:28.781081Z",
  "action_description": "disabled",
  "created_at": "2026-09-26T22:52:28.781394",
  "updated_at": "2026-09-26T22:52:28.781395"
}
```

The list also carries `page`, `limit` and `total_pages`.

---

### GET /api/v1/audit-logs/entity/{entity_type}/{entity_id}

Every entry recorded against one entity, most recent first. There is no fetch-one-by-id
route; query by entity, by actor (`/audit-logs/user/{user_id}`), or filter the collection
(`/audit-logs/`):

```{.bash exec}
curl -s localhost:8000/api/v1/audit-logs/entity/feature_flag/$DARK_ID \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[].action_type'
```
<!-- expect: toggle_disable -->
<!-- expect: feature_flag_create -->

It prints the two entries for the dark-mode flag, newest first: `toggle_disable`, then
`feature_flag_create`.

---

### GET /api/v1/audit-logs/stats

Returns counts: the total, per action type, per entity type, and per user. `from_date` and
`to_date` narrow it:

```{.bash exec}
curl -s localhost:8000/api/v1/audit-logs/stats \
  -H "Authorization: Bearer $TOKEN" | jq '.action_counts.toggle_disable'
```
<!-- expect: 2 -->

It prints `2`. The whole response has the shape
`{"total_logs", "action_counts", "entity_counts", "most_active_users", "date_range"}`.

---

### GET /api/v1/audit-logs/export

Downloads every entry the filters match as one file, newest first. This route is beta: its
shape may still change (see [API stability](stability.md)). The URL has no trailing slash;
with one the API answers `307`.

It takes the list's filters (`user_id`, `entity_type`, `entity_id`, `action_type`,
`from_date`, `to_date`), with the same `400` refusals, plus `format`:

| `format` | File |
|----------|------|
| `json` (default) | `application/json`: an array of the list's entry objects, as shown above |
| `csv` | `text/csv; charset=utf-8`: a header row, then one row per entry |

The CSV columns are `id`, `timestamp`, `user_id`, `user_email`, `action_type`,
`action_description`, `entity_type`, `entity_id`, `entity_name`, `old_value`, `new_value`
and `reason`, in that order. Each cell is the same field's value in the JSON (empty for
`null`), quoted as RFC 4180 describes. Export cells are made safe for spreadsheets: a cell
that starts with `=`, `+`, `-`, `@`, a tab or a carriage return starts with an apostrophe
in the CSV. The JSON holds the stored text unchanged.

The file is named `audit-log-<UTC time>.<format>` in `Content-Disposition`, for example
`audit-log-20261004T090507Z.csv`. Entries written after the request arrived are not in it.

Who sees what is the list's rule: ADMIN and ANALYST export every entry; DEVELOPER and
VIEWER export only their own, whatever `user_id` they send. Exporting is not recorded in
the audit log.

**Check the count.** `X-Total-Count` is the number of entries the file holds. A download
that stops part-way can still look complete, a CSV especially, so compare the entries you
received with it before you use the file. This exports the two bulk-toggle entries as
JSON and compares:

```{.bash exec}
curl -s -D export-headers.txt -o audit-log.json \
  "localhost:8000/api/v1/audit-logs/export?entity_type=feature_flag&action_type=toggle_disable" \
  -H "Authorization: Bearer $TOKEN"
EXPECTED=$(grep -i '^x-total-count:' export-headers.txt | tr -d '\r' | awk '{print $2}')
RECEIVED=$(jq length audit-log.json)
printf 'expected %s, received %s\n' "$EXPECTED" "$RECEIVED"
```
<!-- expect: expected 2, received 2 -->

It prints `expected 2, received 2`. The same entries as CSV start with the header row:

```{.bash exec}
curl -s "localhost:8000/api/v1/audit-logs/export?format=csv&entity_type=feature_flag&action_type=toggle_disable" \
  -H "Authorization: Bearer $TOKEN" | sed -n 1p
```
<!-- expect: id,timestamp,user_id,user_email,action_type,action_description,entity_type,entity_id,entity_name,old_value,new_value,reason -->

To count the entries in a CSV, use a CSV parser: a quoted cell can hold a line break, so
counting lines can be wrong.

At most 50,000 entries fit in one export. When more match, the API answers `422`, says how
many match, and sends no file; narrow `from_date` and `to_date`, or filter by
`action_type` or `entity_type`. It never sends part of the entries. The export has its own
rate limit, 10 requests a minute per client address, apart from the
[data export](data-export.md) routes' limit.

The dashboard's Audit Log page (`/admin/audit`) is in the top navigation for every signed-in
role. It shows what `GET /api/v1/audit-logs/` returns for that user: every entry for ADMIN,
ANALYST and superusers, and their own entries for DEVELOPER and VIEWER, who see a note saying
so. Its **Download CSV** and **Download JSON** buttons export the same entries, with the
page's current filters. The page makes the same comparison, and when the counts differ it
says so and saves nothing.

---

## Action Types

Every action below is written by the route or the part of the platform named. A route
writes it after the change is saved, with the signed-in user as the actor; the
[failure policy](#when-an-entry-can-be-missing) says what happens when an entry cannot be
written. An update records only the fields that changed, as
`old_value` and `new_value` JSON; a create records the new entity's identifying fields in
`new_value`, and a delete the old ones in `old_value`. Descriptions, hypotheses, targeting
rules, segment rules, variants and metrics are never stored: when they change, their names
are listed in `new_value.changed_fields`. No entry holds a password, a token, any part of
an API key, or a request body.

| Action Type | Entity | Written when |
|-------------|--------|--------------|
| `feature_flag_create` | `feature_flag` | `POST /feature-flags/` |
| `feature_flag_update` | `feature_flag` | `PUT /feature-flags/{id}`; `/unarchive`; bulk `archive`; every change to one of the flag's rollout schedules or stages (`reason` is `rollout schedule <change>`, such as `rollout schedule activated`) |
| `feature_flag_delete` | `feature_flag` | `DELETE /feature-flags/{id}` |
| `feature_flag_activate`, `feature_flag_deactivate` | `feature_flag` | `/activate`, `/deactivate`, when the status changes |
| `toggle_enable`, `toggle_disable` | `feature_flag` | A flag is turned on or off (`/toggle`, `/enable`, `/disable`, or bulk `enable`/`disable`); `old_value` and `new_value` are the status |
| `experiment_create` | `experiment` | `POST /experiments/`, `/clone` (`reason` names the source), wizard submit |
| `experiment_update` | `experiment` | `PUT /experiments/{id}`, `/schedule`, `/metadata`, `/archive`, `PUT /bandit/{id}/weights` (`reason` names the change, except for `PUT`) |
| `experiment_delete` | `experiment` | `DELETE /experiments/{id}` |
| `experiment_start`, `experiment_pause`, `experiment_complete` | `experiment` | `/start` (also a resume), `/pause`, `/complete` |
| `api_key_create`, `api_key_revoke` | `api_key` | `POST /api-keys`, `DELETE /api-keys/{id}` |
| `holdout_create` | `holdout` | `POST /holdout`. With `is_active: true` it turns the active holdout off, and that holdout gets its own `holdout_deactivate` |
| `holdout_update`, `holdout_activate`, `holdout_deactivate` | `holdout` | `PUT /holdout/{id}`, one entry per kind of change. Activating a holdout turns any other active one off, and that holdout gets its own `holdout_deactivate`. An activate or deactivate entry also carries the `activated_at` or `deactivated_at` it set |
| `mutual_exclusion_group_create`, `mutual_exclusion_group_update`, `mutual_exclusion_group_archive` | `mutual_exclusion_group` | Create, `PUT`, and `DELETE` (which archives). Adding or removing an experiment is an update with `{"experiment_id": ...}` and the `reason` `experiment added` or `experiment removed` |
| `segment_create`, `segment_update`, `segment_archive` | `segment` | `POST /segments`, `PUT /segments/{id}`, `DELETE /segments/{id}` (which archives). Adding or removing members of an id list (`POST /segments/{id}/members`, `/members/remove`) is an update with the `reason` `members added` or `members removed` and `new_value` `{"added", "already_members", "member_count"}` or `{"removed", "not_members", "member_count"}`: the counts, never the user IDs |
| `user_create`, `user_delete` | `user` | `POST /users/`; `DELETE /users/{id}` and `DELETE /admin/users/{id}`. Deleting your own account is recorded with no `user_id` |
| `role_assign` | `user` | A superuser changes a user's role or superuser flag (`PATCH /admin/users/{id}`, `PUT /admin/users/{id}`, `PUT /users/{id}`); `old_value` and `new_value` are `{"role", "is_superuser"}` |
| `user_activate`, `user_deactivate` | `user` | The same routes change a user's active status |
| `user_login` | `user` | Signing in with a password (`POST /auth/login`, or `POST /auth/token` with `AUTH_PROVIDER=local`); `new_value` is `{"provider": "local"}` |
| `user_login` | `user` | Signing in through Cognito (`POST /auth/token` with the Cognito provider) as a user who already has an account; `new_value` is `{"provider": "cognito"}`. A user's first Cognito sign-in writes no `user_login`: their account is created by the first request they make with the token, which writes `user_create`. A sign-in through the Cognito Hosted UI does not pass through this API and writes nothing |
| `safety_rollback` | `feature_flag` | A safety rollback, manual (`POST /safety/feature-flags/{id}/rollback`, by the calling user) or by the safety monitor. `new_value` is `{"trigger_type", "previous_percentage", "new_percentage", "deactivated", "paused_schedules"}`; `reason` is the rollback's reason. It appears in the flag's history |
| `experiment_start`, `experiment_complete` | `experiment` | The experiment scheduler starts an experiment at its start date or scheduled resume, or completes it at its end date (also when `POST /experiments/schedules/process` runs it). `reason` is `scheduled start`, `scheduled resume` or `scheduled end` |
| `feature_flag_update` | `feature_flag` | The rollout scheduler starts a rollout stage and sets the flag's rollout percentage. `reason` is `rollout schedule stage started` |
| `user_create` | `user` | A user's first Cognito sign-in creates their account. The actor is the new user; `reason` is `first sign-in` |
| `role_assign` | `user` | Cognito group sync (`SYNC_ROLES_ON_LOGIN`) changes a user's role or superuser flag when they sign in; `old_value` and `new_value` are `{"role", "is_superuser"}` |

With the modules installed (the full profile), these are written too:

| Action Type | Entity | Written when |
|-------------|--------|--------------|
| `role_assign` | `user` | `POST /rbac/roles/assign` gives a user a custom role they did not hold; `new_value` is `{"custom_role"}`, `reason` is `custom role assigned` |
| `role_unassign` | `user` | `POST /rbac/roles/revoke` takes away a custom role the user held; `old_value` is `{"custom_role"}`, `reason` is `custom role revoked` |
| `role_assign` | `user` | `PUT /workspaces/{id}/members/{user_id}` changes a member's workspace role; `old_value` and `new_value` are `{"workspace_id", "workspace_role"}`, `reason` is `workspace role changed` |
| `user_login` | `user` | An SSO sign-in: the SAML ACS (`POST /auth/sso/saml/{config_id}/acs`), the dashboard's `POST /auth/sso/exchange`, or the OIDC callback when it answers with a token; `new_value` is `{"provider": "sso"}` |
| `user_create` | `user` | A user's first SSO sign-in creates their account. The actor is the new user; `reason` is `first SSO sign-in` |
| `role_assign` | `user` | An SSO sign-in changes an existing user's role from a mapped group; `old_value` and `new_value` are `{"role", "is_superuser"}` (SSO never changes the superuser flag, so it is the same in both), `reason` is `SSO groups changed` |

A request that changes nothing (a custom role already held, a workspace role or SSO role
already equal) writes no entry. Assigning a direct permission, defining a custom role,
adding or removing a workspace member, invites, SSO configuration and the other module
routes write none.

User changes record the superuser flag. A change to a user's role, superuser flag or
active status is saved together with its entry: if the entry cannot be written, the
change is refused with a `500` and nothing is saved, and sending the same request again
once the problem is fixed writes it once.

`ActionType` also defines `user_update`, `user_logout`, `permission_grant`,
`permission_revoke` and `safety_config_update`. You can filter on them, but nothing in this
release writes them (`role_unassign` is written only with the modules installed).
Granting and revoking a direct permission are not recorded because no permission check
reads direct grants yet
([#891](https://github.com/Controlvar/controlvar/issues/891)).

### Changes the platform makes on its own

An entry the platform makes on its own has no `user_id`, and its `user_email` is one of
these reserved values:

| `user_email` | Written by |
|--------------|------------|
| `system:experiment-scheduler` | The experiment scheduler: scheduled starts, resumes and ends |
| `system:rollout-scheduler` | The rollout scheduler: each rollout stage it starts |
| `system:safety-monitor` | The safety monitor: each automatic rollback |
| `system:cognito-sync` | Cognito group sync: each role or superuser flag change |
| `system:sso-sync` | SSO sign-in (full profile): each role change from a mapped group |

An entry is automatic only when `user_id` is empty **and** `user_email` is one of these
values. Deleting a user also empties `user_id` on their entries, but keeps their own
`user_email`, so those entries are never automatic. A manual rollback is recorded with the
user who asked for it.

### What is never recorded

[Action Types](#action-types) is the complete list: a change not listed there writes no
entry. No entry ever holds:

- reading anything: lists, reports, the audit log itself, or exporting it;
- failed sign-ins (there is no account to attach them to) or sign-outs;
- passwords or their hashes, tokens, or any part of an API key or its hash;
- request bodies, IP addresses or user agents;
- the contents of descriptions, hypotheses, targeting rules, segment rules, variants and
  metrics (only their names, under `changed_fields`);
- error text;
- changes made directly in the database rather than through the platform.

### When an entry can be missing

Whether a change is kept when its entry cannot be written depends on where it is made:

| Where | If the entry cannot be written |
|-------|--------------------------------|
| A route (all the route actions above) | The change is kept and the response is `2xx`; there is no entry |
| A superuser's change to a user's role, superuser flag or active status | Nothing is saved and the response is `500`; resend it once the problem is fixed |
| A safety rollback, manual or by the safety monitor | The rollback is kept; there is no entry |
| The experiment scheduler | The start or end is kept; there is no entry, and **it is not written later** |
| The rollout scheduler | The stage does not start and the rollout percentage does not change; the next run tries both again |
| A first Cognito or SSO sign-in | The account is created; there is no entry |
| Cognito group sync | The role change is kept and the request succeeds; there is no entry, and **it is not written later**: the next request finds the role already changed and writes nothing |
| SSO sign-in changing a role | The role change is kept and the sign-in succeeds; there is no entry, and **it is not written later**: the next sign-in finds the role already changed and writes nothing |

Each entry that cannot be written logs one ERROR line from the API:

```text
Failed to create audit log for <action> on <entity type> <entity id> (<exception type>)
```

In a deployed stack that line is the only signal that an entry is missing. The API also
counts these failures in a Prometheus counter, `audit_write_failures_total`, but do not
rely on it there: the AWS deployment does not collect it.

### Keeping entries, and deleted users

Nothing deletes audit entries: they are kept until someone removes them from the
database. Deleting a user empties `user_id` on their entries and keeps `user_email`, so a
deleted user's email address stays in the log, readable by everyone who reads every entry
(see [Permissions](#permissions)).

---

## Recent Entries as a Stream (SSE)

`GET /api/v1/audit-logs/stream` sends the most recent entries as Server-Sent Events: one
`data:` line per entry, newest first, then a final `{"event": "end"}`, and closes. It does
not stay open for new entries. `limit` (default 50, max 100) and `entity_type` narrow it:

```{.bash exec}
curl -s -N "localhost:8000/api/v1/audit-logs/stream?limit=2&entity_type=feature_flag" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: text/event-stream"
```
<!-- expect: data: {"id": -->
<!-- expect: "action_type": "toggle_disable" -->
<!-- expect: data: {"event": "end"} -->

It prints the two `toggle_disable` entries, then the end event:

```text
data: {"id": "b0322b63-…", "timestamp": "2026-09-26T22:52:28.784985+00:00", "user_email": "admin@demo.com", "action_type": "toggle_disable", "entity_type": "feature_flag", "entity_name": "New checkout", "old_value": "ACTIVE", "new_value": "INACTIVE"}

data: {"id": "9d0a668c-…", "timestamp": "2026-09-26T22:52:28.781081+00:00", "user_email": "admin@demo.com", "action_type": "toggle_disable", "entity_type": "feature_flag", "entity_name": "Dark mode", "old_value": "ACTIVE", "new_value": "INACTIVE"}

data: {"event": "end"}
```

---

## Feature Flag Change History

### GET /api/v1/feature-flags/{flag_id}/history

The audit entries for one flag, most recent first, with `limit` (default 50, max 200) and
`offset`:

```{.bash exec}
curl -s localhost:8000/api/v1/feature-flags/$CHECKOUT_ID/history \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{flag_key, total_changes, latest: .history[0] | {action_type, old_value, new_value, user_email}}'
```
<!-- expect: "flag_key": "new-checkout" -->
<!-- expect: "total_changes": 2 -->
<!-- expect: "action_type": "toggle_disable" -->

It prints `"total_changes": 2` (the flag's creation and the bulk toggle) and the latest
entry, the bulk toggle's: `toggle_disable`, from `ACTIVE` to `INACTIVE`, by
`admin@demo.com`.

---

## Permissions

ADMIN and ANALYST (and superusers) read every audit entry. DEVELOPER and
VIEWER read only the entries they made themselves.

| Route | ADMIN, ANALYST | DEVELOPER, VIEWER |
|-------|----------------|-------------------|
| `GET /audit-logs/` | Every entry; `user_id` filters by any user | Own entries only; a `user_id` naming anyone else is replaced by their own |
| `GET /audit-logs/export` | Every entry; `user_id` filters by any user | Own entries only; a `user_id` naming anyone else is replaced by their own |
| `GET /audit-logs/user/{user_id}` | Any user | Their own id only; any other id is 403 |
| `GET /audit-logs/entity/{entity_type}/{entity_id}` | Every entry | 403 |
| `GET /audit-logs/stats` | Every entry | 403 |
| `GET /audit-logs/stream` | Every entry | Own entries only |
| `GET /feature-flags/{flag_id}/history` | Every entry | Own entries only; `total_changes` counts only those |

`POST /feature-flags/bulk-toggle` changes flags, so it follows the flag rule:
ADMIN and DEVELOPER may use it, ANALYST and VIEWER may not.

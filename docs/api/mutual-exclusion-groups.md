# Mutual Exclusion Groups & Global Holdout

Mutual exclusion groups prevent users from being enrolled in multiple conflicting experiments simultaneously. A global holdout keeps a percentage of new users out of every experiment, so you can compare them with everyone else (feature flags ignore it).

The commands on this page run as written against the stack from the
[Quick Start](../getting-started/quick-start.md), in one terminal, top to bottom. Each one uses
the shell variables set by the ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

---

## Mutual Exclusion Groups

### How It Works

Traffic is partitioned using consistent hashing on user ID. Each group has a `traffic_allocation` (0.0–1.0) that defines the fraction of users eligible for experiments within the group. Users are assigned to at most one experiment within the group.

A user who is not eligible is still answered by `POST /api/v1/tracking/assign`, with status
`200`, the control variant, `"assigned": false` and `"reason": "mutual_exclusion"`. Nothing
is recorded for them.

**Enrolment is sticky across the group.** A user enrolled in one experiment of a group is
never enrolled in another experiment of that group, even as experiments are activated,
paused or resumed. The hash divides new users among the experiments that are active at the
time of the request; a user who already has an assignment in another experiment of the
group that is active, paused or still a draft is answered as not eligible, with the same
`"reason": "mutual_exclusion"`. So an experiment activated later in a group fills more
slowly than one that started with it: only users not yet enrolled elsewhere in the group can
join it. Completing an experiment, or removing it from the group, releases its users, and
they can then be enrolled in another experiment of the group. Assignments that already
exist are always kept, so a user who was enrolled in two experiments of a group before this
rule applied stays in both.

The check is made per request. If the group's set of active experiments changes while two
requests for the same new user are in flight, both can succeed.

---

## Mutual Exclusion Group API

### POST /api/v1/mutual-exclusion-groups

Create a new mutual exclusion group. Requires DEVELOPER or ADMIN role. This saves the new
group's id in `$GROUP_ID`:

```{.bash exec}
GROUP=$(curl -s -X POST localhost:8000/api/v1/mutual-exclusion-groups \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Checkout Page Experiments",
    "description": "All experiments that modify the checkout flow",
    "traffic_allocation": 0.5
  }')
GROUP_ID=$(jq -r .id <<<"$GROUP")

jq '{name, traffic_allocation, status, experiments}' <<<"$GROUP"
```
<!-- expect: "name": "Checkout Page Experiments" -->
<!-- expect: "traffic_allocation": 0.5 -->
<!-- expect: "status": "active" -->

```json
{
  "name": "Checkout Page Experiments",
  "traffic_allocation": 0.5,
  "status": "active",
  "experiments": []
}
```

The response also carries the group's `id`, `description`, `owner_id`, `created_at` and
`updated_at`.

---

### GET /api/v1/mutual-exclusion-groups

List all mutual exclusion groups.

**Query Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by status: `active`, `archived` |
| `skip` | int | Pagination offset (default: 0) |
| `limit` | int | Page size (default: 100, max: 1000) |

```{.bash exec}
curl -s localhost:8000/api/v1/mutual-exclusion-groups \
  -H "Authorization: Bearer $TOKEN" | jq -r '.total, .items[].name'
```
<!-- expect: 1 -->
<!-- expect: Checkout Page Experiments -->

The response is `{"items": [...], "total": N}`. This prints the total, `1`, and each group's
name: `Checkout Page Experiments`.

---

### POST /api/v1/mutual-exclusion-groups/{group_id}/experiments

Add an experiment to a mutual exclusion group. This adds the demo data's
`checkout_button_color` experiment, looking up its id by its key:

```{.bash exec}
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

curl -s -X POST localhost:8000/api/v1/mutual-exclusion-groups/$GROUP_ID/experiments \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"experiment_id\": \"$EXP_ID\"}" | jq .status
```
<!-- expect: "ok" -->

It prints `"ok"`. The response also names the `experiment_id` and `group_id`.

---

### GET /api/v1/mutual-exclusion-groups/{group_id}

Retrieve a specific group, including its member experiments:

```{.bash exec}
curl -s localhost:8000/api/v1/mutual-exclusion-groups/$GROUP_ID \
  -H "Authorization: Bearer $TOKEN" | jq -c '.experiments'
```
<!-- expect: "name":"Checkout Button Color" -->

It prints the member experiments' `id`, `name` and `status`:

```json
[{"id":"5673f9cd-8bbf-40c5-9bba-6613f87a78d4","name":"Checkout Button Color","status":"active"}]
```

---

### PUT /api/v1/mutual-exclusion-groups/{group_id}

Update group name, description, or traffic allocation. Requires DEVELOPER or ADMIN role.

Changing `status` (`"archived"` or `"active"`) requires ADMIN role, as archiving with `DELETE`
does; a DEVELOPER gets `403`. Sending the group's current `status` back, as a form that sends every
field does, is accepted from a DEVELOPER.

---

### DELETE /api/v1/mutual-exclusion-groups/{group_id}

Archive a group (soft delete). Requires ADMIN role. An archived group no longer
constrains its members: from then on a new user can be enrolled in more than one of them.

---

### DELETE /api/v1/mutual-exclusion-groups/{group_id}/experiments/{experiment_id}

Remove an experiment from the group. Its users are released: from their next request they
can be enrolled in another experiment of the group. Their assignment in the removed
experiment is kept.

---

## Global Holdout

A global holdout keeps a percentage of new users out of **all** experiments platform-wide. New users in the holdout are not assigned to any experiment: `POST /api/v1/tracking/assign` answers with the control variant. Users who already had an assignment when the holdout was activated keep it.

Only one holdout can be active at a time, and the database enforces it. Activating a holdout, with `PUT /api/v1/holdout/{holdout_id}` or by creating it with `"is_active": true`, deactivates the active one. If two activations happen at the same moment, the second answers `409` and can be retried.

Each holdout buckets users with its own salt, so the users one holdout keeps out are not the same users the next one keeps out. While a holdout is active, the API records each new user `POST /api/v1/tracking/assign` answers for, and whether the holdout kept them out; that record starts when the holdout is activated. A holdout that was already active when you upgraded to the release that added this record keeps holding the same users out, but nothing was recorded for it: deactivate it and create a new one to start recording.

### Size

The API accepts 1–20%.

The percentage cannot change once the holdout has been active, because that would move users between the holdout and everyone else. A holdout that has been deactivated cannot be activated again. In both cases, create a new holdout.

### GET /api/v1/holdout

Returns the currently active global holdout, or `null` if none is active:

```{.bash exec}
curl -s localhost:8000/api/v1/holdout \
  -H "Authorization: Bearer $TOKEN"
```
<!-- expect: null -->

It prints `null`: the demo data has no holdout.

### GET /api/v1/holdout/all

List all holdout configurations (active and historical). Requires ADMIN role.

### POST /api/v1/holdout

Create a global holdout. Requires ADMIN role. With `"is_active": true` it becomes the active holdout and the one that was active is deactivated.

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/holdout \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Quarterly Holdout",
    "holdout_percentage": 5.0,
    "is_active": true
  }' | jq '{name, holdout_percentage, is_active}'
```
<!-- expect: "name": "Quarterly Holdout" -->
<!-- expect: "holdout_percentage": 5 -->
<!-- expect: "is_active": true -->

```json
{
  "name": "Quarterly Holdout",
  "holdout_percentage": 5,
  "is_active": true
}
```

The response also carries the holdout's `id`, `description`, `owner_id`, `activated_at`,
`deactivated_at`, `created_at` and `updated_at`.

### PUT /api/v1/holdout/{holdout_id}

Update holdout configuration. Requires ADMIN role. Sending `"is_active": true` deactivates the active holdout and activates this one; sending it to the holdout that is already active changes nothing. `"is_active": false` deactivates it, and it has then ended.

Refused with `422`:

- a different `holdout_percentage` once the holdout has been active: "holdout_percentage cannot change once a holdout has been active: it would move users between the holdout and the rest and mix the two groups. Create a new holdout instead." Sending the same percentage is accepted, so a form that sends every field keeps working;
- `"is_active": true` on a holdout that has ended: "A holdout that has ended cannot restart. Create a new holdout."

### GET /api/v1/holdout/check/{user_id}

Check whether a specific user is in the active global holdout:

```{.bash exec}
curl -s localhost:8000/api/v1/holdout/check/user-123 \
  -H "Authorization: Bearer $TOKEN"
```
<!-- expect: "user_id":"user-123" -->
<!-- expect: "holdout_percentage":5 -->

```json
{"user_id":"user-123","is_in_holdout":false,"holdout_percentage":5,"bucket":44}
```

Each user has a `bucket` from 0 to 99, a hash of their id with the active holdout's salt, so
it stays the same for as long as that holdout is active and is different under the next
holdout. A user is in the holdout when their bucket is below `holdout_percentage`: here
`user-123`'s bucket is 44, so a 5% holdout leaves them out. Your bucket will differ, because
your holdout has its own salt. A new user in the holdout is answered by
`POST /api/v1/tracking/assign` with the control variant, `"assigned": false` and
`"reason": "holdout"`.

### GET /api/v1/holdout/{holdout_id}/results (beta)

**Beta:** this measures the combined effect of running experiments' treatment arms on users first seen while the holdout was active. Feature flags ignore the holdout: once a winner ships behind a flag, users in the holdout get it too, so shipped features are not part of this comparison. `always_valid_ci_lower` and `always_valid_ci_upper` stay valid however often you check. See [#445](https://github.com/Controlvar/controlvar/issues/445).

For one event name (`metric`, required), it compares the share of users who sent at least one matching event in two groups: `in_holdout`, the users the holdout kept out, and `not_in_holdout`, everyone else first seen while it was active. A user is first seen the first time `POST /api/v1/tracking/assign` answers for them while the holdout is active. Every user recorded is in their group, including users the targeting rules or a mutual exclusion group turned away. A user who already had an experiment assignment before they were first seen is in neither group; `excluded_users` counts them, in total and per group.

An event counts when its own time and the time the server received it are both between the user's first-seen time and the end of the holdout. Events received after a holdout ends never change its results. `window_end` is when the holdout was deactivated, or the time of your request while it is active. Events with or without an experiment or flag key count alike, so send the metric event for every user, held out or not: an integration that sends it only for users with `"assigned": true` leaves the holdout group without events and makes the experiments look better than they are. The SDKs' key-less `track()` sends it for held-out users too.

`difference` is "not in holdout" minus "in holdout": a positive value means the running experiments raised the metric. `ci_lower` and `ci_upper` are a 95% interval for it (Agresti-Caffo) to read once; `always_valid_ci_lower` and `always_valid_ci_upper` are a confidence sequence (mSPRT). `p_value` and `is_significant` (`p_value < 0.05`) are Fisher's exact test, as on `/results`; the interval is computed separately, so near the boundary it can include 0 while `is_significant` is true, or the reverse. Any role that can read experiment results can read this route.

`holdout_users_with_assignments` counts users in the holdout group who were assigned to an experiment while it was active. It is 0 unless a release that does not record holdouts served traffic, after a rollback; those users stay in their group, the difference is then smaller than the real one, and `analysis_notice` says so.

`difference` is null, and `unavailable_reason` and `message` say why, in these cases:

| `unavailable_reason` | When |
|---|---|
| `activated_before_measurement` | The holdout was active before the release that records who a holdout covers. Create a new holdout. |
| `not_activated` | The holdout has not been activated since then. |
| `too_few_users` | A group has fewer than 100 users. The groups and their counts are filled in. |
| `no_events` | Neither group sent an event with this name in the window: check the event name. |

On a fresh holdout neither group has 100 users yet:

```{.bash exec}
HOLDOUT_ID=$(curl -s localhost:8000/api/v1/holdout -H "Authorization: Bearer $TOKEN" | jq -r .id)
curl -s "localhost:8000/api/v1/holdout/$HOLDOUT_ID/results?metric=purchase" \
  -H "Authorization: Bearer $TOKEN" | jq '{unavailable_reason, groups: [.groups[] | {group, users}]}'
```
<!-- expect: "unavailable_reason": "too_few_users" -->

```json
{
  "unavailable_reason": "too_few_users",
  "groups": [
    {
      "group": "in_holdout",
      "users": 0
    },
    {
      "group": "not_in_holdout",
      "users": 0
    }
  ]
}
```

---

## Permissions

| Action | Minimum Role |
|--------|-------------|
| List groups / view holdout | DEVELOPER |
| Read holdout results | VIEWER |
| Create / update groups (name, description, traffic allocation) | DEVELOPER |
| Add/remove experiments from groups | DEVELOPER |
| Archive or unarchive groups (`DELETE`, or `status` on `PUT`) / list, create and update holdouts | ADMIN |

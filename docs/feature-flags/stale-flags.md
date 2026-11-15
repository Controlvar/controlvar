# Stale Flags

A flag that has served everyone the same answer for weeks, or that nothing has
asked about, is usually ready to come out of your code. The flag list marks such
a flag as stale and says why. It is a suggestion: listing changes no flag, and
nothing is archived or turned off for you. The definition is beta: its
conditions and threshold may change; the fields will not.

The examples run against the [Quick Start](../getting-started/quick-start.md)
stack, in one terminal, in order: later steps use the `$TOKEN`, `$KEY` and
`$FLAG_ID` an earlier one sets.

## When a flag is stale

N is 30 days by default; `STALE_FLAG_DAYS` sets it, from 1 to 3650, for the
whole installation; a change needs a restart.

A flag is stale when it was created at least N days ago, is not archived, is not
tagged `permanent`, has no active rollout schedule, and at least one of these
reasons applies:

| Reason | When |
|---|---|
| `not_evaluated` | No evaluation of the flag was recorded in the last N days, while evaluations of other flags were. |
| `on_for_everyone` | The flag is on at 100% with no targeting rules, and unchanged for N days. |
| `off_for_everyone` | The flag is off, or on at 0% with no targeting rules, and unchanged for N days. |

`stale_reasons` lists every reason that applies, in that order. A flag created
or last changed exactly N days ago is old enough, and an evaluation exactly N
days ago still counts.

Targeting rules that select nobody, such as a rule group with no conditions,
are no rules; rules that cannot be read count as rules. A flag at any other
percentage serves different users different answers, so neither of the last two
reasons applies to it.

An archived flag is never stale, and neither is a flag whose rollout schedule is
active, because its next stage will change it. A schedule that is paused, a
draft, completed or cancelled does not keep a flag off the list.

## What counts as an evaluation

Every evaluation the platform records counts:

- each answer the server gives to `GET` or `POST /api/v1/feature-flags/evaluate/{flag_key}`, including the MCP server's `evaluate_feature_flag`;
- `GET /api/v1/feature-flags/user/{user_id}`, which answers every active flag for one user, and so counts as an evaluation of every active flag; `getAllFlags` (JavaScript, React Native, the edge SDK) and `get_all_flags` (Python) call it;
- the counts the JavaScript and Python SDKs report from local evaluation, every 60 seconds and on `close()`; in local evaluation, `getAllFlags` and `get_all_flags` count every flag they answer.

So one caller that fetches every flag keeps every active flag from being marked
`not_evaluated`. The edge SDK's Vercel middleware fetches every flag on each
request unless `flagKeys` is set: set `flagKeys` to the flags your requests
need.

Not evaluated means no evaluation was recorded, not that no code uses the flag.
A flag read only by a job that runs less often than every N days, a quarterly
report for example, is marked `not_evaluated` between runs.

When the installation recorded no flag evaluation at all in the last N days,
`not_evaluated` is not reported for any flag, and the list says
`"evaluations_seen": false`. A new installation, or one whose applications do
not reach it, sees this.

## What unchanged means

Unchanged means no setting of the flag changed: an edit, including a rename, or
a status change, a rollout step or a safety rollback restarts the count.

The count starts from the flag's `updated_at`. The request for this feature
(#1808) noted that `updated_at` is not the time the flag's answer last changed,
and the platform does not record that time. It is used anyway: every change to a
flag sets it, so a flag marked unchanged has not changed at all in N days. The
cost is that a change that leaves the answer as it was, a rename for example,
also restarts the count.

## Keeping a flag

A flag tagged `permanent` is never stale. The tag must be exactly permanent;
case and surrounding spaces are ignored.

Tags are set through the API, and a `PUT` replaces the whole list, so send the
flag's tags with `permanent` added. The first block signs in and creates a flag
tagged `checkout` for the example; the second adds `permanent` and prints the
tags:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

FLAG_ID=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"key":"stale-flags-example","name":"Stale flags example","tags":["checkout"]}' \
  | jq -r .id)

curl -s "localhost:8000/api/v1/feature-flags/$FLAG_ID" \
  -H "Authorization: Bearer $TOKEN" | jq -c .tags
```
<!-- expect: ["checkout"] -->

```{.bash exec}
TAGS=$(curl -s "localhost:8000/api/v1/feature-flags/$FLAG_ID" \
  -H "Authorization: Bearer $TOKEN" | jq -c '(.tags // []) + ["permanent"] | unique')

curl -s -X PUT "localhost:8000/api/v1/feature-flags/$FLAG_ID" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"tags\": $TAGS}" | jq -c .tags
```
<!-- expect: ["checkout","permanent"] -->

## Listing the stale flags

`GET /api/v1/feature-flags/?stale=true` lists only the stale flags, and
`stale=false` the others; `status` and `search` narrow either. With the filter,
`total` counts the flags that match it, and `skip` and `limit` page them in the
list's order. Every item, with the filter or without it, carries `stale` and
`stale_reasons`, and the answer carries `stale_days` and `evaluations_seen`.

A sign-in can read it, and so can an API key that carries the `mcp:read` scope.
This creates such a key and lists the stale flags with it:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"stale-flags","scopes":["mcp:read"]}' | jq -r .key)

curl -s "localhost:8000/api/v1/feature-flags/?stale=true" -H "X-API-Key: $KEY" \
  | jq -c '{stale_days, total, keys: [.items[].key]}'
```
<!-- expect: "stale_days":30 -->

Listing changes no flag. The MCP server's `list_feature_flags` takes the same
filter: see [The MCP Server](../mcp-server.md#the-tools).

## Next steps

Remove the flag from your code, then archive it with **Archive** on the flag's
page, or with `POST /api/v1/feature-flags/bulk-toggle` and `"action": "archive"`.
An archived flag answers every evaluation
as off, and `POST /api/v1/feature-flags/{flag_id}/unarchive` makes it inactive
again. See [Archived flags](create.md#archived-flags).

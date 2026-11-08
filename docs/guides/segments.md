# Segments

A segment is a named group of users. It is one of two kinds, chosen when you create it and
never changed:

- **Rules** (`kind: rules`, the default): the users whose attributes match conditions you
  set, such as plan is `enterprise`. The rule format and its limits are in the
  [API reference](../api/endpoints.md#audience-segments).
- **ID list** (`kind: id_list`): the users whose `user_id` you upload, for example a customer
  export. This page is about these.

The member routes are beta (`x-stability: beta`): their shape may still change. The full
contract and every refusal are in the
[API reference](../api/endpoints.md#segments-made-from-a-list-of-user-ids-beta).

The examples run against the [Quick Start](../getting-started/quick-start.md) stack and its
demo data.

## Sign in

Changing a segment takes the experiment permissions, which the ADMIN and DEVELOPER roles have;
in the full profile a custom role or direct grant can give them too. Sign in as the demo
administrator:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

## Create an ID-list segment

An ID-list segment has no rules. This creates one and saves its id in `$SEGMENT`:

```{.bash exec}
SEGMENT=$(curl -s -X POST localhost:8000/api/v1/segments \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Enterprise pilot", "kind": "id_list"}' | jq -r .id)

curl -s localhost:8000/api/v1/segments/$SEGMENT \
  -H "Authorization: Bearer $TOKEN" | jq -c '{kind, rules, member_count}'
```
<!-- expect: {"kind":"id_list","rules":null,"member_count":0} -->

It prints `{"kind":"id_list","rules":null,"member_count":0}`.

## Add IDs

Send up to 10,000 IDs per request. Each is 1 to 255 characters and is matched exactly: not
trimmed, not case-folded.

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/segments/$SEGMENT/members \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"add": ["cust-001", "cust-002", "cust-003"]}' | jq -c .
```
<!-- expect: {"added":3,"already_members":0,"member_count":3} -->

It prints `{"added":3,"already_members":0,"member_count":3}`.

**Sending the same IDs again is safe.** An ID already in the segment is counted in
`already_members` and left alone, so after a failure you can send the whole chunk again:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/segments/$SEGMENT/members \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"add": ["cust-003", "cust-004"]}' | jq -c .
```
<!-- expect: {"added":1,"already_members":1,"member_count":4} -->

It prints `{"added":1,"already_members":1,"member_count":4}`.

## Add IDs from a file

A segment holds at most 1,000,000 IDs. Send a longer file in chunks of 10,000, one request
at a time, and stop at the first error. This writes 25,000 IDs, one per line, to a file in a
scratch directory, sends them in three chunks and prints each answer. Each body is piped to
`curl` (`--data-binary @-`): 10,000 IDs are too long for one command-line argument on Linux.

```{.bash exec timeout=300}
cd "$(mktemp -d)"
seq -f 'cust-%06g' 1 25000 > customers.txt
split -l 10000 customers.txt chunk-
for chunk in chunk-*; do
  RESPONSE=$(jq -R . "$chunk" | jq -s '{add: .}' | curl -s -f -X POST localhost:8000/api/v1/segments/$SEGMENT/members \
    -H "Authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    --data-binary @-) || break
  jq -c . <<<"$RESPONSE"
done
```
<!-- expect: "member_count":25004 -->

It prints one line per chunk. The last is `{"added":5000,"already_members":0,"member_count":25004}`.
The file's IDs are written `cust-000001` to `cust-025000`, so none of them was in the segment
before (`cust-001` is a different ID).

A request over a limit answers 422 and adds nothing. The message names the field and the
position, never an ID: `add: at most 10,000 IDs per request`, `add[17]: an ID is 1 to 255
characters`, or, past the cap, `this segment would have 1,000,250 members; a segment holds
at most 1,000,000`.

## Check a user

A user is a member when the `user_id` in the context you send is in the list:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/segments/$SEGMENT/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"user_context": {"user_id": "cust-002"}}' | jq .is_member
```
<!-- expect: true -->

It prints `true`. A `user_id` that is not in the list, or a context with no `user_id`, prints
`false`.

## Remove IDs

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/segments/$SEGMENT/members/remove \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"remove": ["cust-002", "cust-999"]}' | jq -c .
```
<!-- expect: {"removed":1,"not_members":1,"member_count":25003} -->

It prints `{"removed":1,"not_members":1,"member_count":25003}`: `cust-999` was not a member.

## Target a flag or an experiment at a segment

A targeting condition names the segment by id: `{"attribute": "segment", "operator":
"in_segment", "value": "<segment id>"}`, or `not_in_segment` for everyone else. This creates
a flag that is on only for the segment's members (its rollout outside the segment is 0%):

```{.bash exec}
FLAG=pilot-banner-$(date +%s)
jq -n --arg key "$FLAG" --arg segment "$SEGMENT" '{
  key: $key, name: "Pilot banner", is_active: true, rollout_percentage: 0,
  targeting_rules: {groups: [{conditions: [
    {attribute: "segment", operator: "in_segment", value: $segment}]}]}}' \
  | curl -s -X POST localhost:8000/api/v1/feature-flags/ \
    -H "Authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    --data-binary @- | jq -r .status
```
<!-- expect: active -->

It prints `active`. Evaluating it takes an API key. `cust-001` is in the list and `cust-999` is
not:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Segments guide"}' | jq -r .key)

for USER in cust-001 cust-999; do
  printf '%s ' "$USER"
  curl -s -X POST localhost:8000/api/v1/feature-flags/evaluate/$FLAG \
    -H "X-API-Key: $KEY" \
    -H 'content-type: application/json' \
    -d "{\"user_id\": \"$USER\"}" | jq -c '{enabled, reason}'
done
```
<!-- expect: cust-001 {"enabled":true,"reason":"targeting_rule"} -->
<!-- expect: cust-999 {"enabled":false,"reason":"rollout"} -->

It prints one line per user: `cust-001` gets `{"enabled":true,"reason":"targeting_rule"}`, and
`cust-999` falls through to the 0% rollout, `{"enabled":false,"reason":"rollout"}`.

An experiment takes the same condition in its `targeting_rules`; a user who is not a member
gets the control variant with `assigned: false` and `reason: "targeting"`.

- **The server decides membership.** For an ID list, the user is the `user_id` the flag or the
  assignment is evaluated for; a `user_id` (or anything else) in the context you send does not
  make a user a member.
- **A key in a browser or a mobile app answers for any user id.** A key used in a browser or a mobile app is visible to the people who use the app, and it answers for whichever user id it is sent: what that id is served, whether it is in a segment, and which experiments it is assigned to.
  Where that matters, use user ids that do not identify a person (not email addresses), in ID lists too, and read assignments from your server.
- **At most 10 segments per ruleset**, one per condition; put several conditions in an `OR`
  group to match any of them.
- **Saving rules that name a segment that is unknown, inactive or archived answers `422`.** If
  the server cannot decide membership when the flag is evaluated (the flag was unarchived after
  its segment was archived, say), the flag answers `{"enabled": false, "reason": "error"}` and
  the experiment does not enrol the user, for `in_segment` and `not_in_segment` alike.
- **SDKs that evaluate flags locally ask the server** about a flag that uses a segment: see
  [Local evaluation](../sdk/local-evaluation.md).

The full rule is in the
[API reference](../api/endpoints.md#targeting-rules).

## Archive a segment

A segment that a flag or an experiment still uses cannot be archived or made inactive: it
answers `409` and lists them. A flag counts until it is archived, and an experiment until it is
completed or archived, because until then its rules can still be evaluated.

```{.bash exec}
curl -s -X DELETE localhost:8000/api/v1/segments/$SEGMENT \
  -H "Authorization: Bearer $TOKEN" | jq -c '{code: .detail.code, flags: (.detail.feature_flags | length)}'
```
<!-- expect: {"code":"segment_in_use","flags":1} -->

It prints `{"code":"segment_in_use","flags":1}`. The full answer is
`{"detail": {"code": "segment_in_use", "message", "feature_flags": [{"id", "key", "name"}],
"experiments": [{"id", "key", "name", "status"}]}}`. Remove the segment from those rules, or
archive the flag, then archive the segment.

## Limits

| Limit | Value |
|---|---|
| IDs per request | 1 to 10,000 |
| Characters per ID | 1 to 255 |
| IDs per segment | 1,000,000 |
| Who can change members | Users who may change experiments: ADMIN and DEVELOPER, and in the full profile anyone whose custom role or direct grant gives `experiment (update)`; anyone else gets `403` |
| Segments per targeting ruleset | 10 |

A request body is at most 5 MiB (5,242,880 bytes); a larger one is answered `413`. 10,000 IDs
of 255 ASCII characters come to about 2.6 MB, but the ID limit counts characters, not bytes: with
long non-ASCII IDs, send fewer per request.

Each add or remove writes one `segment_update` entry to the audit log with the counts, never
the IDs. A rules segment answers `409` on the member routes, and so does an archived one.

**Before you roll a database back** past the release that added ID lists, read
[`37dcb2969766`](../self-hosting/migrations.md#37dcb2969766-adds-segments-made-from-a-list-of-user-ids):
its downgrade deletes every list's members, so it refuses while any list has members.

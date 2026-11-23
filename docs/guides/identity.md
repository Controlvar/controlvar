# Link a visitor's ids when they sign in

To an experiment, a user is a user id. A visitor who browses under an anonymous id and then signs
in under their account's id is two users, each with an assignment of their own, until the two ids
are linked. `POST /api/v1/tracking/alias` (beta) links the anonymous id to the known id of the
same person. From then on, in each experiment, both ids are answered with the assignment stored
first among them, and an event either id sends to the experiment counts for it. It needs a
server-side key and is called from your backend. The full contract and every error are in the
[API reference](../api/endpoints.md#link-an-anonymous-id-to-a-known-user-id-beta).

The examples run against the [Quick Start](../getting-started/quick-start.md) stack and its
demo data.

## Start with a stable id

Assign with the id you will track purchases with:

- **A signed-in visitor**: their account's id, the same on every device.
- **A visitor who has not signed in**: an id of their own that stays with them, made on their
  first visit and kept in a cookie or on the device.
- **Never a placeholder** such as `anonymous` that every visitor without an id would share. The
  same user id always gets the same variant, so they would all be one user
  ([below](#never-link-a-shared-placeholder)).

A link joins two ids only from the moment it is stored
([What is not merged](#what-is-not-merged)). If every visitor signs in before they reach the
experiment, there is nothing to link.

## When to link

Link when your backend learns who the visitor is: when they sign in, or sign up, on a device
where they were assigned under an anonymous id. Send the anonymous id that device used and the
account's id.

- **Link before the account's id is used.** An assignment or event recorded under either id
  before the link stays as it was ([What is not merged](#what-is-not-merged)), so link as soon
  as you know, before the page assigns or tracks under the account's id.
- **Link each device.** Each browser or device has its own anonymous id; link each one when the
  person signs in there. A known id can have up to 100 anonymous ids linked to it.
- **Link at every sign-in if that is simpler.** The same link sent again answers
  `"created": false` and writes nothing.

## What you need

Sign in as the demo administrator:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

**An ACTIVE experiment.** This creates one with two welcome banners and starts it:

```{.bash exec}
EXP_ID=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Welcome banner",
    "key": "welcome-banner",
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 50, "configuration": {"headline": "Welcome"}},
      {"name": "personal", "traffic_allocation": 50, "configuration": {"headline": "Welcome back"}}
    ],
    "metrics": [{"name": "Purchase", "event_name": "purchase", "is_primary": true}]
  }' | jq -r .id)

curl -s -X POST localhost:8000/api/v1/experiments/$EXP_ID/start \
  -H "Authorization: Bearer $TOKEN" | jq -r .status
```
<!-- expect: active -->

It prints `active`.

**A server-side key**: an API key with the `sdk:ruleset` scope whose owner can change feature
flags, such as an ADMIN or a DEVELOPER. The same scope lets the key download every feature flag's
targeting rules, so keep it on your server and never ship it to a browser or a mobile app. This
creates one and saves it in `$KEY`:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Sign-in backend", "scopes": ["sdk:ruleset"]}' | jq -r .key)

printf '%s\n' "${KEY:0:5}"
```
<!-- expect: eptk_ -->

It prints `eptk_`.

## Assign the visitor

The visitor's first page assigns them under their anonymous id, `visitor-7f3a92`. In your app
that request usually comes from a browser or mobile SDK with a key of its own; here it is sent
with `$KEY`, and the answer is saved in `$VISITOR`:

```{.bash exec}
VISITOR=$(curl -s -X POST localhost:8000/api/v1/tracking/assign \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"experiment_key": "welcome-banner", "user_id": "visitor-7f3a92"}')

jq -c '{user_id, assigned}' <<<"$VISITOR"
```
<!-- expect: {"user_id":"visitor-7f3a92","assigned":true} -->

It prints `{"user_id":"visitor-7f3a92","assigned":true}`. `$VISITOR` also holds the variant,
`control` or `personal`.

## Link the ids at sign-in

The visitor signs in as `customer-1042`, and your backend links the two ids:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/alias \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"anonymous_id": "visitor-7f3a92", "user_id": "customer-1042"}' | jq -c .
```
<!-- expect: {"anonymous_id":"visitor-7f3a92","user_id":"customer-1042","created":true} -->

It prints the two ids as sent and `"created":true`: this request stored the link. Send it again:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/alias \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"anonymous_id": "visitor-7f3a92", "user_id": "customer-1042"}' | jq -c '{created}'
```
<!-- expect: {"created":false} -->

It prints `{"created":false}`: the link already exists, and nothing was written.

## Assign the known id

The next page assigns under the account's id. The answer names `customer-1042`, the id that was
sent, with the variant the visitor already holds, and no second assignment is stored:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/assign \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"experiment_key": "welcome-banner", "user_id": "customer-1042"}' \
  | jq -c --argjson visitor "$VISITOR" '{user_id, same_variant: (.variant_id == $visitor.variant_id)}'
```
<!-- expect: {"user_id":"customer-1042","same_variant":true} -->

It prints `{"user_id":"customer-1042","same_variant":true}`. A purchase tracked under
`customer-1042` with the experiment's key now counts for that variant.

## Read the person's assignments

`GET /api/v1/tracking/assignments/{user_id}` answers a linked id with its person's assignments,
one per experiment, each with `user_id` the id in the path:

```{.bash exec}
curl -s localhost:8000/api/v1/tracking/assignments/customer-1042 \
  -H "X-API-Key: $KEY" \
  | jq -c --argjson visitor "$VISITOR" 'map({user_id, experiment_name, same_variant: (.variant_id == $visitor.variant_id)})'
```
<!-- expect: [{"user_id":"customer-1042","experiment_name":"Welcome banner","same_variant":true}] -->

It prints `[{"user_id":"customer-1042","experiment_name":"Welcome banner","same_variant":true}]`:
one assignment, the visitor's.

## Which assignment answers

In each experiment, every id of a linked person is answered with one assignment:

- **The one stored first.** Of the person's ids that have an assignment in the experiment, the
  one stored first, by the API's clock, answers for all of them. Of two stored at the same
  instant, the one whose assignment id sorts first answers.
- **None yet.** When none of the person's ids has one, the person is assigned once, under the
  known id, whichever of their ids asks first.
- **Two already.** When both ids were assigned before the link (the visitor had already signed in
  on another device, say), both assignments stay, and both ids are answered with the earlier one
  from then on, so the person can see their variant change at the link. Both assignments still
  count in the experiment's results, one each ([below](#what-is-not-merged)).
- **One that races the link.** An assignment made under the known id while the link is being
  stored can still be stored under that id. It stays, and the earlier assignment answers for both
  ids from then on.

Whichever of the person's ids asks, from whichever device, it gets the person's assignment.

## What is not merged

A link changes how requests from then on are answered and stored. It merges nothing recorded
before it:

- **Anything recorded before the link** keeps the id and the variant it was stored with:
  assignments and events are not moved, rewritten or copied.
- **A person assigned under both ids before the link** counts once for each of the two
  assignments in that experiment's results.
- **Holdout results.** Holdout membership follows the person; holdout results do not: a linked
  person's conversions count only under the id the holdout first recorded.
- **Interaction detection, the audience preview and CUPED's history** count each id on its own.
- **An `event_id`** is remembered per id as sent: the same `event_id` sent once under each of two
  linked ids is stored twice.
- **Flags are not linked.** Each id is evaluated on its own, so a flag can answer the two ids
  differently.

[How events count](how-events-count.md#ids-linked-as-one-person) says how events sent after a
link are stored and counted. Erasing a linked person means erasing the rows of every one of their
ids, and their links ([How events count](how-events-count.md#sending-an-event-again)).

## The rules a link follows

A link that breaks one of these rules is refused with `409`, and nothing is written:

- An anonymous id is linked to one known id and is never moved to another.
- Links are one level deep: an id that has anonymous ids linked to it is not itself linked, and
  nothing is linked to an id linked as an anonymous id. A `409` for this rule often means the two
  ids were sent the wrong way round.
- A known id has at most 100 anonymous ids; the 101st is refused.

The ids are compared exactly: `Customer-1042` and `customer-1042` are two ids, and two equal ids
are refused with `422`. The visitor's id is linked to `customer-1042`, so linking it to another
account is refused:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/alias \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"anonymous_id": "visitor-7f3a92", "user_id": "customer-2088"}' | jq -r .detail
```
<!-- expect: already linked to a different user_id -->

It prints the refusal's `detail`, which names neither id.

## When a link fails

- **A `409`** means one of the rules above refused the link.
- **A network error, a `429` or a `500`**: send the same link again. Sending it twice is safe.
- **Until a link is stored**, the known id is an id of its own: an assignment made under it in the
  meantime is stored under it and stays, and the rules [above](#which-assignment-answers) decide
  which assignment answers once the link is stored.

## Never link a shared placeholder

If many visitors share one id, such as an `anonymous` a page falls back to when it has no cookie,
a link from it would make every one of those visitors the person it is linked to. So the route
refuses exactly `anonymous` and `anon`, in either field, with `422`:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/alias \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"anonymous_id": "anonymous", "user_id": "customer-1042"}' | jq -r '.detail[0].msg'
```
<!-- expect: placeholders shared by many -->

It prints `must identify one visitor or customer: 'anonymous' and 'anon' are placeholders shared by
many and cannot be linked`. Only those two are refused, compared exactly: `Anonymous`, `ANON` or
`anonymous-1` is linked like any other id, and so is any other placeholder your code uses, such as
`guest`. Give each visitor an id of their own instead.

## Links have no environment

One deployment is one environment ([Limitations](../getting-started/limitations.md)), and a link
has no environment of its own: a deployment keeps one set of links, and its tracking routes follow
them whichever API key a request carries. Link the ids your production traffic sends on your
production deployment.

## While a 0.47.0 API serves

During 0.48.0's rolling upgrade, and after an API-only rollback to 0.47.0, an API running 0.47.0
answers `404` on `POST /api/v1/tracking/alias` and ignores every link: it answers each id with that
id's own assignment, and assigns it under that id. Send a link answered `404` again once the
upgrade is done. A linked id with no assignment of its own in an experiment is given one by such
an API; once a release that reads links serves again, the person's earlier assignment answers for
both ids, and the second one stays and counts in the experiment's results
([Rolling back past identity aliasing](../deployment/rollback-runbook.md#rolling-back-past-identity-aliasing)).

Downgrading the database below `t449_identity_aliases` drops every link and keeps every
assignment and event; an upgrade afterwards starts with no links, and they must be sent again
([Migrations](../self-hosting/migrations.md#t449_identity_aliases-adds-the-identity-aliases-table)).

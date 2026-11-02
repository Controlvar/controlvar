# Creating Feature Flags

This guide creates a flag, adds targeting rules, turns it on for 10% of users and
evaluates it: first in the dashboard, then with the REST API. The API commands run as
written against the stack from the [Quick Start](../getting-started/quick-start.md).

---

## Prerequisites

- **A running stack.** [Quick Start](../getting-started/quick-start.md) Step 1 starts one
  on `localhost`. On your own deployment, use its URL wherever this page says
  `localhost:8000` or `localhost:3000`.
- **A user account with the ADMIN or DEVELOPER role.** Creating, changing and turning a
  flag on all take a user login. An API key can't do them (the API answers `401`). A
  superuser creates accounts under **Admin → Users**. The examples below use the demo
  administrator, `admin@demo.com`.
- **An API key**, to evaluate the flag the way your application will. The API section
  creates one; in the dashboard, a superuser can also create one under **Admin → API Keys**.

---

## Creating a Flag in the Dashboard

### Step 1: Open the form

Open the dashboard at `http://localhost:3000`, click **Feature Flags** in the top
navigation, then click **+ New Flag**.

### Step 2: Name the flag

| Field | Description | Example |
|-------|-------------|---------|
| **Name** (required) | The label shown in the dashboard. | `New Checkout Flow` |
| **Key** (required) | The identifier your code passes to the SDK. It's filled in from the name (`new_checkout_flow`), and you can edit it. Use lowercase letters, digits, `-` and `_`. The dashboard can't change it after the flag is created. | `new-checkout-flow` |
| **Description** | What the flag controls, and when it should be removed. | `Redesigned single-page checkout experience` |

### Optional: serve a value instead of on or off

Under **Values**, the **Type** is **Boolean** unless you choose another: the flag is on or
off, and your code reads whether it is on. Choose **String**, **Number** or **JSON** to
serve a value instead, and two fields appear:

| Field | What it holds |
|-------|---------------|
| **On value** | What users the flag is on for get: users who match a targeting rule, and the rollout percentage of everyone else. |
| **Off value** | What everyone else gets, and what everyone gets while the flag is off. |

A string may be empty. A number is written as in JSON, such as `10`, `-3` or `2.5`. A JSON
value is an object or an array, such as `{"color": "blue"}`. The two values must differ.
If a value can't be used, the reason is shown under its field and the flag is not created.
The type can't be changed after the flag is created; the two values can, on the flag's page.

A new flag starts off, so everyone gets its off value until you turn it on. Its page names
its type beside the status, and reads *Everyone gets the off value* while it is off and
*Rules and rollout decide who gets the on value* while it is on. Every application that
evaluates the flag can read both values, so don't put secrets in them.
[Serve a value](#serve-a-value) creates the same kind of flag with the API.

### Step 3: Add targeting rules (optional)

Targeting rules turn the flag on for specific users, whatever the rollout percentage
is. Under **Targeting Rules**, click **+ Add Group**: the group starts with one condition.
Click **+ Add Condition** for each condition after the first. A condition has three parts:
- an attribute: pick a suggestion such as `user.country`, or type any attribute your
  application sends;
- an operator;
- a value.

| Attribute | Operator | Value |
|-----------|----------|-------|
| `user.country` | is one of | `US, CA` |
| `user.plan` | equals | `enterprise` |

Each group's **AND**/**OR** decides how its conditions combine, and the **AND**/**OR**
above the groups decides how the groups combine.

If a user matches a rule, the flag is on for them. If they match no rule, the rollout
percentage in Step 4 decides. With no groups, the builder reads *No targeting rules —
all users will match*, and the rollout percentage decides for everyone.

### Step 4: Set the rollout percentage

The **Rollout percentage** slider sets the share of users who get the flag when they
match no targeting rule:

| Percentage | Effect |
|------------|--------|
| `0%` | Only users who match a targeting rule get the flag |
| `5%` | Matching users, plus 5% of everyone else |
| `100%` | Everyone |

Each user always lands on the same side of the percentage. Raising it adds users, and
takes none away.

### Step 5: Create the flag, then turn it on

Click **Create Feature Flag**. The flag starts **off** (its page reads *Not serving*), so
creating it never exposes anyone. When you're ready, turn it on with the switch beside
*Not serving*, and the page then reads *Serving*. The same switch turns it off again. Clients
serve the change within their cache lifetime, 5 minutes by default, the next time they ask for the
flag: see [Turning a Flag Off](kill-switch.md).

The dashboard's **On**/**Off** is the API's `is_active` (`true`/`false`), reported
beside it as `status` (`"active"`/`"inactive"`). You set it with `is_active`.

---

## Creating a Flag with the API

Run these in one terminal, in order. Each step uses the shell variables set by the ones
before it (`$TOKEN`, `$FLAG_ID`, `$KEY`).

**URLs.** The collection URL answers with or without a trailing slash
(`/api/v1/feature-flags/` or `/api/v1/feature-flags`). A single flag's URL has no trailing
slash (`/api/v1/feature-flags/$FLAG_ID`); with one, the API answers
`307 Temporary Redirect`. `curl` doesn't follow the redirect, so nothing happens and
nothing is printed.

### Log in

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

The second command prints `"ADMIN"`. If it prints `null`, the login failed (it takes
the account's `email`, not a username) and `$TOKEN` holds no token.

### Create the flag

```{.bash exec}
FLAG=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "key": "new-checkout-flow",
    "name": "New Checkout Flow",
    "description": "Redesigned single-page checkout experience",
    "rollout_percentage": 0
  }')
FLAG_ID=$(jq -r .id <<<"$FLAG")

jq '{key, status, is_active, rollout_percentage, default_value}' <<<"$FLAG"
```
<!-- expect: "status": "inactive" -->
<!-- expect: "is_active": false -->
<!-- expect: "rollout_percentage": 0 -->

```json
{
  "key": "new-checkout-flow",
  "status": "inactive",
  "is_active": false,
  "rollout_percentage": 0,
  "default_value": false
}
```

**A new flag is off** unless the request sends `"is_active": true`, so creating one never
exposes anyone. The response carries the flag's `id`, which this saves in `$FLAG_ID` for
the next steps.

The request takes these fields:
- `key` and `name` (both required; `name` is at most 100 characters);
- `description`;
- `is_active` (default `false`);
- `rollout_percentage` (0–100, default 0);
- `targeting_rules`;
- `value_type`: `boolean` (the default), `string`, `number` or `json` (see
  [Serve a value](#serve-a-value));
- `on_value` and `off_value`: what a `string`, `number` or `json` flag serves;
- `default_value`: deprecated, `off_value` replaces it. Only `false` is accepted, for every
  flag, and it is the default; `true` answers `422`;
- `tags`.

**Any other field answers `422`** (`"type": "extra_forbidden"`), so a misspelled or
unsupported field is never dropped silently. `null` is refused for `key`, `name`,
`is_active`, `rollout_percentage`, `default_value`, `value_type`, `on_value` and
`off_value`. A key that already exists answers `409`, and a key with capitals or spaces
answers `422`.

The fields a response carries but no request writes (`id`, `owner_id`, `created_at`,
`updated_at` and `status`) are accepted and ignored, so a GET body can be sent back
unchanged unless its `targeting_rules` are ones PUT now refuses (422); omitting the field
still works (see [Add targeting rules](#add-targeting-rules)). A `status` must match:
`"inactive"` (in any case) on a create without `is_active: true`, and the flag's current
status on an update. Any other value answers `422` with `"type": "read_only"`; turn a flag
on or off with `is_active`.

### Add targeting rules

`targeting_rules` uses the same shape the dashboard rule builder writes: a top-level
`logical_operator` combining one or more groups, each group combining its conditions.
This example targets enterprise-plan users in the US, CA or GB, **or** any employee:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "targeting_rules": {
      "logical_operator": "OR",
      "groups": [
        {
          "logical_operator": "AND",
          "conditions": [
            {"attribute": "user.plan", "operator": "equals", "value": "enterprise"},
            {"attribute": "user.country", "operator": "in", "value": ["US", "CA", "GB"]}
          ]
        },
        {
          "logical_operator": "AND",
          "conditions": [
            {"attribute": "employee", "operator": "equals", "value": "true"}
          ]
        }
      ]
    }
  }'
```
<!-- expect: 200 -->

It prints `200`.

**Rules the flag evaluator would not apply as written answer `422`**, on a create and on an
update, and nothing is saved. That is:

- a list of rules (the legacy `[{"type": ...}]` shape), and a value that is not an object
  (a string, a number, `true`);
- a flat object such as `{"country": ["US"]}`, and any other key the shape does not have,
  at the top level, on a group or on a condition;
- `groups` together with `rules`, `logical_operator` without `groups`, and a top-level
  `name`;
- native rules without `rules` (a `default_rule` on its own), and native rules with a key
  the shape does not have (a misspelt `rollout_percentage`, `priority` or `conditions`) on a
  rule, a group at any depth, a condition or the `default_rule`;
- native groups nested more than 10 levels deep, and native rules with more than 1,000
  rules, groups and conditions in all;
- dashboard rules with more than 1,000 groups and conditions in all, each group and each
  condition counting as one;
- a group with no conditions, a condition with no attribute, and an attribute with
  anything other than letters, digits, `_` and `.`;
- an unknown operator or logical operator (`and`, `or` or `not`, in any case);
- a value the operator cannot use (`"abc"` for `greater_than`, `"not-a-version"` for
  `semver_gte`), a `regex` pattern RE2 refuses, and a list operator with more than 1,000
  values;
- a `rollout_percentage`, top-level or on a native rule, that is not an integer from 0 to
  100 (`100.0` is accepted; `33.5`, `true` and `"50"` are not), or an `id` that is not text
  of 1 to 100 characters.

The message names the place and the reason, never the value you sent. Only the first
problem found is reported. This request misspells `equals`:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "targeting_rules": {
      "logical_operator": "AND",
      "groups": [
        {"conditions": [{"attribute": "user.plan", "operator": "equal", "value": "enterprise"}]}
      ]
    }
  }' | jq -c '{loc: .detail[0].loc, msg: .detail[0].msg}'
```
<!-- expect: "loc":["body","targeting_rules"] -->
<!-- expect: "msg":"Value error, groups[0].conditions[0].operator: unknown operator" -->

```json
{"loc":["body","targeting_rules"],"msg":"Value error, groups[0].conditions[0].operator: unknown operator"}
```

`groups[0].conditions[0]` is the first condition of the first group. A problem with the
rules as a whole has no place, and reads `targeting rules: <reason>`, for example
`targeting rules: unknown key`.

**Rules stored before this check are not re-checked.** They are evaluated as before, and
an update that leaves `targeting_rules` out succeeds whatever is stored. An update that
sends stored rules back unchanged, as a `GET` returned them, answers `422` if they are
rules the API now refuses: fix them, or leave the field out. To list the flags whose
stored rules are refused, run this from the repository root, against the same database
settings as the API:

```{.bash skip reason="checkout: runs from a repository checkout, against the API database"}
python -m backend.scripts.check_targeting_rules
```

It prints one line per flag, `feature_flag <id> <key> <place> <reason>`, writes nothing, and
exits 0 whether or not it lists anything (2 when it cannot read the database). A flag whose
legacy (list-shaped) rules hold a condition with an operator other than `eq`, `ne`, `gt`,
`lt`, `contains` or `in` has its own line: that rule matches no user.

Operators: `equals`, `not_equals`, `contains`, `not_contains`, `starts_with`, `ends_with`,
`greater_than`, `less_than`, `greater_than_or_equal`, `less_than_or_equal`, `in`, `not_in`,
`regex`, `is_null`, `is_not_null`, `semver_eq`, `semver_gt`, `semver_lt`, `semver_gte`,
`semver_lte`, `geo_within_radius`, `time_window`, `array_contains`, `array_intersects`,
`in_segment`, `not_in_segment`.
Values typed in the dashboard are strings; they are compared leniently against typed
context values (`"true"` matches `true`, `"17"` matches `17`, `"beta, staff"` is a list
for `in`/`not_in`, `"17.4"` is padded to `17.4.0` for `semver_*`).

`regex` patterns use [RE2 syntax](https://github.com/google/re2/wiki/Syntax): `\w`, `\d`,
`\s` are ASCII-only, `$` matches only at the very end of the value, lookaround and
backreferences are refused, and values longer than 256 characters are not evaluated. A
pattern RE2 refuses answers `422` when it is saved (`groups[0].conditions[0]: pattern is
not valid`). A flag whose rules were stored with such a pattern before this check, or whose
context value cannot be evaluated, evaluates disabled with reason `error` rather than falling
through to the rollout; `python -m backend.scripts.check_targeting_rules` lists those flags.
See the [rules engine reference](../Enhanced_Rules_Engine_Reference.md#match_regex-match_regex).

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

Users who match a rule are bucketed with the rule's `rollout_percentage` (100 unless set on
the rules object); users who match no rule fall through to the flag's global
`rollout_percentage`. The native Enhanced Rules Engine shape (`{"rules": [...]}`) is accepted
as well; it must carry `rules`. `null`, `{}` and `{"groups": []}` mean no rules. A list of
rules (the legacy shape) is refused when saved; a flag stored with one before is still
evaluated.

#### Targeting context and attribute aliases

Rules are matched against the **context** the SDK sends with each evaluation (the user's
attributes: `context=<url-encoded JSON>` on `GET /feature-flags/evaluate/{key}`, or the
`context` object on the `POST` variant). Before matching, the context is expanded so that:

- every key is available as given (`{"country": "US"}` answers `country`);
- nested objects are flattened to dotted keys (`{"app": {"version": "3.2.1"}}` answers
  `app.version`);
- every top-level key also answers `user.<key>`, `device.<key>` and `app.<key>`
  (`{"country": "US"}` answers `user.country`; `{"os_version": "17.4.0"}` answers
  `device.os_version`), and `user.<key>` / `device.<key>` / `app.<key>` keys in the context
  answer the bare `<key>` too;
- explicit keys always win over aliases.

So a dashboard rule on `user.country` matches an SDK that sends `{"country": "US"}` without
any renaming on either side. A condition whose attribute is absent from the context does not
match (except `is_null`, which does). A context larger or more deeply nested than the
[Targeting Context Limits](../api/endpoints.md#targeting-context-limits) allow answers `422`.

### Turn the flag on at 10%

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"rollout_percentage": 10, "is_active": true}' | jq '{status, rollout_percentage}'
```
<!-- expect: "status": "active" -->
<!-- expect: "rollout_percentage": 10 -->

```json
{
  "status": "active",
  "rollout_percentage": 10
}
```

`"is_active": false` turns it off again. `POST /api/v1/feature-flags/$FLAG_ID/activate`
and `.../deactivate` do the same without a body. A `PUT` changes only the fields it sends;
a `status` in it must be the flag's current status.

### Archived flags

An archived flag is retired: it is never served, and nothing turns it back on by
accident. Every request that would turn it on (`PUT` with `"is_active": true`,
`/activate`, `/enable`, `/toggle`, and bulk `enable`) answers `400` with

```text
This flag is archived. Unarchive it before turning it on.
```

and leaves the flag archived. A request that turns it off (`"is_active": false`,
`/deactivate`, `/disable`, bulk `disable`) succeeds and changes nothing, because an
archived flag is already off. The way back is
`POST /api/v1/feature-flags/$FLAG_ID/unarchive` (beta), which makes the flag
inactive; turn it on afterwards as usual. A flag is archived with
`POST /api/v1/feature-flags/bulk-toggle` and `"action": "archive"`. The dashboard has no
archive or unarchive action yet.

### Create an API key

Applications evaluate flags with an API key, not a user token. The key is shown only
once, and this saves it in `$KEY`:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"flag-guide"}' | jq -r .key)
```

### Serve a value

A flag can serve a string, a number or a JSON value instead of on or off. Its `value_type`
is set when it is created, and it has two values: `on_value`, what the users it is on for
get, and `off_value`, what everyone else gets, and everyone while the flag is off. This
creates a string flag for a checkout button's label, on for enterprise-plan users, and
turns it on:

```{.bash exec}
LABEL=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "key": "checkout-button-label",
    "name": "Checkout button label",
    "value_type": "string",
    "on_value": "Complete purchase",
    "off_value": "Buy now",
    "rollout_percentage": 0,
    "is_active": true,
    "targeting_rules": {
      "logical_operator": "AND",
      "groups": [
        {
          "logical_operator": "AND",
          "conditions": [
            {"attribute": "user.plan", "operator": "equals", "value": "enterprise"}
          ]
        }
      ]
    }
  }')

jq '{key, status, value_type, on_value, off_value}' <<<"$LABEL"
```
<!-- expect: "on_value": "Complete purchase" -->
<!-- expect: "off_value": "Buy now" -->

A user on the enterprise plan matches the rule, so the flag is on for them and they get the
on value:

```{.bash exec}
curl -s -G localhost:8000/api/v1/feature-flags/evaluate/checkout-button-label \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-123" \
  --data-urlencode 'context={"plan":"enterprise"}' | jq
```
<!-- expect: "value": "Complete purchase" -->
<!-- expect: "value_type": "string" -->

```json
{
  "key": "checkout-button-label",
  "enabled": true,
  "value": "Complete purchase",
  "value_type": "string",
  "config": null,
  "reason": "targeting_rule"
}
```

A user on the free plan matches no rule, and the rollout percentage is 0, so they get the
off value:

```{.bash exec}
curl -s -G localhost:8000/api/v1/feature-flags/evaluate/checkout-button-label \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-123" \
  --data-urlencode 'context={"plan":"free"}' | jq -c '{enabled, value, reason}'
```
<!-- expect: {"enabled":false,"value":"Buy now","reason":"rollout"} -->

**What the API checks.** A `string` flag's two values are strings (an empty string is
allowed), a `number` flag's are numbers (not `true` or `false`), and a `json` flag's are
JSON objects or arrays. A create sends both, and they must differ: `3` and `3.0` are the
same number. Each value is at most 8,192 bytes as compact JSON and 10 levels deep, every
number in it is finite and from -9007199254740991 to 9007199254740991, and no string in it
holds a NUL or an unpaired surrogate character. A value outside these answers `422` at that
field, and the message names the field, never the value. This number flag is given text:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"key": "checkout-retries", "name": "Checkout retries", "value_type": "number",
       "on_value": "ten", "off_value": 3}' | jq -c '{loc: .detail[0].loc, msg: .detail[0].msg}'
```
<!-- expect: "loc":["body","on_value"] -->
<!-- expect: "msg":"on_value must be a number, because value_type is number" -->

```json
{"loc":["body","on_value"],"msg":"on_value must be a number, because value_type is number"}
```

`value_type` cannot change once the flag exists: an update may send it back unchanged, and
any other value answers `422` with `"type": "read_only"`. An update may send either value
on its own; it is checked against the flag's type, and the two values as they will be
stored must still differ. A boolean flag's `on_value` is `true` and its `off_value` is
`false`: a request may send those back, so a GET body still round-trips, and any other
value answers `422`. `default_value` is deprecated and accepts only `false`, for every flag;
read `off_value`.

Every application that evaluates the flag can read both of its values, so do not put
secrets in them.

---

## Flag Key Naming Conventions

A key is lowercase letters, digits, hyphens and underscores, starting with a letter or
digit. Anything else is refused with `422`.

---

## When a Flag Evaluates to Off

Every flag serves two values. Evaluation returns `enabled: false`, and `value` set to the
flag's `off_value` (`false` for a boolean flag), when:

- the flag is off (`reason: "inactive"`), which includes a flag that a safety rollback to
  0% turned off (see [Safety Monitoring](safety.md#what-a-rollback-changes));
- the user matches no targeting rule and falls outside the rollout percentage
  (`reason: "rollout"`);
- the user matches a rule but falls outside that rule's own rollout percentage
  (`reason: "targeting_rule"`);
- evaluation fails on the server (`reason: "error"`).

When the call itself fails (a network error or a wrong API key), the SDKs'
`isFeatureEnabled` and `is_feature_enabled` return `false`. So a flag your code can't
reach behaves as off.

How soon each client serves a change: [Turning a Flag Off](kill-switch.md).

---

## Evaluating the Flag from Your Application

### JavaScript SDK

```javascript
import { ExperimentationClient } from '@controlvar/js-sdk';

const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',   // your API origin; the SDK appends /api/v1/...
  apiKey: process.env.CONTROLVAR_API_KEY,
});

const isEnabled = await client.isFeatureEnabled('new-checkout-flow', {
  userId: 'user-123',
  attributes: { plan: 'enterprise', country: 'US' },
});

if (isEnabled) {
  showNewCheckoutFlow();
} else {
  showCurrentCheckoutFlow();
}
```

### Python SDK

```python
import os

from controlvar import ExperimentationClient

client = ExperimentationClient(
    api_url="http://localhost:8000",   # your API origin
    api_key=os.environ["CONTROLVAR_API_KEY"],
)

is_enabled = client.is_feature_enabled(
    flag_key="new-checkout-flow",
    user_id="user-123",
    user_attributes={"plan": "enterprise", "country": "US"},
)
```

### REST API (Direct)

Send the targeting context as a URL-encoded JSON object on the `GET` endpoint (this is
what the SDKs do):

```{.bash exec}
curl -s -G localhost:8000/api/v1/feature-flags/evaluate/new-checkout-flow \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-123" \
  --data-urlencode 'context={"plan":"enterprise","country":"US"}' | jq
```
<!-- expect: "enabled": true -->
<!-- expect: "reason": "targeting_rule" -->

```json
{
  "key": "new-checkout-flow",
  "enabled": true,
  "value": true,
  "value_type": "boolean",
  "config": null,
  "reason": "targeting_rule"
}
```

Or send it as the `context` object in the body of the `POST` variant:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/feature-flags/evaluate/new-checkout-flow \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"user_id": "user-123", "context": {"plan": "enterprise", "country": "US"}}' | jq .reason
```
<!-- expect: "targeting_rule" -->

It prints `"targeting_rule"`.

The same user on a free plan in Germany matches no rule. The 10% rollout then decides,
and `user-123` falls outside it:

```{.bash exec}
curl -s -G localhost:8000/api/v1/feature-flags/evaluate/new-checkout-flow \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-123" \
  --data-urlencode 'context={"plan":"free","country":"DE"}' | jq '{enabled, reason}'
```
<!-- expect: "enabled": false -->
<!-- expect: "reason": "rollout" -->

`reason` explains the outcome: `targeting_rule` (a rule matched and its rollout percentage
decided), `rollout` (no rule matched; the global rollout percentage decided), `inactive` or
`error`. A `context` that is not a JSON object returns `422`.
`GET /api/v1/feature-flags/user/{user_id}?context=...` evaluates every active flag the same way
and returns `{"flag-key": true|false, ...}`. A value flag answers `true` when the user gets
its on value.

### Reporting client-side errors

Safety monitoring computes a flag's error rate from `error_logs` rows divided by its
evaluations. The errors your applications see behind a flag (crashes, failed requests)
count toward it when they are reported with a server-side key: an API key with the
`sdk:ruleset` scope, created by a user who can change feature flags (ADMIN, DEVELOPER or a
superuser). A browser or mobile app sends its errors to your server, and the server forwards
them with that key. Keep the key on the server: it can also download every flag's targeting
rules.

```{.bash exec}
SERVER_KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"flag-guide-server","scopes":["sdk:ruleset"]}' | jq -r .key)

curl -s -X POST localhost:8000/api/v1/tracking/errors \
  -H "X-API-Key: $SERVER_KEY" \
  -H 'content-type: application/json' \
  -d '{
    "feature_flag_key": "new-checkout-flow",
    "user_id": "user-123",
    "error_type": "crash",
    "message": "NullPointerException in CheckoutV2",
    "metadata": {"os": "Android", "os_version": "12.0.0"}
  }' | jq .error_type
```
<!-- expect: "crash" -->

It prints `"crash"`, the stored row's `error_type`.

A report sent with `$KEY`, or with any other key without the `sdk:ruleset` scope, is accepted
(`201`) and stored the same way, but does not count toward the error rate or automatic
rollback: the flag's safety check shows it in `details.uncounted_error_count` (see
[Which errors count](safety.md#which-errors-count)).

A server that uses the JavaScript or Python SDK sends the same request with `reportError` or
`report_error` (see [Reporting errors](../sdk/javascript.md#reporting-errors)). Their `true`
means the report was stored (`201`); whether it counts depends on the key, as above.

`POST /api/v1/tracking/errors/batch` accepts `{"errors": [...]}` (up to 100) and returns
`{"success_count", "failure_count", "errors"}`. At least one of `feature_flag_key` /
`experiment_key` is required; unknown keys return `404` (single) or are listed per item
(batch).

What is stored for each report, on both endpoints:

| Field | Stored as |
|-------|-----------|
| `timestamp` | The time the server received the report, for the stored row and for the `timestamp` in the `POST /api/v1/tracking/errors` response. A `timestamp` you send is kept in the row's metadata as `client_timestamp` (ISO 8601). |
| `message` | The first 1000 characters. |
| `stack_trace` | At most 8 KB (8192 bytes, UTF-8); anything longer is cut off at a character boundary. |
| `metadata` | Stored as sent when its compact JSON is at most 8 KB (8192 bytes, UTF-8). Larger metadata is replaced by `{"metadata_truncated": true, "metadata_bytes": <size>}`. `client_timestamp` is added after this check. |

---

## Next Steps

- [Gradual Rollouts](rollouts.md) — set up a staged rollout schedule with automatic progression
- [Safety Monitoring](safety.md) — automatic rollback if error rates spike: off by default, and a
  superuser turns it on in **Admin → Safety**
- [SDK Documentation](../sdk/javascript.md) — full SDK integration guide

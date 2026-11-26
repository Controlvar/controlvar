---
title: Multi-way flags, a split and a rule's own value
description: "Serve one of 3 to 10 values to the users a string, number or json flag is on for, by whole-number weights, or give a targeting rule a value of its own, in the dashboard or with the REST API."
---

# Multi-way Flags

A string, number or json flag serves its on value to the users it is on for. It can serve
several values instead. A **split** lists 3 to 10 values, each with a **weight**: the share of
those users who get it. A targeting rule can also have a **value of its own**, which the users it
matches get. Neither changes who the flag is on for: its targeting rules and its rollout
percentage decide that, as for any flag. A boolean flag has neither.

The commands on this page run as written against the stack from the
[Quick Start](../getting-started/quick-start.md), in one terminal, top to bottom. Each one uses
the shell variables set by the ones before it.

## What a split is

- A split is a list of values and weights. The weights are whole numbers that add up to 100.
- A user's value comes from a hash of their user ID and the flag's key, which puts each user in
  one of 100 buckets. The values take the buckets in the order the split lists them, each as
  many as its weight, so the split gives a value of weight 0 to nobody.
- The hash is not the rollout percentage's hash. Changing the rollout percentage changes who
  the flag is on for, and never which value a user gets.
- The flag still has an on value and an off value. The off value is what everyone else gets,
  and everyone while the flag is off. The on value is not served while a split is set: it is
  served again once the split is removed.
- Each environment has its own split, and a promotion copies it. The flag's value type, on
  value and off value belong to the flag, in every environment. See
  [Flag environments](environments.md).

## A split or an experiment

A split serves values and measures nothing. To learn which value does better on a metric,
create an experiment.

| | Do I get results? | Can I change shares while live? | Is a user's value stable? |
|---|---|---|---|
| An on/off flag with a rollout | No | Yes: raise or lower the rollout percentage | Yes: each user always lands on the same side of the percentage |
| A flag with a split | No | Yes: change the weights; only the users in the buckets that change hands get another value | Yes: the same user gets the same value until the weights change, whatever the rollout percentage |
| A rule serving a value | No | There are no shares: the users the rule matches get its value | Yes, while the user matches the rule |
| An experiment | Yes: each variant's metrics, compared with the control | No: the variants are fixed once it starts; a bandit moves new users by itself | Yes: the server stores each user's variant |

## Set a split with the API

Log in as the demo administrator:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

Create a string flag for a checkout button's text, on for everyone:

```{.bash exec}
FLAG=$(curl -s -X POST localhost:8000/api/v1/feature-flags/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "key": "checkout-button-text",
    "name": "Checkout button text",
    "value_type": "string",
    "on_value": "Buy now",
    "off_value": "Checkout",
    "rollout_percentage": 100,
    "is_active": true
  }')
FLAG_ID=$(jq -r .id <<<"$FLAG")

jq -c '{key, status, on_value, off_value, split}' <<<"$FLAG"
```
<!-- expect: {"key":"checkout-button-text","status":"active","on_value":"Buy now","off_value":"Checkout","split":null} -->

It prints `{"key":"checkout-button-text","status":"active","on_value":"Buy now","off_value":"Checkout","split":null}`.
`split` is `null`: the flag has none, and serves its on value.

Give it a split of three values. A `PUT` changes only the fields it sends:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "split": [
      {"value": "Buy now", "weight": 34},
      {"value": "Complete purchase", "weight": 33},
      {"value": "Pay now", "weight": 33}
    ]
  }' | jq -c .split
```
<!-- expect: [{"value":"Buy now","weight":34},{"value":"Complete purchase","weight":33},{"value":"Pay now","weight":33}] -->

It prints the split as stored. A split can also be sent when the flag is created, and
`"split": null` removes it.

Create an API key, then evaluate the flag for three users:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"multi-way-guide"}' | jq -r .key)

curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text?user_id=user-1" \
  -H "X-API-Key: $KEY" | jq -c '{enabled, value, reason}'
curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text?user_id=user-2" \
  -H "X-API-Key: $KEY" | jq -c '{enabled, value, reason}'
curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text?user_id=user-11" \
  -H "X-API-Key: $KEY" | jq -c '{enabled, value, reason}'
```
<!-- expect: {"enabled":true,"value":"Buy now","reason":"rollout"} -->
<!-- expect: {"enabled":true,"value":"Complete purchase","reason":"rollout"} -->
<!-- expect: {"enabled":true,"value":"Pay now","reason":"rollout"} -->

`user-1` gets `Buy now`, `user-2` gets `Complete purchase` and `user-11` gets `Pay now`. These
three IDs were picked by running the server's hash, so that each gets a different value. Your IDs
will land differently; what is fixed is that the same ID always gets the same value.

The answer has the fields it always had. `reason` is `rollout` or `targeting_rule`, decided as
for any flag: a split changes the value, not the reason.

Lower the rollout percentage to 50. The users the flag is still on for keep their value, and the
others get the off value:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"rollout_percentage": 50}' | jq .rollout_percentage

curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text?user_id=user-1" \
  -H "X-API-Key: $KEY" | jq -c '{enabled, value, reason}'
curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text?user_id=user-2" \
  -H "X-API-Key: $KEY" | jq -c '{enabled, value, reason}'
curl -s "localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text?user_id=user-11" \
  -H "X-API-Key: $KEY" | jq -c '{enabled, value, reason}'
```
<!-- expect: 50 -->
<!-- expect: {"enabled":true,"value":"Buy now","reason":"rollout"} -->
<!-- expect: {"enabled":false,"value":"Checkout","reason":"rollout"} -->
<!-- expect: {"enabled":true,"value":"Pay now","reason":"rollout"} -->

`user-2` is outside 50% and gets `Checkout`, the off value; `user-1` and `user-11` keep theirs.

## Give a rule a value of its own

A targeting rule with a `value` serves it to the users it matches, instead of the split. This
gives enterprise-plan users `Request a quote`:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "targeting_rules": {
      "logical_operator": "AND",
      "value": "Request a quote",
      "groups": [
        {
          "logical_operator": "AND",
          "conditions": [
            {"attribute": "user.plan", "operator": "equals", "value": "enterprise"}
          ]
        }
      ]
    }
  }'

curl -s -G localhost:8000/api/v1/feature-flags/evaluate/checkout-button-text \
  -H "X-API-Key: $KEY" \
  --data-urlencode "user_id=user-2" \
  --data-urlencode 'context={"plan":"enterprise"}' | jq -c '{enabled, value, reason}'
```
<!-- expect: 200 -->
<!-- expect: {"enabled":true,"value":"Request a quote","reason":"targeting_rule"} -->

It prints `200`, then `{"enabled":true,"value":"Request a quote","reason":"targeting_rule"}`:
`user-2` matches the rule, so the rule's percentage (100 unless the rule sets one) decides, not
the flag's 50%, and the rule's value is served. A user the rule does not match gets the split, as
above.

- The `value` goes at the top of the dashboard shape, beside `logical_operator` and `groups`. In
  the native shape (`{"rules": [...]}`) each rule and the `default_rule` may carry one.
- A rule with no value serves the split, or the on value when there is no split.
- A user who matches a rule but falls outside that rule's own `rollout_percentage` gets the off
  value.

## In the dashboard

A string, number or json flag's page has a **Split** section in each environment; in Staging it
reads **Split in Staging**. A boolean flag's page has none.

- **Add a split** starts three empty values at weights 34, 33 and 33, so nothing is served until
  each is typed. Each row has a **Value** and a **Weight (%)**.
- **Add value** adds a value at the end, at weight 0, up to 10 values, and **Remove** takes one
  out, down to 3. **Spread evenly** shares the 100 between them, the extra to the first rows.
- The total reads `Total 100%`, or for example `Total 90%: 10% more needed`. A split whose weights
  do not add up to 100 is not sent, and the page says `Weights must add up to 100; they add up to 90.`
- When you change the weights of a split, the page says, before you save, about how many in every
  100 users the flag is on for move, and from which value to which.
- **Remove the split** removes it when you save: the users the flag is on for then get the on
  value.
- **Users these rules match get**, under the targeting rules, offers
  `A value of the split (the default)` (or `The on value (the default)` with no split) and
  `A value of their own`.
- Someone who may not change flags reads `Serving 3 values` and the table without inputs. The flag
  list marks a flag that has a split in the environment it shows with **Split**.

The section ends with a note that links to the experiments page:
`A split does not measure anything. To compare values on a metric, create an experiment.`

## Limits

What a request is refused with `422` for, at the field named:

- a split of fewer than 3 or more than 10 values, or weights that do not add up to exactly 100;
- a weight that is not a whole number from 0 to 100 (`33.5`, `true` and `"33"` are refused);
- a value that is not of the flag's type, `null`, or two values that are equal (`3` and `3.0` are
  the same number); a value may equal the on value or the off value;
- a value of more than 8192 bytes as compact JSON, or more than 10 levels deep, as for the on and
  off values;
- more than 81920 bytes of split values and rules' own values together, in one environment;
- a split, or a rule's own value, on a boolean flag;
- a value on dashboard rules with no condition, which match no user.

The message names the field, never the value. These weights add up to 90:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/feature-flags/$FLAG_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"split": [{"value": "Buy now", "weight": 30}, {"value": "Complete purchase", "weight": 30}, {"value": "Pay now", "weight": 30}]}' \
  | jq -c '{loc: .detail[0].loc, msg: .detail[0].msg}'
```
<!-- expect: {"loc":["body","split"],"msg":"Weights must add up to 100; they add up to 90."} -->

It prints `{"loc":["body","split"],"msg":"Weights must add up to 100; they add up to 90."}`, and
the stored split is unchanged. For two values, use the on and off values with a rollout
percentage, or add a third value with weight 0.

## Changing a flag that serves a split

- **Weights.** A user's bucket never changes, so a user moves only when the value at their bucket
  changes. Raising one value's weight by 10 and lowering the weight of the value next to it by 10
  moves about 10 in every 100 users the flag is on for, from that value to the raised one, and
  nobody else.
- **The rollout percentage.** Raising it adds users, each with the value the split gives them;
  lowering it gives the users it takes away the off value. Nobody else's value changes.
- **Turning the flag off.** Everyone gets the off value.
- **A safety rollback.** A rollback to 0% turns the flag off, and everyone gets the off value. A
  rollback to 1–100% lowers only the global rollout percentage: the users it takes away get the
  off value, and every user who stays on keeps their value. Users a rule matches keep their rule's
  percentage and its value. See [Safety monitoring](safety.md#what-a-rollback-changes).
- **A rollout schedule.** Each stage sets the rollout percentage: the users it adds get a value
  of the split, and nobody else's value changes.

The audit log names `split` among the fields a change made, and never stores its values.

## SDKs and local evaluation

Every SDK asks the server about a flag with a split or a rule's own value: the JavaScript and
Python SDKs' local evaluation (beta) leaves such a flag to the server. Turning it off reaches every
release within one ruleset refresh. Any other change reaches a user an SDK has already cached only
after the cache lifetime, 300 s by default. [Local evaluation](../sdk/local-evaluation.md#what-is-evaluated-locally)
has the details, and the cost of bulk calls.

The OpenFeature providers report the variant `"on"` for every user the flag is on for, whichever
value they get, and `"off"` for a user it is off for.

## Older releases

A 0.48.0 task ignores splits and rules' own values, and serves each such flag's on value to every
user the flag is on for: during the rolling upgrade to 0.49.0, and after a rollback of the API
alone to 0.48.0. See
[Rolling back past flag splits](../deployment/rollback-runbook.md#rolling-back-past-flag-splits).

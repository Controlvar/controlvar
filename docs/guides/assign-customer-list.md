# Assign a customer list to an experiment

Use this when you know who will see an experiment before they arrive (an email campaign, a
CRM export, a list from your warehouse) and need each person's variant now.
`POST /api/v1/tracking/assign/batch` (beta) assigns up to 1,000 users in one request. Each
user gets what `POST /api/v1/tracking/assign` would give them, with the bandit weights read at
the start of the request: the global holdout, the experiment's mutual exclusion group and its
targeting rules apply to each new user, and a user who is already assigned keeps their variant.
The full contract and every error are in the
[API reference](../api/endpoints.md#assign-a-list-of-users-to-an-experiment-beta).

The examples run against the [Quick Start](../getting-started/quick-start.md) stack and its
demo data.

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

**An ACTIVE experiment.** This creates one for an email campaign, with two subject lines and
a targeting rule that admits customers in the US only, and starts it:

```{.bash exec}
EXP_ID=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Spring email",
    "key": "spring-email",
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 50, "configuration": {"subject": "Spring sale"}},
      {"name": "urgent", "traffic_allocation": 50, "configuration": {"subject": "48 hours left"}}
    ],
    "metrics": [{"name": "Purchase", "event_name": "purchase", "is_primary": true}],
    "targeting_rules": {
      "logical_operator": "AND",
      "groups": [
        {"logical_operator": "AND", "conditions": [{"attribute": "country", "operator": "equals", "value": "US"}]}
      ]
    }
  }' | jq -r .id)

curl -s -X POST localhost:8000/api/v1/experiments/$EXP_ID/start \
  -H "Authorization: Bearer $TOKEN" | jq -r .status
```
<!-- expect: active -->

It prints `active`.

**An API key with the `sdk:ruleset` scope**, created by an ADMIN or a DEVELOPER. The same scope lets the key download every feature flag's targeting
rules, so keep it on a server and never ship it to a browser or a mobile app. This creates one
and saves it in `$KEY`:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Campaign batch job", "scopes": ["sdk:ruleset"]}' | jq -r .key)

printf '%s\n' "${KEY:0:5}"
```
<!-- expect: eptk_ -->

It prints `eptk_`.

## Send the list

Each user has a `user_id` and, optionally, a `context`: the attributes the experiment's
targeting rules read, as on `POST /api/v1/tracking/assign`. This sends three customers, two
in the US and one in Germany, and saves the response in `$RESULT`:

```{.bash exec}
RESULT=$(curl -s -X POST localhost:8000/api/v1/tracking/assign/batch \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{
    "experiment_key": "spring-email",
    "users": [
      {"user_id": "cust-001", "context": {"country": "US"}},
      {"user_id": "cust-002", "context": {"country": "US"}},
      {"user_id": "cust-003", "context": {"country": "DE"}}
    ]
  }')

jq -c .counts <<<"$RESULT"
```
<!-- expect: "assigned":2 -->
<!-- expect: "targeting":1 -->

It prints `{"assigned":2,"holdout":0,"mutual_exclusion":0,"targeting":1}`. The response also
has `assignments`, one entry per user in the order sent, and `variants`, each variant's name
and configuration by id.

## Read who was not enrolled

```{.bash exec}
jq -r '.assignments[] | select(.assigned == false) | [.user_id, .reason] | @tsv' <<<"$RESULT"
```
<!-- expect: cust-003 -->
<!-- expect: targeting -->

It prints `cust-003` and `targeting`. A user who was not enrolled gets the control variant, the
experience to show them, and nothing is recorded for them:

| `reason` | What it means | What to do |
|---|---|---|
| `holdout` | The user is in the global holdout, which sees no experiment | Nothing: show the control |
| `mutual_exclusion` | The user is enrolled in, or hashed to, another experiment of the same mutual exclusion group | Nothing: show the control |
| `targeting` | The user's `context` does not match the experiment's targeting rules | Send the attributes your rules use in each user's `context` |

## Join variants to your list

```{.bash exec}
jq -r '.variants as $v | .assignments[] | [.user_id, $v[.variant_id].name, .assigned] | @csv' <<<"$RESULT"
```
<!-- expect: "cust-003","control",false -->

It prints one line per customer: the id, the variant's name and whether they were enrolled.
`cust-003` is `"cust-003","control",false`; the other two are enrolled in `control` or
`urgent`.

## Retries

Send the same request again. Users already assigned keep their variant and no second
assignment is recorded:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/assign/batch \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{
    "experiment_key": "spring-email",
    "users": [
      {"user_id": "cust-001", "context": {"country": "US"}},
      {"user_id": "cust-002", "context": {"country": "US"}},
      {"user_id": "cust-003", "context": {"country": "DE"}}
    ]
  }' | jq --argjson before "$RESULT" '.assignments == $before.assignments'
```
<!-- expect: true -->

It prints `true`: the same answers as the first time. A `409` (the experiment was paused or
completed during the request) or a `500` may come after some users were assigned; sending the
same request again is safe for the same reason. Any other `4xx` assigns nobody.

## When they arrive

When a customer reaches your site or app, call `POST /api/v1/tracking/assign` (an SDK's
`get_assignment` / `getAssignment`) as usual. The user gets the same variant, and that call
records that they saw the experiment; the batch route records only the assignment:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/tracking/assign \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"experiment_key": "spring-email", "user_id": "cust-001", "context": {"country": "US"}}' \
  | jq --argjson before "$RESULT" '.variant_id == $before.assignments[0].variant_id'
```
<!-- expect: true -->

It prints `true`.

## More than 1,000 users

Send the list in chunks of 1,000, one request at a time. The route allows 60 requests a minute
from one address; over that it answers `429` with a `Retry-After` header, in seconds, and the
loop below waits that long and sends the same chunk again. It stops at any other error; run it
again from the start, which is safe because assignments are sticky.

This writes 2,500 customer ids to a file in a scratch directory, sends them in three chunks,
and appends each answer to `assignments.csv`:

```{.bash exec timeout=600}
cd "$(mktemp -d)"
seq -f 'cust-%05g' 1 2500 > customers.txt
split -l 1000 customers.txt chunk-
for chunk in chunk-*; do
  BODY=$(jq -R '{user_id: ., context: {country: "US"}}' "$chunk" | jq -s '{experiment_key: "spring-email", users: .}')
  while true; do
    STATUS=$(curl -s -o response.json -D headers.txt -w '%{http_code}' \
      -X POST localhost:8000/api/v1/tracking/assign/batch \
      -H "X-API-Key: $KEY" \
      -H 'content-type: application/json' \
      -d "$BODY")
    if [ "$STATUS" != 429 ]; then
      break
    fi
    sleep "$(tr -d '\r' < headers.txt | awk 'tolower($1) == "retry-after:" {print $2}')"
  done
  if [ "$STATUS" != 200 ]; then
    printf '%s\n' "stopped at $chunk: HTTP $STATUS $(jq -c .detail response.json)"
    break
  fi
  jq -r '.assignments[] | [.user_id, .variant_id, .assigned, .reason] | @csv' response.json >> assignments.csv
done
wc -l < assignments.csv
```
<!-- expect: 2500 -->

It prints `2500`, one line per customer.

- **One chunk at a time.** Chunks sent in parallel share the 60-a-minute limit and the API's
  database connections with every other request.

## What counts in results

Every enrolled user counts in the experiment's results from the moment they are assigned,
whether or not they open the email. If only some of them will ever see the experience, analyse
it as an intent-to-treat experiment, or assign only the users you will contact. For a bandit
experiment, each enrolled user also counts as a pull of their variant.

# Dimensional Analysis & Segment Breakdown (Issue #28)

Dimensional analysis breaks experiment results down by user segments (e.g., by device, country, plan tier, browser) to reveal heterogeneous treatment effects (HTE) — cases where the treatment works differently for different groups.

---

## Overview

When you run an experiment, the top-level result shows the average effect across all users. Dimensional analysis answers: **"Did the effect differ by segment?"**

This is useful for:
- Discovering that a feature performs well for mobile users but poorly for desktop
- Identifying segments where the treatment has a negative effect (safety check)
- Informing rollout decisions (e.g., roll out to the winning segment first)

---

## Statistical Approach

### Bonferroni Correction

When testing multiple segments simultaneously, running standard p-value tests at `α = 0.05` per segment inflates the overall false positive rate. The service applies **Bonferroni correction**:

```text
adjusted_alpha = base_alpha / num_segments
```

For example, testing 10 segments at `α = 0.05` gives an adjusted threshold of `α = 0.005` per segment.

All segment results are marked `is_exploratory: true` — they should be treated as hypothesis-generating, not confirmatory. A significant segment finding should be validated with a dedicated follow-up experiment.

### HTE Detection

Heterogeneous treatment effect detection compares the first treatment (the non-control variant whose name sorts first) with the control. It tests whether that treatment's difference from the control, in percentage points, is the same in every segment, and flags the experiment when the test rejects at 1 minus the confidence level the results were computed at. A segment is tested only when, in both arms, its users times the pooled conversion rate, and times one minus that rate, are at least 10. With fewer than two such segments it never flags, so no flag means either that the segments' effects look alike or that there was too little data to tell. Each dimension is tested on its own, with no correction across dimensions, so the more dimensions you open, the likelier one flags by chance; treat a flag as a lead to confirm with a follow-up experiment.

---

## Where the segments come from

Each user is counted once, in the segment of the value the user was first assigned with,
or else the first value tracked for them, so for every variant the segments add up to the
experiment's users and converting users. The value is a top-level key of the `context`
sent with `POST /api/v1/tracking/assign`, which is stored with the user's assignment
event, or of an event's `metadata`. Assignment events come first, then the user's other
events in the order the server received them; an event's own `timestamp` does not move a
user. So a user assigned with `{"device": "mobile"}` who checks out on `desktop` is
counted once, under `mobile`, with that checkout. A user's conversions count in their
segment whether or not the conversion events carry the key.

Users with no value are counted under `unknown`: users whose events never carry the key,
or carry it as `null` or `""`, and users with no events at all. A value is compared as
exact text, with no trimming or change of case: `iOS` and `ios` are two segments, `true`
is the text `true`, and an object is its JSON text. A value `unknown` is counted under
`unknown`. `POST /api/v1/tracking/assign/batch` records no assignment event, so its
`context` sets no segment; send the key on an event. A breakdown lists the 20 values with
the most users and counts the rest under `other`. A value longer than 255 characters is
counted under other.

To break results down by an attribute, send it in the user's attributes when you assign
them: `context` on the API, `user_attributes` in the Python SDK and `attributes` in the
JavaScript and React SDKs. The events you track then need no properties. The dashboard's
**Breakdowns** tab offers `platform`, `country` and `user_tier`; any other key works
through the API.

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

It prints `"ADMIN"`. The examples use the demo data's `checkout_button_color` experiment,
whose primary metric counts `checkout_completed` events. Its demo users were assigned with
no attributes, so this page assigns a few with a `device`. That takes an API key, which this saves in
`$KEY`, with the experiment's id in `$EXP_ID`. The collection URL ends with a slash,
`/api/v1/experiments/`; without it the API answers `307`, which `curl` doesn't follow:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Segment demo"}' | jq -r .key)
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

echo "${KEY:0:5}"
```
<!-- expect: eptk_ -->

It prints `eptk_`, the start of every API key. This assigns eight users to the
experiment, the odd numbers with the `device` `desktop` and the even ones with `mobile`,
sent in each user's `context`:

```{.bash exec}
{
  for i in 1 3 5 7; do
    curl -s -X POST localhost:8000/api/v1/tracking/assign \
      -H "X-API-Key: $KEY" \
      -H 'content-type: application/json' \
      -d '{"experiment_key": "checkout_button_color", "user_id": "segment-user-'"$i"'", "context": {"device": "desktop"}}' \
      | jq -r .assigned
  done
  for i in 2 4 6 8; do
    curl -s -X POST localhost:8000/api/v1/tracking/assign \
      -H "X-API-Key: $KEY" \
      -H 'content-type: application/json' \
      -d '{"experiment_key": "checkout_button_color", "user_id": "segment-user-'"$i"'", "context": {"device": "mobile"}}' \
      | jq -r .assigned
  done
} | sort | uniq -c
```
<!-- expect: 8 true -->

It prints `8 true`: all eight were enrolled. Then each completes a checkout. The events
carry no metadata: each user's segment comes from their assignment. `jq` builds the batch
of eight events:

```{.bash exec}
jq -n '{events: [range(1; 9) | {
  event_type: "checkout_completed",
  user_id: "segment-user-\(.)",
  experiment_key: "checkout_button_color"
}]}' \
  | curl -s -X POST localhost:8000/api/v1/tracking/batch \
    -H "X-API-Key: $KEY" \
    -H 'content-type: application/json' \
    -d @- | jq .success_count
```
<!-- expect: 8 -->

It prints `8`.

---

## Balanced groups

An experiment that balances its split within groups also answers
`GET /api/v1/results/{experiment_id}/balanced-groups` (beta). Its rows are built the same way,
with each user in the group stored when they were assigned instead of their first value, and it
adds one estimate over the groups.
[Balance the split within groups](../guides/balance-within-groups.md#results-by-group) says what
it returns and where the two differ. On the results page, the **Breakdowns** tab of such an
experiment lists its balanced groups first and opens on them; its other choices are breakdowns
as above.

---

## API Reference

### GET /api/v1/results/{experiment_id}?breakdown={dimension}

The experiment's results, with a `breakdown` field that splits the primary metric by one
dimension, with the Bonferroni-corrected significance test above. Without `breakdown`, the
field is `null`. A user who may view the experiment can read it.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `breakdown` | string | ❌ | The metadata key to break down by (e.g. `device`, `country`, `plan`) |
| `confidence_level` | float | ❌ | 0.80–0.99, default `0.95` |

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID?breakdown=device" \
  -H "Authorization: Bearer $TOKEN" \
  | jq '.breakdown | {dimension, is_exploratory, adjusted_alpha, segments: [.segments[].segment_value]}'
```
<!-- expect: "dimension": "device" -->
<!-- expect: "is_exploratory": true -->
<!-- expect: "desktop" -->
<!-- expect: "mobile" -->
<!-- expect: "unknown" -->

It prints the dimension, `"is_exploratory": true`, the corrected threshold and the
segments: `desktop`, `mobile`, and `unknown`: the demo's own users, assigned with no
`device`. With three segments the threshold is 0.05 / 3, about `0.0167`.

For every variant the segments add up to the experiment's users and converting users:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID?breakdown=device" \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '{users: (([.breakdown.segments[].sample_size] | add) == .summary.total_users),
      conversions: (([.breakdown.segments[].variants[].conversions] | add)
        == ([.metrics[0].variants[].conversions] | add))}'
```
<!-- expect: {"users":true,"conversions":true} -->

It prints `{"users":true,"conversions":true}`.

A variant's `conversions` in a segment are users, as in the rest of `/results`: the
segment's users assigned to it with a conversion event, wherever that event was, each
counted once. Every segment lists every variant of the experiment by name, with zero counts where the
segment has none of its users, and `is_control` is the flag the experiment gives the
variant. The control comes first:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID?breakdown=device" \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '.breakdown.segments[] | select(.segment_value == "desktop") | [.variants[] | {variant_name, is_control}]'
```
<!-- expect: [{"variant_name":"blue_button","is_control":true},{"variant_name":"green_button","is_control":false}] -->

It prints `blue_button`, the control, then `green_button`. One segment in full:

```json
{
  "segment_value": "desktop",
  "sample_size": 4,
  "variants": [
    {
      "variant_id": "34852c9e-05db-4168-b164-d1e8332503d5",
      "variant_name": "blue_button",
      "is_control": true,
      "sample_size": 1,
      "conversions": 1,
      "mean": 1.0,
      "confidence_interval": [0.1486, 1.0],
      "p_value": null,
      "is_significant": false
    },
    {
      "variant_id": "f180194d-b5d7-40bc-abd8-a6b155772845",
      "variant_name": "green_button",
      "is_control": false,
      "sample_size": 3,
      "conversions": 3,
      "mean": 1.0,
      "confidence_interval": [0.3436, 1.0],
      "p_value": 1.0,
      "is_significant": false
    }
  ]
}
```

The control's `p_value` is `null`; every other variant's is Fisher's exact test against the
control in the same segment, the test the headline uses for a conversion rate. The breakdown
also carries `has_heterogeneous_effects` and `hte_warning`.

### GET /api/v1/experiments/{experiment_id}/segmented-results/{segment_by}

Counts per segment, variant and metric, with no significance test. Its segments are the
ones above, by the same rule, `unknown` included; `conversion_rate` is a percentage, and an
experiment in `draft` answers `400`. `metric_id` narrows it to one metric:

```{.bash exec}
curl -s localhost:8000/api/v1/experiments/$EXP_ID/segmented-results/device \
  -H "Authorization: Bearer $TOKEN" \
  | jq '[.segments[] | {segment_value, variants: [.metrics[0].variants[] | {variant_name, sample_size, conversions}]}]'
```
<!-- expect: "segment_value": "desktop" -->
<!-- expect: "variant_name": "blue_button" -->
<!-- expect: "segment_value": "mobile" -->
<!-- expect: "segment_value": "unknown" -->

It prints the `desktop`, `mobile` and `unknown` segments, each with the users and
conversions of `blue_button` and `green_button`.

---

## Results for one saved segment

`GET /api/v1/results/{experiment_id}/segments/{segment_id}` (beta) computes the experiment's
results for the users in one ID-list segment and nobody else. Nobody's assignment changes, and
`GET /api/v1/results/{experiment_id}` answers exactly as before.

A result for a segment is exploratory, like a breakdown. The correction covers the comparisons
inside the segment, not the choice of segment, and each segment you read is another chance of a
false positive. Confirm a finding with an experiment that targets the segment.

A user is counted when their `user_id` is in the list at the time the results are computed,
matched exactly. A user added later is counted, and one removed is not. A list built from
something the experiment could change biases the comparison.

The segment must be an ID list, in any status. An archived list's IDs cannot change while it
stays archived, so its answer stays the same. A rules segment is refused: the attributes its rules
read are stored only with the assignment event of `/tracking/assign`, and `/tracking/assign/batch`
records none.

This creates an ID-list segment, "Desktop testers", with the four desktop users above, and saves
its id in `$DESKTOP`:

```{.bash exec}
DESKTOP=$(curl -s -X POST localhost:8000/api/v1/segments \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Desktop testers", "kind": "id_list"}' | jq -r .id)

curl -s -X POST localhost:8000/api/v1/segments/$DESKTOP/members \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"add": ["segment-user-1", "segment-user-3", "segment-user-5", "segment-user-7"]}' | jq -c .
```
<!-- expect: {"added":4,"already_members":0,"member_count":4} -->

It prints `{"added":4,"already_members":0,"member_count":4}`. The experiment's results for those
four users:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID/segments/$DESKTOP" \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '{segment: (.segment | {name, kind, status, membership, member_count}), is_exploratory, total_users: .summary.total_users}'
```
<!-- expect: {"segment":{"name":"Desktop testers","kind":"id_list","status":"active","membership":"current","member_count":4},"is_exploratory":true,"total_users":4} -->

It prints `{"segment":{"name":"Desktop testers","kind":"id_list","status":"active","membership":"current","member_count":4},"is_exploratory":true,"total_users":4}`.

The four were assigned with the `device` `desktop`, so each variant's users and converting users
are the `desktop` row of the breakdown above:

```{.bash exec}
A=$(curl -s "localhost:8000/api/v1/results/$EXP_ID/segments/$DESKTOP" \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '[.metrics[0].variants[] | {variant_name, sample_size, conversions}] | sort_by(.variant_name)')
B=$(curl -s "localhost:8000/api/v1/results/$EXP_ID?breakdown=device" \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '[.breakdown.segments[] | select(.segment_value == "desktop") | .variants[] | {variant_name, sample_size, conversions}] | sort_by(.variant_name)')
[ "$A" = "$B" ] && echo same
```
<!-- expect: same -->

It prints `same`.

`srm` is the sample ratio check of the segment's users alone. They were assigned by the
allocation like everyone else, so they should split as allocated; a mismatch inside the segment
while all users pass usually means the list depends on something the experiment changed. Its
counts are the segment's users:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID/segments/$DESKTOP" \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '{srm_users: (.srm.observed | add)}'
```
<!-- expect: {"srm_users":4} -->

It prints `{"srm_users":4}`. A rules segment is refused. This creates one on the `device`
`desktop` and asks for its results:

```{.bash exec}
RULES=$(curl -s -X POST localhost:8000/api/v1/segments \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Desktop by rule", "rules": {"logical_operator": "AND", "groups": [{"logical_operator": "AND", "conditions": [{"attribute": "device", "operator": "equals", "value": "desktop"}]}]}}' | jq -r .id)

curl -s "localhost:8000/api/v1/results/$EXP_ID/segments/$RULES" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.detail[0].msg'
```
<!-- expect: This is a rules segment. -->

It prints `This is a rules segment. Results can be computed for the members of an id_list
segment only: the attributes a rules segment matches are not stored for every assigned user.`

| Request | Answer |
|---|---|
| An experiment that does not exist | `404` `Experiment not found` |
| A segment that does not exist | `404` `Segment not found` |
| A rules segment | `422`, its `detail` a list with one entry at `loc` `["path", "segment_id"]`, the text above |
| An id that is not a UUID | FastAPI's own `422` at `["path", "experiment_id"]` or `["path", "segment_id"]`; like every route's, it repeats the value sent |
| A caller without READ on experiments | `403` `You don't have permission to view experiments`, before any of the above |
| Another segment's results are being computed in the same API process | `503` with `Retry-After: 5`: `Another segment's results are being computed. Try again shortly.` |

The answer has the fields of `GET /api/v1/results/{experiment_id}` that describe the results:

- `segment` (`id`, `name`, `kind`, `status`, `membership`, `member_count`, `experiment_users`,
  `experiment_srm_warning`) and `is_exploratory`;
- `metrics`, `summary`, `sample_size_adequate` and `srm`, for the segment's users;
- `summary.recommendation`, computed for them, with the sample-ratio rule applied to their check
  and to the whole experiment's: it is `INCONCLUSIVE` when either warns. Do not automate a decision
  from it;
- no `sequential_testing`, `bayesian_results` or `breakdown`.

Only `GET /api/v1/results/{experiment_id}/segments/{segment_id}` filters by a saved segment. Its
answer always has a `segment` object and `is_exploratory: true`, and never has
`sequential_testing`, `bayesian_results` or `breakdown`. Every other results route,
`GET /api/v1/experiments/{experiment_id}/results` and the export answer for all users; a
`segment_id` query parameter sent to one of them is ignored, not refused. A server older than
0.48.0 answers the segment route with `404` `{"detail": "Not Found"}`.

The answer is computed on each request: it is not cached, and no analysis snapshot is recorded.

---

## Reading the Results

1. **`adjusted_alpha`** is the per-segment significance threshold after Bonferroni correction. A segment is `is_significant: true` only if its p-value is below this threshold.

2. **`is_exploratory: true`** on all segments means treat findings as signals, not conclusions. Run a dedicated follow-up experiment to confirm.

3. **Segments with small sample sizes** (< ~200 per variant) will rarely show significance even with large effects. The p-values are still valid but the confidence intervals will be wide.

---

## Common Dimensions

| Dimension Key | Example Values |
|---------------|---------------|
| `device` | `mobile`, `desktop`, `tablet` |
| `country` | `US`, `GB`, `DE` |
| `plan` | `free`, `pro`, `enterprise` |
| `browser` | `chrome`, `safari`, `firefox` |
| `new_user` | `true`, `false` |

---

## Permissions

Reading either breakdown takes permission to view the experiment, as on the experiment page; every role has it.

Reading the results for a saved segment takes the same permission, which is also the one that reads the segment, so every role can do it.

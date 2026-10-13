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

Heterogeneous treatment effect detection compares the first treatment with the control. It flags the experiment when a chi-squared test on their conversion counts across the segments gives p < 0.05, when the lift varies widely between segments, or when the lift is positive in one segment and negative in another.

---

## Where the segments come from

A segment is a value of a key in the events' `metadata`. Send the dimension with the
events you track, as in `"metadata": {"device": "mobile"}`, and the breakdown groups the
experiment's users by it. A user whose events don't carry the key is counted under the
segment `unknown`.

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
whose primary metric counts `checkout_completed` events. Its demo events carry no
metadata, so this page tracks a few that do. That takes an API key, which this saves in
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
experiment:

```{.bash exec}
for i in 1 2 3 4 5 6 7 8; do
  curl -s -X POST localhost:8000/api/v1/tracking/assign \
    -H "X-API-Key: $KEY" \
    -H 'content-type: application/json' \
    -d '{"experiment_key": "checkout_button_color", "user_id": "segment-user-'"$i"'"}' \
    | jq -r .assigned
done | sort | uniq -c
```
<!-- expect: 8 true -->

It prints `8 true`: all eight were enrolled. Then each completes a checkout, the odd
numbers on `desktop` and the even ones on `mobile`. `jq` builds the batch of eight events:

```{.bash exec}
jq -n '{events: [range(1; 9) | {
  event_type: "checkout_completed",
  user_id: "segment-user-\(.)",
  experiment_key: "checkout_button_color",
  metadata: {device: (if . % 2 == 0 then "mobile" else "desktop" end)}
}]}' \
  | curl -s -X POST localhost:8000/api/v1/tracking/batch \
    -H "X-API-Key: $KEY" \
    -H 'content-type: application/json' \
    -d @- | jq .success_count
```
<!-- expect: 8 -->

It prints `8`.

---

## API Reference

### GET /api/v1/results/{experiment_id}?breakdown={dimension}

The experiment's results, with a `breakdown` field that splits the primary metric by one
dimension, with the Bonferroni-corrected significance test above. Without `breakdown`, the
field is `null`. Any logged-in user can read it.

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

It prints the dimension, `"is_exploratory": true`, the corrected threshold and the
segments: `desktop`, `mobile`, and `unknown` for the users' assignment events, which carry
no `device`. With three segments the threshold is 0.05 / 3, about `0.0167`.

A variant's `conversions` in a segment are users, as in the rest of `/results`: the
users assigned to it with at least one conversion event in that segment, each counted
once. Every segment lists every variant of the experiment by name, with zero counts where the
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

The control's `p_value` is `null`; every other variant's is its test against the control in
the same segment. The breakdown also carries `has_heterogeneous_effects` and `hte_warning`.

### GET /api/v1/experiments/{experiment_id}/segmented-results/{segment_by}

Counts per segment, variant and metric, with no significance test. Only segments with the
key are listed (no `unknown`), `conversion_rate` is a percentage, and an experiment in
`draft` answers `400`. `metric_id` narrows it to one metric:

```{.bash exec}
curl -s localhost:8000/api/v1/experiments/$EXP_ID/segmented-results/device \
  -H "Authorization: Bearer $TOKEN" \
  | jq '[.segments[] | {segment_value, variants: [.metrics[0].variants[] | {variant_name, sample_size, conversions}]}]'
```
<!-- expect: "segment_value": "desktop" -->
<!-- expect: "variant_name": "blue_button" -->
<!-- expect: "segment_value": "mobile" -->

It prints the `desktop` and `mobile` segments, each with the users and conversions of
`blue_button` and `green_button`.

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

Any logged-in user can read both breakdowns, whatever the role.

# Experiment Interaction Detection

Two experiments that run at the same time usually share users. That is normal and
harmless unless they **interact**: one experiment's effect is different depending on
which variant of the other a user sees. This page shows how to measure the overlap and
how to test for an interaction.

- `GET /api/v1/interactions/scan` (stable) lists the pairs of active experiments that
  share many users.
- `GET /api/v1/interactions/{exp_a_id}/{exp_b_id}` (beta) reports the overlap of any two
  experiments and, for each treatment of each one, tests whether its lift on the
  experiment's primary conversion metric differs across the other experiment's variants,
  among the users in both.

The pair route is beta
([#219](https://github.com/Controlvar/controlvar/issues/219)): its responses carry
`"analysis_status": "beta"` and an `analysis_notice`, and the route is marked
`x-stability: beta` in the OpenAPI document. Only each experiment's primary metric is
tested, and only a conversion metric; pairs are tested one at a time, with no correction
across pairs.

An interaction does not mean either experiment is wrong. Each experiment's own result is
still a valid average over the other's current split; it may change once the other
experiment ships one of its variants.

---

## What gets detected

| Issue | Description | In this release |
|-------|-------------|-----------------|
| **User overlap** | Users assigned to both experiments | Measured, on both routes |
| **Interaction** | A treatment's lift, in percentage points, differs across the other experiment's variants | Tested on the pair route (beta) |
| **Novelty effects** | An early effect that decays over time | Not offered |
| **SUTVA violations** | Users in different variants influencing each other | Not offered |

---

## Two overlapping experiments

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

It prints `"ADMIN"`. The demo data's running experiments share no users, so this creates
two experiments that will, and starts them.
This saves their ids in `$PRICING_ID` and `$ONBOARDING_ID`:

```{.bash exec}
PRICING_ID=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Pricing page",
    "key": "pricing-page",
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 50},
      {"name": "annual_first", "traffic_allocation": 50}
    ],
    "metrics": [{"name": "Signup", "event_name": "signup", "is_primary": true}]
  }' | jq -r .id)
ONBOARDING_ID=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Onboarding flow",
    "key": "onboarding-flow",
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 50},
      {"name": "checklist", "traffic_allocation": 50}
    ],
    "metrics": [{"name": "Signup", "event_name": "signup", "is_primary": true}]
  }' | jq -r .id)

for id in $PRICING_ID $ONBOARDING_ID; do
  curl -s -X POST localhost:8000/api/v1/experiments/$id/start \
    -H "Authorization: Bearer $TOKEN" | jq -r .status
done
```
<!-- expect: active -->
<!-- expect: active -->

It prints `active` twice. Then the same 40 users enter both, through the tracking API.
That takes an API key, which this saves in `$KEY`. The key has the `sdk:ruleset` scope,
which the batch assignment below needs; keep such a key on a server:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "Interaction demo", "scopes": ["sdk:ruleset"]}' | jq -r .key)

for i in $(seq 1 40); do
  for experiment in pricing-page onboarding-flow; do
    curl -s -X POST localhost:8000/api/v1/tracking/assign \
      -H "X-API-Key: $KEY" \
      -H 'content-type: application/json' \
      -d '{"experiment_key": "'"$experiment"'", "user_id": "shared-user-'"$i"'"}' \
      | jq -r .assigned
  done
done | sort | uniq -c
```
<!-- expect: 80 true -->

It prints `80 true`: 40 users, each enrolled in both experiments.

---

## GET /api/v1/interactions/scan

Checks every pair of active experiments and returns the pairs whose overlap is above
0.3, with a count of the high-risk ones:

```{.bash exec}
curl -s localhost:8000/api/v1/interactions/scan \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{total_active_experiments, pairs_analyzed, high_risk_pairs, overlaps: [.analyses[].overlap_coefficient]}'
```
<!-- expect: "total_active_experiments": 4 -->
<!-- expect: "pairs_analyzed": 1 -->

It prints four active experiments (the two new ones and the demo data's two running
ones), and one pair above the threshold, with an overlap of `1.0`: the two new
experiments. `high_risk_pairs` counts the pairs whose overlap is above 0.6.

The scan measures overlap only. Each entry in `analyses` has `overlap_coefficient`,
`has_significant_overlap`, `overall_risk` (from the overlap alone, below) and
`recommendations`; its `interaction_result`, `novelty_result` and `sutva_result` are
always `null`, because the scan does not test interactions. To test one pair, call the
pair route.

The scan lists only pairs whose Jaccard overlap is above 0.3, so a small experiment that sits entirely inside a
large one has a low overlap and is not listed. To check one experiment against another,
call the pair route: it answers for any two experiments, however little they overlap.

---

## GET /api/v1/interactions/{exp_a_id}/{exp_b_id}

The overlap of two experiments, in any status, and the interaction test (beta). With only
the 40 shared users, nothing can be tested yet:

```{.bash exec}
curl -s localhost:8000/api/v1/interactions/$PRICING_ID/$ONBOARDING_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '{overlap_coefficient, shared_users, reasons: [.interaction_results[].unavailable_reason]}'
```
<!-- expect: "shared_users":40 -->
<!-- expect: "reasons":["too_few_shared_users","too_few_shared_users"] -->

It prints an overlap of `1` and 40 shared users, and both rows say
`too_few_shared_users`: the 40 users split about 10 per combination of variants, and a
row is tested only when every combination has at least `min_users_per_cell` (100) users.

This enrols 1,000 more visitors in both experiments, in one request per experiment
([Assign a customer list to an experiment](../guides/assign-customer-list.md)), and saves
the two answers in `$PRICING` and `$ONBOARDING`:

```{.bash exec}
PRICING=$(jq -n '{experiment_key: "pricing-page", users: [range(1; 1001) | {user_id: "visitor-\(.)"}]}' \
  | curl -s -X POST localhost:8000/api/v1/tracking/assign/batch \
    -H "X-API-Key: $KEY" \
    -H 'content-type: application/json' \
    -d @-)
ONBOARDING=$(jq -n '{experiment_key: "onboarding-flow", users: [range(1; 1001) | {user_id: "visitor-\(.)"}]}' \
  | curl -s -X POST localhost:8000/api/v1/tracking/assign/batch \
    -H "X-API-Key: $KEY" \
    -H 'content-type: application/json' \
    -d @-)

for r in "$PRICING" "$ONBOARDING"; do jq -c .counts <<<"$r"; done
```
<!-- expect: {"assigned":1000,"holdout":0,"mutual_exclusion":0,"targeting":0} -->
<!-- expect: {"assigned":1000,"holdout":0,"mutual_exclusion":0,"targeting":0} -->

It prints `"assigned":1000` for each experiment. Now plant an interaction: annual-first
pricing lifts signups only for visitors who did not get the checklist, and the checklist
lifts signups whatever the pricing. Signup rates:

- pricing: 30% for `annual_first` visitors who are in onboarding's `control`, 10% for
  everyone else;
- onboarding: 20% for `checklist` visitors, 10% for everyone else.

Each signup is tagged with the experiment it counts for. This joins the two batch answers
by position, picks every visitor's signups, and sends them 100 at a time:

```{.bash exec}
jq -n --argjson p "$PRICING" --argjson o "$ONBOARDING" '
  [range(0; 1000) as $i
   | ($p.variants[$p.assignments[$i].variant_id].name) as $price
   | ($o.variants[$o.assignments[$i].variant_id].name) as $onboard
   | (($i + 1) % 10) as $d
   | (if $price == "annual_first" and $onboard == "control" then $d < 3 else $d < 1 end) as $pricing_signup
   | (if $onboard == "checklist" then $d < 2 else $d < 1 end) as $onboarding_signup
   | (if $pricing_signup then {event_type: "signup", user_id: "visitor-\($i + 1)", experiment_key: "pricing-page"} else empty end),
     (if $onboarding_signup then {event_type: "signup", user_id: "visitor-\($i + 1)", experiment_key: "onboarding-flow"} else empty end)]
  | range(0; length; 100) as $start | {events: .[$start:$start + 100]}' -c \
  | while read -r batch; do
      curl -s -X POST localhost:8000/api/v1/tracking/batch \
        -H "X-API-Key: $KEY" \
        -H 'content-type: application/json' \
        -d "$batch" | jq .success_count
    done | jq -s 'add > 250'
```
<!-- expect: true -->

It prints `true`: more than 250 signups were stored. Ask the pair route again, and print
each row's experiment, treatment and decision:

```{.bash exec}
curl -s localhost:8000/api/v1/interactions/$PRICING_ID/$ONBOARDING_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c --arg p "$PRICING_ID" '.interaction_results[] | [(if .experiment_id == $p then "pricing-page" else "onboarding-flow" end), .variant_name, .is_significant]'
```
<!-- expect: ["pricing-page","annual_first",true] -->
<!-- expect: ["onboarding-flow","checklist",false] -->

It prints `["pricing-page","annual_first",true]`: annual-first pricing's lift differs
across the onboarding variants, as planted. And `["onboarding-flow","checklist",false]`:
the checklist's lift is about the same whatever the pricing. The p-values themselves vary
a little from one stack to another, because which visitor lands in which variant depends
on the order the variants are read in; the two decisions do not.

The pricing row of the response looks like this when the variants are read in the order
they were created (numbers rounded; `arms` shortened to its first entry, onboarding's
`control`: +22.2 percentage points there, -0.7 in `checklist`):

```json
{
  "experiment_id": "8f3b1030-0fa2-4cc5-9842-08650d708925",
  "other_experiment_id": "0ee2eb89-1aba-4e46-bbc6-7ee027e6f0e4",
  "metric_name": "Signup",
  "variant_name": "annual_first",
  "arms": [
    {
      "other_variant_name": "control",
      "n_control": 256,
      "converted_control": 22,
      "n_treatment": 263,
      "converted_treatment": 81,
      "control_rate": 0.0859,
      "treatment_rate": 0.308,
      "effect": 0.2221,
      "relative_lift": 2.584
    }
  ],
  "statistic": 28.08,
  "degrees_of_freedom": 1,
  "p_value": 1.2e-07,
  "corrected_p_value": 1.2e-07,
  "correction_method": "benjamini_hochberg",
  "confidence_level": 0.95,
  "is_significant": true,
  "unavailable_reason": null
}
```

Two experiments with no shared users still answer: the demo data's two running
experiments, looked up by their keys, give one row each with `no_shared_users`:

```{.bash exec}
EXPERIMENTS=$(curl -s localhost:8000/api/v1/experiments/ -H "Authorization: Bearer $TOKEN")
CHECKOUT_ID=$(jq -r '.items[] | select(.key == "checkout_button_color") | .id' <<<"$EXPERIMENTS")
RECOMMENDATIONS_ID=$(jq -r '.items[] | select(.key == "recommendation_algorithm_mab") | .id' <<<"$EXPERIMENTS")

curl -s localhost:8000/api/v1/interactions/$CHECKOUT_ID/$RECOMMENDATIONS_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '{shared_users, has_interaction, reasons: [.interaction_results[].unavailable_reason]}'
```
<!-- expect: "shared_users":0 -->
<!-- expect: "has_interaction":null -->
<!-- expect: "reasons":["no_shared_users","no_shared_users"] -->

`has_interaction` is `null`: not tested, which never means "no interaction".

### What the test measures

For one treatment of experiment A, take the users who are in both experiments, and split
them by A's control or treatment and by B's variant. In each of B's variants the
treatment's **lift** is its conversion rate minus the control's, in **percentage points**
(`effect` in each entry of `arms`; 0.05 is 5 percentage points). The test asks whether
that lift is the same in every variant of B. It fits the model in which it is (one
baseline rate per variant of B plus one common lift) by maximum likelihood, and compares
the counts with that model: `statistic` is Pearson's X² and `degrees_of_freedom` is the
number of B's variants minus one.

Because the lift is measured in percentage points, **a constant relative lift is an
interaction.** A treatment that lifts conversion by 50% everywhere, 10% → 15% in one
variant of B and 20% → 30% in the other, is +5 points in one and +10 in the other. With
2,000 users in each of the four groups that gives X² = 8.46 and p = 0.0036: an
interaction.

### The decision

- `p_value` is the test's p-value, uncorrected.
- `corrected_p_value` applies the tested experiment's stored `correction_method` across
  that experiment's own tested rows (its treatments), as `/results` does. It is `null`
  under `none`.
- `is_significant` is `true` when the corrected p-value (the p-value under `none`) is below
  1 − `confidence_level`, the experiment's stored level.
- `has_interaction` is `true` when any row is significant, `false` only when every row
  was tested and none is, and `null` otherwise.

**"No interaction" with few users is weak evidence.** The test has little power when the
groups are small. The 50% lift example above (10% → 15% and 20% → 30%) is found with 2,000
users in each group (X² = 8.46, p = 0.0036). With 200 users in each group, the same rates
give X² = 0.85 and p = 0.36: not significant, although the interaction is there.

### Rows that are not tested

Every treatment of both experiments has a row; a row that cannot be tested is listed with
an `unavailable_reason` and `null` results, never dropped. The first reason that applies
wins, in this order:

| `unavailable_reason` | Applies to | Meaning |
|---|---|---|
| `mutual_exclusion_group` | the experiment | Both experiments are in the same mutual exclusion group now (`mutual_exclusion_group_id`), whatever the group's status. The overlap is still reported. |
| `no_shared_users` | the experiment | The two experiments share no users. |
| `no_metric` | the experiment | The experiment has no metric. |
| `not_a_proportion_metric` | the experiment | Its primary metric is not a conversion metric. |
| `no_control_variant` | the experiment | It has no control variant. |
| `too_few_shared_users` | one treatment | Fewer than two of the other experiment's variants share users with it, or some combination of variants has fewer than `min_users_per_cell` (100) users. |
| `too_few_conversions` | one treatment | Some combination of variants is expected to have fewer than `min_expected_per_cell` (25) converters or non-converters, at the row's pooled rate. |

A reason that applies to the whole experiment gives one row with `variant_id` `null`.
Below those minimums the test's false-positive rate is not what its p-value says on
uneven splits (95/5, 99/1), so the row is not tested.

### When an interaction is found

Read the lift in each of B's variants in `arms`, and consider deciding B first:
A's result is still a valid average over B's current split, but it may change once B
ships one variant.

The test can also flag an interaction that is not there when one experiment changes
**who enters** the other, for example when B's treatment sends more visitors to A's
page. Check it by comparing, for each of B's variants, the share of its users who are
also in A. In A's row, add `n_control` and `n_treatment` of that variant's entry in
`arms` (for an A with more than one treatment, add each row's `n_treatment` and the
control once), and divide by that variant's `sample_size` in
`GET /api/v1/results/{exp_b_id}`: the primary metric's entry in `metrics`, under
`variants`. Shares that differ between B's variants mean B changed who entered A. A
ramp-up or a bandit changes those shares by design. On the pair above, every user is in
both experiments, so each share is 1:

```{.bash exec}
SIZES=$(curl -s localhost:8000/api/v1/results/$ONBOARDING_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c '[.metrics[] | select(.is_primary) | .variants[] | {(.variant_name): .sample_size}] | add')

curl -s localhost:8000/api/v1/interactions/$PRICING_ID/$ONBOARDING_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq -c --argjson sizes "$SIZES" --arg p "$PRICING_ID" \
    '[.interaction_results[] | select(.experiment_id == $p) | .arms[] | {(.other_variant_name): ((.n_control + .n_treatment) / $sizes[.other_variant_name])}] | add'
```
<!-- expect: {"control":1,"checklist":1} -->

### Errors

| Status | When | `detail` |
|---|---|---|
| 403 | a VIEWER | `Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role.` |
| 404 | an id that matches no experiment | `experiment_a_id does not match an experiment.` (or `experiment_b_id`; `experiment_a_id` when both) |
| 422 | the same id twice | `The two experiments must be different.` |
| 500 | anything else, a database error included | `Could not analyse the two experiments (request ID: <id>).` |

---

## Overlap

Both routes compute the overlap from the experiments' assignments, using Jaccard
similarity:

```text
overlap = |users_in_A ∩ users_in_B| / |users_in_A ∪ users_in_B|
```

The pair route also reports `shared_users`, and `share_of_a` and `share_of_b`: the
fraction of each experiment's users who are also in the other. For a small experiment
inside a large one the overlap is low and `share_of_a` is 1.

`has_significant_overlap` means an overlap above 0.3, not a statistical test.

---

## Risk levels

`/scan`'s `overall_risk` describes how much two experiments share users, not whether
they interact. The pair route does not return it.

| Level | Overlap |
|-------|---------|
| `low` | 0.3 or less |
| `medium` | above 0.3, up to 0.6 |
| `high` | above 0.6 |

---

## Permissions

| Action | Who |
|--------|-----|
| Scan and pair analysis | ADMIN, DEVELOPER or ANALYST |

A VIEWER gets `403` on every interaction endpoint.

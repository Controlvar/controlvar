# Sequential Testing & Early Stopping

Sequential testing lets you continuously monitor experiment results and stop as soon as you have enough evidence — without inflating your false positive rate.

---

## Methods

### mSPRT (mixture Sequential Probability Ratio Test)

The method the analysis uses. Computes a likelihood ratio (`lambda_ratio`) that accumulates evidence for an effect:

- `lambda_ratio >= 1/alpha` → strong evidence for an effect, in either direction (`stop_for_effect`); the direction decides whether to ship (see [Reading a stop decision](#reading-a-stop-decision))
- Otherwise → continue collecting data (`continue`)

The mSPRT has no futility boundary, so the analysis never tells you to stop for
futility. An experiment that is running well past its expected duration is flagged
with the advisory `at_risk`, which is not evidence that there is no effect.

The `always_valid_p_value` can be interpreted like a standard p-value at any point without inflating Type I error.

`lambda_ratio` is computed in log space and capped at the largest finite double
(about 1.8 × 10³⁰⁸), so an overwhelming difference, such as 10% against 11% with
ten million users per arm, still reports a finite number and can stop.

### Always-Valid Confidence Intervals (Confidence Sequences)

A confidence sequence is a confidence interval that is valid at every sample size simultaneously. The interval shrinks as more data is collected.

It is the same mSPRT turned into an interval (the normal-mixture confidence sequence
of Johari et al. 2017 and Howard et al. 2021), on the difference in conversion rates
(treatment minus control), with the same alpha and `tau_squared`:

```text
estimate ± sqrt( V (V + τ²) / τ² · (2 ln(1/α) + ln((V + τ²) / V)) )
```

where the estimate is the observed difference in rates and V is the Agresti-Caffo
variance of that difference: one conversion and one non-conversion are added to each
arm, `p̃ = (conversions + 1) / (users + 2)`, and
`V = p̃_c (1 − p̃_c) / (n_c + 2) + p̃_t (1 − p̃_t) / (n_t + 2)`. The mSPRT uses the
same V. Because the interval and the stop decision share V, τ² and α, they cannot
disagree: 0 is outside the interval exactly when `can_stop` is `true`. At a 10%
conversion rate, α = 0.05 and the default τ² = 0.001, the half-width is about 0.041
at 1,000 users per arm, 0.0136 at 10,000 and 0.00055 at 10 million. A difference in rates cannot leave `[-1, 1]`, so the interval is clipped to
that range: with 10 users per arm at a 50% rate the formula gives about ±3.27, and
the response reports `[-1, 1]`. Clipping changes neither the stop decision nor
coverage, because 0 and the true difference always lie inside `[-1, 1]`. While an
arm has no users, nothing bounds the effect: the interval is `[-1, 1]`, the whole
range of a difference in rates, and `can_stop` is `false`. An arm in which every
user has the same outcome (nobody converts, or everybody does) still gets a finite
interval: 0 of 500 against 0 of 500 gives about ±0.0093 around 0 and cannot stop,
while 0 of 500 against 500 of 500 can.

### Alpha Spending (not computed yet)

A group-sequential design with planned interim analyses (O'Brien-Fleming or Pocock
alpha spending) is not implemented: `alpha_spending` is
always an empty list. `spending_function` and `planned_looks` are still accepted and
stored. The mSPRT above stays valid however often you read the results, so no
planned-looks table is needed to use it.

---

## Turning it on

Sequential testing is set on the experiment, when you create it
(`POST /api/v1/experiments/`) or change it (`PUT /api/v1/experiments/{id}`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sequential_testing_enabled` | bool | `false` | Turns the analysis on |
| `sequential_testing_config.method` | `msprt` \| `always_valid` | `msprt` | `always_valid` is an alias of `msprt`: the analysis is mSPRT with a confidence sequence, reports `"method": "msprt"`, and says so in `analysis_notice` |
| `sequential_testing_config.alpha` | float | `0.05` | The significance level, above 0 and at most 0.2. The stopping boundary is 1/alpha. A value outside the range is refused with `422` |
| `sequential_testing_config.tau_squared` | float | `0.001` | The mSPRT mixing parameter, above 0 and at most 1 |
| `sequential_testing_config.spending_function` | `obrien_fleming` \| `pocock` | `obrien_fleming` | Accepted and stored; no alpha-spending table is computed yet |
| `sequential_testing_config.planned_looks` | int | `10` | Accepted and stored (1–100); no alpha-spending table is computed yet |

The sequential analysis uses its own `alpha`. It does not follow the experiment's
stored `confidence_level` or `correction_method`, which judge the fixed-horizon results
([How results are judged](endpoints.md#how-results-are-judged)).

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
which is running and has data. This saves its id in `$EXP_ID` and turns sequential testing
on with five planned looks.

```{.bash exec}
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

curl -s -o /dev/null -w '%{http_code}\n' -X PUT localhost:8000/api/v1/experiments/$EXP_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "sequential_testing_enabled": true,
    "sequential_testing_config": {"spending_function": "obrien_fleming", "planned_looks": 5}
  }'
```
<!-- expect: 200 -->

It prints `200`.

---

## API Reference

### GET /api/v1/results/{experiment_id}/sequential

Returns the full sequential analysis for an experiment, on its primary metric: the control
against the treatment created first, so an experiment with three or more variants compares
only that one, and the same one on every call. It takes
one optional query parameter, `alpha`: the significance level for this request, above 0
and at most 0.2, which overrides the stored `sequential_testing_config.alpha` (default
`0.05`); a value outside the range answers `422`. Any logged-in user can read it. It answers `404` with
`"Sequential testing is not enabled for this experiment"` when it is off, and
`"Experiment not found"` when there is no such experiment.

```{.bash exec}
curl -s localhost:8000/api/v1/results/$EXP_ID/sequential \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{method, boundary: .msprt_result.boundary, evidence_strength: .msprt_result.evidence_strength, recommended_action, at_risk, alpha_spending, analysis_status}'

curl -s "localhost:8000/api/v1/results/$EXP_ID/sequential?alpha=0.01" \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{boundary_at_alpha_001: .msprt_result.boundary}'
```
<!-- expect: "method": "msprt" -->
<!-- expect: "boundary": 20 -->
<!-- expect: "evidence_strength": -->
<!-- expect: "recommended_action": -->
<!-- expect: "at_risk": -->
<!-- expect: "alpha_spending": [] -->
<!-- expect: "analysis_status": "beta" -->
<!-- expect: "boundary_at_alpha_001": 100 -->

It prints `"method": "msprt"`, the boundary `20.0` (that is 1/α at the default α of
0.05), the strength of the evidence so far, the recommended action, the advisory
`at_risk`, an empty `alpha_spending` and `"analysis_status": "beta"`. The second request
asks for α = 0.01, so its boundary is `100.0`. A full response, abridged:

```json
{
  "method": "msprt",
  "msprt_result": {
    "lambda_ratio": 5.73,
    "always_valid_p_value": 0.174,
    "can_stop": false,
    "evidence_strength": "inconclusive",
    "boundary": 20.0
  },
  "confidence_sequence": {
    "lower": -0.0015,
    "upper": 0.0215,
    "width": 0.0230,
    "sample_size": 30000
  },
  "evidence_trajectory": [
    {"sample_size": 30000, "lambda_ratio": 5.73, "always_valid_p_value": 0.174, "can_stop": false}
  ],
  "alpha_spending": [],
  "long_running_risk": {
    "is_at_risk": false,
    "expected_duration_days": 30,
    "actual_duration_days": 14,
    "risk_ratio": 0.467,
    "recommendation": "Experiment is on track. No action needed."
  },
  "recommended_action": "continue",
  "at_risk": false,
  "analysis_status": "beta",
  "analysis_notice": "Beta: the stop/continue decision is mSPRT alone, at the significance level shown by the boundary (1/alpha). alpha_spending is always empty: the planned-looks (alpha-spending) table is not computed yet. https://github.com/Controlvar/controlvar/issues/232"
}
```

`analysis_status` is `"beta"` while part of the analysis is not computed yet (here the
alpha-spending table), and
`analysis_notice` then says what; when the status is `"ga"` the notice is `null`. If the
experiment's method is stored as `always_valid`, the notice adds that it is an alias of
`msprt`.

**Evidence Strength Values**

| Value | Meaning |
|-------|---------|
| `strong_for_effect` | Clear effect detected, better or worse: the sign of the `confidence_sequence` decides whether to ship (see [Reading a stop decision](#reading-a-stop-decision)) |
| `moderate_for_effect` | Trending toward an effect, in either direction — consider continuing for confirmation |
| `inconclusive` | Not enough data yet — continue |
| `moderate_for_null` | Trending toward no effect |
| `strong_for_null` | Evidence leans strongly toward no effect. Not a stopping rule |

**Recommended Actions**

| Value | Meaning |
|-------|---------|
| `stop_for_effect` | The mSPRT crossed its boundary (1/alpha): stop, there is an effect, which may be an improvement or a harm |
| `continue` | Keep running — not enough evidence yet |
| `stop_for_futility` | Kept in the value set, but not returned by the current analysis: the mSPRT has no futility boundary. A long-running experiment is flagged with `at_risk` instead |

#### Reading a stop decision

The mSPRT is a two-sided test: `stop_for_effect` and `strong_for_effect` mean the
effect is not zero, whichever way it points. A treatment that is worse than the
control crosses the boundary exactly as a better one does. The response has no separate
"worse" value, so read the direction from the `confidence_sequence`, which is the
difference in rates, treatment minus control:

| `confidence_sequence` | Metric where higher is better | Metric where lower is better (`lower_is_better`) |
|-----------------------|-------------------------------|---------------------------------------------------|
| `lower` and `upper` both above 0 | Better: shipping is justified | Worse: do not ship |
| `lower` and `upper` both below 0 | Worse: do not ship | Better: shipping is justified |
| the interval includes 0 | The direction is not clear; this should not happen with `stop_for_effect` | Same |

For example, 10% against 13% and 10% against 7% at 10,000 users per arm both answer
`stop_for_effect` with `strong_for_effect`. The first has a `confidence_sequence` of
0.0157 to 0.0443, the second -0.0427 to -0.0173 (at the default α and τ²; they are not
mirror images, because the variance is larger at 13%). With a higher-is-better
metric the first is a win and the second is a harm. The dashboard's banner follows this
table for a conversion metric: it reads "Ready to stop" in green only for a change in the
better direction, "Stop — the variant is significantly worse" in red for the other, and a
neutral message when the interval includes 0.

**Only for conversion metrics.** The analysis counts the users who sent the metric's
event (the same count the results use for a conversion), whatever the metric's
`metric_type`, so the `confidence_sequence` is always a difference in the share of users
who sent the event. For a conversion metric that share is the metric. For a `duration`,
`count`, `revenue` or `custom` metric it is not: with a lower-is-better metric such as a
page load time, fewer treatment users sending the event can mean the page broke, not that
it got faster. The table above therefore does not apply to those metrics, and the
dashboard's banner does not judge their direction: it shows "Significant change detected
— direction unknown" and leaves the call to you.

### In the results response

`GET /api/v1/results/{experiment_id}` and `GET /api/v1/experiments/{experiment_id}/results`
carry the same analysis as `sequential_testing`, computed by the same code as
`/sequential` without `alpha`: the primary metric, the control against the treatment
created first, at the stored `sequential_testing_config.alpha`. The `alpha` parameter
exists only on `/sequential`, and the results routes' `confidence_level` and
`correction_method` do not change this block. It is `null` when sequential testing is
off. It is also `null` when the analysis could not be computed: the API logs a warning
and the rest of the response is unaffected.

It is cached with the rest of the results: for up to 5 minutes for a running experiment
and 24 hours otherwise. A change to `sequential_testing_enabled` or
`sequential_testing_config`, which only a superuser can make once the experiment is
running (see [Permissions](#permissions)), therefore shows here only when the cached
answer expires or
`POST /api/v1/results/{experiment_id}/invalidate-cache` clears it, not immediately;
`/sequential` is not cached. The request below passes `use_cache=false` for that reason:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID?use_cache=false" \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{embedded_method: .sequential_testing.method, embedded_boundary: .sequential_testing.msprt_result.boundary}'
```
<!-- expect: "embedded_method": "msprt" -->
<!-- expect: "embedded_boundary": 20 -->

It prints `"embedded_method": "msprt"` and the boundary `20.0`, the same as the first
`/sequential` request above.

---

## Permissions

Changing an experiment's sequential testing
settings is a change to the experiment, decided by role and by the experiment's state, not
by who created it. ANALYST and VIEWER cannot change any experiment: the `PUT` above answers
`403` with `"You don't have permission to update experiments"`. ADMIN and DEVELOPER may
change experiments, but only a superuser may change one that is no longer a draft, and the
demo experiment is running: for a DEVELOPER (or an ADMIN who is not a superuser) the `PUT`
above answers `400` with `"Cannot update experiments in active status"`. The demo's
`admin@demo.com` is a superuser, which is why it gets `200`.

---

## Common Mistakes

**Long-running risk is informational only.** An experiment flagged `at_risk` (the same value as `long_running_risk.is_at_risk`) may still be producing valid results. It means the experiment is running well past its expected duration or collecting samples slowly, not that there is no effect.

**Do not change the configuration mid-experiment.** Choose `alpha` and `tau_squared`
before the experiment starts. Once it is running, the `PUT` that would change
`sequential_testing_enabled` or `sequential_testing_config` answers `400` with
`"Cannot update experiments in active status"` for an ADMIN or DEVELOPER who is not a
superuser.

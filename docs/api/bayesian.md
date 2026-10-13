# Bayesian Experimentation API

When an experiment has Bayesian analysis turned on, its results carry a Bayesian block
beside the frequentist statistics: a posterior for each variant, the probability that
each is best, the expected loss of choosing it, and a stopping recommendation.

---

## Overview

The platform fits a **Beta-Binomial model** to each variant's conversions on the
experiment's primary metric. A conversion is a user: the users assigned to the variant
with at least one conversion event, so a user who purchases three times counts once, as
in `GET /results/{id}`.

- **Posterior** parameters (`alpha`, `beta`), updated from the prior with the observed
  conversions and non-conversions, and the posterior mean.
- **Credible interval** (equal-tailed: at 95%, the 2.5th and 97.5th percentiles of the
  posterior) at the configured level, 95% by default.
- **Probability to be best**: the share of Monte Carlo draws in which this variant has the
  highest rate.
- **Expected loss**: how much conversion rate you would give up, on average, by choosing
  this variant if it is not the best.
- **A decision** from the stopping rules below.

The Monte Carlo seed is derived from the experiment id, the UTC day and the number of
samples, and echoed in the response as `seed`, so two requests on the same day return the
same numbers.

---

## Reading the Bayesian results

### GET /api/v1/results/{experiment_id}/bayesian

Returns the Bayesian block on its own, always freshly computed. Any logged-in user can
read it. An experiment that hasn't turned Bayesian analysis on answers `200` with
`"is_enabled": false`; an experiment that doesn't exist answers `404`.

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

It prints `"ADMIN"`. This reads the Bayesian block of the demo data's
`checkout_button_color` experiment, saving its id in `$EXP_ID` first:

```{.bash exec}
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

curl -s localhost:8000/api/v1/results/$EXP_ID/bayesian \
  -H "Authorization: Bearer $TOKEN" | jq '{is_enabled, decision, variant_results}'
```
<!-- expect: "is_enabled": false -->
<!-- expect: "decision": null -->

It prints `"is_enabled": false`, with no decision and no variant results, because the
demo experiment hasn't turned Bayesian analysis on.

`GET /api/v1/results/{experiment_id}` embeds the same block as `bayesian_results`, which is
`null` when the experiment hasn't turned it on.

---

## Turning Bayesian analysis on

Set `bayesian_enabled` to `true` on `POST /api/v1/experiments/` or
`PUT /api/v1/experiments/{experiment_id}`, and optionally a `bayesian_config`. In the dashboard,
the new-experiment form's **Also analyse the primary metric with Bayesian statistics** checkbox
sends `bayesian_enabled: true` with no config. Turning it on
without a config stores the defaults in the table below. This creates an experiment with
Bayesian analysis on, prints the stored settings, and saves its id in `$BAYES_ID`:

```{.bash exec}
BAYES=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Checkout Button Bayesian Test",
    "hypothesis": "Green button increases conversion rate",
    "bayesian_enabled": true,
    "bayesian_config": {
      "prior_family": "beta",
      "alpha": 1.0,
      "beta": 1.0,
      "loss_threshold": 0.001,
      "rope": [-0.005, 0.005],
      "credible_level": 0.95
    },
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 50},
      {"name": "treatment", "traffic_allocation": 50}
    ],
    "metrics": [
      {"name": "Checkout", "event_name": "checkout_completed", "is_primary": true}
    ]
  }')
BAYES_ID=$(echo "$BAYES" | jq -r .id)

echo "$BAYES" | jq '{bayesian_enabled, bayesian_config, bayesian_decision}'
```
<!-- expect: "bayesian_enabled": true -->
<!-- expect: "loss_threshold": 0.001 -->
<!-- expect: "bayesian_decision": null -->

It prints `"bayesian_enabled": true`, the stored config with `"loss_threshold": 0.001`,
and `"bayesian_decision": null`, because nothing has been analysed yet. The experiment
response carries all three fields. `bayesian_decision` is read-only: a request that sends
it is answered as if it had not.

**`bayesian_config` Object**

| Field | Type | Default | Description |
|---|---|---|---|
| `prior_family` | `string` | `beta` | Stored only: `beta`, `normal` or `gamma` are accepted, and every config is analysed as Beta-Binomial |
| `alpha` | `float` | `1.0` | Prior alpha (> 0) |
| `beta` | `float` | `1.0` | Prior beta (> 0) |
| `loss_threshold` | `float` | `0.001` | Expected loss the leading variant must be below to be declared the winner (> 0); it also needs a probability to be best of at least 0.975 (see [the decision values](#bayesiandecision-values)) |
| `rope` | `[float, float]` | `null` | Region of Practical Equivalence, `[lower, upper]` with `lower < upper`, on the difference in conversion rate |
| `credible_level` | `float` | `0.95` | Credible interval level, between 0 and 1 |

A config that fails these checks is refused with `422`, and the error's `loc` names the
field, for example `["body", "bayesian_config", "alpha"]`. So is a `PUT` that sends
`"bayesian_config": null` while Bayesian analysis stays on; to turn it off and clear the
config, send `"bayesian_enabled": false` with it.

---

## An enabled experiment's results

For an experiment with Bayesian analysis on, the endpoint answers with a result per
variant. This reads the one just created:

```{.bash exec}
curl -s localhost:8000/api/v1/results/$BAYES_ID/bayesian \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{is_enabled, variants: [.variant_results[].variant_key]}'
```
<!-- expect: "is_enabled": true -->
<!-- expect: "control" -->
<!-- expect: "treatment" -->

It prints `"is_enabled": true` and one result for each variant, `"control"` and
`"treatment"`. The new experiment has no traffic yet, so each posterior is still the
prior. With traffic, the full response looks like this; the numbers are illustrative:

```json
{
  "is_enabled": true,
  "decision": "CONTINUE",
  "variant_results": [
    {
      "variant_key": "control",
      "posterior": {
        "alpha": 598.0,
        "beta": 4224.0,
        "mean": 0.1240,
        "credible_interval_lower": 0.1148,
        "credible_interval_upper": 0.1334
      },
      "probability_to_be_best": 0.0269,
      "expected_loss": 0.0148,
      "bayes_factor": null
    },
    {
      "variant_key": "treatment",
      "posterior": {
        "alpha": 666.0,
        "beta": 4131.0,
        "mean": 0.1388,
        "credible_interval_lower": 0.1291,
        "credible_interval_upper": 0.1488
      },
      "probability_to_be_best": 0.9731,
      "expected_loss": 0.0002,
      "bayes_factor": null
    }
  ],
  "seed": 1873460932,
  "n_samples": 100000,
  "engine_version": "1.2.0"
}
```

---

## `BayesianDecision` values

The rules are checked in this order:

| Value | When |
|---|---|
| `STOP_WINNER` | The variant most likely to be best has a probability to be best of at least **0.975**, and its expected loss is below `loss_threshold`. |
| `STOP_EQUIVALENT` | `rope` is set, and for every other variant the probability that its difference from the best lies inside the ROPE is above 0.95. |
| `CONTINUE` | Neither of the above. Keep collecting data. |

The decision is a recommendation: nothing stops the experiment or changes its status.
`STOP_FUTILE` is defined but no rule returns it.

The example response above answers `CONTINUE`: the treatment's expected loss, 0.0002, is
below the default `loss_threshold`, but its probability to be best, 0.9731, is under 0.975.

### Reading the decision

- **Read the decision once, at the planned sample size.** Like a fixed-horizon p-value,
  the decision assumes a single look. For an
  experiment you want to monitor continuously, use the sequential analysis
  ([`GET /results/{id}/sequential`](sequential-testing.md)), which is built for repeated
  looks.
- **The 0.975 threshold is deliberately not configurable.** `bayesian_config` has no field
  for it.
- A smaller `loss_threshold` makes the
  rule stricter; a larger one cannot make it looser than the 0.975 probability allows.

---

## Error Responses

| Status | Meaning |
|---|---|
| `401 Unauthorized` | Missing or invalid bearer token |
| `404 Not Found` | The experiment doesn't exist |
| `500 Internal Server Error` | The computation failed; `detail` is `Could not compute the Bayesian results (request ID: <id>).`, where `<id>` is the response's `X-Request-ID`, and the full error is in the server log under that ID |

# CUPED Variance Reduction

CUPED (Controlled-experiment Using Pre-Experiment Data) narrows the interval around a
treatment effect by adjusting for what each user did before the experiment. How much it
narrows depends on how well that history predicts the outcome. The endpoint is
`GET /api/v1/results/{experiment_id}/cuped`; the dashboard and `GET /results/{id}` show the
unadjusted numbers.

Reference: Deng, Xu, Kohavi & Walker — *Improving the sensitivity of online controlled experiments by utilizing pre-experiment data* (WSDM 2013).

---

## How It Works

For each conversion metric, the outcome `Y` of a user is 1 if they converted after
assignment (counted exactly as `GET /results/{id}` counts them) and 0 otherwise. The
covariate `X` is 1 if the same user sent the metric's event in the `covariate_lookback_days`
before their assignment, and 0 if they did not. A user with no history stays in the
analysis with `X = 0`.

Each arm's adjusted conversion rate is:

```text
Y_adjusted = mean(Y) - θ × (mean(X) - overall mean(X))
```

Where:
- `θ` is the slope of `Y` on `X`, pooled within arms over every arm of the experiment: the
  `x` coefficient of the regression `Y ~ arm + X`
- `overall mean(X)` is the share of all assigned users with history, so the adjusted rates
  stay on the conversion-rate scale

The adjusted effect is the treatment's adjusted rate minus the control's. Its variance is
each arm's residual variance over its size, so the more of the outcome the history
explains, the narrower the interval. Each residual variance is a sample variance, divided
by the arm's users minus 1.

What counts as history, per user:

- the event is the metric's own `event_name` (or that of `covariate_metric_id`, below),
  matched the way results match conversions: an experiment-view row never counts;
- it happened in `[assigned_at − covariate_lookback_days, assigned_at)`: an event at the
  moment of assignment is not history;
- it can carry any experiment key, any flag key, or none. One event is enough; a user with
  three copies of the same purchase still has `X = 1`;
- **History counts only if the server received it before the user was assigned. Send
  history before you start the experiment. History the server receives after a user was
  assigned is ignored for that user.**

---

## Sending pre-experiment history

Send each user's past events to `POST /api/v1/tracking/track`, or 100 at a time to
`POST /api/v1/tracking/batch`, **without** an `experiment_key` or a `feature_flag_key`. Such
an event is stored as history: it counts in no experiment's results, only as CUPED history.
For each event:

- `user_id` is exactly the id the user will be assigned with (it is case-sensitive);
- `event_type` is the metric's `event_name`, for example `checkout_completed`;
- `timestamp` is when the event happened, in ISO 8601 (no offset means UTC). Without it the
  event is dated when it is received.

Send events from at least `covariate_lookback_days` before the planned start; anything older
is never read.

Events during the experiment still need the experiment's key, as they always have: an event
without one counts in no experiment's results.

The SDKs' `track()` without a key sends one event for each experiment and flag that SDK
answered for the user (in the last 24 hours for the JavaScript, Python and React SDKs, while
the answer is cached for the others), and those tagged events count as history for a later
experiment. **With nothing to attribute it to, the JavaScript, Python and React SDKs send the
event once with no key**, which stores it as history; the other SDKs send nothing then, which
is usual before a user's first assignment. Each result's `covariate_coverage_pct` shows how
many users had history.

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
whose primary metric counts `checkout_completed` events. The demo data already gives about
one user in ten a `checkout_completed` before their assignment. Sending an event takes an
API key, which this saves in `$KEY`, with the experiment's id in `$EXP_ID`; then it sends one
history event with no key:

```{.bash exec}
KEY=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "CUPED history"}' | jq -r .key)
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

curl -s -X POST localhost:8000/api/v1/tracking/track \
  -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"event_type": "checkout_completed", "user_id": "returning-shopper-1", "timestamp": "2026-09-01T09:30:00Z"}' \
  | jq '{event_name, experiment_id}'
```
<!-- expect: "experiment_id": null -->

It prints `"experiment_id": null`: the event is stored as history, tagged with no
experiment.

---

## Choosing the method

The method is part of the experiment, in its `variance_reduction_config`, set when you
create it (`POST /api/v1/experiments/`) or change it (`PUT /api/v1/experiments/{id}`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `method` | string | `none` | `none`, `cuped`, `cuped_plus` or `winsorization` |
| `covariate_metric_id` | string | `null` | The id of one of the experiment's metrics whose `event_name` is the history to read. By default each metric's own `event_name` |
| `covariate_lookback_days` | int | `7` | Days of history before each user's assignment, 1–90 |
| `winsorization_percentile` | float | `99.0` | The percentile above which values are clipped, 50–100 |

- `none` applies no adjustment: `theta` is `0` and the numbers are the unadjusted ones.
- `cuped` adjusts as described above.
- `cuped_plus` is currently computed as `cuped`: the response's top-level `method` says
  `cuped_plus`, and each row's `method` says `cuped`.
- `winsorization` clips values above a percentile, which only means something for a mean
  metric (revenue, say). This view does not apply it: a conversion metric is listed with
  `unavailable_reason: "winsorization_needs_mean_metric"`.

This sets the demo experiment's method to `cuped` with a 7-day lookback:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' -X PUT localhost:8000/api/v1/experiments/$EXP_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"variance_reduction_config": {"method": "cuped", "covariate_lookback_days": 7}}'
```
<!-- expect: 200 -->

It prints `200`.

---

## API Reference

### GET /api/v1/results/{experiment_id}/cuped

Returns one comparison per conversion metric and treatment, each against the control. It
takes no query parameters: the method comes from the experiment, the interval uses the
experiment's stored `confidence_level`, and `corrected_p_value` applies its stored
`correction_method` across the metric's treatments
([How results are judged](endpoints.md#how-results-are-judged)). Like the main results, the
view is fixed-horizon: do not stop an experiment the first time it looks significant. Any
logged-in user can read it; an experiment that doesn't exist answers `404`.

```{.bash exec}
curl -s localhost:8000/api/v1/results/$EXP_ID/cuped \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{analysis_status, method, metrics: [.metrics[] | {metric_name, variant_name, covariate_coverage_pct, variance_reduction_pct, adjusted_effect, corrected_p_value}]}'
```
<!-- expect: "analysis_status": "ga" -->
<!-- expect: "method": "cuped" -->
<!-- expect: "variant_name": "green_button" -->

It prints `"analysis_status": "ga"`, `"method": "cuped"` and one comparison, for
`green_button` against `blue_button`. This checks that the history narrowed the interval:

```{.bash exec}
curl -s localhost:8000/api/v1/results/$EXP_ID/cuped \
  -H "Authorization: Bearer $TOKEN" \
  | jq '.metrics[0] | .variance_reduction_pct > 0 and .covariate_coverage_pct > 0'
```
<!-- expect: true -->

It prints `true`. The full response:

```json
{
  "experiment_id": "cc748188-56cb-4068-ae84-bd69d1c4a64e",
  "method": "cuped",
  "confidence_level": 0.95,
  "correction_method": "benjamini_hochberg",
  "covariate_lookback_days": 7,
  "metrics": [
    {
      "metric_id": "ca34ed90-3fda-402d-9cb0-9511e69574a8",
      "metric_name": "Checkout Completion",
      "variant_id": "da92fe7a-e520-4911-a3da-2aa644bc01fc",
      "variant_name": "green_button",
      "control_variant_id": "31986f72-fe9c-4140-9b55-e9408717376d",
      "control_sample_size": 15000,
      "treatment_sample_size": 15000,
      "adjusted_control_mean": 0.0799,
      "adjusted_treatment_mean": 0.0885,
      "adjusted_effect": 0.0086,
      "adjusted_se": 0.0030,
      "adjusted_p_value": 0.0042,
      "adjusted_ci_lower": 0.0027,
      "adjusted_ci_upper": 0.0145,
      "unadjusted_effect": 0.0091,
      "unadjusted_se": 0.0032,
      "corrected_p_value": 0.0042,
      "is_significant": true,
      "variance_reduction_pct": 12.69,
      "theta": 0.331,
      "covariate_event_name": "checkout_completed",
      "covariate_coverage_pct": 9.89,
      "unavailable_reason": null,
      "method": "cuped"
    }
  ],
  "computed_at": "2026-10-04T14:54:13.935435+00:00",
  "seed": null,
  "n_samples": null,
  "engine_version": "1.3.0",
  "analysis_status": "ga",
  "analysis_notice": null
}
```

**Response Fields**

| Field | Description |
|-------|-------------|
| `method` (top level) | The method the experiment is configured with |
| `confidence_level`, `correction_method` | The experiment's stored settings the numbers use |
| `covariate_lookback_days` | Days of history before each assignment that were read |
| `variant_id`, `variant_name`, `control_variant_id` | The treatment, and the control it is compared with |
| `control_sample_size`, `treatment_sample_size` | Users assigned to each, the same as `GET /results/{id}` |
| `adjusted_control_mean`, `adjusted_treatment_mean` | The two conversion rates, adjusted for history |
| `adjusted_effect`, `adjusted_se` | Treatment minus control, adjusted, and its standard error |
| `adjusted_ci_lower`, `adjusted_ci_upper` | The adjusted effect's interval at `confidence_level` |
| `adjusted_p_value` | Two-sided p-value of the adjusted effect, before any correction |
| `corrected_p_value` | `adjusted_p_value` after `correction_method`, across the metric's treatments; `null` for `none` |
| `is_significant` | `corrected_p_value` (or `adjusted_p_value` when there is none) is below `1 − confidence_level` |
| `unadjusted_effect`, `unadjusted_se` | The same comparison without CUPED |
| `variance_reduction_pct` | `100 × (1 − adjusted variance / unadjusted variance)` of the effect. At *r* % the interval is √(1 − *r*/100) as wide: 36% means 80% as wide, worth about 1.56 times the users |
| `theta` | The slope of the outcome on history, pooled within arms. `0` when no user (or every user) has history, and for `none` |
| `covariate_event_name` | The event read as history; `null` when none is read |
| `covariate_coverage_pct` | Share of this comparison's users (control and treatment), 0–100, with history |
| `unavailable_reason` | `null`, or why the comparison has no numbers (below) |
| `method` (each row) | The method actually computed: `cuped` when `cuped_plus` is configured |
| `analysis_status` | `ga`: the numbers are computed as described. `analysis_notice` is `null` |

In `GET /results/{id}`, `adjusted_p_value` is the p-value after the multiple-comparison
correction; here it is the p-value of the CUPED-adjusted effect before any correction, and
the corrected one is `corrected_p_value`.

Three things can surprise:

- **`variance_reduction_pct` can be negative.** `θ` is one slope pooled over every arm. If
  an arm's history relates to its outcome differently from the others' (a small arm, say),
  its adjusted variance can come out larger than its unadjusted one. The estimate is still
  valid; CUPED simply did not help that comparison.
- **Adding an arm changes the other arms' adjusted effects.** Because `θ` is pooled over
  every arm, a new treatment's users move `θ`, and with it every comparison's adjusted
  numbers. The unadjusted numbers do not move.
- **Coverage near 0 gives the unadjusted numbers.** With no history, `θ` is `0`,
  `variance_reduction_pct` is `0` and the adjusted effect equals the unadjusted one. That is
  not an error.

**`unavailable_reason`**

| Value | Meaning |
|-------|---------|
| `not_a_proportion_metric` | The metric is not a conversion metric; this view computes conversion metrics only |
| `winsorization_needs_mean_metric` | The method is `winsorization`, which this view does not apply to a conversion metric |
| `fewer_than_2_units` | The control or this treatment has fewer than 2 assigned users |
| `no_variation` | Neither arm's outcome varies (in each arm nobody, or everybody, converted): there is no standard error to compute |
| `no_residual_variation` | History predicts every user's outcome exactly in both arms, so the adjusted standard error would be 0 and no p-value can be given |
| `no_control_variant` / `no_treatment_variant` | The experiment has no control, or no treatment; the row has no variant |
| `covariate_metric_not_found` | `covariate_metric_id` is not one of the experiment's metrics (after a `PUT` replaced them, say) |
| `metric_has_no_event_name` | The metric whose event is the history has no `event_name` |
| `result_invalid` | The counts were ones no sample could produce |

---

## Permissions

Any logged-in user can read the results. Changing an experiment's
`variance_reduction_config` is a change to the experiment, decided by role and by the
experiment's state, not by who created it. ANALYST and VIEWER cannot change any experiment:
the `PUT` above answers `403` with `"You don't have permission to update experiments"`.
ADMIN and DEVELOPER may change experiments, but only a superuser may change one that is no
longer a draft, and the demo experiment is running: for a DEVELOPER (or an ADMIN who is not
a superuser) the `PUT` above answers `400` with `"Cannot update experiments in active
status"`. The demo's `admin@demo.com` is a superuser, which is why it gets `200`.

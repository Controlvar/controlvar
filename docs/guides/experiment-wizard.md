# Guided experiment builder

There are two guided ways to create an experiment, and they are unrelated: the
dashboard's **guided setup**, and the **wizard API** described in the rest of this
page.

## In the dashboard: guided setup

**Experiments → + New Experiment** opens guided setup, which asks for a new
experiment in five steps:

| Step | What you set |
|------|--------------|
| 1. Type | A/B Test or Multivariate |
| 2. Details | Name, key, description, hypothesis, and the metrics (each with its own name), one of them primary |
| 3. Variants | The variants, their traffic split (it must add up to exactly 100%), which one is the control, and optional targeting rules |
| 4. Estimate | How results will be judged (the confidence level and the correction, saved with the experiment), then, optionally, the minimum detectable effect (saved with the experiment), how many users each variant needs and roughly how many days that takes |
| 5. Review | Everything as it will be sent; **Create Experiment** creates it as a draft |

- **Next** checks only the step you are on; **Back** never checks anything. The step
  is in the address (`?step=details`), so the browser's Back and Forward move between
  steps.
- Your answers are kept **only in this browser tab**. Nothing is saved until you
  create the experiment: reloading the page or opening a link to a later step starts
  over at step 1, and the browser asks before you reload or close a tab with answers
  in it.
- **Use the single-page form (advanced)** switches to every field on one page
  (`/experiments/new?advanced`); your answers carry over in both directions.
- The **Estimate** step starts with **How results will be judged**: the confidence
  level (90%, 95% or 99%; default 95%) and the correction for several variants
  (Benjamini-Hochberg by default, Bonferroni or None). Those two are saved with the
  experiment as `confidence_level` and `correction_method`, and cannot be changed once
  it leaves draft. The single-page form has them in a collapsed **Analysis settings**
  section. The Review step shows them in its **Analysis** row.
- The rest of the step is the estimate. Its **minimum detectable effect** is saved with
  the experiment, as the primary metric's `expected_effect` (10% is saved as `0.1`), and
  the results page's **Planned sample** plans the sample for it; leave it blank and the
  results page plans for 5%. An effect below 0.1%, or of 100% or more, cannot be saved:
  **Next** says so and stays on the step. Nothing else in the estimate is saved: the
  results page plans from the control variant's own conversion rate, at 80% power. The
  Review step's **Sample size** row says what is saved, and the single-page form shows a
  saved effect read-only under the metrics.
- The estimate calls the sample-size endpoint shown [below](#sample-size-guidance) at a significance of
  1 − the confidence level, treats the minimum detectable effect as a *relative*
  change (5% of a 12% baseline is 12.6%, not 17%), and assumes users are split evenly
  between the variants. It makes no correction for comparing several variants with
  the control, even when one is chosen above, so with three or more variants the
  experiment needs more users than it says; the Power Calculator applies a Bonferroni
  correction, so its number is higher.
- Only ADMIN and DEVELOPER users (and superusers) can create experiments; anyone else
  sees a note saying so instead of the form.
- If **Create Experiment** fails, you stay where you are with every answer kept, in
  either view. A key another experiment already has is named, with **Edit details** to
  change it. If your session has ended, sign in again in another tab, then press
  **Create Experiment** again.
- Guided setup offers only A/B Test and Multivariate. Create a split URL or bandit
  experiment through the API (`POST /api/v1/experiments/`).

Guided setup sends the same `POST /api/v1/experiments` request as the single-page
form. **It does not use the wizard API below** and creates no draft.

## The wizard API

The wizard API is a 5-step flow for designing an experiment from your own tooling:
it keeps a draft while you answer one question at a time, validates each step, and
creates the experiment when you submit. Submitting writes a real experiment in DRAFT
status, owned by the caller, and discards the draft.

The dashboard does not use it. Drafts are held in the API process's memory, so they
do not survive a restart and are not shared between replicas.

**The wizard endpoints are deprecated** and may be removed in a later release. Drafts are
per user: a draft that belongs to another user is answered exactly as one that does not
exist (`404`).

---

## Steps Overview

| Step | Name | What You Configure |
|------|------|--------------------|
| 1 | `choose_type` | Experiment type: `ab`, `multivariate` or `feature_flag_rollout` |
| 2 | `define_hypothesis` | Name, description, hypothesis, primary metric and guardrail metrics |
| 3 | `targeting` | Audience rules |
| 4 | `sample_size` | Baseline rate and minimum detectable effect (MDE) |
| 5 | `review` | Final review, then submission |

Each completed step is saved in the draft, and the draft's `current_step` moves on to the
next one. You can come back to a draft later, as long as the API process has not restarted.

---

## API Reference

All wizard endpoints are under `/api/v1/wizard`. The commands below run as written against
the stack from the [Quick Start](../getting-started/quick-start.md), in one terminal, top to
bottom; each uses the shell variables set by the ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

### POST /api/v1/wizard/drafts

Create a new wizard draft. The draft starts at step 1 (`choose_type`). This saves its id in
`$DRAFT_ID`:

```{.bash exec}
DRAFT=$(curl -s -X POST localhost:8000/api/v1/wizard/drafts \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"experiment_type": "ab"}')
DRAFT_ID=$(jq -r .id <<<"$DRAFT")

jq '{current_step, experiment_type, hypothesis}' <<<"$DRAFT"
```
<!-- expect: "current_step": "choose_type" -->
<!-- expect: "experiment_type": "ab" -->

The API answers `201 Created`:

```json
{
  "current_step": "choose_type",
  "experiment_type": "ab",
  "hypothesis": null
}
```

The draft also carries its `id`, `user_id` and the other step fields, all empty:
`primary_metric_id`, `guardrail_metric_ids`, `targeting_rules`, `baseline_rate`, `mde`,
`name` and `description`.

---

### POST /api/v1/wizard/validate

Validate one step's data before you save it. The body names the step and its data, and the
response lists what is wrong, if anything:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/wizard/validate \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"step": "define_hypothesis", "data": {"hypothesis": "Greener"}}'
```
<!-- expect: "is_valid":false -->
<!-- expect: "hypothesis must be at least 10 characters" -->
<!-- expect: "primary_metric_id is required" -->

```json
{"is_valid":false,"errors":["hypothesis must be at least 10 characters","primary_metric_id is required"]}
```

Valid data answers `{"is_valid":true,"errors":[]}`. The rules, by step:
- `choose_type`: `experiment_type` is `ab`, `multivariate` or `feature_flag_rollout`;
- `define_hypothesis`: a `hypothesis` of at least 10 characters, and a `primary_metric_id`;
- `sample_size`: `baseline_rate` and `mde` each between 0 and 1;
- `review`: `experiment_type`, `hypothesis` and `primary_metric_id` are all set;
- `targeting`: anything. The conditions are checked when the draft is submitted.

---

### PUT /api/v1/wizard/drafts/{draft_id}/step

Save one step's data in the draft, and move the draft to the next step. The body names the
step and its data, as for validation. Fields the step doesn't send are kept. `data` may
only contain step fields (`experiment_type`, `hypothesis`, `primary_metric_id`,
`guardrail_metric_ids`, `targeting_rules`, `baseline_rate`, `mde`, `name`, `description`);
any other key answers `422` and the draft is left unchanged. So does a step field of the
wrong type: `guardrail_metric_ids` takes a list of strings, `targeting_rules` a list of
objects, `baseline_rate` and `mde` numbers, and the other fields strings (any of them may
be `null`). The answer names the field, for example
`Step field 'hypothesis' must be a string or null.`

**Step 1: type.**

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/wizard/drafts/$DRAFT_ID/step \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"step": "choose_type", "data": {"experiment_type": "ab"}}' | jq .current_step
```
<!-- expect: "define_hypothesis" -->

It prints `"define_hypothesis"`, the next step.

**Step 2: hypothesis and metrics.** `primary_metric_id` and `guardrail_metric_ids` are the
event names the metrics count, such as `checkout_completed`:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/wizard/drafts/$DRAFT_ID/step \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "step": "define_hypothesis",
    "data": {
      "name": "Checkout CTA Button Color",
      "description": "Test whether a green CTA button increases checkout completion",
      "hypothesis": "A green button will increase checkout completion by 5% because it signals action and creates visual contrast",
      "primary_metric_id": "checkout_completed",
      "guardrail_metric_ids": ["page_error"]
    }
  }' | jq .current_step
```
<!-- expect: "targeting" -->

It prints `"targeting"`.

**Step 3: targeting.** Each rule is one condition, and a user must match all of them:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/wizard/drafts/$DRAFT_ID/step \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "step": "targeting",
    "data": {
      "targeting_rules": [
        {"attribute": "country", "operator": "in", "value": ["US", "CA", "GB"]}
      ]
    }
  }' | jq .current_step
```
<!-- expect: "sample_size" -->

It prints `"sample_size"`.

**Step 4: sample size.** `baseline_rate` is the metric's current conversion rate and `mde`
the smallest relative change worth detecting:

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/wizard/drafts/$DRAFT_ID/step \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"step": "sample_size", "data": {"baseline_rate": 0.12, "mde": 0.05}}' | jq .current_step
```
<!-- expect: "review" -->

It prints `"review"`: the draft is ready to submit.

---

### GET /api/v1/wizard/drafts

List your drafts that are still in progress:

```{.bash exec}
curl -s localhost:8000/api/v1/wizard/drafts \
  -H "Authorization: Bearer $TOKEN" | jq '{total, steps: [.drafts[].current_step]}'
```
<!-- expect: "total": 1 -->
<!-- expect: "review" -->

It prints `"total": 1`, and the draft's step, `"review"`.

### GET /api/v1/wizard/drafts/{draft_id}

Retrieve one of your drafts, with all the data its steps have saved. A draft that doesn't
exist, was submitted, or belongs to another user answers `404`.

---

### POST /api/v1/wizard/drafts/{draft_id}/submit

Submit the draft: this creates a real experiment in DRAFT status, and the draft is
discarded. It saves the new experiment's id in `$EXP_ID`:

```{.bash exec}
SUBMIT=$(curl -s -X POST localhost:8000/api/v1/wizard/drafts/$DRAFT_ID/submit \
  -H "Authorization: Bearer $TOKEN")
EXP_ID=$(jq -r .experiment_id <<<"$SUBMIT")

jq '{success, errors}' <<<"$SUBMIT"
```
<!-- expect: "success": true -->

```json
{
  "success": true,
  "errors": []
}
```

A draft that fails the review step's rules answers `"success": false`, with `errors` listing
them.

So does a draft whose targeting conditions would not be applied as written, such as an
unknown operator or a blank attribute. The error names the condition, for example
`targeting_rules: Value error, groups[0].conditions[0].operator: unknown operator`,
and no experiment is created.

The experiment is an ordinary one. An `ab` draft gets two variants, `Control` and
`Variant A`, at 50% each; its primary metric counts `checkout_completed`, and carries the
draft's `mde` as its `expected_effect`, which the results page plans the sample for:

```{.bash exec}
curl -s localhost:8000/api/v1/experiments/$EXP_ID \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{name, status, variants: [.variants[].name], metrics: [.metrics[] | {event_name, is_primary, expected_effect}]}'
```
<!-- expect: "name": "Checkout CTA Button Color" -->
<!-- expect: "status": "draft" -->
<!-- expect: "Variant A" -->
<!-- expect: "expected_effect": 0.05 -->

```json
{
  "name": "Checkout CTA Button Color",
  "status": "draft",
  "variants": [
    "Control",
    "Variant A"
  ],
  "metrics": [
    {
      "event_name": "checkout_completed",
      "is_primary": true,
      "expected_effect": 0.05
    },
    {
      "event_name": "page_error",
      "is_primary": false,
      "expected_effect": null
    }
  ]
}
```

A `multivariate` draft gets three variants (33/33/34), and a `feature_flag_rollout` draft
gets a held-back control with `{"enabled": false}` and a treatment with `{"enabled": true}`,
at 50% each. Review the experiment and start it when you're ready.

---

## Sample Size Guidance

The draft stores `baseline_rate` and `mde`. Submitting saves `mde` as the primary metric's
`expected_effect`, and the results page plans the sample for it; `baseline_rate` is not
saved, because the results page plans from the control variant's own conversion rate. The
wizard doesn't compute a sample size itself. The experiments API does, and the dashboard's guided setup calls it on its
Estimate step:

```{.bash exec}
curl -s -G localhost:8000/api/v1/experiments/analysis/sample-size \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "baseline_rate=0.12" \
  --data-urlencode "minimum_detectable_effect=0.05" | jq '{samples_per_variant, total_samples}'
```
<!-- expect: "samples_per_variant": 47036 -->
<!-- expect: "total_samples": 94072 -->

For a 12% baseline and a 5% relative change it needs `47036` users per variant, `94072` in
all, at 80% power and 5% significance (two-sided). It also takes `statistical_power`,
`significance_level`, `is_one_sided`, `variant_count`, and `daily_traffic` with
`traffic_allocation` for a duration estimate.

The calculation is the standard one for two proportions (Fleiss), the same one the results
page's Sample Size tab uses: the variance is pooled under the null hypothesis and unpooled
under the alternative.

```text
n = [z_α/2 × √(2p̄(1−p̄)) + z_β × √(p₁(1−p₁) + p₂(1−p₂))]² / (p₁ − p₂)²
```

Where `p₁` = baseline rate, `p₂` = baseline rate × (1 + MDE), `p̄` = (p₁ + p₂) / 2,
`z_α/2` = 1.96 (two-tailed, α=0.05; a one-sided test uses `z_α` = 1.645), `z_β` = 0.842
(80% power), and `n` is rounded up. It is per variant, with no correction for comparing more
than two variants; the total is `n` times the number of variants. A baseline that the effect
would raise to 100% or more is refused with a 422.

---

## Permissions

- Any logged-in user can create, read and update their own wizard drafts, and only
  their own: for every role, another user's draft is answered exactly as a draft that
  does not exist.
- Submitting needs permission to create experiments, as `POST /api/v1/experiments` does.

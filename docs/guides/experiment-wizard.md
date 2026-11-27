# Guided experiment builder

This page describes the dashboard's guided setup, which asks for a new experiment
one step at a time, and the sample-size guidance its Estimate step uses.

## In the dashboard: guided setup

**Experiments → + New Experiment** opens guided setup, which asks for a new
experiment in five steps:

| Step | What you set |
|------|--------------|
| 1. Type | A/B Test or Multivariate |
| 2. Details | Name, key, description, hypothesis, and the metrics (each with its own name), one of them primary |
| 3. Variants | The variants, their traffic split (it must add up to exactly 100%), which one is the control, whether to [balance the split within groups](balance-within-groups.md#in-the-dashboard), and optional targeting rules |
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
- Below them, **Also test the primary metric sequentially (beta)** turns on
  [sequential testing](../api/sequential-testing.md#turning-it-on) for the new experiment;
  it is off by default. Like those two, it cannot be changed once the experiment leaves
  draft. The Review step's **Analysis** row then ends **Sequential testing
  (beta) on**, as does the single-page form's **Analysis settings** summary.
- The rest of the step is the estimate. Its **minimum detectable effect** is saved with
  the experiment, as the primary metric's `expected_effect` (10% is saved as `0.1`), and
  the results page's **Planned sample** plans the sample for it; leave it blank and the
  results page plans for 5%. An effect below 0.1%, or of 100% or more, cannot be saved:
  **Next** says so and stays on the step. Nothing else in the estimate is saved: the
  results page plans from the control variant's own conversion rate, at 80% power. The
  Review step's **Sample size** row says what is saved, and the single-page form shows a
  saved effect read-only under the metrics.
- The estimate calls the sample-size endpoint shown [below](#sample-size-guidance) at a significance of
  1 − the confidence level and with the correction chosen above, treats the minimum
  detectable effect as a *relative* change (5% of a 12% baseline is 12.6%, not 17%), and
  assumes users are split evenly between the variants. With three or more variants and
  Bonferroni or Benjamini-Hochberg, each comparison with the control is planned at that
  significance ÷ (variants − 1), as the results page's Sample Size tab plans it, and the
  estimate says so under its number. With None it makes no correction, the same as the
  results, and the Power Calculator, which applies a Bonferroni correction, gives a higher
  number.
- With **Balance the split within groups** on, the Review step's **Variants** row ends **Balance:
  within** what you chose, such as `Platform (ios, android, web)`, or **Balance: off**.
- On **Details**, each metric's **Conversion window in hours (beta, optional)** takes a whole
  number from 1 to 8,760, or blank for none; **Next** refuses anything else, naming the metric, and
  focuses the box. With a window on the primary metric, the **Estimate** step says that the
  estimate counts users, not their windows. The Review step's line for a metric with a window
  names it, as *conversion window 7 days*.
- Only users who may create experiments see the form: ADMIN and DEVELOPER users,
  superusers, and in the full profile anyone whose custom role or direct grant gives
  `experiment (create)`. Anyone else sees a note saying so instead.
- If **Create Experiment** fails, you stay where you are with every answer kept, in
  either view. A key another experiment already has is named, with **Edit details** to
  change it. If your session has ended, sign in again in another tab, then press
  **Create Experiment** again.
- Guided setup offers A/B Test, Multivariate and Bandit. In the full profile it offers
  Split URL last, and asks for each variant's page.

Guided setup sends the same `POST /api/v1/experiments` request as the single-page
form.

---

## Sample Size Guidance

The experiments API computes a sample size, and the dashboard's guided setup calls it on
its Estimate step. The commands below run as written against the stack from the
[Quick Start](../getting-started/quick-start.md), in one terminal, top to bottom; each uses
the shell variables set by the ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. Then ask for a sample size:

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
`significance_level`, `is_one_sided`, `variant_count` (2 to 100), `correction_method`, and
`daily_traffic` (at most 1,000,000,000,000) with `traffic_allocation` for a duration estimate.

With three or more variants, send the experiment's correction as `correction_method`: `none`
(the default), `bonferroni` or `benjamini_hochberg`, the values an experiment's
`correction_method` takes. With either correction, each of the `variant_count − 1`
comparisons with the control is planned at `significance_level ÷ (variant_count − 1)`, as the
results page's Sample Size tab does; Benjamini-Hochberg is planned at the same level as
Bonferroni. Three variants at 5%:

```{.bash exec}
curl -s -G localhost:8000/api/v1/experiments/analysis/sample-size \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "baseline_rate=0.12" \
  --data-urlencode "minimum_detectable_effect=0.05" \
  --data-urlencode "variant_count=3" \
  --data-urlencode "correction_method=benjamini_hochberg" | jq '{samples_per_variant, comparisons, alpha}'
```
<!-- expect: "samples_per_variant": 56961 -->
<!-- expect: "comparisons": 2 -->
<!-- expect: "alpha": 0.025 -->

That is `56961` users per variant, where no correction needs `47036`. The answer also carries
`correction_method`, `comparisons` and `alpha`, the significance each comparison is planned
at; `significance_level` is still the `0.05` you sent.

The calculation is the standard one for two proportions (Fleiss), the same one the results
page's Sample Size tab uses: the variance is pooled under the null hypothesis and unpooled
under the alternative.

```text
n = [z_α/2 × √(2p̄(1−p̄)) + z_β × √(p₁(1−p₁) + p₂(1−p₂))]² / (p₁ − p₂)²
```

Where `p₁` = baseline rate, `p₂` = baseline rate × (1 + MDE), `p̄` = (p₁ + p₂) / 2,
`z_α/2` = 1.96 (two-tailed, α=0.05; a one-sided test uses `z_α` = 1.645), `z_β` = 0.842
(80% power), and `n` is rounded up. It is per variant: with a correction, `α` is
`significance_level ÷ (variant_count − 1)`. The total is `n` times the number of variants. A baseline that the effect
would raise to 100% or more is refused with a 422.

# Statistical Power Analysis

This guide explains how to use the pre-experiment Power Calculator to plan your A/B tests before you launch them.

## Inputs

### 1. Baseline Rate

The current performance of your metric before any change. For a conversion rate experiment, this might be 5% (0.05). This must be between 0 and 1 exclusive.

### 2. Minimum Detectable Effect (MDE)

The smallest relative improvement you want to be able to detect. An MDE of 10% on a 5% baseline means you want to detect a lift from 5% to 5.5% (absolute change = 0.5 percentage points).

---

## The Power Calculator in the dashboard

The dashboard's Power Calculator is at `/power-calculator`: on the Docker Compose stack,
http://localhost:3000/power-calculator. It needs no sign-in. Set **Baseline Conversion Rate**
and **Minimum Detectable Effect (relative)** with their sliders, and choose
**Significance Level (alpha)**, **Statistical Power** and
**Number of Variants (including control)**. **Sample Size per Variant** then shows how many
users each variant needs, by the formula below, with the Bonferroni correction of
[Multi-Variant Experiments](#multi-variant-experiments) when there are more than two
variants; **MDE (absolute)** shows the effect in percentage points.

---

## The Formula

For a two-proportions z-test (the standard for conversion rate experiments), the required sample size per variant is:

```text
n = [z_alpha * sqrt(2 * p_bar * (1 - p_bar))
     + z_power * sqrt(p1*(1-p1) + p2*(1-p2))]^2
    / (p2 - p1)^2
```

Where:
- `p1` = baseline rate
- `p2` = baseline rate + (baseline rate * MDE)
- `p_bar` = (p1 + p2) / 2
- `z_alpha` = `norm.ppf(1 - alpha/2)` for two-tailed (e.g. 1.96 for alpha=0.05)
- `z_power` = `norm.ppf(power)` (e.g. 0.842 for power=0.80)

The Power Calculator and `POST /api/v1/power/sample-size` give `n` rounded up to a whole user.

**Verification example** for `baseline=0.05, MDE=10% relative, alpha=0.05, power=0.80`:
- `p1 = 0.05`, `p2 = 0.055`, `p_bar = 0.0525`
- `z_alpha = 1.9600`, `z_power = 0.8416`
- `n ≈ 31,234 per variant` (31,234 control + 31,234 treatment = 62,468 total)

---

## Runtime Estimation

Once you have the required sample size per variant, you can estimate the runtime:

```text
daily_per_variant = daily_traffic * traffic_allocation / n_variants
days = required_sample_size / daily_per_variant
```

**Example**: You need 31,234 per variant, have 10,000 daily users, 50% traffic allocation, 2 variants:
- `daily_per_variant = 10,000 * 0.50 / 2 = 2,500`
- `days = 31,234 / 2,500 = 12.5 days`

---

## Multi-Variant Experiments

When you have more than 2 variants (e.g. A/B/C), the platform applies **Bonferroni correction** to control the family-wise error rate:

```text
corrected_alpha = alpha / (n_variants - 1)
```

For a 3-variant experiment with alpha=0.05:
- `corrected_alpha = 0.05 / 2 = 0.025`
- This increases the required sample size per variant

---

## Minimum Detectable Effect (MDE) Calculator

If you have a fixed sample size available (e.g. 2 weeks of traffic at 50% allocation), you can use the **MDE calculator** to find the smallest effect you can detect:

`POST /api/v1/power/mde`

---

## During the experiment: the Sample Size tab

Everything above is for planning before launch. Once an experiment is running, the
**Sample Size** tab of its results page (and `GET /api/v1/results/{experiment_id}/sample-size`)
plans again from the experiment's own data, with the same formula as above:

- **Baseline**: the conversion rate observed in the control variant on the primary metric so
  far, counted the way the results count it (users with a conversion, over users assigned).
  You can type a different rate instead. The results page is cached for up to five minutes and
  the tab is not, so for that long the tab's rate can be newer than the one on the Overview.
- **MDE**: the effect saved with the experiment, its primary metric's `expected_effect`, when
  it is above 0 and below 1; otherwise 5% relative. You can type another. The response's
  `mde_source` says which: `experiment`, `default` or `request`.
- **Power** 80%, two-sided, unless you change it. Two-sided is what the results test.
- **Significance**: one minus the experiment's stored confidence level (5% for the default
  0.95) unless you change it, the same as the results.
- **Correction**: the experiment's stored correction method (Benjamini-Hochberg unless it was
  created with another) unless you choose another, the same as the results. With Bonferroni
  or Benjamini-Hochberg each of the `k - 1` comparisons is planned at `alpha / (k - 1)`.
  Unlike the Power Calculator, the tab does not apply Bonferroni on its own: the
  multi-variant paragraph above is about the Power Calculator only.
- **Progress** is the smallest variant's users against the planned number, and the achieved
  power is the power to detect the planned MDE at that size, not the effect observed so far.

The tab plans a conversion rate (a two-proportion test) whatever the metric's type, and it
says so. For a revenue metric, which the results analyse as a mean per user, the plan is for
the share of users who buy. Nothing typed on the tab is saved.

**Worked example**: a control rate of 12% so far, a 5% relative MDE, 80% power, 5%
significance, two variants (so the correction changes nothing):

- `p1 = 0.12`, `p2 = 0.126`
- `n = 47,036 per variant`
- with 20,000 users in the smallest variant: 43% of the planned sample, and a power of 45% to
  detect the 5% lift
- with three variants and Bonferroni: each comparison at 2.5%, `n = 56,961 per variant`

When there is nothing to plan from yet (no users in control, no control conversions, or every
control user converted) the tab asks for the rate you expect, and the API answers 200 with
`required_sample_size_per_variant: null` and an `unavailable_reason`.

---

## REST API Reference

All endpoints are unauthenticated (no login required). They perform pure computation with no database access.

**Limits.** `n_variants` is at most 100 (an experiment's variants split its traffic in whole percentages, so no more than 100 of them receive any), and `sample_size_per_variant`, `required_sample_size` and `daily_traffic` are at most 1,000,000,000,000 (10^12, more than a hundred times the world's population). A larger value answers 422 naming the field: the error's `loc` is `["body", "<field>"]` and its `type` is `less_than_equal`. A calculation whose numbers still leave floating-point range, such as a `mean` metric with a very large `baseline_std`, answers 422 with the detail "These values are too large or too small to calculate with. Use values nearer those of a real experiment." A `proportion` sample size whose treatment rate (the baseline raised by the effect) is too close to the baseline for the size to be a finite number, such as a `baseline_rate` of `1e-300` or a `minimum_detectable_effect` of `1e-17`, answers 422 with the detail "This baseline raised by this effect changes too little to estimate a sample size. Raise the baseline rate or the effect."

### POST /api/v1/power/sample-size

Compute the required sample size per variant.

**Request body**:
```json
{
  "baseline_rate": 0.05,
  "minimum_detectable_effect": 0.10,
  "alpha": 0.05,
  "power": 0.80,
  "n_variants": 2,
  "two_tailed": true,
  "metric_type": "proportion",
  "daily_traffic": 10000,
  "traffic_allocation": 0.5
}
```

**Response**:
```json
{
  "per_variant": 31234,
  "total": 62468,
  "alpha": 0.05,
  "power": 0.8,
  "baseline_rate": 0.05,
  "mde_absolute": 0.005,
  "mde_relative": 0.1,
  "confidence_level": 0.95,
  "runtime_days": 12.5,
  "n_variants": 2,
  "two_tailed": true,
  "metric_type": "proportion"
}
```

### POST /api/v1/power/mde

Given a fixed sample size per variant, compute the minimum detectable effect.

**Request body**:
```json
{
  "sample_size_per_variant": 31234,
  "baseline_rate": 0.05,
  "alpha": 0.05,
  "power": 0.80,
  "n_variants": 2,
  "two_tailed": true
}
```

**Response**:
```json
{
  "mde_absolute": 0.005,
  "mde_relative": 0.1,
  "per_variant_sample": 31234,
  "total_sample": 62468,
  "alpha": 0.05,
  "power": 0.8,
  "n_variants": 2,
  "two_tailed": true
}
```

### POST /api/v1/power/runtime

Estimate how many days it will take to collect the required sample size.

**Request body**:
```json
{
  "required_sample_size": 31234,
  "daily_traffic": 10000,
  "traffic_allocation": 0.5,
  "n_variants": 2
}
```

**Response**:
```json
{
  "days_to_significance": 12.49,
  "weeks_to_significance": 1.78,
  "daily_traffic_per_variant": 2500,
  "confidence_interval_days": [6.68, 18.31]
}
```

### GET /api/v1/power/curve

Return the power curve — sample size required per variant for a range of effect sizes.

**Query parameters**: `baseline`, `alpha`, `power`, `mde` (optional, marks a point)

**Example**: `GET /api/v1/power/curve?baseline=0.05&alpha=0.05&power=0.80&mde=0.10`

**Response**, abridged to 3 of its 15 points:
```json
{
  "points": [
    {"effect_size_relative": 0.01, "sample_size_per_variant": 2996698, "is_current_target": false},
    {"effect_size_relative": 0.10, "sample_size_per_variant": 31234, "is_current_target": true},
    {"effect_size_relative": 0.50, "sample_size_per_variant": 1471, "is_current_target": false}
  ],
  "baseline_rate": 0.05,
  "alpha": 0.05,
  "power_target": 0.80
}
```

### POST /api/v1/power/plan

Generate plain-English planning advice for a power analysis result. The advice is the
built-in planning advice, and `generated_by` is always `"template"`.

**Request body**:
```json
{
  "experiment_name": "Checkout CTA Button Test",
  "metric_description": "checkout conversion rate from cart page",
  "baseline_rate": 0.05,
  "mde": 0.10,
  "runtime_days": 12.5,
  "business_context": "Q4 launch, mobile-only segment"
}
```

**Response**:
```json
{
  "advice": "Your power analysis for 'Checkout CTA Button Test' targets a 10.0% relative lift...",
  "generated_by": "template",
  "experiment_name": "Checkout CTA Button Test",
  "baseline_rate": 0.05,
  "mde": 0.10,
  "runtime_days": 12.5
}
```

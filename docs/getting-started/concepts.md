# Core Concepts

This guide explains the fundamental concepts you need to understand before working with the platform. Whether you are a product manager designing experiments, an engineer integrating feature flags, or an analyst reading results, this reference will clarify what each term means and how the pieces fit together.

---

## Feature Flags

A feature flag (also called a feature toggle or feature switch) is a configuration key that controls whether a piece of functionality is visible or active for a given user. Instead of deploying code to enable or disable a feature, you change a flag value at runtime — no deployment required.

### On/Off Flags

The simplest type. A boolean value controls whether the feature is shown at all.

```text
dark-mode: true   →  show dark theme
dark-mode: false  →  show light theme
```

Common uses: gradual rollouts, kill switches, beta programs, maintenance mode.

### Several Experiences

A flag is on or off for each user: it carries no variants or values. To serve one of several experiences, run a multivariate experiment instead. Each of its variants carries a `configuration`, which `POST /api/v1/tracking/assign` returns.

### Why Use Feature Flags

- Deploy code that is hidden behind a flag before it is ready to ship
- Roll out to a small percentage of users first, then expand gradually
- Instantly disable a problematic feature without a code deploy
- Enable features for internal testers before public launch
- Target features to specific segments (premium users, US users, mobile users)

---

## Experiments / A/B Tests

An experiment (also called an A/B test or controlled experiment) is the process of splitting users into groups, exposing each group to a different version of a feature, and measuring which version performs better on a defined metric.

### Hypothesis

Every experiment starts with a hypothesis: a precise, falsifiable statement of what you expect to happen and why.

Good hypothesis: "Changing the checkout CTA button from blue to green will increase checkout completion rate by 5% because green creates stronger visual contrast against the white background."

A good hypothesis specifies the change, the expected direction, the magnitude, and the reason.

### Control vs Treatment

- **Control**: the existing experience, unchanged. Serves as the baseline.
- **Treatment** (or **Variant**): the new experience you are testing.

An experiment can have one control and one or more treatments. With two variants (control + one treatment) it is called an A/B test. With more than two variants it is called an A/B/n test or multivariate test.

### Statistical Significance

Statistical significance tells you how confident you are that the difference between control and treatment is real — not just random noise. It is expressed as a p-value.

- **p-value < 0.05**: 95% confidence the difference is real. Standard threshold for most experiments.
- **p-value < 0.01**: 99% confidence. Use for high-stakes decisions.

A result is statistically significant when p < your chosen alpha threshold (usually 0.05).

**Important**: statistical significance does not tell you the effect is large or meaningful. Always check effect size alongside p-value.

---

## Variants

A variant is one of the possible experiences in an experiment. A running experiment has at least two variants:

- **Control variant**: the baseline experience (usually what users see today)
- **Treatment variant(s)**: the new experience(s) you are testing

Each variant is assigned a **traffic weight** — the fraction of eligible users who will be shown that variant. The weights must add up to exactly 100%; anything else is refused with `422` ("Traffic allocations must sum to 100% (currently 90%)"). To keep users out of an experiment, use targeting rules or the global holdout.

### Variants vs Feature Flags

A feature flag controls access to a feature for a user. A variant controls which version of a feature a user sees within an experiment. Experiments are temporary; feature flags are often permanent configuration.

---

## Metrics

A metric is the measurable outcome you use to evaluate whether a variant is better or worse than the control.

### Metric Types

| Type | Description | Example |
|------|-------------|---------|
| **Conversion** | Binary: did the event happen or not? | Clicked button, completed checkout, signed up |

A metric can also be created with the type `revenue`, `count` or `duration`, but the results analyse every metric as a conversion: whether each user sent the metric's event, not the event's value. See [Limitations](limitations.md).

### Primary vs Secondary Metrics

Each experiment should have exactly one **primary metric** — the north star for your ship/no-ship decision. All other metrics are **secondary** and used to check for unexpected side effects.

Example: primary = checkout completion rate; secondary = cart abandonment rate, average order value, customer support contacts.

### Guardrail Metrics

Guardrail metrics are secondary metrics with a minimum acceptable threshold. If a treatment improves your primary metric but degrades a guardrail metric beyond its threshold, the experiment should not ship.

Example guardrail: "Support ticket rate must not increase by more than 5%."

---

## User Bucketing

User bucketing is the process of assigning each user to a variant. The platform uses consistent hash-based bucketing.

### Consistent Hash Assignment

The assignment is computed as a deterministic hash of `experiment_key + user_id`. This means:

- The same user always gets the same variant for a given experiment (sticky assignment)
- The API stores each assignment, and a user who already has one gets it back unchanged, even if the weights have changed since
- Sticky assignment survives server restarts, cache clears, and SDK reinitializations

The hash puts each user in one of 100 buckets, numbered 0 to 99, and the traffic weights divide the buckets between the variants in order: with 50% control and 50% treatment, buckets 0–49 get control and buckets 50–99 get treatment. A multi-armed bandit experiment places new users by the bandit's current weights instead.

### Why Consistent Bucketing Matters

Without consistent bucketing, a user could see the control variant on Monday and the treatment variant on Wednesday. This would contaminate your results and create a confusing user experience.

---

## Variant Weights vs Rollout Percentage

These two concepts are related but distinct:

### Variant Weights (Experiments)

An experiment has no separate traffic allocation: every eligible user is assigned, and the variant weights decide how users are split. Users are left out of an experiment only by its targeting rules, a mutual exclusion group or the global holdout.

### Rollout Percentage (Feature Flags)

Rollout percentage is the fraction of users for whom a feature flag evaluates to `true` (enabled). A feature flag at 10% rollout means 10% of users see the feature and 90% do not.

Unlike variant weights, a rollout percentage is usually increased over time as confidence grows.

---

## Targeting Rules

Targeting rules define which users are eligible for an experiment, and which users a feature flag's rules turn on. In an experiment, a new user who does not match the rules is not assigned: the API answers with the control variant and `"assigned": false`, records no assignment, and the user does not count in the results. For a feature flag, a user no rule matches gets the flag's rollout percentage.

### Attribute-Based Rules

Rules are evaluated against user attributes that your application passes to the platform at evaluation time.

```text
country IN [US, CA, GB]
plan EQUALS premium
account_age_days GREATER_THAN 30
app_version SEMVER_GTE 3.0.0
email ENDS_WITH @company.com
```

### Combining Rules

Rules can be combined using AND (all must match), OR (at least one must match), and NOT (inverts the result).

```text
AND:
  country IN [US, CA]
  plan EQUALS enterprise
  account_age_days GREATER_THAN 14
```

This would target enterprise users in the US or Canada who have had their account for at least two weeks.

### Common Targeting Attributes

| Attribute | Type | Example |
|-----------|------|---------|
| `user_id` | string | Target a specific beta tester |
| `country` | string | Geographic targeting |
| `plan` | string | Subscription tier |
| `account_age_days` | number | Target new vs returning users |
| `app_version` | semver | Target specific app versions |
| `device_type` | string | Mobile vs desktop |
| `email` | string | Target internal users by domain |

---

## Environments

One deployment is one environment: flags, experiments and API keys carry no environment field. To test experiments and flags in staging before they reach production users, run staging and production as separate deployments.

### Staging vs Production

| Environment | Purpose |
|-------------|---------|
| **staging** | Test experiment logic, SDK integration, and targeting rules before launch |
| **production** | Live experiments and flags serving real users |

Each deployment has its own database, so its feature flag states and experiment assignments are its own. A feature flag that is 100% enabled in staging has no effect in production unless you also enable it there.

### Environment Variables

Your SDK client is initialized with an API key, which is stored in the database of the deployment it was created on, so a key created on staging is unknown to a separate production deployment. Use separate API keys for staging and production to prevent accidental cross-environment leakage.

---

## Glossary

| Term | Definition |
|------|-----------|
| **A/B test** | Experiment comparing two variants (control and treatment) |
| **A/B/n test** | Experiment comparing more than two variants |
| **Alpha (α)** | The significance threshold; probability of a false positive you are willing to accept (typically 0.05) |
| **Bucketing** | The process of assigning users to variants |
| **Confidence interval** | Range of values within which the true effect size likely falls |
| **Control** | The baseline variant; typically the existing experience |
| **Conversion rate** | Fraction of users who completed a defined action |
| **Covariate** | In CUPED, the pre-experiment data used to adjust a result: here, whether the user sent the metric's event before being assigned |
| **CUPED** | Controlled-experiment Using Pre-Experiment Data; a variance reduction technique |
| **Effect size** | The magnitude of the difference between variants |
| **Experiment** | A controlled test that assigns users to variants and measures outcomes |
| **Feature flag** | A runtime configuration key that controls feature visibility |
| **Guardrail metric** | A secondary metric that must not degrade beyond a threshold |
| **Holdout** | A percentage of new users kept out of all experiments, to compare with everyone else. Users already assigned keep their assignments, and feature flags ignore it |
| **Hypothesis** | A falsifiable statement predicting what will happen in an experiment and why |
| **MAB** | Multi-Armed Bandit; an adaptive experiment that shifts traffic toward better-performing variants |
| **MDE** | Minimum Detectable Effect; the smallest improvement worth measuring |
| **Metric** | A measurable outcome used to evaluate experiment results |
| **Mutual exclusion group** | A set of experiments configured so users are enrolled in at most one at a time |
| **p-value** | Probability of observing results as extreme as these if there were no true effect |
| **Primary metric** | The single metric that determines the ship/no-ship decision |
| **Rollout percentage** | Fraction of users for whom a feature flag is enabled |
| **Segment** | A subset of users defined by shared attributes |
| **Statistical significance** | p-value below the chosen alpha threshold |
| **Targeting rule** | An attribute-based condition that determines experiment eligibility |
| **Traffic weight** | The share of an experiment's users a variant gets; the weights add up to 100% |
| **Treatment** | A non-control variant in an experiment |
| **Variant** | One of the possible experiences in an experiment |

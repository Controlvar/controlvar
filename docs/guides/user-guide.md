# User Guide

Guide for product managers, experimenters, and analysts using Controlvar.

---

## Accessing the Platform

You'll need an account with one of the following roles:

| Role | What You Can Do |
|------|----------------|
| **ADMIN** | Create, change, schedule and delete any experiment (scheduling is through the API; [which actions have a screen](dashboard-and-api.md)); create and manage any feature flag |
| **DEVELOPER** | Create, change, schedule and delete any experiment (scheduling is through the API; [which actions have a screen](dashboard-and-api.md)); create and manage any feature flag |
| **ANALYST** | View all experiments, results, and reports, and every feature flag, and change no experiment or flag; like every role, create an API key, which writes tracking data |
| **VIEWER** | View every experiment and its results, and every feature flag, and change no experiment or flag; like every role, create an API key, which writes tracking data |

The ADMIN role does not, by itself, open the dashboard's Users, API Keys, Safety settings
or Roles pages, manage user accounts, or change a flag's per-flag safety config or roll it
back: those need the separate, superuser account flag (`is_superuser`), which an ADMIN account
may or may not have. Through the API, the ADMIN role alone may manage other users' API keys and
custom Roles ([details](../rbac/README.md)). A superuser can do all of the above regardless of role. Contact your platform admin
to request access, a role change, or superuser access.

### What the dashboard does not do yet

[What the dashboard does, and what is API only](dashboard-and-api.md) lists, row by row, what
has a screen and what works through the API only, with the guide for each.

---

## Experiments

### Creating an Experiment

**Experiments → + New Experiment** opens *guided setup*, five short steps. Your answers
stay in this browser tab until you press **Create Experiment**; reloading starts over.
If you prefer every field on one page, choose **Use the single-page form (advanced)** —
your answers carry over.

**Step 1: Type**

Choose **A/B Test** or **Multivariate**. Create a split URL or bandit experiment through the
API (`POST /api/v1/experiments/`).

**Step 2: Details**

- **Name**: Descriptive name, e.g., "Homepage CTA Button Color Q1 2026"
- **Key**: generated from the name until you edit it, e.g., `homepage_cta_button_color_q1_2026` — your code passes this as `experiment_key`
- **Description**: Background context, links to design doc, Jira ticket
- **Hypothesis**: "Changing the CTA button from blue to green will increase click-through rate by 10%"

Then choose what to measure. Mark one metric as the **primary metric** (the one you decide
on); any others are tracked alongside it.

Every metric is analysed as a conversion today: whatever its type, the results report the
share of users who sent the metric's event.

The metric's **event name** must match exactly what your engineers track in code (e.g., `checkout_complete`).

**Step 3: Variants**

Every experiment needs a control; mark one variant as it:

| Variant | Is Control | Traffic % |
|---------|-----------|-----------|
| Control (Blue Button) | ✅ | 50% |
| Treatment (Green Button) | — | 50% |

The traffic split must add up to exactly 100%.

For multivariate tests, add more variants:

| Variant | Is Control | Traffic % |
|---------|-----------|-----------|
| Control | ✅ | 34% |
| Green Button | — | 33% |
| Red Button | — | 33% |

Each variant can also carry a **configuration**: a JSON object your app receives with the
assignment, so code can read values instead of branching on the variant's name. Open
**Configuration (JSON, optional)** under the variant and type an object, for example:

```json
{"button_color": "green", "button_text": "Buy now"}
```

Leave it empty when the variant name is enough; the variant then has no configuration (`null`,
not `{}`). The form refuses anything that is not a JSON object, such as a list, a string or text
that does not parse, and shows the problem under the field; **Next** and **Create Experiment**
open the field and move to it. The dashboard accepts up to 16,384 bytes (UTF-8) per
configuration; the API accepts larger ones. `POST /api/v1/tracking/assign`, which the SDKs
call, returns the configuration as saved, and the experiment's page shows it in the variants
table.

Only want to test on specific users? Add targeting rules on the same step:

- Country is in [US, CA]
- Subscription plan is "premium"
- Account age is greater than 30 days

Users who don't match targeting rules are excluded from the experiment entirely.

Every condition needs an attribute, an operator and (for most operators) a value. If one is
missing, **Create Experiment** stops and the problem is shown beside the targeting rules; a
condition the server cannot apply is reported there too.

**Step 4: How results will be judged, and an estimate**

First, **How results will be judged**. These two settings are saved with the experiment and
cannot be changed after it starts:

- **Confidence level**: 90%, 95% (recommended) or 99%. At 95%, a variant is significant when
  its p-value is below 0.05.
- **Correction for several variants**: Benjamini-Hochberg (recommended), Bonferroni (stricter)
  or None. It only matters with three or more variants: each extra variant is another chance
  for one to look like a winner by luck. Benjamini-Hochberg keeps the share of false winners
  among the variants called significant at about 5% (at 95% confidence); Bonferroni keeps the
  chance of even one false winner at 5% and needs more users. The correction applies to the
  variants of each metric, not across metrics.

Below them, **Also analyse the primary metric with Bayesian statistics** is off by default. Turned
on, the results also carry a Bayesian analysis of the primary metric (the probability that each
variant is the best) with default priors; the frequentist results and the recommendation do not
change. To set the priors, see [Bayesian analysis](../api/bayesian.md).

The single-page form has the same settings in a collapsed **Analysis settings** section,
whose title shows the current values.

Then, optionally, the estimate. Enter your baseline conversion rate and the smallest change
worth detecting — a *relative* change, so 5% on a 12% baseline means 12% → 12.6% — and press
**Calculate estimate**. At 80% power and 95% confidence that example needs 47,036 users per
variant. Add your daily users to see roughly how many days that takes. The smallest change
worth detecting (the minimum detectable effect) is saved with the experiment, and the results
page's **Planned sample** plans for it; leave it blank and the results page plans for 5%. An
effect below 0.1%, or of 100% or more, cannot be saved, and **Next** says so. The rest of the
estimate is a guide and is not saved: the results page plans from the control variant's own
conversion rate, at 80% power. The estimate plans each comparison at 1 − the confidence level and makes no
correction for comparing several variants, so with three or more variants and a correction
the experiment needs more users than it says.

**Step 5: Review and create**

Check the summary, including each variant's configuration and the **Analysis** row (for example
"95% confidence · Benjamini-Hochberg correction", with "· Bayesian analysis on" when it is on),
use **Edit** to go back to any step, then press **Create Experiment**.
The **Sample size** row says exactly what is saved: the minimum detectable effect, if you
entered one, and nothing else from the estimate.
The experiment is created as a draft and you land on its page.

**Then: start the experiment**

Click **Start** on the experiment's page. The status changes to `ACTIVE`.

You can also schedule automatic start and end dates. The dashboard has no schedule
screen yet, so this is an API call: `PUT /api/v1/experiments/{experiment_id}/schedule`,
on a `DRAFT` experiment, with an ADMIN or DEVELOPER token. The experiment activates at
`start_date` and completes at `end_date`, which must be at least an hour later. A date you
leave out of the request is left as it is, and `null` clears it. A date written without a
UTC offset is read in `time_zone`, an IANA name such as `America/Los_Angeles` (default
`UTC`); a date with an offset, like the ones below, keeps it. Dates are stored in UTC.
Clicking **Start** before the scheduled start starts the experiment right away: its start date
becomes the time you clicked, and a scheduled end date is kept.

```{.bash skip reason="server: needs a running API, a signed-in token in TOKEN and a draft experiment's id in EXPERIMENT_ID"}
curl -s -X PUT "localhost:8000/api/v1/experiments/$EXPERIMENT_ID/schedule" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"start_date": "2026-11-02T09:00:00Z", "end_date": "2026-11-16T09:00:00Z"}'
```

---

### Pausing and Stopping

**Pause**: Temporarily halt assignment of new users. Existing assignments are preserved. Use when you need to investigate anomalies or if there's a critical bug.

A paused experiment stays paused until you click **Start** again. Nothing resumes it
behind your back: not the scheduler, and not a restart of the service.

To have it resume by itself at a set time, schedule a resume *after* pausing: the same
`PUT /api/v1/experiments/{experiment_id}/schedule` call, where `start_date` is the time
to resume at. It is stored as `resume_at` (returned with the experiment); the experiment's own
start date does not move. On a paused experiment:

- `"start_date": null` cancels a scheduled resume;
- a field you leave out is left as it is, as on a draft;
- `end_date` must be later than the experiment's start date and, with a resume scheduled,
  at least an hour after the resume time.

Starting, completing or archiving the experiment cancels a scheduled resume, and so does
any other change of status.

**Complete**: Stop the experiment. Use when you have sufficient data and are ready to make a decision. Existing data is preserved.

Use the **Sample Size** tab to confirm the smallest variant has reached the planned sample
before deciding. Do not stop the experiment the first time a p-value drops below 0.05: decide at
the planned sample size.

---

### Reading Results

Open the experiment from **Experiments** and choose **View results** (it cannot be chosen
while the experiment is a draft), or go to http://localhost:3000/results/EXPERIMENT_ID.

#### Experiment Summary Card

Shows at a glance:
- **Status**: Active / Completed
- **Duration**: Days running
- **Total Users**: Participants across all variants
- **Planned sample**: *Reached* when the smallest variant has the planned sample per variant
  in the plan the **Sample Size** tab shows before you change anything, for example "4,000 of 3,000
  per variant"; otherwise *Not yet*, with the progress, for example "203 of 69,410 per
  variant". With no rate to plan from yet it reads *Not yet* with "0 per variant, no plan yet".
  The line under the cards names the effect the plan is for and the control rate it starts
  from: "To detect a 10% lift on the control's 5.01%. The 10% was set when the experiment was
  created." for an effect saved with the experiment, or "To detect a 5% lift (the default: no
  effect was saved with this experiment) on the control's 10%." when none was. Changing the
  inputs on the tab does not change the card, and the card's "See the Sample Size tab for how
  the plan is calculated, or to try another effect." link opens the tab.
- **Shipping floor**: a variant is recommended for shipping only once every variant has at
  least the primary metric's minimum sample size (100 users unless the metric sets another):
  until then a significantly better variant is shown as *Leading*, and the recommendation is
  CONTINUE TESTING, with the reason "Not every variant has reached the minimum sample size
  yet". *Planned sample* is the sample in the plan the Sample Size tab shows before you
  change anything, while the recommendation uses its own minimum of 100 users per variant, so
  SHIP VARIANT beside a *Planned sample* of *Not yet* is not a contradiction.
- **Recommendation**: SHIP VARIANT / KEEP CONTROL / CONTINUE TESTING / INCONCLUSIVE
- **Analysis**: the confidence level and correction the results use, which are the
  experiment's own settings. For example "95% confidence · Benjamini-Hochberg correction for
  the 2 comparisons with the control on each metric", or, with one treatment, "one comparison
  with the control on each metric, so no correction is needed".

To see the uncorrected numbers for an experiment with several variants and a correction, ask
the API with `?correction_method=none` (`GET /api/v1/results/{experiment_id}`).

#### Sample Ratio Check

The results page compares how many users each variant received with its traffic allocation (a
chi-square test of the assignment counts). If they differ by more than chance allows
(p < 0.001), a notice above the tabs says so, with each variant's users and the number expected.
The recommendation is then **INCONCLUSIVE**, and a "Leading" variant and the Bayesian analysis
carry a warning. A mismatch usually means assignment or tracking is broken, so do not use the
numbers for a decision until you find out why.

When the split matches, one line below the summary card says so, with the check's p-value.
Nothing is shown for a bandit, which moves traffic on purpose, for an experiment with no users
yet, or when the check could not be run; that is not a pass. The API returns the check as `srm`
in `GET /api/v1/results/{experiment_id}`.

#### Bayesian Analysis

For an experiment with Bayesian analysis on, the end of the **Overview** tab shows a Bayesian
analysis of the primary metric: its decision (*Continue*, *Stop: a winner is clear*, *Stop: the
variants are equivalent* or *Stop: a meaningful difference is unlikely*) and, for each variant,
the chance it is the best, the expected loss, the posterior mean and the credible interval. It is
a second analysis and does not change the recommendation. When Bayesian analysis is on but the
results carry none, a line says it could not be computed. The method is described in
[Bayesian analysis](../api/bayesian.md).

#### Recommendation Meanings

| Recommendation | Meaning | Action |
|----------------|---------|--------|
| **SHIP VARIANT** | Treatment is significantly better | Deploy the treatment to all users |
| **KEEP CONTROL** | Control is significantly better | Do not ship the treatment |
| **CONTINUE TESTING** | Not enough data yet | Wait for more data before deciding |
| **INCONCLUSIVE** | The sample-ratio check failed: users were not split the way the traffic allocation says, so the numbers cannot be trusted | Do not ship on these results. Find and fix the cause of the uneven split, then run the experiment again |

#### Metric Comparison Table

For each metric and variant pair:

| Column | Meaning |
|--------|---------|
| **Sample Size** | Users assigned to this variant |
| **Value** | Conversion rate for this variant: the share of its users who sent the metric's event |
| **Improvement** | How much better/worse vs. control (e.g., +12.3%) |
| **p-value** | Probability the difference is due to chance (lower = more confident). Headed **Adjusted p-value** when a correction applies and a metric has two or more treatments with a p-value: the p-value after the experiment's correction, with the unadjusted value beneath it. A footnote under the table names the correction. |
| **Significance** | (adjusted) p-value < (1 - confidence level), e.g., < 0.05 for 95% confidence |

For each variant, `GET /api/v1/results/{experiment_id}` also returns `effect_size`,
`effect_size_label` (negligible / small / medium / large) and `confidence_interval`.

**Green** = statistically significant improvement
**Red** = statistically significant degradation
**Gray** = not statistically significant

#### Trend Chart

Shows conversion rate over time for each variant. Two modes:
- **Cumulative**: Running total from experiment start (recommended — smooths noise)
- **Daily**: Day-by-day rate (useful for detecting novelty effects or day-of-week patterns)

#### Sample Size

The **Sample Size** tab shows the planned sample size: how many users each variant needs to
detect the smallest lift you care about, and how far the experiment has got.

**Calculated from.** Every number the plan uses is shown, with where it came from:

- **Baseline conversion rate**: the rate observed in the control variant on the primary metric
  so far ("Observed in control so far: 11.8% (2,140 users)"), or the rate you typed ("Entered by
  you").
- **Minimum detectable effect**: the effect saved with the experiment ("Set when this experiment
  was created"), or 5% relative when none was saved ("Default — no effect was saved with this
  experiment"), unless you change it. Relative means 12% → 12.6%, not 17%. The saved effect is
  the primary metric's `expected_effect` (see
  [The planned effect](../api/endpoints.md#the-planned-effect)).
- **Power** (80%, 90% or 95%) and **significance** (always two-sided, which is what the
  results test): 1 − the experiment's confidence level unless you choose 5%, 1% or 10%. A level
  set through the API, such as 92%, shows as "8% (this experiment's setting)".
- **Correction**, for three or more variants: the experiment's correction unless you choose
  another. Bonferroni or Benjamini-Hochberg plans each comparison with the control at
  significance ÷ (variants − 1).

Change any of them and press **Recalculate**.

**Progress** is the smallest variant: "Smallest variant: 20,000 of 47,036 (43%)", then either
"43% of planned sample" or "Planned sample reached". The power shown is the power to detect your
planned lift at the current size, not the effect seen so far.

The tab also says when:

- **no users have been assigned yet**, or there is no rate to plan from (no control conversions,
  or every control user converted): type the rate you expect;
- **the metric is not a conversion**: every metric is analysed as a conversion today, so the
  plan is for a conversion rate. The Power Calculator plans conversion rates only too, and the
  tab says so;
- **a fixed sample size is a guide only**: a bandit, an uneven split, sequential testing or
  Bayesian analysis.

The **Planned sample** card on the Overview shows the same progress, for the plan the tab shows
before you change anything.

The tab loads its numbers separately from the rest of the page. If they cannot be loaded,
the tab says so and offers **Try again**; the Overview and the other tabs still work.

---

## Feature Flags

Feature flags let you control which users see a feature without deploying new code.

### Creating a Feature Flag

Navigate to **Feature Flags → New Flag**:

- **Key**: Identifier used in code, e.g., `new-checkout-flow`
- **Name**: Human-readable name
- **Description**: What this flag controls

### Controlling Rollout

**Manual rollout:**

Open the flag, move the **Rollout percentage** slider to the desired value and press **Save Changes**.

| Percentage | Meaning |
|------------|---------|
| 0% | No user outside a targeting rule gets the feature |
| 5% | 1 in 20 users sees the feature |
| 50% | Half of users |
| 100% | All users see the feature |

**Who gets the feature?**
The platform uses a deterministic hash of the user's ID. The same user always gets the same result (sticky assignment). This means:
- If you set 5%, the same 5% of users will consistently see the feature
- If you increase to 50%, the original 5% are still included (plus 45% more)

**Adding targeting rules:**

Optionally restrict to specific users:
- Enable only for users in the US
- Only for users on the "enterprise" plan
- Only for users who signed up before a certain date

Every condition needs an attribute, an operator and (for most operators) a value. If one is
missing, **Create Feature Flag** or **Save Changes** stops and the problem is shown beside the
targeting rules; a condition the server cannot apply is reported there too.

Rules written through the API in a shape the rule builder cannot show are displayed
read-only. Saving other changes, such as the rollout percentage, leaves them as they are;
**Replace rules** starts again with the builder.

### Scheduled Rollouts

For gradual rollouts on a schedule:

Create a rollout schedule through the API with `POST /api/v1/rollout-schedules/` (the stages
go in its `stages` list; see [Rollouts](../feature-flags/rollouts.md) for the full contract),
then start it with
`POST /api/v1/rollout-schedules/{schedule_id}/activate`. The flag's page in the dashboard
shows the schedule, its status and its stages, read-only. A schedule like this one:

| Stage | Target % | Trigger Type | Start Date |
|-------|----------|-------------|-----------|
| Stage 1 | 5% | Time-based | Apr 1, 2026 |
| Stage 2 | 25% | Time-based | Apr 8, 2026 |
| Stage 3 | 100% | Manual | — |

The platform automatically advances time-based stages. A manual stage waits until you
advance it with `POST /api/v1/rollout-schedules/stages/{stage_id}/advance`.

### Disabling / Rolling Back

To disable a flag, turn it off with the switch at the top of its page; it then
shows **Not serving**. Setting the rollout percentage to 0 is not the same: users matched by a
targeting rule keep the flag.

If you have safety monitoring configured, the platform can roll the flag back automatically when
its error rate or latency breaches a threshold. Automatic rollback is off by default: a superuser
turns it on with **Enable automatic rollbacks** in **Admin → Safety**. A rollback to 0% turns the
flag off for every user and pauses its rollout schedule (see
[Safety Monitoring](../feature-flags/safety.md#what-a-rollback-changes)).

### Archived Flags

An archived flag is retired and never served. It cannot be turned on until it is
unarchived: the API refuses every request that would turn it on with "This flag is
archived. Unarchive it before turning it on." Turning it off succeeds and changes
nothing. Archiving and unarchiving are API calls for now (bulk toggle with
`archive`, and `POST /api/v1/feature-flags/{id}/unarchive`, which is beta); an
unarchived flag comes back inactive. See
[Archived flags](../feature-flags/create.md#archived-flags).

---

## Analytics & Reporting

### Viewing All Experiments

**Experiments list** shows all experiments with their name, status, type, number of
variants and creation date.

Filter by status: All / Draft / Active / Paused / Completed.

### Exporting Results

Export results through the API:
- **CSV export**: `GET /api/v1/export/variants` downloads per-variant results as CSV
  (`?format=json` for JSON); see [Data Export](../api/data-export.md)
- **Full results**: `GET /api/v1/results/{experiment_id}` returns JSON with all statistics

---

## Advanced Statistical Methods

### Sequential Testing — Stop Experiments Early

Standard A/B tests require a fixed sample size decided upfront. Sequential testing lets you monitor results continuously and stop as soon as you have enough evidence — without inflating your false positive rate.

When sequential testing is on for an experiment, its results page has a **Sequential** tab.
The API route is:
```text
GET /api/v1/results/{experiment_id}/sequential
```

The platform uses **mSPRT** (mixture Sequential Probability Ratio Test). When `recommended_action` is `stop_for_effect`, it is safe to stop: there is an effect, but it may be a harm, so the sign of the confidence sequence and the metric's `lower_is_better` decide whether to ship. An experiment running well past its expected duration is flagged `at_risk`; that is advisory, not a reason to conclude there is no effect. See the [Sequential Testing Guide](../api/sequential-testing.md) for details.

---

### CUPED — Reach Significance Faster

CUPED reduces result noise by adjusting for each user's own events in the days before they were assigned; how much depends on how well that history predicts the outcome.

**When to use it:** When your users sent the metric's event before the experiment (returning customers who bought before, say). Send that history to the tracking API with no experiment key, before you start the experiment: history the server receives after a user was assigned is ignored for that user.

The dashboard has no CUPED tab; it is available through the API:
```text
GET /api/v1/results/{experiment_id}/cuped
```

See the [CUPED Guide](../api/cuped.md) for sending history and reading the result.

---

### Dimensional Analysis — Did the Effect Vary by Segment?

After an experiment concludes, use dimensional analysis to understand whether the treatment worked differently for different groups (mobile vs desktop, new vs returning users, etc.).

**Important:** Segment findings are always exploratory. Use them to generate hypotheses for follow-up experiments, not as final conclusions.

Access via the **Breakdowns** tab on any experiment results page, or via the route below,
where `segment_by` is the dimension, such as `device`:
```text
GET /api/v1/experiments/{experiment_id}/segmented-results/{segment_by}
```

See the [Dimensional Analysis Guide](../api/dimensional-analysis.md).

---

### Multi-Armed Bandit — Maximize Conversions During the Experiment

A bandit experiment automatically shifts traffic toward the better-performing variant as data accumulates. Use this when maximizing conversions during the experiment matters more than getting precise effect size estimates.

Set `optimization_type` to `thompson_sampling`, `ucb1`, or `epsilon_greedy` when creating an experiment. See the [Multi-Armed Bandit Guide](../api/multi-armed-bandit.md).

A bandit experiment's page has a **Current traffic weights** panel: each variant's share of new
users, with its pulls, successes and conversion rate, and when the weights were last computed.
Every role can read it, and nothing in it changes the experiment. It loads once and again when you
press **Refresh**. Before the first update it says so, and new users follow each variant's
starting allocation until then. The dashboard shows the current weights only, with no history.

---

### Interaction Detection — Are Your Experiments Interfering?

When multiple experiments run simultaneously on overlapping user populations, they can distort each other's results. Run an interaction scan to find the pairs that share users, then ask the pair route whether one experiment's effect differs across the other's arms (beta). The scan measures overlap only.

Access via:
```text
GET /api/v1/interactions/scan
GET /api/v1/interactions/{exp_a_id}/{exp_b_id}
```

If high-risk pairs are found, add the experiments to a [Mutual Exclusion Group](../api/mutual-exclusion-groups.md) to prevent overlap in future runs. See the [Interaction Detection Guide](../api/interaction-detection.md).

---

## Getting Help

- **API Documentation**: http://localhost:8000/api/v1/docs
- **Technical Guide**: See [Technical Guide](../architecture/technical-guide.md) for implementation details
- **Issues**: https://github.com/Controlvar/controlvar/issues

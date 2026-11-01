# Limitations

What Controlvar does not do, and what happens instead. Where a public issue tracks a change,
the row links it.

## Feature flags

| What you might expect | What happens instead |
|---|---|
| A flag that serves one of several values | A flag serves two values: its on value to the users it is on for (a matching targeting rule's percentage, or the rollout percentage), and its off value to everyone else and to everyone while it is off. No rule or rollout splits users among three or more values. `GET /api/v1/feature-flags/user/{user_id}` answers every flag as on or off. For three or more experiences, run a multivariate experiment: each variant carries a `configuration`, which `POST /api/v1/tracking/assign` returns. |
| A flag's value in every SDK | An SDK reads a flag's value only where its page documents a value method; every other SDK reads a value flag as on or off (`enabled` is `true` when the user gets the on value). `GET /api/v1/feature-flags/evaluate/{key}` returns the value in `value`. |

## Experiments and results

| What you might expect | What happens instead |
|---|---|
| Every event value counted in a revenue, duration or custom metric | A value that is not a number, is infinite, or is larger than 10^12 in size, in the unit it was sent in, adds nothing to its user's total; the event still counts its user in `conversions`. A count metric counts events and does not read their value. |
| An event sent twice counted once | It is stored and counted twice: a count metric counts it again, and a revenue, duration or custom metric adds its value again. A request that timed out may have been stored, so sending it again can double it ([#1543](https://github.com/Controlvar/controlvar/issues/1543)). A conversion rate counts each user once and is not affected. See [How events count](../guides/how-events-count.md). |
| A revenue, count, duration or custom metric's sequential analysis, Bayesian analysis, sample-size plan, daily series and Live tab by its values | Each is of the share of users who sent the metric's event, as for a conversion. CUPED does not analyse these metrics. |
| A breakdown by segment of a revenue, count, duration or custom metric by its values | The Breakdowns view of such a metric shows, in its column headed Mean, the share of users who sent the metric's event in each segment. The metric's headline row is its mean per user. |
| The average time one event takes, such as page load time | A duration metric is each user's total of its events' values, 0 for a user with none, averaged over every assigned user: a variant whose users send the event less often reads as less time. No metric averages over events rather than users. |
| Intervals on a treatment's difference adjusted for the experiment's correction | The intervals on a treatment's difference from the control, on its relative change and on its impact are not adjusted for comparing several treatments with the control, while `is_significant` is (Benjamini-Hochberg by default). With two or more treatments an interval can exclude 0 on a treatment that is not significant. For a metric analysed as a share of users, the interval and Fisher's exact test can also disagree near the threshold, either way. |
| An interval on the relative change for every treatment | For a proportion, `relative_improvement_pct_ci` is null only when an arm has no users or the control has no conversions; at low counts it is wide: a treatment with 6 converting users out of 200 against a control with 2 out of 200 gives -34.2% to +1,206.3% at the 0.95 level. For a mean it is null when the control's mean plus or minus its margin reaches 0, where Fieller's set of ratios is unbounded. The interval on the difference is still reported. |
| A relative-change interval bound above +1,000,000% | The dashboard shows a bound beyond 1,000,000% in either direction as 'above +1,000,000%' or 'below −1,000,000%'; the API returns the exact number. |
| The impact projected to all traffic, to a later period, or in a currency | `incremental_impact` is the difference from the control times the treatment's own assigned users so far, as a plain number: nothing projects it further, and no currency or unit is read or added. |
| The intervals and the impact in the data export | The export has no interval or impact column; `GET /api/v1/results/{experiment_id}` reports them. |
| An interval on the difference, or an impact, in a warehouse analysis of a mean | A warehouse analysis gives a proportion metric's treatments their difference from the control with its interval, the relative change's interval and the impact, as `/results` does. A mean metric's treatments get none of them; their p-value and relative change are reported as before. |
| A traffic allocation for the experiment as a whole | There is none. Variant weights must add up to exactly 100%, or the request is refused with `422`. Users are left out only by targeting rules, a mutual exclusion group or the global holdout. |
| One user known by two ids, such as a visitor who signs in | No identify or alias call: a user who signs in under a new id mid-funnel gets a second assignment. Assign with the id you will track purchases with. |
| An LLM experiment that picks the variant with the lowest value | An LLM experiment's winner is the variant with the highest mean of the metric it is compared on: the business metric when every variant has one, otherwise the judge score. There is no lower-is-better setting for it. |
| A warehouse analysis of a metric where lower is better | A warehouse analysis treats every metric as higher is better: a warehouse metric source has no lower-is-better setting, so its results report `lower_is_better: false` and name a winner only for a significant increase ([#1386](https://github.com/Controlvar/controlvar/issues/1386)). |

## Deployments

| What you might expect | What happens instead |
|---|---|
| Staging and production inside one deployment | One deployment is one environment: flags, experiments and API keys carry no environment field. Run staging and production as separate deployments, each with its own API keys. |
| Events streamed to Kinesis, OpenSearch or DynamoDB | The API stores events in PostgreSQL and computes results from there. The full-profile CDK creates a Kinesis stream and an OpenSearch domain, and the API writes to neither. |

## Sign-in and access

| What you might expect | What happens instead |
|---|---|
| Multi-factor sign-in | Not available: a local account signs in with its email and password alone. |
| SAML sign-in | Not available. OIDC sign-in is, with the `sso` module (full profile). |
| Custom roles and direct permission grants that change access | The `rbac` module records them and shows them, but no permission check reads them: each user's built-in role decides ([#891](https://github.com/Controlvar/controlvar/issues/891)). |

## Data

| What you might expect | What happens instead |
|---|---|
| A retention policy, or a tool that erases one person's data | Neither exists. Nothing deletes events, assignments or audit records by age, and no route deletes the events of one of your app's users. One setting purges the platform's own analysis history: `ANALYSIS_HISTORY_RETENTION_DAYS` (default 90) deletes analysis snapshots and bandit-state history older than that, each time the metrics scheduler runs. |

## Split URL testing

| What you might expect | What happens instead |
|---|---|
| Traffic split between URLs at the edge | The `split_url` module's edge router has no configuration source in this release, so it splits no traffic ([#393](https://github.com/Controlvar/controlvar/issues/393)). |

## SDKs

| What you might expect | What happens instead |
|---|---|
| Flag targeting from a native mobile SDK | iOS, Android, React Native and Flutter send only the user id on flag evaluation, so rules on attributes do not match ([#438](https://github.com/Controlvar/controlvar/issues/438)). Experiment assignment sends them. |
| Every SDK on a package registry | The JavaScript SDK is on npm and the Python SDK is on PyPI. The Go SDK installs by module path, and every other SDK installs from a clone of this repository. |
| An event tracked without an experiment key counted, however late it comes | In the Go, Java, .NET, Ruby, PHP, Elixir, iOS, Android, Flutter, React Native and edge SDKs, an event tracked without a key reaches only the experiments and flags still in that client's cache (5 minutes by default, 60 seconds in the edge SDK), and is not sent at all once they have expired. Pass the experiment key on a conversion that can come later. |

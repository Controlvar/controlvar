# Limitations

What Controlvar does not do, and what happens instead. Where a public issue tracks a change,
the row links it.

## Feature flags

| What you might expect | What happens instead |
|---|---|
| A flag that serves one of several values | A flag serves two values: its on value to the users it is on for (a matching targeting rule's percentage, or the rollout percentage), and its off value to everyone else and to everyone while it is off. No rule or rollout splits users among three or more values. `GET /api/v1/feature-flags/user/{user_id}` answers every flag as on or off. For three or more experiences, run a multivariate experiment: each variant carries a `configuration`, which `POST /api/v1/tracking/assign` returns. |
| A flag's value in every SDK | An SDK reads a flag's value only where its page documents a value method; every other SDK reads a value flag as on or off (`enabled` is `true` when the user gets the on value). `GET /api/v1/feature-flags/evaluate/{key}` returns the value in `value`. |
| A flag targeted on a value inside a JSON attribute | Flag targeting refuses the `json_path` operator with `422`, because flag evaluation does not implement it. Experiment targeting accepts it. |
| A flag targeted on a percentage bucket of an attribute | Flag targeting refuses the `percentage_bucket` operator with `422`, because flag evaluation does not implement it. Experiment targeting accepts it. |
| A flag turned off for every user at once | Each client keeps the answer it has until its cache expires; a React or React Native component already on screen keeps its answer until it mounts again; and an iOS, Android, Flutter or React Native app that cannot reach the API keeps serving the last answer it stored. See [Turning a Flag Off](../feature-flags/kill-switch.md). |

## Experiments and results

| What you might expect | What happens instead |
|---|---|
| Every event value counted in a revenue, duration or custom metric | A value that is not a number, is infinite, or is larger than 10^12 in size, in the unit it was sent in, adds nothing to its user's total; the event still counts its user in `conversions`. A count metric counts events and does not read their value. |
| An event sent twice counted once | Only an event with an `event_id` is recognised when it is sent again. Without one it is stored and counted twice: a count metric counts it again, and a revenue, duration or custom metric adds its value again. A request that timed out may have been stored, so sending it again without an `event_id` can double it. A conversion rate counts each user once and is not affected. See [How events count](../guides/how-events-count.md#sending-an-event-again). |
| A revenue, count, duration or custom metric's sequential analysis, Bayesian analysis, sample-size plan, daily series and Live tab by its values | Each is of the share of users who sent the metric's event, as for a conversion. CUPED does not analyse these metrics. |
| A breakdown by segment of a revenue, count, duration or custom metric by its values | The Breakdowns view of such a metric shows, in its column headed Mean, the share of users who sent the metric's event in each segment. The metric's headline row is its mean per user. |
| The average time one event takes, such as page load time | A duration metric is each user's total of its events' values, 0 for a user with none, averaged over every assigned user: a variant whose users send the event less often reads as less time. No metric averages over events rather than users. |
| Outlier control on a revenue, duration or custom metric (winsorization, trimming) | None is applied. A revenue, duration or custom metric is each user's total, averaged; one very large user moves the mean and widens the interval. `variance_reduction_config.method = winsorization` is stored and clips nothing. Only a value that is not a number or exceeds 10^12 in size is dropped, as the first row of this section says. |
| A ratio metric (revenue per order, clicks per session) | There is no ratio metric type. Metric types are conversion, revenue, count, duration and custom, each averaged over assigned users: see the row on the average time one event takes. Revenue per order can only be computed outside the platform. |
| A sample-ratio mismatch that stops the experiment or removes its winner | Under a mismatch, `recommendation` is `INCONCLUSIVE` in `GET /api/v1/results/{experiment_id}`, in the data export and in the report. `winner_variant`, `has_winner`, the Bayesian decision and the sequential panel are unchanged. |
| A variant that is no better recommended to ship no more often than α says, whatever the split | On zero-inflated, right-skewed revenue (most users spend nothing, a few spend a lot), a lopsided split recommended shipping a variant that was no better more often than α says. Measured in 2,000 A/A experiments (both variants the same) at each split, at α = 0.05, with 5% of users buying, order values lognormal and repeat purchases: with 600 users in the control and 5,400 in the treatment, the treatment was recommended to ship in 150 (7.5%), where about 50 (2.5%) are expected in that direction, and there the larger variant was the one wrongly named; with 3,000 users in each, in 46, within the expected rate. These are the sizes measured; no size is known at which a lopsided split meets α. |
| Intervals on a treatment's difference adjusted for the experiment's correction | The intervals on a treatment's difference from the control, on its relative change and on its impact are not adjusted for comparing several treatments with the control, while `is_significant` is (Benjamini-Hochberg by default). With two or more treatments an interval can exclude 0 on a treatment that is not significant. For a metric analysed as a share of users, the interval and Fisher's exact test can also disagree near the threshold, either way. |
| An interval on the relative change for every treatment | For a proportion, `relative_improvement_pct_ci` is null only when an arm has no users or the control has no conversions; at low counts it is wide: a treatment with 6 converting users out of 200 against a control with 2 out of 200 gives -34.2% to +1,206.3% at the 0.95 level. For a mean it is null when the control's mean plus or minus its margin reaches 0, where Fieller's set of ratios is unbounded. The interval on the difference is still reported. |
| A relative-change interval bound above +1,000,000% | The dashboard shows a bound beyond 1,000,000% in either direction as 'above +1,000,000%' or 'below −1,000,000%'; the API returns the exact number. |
| The impact projected to all traffic, to a later period, or in a currency | `incremental_impact` is the difference from the control times the treatment's own assigned users so far, as a plain number: nothing projects it further, and no currency or unit is read or added. |
| The intervals and the impact in the data export | The export has no interval or impact column; `GET /api/v1/results/{experiment_id}` reports them. |
| An interval on the difference, or an impact, in a warehouse analysis of a mean | A warehouse analysis gives a proportion metric's treatments their difference from the control with its interval, the relative change's interval and the impact, as `/results` does. A mean metric's treatments get none of them; their p-value and relative change are reported as before. |
| A traffic allocation for the experiment as a whole | There is none. Variant weights must add up to exactly 100%, or the request is refused with `422`. Users are left out only by targeting rules, a mutual exclusion group or the global holdout. |
| One user known by two ids, such as a visitor who signs in | No identify or alias call: a user who signs in under a new id mid-funnel gets a second assignment. Assign with the id you will track purchases with. |
| An LLM experiment that picks the variant with the lowest value | An LLM experiment's winner is the variant with the highest mean of the metric it is compared on: the business metric when every variant has one, otherwise the judge score. There is no lower-is-better setting for it. |

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
| A custom role that removes access, or that grants administration | Custom roles and direct permission grants (the `rbac` module) only add, and only to experiments, flags and reading the audit log; never administration or the compliance audit trail. Administration depends on the built-in role or the superuser flag; to restrict someone, give them a lower built-in role. See [What a role or grant cannot give](../api/rbac.md#what-a-role-or-grant-cannot-give). |
| Assigning roles and granting permissions in the dashboard | The Roles page defines roles; assigning them and granting permissions go through the [RBAC API](../api/rbac.md). |

## Data

| What you might expect | What happens instead |
|---|---|
| A retention policy, or a tool that erases one person's data | Neither exists. Nothing deletes events, assignments or audit records by age, and no route deletes the events of one of your app's users. One setting purges the platform's own analysis history: `ANALYSIS_HISTORY_RETENTION_DAYS` (default 90) deletes analysis snapshots and bandit-state history older than that, each time the metrics scheduler runs. |

## Split URL testing

| What you might expect | What happens instead |
|---|---|
| Traffic split between URLs with nothing to install | Your server or edge must ask the API where to send each visitor and redirect them; a site with no server or edge of its own in front of its pages cannot split, because nothing redirects in the browser. On AWS, the CloudFront edge router (beta) is one you deploy yourself. |
| Split URL experiments on the AWS installer | It installs the core profile, which does not run them. |
| Each visitor counted once, whatever their browser does | A browser that blocks cookies is a new visitor on each visit, with a new assignment. |
| No limit on how many visitors a site splits | A per-address rate limit, below. |

Both split URL routes have the limit `POST /api/v1/tracking/assign` has: 6,000 requests a
minute per client address, per route. `SDK_RATE_LIMIT_PER_MINUTE` raises it, for every SDK
route and every address. Over the limit the API answers `429`, and the reference integration
serves the page with `X-Split-URL: failed` and logs `split_url_failed status=429`. The
CloudFront edge router does the same: it serves the page with `X-Split-URL: failed` (a header
that rests on an AWS behaviour not yet verified, see
[AWS integration](../integrations/aws.md#the-split-url-edge-router)), and its log line carries
`"status": "429"`. Above the
limit, a returning visitor whose assignment is a treatment is served the control page while
their conversions still count for the treatment; a site with more than about 100 entry-page
views a second is in that range.

## SDKs

| What you might expect | What happens instead |
|---|---|
| Flag targeting from a native mobile SDK | iOS, Android and Flutter send only the user id on flag evaluation, so rules on attributes do not match ([#438](https://github.com/Controlvar/controlvar/issues/438)). Experiment assignment sends them. React Native sends them on flag evaluation too. |
| Every SDK on a package registry | The JavaScript, React and React Native SDKs and the OpenFeature provider for Node are on npm; the Python SDK and the OpenFeature provider for Python are on PyPI; the .NET SDK is on NuGet. The Go SDK installs by module path, and every other SDK installs from a clone of this repository. |
| An `event_id` sent by the SDK, so that an event sent again is stored once | The Java SDK sends one with every event ([Event ids](../sdk/java.md#event-ids)). The JavaScript SDK sends the `eventId` you give it and the Python SDK the `event_id` you give it ([JavaScript](../sdk/javascript.md#event-ids), [Python](../sdk/python.md#event-ids)). No other SDK has an `event_id` option yet: not the React, React Native, edge, Go, Android, iOS, Flutter, .NET, PHP, Ruby or Elixir SDK, nor the OpenFeature providers. Send an event that may be sent again to `POST /api/v1/tracking/track` or `POST /api/v1/tracking/batch` with its `event_id` ([#1543](https://github.com/Controlvar/controlvar/issues/1543)). |
| An event tracked without an experiment key counted, however late it comes | In the Go, Java, .NET, Ruby, PHP, Elixir, iOS, Android, Flutter, React Native and edge SDKs, an event tracked without a key reaches only the experiments and flags still in that client's cache (5 minutes by default, 60 seconds in the edge SDK), and is not sent at all once they have expired. Pass the experiment key on a conversion that can come later. |

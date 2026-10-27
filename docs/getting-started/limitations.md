# Limitations

What Controlvar does not do, and what happens instead. Where a public issue tracks a change,
the row links it.

## Feature flags

| What you might expect | What happens instead |
|---|---|
| A flag that serves one of several values | A flag is on or off for each user. Evaluation answers `"config": null` for every flag, a request that sends a flag a `variants` field is refused with `422`, and `default_value` accepts only `false`. To serve one of several experiences, run a multivariate experiment: each variant carries a `configuration`, which `POST /api/v1/tracking/assign` returns. |

## Experiments and results

| What you might expect | What happens instead |
|---|---|
| A revenue, count or duration metric analysed by its values | Every metric is analysed as a conversion: whether each user sent the metric's event. A metric's type is stored, and its events' values are not analysed. |
| A traffic allocation for the experiment as a whole | There is none. Variant weights must add up to exactly 100%, or the request is refused with `422`. Users are left out only by targeting rules, a mutual exclusion group or the global holdout. |
| One user known by two ids, such as a visitor who signs in | No identify or alias call: a user who signs in under a new id mid-funnel gets a second assignment. Assign with the id you will track purchases with. |
| A bandit that moves traffic toward the better variant of a lower-is-better metric | A bandit (an experiment whose `optimization_type` is not `fixed`) moves traffic toward the variant whose users send the primary metric's event most often. It does not read the metric's `lower_is_better`, so on a metric such as uninstalls or errors it moves traffic toward the worse variant. Use a fixed split for such a metric ([#1385](https://github.com/Controlvar/controlvar/issues/1385)). |
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

# What Controlvar does today

The main capabilities of Controlvar, one line each, grouped by area; each line links the page that explains it.
**(beta)** marks a capability its own page calls beta; each route's level is in [API stability](api/stability.md). **(full profile)** marks a capability of the optional modules ([Modules & Profiles](getting-started/modules.md)).

## Experiments and statistics

- [A/B and multivariate experiments](guides/user-guide.md#creating-an-experiment): the server assigns each user a variant, and the same user always gets the same one.
- [Frequentist results](api/endpoints.md#how-results-are-judged): judged at the experiment's confidence level, with Benjamini-Hochberg (the default) or Bonferroni correction across its variants.
- [Sequential testing](api/sequential-testing.md) (beta): mSPRT with always-valid confidence intervals, so you can read results at any time.
- [Bayesian analysis](api/bayesian.md): each variant's chance to be best, the expected loss of choosing it, and a stopping recommendation.
- [CUPED](api/cuped.md) (beta): narrower intervals from each user's pre-experiment history, through the API.
- [Sample size planning](statistics/power-analysis.md): the Power Calculator and guided setup before an experiment, and the results page's Sample Size tab during it.
- [Multi-armed bandits](api/multi-armed-bandit.md): new users go more often to the variants that perform better.
- [Mutual exclusion groups and a global holdout](api/mutual-exclusion-groups.md): a user joins at most one experiment of a group, and the holdout keeps a share of new users out of every experiment (its results are beta).
- [Breakdowns](api/dimensional-analysis.md): results by a user attribute such as device or country, flagged when the effect differs between segments.
- [Interaction detection](api/interaction-detection.md): a scan for running experiments that share users, and a test of one pair of them (beta).
- [Live results](websocket-streaming.md): a WebSocket that sends an experiment's results every 30 seconds by default.
- [Scheduling](guides/user-guide.md#creating-an-experiment): start and end dates that the scheduler acts on, and a scheduled resume after a pause.
- [Split URL experiments](api/split-url.md) (full profile, beta): each variant is its own page, and your server or edge sends each visitor to theirs.
- [Warehouse analysis](api/warehouse-analytics.md) (full profile, beta): an experiment's analysis run on tables in your own Snowflake or BigQuery warehouse.
- [LLM experiments](llm-evaluation/overview.md): users split between model variants, with an optional LLM judge that scores the answers.

## Feature flags and rollouts

- [Feature flags](feature-flags/create.md): on or off for each user, or serving a string, number or JSON value instead.
- [Rollout percentage](feature-flags/create.md): a flag is on for a share of users, and a targeting rule can give the users it matches their own share.
- [Rollout schedules](feature-flags/rollouts.md): a flag's percentage raised in stages, each started at its date or by hand.
- [Safety monitoring](feature-flags/safety.md): each active flag's error rate and latency checked against your thresholds, and an automatic rollback on a breach when you turn that on.
- [Turning a flag off](feature-flags/kill-switch.md): the server answers off from the next evaluation, and each SDK once its cached answer expires.
- [Stale flags](feature-flags/stale-flags.md): the flag list marks a flag that looks ready to remove, and says why; listing changes no flag.
- [Slack alerts](api/alerting.md): a message when the safety monitor rolls a flag back, a rollout stage advances, or the scheduler starts, resumes or completes an experiment.

## Targeting and segments

- [Targeting rules](Enhanced_Rules_Engine_Reference.md): more than 20 operators, including semantic versions, geo-distance and time windows, answered the same way for flags and experiments.
- [Experiment eligibility](api/endpoints.md#assign-user-to-experiment): a new user whom the global holdout, an exclusion group or the experiment's targeting keeps out gets the control variant, and nothing is recorded for them.
- [Segments](guides/segments.md): a named group of users, by rules or by an uploaded list of user ids, that a flag or an experiment can target (adding and removing ids is beta).

## SDKs and the MCP server

- [16 SDKs](sdk-guide.md#sdk-status) for experiment assignment, flag evaluation and event tracking; 13 of them run against a live API in CI.
- [Released SDKs](sdk-guide.md#sdk-status): the JavaScript, React, React Native, Python, Java and .NET SDKs and the OpenFeature providers are on npm, PyPI, Maven Central or NuGet, and the Go SDK installs by module path.
- [Local evaluation](sdk/local-evaluation.md) (beta): the JavaScript and Python SDKs answer flags in a server process from a downloaded ruleset, with no request per evaluation.
- [Exposure hook](sdk/exposure-hook.md): the JavaScript, Python, Go, React, React Native, Java and .NET SDKs call your function when a user is enrolled, so you can send the exposure to your analytics tool.
- [The MCP server](mcp-server.md): an AI assistant starts it on your computer to read your flags, experiments and results with your API key; it changes no flag or experiment.

## The dashboard

- [Guided setup](guides/experiment-wizard.md): an A/B or multivariate experiment in five steps, with an optional sample-size estimate.
- [The results page](guides/user-guide.md#reading-results): each variant's users, the planned sample reached, the leading variant and the sample ratio check.
- [Experiment pages](guides/dashboard-and-api.md): start, pause, resume and complete an experiment, clone it, and edit, schedule or delete a draft.
- [Administration](guides/dashboard-and-api.md): users and their roles, API keys, and the audit log, which downloads as CSV or JSON (the download is beta).
- [What has a screen and what is API only](guides/dashboard-and-api.md), capability by capability.

## Self-hosting and deployment

- [Docker Compose](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/README.md): one host, on the published images; it builds nothing.
- [Kubernetes](self-hosting/kubernetes.md): a Helm chart, which CI installs on a kind cluster.
- [AWS CDK](self-hosting/cdk.md): stacks that provision the API, the dashboard and their data stores in your AWS account.
- [AWS installer](self-hosting/aws-install.md) (beta): one command that runs the CDK steps for you, for the core profile in us-west-2.
- [Signed releases](self-hosting/releases.md#verifying-what-you-pulled): each image is signed with cosign and carries an SBOM, and you can verify both.
- [Two profiles](getting-started/modules.md): core, and full with the optional modules; `GET /api/v1/modules` says which one is running.
- [Monitoring](self-hosting/monitoring.md): Prometheus metrics at `/metrics`, structured logs and health probes.

## Security, roles and audit

- [Four built-in roles](rbac/README.md): Admin, Developer, Analyst, Viewer.
- [Custom roles and grants](api/rbac.md) (full profile): roles beyond the four, and permissions granted to one user, optionally until a date.
- [Audit log](api/audit-logging.md): the changes people make through the API to flags, experiments, rollout schedules and API keys, and no API edits or deletes an entry.
- [Signed audit events and compliance reports](api/compliance.md) (full profile): HMAC-signed events, SOC 2 and ISO 27001 reports, and an audit export.
- [API keys](security/api-keys.md): shown once and stored hashed, with scopes for local evaluation and the MCP server.
- [Single sign-on](auth/sso.md) (full profile): OIDC providers, with an account created at a user's first sign-in and roles mapped from the provider's groups.
- [HIPAA module](hipaa/overview.md) (full profile): PHI encryption, a PHI access log and BAA records.
- [Workspaces](workspaces/overview.md) (full profile): users grouped into teams; a workspace role does not decide access to experiments and flags.

What Controlvar does not do is in [Limitations](getting-started/limitations.md), and what is planned, Now, Next and Later, is in the [roadmap](https://github.com/Controlvar/controlvar/blob/main/ROADMAP.md).

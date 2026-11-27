# What the Dashboard Does, and What Is API Only

The table lists each capability and where you do it today:

- **Dashboard**: a screen in the dashboard does it.
- **API only**: it works through the API, with no screen yet. The guide in the last column shows
  the requests.

| Capability | Where you do it | Guide |
|---|---|---|
| Create an experiment, with guided setup or the single-page form | Dashboard | [Running experiments](user-guide.md) |
| Start, pause, resume and complete an experiment | Dashboard | [Running experiments](user-guide.md) |
| Change an experiment's targeting while it is a draft or paused | Dashboard | [Running experiments](user-guide.md) |
| Create a feature flag | Dashboard | [Creating feature flags](../feature-flags/create.md) |
| Turn a feature flag on or off | Dashboard | [Creating feature flags](../feature-flags/create.md) |
| Set a flag's targeting rules and rollout percentage | Dashboard | [Creating feature flags](../feature-flags/create.md) |
| See a flag's rollout schedule and its stages | Dashboard | [Gradual rollouts](../feature-flags/rollouts.md) |
| Roll a feature flag back from the safety page | Dashboard | [Safety monitoring](../feature-flags/safety.md) |
| Set a feature flag's per-flag safety config (error-rate and latency thresholds, rollback percentage) | API only | [Safety monitoring](../feature-flags/safety.md#configuring-per-flag-safety) |
| Invite users, change their role and whether they are active | Dashboard | [Authentication user guide](../auth/auth-user-guide.md) |
| Change your own password (local sign-in) | Dashboard | [Authentication user guide](../auth/auth-user-guide.md#changing-your-password) |
| Read the audit log, and download it as CSV or JSON | Dashboard | [Audit logging](../api/audit-logging.md) |
| Give each variant a configuration (a JSON payload) when creating an experiment | Dashboard | [Running experiments](user-guide.md) |
| Turn on Bayesian analysis when creating an experiment | Dashboard | [Bayesian analysis](../api/bayesian.md) |
| See on the experiment page whether Bayesian analysis is on | Dashboard | [Bayesian analysis](../api/bayesian.md) |
| See on the experiment page whether sequential testing (beta) is on | Dashboard | [Sequential testing](../api/sequential-testing.md) |
| See on the experiment page its start and end dates | Dashboard | [Running experiments](user-guide.md) |
| See on the experiment page its exclusion group | Dashboard | [Mutual exclusion groups and global holdout](../api/mutual-exclusion-groups.md) |
| See on the experiment page whether a global holdout is active | Dashboard | [Mutual exclusion groups and global holdout](../api/mutual-exclusion-groups.md#global-holdout) |
| Segments: list, create, and upload a list of user ids | Dashboard | [Segments](segments.md) |
| The sample-ratio check on the results page | Dashboard | [Running experiments](user-guide.md) |
| Bayesian results: chance to be best, expected loss and credible intervals | Dashboard | [Bayesian analysis](../api/bayesian.md) |
| Sequential results (beta): whether to stop, and the confidence sequence | Dashboard | [Sequential testing](../api/sequential-testing.md) |
| See a bandit experiment's current traffic weights | Dashboard | [Multi-armed bandits](../api/multi-armed-bandit.md) |
| Clone an experiment | Dashboard | [API endpoints](../api/endpoints.md) |
| Delete a draft experiment | Dashboard | [API endpoints](../api/endpoints.md) |
| Edit a draft experiment's name, description and hypothesis | Dashboard | [API endpoints](../api/endpoints.md) |
| Change a draft experiment's variants or metrics | Dashboard | [API endpoints](../api/endpoints.md) |
| Schedule a draft experiment's start and end dates | Dashboard | [Running experiments](user-guide.md) |
| Schedule a paused experiment's resume | Dashboard | [Running experiments](user-guide.md#pausing-and-stopping) |
| Create or edit a rollout schedule and its stages | Dashboard | [Gradual rollouts](../feature-flags/rollouts.md#in-the-dashboard) |
| Activate, pause, resume or cancel a rollout schedule, and advance a manual stage | Dashboard | [Gradual rollouts](../feature-flags/rollouts.md#in-the-dashboard) |
| Set a rollout schedule's own start and end dates, maximum percentage and minimum stage duration | Dashboard | [Gradual rollouts](../feature-flags/rollouts.md#in-the-dashboard) |
| Delete a feature flag | Dashboard | [Deleting a flag](../feature-flags/create.md#deleting-a-flag) |
| Archive and unarchive a feature flag | Dashboard | [Archived flags](user-guide.md#archived-flags) |
| Create a bandit experiment | Dashboard | [Multi-armed bandits](../api/multi-armed-bandit.md) |
| Set a bandit's weights by hand | Dashboard | [Multi-armed bandits](../api/multi-armed-bandit.md) |
| A bandit's weight history (beta) | Dashboard | [Multi-armed bandits](../api/multi-armed-bandit.md) |
| Global holdouts: create and activate; read their results (beta) | Dashboard | [Mutual exclusion groups and global holdout](../api/mutual-exclusion-groups.md) |
| Mutual exclusion groups | Dashboard | [Mutual exclusion groups and global holdout](../api/mutual-exclusion-groups.md) |
| Split-URL experiments (full profile) | Dashboard | [Split URL testing](../api/split-url.md) |
| Read CUPED-adjusted results (beta), once CUPED is on | Dashboard | [CUPED](../api/cuped.md) |
| The interaction scan and the pair test (beta) | Dashboard | [Interaction detection](../api/interaction-detection.md) |
| Download results as CSV | Dashboard | [Exporting results](user-guide.md#exporting-results) |
| The effect size in the metric comparison table | Dashboard | [Running experiments](user-guide.md) |
| Each variant's confidence interval on the results page | Dashboard | [Running experiments](user-guide.md) |
| Bayesian priors, loss threshold and credible level | Dashboard | [Bayesian analysis](../api/bayesian.md) |
| Turn on sequential testing (beta) when creating an experiment | Dashboard | [Sequential testing](../api/sequential-testing.md#turning-it-on) |
| Turn sequential testing (beta) on or off for an existing draft, and set its alpha | API only | [Sequential testing](../api/sequential-testing.md#turning-it-on) |
| Set another user's password; change their name, email, username and superuser flag | API only | [Authentication user guide](../auth/auth-user-guide.md) |
| Turn CUPED on, and choose its covariate and lookback | API only | [CUPED](../api/cuped.md) |
| Read the change history of one flag, experiment or person | API only | [Audit logging](../api/audit-logging.md) |
| Write notes and learnings on an experiment | API only | [Running experiments](user-guide.md) |
| See a feature flag's evaluations, latency and client error log | API only | [Safety monitoring](../feature-flags/safety.md) |
| Check one user id against the global holdout | API only | [Mutual exclusion groups and global holdout](../api/mutual-exclusion-groups.md#global-holdout) |
| See every user's notification settings | API only | [Slack & Email Alerting](../api/alerting.md) |
| Send a test notification through the scheduler's webhook | API only | [API endpoints](../api/endpoints.md) |
| Add tags to feature flags and experiments | API only | [Creating feature flags](../feature-flags/create.md) |
| Plan the sample size for a mean metric (revenue, count or duration) | API only | [Power analysis](../statistics/power-analysis.md) |
| LLM experiments | API only | [LLM evaluation](../llm-evaluation/overview.md) |
| AI design suggestions, results interpretation and templates | API only | [The MCP server and the AI endpoints](../mcp-server.md) |
| Compliance reports | API only | [Compliance audit trail](../api/compliance.md) |
| Preview a split-URL experiment (full profile) | API only | [Split URL testing](../api/split-url.md) |
| HIPAA administration (full profile) | API only | [HIPAA compliance](../hipaa/overview.md) |
| Integrations (full profile) | API only | [Third-party integrations](../api/integrations.md) |
| Custom roles and direct permission grants (full profile) | API only | [RBAC API reference](../api/rbac.md) |
| Delete a workspace (full profile) | API only | [Team workspaces](../workspaces/overview.md) |
| Erase one app user's data | API only | [Erase one app user's data](../api/erase-user-data.md) |
| Delete a rollout schedule | API only | [Gradual rollouts](../feature-flags/rollouts.md) |
| Configure single sign-on | API only | [SSO guide](../auth/sso.md) |
| Edit a page with the visual editor | API only | [Visual experiments](../sdk/visual.md) |
| Read audit statistics and the live stream of audit entries | API only | [Audit logging](../api/audit-logging.md) |

# Roadmap

This file is for anyone deciding whether to use Controlvar or to contribute to it. It says where
the project is heading, in three horizons: **Now**, **Next** and **Later**. There are no dates.
Each theme has a goal and links the public issues that carry its work; an item with no issue yet
has no link.

It is a plan, not a promise, and the order can change; what people running Controlvar report
weighs most. We aim to review it monthly, and each update comes with a short note on what changed
and why. An item is removed when it is done or stops earning its place, and the note says why.

## Now

**Goal: what the first users hit gets fixed, and the cheapest gaps close.** An item in Now can move
to Next if something more urgent comes up.

- **What you report comes first.** A bug reported by someone running Controlvar comes before
  everything else on this page.
- **A balanced split within each segment**, so test and control match within every group
  ([#448](https://github.com/Controlvar/controlvar/issues/448)).
- **Analysis settings fixed once an experiment starts**, so it is read the way it was planned
  ([#848](https://github.com/Controlvar/controlvar/issues/848)).
- **A segment preview from real user attributes**
  ([#830](https://github.com/Controlvar/controlvar/issues/830)), and **one accessible dialog
  across the dashboard** ([#369](https://github.com/Controlvar/controlvar/issues/369)).
- **Dashboard sign-in when Controlvar uses Amazon Cognito**
  ([#708](https://github.com/Controlvar/controlvar/issues/708)).
- **A tested upgrade for Docker Compose installs:** a written procedure, rehearsed in CI from the
  previous release's published images ([#1807](https://github.com/Controlvar/controlvar/issues/1807)).
- **Stale-flag detection:** find the flags nobody has changed or evaluated lately
  ([#1808](https://github.com/Controlvar/controlvar/issues/1808)).
- **Warehouse analysis on Redshift, then Databricks**
  ([#826](https://github.com/Controlvar/controlvar/issues/826),
  [#1605](https://github.com/Controlvar/controlvar/issues/1605)).
- **SDKs that do the same things:** every SDK reads a flag's value, not only on or off
  ([#1810](https://github.com/Controlvar/controlvar/issues/1810)), and sends user attributes on
  flag evaluation ([#438](https://github.com/Controlvar/controlvar/issues/438),
  [#1720](https://github.com/Controlvar/controlvar/issues/1720)). An SDK that installs from a clone
  today is published to its registry when someone asks for it, with event ids
  ([#1543](https://github.com/Controlvar/controlvar/issues/1543)) and the exposure hook.
- **Two-factor sign-in for local accounts**
  ([#1812](https://github.com/Controlvar/controlvar/issues/1812)).
- **A visual page editor:** change a live page's text and layout to make a variant, with no code
  ([#1545](https://github.com/Controlvar/controlvar/issues/1545)). It is built in steps, carries on
  into Next, and reaches users once it is complete.

## Next

**Goal: a team can move its flags and experiments here and run them the way it already does, with
the guardrails it expects.**

**Flag delivery.** Flag checks are answered next to your apps, quickly, and keep working when the
Controlvar server is down.

- **A relay** that runs beside your apps, holds the rules, answers flag checks for every SDK and
  streams changes. Browsers and phones never receive the rules.
- **Multi-way flags:** a weighted split among three or more values, and a different value per rule.

**Release workflow.** A flag change is made in one environment, approved, then promoted, in one
install.

- **Environments** (development, staging, production) in one install, and **approvals** for flag
  changes in each.
- **Flag prerequisites:** a flag that is on only when another flag is.

**Statistics.** Every analysis reads a revenue, count or duration metric by its values.

- **A shared metric library:** define a metric once, use it in experiments and rollouts.
- **Every view of a mean metric reads its values:** Bayesian, sequential, sample size, daily, Live
  and breakdowns; then CUPED and outlier control (winsorization) for means, and ratio metrics.

**Also in Next:**

- **Dashboard screens** for what only the API does today
  ([#442](https://github.com/Controlvar/controlvar/issues/442)).
- **Guardrail thresholds that act:** alert, then stop or roll back on a business metric, in
  experiments and in rollouts.
- **SAML sign-in**, and Microsoft Entra ID over OIDC
  ([#123](https://github.com/Controlvar/controlvar/issues/123)).
- **Identity aliasing:** a visitor who signs in keeps one assignment
  ([#449](https://github.com/Controlvar/controlvar/issues/449)).
- **Data retention by age**, and erasing one person's data.
- **Moving in and out:** import flags from other flag tools, and export and import between
  Controlvar instances.
- **Rollback on an outside alert:** an alert from your monitoring stops the matching rollouts.

## Later

**Goal: depth for larger teams, and for teams whose data lives in a warehouse.**

- Events from Segment or RudderStack counted as conversions.
- API keys that belong to a service, not a person
  ([#657](https://github.com/Controlvar/controlvar/issues/657)), and a Terraform provider.
- One owner tier: the admin role and superuser access tied together
  ([#758](https://github.com/Controlvar/controlvar/issues/758)); and SCIM provisioning.
- Warehouse analysis on Athena ([#908](https://github.com/Controlvar/controlvar/issues/908)) and
  ClickHouse, and intervals and the impact for mean metrics in a warehouse analysis.
- Workspaces that keep teams' experiments and flags apart, not only group people.
- Holdout results for mean metrics ([#835](https://github.com/Controlvar/controlvar/issues/835)).
- Quantile and funnel metrics, and metrics defined from dbt models.
- Local evaluation in more server SDKs, including segments and the advanced operators.
- Changes through the MCP server, under the same role checks, with a person approving risky ones
  ([#1607](https://github.com/Controlvar/controlvar/issues/1607)).
- More integrations, such as Slack and PagerDuty, when people ask for them.
- The one-command AWS installer out of beta
  ([#1299](https://github.com/Controlvar/controlvar/issues/1299)).
- Scheduled flag changes beyond rollout schedules.

## What we won't do

- **No paid or proprietary feature tier.** Controlvar is an open-source project. Shipped code stays
  open source: the core, the optional modules and the dashboard under Apache-2.0, the client SDKs
  under MIT. Everything on this page lands in that code, approvals, SAML and SCIM included.
- **No product analytics or session replay.** Controlvar sends exposures to the tools that do this,
  and its warehouse analysis (beta) reads your own tables.
- **Not planned unless users ask:** interleaving, switchbacks, contextual bandits, an AI-assisted
  visual editor, post-stratification, retention metrics, and effects by segment beyond breakdowns.
- **No dates on this page.** A date we cannot keep is worse than none.

## How to influence it

- **Open an issue** that describes the problem you are trying to solve, before the solution.
- **Add a 👍** to the issues that matter to you. Reactions inform priority; they are not a queue.
- To contribute code or documentation, see [CONTRIBUTING.md](CONTRIBUTING.md).

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
- **Dashboard sign-in when Controlvar uses Amazon Cognito**
  ([#708](https://github.com/Controlvar/controlvar/issues/708)).
- **Warehouse analysis on Databricks**
  ([#1605](https://github.com/Controlvar/controlvar/issues/1605)). Amazon Redshift is available
  ([#826](https://github.com/Controlvar/controlvar/issues/826)).
- **SDKs on their registries:** an SDK that installs from a clone today is published to its
  registry when someone asks for it, with the exposure hook.
- **A visual page editor:** change a live page's text and layout to make a variant, with no code
  ([#1545](https://github.com/Controlvar/controlvar/issues/1545)). It is built in steps, carries on
  into Next, and reaches users once it is complete. Its first part, storing a visual experiment's
  page changes, pages and goals, is in 0.47.0
  ([#1940](https://github.com/Controlvar/controlvar/pull/1940)).

## Next

**Goal: a team can move its flags and experiments here and run them the way it already does, with
the guardrails it expects.**

**Flag delivery.** Flag checks are answered next to your apps, quickly, and keep working when the
Controlvar server is down.

- **A relay** that runs beside your apps, holds the rules and answers flag checks for every SDK
  ([#2212](https://github.com/Controlvar/controlvar/issues/2212)). Browsers and phones never receive the rules.
- **Multi-way flags:** a weighted split among three or more values, and a different value per rule.

**Release workflow.** A flag change is made in one environment, approved, then promoted, in one
install.

- **Environments** (development, staging, production) in one install, and **approvals** for flag
  changes in each.
- **Flag prerequisites:** a flag that is on only when another flag is.

**Statistics.** Every analysis reads a revenue, count or duration metric by its values.

- **A shared metric library:** define a metric once, use it in experiments and rollouts
  ([#2213](https://github.com/Controlvar/controlvar/issues/2213)).
- **Every view of a mean metric reads its values:** Bayesian, sequential, sample size, daily, Live
  and breakdowns; then CUPED and outlier control (winsorization) for means
  ([#2214](https://github.com/Controlvar/controlvar/issues/2214)).

**Also in Next:**

- **Dashboard screens** for what only the API does today
  ([#442](https://github.com/Controlvar/controlvar/issues/442)), starting with the per-flag safety
  configuration page ([#2215](https://github.com/Controlvar/controlvar/issues/2215)).
- **Guardrail thresholds that act:** alert, then stop or roll back on a business metric, in
  experiments and in rollouts ([#2218](https://github.com/Controlvar/controlvar/issues/2218)).
- **SAML sign-in**, and Microsoft Entra ID over OIDC
  ([#123](https://github.com/Controlvar/controlvar/issues/123)).
- **Identity aliasing:** a visitor who signs in keeps one assignment
  ([#449](https://github.com/Controlvar/controlvar/issues/449)). Its first part,
  `POST /api/v1/tracking/alias` (beta), links a visitor's anonymous id to their account's id from
  your backend ([the guide](docs/guides/identity.md)). The JavaScript and Python SDKs gain the call
  in their next releases.
- **Data retention by age**, and erasing one person's data.
- **Moving in and out:** import flags from other flag tools, and export and import between
  Controlvar instances ([#2216](https://github.com/Controlvar/controlvar/issues/2216)).
- **Rollback on an outside alert:** an alert from your monitoring stops the matching rollouts
  ([#2217](https://github.com/Controlvar/controlvar/issues/2217)).

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
- Quantile, funnel and ratio metrics, and metrics defined from dbt models.
- Streaming flag changes to the relay.
- Local evaluation in more server SDKs, including segments and the advanced operators.
- Changes through the MCP server, under the same role checks, with a person approving risky ones
  ([#1607](https://github.com/Controlvar/controlvar/issues/1607)).
- More Slack events, and PagerDuty, when people ask for them.
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
  visual editor, post-stratification, retention metrics, and effects for a rules segment's members
  beyond breakdowns.
- **No dates on this page.** A date we cannot keep is worse than none.

## How to influence it

- **Open an issue** that describes the problem you are trying to solve, before the solution.
- **Add a 👍** to the issues that matter to you. Reactions inform priority; they are not a queue.
- To contribute code or documentation, see [CONTRIBUTING.md](CONTRIBUTING.md).

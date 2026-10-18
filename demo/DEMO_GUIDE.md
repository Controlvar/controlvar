# Controlvar — Client Demo Guide

## Overview

This guide walks you through a 20-minute live demo of the Controlvar experimentation platform. The demo environment comes pre-loaded with 100K+ historical events, and a live simulator adds new events in the background while you present.

---

## Setup

### Local (recommended)

From the repository root. The first run installs the Python and Node
dependencies and seeds the data, which takes a few minutes:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
./demo/setup-local.sh
```

When complete, you'll see the "Ready!" box with URLs and credentials.

### AWS

There is no one-command AWS demo. The script that claimed to be one could not
work (it set neither of the two values the stacks refuse to synthesise
without, and created no image, repository or secret), and it has been retired
(#73). Deploying to AWS is the self-hosting path:
[AWS CDK deployment](../docs/self-hosting/cdk.md), then the ordered first-deploy
checklist in the [deployment guide](../docs/deployment/deployment-guide.md#1-before-the-first-deploy).
The stacks create resources that AWS bills for while they exist (Aurora, NAT
gateways, a load balancer); `cdk.md` lists what a teardown leaves behind.

---

## Credentials

| Role      | Email              | Password   |
|-----------|--------------------|------------|
| Admin     | admin@demo.com     | Demo1234!  |
| Developer | dev@demo.com       | Demo1234!  |
| Analyst   | analyst@demo.com   | Demo1234!  |
| Viewer    | viewer@demo.com    | Demo1234!  |

---

## 20-Minute Demo Walkthrough

### Scene 0 — ShopLab storefront: experiments in a real app (4 min)

ShopLab is a small e-commerce site (`demo/shoplab`, http://localhost:3200) that runs four live experiments and two feature flags through the
React SDK and the public tracking API — the same path a customer's app would use. `setup-local.sh` seeds it, starts it,
and runs a traffic simulator against it so the dashboards fill up while you talk.

**What to show:**
- Open http://localhost:3200 — point at the **Controlvar panel** (bottom-right): the visitor id, the variant this visitor
  got for each experiment, and the two feature flags.
  - "This is a real storefront calling our API with an API key. Every visitor is bucketed server-side, sticky, in one call."
- Click **Shop the collection** (or **Watch & shop** if you got the video hero) → the panel logs `hero_cta_click`
  - "That click just became a conversion for the hero experiment. Nothing else to wire up."
- On **/products** change nothing — explain the sort order is a **multi-armed bandit** (`shoplab_plp_sort`): Thompson Sampling
  moves traffic toward the algorithm with the best click rate. Click a product.
- On the product page the **buy button** is one of four multivariate treatments (`shoplab_pdp_buy_button`). Add to cart.
- **/checkout** is a 3-step vs one-page A/B test (configured for CUPED; its new visitors have no history, so it adjusts almost nothing). Place the order → `purchase` with the order value.
- **/search** shows a gradual-rollout flag (`shoplab_new_search`, 10% → 50% → 100%) — press **New visitor** in the panel a
  few times to land in the 10% and see the "New search ✨" engine.
- Switch to the dashboard (http://localhost:3100/experiments): open **shoplab_hero_banner** — results are moving because
  the simulator is streaming visitors through the same API.

**Key talking points:**
- One SDK call per experiment, one call per event; no client-side bucketing to keep consistent
- Bandit, multivariate and sequential all running on the same storefront at once
- Gradual rollout + kill switch (`shoplab_free_shipping_banner`) without a deploy

Run pieces by hand: `python backend/scripts/seed_shoplab.py` (seeds experiments, flags, API key, 14 days of history),
`cd demo/shoplab && npm run dev`, `python demo/shoplab/simulator/traffic.py --rate 3`. See `demo/shoplab/README.md`.

---

### Scene 0b — StreamPulse: feature flags on mobile (5 min)

StreamPulse (`demo/streampulse`, http://localhost:3300) is a simulated music app: a phone frame with seven screens and a
device picker. Every flag and experiment is evaluated for the chosen device through the React SDK, with the device's
attributes (OS, OS version, app version, region, tier, employee) sent as targeting context. A simulator streams thousands
of devices through the same API, and a scripted "rollout story" walks the platform through a real launch.

**What to show:**
- Pick **iPhone 15 · iOS 17.4 · US · premium** → the Search tab shows **AI search ✨**. Pick **Pixel 7 · Android 14 · DE**
  → keyword search. The Controlvar panel shows `streampulse_ai_search: on (targeting_rule)` vs `off (rollout)`.
  - "That rule is `os = iOS AND os_version ≥ 17.0.0 AND region = US AND tier = premium`, built in the dashboard's targeting
    editor. Nothing is hard-coded in the app."
- Pick **Internal tester** → the Player tab shows the redesigned player (`streampulse_player_v2: on (targeting_rule)`,
  employee rule) while normal devices are in the 5% canary.
- Profile tab: `streampulse_wrapped` and `streampulse_profile_badges` are in a **mutual exclusion group** — the panel shows
  one assigned and the other "excluded: mutual exclusion". A few devices show "in global holdout" for every experiment.
- Kill switch: in the dashboard (http://localhost:3100/feature-flags) disable **streampulse_recs_v2**, then reload StreamPulse
  or press **Refresh now** in its panel: the Home tab falls back to the classic feed.
- The rollout story (drawer in the app, driven by `python demo/streampulse/simulator/rollout_story.py --auto`):
  1. player v2 at 5% + internal rule → 2. advance the schedule to 25% → 3. Android 12 devices start crashing (the simulator
  reports the crashes through the API with the StreamPulse server key, the way the app's server would forward them)
  → 4. the safety monitor sees the error rate cross 5% and rolls the flag back to 5%
  automatically → 5. a fix ships: rule `app_version ≥ 3.2.1` → 6. 50% → 7. 100%.
  - "Targeting, gradual rollout, safety monitoring, auto-rollback and lifecycle management, in one walkthrough."

**Key talking points:**
- Server-side targeting with 20+ operators, including semantic versions
- Rollout schedules and safety monitoring work together: expand automatically, retreat automatically
- Kill switches take effect without a release, which is the whole point on mobile

Run pieces by hand: `python backend/scripts/seed_streampulse.py`, `cd demo/streampulse && npm run dev`,
`python demo/streampulse/simulator/traffic.py --rate 3`, `python demo/streampulse/simulator/rollout_story.py --auto`.
For the story to complete on its own, run the backend with `SAFETY_CHECK_INTERVAL_MINUTES=1 ROLLOUT_CHECK_INTERVAL_MINUTES=1`.
See `demo/streampulse/README.md`.

---

### Scene 1 — Feature Flags (3 min)

**What to show:**
- Navigate to `/feature-flags`
- Click **new_dashboard_ui** — show it's at 50% rollout with a 3-stage schedule
  - "We started at 10%, expanded to 25%, and now we're at 50%. The schedule automates this — zero manual work."
  - An automatic rollback is shown live in Scene 0b's rollout story, on StreamPulse's Player v2.
- Click **beta_features** — show the targeting rule
  - "This uses our 20+ targeting operators. Users with role 'beta' or 'internal', OR an @acme.com email, match this rule."
    The flag's rollout is 100%, so users the rule does not match get it too.
- Toggle `beta_features` off and back on
  - "The API answers the very next evaluation with the change — no deploy needed."

**Key talking points:**
- Gradual rollouts with automated progression
- Advanced targeting (20+ operators: semver, geo, array, time windows)
- Safety monitoring with automatic rollback

---

### Scene 2 — Running A/B Test (4 min)

**What to show:**
- Navigate to `/experiments` → click **Checkout Button Color**
- Show: Status ACTIVE, 2 variants (blue 50% / green 50%), metric: checkout_completed
  - "This test has been running for 14 days. We've pre-seeded 30K historical events."
- Watch the results panel — numbers are ticking up
  - "These are updating right now. The live simulator is streaming ~3 events/second to both variants."
- Show the time-series chart — you can see the trend building
  - "Green converts at about 9% and blue at about 8%, and the difference is already significant."

**Key talking points:**
- Live data, not screenshots — the platform is actually running
- Consistent hash-based assignment (same user always gets same variant)

---

### Scene 3 — Statistical Results (5 min)

**What to show:**
- Navigate to `/experiments` → click **Homepage Hero Copy Test**
- Status: COMPLETED — this experiment ran for 35 days with 50K events
- Show the results panel:
  - p-value < 0.001 — highly significant
  - +15.0% relative lift in signup conversion (7.34% → 8.44%)
  - "The results page recommends the decision: Ship Variant ✓."
- Click the **Sequential** tab (it appears when the experiment has a sequential analysis)
  - "mSPRT lets us peek at results without inflating false positives. No more waiting for a fixed sample size."
- The Bayesian analysis: if the end of the **Overview** tab has a **Bayesian analysis (primary metric)** panel,
  show it there. A version without that panel has it in the API only (`GET /api/v1/results/{id}/bayesian`, see
  `docs/api/bayesian.md`); show it from http://localhost:8000/api/v1/docs if the audience asks.
- CUPED has no tab in the dashboard; it is in the API (`GET /api/v1/results/{id}/cuped`). On ShopLab's checkout it
  adjusts almost nothing, because the simulator's visitors are new and have no history; the Quick Start's
  `checkout_button_color` has history and shows the reduction (see `docs/api/cuped.md`).
- `docs/guides/dashboard-and-api.md` lists which capabilities have a screen and which are API only.

**Key talking points:**
- Multiple statistical methods in one platform: frequentist, sequential and Bayesian
- A recommendation on the results page: Ship Variant, Keep Control, Continue Testing or Inconclusive

---

### Scene 4 — Multi-Armed Bandit (3 min)

**What to show:**
- Navigate to `/experiments` → click **Recommendation Algorithm MAB**
- Show: 3 variants (algo_v1, v2, v3), Thompson Sampling, ACTIVE
- Scroll to **Current traffic weights** — algo_v2 has the largest share of new traffic
  - "This started at 33/33/34%. Thompson Sampling has noticed v2 performs better (9% click rate vs 5–6%) and is routing more traffic there automatically."
  - "No one made that decision. The algorithm did."
- Press **Refresh** to see the latest weights. The panel does not update by itself; `setup-local.sh` recomputes the weights every minute (`BANDIT_UPDATE_INTERVAL_MINUTES`, 5 by default)
  - "While we're talking, the platform is learning and reallocating."
- The dashboard shows the current weights only. It has no chart of how they changed over time

**Key talking points:**
- MAB for optimization problems where you can't wait for a winner
- Thompson Sampling, UCB1, Epsilon-Greedy all supported
- Automatic traffic reallocation — maximize conversions during the experiment

---

### Scene 5 — Modules / Admin (5 min)

**What to show:**

**Audit Trail:**
- Navigate to `/admin/audit`
- Show: each entry records who acted, what changed (old and new value) and when. No API edits or deletes an entry.
  These entries are not signed; do not describe them as signed or tamper-proof.
- Show the log: it records creating, changing and deleting flags and experiments, turning flags on and off,
  starting, pausing and completing experiments, rollout schedule changes, API keys, holdouts, mutual exclusion
  groups, segments, creating and deleting users, changes to a user's role, superuser flag or active status,
  signing in with a password, and safety rollbacks. Changes the platform makes on its own (scheduled starts and
  ends, rollout stages, the safety monitor's rollbacks, a first Cognito sign-in and Cognito role sync) are
  recorded too, by a named system actor; the page shows it as, for example, "Safety monitor (automatic)"
  - "You can filter these entries by action, resource type and date."
- Press **Download CSV** (or **Download JSON**): the file holds every entry the current filters match
- Show the `compliance` module's reports: `GET /api/v1/compliance/reports/soc2` (or `iso27001`) builds a report, and
  `GET /api/v1/compliance/export?format=csv` (or `json`) downloads the audit events, for an Admin

**RBAC:**
- Navigate to `/admin/users`
- Show 4 roles: Admin, Developer, Analyst, Viewer
  - "Analyst and Viewer can see experiments and flags but can't create or change them."
  - Every role, Viewer included, can still create API keys for itself, and an API key can send
    assignments and events through the tracking API.
- Four built-in roles are enforced today. Custom roles and resource-level grants exist in the data model but are
  not yet enforced by the permission checks, so do not demo them (tracked in the launch plan, P5).

**API / Integrations:**
- Open http://localhost:8000/api/v1/docs (Swagger UI)
  - "Full REST API. Your team can automate everything — CI/CD can create and start experiments, and with Slack or
    email set up, an alert goes out when the scheduler ends a test on its end date."
- Jira/Salesforce/GitHub integrations are configurable but their outbound delivery has only been exercised with
  mocks; treat them as roadmap in demos until the P5 path tests are green.

**Key talking points:**
- An audit log of changes to flags, experiments, users and roles, and with the
  `compliance` module HMAC-SHA256 signed compliance events and report exports
- RBAC with four built-in roles (custom roles: roadmap)
- Full REST API + SDKs: Python and JavaScript on PyPI and npm, Go with `go get`, Java and React built from `sdk/`
- Deployable to your own AWS account with the AWS CDK

---

## FAQ / Objections

**"Can it connect to our data warehouse?"**
> Yes, as a beta in the full profile: Snowflake and BigQuery are available, Amazon Athena is not yet. See `docs/api/warehouse-analytics.md`.

**"Is it SOC 2 compliant?"**
> The platform is not certified and we do not claim it. What it gives you are controls that support your own program: an audit log of changes to flags, experiments, API keys, users and roles, made by people or by the platform itself, in the core profile, where no API edits or deletes an entry, and with the `compliance` module HMAC-SHA256 signed audit events with exportable report packs. Your auditor decides what they satisfy.

**"What about our tech stack?"**
> The Python and JavaScript/TypeScript SDKs are on PyPI and npm, and the Go SDK installs with `go get`. The Java SDK (with Spring Boot auto-configuration) and the React SDK (with hooks and SSR support) build from the repository's `sdk/` directory. The REST API means you can integrate from anything.

**"How do we deploy? What's the infrastructure cost?"**
> Into your own AWS account with the AWS CDK — ECS Fargate, Aurora PostgreSQL, ElastiCache Redis — following docs/self-hosting/cdk.md, then dispatching the Deploy workflow for staging or prod. You own all the infrastructure. It is not one command, and the stacks create resources that AWS bills for (Aurora, NAT gateways, a load balancer); cdk.md says what a teardown leaves behind.

**"Can we run A/B tests across multiple products simultaneously?"**
> Yes. Mutual Exclusion Groups ensure users are never in conflicting experiments. The Global Holdout keeps a percentage of new users out of every experiment, as a baseline to compare with everyone else. Users already assigned keep their assignments, and feature flags ignore it, so shipped features reach the holdout too.

**"What if an experiment goes wrong?"**
> Safety monitoring checks error rates and latency every 5 minutes by default. If a feature flag causes problems, it auto-rolls back. For experiments, you can pause or stop at any time.

---

## Quick Reference URLs

| Page | URL |
|------|-----|
| ShopLab storefront | http://localhost:3200 |
| StreamPulse mobile demo | http://localhost:3300 |
| Home | http://localhost:3100 |
| Feature Flags | http://localhost:3100/feature-flags |
| Experiments | http://localhost:3100/experiments |
| Audit | http://localhost:3100/admin/audit |
| Users/RBAC | http://localhost:3100/admin/users |
| API Docs | http://localhost:8000/api/v1/docs |
| Health | http://localhost:8000/health |

An experiment's own page and its results are addressed by the experiment's id, which the seed
generates, so they have no fixed URL: open **Homepage Hero Copy Test** or **Recommendation
Algorithm MAB** from the Experiments list.

---

## Stopping the Demo

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
./demo/teardown-local.sh
```

This stops the processes `setup-local.sh` started, stops the Docker services with
`docker compose down` (or `docker-compose down` where only that is installed; the database
volume is kept), and removes `demo/.logs`, `demo/.pids`
and the demo API key file.

---

## Logs

`setup-local.sh` writes one log per process to `demo/.logs/`:

| Process | Log file |
|---------|----------|
| Backend API | `demo/.logs/backend.log` |
| Live event simulator (its stats) | `demo/.logs/simulator.log` |
| Dashboard | `demo/.logs/frontend.log` |
| ShopLab storefront | `demo/.logs/shoplab.log` |
| ShopLab traffic simulator | `demo/.logs/shoplab-simulator.log` |
| StreamPulse | `demo/.logs/streampulse.log` |
| StreamPulse device simulator | `demo/.logs/streampulse-simulator.log` |

`tail -f` follows one of them until you press Ctrl-C, so run it for the log you want, for
example the backend's:

```{.bash skip reason="demo: runs the demo applications (Stream F)"}
tail -f demo/.logs/backend.log
```

---
title: Limitations, what Controlvar does not do
description: "What Controlvar does not do and what happens instead, for feature flags, experiments and more, with links to the public issues that track each change."
---

# Limitations

What Controlvar does not do, and what happens instead. Where a public issue tracks a change,
the row links it.

## Feature flags

| What you might expect | What happens instead |
|---|---|
| A flag that reports which of its values does better | A flag's split (3 to 10 values by weight, on a string, number or json flag) and a targeting rule's own value change only what the users the flag is on for are served: no exposure, assignment or result is recorded, and `GET /api/v1/feature-flags/user/{user_id}` answers every flag as on or off. To compare values, run a multivariate experiment: each variant carries a `configuration`, which `POST /api/v1/tracking/assign` returns. |
| A split of two values, or weights that add up to less than 100 | A split has 3 to 10 values, and its weights are whole numbers that add up to exactly 100; anything else is refused with `422`. For two values, use the on and off values with a rollout percentage, or add a third value with weight 0. |
| A flag's value in every SDK | An SDK reads a flag's value only where its page documents a value method: JavaScript, Python, Go, OpenFeature (JavaScript and Python), React, React Native, Java and .NET. Android, iOS, Flutter, PHP, Ruby, Elixir and Edge read a value flag as on or off (`enabled` is `true` when the flag is on for the user). `GET /api/v1/feature-flags/evaluate/{key}` returns the value in `value`. |
| A flag targeted on a value inside a JSON attribute | Flag targeting refuses the `json_path` operator with `422`, because flag evaluation does not implement it. Experiment targeting accepts it. |
| A flag targeted on a percentage bucket of an attribute | Flag targeting refuses the `percentage_bucket` operator with `422`, because flag evaluation does not implement it. Experiment targeting accepts it. |
| A flag turned off for every user at once | Each client keeps the answer it has until its cache expires; a React or React Native component already on screen keeps its answer until it mounts again; and an iOS, Android, Flutter or React Native app that cannot reach the API keeps serving the last answer it stored. See [Turning a Flag Off](../feature-flags/kill-switch.md). |
| Error reports from every SDK reaching automatic safety rollback | Only the JavaScript SDK (`reportError`) and the Python SDK (`report_error`) send error reports to `POST /api/v1/tracking/errors`; no other SDK has its own call for it, and neither OpenFeature provider has one of its own (its `client` property is the JavaScript or Python client, which does). See [Which errors count](../feature-flags/safety.md#which-errors-count). |
| Every SDK retrying after a 429 | Only the JavaScript and Python SDKs retry, once, after the `Retry-After` the API sends (the JavaScript SDK waits up to its `timeoutMs`, the Python SDK up to 5 seconds). The two OpenFeature providers retry because they call those clients; no other SDK retries: a call that was answered `429` does not complete, and the Go SDK's error carries the header in `RetryAfter`. |

## Experiments and results

| What you might expect | What happens instead |
|---|---|
| Every event value counted in a revenue, duration or custom metric | A value that is not a number, is infinite, or is larger than 10^12 in size, in the unit it was sent in, adds nothing to its user's total; the event still counts its user in `conversions`. A count metric counts events and does not read their value. |
| An event sent twice counted once | Only an event with an `event_id` is recognised when it is sent again. Without one it is stored and counted twice: a count metric counts it again, and a revenue, duration or custom metric adds its value again. A request that timed out may have been stored, so sending it again without an `event_id` can double it. A conversion rate counts each user once and is not affected. See [How events count](../guides/how-events-count.md#sending-an-event-again). |
| A revenue, count, duration or custom metric's sequential analysis, Bayesian analysis, sample-size plan, daily series and Live tab by its values | Each is of the share of users who sent the metric's event, as for a conversion. CUPED does not analyse these metrics. |
| A breakdown by segment of a revenue, count, duration or custom metric by its values | The Breakdowns view of such a metric shows, in its column headed Mean, the share of users who sent the metric's event in each segment. The metric's headline row is its mean per user. |
| An experiment's results for the members of a rules segment | Not offered: `GET /api/v1/results/{experiment_id}/segments/{segment_id}` answers `422` for a rules segment, because the attributes its rules match are not stored for every assigned user. An ID-list segment's members can be read. |
| A saved segment applied to the other results views or the export | Only `GET /api/v1/results/{experiment_id}/segments/{segment_id}` filters by a saved segment. The other results routes, `GET /api/v1/experiments/{experiment_id}/results` and the export answer for all users, and ignore a `segment_id` parameter. |
| A segment's results counted by membership when each user was assigned, and corrected for the segments you look at | A segment's results count the list as it is when they are computed: users added since are counted, users removed are not. The correction covers the comparisons inside the segment, not how many segments you look at, so the answer is exploratory. |
| The average time one event takes, such as page load time | A duration metric is each user's total of its events' values, 0 for a user with none, averaged over every assigned user: a variant whose users send the event less often reads as less time. No metric averages over events rather than users. |
| Outlier control on a revenue, duration or custom metric (winsorization, trimming) | None is applied. A revenue, duration or custom metric is each user's total, averaged; one very large user moves the mean and widens the interval. `variance_reduction_config.method = winsorization` is stored and clips nothing. Only a value that is not a number or exceeds 10^12 in size is dropped, as the first row of this section says. |
| A ratio metric (revenue per order, clicks per session) | There is no ratio metric type. Metric types are conversion, revenue, count, duration and custom, each averaged over assigned users: see the row on the average time one event takes. Revenue per order can only be computed outside the platform. |
| A conversion window (beta) that gives every user the full window | With a window, users assigned within the window before the experiment stops never get a full window, and this cannot change after it ends. The number is not the N-day conversion rate: a change that makes people convert later can show the opposite sign, even when no window does not. Each variant's `open_window_users` in the results says how many of its users had less than the full window. See [Conversion window](../guides/how-events-count.md#conversion-window). |
| Changing a running experiment's conversion window | A metric's `conversion_window_hours` cannot be changed once the experiment leaves draft (`400`), as nothing else about its metrics can. A clone copies the windows and can be changed while it is a draft. |
| A metric's conversion window in a global holdout's results | A holdout's results are for one event name you give, counted from each user's first-seen time to the end of the holdout; no experiment's metric is read, so no metric's conversion window applies. See [Mutual Exclusion Groups](../api/mutual-exclusion-groups.md). |
| A sample-ratio mismatch that stops the experiment or removes its winner | Under a mismatch, `recommendation` is `INCONCLUSIVE` in `GET /api/v1/results/{experiment_id}`, in the data export and in the report. `winner_variant`, `has_winner`, the Bayesian decision and the sequential panel are unchanged. |
| A variant that is no better recommended to ship no more often than α says, whatever the split | On zero-inflated, right-skewed revenue (most users spend nothing, a few spend a lot), measured in 2,000 experiments at each split, at α = 0.05, with 5% of users buying, order values lognormal and repeat purchases. When both variants have the same distribution (an A/A test) it holds: with 600 users in the control and 5,400 in the treatment, the treatment was recommended to ship in 48 and the control kept in 52, about 50 expected in each direction (before engine 1.13.0, which compared means with Welch's t-test, 150 and 12). A variant that changes the shape of the values without changing the mean is not covered: with 5,400 users in the control and 600 in the treatment, 12.5% of the treatment's users buying and each of their orders 0.4 times the size, 71 of 2,000 such experiments recommended shipping it, where about 50 are expected. The interval on the difference is Welch's and is unchanged: in the same A/A experiments at 600/5,400 it excluded 0 in 162 of 2,000, where about 100 are expected. |
| Intervals on a treatment's difference adjusted for the experiment's correction | The intervals on a treatment's difference from the control, on its relative change and on its impact are not adjusted for comparing several treatments with the control, while `is_significant` is (Benjamini-Hochberg by default). With two or more treatments an interval can exclude 0 on a treatment that is not significant. For a metric analysed as a share of users, the interval and Fisher's exact test can also disagree near the threshold, either way. For a metric analysed as a mean, the interval is Welch's while `is_significant` follows the permutation test, so the two can disagree either way, most often at a lopsided split: in 2,000 A/A experiments with 600 users in the control and 5,400 in the treatment they disagreed in 142 (7.1%). |
| A mean metric tested by the permutation test everywhere | A warehouse analysis has only each variant's sums, so a mean metric there keeps Welch's t-test (`welch_t_test`), which on zero-inflated, right-skewed values at a lopsided split names a variant that is no better more often than α says: 150 of 2,000 A/A experiments with 600 users in the control and 5,400 in the treatment recommended shipping it. An LLM experiment compares its variants with its own Welch's t-test. |
| An interval on the relative change for every treatment | For a proportion, `relative_improvement_pct_ci` is null only when an arm has no users or the control has no conversions; at low counts it is wide: a treatment with 6 converting users out of 200 against a control with 2 out of 200 gives -34.2% to +1,206.3% at the 0.95 level. For a mean it is null when the control's mean plus or minus its margin reaches 0, where Fieller's set of ratios is unbounded. The interval on the difference is still reported. |
| A relative-change interval bound above +1,000,000% | The dashboard shows a bound beyond 1,000,000% in either direction as 'above +1,000,000%' or 'below −1,000,000%'; the API returns the exact number. |
| The impact projected to all traffic, to a later period, or in a currency | `incremental_impact` is the difference from the control times the treatment's own assigned users so far, as a plain number: nothing projects it further, and no currency or unit is read or added. |
| The intervals and the impact in the data export | The export has no interval or impact column; `GET /api/v1/results/{experiment_id}` reports them. |
| An interval on the difference, or an impact, in a warehouse analysis of a mean | A warehouse analysis gives a proportion metric's treatments their difference from the control with its interval, the relative change's interval and the impact, as `/results` does. A mean metric's treatments get none of them; their p-value and relative change are reported as before. |
| A traffic allocation for the experiment as a whole | There is none. Variant weights must add up to exactly 100%, or the request is refused with `422`. Users are left out only by targeting rules, a mutual exclusion group or the global holdout. |
| One user known by two ids, such as a visitor who signs in | There is no identify call. `POST /api/v1/tracking/alias` (beta) [links an anonymous id to a known id](../guides/identity.md); it needs a server-side key and is called from your backend. From the link on, both ids get one assignment per experiment, and an event either sends to an experiment counts for it. Until the ids are linked, a user who signs in under a new id mid-funnel gets a second assignment, and nothing recorded before the link is merged. Assign with the id you will track purchases with. |
| An LLM experiment that picks the variant with the lowest value | An LLM experiment's winner is the variant with the highest mean of the metric it is compared on: the business metric when every variant has one, otherwise the judge score. There is no lower-is-better setting for it. |
| An experiment balanced within a nested or dotted attribute (`user.tier`) | A balanced experiment reads its attribute from the top level of the assign `context` only: a user sent `{"user": {"tier": "gold"}}` or `{"user.tier": "gold"}` is balanced in `unknown`, and the setting refuses a key with a dot. See [Balance the split within groups](../guides/balance-within-groups.md). |
| A balanced experiment with more than 20 groups | At most 20 values are listed; every other value is balanced together as `other`, and users without the attribute as `unknown`. |
| A balanced experiment with more than 10 saved segments | At most 10 segments are listed, as in one set of targeting rules. |
| A user balanced in each of the saved segments they belong to | A user is balanced in the first listed segment they belong to, so the order of `segments` decides; a user in none of them is balanced in `other`. |
| Archiving a saved segment that an experiment balances within | Refused with `409` while a draft, active or paused experiment balances within it, as for a segment its targeting rules use. |
| A balanced 34/33/33 split | Every share of a balanced experiment must be a multiple of 5, such as 50/50, 20/80, 35/35/30 or 40/30/30; any other split is refused with `422`. |
| A balanced bandit or split URL experiment | Both are refused with `422`: a bandit moves traffic between variants as results come in, and the edge router sends no user attributes. |
| Changing what a running experiment balances within | `balance_within` cannot be changed once the experiment leaves draft (`400`). A clone copies it and can be changed while it is a draft. |
| A user who changes tier moving to their new group | A user keeps the variant and the group of their first assignment whatever a later call sends. |
| Every user of a balanced experiment balanced within their group | A user assigned while the server is busy balancing other users is assigned by the hash instead, keeps their group and is counted in `experiment_balance_fallback_total`. An older server version, during a rolling deploy or a rollback, assigns every new user by the hash with no group. Either way the user's chance of each variant is still its share, but they are outside the balance. |

A metric with a conversion window reads each converting user's assignment time as well as their events, so its results take longer to compute. On AWS the load balancer ends a request that has had no answer for 60 seconds, so `GET /api/v1/results/{experiment_id}` reaches that limit with fewer events when a metric has a window. On 2026-11-26, on one machine, the results of an experiment with 1.5 million of a metric's events took 8 seconds to compute without a window and 14 seconds with one; at that rate the limit comes at about 11 million events without a window and about 6 million with one. While the dashboard's Live tab is open, its results are computed again every 30 seconds, the default of `RESULTS_STREAM_INTERVAL_SECONDS`.

A revenue, count, duration or custom metric whose variants have the same distribution: the p-value comes from a permutation test that holds α at every split measured; 2,000 A/A experiments at 600/5,400 and at 3,000/3,000 recommended shipping the treatment in 48 and 46 and keeping the control in 52 and 45, about 50 expected in each direction.

## Deployments

| What you might expect | What happens instead |
|---|---|
| Experiments in staging or development | Experiments and event tracking run in production only. An API key belongs to `production`, `staging` or `development`, chosen when it is created: a `staging` or `development` key is answered that environment's feature flags, and the experiment and event-tracking routes answer it `403`. To run an experiment before production, run a separate deployment, with its own API keys. |
| Events streamed to Kinesis, OpenSearch or DynamoDB | The API stores events in PostgreSQL and computes results from there. The full-profile CDK creates a Kinesis stream and an OpenSearch domain, and the API writes to neither. |

## Sign-in and access

| What you might expect | What happens instead |
|---|---|
| Requiring two-step sign-in for a role on an AWS CDK install | Give `cdk deploy` the context `-c local_auth_2fa_required_roles=ADMIN,DEVELOPER`: the API task then has `LOCAL_AUTH_2FA_REQUIRED_ROLES`. It applies to local accounts only, so the CDK app refuses it together with `-c cognito_hosted_ui=true`, where sign-in goes through Cognito. Without the context, each local account turns two-step sign-in on or off for itself. Docker Compose and the Helm chart can set it too. See [Requiring it for a role](../api/auth.md#requiring-it-for-a-role). |
| SAML sign-in | Not available. OIDC sign-in is, with the `sso` module (full profile). |
| A custom role that removes access, or that grants administration | Custom roles and direct permission grants (the `rbac` module) only add, and only to experiments, flags and reading the audit log; never administration or the compliance audit trail. Administration depends on the built-in role or the superuser flag; to restrict someone, give them a lower built-in role. See [What a role or grant cannot give](../api/rbac.md#what-a-role-or-grant-cannot-give). |
| Assigning roles and granting permissions in the dashboard | The Roles page defines roles; assigning them and granting permissions go through the [RBAC API](../api/rbac.md). |

## Data

| What you might expect | What happens instead |
|---|---|
| A retention policy | None exists. Nothing deletes events, assignments or audit records by age. One setting purges the platform's own analysis history: `ANALYSIS_HISTORY_RETENTION_DAYS` (default 90) deletes analysis snapshots and bandit-state history older than that, each time the metrics scheduler runs. |
| Erasing one person's data from the dashboard | A superuser erases one app user's data with the API (beta): [Erase one app user's data](../api/erase-user-data.md). The dashboard has no control for it, and copies already exported to a warehouse or a backup are not touched. |

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
| Every SDK on a package registry | The JavaScript, React and React Native SDKs and the OpenFeature provider for Node are on npm; the Python SDK and the OpenFeature provider for Python are on PyPI; the Java SDK and its Spring Boot starter are on Maven Central; the .NET SDK is on NuGet. The Go SDK installs by module path, and every other SDK installs from a clone of this repository. |
| An `event_id` sent by the SDK, so that an event sent again is stored once | The Java SDK sends one with every event ([Event ids](../sdk/java.md#event-ids)). The JavaScript SDK sends the `eventId` you give it and the Python SDK the `event_id` you give it ([JavaScript](../sdk/javascript.md#event-ids), [Python](../sdk/python.md#event-ids)). The React and React Native SDKs send an `event_id` with every event, from version 0.2.0 ([React](../sdk/react.md#event-ids), [React Native](../sdk/react-native.md#event-ids)). The .NET SDK sends an `event_id` with every event, from version 0.2.0 ([.NET](../sdk/dotnet.md#event-ids)). The Android SDK, which you build from a clone of this repository, sends an `event_id` with every event ([Android](../sdk/android.md#event-ids)). The Elixir SDK, which you build from this repository, sends an `event_id` with every event ([Elixir](../sdk/elixir.md#event-ids)). No other SDK has an `event_id` option: not the edge, Go, iOS, Flutter, PHP or Ruby SDK, nor the OpenFeature providers. Send an event that may be sent again to `POST /api/v1/tracking/track` or `POST /api/v1/tracking/batch` with its `event_id` ([#1543](https://github.com/Controlvar/controlvar/issues/1543)). |
| An event tracked without an experiment key counted, however late it comes | In the Go, Java, .NET, Ruby, PHP, Elixir, iOS, Android, Flutter, React Native and edge SDKs, an event tracked without a key reaches only the experiments and flags still in that client's cache (5 minutes by default, 60 seconds in the edge SDK), and is not sent at all once they have expired. Pass the experiment key on a conversion that can come later. |

## Visual experiments (not yet available)

The visual editor is not yet available. These lines are about the browser script and the sign-in it will use.

| What you might expect | What happens instead |
|---|---|
| A text change on a page React renders on the server, with no error | On a site whose pages React renders on the server (Next.js and others), a text change makes React report at least one hydration error per page view (several on React 18), which your error monitoring will record. The change still shows. |
| A change to a text the page keeps updating | Do not change a text your site keeps updating while the page is open (a cart count, a timer, a price that loads late): once changed, either it stops showing your site's updates or your site's update replaces the change. |
| A visual experiment on a site that routes after `#` | On a site that routes with the part of the address after `#` (hash routing), the visual editor always opens the site's default route, and page rules and page-view goals treat every route as one page. |
| An editor sign-in that can be extended | The visual editor's sign-in on your page lasts 4 hours and cannot be extended there. Every saved change is kept, and you go on by opening the editor again from the experiment's page. This does not meet WCAG 2.1 success criterion 2.2.1 (Timing Adjustable). |
| An iPad counted as a tablet | An iPad that reports a Mac, as iPadOS Safari does by default, is counted as a tablet by its touch support. This was checked by emulating one in Chromium, not on real iPads. |
| A returning visitor counted as returning | Safari may delete what a script stores after 7 days without a visit to your site, so a returning visitor can count as new. |
| A campaign from any page of a visit | The campaign (`utm_`) values come from the first page of a visit only; a campaign in a later page's address is not used until a new visit starts. |
| Every campaign value recorded | A campaign value with a space, an @, a + or another character, or over 100 characters, is not recorded. |
| Every landing page recorded | A landing path over 255 characters, or with an @, is not recorded. |
| A phones-only test shown only on phones | A visitor your site signs in (`data-user-id`) keeps their variant on every device, so one who joins a phones-only test on a phone also gets its changes on a desktop. |
| A goal on an element inside a frame or a shadow root | A goal's selector finds the elements of the page itself only: an element inside a frame (`<iframe>`) or a shadow root is not found, so clicks on it count nothing, and the goal is reported as matching no element. |
| A click that takes the visitor to another site, always counted | The click's event is sent as the page goes away, with `keepalive`; if the browser does not deliver that request, the click counts only if the visitor returns within 24 hours, when the next page on the same origin sends it again. |
| A goal on a page of another domain or origin | A goal's page must be on one of the experiment's hosts, and the script keeps goals in the localStorage of the origin where the visitor was assigned, which the browser keeps apart for each scheme, host and port, so a page on another domain, subdomain, port or scheme records none of the goals kept there. |
| A click goal after a redesign | When a page no longer has exactly one element that matches a click goal's selector, no click counts for that goal, in any arm, and the script sends a report (`goal_not_found` or `goal_ambiguous`) once per page view. |
| Goals in a browser that refuses storage | The script keeps goals and their events in your site's localStorage; where the browser refuses it, no goal counts. |

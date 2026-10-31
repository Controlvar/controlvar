# Multi-Armed Bandit

Multi-armed bandit (MAB) experiments dynamically reallocate traffic toward better-performing variants as data accumulates, rather than maintaining fixed allocations throughout the experiment.

---

## Algorithms

Each algorithm turns every variant's pulls and successes into a weight; the weights add up
to 1 and decide where new users go (see [How the weights affect traffic](#how-the-weights-affect-traffic)).
An experiment is a bandit only when it names one of the three; `optimization_type` defaults
to `fixed`.

### Thompson Sampling

Each variant has a Beta posterior, Beta(1 + successes, 1 + failures). A weight update draws
10,000 samples from every posterior, and each variant's weight is the share of draws in which
it is highest.

### UCB1 (Upper Confidence Bound)

Each variant's score is `mean + sqrt(2 * ln(total_pulls) / variant_pulls)`, and its weight is
its score divided by the sum of the scores, so every variant keeps some traffic. While any
variant has no pulls, the variants with none share all the weight equally.

### Epsilon-Greedy

The variant with the highest conversion rate gets the weight `0.9 + 0.1 / n`, and each of
the other variants `0.1 / n`, for `n` variants: epsilon is 0.1, and it is not a setting.

---

## Creating a MAB Experiment

Run the commands on this page in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by the
ones before it. Log in first; creating an experiment takes the DEVELOPER or ADMIN role:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

Set `optimization_type` to one of `thompson_sampling`, `ucb1` or `epsilon_greedy` when you
create the experiment. The bandit counts a conversion as an event matching the experiment's primary
metric. This saves the experiment's id in `$EXP_ID`:

```{.bash exec}
EXP=$(curl -s -X POST localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "CTA Button Bandit",
    "optimization_type": "thompson_sampling",
    "variants": [
      {"name": "control", "is_control": true, "traffic_allocation": 34},
      {"name": "red_button", "traffic_allocation": 33},
      {"name": "green_button", "traffic_allocation": 33}
    ],
    "metrics": [
      {"name": "CTA click", "event_name": "cta_click", "is_primary": true}
    ]
  }')
EXP_ID=$(jq -r .id <<<"$EXP")

jq '{name, status}' <<<"$EXP"
```
<!-- expect: "name": "CTA Button Bandit" -->
<!-- expect: "status": "draft" -->

The experiment is created in `draft`. Variants' `traffic_allocation`s are whole percentages
that add up to 100; they decide the split until the bandit's first weight update.

---

## API Reference

### GET /api/v1/bandit/{experiment_id}

Returns the current bandit state: the algorithm, and each variant's weight, pulls,
successes and conversion rate. Any logged-in user can read it. This also saves each
variant's id, for the weight override further down:

```{.bash exec}
STATUS=$(curl -s localhost:8000/api/v1/bandit/$EXP_ID \
  -H "Authorization: Bearer $TOKEN")
CONTROL_ID=$(jq -r '.current_weights[] | select(.variant_name == "control") | .variant_id' <<<"$STATUS")
RED_ID=$(jq -r '.current_weights[] | select(.variant_name == "red_button") | .variant_id' <<<"$STATUS")
GREEN_ID=$(jq -r '.current_weights[] | select(.variant_name == "green_button") | .variant_id' <<<"$STATUS")

jq '{algorithm, total_pulls, recommendation}' <<<"$STATUS"
```
<!-- expect: "algorithm": "thompson_sampling" -->
<!-- expect: "total_pulls": 0 -->
<!-- expect: "recommendation": "EXPLORING" -->

```json
{
  "algorithm": "thompson_sampling",
  "total_pulls": 0,
  "recommendation": "EXPLORING"
}
```

Before any update has run, every variant has an equal weight. With traffic, a response
looks like this:

```json
{
  "experiment_id": "0bdda20f-2cd5-4e99-8ba0-0ce5fc378761",
  "algorithm": "thompson_sampling",
  "current_weights": [
    {
      "variant_id": "104a0022-083b-4e22-a793-096788bbe49b",
      "variant_name": "control",
      "current_weight": 0.18,
      "successes": 820,
      "pulls": 2246,
      "conversion_rate": 0.365
    },
    {
      "variant_id": "1f1f863f-38ae-49c9-b995-60290d58def3",
      "variant_name": "red_button",
      "current_weight": 0.61,
      "successes": 3890,
      "pulls": 6382,
      "conversion_rate": 0.61
    },
    {
      "variant_id": "6f07b272-cbcc-47f9-8336-c2b58992949a",
      "variant_name": "green_button",
      "current_weight": 0.21,
      "successes": 1240,
      "pulls": 3852,
      "conversion_rate": 0.322
    }
  ],
  "total_pulls": 12480,
  "regret_reduction_pct": 2.1,
  "recommendation": "CONVERGING",
  "last_updated": "2026-09-26T14:32:00+00:00",
  "seed": 6346510783624545786,
  "n_samples": 10000,
  "engine_version": "1.2.0"
}
```

**Response Fields**

| Field | Description |
|-------|-------------|
| `algorithm` | The experiment's `optimization_type` |
| `current_weight` | Current traffic allocation fraction (0–1, sums to 1.0 across variants) |
| `pulls` | Total assignments to this variant |
| `successes` | Converting users in this variant: users assigned to it with at least one conversion event, each counted once. When the primary metric has `lower_is_better` set, the users who did not convert instead (see below) |
| `regret_reduction_pct` | Estimated regret reduction compared with a uniform split; `null` until there is data, and while the weights do no better than a uniform split |
| `recommendation` | `EXPLORING`, `CONVERGING`, or `DEPLOYING_<variant name>` once one variant dominates |
| `last_updated` | When the weights were last computed; `null` before the first update |
| `seed`, `n_samples` | The random seed and draws per variant of the last Thompson-sampling update; `null` for the other algorithms |

An experiment whose `optimization_type` is `fixed` answers `404` here.

---

### POST /api/v1/bandit/{experiment_id}/update

Recalculates the weights now, outside the scheduler cycle. Requires the DEVELOPER or ADMIN
role:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/bandit/$EXP_ID/update \
  -H "Authorization: Bearer $TOKEN" | jq '{algorithm, n_samples}'
```
<!-- expect: "n_samples": 10000 -->

It returns the new state, which now has a `last_updated` time. Thompson sampling draws
`n_samples` (10000) samples per variant, so with no data yet the weights move a little away
from equal at random.

---

### PUT /api/v1/bandit/{experiment_id}/weights

Overrides the variant weights. Requires the ADMIN role. The body maps each variant's id to
its weight; the weights must be at least 0 and add up to 1.0, or the API answers `422`.
Use with caution: the scheduler replaces an override on its next cycle while the experiment
is active.

```{.bash exec}
curl -s -X PUT localhost:8000/api/v1/bandit/$EXP_ID/weights \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "{
    \"weights\": {
      \"$CONTROL_ID\": 0.1,
      \"$RED_ID\": 0.8,
      \"$GREEN_ID\": 0.1
    }
  }" | jq -c '[.current_weights[] | {variant_name, current_weight}]'
```
<!-- expect: {"variant_name":"red_button","current_weight":0.8} -->

It prints the new weights:

```json
[{"variant_name":"control","current_weight":0.1},{"variant_name":"red_button","current_weight":0.8},{"variant_name":"green_button","current_weight":0.1}]
```

---

## Scheduler

The bandit scheduler (`BanditSchedulerRunner` in `backend/app/core/bandit_scheduler.py`) starts with the API
process and recalculates weights for every ACTIVE experiment whose `optimization_type` is not `fixed`.
The cadence is `BANDIT_UPDATE_INTERVAL_MINUTES` (default 5). It does not run under `APP_ENV=test`.

### Where the counts come from

For each variant the scheduler needs pulls (assignments) and successes (conversions). Sources are tried in order:

1. **DynamoDB real-time counters** (`get_experiment_counters`), when the counters stack is deployed and
   every variant has at least as many pulls and successes there as in PostgreSQL. A partial DynamoDB count (a counter
   that started late, or a manual increment) is ignored in favour of PostgreSQL's complete one.
2. **PostgreSQL**: pulls = `assignments` rows per variant; successes = converting users, counted exactly as
   `/results` counts them: users assigned to the variant with at least one event, tagged with that variant, whose
   `event_name` matches the experiment's primary metric (see the event-matching rule in the tracking API docs). A
   user who converts three times counts once. With no metric configured, every event other than an experiment view is a success.
3. The previously persisted `BanditState`.
4. Zero-count priors (equal weights).

When the experiment's primary metric has `lower_is_better` set (uninstalls, errors), the bandit counts a user who did
not convert as the success, so the variant with fewer converting users earns the traffic. Every algorithm and both
live sources (DynamoDB and PostgreSQL) apply this; the persisted `BanditState` already holds the result.
### How the weights affect traffic

`POST /api/v1/tracking/assign` reads the latest `BanditState` for bandit experiments and routes **new** users
according to the current weights (a deterministic hash of the user id is looked up in the cumulative weight
distribution, so repeated calls agree before the assignment row exists). Users who already have an assignment
keep it. Variants whose weight is `0` receive no new traffic. Until the first refresh has run, new users are
split by the variants' `traffic_allocation`.

---

## Permissions

| Action | Minimum Role |
|--------|-------------|
| View bandit status | VIEWER |
| Trigger weight update | DEVELOPER |
| Override weights | ADMIN |

# How events count

Which of the events you send count in an experiment's results. The rules are the same for every
SDK, because each one sends its events through `POST /api/v1/tracking/track` and
`POST /api/v1/tracking/batch`.

| The event | Stored | Counts in its variant's results |
|---|---|---|
| Sent again (with or without anything identifying it) | Yes, each time | Each time in count and revenue metrics; a conversion metric counts the user once ([#1543](https://github.com/Controlvar/controlvar/issues/1543)) |
| Received before the user is assigned to the experiment | Yes, with no variant | No |
| Received while the experiment is a draft, paused, completed or archived | Yes, with no variant | No |
| Received after the user is assigned, with an earlier `timestamp` | Yes | Yes |
| With a `value` larger than 10^12 in size | Yes | Its user counts as converting; the value adds nothing |
| Sent by a bot or a crawler | Yes | Yes |

## Sending an event again

A request that timed out may already have been stored. An event you send again is stored again,
and then a count metric counts it twice and a revenue metric adds its value twice.

### When sending fails

- The JavaScript and Python SDKs retry a `429` once, after `Retry-After`; the server stores
  nothing for a `429`.

## When an event counts

An event counts for the variant the user is assigned to when the server receives it. Its
`timestamp` does not change that.

- An event received before the user's assignment is stored with no variant, and counts in no
  variant's results, even after the user is assigned. Track a user's events for an experiment
  once its assignment has answered.
- An event received while the experiment is a draft, paused, completed or archived is stored
  with no variant, and counts in no variant's results, even after the experiment is started
  again.
- `POST /api/v1/tracking/events` stores the `variant_id` it is sent, whatever the experiment's
  status; the event counts only for a user assigned to that variant.
- The results summary's Total Events counts every stored event of the experiment, the ones that
  count in no variant included.
- Holdout results and CUPED history have their own rules: see
  [Mutual Exclusion Groups](../api/mutual-exclusion-groups.md) and [CUPED](../api/cuped.md).

## Timestamps

- `timestamp` is when the event happened, by the sender's clock. Without one it is the time the
  server received the event. The Ruby, PHP and Elixir SDKs send the host's clock when you give
  none.
- Any `timestamp` is stored, however far in the past or the future.
- `GET /api/v1/results/{experiment_id}` does not read it.
- `GET /api/v1/results/{experiment_id}/daily` puts each user's first conversion on the UTC day
  of its `timestamp`. A day before the experiment's first day counts on that first day, and a
  day after its last day on that last day.

## Bots

Nothing detects bots or crawlers, and nothing limits the events one user sends beyond the
per-address rate limit. Keep bots out where you assign them: do not request an assignment for a
bot.

# How events count

Which of the events you send count in an experiment's results. The rules are the same for every
SDK, because each one sends its events through `POST /api/v1/tracking/track` and
`POST /api/v1/tracking/batch`.

| The event | Stored | Counts in its variant's results |
|---|---|---|
| Sent again with the same `event_id` | No: the first one is kept | Once |
| Sent again without an `event_id` | Yes, each time | Each time: a count metric counts it again, and a revenue, duration or custom metric adds its value again; a conversion metric counts the user once |
| Received before the user is assigned to the experiment | Yes, with no variant | No |
| Received while the experiment is a draft, paused, completed or archived | Yes, with no variant | No |
| Received after the user is assigned, with an earlier `timestamp` | Yes | Yes |
| With a `value` that is not a finite number from -10^12 to 10^12 | No: `/track` and `/events` answer `422`; `/batch` reports the entry in `errors` and stores the others, but refuses the whole request when the number in the JSON is NaN or infinite | No |
| Stored before the server refused such a value, with a `value` larger than 10^12 in size | Yes | Its user counts as converting; the value adds nothing |
| Sent by a bot or a crawler | Yes | Yes |

## Sending an event again

A request that timed out may already have been stored. An event you send again without an
`event_id` is stored again, and then a count metric counts it twice and a revenue, duration or
custom metric adds its value twice. To have it counted once, give it an `event_id`.

- `event_id` is your id for one event: text of up to 255 characters, compared exactly. Leaving it
  out, `null` and `""` all mean no `event_id`. A number, longer text, or text holding NUL is
  refused with `422`, and in `/batch` one such event refuses the whole request.
- The server does not store an event when it already has one with the same `event_id`, user,
  experiment, flag and event name. `POST /api/v1/tracking/track` and `POST /api/v1/tracking/events`
  answer it with `200`, `"duplicate": true` and the stored event's `id`;
  `POST /api/v1/tracking/batch` counts it in `success_count` and in `duplicate_count`. The first
  one sent is kept: the value, metadata and timestamp of a later one are not compared and not
  stored.
- The same `event_id` for another user, experiment, flag or event name is another event. So an
  event sent to each of a user's experiments and flags counts once in each of them, and an order
  id given to both `checkout_completed` and `purchase` is two events.
- The experiment and the flag are the ones the event was given when it was received. An event
  first received while its experiment was paused is not stored again when it is sent again after
  the experiment is started, and stays out of every variant's results.
- Events stored before you send `event_id` keep their repeats. Send it before you start an
  experiment, so that the whole experiment is counted the same way.
- To check that your server counts a repeat once, send one event twice with the same `event_id`:
  the second answer has `"duplicate": true`.

Give each event an `event_id` that your own system decides and does not publish: the id the SDK
makes, a random UUID, or, for events you send from your server with a server-side key, your order
or message id. From a browser or an app, let the SDK make the id. Send revenue events from your
server. Make the id once, when the event happens, and send that same id each time.

The Java SDK gives every event an `event_id` ([Event ids](../sdk/java.md#event-ids)). The
JavaScript SDK sends the `eventId` you give it, and makes none itself
([Event ids](../sdk/javascript.md#event-ids)). The Python SDK sends the `event_id` you give it, and
makes none itself ([Event ids](../sdk/python.md#event-ids)).
No other
SDK has an `event_id` option yet ([Limitations](../getting-started/limitations.md#sdks)): send an
event that may be sent again to `POST /api/v1/tracking/track` or `POST /api/v1/tracking/batch`
yourself.

`event_dedup_keys` holds your app's user ids and keeps them when events are deleted. An operator
erasing one person's events by hand must delete their keys too. An `event_id` is remembered for as
long as its key is kept.

### When sending fails

- The JavaScript and Python SDKs retry a `429` once, after `Retry-After`; the server stores
  nothing for a `429`.

## When an event counts

An event sent to `POST /api/v1/tracking/track` or `POST /api/v1/tracking/batch` counts for the
variant the user is assigned to when the server receives it. Its `timestamp` does not change
that.

- An event received by `/track` or `/batch` before the user's assignment is stored with no
  variant, and counts in no variant's results, even after the user is assigned. Track a user's
  events for an experiment once its assignment has answered.
- An event received while the experiment is a draft, paused, completed or archived is stored
  with no variant, and counts in no variant's results, even after the experiment is started
  again.
- `POST /api/v1/tracking/events` stores the `variant_id` it is sent, whatever the experiment's
  status; the event counts for that variant once the user is assigned to it, whether the
  assignment came before or after the event, and in no variant while the user is assigned to
  another or to none.
- The results summary's Total Events counts every stored event of the experiment, the ones that
  count in no variant included.
- Holdout results and CUPED history have their own rules: see
  [Mutual Exclusion Groups](../api/mutual-exclusion-groups.md) and [CUPED](../api/cuped.md).

## Timestamps

- `timestamp` is when the event happened, by the sender's clock. Without one it is the time the
  server received the event. The Ruby, PHP and Elixir SDKs send the host's clock when you give
  none.
- Any `timestamp` is stored, however far in the past or the future, with one exception: a value
  at the very edge of the date range whose UTC offset moves it past 0001-01-01 or 9999-12-31
  (`9999-12-31T23:59:59-01:00`, say) is refused with `422`, or reported in a batch's `errors`,
  and nothing is stored.
- `GET /api/v1/results/{experiment_id}` does not read it.
- `GET /api/v1/results/{experiment_id}/daily` puts each user's first conversion on the UTC day
  of its `timestamp`. A day before the experiment's first day counts on that first day, and a
  day after its last day on that last day.

## Bots

Nothing detects bots or crawlers, and nothing limits the events one user sends beyond the
per-address rate limit. Keep bots out where you assign them: do not request an assignment for a
bot.

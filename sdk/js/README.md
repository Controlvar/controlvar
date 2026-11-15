# @controlvar/js-sdk

**Beta.** This is a 0.x release: pin the exact version you test against.

JavaScript/TypeScript client for the Controlvar public API. Node >= 18 and browsers,
zero runtime dependencies (global `fetch`), CommonJS build with type declarations.

Flag evaluation and experiment assignment are **decided by the server** by default; results are
cached per user + key in memory. Server-side code can opt in to **local evaluation**
(`evaluation: 'local'`, beta): flags are answered in-process from the server's ruleset, with the
server's answers, and anything that cannot be answered exactly goes to the server. See
[`docs/sdk/local-evaluation.md`](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/local-evaluation.md).

Full documentation: [`docs/sdk/javascript.md`](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/javascript.md).

## Install

If you installed `@getexperimently/js-sdk`, the name this package had before the project was renamed, uninstall it and install `@controlvar/js-sdk`.

The `@controlvar/js-sdk` package is on [npm](https://www.npmjs.com/package/@controlvar/js-sdk). 0.3.0 is a
beta release.

```bash
npm install @controlvar/js-sdk@0.3.0
```

To work on the SDK itself, see Development below.

## Changes in this version

- `getStringValue`, `getNumberValue` and `getJsonValue` are new: they read the value a string,
  number or json flag serves. Each returns your default when it cannot, and passes the reason to
  `onError(err, 'value')`. See
  [Flag values](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/javascript.md#flag-values).
- `FlagEvaluation` gains `value` and `valueType`.
- `onExposure` is new: a function you give the client when you create it, which it calls once each
  time the server enrols a user in an experiment and the answer did not come from its cache. See
  Exposure hook below.
- `SwallowedOperation`, the type of `onError`'s second argument, has two new members: `'value'`, for
  a typed getter that returned your default, and `'exposure'`, for a failure in `onExposure`.
  `ExperimentationErrorCode`, the type of an `ExperimentationError`'s `code`, has two new members:
  `'TYPE_MISMATCH'`, for a flag that serves another type than the getter reads, and `'HOOK_ERROR'`,
  for a failure in `onExposure`. TypeScript code that checks either type exhaustively, such as a
  `switch` whose `default` assigns the value to `never`, stops compiling until it handles all four
  new members: `'value'`, `'exposure'`, `'TYPE_MISMATCH'` and `'HOOK_ERROR'`.
- `eventId` is new on `track()` and `trackBatch()`. See Event ids below.
- Local evaluation answers a string, number or json flag's value too. A flag entry this SDK cannot
  read (an `evaluation` or `value_type` it does not know, or a value of the wrong type) sends only
  that flag to the server, and the rest of the ruleset is still answered locally; before this
  version an `evaluation` it did not know refused the whole ruleset, and the client kept answering
  from its last one.
- Once local evaluation stops answering a flag (`maxStaleMs` passed, the ruleset discarded,
  `close()`), a call for a user it answered locally goes to the server; before this version that
  user kept the local answer from the cache for up to `cacheTtlMs`.
- A `cacheTtlMs` of `0` caches nothing. Before this version an answer was reused within the same
  millisecond, and every cached answer lived one millisecond past `cacheTtlMs`.
- With a finite `cacheTtlMs` above 24 hours, a `track()` without a key still reaches an experiment
  or flag whose answer is still cached; before this version it reached only those answered in the
  last 24 hours.
- Request error messages name the method and the path without its query string, which carries the
  user id and the user's attributes; before this version some named the full URL.

## Event ids

`track()` and `trackBatch()` take an `eventId`, sent as `event_id`. The server stores an event sent
again with the same `event_id`, user, experiment, flag and event name only once. It needs a
Controlvar server that includes the `event_id` change (#1640, in the first platform release after it
merged): an older server ignores the field.

- The SDK makes no id: without an `eventId` none is sent, and an event sent again is counted
  again. Make the id once, when the event happens, and give the same one each time you send it.
- `undefined`, `null` and `''` send none. The server refuses an id of more than 255 characters or
  one holding NUL with `422`; the SDK does not check it.
- An event fanned out to several experiments and flags carries the same id in every entry. A `429`
  that is retried sends the same id. A request that times out is not sent again by the SDK.

## Quick start

```ts
import { ExperimentationClient } from '@controlvar/js-sdk';

const client = new ExperimentationClient({ apiUrl: 'http://localhost:8000', apiKey: 'eptk_...' });
const user = { userId: 'user-123', attributes: { plan: 'pro' } };

const assignment = await client.getAssignment('checkout_flow', user); // { experimentKey, userId, variantId, variantName, isControl, configuration, assigned?, reason? }
const variant = await client.getVariant('checkout_flow', user);       // variantName, 'control' on failure
const { enabled, config, reason } = await client.evaluateFlag('new_search', user); // attributes sent as ?context=
const on = await client.isFeatureEnabled('new_search', user);         // false on failure

await client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
await client.track('user-123', 'page_view'); // no key → every experiment + flag answered for this user
```

A `track` without a key goes to every experiment and flag this client answered for the user in the
last 24 hours, cached or not, except an experiment that is a draft, paused, completed or archived.
With none, it is sent once with no key and stored as history. The keys live in this client's memory
(up to 10,000 users), and `clearCache()` keeps them. In a server, pass `experimentKey` on
conversions.

## API

| Method | Returns | On failure |
|---|---|---|
| `getAssignment(experimentKey, user)` | `Promise<Assignment>` | rejects with `ExperimentationError` (`code`, `status`, `body`) |
| `getVariant(experimentKey, user)` | `Promise<string>` | `defaultVariant` (`'control'`) |
| `evaluateFlag(flagKey, user)` | `Promise<FlagEvaluation>` — `{ key, enabled, config, reason?, value?, valueType? }` | rejects with `ExperimentationError` |
| `isFeatureEnabled(flagKey, user)` | `Promise<boolean>` | `false` |
| `getStringValue(flagKey, user, defaultValue)` | `Promise<string>`, the string a string flag serves this user | `defaultValue`; never rejects; `onError(err, 'value')` |
| `getNumberValue(flagKey, user, defaultValue)` | `Promise<number>`, the number a number flag serves this user | `defaultValue`; never rejects; `onError(err, 'value')` |
| `getJsonValue(flagKey, user, defaultValue)` | `Promise<T>`, a new copy of the object or array a json flag serves this user | `defaultValue`; never rejects; `onError(err, 'value')` |
| `getAllFlags(userId, attributes?)` | `Promise<Record<string, boolean>>` (not cached) | rejects |
| `track(userId, eventName, { value?, properties?, experimentKey?, featureFlagKey?, eventType?, timestamp?, eventId? })` | `Promise<void>` | never rejects; `onError` |
| `trackBatch(events)` | `Promise<{ successCount, failureCount, errors }>` (chunked at 100) | never rejects |
| `reportError({ errorType, message, featureFlagKey?, experimentKey?, userId?, stackTrace?, metadata?, timestamp? })` | `Promise<boolean>`: `true` when the server stored the report (201) | `false`, never rejects; `onError` |
| `fetchAssignments(userId, { activeOnly? })` | `Promise<AssignmentRecord[]>` from the server | rejects |
| `getAssignments(userId)`, `getEvaluatedFlags(userId)`, `clearCache()` | in-memory cache access | — |
| `getCachedFlags(userId)` | `FlagEvaluation[]`: cached, unexpired evaluations for the user; in local mode each carries `source` (`'local'` or `'server'`) | — |
| `ready({ timeoutMs? })`, `status()`, `close()` | local mode: readiness, ruleset state, shutdown (sends evaluation counts) | never reject |
| `consistentHash(userId, flagKey)`, `md5Hex(s)`, `md5Bytes(s)` | compatibility hash utilities (not the flag bucketing function) | — |

Config: `apiUrl`, `apiKey` (required); `timeoutMs` (5000), `cacheTtlMs` (300000; `0` caches nothing),
`defaultVariant` (`'control'`), `fetch`, `onError(err, 'track' | 'trackBatch' | 'refresh' | 'flush' | 'evaluate' | 'reportError' | 'value' | 'exposure')`,
`onExposure(exposure)` (see Exposure hook), `evaluation` (`'server'` or `'local'`), `refreshIntervalMs` (30000, minimum 5000), `maxStaleMs`.
Failures are never cached; concurrent calls for the same user + key share one request; a 429 is
retried once after `Retry-After`.

`user.attributes` is sent as `context` on assignment **and** on flag evaluation
(`&context=<url-encoded JSON>`, omitted when empty), so flag targeting rules evaluate against it
(`country` also matches `user.country`; nested objects flatten to dotted keys such as `app.version`).
Caches are keyed by user + key only — call `clearCache()` after changing a user's attributes.

`reportError` resolves `true` when the server stored the report. A stored report counts toward the
flag's error rate and automatic rollback only when the client's key has the `sdk:ruleset` scope and
was created by a user who can change feature flags; the flag's safety check shows the others as
`uncounted_error_count`.

### Enrolment: `assigned` and `reason`

An `Assignment` also carries `assigned` and `reason`. `assigned: false` means the server did not
enrol the user (`reason` is `'holdout'`, `'mutual_exclusion'` or `'targeting'`) and returned the
control variant so you render the default experience; no exposure was recorded. **When you export
exposures to a warehouse, exclude assignments with `assigned: false`**: the user was not enrolled,
and `reason` says why. A server that predates the fields sends neither, so `assigned` is
`undefined` (never `false`). Decide deliberately how to treat those rows rather than dropping them
with an `assigned === true` filter.

An experiment that is not ACTIVE answers every user with its control variant, `assigned: false` and
`reason` set to its status (`'draft'`, `'paused'`, `'completed'` or `'archived'`).

## Exposure hook

`onExposure` is a function you give the client when you create it. The client calls it once each
time the server enrols the user in an experiment and the answer did not come from the client's
cache: the moment the server records the exposure. Use it to send the exposure to your analytics
tool or warehouse.

```ts
const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',
  apiKey: process.env.CONTROLVAR_API_KEY!,
  onExposure: (exposure) => {
    // your analytics tool's call goes here
    console.log('exposure', exposure.experimentKey, exposure.variantName, exposure.userId);
  },
  onError: (error, operation) => {
    if (operation === 'exposure') console.warn('onExposure failed', error.cause);
  },
});
```

It receives an `Exposure`: `experimentKey`, `userId`, `variantId` (`null` when the server sends
none) and `variantName`. It is not called for a cached answer, a user the experiment did not enrol
or an experiment that is not running (`assigned: false`), a call that fails, or a feature flag. If
it throws, or returns a Promise that rejects, the answer is unchanged, and the error goes to
`onError` with the operation `'exposure'` and `code` `'HOOK_ERROR'`.

Full details: [Exposure hook](https://github.com/Controlvar/controlvar/blob/main/docs/sdk/javascript.md#exposure-hook).

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| SDK call | Method and path | 200 response |
|---|---|---|
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}` |
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `{key, enabled, config, reason}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` | `{"<flag_key>": bool}` |
| `track` with a key | `POST /api/v1/tracking/track` | ignored |
| `track` without keys | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100), one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` with no key | ignored |
| `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |
| `fetchAssignments` | `GET /api/v1/tracking/assignments/{user_id}?active_only=true` | list of rows |

Errors: 401 bad key, 404 an unknown experiment or flag key, or a draft experiment with no control variant (never cached), 422 invalid event, 429 rate limited.
A flag that is not ACTIVE is not an error: it answers `enabled: false`, `reason: "inactive"`.

## Contract smoke

```bash
cd sdk/js && npm run build --silent && node examples/contract_smoke.mjs
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (`sdk_contract_flag`), `CONTRACT_USER_ID`.

CI runs this SDK's contract smoke against a real API: `js` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

## Development

From `sdk/js` in a clone of [this repository](https://github.com/Controlvar/controlvar):

```bash
npm install
npm test
npm run build
```

- `npm test`: the Jest tests (fetch mocked; the local evaluator runs against tests/sdk-contract/ruleset-vectors.json)
- `npm run build`: tsc → dist/ (CommonJS + .d.ts)

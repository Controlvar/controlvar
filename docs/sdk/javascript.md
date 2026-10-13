# JavaScript SDK

`@getexperimently/js-sdk` (v0.1.0) is the JavaScript/TypeScript client for the
Experimently public API: experiment assignment, feature flag evaluation and event
tracking for Node >= 18 and browsers. Zero runtime dependencies — it uses the global `fetch`.

By default, flag evaluation and experiment assignment are decided **by the server**: every call
goes to the public API with your `X-API-Key`, the server buckets the user (sticky per user +
experiment), and the SDK caches the answer per user + key. In server-side code you can opt in to
[local evaluation](#local-evaluation-server-side-beta) (`evaluation: 'local'`): flags are then
answered in-process from the server's ruleset, with the server's answers, and experiments stay on
the server.

Source: `sdk/js`. For React apps use the [React SDK](react.md); for edge runtimes the
[Edge SDK](edge.md); for the vendor-neutral API the [OpenFeature provider](openfeature.md), which
delegates to this package.

CI runs this SDK's contract smoke against a real API: `js` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Installation

The `@getexperimently/js-sdk` package is on [npm](https://www.npmjs.com/package/@getexperimently/js-sdk).
0.1.0 is a beta release.

```bash
npm install @getexperimently/js-sdk@0.1.0
```

To work on the SDK itself, build it from a clone of this repository instead. `npm pack` writes
`getexperimently-js-sdk-0.1.0.tgz`:

```bash
git clone https://github.com/getexperimently/experimently.git
cd experimently/sdk/js
npm ci && npm run build && npm pack
```

Then, in your app, install the packed tarball:

```bash
npm install /path/to/experimently/sdk/js/getexperimently-js-sdk-0.1.0.tgz
```

CommonJS build (`dist/index.js`) with type declarations; `import`/`require` both work.

---

## Quick start

```typescript
import { ExperimentationClient } from '@getexperimently/js-sdk';

const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',   // origin only; the SDK appends /api/v1/...
  apiKey: process.env.EXPERIMENTLY_API_KEY!,
});

const user = { userId: 'user-123', attributes: { country: 'US', plan: 'pro' } };

// Sticky experiment assignment (POST /api/v1/tracking/assign); attributes are sent as `context`
const assignment = await client.getAssignment('checkout_button_color', user);
assignment.variantName;     // 'control' | 'treatment'
assignment.configuration;   // the variant's configuration object, or null

// Or just the name — 'control' (defaultVariant) when assignment fails
const variant = await client.getVariant('checkout_button_color', user);

// Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=...&context=<url-encoded attributes>)
const { key, enabled, config, reason } = await client.evaluateFlag('dark_mode', user);
const isEnabled = await client.isFeatureEnabled('dark_mode', user);   // false on failure

// Track a conversion for one experiment (POST /api/v1/tracking/track)
await client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_button_color' });

// Track without a key: fans out to every cached assignment and flag for the user (see below)
await client.track('user-123', 'page_view', { properties: { page: '/checkout' } });
```

The block is TypeScript and uses top-level `await`, so it runs as an ES module. To run it as it
is, save it as `quick-start.mts` in the project where you installed the package, set
`EXPERIMENTLY_API_KEY`, and run it with `node --experimental-strip-types quick-start.mts`
(Node 22.6 or later; it may print an `ExperimentalWarning`). As plain JavaScript, delete the `!` after
`process.env.EXPERIMENTLY_API_KEY`, save it as `quick-start.mjs` and run `node quick-start.mjs`.

---

## Configuration (`ClientConfig`)

```typescript
const client = new ExperimentationClient({
  apiUrl: 'https://api.example.com', // Required. Backend origin; trailing slashes are stripped.
  apiKey: 'eptk_...',                // Required. Sent as X-API-Key.
  timeoutMs: 5000,                   // Per-request timeout (default 5000)
  cacheTtlMs: 300_000,               // Successful evaluations/assignments reused per user + key (default 5 min)
  defaultVariant: 'control',         // Returned by getVariant when assignment fails (default 'control')
  fetch: customFetch,                // Optional custom fetch (tests, polyfills); defaults to globalThis.fetch
  onError: (err, operation) => {},   // Optional; called when track / trackBatch (or, in local mode, refresh / flush / evaluate) swallow a failure
  evaluation: 'server',              // 'server' (default) or 'local' (server-side only; see Local evaluation)
});
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `apiUrl` | `string` | — | Backend origin, e.g. `https://api.example.com`; the SDK appends `/api/v1/...` |
| `apiKey` | `string` | — | API key, sent as `X-API-Key` |
| `timeoutMs` | `number` | `5000` | Per-request timeout (`AbortController`); also caps the 429 retry delay |
| `cacheTtlMs` | `number` | `300000` | TTL of successful evaluations/assignments, per user + key |
| `defaultVariant` | `string` | `'control'` | Variant name `getVariant` returns on failure |
| `fetch` | `typeof fetch` | global `fetch` | Custom fetch implementation |
| `onError` | `(error: ExperimentationError, op: 'track' \| 'trackBatch' \| 'refresh' \| 'flush' \| 'evaluate') => void` | — | Only way to observe swallowed failures; a throwing handler is itself swallowed. `'refresh'`, `'flush'` and `'evaluate'` occur only in local mode |
| `evaluation` | `'server' \| 'local'` | `'server'` | `'local'`: answer flags in-process from `GET /api/v1/sdk/ruleset`; needs a key with the `sdk:ruleset` scope; throws in a browser |
| `refreshIntervalMs` | `number` | `30000` | Local mode: ruleset refresh interval (minimum 5000, ±10% jitter) |
| `maxStaleMs` | `number` | none | Local mode: evaluate on the server once the ruleset has not been refreshed for this long |

The constructor throws a plain `Error` (`apiKey is required` / `apiUrl is required`) when either
required field is missing, when `evaluation` is not `'server'` or `'local'`, and when
`evaluation: 'local'` is used in a browser.

### `UserContext`

```typescript
interface UserContext {
  userId: string;                       // stable id; the server buckets on it
  attributes?: Record<string, unknown>; // sent as `context` on assignment and on flag evaluation (targeting rules)
}
```

### Targeting context

`attributes` is what the platform's targeting rules evaluate against. It is sent as `context` in
the `POST /api/v1/tracking/assign` body and, when it is a non-empty object, as
`context=<url-encoded JSON>` on `GET /api/v1/feature-flags/evaluate/{flagKey}` (and on
`getAllFlags(userId, attributes)`), so a flag whose dashboard rule says
`os_version semver_gte 17.0.0 AND tier equals premium` turns on only for matching users. Top-level
keys are also reachable under `user.` / `device.` / `app.` aliases in rules (`country` matches
`user.country`), and nested objects flatten to dotted keys (`{ app: { version: '3.2.1' } }`
answers `app.version`).

Attributes are assumed **stable per user**: evaluations and assignments are cached by user + key
only, so call `clearCache()` after changing a user's attributes.

`getAssignment` and `evaluateFlag` reject with `ExperimentationError` (`code: 'INVALID_RESPONSE'`,
`user.userId is required`) when `userId` is missing or empty; `getVariant` / `isFeatureEnabled`
turn that into their safe default.

---

## API

| Method | Signature | Returns | On failure |
|--------|-----------|---------|------------|
| `getAssignment` | `(experimentKey, user: UserContext) => Promise<Assignment>` | `{ experimentKey, userId, variantId, variantName, isControl, configuration, assigned?, reason? }` | Rejects with `ExperimentationError` (404 when the experiment is not ACTIVE) |
| `getVariant` | `(experimentKey, user) => Promise<string>` | `assignment.variantName` | `defaultVariant` (`'control'`); never rejects |
| `evaluateFlag` | `(flagKey, user) => Promise<FlagEvaluation>` | `{ key, enabled, config, reason? }` (`user.attributes` sent as `context`) | Rejects with `ExperimentationError` (404 for an unknown key); a flag that exists but is not ACTIVE resolves `enabled: false`, `reason: "inactive"` |
| `isFeatureEnabled` | `(flagKey, user) => Promise<boolean>` | `evaluation.enabled` | `false`; never rejects |
| `getAllFlags` | `(userId, attributes?) => Promise<Record<string, boolean>>` | `{ flagKey: enabled }`; `attributes` sent as `context`; not cached, not part of the fan-out | Rejects |
| `track` | `(userId, eventName, options?: TrackOptions) => Promise<void>` | — | Never rejects; `onError(err, 'track')` |
| `trackBatch` | `(events: TrackEvent[]) => Promise<BatchResult>` | `{ successCount, failureCount, errors }` | Never rejects; a failed chunk counts all its events as failures and calls `onError(err, 'trackBatch')` |
| `fetchAssignments` | `(userId, { activeOnly?: boolean } = { activeOnly: true }) => Promise<AssignmentRecord[]>` | Server-side assignment rows (raw objects); not cached | Rejects |
| `getAssignments` | `(userId) => Assignment[]` | Cached, unexpired assignments in assignment order (no network) | — |
| `getEvaluatedFlags` | `(userId) => string[]` | Keys of cached, successfully evaluated flags (no network) | — |
| `clearCache` | `() => void` | Drops both caches | — |
| `ready` | `({ timeoutMs? }?) => Promise<ReadyResult>` | `{ ok: true, rulesetVersion }` or `{ ok: false, error }`; `{ ok: true, rulesetVersion: null }` at once in server mode | Never rejects |
| `status` | `() => LocalEvaluationStatus` | `{ evaluation, ready, rulesetVersion, lastRefreshAt, lastError, serverEvaluatedFlags }` | — |
| `close` | `() => Promise<void>` | Local mode: stops the ruleset refresh and sends the remaining evaluation counts; later calls are evaluated by the server. A no-op in server mode | Never rejects |

### Enrolment: `assigned` and `reason`

An `Assignment` also carries `assigned` and `reason`. `assigned: false` means the server did not
enrol the user (`reason` is `'holdout'`, `'mutual_exclusion'` or `'targeting'`) and returned the
control variant so you render the default experience; no exposure was recorded. **When you export
exposures to a warehouse, exclude assignments with `assigned: false`**: the user was not enrolled,
and `reason` says why. A server that predates the fields sends neither, so `assigned` is
`undefined` (never `false`). Decide deliberately how to treat those rows rather than dropping them
with an `assigned === true` filter.

`TrackOptions`: `value?` (number), `properties?` (sent as `metadata`), `experimentKey?`,
`featureFlagKey?`, `eventType?` (defaults to the event name), `timestamp?` (`Date` or string, sent as
ISO-8601). `TrackEvent` = `TrackOptions & { userId, eventName }`.

### Tracking and the fan-out rule

- With `experimentKey` and/or `featureFlagKey`: exactly one `POST /api/v1/tracking/track`.
- Without a key: one `POST /api/v1/tracking/batch` (chunked at 100) containing one entry per
  experiment the user has been assigned to in this client (`experiment_key`) plus one per flag
  evaluated for the user (`feature_flag_key`), taken from the cache. **If nothing is cached, nothing
  is sent.** This is what makes a single `track(userId, 'purchase', …)` count as a conversion for
  every experiment the user is in.
- `trackBatch` applies the same expansion to keyless entries, then sends everything in chunks of 100
  and sums the server's `success_count` / `failure_count` / `errors`.

Conversions are matched to metrics by **event name**.

### Caching, dedupe and retries

- Successful assignments and evaluations are cached per user + key for `cacheTtlMs`; failures are
  never cached, so the next call retries.
- Concurrent calls for the same user + key share one in-flight request (one assignment, one
  exposure event).
- A `429` is retried once after the server's `Retry-After` (seconds or HTTP date; default 1 s,
  capped at `timeoutMs`).

### Errors

`getAssignment`, `evaluateFlag`, `getAllFlags` and `fetchAssignments` reject with
`ExperimentationError` (`name === 'ExperimentationError'`):

| `code` | Meaning | `status` |
|--------|---------|----------|
| `HTTP_ERROR` | Non-2xx response; `body` holds the parsed JSON error (e.g. `{detail}`) | HTTP status (401 bad key, 404 an experiment unknown or not ACTIVE, or an unknown flag key, 422, 429 after the retry) |
| `NETWORK_ERROR` | `fetch` rejected (DNS, connection refused, CORS, no fetch available) | `undefined` |
| `TIMEOUT` | The request exceeded `timeoutMs` | `undefined` |
| `INVALID_RESPONSE` | 2xx but the body is not what the SDK expects (or `userId` missing) | HTTP status or `undefined` |

```typescript
import { ExperimentationClient, ExperimentationError } from '@getexperimently/js-sdk';

try {
  const a = await client.getAssignment('checkout_flow', user);
} catch (err) {
  if (err instanceof ExperimentationError && err.status === 404) {
    // experiment is not ACTIVE — fall back to control
  }
}
```

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.
`apiUrl` is the origin only; the SDK appends the paths below.

| SDK call | Method and path | Body / query | 200 response |
|---|---|---|---|
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` | `{"experiment_key","user_id","context"?: object}` | `{"experiment_key","user_id","variant_id","variant_name","is_control","configuration"}` |
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<id>&context=<url-encoded JSON>` | `context` = `user.attributes` (omitted when empty) | `{"key","enabled","config","reason"}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` | `context` = `attributes` (omitted when empty) | `{"<flag_key>": bool, ...}` |
| `track` with a key | `POST /api/v1/tracking/track` | `{"event_type","event_name","user_id","experiment_key"?,"feature_flag_key"?,"value"?,"metadata"?,"timestamp"?}` | stored event (ignored) |
| `track` without a key, `trackBatch` | `POST /api/v1/tracking/batch` | `{"events":[<track body>...]}` (max 100) | `{"success_count","failure_count","errors"}` |
| `fetchAssignments` | `GET /api/v1/tracking/assignments/{user_id}?active_only=true` | — | list of assignment rows |
| local mode: ruleset refresh | `GET /api/v1/sdk/ruleset` (beta) | `If-None-Match: "<version>"` | the ruleset, `ETag`; `304` when unchanged |
| local mode: evaluation counts | `POST /api/v1/tracking/evaluations` (beta) | `{"evaluations":[{"flag_key","count","enabled_count","window_start","window_end"}]}` | `{"accepted","errors"}` |

Errors: 401 bad key; 404 an experiment unknown or not ACTIVE, or an unknown flag key; 422 invalid event; 429
rate limited (`Retry-After` header, retried once).

---

## TypeScript types

```typescript
import type {
  ClientConfig, UserContext, Assignment, FlagEvaluation,
  TrackOptions, TrackEvent, BatchResult, AssignmentRecord,
} from '@getexperimently/js-sdk';

interface Assignment {
  experimentKey: string;
  userId: string;
  variantId: string | null;
  variantName: string;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
}

interface FlagEvaluation {
  key: string;
  enabled: boolean;
  config: unknown | null;
  reason?: string;     // 'targeting_rule' | 'rollout' | 'inactive' | 'error'; undefined when the server does not send it
  source?: 'server' | 'local'; // local mode only: answered in-process, or by the server
}

interface BatchResult {
  successCount: number;
  failureCount: number;
  errors: unknown[];   // server-reported per-event errors plus {message, status?} per failed chunk
}
```

The raw wire shapes (`AssignResponse`, `FlagEvaluateResponse`, `BatchResponse`, `TrackBody`) are
exported too.

---

## Hash utilities (compatibility only)

`consistentHash(userId, flagKey)`, `md5Hex(input)` and `md5Bytes(input)` are exported so the
cross-SDK golden-vector tests and custom integrations can verify parity:

```
MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → ÷ 2^32 (4294967296)
consistentHash('user-123', 'my-flag') ≈ 0.6927449859
```

**Nothing in the SDK calls `consistentHash` to pick a variant or decide a flag.** It is not the
function the server buckets flag rollouts with: that is `md5-mod100-v1`, the whole MD5 digest of
`"{userId}:{flagKey}"` read as a big-endian integer, mod 100, which local evaluation reproduces
internally (`consistentHash('user-123', 'my-flag')` is bucket 69; the server puts that user in
bucket 79).

---

## Local evaluation (server-side, beta)

With `evaluation: 'local'` the client downloads the flag ruleset (`GET /api/v1/sdk/ruleset`),
refreshes it every `refreshIntervalMs` (30 s by default, 5 s minimum), and answers `evaluateFlag`,
`isFeatureEnabled` and `getAllFlags` in-process whenever it can give exactly the server's answer;
otherwise it makes the usual request. Experiments are always assigned by the server. It needs an
Experimently server at 0.11.0 or later and a key with the `sdk:ruleset` scope, and it is for
server-side code only: the constructor throws in a browser.

```typescript
const client = new ExperimentationClient({
  apiUrl: process.env.EXPERIMENTLY_API_URL!,
  apiKey: process.env.EXPERIMENTLY_SERVER_KEY!, // sdk:ruleset scope
  evaluation: 'local',
});
await client.ready({ timeoutMs: 5_000 });        // optional; never rejects
const { enabled, reason, source } = await client.evaluateFlag('new_search', user); // source: 'local' | 'server'
await client.close();                            // on shutdown: sends the evaluation counts
```

A change made in the dashboard reaches the process at its next successful refresh. While the API
is unreachable the last ruleset keeps being served (set `maxStaleMs` to bound that); a 401 or 403
on refresh discards it. What is answered locally, the refresh failure table, safety monitoring and
troubleshooting: [Local evaluation](local-evaluation.md).

---

## Browser vs Node.js

The SDK is isomorphic. In the browser the API key is visible to end users, and keys are not scoped
for browser use: every active key, including one shipped to a browser, can evaluate flags and track
events as the user who created it. Use a dedicated key for browser traffic so it can be revoked on its
own, and never give it the `sdk:ruleset` scope, which lets a key download every flag's targeting
rules. Server-side, lower `timeoutMs` if the SDK sits on a request path:

```typescript
const client = new ExperimentationClient({
  apiUrl: process.env.EXPERIMENTLY_API_URL!,
  apiKey: process.env.EXPERIMENTLY_API_KEY!,
  timeoutMs: 1000,
});
```

---

## Contract smoke

```bash
cd sdk/js && npm run build --silent && node examples/contract_smoke.mjs
```

It prints one line:

```json
{"sdk":"js","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The script assigns twice
(sticky), evaluates the flag, tracks `purchase` with a key, tracks `page_view` without a key
(fan-out) and sends a 2-event `trackBatch`. `npm run smoke` is a shortcut.

With a seeded backend (`python backend/scripts/seed_sdk_contract.py`):
`python tests/sdk-contract/live/run_live_contract.py --sdk js --strict`. That also runs
`examples/local_eval_smoke.mjs` with the seed's `sdk:ruleset` key (the `js (local)` row): 40
evaluations answered locally, the counts sent on `close()`, then two error reports sent with the
same key, and the runner checks in the database that no evaluate call was made, that the safety
monitor's denominator grew by exactly 40 and that both reports were counted.

---

## Development

`npx jest` runs the unit tests, with `fetch` mocked; `npm run build` runs `tsc` into `dist/`
(CommonJS and `.d.ts`):

```bash
cd sdk/js && npm install
npx jest
npm run build
```

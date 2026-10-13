# @getexperimently/js-sdk

**Beta.** This is a 0.x release: pin the exact version you test against.

JavaScript/TypeScript client for the Experimently public API. Node >= 18 and browsers,
zero runtime dependencies (global `fetch`), CommonJS build with type declarations.

Flag evaluation and experiment assignment are **decided by the server** by default; results are
cached per user + key in memory. Server-side code can opt in to **local evaluation**
(`evaluation: 'local'`, beta): flags are answered in-process from the server's ruleset, with the
server's answers, and anything that cannot be answered exactly goes to the server. See
[`docs/sdk/local-evaluation.md`](https://github.com/getexperimently/experimently/blob/main/docs/sdk/local-evaluation.md).

Full documentation: [`docs/sdk/javascript.md`](https://github.com/getexperimently/experimently/blob/main/docs/sdk/javascript.md).

## Install

The `@getexperimently/js-sdk` package is on [npm](https://www.npmjs.com/package/@getexperimently/js-sdk). 0.1.0 is a
beta release.

```bash
npm install @getexperimently/js-sdk@0.1.0
```

To work on the SDK itself, see Development below.

## Quick start

```ts
import { ExperimentationClient } from '@getexperimently/js-sdk';

const client = new ExperimentationClient({ apiUrl: 'http://localhost:8000', apiKey: 'eptk_...' });
const user = { userId: 'user-123', attributes: { plan: 'pro' } };

const assignment = await client.getAssignment('checkout_flow', user); // { experimentKey, userId, variantId, variantName, isControl, configuration, assigned?, reason? }
const variant = await client.getVariant('checkout_flow', user);       // variantName, 'control' on failure
const { enabled, config, reason } = await client.evaluateFlag('new_search', user); // attributes sent as ?context=
const on = await client.isFeatureEnabled('new_search', user);         // false on failure

await client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
await client.track('user-123', 'page_view'); // no key → fans out to every cached assignment + flag
```

## API

| Method | Returns | On failure |
|---|---|---|
| `getAssignment(experimentKey, user)` | `Promise<Assignment>` | rejects with `ExperimentationError` (`code`, `status`, `body`) |
| `getVariant(experimentKey, user)` | `Promise<string>` | `defaultVariant` (`'control'`) |
| `evaluateFlag(flagKey, user)` | `Promise<FlagEvaluation>` — `{ key, enabled, config, reason? }` | rejects with `ExperimentationError` |
| `isFeatureEnabled(flagKey, user)` | `Promise<boolean>` | `false` |
| `getAllFlags(userId, attributes?)` | `Promise<Record<string, boolean>>` (not cached) | rejects |
| `track(userId, eventName, { value?, properties?, experimentKey?, featureFlagKey?, eventType?, timestamp? })` | `Promise<void>` | never rejects; `onError` |
| `trackBatch(events)` | `Promise<{ successCount, failureCount, errors }>` (chunked at 100) | never rejects |
| `fetchAssignments(userId, { activeOnly? })` | `Promise<AssignmentRecord[]>` from the server | rejects |
| `getAssignments(userId)`, `getEvaluatedFlags(userId)`, `clearCache()` | in-memory cache access | — |
| `ready({ timeoutMs? })`, `status()`, `close()` | local mode: readiness, ruleset state, shutdown (sends evaluation counts) | never reject |
| `consistentHash(userId, flagKey)`, `md5Hex(s)`, `md5Bytes(s)` | compatibility hash utilities (not the flag bucketing function) | — |

Config: `apiUrl`, `apiKey` (required); `timeoutMs` (5000), `cacheTtlMs` (300000),
`defaultVariant` (`'control'`), `fetch`, `onError(err, 'track' | 'trackBatch' | 'refresh' | 'flush' | 'evaluate')`,
`evaluation` (`'server'` or `'local'`), `refreshIntervalMs` (30000, minimum 5000), `maxStaleMs`.
Failures are never cached; concurrent calls for the same user + key share one request; a 429 is
retried once after `Retry-After`.

`user.attributes` is sent as `context` on assignment **and** on flag evaluation
(`&context=<url-encoded JSON>`, omitted when empty), so flag targeting rules evaluate against it
(`country` also matches `user.country`; nested objects flatten to dotted keys such as `app.version`).
Caches are keyed by user + key only — call `clearCache()` after changing a user's attributes.

### Enrolment: `assigned` and `reason`

An `Assignment` also carries `assigned` and `reason`. `assigned: false` means the server did not
enrol the user (`reason` is `'holdout'`, `'mutual_exclusion'` or `'targeting'`) and returned the
control variant so you render the default experience; no exposure was recorded. **When you export
exposures to a warehouse, exclude assignments with `assigned: false`**: the user was not enrolled,
and `reason` says why. A server that predates the fields sends neither, so `assigned` is
`undefined` (never `false`). Decide deliberately how to treat those rows rather than dropping them
with an `assigned === true` filter.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json`, `Accept: application/json`.

| SDK call | Method and path | 200 response |
|---|---|---|
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}` |
| `evaluateFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON>` | `{key, enabled, config, reason}` |
| `getAllFlags` | `GET /api/v1/feature-flags/user/{user_id}?context=<url-encoded JSON>` | `{"<flag_key>": bool}` |
| `track` with a key | `POST /api/v1/tracking/track` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (≤ 100) | `{success_count, failure_count, errors}` |
| `fetchAssignments` | `GET /api/v1/tracking/assignments/{user_id}?active_only=true` | list of rows |

Errors: 401 bad key, 404 an experiment unknown or not ACTIVE, or an unknown flag key (never cached), 422 invalid event, 429 rate limited.
A flag that is not ACTIVE is not an error: it answers `enabled: false`, `reason: "inactive"`.

## Contract smoke

```bash
cd sdk/js && npm run build --silent && node examples/contract_smoke.mjs
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (`sdk_contract_flag`), `CONTRACT_USER_ID`.

CI runs this SDK's contract smoke against a real API: `js` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Development

From `sdk/js` in a clone of [this repository](https://github.com/getexperimently/experimently):

```bash
npm install
npm test
npm run build
```

- `npm test`: the Jest tests (fetch mocked; the local evaluator runs against tests/sdk-contract/ruleset-vectors.json)
- `npm run build`: tsc → dist/ (CommonJS + .d.ts)

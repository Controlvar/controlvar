# Edge SDK

`@controlvar/edge-sdk` (v0.2) is written for edge runtimes, with adapters for Cloudflare
Workers, Vercel Edge Middleware and Deno Deploy, and declares Node >= 18. It has zero runtime
dependencies and uses no Node.js built-ins — only Web-standard `fetch`, `AbortController` and
`Headers`. Its unit tests and its contract smoke run it under Node; the adapters are tested there
with stubbed KV and request objects, not on the three platforms.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and the
SDK caches the answer per user + key — in memory and, optionally, in a shared KV store so other
isolates can reuse it. Nothing is bucketed locally.

Source: `sdk/edge`.

CI runs this SDK's contract smoke against a real API: `edge` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Installation

**Not yet published.** `@controlvar/edge-sdk` is not on npm, so build it from source in a
clone of this repository. `npm pack` writes `controlvar-edge-sdk-0.2.0.tgz`:

```bash
git clone https://github.com/Controlvar/controlvar.git
cd controlvar/sdk/edge
npm ci && npm run build && npm pack
```

Then, in your app, install the packed tarball:

```bash
npm install /path/to/controlvar/sdk/edge/controlvar-edge-sdk-0.2.0.tgz
```

Entry points: the package root (`EdgeExperimentationClient`), `/cloudflare`, `/vercel` and `/deno`.
The build (`npm run build`) emits ESM to `dist/` with type declarations.

---

## Quick start

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

```typescript
import { EdgeExperimentationClient } from '@controlvar/edge-sdk';

const client = new EdgeExperimentationClient({
  apiKey: env.EP_API_KEY,
  baseUrl: 'http://localhost:8000',   // origin only; the SDK appends /api/v1/...
});

// Sticky experiment assignment (server-side); `attributes` is sent as `context`
const assignment = await client.getAssignment('checkout_flow', userId, { country: 'US' });
// { experimentKey, userId, variantId, variantName, isControl, configuration } or null on failure

// Flag evaluation
const { key, enabled, config } = await client.evaluateFlag('new-checkout', userId);

// Zero-latency re-reads from the in-memory cache (no network)
client.evaluateFlagSync('new-checkout', userId);    // boolean, false when not cached
client.getAssignmentSync('checkout_flow', userId);  // variant name, null when not cached

// Tracking — never throws
await client.track('purchase', userId, { sku: 'A1' }, { value: 49.99, experimentKey: 'checkout_flow' });
await client.track('page_view', userId, { path: '/' }); // no key: fanned out, see below
```

---

## Evaluation order

Every `evaluateFlag` / `getAssignment` call resolves in this order:

1. **In-memory cache** (per user + key, `cacheTtlMs`, default 60 s) — zero latency.
2. **Shared store** (`config.store`; Cloudflare KV / Deno KV via the adapters) — one KV read.
3. **Network** — one request to the backend; the result is written to both caches.

Failures (network errors, timeouts, and a 404: an unknown experiment or flag key, or a draft
experiment with no control variant) are **never cached**:
`evaluateFlag` resolves to `{ key, enabled: false, config: null }` and `getAssignment` to `null`, and
the next call retries. Concurrent calls for the same user + key share one in-flight request.

`evaluateFlagSync` / `getAssignmentSync` / `getCachedFlag` / `getCachedAssignment` read the in-memory
cache only and never touch the network — call the async method first (typically once at the top of
the request handler), then use the sync accessors freely.

---

## Cloudflare Workers

A Worker's secrets and bindings arrive in `env` with each request, so build the client inside the
fetch handler:

```typescript
import { CloudflareExperimentationClient } from '@controlvar/edge-sdk/cloudflare';

export default {
  async fetch(request, env, ctx) {
    const client = new CloudflareExperimentationClient({
      apiKey: env.EP_API_KEY,
      baseUrl: 'https://api.your-platform.example.com',
      kvNamespace: env.EP_FLAGS_KV, // optional: share cached results across worker instances
    });
    const userId = request.headers.get('X-User-Id') ?? 'anon';

    const { enabled } = await client.evaluateFlag('new-checkout', userId);
    ctx.waitUntil(client.track('page_view', userId, { path: new URL(request.url).pathname }));

    return enabled ? Response.redirect('https://checkout-v2.example.com', 302) : fetch(request);
  },
};
```

`withExperimentation(handler, config, clientEnvKey = 'EP_CLIENT')` wraps a
`(request, env, ctx)` handler: it builds a `CloudflareExperimentationClient` from `config` on every
call and passes it to the handler as `env.EP_CLIENT`. Its `config` is a fixed object, so it cannot
read `env`; when `config.kvNamespace` is not set it uses the `env.KV` binding.

### KV caching

When a `kvNamespace` is provided the SDK stores each **server result** under
`ep:flag:{userId}:{flagKey}` / `ep:assign:{userId}:{experimentKey}` with `expirationTtl` derived from
`cacheTtlMs` (Cloudflare enforces a 60 s minimum; override with `kvTtlSeconds`). Another instance
serving the same user reads the value from KV instead of calling the API. No bootstrap payload is
stored any more.

The binding is declared in `wrangler.toml` and read as `env.EP_FLAGS_KV` in the handler above:

```toml
[[kv_namespaces]]
binding = "EP_FLAGS_KV"
id = "your-kv-namespace-id"
```

`kvTtlSeconds` sets the KV entry TTL (default `cacheTtlMs / 1000`, never below 60) and `cacheTtlMs`
the in-memory TTL (default 60 s), both in the client's options.

`CloudflareExperimentationClient` can also be constructed directly; its `loadFromKvOrApi()` and
`refreshAndStore()` methods are deprecated no-ops.

---

## Vercel Edge Middleware

`createEdgeMiddleware({ apiKey, baseUrl, flagKeys?, userIdHeaderName?, userIdCookieName? })` from
`@controlvar/edge-sdk/vercel` returns a `(request: Request) => Promise<Response>` that builds
one client and, on each request:

1. Resolves the user id from the `X-User-Id` header (`userIdHeaderName`) or the `ep_user_id`
   cookie (`userIdCookieName`). Anonymous requests evaluate nothing.
2. Evaluates each `flagKeys` entry **in parallel on the server**; without `flagKeys` it injects every
   flag the server reports via `GET /api/v1/feature-flags/user/{user_id}`.
3. Injects `X-EP-Flag-{flagKey}: "true"|"false"` and `X-EP-User-Id` request headers for downstream
   pages and API routes. Evaluation failures are reported as `"false"` and never break the request.
4. Passes the request on with `fetch(new Request(request, { headers }))`.

In a Next.js project the adapter's own note says to pass the same headers with
`NextResponse.next({ request: { headers } })` instead of that pass-through; `extractUserId` and
`evaluateFlagsForRequest` are exported for that.

---

## Deno Deploy

`createDenoHandler(handler, config)` from `@controlvar/edge-sdk/deno` returns a
`(request: Request) => Promise<Response>` for `Deno.serve`, and calls `handler(request, client)`.
`config` takes the client options plus `kv` (a Deno KV instance, optional: share cached results
across isolates) and `kvTtlMs` (default `cacheTtlMs`).

`createDenoHandler` creates one client per isolate so the in-memory cache is reused across
requests. With `kv`, results are stored under `["ep", "flag:{userId}:{flagKey}"]` /
`["ep", "assign:{userId}:{experimentKey}"]` as JSON strings with `expireIn`.

---

## Tracking and the fan-out rule

`track(eventName, userId, properties?, options?)` never throws. `options` accepts `value`,
`experimentKey`, `featureFlagKey`, `eventType` (defaults to `eventName`) and `timestamp`
(`Date` or ISO string).

- With `experimentKey` and/or `featureFlagKey`: one `POST /api/v1/tracking/track`.
- Without a key: one `POST /api/v1/tracking/batch` (chunked at 100) containing one entry per
  experiment the user has been assigned to in this client plus one per flag evaluated for the user —
  from the **in-memory** cache. If nothing is cached, nothing is sent.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's in-memory cache entries have
expired (`cacheTtlMs`, 60 s by default) is not sent, so pass `experimentKey` on a conversion that
can come later.

`trackBatch(events)` sends many events at once (chunked at 100) and resolves to
`{ successCount, failureCount, errors }`; keyless entries are expanded with the same rule.

Conversions are matched to metrics by **event name**.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag`, `isFeatureEnabled`, Vercel `flagKeys` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `getAllFlags`, Vercel middleware without `flagKeys` | `GET /api/v1/feature-flags/user/{user_id}` | — | `{"<flag_key>": bool, ...}` |
| `getAssignment`, `getVariant` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [ ...track bodies ]}` (max 100 per request) | `{success_count, failure_count, errors}` |

---

## Configuration reference

```typescript
interface EdgeSdkConfig {
  apiKey: string;        // Required. Sent as X-API-Key.
  baseUrl: string;       // Required. Backend origin; the SDK appends /api/v1/... There is no default.
  cacheTtlMs?: number;   // Per user + key cache TTL (default: 60_000 ms).
  timeout?: number;      // Per-request timeout (default: 500 ms).
  store?: EdgeStore;     // Optional shared KV store ({ get(key), put(key, value, ttlMs) }).
  fetch?: typeof fetch;  // Custom fetch (defaults to the global).
}
```

Adapter configs add `kvNamespace` / `kvTtlSeconds` (Cloudflare), `kv` / `kvTtlMs` (Deno) and
`flagKeys` / `userIdHeaderName` / `userIdCookieName` (Vercel).

---

## API reference

### `EdgeExperimentationClient`

| Method | Returns | Description |
|---|---|---|
| `evaluateFlag(flagKey, userId)` | `Promise<FlagEvaluation>` | Server-decided; `{key, enabled, config}`. Disabled evaluation on failure (not cached). |
| `isFeatureEnabled(flagKey, userId)` | `Promise<boolean>` | `evaluateFlag(...).enabled` |
| `getAllFlags(userId)` | `Promise<Record<string, boolean>>` | Every flag for the user; `{}` on failure. Not cached. |
| `getAssignment(experimentKey, userId, attributes?)` | `Promise<Assignment \| null>` | Sticky server assignment; `attributes` → `context`. `null` on failure (not cached). |
| `getVariant(experimentKey, userId, attributes?)` | `Promise<string \| null>` | `getAssignment(...).variantName` |
| `evaluateFlagSync(flagKey, userId)` | `boolean` | In-memory cache only; `false` when not cached. |
| `getAssignmentSync(experimentKey, userId)` | `string \| null` | In-memory cache only. |
| `getCachedFlag` / `getCachedAssignment` | object \| `null` | In-memory cache only. |
| `track(eventName, userId, properties?, options?)` | `Promise<void>` | Fire-and-forget; fan-out rule above. |
| `trackBatch(events)` | `Promise<BatchResult>` | Up to 100 events per request; never rejects. |
| `getAssignments(userId)` / `getEvaluatedFlags(userId)` | `Assignment[]` / `string[]` | What the fan-out would send. |
| `clearCache()` | `void` | Drop the in-memory caches. |
| `refreshFlags()` | `Promise<void>` | **Deprecated** no-op (definitions are no longer fetched). |
| `flagCount` / `experimentCount` | `number` | Size of the in-memory caches (getters). |

No public method throws or rejects: the constructor is the only exception, when `apiKey` or
`baseUrl` is missing (`apiKey is required`, `baseUrl is required: ...`).
There is no `onError` hook and no automatic 429 retry; `trackBatch` surfaces HTTP failures as
`{ message, status }` entries in `errors` (`EdgeApiError` carries the `status`). Failures are never
cached, so the next call retries.

`hashUser(userId, flagKey)` (and `md5` / `md5Hex`) remain exported as utilities for the cross-SDK
golden-vector tests; nothing in the SDK uses them to decide a variant.

### Types

```typescript
interface FlagEvaluation { key: string; enabled: boolean; config: unknown | null }

interface TrackOptions { value?: number; experimentKey?: string; featureFlagKey?: string; eventType?: string; timestamp?: Date | string }
interface TrackEvent extends TrackOptions { eventName: string; userId: string; properties?: Record<string, unknown> }
interface BatchResult { successCount: number; failureCount: number; errors: unknown[] }

interface Assignment {
  experimentKey: string;
  userId: string;
  variantId: string | null;
  variantName: string;
  isControl: boolean;
  configuration: Record<string, unknown> | null;
}
```

---

## Migrating from 0.1

- `evaluateFlag` now resolves to `FlagEvaluation` instead of `boolean` — use `.enabled` or
  `isFeatureEnabled`. The `attributes` argument was dropped: `evaluateFlag` sends the user id
  alone (`?user_id=`), so the server evaluates the flag's targeting rules against an empty context.
- `getAssignment` now resolves to `Assignment | null` instead of `string | null` — use `.variantName`
  or `getVariant`. It accepts `attributes` (sent as `context`).
- `bootstrapFlags`, `FeatureFlag`, `TargetingRule`, `FlagVariant`, `Experiment`, `BootstrapResponse`,
  `evaluateFlag`/`assignVariant`/`matchesRule` (local evaluator) and `isBootstrapped` are gone; the
  `/api/v1/edge/bootstrap` and `/api/v1/feature-flags/{key}` definition endpoints are no longer used.
  `GET /api/v1/edge/bootstrap` has since been removed from the server (#737); for server-side
  local evaluation see [Local evaluation](local-evaluation.md).
- `refreshFlags`, `loadFromKvOrApi` and `refreshAndStore` are deprecated no-ops.
- `track` gained a fourth `options` argument and no longer posts to `/api/v1/events`.

---

## Contract smoke

```bash
cd sdk/edge && npm run build --silent && node examples/contract_smoke.mjs
```

It prints one line:

```json
{"sdk":"edge","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Runs under plain Node
(no adapter) and assigns twice (sticky), evaluates the flag (async and sync), tracks with a key,
tracks without a key (fan-out) and sends a 2-event `trackBatch`. `npm run smoke` is a shortcut.

Or, with a seeded backend: `python tests/sdk-contract/live/run_live_contract.py --sdk edge --strict`.

---

## Development

`npx jest` runs the unit tests, with `fetch` mocked; `npm run build` runs `tsc` into `dist/`
(ESM and `.d.ts`):

```bash
cd sdk/edge && npm install
npx jest
npm run build
```

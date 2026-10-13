# OpenFeature Provider

## What is OpenFeature?

[OpenFeature](https://openfeature.dev) is a CNCF (Cloud Native Computing Foundation) standard
for feature flag management. It defines a vendor-neutral API so that application code only
talks to a single, stable interface — **without depending on any specific feature flag
vendor's SDK**.

This means you can:
- Switch feature flag providers without changing a line of application code.
- Use the same `client.getBooleanValue("my-flag", false)` call whether you are using
  LaunchDarkly, Split, Flagsmith, or this Experimently.
- Add OpenFeature hooks (logging, metrics, tracing) once and have them apply to all providers.

---

## TypeScript / Node.js Provider

`@getexperimently/openfeature-provider` (v0.1.0 beta, source `sdk/openfeature`) implements the
`@openfeature/server-sdk` `Provider` interface by delegating every evaluation to the
[JavaScript SDK](javascript.md) (`@getexperimently/js-sdk`), which calls
`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targetingKey>` and caches the answer per
user + flag. Flags are decided **by the server**; nothing is bucketed locally and no flag
definitions are downloaded. Requires Node >= 18 and `@openfeature/server-sdk >= 1.7`.

CI runs this SDK's contract smoke against a real API: `openfeature` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).
The live runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk openfeature --strict`.

### Installation

**Not yet published.** `@getexperimently/openfeature-provider` is not on npm yet, so this fails
today (`@getexperimently/js-sdk` and `@openfeature/server-sdk` are on npm):

```bash
npm install @openfeature/server-sdk @getexperimently/js-sdk @getexperimently/openfeature-provider
```

Build both from a clone of this repository instead (the provider's build also builds `sdk/js`):

```bash
git clone https://github.com/getexperimently/experimently.git
npm --prefix experimently/sdk/js ci
npm --prefix experimently/sdk/openfeature ci
npm --prefix experimently/sdk/openfeature run build
```

`@openfeature/server-sdk` is a **peer dependency** of the provider (`^1.7.0`), so your app installs
it. Installed as a package (from npm, or an `npm pack` tarball of `sdk/openfeature`), the provider
resolves your app's copy rather than bringing its own. A directory install, as below, is a symlink,
so the provider resolves the copy in `sdk/openfeature/node_modules` instead.

Then, in your app, install the two directories (`@openfeature/server-sdk` itself is on npm):

```bash
npm install @openfeature/server-sdk /path/to/experimently/sdk/js /path/to/experimently/sdk/openfeature
```

### Quick start

```typescript
import { OpenFeature } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '@getexperimently/openfeature-provider';

// 1. Register the provider (once, at app start-up).
const provider = new ExperimentationProvider({
  apiKey: process.env.EXPERIMENTLY_API_KEY!,
  baseUrl: process.env.EXPERIMENTLY_API_URL ?? 'http://localhost:8000', // origin only
  cacheTtlMs: 5 * 60_000, // reuse a successful evaluation per user + flag for 5 minutes
});
await OpenFeature.setProviderAndWait(provider);

// 2. Get a client (reuse it throughout your application).
const client = OpenFeature.getClient('my-service');

// 3. Evaluate flags. targetingKey is the platform user_id and is required.
const ctx = { targetingKey: 'user-12345' };
const enabled = await client.getBooleanValue('dark-mode', false, ctx);              // → enabled
// Only boolean flags carry a value: string, number and object calls return your default.

// 4. Experiments and event tracking are outside OpenFeature: use the JS SDK client underneath.
const assignment = await provider.client.getAssignment('checkout_flow', { userId: 'user-12345', attributes: { plan: 'pro' } });
await provider.client.track('user-12345', 'purchase', { value: 49.99, experimentKey: 'checkout_flow' });
await provider.client.track('user-12345', 'page_view'); // no key → fans out to the cached assignment + flags evaluated via OpenFeature
```

### EvaluationContext mapping

| OpenFeature field | Platform concept | Notes |
|---|---|---|
| `targetingKey` | `user_id` | Required. Sent as `?user_id=` on the evaluate call; the server buckets on it. Missing or empty → default value with `errorCode: TARGETING_KEY_MISSING`. |
| `attributes.*` | — | **Not sent.** The evaluate endpoint takes a `context`, but this provider does not send it yet; attributes stay available to your own hooks. Pass them as `attributes` on `provider.client.getAssignment(...)` if an experiment needs them. |

### Resolution rules

| OpenFeature call | Value |
|---|---|
| `getBooleanValue` / `getBooleanDetails` | `enabled` |
| `getStringValue`, `getNumberValue`, `getObjectValue` and their `…Details` | the caller's default, reason `DEFAULT` |

Only boolean flags carry a value: the evaluate endpoint returns `config: null` for every flag, so
a string, number or object call on a flag that is on returns the caller's default.

- A disabled flag resolves boolean calls to `false` with reason `DISABLED`, and non-boolean calls
  to the caller's default with reason `DISABLED`.
- `variant` on the resolution details is not set, because `config` is always `null`; `flagMetadata` is
  `{ flagKey, enabled }` on every successful resolution.
- Errors never throw and are never cached: 404 (an unknown flag key) → `FLAG_NOT_FOUND`;
  a 2xx with an unexpected body → `PARSE_ERROR`; anything else (401, 5xx, timeout, network) →
  `GENERAL`. All return the default with reason `ERROR` and an `errorMessage`.

### Resolution reasons

| Reason | Meaning |
|---|---|
| `TARGETING_MATCH` | Fresh answer from the server; the flag is on for this user. |
| `CACHED` | Served from the JS SDK's per-user cache (within `cacheTtlMs`). |
| `DISABLED` | The server reports the flag off for this user. |
| `DEFAULT` | A string, number or object call on a flag that is on: `config` is always `null`, so the caller's default was returned. |
| `ERROR` | Evaluation failed (see `errorCode`); the caller's default was returned. |

### Constructor options

```typescript
new ExperimentationProvider({
  apiKey: string,               // Required unless `client` is given. X-API-Key header value.
  baseUrl?: string,             // Backend origin; the SDK appends /api/v1/... Default: 'http://localhost:8000'
  cacheTtlMs?: number,          // Default: 300_000 (5 minutes)
  timeout?: number,             // Per-request timeout in ms. Default: 5_000
  fetch?: typeof fetch,         // Custom fetch implementation (tests, polyfills)
  client?: ExperimentationClient, // Reuse your app's JS SDK client (and its cache); other options are then ignored
})
```

### Provider API

| Member | Signature | Description |
|---|---|---|
| `metadata` | `{ name: 'experimently-provider' }` | OpenFeature provider metadata |
| `client` | `ExperimentationClient` | The underlying JS SDK client (`getAssignment`, `getVariant`, `track`, `trackBatch`, `getAssignments`, …) |
| `initialize` | `(context?) => Promise<void>` | No-op: nothing is pre-fetched, flags are evaluated per user on demand |
| `onClose` | `() => Promise<void>` | Clears the evaluation cache |
| `resolveBooleanEvaluation` / `resolveStringEvaluation` / `resolveNumberEvaluation` / `resolveObjectEvaluation` | `(flagKey, defaultValue, context) => Promise<ResolutionDetails<T>>` | The rules above |
| `refreshFlags` | `() => Promise<void>` | **Deprecated**: there is no list endpoint to refresh from; clears the cache so the next resolution hits the server |
| `hashUser` | `(userId, flagKey) => number` | Cross-SDK consistent hash, compatibility utility only (see below) |

The package also re-exports `ExperimentationClient`, `ExperimentationError` and the
`FlagEvaluation`, `Assignment`, `UserContext`, `TrackOptions` types from the JS SDK.

### Backend endpoints used (TypeScript)

Every request carries `X-API-Key: <key>`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | Response used |
|---|---|---|
| every `resolve*Evaluation` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `provider.client.getAssignment` / `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when not ACTIVE |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.client.track` without a key, `trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (max 100 per request) | `{success_count, failure_count, errors}` |

### Contract smoke (TypeScript)

```bash
cd sdk/openfeature && npm run build --silent && node examples/contract_smoke.mjs
```

It prints one line:

```json
{"sdk":"openfeature","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The flag is resolved
through `OpenFeature.getClient().getBooleanDetails`; assignment and tracking go through
`provider.client`. `npm run smoke` is a shortcut.

### Tests (TypeScript)

`npx jest` runs the tests, with `fetch` mocked; it builds `../js` first. `npm run build` runs
`tsc` into `dist/`:

```bash
cd sdk/openfeature && npm install
npx jest
npm run build
```

---

## Python Provider

`experimently-openfeature` (v0.1.0 beta, source `sdk/openfeature-python`) delegates every
evaluation to the [`experimentation` Python SDK](python.md), which calls
`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targeting_key>` and caches the answer per
user + flag. Flags are decided **by the server**; nothing is bucketed locally and no flag
definitions are downloaded. Requires Python 3.9+ and `openfeature-sdk >= 0.9.0`.

### Installation

**Not yet published.** `experimently-openfeature` is not on PyPI yet, so this fails today
(`experimently` and `openfeature-sdk` are on PyPI):

```bash
pip install openfeature-sdk experimently experimently-openfeature
```

Install both from the root of a clone of this repository instead (`openfeature-sdk` itself is on
PyPI and is installed as a dependency):

```bash
pip install -e sdk/python -e sdk/openfeature-python
```

### Quick start

```python
import os
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from experimentation_openfeature import ExperimentationProvider

# 1. Register the provider (once, at startup).
provider = ExperimentationProvider(
    api_key=os.environ["EXPERIMENTLY_API_KEY"],
    base_url=os.environ.get("EXPERIMENTLY_API_URL", "http://localhost:8000"),  # origin only
    cache_ttl=300,   # seconds a successful evaluation is reused per user + flag
    timeout=10,      # seconds
)
api.set_provider(provider)

# 2. Get a client.
client = api.get_client()

# 3. Evaluate flags. targeting_key is the platform user_id and is required.
ctx = EvaluationContext(targeting_key="user-12345")
enabled = client.get_boolean_value("dark-mode", False, ctx)                 # -> enabled
# Only boolean flags carry a value: string, number and object calls return your default.

# 4. Experiments and event tracking are outside OpenFeature: use the SDK client underneath.
assignment = provider.client.get_assignment("checkout_flow", "user-12345", {"plan": "pro"})
provider.client.track("user-12345", "purchase", event_value=49.99, experiment_key="checkout_flow")
```

### EvaluationContext mapping

| OpenFeature field | Platform concept | Notes |
|---|---|---|
| `targeting_key` | `user_id` | Required. Sent as `?user_id=` on the evaluate call; the server buckets on it. Missing or empty → default value with `TARGETING_KEY_MISSING`. |
| `attributes.*` | — | **Not sent.** The evaluate endpoint takes a `context`, but this provider does not send it yet; attributes stay available to your own hooks. |

### Resolution rules

| OpenFeature call | Value |
|---|---|
| `get_boolean_*` | `enabled` |
| `get_string_*`, `get_integer_*`, `get_float_*`, `get_object_*` | the caller's default, reason `DEFAULT` |

Only boolean flags carry a value: the evaluate endpoint returns `config: null` for every flag, so
a string, number or object call on a flag that is on returns the caller's default.

- A disabled flag resolves non-boolean calls to the default with reason `DISABLED`.
- `variant` on the resolution details is not set, because `config` is always `null`.
- Errors never raise and are never cached: 404 (an unknown flag key) → `FLAG_NOT_FOUND`,
  anything else (401, 5xx, timeout, network) → `GENERAL`; both return the default with reason
  `ERROR`.
- `client.track(event_name, ctx, TrackingEventDetails(value=…, attributes=…))` forwards to
  `ExperimentationClient.track`, which fans out to the user's cached assignments and flags.

### Resolution reasons

| Reason | Meaning |
|---|---|
| `TARGETING_MATCH` | Fresh answer from the server; the flag is on for this user. |
| `DISABLED` | The server reports the flag off for this user. |
| `CACHED` | Served from the SDK's per-user cache (within `cache_ttl`). |
| `DEFAULT` | A string, number or object call on a flag that is on: `config` is always `null`, so the caller's default was returned. |
| `ERROR` | Evaluation failed (see `error_code`); the caller's default was returned. |

### Constructor options

```python
ExperimentationProvider(
    api_key: str = "",                        # Required unless client= is given. X-API-Key header value.
    base_url: str = "http://localhost:8000",  # Backend origin; the SDK appends /api/v1/...
    cache_ttl: float = 300,                   # seconds
    timeout: float = 10,                      # seconds
    *,
    client: ExperimentationClient | None = None,  # share one SDK client (and its cache) with your app
    transport: Transport | None = None,           # custom HTTP layer (tests use experimentation.testing.FakeTransport)
)
```

`provider.client` exposes the underlying `experimentation.ExperimentationClient`; `shutdown()`
clears its cache.

### Backend endpoints used (Python)

Every request carries `X-API-Key: <key>`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | Response used |
|---|---|---|
| every `resolve_*_details` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `provider.track` / `provider.client.track` | `POST /api/v1/tracking/track` (with a key) or `POST /api/v1/tracking/batch` (fan-out, max 100 per request) | ignored / `{success_count, failure_count, errors}` |
| `provider.client.get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when not ACTIVE |

### Contract smoke (Python)

```bash
EXPERIMENTLY_API_KEY=<key> python sdk/openfeature-python/examples/contract_smoke.py
```

It prints one line:

```json
{"sdk":"openfeature-python","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The flag is resolved
through the OpenFeature API; assignment and tracking go through `provider.client`.

CI runs this SDK's contract smoke against a real API: `openfeature-python` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).
The live runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk openfeature-python --strict`.

### Tests (Python)

The tests fake HTTP:

```bash
source venv/bin/activate
python -m pytest sdk/openfeature-python/tests -q -o addopts="" -p no:cacheprovider
```

---

## Backend API endpoints

Both providers are thin adapters over the platform SDKs and use the same public endpoints as every
other SDK. There are **no OpenFeature-specific endpoints in the SDK contract**: no flag definitions
are downloaded, and no evaluation happens locally.

- `POST /api/v1/openfeature/evaluate` and `POST /api/v1/openfeature/bulk-evaluate` have been
  **removed**. They could not evaluate an existing flag (they read a `FeatureFlag.enabled`
  attribute the model does not have), and neither provider called them. To evaluate a flag,
  call `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` with your API key, as the
  table below shows.
- `GET /api/v1/openfeature/flags` has been **removed** (#737) and answers 404. Neither provider
  called it.

Base URL = origin only (e.g. `http://localhost:8000`); the SDK appends `/api/v1/...`. Every request
carries `X-API-Key: <key>`, `Content-Type: application/json`, `Accept: application/json`.

| Purpose | Method and path | Body / query | 200 response |
|---|---|---|---|
| Evaluate one flag (every OpenFeature resolution) | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targetingKey>` | — | `{"key","enabled": bool,"config": any\|null}` |
| Assign user to experiment (`provider.client`) | `POST /api/v1/tracking/assign` | `{"experiment_key","user_id","context"?: object}` | `{"experiment_key","user_id","variant_id","variant_name","is_control": bool,"configuration": object\|null}` |
| Track one event (`provider.client.track` with a key) | `POST /api/v1/tracking/track` | `{"event_type","event_name"?,"user_id","experiment_key"?,"feature_flag_key"?,"value"?: number,"metadata"?: object,"timestamp"?: ISO-8601}` — at least one key | stored event (ignored) |
| Track up to 100 events (`track` without a key, `trackBatch`) | `POST /api/v1/tracking/batch` | `{"events":[<track body>...]}` | `{"success_count","failure_count","errors": list\|null}` |

Errors: 401 bad key; 404 an experiment unknown or not ACTIVE, or an unknown flag key (→
`FLAG_NOT_FOUND`, never cached); 422 invalid event; 429 rate limited (`Retry-After` header).

Successful evaluations are cached per user + flag for the configured TTL (`cacheTtlMs` /
`cache_ttl`, default 5 minutes) and reported with reason `CACHED`; failures are never cached.

---

## Hash algorithm (compatibility utility only)

The platform's cross-SDK consistent hash is still shipped by every SDK so that the golden-vector
contract tests (`tests/sdk-contract/golden-vectors.json`) and custom integrations can verify
byte-for-byte parity, but **neither provider uses it to decide a flag or a variant** — the server
does. It is exposed as `provider.hashUser(userId, flagKey)` (TypeScript, delegating to the JS
SDK's `consistentHash`) and by the Python SDK's hashing helper.

```
input  = "{userId}:{flagKey}"  (UTF-8)
digest = MD5(input)
uint32 = first 4 bytes of digest, little-endian unsigned 32-bit int
hash   = uint32 / 2^32         ∈ [0.0, 1.0)      (divisor 4294967296, not 4294967295)
```

**Test vector** (all SDKs produce this value):

```
hash_user("user-123", "my-flag") ≈ 0.6927449859
  MD5("user-123:my-flag") = 43bc57b1e81dec71c5242122ac05170f
  First 4 bytes LE → uint32 = 2975317059
  2975317059 / 4294967296 = 0.692744985921308
```

---

## Migration guide

### Switching from LaunchDarkly to this platform

**Before (LaunchDarkly SDK):**
```typescript
import * as ld from 'launchdarkly-node-server-sdk';
const client = ld.init('sdk-key');
await client.waitForInitialization();
const value = await client.variation('my-flag', user, false);
```

**After (OpenFeature + Experimently provider):**
```typescript
import { OpenFeature } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '@getexperimently/openfeature-provider';
await OpenFeature.setProviderAndWait(new ExperimentationProvider({ apiKey: 'api-key' }));
const client = OpenFeature.getClient();
const value = await client.getBooleanValue('my-flag', false, { targetingKey: userId });
```

The application logic does not change — only the provider initialization at startup.

### Switching from Split.io

**Before:**
```python
from splitio import get_factory
factory = get_factory('api-key')
factory.block_until_ready()
client = factory.client()
treatment = client.get_treatment('user-123', 'my_feature')
```

**After:**
```python
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from experimentation_openfeature import ExperimentationProvider
api.set_provider(ExperimentationProvider(api_key='api-key'))
client = api.get_client()
# A Split treatment becomes a boolean flag: only boolean flags carry a value here.
enabled = client.get_boolean_value('my_feature', False, EvaluationContext('user-123'))
```

---

## Advanced: OpenFeature hooks

OpenFeature hooks let you add cross-cutting concerns (logging, metrics, tracing) that apply
to every flag evaluation, regardless of the provider:

```typescript
import { OpenFeature, Hook, HookContext } from '@openfeature/server-sdk';

const loggingHook: Hook = {
  before(hookContext: HookContext) {
    console.log(`Evaluating flag: ${hookContext.flagKey}`);
  },
  after(hookContext: HookContext, details) {
    console.log(`Flag ${hookContext.flagKey} = ${details.value} (reason: ${details.reason})`);
  },
  error(hookContext: HookContext, error) {
    console.error(`Flag evaluation error for ${hookContext.flagKey}:`, error);
  },
};

OpenFeature.addHooks(loggingHook);
```

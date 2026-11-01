# OpenFeature Provider

## What is OpenFeature?

[OpenFeature](https://openfeature.dev) is a CNCF (Cloud Native Computing Foundation) standard
for feature flag management. It defines a vendor-neutral API so that application code only
talks to a single, stable interface — **without depending on any specific feature flag
vendor's SDK**.

This means you can:
- Switch feature flag providers without changing a line of application code.
- Use the same `client.getBooleanValue("my-flag", false, context)` call whichever provider is
  registered (this one needs a `targetingKey` in the context).
- Add OpenFeature hooks (logging, metrics, tracing) once and have them apply to all providers.

---

## TypeScript / Node.js Provider

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

`@controlvar/openfeature-provider` (v0.1.0 beta, source `sdk/openfeature`) implements the
`@openfeature/server-sdk` `Provider` interface by delegating every evaluation to the
[JavaScript SDK](javascript.md) (`@controlvar/js-sdk`), which calls
`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targetingKey>` and caches the answer per
user + flag. Flags are decided **by the server**; nothing is bucketed locally and no flag
definitions are downloaded. Requires Node >= 18 and `@openfeature/server-sdk >= 1.7`.

CI runs this SDK's contract smoke against a real API: `openfeature` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).
The live runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk openfeature --strict`.

### Installation

**Not yet published.** `@controlvar/openfeature-provider` is not on npm, so build it from
source: build both from a clone of this repository (the provider's build also builds `sdk/js`):

```bash
git clone https://github.com/Controlvar/controlvar.git
npm --prefix controlvar/sdk/js ci
npm --prefix controlvar/sdk/openfeature ci
npm --prefix controlvar/sdk/openfeature run build
```

`@openfeature/server-sdk` is a **peer dependency** of the provider (`^1.7.0`), so your app installs
it. Installed as a package (from npm, or an `npm pack` tarball of `sdk/openfeature`), the provider
resolves your app's copy rather than bringing its own. A directory install, as below, is a symlink,
so the provider resolves the copy in `sdk/openfeature/node_modules` instead.

Then, in your app, install the two directories (`@openfeature/server-sdk` itself is on npm):

```bash
npm install @openfeature/server-sdk /path/to/controlvar/sdk/js /path/to/controlvar/sdk/openfeature
```

### Quick start

```typescript
import { OpenFeature } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '@controlvar/openfeature-provider';

// 1. Register the provider (once, at app start-up).
const provider = new ExperimentationProvider({
  apiKey: process.env.CONTROLVAR_API_KEY!,
  baseUrl: process.env.CONTROLVAR_API_URL ?? 'http://localhost:8000', // origin only
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
await provider.client.track('user-12345', 'page_view'); // no key → fanned out by the JS SDK's rule to the assignment + flags evaluated via OpenFeature
```

### EvaluationContext mapping

| OpenFeature field | Platform concept | Notes |
|---|---|---|
| `targetingKey` | `user_id` | Required. Sent as `?user_id=` on the evaluate call; the server buckets on it. Missing or empty → default value with `errorCode: TARGETING_KEY_MISSING`. |
| `attributes.*` | — | **Not sent.** The evaluate call carries `user_id` alone, so the server evaluates the flag's targeting rules against an empty context; attributes stay available to your own hooks. Pass them as `attributes` on `provider.client.getAssignment(...)` if an experiment needs them. |

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
| `metadata` | `{ name: 'controlvar-provider' }` | OpenFeature provider metadata |
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
| `provider.client.getAssignment` / `getVariant` | `POST /api/v1/tracking/assign` `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.client.track` without a key | `POST /api/v1/tracking/batch` `{events: [...]}` (max 100 per request), one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` with no key | ignored |
| `provider.client.trackBatch` | `POST /api/v1/tracking/batch` `{events: [...]}` (max 100 per request) | `{success_count, failure_count, errors}` |

### Contract smoke (TypeScript)

```bash
cd sdk/openfeature && npm run build --silent && node examples/contract_smoke.mjs
```

It prints one line:

```json
{"sdk":"openfeature","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
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

`controlvar-openfeature` (v0.1.0 beta, source `sdk/openfeature-python`) delegates every
evaluation to the [`controlvar` Python SDK](python.md), which calls
`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targeting_key>&context=<attributes>` and
caches the answer per user + flag. Flags are decided **by the server**; nothing is bucketed locally
and no flag definitions are downloaded, unless you pass a `client=` built with `evaluation="local"`.
Boolean, string, integer, float and object calls are answered. Requires Python 3.9+ and
`openfeature-sdk >= 0.10.0`.

### Installation

**Not yet published.** This package is not on PyPI: the `controlvar-openfeature` name there
holds a 0.0.0 placeholder, which installs but cannot be imported. Build it from source: install
both from the root of a clone of this repository (`openfeature-sdk` is installed as a
dependency):

```bash
pip install -e sdk/python -e sdk/openfeature-python
```

Install the Python SDK from the same clone, as above: the `controlvar` 0.2.0 on PyPI reads no flag
value, so with it every string, integer, float and object call answers your default with `GENERAL`.

### Quick start

```python
import os
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from controlvar_openfeature import ExperimentationProvider

# 1. Register the provider (once, at startup).
provider = ExperimentationProvider(
    api_key=os.environ["CONTROLVAR_API_KEY"],
    base_url=os.environ.get("CONTROLVAR_API_URL", "http://localhost:8000"),  # origin only
    cache_ttl=300,   # seconds a successful evaluation is reused per user + flag
    timeout=10,      # seconds
)
api.set_provider(provider)

# 2. Get a client.
client = api.get_client()

# 3. Evaluate flags. targeting_key is the platform user_id and is required; the attributes are
#    sent to the server as the user's attributes, for the flag's targeting rules.
ctx = EvaluationContext(targeting_key="user-12345", attributes={"plan": "enterprise"})
enabled = client.get_boolean_value("dark-mode", False, ctx)                       # -> enabled
label = client.get_string_value("checkout-button-label", "Buy now", ctx)          # -> the flag's string value
max_items = client.get_integer_value("cart-max-items", 10, ctx)                   # -> the flag's number value
theme = client.get_object_value("checkout-theme", {"color": "blue"}, ctx)         # -> the flag's JSON value

# 4. Experiments and event tracking are outside OpenFeature: use the SDK client underneath.
assignment = provider.client.get_assignment("checkout_flow", "user-12345", {"plan": "pro"})
provider.client.track("user-12345", "purchase", event_value=49.99, experiment_key="checkout_flow")
```

### EvaluationContext mapping

| OpenFeature field | Platform concept | Notes |
|---|---|---|
| `targeting_key` | `user_id` | Required. Sent as `?user_id=` on the evaluate call; the server buckets on it. Missing or empty → default value with `TARGETING_KEY_MISSING`. |
| `attributes` | the user's attributes | Passed to the Python SDK as `user_attributes`, which it sends on the evaluate call as `&context=<url-encoded JSON>`, so the flag's targeting rules are evaluated against them. A `datetime`, at any depth, is sent as an ISO 8601 string (a naive one is taken as UTC). A context with no attributes sends no `context`. By default the SDK caches an answer per user + flag key, and the attributes are not part of that key: within `cache_ttl`, a second call for the same `targeting_key` and flag gets the first answer (reason `CACHED`) whatever its attributes, so a changed attribute is seen once that answer expires. `cache_ttl=0` caches nothing; `provider.shutdown()` or `provider.client.clear_cache()` drops every cached answer. A `client=` built with `evaluation="local"` answers each call it can from its ruleset, using that call's attributes, and such an answer is never `CACHED`. |

### Resolution rules

| OpenFeature call | Reads | Value |
|---|---|---|
| `get_boolean_*` | a boolean flag | `enabled` |
| `get_string_*` | a string flag | the flag's value |
| `get_integer_*` | a number flag | the flag's value when it has no fractional part (`10` or `10.0` answers `10`) |
| `get_float_*` | a number flag | the flag's value as a float (`10` answers `10.0`) |
| `get_object_*` | a json flag | a new copy of the flag's value (an object or an array) |

Every call reads the value the flag serves: its on value when the flag is on for the user, its
off value otherwise; a boolean flag's is `enabled`. There is no coercion: a call of another type
gets the caller's default, a boolean call on a string, number or json flag and an integer call
on a number with a fractional part (`2.5`) included, and `bool` is never a number. A boolean call
reads `enabled`, so an answer with no `value_type` (a server or an SDK without flag values, where
every flag is boolean) answers it rather than the caller's default.

| The server's answer | Value | Reason | Variant |
|---|---|---|---|
| `reason: "targeting_rule"`, on / off | the on value / the off value | `TARGETING_MATCH` | `"on"` / `"off"` |
| `reason: "rollout"`, on / off | the on value / the off value | `SPLIT` | `"on"` / `"off"` |
| `reason: "inactive"` | the off value, not the caller's default | `DISABLED` | `"off"` |
| answered from the SDK's cache | as cached | `CACHED` | as cached |
| `reason: "error"` | the caller's default | `ERROR`, `error_code` `GENERAL` | not set |
| the flag serves another type than the call | the caller's default | `ERROR`, `error_code` `TYPE_MISMATCH` | not set |
| no `value` (a string, integer, float or object call; a server or an SDK without flag values) | the caller's default | `ERROR`, `error_code` `GENERAL` | not set |
| a `value` that is not of the flag's type | the caller's default | `ERROR`, `error_code` `PARSE_ERROR` | not set |

- A server reason this provider does not know answers the value with reason `UNKNOWN`.
- Errors never raise: 404 (an unknown flag key) → `FLAG_NOT_FOUND`; anything else (401, 422,
  5xx, a 2xx whose body is not an evaluation, timeout, network) → `GENERAL`. Both return the
  default with reason `ERROR`, and a failed
  request is never cached. The answers in the table above are successful requests, which the
  Python SDK caches like any other.
- `provider.client`, the Python SDK client, has typed getters of its own:
  `get_string_value(flag_key, user_id, user_attributes=None, *, default)`, `get_number_value` and
  `get_json_value` take the user second and the default as the keyword `default`, while
  OpenFeature's `client.get_string_value(flag_key, default_value, evaluation_context)` takes the
  default second. `default` is keyword-only, so a call written in OpenFeature's order on
  `provider.client` raises `TypeError`.
- `client.track(event_name, ctx, TrackingEventDetails(value=…, attributes=…))` forwards to
  `ExperimentationClient.track`, which fans an event with no key out by the Python SDK's rule
  (see the Python SDK page).

### Resolution reasons

| Reason | Meaning |
|---|---|
| `TARGETING_MATCH` | A targeting rule decided (`reason: "targeting_rule"`), on or off. |
| `SPLIT` | The rollout percentage decided (`reason: "rollout"`), on or off. |
| `DISABLED` | The flag is not ACTIVE (`reason: "inactive"`), and the value is its off value. |
| `CACHED` | Served from the SDK's per-user cache (within `cache_ttl`); never for an answer a client with local evaluation computed. |
| `UNKNOWN` | A server reason this provider does not know; the value is the flag's. |
| `ERROR` | Evaluation failed (see `error_code`); the caller's default was returned. |

### Constructor options

```python
ExperimentationProvider(
    api_key: str = "",                        # Required unless client= is given. X-API-Key header value.
    base_url: str = "http://localhost:8000",  # Backend origin; the SDK appends /api/v1/...
    cache_ttl: float = 300,                   # seconds; per user + flag, not the attributes; 0 caches nothing
    timeout: float = 10,                      # seconds
    *,
    client: ExperimentationClient | None = None,  # share one SDK client (and its cache) with your app
    transport: Transport | None = None,           # custom HTTP layer (tests use controlvar.testing.FakeTransport)
)
```

`provider.client` exposes the underlying `controlvar.ExperimentationClient`; `shutdown()`
clears its cache.

### Backend endpoints used (Python)

Every request carries `X-API-Key: <key>`, `Content-Type: application/json`, `Accept: application/json`.

| Call | Method and path | Response used |
|---|---|---|
| every `resolve_*_details` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…[&context=…]` | `{key, enabled, value, value_type, config, reason}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `provider.client.track` with a key | `POST /api/v1/tracking/track` | ignored |
| `provider.track`, and `provider.client.track` without a key | `POST /api/v1/tracking/batch` (max 100 per request), one entry per experiment and flag it reaches; with none, `POST /api/v1/tracking/track` with no key | ignored |
| `provider.client.track_batch` | `POST /api/v1/tracking/batch` (max 100 per request) | `{success_count, failure_count, errors}` |
| `provider.client.get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |

### Contract smoke (Python)

```bash
CONTROLVAR_API_KEY=<key> python sdk/openfeature-python/examples/contract_smoke.py
```

It prints one line:

```json
{"sdk":"openfeature-python","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The flag is resolved
through the OpenFeature API; assignment and tracking go through `provider.client`.

CI runs this SDK's contract smoke against a real API: `openfeature-python` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).
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
other SDK. There are **no OpenFeature-specific endpoints in the SDK contract**: by default no flag
definitions are downloaded, and no evaluation happens locally. A `client` you pass with local
evaluation (`evaluation="local"` in Python, `evaluation: 'local'` in TypeScript) downloads the SDK
ruleset (`GET /api/v1/sdk/ruleset`) and answers each flag it can from it.

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
| Evaluate one flag (every OpenFeature resolution) | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=<targetingKey>` | `context=<url-encoded JSON object>`: the Python provider sends the evaluation context's attributes; the TypeScript provider sends none | `{"key","enabled": bool,"value","value_type","config": null,"reason"}` |
| Assign user to experiment (`provider.client`) | `POST /api/v1/tracking/assign` | `{"experiment_key","user_id","context"?: object}` | `{"experiment_key","user_id","variant_id","variant_name","is_control": bool,"configuration": object\|null}` |
| Track one event (`provider.client.track` with a key; a `track` without a key that reaches no experiment or flag) | `POST /api/v1/tracking/track` | `{"event_type","event_name"?,"user_id","experiment_key"?,"feature_flag_key"?,"value"?: number,"metadata"?: object,"timestamp"?: ISO-8601}`; both keys are optional, and an event with neither counts in no experiment's results | stored event (ignored) |
| Fan out an event without a key (`track`: one entry per experiment and flag it reaches) | `POST /api/v1/tracking/batch` | `{"events":[<track body>...]}` (max 100 per request) | `{"success_count","failure_count","errors": list\|null}` (ignored) |
| Track up to 100 events (`trackBatch`, `track_batch`) | `POST /api/v1/tracking/batch` | `{"events":[<track body>...]}` | `{"success_count","failure_count","errors": list\|null}` |

Errors: 401 bad key; 404 an unknown experiment or flag key, or a draft experiment with no control
variant (→ `FLAG_NOT_FOUND`, never cached); 422 invalid event; 429 rate limited (`Retry-After`
header).

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

With OpenFeature and this provider, a boolean flag reads:

```typescript
import { OpenFeature } from '@openfeature/server-sdk';
import { ExperimentationProvider } from '@controlvar/openfeature-provider';
await OpenFeature.setProviderAndWait(new ExperimentationProvider({ apiKey: 'api-key' }));
const client = OpenFeature.getClient();
const value = await client.getBooleanValue('my-flag', false, { targetingKey: userId });
```

For a boolean flag only the provider initialization changes. A string, number or object call
returns its default through this provider, and the context's attributes are not sent (the evaluate
call carries the targeting key alone), so a flag read as a string, number or object, or targeted on
attributes, behaves differently.

### Switching from Split.io

With OpenFeature and this provider:

```python
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from controlvar_openfeature import ExperimentationProvider
api.set_provider(ExperimentationProvider(api_key='api-key'))
client = api.get_client()
ctx = EvaluationContext('user-123', {'plan': 'pro'})
# A split with an on and an off treatment becomes a boolean flag...
enabled = client.get_boolean_value('my_feature', False, ctx)
# ...or a string flag whose on and off values are the two treatments.
treatment = client.get_string_value('checkout_copy', 'control', ctx)
```

A flag serves two values, its on value and its off value, so a split with three or more
treatments has no single equivalent: run a multivariate experiment for that (see
[Limitations](../getting-started/limitations.md)).

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

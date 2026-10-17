# SDK Integration Guide

SDKs integrate experiment assignment, feature-flag evaluation and event tracking into your application.
Every SDK talks to the same small public API surface, authenticated with an API key.

## Endpoint contract

All SDK traffic uses the `X-API-Key` header and addresses experiments and flags by their public **keys**.
The server decides bucketing. The one exception is opt-in and server-side only: the JavaScript and
Python SDKs' [local evaluation](sdk/local-evaluation.md) (beta) answers flags from the server's ruleset
(`GET /api/v1/sdk/ruleset`, which needs a key with the `sdk:ruleset` scope) with the server's own
bucketing (`md5-mod100-v1`), and asks the server whenever it cannot give exactly the server's answer.
No other SDK buckets locally, and experiments are always assigned by the server.

| Purpose | Method and path | Body / query | Response |
|---|---|---|---|
| Assign a user to an experiment (sticky) | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration, assigned, reason}` |
| Evaluate a flag | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=<url-encoded JSON, optional>` | — | `{key, enabled, config, reason}` |
| All flags for a user | `GET /api/v1/feature-flags/user/{user_id}` | — | `{flag_key: boolean, …}` |
| Track one event | `POST /api/v1/tracking/track` | `{event_type, event_name?, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | stored event |
| Track up to 100 events | `POST /api/v1/tracking/batch` | `{events: [...]}` | `{success_count, failure_count, errors}` |
| A user's assignments | `GET /api/v1/tracking/assignments/{user_id}` | — | list |

Each entry of the batch response's `errors` is `{index, event_type, user_id, error}`. `error` says
why the item was refused (for example, a key that was given was not found), or reads
`Could not store this event (request ID: <id>).` when the server failed to store it; quote that ID
when you report the failure.

An assign call for a user who is not eligible (global holdout, mutual exclusion group or targeting
rules) still answers 200 with the control variant, `assigned: false` and `reason` set to `holdout`,
`mutual_exclusion` or `targeting`; nothing is recorded for that user. Otherwise `assigned` is `true`,
`reason` is `assigned`, and every call, sticky ones included, records that the user saw the experiment.

Conversions are matched to experiment metrics by `event_name` (exposures excluded), whatever `event_type`
an SDK sends. Full request/response examples: [Tracking Endpoints](api/endpoints.md#tracking-endpoints). Per-IP rate
limit for these paths: `SDK_RATE_LIMIT_PER_MINUTE` (default 6000/min).

**Targeting context.** The user's attributes (`user.attributes` / `user_attributes`) are sent as `context`:
in the `/tracking/assign` body, and on flag evaluation as `context=<url-encoded JSON>` (omitted when empty).
A flag's targeting rules — as written in the dashboard rule editor — evaluate against them; the response's
`reason` (`targeting_rule` / `rollout` / `inactive` / `error`) says which path decided. Attribute lookup is
aliased: a top-level `country` also matches a rule on `user.country` (likewise `device.` / `app.` prefixes),
and nested objects flatten to dotted keys (`{"app": {"version": "3.2.1"}}` answers `app.version`). The
JavaScript, Python and React SDKs send it on both calls; each SDK's page says what that SDK sends.

## SDK status

Every SDK uses the contract above: the server decides assignment and flag
evaluation, results are cached per user + key, and `track` fans out to the user's cached assignments and
flags when no key is given. The cached assignments include answers with `"assigned": false` (a user the
global holdout kept out, for example): holdout results count those users' events like everyone else's. The MD5 consistent hash remains exported by each SDK as a compatibility utility
(the golden-vector tests still cover it). It is not the function the server buckets flag rollouts with,
and nothing uses it to decide a flag or a variant; local evaluation (JavaScript and Python, opt-in) uses
the server's `md5-mod100-v1` bucketing instead.

"Verified live" means the SDK's `contract_smoke` entry point passed
`tests/sdk-contract/live/run_live_contract.py` against a running backend (see
[tests/sdk-contract/README.md](https://github.com/Controlvar/controlvar/blob/main/tests/sdk-contract/README.md)); the `SDK Live Contract` CI job repeats
this on every pull request, with `--strict`, for python, openfeature-python, js, openfeature, edge,
react, react-native, go, java, android, ruby, php and dotnet (the `sdks:` list of the job in
[pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml)).
Each SDK's own unit tests run in
[sdk-unit-tests.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/sdk-unit-tests.yml):
the Linux SDKs on pull requests that touch them (js, python and react on every one), and elixir, flutter, android and ios nightly and
when touched.

| SDK | Location | Unit test runner | Verified live | Docs |
|---|---|---|---|---|
| React | `sdk/react` | jest | yes | [react.md](sdk/react.md) |
| JavaScript / TypeScript | `sdk/js` | jest | yes, including local evaluation | [javascript.md](sdk/javascript.md), [local-evaluation.md](sdk/local-evaluation.md) |
| OpenFeature (JS) | `sdk/openfeature` | jest | yes | [openfeature.md](sdk/openfeature.md) |
| Edge (Cloudflare Workers) | `sdk/edge` | jest | yes | [edge.md](sdk/edge.md) |
| React Native | `sdk/react-native` | jest | yes, in CI — the smoke runs under jest in Node (no device runtime) | [react-native.md](sdk/react-native.md) |
| Python | `sdk/python` | pytest | yes, including local evaluation | [python.md](sdk/python.md), [local-evaluation.md](sdk/local-evaluation.md) |
| OpenFeature (Python) | `sdk/openfeature-python` | pytest | yes | [openfeature.md](sdk/openfeature.md) |
| Go | `sdk/go` | `go test` | yes | [go.md](sdk/go.md) |
| Java + Spring Boot starter | `sdk/java` | JUnit 5 | yes | [java.md](sdk/java.md) |
| iOS (Swift) | `sdk/ios` | XCTest | not yet (unit tests run in the nightly tier) | [ios.md](sdk/ios.md) |
| Ruby | `sdk/ruby` | RSpec | yes | [ruby.md](sdk/ruby.md) |
| PHP | `sdk/php` | PHPUnit | yes | [php.md](sdk/php.md) |
| .NET | `sdk/dotnet` | xUnit | yes | [dotnet.md](sdk/dotnet.md) |
| Android (Kotlin) | `sdk/android` | JUnit 5 | yes, in CI — the smoke runs the JVM build of the Kotlin sources (no emulator); the Android library tests run in the nightly tier | [android.md](sdk/android.md) |
| Flutter / Dart | `sdk/flutter` | `flutter test` | not yet (unit tests run in the nightly tier) | [flutter.md](sdk/flutter.md) |
| Elixir | `sdk/elixir` | ExUnit | not yet (unit tests run in the nightly tier) | [elixir.md](sdk/elixir.md) |

Rows marked "not yet" have a `contract_smoke` entry point that no CI job runs against a live backend;
their unit tests run in the nightly tier and on pull requests that touch them. Run `run_live_contract.py --sdk <name> --strict` on a
machine with the toolchain before relying on them.

---

## Python SDK

`sdk/python` — package `experimently`, stdlib only (no runtime dependencies), Python ≥ 3.9,
synchronous. Full reference: [Python SDK](sdk/python.md).

### Installation

The `experimently` package is on [PyPI](https://pypi.org/project/experimently/) (0.1.0, beta):

```bash
pip install experimently==0.1.0
```

To work on the SDK itself, install it from a clone of this repository instead:

```bash
pip install -e ./sdk/python
```

### Quick Start

```python
from experimentation import ExperimentationClient

client = ExperimentationClient(
    api_url="http://localhost:8000",   # origin only; the SDK appends /api/v1/...
    api_key="eptk_...",
)

# Sticky experiment assignment (POST /api/v1/tracking/assign)
assignment = client.get_assignment(
    "checkout_button_color",
    user_id="user-123",
    user_attributes={"country": "US", "plan": "pro"},   # sent as `context`
)
assignment.variant_name     # "control" or "treatment"
assignment.is_control       # bool
assignment.configuration    # the variant's configuration dict, or None

# Or just the variant name, "control" (default_variant) when the API is unreachable
variant = client.get_variant("checkout_button_color", user_id="user-123")

# Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=...)
flag = client.get_feature_flag("dark_mode", user_id="user-123")
flag.enabled, flag.config
if client.is_feature_enabled("dark_mode", user_id="user-123"):
    ...

# Track a conversion for a specific experiment (POST /api/v1/tracking/track)
client.track("user-123", "purchase", event_value=49.99,
             properties={"payment_method": "card"},
             experiment_key="checkout_button_color")

# Track without a key: fans out to every cached assignment and flag for the user
client.track("user-123", "page_view", properties={"page": "/checkout"})
```

### Client Configuration

```python
client = ExperimentationClient(
    api_url="http://localhost:8000",
    api_key="eptk_...",
    timeout_seconds=5.0,        # per request
    cache_ttl_seconds=300,      # assignments and flag evaluations are cached per user + key
    default_variant="control",  # returned by get_variant() on failure
)
```

### API

| Method | Returns | On failure |
|---|---|---|
| `get_assignment(experiment_key, user_id, user_attributes=None)` | `Assignment(experiment_key, user_id, variant_id, variant_name, is_control, configuration)` | raises `ExperimentationError(status, body)` (404 when the experiment is not ACTIVE) |
| `get_variant(experiment_key, user_id, user_attributes=None)` | `str` | `default_variant` |
| `get_feature_flag(flag_key, user_id, user_attributes=None)` | `FlagEvaluation(key, enabled, config, reason)` | raises `ExperimentationError` |
| `is_feature_enabled(flag_key, user_id, user_attributes=None)` | `bool` | `False` |
| `get_all_flags(user_id, user_attributes=None)` | `dict[str, bool]` | raises `ExperimentationError` |
| `track(user_id, event_name, event_value=None, properties=None, experiment_key=None, feature_flag_key=None, event_type=None, timestamp=None)` | `bool` | `False`, never raises |
| `track_batch(events)` | `BatchResult(success_count, failure_count, errors)` | never raises; chunked at 100 |
| `get_assignments(user_id)` | `list[dict]` from `/tracking/assignments/{user_id}` | raises `ExperimentationError` |
| `cached_assignments(user_id)`, `cached_flags(user_id)`, `clear_cache()` | local cache access | — |
| `consistent_hash(user_id, flag_key)`, `md5_hex(user_id, flag_key)` | compatibility hash utilities | — |

Successful results are cached per user + key for `cache_ttl_seconds`; failures are never cached. A 429
is retried once after the server's `Retry-After` (capped at 5 s). The client is thread-safe.

Testing: construct the client with `transport=` (see `experimentation.testing.FakeTransport`) to assert
requests without a network.

Smoke against a live backend: `python sdk/python/examples/contract_smoke.py`.

---

## JavaScript / TypeScript SDK

`sdk/js` — package `@getexperimently/js-sdk`, zero runtime dependencies, uses the global
`fetch` (Node ≥ 18 and browsers), CommonJS build with type declarations. Full reference:
[JavaScript SDK](sdk/javascript.md). For React apps use the [React SDK](sdk/react.md) instead.

### Installation

The `@getexperimently/js-sdk` package is on [npm](https://www.npmjs.com/package/@getexperimently/js-sdk) (0.1.0, beta):

```bash
npm install @getexperimently/js-sdk@0.1.0
```

To work on the SDK itself, build it from a clone of this repository instead. `npm pack` writes
`getexperimently-js-sdk-0.1.0.tgz`:

```bash
cd sdk/js
npm ci && npm run build && npm pack
```

Then, in your app, install the packed tarball:

```bash
npm install /path/to/experimently/sdk/js/getexperimently-js-sdk-0.1.0.tgz
```

### Quick Start

```typescript
import { ExperimentationClient } from '@getexperimently/js-sdk';

const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',   // origin only; the SDK appends /api/v1/...
  apiKey: 'eptk_...',
});

const user = { userId: 'user-123', attributes: { country: 'US', plan: 'pro' } };

// Sticky experiment assignment (POST /api/v1/tracking/assign)
const assignment = await client.getAssignment('checkout_button_color', user);
assignment.variantName;     // 'control' | 'treatment'
assignment.configuration;   // the variant's configuration, or null

// Or just the name, 'control' (defaultVariant) on failure
const variant = await client.getVariant('checkout_button_color', user);

// Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=...)
const flag = await client.evaluateFlag('dark_mode', user);        // { key, enabled, config }
const isEnabled = await client.isFeatureEnabled('dark_mode', user); // false on failure

// Track for a specific experiment (POST /api/v1/tracking/track)
await client.track('user-123', 'purchase', { value: 49.99, experimentKey: 'checkout_button_color' });

// Track without a key: fans out to every cached assignment and flag for the user
await client.track('user-123', 'page_view', { properties: { page: '/checkout' } });
```

### Client Configuration

```typescript
const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',
  apiKey: 'eptk_...',
  timeoutMs: 5000,          // per request
  cacheTtlMs: 300_000,      // assignments and flag evaluations cached per user + key
  defaultVariant: 'control',
  fetch: customFetch,       // optional (tests, polyfills)
  onError: (err) => log(err),  // optional; called for swallowed failures
});
```

### API

| Method | Returns | On failure |
|---|---|---|
| `getAssignment(experimentKey, user)` | `Promise<Assignment>` (`experimentKey, userId, variantId, variantName, isControl, configuration`) | rejects with `ExperimentationError` (`status`, `code`) |
| `getVariant(experimentKey, user)` | `Promise<string>` | `defaultVariant` |
| `evaluateFlag(flagKey, user)` | `Promise<FlagEvaluation>` (`key, enabled, config, reason?`) — `user.attributes` sent as `context` | rejects with `ExperimentationError` |
| `isFeatureEnabled(flagKey, user)` | `Promise<boolean>` | `false` |
| `getAllFlags(userId, attributes?)` | `Promise<Record<string, boolean>>` | rejects |
| `track(userId, eventName, { value?, properties?, experimentKey?, featureFlagKey?, eventType?, timestamp? })` | `Promise<void>` | never rejects |
| `trackBatch(events)` | `Promise<BatchResult>` | never rejects; chunked at 100 |
| `fetchAssignments(userId)` | server-side list from `/tracking/assignments/{userId}` | rejects |
| `getAssignments(userId)`, `getEvaluatedFlags(userId)`, `clearCache()` | local cache access | — |
| `consistentHash(userId, flagKey)`, `md5Hex(...)` (module exports) | compatibility hash utilities | — |

Concurrent calls for the same user + key share one request; failures are never cached; a 429 is
retried once after `Retry-After`.

Smoke against a live backend: `cd sdk/js && npm run build && node examples/contract_smoke.mjs`.

### React Integration

Use the dedicated React SDK (`sdk/react`). Full reference: [React SDK](sdk/react.md).

```tsx
import {
  ExperimentationProvider,
  useExperiment,
  useFeatureFlag,
  useTrackEvent,
} from '@getexperimently/react-sdk';

function App() {
  return (
    <ExperimentationProvider
      config={{ apiKey: process.env.NEXT_PUBLIC_EXPERIMENTLY_API_KEY!, baseUrl: 'http://localhost:8000' }}
      user={{ userId: currentUser.id, attributes: { plan: currentUser.plan } }}
    >
      <CheckoutButton />
    </ExperimentationProvider>
  );
}

function CheckoutButton() {
  const { variantKey, configuration, loading } = useExperiment('checkout-cta');   // POST /tracking/assign
  const darkMode = useFeatureFlag('dark-mode');                                    // GET /feature-flags/evaluate/dark-mode
  const track = useTrackEvent();

  if (loading) return <DefaultButton />;
  return (
    <button
      style={{ background: (configuration?.color as string) ?? '#0f172a' }}
      onClick={() => track('purchase', { plan: currentUser.plan }, { value: 49 })}  // fans out to every assigned experiment
    >
      {variantKey === 'treatment' ? 'Buy now' : 'Add to cart'}
    </button>
  );
}
```

---

## Java SDK

`sdk/java` — Maven modules `core` (OkHttp + Jackson client, Java 11+) and `spring-boot-starter`
(auto-configuration). Full reference: [Java SDK](sdk/java.md).

### Installation

Install it from a clone of this repository: `mvn install` in `sdk/java` builds both modules and
installs them into your local `~/.m2`, where the dependency below resolves.

```xml
<dependency>
  <groupId>com.getexperimently</groupId>
  <artifactId>experimently-sdk</artifactId>
  <version>1.0.0</version>
</dependency>
<!-- Spring Boot: use experimently-spring-boot-starter instead -->
```

### Quick Start

```java
import com.getexperimently.sdk.ExperimentationClient;
import com.getexperimently.sdk.config.SdkConfig;
import com.getexperimently.sdk.model.*;

SdkConfig config = SdkConfig.builder("eptk_...", "http://localhost:8000")  // apiKey, baseUrl (origin only)
    .timeoutMs(5000)
    .cacheTtlMs(300_000)
    .cacheSize(1000)
    .build();
ExperimentationClient client = new ExperimentationClient(config);

User user = User.builder("user-123")
    .attribute("country", "US")
    .attribute("plan", "pro")          // sent as `context` on assignment
    .build();

// Sticky experiment assignment (POST /api/v1/tracking/assign)
ExperimentAssignment assignment = client.getExperimentAssignment(user, "checkout_button_color");
assignment.getVariantName();       // "control" | "treatment"
assignment.isControl();
assignment.getConfiguration();     // Map<String,Object> or null

// Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=...)
FlagEvaluation flag = client.evaluateFeatureFlag(user, "dark_mode");
flag.isEnabled(); flag.getConfig();
boolean on = client.isFeatureEnabled(user, "dark_mode");   // false on failure

// Track for a specific experiment (POST /api/v1/tracking/track) — asynchronous, never throws
client.trackEvent("user-123", "purchase", Map.of("payment_method", "card"),
                  "checkout_button_color", null, 49.99);

// Track without a key: fans out to every cached assignment and flag for the user
client.trackEvent("user-123", "page_view", Map.of("page", "/checkout"));

// Full control
client.trackEvent(TrackEvent.builder("user-123", "purchase")
    .experimentKey("checkout_button_color").value(49.99).property("currency", "USD").build());
client.trackBatch(List.of(...));     // chunked at 100; trackEventSync / trackBatchSync block
```

### API

| Method | Returns | On failure |
|---|---|---|
| `getExperimentAssignment(user, experimentKey)` | `ExperimentAssignment` (`getExperimentKey, getUserId, getVariantId, getVariantName, isControl, getConfiguration`) | throws `ExperimentationException` (unchecked; 404 when not ACTIVE, network errors) |
| `evaluateFeatureFlag(user, flagKey)` | `FlagEvaluation` (`getKey, isEnabled, getConfig, getConfigMap`) | throws `ExperimentationException` |
| `isFeatureEnabled(user, flagKey)` | `boolean` | `false` |
| `trackEvent(userId, eventName, properties[, experimentKey, featureFlagKey, value])`, `trackEvent(TrackEvent)` | `void` (async) | swallowed |
| `trackBatch(List<TrackEvent>)`, `trackEventSync`, `trackBatchSync` | `void` | swallowed |
| `getCachedAssignments(userId)`, `getCachedFlagKeys(userId)`, `invalidateCache(userId, key)`, `clearCache()`, `getCacheSize()`, `close()` | cache / lifecycle | — |
| `ConsistentHash.hash(userId, flagKey)` | compatibility hash utility | — |

### Spring Boot starter

```yaml
experimentation:
  api-key: eptk_...
  base-url: http://localhost:8000
  cache-ttl-seconds: 300
  cache-size: 1000
  timeout-ms: 5000
```

The starter registers an `ExperimentationClient` bean (`@ConditionalOnMissingBean`) that you can inject
anywhere. Tests: `cd sdk/java && mvn test`. Smoke against a live backend:
`bash sdk/java/examples/contract_smoke.sh`.

---

## React SDK

The React SDK's full reference — provider, hooks, HOC, SSR client, types and the exact backend calls — lives
in [docs/sdk/react.md](sdk/react.md); `sdk/react/README.md` is the package README.

Highlights:

- `ExperimentationProvider` takes `config={{ apiKey, baseUrl }}` and `user={{ userId, attributes }}`.
- `useExperiment(key)` → `{ variantKey, variantName, variantId, isControl, configuration, loading, error }`
  via `POST /api/v1/tracking/assign` (sticky on the server).
- `useFeatureFlag(key)` → `{ isEnabled, variant, config, loading, error }` via
  `GET /api/v1/feature-flags/evaluate/{key}?user_id=`; `useVariant` and `useMultipleFlags` build on it.
- `useTrackEvent()` → `track(eventName, properties?, { experimentKey?, featureFlagKey?, value? })`; without a
  key the event fans out to every experiment the user is assigned to.
- `ServerClient`, exported from the package root, offers the same calls for Node/SSR and never throws.

## API Key Authentication

The SDK uses API key authentication, separate from user JWT tokens. API keys are intended for server-side and client-side SDK use.

**Obtaining an API key**: any signed-in user can create one for themselves with `POST /api/v1/api-keys`;
superusers can also use **Admin → API Keys** in the dashboard.

The API key is passed as `X-API-Key: your-api-key` in all SDK requests.

---

## Error Handling

Both SDKs handle API failures gracefully:

```python
# Python — falls back to default_variant, never raises on assignment
variant = client.get_variant("my-experiment", user_id="user-123")
# Returns "control" (default_variant) if API is unreachable
```

```typescript
// JavaScript — falls back to defaultVariant, never rejects on assignment
const variant = await client.getVariant('my-experiment', { userId: 'user-123' });
// Returns 'control' (defaultVariant) if API is unreachable
```

Tracking never throws: in Python `track` returns `False` on a failure, and in JavaScript `track` never
rejects and reports the failure to `onError`.

When the server fails to store an assignment, an event or an error report, it answers 500 with a
short sentence ending in a request ID, such as `Could not store the event (request ID: <id>).` The
same ID is on the response's `X-Request-ID` header and on the API's log line for the failure.

---

## Local Development

Point the SDK at your local instance during development:

```python
client = ExperimentationClient(
    api_url="http://localhost:8000",
    api_key="dev-api-key",
)
```

```typescript
const client = new ExperimentationClient({
  apiUrl: 'http://localhost:8000',
  apiKey: 'dev-api-key',
});
```

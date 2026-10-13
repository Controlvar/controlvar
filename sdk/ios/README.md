# Experimently iOS / Swift SDK

`Experimently` is a Swift Package (iOS 14+, macOS 11+, Swift 5.5+) for feature flags,
experiment assignment and event tracking with `async`/`await`. Foundation only (URLSession,
NSCache, UserDefaults, CommonCrypto).

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed on device.

> **Beta: flag targeting on user attributes does not work in this SDK yet.** Flag
> evaluation sends only the user id (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`),
> not the user's attributes, so a flag targeting rule on an attribute (app version, OS,
> country, plan) never matches here, and the flag's rollout percentage alone decides. Experiment
> assignment is not affected: `getAssignment`, with `user.attributes`, sends the attributes as `context`, so experiment
> targeting works. Until this is fixed ([#438](https://github.com/getexperimently/experimently/issues/438)), an app that needs attribute targeting on a
> flag can call the evaluate endpoint itself with a `context` query parameter (a URL-encoded
> JSON object of the attributes).

Full reference: [`docs/sdk/ios.md`](../../docs/sdk/ios.md).

## Installation

The package manifest lives in `sdk/ios` (not the repository root), so add it as a local package:

```swift
// Package.swift
dependencies: [
    .package(name: "Experimently", path: "../experimently/sdk/ios")
],
targets: [
    .target(name: "MyApp", dependencies: [.product(name: "Experimently", package: "Experimently")])
]
```

In Xcode: File → Add Package Dependencies… → Add Local… → select `sdk/ios` → product
`Experimently`.

## Quick Start

```swift
import Experimently

let client = ExperimentationClient(
    config: SdkConfig(baseURL: "http://localhost:8000",   // origin only; the SDK appends /api/v1/...
                      apiKey: "<EXPERIMENTLY_API_KEY>")   // sent as X-API-Key
)
let user = User(id: "user-123", attributes: ["plan": "pro", "country": "US"])

// Experiment assignment — POST /api/v1/tracking/assign (sticky on the server)
do {
    let a = try await client.getAssignment("checkout_flow", user: user)
    print(a.variantName, a.isControl, a.configuration ?? [:])   // "treatment" false ["headline": ...]
} catch ExperimentationError.experimentNotFound {
    // experiment unknown or not ACTIVE (404): show the control experience
} catch {
    // network / server error with nothing cached
}

// Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123
let enabled = (try? await client.evaluateFlag("new_search", user: user))?.enabled ?? false

// Track with a key — one POST /api/v1/tracking/track (never throws)
try? await client.track(TrackEvent(userId: user.id, eventName: "purchase",
                                   properties: ["currency": "USD"],
                                   experimentKey: "checkout_flow", value: 49.99))

// Track without a key — fanned out to every cached assignment + flag of the user
let delivered = await client.trackWithStatus(TrackEvent(userId: user.id, eventName: "page_view"))
```

## Configuration

`SdkConfig(baseURL:apiKey:timeout:cacheSize:cacheTTL:enableOfflineFallback:)`; the convenience
`ExperimentationClient(baseURL:apiKey:)` uses the defaults.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `baseURL` | `String` | `"http://localhost:8000"` | Backend origin; trailing `/` tolerated |
| `apiKey` | `String` | — (required) | Sent as `X-API-Key` |
| `timeout` | `TimeInterval` | `10` | Per-request timeout (seconds) |
| `cacheSize` | `Int` | `1000` | NSCache `countLimit` for each in-memory cache (flags, assignments) |
| `cacheTTL` | `TimeInterval` | `300` | Lifetime of a cached evaluation/assignment (seconds) |
| `enableOfflineFallback` | `Bool` | `true` | Also persist successful results to `UserDefaults` (`ep_sdk_` prefix) and serve them when a request fails |
| `session` / `offlineStore` (client init) | `URLSession` / `OfflineStore?` | `.shared` / `UserDefaults.standard` | Injection points for tests and App Groups |
| `enableLocalEval` (deprecated init) | — | — | Ignored; the server evaluates everything |

## API

| Method | Returns | On failure |
|---|---|---|
| `evaluateFlag(_ flagKey: String, user: User) async throws` | `EvalResult` | throws (see below) |
| `getAssignment(_ experimentKey: String, user: User) async throws` | `Assignment` | throws (see below) |
| `track(_ event: TrackEvent) async throws` | `Void` | **never throws** (`throws` kept for source compatibility) |
| `trackWithStatus(_ event: TrackEvent) async` | `Bool` | `false` when a request failed |
| `trackBatch(_ events: [TrackEvent]) async` | `Bool` | `false` when a chunk failed; 100 events per request |
| `getAssignments(for userId: String)` / `getEvaluatedFlags(for userId: String)` | `[Assignment]` / `[String]` | cached, unexpired entries only |
| `clearCache()` / `clearOfflineCache()` / `close()` | `Void` | memory caches / UserDefaults / caches + in-flight tasks |
| `refreshFlags() async throws` | `Void` | deprecated no-op |
| `FeatureFlagEvaluator.hashUser(_:flagKey:)` | `Double` in `[0, 1)` | pure (parity utility) |

Types: `User(id:attributes:)` (attributes → assignment `context`); `EvalResult` → `key: String`,
`enabled: Bool`, `config: [String: Any]?`; `Assignment` → `experimentKey`, `userId`,
`variantId: String?`, `variantName`, `isControl: Bool`, `configuration: [String: Any]?`;
`TrackEvent(userId:eventName:properties:experimentKey:featureFlagKey:value:eventType:timestamp:)`
(`properties` → `metadata`, `eventType` defaults to `eventName`, `timestamp` → ISO-8601).

## Caching and failure behaviour

- Successful results are cached in memory per **user + key** for `cacheTTL`; a hit makes no
  request, and concurrent calls for the same user + key share one in-flight request.
  **Failures are never cached.**
- **404** (an experiment unknown or not ACTIVE, or an unknown flag key) throws `.flagNotFound(key)` /
  `.experimentNotFound(key)` and never falls back to a persisted value (it is removed).
- **Any other failure** (network, timeout, 401, 5xx, decoding): with `enableOfflineFallback` the
  last persisted value for that user + key is returned if one exists; otherwise the call throws
  `.networkError(Error)`, `.serverError(Int, String)`, `.decodingError(Error)` or `.cancelled`.
  Wrap calls with `try?` to degrade to "off" / control.
- `track`, `trackWithStatus` and `trackBatch` never throw.

## Tracking fan-out

With `experimentKey` and/or `featureFlagKey` set, `track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` containing one entry per experiment the
user was assigned to through this client (`experiment_key`) plus one per flag evaluated for the
user (`feature_flag_key`), taken from the in-memory cache. Nothing cached → nothing is sent (and
`true` is reported). Metrics match events by **event name**.

## Consistent hash (compatibility utility)

`FeatureFlagEvaluator.hashUser(userId, flagKey:)` = `MD5("{userId}:{flagKey}")`, first 4 bytes as
little-endian UInt32 / 2^32 (`0.6927449859213084` for `user-123`/`my-flag`). Kept only so the
cross-SDK golden vectors in `tests/sdk-contract/` keep passing; **nothing buckets on device**.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `getAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

## Contract smoke

```bash
cd sdk/ios && swift run contract-smoke
```

It prints:

```text
{"sdk":"ios","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Build output goes to stderr (the repo runner passes `-q`). Env: `EXPERIMENTLY_API_URL` (default
`http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required), `CONTRACT_EXPERIMENT_KEY` (default
`sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default `sdk_contract_flag`), `CONTRACT_USER_ID` (default
random `smoke-<uuid>`). The smoke runs with `enableOfflineFallback: false` so it never writes
`UserDefaults`. Repo-wide runner: `python tests/sdk-contract/live/run_live_contract.py --sdk ios --strict`.

CI does not run this SDK's contract smoke against a real API: `ios` is not in the
`sdks:` list of the SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Tests

```bash
cd sdk/ios && swift test
```

- `cd sdk/ios && swift test`: 71 XCTests; URLSession is intercepted with a URLProtocol mock

`Examples/SwiftUIExample/ExampleApp.swift` shows the same calls from a SwiftUI view.

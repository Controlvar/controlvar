# iOS Swift SDK

`Controlvar` is a Swift Package (iOS 14+, macOS 11+, Swift 5.5+) providing feature flag
evaluation, experiment assignment and event tracking with `async`/`await`. It depends only on
Foundation (URLSession, NSCache, UserDefaults) and CommonCrypto.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed on device.

> **Flag evaluation sends only the user id.** `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`
> carries none of the user's attributes, so the server evaluates a flag's targeting rules against
> an empty context. Experiment assignment sends them: `getAssignment`, with `user.attributes`, sends the attributes as
> `context`. To evaluate a flag with attributes, call the evaluate endpoint with a `context`
> query parameter (a URL-encoded JSON object of the attributes).

Source: `sdk/ios`. A SwiftUI demo lives in `sdk/ios/Examples/SwiftUIExample/ExampleApp.swift`.

---

## Installation

**Not yet published.** Build it from source: the manifest lives in `sdk/ios` of a clone of this
repository, so add the SDK as a local package:

```swift
// Package.swift
dependencies: [
    .package(name: "Controlvar", path: "../controlvar/sdk/ios")
],
targets: [
    .target(name: "MyApp", dependencies: [.product(name: "Controlvar", package: "Controlvar")])
]
```

Xcode: File → Add Package Dependencies… → Add Local… → `sdk/ios` → product `Controlvar`.

---

## Quick Start

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

```swift
import Controlvar

let client = ExperimentationClient(config: SdkConfig(
    baseURL: "http://localhost:8000",             // origin only; the SDK appends /api/v1/...
    apiKey: Secrets.controlvarApiKey            // sent as X-API-Key
))
let user = User(id: "user-123", attributes: ["plan": "pro"])   // attributes = assignment context

// 1. Assignment — POST /api/v1/tracking/assign (sticky, records the exposure)
var headline = "Buy now"
do {
    let a = try await client.getAssignment("checkout_flow", user: user)
    headline = a.configuration?["headline"] as? String ?? headline
} catch ExperimentationError.experimentNotFound {
    // unknown or not ACTIVE (404): keep control
} catch {
    // network / server error and nothing cached: keep control
}

// 2. Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123
let flag = try? await client.evaluateFlag("new_search", user: user)
if flag?.enabled == true { /* ... */ }

// 3. Track with a key → one POST /api/v1/tracking/track (never throws)
try? await client.track(TrackEvent(userId: user.id, eventName: "purchase",
                                   properties: ["sku": "pro-plan"],
                                   experimentKey: "checkout_flow", value: 49.99))

// 4. Track without a key → fanned out to every cached assignment + flag of this user
await client.trackWithStatus(TrackEvent(userId: user.id, eventName: "page_view"))
```

---

## Configuration

```swift
let config = SdkConfig(
    baseURL: "http://localhost:8000",
    apiKey: "your-api-key",
    timeout: 10,                  // seconds
    cacheSize: 1000,
    cacheTTL: 300,                // seconds
    enableOfflineFallback: true
)
let client = ExperimentationClient(config: config, session: .shared, offlineStore: nil)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `baseURL` | `String` | `"http://localhost:8000"` | Backend origin; trailing `/` tolerated |
| `apiKey` | `String` | — (required) | Sent as `X-API-Key` on every request |
| `timeout` | `TimeInterval` | `10` | `URLRequest` timeout per call |
| `cacheSize` | `Int` | `1000` | Advisory NSCache `countLimit` for each in-memory cache (flags, assignments) |
| `cacheTTL` | `TimeInterval` | `300` | Lifetime of a cached evaluation/assignment |
| `enableOfflineFallback` | `Bool` | `true` | Persist successful results in `UserDefaults` (prefix `ep_sdk_`) and serve them when a request fails |
| `session` (client init) | `URLSession` | `.shared` | Inject a mock session in tests |
| `offlineStore` (client init) | `OfflineStore?` | `OfflineStore()` | `OfflineStore(suiteName:keyPrefix:)` for App Groups / custom prefix |

`ExperimentationClient(baseURL:apiKey:)` is a convenience initializer with the defaults above. The
`SdkConfig(... enableLocalEval:)` initializer is deprecated and ignores the flag.

---

## API Reference

`ExperimentationClient` conforms to `ExperimentationClientProtocol` (use it for injection/mocking).

| Method | Endpoint | Returns | On failure |
|---|---|---|---|
| `evaluateFlag(_ flagKey: String, user: User) async throws` | `GET /feature-flags/evaluate/{key}?user_id=` | `EvalResult` | throws `ExperimentationError` (see below) |
| `getAssignment(_ experimentKey: String, user: User) async throws` | `POST /tracking/assign` | `Assignment` | throws `ExperimentationError` |
| `track(_ event: TrackEvent) async throws` | `/tracking/track` or `/tracking/batch` | `Void` | **never throws**; `throws` kept for source compatibility |
| `trackWithStatus(_ event: TrackEvent) async` | same | `Bool` | `false` on any failed request |
| `trackBatch(_ events: [TrackEvent]) async` | `/tracking/batch` (chunks of 100) | `Bool` | `false` when any chunk failed |
| `getAssignments(for userId: String)` | — | `[Assignment]` | cached, unexpired, insertion order |
| `getEvaluatedFlags(for userId: String)` | — | `[String]` | cached flag keys |
| `clearCache()` | — | `Void` | drops the in-memory caches (offline store kept) |
| `clearOfflineCache()` | — | `Void` | wipes everything the SDK wrote to `UserDefaults` |
| `close()` | — | `Void` | clears memory caches and calls `cancel()` on its in-flight tasks; a request already sent is not stopped, and its caller still gets the answer |
| `refreshFlags() async throws` | — | `Void` | **deprecated** no-op (no flag list to download) |
| `FeatureFlagEvaluator.hashUser(_ userId: String, flagKey: String)` | — | `Double` in `[0, 1)` | pure |

### Types

| Type | Members |
|---|---|
| `User(id:attributes:)` | `id: String`, `attributes: [String: AnyCodable]?` — sent as `context` on assignment |
| `EvalResult` | `key: String`, `enabled: Bool`, `config: [String: Any]?` (`nil` when the server returned `null` or a non-object) |
| `Assignment` | `experimentKey: String`, `userId: String`, `variantId: String?` (UUID), `variantName: String` (`"control"`, `"treatment"`, …), `isControl: Bool`, `configuration: [String: Any]?`; `variantKey` is a deprecated alias of `variantName` |
| `TrackEvent(userId:eventName:properties:experimentKey:featureFlagKey:value:eventType:timestamp:)` | `properties` → `metadata`; `eventType` defaults to `eventName`; `value: Double?`; `timestamp: Date?` → ISO-8601 (server-stamped when nil); `hasKey`, `attributed(experimentKey:featureFlagKey:)` |
| `ExperimentationError` | `.invalidConfig(String)`, `.networkError(Error)`, `.decodingError(Error)`, `.flagNotFound(String)`, `.experimentNotFound(String)`, `.serverError(Int, String)`, `.cancelled` — all `LocalizedError` |

---

## Caching and failure behaviour

- Successful results are cached in memory per **user + key** (`UserKeyCache`, NSCache-backed,
  TTL `cacheTTL`); a hit makes no request. Concurrent calls for the same user + key share one
  in-flight request, so mounting several views on the same experiment yields one assignment
  (and one exposure). **Failures are never cached** — the next call retries.
- With `enableOfflineFallback` (default) every successful result is also written to
  `UserDefaults`; those entries have no TTL and survive relaunches.
- **HTTP 404** (an experiment unknown or not ACTIVE, or an unknown flag key): throws `.flagNotFound(key)` /
  `.experimentNotFound(key)`, removes the persisted entry and never serves a stale value.
- **Network error, timeout, 401, 422, 429, 5xx, undecodable body**: returns the persisted value
  for that user + key when offline fallback is on and one exists; otherwise throws
  `.networkError`, `.serverError(status, body)`, `.decodingError` or `.cancelled`.
- `track`, `trackWithStatus`, `trackBatch` never throw.
- The client is safe to use from any thread or task: caches and the in-flight table are
  `NSLock`-protected and the lock is never held across a suspension point.

```swift
let enabled = (try? await client.evaluateFlag("new_search", user: user))?.enabled ?? false
```

---

## Tracking fan-out

With `experimentKey` and/or `featureFlagKey` set, `track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` with one entry per experiment the user
was assigned to through this client (`experiment_key`) plus one per flag evaluated for the user
(`feature_flag_key`), taken from the in-memory cache. Nothing cached → nothing is sent and `true`
is reported. `trackBatch` keeps keyed events as-is and fans out the unkeyed ones; requests are
chunked at `ExperimentationClient.batchLimit` (100).

Conversions are matched to metrics by **event name**: a metric on `purchase` counts every
`purchase` event regardless of `event_type`.

---

## SwiftUI

```swift
struct SearchView: View {
    let client: ExperimentationClient
    let user: User
    @State private var newSearch = false

    var body: some View {
        Group { newSearch ? AnyView(NewSearch()) : AnyView(LegacySearch()) }
            .onAppear {   // `.task {}` on iOS 15+
                Task { newSearch = (try? await client.evaluateFlag("new_search", user: user))?.enabled ?? false }
            }
    }
}
```

---

## Consistent hash (compatibility utility)

`FeatureFlagEvaluator.hashUser(userId, flagKey:)` = `MD5("{userId}:{flagKey}")`, first 4 bytes
as little-endian UInt32, divided by 2^32 (`0.6927449859213084` for `"user-123"`, `"my-flag"`).
It is kept only so the golden vectors in `tests/sdk-contract/golden-vectors.json` stay identical
across SDKs. **Nothing in the SDK calls it to pick a variant** — the server decides.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `getAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

Errors: 401 bad key, 404 (an experiment unknown or not ACTIVE, or an unknown flag key), 422 invalid event, 429
rate limited (`Retry-After`). These paths share the backend's per-IP `SDK_RATE_LIMIT_PER_MINUTE`
ceiling (default 6000).

---

## Contract smoke

```bash
cd sdk/ios && swift run contract-smoke
```

It prints:

```text
{"sdk":"ios","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

SwiftPM build output goes to stderr (the repo runner uses `swift run -q contract-smoke`). Env:
`CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The smoke uses
`enableOfflineFallback: false`, assigns on two clients (server stickiness), evaluates the flag,
tracks `purchase` with the experiment key, tracks `page_view` without a key and sends a 2-event
`trackBatch`. Fixtures: `backend/scripts/seed_sdk_contract.py`; repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk ios --strict`.

CI does not run this SDK's contract smoke against a real API: `ios` is not in the
`sdks:` list of the SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

```bash
cd sdk/ios
swift build
swift test
```

- `swift test`: XCTest; URLSession is intercepted with a URLProtocol mock

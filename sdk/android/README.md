# Controlvar Android SDK

Kotlin client for the Controlvar A/B testing and feature flag platform. Coroutine-based,
OkHttp under the hood, an offline fallback (in memory by default, `SharedPreferences` through an
adapter you pass), `minSdk 21`.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

> **Flag evaluation sends the user's attributes.** `evaluateFlag(flagKey, user)` sends `user.attributes`
> as `context` on `GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=<URL-encoded JSON object>`,
> so the flag's targeting rules are evaluated against them. With no attributes, or an empty map,
> no `context` is sent and the request carries the user id alone. The change is in this
> repository ([#438](https://github.com/Controlvar/controlvar/issues/438)); it reaches a registry
> at this SDK's first publish, so a build from a clone of `main` has it and a copy installed
> earlier does not.

Full reference: [`docs/sdk/android.md`](../../docs/sdk/android.md).

## Installation

**Not yet published.** The module is not on Maven Central, so build it from source: include
`sdk/android/sdk` of a clone of this repository in your Gradle build.

```kotlin
// settings.gradle.kts
include(":sdk")
project(":sdk").projectDir = file("../controlvar/sdk/android/sdk")

// build.gradle.kts (app module)
dependencies {
    implementation(project(":sdk"))
}
```

Requires Kotlin 1.9, coroutines 1.7 and OkHttp 4.12 (declared by the module). Package:
`com.controlvar.android`.

## Quick Start

```kotlin
import com.controlvar.android.ExperimentationClient
import com.controlvar.android.SdkConfig
import com.controlvar.android.TrackEvent
import com.controlvar.android.User

val client = ExperimentationClient(
    SdkConfig(
        baseUrl = "http://10.0.2.2:8000",   // origin only; 10.0.2.2 = host localhost from the emulator
        apiKey = BuildConfig.CONTROLVAR_API_KEY   // sent as X-API-Key
    )
)

lifecycleScope.launch {
    val user = User(id = "user-123", attributes = mapOf("plan" to "pro", "country" to "US"))

    // Experiment assignment — POST /api/v1/tracking/assign (sticky on the server)
    val assignment = client.getAssignment("checkout_flow", user)
    assignment.variantName      // "control" / "treatment"
    assignment.configuration    // Map<String, Any?>? from the variant definition

    // Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123&context=%7B%22country%22%3A%22US%22%2C%22plan%22%3A%22pro%22%7D
    val flag = client.evaluateFlag("new_search", user)
    if (flag.enabled) { /* show the feature; flag.config is always null */ }

    // Track with a key — one POST /api/v1/tracking/track
    client.track(TrackEvent(user.id, "purchase", mapOf("currency" to "USD"),
        experimentKey = "checkout_flow", value = 49.99))

    // Track without a key — fanned out to every cached assignment + flag for the user
    client.track(TrackEvent(user.id, "page_view"))
}
```

## Configuration

| Property | Type | Default | Description |
|---|---|---|---|
| `baseUrl` | `String` | `http://localhost:8000` | Backend origin; the SDK appends `/api/v1/...` |
| `apiKey` | `String` | *(required)* | Sent as `X-API-Key` on every request |
| `timeoutMs` | `Long` | `10000` | OkHttp connect/read/write timeout |
| `cacheSize` | `Int` | `1000` | Max cached evaluations (and, separately, assignments), LRU eviction |
| `cacheTtlMs` | `Long` | `300000` | How long a successful evaluation/assignment is reused |
| `enableLocalEval` | `Boolean` | — | Deprecated no-op kept for source compatibility |

`ExperimentationClient` also accepts an `HttpClient`, two `ResultCache`s and an `OfflineStore`
for injection; pass `OfflineStore(prefsAdapter)` wrapping `context.getSharedPreferences(...)`
to persist results across process restarts.

## API

| Method | Returns | On failure |
|---|---|---|
| `suspend evaluateFlag(flagKey, user)` | `EvalResult(key, enabled, config)` | Last offline value if any (not after a 404), else throws `FlagNotFoundException` (404) / `ServerException` / `NetworkException` |
| `suspend getAssignment(experimentKey, user)` | `Assignment(experimentKey, userId, variantId, variantName, isControl, configuration)` | Last offline value if any, else throws `ServerException` (404: no experiment has the key, or a draft has no control variant) / `NetworkException` |
| `suspend track(event)` | `Unit` | Never throws; runs in the background |
| `suspend trackBatch(events)` | `Unit` | Never throws; ≤ 100 events per request |
| `getCachedAssignments(userId)`, `getCachedFlagKeys(userId)` | cached, unexpired entries | — |
| `invalidateCache(userId, key)`, `clearCache()`, `getCacheSize()`, `close()` | — | — |
| `ConsistentHash.compute(userId, key)` | `Double` in `[0, 1)` | Pure function (parity utility) |

## Caching and failure behaviour

- Successful evaluations and assignments are cached in memory per **user + key** for
  `cacheTtlMs` and written to the `OfflineStore`. A cache hit makes no request.
- The SDK keeps one cached answer per user and flag, together with the attributes it was asked
  with. A later call is answered from the cache only if its attributes are exactly the same;
  otherwise the SDK asks the server, and the new answer replaces the cached one. So two calls
  that differ only in attributes get two answers. The attributes are written with their keys
  sorted, so the same attributes always find the same cached answer.
- **Failures are never cached.** The SDK stores the last answer the server gave for each user and
  flag, whatever attributes it was asked with, and serves it only when the server cannot answer: on a network or HTTP failure other than
  a 404 it is returned (not re-cached, so the next call retries); with nothing stored the call
  throws, exactly as the SDK did before the rewire. A 404 removes the stored answer, and the call
  throws `FlagNotFoundException`.
- If the attributes cannot be written as JSON (a NaN or an infinite number, at any depth),
  `evaluateFlag` throws `IllegalArgumentException` ("user.attributes cannot be sent as JSON") and
  sends nothing.
- `track` / `trackBatch` never throw: the request is launched on a background `Dispatchers.IO`
  scope and failures are swallowed.

## Tracking fan-out

With `experimentKey` and/or `featureFlagKey` set, `track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` containing one entry per experiment the
user was assigned to through this client (`experiment_key`) plus one per flag evaluated for the
user (`feature_flag_key`), taken from the in-memory cache. Nothing cached → nothing is sent.
`event_type` defaults to the event name; `properties` is sent as `metadata`.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`cacheTtlMs`, 5 minutes by default) is not sent, so pass `experimentKey` on a conversion that
can come later.

## Event ids

Every event this SDK sends carries an `event_id`, and the server stores an event sent again with
the same `event_id`, user, experiment, flag and event name only once. The SDK is not on Maven
Central yet, so this is in the build you make from a clone of this repository.

- `TrackEvent(userId, eventName, eventId = ...)` sets the id. Without one, the SDK makes a
  lowercase random UUID (version 4) each time you track the event, so tracking the same
  `TrackEvent` twice sends two events. For an event your code may send again, make the id once,
  when the event happens (for example `UUID.randomUUID().toString()`), keep it with the event,
  and pass the same `eventId` each time you send it.
- A fanned-out event carries the same id in every entry. In `trackBatch`, each event has its own id.
- At most 255 characters, with no NUL and no lone surrogate; `TrackEvent`'s constructor and
  `copy()` throw `IllegalArgumentException` for any other id, so that event is never sent. `null`
  and `""` set none.
- OkHttp sends a request again by itself when a kept-alive connection that the server or a proxy
  has closed drops after the request was written. The request it sends again carries the same
  body and the same `event_id`, so the server stores the event once.
- A request that times out, or whose new connection drops, is not sent again. To send it again
  yourself and have it stored once, give the event an `eventId` and send that same id again.

Full details: [Event ids](../../docs/sdk/android.md#event-ids).

## Consistent hash (compatibility utility)

`ConsistentHash.compute(userId, key)` computes `MD5("{userId}:{key}")`, reads the first 4 bytes as
a little-endian uint32 and divides by 2^32 — `0.6927449859213084` for `("user-123", "my-flag")`.
It is exported only so the golden vectors in `tests/sdk-contract/` keep passing; nothing in the SDK
buckets locally. The former `FeatureFlagEvaluator.hashUser` is a deprecated alias.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…` | `context`: the user's attributes as JSON, left out when there are none | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `getAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?, event_id}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

## Tests

Nothing in `sdk/src/main/kotlin` touches an `android.*` API — `OfflineStore` talks to a
`PrefsAdapter` interface, `ResultCache` is hand-rolled rather than `android.util.LruCache`, and JSON
goes through `org.json` (which `android.jar` only stubs, so the real implementation is a dependency
for unit tests either way). So the same sources compile and test on a plain JVM:

```bash
cd sdk/android/jvm && ./mvnw clean test
cd sdk/android && gradle :sdk:testDebugUnitTest
```

- `cd sdk/android/jvm && ./mvnw clean test`: JUnit 5 + MockWebServer — no Android SDK, no emulator
- `cd sdk/android && gradle :sdk:testDebugUnitTest`: the same tests through the AAR build (Gradle 8 + Android SDK)

Both run in `.github/workflows/sdk-unit-tests.yml` (nightly, and on any PR touching `sdk/android/**`).

## Contract smoke

```bash
bash sdk/android/examples/contract_smoke.sh
```

It prints:

```text
{"sdk":"android","assign":{"variant_name":"treatment","is_control":false,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true},"event_id":{"user_id":"smoke-<uuid>","event_name":"contract_dedup_<uuid>","id":"contract-<uuid>","sends":2}}
```

Runs `com.controlvar.android.examples.ContractSmoke` from the JVM build above
(`./mvnw -q package -DskipTests` when `jvm/target` is stale, Maven output to stderr; `FORCE_BUILD=1`
forces a rebuild). Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`),
`CONTROLVAR_API_KEY` (required), `CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`),
`CONTRACT_FLAG_KEY` (default `sdk_contract_flag`), `CONTRACT_TARGETED_FLAG_KEY` (default
`sdk_contract_targeted`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Its `targeted` report entry holds the answers for `plan: pro` and `plan: free`. Repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk android`.

`ExperimentationClient.track()` is fire-and-forget — it launches on a background scope and swallows
every error — so the smoke calls it *and* posts the same `TrackEvent.toJson()` bodies through
`HttpClient`, which throws on a non-2xx response. A renamed field or a path the backend does not
serve fails the smoke. It also sends one keyed event twice with one fixed `eventId` and reports it
as `event_id`; the runner then checks that the server stored that event once.

CI runs this SDK's contract smoke against a real API: `android` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml). The smoke
compiles the SDK's Kotlin sources for a plain JVM; the Gradle/AAR build is not part of it.

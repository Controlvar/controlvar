# Android Kotlin SDK

The Android SDK (`com.controlvar.android`) provides feature flag evaluation,
experiment assignment and event tracking for Android applications. It is built on Kotlin
Coroutines and OkHttp, keeps the last successful results in an offline store for fallback (in
memory by default; persisted to `SharedPreferences` when you pass an adapter, see [Configuration](#configuration)),
and its unit tests run on the plain JVM with OkHttp's `MockWebServer`.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

> **Flag evaluation sends only the user id.** `GET /api/v1/feature-flags/evaluate/{key}?user_id=…`
> carries none of the user's attributes, so the server evaluates a flag's targeting rules against
> an empty context. Experiment assignment sends them: `getAssignment`, with `user.attributes`, sends the attributes as
> `context`. To evaluate a flag with attributes, call the evaluate endpoint with a `context`
> query parameter (a URL-encoded JSON object of the attributes).

Source: `sdk/android` (module `sdk/android/sdk`, Compose example in `sdk/android/examples`).

---

## Requirements

- Android `minSdk 21`, `compileSdk 34`
- Kotlin 1.9, `kotlinx-coroutines-android` 1.7, OkHttp 4.12 (declared by the module)
- JDK 17 for the build

---

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

---

## Quick Start

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

```kotlin
import com.controlvar.android.ExperimentationClient
import com.controlvar.android.SdkConfig
import com.controlvar.android.TrackEvent
import com.controlvar.android.User

val client = ExperimentationClient(
    SdkConfig(
        baseUrl = "http://10.0.2.2:8000",           // origin only; the SDK appends /api/v1/...
        apiKey = BuildConfig.CONTROLVAR_API_KEY   // sent as X-API-Key
    )
)

lifecycleScope.launch {
    val user = User(id = "user-123", attributes = mapOf("plan" to "pro", "country" to "US"))

    val flag = client.evaluateFlag("new-checkout", user)          // server decides
    if (flag.enabled) showNewCheckout()

    val assignment = client.getAssignment("checkout-cta-copy", user)   // sticky on the server
    updateCtaCopy(assignment.variantName, assignment.configuration)

    client.track(TrackEvent(user.id, "purchase", mapOf("sku" to "pro-plan"),
        experimentKey = "checkout-cta-copy", value = 99.99))
}
```

`http://10.0.2.2:8000` reaches a backend on the host machine from the emulator. Seed a working
experiment, flag and API key with `python backend/scripts/seed_sdk_contract.py` (the key is
written to `tests/sdk-contract/live/.api_key`).

---

## Configuration

```kotlin
val config = SdkConfig(
    baseUrl = "https://api.example.com",   // Required — origin only
    apiKey = "your-api-key",               // Required — sent as X-API-Key
    timeoutMs = 10_000L,                   // OkHttp connect/read/write timeout (default 10 s)
    cacheSize = 1000,                      // Max cached evaluations and, separately, assignments (default 1000)
    cacheTtlMs = 300_000L                  // How long a successful result is reused (default 5 min)
)
val client = ExperimentationClient(config)
```

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `baseUrl` | `String` | `http://localhost:8000` | Backend origin, e.g. `https://api.example.com`; the SDK appends `/api/v1/...` |
| `apiKey` | `String` | *(required)* | API key sent as `X-API-Key` |
| `timeoutMs` | `Long` | `10000` | OkHttp connect, read and write timeout in milliseconds |
| `cacheSize` | `Int` | `1000` | Maximum LRU entries per cache (evaluations, assignments) |
| `cacheTtlMs` | `Long` | `300000` | Milliseconds a successful evaluation/assignment is reused |
| `enableLocalEval` | `Boolean` | — | Deprecated; has no effect (flags are evaluated by the server) |

`ExperimentationClient(config, httpClient, flagCache, assignmentCache, offlineStore)` accepts
every collaborator for injection. `OfflineStore()` without arguments is in-memory (tests); in an
app pass an `OfflineStore.PrefsAdapter` wrapping `SharedPreferences`:

```kotlin
class PrefsAdapter(private val prefs: SharedPreferences) : OfflineStore.PrefsAdapter {
    override fun getString(key: String) = prefs.getString(key, null)
    override fun putString(key: String, value: String) { prefs.edit().putString(key, value).apply() }
    override fun remove(key: String) { prefs.edit().remove(key).apply() }
    override fun allKeys() = prefs.all.keys
    override fun clear() { prefs.edit().clear().apply() }
}

val client = ExperimentationClient(
    config,
    offlineStore = OfflineStore(PrefsAdapter(context.getSharedPreferences("controlvar", Context.MODE_PRIVATE)))
)
```

---

## Lifecycle

Each `ExperimentationClient` has its own in-memory caches and its own background coroutine scope
for tracking, so share one per process (an `Application` field or a DI singleton). Call
`close()` when it is no longer needed (`ViewModel.onCleared`, `Activity.onDestroy`); it drops the
in-memory caches. Events already handed to `track` keep flushing in the background.

---

## User model

```kotlin
val user = User(
    id = "user-123",                                      // stable identifier, required
    attributes = mapOf("plan" to "pro", "country" to "US", "beta" to true)
)
```

`attributes` are sent as the assignment `context` (targeting rules). Values may be `String`,
`Int`, `Long`, `Double`, `Boolean`, nested `Map`/`List` or `null`. Flag evaluation sends the
user id only (see the note at the top of this page).

---

## Feature Flag Evaluation

### `suspend fun evaluateFlag(flagKey: String, user: User): EvalResult`

Calls `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…` (both values percent-encoded).

| Property | Type | Description |
|----------|------|-------------|
| `key` | `String` | The flag key you asked for |
| `enabled` | `Boolean` | Server decision for this user |
| `config` | `Any?` | Always `null`: the evaluate endpoint returns `config: null` for every flag |
| `configMap` | `Map<String, Any?>?` | Always `null`, like `config` |
| `variant` | `String?` | `null` when off, otherwise `"on"` |

```kotlin
val result = client.evaluateFlag("dark-mode", user)
if (result.enabled) {
    enableDarkMode()
}
```

Resolution order: the in-memory cache for this user + flag, then the API, then (on failure) the
offline store's last successful evaluation for this user + flag. Failures are never cached; an
offline fallback is returned without being re-cached, so the next call retries the server. With
no offline value the call **throws** — `FlagNotFoundException` on 404 (an unknown flag key; a
flag that is not ACTIVE is not an error, it answers `enabled: false`), `ServerException` on another non-2xx status, `NetworkException` on IO errors — which is
what the SDK did before the rewire.

---

## Experiment Assignment

### `suspend fun getAssignment(experimentKey: String, user: User): Assignment`

Calls `POST /api/v1/tracking/assign` with `{experiment_key, user_id, context}` (`context` is
omitted when the user has no attributes). The server buckets the user, keeps the assignment
sticky and records the exposure.

| Property | Type | Description |
|----------|------|-------------|
| `experimentKey` | `String` | The experiment key |
| `userId` | `String` | The assigned user |
| `variantId` | `String?` | UUID of the assigned variant |
| `variantName` | `String` | Assigned variant name (e.g. `"control"`, `"treatment"`) |
| `isControl` | `Boolean` | `true` for the control variant |
| `configuration` | `Map<String, Any?>?` | The variant's `configuration` JSON from the experiment definition |
| `variantKey` | `String` | Deprecated alias for `variantName` |

```kotlin
val assignment = client.getAssignment("checkout-cta-copy", user)
when (assignment.variantName) {
    "treatment-a" -> renderShortCta()
    "treatment-b" -> renderUrgencyCta()
    else          -> renderOriginalCta()
}
```

Successful assignments are cached per user + experiment for `cacheTtlMs` (the second call makes
no request) and persisted to the offline store. On failure the offline value is returned if one
exists; otherwise `ServerException` (404 when no experiment has the key, or for a draft with no
control variant; 401 bad key; 429 rate limited) or `NetworkException` is thrown. Failures are
never cached.

---

## Event Tracking

### `suspend fun track(event: TrackEvent)`

Fire-and-forget: the request runs on a background `Dispatchers.IO` scope and **never throws**.
Events with an empty `userId` or `eventName` are ignored.

```kotlin
data class TrackEvent(
    val userId: String,
    val eventName: String,
    val properties: Map<String, Any?> = emptyMap(),   // sent as metadata
    val experimentKey: String? = null,
    val featureFlagKey: String? = null,
    val value: Double? = null,
    val eventType: String? = null,                    // defaults to eventName
    val timestampMs: Long? = null                     // sent as ISO-8601 UTC when set
)
```

```kotlin
client.track(TrackEvent(user.id, "purchase", mapOf("sku" to "pro-plan"), experimentKey = "checkout-cta-copy", value = 99.99))
client.track(TrackEvent(user.id, "search", featureFlagKey = "new-search"))
client.track(TrackEvent(user.id, "page_view", mapOf("page" to "/products")))   // no key: fanned out
```

**Fan-out rule.** With `experimentKey` and/or `featureFlagKey` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch`
containing one entry per experiment the user has been assigned to through this client plus one
per flag evaluated for the user (from the in-memory cache). If nothing is cached, nothing is
sent. This is what makes a single `track("purchase")` count as a conversion for every experiment
the user is in. Conversions are matched to metrics by **event name**.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`cacheTtlMs`, 5 minutes by default) is not sent, so pass `experimentKey` on a conversion that
can come later.

### `suspend fun trackBatch(events: List<TrackEvent>)`

Sends `POST /api/v1/tracking/batch` with at most 100 events per request (longer lists are
chunked). Keyed events are sent as-is; unkeyed events are fanned out like `track`, and unkeyed
events whose user has nothing cached are dropped. Never throws.

---

## Cache helpers

| Method | Description |
|--------|-------------|
| `getCachedAssignments(userId): List<Assignment>` | Cached (successful, unexpired) assignments for the user |
| `getCachedFlagKeys(userId): List<String>` | Keys of flags successfully evaluated (and still cached) for the user |
| `invalidateCache(userId, key)` | Drop the evaluation and assignment for one user + key from memory and the offline store |
| `clearCache()` | Drop every in-memory evaluation and assignment (the offline store is kept) |
| `getCacheSize(): Int` | Cached evaluations plus assignments |

The in-memory caches are thread-safe LRU maps (`ResultCache`) keyed by `userId`, a NUL
separator and the flag/experiment `key`.
The offline store keys are `ep_flag_<len>_<userId>_<key>` / `ep_assign_<len>_<userId>_<key>`.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFlag` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `getAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `track` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend; a `429` surfaces as `ServerException(429, …)`. The former
`/api/v1/feature-flags/{key}`, `/api/v1/feature-flags` (list), `/api/v1/experiments/{key}/assign`
and `/api/v1/events` calls, the `Authorization: ApiKey` header and `refreshFlags()` are gone.

---

## Error Handling

`evaluateFlag` and `getAssignment` throw `ExperimentationException` subclasses when the API
fails and no offline value exists; `track`/`trackBatch` never throw.

| Class | When |
|-------|------|
| `ExperimentationException.NetworkException(message, cause)` | OkHttp IO failure or timeout |
| `ExperimentationException.ServerException(statusCode, message)` | Non-2xx response: 401 bad key, 404 on an assignment (no experiment has the key, or a draft with no control variant), 422 invalid event, 429 rate limited; `message` carries the API's `detail` |
| `ExperimentationException.FlagNotFoundException(flagKey)` | `evaluateFlag` got a 404 and nothing is stored offline |
| `ExperimentationException.InvalidConfigException(message)` | Reserved for configuration errors |

```kotlin
try {
    val result = client.evaluateFlag("my-flag", user)
} catch (e: ExperimentationException.FlagNotFoundException) {
    // unknown flag key → treat as off
} catch (e: ExperimentationException.NetworkException) {
    // offline and nothing stored → treat as off
} catch (e: ExperimentationException.ServerException) {
    Log.w("exp", "API error ${e.statusCode}: ${e.message}")
}
```

`evaluateFlag`/`getAssignment` also throw `IllegalArgumentException` for an empty key or user id.

---

## Consistent Hash Utility

`ConsistentHash.compute(userId: String, key: String): Double` implements the cross-SDK formula —
`MD5("{userId}:{key}")`, first 4 bytes as little-endian uint32, divided by 2^32 — and is pinned by
the golden vectors in `tests/sdk-contract/golden-vectors.json` (`HashCompatibilityTest`):

```kotlin
ConsistentHash.compute("user-123", "my-flag")   // 0.6927449859213084
```

It is exported as a utility only. Since assignment moved to the server, nothing in the SDK uses
it to decide a variant. `FeatureFlagEvaluator.hashUser` remains as a deprecated alias.

---

## Jetpack Compose

```kotlin
@Composable
fun DashboardScreen(client: ExperimentationClient, user: User) {
    var showNewDashboard by remember { mutableStateOf(false) }

    LaunchedEffect(user.id) {
        showNewDashboard = try {
            client.evaluateFlag("new-dashboard", user).enabled
        } catch (e: ExperimentationException) {
            false
        }
    }

    if (showNewDashboard) NewDashboard() else LegacyDashboard()
}
```

A complete Activity is in `sdk/android/examples/ComposeExample/ExampleActivity.kt`.

---

## Testing your own code

Point the client at OkHttp's `MockWebServer` (the SDK's own tests do this) or inject fakes
through the constructor:

```kotlin
val server = MockWebServer().apply { start() }
server.enqueue(MockResponse().setResponseCode(200)
    .setBody("""{"key":"new-checkout","enabled":true,"config":null}"""))

val client = ExperimentationClient(SdkConfig(baseUrl = server.url("/").toString().trimEnd('/'), apiKey = "test"))

runTest {
    assertTrue(client.evaluateFlag("new-checkout", User("u1")).enabled)
    val request = server.takeRequest()
    assertEquals("/api/v1/feature-flags/evaluate/new-checkout?user_id=u1", request.path)
    assertEquals("test", request.getHeader("X-API-Key"))
}
```

---

## Development and verification

```bash
cd sdk/android && gradle :sdk:test
```

- `cd sdk/android && gradle :sdk:test`: JUnit 5 + MockWebServer (no wrapper checked in)

`ExperimentationClientTest` covers the exact request for each endpoint (method, path
encoding, `user_id` query, headers, JSON body), response mapping, sticky cache hits, TTL expiry,
404/401/network failures, offline fallback, that failures are never cached, keyed `track`
bodies, key-less fan-out, "nothing cached → no request", batch chunking at 100 and that `track`
never throws. `HashCompatibilityTest` pins the golden vectors.

The contract smoke, `bash sdk/android/examples/contract_smoke.sh`, compiles the SDK's Kotlin
sources for a plain JVM (`sdk/android/jvm`), so it needs no Android SDK and no emulator; the
live runner runs it with `python tests/sdk-contract/live/run_live_contract.py --sdk android --strict`.

CI runs this SDK's contract smoke against a real API: `android` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

# Java SDK

`com.controlvar:controlvar-sdk` (v1.0.0) is a Java 11+ client for feature
flags, experiment assignment and event tracking, built on OkHttp 4 and Jackson.
`controlvar-spring-boot-starter` adds Spring Boot auto-configuration (built and tested
against Spring Boot 3.5.16, which needs Java 17+ at runtime).

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

Source: `sdk/java` (`core/`, `spring-boot-starter/`).

---

## Installation

**Not yet published.** `com.controlvar:controlvar-sdk` and the Spring Boot starter are not
on Maven Central, so build them from source: in a clone of this repository,
`cd sdk/java && ./mvnw install` installs both into your local `~/.m2`, and the dependencies
below then resolve from there.

```xml
<!-- core client -->
<dependency>
    <groupId>com.controlvar</groupId>
    <artifactId>controlvar-sdk</artifactId>
    <version>1.0.0</version>
</dependency>

<!-- Spring Boot: the starter depends on the core, so add only this -->
<dependency>
    <groupId>com.controlvar</groupId>
    <artifactId>controlvar-spring-boot-starter</artifactId>
    <version>1.0.0</version>
</dependency>
```

Gradle reads `~/.m2` only through `mavenLocal()`: add it to `repositories`, then
`implementation 'com.controlvar:controlvar-sdk:1.0.0'` (or the starter).

---

## Quick Start

```java
import com.controlvar.sdk.ExperimentationClient;
import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.TrackEvent;
import com.controlvar.sdk.model.User;
import java.util.Map;

SdkConfig config = SdkConfig.builder(System.getenv("EXPERIMENTLY_API_KEY"), "http://localhost:8000")
        .timeoutMs(3000)
        .build();

try (ExperimentationClient client = new ExperimentationClient(config)) {   // AutoCloseable
    User user = User.builder("user-123").attribute("plan", "pro").build(); // sent as the assignment's context

    // 1. Assignment — POST /api/v1/tracking/assign (sticky, records the exposure)
    String headline = "Buy now";
    try {
        ExperimentAssignment a = client.getExperimentAssignment(user, "checkout_flow");
        if (a.getConfiguration() != null) {
            headline = (String) a.getConfiguration().getOrDefault("headline", headline);
        }
    } catch (ExperimentationException e) {
        // 404 (not ACTIVE / unknown), 401, network: keep the control experience
    }

    // 2. Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123
    if (client.isFeatureEnabled(user, "new_search")) { /* false on any failure */ }
    FlagEvaluation flag = client.evaluateFeatureFlag(user, "new_search"); // throws on failure

    // 3. Track with a key → one POST /api/v1/tracking/track (async, never throws)
    client.trackEvent(TrackEvent.builder("user-123", "purchase")
            .experimentKey("checkout_flow").value(49.99).property("sku", "pro-plan").build());

    // 4. Track without a key → fanned out to every cached assignment + flag of this user
    client.trackEvent("user-123", "page_view", Map.of("page", "/products"));
}
```

---

## Configuration

`SdkConfig.builder(apiKey, baseUrl)`; both are required and non-empty, every setter rejects
non-positive values with `IllegalArgumentException`.

| Builder | Default | Description |
|---|---|---|
| `apiKey` (ctor) | — | Sent as `X-API-Key` |
| `baseUrl` (ctor) | — | Backend origin, e.g. `https://api.example.com`; trailing `/` stripped; the SDK appends `/api/v1/...` |
| `timeoutMs(int)` | `5000` | OkHttp connect / read / write timeout |
| `cacheTtlMs(long)` | `300000` (5 min) | Lifetime of a cached evaluation or assignment |
| `cacheSize(int)` | `1000` | Max entries in **each** of the two caches (flags, assignments), LRU eviction |

For tests, the package-private constructor `ExperimentationClient(SdkConfig, OkHttpClient)`
accepts a custom `OkHttpClient` (used with `MockWebServer`).

---

## API Reference

| Method | Endpoint | Returns | On failure |
|---|---|---|---|
| `evaluateFeatureFlag(User, String flagKey)` | `GET /feature-flags/evaluate/{key}?user_id=` | `FlagEvaluation` | throws `ExperimentationException` |
| `isFeatureEnabled(User, String flagKey)` | same (via cache) | `boolean` | `false` |
| `getExperimentAssignment(User, String experimentKey)` | `POST /tracking/assign` | `ExperimentAssignment` (never null) | throws `ExperimentationException` |
| `trackEvent(TrackEvent)` | `/tracking/track` or `/tracking/batch` | `void` (async) | swallowed |
| `trackEvent(String userId, String eventName, Map props)` | `/tracking/batch` (fan-out) | `void` (async) | swallowed; invalid args ignored |
| `trackEvent(userId, eventName, props, experimentKey, featureFlagKey, Double value)` | `/tracking/track` | `void` (async) | swallowed |
| `trackBatch(List<TrackEvent>)` | `/tracking/batch`, 100 per request | `void` (async) | swallowed |
| `trackEventSync(TrackEvent)` / `trackBatchSync(List<TrackEvent>)` | as above, blocking | `void` | throws `ExperimentationException` |
| `getCachedAssignments(String userId)` | — | `List<ExperimentAssignment>` | — |
| `getCachedFlagKeys(String userId)` | — | `List<String>` | — |
| `invalidateCache(userId, key)`, `clearCache()`, `getCacheSize()` | — | `void` / `int` | — |
| `close()` | — | shuts down the OkHttp dispatcher and pool, clears caches | — |
| `ConsistentHash.compute(String userId, String key)` | — | `double` in `[0, 1)` | — |

### Models

| Class | Accessors |
|---|---|
| `User.builder(userId).attribute(k, v).attributes(map).build()` | `getUserId()`, `getAttributes()` (unmodifiable), `getAttribute(k)` |
| `FlagEvaluation` | `getKey()`, `isEnabled()`, `getConfig()` (`Object`: `Map`, `List`, scalar or `null`), `getConfigMap()` (`Map` or `null`) |
| `ExperimentAssignment` | `getExperimentKey()`, `getUserId()`, `getVariantId()` (UUID), `getVariantName()`, `isControl()`, `getConfiguration()` (`Map<String,Object>` or `null`); `getVariantKey()` is a deprecated alias |
| `TrackEvent.builder(userId, eventName)` | `.eventType(s)` (defaults to `eventName`), `.experimentKey(s)`, `.featureFlagKey(s)`, `.value(Double)`, `.property(k, v)` / `.properties(map)` (sent as `metadata`), `.timestamp(Instant)` (ISO-8601); `hasKey()`, `toBody()` |
| `ExperimentationException` (`RuntimeException`) | `getStatusCode()` (HTTP status, `0` for network/serialization), `isApiError()` |

---

## Caching and failure behaviour

- Successful evaluations and assignments are cached per **user + key** in two LRU caches
  (`AssignmentCache<FlagEvaluation>`, `AssignmentCache<ExperimentAssignment>`) for `cacheTtlMs`.
  A hit makes no request. **Failures are never cached** — the next call retries.
- Non-2xx responses (401 bad key, 404 for an experiment unknown or not ACTIVE, or an unknown flag key, 422, 429 rate
  limited, 5xx) and `IOException`s become `ExperimentationException`; `evaluateFeatureFlag` and
  `getExperimentAssignment` **throw**, `isFeatureEnabled` returns `false`. There is no stale
  fallback beyond the TTL — a request that fails after the entry expired surfaces the error.
- Async tracking (`trackEvent`, `trackBatch`) is fire-and-forget on OkHttp's dispatcher and
  swallows every error; the `*Sync` variants block and throw.
- The client is thread-safe (synchronized caches, OkHttp connection pool). Create one per
  application and `close()` it on shutdown.

---

## Tracking fan-out

With `experimentKey` and/or `featureFlagKey` set, `trackEvent` sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch` with one
entry per experiment the user was assigned to through this client (`experiment_key`) plus one per
flag evaluated for the user (`feature_flag_key`), taken from the caches. Nothing cached → nothing
is sent. `trackBatch` puts keyed events in the batch as-is and fans out the unkeyed ones; requests
are chunked at 100 events.

Conversions are matched to metrics by **event name**: a metric on `purchase` counts every
`purchase` event regardless of `event_type`.

---

## Spring Boot starter

`ExperimentationAutoConfiguration` is registered via
`META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports` and activates
when `ExperimentationClient` is on the classpath **and** `experimentation.api-key` is set. It
registers one `ExperimentationClient` bean (`@ConditionalOnMissingBean`, so your own bean wins).
`@EnableExperimentation` is an optional `@Import` for plain-Spring or self-documenting setups.

```yaml
experimentation:
  api-key: ${EXPERIMENTLY_API_KEY}      # required; relaxed binding (EXPERIMENTATION_API_KEY works)
  base-url: http://localhost:8000
  timeout-ms: 5000
  cache-ttl-seconds: 300
  cache-size: 1000
```

| Property | Type | Default | Maps to |
|---|---|---|---|
| `experimentation.api-key` | `String` | — (required) | `SdkConfig.apiKey` |
| `experimentation.base-url` | `String` | `https://api.controlvar.example.com` | `SdkConfig.baseUrl` — set it to your backend origin |
| `experimentation.timeout-ms` | `int` | `5000` | `timeoutMs` |
| `experimentation.cache-ttl-seconds` | `int` | `300` | `cacheTtlMs` (× 1000) |
| `experimentation.cache-size` | `int` | `1000` | `cacheSize` |

```java
@Service
public class SearchService {
    private final ExperimentationClient client;          // constructor-injected bean
    public SearchService(ExperimentationClient client) { this.client = client; }

    public boolean useNewSearch(String userId) { return client.isFeatureEnabled(User.builder(userId).build(), "new_search"); }
}
```

---

## Consistent hash (compatibility utility)

`ConsistentHash.compute(userId, key)` = `MD5("{userId}:{key}")`, first 4 bytes as little-endian
uint32, divided by 2^32 (`0.6927449859213084` for `"user-123"`, `"my-flag"`). It is kept only so
the golden vectors in `tests/sdk-contract/golden-vectors.json` stay identical across SDKs.
**Nothing in the SDK calls it to pick a variant** — the server decides.
`FeatureFlagEvaluator.computeHash` is a deprecated alias.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFeatureFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `getExperimentAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `trackEvent` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `trackEvent` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

Errors: 401 bad key, 404 (an experiment unknown or not ACTIVE, or an unknown flag key), 422 invalid event, 429
rate limited (`Retry-After`). These paths share the backend's per-IP `SDK_RATE_LIMIT_PER_MINUTE`
ceiling (default 6000).

---

## Contract smoke

```bash
bash sdk/java/examples/contract_smoke.sh
```

It prints:

```text
{"sdk":"java","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

The script builds when needed (`./mvnw -q -B -pl core -am package -DskipTests`, Maven output on stderr;
`FORCE_BUILD=1` forces it) and runs
`java -cp "core/target/classes:core/target/lib/*" com.controlvar.sdk.examples.ContractSmoke`.
Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The smoke assigns on one
client twice and on a fresh client once (server stickiness), evaluates the flag, tracks `purchase`
with the experiment key via `trackEventSync`, tracks `page_view` without a key and sends a 2-event
`trackBatchSync`. Fixtures: `backend/scripts/seed_sdk_contract.py`; repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk java --strict`.

CI runs this SDK's contract smoke against a real API: `java` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

```bash
cd sdk/java && mvn test
```

- `cd sdk/java && mvn test`: the core module (JUnit 5, MockWebServer) and the spring-boot-starter module

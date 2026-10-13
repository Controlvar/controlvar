# Experimently Java SDK

Java 11+ client for the Experimently A/B testing and feature flag platform (OkHttp + Jackson),
plus a Spring Boot 3 auto-configuration starter. Thread-safe; one client per application.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

Modules: `core/` (`experimently-sdk`) and `spring-boot-starter/`
(`experimently-spring-boot-starter`). Full reference: [`docs/sdk/java.md`](../../docs/sdk/java.md).

## Installation

**Not yet published.** `com.getexperimently:experimently-sdk` and the Spring Boot starter are not
on Maven Central yet, so the dependencies below resolve only after you run `mvn install` in
`sdk/java` of a clone of this repository, which installs both into your local `~/.m2`.

```xml
<dependency>
    <groupId>com.getexperimently</groupId>
    <artifactId>experimently-sdk</artifactId>          <!-- core client -->
    <version>1.0.0</version>
</dependency>
<!-- or, for Spring Boot: -->
<dependency>
    <groupId>com.getexperimently</groupId>
    <artifactId>experimently-spring-boot-starter</artifactId> <!-- pulls in the core -->
    <version>1.0.0</version>
</dependency>
```

Gradle: `implementation 'com.getexperimently:experimently-sdk:1.0.0'`.
Inside this monorepo: `cd sdk/java && mvn install`.

## Quick Start (core client)

```java
import com.getexperimently.sdk.ExperimentationClient;
import com.getexperimently.sdk.config.SdkConfig;
import com.getexperimently.sdk.exception.ExperimentationException;
import com.getexperimently.sdk.model.*;

SdkConfig config = SdkConfig.builder(System.getenv("EXPERIMENTLY_API_KEY"), "http://localhost:8000").build();

try (ExperimentationClient client = new ExperimentationClient(config)) {
    User user = User.builder("user-123").attribute("plan", "pro").attribute("country", "US").build();

    // Experiment assignment — POST /api/v1/tracking/assign (sticky on the server)
    try {
        ExperimentAssignment a = client.getExperimentAssignment(user, "checkout_flow");
        a.getVariantName();   // "control" / "treatment"
        a.isControl();
        a.getConfiguration(); // Map<String,Object> or null
    } catch (ExperimentationException e) {
        // 404 not ACTIVE, 401 bad key, network error -> show the control experience
    }

    // Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123
    boolean on = client.isFeatureEnabled(user, "new_search");          // false on any failure
    FlagEvaluation flag = client.evaluateFeatureFlag(user, "new_search"); // throws on failure

    // Track with a key — one POST /api/v1/tracking/track (async, never throws)
    client.trackEvent(TrackEvent.builder("user-123", "purchase")
            .experimentKey("checkout_flow").value(49.99).property("currency", "USD").build());

    // Track without a key — fanned out to every cached assignment + flag of the user
    client.trackEvent("user-123", "page_view", Map.of("page", "/"));
}
```

## Quick Start (Spring Boot)

```properties
# application.properties — the starter registers an ExperimentationClient bean when api-key is set
experimentation.api-key=${EXPERIMENTLY_API_KEY}
experimentation.base-url=http://localhost:8000
```

```java
@Service
public class CheckoutService {
    private final ExperimentationClient client;
    public CheckoutService(ExperimentationClient client) { this.client = client; }

    public String headline(String userId) {
        try {
            var a = client.getExperimentAssignment(User.builder(userId).build(), "checkout_flow");
            return (String) a.getConfiguration().getOrDefault("headline", "Buy now");
        } catch (ExperimentationException e) {
            return "Buy now";
        }
    }
}
```

`@EnableExperimentation` on your application class is optional (auto-configuration is picked up
from `META-INF/spring/...AutoConfiguration.imports`). Declaring your own `ExperimentationClient`
bean suppresses the auto-configured one.

## Configuration

`SdkConfig.builder(apiKey, baseUrl)` — both required and non-empty (`IllegalArgumentException`).

| Builder method | Starter property (`experimentation.*`) | Default | Description |
|---|---|---|---|
| *(constructor)* `apiKey` | `api-key` | — (required) | Sent as `X-API-Key`; the starter is inactive without it |
| *(constructor)* `baseUrl` | `base-url` | starter: `https://api.experimently.example.com` | Backend origin; the SDK appends `/api/v1/...`; trailing `/` stripped |
| `timeoutMs(int)` | `timeout-ms` | `5000` | OkHttp connect/read/write timeout |
| `cacheTtlMs(long)` | `cache-ttl-seconds` | `300000` ms / `300` s | Lifetime of a cached evaluation/assignment |
| `cacheSize(int)` | `cache-size` | `1000` | Max entries per cache (flags and assignments each), LRU |

## API

| Method | Returns | On failure |
|---|---|---|
| `evaluateFeatureFlag(User, flagKey)` | `FlagEvaluation` | throws `ExperimentationException` (`getStatusCode()` = 401/404/…, `0` for network) |
| `isFeatureEnabled(User, flagKey)` | `boolean` | `false` |
| `getExperimentAssignment(User, experimentKey)` | `ExperimentAssignment` (never null) | throws `ExperimentationException` |
| `trackEvent(TrackEvent)` / `trackEvent(userId, name, props)` / `trackEvent(userId, name, props, experimentKey, flagKey, value)` | `void`, async | never throws |
| `trackBatch(List<TrackEvent>)` | `void`, async | never throws; 100 events per request |
| `trackEventSync(TrackEvent)` / `trackBatchSync(List)` | `void`, blocking | throws `ExperimentationException` |
| `getCachedAssignments(userId)` / `getCachedFlagKeys(userId)` | `List<ExperimentAssignment>` / `List<String>` | — |
| `invalidateCache(userId, key)` / `clearCache()` / `getCacheSize()` | — | — |
| `close()` | — | `AutoCloseable`; shuts down the OkHttp pool |
| `ConsistentHash.compute(userId, key)` | `double` in `[0, 1)` | pure (parity utility) |

Model fields: `FlagEvaluation` → `getKey()`, `isEnabled()`, `getConfig()` (`Object`: Map / List /
scalar / null), `getConfigMap()`. `ExperimentAssignment` → `getExperimentKey()`, `getUserId()`,
`getVariantId()`, `getVariantName()`, `isControl()`, `getConfiguration()` (`Map<String,Object>`).
`TrackEvent.builder(userId, eventName)` → `.eventType()` (defaults to the name), `.experimentKey()`,
`.featureFlagKey()`, `.value(Double)`, `.property(k, v)` / `.properties(Map)` (sent as `metadata`),
`.timestamp(Instant)`. `User.builder(userId).attribute(k, v)` — attributes are the assignment `context`.

## Caching and failure behaviour

- Successful evaluations and assignments are cached per **user + key** for `cacheTtlMs`
  (default 5 min); a hit makes no request. **Failures are never cached.**
- Network error, timeout, 401, 404 (an experiment unknown or not ACTIVE, or an unknown flag key), 429: `evaluateFeatureFlag` and
  `getExperimentAssignment` throw `ExperimentationException` (`isApiError()` is true for HTTP
  errors); `isFeatureEnabled` returns `false`. There is no stale fallback beyond the TTL.
- Async `trackEvent`/`trackBatch` swallow every error; use the `*Sync` variants to observe them.

## Tracking fan-out

With `experimentKey` and/or `featureFlagKey` set, `trackEvent` sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch` with one
entry per experiment the user was assigned to through this client (`experiment_key`) plus one per
flag evaluated for the user (`feature_flag_key`), taken from the cache. Nothing cached → nothing is
sent. Metrics match events by **event name**.

## Consistent hash (compatibility utility)

`ConsistentHash.compute(userId, key)` = `MD5("{userId}:{key}")`, first 4 bytes little-endian
uint32 / 2^32 (`0.6927449859213084` for `user-123`/`my-flag`). Kept only for the cross-SDK golden
vectors in `tests/sdk-contract/`; **nothing buckets locally**. `FeatureFlagEvaluator.computeHash`
is a deprecated alias.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluateFeatureFlag`, `isFeatureEnabled` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `getExperimentAssignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `trackEvent` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp?}` | ignored |
| `trackEvent` without keys, `trackBatch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | ignored |

## Contract smoke

```bash
bash sdk/java/examples/contract_smoke.sh
```

It prints:

```text
{"sdk":"java","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

The script runs `./mvnw -q -B -pl core -am package -DskipTests` when `core/target` is stale (Maven output
goes to stderr) and then `java -cp "core/target/classes:core/target/lib/*"
com.getexperimently.sdk.examples.ContractSmoke`; `FORCE_BUILD=1` forces a rebuild. Env:
`EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk java --strict`.

CI runs this SDK's contract smoke against a real API: `java` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Tests

```bash
cd sdk/java && ./mvnw clean test
```

- `cd sdk/java && ./mvnw clean test`: the core module (MockWebServer) and the spring-boot-starter module

`./mvnw` pins Maven 3.9.9 and verifies the download against a SHA-256 in
`.mvn/wrapper/maven-wrapper.properties` (no `maven-wrapper.jar` is committed —
`distributionType=only-script`). The pin matters: `ExperimentationClientTest` is a `@Nested`-heavy
JUnit 5 suite and only Surefire 3.x discovers those, so "whichever Maven the runner ships" must not
be part of the answer.

`clean` matters too. Without it a stale `target/classes` keeps a deleted
`META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports` on the test
classpath, and `ExperimentationStarterRegistrationTest` — the suite that checks the starter is
actually registered rather than hand-instantiated — passes when it should not.

Both run in `.github/workflows/sdk-unit-tests.yml`.

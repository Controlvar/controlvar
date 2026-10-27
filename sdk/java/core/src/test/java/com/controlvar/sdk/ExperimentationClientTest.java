package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.TrackEvent;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.OkHttpClient;
import okhttp3.mockwebserver.Dispatcher;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Tests for {@link ExperimentationClient} against OkHttp's {@link MockWebServer}.
 *
 * <p>They assert the exact requests the SDK sends to the backend contract
 * ({@code POST /api/v1/tracking/assign}, {@code GET /api/v1/feature-flags/evaluate/{key}},
 * {@code POST /api/v1/tracking/track}, {@code POST /api/v1/tracking/batch}), how the
 * responses are mapped, and the cache / fan-out / never-throw behaviour.
 */
@DisplayName("ExperimentationClient")
class ExperimentationClientTest {

    private static final String ASSIGN_JSON = "{"
            + "\"experiment_key\":\"checkout_flow\","
            + "\"user_id\":\"user-1\","
            + "\"variant_id\":\"8b6f1c2e-0000-4000-8000-000000000001\","
            + "\"variant_name\":\"treatment\","
            + "\"is_control\":false,"
            + "\"configuration\":{\"headline\":\"Buy now\",\"discount\":10}"
            + "}";

    private MockWebServer mockServer;
    private ExperimentationClient client;
    private final ObjectMapper objectMapper = new ObjectMapper();

    @BeforeEach
    void setUp() throws IOException {
        mockServer = new MockWebServer();
        mockServer.start();
        client = newClient(60_000L);
    }

    @AfterEach
    void tearDown() throws IOException {
        client.close();
        mockServer.shutdown();
    }

    // =========================================================================
    // Helpers
    // =========================================================================

    private ExperimentationClient newClient(long cacheTtlMs) {
        SdkConfig config = SdkConfig.builder("test-api-key", mockServer.url("/").toString())
                .timeoutMs(5000)
                .cacheSize(100)
                .cacheTtlMs(cacheTtlMs)
                .build();
        OkHttpClient httpClient = new OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(5, TimeUnit.SECONDS)
                .build();
        return new ExperimentationClient(config, httpClient);
    }

    private static MockResponse json(int code, String body) {
        return new MockResponse()
                .setResponseCode(code)
                .setHeader("Content-Type", "application/json")
                .setBody(body);
    }

    private static MockResponse flagResponse(String key, boolean enabled, String configJson) {
        return json(200, "{\"key\":\"" + key + "\",\"enabled\":" + enabled + ",\"config\":" + configJson + "}");
    }

    private static MockResponse trackOk() {
        return json(200, "{\"id\":\"evt-1\"}");
    }

    private static MockResponse batchOk() {
        return json(200, "{\"success_count\":1,\"failure_count\":0,\"errors\":null}");
    }

    private RecordedRequest takeRequest() throws InterruptedException {
        RecordedRequest request = mockServer.takeRequest(3, TimeUnit.SECONDS);
        assertNotNull(request, "expected a request to reach the server");
        return request;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> bodyOf(RecordedRequest request) throws IOException {
        return objectMapper.readValue(request.getBody().readUtf8(), Map.class);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> events(Map<String, Object> batchBody) {
        return (List<Map<String, Object>>) batchBody.get("events");
    }

    /** Routes by path so async / multi-request scenarios do not depend on enqueue order. */
    private void routeByPath() {
        mockServer.setDispatcher(new Dispatcher() {
            @Override
            public MockResponse dispatch(RecordedRequest request) {
                String path = request.getPath();
                if (path == null) return new MockResponse().setResponseCode(404);
                if (path.equals("/api/v1/tracking/assign")) return json(200, ASSIGN_JSON);
                if (path.startsWith("/api/v1/feature-flags/evaluate/")) {
                    String key = path.substring("/api/v1/feature-flags/evaluate/".length());
                    int q = key.indexOf('?');
                    if (q >= 0) key = key.substring(0, q);
                    return flagResponse(key, true, "null");
                }
                if (path.equals("/api/v1/tracking/track")) return trackOk();
                if (path.equals("/api/v1/tracking/batch")) return batchOk();
                return new MockResponse().setResponseCode(404).setBody("{\"detail\":\"no route\"}");
            }
        });
    }

    // =========================================================================
    // Headers
    // =========================================================================

    @Test
    @DisplayName("every request carries X-API-Key, Accept and Content-Type")
    void requestHeaders() throws Exception {
        routeByPath();
        User user = User.builder("user-1").build();

        client.evaluateFeatureFlag(user, "f");
        client.getExperimentAssignment(user, "checkout_flow");
        client.trackEventSync(TrackEvent.builder("user-1", "click").experimentKey("checkout_flow").build());

        for (int i = 0; i < 3; i++) {
            RecordedRequest request = takeRequest();
            assertEquals("test-api-key", request.getHeader("X-API-Key"), request.getPath());
            assertEquals("application/json", request.getHeader("Accept"), request.getPath());
            assertTrue(request.getHeader("Content-Type").startsWith("application/json"), request.getPath());
        }
    }

    // =========================================================================
    // evaluateFeatureFlag
    // =========================================================================

    @Nested
    @DisplayName("evaluateFeatureFlag")
    class EvaluateFeatureFlag {

        @Test
        @DisplayName("sends GET /api/v1/feature-flags/evaluate/{key}?user_id= with the key and user encoded")
        void requestAndMapping() throws Exception {
            mockServer.enqueue(flagResponse("new search/v2", true, "{\"variant\":\"blue\",\"limit\":3}"));

            FlagEvaluation result = client.evaluateFeatureFlag(User.builder("user 1&2").build(), "new search/v2");

            RecordedRequest request = takeRequest();
            assertEquals("GET", request.getMethod());
            assertEquals("/api/v1/feature-flags/evaluate/new%20search%2Fv2?user_id=user%201%262", request.getPath());
            assertEquals("user 1&2", request.getRequestUrl().queryParameter("user_id"));
            assertEquals(0, request.getBodySize(), "GET must have no body");

            assertEquals("new search/v2", result.getKey());
            assertTrue(result.isEnabled());
            assertNotNull(result.getConfigMap());
            assertEquals("blue", result.getConfigMap().get("variant"));
            assertEquals(3, result.getConfigMap().get("limit"));
        }

        @Test
        @DisplayName("maps a disabled flag with null config")
        void disabledFlag() {
            mockServer.enqueue(flagResponse("off-flag", false, "null"));

            FlagEvaluation result = client.evaluateFeatureFlag(User.builder("user-1").build(), "off-flag");

            assertFalse(result.isEnabled());
            assertNull(result.getConfig());
            assertNull(result.getConfigMap());
        }

        @Test
        @DisplayName("keeps a non-object config as the raw JSON value")
        void nonObjectConfig() {
            mockServer.enqueue(flagResponse("string-config", true, "\"just-a-string\""));

            FlagEvaluation result = client.evaluateFeatureFlag(User.builder("user-1").build(), "string-config");

            assertTrue(result.isEnabled());
            assertEquals("just-a-string", result.getConfig());
            assertNull(result.getConfigMap());
        }

        @Test
        @DisplayName("falls back to the requested key when the response omits it")
        void fillsMissingKey() {
            mockServer.enqueue(json(200, "{\"enabled\":true}"));

            FlagEvaluation result = client.evaluateFeatureFlag(User.builder("user-1").build(), "no-key-in-body");

            assertEquals("no-key-in-body", result.getKey());
        }

        @Test
        @DisplayName("serves repeated calls from the cache (one HTTP request)")
        void cacheHit() {
            mockServer.enqueue(flagResponse("cached-flag", true, "null"));
            User user = User.builder("user-cache").build();

            for (int i = 0; i < 3; i++) {
                assertTrue(client.evaluateFeatureFlag(user, "cached-flag").isEnabled());
            }

            assertEquals(1, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("caches per user + key")
        void cacheIsPerUserAndKey() {
            routeByPath();

            client.evaluateFeatureFlag(User.builder("u1").build(), "f");
            client.evaluateFeatureFlag(User.builder("u2").build(), "f");
            client.evaluateFeatureFlag(User.builder("u1").build(), "g");
            client.evaluateFeatureFlag(User.builder("u1").build(), "f");

            assertEquals(3, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("re-fetches after the cache TTL expires")
        void cacheTtlExpiry() throws Exception {
            routeByPath();
            try (ExperimentationClient shortTtl = newClient(50L)) {
                User user = User.builder("user-ttl").build();
                shortTtl.evaluateFeatureFlag(user, "ttl-flag");
                shortTtl.evaluateFeatureFlag(user, "ttl-flag");
                Thread.sleep(100L);
                shortTtl.evaluateFeatureFlag(user, "ttl-flag");
            }

            assertEquals(2, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("throws ExperimentationException(404) when the flag is unknown or not ACTIVE")
        void notFound() {
            mockServer.enqueue(json(404, "{\"detail\":\"Feature flag not found\"}"));

            ExperimentationException ex = assertThrows(ExperimentationException.class,
                    () -> client.evaluateFeatureFlag(User.builder("user-1").build(), "missing-flag"));

            assertEquals(404, ex.getStatusCode());
            assertTrue(ex.isApiError());
        }

        @Test
        @DisplayName("throws ExperimentationException(401) for a bad API key")
        void unauthorized() {
            mockServer.enqueue(json(401, "{\"detail\":\"Invalid API key\"}"));

            ExperimentationException ex = assertThrows(ExperimentationException.class,
                    () -> client.evaluateFeatureFlag(User.builder("user-1").build(), "f"));

            assertEquals(401, ex.getStatusCode());
        }

        @Test
        @DisplayName("never caches failures: the next call hits the server again")
        void failureNotCached() {
            mockServer.enqueue(json(500, "{\"detail\":\"boom\"}"));
            mockServer.enqueue(flagResponse("flaky", true, "null"));
            User user = User.builder("user-1").build();

            assertThrows(ExperimentationException.class, () -> client.evaluateFeatureFlag(user, "flaky"));
            assertTrue(client.evaluateFeatureFlag(user, "flaky").isEnabled());

            assertEquals(2, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("wraps network errors in ExperimentationException")
        void networkError() {
            SdkConfig config = SdkConfig.builder("k", "http://127.0.0.1:1").timeoutMs(500).build();
            try (ExperimentationClient unreachable = new ExperimentationClient(config)) {
                ExperimentationException ex = assertThrows(ExperimentationException.class,
                        () -> unreachable.evaluateFeatureFlag(User.builder("u").build(), "f"));
                assertFalse(ex.isApiError());
            }
        }

        @Test
        @DisplayName("rejects a null user or an empty key")
        void validation() {
            assertThrows(IllegalArgumentException.class, () -> client.evaluateFeatureFlag(null, "f"));
            assertThrows(IllegalArgumentException.class,
                    () -> client.evaluateFeatureFlag(User.builder("u").build(), ""));
            assertThrows(IllegalArgumentException.class,
                    () -> client.evaluateFeatureFlag(User.builder("u").build(), null));
        }
    }

    // =========================================================================
    // isFeatureEnabled
    // =========================================================================

    @Nested
    @DisplayName("isFeatureEnabled")
    class IsFeatureEnabled {

        @Test
        @DisplayName("returns the server decision")
        void returnsServerDecision() {
            mockServer.enqueue(flagResponse("on-flag", true, "null"));
            mockServer.enqueue(flagResponse("off-flag", false, "null"));
            User user = User.builder("user-1").build();

            assertTrue(client.isFeatureEnabled(user, "on-flag"));
            assertFalse(client.isFeatureEnabled(user, "off-flag"));
        }

        @Test
        @DisplayName("returns false instead of throwing on 404")
        void falseOnNotFound() {
            mockServer.enqueue(json(404, "{\"detail\":\"not found\"}"));

            assertFalse(client.isFeatureEnabled(User.builder("user-1").build(), "missing"));
        }

        @Test
        @DisplayName("returns false instead of throwing when the server is unreachable")
        void falseOnNetworkError() {
            SdkConfig config = SdkConfig.builder("k", "http://127.0.0.1:1").timeoutMs(500).build();
            try (ExperimentationClient unreachable = new ExperimentationClient(config)) {
                assertFalse(unreachable.isFeatureEnabled(User.builder("u").build(), "f"));
            }
        }
    }

    // =========================================================================
    // getExperimentAssignment
    // =========================================================================

    @Nested
    @DisplayName("getExperimentAssignment")
    class GetExperimentAssignment {

        @Test
        @DisplayName("sends POST /api/v1/tracking/assign with experiment_key, user_id and context")
        void requestAndMapping() throws Exception {
            mockServer.enqueue(json(200, ASSIGN_JSON));
            User user = User.builder("user-1").attribute("plan", "pro").attribute("country", "US").build();

            ExperimentAssignment result = client.getExperimentAssignment(user, "checkout_flow");

            RecordedRequest request = takeRequest();
            assertEquals("POST", request.getMethod());
            assertEquals("/api/v1/tracking/assign", request.getPath());
            Map<String, Object> body = bodyOf(request);
            assertEquals("checkout_flow", body.get("experiment_key"));
            assertEquals("user-1", body.get("user_id"));
            @SuppressWarnings("unchecked")
            Map<String, Object> context = (Map<String, Object>) body.get("context");
            assertEquals("pro", context.get("plan"));
            assertEquals("US", context.get("country"));
            assertFalse(body.containsKey("attributes"), "legacy 'attributes' field must not be sent");

            assertEquals("checkout_flow", result.getExperimentKey());
            assertEquals("user-1", result.getUserId());
            assertEquals("8b6f1c2e-0000-4000-8000-000000000001", result.getVariantId());
            assertEquals("treatment", result.getVariantName());
            assertFalse(result.isControl());
            assertEquals("Buy now", result.getConfiguration().get("headline"));
            assertEquals(10, result.getConfiguration().get("discount"));
        }

        @Test
        @DisplayName("omits context when the user has no attributes")
        void omitsEmptyContext() throws Exception {
            mockServer.enqueue(json(200, ASSIGN_JSON));

            client.getExperimentAssignment(User.builder("user-1").build(), "checkout_flow");

            assertFalse(bodyOf(takeRequest()).containsKey("context"));
        }

        @Test
        @DisplayName("maps a control assignment with null configuration")
        void controlAssignment() {
            mockServer.enqueue(json(200, "{\"experiment_key\":\"e\",\"user_id\":\"u\",\"variant_id\":\"v-0\","
                    + "\"variant_name\":\"control\",\"is_control\":true,\"configuration\":null}"));

            ExperimentAssignment result = client.getExperimentAssignment(User.builder("u").build(), "e");

            assertEquals("control", result.getVariantName());
            assertTrue(result.isControl());
            assertNull(result.getConfiguration());
        }

        @Test
        @DisplayName("is sticky: the second call is served from the cache")
        void stickyCacheHit() {
            mockServer.enqueue(json(200, ASSIGN_JSON));
            User user = User.builder("user-1").build();

            ExperimentAssignment first = client.getExperimentAssignment(user, "checkout_flow");
            ExperimentAssignment second = client.getExperimentAssignment(user, "checkout_flow");

            assertEquals(first, second);
            assertEquals(1, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("throws ExperimentationException(404) when no experiment has the key, and does not cache it")
        void notFoundNotCached() {
            mockServer.enqueue(json(404, "{\"detail\":\"No experiment has the key 'nope'.\"}"));
            mockServer.enqueue(json(404, "{\"detail\":\"No experiment has the key 'nope'.\"}"));
            User user = User.builder("u1").build();

            ExperimentationException ex = assertThrows(ExperimentationException.class,
                    () -> client.getExperimentAssignment(user, "nope"));
            assertEquals(404, ex.getStatusCode());

            assertThrows(ExperimentationException.class, () -> client.getExperimentAssignment(user, "nope"));
            assertEquals(2, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("rejects a null user or an empty key")
        void validation() {
            assertThrows(IllegalArgumentException.class, () -> client.getExperimentAssignment(null, "e"));
            assertThrows(IllegalArgumentException.class,
                    () -> client.getExperimentAssignment(User.builder("u").build(), ""));
        }
    }

    // =========================================================================
    // trackEvent
    // =========================================================================

    @Nested
    @DisplayName("trackEvent")
    class Track {

        @Test
        @DisplayName("with an experiment key sends one POST /api/v1/tracking/track")
        void withExperimentKey() throws Exception {
            mockServer.enqueue(trackOk());
            Map<String, Object> props = new HashMap<>();
            props.put("sku", "pro");

            client.trackEvent("user-track", "purchase", props, "checkout_flow", null, 12.5);

            RecordedRequest request = takeRequest();
            assertEquals("POST", request.getMethod());
            assertEquals("/api/v1/tracking/track", request.getPath());
            Map<String, Object> body = bodyOf(request);
            assertEquals("purchase", body.get("event_type"));
            assertEquals("purchase", body.get("event_name"));
            assertEquals("user-track", body.get("user_id"));
            assertEquals("checkout_flow", body.get("experiment_key"));
            assertEquals(12.5, body.get("value"));
            @SuppressWarnings("unchecked")
            Map<String, Object> metadata = (Map<String, Object>) body.get("metadata");
            assertEquals("pro", metadata.get("sku"));
            assertFalse(body.containsKey("feature_flag_key"), "feature_flag_key must be omitted when unset");
            assertFalse(body.containsKey("properties"), "legacy 'properties' field must not be sent");
            assertFalse(body.containsKey("timestamp"), "timestamp must be omitted when unset");
        }

        @Test
        @DisplayName("with a feature flag key sends event_type override and ISO-8601 timestamp")
        void withFeatureFlagKey() throws Exception {
            mockServer.enqueue(trackOk());

            client.trackEvent(TrackEvent.builder("u1", "search")
                    .eventType("interaction")
                    .featureFlagKey("new_search")
                    .timestamp(Instant.parse("2026-09-11T10:30:00Z"))
                    .build());

            Map<String, Object> body = bodyOf(takeRequest());
            assertEquals("interaction", body.get("event_type"));
            assertEquals("search", body.get("event_name"));
            assertEquals("new_search", body.get("feature_flag_key"));
            assertEquals("2026-09-11T10:30:00Z", body.get("timestamp"));
            for (String absent : Arrays.asList("experiment_key", "value", "metadata")) {
                assertFalse(body.containsKey(absent), absent + " must be omitted when unset");
            }
        }

        @Test
        @DisplayName("without a key fans out via POST /api/v1/tracking/batch to the cached assignment and flag")
        void fanOut() throws Exception {
            routeByPath();
            User user = User.builder("user-1").build();
            client.getExperimentAssignment(user, "checkout_flow");
            client.evaluateFeatureFlag(user, "new_search");
            // Another user's cache entries must not leak into this user's fan-out.
            client.evaluateFeatureFlag(User.builder("user-2").build(), "new_search");
            for (int i = 0; i < 3; i++) takeRequest();

            Map<String, Object> props = new HashMap<>();
            props.put("page", "/home");
            client.trackEvent("user-1", "page_view", props);

            RecordedRequest request = takeRequest();
            assertEquals("POST", request.getMethod());
            assertEquals("/api/v1/tracking/batch", request.getPath());
            List<Map<String, Object>> entries = events(bodyOf(request));
            assertEquals(2, entries.size(), "one entry per cached assignment + one per cached flag");
            assertEquals("checkout_flow", entries.get(0).get("experiment_key"));
            assertFalse(entries.get(0).containsKey("feature_flag_key"));
            assertEquals("new_search", entries.get(1).get("feature_flag_key"));
            assertFalse(entries.get(1).containsKey("experiment_key"));
            for (Map<String, Object> entry : entries) {
                assertEquals("page_view", entry.get("event_type"));
                assertEquals("page_view", entry.get("event_name"));
                assertEquals("user-1", entry.get("user_id"));
                @SuppressWarnings("unchecked")
                Map<String, Object> metadata = (Map<String, Object>) entry.get("metadata");
                assertEquals("/home", metadata.get("page"));
            }
            assertEquals(4, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("without a key and nothing cached sends nothing")
        void nothingCachedSendsNothing() throws Exception {
            routeByPath();

            client.trackEvent("nobody", "page_view", null);
            client.trackEventSync(TrackEvent.builder("nobody", "page_view").build());
            Thread.sleep(150L);

            assertEquals(0, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("never throws: server error, unreachable server, invalid input")
        void neverThrows() throws Exception {
            mockServer.enqueue(json(500, "{\"detail\":\"server error\"}"));
            assertDoesNotThrow(() -> client.trackEvent("u1", "click", null, "e", null, null));
            takeRequest();

            assertDoesNotThrow(() -> client.trackEvent("", "click", null));
            assertDoesNotThrow(() -> client.trackEvent("u1", "", null));
            assertDoesNotThrow(() -> client.trackEvent(null, null, null));
            assertDoesNotThrow(() -> client.trackEvent((TrackEvent) null));
            assertDoesNotThrow(() -> client.trackBatch(null));
            assertDoesNotThrow(() -> client.trackBatch(Collections.emptyList()));
            assertDoesNotThrow(() -> client.trackBatch(Collections.singletonList(null)));
            Thread.sleep(100L);
            assertEquals(1, mockServer.getRequestCount(), "invalid events must not produce requests");

            SdkConfig config = SdkConfig.builder("k", "http://127.0.0.1:1").timeoutMs(500).build();
            try (ExperimentationClient unreachable = new ExperimentationClient(config)) {
                assertDoesNotThrow(() -> unreachable.trackEvent("u1", "click", null, "e", null, null));
                assertDoesNotThrow(() -> unreachable.trackBatch(Collections.singletonList(
                        TrackEvent.builder("u1", "click").experimentKey("e").build())));
            }
        }

        @Test
        @DisplayName("trackEventSync reports failures as ExperimentationException")
        void syncReportsFailures() {
            mockServer.enqueue(json(500, "{\"detail\":\"server error\"}"));

            ExperimentationException ex = assertThrows(ExperimentationException.class,
                    () -> client.trackEventSync(TrackEvent.builder("u1", "click").experimentKey("e").build()));
            assertEquals(500, ex.getStatusCode());
        }
    }

    // =========================================================================
    // trackBatch
    // =========================================================================

    @Nested
    @DisplayName("trackBatch")
    class TrackBatch {

        @Test
        @DisplayName("sends keyed events in one POST /api/v1/tracking/batch")
        void batchesKeyedEvents() throws Exception {
            mockServer.enqueue(batchOk());

            client.trackBatch(Arrays.asList(
                    TrackEvent.builder("u1", "page_view").experimentKey("e").build(),
                    TrackEvent.builder("u1", "page_view").featureFlagKey("f").build()));

            RecordedRequest request = takeRequest();
            assertEquals("/api/v1/tracking/batch", request.getPath());
            List<Map<String, Object>> entries = events(bodyOf(request));
            assertEquals(2, entries.size());
            assertEquals("e", entries.get(0).get("experiment_key"));
            assertEquals("f", entries.get(1).get("feature_flag_key"));
        }

        @Test
        @DisplayName("chunks at 100 events per request")
        void chunksAt100() throws Exception {
            routeByPath();
            List<TrackEvent> batch = new ArrayList<>();
            for (int i = 0; i < 150; i++) {
                batch.add(TrackEvent.builder("u" + i, "click").experimentKey("e").build());
            }

            client.trackBatchSync(batch);

            List<Integer> sizes = new ArrayList<>();
            for (int i = 0; i < 2; i++) {
                RecordedRequest request = takeRequest();
                assertEquals("/api/v1/tracking/batch", request.getPath());
                sizes.add(events(bodyOf(request)).size());
            }
            assertEquals(Arrays.asList(100, 50), sizes);
            assertEquals(2, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("expands unkeyed events from the cache and drops those with nothing cached")
        void expandsUnkeyedEvents() throws Exception {
            routeByPath();
            client.getExperimentAssignment(User.builder("user-1").build(), "checkout_flow");
            takeRequest();

            client.trackBatchSync(Arrays.asList(
                    TrackEvent.builder("user-1", "purchase").featureFlagKey("f").build(),
                    TrackEvent.builder("user-1", "page_view").build(),   // fans out to checkout_flow
                    TrackEvent.builder("user-9", "page_view").build())); // nothing cached -> dropped

            List<Map<String, Object>> entries = events(bodyOf(takeRequest()));
            assertEquals(2, entries.size());
            assertEquals("f", entries.get(0).get("feature_flag_key"));
            assertEquals("checkout_flow", entries.get(1).get("experiment_key"));
            assertEquals("page_view", entries.get(1).get("event_name"));
        }
    }

    // =========================================================================
    // Cache access
    // =========================================================================

    @Nested
    @DisplayName("cache")
    class CacheAccess {

        @Test
        @DisplayName("getCachedAssignments and getCachedFlagKeys list the user's live entries")
        void cachedEntries() {
            routeByPath();
            User user = User.builder("user-1").build();
            client.getExperimentAssignment(user, "checkout_flow");
            client.evaluateFeatureFlag(user, "new_search");
            client.evaluateFeatureFlag(User.builder("user-10").build(), "other");

            List<ExperimentAssignment> assignments = client.getCachedAssignments("user-1");
            assertEquals(1, assignments.size());
            assertEquals("checkout_flow", assignments.get(0).getExperimentKey());
            assertEquals(Collections.singletonList("new_search"), client.getCachedFlagKeys("user-1"));
            assertTrue(client.getCachedAssignments("user-10").isEmpty());
            assertEquals(Collections.singletonList("other"), client.getCachedFlagKeys("user-10"));
            assertTrue(client.getCachedFlagKeys("user-1x").isEmpty(), "prefix must not match a longer user id");
        }

        @Test
        @DisplayName("invalidateCache forces a fresh request for that user + key only")
        void invalidateCache() {
            routeByPath();
            User user = User.builder("user-1").build();
            client.evaluateFeatureFlag(user, "f");
            client.getExperimentAssignment(user, "checkout_flow");
            assertEquals(2, mockServer.getRequestCount());

            client.invalidateCache("user-1", "f");
            client.evaluateFeatureFlag(user, "f");
            client.getExperimentAssignment(user, "checkout_flow");

            assertEquals(3, mockServer.getRequestCount());
        }

        @Test
        @DisplayName("clearCache drops evaluations and assignments")
        void clearCache() {
            routeByPath();
            User user = User.builder("user-1").build();
            client.evaluateFeatureFlag(user, "f");
            client.getExperimentAssignment(user, "checkout_flow");
            assertEquals(2, client.getCacheSize());

            client.clearCache();

            assertEquals(0, client.getCacheSize());
            client.evaluateFeatureFlag(user, "f");
            client.getExperimentAssignment(user, "checkout_flow");
            assertEquals(4, mockServer.getRequestCount());
        }
    }

    // =========================================================================
    // Thread safety
    // =========================================================================

    @Test
    @DisplayName("concurrent evaluate / assign / track calls are safe")
    void concurrentUse() throws Exception {
        routeByPath();
        List<Thread> threads = new ArrayList<>();
        List<Throwable> failures = Collections.synchronizedList(new ArrayList<>());
        for (int t = 0; t < 8; t++) {
            final int id = t;
            Thread thread = new Thread(() -> {
                try {
                    for (int i = 0; i < 20; i++) {
                        User user = User.builder("cuser-" + ((id * 20 + i) % 5)).build();
                        client.evaluateFeatureFlag(user, "race-flag");
                        client.getExperimentAssignment(user, "checkout_flow");
                        client.trackEvent(user.getUserId(), "page_view", null);
                        client.trackEvent(user.getUserId(), "purchase", null, "checkout_flow", null, 1.0);
                    }
                } catch (Throwable e) {
                    failures.add(e);
                }
            });
            threads.add(thread);
            thread.start();
        }
        for (Thread thread : threads) thread.join(10_000L);

        assertTrue(failures.isEmpty(), "failures: " + failures);
    }
}

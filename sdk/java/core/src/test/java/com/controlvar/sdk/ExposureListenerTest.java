package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.Exposure;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.OkHttpClient;
import okhttp3.mockwebserver.Dispatcher;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import okhttp3.mockwebserver.SocketPolicy;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.*;

/**
 * The exposure listener (#1499): called once for each {@code POST /api/v1/tracking/assign} the
 * server answered with {@code "assigned": true} and the client stored into a slot with no
 * unexpired answer, and never for a cached answer, the second of two concurrent first calls,
 * a failed call, an {@code "assigned": false} answer, an answer without {@code assigned} or a
 * flag. Nothing it throws, other than a {@link VirtualMachineError}, reaches the caller.
 */
@DisplayName("ExposureListener")
class ExposureListenerTest {

    /** The server's answer for an enrolled user; field names copied from VariantAssignmentResponse. */
    private static final String ASSIGNED = "{"
            + "\"experiment_key\":\"exp-1\","
            + "\"user_id\":\"u-1\","
            + "\"variant_id\":\"v-2\","
            + "\"variant_name\":\"treatment\","
            + "\"is_control\":false,"
            + "\"configuration\":{\"headline\":\"Buy now\"},"
            + "\"assigned\":true,"
            + "\"reason\":\"assigned\""
            + "}";

    private static final Exposure EXPECTED = new Exposure("exp-1", "u-1", "v-2", "treatment");

    private final ObjectMapper objectMapper = new ObjectMapper();
    private MockWebServer server;
    private final List<Exposure> calls = Collections.synchronizedList(new ArrayList<>());
    private final ExposureListener recording = calls::add;

    @BeforeEach
    void setUp() throws IOException {
        server = new MockWebServer();
        server.start();
    }

    @AfterEach
    void tearDown() throws IOException {
        server.shutdown();
    }

    // =========================================================================
    // Helpers
    // =========================================================================

    private ExperimentationClient client(ExposureListener listener) {
        return client(listener, 60_000L, 100, 5_000L);
    }

    private ExperimentationClient client(ExposureListener listener, long cacheTtlMs, int cacheSize,
                                         long readTimeoutMs) {
        SdkConfig config = SdkConfig.builder("test-api-key", server.url("/").toString())
                .cacheTtlMs(cacheTtlMs)
                .cacheSize(cacheSize)
                .exposureListener(listener)
                .build();
        OkHttpClient http = new OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(readTimeoutMs, TimeUnit.MILLISECONDS)
                .build();
        return new ExperimentationClient(config, http);
    }

    private static MockResponse json(int code, String body) {
        return new MockResponse().setResponseCode(code)
                .setHeader("Content-Type", "application/json").setBody(body);
    }

    /** Answers every /assign with ASSIGNED, but with the experiment and user the request named. */
    private void echoAssigned() {
        server.setDispatcher(new Dispatcher() {
            @Override
            public MockResponse dispatch(RecordedRequest request) {
                return json(200, assignedFor(request));
            }
        });
    }

    @SuppressWarnings("unchecked")
    private String assignedFor(RecordedRequest request) {
        try {
            Map<String, Object> body = objectMapper.readValue(request.getBody().readUtf8(), Map.class);
            return ASSIGNED.replace("\"exp-1\"", "\"" + body.get("experiment_key") + "\"")
                    .replace("\"u-1\"", "\"" + body.get("user_id") + "\"");
        } catch (IOException e) {
            throw new AssertionError(e);
        }
    }

    private static User user(String id) {
        return User.builder(id).build();
    }

    @SuppressWarnings("unchecked")
    private static <T extends Throwable> void sneakyThrow(Throwable t) throws T {
        throw (T) t;
    }

    // =========================================================================
    // Once per server-recorded exposure
    // =========================================================================

    @Nested
    @DisplayName("once per exposure the server recorded")
    class Once {

        @Test
        @DisplayName("is called once, with exactly the experiment key, user id, variant id and variant name, for an assigned: true answer")
        void calledOnceWithTheFourValues() {
            server.enqueue(json(200, ASSIGNED));
            try (ExperimentationClient client = client(recording)) {
                ExperimentAssignment answer = client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals("treatment", answer.getVariantName());
                assertEquals(Collections.singletonList(EXPECTED), calls);
                assertEquals(1, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("passes a null variant id when the server sends none")
        void variantIdIsNullWhenTheServerSendsNone() {
            server.enqueue(json(200, ASSIGNED.replace("\"variant_id\":\"v-2\",", "")));
            try (ExperimentationClient client = client(recording)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(Collections.singletonList(new Exposure("exp-1", "u-1", null, "treatment")), calls);
            }
        }

        @Test
        @DisplayName("a cached answer calls nothing: however often the answer is asked for, one request and one call")
        void aCachedAnswerCallsNothing() {
            echoAssigned();
            try (ExperimentationClient client = client(recording)) {
                for (int i = 0; i < 5; i++) {
                    assertEquals("treatment", client.getExperimentAssignment(user("u-1"), "exp-1").getVariantName());
                }

                assertEquals(1, server.getRequestCount());
                assertEquals(1, calls.size());
            }
        }

        @Test
        @DisplayName("is called once per user and experiment: another user or another experiment is another request and another call")
        void oncePerUserAndExperiment() {
            echoAssigned();
            try (ExperimentationClient client = client(recording)) {
                for (int round = 0; round < 2; round++) {
                    client.getExperimentAssignment(user("u-1"), "exp-1");
                    client.getExperimentAssignment(user("u-2"), "exp-1");
                    client.getExperimentAssignment(user("u-1"), "exp-2");
                }

                assertEquals(3, server.getRequestCount());
                assertEquals(Arrays.asList(
                        new Exposure("exp-1", "u-1", "v-2", "treatment"),
                        new Exposure("exp-1", "u-2", "v-2", "treatment"),
                        new Exposure("exp-2", "u-1", "v-2", "treatment")), calls);
            }
        }

        @Test
        @DisplayName("two threads asking for the same new user and experiment at once make two requests and one call")
        void concurrentFirstCallsMakeTwoRequestsAndOneCall() throws Exception {
            // The barrier holds each request until both have reached the server, so both calls
            // miss the cache. Two requests is the positive control: without it the second call
            // would be a cache hit and this test would prove nothing. A request still alone
            // after 5 s gets a 500, which fails the test loudly instead of hanging it.
            CountDownLatch bothArrived = new CountDownLatch(2);
            server.setDispatcher(new Dispatcher() {
                @Override
                public MockResponse dispatch(RecordedRequest request) throws InterruptedException {
                    bothArrived.countDown();
                    if (!bothArrived.await(5, TimeUnit.SECONDS)) {
                        return json(500, "{\"detail\":\"the other request never arrived\"}");
                    }
                    return json(200, ASSIGNED);
                }
            });
            ExecutorService pool = Executors.newFixedThreadPool(2);
            try (ExperimentationClient client = client(recording)) {
                Future<ExperimentAssignment> a = pool.submit(() -> client.getExperimentAssignment(user("u-1"), "exp-1"));
                Future<ExperimentAssignment> b = pool.submit(() -> client.getExperimentAssignment(user("u-1"), "exp-1"));

                assertEquals("treatment", a.get(10, TimeUnit.SECONDS).getVariantName());
                assertEquals("treatment", b.get(10, TimeUnit.SECONDS).getVariantName());
                assertEquals(2, server.getRequestCount(), "both calls must have missed the cache");
                assertEquals(Collections.singletonList(EXPECTED), calls);
            } finally {
                pool.shutdownNow();
            }
        }

        @Test
        @DisplayName("each call gets a new Exposure, not the cached assignment")
        void eachCallGetsANewExposure() {
            echoAssigned();
            try (ExperimentationClient client = client(recording)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");
                client.clearCache();
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(2, calls.size());
                assertEquals(calls.get(0), calls.get(1));
                assertNotSame(calls.get(0), calls.get(1));
            }
        }
    }

    // =========================================================================
    // Not called
    // =========================================================================

    @Nested
    @DisplayName("not called")
    class NotCalled {

        @ParameterizedTest(name = "is not called for assigned: false ({0}): the control answer comes back unchanged")
        @ValueSource(strings = {"holdout", "mutual_exclusion", "targeting", "draft", "paused", "completed", "archived"})
        void notCalledForAssignedFalse(String reason) {
            server.enqueue(json(200, "{\"experiment_key\":\"exp-1\",\"user_id\":\"u-1\",\"variant_id\":\"v-1\","
                    + "\"variant_name\":\"control\",\"is_control\":true,\"configuration\":null,"
                    + "\"assigned\":false,\"reason\":\"" + reason + "\"}"));
            try (ExperimentationClient client = client(recording)) {
                ExperimentAssignment answer = client.getExperimentAssignment(user("u-1"), "exp-1");
                ExperimentAssignment again = client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals("control", answer.getVariantName());
                assertTrue(answer.isControl());
                assertSame(answer, again);
                assertEquals(1, server.getRequestCount());
                assertEquals(0, calls.size());
            }
        }

        @ParameterizedTest(name = "is not called when assigned is {0}: the answer is returned")
        @ValueSource(strings = {"missing", "null", "the string \"true\"", "the number 1"})
        void notCalledWhenAssignedIsNotTheLiteralTrue(String shape) {
            String withoutIt = ASSIGNED.replace(",\"assigned\":true,\"reason\":\"assigned\"", "");
            String body;
            switch (shape) {
                case "missing": body = withoutIt; break;
                case "null": body = withoutIt.replace("}}", "},\"assigned\":null}"); break;
                case "the string \"true\"": body = withoutIt.replace("}}", "},\"assigned\":\"true\"}"); break;
                default: body = withoutIt.replace("}}", "},\"assigned\":1}"); break;
            }
            assertNotEquals(ASSIGNED, body, "the fixture must have changed");
            server.enqueue(json(200, body));
            try (ExperimentationClient client = client(recording)) {
                ExperimentAssignment answer = client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals("treatment", answer.getVariantName());
                assertEquals(0, calls.size());
            }
        }

        @ParameterizedTest(name = "a failed call calls nothing: {0}")
        @ValueSource(strings = {"404", "500", "network error", "invalid JSON", "empty body"})
        void aFailedCallCallsNothing(String failure) {
            MockResponse response;
            switch (failure) {
                case "404": response = json(404, "{\"detail\":\"No experiment has the key 'exp-1'.\"}"); break;
                case "500": response = json(500, "{\"detail\":\"boom\"}"); break;
                // every attempt, OkHttp's retry included, is cut before an answer
                case "network error": response = new MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START); break;
                case "invalid JSON": response = json(200, "{\"assigned\":true,"); break;
                default: response = json(200, ""); break;
            }
            server.setDispatcher(new Dispatcher() {
                @Override
                public MockResponse dispatch(RecordedRequest request) {
                    return response;
                }
            });
            try (ExperimentationClient client = client(recording)) {
                assertThrows(ExperimentationException.class,
                        () -> client.getExperimentAssignment(user("u-1"), "exp-1"));

                assertEquals(0, calls.size());
                assertEquals(0, client.getCacheSize(), "a failure is not cached");
            }
        }

        @Test
        @DisplayName("a call that times out calls nothing, even though the server recorded the exposure")
        void aTimedOutCallCallsNothing() {
            server.enqueue(json(200, ASSIGNED).setHeadersDelay(2, TimeUnit.SECONDS));
            try (ExperimentationClient client = client(recording, 60_000L, 100, 200L)) {
                assertThrows(ExperimentationException.class,
                        () -> client.getExperimentAssignment(user("u-1"), "exp-1"));

                assertEquals(1, server.getRequestCount(), "the server received the request");
                assertEquals(0, calls.size());
            }
        }

        @Test
        @DisplayName("flags never call it: evaluateFeatureFlag and isFeatureEnabled")
        void flagsNeverCallIt() {
            server.setDispatcher(new Dispatcher() {
                @Override
                public MockResponse dispatch(RecordedRequest request) {
                    return json(200, "{\"key\":\"f\",\"enabled\":true,\"config\":null,\"assigned\":true}");
                }
            });
            try (ExperimentationClient client = client(recording)) {
                assertTrue(client.evaluateFeatureFlag(user("u-1"), "f").isEnabled());
                assertTrue(client.isFeatureEnabled(user("u-2"), "f"));

                assertEquals(2, server.getRequestCount());
                assertEquals(0, calls.size());
            }
        }

        @Test
        @DisplayName("exposureListener(null) sets none, and the assignment works as before")
        void nullSetsNone() {
            SdkConfig config = SdkConfig.builder("k", "http://localhost:1").exposureListener(null).build();
            assertNull(config.getExposureListener());
            assertNull(SdkConfig.builder("k", "http://localhost:1").build().getExposureListener(),
                    "the default is none");

            server.enqueue(json(200, ASSIGNED));
            try (ExperimentationClient client = client(null)) {
                assertEquals("treatment", client.getExperimentAssignment(user("u-1"), "exp-1").getVariantName());
                assertEquals(1, server.getRequestCount());
            }
        }
    }

    // =========================================================================
    // Called again
    // =========================================================================

    @Nested
    @DisplayName("called again when the client asks the server again")
    class CalledAgain {

        @Test
        @DisplayName("after the cache lifetime")
        void afterTheCacheLifetime() throws InterruptedException {
            echoAssigned();
            try (ExperimentationClient client = client(recording, 20L, 100, 5_000L)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");
                Thread.sleep(80L); // more than the 20 ms lifetime has passed; no timing is asserted
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(2, server.getRequestCount());
                assertEquals(Arrays.asList(EXPECTED, EXPECTED), calls);
            }
        }

        @Test
        @DisplayName("after clearCache()")
        void afterClearCache() {
            echoAssigned();
            try (ExperimentationClient client = client(recording)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");
                client.clearCache();
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(2, server.getRequestCount());
                assertEquals(2, calls.size());
            }
        }

        @Test
        @DisplayName("after invalidateCache(userId, key)")
        void afterInvalidateCache() {
            echoAssigned();
            try (ExperimentationClient client = client(recording)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");
                client.invalidateCache("u-1", "exp-1");
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(2, server.getRequestCount());
                assertEquals(2, calls.size());
            }
        }

        @Test
        @DisplayName("after eviction from a full cache")
        void afterEviction() {
            echoAssigned();
            try (ExperimentationClient client = client(recording, 60_000L, 1, 5_000L)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");
                client.getExperimentAssignment(user("u-1"), "exp-2"); // evicts exp-1 (cacheSize 1)
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(3, server.getRequestCount());
                assertEquals(Arrays.asList(
                        new Exposure("exp-1", "u-1", "v-2", "treatment"),
                        new Exposure("exp-2", "u-1", "v-2", "treatment"),
                        new Exposure("exp-1", "u-1", "v-2", "treatment")), calls);
            }
        }

        @Test
        @DisplayName("in a new client")
        void inANewClient() {
            echoAssigned();
            try (ExperimentationClient first = client(recording)) {
                first.getExperimentAssignment(user("u-1"), "exp-1");
            }
            try (ExperimentationClient second = client(recording)) {
                second.getExperimentAssignment(user("u-1"), "exp-1");
            }

            assertEquals(2, server.getRequestCount());
            assertEquals(Arrays.asList(EXPECTED, EXPECTED), calls);
        }
    }

    // =========================================================================
    // Where and how it runs
    // =========================================================================

    @Nested
    @DisplayName("where it runs")
    class WhereItRuns {

        @Test
        @DisplayName("runs on the calling thread, before getExperimentAssignment returns")
        void runsOnTheCallingThreadBeforeTheCallReturns() {
            server.enqueue(json(200, ASSIGNED));
            AtomicReference<Thread> ranOn = new AtomicReference<>();
            try (ExperimentationClient client = client(e -> ranOn.set(Thread.currentThread()))) {
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertSame(Thread.currentThread(), ranOn.get());
            }
        }

        @Test
        @DisplayName("runs after the answer is cached: a listener that asks for the same assignment gets it from the cache")
        void runsAfterTheAnswerIsCached() {
            echoAssigned();
            AtomicReference<ExperimentationClient> self = new AtomicReference<>();
            List<String> seen = new ArrayList<>();
            try (ExperimentationClient client = client(e -> {
                seen.add(self.get().getExperimentAssignment(user("u-1"), "exp-1").getVariantName());
                calls.add(e);
            })) {
                self.set(client);
                client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals(1, server.getRequestCount());
                assertEquals(Collections.singletonList("treatment"), seen);
                assertEquals(1, calls.size());
            }
        }

        @Test
        @DisplayName("holds no cache lock: while a listener waits, another thread's cached call returns")
        void holdsNoCacheLock() throws Exception {
            echoAssigned();
            CountDownLatch entered = new CountDownLatch(1);
            CountDownLatch release = new CountDownLatch(1);
            ExecutorService pool = Executors.newFixedThreadPool(2);
            try (ExperimentationClient client = client(e -> {
                if (e.getExperimentKey().equals("exp-1")) {
                    entered.countDown();
                    try {
                        release.await(10, TimeUnit.SECONDS);
                    } catch (InterruptedException interrupted) {
                        Thread.currentThread().interrupt();
                    }
                }
            })) {
                client.getExperimentAssignment(user("u-1"), "exp-2"); // cached before the listener waits
                Future<ExperimentAssignment> waiting = pool.submit(() -> client.getExperimentAssignment(user("u-1"), "exp-1"));
                assertTrue(entered.await(5, TimeUnit.SECONDS), "the listener for exp-1 never ran");

                Future<ExperimentAssignment> cached = pool.submit(() -> client.getExperimentAssignment(user("u-1"), "exp-2"));
                assertEquals("treatment", cached.get(5, TimeUnit.SECONDS).getVariantName());
                assertFalse(waiting.isDone(), "the listener is still waiting");

                release.countDown();
                assertEquals("treatment", waiting.get(5, TimeUnit.SECONDS).getVariantName());
            } finally {
                release.countDown();
                pool.shutdownNow();
            }
        }

        @Test
        @DisplayName("the client sends nothing because of it")
        void sendsNothingBecauseOfIt() throws InterruptedException {
            server.enqueue(json(200, ASSIGNED));
            try (ExperimentationClient client = client(recording)) {
                client.getExperimentAssignment(user("u-1"), "exp-1");

                RecordedRequest only = server.takeRequest(1, TimeUnit.SECONDS);
                assertNotNull(only);
                assertEquals("POST /api/v1/tracking/assign", only.getMethod() + " " + only.getPath());
                assertNull(server.takeRequest(200, TimeUnit.MILLISECONDS), "a second request was sent");
                assertEquals(1, calls.size());
            }
        }
    }

    // =========================================================================
    // What it throws
    // =========================================================================

    @Nested
    @DisplayName("when it throws")
    class WhenItThrows {

        @ParameterizedTest(name = "a listener that throws {0} changes nothing: the answer is returned and cached")
        @ValueSource(strings = {"RuntimeException", "NoClassDefFoundError", "ExperimentationException", "checked IOException", "InterruptedException"})
        void aThrowingListenerChangesNothing(String what) {
            Throwable thrown;
            switch (what) {
                case "RuntimeException": thrown = new RuntimeException("hook boom"); break;
                case "NoClassDefFoundError": thrown = new NoClassDefFoundError("com/example/analytics/Analytics"); break;
                case "ExperimentationException": thrown = new ExperimentationException("hook boom", 503); break;
                case "checked IOException": thrown = new IOException("hook boom"); break;
                default: thrown = new InterruptedException("hook boom"); break;
            }
            server.enqueue(json(200, ASSIGNED));
            AtomicInteger listened = new AtomicInteger();
            try (ExperimentationClient client = client(e -> {
                listened.incrementAndGet();
                ExposureListenerTest.<RuntimeException>sneakyThrow(thrown);
            })) {
                ExperimentAssignment answer = client.getExperimentAssignment(user("u-1"), "exp-1");
                ExperimentAssignment again = client.getExperimentAssignment(user("u-1"), "exp-1");

                assertEquals("treatment", answer.getVariantName());
                assertSame(answer, again, "the answer is cached");
                assertEquals(1, server.getRequestCount());
                assertEquals(1, listened.get());
                assertEquals(what.equals("InterruptedException"), Thread.interrupted(),
                        "the interrupt flag is set again for an InterruptedException, and only then");
            }
        }

        @Test
        @DisplayName("an OutOfMemoryError or a StackOverflowError is not caught (VirtualMachineError)")
        void aVirtualMachineErrorIsNotCaught() {
            for (VirtualMachineError fatal : Arrays.<VirtualMachineError>asList(
                    new OutOfMemoryError("planted"), new StackOverflowError("planted"))) {
                server.enqueue(json(200, ASSIGNED));
                try (ExperimentationClient client = client(e -> { throw fatal; })) {
                    VirtualMachineError reached = assertThrows(VirtualMachineError.class,
                            () -> client.getExperimentAssignment(user("u-1"), "exp-1"));
                    assertSame(fatal, reached);
                }
            }
        }
    }

    // =========================================================================
    // The hook error: the Java SDK's kind for a listener's failure
    // =========================================================================

    @Nested
    @DisplayName("the hook error")
    class HookErrorKind {

        @Test
        @DisplayName("what a listener throws becomes a hook error: message onExposure failed: plus its message, cause what it threw, never a network error")
        void whatAListenerThrowsBecomesAHookError() {
            RuntimeException thrown = new RuntimeException("hook boom");

            ExperimentationException error = ExposureHook.call(e -> { throw thrown; }, EXPECTED);

            assertInstanceOf(ExposureHook.HookError.class, error);
            assertEquals("onExposure failed: hook boom", error.getMessage());
            assertTrue(error.getMessage().startsWith(ExposureHook.MESSAGE_PREFIX));
            assertSame(thrown, error.getCause());
            assertEquals(0, error.getStatusCode());
            assertFalse(error.isApiError());
            assertFalse(error.getMessage().startsWith("Network error"), error.getMessage());
        }

        @Test
        @DisplayName("an Error is a hook error too, with the Error as its cause")
        void anErrorIsAHookErrorToo() {
            NoClassDefFoundError thrown = new NoClassDefFoundError("com/example/analytics/Analytics");

            ExperimentationException error = ExposureHook.call(e -> { throw thrown; }, EXPECTED);

            assertInstanceOf(ExposureHook.HookError.class, error);
            assertEquals("onExposure failed: com/example/analytics/Analytics", error.getMessage());
            assertSame(thrown, error.getCause());
        }

        @Test
        @DisplayName("a throwable with no message, or one whose getMessage() throws, is named by its class")
        void aThrowableWithNoMessageIsNamedByItsClass() {
            assertEquals("onExposure failed: java.lang.IllegalStateException",
                    ExposureHook.error(new IllegalStateException()).getMessage());
            RuntimeException unreadable = new RuntimeException() {
                @Override
                public String getMessage() {
                    throw new IllegalStateException("cannot say");
                }
            };
            assertEquals("onExposure failed: " + unreadable.getClass().getName(),
                    ExposureHook.error(unreadable).getMessage());
        }

        @Test
        @DisplayName("a listener that returns, or no listener, gives no hook error")
        void noErrorWhenItReturns() {
            AtomicInteger listened = new AtomicInteger();
            assertNull(ExposureHook.call(e -> listened.incrementAndGet(), EXPECTED));
            assertNull(ExposureHook.call(null, EXPECTED));
            assertEquals(1, listened.get());
        }
    }

    // =========================================================================
    // The cache's fresh store
    // =========================================================================

    @Nested
    @DisplayName("AssignmentCache.fill")
    class Fill {

        @Test
        @DisplayName("is true for an empty slot, false for an unexpired one, true for an expired one, and stores the value each time")
        void reportsWhetherTheSlotHeldALiveEntry() throws InterruptedException {
            AssignmentCache<String> cache = new AssignmentCache<>(10, 60_000L);
            assertTrue(cache.fill("k", "a"), "empty slot");
            assertFalse(cache.fill("k", "b"), "unexpired slot");
            assertEquals("b", cache.get("k"));

            AssignmentCache<String> shortLived = new AssignmentCache<>(10, 20L);
            assertTrue(shortLived.fill("k", "a"), "empty slot");
            Thread.sleep(80L); // more than the 20 ms lifetime has passed; no timing is asserted
            assertTrue(shortLived.fill("k", "c"), "expired slot");
            assertEquals("c", shortLived.get("k"));
        }

        @Test
        @DisplayName("is true again for a slot that was evicted, invalidated or cleared")
        void trueAgainAfterEvictionInvalidationAndClear() {
            AssignmentCache<String> cache = new AssignmentCache<>(1, 60_000L);

            assertTrue(cache.fill("k", "a"));
            assertTrue(cache.fill("other", "x"), "evicts k");
            assertTrue(cache.fill("k", "b"), "after eviction");
            cache.invalidate("k");
            assertTrue(cache.fill("k", "c"), "after invalidate");
            cache.clear();
            assertTrue(cache.fill("k", "d"), "after clear");
        }
    }

    // =========================================================================
    // The Exposure type
    // =========================================================================

    @Nested
    @DisplayName("Exposure")
    class ExposureType {

        @Test
        @DisplayName("is a final class of exactly four private final String fields and no setter")
        void shape() {
            assertTrue(Modifier.isFinal(Exposure.class.getModifiers()));
            Set<String> names = new HashSet<>();
            for (Field field : Exposure.class.getDeclaredFields()) {
                if (field.isSynthetic()) continue;
                int m = field.getModifiers();
                assertTrue(Modifier.isPrivate(m) && Modifier.isFinal(m) && !Modifier.isStatic(m), field.toString());
                assertEquals(String.class, field.getType(), field.toString());
                names.add(field.getName());
            }
            assertEquals(new HashSet<>(Arrays.asList("experimentKey", "userId", "variantId", "variantName")), names);
            for (Method method : Exposure.class.getDeclaredMethods()) {
                assertFalse(method.getName().startsWith("set"), method.toString());
            }
        }

        @Test
        @DisplayName("takes its values in the Assignment order: experiment key, user id, variant id, variant name")
        void constructorOrder() {
            Exposure exposure = new Exposure("k", "u", "v", "n");

            assertEquals("k", exposure.getExperimentKey());
            assertEquals("u", exposure.getUserId());
            assertEquals("v", exposure.getVariantId());
            assertEquals("n", exposure.getVariantName());
        }

        @Test
        @DisplayName("equals and hashCode compare the four values; toString shows them")
        void valueSemantics() {
            Exposure a = new Exposure("k", "u", null, "n");
            Exposure b = new Exposure("k", "u", null, "n");

            assertEquals(a, b);
            assertEquals(a.hashCode(), b.hashCode());
            assertNotEquals(a, new Exposure("k", "u", "v", "n"));
            assertNotEquals(a, new Exposure("k", "n", null, "u"), "user id and variant name swapped");
            assertEquals("Exposure{experimentKey='k', userId='u', variantId='null', variantName='n'}", a.toString());
        }
    }
}

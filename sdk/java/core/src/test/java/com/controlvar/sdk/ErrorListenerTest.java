package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.exception.FlagValueException;
import com.controlvar.sdk.model.User;
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
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

/**
 * The error listener (#1810): the typed getters pass it the reason each time they return the
 * caller's default, with the operation {@code "value"}, on the calling thread. Nothing it throws,
 * other than a {@link VirtualMachineError}, reaches the caller or changes the answer.
 */
@DisplayName("ErrorListener")
class ErrorListenerTest {

    private static final User USER = User.builder("u-1").build();
    /** A string flag: a number getter falls back on it with TYPE_MISMATCH. */
    private static final String STRING_FLAG = "{\"key\":\"f\",\"enabled\":true,\"value\":\"on-copy\","
            + "\"value_type\":\"string\",\"config\":null,\"reason\":\"rollout\"}";

    private MockWebServer server;

    @BeforeEach
    void setUp() throws IOException {
        server = new MockWebServer();
        server.start();
    }

    @AfterEach
    void tearDown() throws IOException {
        Thread.interrupted(); // no test leaves this thread interrupted for the next
        server.shutdown();
    }

    private ExperimentationClient client(ErrorListener listener) {
        return client(listener, null);
    }

    private ExperimentationClient client(ErrorListener listener, ExposureListener exposures) {
        SdkConfig config = SdkConfig.builder("test-api-key", server.url("/").toString())
                .errorListener(listener)
                .exposureListener(exposures)
                .build();
        OkHttpClient http = new OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(5, TimeUnit.SECONDS)
                .build();
        return new ExperimentationClient(config, http);
    }

    private static MockResponse json(int code, String body) {
        return new MockResponse().setResponseCode(code)
                .setHeader("Content-Type", "application/json").setBody(body);
    }

    private void alwaysAnswer(int code, String body) {
        server.setDispatcher(new Dispatcher() {
            @Override
            public MockResponse dispatch(RecordedRequest request) {
                return json(code, body);
            }
        });
    }

    @SuppressWarnings("unchecked")
    private static <T extends Throwable> void sneakyThrow(Throwable t) throws T {
        throw (T) t;
    }

    // =========================================================================
    // When it is called
    // =========================================================================

    @Nested
    @DisplayName("when it is called")
    class WhenCalled {

        @Test
        @DisplayName("once per fallback, with the reason and the operation \"value\", on the calling thread, before the getter returns")
        void oncePerFallbackOnTheCallingThread() {
            alwaysAnswer(200, STRING_FLAG);
            List<ExperimentationException> errors = new ArrayList<>();
            List<String> operations = new ArrayList<>();
            List<Thread> threads = new ArrayList<>();
            try (ExperimentationClient client = client((error, operation) -> {
                errors.add(error);
                operations.add(operation);
                threads.add(Thread.currentThread());
            })) {
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
                assertEquals(1, operations.size(), "called before the getter returned");

                assertEquals(Collections.singletonList("value"), operations);
                assertEquals(FlagValueException.TYPE_MISMATCH, ((FlagValueException) errors.get(0)).getCode());
                assertEquals(Collections.singletonList(Thread.currentThread()), threads);
            }
        }

        @Test
        @DisplayName("not when the getter returns the flag's value")
        void notForAValue() {
            alwaysAnswer(200, STRING_FLAG);
            AtomicInteger calls = new AtomicInteger();
            try (ExperimentationClient client = client((error, operation) -> calls.incrementAndGet())) {
                assertEquals("on-copy", client.getStringValue(USER, "f", "fb"));
                assertEquals(0, calls.get());
            }
        }

        @Test
        @DisplayName("not for evaluateFeatureFlag, isFeatureEnabled or getExperimentAssignment: getter fallbacks only")
        void gettersOnly() {
            alwaysAnswer(404, "{\"detail\":\"not found\"}");
            AtomicInteger calls = new AtomicInteger();
            try (ExperimentationClient client = client((error, operation) -> calls.incrementAndGet())) {
                assertThrows(ExperimentationException.class, () -> client.evaluateFeatureFlag(USER, "f"));
                assertFalse(client.isFeatureEnabled(USER, "f"));
                assertThrows(ExperimentationException.class, () -> client.getExperimentAssignment(USER, "exp"));
                assertEquals(0, calls.get());
            }
        }

        @Test
        @DisplayName("not for an exposure listener that throws")
        void notForAnExposureListenersFailure() {
            alwaysAnswer(200, "{\"experiment_key\":\"exp\",\"user_id\":\"u-1\",\"variant_id\":\"v\","
                    + "\"variant_name\":\"treatment\",\"is_control\":false,\"assigned\":true}");
            AtomicInteger calls = new AtomicInteger();
            try (ExperimentationClient client = client((error, operation) -> calls.incrementAndGet(),
                    exposure -> { throw new IllegalStateException("exposure boom"); })) {
                assertEquals("treatment", client.getExperimentAssignment(USER, "exp").getVariantName());
                assertEquals(0, calls.get());
            }
        }

        @Test
        @DisplayName("errorListener(null) sets none, and is the default: a getter still returns the default")
        void noListener() {
            alwaysAnswer(200, STRING_FLAG);
            assertNull(SdkConfig.builder("k", "http://localhost").build().getErrorListener());
            try (ExperimentationClient client = client(null)) {
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
            }
        }

        @Test
        @DisplayName("can run on several threads at once, once per fallback on each")
        void onSeveralThreadsAtOnce() throws Exception {
            alwaysAnswer(200, STRING_FLAG);
            int threads = 4;
            CountDownLatch inside = new CountDownLatch(threads);
            AtomicInteger together = new AtomicInteger();
            Set<Thread> seen = Collections.synchronizedSet(new HashSet<>());
            try (ExperimentationClient client = client((error, operation) -> {
                seen.add(Thread.currentThread());
                inside.countDown();
                try {
                    // Counted only when every thread is inside the listener at the same moment.
                    if (inside.await(5, TimeUnit.SECONDS)) together.incrementAndGet();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            })) {
                ExecutorService pool = Executors.newFixedThreadPool(threads);
                try {
                    List<Future<Double>> answers = new ArrayList<>();
                    for (int i = 0; i < threads; i++) {
                        answers.add(pool.submit(() -> client.getNumberValue(USER, "f", -1)));
                    }
                    for (Future<Double> answer : answers) {
                        assertEquals(-1.0, answer.get(10, TimeUnit.SECONDS));
                    }
                } finally {
                    pool.shutdownNow();
                }
                assertEquals(threads, seen.size());
                assertEquals(threads, together.get());
            }
        }
    }

    // =========================================================================
    // When it throws
    // =========================================================================

    @Nested
    @DisplayName("when it throws")
    class WhenItThrows {

        @ParameterizedTest(name = "a listener that throws {0} changes no answer")
        @ValueSource(strings = {"RuntimeException", "NoClassDefFoundError", "ExperimentationException", "checked IOException", "InterruptedException"})
        void aThrowingListenerChangesNoAnswer(String what) {
            Throwable thrown;
            switch (what) {
                case "RuntimeException": thrown = new RuntimeException("listener boom"); break;
                case "NoClassDefFoundError": thrown = new NoClassDefFoundError("com/example/logging/Log"); break;
                case "ExperimentationException": thrown = new ExperimentationException("listener boom", 503); break;
                case "checked IOException": thrown = new IOException("listener boom"); break;
                default: thrown = new InterruptedException("listener boom"); break;
            }
            alwaysAnswer(200, STRING_FLAG);
            AtomicInteger calls = new AtomicInteger();
            try (ExperimentationClient client = client((error, operation) -> {
                calls.incrementAndGet();
                ErrorListenerTest.<RuntimeException>sneakyThrow(thrown);
            })) {
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
                assertEquals(what.equals("InterruptedException"), Thread.interrupted(),
                        "the interrupt flag is set again for an InterruptedException, and only then");
                assertEquals("on-copy", client.getStringValue(USER, "f", "fb"));
                assertEquals(1, calls.get());
                assertEquals(1, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("a listener that interrupts the thread and then throws leaves the interrupt flag set")
        void interruptsThenThrows() {
            alwaysAnswer(200, STRING_FLAG);
            try (ExperimentationClient client = client((error, operation) -> {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("listener boom");
            })) {
                Object fallback = new Object();
                assertSame(fallback, client.getJsonValue(USER, "f", fallback));
                assertTrue(Thread.interrupted(), "the listener's interrupt is still set");
            }
        }

        @Test
        @DisplayName("an OutOfMemoryError or a StackOverflowError is not caught (VirtualMachineError)")
        void aVirtualMachineErrorIsNotCaught() {
            alwaysAnswer(200, STRING_FLAG);
            for (VirtualMachineError fatal : Arrays.<VirtualMachineError>asList(
                    new OutOfMemoryError("planted"), new StackOverflowError("planted"))) {
                try (ExperimentationClient client = client((error, operation) -> { throw fatal; })) {
                    VirtualMachineError reached = assertThrows(VirtualMachineError.class,
                            () -> client.getNumberValue(USER, "f", -1));
                    assertSame(fatal, reached);
                }
            }
        }
    }
}

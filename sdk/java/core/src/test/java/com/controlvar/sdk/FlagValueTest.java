package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.exception.FlagValueException;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.User;
import okhttp3.OkHttpClient;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.SocketPolicy;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.*;

/**
 * The typed getters (#1810): {@code getStringValue}, {@code getNumberValue} and
 * {@code getJsonValue} read the value a string, number or json flag serves the user from
 * {@code GET /api/v1/feature-flags/evaluate/{key}}, through the same call and cache as
 * {@code evaluateFeatureFlag}, and return the caller's default, reported once to the error
 * listener, whenever they cannot. Every answer here is a raw body from {@link MockWebServer}.
 */
@DisplayName("typed flag value getters")
class FlagValueTest {

    private static final User USER = User.builder("u-1").build();

    private MockWebServer server;
    private final List<ExperimentationException> errors = Collections.synchronizedList(new ArrayList<>());
    private final List<String> operations = Collections.synchronizedList(new ArrayList<>());

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

    private ExperimentationClient client() {
        return client(server.url("/").toString());
    }

    private ExperimentationClient client(String baseUrl) {
        SdkConfig config = SdkConfig.builder("test-api-key", baseUrl)
                .cacheTtlMs(60_000L)
                .errorListener((error, operation) -> {
                    errors.add(error);
                    operations.add(operation);
                })
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

    /** The route's answer for a flag that serves {@code valueJson} as {@code type}. */
    private static MockResponse serves(boolean enabled, String valueJson, String type) {
        return json(200, "{\"key\":\"f\",\"enabled\":" + enabled + ",\"value\":" + valueJson
                + ",\"value_type\":\"" + type + "\",\"config\":null,\"reason\":\"rollout\"}");
    }

    private static MockResponse servesNumber(String numberJson) {
        return serves(true, numberJson, "number");
    }

    private void assertReportedOnce(String code) {
        assertEquals(Collections.singletonList("value"), operations, "one report, for the operation \"value\"");
        ExperimentationException error = errors.get(0);
        assertTrue(error instanceof FlagValueException, "a FlagValueException, not " + error.getClass().getName());
        assertEquals(code, ((FlagValueException) error).getCode());
        assertEquals(0, error.getStatusCode(), "not an HTTP error");
        assertFalse(error.isApiError());
    }

    private void assertNothingReported() {
        assertEquals(Collections.emptyList(), operations, "nothing reported");
    }

    // =========================================================================
    // The wire: value and value_type are read from the answer
    // =========================================================================

    @Nested
    @DisplayName("value and value_type from the answer")
    class Mapping {

        @Test
        @DisplayName("evaluateFeatureFlag keeps the answer's value and value_type: a string, a number and a JSON object")
        void evaluateKeepsTheValueAndItsType() {
            server.enqueue(serves(true, "\"on-copy\"", "string"));
            server.enqueue(serves(true, "10", "number"));
            server.enqueue(serves(true, "{\"color\":\"blue\",\"sizes\":[1,2]}", "json"));
            try (ExperimentationClient client = client()) {
                FlagEvaluation string = client.evaluateFeatureFlag(USER, "s");
                FlagEvaluation number = client.evaluateFeatureFlag(USER, "n");
                FlagEvaluation json = client.evaluateFeatureFlag(USER, "j");

                assertEquals("on-copy", string.getValue());
                assertEquals("string", string.getValueType());
                assertEquals(10, number.getValue());
                assertEquals("number", number.getValueType());
                Map<String, Object> expected = new LinkedHashMap<>();
                expected.put("color", "blue");
                expected.put("sizes", Arrays.asList(1, 2));
                assertEquals(expected, json.getValue());
                assertEquals("json", json.getValueType());
            }
        }

        @Test
        @DisplayName("the getters read the answer's value through the client's own mapping")
        void theGettersReadTheMappedValue() {
            server.enqueue(serves(true, "\"on-copy\"", "string"));
            server.enqueue(serves(true, "10", "number"));
            server.enqueue(serves(true, "{\"color\":\"blue\"}", "json"));
            try (ExperimentationClient client = client()) {
                assertEquals("on-copy", client.getStringValue(USER, "s", "fb"));
                assertEquals(10.0, client.getNumberValue(USER, "n", -1));
                assertEquals(Collections.singletonMap("color", "blue"), client.getJsonValue(USER, "j", "fb"));
                assertNothingReported();
            }
        }

        @Test
        @DisplayName("an answer without value or value_type (a server from before flag values) gives null for both, and the flag still evaluates")
        void anAnswerWithoutAValue() {
            server.enqueue(json(200, "{\"key\":\"f\",\"enabled\":true,\"config\":null}"));
            try (ExperimentationClient client = client()) {
                FlagEvaluation evaluation = client.evaluateFeatureFlag(USER, "f");

                assertTrue(evaluation.isEnabled());
                assertNull(evaluation.getValue());
                assertNull(evaluation.getValueType());
            }
        }

        @Test
        @DisplayName("a value_type that is not a string is no type, and the flag still evaluates as before")
        void aValueTypeThatIsNotAString() {
            server.enqueue(json(200, "{\"key\":\"f\",\"enabled\":true,\"value\":\"x\",\"value_type\":{\"a\":1}}"));
            try (ExperimentationClient client = client()) {
                FlagEvaluation evaluation = client.evaluateFeatureFlag(USER, "f");

                assertTrue(evaluation.isEnabled());
                assertNull(evaluation.getValueType());
                // With no type, the value's own JSON type is the served type: a string.
                assertEquals("x", client.getStringValue(USER, "f", "fb"));
                assertTrue(client.isFeatureEnabled(USER, "f"));
            }
        }

        @Test
        @DisplayName("a FlagEvaluation built by hand has no value and no type")
        void aHandBuiltEvaluationHasNoValue() {
            FlagEvaluation evaluation = new FlagEvaluation("f", true, null);

            assertNull(evaluation.getValue());
            assertNull(evaluation.getValueType());
        }

        @Test
        @DisplayName("equals and hashCode compare the value and its type; toString shows them")
        void equalsComparesTheValue() throws IOException {
            server.enqueue(serves(true, "\"a\"", "string"));
            server.enqueue(serves(true, "\"a\"", "string"));
            server.enqueue(serves(true, "\"b\"", "string"));
            try (ExperimentationClient client = client()) {
                FlagEvaluation a = client.evaluateFeatureFlag(USER, "f");
                FlagEvaluation same = client.evaluateFeatureFlag(User.builder("u-2").build(), "f");
                FlagEvaluation other = client.evaluateFeatureFlag(User.builder("u-3").build(), "f");

                assertEquals(a, same);
                assertEquals(a.hashCode(), same.hashCode());
                assertNotEquals(a, other);
                assertNotEquals(a, new FlagEvaluation("f", true, null));
                assertTrue(a.toString().contains("value=a"), a.toString());
                assertTrue(a.toString().contains("valueType=string"), a.toString());
            }
        }
    }

    // =========================================================================
    // Off is a value
    // =========================================================================

    @Nested
    @DisplayName("the off value")
    class Off {

        @Test
        @DisplayName("a flag that is off for the user serves its off value, which the getter returns")
        void offServesTheOffValue() {
            server.enqueue(serves(false, "\"off-copy\"", "string"));
            server.enqueue(serves(false, "2.5", "number"));
            server.enqueue(serves(false, "[\"fallback\"]", "json"));
            try (ExperimentationClient client = client()) {
                assertEquals("off-copy", client.getStringValue(USER, "s", "fb"));
                assertEquals(2.5, client.getNumberValue(USER, "n", -1));
                assertEquals(Collections.singletonList("fallback"), client.getJsonValue(USER, "j", "fb"));
                assertNothingReported();
            }
        }

        @Test
        @DisplayName("an inactive flag serves its off value too")
        void inactiveServesTheOffValue() {
            server.enqueue(json(200, "{\"key\":\"f\",\"enabled\":false,\"value\":\"off-copy\","
                    + "\"value_type\":\"string\",\"config\":null,\"reason\":\"inactive\"}"));
            try (ExperimentationClient client = client()) {
                assertEquals("off-copy", client.getStringValue(USER, "f", "fb"));
                assertNothingReported();
            }
        }
    }

    // =========================================================================
    // Falsy values are values
    // =========================================================================

    @Nested
    @DisplayName("empty and zero values")
    class Falsy {

        @Test
        @DisplayName("\"\", 0, {} and [] are values, returned and not reported")
        void falsyValuesAreValues() {
            server.enqueue(serves(true, "\"\"", "string"));
            server.enqueue(serves(true, "0", "number"));
            server.enqueue(serves(true, "{}", "json"));
            server.enqueue(serves(true, "[]", "json"));
            try (ExperimentationClient client = client()) {
                assertEquals("", client.getStringValue(USER, "s", "fb"));
                assertEquals(0.0, client.getNumberValue(USER, "n", -1));
                assertEquals(Collections.emptyMap(), client.getJsonValue(USER, "o", "fb"));
                assertEquals(Collections.emptyList(), client.getJsonValue(USER, "a", "fb"));
                assertNothingReported();
            }
        }
    }

    // =========================================================================
    // Each fallback is reported once
    // =========================================================================

    @Nested
    @DisplayName("each fallback is reported once")
    class Reports {

        @Test
        @DisplayName("a flag of another type: the default, reported once as TYPE_MISMATCH")
        void anotherType() {
            server.enqueue(serves(true, "\"on-copy\"", "string"));
            try (ExperimentationClient client = client()) {
                assertEquals(-1.0, client.getNumberValue(USER, "s", -1));
                assertReportedOnce(FlagValueException.TYPE_MISMATCH);
                assertTrue(errors.get(0).getMessage().contains("getNumberValue returned your default"),
                        errors.get(0).getMessage());
            }
        }

        @Test
        @DisplayName("an answer without a value: the default, reported once as INVALID_RESPONSE")
        void noValue() {
            server.enqueue(json(200, "{\"key\":\"f\",\"enabled\":true,\"config\":null}"));
            try (ExperimentationClient client = client()) {
                Object fallback = new Object();
                assertSame(fallback, client.getJsonValue(USER, "f", fallback));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
            }
        }

        @Test
        @DisplayName("a value that is not of its own value_type: the default, reported once as INVALID_RESPONSE")
        void aValueNotOfItsType() {
            server.enqueue(serves(true, "\"7\"", "number"));
            try (ExperimentationClient client = client()) {
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
            }
        }

        @Test
        @DisplayName("a null user: the default, reported as INVALID_RESPONSE, and no request")
        void aNullUser() {
            try (ExperimentationClient client = client()) {
                String fallback = new String("fb");
                assertSame(fallback, client.getStringValue(null, "f", fallback));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
                assertEquals(0, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("an empty or null flag key: the default, reported as INVALID_RESPONSE, and no request")
        void anEmptyKey() {
            try (ExperimentationClient client = client()) {
                assertEquals(-1.0, client.getNumberValue(USER, "", -1));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
                errors.clear();
                operations.clear();
                Object fallback = new Object();
                assertSame(fallback, client.getJsonValue(USER, null, fallback));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
                assertEquals(0, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("a value returned is reported to no one")
        void aValueIsNotReported() {
            server.enqueue(serves(true, "\"on-copy\"", "string"));
            try (ExperimentationClient client = client()) {
                assertEquals("on-copy", client.getStringValue(USER, "f", "fb"));
                assertEquals("on-copy", client.getStringValue(USER, "f", "fb"));
                assertNothingReported();
            }
        }

        @Test
        @DisplayName("each call that falls back is reported, a cached answer's included")
        void eachFallbackCallIsReported() {
            server.enqueue(serves(true, "\"on-copy\"", "string"));
            try (ExperimentationClient client = client()) {
                client.getNumberValue(USER, "f", -1);
                client.getNumberValue(USER, "f", -1);

                assertEquals(Arrays.asList("value", "value"), operations);
                assertEquals(1, server.getRequestCount());
            }
        }
    }

    // =========================================================================
    // JSON values are copies
    // =========================================================================

    @Nested
    @DisplayName("a JSON value is a new copy on every call")
    class Copies {

        @Test
        @DisplayName("changing what getJsonValue returned, at any depth, changes nothing the next call returns")
        @SuppressWarnings("unchecked")
        void changingTheReturnedValueChangesNothing() {
            server.enqueue(serves(true, "{\"color\":\"blue\",\"sizes\":[1,2]}", "json"));
            try (ExperimentationClient client = client()) {
                Map<String, Object> first = (Map<String, Object>) client.getJsonValue(USER, "f", null);
                first.put("color", "red");
                ((List<Object>) first.get("sizes")).add(3);

                Map<String, Object> second = (Map<String, Object>) client.getJsonValue(USER, "f", null);
                assertEquals("blue", second.get("color"));
                assertEquals(Arrays.asList(1, 2), second.get("sizes"));
                assertNotSame(first, second);
                assertEquals(1, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("changing evaluateFeatureFlag(...).getValue(), the cached evaluation's, changes nothing getJsonValue returns")
        @SuppressWarnings("unchecked")
        void changingTheCachedEvaluationsValueChangesNothing() {
            server.enqueue(serves(true, "{\"color\":\"blue\",\"sizes\":[1,2]}", "json"));
            try (ExperimentationClient client = client()) {
                FlagEvaluation cached = client.evaluateFeatureFlag(USER, "f");
                Map<String, Object> fromModel = (Map<String, Object>) cached.getValue();
                fromModel.put("color", "red");
                ((List<Object>) fromModel.get("sizes")).clear();

                Map<String, Object> read = (Map<String, Object>) client.getJsonValue(USER, "f", null);
                assertEquals("blue", read.get("color"));
                assertEquals(Arrays.asList(1, 2), read.get("sizes"));
                assertNotSame(cached.getValue(), cached.getValue(), "getValue() copies on every call");
                assertEquals(1, server.getRequestCount());
            }
        }
    }

    // =========================================================================
    // One request, shared with evaluateFeatureFlag
    // =========================================================================

    @Nested
    @DisplayName("one request")
    class OneRequest {

        @Test
        @DisplayName("isFeatureEnabled then a getter: one request, the getter reads the cached answer")
        void isFeatureEnabledThenAGetter() {
            server.enqueue(serves(true, "\"on-copy\"", "string"));
            try (ExperimentationClient client = client()) {
                assertTrue(client.isFeatureEnabled(USER, "f"));
                assertEquals("on-copy", client.getStringValue(USER, "f", "fb"));

                assertEquals(1, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("a getter then evaluateFeatureFlag: one request")
        void aGetterThenEvaluate() {
            server.enqueue(serves(true, "10", "number"));
            try (ExperimentationClient client = client()) {
                assertEquals(10.0, client.getNumberValue(USER, "f", -1));
                assertTrue(client.evaluateFeatureFlag(USER, "f").isEnabled());

                assertEquals(1, server.getRequestCount());
            }
        }
    }

    // =========================================================================
    // Never throws
    // =========================================================================

    @Nested
    @DisplayName("never throws")
    class NeverThrows {

        @Test
        @DisplayName("an unknown flag (404): the default, reported once as the HTTP error with status 404")
        void unknownFlag() {
            server.enqueue(json(404, "{\"detail\":\"Feature flag with key 'f' not found\"}"));
            try (ExperimentationClient client = client()) {
                assertEquals("fb", client.getStringValue(USER, "f", "fb"));
                assertEquals(Collections.singletonList("value"), operations);
                assertFalse(errors.get(0) instanceof FlagValueException);
                assertEquals(404, errors.get(0).getStatusCode());
            }
        }

        @Test
        @DisplayName("a 503: the default, reported once with status 503, and the failure is not cached")
        void serviceUnavailable() {
            server.enqueue(json(503, "{\"detail\":\"Service Unavailable\"}"));
            server.enqueue(serves(true, "10", "number"));
            try (ExperimentationClient client = client()) {
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
                assertEquals(503, errors.get(0).getStatusCode());
                assertEquals(10.0, client.getNumberValue(USER, "f", -1));
                assertEquals(Collections.singletonList("value"), operations);
                assertEquals(2, server.getRequestCount());
            }
        }

        @Test
        @DisplayName("a dropped connection: the default, reported once as a network error")
        void aDroppedConnection() {
            // Three, as ExperimentationClientTest's dropped connection: OkHttp may retry by itself.
            for (int i = 0; i < 3; i++) {
                server.enqueue(new MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST));
            }
            try (ExperimentationClient client = client()) {
                Object fallback = new Object();
                assertSame(fallback, client.getJsonValue(USER, "f", fallback));
                assertEquals(Collections.singletonList("value"), operations);
                assertEquals(0, errors.get(0).getStatusCode());
                assertTrue(errors.get(0).getMessage().startsWith("Network error"), errors.get(0).getMessage());
            }
        }

        @Test
        @DisplayName("a 2xx body that is not JSON, or JSON null: the default, reported once")
        void anUnreadableBody() {
            server.enqueue(json(200, "not json"));
            server.enqueue(json(200, "null"));
            try (ExperimentationClient client = client()) {
                assertEquals("fb", client.getStringValue(USER, "f", "fb"));
                assertEquals("fb", client.getStringValue(User.builder("u-2").build(), "f", "fb"));
                assertEquals(Arrays.asList("value", "value"), operations);
            }
        }

        @Test
        @DisplayName("a base URL OkHttp cannot read: the default, reported once, the cause kept")
        void aBaseUrlThatCannotBeRead() {
            try (ExperimentationClient client = client("not a url")) {
                assertEquals("fb", client.getStringValue(USER, "f", "fb"));
                assertEquals(Collections.singletonList("value"), operations);
                assertTrue(errors.get(0).getCause() instanceof IllegalArgumentException,
                        String.valueOf(errors.get(0).getCause()));
            }
        }
    }

    // =========================================================================
    // Numbers
    // =========================================================================

    @Nested
    @DisplayName("numbers")
    class Numbers {

        @Test
        @DisplayName("an integer (Jackson's Integer) is returned as a double: 10 is 10.0")
        void anIntegerIsADouble() {
            server.enqueue(servesNumber("10"));
            try (ExperimentationClient client = client()) {
                assertTrue(client.evaluateFeatureFlag(USER, "f").getValue() instanceof Integer);
                assertEquals(10.0, client.getNumberValue(USER, "f", -1));
                assertNothingReported();
            }
        }

        @Test
        @DisplayName("a decimal (Jackson's Double) is returned as it is: 2.5 is 2.5")
        void aDecimalIsKept() {
            server.enqueue(servesNumber("2.5"));
            try (ExperimentationClient client = client()) {
                assertTrue(client.evaluateFeatureFlag(USER, "f").getValue() instanceof Double);
                assertEquals(2.5, client.getNumberValue(USER, "f", -1));
                assertNothingReported();
            }
        }

        @Test
        @DisplayName("2^53-1 and -(2^53-1) (Jackson's Long) are returned")
        void theLargestSafeIntegersAreReturned() {
            server.enqueue(servesNumber("9007199254740991"));
            server.enqueue(servesNumber("-9007199254740991"));
            try (ExperimentationClient client = client()) {
                assertTrue(client.evaluateFeatureFlag(USER, "max").getValue() instanceof Long);
                assertEquals(9007199254740991.0, client.getNumberValue(USER, "max", -1));
                assertEquals(-9007199254740991.0, client.getNumberValue(USER, "min", -1));
                assertNothingReported();
            }
        }

        @Test
        @DisplayName("2^53 (a Long) is refused: the default, INVALID_RESPONSE")
        void twoToTheFiftyThirdIsRefused() {
            server.enqueue(servesNumber("9007199254740992"));
            try (ExperimentationClient client = client()) {
                assertTrue(client.evaluateFeatureFlag(USER, "f").getValue() instanceof Long);
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
            }
        }

        @Test
        @DisplayName("-2^53, an integer beyond a Long (a BigInteger) and 1e300 (a finite Double) are refused")
        void otherLargeNumbersAreRefused() {
            server.enqueue(servesNumber("-9007199254740992"));
            server.enqueue(servesNumber("123456789012345678901234567890"));
            server.enqueue(servesNumber("1e300"));
            try (ExperimentationClient client = client()) {
                assertTrue(client.evaluateFeatureFlag(USER, "a").getValue() instanceof Long);
                assertTrue(client.evaluateFeatureFlag(USER, "b").getValue() instanceof BigInteger);
                assertTrue(client.evaluateFeatureFlag(USER, "c").getValue() instanceof Double);
                assertEquals(-1.0, client.getNumberValue(USER, "a", -1));
                assertEquals(-1.0, client.getNumberValue(USER, "b", -1));
                assertEquals(-1.0, client.getNumberValue(USER, "c", -1));
                assertEquals(Arrays.asList("value", "value", "value"), operations);
                for (ExperimentationException error : errors) {
                    assertEquals(FlagValueException.INVALID_RESPONSE, ((FlagValueException) error).getCode());
                }
            }
        }

        @Test
        @DisplayName("true served as a number is refused: the default, INVALID_RESPONSE")
        void trueIsNotANumber() {
            server.enqueue(servesNumber("true"));
            try (ExperimentationClient client = client()) {
                assertEquals(-1.0, client.getNumberValue(USER, "f", -1));
                assertReportedOnce(FlagValueException.INVALID_RESPONSE);
            }
        }

        @Test
        @DisplayName("the range check reads each Number class exactly, before any conversion to double")
        void theRangeCheckPerClass() {
            long max = 9007199254740991L;
            for (Object ok : Arrays.<Object>asList(0, 10, Integer.MIN_VALUE, (short) 7, (byte) -3, max, -max,
                    2.5, -0.0, 2.5f, BigInteger.valueOf(max), new BigDecimal("2.5"), new BigDecimal("-9007199254740991"))) {
                assertTrue(ExperimentationClient.isPortableNumber(ok), "accepted: " + ok + " (" + ok.getClass().getSimpleName() + ")");
            }
            for (Object refused : Arrays.<Object>asList(max + 1, -max - 1, Long.MIN_VALUE, Long.MAX_VALUE,
                    BigInteger.valueOf(max + 1), BigInteger.TEN.pow(30), 9007199254740992.0, 1e300,
                    Double.NaN, Double.POSITIVE_INFINITY, Float.NEGATIVE_INFINITY, Float.NaN,
                    new BigDecimal("9007199254740991.5"), new AtomicLong(1), true, "10", null)) {
                assertFalse(ExperimentationClient.isPortableNumber(refused), "refused: " + refused);
            }
        }
    }
}

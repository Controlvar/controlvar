package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.exception.FlagValueException;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.OkHttpClient;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The typed getters against {@code tests/sdk-contract/value-vectors.json} (#1810): for each answer
 * of {@code GET /api/v1/feature-flags/evaluate/{key}} in the file, what {@code getStringValue},
 * {@code getNumberValue} or {@code getJsonValue} returns and what it passes to the error listener.
 * The served half is generated from the server's own answer builder (drift-checked in the backend
 * suite); the malformed half is answers no server sends.
 *
 * <p>Each answer's body goes through {@link MockWebServer} as the bytes the file holds, so the
 * client's own HTTP call and JSON mapping read it, as they read a server's.
 *
 * <p>One test, not one per vector: it counts the vectors it answered and checked, and requires
 * 49, so an empty or truncated file fails here instead of running nothing.
 */
class ValueVectorsTest {

    /** Surefire runs this module in sdk/java/core. */
    private static final Path VECTORS = Paths.get("..", "..", "..", "tests", "sdk-contract", "value-vectors.json");
    /** Pinned: 35 served + 14 malformed (backend/tests/unit/services/test_sdk_value_vectors.py). */
    private static final int EXPECTED = 49;
    private static final User USER = User.builder("value-vector-user").build();
    private static final String FLAG_KEY = "value-vector";

    private final ObjectMapper mapper = new ObjectMapper();
    private MockWebServer server;

    @BeforeEach
    void setUp() throws IOException {
        server = new MockWebServer();
        server.start();
    }

    @AfterEach
    void tearDown() throws IOException {
        server.shutdown();
    }

    private List<JsonNode> load() throws IOException {
        assertTrue(Files.isRegularFile(VECTORS), "missing " + VECTORS.toAbsolutePath().normalize());
        JsonNode document = mapper.readTree(Files.readAllBytes(VECTORS));
        List<JsonNode> vectors = new ArrayList<>();
        for (String half : new String[] {"served", "malformed"}) {
            for (JsonNode vector : document.path(half)) {
                vectors.add(vector);
            }
        }
        return vectors;
    }

    private ExperimentationClient client(List<ExperimentationException> errors, List<String> operations) {
        SdkConfig config = SdkConfig.builder("test-api-key", server.url("/").toString())
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

    @Test
    void theTypedGettersAnswerEachValueVectorAsTheContractSays() throws IOException {
        List<String> problems = new ArrayList<>();
        int answered = 0;
        int asserted = 0;
        for (JsonNode vector : load()) {
            String name = vector.path("name").asText();
            JsonNode response = vector.path("response");
            // The file's body, re-written from its JSON tree: the client reads these bytes.
            server.enqueue(new MockResponse()
                    .setResponseCode(response.path("status").asInt())
                    .setHeader("Content-Type", "application/json")
                    .setBody(mapper.writeValueAsString(response.path("body"))));

            List<ExperimentationException> errors = new ArrayList<>();
            List<String> operations = new ArrayList<>();
            String getter = vector.path("getter").asText();
            JsonNode defaultNode = vector.path("default");
            JsonNode expect = vector.path("expect");
            boolean expectDefault = expect.isTextual() && "default".equals(expect.asText());
            String got;
            try (ExperimentationClient client = client(errors, operations)) {
                switch (getter) {
                    case "string": {
                        // A new String, so "the default" is checked by identity.
                        String fallback = new String(defaultNode.asText());
                        String value = client.getStringValue(USER, FLAG_KEY, fallback);
                        got = String.valueOf(value);
                        if (expectDefault ? value != fallback : !expect.isTextual() || !expect.asText().equals(value)) {
                            problems.add(name + ": got " + quote(value) + ", expected " + describe(expect));
                        }
                        break;
                    }
                    case "number": {
                        double fallback = defaultNode.asDouble();
                        double value = client.getNumberValue(USER, FLAG_KEY, fallback);
                        got = String.valueOf(value);
                        boolean same = expectDefault
                                ? Double.compare(value, fallback) == 0
                                : expect.isNumber() && Double.compare(value, expect.asDouble()) == 0;
                        if (!same) {
                            problems.add(name + ": got " + value + ", expected " + describe(expect));
                        }
                        break;
                    }
                    case "json": {
                        Object fallback = mapper.convertValue(defaultNode, Object.class);
                        Object value = client.getJsonValue(USER, FLAG_KEY, fallback);
                        got = String.valueOf(value);
                        boolean same = expectDefault
                                ? value == fallback
                                : (value instanceof Map || value instanceof List) && mapper.valueToTree(value).equals(expect);
                        if (!same) {
                            problems.add(name + ": got " + value + ", expected " + describe(expect));
                        }
                        break;
                    }
                    default:
                        problems.add(name + ": unknown getter " + getter);
                        continue;
                }
            }
            answered += 1;

            int reports = 0;
            for (String operation : operations) {
                if ("value".equals(operation)) reports += 1;
            }
            if (reports != operations.size()) {
                problems.add(name + ": reported as " + operations);
            }
            int expectedReports = vector.path("reported").asBoolean() ? 1 : 0;
            if (reports != expectedReports) {
                problems.add(name + ": reported " + reports + ", expected " + expectedReports + " (got " + got + ")");
            }
            // A null code is not compared: this SDK passes a 404 or a 503 on as the transport's
            // ExperimentationException, whose status is the HTTP status.
            JsonNode code = vector.path("error_code");
            if (code.isTextual()) {
                ExperimentationException first = errors.isEmpty() ? null : errors.get(0);
                String gotCode = first instanceof FlagValueException ? ((FlagValueException) first).getCode() : null;
                if (!code.asText().equals(gotCode)) {
                    problems.add(name + ": code " + gotCode + ", expected " + code.asText());
                }
            }
            asserted += 1;
        }

        assertEquals(new ArrayList<String>(), problems);
        assertEquals("answered " + EXPECTED + " of " + EXPECTED + ", asserted " + EXPECTED,
                "answered " + answered + " of " + EXPECTED + ", asserted " + asserted);
        assertEquals(EXPECTED, server.getRequestCount(), "one request per vector");
    }

    private static String quote(String value) {
        return value == null ? "null" : "\"" + value + "\"";
    }

    private static String describe(JsonNode expect) {
        return expect.isTextual() && "default".equals(expect.asText()) ? "the default" : expect.toString();
    }
}

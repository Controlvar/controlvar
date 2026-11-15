package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.exception.FlagValueException;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.Exposure;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.TrackEvent;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.module.SimpleModule;
import com.fasterxml.jackson.databind.ser.std.ToStringSerializerBase;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.time.temporal.TemporalAccessor;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * Main client for the Controlvar SDK.
 *
 * <p>Feature flag evaluation and experiment assignment are decided <em>by the server</em>:
 * every call goes to the public API with your {@code X-API-Key}, the server buckets the
 * user (sticky per user + experiment), and the client caches the answer per user + key for
 * the configured TTL. Nothing is bucketed locally.
 *
 * <ul>
 *   <li>{@link #evaluateFeatureFlag} — {@code GET /api/v1/feature-flags/evaluate/{key}?user_id=…&context=…}</li>
 *   <li>{@link #getStringValue}, {@link #getNumberValue}, {@link #getJsonValue} — the value a
 *       flag serves the user, from the same request and cache as {@link #evaluateFeatureFlag}</li>
 *   <li>{@link #getExperimentAssignment} — {@code POST /api/v1/tracking/assign}</li>
 *   <li>{@link #trackEvent(TrackEvent)} — {@code POST /api/v1/tracking/track}, or
 *       {@code POST /api/v1/tracking/batch} when the event has no key (fan-out)</li>
 * </ul>
 *
 * <h2>Usage</h2>
 * <pre>
 *     SdkConfig config = SdkConfig.builder("my-api-key", "https://api.example.com").build();
 *
 *     try (ExperimentationClient client = new ExperimentationClient(config)) {
 *         User user = User.builder("user-123").attribute("country", "US").build();
 *
 *         ExperimentAssignment a = client.getExperimentAssignment(user, "checkout_flow");
 *         if (client.isFeatureEnabled(user, "new_search")) { ... }
 *
 *         client.trackEvent("user-123", "purchase", Map.of("sku", "pro"), "checkout_flow", null, 49.99);
 *     }
 * </pre>
 *
 * <p>The client is thread-safe. It implements {@link AutoCloseable} — always close it
 * (or use try-with-resources) to release the underlying HTTP connection pool.
 */
public class ExperimentationClient implements AutoCloseable {

    static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");

    /** Maximum events per {@code POST /api/v1/tracking/batch} request. */
    static final int BATCH_LIMIT = 100;

    /** Separator inside cache keys; a NUL keeps "a"+"b:c" and "a:b"+"c" distinct. */
    private static final char KEY_SEPARATOR = '\0';

    private final SdkConfig config;
    private final OkHttpClient httpClient;
    private final ObjectMapper objectMapper;
    private final AssignmentCache<FlagEvaluation> flagCache;
    private final AssignmentCache<ExperimentAssignment> assignmentCache;

    /**
     * Creates a new ExperimentationClient with the provided configuration.
     *
     * @param config SDK configuration (API key, base URL, timeouts, cache settings)
     */
    public ExperimentationClient(SdkConfig config) {
        this(config, new OkHttpClient.Builder()
                .connectTimeout(config.getTimeoutMs(), TimeUnit.MILLISECONDS)
                .readTimeout(config.getTimeoutMs(), TimeUnit.MILLISECONDS)
                .writeTimeout(config.getTimeoutMs(), TimeUnit.MILLISECONDS)
                .build());
    }

    /**
     * Package-private constructor for testing with a custom OkHttpClient (e.g., MockWebServer).
     */
    ExperimentationClient(SdkConfig config, OkHttpClient httpClient) {
        this.config = config;
        this.httpClient = httpClient;
        this.objectMapper = new ObjectMapper().registerModule(isoDateTimes());
        this.flagCache = new AssignmentCache<>(config.getCacheSize(), config.getCacheTtlMs());
        this.assignmentCache = new AssignmentCache<>(config.getCacheSize(), config.getCacheTtlMs());
    }

    // =========================================================================
    // Feature flags
    // =========================================================================

    /**
     * Evaluates a feature flag for a user via
     * {@code GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…&context=…}.
     *
     * <p>The user's attributes are sent as {@code context}: compact JSON, percent-encoded, so
     * the flag's targeting rules match against them. A user with no attributes sends no
     * {@code context}. An {@link Instant}, {@link LocalDate}, {@link LocalDateTime},
     * {@link OffsetDateTime} or {@link ZonedDateTime} value is sent as an ISO-8601 string.
     *
     * <p>Successful evaluations are cached per user + flag for the configured TTL, whatever
     * the attributes: call {@link #clearCache()} or {@link #invalidateCache(String, String)}
     * after changing a user's attributes. Failures are never cached.
     *
     * @param user    the user to evaluate the flag for
     * @param flagKey the feature flag key (e.g., "new-checkout-flow")
     * @return the server's evaluation (key, enabled, config)
     * @throws ExperimentationException on a non-2xx response (404 when the flag key is
     *                                  unknown, 401 for a bad key; a flag that is not ACTIVE is a
     *                                  200 answer with enabled false, not an error), a network error,
     *                                  a 2xx body that is not the expected JSON, or attributes
     *                                  that cannot be written as JSON (nothing is sent then)
     */
    public FlagEvaluation evaluateFeatureFlag(User user, String flagKey) {
        if (user == null) throw new IllegalArgumentException("user must not be null");
        if (flagKey == null || flagKey.isEmpty()) throw new IllegalArgumentException("flagKey must not be empty");

        // User + flag only, not the attributes: they are assumed stable per user.
        String cacheKey = cacheKey(user.getUserId(), flagKey);
        FlagEvaluation cached = flagCache.get(cacheKey);
        if (cached != null) {
            return cached;
        }

        HttpUrl url;
        try {
            url = evaluateUrl(user, flagKey);
        } catch (IOException e) {
            throw new ExperimentationException(
                    "Failed to serialize the user's attributes for flag '" + flagKey + "': " + e.getMessage(), e);
        }
        Request request = requestBuilder(url).get().build();

        try (Response response = httpClient.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new ExperimentationException(
                        "API error evaluating flag '" + flagKey + "': HTTP " + response.code(),
                        response.code()
                );
            }
            ResponseBody body = response.body();
            if (body == null) {
                throw new ExperimentationException("Empty response body from API for flag: " + flagKey);
            }
            String json = body.string();
            FlagEvaluation evaluation = readAnswer(json, FlagEvaluation.class, "evaluating flag '" + flagKey + "'");
            if (evaluation.getKey() == null || evaluation.getKey().isEmpty()) {
                evaluation.setKey(flagKey);
            }
            flagCache.put(cacheKey, evaluation);
            return evaluation;
        } catch (IOException e) {
            // The exception's own text may name the URL; its query is left out (#1551).
            throw new ExperimentationException(
                    "Network error evaluating flag '" + flagKey + "': " + withoutQuery(e.getMessage(), url), e);
        }
    }

    /**
     * Convenience wrapper around {@link #evaluateFeatureFlag} that never throws
     * {@link ExperimentationException}: returns {@code false} when the flag is off,
     * unknown, not ACTIVE, or the API cannot be reached.
     *
     * @param user    the user to evaluate the flag for
     * @param flagKey the feature flag key
     * @return true when the server reports the flag enabled for this user
     */
    public boolean isFeatureEnabled(User user, String flagKey) {
        try {
            return evaluateFeatureFlag(user, flagKey).isEnabled();
        } catch (ExperimentationException e) {
            return false;
        }
    }

    // =========================================================================
    // Flag values
    // =========================================================================

    /**
     * The string a string flag serves this user: its on value when the flag is on for them, its
     * off value otherwise. Evaluated as {@link #evaluateFeatureFlag} evaluates, with the same
     * request and the same cache: reading a value records no exposure.
     *
     * <p>Never throws. Returns {@code defaultValue}, and passes the reason to the
     * {@link ErrorListener} with the operation {@code "value"}, when the request fails, the flag
     * is unknown (404), the server sends no value (a server without flag values), the flag serves
     * another type ({@link FlagValueException#TYPE_MISMATCH}), or {@code user} is {@code null} or
     * {@code flagKey} empty (then nothing is sent; {@link FlagValueException#INVALID_RESPONSE}).
     * Unlike {@link #evaluateFeatureFlag}, it throws no {@link IllegalArgumentException}.
     *
     * @param user         the user to evaluate the flag for
     * @param flagKey      the flag key
     * @param defaultValue what to return when there is no string value to return
     * @return the flag's string value, or {@code defaultValue}
     */
    public String getStringValue(User user, String flagKey, String defaultValue) {
        Object value = typedValue(user, flagKey, "string", "getStringValue");
        return value == null ? defaultValue : (String) value;
    }

    /**
     * The number a number flag serves this user, as a {@code double}: an integer as saved
     * ({@code 10} is {@code 10.0}) or a decimal. See {@link #getStringValue} for when it returns
     * {@code defaultValue} instead; a number beyond ±(2^53−1), which no server sends, is one of
     * those ({@link FlagValueException#INVALID_RESPONSE}). Never throws.
     *
     * @param user         the user to evaluate the flag for
     * @param flagKey      the flag key
     * @param defaultValue what to return when there is no number value to return
     * @return the flag's number value, or {@code defaultValue}
     */
    public double getNumberValue(User user, String flagKey, double defaultValue) {
        Object value = typedValue(user, flagKey, "number", "getNumberValue");
        return value == null ? defaultValue : ((Number) value).doubleValue();
    }

    /**
     * The JSON object or array a json flag serves this user: a {@code Map<String, Object>} or a
     * {@code List<Object>}, holding strings, numbers, booleans, {@code null}s, maps and lists. A
     * new copy on every call: changing it changes nothing the client keeps. See
     * {@link #getStringValue} for when it returns {@code defaultValue} instead (the same object,
     * not a copy). Never throws.
     *
     * @param user         the user to evaluate the flag for
     * @param flagKey      the flag key
     * @param defaultValue what to return when there is no JSON value to return
     * @return a copy of the flag's JSON value, or {@code defaultValue}
     */
    public Object getJsonValue(User user, String flagKey, Object defaultValue) {
        Object value = typedValue(user, flagKey, "json", "getJsonValue");
        return value == null ? defaultValue : value;
    }

    /**
     * The typed getters' one body, as the JavaScript SDK's: the value the flag serves this user
     * when it is of {@code type} (a copy), and otherwise {@code null}, with the reason passed to
     * the error listener once. The order of the checks is the contract
     * ({@code tests/sdk-contract/value-vectors.json}): no value; a served type other than
     * {@code type} ({@code value_type} when the answer has one, else the value's own JSON type);
     * a value that is not of {@code type}.
     */
    private Object typedValue(User user, String flagKey, String type, String method) {
        ExperimentationException reason;
        try {
            if (user == null) {
                throw new FlagValueException(
                        "user is null; " + method + " sent nothing and returned your default",
                        FlagValueException.INVALID_RESPONSE);
            }
            if (flagKey == null || flagKey.isEmpty()) {
                throw new FlagValueException(
                        "flagKey is empty; " + method + " sent nothing and returned your default",
                        FlagValueException.INVALID_RESPONSE);
            }
            FlagEvaluation evaluation = evaluateFeatureFlag(user, flagKey);
            Object value = evaluation.getValue();
            String flag = "Flag '" + flagKey + "'";
            if (value == null) {
                throw new FlagValueException(flag + " came back without a value (from a server without flag "
                        + "values, or one that cannot serve this flag's stored value); " + method
                        + " returned your default", FlagValueException.INVALID_RESPONSE);
            }
            String served = evaluation.getValueType() != null ? evaluation.getValueType() : jsonTypeOf(value);
            if (!served.equals(type)) {
                throw new FlagValueException(flag + " serves " + describeType(served) + "; " + method
                        + " returned your default", FlagValueException.TYPE_MISMATCH);
            }
            if (!isValueOfType(value, type)) {
                throw new FlagValueException(flag + " serves " + describeType(type) + ", but the server's "
                        + "value is not one; " + method + " returned your default",
                        FlagValueException.INVALID_RESPONSE);
            }
            return value;
        } catch (ExperimentationException e) {
            reason = e;
        } catch (RuntimeException e) {
            // Not the SDK's own kind (an unreadable base URL is OkHttp's IllegalArgumentException):
            // reported as one, with what was thrown as its cause.
            reason = new ExperimentationException(
                    "Could not evaluate flag '" + flagKey + "' (" + e.getClass().getName() + "); "
                            + method + " returned your default", e);
        }
        ErrorHook.call(config.getErrorListener(), reason, "value");
        return null;
    }

    /** A JSON value's type as the server names value types; the answer's value is one of these. */
    private static String jsonTypeOf(Object value) {
        if (value instanceof String) return "string";
        if (value instanceof Boolean) return "boolean";
        if (value instanceof Number) return "number";
        if (value instanceof Map || value instanceof List) return "json";
        return value.getClass().getName();
    }

    /** A value type as the typed getters' messages name it. */
    private static String describeType(String type) {
        switch (type) {
            case "boolean": return "a boolean";
            case "string": return "a string";
            case "number": return "a number";
            case "json": return "a JSON value";
            default: return "a value of type \"" + (type.length() > 64 ? type.substring(0, 64) : type) + "\"";
        }
    }

    /**
     * Whether {@code value} is a value of {@code type} as the server serves one: a string; a
     * number that is finite and within ±(2^53−1) ({@link #isPortableNumber}); a JSON object or
     * array.
     */
    private static boolean isValueOfType(Object value, String type) {
        switch (type) {
            case "string": return value instanceof String;
            case "number": return isPortableNumber(value);
            case "json": return value instanceof Map || value instanceof List;
            default: return false;
        }
    }

    /** 2^53−1: the largest integer every SDK's numbers hold exactly. */
    private static final long MAX_SAFE_INTEGER = 9007199254740991L;

    /**
     * Whether {@code value} is a number the server serves: finite and within ±(2^53−1). Each
     * class the JSON reader gives ({@code Integer}, {@code Long}, {@code BigInteger},
     * {@code Double}; and {@code Short}, {@code Byte}, {@code Float}, {@code BigDecimal}) is
     * checked as it is, before any conversion to {@code double} could round it into range. A
     * {@code Boolean} is not a number, and neither is any other class.
     */
    static boolean isPortableNumber(Object value) {
        if (value instanceof Integer || value instanceof Short || value instanceof Byte) {
            return true;
        }
        if (value instanceof Long) {
            long number = (Long) value;
            return number >= -MAX_SAFE_INTEGER && number <= MAX_SAFE_INTEGER;
        }
        if (value instanceof Double || value instanceof Float) {
            double number = ((Number) value).doubleValue();
            return !Double.isNaN(number) && !Double.isInfinite(number) && Math.abs(number) <= MAX_SAFE_INTEGER;
        }
        if (value instanceof BigInteger) {
            return ((BigInteger) value).abs().compareTo(BigInteger.valueOf(MAX_SAFE_INTEGER)) <= 0;
        }
        if (value instanceof BigDecimal) {
            return ((BigDecimal) value).abs().compareTo(BigDecimal.valueOf(MAX_SAFE_INTEGER)) <= 0;
        }
        return false;
    }

    // =========================================================================
    // Experiments
    // =========================================================================

    /**
     * Assigns a user to an experiment variant via {@code POST /api/v1/tracking/assign}
     * (sticky server-side; the server records the exposure).
     *
     * <p>The user's attributes are sent as the assignment {@code context} for targeting
     * rules, written as {@link #evaluateFeatureFlag} writes them. Successful assignments are
     * cached per user + experiment for the configured TTL; failures are never cached.
     *
     * <p>When the server answers {@code "assigned": true} and no unexpired answer for this user
     * and experiment was cached, the configured {@link ExposureListener} is called on this
     * thread before this method returns. A cached answer calls nothing, and nothing the
     * listener throws reaches the caller, except a {@link VirtualMachineError}.
     *
     * @param user          the user to assign
     * @param experimentKey the experiment key (e.g., "checkout-button-color")
     * @return the assignment (never null)
     * @throws ExperimentationException on a non-2xx response (404 when no experiment has the
     *                                  key, or for a draft with no control variant), a network
     *                                  error, or a 2xx body that is not the expected JSON; an
     *                                  experiment that is not ACTIVE answers its control variant
     */
    public ExperimentAssignment getExperimentAssignment(User user, String experimentKey) {
        if (user == null) throw new IllegalArgumentException("user must not be null");
        if (experimentKey == null || experimentKey.isEmpty()) {
            throw new IllegalArgumentException("experimentKey must not be empty");
        }

        String cacheKey = cacheKey(user.getUserId(), experimentKey);
        ExperimentAssignment cached = assignmentCache.get(cacheKey);
        if (cached != null) {
            return cached;
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("experiment_key", experimentKey);
        payload.put("user_id", user.getUserId());
        if (!user.getAttributes().isEmpty()) {
            payload.put("context", user.getAttributes());
        }

        ExposureListener listener = config.getExposureListener();
        ExperimentAssignment assignment;
        Exposure exposure = null;
        try {
            Request request = requestBuilder(config.getBaseUrl() + "/api/v1/tracking/assign")
                    .post(RequestBody.create(objectMapper.writeValueAsString(payload), JSON))
                    .build();

            try (Response response = httpClient.newCall(request).execute()) {
                if (!response.isSuccessful()) {
                    throw new ExperimentationException(
                            "API error assigning experiment '" + experimentKey + "': HTTP " + response.code(),
                            response.code()
                    );
                }
                ResponseBody body = response.body();
                if (body == null) {
                    throw new ExperimentationException("Empty response body for experiment: " + experimentKey);
                }
                String json = body.string();
                assignment = readAnswer(json, ExperimentAssignment.class,
                        "assigning experiment '" + experimentKey + "'");
                if (assignment.getExperimentKey() == null || assignment.getExperimentKey().isEmpty()) {
                    assignment.setExperimentKey(experimentKey);
                }
                if (assignment.getUserId() == null || assignment.getUserId().isEmpty()) {
                    assignment.setUserId(user.getUserId());
                }
                // The server recorded an exposure for this request only if it said
                // "assigned": true; a missing field is not taken as true.
                boolean recorded = listener != null && saysAssigned(json);
                // fill() is put() that also says whether the slot held no live answer. Two
                // threads that asked for the same new user and experiment at once each store an
                // answer here; only the first is told the slot was empty.
                if (assignmentCache.fill(cacheKey, assignment) && recorded) {
                    exposure = new Exposure(assignment.getExperimentKey(), assignment.getUserId(),
                            assignment.getVariantId(), assignment.getVariantName());
                }
            }
        } catch (IOException e) {
            throw new ExperimentationException(
                    "Network error assigning experiment '" + experimentKey + "': " + e.getMessage(), e);
        }
        if (exposure != null) {
            // Last, outside the request's try blocks, with no cache lock held. The listener's
            // failure becomes a hook error (ExposureHook.HookError), which this SDK drops (its
            // ErrorListener hears only the typed getters' fallbacks): the answer below is
            // returned whatever the listener did.
            ExposureHook.call(listener, exposure);
        }
        return assignment;
    }

    // =========================================================================
    // Tracking (fire-and-forget)
    // =========================================================================
    //
    // Every event sent carries an event_id: the one set with TrackEvent.Builder#eventId, or a
    // new random UUID made once per event per call and shared by that event's fan-out entries.
    // OkHttp can send a request again by itself when the connection fails after the request was
    // written (on a kept-alive connection, or when another route exists); whenever it does, the
    // request is the same body, with the same ids, so the server stores each event once.

    /**
     * Tracks an event with no experiment/flag attribution. Fire-and-forget: never throws.
     *
     * <p>The event is fanned out via {@code POST /api/v1/tracking/batch} to every experiment
     * the user was assigned to and every flag evaluated for the user by this client (from
     * the cache). If nothing is cached for the user, nothing is sent.
     *
     * @param userId     the user's unique identifier
     * @param eventName  the event name (e.g., "purchase"); experiment metrics match on it
     * @param properties optional event properties, sent as {@code metadata} (may be null)
     */
    public void trackEvent(String userId, String eventName, Map<String, Object> properties) {
        trackEvent(userId, eventName, properties, null, null, null);
    }

    /**
     * Tracks an event attributed to an experiment and/or a feature flag. Fire-and-forget:
     * never throws.
     *
     * <p>With {@code experimentKey} or {@code featureFlagKey} set the client sends one
     * {@code POST /api/v1/tracking/track}; with both null it fans out like
     * {@link #trackEvent(String, String, Map)}.
     *
     * @param userId         the user's unique identifier
     * @param eventName      the event name (e.g., "purchase")
     * @param properties     optional event properties, sent as {@code metadata} (may be null)
     * @param experimentKey  experiment to attribute the event to (may be null)
     * @param featureFlagKey feature flag to attribute the event to (may be null)
     * @param value          optional numeric value such as revenue (may be null)
     */
    public void trackEvent(String userId, String eventName, Map<String, Object> properties,
                           String experimentKey, String featureFlagKey, Double value) {
        if (userId == null || userId.isEmpty() || eventName == null || eventName.isEmpty()) {
            return; // silently ignore invalid inputs for fire-and-forget
        }
        try {
            trackEvent(TrackEvent.builder(userId, eventName)
                    .properties(properties)
                    .experimentKey(experimentKey)
                    .featureFlagKey(featureFlagKey)
                    .value(value)
                    .build());
        } catch (Exception ignored) {
            // Fire and forget
        }
    }

    /**
     * Tracks one event asynchronously. Fire-and-forget: never throws, and failures are
     * silently ignored — tracking must never affect the user experience.
     *
     * <p>The event is sent with its {@code event_id}, or, when it has none, with a random UUID
     * made for this call (shared by its fan-out entries): tracking the same {@code TrackEvent}
     * twice without an id sends two events.
     *
     * @param event the event to track (ignored when null)
     * @see #trackEventSync(TrackEvent) for a blocking variant that reports failures
     */
    public void trackEvent(TrackEvent event) {
        if (event == null) return;
        try {
            for (Request request : buildTrackRequests(Collections.singletonList(event))) {
                enqueue(request);
            }
        } catch (Exception ignored) {
            // Fire and forget: swallow serialization errors silently
        }
    }

    /**
     * Tracks several events asynchronously, at most 100 per request. Events without a key
     * are fanned out like {@link #trackEvent(String, String, Map)}. Never throws.
     *
     * <p>Each event is sent with its {@code event_id}, or with a random UUID of its own made
     * for this call.
     *
     * @param events the events to track (ignored when null or empty)
     */
    public void trackBatch(List<TrackEvent> events) {
        if (events == null || events.isEmpty()) return;
        try {
            for (Request request : buildTrackRequests(events)) {
                enqueue(request);
            }
        } catch (Exception ignored) {
            // Fire and forget
        }
    }

    /**
     * Blocking variant of {@link #trackEvent(TrackEvent)}: sends the request(s) on the
     * calling thread and reports failures.
     *
     * <p>A request that failed may still have been stored. To send the event again yourself
     * and have it stored once, give it an id with {@link TrackEvent.Builder#eventId(String)};
     * without one, each call sends a new id.
     *
     * @param event the event to track
     * @throws ExperimentationException on a non-2xx response or a network error
     */
    public void trackEventSync(TrackEvent event) {
        if (event == null) throw new IllegalArgumentException("event must not be null");
        trackBatchSync(Collections.singletonList(event));
    }

    /**
     * Blocking variant of {@link #trackBatch(List)}: sends the request(s) on the calling
     * thread and reports failures. Returns normally when there is nothing to send.
     *
     * @param events the events to track
     * @throws ExperimentationException on a non-2xx response or a network error
     */
    public void trackBatchSync(List<TrackEvent> events) {
        if (events == null || events.isEmpty()) return;
        List<Request> requests;
        try {
            requests = buildTrackRequests(events);
        } catch (IOException e) {
            throw new ExperimentationException("Failed to serialize events: " + e.getMessage(), e);
        }
        for (Request request : requests) {
            try (Response response = httpClient.newCall(request).execute()) {
                if (!response.isSuccessful()) {
                    throw new ExperimentationException(
                            "API error tracking events: HTTP " + response.code(), response.code());
                }
            } catch (IOException e) {
                throw new ExperimentationException("Network error tracking events: " + e.getMessage(), e);
            }
        }
    }

    // =========================================================================
    // Cache access
    // =========================================================================

    /**
     * Returns the cached (successful, unexpired) assignments for the user.
     *
     * @param userId the user's ID
     * @return assignments in cache order (never null)
     */
    public List<ExperimentAssignment> getCachedAssignments(String userId) {
        return assignmentCache.valuesWithPrefix(userPrefix(userId));
    }

    /**
     * Returns the keys of flags successfully evaluated (and still cached) for the user.
     *
     * @param userId the user's ID
     * @return flag keys in cache order (never null)
     */
    public List<String> getCachedFlagKeys(String userId) {
        List<String> keys = new ArrayList<>();
        for (FlagEvaluation evaluation : flagCache.valuesWithPrefix(userPrefix(userId))) {
            keys.add(evaluation.getKey());
        }
        return keys;
    }

    /**
     * Invalidates the cached evaluation and assignment for a specific user + key.
     *
     * @param userId the user's ID
     * @param key    the feature flag or experiment key
     */
    public void invalidateCache(String userId, String key) {
        String cacheKey = cacheKey(userId, key);
        flagCache.invalidate(cacheKey);
        assignmentCache.invalidate(cacheKey);
    }

    /**
     * Clears all cached evaluations and assignments.
     */
    public void clearCache() {
        flagCache.clear();
        assignmentCache.clear();
    }

    /**
     * Returns the current number of cached evaluations plus assignments.
     *
     * @return cache size
     */
    public int getCacheSize() {
        return flagCache.size() + assignmentCache.size();
    }

    /**
     * Closes the client, releasing the HTTP connection pool and clearing the cache.
     *
     * <p>After calling this method, the client should not be used.
     */
    @Override
    public void close() {
        httpClient.dispatcher().executorService().shutdown();
        httpClient.connectionPool().evictAll();
        clearCache();
    }

    // =========================================================================
    // Internals
    // =========================================================================

    /**
     * True only when the {@code /assign} answer's {@code assigned} is the JSON literal
     * {@code true}. A missing field, {@code null}, {@code "true"} or {@code 1} is not, and an
     * answer that cannot be read as a tree is not either: it never fails the call.
     */
    private boolean saysAssigned(String json) {
        try {
            JsonNode tree = objectMapper.readTree(json);
            if (tree == null) return false;
            JsonNode assigned = tree.path("assigned");
            return assigned.isBoolean() && assigned.booleanValue();
        } catch (IOException unreadable) {
            return false;
        }
    }

    /**
     * A 2xx answer's body read as {@code type}. A body that is not that JSON (not JSON, a field
     * of the wrong type, or {@code null}) is an {@link ExperimentationException} whose message
     * is fixed text, {@code "Invalid JSON in the response " + what}, with no cause: it is not a
     * network error, and nothing from the body is in it.
     */
    private <T> T readAnswer(String json, Class<T> type, String what) {
        T value;
        try {
            value = objectMapper.readValue(json, type);
        } catch (IOException unreadable) {
            value = null;
        }
        if (value == null) {
            throw new ExperimentationException("Invalid JSON in the response " + what);
        }
        return value;
    }

    /**
     * Writes a {@code java.time} value as the other SDKs send a date: an ISO-8601 string. An
     * {@link Instant} in UTC ({@code 2026-11-03T12:00:00Z}), a {@link LocalDate} as a date
     * ({@code 2026-11-03}), a {@link LocalDateTime} with no offset
     * ({@code 2026-11-03T12:00:00}), and an {@link OffsetDateTime} or {@link ZonedDateTime} with
     * its offset ({@code 2026-11-03T12:00:00+01:00}): a zone's ID is left out, as the server
     * reads the text with Python's {@code datetime.fromisoformat}. Jackson refuses these five
     * classes unless a module handles them; this one needs nothing beyond jackson-databind.
     * Any other value is written as Jackson writes it, and one Jackson refuses fails the call.
     */
    private static SimpleModule isoDateTimes() {
        SimpleModule module = new SimpleModule("controlvar-iso-date-times");
        iso(module, Instant.class, DateTimeFormatter.ISO_INSTANT);
        iso(module, LocalDate.class, DateTimeFormatter.ISO_LOCAL_DATE);
        iso(module, LocalDateTime.class, DateTimeFormatter.ISO_LOCAL_DATE_TIME);
        iso(module, OffsetDateTime.class, DateTimeFormatter.ISO_OFFSET_DATE_TIME);
        iso(module, ZonedDateTime.class, DateTimeFormatter.ISO_OFFSET_DATE_TIME);
        // A java.util.Date is an instant. Jackson would write epoch milliseconds, which the
        // server reads as epoch seconds. getTime() rather than toInstant(): java.sql.Date
        // (a subclass this serializer also catches) throws from toInstant().
        module.addSerializer(Date.class, new ToStringSerializerBase(Date.class) {
            @Override
            public String valueToString(Object value) {
                return Instant.ofEpochMilli(((Date) value).getTime()).toString();
            }
        });
        return module;
    }

    private static void iso(SimpleModule module, Class<? extends TemporalAccessor> type, DateTimeFormatter format) {
        module.addSerializer(type, new ToStringSerializerBase(type) {
            @Override
            public String valueToString(Object value) {
                return format.format((TemporalAccessor) value);
            }
        });
    }

    /**
     * The flag evaluation URL: {@code user_id}, then {@code context} when the user has
     * attributes, as compact JSON that OkHttp percent-encodes ({@code {"plan":"pro"}} travels as
     * {@code context=%7B%22plan%22%3A%22pro%22%7D}). With no attributes there is no
     * {@code context}, and the URL is the one sent before attributes were. The server's limits
     * on a context (size, depth) are not checked here: a context over them is its 422.
     *
     * @throws IOException when the attributes cannot be written as JSON
     */
    private HttpUrl evaluateUrl(User user, String flagKey) throws IOException {
        HttpUrl.Builder url = HttpUrl.get(config.getBaseUrl()).newBuilder()
                .addEncodedPathSegments("api/v1/feature-flags/evaluate")
                .addPathSegment(flagKey)
                .addQueryParameter("user_id", user.getUserId());
        if (!user.getAttributes().isEmpty()) {
            url.addQueryParameter("context", objectMapper.writeValueAsString(user.getAttributes()));
        }
        return url.build();
    }

    /**
     * {@code text} without {@code url}'s query as sent: the query holds the user's id and
     * attributes ({@code ?user_id=…&context=…}), and no error message names it (#1551).
     */
    private static String withoutQuery(String text, HttpUrl url) {
        String query = url.encodedQuery();
        if (text == null || query == null || query.isEmpty()) return text;
        return text.replace("?" + query, "").replace(query, "");
    }

    private static String userPrefix(String userId) {
        return userId + KEY_SEPARATOR;
    }

    private static String cacheKey(String userId, String key) {
        return userPrefix(userId) + key;
    }

    private Request.Builder requestBuilder(String url) {
        return requestBuilder(HttpUrl.get(url));
    }

    private Request.Builder requestBuilder(HttpUrl url) {
        return new Request.Builder()
                .url(url)
                .header("X-API-Key", config.getApiKey())
                .header("Accept", "application/json")
                .header("Content-Type", "application/json");
    }

    private void enqueue(Request request) {
        httpClient.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, IOException e) {
                // Fire and forget: swallow network errors silently
            }

            @Override
            public void onResponse(Call call, Response response) {
                response.close();
            }
        });
    }

    /**
     * Builds the HTTP requests for a list of events: one {@code /tracking/track} per keyed
     * event; unkeyed events are fanned out and all fan-out entries are sent through
     * {@code /tracking/batch} in chunks of {@link #BATCH_LIMIT}.
     *
     * <p>Each event gets its {@code event_id} here, once per call: its own, or a new random
     * UUID shared by all of its fan-out entries. Each request body is serialised here, once,
     * so a request OkHttp sends again by itself carries the same ids.
     */
    private List<Request> buildTrackRequests(List<TrackEvent> events) throws IOException {
        List<Request> requests = new ArrayList<>();
        List<Map<String, Object>> batch = new ArrayList<>();

        boolean single = events.size() == 1;
        for (TrackEvent event : events) {
            if (event == null) continue;
            Map<String, Object> body = wireBody(event);
            if (event.hasKey()) {
                if (single) {
                    requests.add(requestBuilder(config.getBaseUrl() + "/api/v1/tracking/track")
                            .post(RequestBody.create(objectMapper.writeValueAsString(body), JSON))
                            .build());
                } else {
                    batch.add(body);
                }
            } else {
                batch.addAll(fanOut(event.getUserId(), body));
            }
        }

        for (int start = 0; start < batch.size(); start += BATCH_LIMIT) {
            List<Map<String, Object>> chunk = batch.subList(start, Math.min(start + BATCH_LIMIT, batch.size()));
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("events", chunk);
            requests.add(requestBuilder(config.getBaseUrl() + "/api/v1/tracking/batch")
                    .post(RequestBody.create(objectMapper.writeValueAsString(payload), JSON))
                    .build());
        }
        return requests;
    }

    /**
     * The event's wire form with its {@code event_id}: the one set on the event, or else a new
     * lowercase random (version 4) UUID.
     */
    private static Map<String, Object> wireBody(TrackEvent event) {
        Map<String, Object> body = event.toBody();
        if (!body.containsKey("event_id")) {
            body.put("event_id", UUID.randomUUID().toString());
        }
        return body;
    }

    /**
     * Expands an event without a key into one entry per cached assignment
     * ({@code experiment_key}) plus one per cached evaluated flag ({@code feature_flag_key})
     * for the event's user. Every entry is a copy of {@code base}, so all of them carry the
     * event's one {@code event_id}. Empty when nothing is cached.
     */
    private List<Map<String, Object>> fanOut(String userId, Map<String, Object> base) {
        List<Map<String, Object>> entries = new ArrayList<>();
        for (ExperimentAssignment assignment : getCachedAssignments(userId)) {
            Map<String, Object> entry = new LinkedHashMap<>(base);
            entry.put("experiment_key", assignment.getExperimentKey());
            entries.add(entry);
        }
        for (String flagKey : getCachedFlagKeys(userId)) {
            Map<String, Object> entry = new LinkedHashMap<>(base);
            entry.put("feature_flag_key", flagKey);
            entries.add(entry);
        }
        return entries;
    }
}

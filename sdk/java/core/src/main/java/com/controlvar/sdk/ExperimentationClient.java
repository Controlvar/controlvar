package com.controlvar.sdk;

import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.model.ExperimentAssignment;
import com.controlvar.sdk.model.FlagEvaluation;
import com.controlvar.sdk.model.TrackEvent;
import com.controlvar.sdk.model.User;
import com.fasterxml.jackson.databind.ObjectMapper;
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
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
 *   <li>{@link #evaluateFeatureFlag} — {@code GET /api/v1/feature-flags/evaluate/{key}?user_id=…}</li>
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
        this.objectMapper = new ObjectMapper();
        this.flagCache = new AssignmentCache<>(config.getCacheSize(), config.getCacheTtlMs());
        this.assignmentCache = new AssignmentCache<>(config.getCacheSize(), config.getCacheTtlMs());
    }

    // =========================================================================
    // Feature flags
    // =========================================================================

    /**
     * Evaluates a feature flag for a user via
     * {@code GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=…}.
     *
     * <p>Successful evaluations are cached per user + flag for the configured TTL;
     * failures are never cached.
     *
     * @param user    the user to evaluate the flag for
     * @param flagKey the feature flag key (e.g., "new-checkout-flow")
     * @return the server's evaluation (key, enabled, config)
     * @throws ExperimentationException on a non-2xx response (404 when the flag is not
     *                                  ACTIVE or unknown, 401 for a bad key) or a network error
     */
    public FlagEvaluation evaluateFeatureFlag(User user, String flagKey) {
        if (user == null) throw new IllegalArgumentException("user must not be null");
        if (flagKey == null || flagKey.isEmpty()) throw new IllegalArgumentException("flagKey must not be empty");

        String cacheKey = cacheKey(user.getUserId(), flagKey);
        FlagEvaluation cached = flagCache.get(cacheKey);
        if (cached != null) {
            return cached;
        }

        HttpUrl url = HttpUrl.get(config.getBaseUrl()).newBuilder()
                .addEncodedPathSegments("api/v1/feature-flags/evaluate")
                .addPathSegment(flagKey)
                .addQueryParameter("user_id", user.getUserId())
                .build();
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
            FlagEvaluation evaluation = objectMapper.readValue(body.string(), FlagEvaluation.class);
            if (evaluation.getKey() == null || evaluation.getKey().isEmpty()) {
                evaluation.setKey(flagKey);
            }
            flagCache.put(cacheKey, evaluation);
            return evaluation;
        } catch (IOException e) {
            throw new ExperimentationException("Network error evaluating flag '" + flagKey + "': " + e.getMessage(), e);
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
    // Experiments
    // =========================================================================

    /**
     * Assigns a user to an experiment variant via {@code POST /api/v1/tracking/assign}
     * (sticky server-side; the server records the exposure).
     *
     * <p>The user's attributes are sent as the assignment {@code context} for targeting
     * rules. Successful assignments are cached per user + experiment for the configured
     * TTL; failures are never cached.
     *
     * @param user          the user to assign
     * @param experimentKey the experiment key (e.g., "checkout-button-color")
     * @return the assignment (never null)
     * @throws ExperimentationException on a non-2xx response (404 when the experiment is
     *                                  not ACTIVE or unknown) or a network error
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
                ExperimentAssignment assignment = objectMapper.readValue(body.string(), ExperimentAssignment.class);
                if (assignment.getExperimentKey() == null || assignment.getExperimentKey().isEmpty()) {
                    assignment.setExperimentKey(experimentKey);
                }
                if (assignment.getUserId() == null || assignment.getUserId().isEmpty()) {
                    assignment.setUserId(user.getUserId());
                }
                assignmentCache.put(cacheKey, assignment);
                return assignment;
            }
        } catch (IOException e) {
            throw new ExperimentationException(
                    "Network error assigning experiment '" + experimentKey + "': " + e.getMessage(), e);
        }
    }

    // =========================================================================
    // Tracking (fire-and-forget)
    // =========================================================================

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
     */
    private List<Request> buildTrackRequests(List<TrackEvent> events) throws IOException {
        List<Request> requests = new ArrayList<>();
        List<Map<String, Object>> batch = new ArrayList<>();

        boolean single = events.size() == 1;
        for (TrackEvent event : events) {
            if (event == null) continue;
            if (event.hasKey()) {
                if (single) {
                    requests.add(requestBuilder(config.getBaseUrl() + "/api/v1/tracking/track")
                            .post(RequestBody.create(objectMapper.writeValueAsString(event.toBody()), JSON))
                            .build());
                } else {
                    batch.add(event.toBody());
                }
            } else {
                batch.addAll(fanOut(event));
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
     * Expands an event without a key into one entry per cached assignment
     * ({@code experiment_key}) plus one per cached evaluated flag ({@code feature_flag_key})
     * for the event's user. Empty when nothing is cached.
     */
    private List<Map<String, Object>> fanOut(TrackEvent event) {
        List<Map<String, Object>> entries = new ArrayList<>();
        Map<String, Object> base = event.toBody();
        for (ExperimentAssignment assignment : getCachedAssignments(event.getUserId())) {
            Map<String, Object> entry = new LinkedHashMap<>(base);
            entry.put("experiment_key", assignment.getExperimentKey());
            entries.add(entry);
        }
        for (String flagKey : getCachedFlagKeys(event.getUserId())) {
            Map<String, Object> entry = new LinkedHashMap<>(base);
            entry.put("feature_flag_key", flagKey);
            entries.add(entry);
        }
        return entries;
    }
}

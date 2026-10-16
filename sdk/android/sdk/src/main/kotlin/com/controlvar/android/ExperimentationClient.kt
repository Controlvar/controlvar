package com.controlvar.android

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.HttpUrl
import org.json.JSONArray
import org.json.JSONObject

/**
 * Main Controlvar SDK client for Android.
 *
 * Feature flag evaluation and experiment assignment are decided **by the server**:
 * every call goes to the public API with your `X-API-Key`, the server buckets the
 * user (sticky per user + experiment), and the client caches the answer per user + key
 * for [SdkConfig.cacheTtlMs]. Nothing is bucketed locally.
 *
 * - [evaluateFlag] — `GET /api/v1/feature-flags/evaluate/{key}?user_id=...`
 * - [getAssignment] — `POST /api/v1/tracking/assign`
 * - [track] — `POST /api/v1/tracking/track` (keyed event), or
 *   `POST /api/v1/tracking/batch` fanned out to the cached assignments/flags (no key)
 * - [trackBatch] — `POST /api/v1/tracking/batch`, at most 100 events per request
 *
 * Successful results are also written to the [OfflineStore] (SharedPreferences in
 * production) and read back when the API cannot be reached. Failures are never cached.
 * There is no local bucketing and no "fetch all flag definitions" path: the former
 * `refreshFlags()` and `SdkConfig.enableLocalEval` are gone / no-ops.
 *
 * All network operations use Kotlin Coroutines and run on Dispatchers.IO.
 * The client is thread-safe and suitable for use across multiple coroutines.
 *
 * Usage:
 * ```kotlin
 * val client = ExperimentationClient(
 *     SdkConfig(baseUrl = "https://api.example.com", apiKey = "your-key")
 * )
 * val result = client.evaluateFlag("new-dashboard", User("user-123"))
 * if (result.enabled) { showNewDashboard() }
 * ```
 */
class ExperimentationClient(
    private val config: SdkConfig,
    private val httpClient: HttpClient = HttpClient(config.baseUrl, config.apiKey, config.timeoutMs),
    private val flagCache: ResultCache<EvalResult> = ResultCache(config.cacheSize, config.cacheTtlMs),
    private val assignmentCache: ResultCache<Assignment> = ResultCache(config.cacheSize, config.cacheTtlMs),
    private val offlineStore: OfflineStore = OfflineStore()
) {
    // Background scope for fire-and-forget operations (SupervisorJob: child failures don't cancel parent)
    private val backgroundScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /**
     * Evaluates a feature flag for the given user via
     * `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=...`.
     *
     * Resolution order:
     *   1. In-memory cache for this user + flag (fast path, ~O(1))
     *   2. API call; the result is cached and persisted to the offline store
     *   3. On failure: the offline store's last successful evaluation, if any
     *
     * @param flagKey the feature flag's unique key
     * @param user    the user to evaluate the flag for
     * @return EvalResult with the flag key, enabled state and config
     * @throws ExperimentationException.FlagNotFoundException if the flag is unknown or not
     *         ACTIVE (HTTP 404) and no offline value exists
     * @throws ExperimentationException.ServerException on another non-2xx response with no offline value
     * @throws ExperimentationException.NetworkException on network failure with no offline value
     */
    suspend fun evaluateFlag(flagKey: String, user: User): EvalResult {
        require(flagKey.isNotEmpty()) { "flagKey must not be empty" }
        require(user.id.isNotEmpty()) { "user.id must not be empty" }

        val cacheKey = ResultCache.key(user.id, flagKey)
        flagCache.get(cacheKey)?.let { return it }

        val response = try {
            httpClient.get(evaluateFlagPath(flagKey, user.id))
        } catch (e: ExperimentationException) {
            // Offline fallback: the last successful evaluation, if any. Not re-cached in
            // memory so the next call retries the server.
            offlineStore.loadFlag(user.id, flagKey)?.let { return it }
            throw when {
                e is ExperimentationException.ServerException && e.statusCode == 404 ->
                    ExperimentationException.FlagNotFoundException(flagKey)
                e is ExperimentationException.NetworkException ->
                    ExperimentationException.NetworkException(
                        "Failed to evaluate flag '$flagKey' and no offline data available", e
                    )
                else -> e
            }
        }

        val parsed = EvalResult.fromJson(response)
        val result = if (parsed.key.isEmpty()) parsed.copy(key = flagKey) else parsed
        flagCache.set(cacheKey, result)
        offlineStore.saveFlag(user.id, result)
        return result
    }

    /**
     * Assigns the user to an experiment variant via `POST /api/v1/tracking/assign`
     * (sticky server-side; the server records the exposure).
     *
     * The user's attributes are sent as the assignment `context` for targeting rules.
     * Successful assignments are cached per user + experiment and persisted to the
     * offline store; on failure the last successful assignment is returned if one exists.
     *
     * @param experimentKey unique key for the experiment
     * @param user          the user requesting assignment
     * @return the assignment (experiment key, variant id/name, control flag, configuration)
     * @throws ExperimentationException.ServerException on API error (404 when the experiment
     *         is not ACTIVE) with no offline value
     * @throws ExperimentationException.NetworkException on network failure with no offline value
     */
    suspend fun getAssignment(experimentKey: String, user: User): Assignment {
        require(experimentKey.isNotEmpty()) { "experimentKey must not be empty" }
        require(user.id.isNotEmpty()) { "user.id must not be empty" }

        val cacheKey = ResultCache.key(user.id, experimentKey)
        assignmentCache.get(cacheKey)?.let { return it }

        val body = JSONObject().apply {
            put("experiment_key", experimentKey)
            put("user_id", user.id)
            if (user.attributes.isNotEmpty()) {
                put("context", kotlinToJson(user.attributes))
            }
        }

        val response = try {
            httpClient.post("/api/v1/tracking/assign", body)
        } catch (e: ExperimentationException) {
            offlineStore.loadAssignment(user.id, experimentKey)?.let { return it }
            throw e
        }

        val parsed = Assignment.fromJson(response)
        val assignment = parsed.copy(
            experimentKey = parsed.experimentKey.ifEmpty { experimentKey },
            userId = parsed.userId.ifEmpty { user.id }
        )
        assignmentCache.set(cacheKey, assignment)
        offlineStore.saveAssignment(user.id, assignment)
        return assignment
    }

    /**
     * Tracks a user event asynchronously. Fire-and-forget — never throws; the request
     * runs on a background coroutine and failures are swallowed.
     *
     * - With [TrackEvent.experimentKey] or [TrackEvent.featureFlagKey] set: one
     *   `POST /api/v1/tracking/track`.
     * - Without a key: one `POST /api/v1/tracking/batch` containing one entry per
     *   experiment the user was assigned to plus one per flag evaluated for the user
     *   by this client (from the in-memory cache). If nothing is cached, nothing is sent.
     *
     * Events with an empty `userId` or `eventName` are ignored.
     *
     * @param event the event to track
     */
    suspend fun track(event: TrackEvent) {
        if (!isValid(event)) return
        if (event.hasKey()) {
            send(listOf("/api/v1/tracking/track" to event.toJson()))
        } else {
            send(batchRequests(fanOut(event)))
        }
    }

    /**
     * Tracks several events asynchronously via `POST /api/v1/tracking/batch`, at most
     * [BATCH_LIMIT] per request. Events without a key are fanned out like [track];
     * events with nothing cached for their user are dropped. Fire-and-forget — never
     * throws.
     *
     * @param events the events to track
     */
    suspend fun trackBatch(events: List<TrackEvent>) {
        val entries = mutableListOf<JSONObject>()
        for (event in events) {
            if (!isValid(event)) continue
            if (event.hasKey()) entries.add(event.toJson()) else entries.addAll(fanOut(event))
        }
        send(batchRequests(entries))
    }

    /**
     * Returns the cached (successful, unexpired) assignments for the user.
     */
    fun getCachedAssignments(userId: String): List<Assignment> =
        assignmentCache.valuesWithPrefix(ResultCache.userPrefix(userId))

    /**
     * Returns the keys of flags successfully evaluated (and still cached) for the user.
     */
    fun getCachedFlagKeys(userId: String): List<String> =
        flagCache.valuesWithPrefix(ResultCache.userPrefix(userId)).map { it.key }

    /**
     * Drops the cached evaluation and assignment for one user + key (memory and offline store).
     */
    fun invalidateCache(userId: String, key: String) {
        val cacheKey = ResultCache.key(userId, key)
        flagCache.remove(cacheKey)
        assignmentCache.remove(cacheKey)
        offlineStore.removeFlag(userId, key)
        offlineStore.removeAssignment(userId, key)
    }

    /**
     * Drops every cached evaluation and assignment (memory only; the offline store is kept).
     */
    fun clearCache() {
        flagCache.clear()
        assignmentCache.clear()
    }

    /**
     * Returns the number of cached evaluations plus assignments.
     */
    fun getCacheSize(): Int = flagCache.size() + assignmentCache.size()

    /**
     * Cleans up resources. Call when the client is no longer needed
     * (e.g., in Activity.onDestroy()).
     */
    fun close() {
        clearCache()
    }

    // ---- Private helpers ----

    /** Builds `/api/v1/feature-flags/evaluate/{key}?user_id=...` with both values percent-encoded. */
    private fun evaluateFlagPath(flagKey: String, userId: String): String {
        val url = HttpUrl.Builder()
            .scheme("http")
            .host("localhost")
            .addEncodedPathSegments("api/v1/feature-flags/evaluate")
            .addPathSegment(flagKey)
            .addQueryParameter("user_id", userId)
            .build()
        return "${url.encodedPath}?${url.encodedQuery}"
    }

    /** Posts each (path, body) in order on the background scope, swallowing every failure. */
    private fun send(requests: List<Pair<String, JSONObject>>) {
        if (requests.isEmpty()) return
        backgroundScope.launch {
            for ((path, body) in requests) {
                try {
                    httpClient.post(path, body)
                } catch (e: Exception) {
                    // Swallow errors — tracking must never crash the caller
                }
            }
        }
    }

    private fun isValid(event: TrackEvent): Boolean =
        event.userId.isNotEmpty() && event.eventName.isNotEmpty()

    /** Wraps batch entries into `/tracking/batch` requests of at most [BATCH_LIMIT] events each. */
    private fun batchRequests(entries: List<JSONObject>): List<Pair<String, JSONObject>> =
        entries.chunked(BATCH_LIMIT).map { chunk ->
            "/api/v1/tracking/batch" to JSONObject().put("events", JSONArray(chunk))
        }

    /**
     * Expands an event without a key into one entry per cached assignment
     * (`experiment_key`) plus one per cached evaluated flag (`feature_flag_key`) for
     * the event's user. Empty when nothing is cached.
     */
    private fun fanOut(event: TrackEvent): List<JSONObject> {
        val entries = mutableListOf<JSONObject>()
        for (assignment in getCachedAssignments(event.userId)) {
            entries.add(event.copy(experimentKey = assignment.experimentKey).toJson())
        }
        for (flagKey in getCachedFlagKeys(event.userId)) {
            entries.add(event.copy(featureFlagKey = flagKey).toJson())
        }
        return entries
    }

    companion object {
        /** Maximum events per `POST /api/v1/tracking/batch` request. */
        const val BATCH_LIMIT = 100
    }
}

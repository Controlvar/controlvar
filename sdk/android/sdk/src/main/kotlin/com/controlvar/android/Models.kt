package com.controlvar.android

import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * SDK Configuration.
 *
 * `baseUrl` is the origin of the Controlvar API (e.g. `https://api.example.com`
 * or `http://10.0.2.2:8000` from the emulator); the SDK appends `/api/v1/...`.
 * `apiKey` is sent as the `X-API-Key` header on every request.
 */
data class SdkConfig(
    val baseUrl: String = "http://localhost:8000",
    val apiKey: String,
    val timeoutMs: Long = 10_000L,
    /** Maximum number of cached evaluations (and, separately, assignments). */
    val cacheSize: Int = 1000,
    /** How long a successful evaluation or assignment is reused. Failures are never cached. */
    val cacheTtlMs: Long = 300_000L, // 5 minutes
    /**
     * Kept for source compatibility; has no effect. Flags and experiments are
     * evaluated by the server, the SDK no longer buckets users locally.
     */
    @Deprecated("Flags are evaluated by the server; this setting has no effect.")
    val enableLocalEval: Boolean = true
)

/**
 * Platform user for flag evaluation and experiment assignment.
 *
 * `attributes` are sent as the assignment `context` (used by targeting rules).
 */
data class User(
    val id: String,
    val attributes: Map<String, Any?> = emptyMap()
)

/**
 * The server's evaluation of one feature flag for one user
 * (`GET /api/v1/feature-flags/evaluate/{key}?user_id=...`).
 *
 * @property key     the evaluated flag key
 * @property enabled the server's decision for this user
 * @property config  `null` today: the evaluate route returns `config: null` for every
 *                   flag. A JSON value the server might send decodes to a
 *                   `Map<String, Any?>` (object), a `List<Any?>` (array) or a
 *                   `String`/`Number`/`Boolean` (scalar)
 */
data class EvalResult(
    val key: String,
    val enabled: Boolean,
    val config: Any? = null
) {
    /** The config as a map when the server returned a JSON object, `null` otherwise. */
    @Suppress("UNCHECKED_CAST")
    val configMap: Map<String, Any?>?
        get() = config as? Map<String, Any?>

    /**
     * Convenience variant name: `null` when the flag is off, otherwise `"on"`. The API
     * returns `config: null`, so no variant name comes back; the code would return a string
     * `config["variant"]` instead if the server ever sent one.
     */
    val variant: String?
        get() = if (!enabled) null else (configMap?.get("variant") as? String) ?: "on"

    /** Wire form (same shape as the server response); used by [OfflineStore]. */
    fun toJson(): JSONObject = JSONObject().apply {
        put("key", key)
        put("enabled", enabled)
        put("config", kotlinToJson(config))
    }

    companion object {
        fun fromJson(json: JSONObject): EvalResult = EvalResult(
            key = json.stringOrNull("key") ?: "",
            enabled = json.optBoolean("enabled", false),
            config = if (json.isNull("config")) null else jsonToKotlin(json.opt("config"))
        )
    }
}

/**
 * The variant the server assigned a user to (`POST /api/v1/tracking/assign`).
 * Assignments are sticky server-side.
 *
 * @property experimentKey the experiment key
 * @property userId        the assigned user
 * @property variantId     the variant UUID (`null` if the server did not return one)
 * @property variantName   the variant name, e.g. `"control"` or `"treatment"`
 * @property isControl     `true` for the control variant
 * @property configuration the variant's configuration JSON from the experiment definition, or `null`
 */
data class Assignment(
    val experimentKey: String,
    val userId: String,
    val variantId: String?,
    val variantName: String,
    val isControl: Boolean,
    val configuration: Map<String, Any?>? = null
) {
    /** Alias for [variantName]. */
    @Deprecated("The server identifies variants by name; use variantName.", ReplaceWith("variantName"))
    val variantKey: String
        get() = variantName

    /** Wire form (same shape as the server response); used by [OfflineStore]. */
    fun toJson(): JSONObject = JSONObject().apply {
        put("experiment_key", experimentKey)
        put("user_id", userId)
        put("variant_id", variantId ?: JSONObject.NULL)
        put("variant_name", variantName)
        put("is_control", isControl)
        put("configuration", kotlinToJson(configuration))
    }

    companion object {
        @Suppress("UNCHECKED_CAST")
        fun fromJson(json: JSONObject): Assignment = Assignment(
            experimentKey = json.stringOrNull("experiment_key") ?: "",
            userId = json.stringOrNull("user_id") ?: "",
            variantId = json.stringOrNull("variant_id"),
            variantName = json.stringOrNull("variant_name") ?: "",
            isControl = json.optBoolean("is_control", false),
            configuration = json.optJSONObject("configuration")?.let { jsonToKotlin(it) as Map<String, Any?> }
        )
    }
}

/**
 * An analytics event recorded with [ExperimentationClient.track] / [ExperimentationClient.trackBatch].
 *
 * When neither [experimentKey] nor [featureFlagKey] is set, the client fans the event
 * out to every experiment the user was assigned to and every flag evaluated for the
 * user (from its cache).
 *
 * @property userId         the user who performed the action (required)
 * @property eventName      the event name, e.g. `"purchase"`; experiment metrics match on it
 * @property properties     sent as the event's `metadata`
 * @property experimentKey  attributes the event to one experiment
 * @property featureFlagKey attributes the event to one feature flag
 * @property value          optional numeric value (revenue, duration, ...)
 * @property eventType      sent as `event_type`; defaults to [eventName]
 * @property timestampMs    optional epoch millis, sent as ISO-8601; the server stamps the event otherwise
 */
data class TrackEvent(
    val userId: String,
    val eventName: String,
    val properties: Map<String, Any?> = emptyMap(),
    val experimentKey: String? = null,
    val featureFlagKey: String? = null,
    val value: Double? = null,
    val eventType: String? = null,
    val timestampMs: Long? = null
) {
    /** True when the event is attributed to an experiment or a feature flag. */
    fun hasKey(): Boolean = !experimentKey.isNullOrEmpty() || !featureFlagKey.isNullOrEmpty()

    /**
     * Wire form: the body of `POST /api/v1/tracking/track` and of each
     * `POST /api/v1/tracking/batch` entry.
     */
    fun toJson(): JSONObject = JSONObject().apply {
        put("event_type", eventType?.takeIf { it.isNotEmpty() } ?: eventName)
        put("event_name", eventName)
        put("user_id", userId)
        if (!experimentKey.isNullOrEmpty()) put("experiment_key", experimentKey)
        if (!featureFlagKey.isNullOrEmpty()) put("feature_flag_key", featureFlagKey)
        if (value != null) put("value", value)
        if (properties.isNotEmpty()) put("metadata", kotlinToJson(properties))
        if (timestampMs != null) put("timestamp", isoTimestamp(timestampMs))
    }

    companion object {
        /** Formats epoch millis as ISO-8601 UTC, e.g. `2026-09-11T10:30:00.000Z`. */
        fun isoTimestamp(epochMs: Long): String =
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
                .apply { timeZone = TimeZone.getTimeZone("UTC") }
                .format(Date(epochMs))
    }
}

/**
 * SDK exceptions
 */
sealed class ExperimentationException(message: String, cause: Throwable? = null) :
    Exception(message, cause) {

    /** OkHttp IO failure or timeout. */
    class NetworkException(message: String, cause: Throwable? = null) :
        ExperimentationException(message, cause)

    /** The flag key is unknown (HTTP 404) and no offline value exists. */
    class FlagNotFoundException(flagKey: String) :
        ExperimentationException("Flag not found: $flagKey")

    /** Non-2xx HTTP response: 401 bad key, 404 unknown key (or a draft with no control variant), 422 invalid event, 429 rate limited. */
    class ServerException(val statusCode: Int, message: String) :
        ExperimentationException("Server error $statusCode: $message")

    class InvalidConfigException(message: String) :
        ExperimentationException(message)
}

// ---- JSON helpers (org.json <-> Kotlin) ----

/** `optString` returns the literal "null" for JSON null; this returns `null` instead. */
internal fun JSONObject.stringOrNull(name: String): String? =
    if (!has(name) || isNull(name)) null else optString(name)

/** Converts an org.json value tree into Kotlin maps / lists / scalars. */
internal fun jsonToKotlin(value: Any?): Any? = when (value) {
    null, JSONObject.NULL -> null
    is JSONObject -> LinkedHashMap<String, Any?>().also { map ->
        val keys = value.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            map[k] = jsonToKotlin(value.opt(k))
        }
    }
    is JSONArray -> (0 until value.length()).map { jsonToKotlin(value.opt(it)) }
    else -> value
}

/** Converts Kotlin maps / collections / scalars into org.json values. */
internal fun kotlinToJson(value: Any?): Any = when (value) {
    null -> JSONObject.NULL
    is JSONObject, is JSONArray -> value
    is Map<*, *> -> JSONObject().also { obj ->
        value.forEach { (k, v) -> obj.put(k.toString(), kotlinToJson(v)) }
    }
    is Iterable<*> -> JSONArray().also { arr -> value.forEach { arr.put(kotlinToJson(it)) } }
    is Array<*> -> JSONArray().also { arr -> value.forEach { arr.put(kotlinToJson(it)) } }
    is Boolean, is Int, is Long, is Double, is String -> value
    is Float -> value.toDouble()
    is Number -> value.toDouble()
    else -> value.toString()
}

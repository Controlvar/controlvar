package com.controlvar.android

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * OkHttp-based HTTP client with Kotlin Coroutine support.
 *
 * Every request carries `X-API-Key: <apiKey>`, `Accept: application/json` and
 * `Content-Type: application/json`. Non-2xx responses are mapped to
 * [ExperimentationException.ServerException], IO errors to
 * [ExperimentationException.NetworkException].
 */
class HttpClient(
    private val baseUrl: String,
    private val apiKey: String,
    timeoutMs: Long = 10_000L,
    private val okHttpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .writeTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .build()
) {
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    /**
     * Performs an authenticated GET request and returns the parsed JSON body.
     *
     * @param path API path relative to baseUrl, already percent-encoded
     *             (e.g. "/api/v1/feature-flags/evaluate/my-flag?user_id=u1")
     * @throws ExperimentationException.ServerException on non-2xx HTTP status
     * @throws ExperimentationException.NetworkException on IO errors
     */
    suspend fun get(path: String): JSONObject = withContext(Dispatchers.IO) {
        val request = requestBuilder(path)
            .get()
            .build()

        executeRequest(request)
    }

    /**
     * Performs an authenticated POST request with a JSON body.
     *
     * @param path API path relative to baseUrl
     * @param body JSON body to send
     * @return parsed JSON response body
     * @throws ExperimentationException.ServerException on non-2xx HTTP status
     * @throws ExperimentationException.NetworkException on IO errors
     */
    suspend fun post(path: String, body: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val request = requestBuilder(path)
            .post(body.toString().toRequestBody(jsonMediaType))
            .build()

        executeRequest(request)
    }

    /**
     * Fire-and-forget POST — enqueues asynchronously without suspending.
     * Errors are silently swallowed (suitable for analytics/tracking).
     *
     * @param path API path relative to baseUrl
     * @param body JSON body to send
     */
    fun postAsync(path: String, body: JSONObject) {
        val request = requestBuilder(path)
            .post(body.toString().toRequestBody(jsonMediaType))
            .build()

        okHttpClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { /* intentionally ignored */ }
            override fun onResponse(call: Call, response: Response) { response.close() }
        })
    }

    private fun requestBuilder(path: String): Request.Builder =
        Request.Builder()
            .url(normalizeUrl(path))
            .header("X-API-Key", apiKey)
            .header("Accept", "application/json")
            .header("Content-Type", "application/json")

    private fun executeRequest(request: Request): JSONObject {
        return try {
            okHttpClient.newCall(request).execute().use { response ->
                val bodyString = response.body?.string() ?: ""
                if (!response.isSuccessful) {
                    throw ExperimentationException.ServerException(
                        response.code,
                        errorDetail(response.message, bodyString)
                    )
                }
                if (bodyString.isBlank()) JSONObject() else JSONObject(bodyString)
            }
        } catch (e: ExperimentationException) {
            throw e
        } catch (e: IOException) {
            // A flag evaluation's query holds the user's attributes: the message names the
            // request without it, and the cause is kept only if no message in its chain repeats it.
            val url = request.url
            throw ExperimentationException.NetworkException(
                "Network error: ${withoutQuery(e.message, url)}",
                if (repeatsQuery(e, url)) null else e
            )
        }
    }

    /**
     * What a message must not repeat of [url]: its query and its `context` value, each encoded
     * and decoded, longest first. Empty strings are left out (they would match everything).
     */
    private fun queryForms(url: HttpUrl): List<String> {
        val encoded = url.encodedQuery ?: return emptyList()
        return listOfNotNull(
            encoded,
            url.query,
            encoded.substringAfter("context=", ""),
            url.queryParameter("context")
        ).filter { it.isNotEmpty() }.distinct().sortedByDescending { it.length }
    }

    /** [message] with [url]'s query removed, wherever the message names it, in any form. */
    private fun withoutQuery(message: String?, url: HttpUrl): String? {
        val forms = queryForms(url)
        if (message == null || forms.isEmpty()) return message
        var cut = message.replace(url.toString(), url.newBuilder().query(null).build().toString())
        for (form in forms) cut = cut.replace("?$form", "").replace(form, "")
        return cut
    }

    /** True when a message in [error]'s cause chain contains [url]'s query or `context` value. */
    private fun repeatsQuery(error: Throwable, url: HttpUrl): Boolean {
        val queries = queryForms(url)
        var cause: Throwable? = error
        var depth = 0
        while (cause != null && depth++ < 20) {
            val text = cause.message
            if (text != null && queries.any { text.contains(it) }) return true
            cause = cause.cause
        }
        return false
    }

    /** Prefers the API's `detail` field, then the HTTP reason phrase, for error messages. */
    private fun errorDetail(reason: String, body: String): String {
        val detail = try {
            JSONObject(body).stringOrNull("detail")
        } catch (e: Exception) {
            null
        }
        return detail ?: reason.ifEmpty { body.take(200) }
    }

    private fun normalizeUrl(path: String): String {
        val base = baseUrl.trimEnd('/')
        val p = if (path.startsWith("/")) path else "/$path"
        return "$base$p"
    }
}

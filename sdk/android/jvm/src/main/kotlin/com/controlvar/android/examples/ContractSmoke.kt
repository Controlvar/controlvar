package com.controlvar.android.examples

import com.controlvar.android.ExperimentationClient
import com.controlvar.android.HttpClient
import com.controlvar.android.SdkConfig
import com.controlvar.android.TrackEvent
import com.controlvar.android.User
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.util.Collections
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.system.exitProcess

/**
 * Contract smoke for the Android SDK, run on a plain JVM (no emulator, no
 * Android SDK — see ../../pom.xml for why that works).
 *
 * Exercises the SDK's public API against a live backend and prints exactly one
 * JSON line on stdout on success (exit 0); on failure it prints one line to
 * stderr and exits 1.
 *
 * Environment: CONTROLVAR_API_URL (default http://localhost:8000),
 * CONTROLVAR_API_KEY (required), CONTRACT_EXPERIMENT_KEY (default
 * sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
 * CONTRACT_TARGETED_FLAG_KEY (default sdk_contract_targeted), CONTRACT_USER_ID
 * (default smoke-<uuid>).
 *
 * Run from the repo root with `bash sdk/android/examples/contract_smoke.sh`.
 */
object ContractSmoke {

    private fun env(key: String, fallback: String): String =
        System.getenv(key)?.takeIf { it.isNotEmpty() } ?: fallback

    private fun fail(message: String): Nothing {
        System.err.println("android contract smoke failed: $message")
        exitProcess(1)
    }

    @JvmStatic
    fun main(args: Array<String>) {
        val apiKey = env("CONTROLVAR_API_KEY", env("EXPERIMENTLY_API_KEY", ""))
        if (apiKey.isEmpty()) fail("CONTROLVAR_API_KEY is required")
        val baseUrl = env("CONTROLVAR_API_URL", env("EXPERIMENTLY_API_URL", "http://localhost:8000"))
        val experimentKey = env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
        val flagKey = env("CONTRACT_FLAG_KEY", "sdk_contract_flag")
        val targetedKey = env("CONTRACT_TARGETED_FLAG_KEY", "sdk_contract_targeted")
        val userId = env("CONTRACT_USER_ID", "smoke-${UUID.randomUUID()}")

        val config = SdkConfig(baseUrl = baseUrl, apiKey = apiKey, timeoutMs = 10_000L)
        val user = User(userId, mapOf("source" to "contract_smoke"))

        try {
            runBlocking {
                val client = ExperimentationClient(config)

                // 1. Assign twice through this client (the second call is a cache hit)
                //    and once through a fresh one, so "sticky" reflects the server.
                val first = client.getAssignment(experimentKey, user)
                val second = client.getAssignment(experimentKey, user)
                val fresh = ExperimentationClient(config)
                val third = fresh.getAssignment(experimentKey, user)
                fresh.close()

                if (first.variantName !in setOf("control", "treatment")) {
                    fail("unexpected variant_name '${first.variantName}'")
                }
                val sticky = first.variantName == second.variantName &&
                    first.variantId == second.variantId &&
                    first.variantName == third.variantName &&
                    first.variantId == third.variantId
                if (!sticky) {
                    fail(
                        "assignment not sticky: ${first.variantName} / " +
                            "${second.variantName} / ${third.variantName}"
                    )
                }

                // 2. Evaluate the flag (100% on in the seeded data).
                val flag = client.evaluateFlag(flagKey, user)

                // 3/4. Tracking. ExperimentationClient.track() is fire-and-forget: it
                //      launches on a background scope and swallows every error, so
                //      calling it proves nothing about the contract. Call it anyway
                //      (it must not throw), then send the *same* bodies the SDK builds
                //      — TrackEvent.toJson(), the SDK's own serializer, to the SDK's own
                //      paths — through HttpClient, which throws on a non-2xx response.
                //      A renamed field or a path the backend does not serve fails here.
                val http = HttpClient(baseUrl, apiKey, 10_000L)

                val conversion = TrackEvent(
                    userId = userId,
                    eventName = "purchase",
                    properties = mapOf("sdk" to "android"),
                    experimentKey = experimentKey,
                    value = 12.5
                )
                client.track(conversion)
                http.post("/api/v1/tracking/track", conversion.toJson())

                // Fan-out: one entry per cached assignment plus one per cached flag,
                // exactly as ExperimentationClient.track() expands a keyless event.
                val keyless = TrackEvent(userId = userId, eventName = "page_view")
                client.track(keyless)
                val fanout = JSONArray()
                for (assignment in client.getCachedAssignments(userId)) {
                    fanout.put(keyless.copy(experimentKey = assignment.experimentKey).toJson())
                }
                for (cachedFlagKey in client.getCachedFlagKeys(userId)) {
                    fanout.put(keyless.copy(featureFlagKey = cachedFlagKey).toJson())
                }
                if (fanout.length() != 2) {
                    fail(
                        "cache holds ${client.getCachedAssignments(userId).size} assignment(s) and " +
                            "${client.getCachedFlagKeys(userId).size} flag(s); fan-out would send nothing"
                    )
                }
                http.post("/api/v1/tracking/batch", JSONObject().put("events", fanout))

                // 4b. One keyed event sent twice with one fixed event_id (#1543). The server is
                //     to store it once: the runner counts the rows for this user and event name.
                //     Sent through HttpClient, like the tracking above, so a refused body fails.
                val dedupName = "contract_dedup_${UUID.randomUUID()}"
                val dedupId = "contract-${UUID.randomUUID()}"
                val dedup = TrackEvent(
                    userId = userId,
                    eventName = dedupName,
                    experimentKey = experimentKey,
                    eventId = dedupId
                )
                repeat(2) { http.post("/api/v1/tracking/track", dedup.toJson()) }

                // 4c. The attributes reach flag evaluation (#2017). The seeded targeted flag is
                //     at 0% with one rule, plan equals pro, at 100%. A NEW client (so the fan-out
                //     above is untouched) asks for it with plan=pro, then, after clearCache(), with
                //     plan=free. EvalResult carries no reason, so the server's reason is read from
                //     the answer to the client's own request: the client's HttpClient is given an
                //     OkHttpClient with the default one's three timeouts and an interceptor that
                //     only reads the targeted flag's answers. The runner requires on with
                //     targeting_rule, then off.
                val reasons: MutableList<String?> = Collections.synchronizedList(mutableListOf())
                val recording = OkHttpClient.Builder()
                    .connectTimeout(config.timeoutMs, TimeUnit.MILLISECONDS)
                    .readTimeout(config.timeoutMs, TimeUnit.MILLISECONDS)
                    .writeTimeout(config.timeoutMs, TimeUnit.MILLISECONDS)
                    .addInterceptor { chain ->
                        val response = chain.proceed(chain.request())
                        val path = chain.request().url.pathSegments
                        if (path.size >= 2 && path[path.size - 2] == "evaluate" && path.last() == targetedKey) {
                            val reason = try {
                                JSONObject(response.peekBody(1L shl 20).string()).opt("reason") as? String
                            } catch (e: JSONException) {
                                null // Not JSON: no reason.
                            }
                            reasons.add(reason)
                        }
                        response
                    }
                    .build()
                val targeting = ExperimentationClient(config, HttpClient(baseUrl, apiKey, config.timeoutMs, recording))
                val pro = targeting.evaluateFlag(targetedKey, User(userId, mapOf("plan" to "pro")))
                targeting.clearCache()
                val free = targeting.evaluateFlag(targetedKey, User(userId, mapOf("plan" to "free")))
                targeting.close()
                if (reasons.size != 2) {
                    fail("the targeted flag was asked ${reasons.size} time(s), expected 2")
                }

                client.close()

                val report = JSONObject()
                    .put("sdk", "android")
                    .put(
                        "assign",
                        JSONObject()
                            .put("variant_name", first.variantName)
                            .put("is_control", first.isControl)
                            .put("sticky", sticky)
                    )
                    .put("flag", JSONObject().put("enabled", flag.enabled))
                    .put("track", JSONObject().put("ok", true))
                    .put("fanout", JSONObject().put("ok", true))
                    .put(
                        "targeted",
                        JSONObject()
                            .put("pro", pro.enabled)
                            .put("free", free.enabled)
                            // JSONObject.NULL: put(key, null) would drop the key.
                            .put("reason", reasons[0] ?: JSONObject.NULL)
                    )
                    .put(
                        "event_id",
                        JSONObject()
                            .put("user_id", userId)
                            .put("event_name", dedupName)
                            .put("id", dedupId)
                            .put("sends", 2)
                    )
                println(report.toString())
            }
        } catch (e: Exception) {
            fail("${e.javaClass.simpleName}: ${e.message}")
        }
        exitProcess(0)
    }
}

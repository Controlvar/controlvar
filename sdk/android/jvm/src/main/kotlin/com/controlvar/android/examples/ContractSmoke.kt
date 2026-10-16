package com.controlvar.android.examples

import com.controlvar.android.ExperimentationClient
import com.controlvar.android.HttpClient
import com.controlvar.android.SdkConfig
import com.controlvar.android.TrackEvent
import com.controlvar.android.User
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import kotlin.system.exitProcess

/**
 * Contract smoke for the Android SDK, run on a plain JVM (no emulator, no
 * Android SDK — see ../../pom.xml for why that works).
 *
 * Exercises the SDK's public API against a live backend and prints exactly one
 * JSON line on stdout on success (exit 0); on failure it prints one line to
 * stderr and exits 1.
 *
 * Environment: EXPERIMENTLY_API_URL (default http://localhost:8000),
 * EXPERIMENTLY_API_KEY (required), CONTRACT_EXPERIMENT_KEY (default
 * sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
 * CONTRACT_USER_ID (default smoke-<uuid>).
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
        val apiKey = env("EXPERIMENTLY_API_KEY", "")
        if (apiKey.isEmpty()) fail("EXPERIMENTLY_API_KEY is required")
        val baseUrl = env("EXPERIMENTLY_API_URL", "http://localhost:8000")
        val experimentKey = env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
        val flagKey = env("CONTRACT_FLAG_KEY", "sdk_contract_flag")
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
                println(report.toString())
            }
        } catch (e: Exception) {
            fail("${e.javaClass.simpleName}: ${e.message}")
        }
        exitProcess(0)
    }
}

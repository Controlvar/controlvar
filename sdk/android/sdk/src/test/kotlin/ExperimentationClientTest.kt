import com.controlvar.android.*
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONObject
import org.junit.jupiter.api.*
import org.junit.jupiter.api.Assertions.*
import java.util.concurrent.TimeUnit

/**
 * Test suite for the Controlvar Android SDK against OkHttp's MockWebServer.
 *
 * Tests cover:
 * - SdkConfig defaults and custom values
 * - ResultCache: set/get, TTL expiry, LRU eviction, prefix enumeration, thread safety
 * - ExperimentationClient.evaluateFlag: exact request (encoded path + user_id query, headers),
 *   response mapping, per user+key cache, TTL expiry, 404 / 401 / network failures,
 *   offline fallback, failures never cached
 * - ExperimentationClient.getAssignment: POST /api/v1/tracking/assign body, mapping,
 *   sticky cache hit, 404, offline fallback
 * - ExperimentationClient.track / trackBatch: /tracking/track body, /tracking/batch fan-out,
 *   nothing cached -> no request, chunking at 100, never throws
 * - OfflineStore: per user+key persistence of evaluations and assignments
 * - Model JSON parsing / serialization
 */
class ExperimentationClientTest {

    private lateinit var server: MockWebServer
    private lateinit var client: ExperimentationClient
    private lateinit var flagCache: ResultCache<EvalResult>
    private lateinit var assignmentCache: ResultCache<Assignment>
    private lateinit var offlineStore: OfflineStore

    private val assignJson = """{"experiment_key":"checkout_flow","user_id":"user-1",""" +
        """"variant_id":"8b6f1c2e-0000-4000-8000-000000000001","variant_name":"treatment",""" +
        """"is_control":false,"configuration":{"headline":"Buy now","discount":10}}"""

    // ---- Setup/Teardown ----

    @BeforeEach
    fun setUp() {
        server = MockWebServer()
        server.start()
        flagCache = ResultCache(maxSize = 100, ttlMs = 300_000L)
        assignmentCache = ResultCache(maxSize = 100, ttlMs = 300_000L)
        offlineStore = OfflineStore()
        client = newClient(cacheTtlMs = 300_000L)
    }

    @AfterEach
    fun tearDown() {
        client.close()
        server.shutdown()
    }

    private fun newClient(cacheTtlMs: Long): ExperimentationClient {
        val baseUrl = server.url("/").toString().trimEnd('/')
        val config = SdkConfig(baseUrl = baseUrl, apiKey = "test-key", cacheTtlMs = cacheTtlMs)
        val httpClient = HttpClient(baseUrl, "test-key", 5_000L)
        if (cacheTtlMs != 300_000L) {
            flagCache = ResultCache(maxSize = 100, ttlMs = cacheTtlMs)
            assignmentCache = ResultCache(maxSize = 100, ttlMs = cacheTtlMs)
        }
        return ExperimentationClient(config, httpClient, flagCache, assignmentCache, offlineStore)
    }

    private fun unreachableClient(): ExperimentationClient =
        ExperimentationClient(SdkConfig(baseUrl = "http://127.0.0.1:1", apiKey = "k", timeoutMs = 500L))

    private fun json(code: Int, body: String): MockResponse =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    private fun flagJson(key: String, enabled: Boolean, config: String = "null"): String =
        """{"key":"$key","enabled":$enabled,"config":$config}"""

    private fun takeRequest(): RecordedRequest {
        val request = server.takeRequest(2, TimeUnit.SECONDS)
        assertNotNull(request, "expected a request to reach the server")
        return request!!
    }

    private fun bodyOf(request: RecordedRequest): JSONObject = JSONObject(request.body.readUtf8())

    /** Routes by path so multi-request scenarios do not depend on enqueue order. */
    private fun routeByPath() {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path ?: return MockResponse().setResponseCode(404)
                return when {
                    path == "/api/v1/tracking/assign" -> json(200, assignJson)
                    path.startsWith("/api/v1/feature-flags/evaluate/") -> {
                        val key = path.removePrefix("/api/v1/feature-flags/evaluate/").substringBefore('?')
                        json(200, flagJson(key, true))
                    }
                    path == "/api/v1/tracking/track" -> json(200, """{"id":"evt-1"}""")
                    path == "/api/v1/tracking/batch" -> json(200, """{"success_count":1,"failure_count":0,"errors":null}""")
                    else -> json(404, """{"detail":"no route"}""")
                }
            }
        }
    }

    // ============================
    // Config tests
    // ============================

    @Test
    fun `config has sensible defaults`() {
        val config = SdkConfig(apiKey = "my-key")
        assertEquals("http://localhost:8000", config.baseUrl)
        assertEquals("my-key", config.apiKey)
        assertEquals(10_000L, config.timeoutMs)
        assertEquals(1000, config.cacheSize)
        assertEquals(300_000L, config.cacheTtlMs)
    }

    @Test
    fun `config accepts custom values`() {
        val config = SdkConfig(
            baseUrl = "https://api.example.com",
            apiKey = "custom-key",
            timeoutMs = 5_000L,
            cacheSize = 500,
            cacheTtlMs = 60_000L
        )
        assertEquals("https://api.example.com", config.baseUrl)
        assertEquals("custom-key", config.apiKey)
        assertEquals(5_000L, config.timeoutMs)
        assertEquals(500, config.cacheSize)
        assertEquals(60_000L, config.cacheTtlMs)
    }

    // ============================
    // ResultCache tests
    // ============================

    @Test
    fun `cache set and get returns correct value`() {
        flagCache.set("k", EvalResult("flag-a", true))
        val result = flagCache.get("k")
        assertNotNull(result)
        assertEquals("flag-a", result!!.key)
        assertTrue(result.enabled)
    }

    @Test
    fun `cache returns null on miss`() {
        assertNull(flagCache.get("nonexistent"))
    }

    @Test
    fun `cache respects TTL expiry and removes expired entries`() {
        val shortCache = ResultCache<EvalResult>(maxSize = 100, ttlMs = 1L)
        shortCache.set("k", EvalResult("flag-b", true))
        Thread.sleep(10)
        assertNull(shortCache.get("k"), "Entry should be expired after TTL")
        assertEquals(0, shortCache.size(), "Expired entry removed on access")
    }

    @Test
    fun `cache LRU eviction removes eldest entry`() {
        val smallCache = ResultCache<String>(maxSize = 3, ttlMs = 300_000L)
        smallCache.set("a", "1")
        smallCache.set("b", "2")
        smallCache.set("c", "3")
        smallCache.get("a") // make "a" recently used
        smallCache.set("d", "4") // evicts "b"
        assertNull(smallCache.get("b"), "b was least recently used and should be evicted")
        assertNotNull(smallCache.get("a"))
        assertNotNull(smallCache.get("c"))
        assertNotNull(smallCache.get("d"))
    }

    @Test
    fun `cache clear and remove`() {
        flagCache.set("x", EvalResult("x", true))
        flagCache.set("y", EvalResult("y", true))
        assertEquals(2, flagCache.size())
        flagCache.remove("x")
        assertNull(flagCache.get("x"))
        assertEquals(1, flagCache.size())
        flagCache.clear()
        assertNull(flagCache.get("y"))
        assertEquals(0, flagCache.size())
    }

    @Test
    fun `cache valuesWithPrefix returns live values for that prefix only`() {
        val shortCache = ResultCache<String>(maxSize = 100, ttlMs = 50L)
        shortCache.set(ResultCache.key("user-1", "expired"), "expired")
        Thread.sleep(100)
        shortCache.set(ResultCache.key("user-1", "b"), "b")
        shortCache.set(ResultCache.key("user-1", "c"), "c")
        shortCache.set(ResultCache.key("user-10", "a"), "other-user")

        assertEquals(listOf("b", "c"), shortCache.valuesWithPrefix(ResultCache.userPrefix("user-1")))
        assertEquals(listOf("other-user"), shortCache.valuesWithPrefix(ResultCache.userPrefix("user-10")))
        assertTrue(shortCache.valuesWithPrefix(ResultCache.userPrefix("user-2")).isEmpty())
        assertEquals(3, shortCache.size(), "expired entry removed during the scan")
    }

    @Test
    fun `cache is thread safe under concurrent access`() {
        val threads = (1..20).map { i ->
            Thread {
                repeat(50) {
                    flagCache.set("flag-$i", EvalResult("flag-$i", true))
                    flagCache.get("flag-$i")
                    flagCache.valuesWithPrefix("flag-")
                }
            }
        }
        threads.forEach { it.start() }
        threads.forEach { it.join() }
        // No exception = thread safe
    }

    // ============================
    // Client evaluateFlag tests
    // ============================

    @Test
    fun `evaluateFlag sends GET with encoded key, user_id query and headers`() = runTest {
        server.enqueue(json(200, flagJson("new search/v2", true, """{"variant":"blue","limit":3}""")))

        val result = client.evaluateFlag("new search/v2", User("user 1&2"))

        val request = takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/v1/feature-flags/evaluate/new%20search%2Fv2?user_id=user%201%262", request.path)
        assertEquals("user 1&2", request.requestUrl!!.queryParameter("user_id"))
        assertEquals(0L, request.bodySize, "GET must have no body")
        assertEquals("test-key", request.getHeader("X-API-Key"))
        assertEquals("application/json", request.getHeader("Accept"))
        assertEquals("application/json", request.getHeader("Content-Type"))
        assertNull(request.getHeader("Authorization"), "legacy Authorization header must not be sent")

        assertEquals("new search/v2", result.key)
        assertTrue(result.enabled)
        assertEquals("blue", result.configMap!!["variant"])
        assertEquals(3, result.configMap!!["limit"])
        assertEquals("blue", result.variant)
    }

    @Test
    fun `evaluateFlag maps a disabled flag with null config`() = runTest {
        server.enqueue(json(200, flagJson("off-flag", false)))
        val result = client.evaluateFlag("off-flag", User("user-1"))
        assertFalse(result.enabled)
        assertNull(result.config)
        assertNull(result.configMap)
        assertNull(result.variant)
    }

    @Test
    fun `evaluateFlag keeps a non-object config as the raw value`() = runTest {
        server.enqueue(json(200, flagJson("string-config", true, "\"just-a-string\"")))
        val result = client.evaluateFlag("string-config", User("user-1"))
        assertTrue(result.enabled)
        assertEquals("just-a-string", result.config)
        assertNull(result.configMap)
        assertEquals("on", result.variant)
    }

    @Test
    fun `evaluateFlag falls back to the requested key when the response omits it`() = runTest {
        server.enqueue(json(200, """{"enabled":true}"""))
        val result = client.evaluateFlag("no-key-in-body", User("user-1"))
        assertEquals("no-key-in-body", result.key)
    }

    @Test
    fun `evaluateFlag uses cache on second call without hitting server`() = runTest {
        server.enqueue(json(200, flagJson("cached-flag", true)))
        repeat(3) { assertTrue(client.evaluateFlag("cached-flag", User("user-1")).enabled) }
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `evaluateFlag caches per user and key`() = runTest {
        routeByPath()
        client.evaluateFlag("f", User("u1"))
        client.evaluateFlag("f", User("u2"))
        client.evaluateFlag("g", User("u1"))
        client.evaluateFlag("f", User("u1"))
        assertEquals(3, server.requestCount)
    }

    @Test
    fun `evaluateFlag re-fetches after the cache TTL expires`() = runTest {
        routeByPath()
        val shortTtl = newClient(cacheTtlMs = 50L)
        shortTtl.evaluateFlag("ttl-flag", User("user-ttl"))
        shortTtl.evaluateFlag("ttl-flag", User("user-ttl"))
        Thread.sleep(100)
        shortTtl.evaluateFlag("ttl-flag", User("user-ttl"))
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `evaluateFlag throws FlagNotFoundException on 404 and does not cache the failure`() = runTest {
        server.enqueue(json(404, """{"detail":"Feature flag not found"}"""))
        server.enqueue(json(404, """{"detail":"Feature flag not found"}"""))
        assertThrows<ExperimentationException.FlagNotFoundException> {
            client.evaluateFlag("ghost-flag", User("user-1"))
        }
        assertThrows<ExperimentationException.FlagNotFoundException> {
            client.evaluateFlag("ghost-flag", User("user-1"))
        }
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `evaluateFlag throws ServerException with status code on 401`() = runTest {
        server.enqueue(json(401, """{"detail":"Invalid API key"}"""))
        val ex = assertThrows<ExperimentationException.ServerException> {
            client.evaluateFlag("f", User("user-1"))
        }
        assertEquals(401, ex.statusCode)
        assertTrue(ex.message!!.contains("Invalid API key"))
    }

    @Test
    fun `evaluateFlag throws NetworkException on connection refused with no offline data`() = runTest {
        assertThrows<ExperimentationException.NetworkException> {
            unreachableClient().evaluateFlag("any-flag", User("u"))
        }
    }

    @Test
    fun `evaluateFlag falls back to offline store on server error`() = runTest {
        offlineStore.saveFlag("user-1", EvalResult("offline-flag", true))
        server.enqueue(json(503, """{"detail":"unavailable"}"""))
        val result = client.evaluateFlag("offline-flag", User("user-1"))
        assertTrue(result.enabled, "Should use offline fallback")
        assertEquals("offline-flag", result.key)
    }

    @Test
    fun `evaluateFlag offline fallback is per user and is not re-cached`() = runTest {
        offlineStore.saveFlag("user-1", EvalResult("offline-flag", true))
        server.enqueue(json(503, "{}"))
        server.enqueue(json(503, "{}"))
        server.enqueue(json(200, flagJson("offline-flag", false)))

        assertTrue(client.evaluateFlag("offline-flag", User("user-1")).enabled)
        assertThrows<ExperimentationException.ServerException> {
            client.evaluateFlag("offline-flag", User("user-2")) // nothing stored for user-2
        }
        // The fallback was not cached in memory: the next call retries the server.
        assertFalse(client.evaluateFlag("offline-flag", User("user-1")).enabled)
        assertEquals(3, server.requestCount)
    }

    @Test
    fun `evaluateFlag persists successful results to the offline store`() = runTest {
        server.enqueue(json(200, flagJson("persist-flag", true, """{"x":1}""")))
        client.evaluateFlag("persist-flag", User("user-1"))
        val stored = offlineStore.loadFlag("user-1", "persist-flag")
        assertNotNull(stored)
        assertTrue(stored!!.enabled)
        assertEquals(1, stored.configMap!!["x"])
        assertNull(offlineStore.loadFlag("user-2", "persist-flag"))
    }

    @Test
    fun `evaluateFlag rejects empty key or user id`() = runTest {
        assertThrows<IllegalArgumentException> { client.evaluateFlag("", User("u")) }
        assertThrows<IllegalArgumentException> { client.evaluateFlag("f", User("")) }
    }

    // ============================
    // Client getAssignment tests
    // ============================

    @Test
    fun `getAssignment sends POST tracking assign with experiment_key, user_id and context`() = runTest {
        server.enqueue(json(200, assignJson))
        val user = User("user-1", mapOf("plan" to "pro", "country" to "US"))

        val assignment = client.getAssignment("checkout_flow", user)

        val request = takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/tracking/assign", request.path)
        assertEquals("test-key", request.getHeader("X-API-Key"))
        assertTrue(request.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = bodyOf(request)
        assertEquals("checkout_flow", body.getString("experiment_key"))
        assertEquals("user-1", body.getString("user_id"))
        assertEquals("pro", body.getJSONObject("context").getString("plan"))
        assertEquals("US", body.getJSONObject("context").getString("country"))
        assertFalse(body.has("attributes"), "legacy 'attributes' field must not be sent")

        assertEquals("checkout_flow", assignment.experimentKey)
        assertEquals("user-1", assignment.userId)
        assertEquals("8b6f1c2e-0000-4000-8000-000000000001", assignment.variantId)
        assertEquals("treatment", assignment.variantName)
        assertFalse(assignment.isControl)
        assertEquals("Buy now", assignment.configuration!!["headline"])
        assertEquals(10, assignment.configuration!!["discount"])
    }

    @Test
    fun `getAssignment omits context when the user has no attributes`() = runTest {
        server.enqueue(json(200, assignJson))
        client.getAssignment("checkout_flow", User("user-1"))
        assertFalse(bodyOf(takeRequest()).has("context"))
    }

    @Test
    fun `getAssignment maps a control assignment with null configuration`() = runTest {
        server.enqueue(json(200, """{"experiment_key":"e","user_id":"u","variant_id":null,""" +
            """"variant_name":"control","is_control":true,"configuration":null}"""))
        val assignment = client.getAssignment("e", User("u"))
        assertEquals("control", assignment.variantName)
        assertTrue(assignment.isControl)
        assertNull(assignment.variantId)
        assertNull(assignment.configuration)
    }

    @Test
    fun `getAssignment is sticky - second call is served from the cache`() = runTest {
        server.enqueue(json(200, assignJson))
        val first = client.getAssignment("checkout_flow", User("user-1"))
        val second = client.getAssignment("checkout_flow", User("user-1"))
        assertEquals(first, second)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `getAssignment throws ServerException on 404 and does not cache the failure`() = runTest {
        server.enqueue(json(404, """{"detail":"No experiment has the key 'nope'."}"""))
        server.enqueue(json(404, """{"detail":"No experiment has the key 'nope'."}"""))
        val ex = assertThrows<ExperimentationException.ServerException> {
            client.getAssignment("nope", User("user-1"))
        }
        assertEquals(404, ex.statusCode)
        assertThrows<ExperimentationException.ServerException> {
            client.getAssignment("nope", User("user-1"))
        }
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `getAssignment throws NetworkException on IO failure`() = runTest {
        assertThrows<ExperimentationException.NetworkException> {
            unreachableClient().getAssignment("exp-1", User("u"))
        }
    }

    @Test
    fun `getAssignment falls back to the offline store and persists successes`() = runTest {
        server.enqueue(json(200, assignJson))
        client.getAssignment("checkout_flow", User("user-1"))
        val stored = offlineStore.loadAssignment("user-1", "checkout_flow")
        assertNotNull(stored)
        assertEquals("treatment", stored!!.variantName)

        client.clearCache()
        server.enqueue(json(500, """{"detail":"boom"}"""))
        val fallback = client.getAssignment("checkout_flow", User("user-1"))
        assertEquals("treatment", fallback.variantName)
        assertEquals(2, server.requestCount)
    }

    // ============================
    // Client track tests
    // ============================

    @Test
    fun `track with experiment key sends one POST tracking track`() = runTest {
        server.enqueue(json(200, """{"id":"evt-1"}"""))
        client.track(TrackEvent("user-track", "purchase", mapOf("sku" to "pro"), experimentKey = "checkout_flow", value = 12.5))

        val request = takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/tracking/track", request.path)
        assertEquals("test-key", request.getHeader("X-API-Key"))
        val body = bodyOf(request)
        assertEquals("purchase", body.getString("event_type"))
        assertEquals("purchase", body.getString("event_name"))
        assertEquals("user-track", body.getString("user_id"))
        assertEquals("checkout_flow", body.getString("experiment_key"))
        assertEquals(12.5, body.getDouble("value"), 1e-9)
        assertEquals("pro", body.getJSONObject("metadata").getString("sku"))
        assertFalse(body.has("feature_flag_key"))
        assertFalse(body.has("properties"), "legacy 'properties' field must not be sent")
        assertFalse(body.has("timestamp"))
    }

    @Test
    fun `track with feature flag key sends event_type override and ISO-8601 timestamp`() = runTest {
        server.enqueue(json(200, "{}"))
        client.track(TrackEvent("u1", "search", featureFlagKey = "new_search", eventType = "interaction",
            timestampMs = 1_789_165_800_000L))

        val body = bodyOf(takeRequest())
        assertEquals("interaction", body.getString("event_type"))
        assertEquals("search", body.getString("event_name"))
        assertEquals("new_search", body.getString("feature_flag_key"))
        assertEquals("2026-09-11T22:30:00.000Z", body.getString("timestamp"))
        for (absent in listOf("experiment_key", "value", "metadata")) {
            assertFalse(body.has(absent), "$absent must be omitted when unset")
        }
    }

    @Test
    fun `track without a key fans out via tracking batch to the cached assignment and flag`() = runTest {
        routeByPath()
        client.getAssignment("checkout_flow", User("user-1"))
        client.evaluateFlag("new_search", User("user-1"))
        client.evaluateFlag("new_search", User("user-2")) // must not leak into user-1's fan-out
        repeat(3) { takeRequest() }

        client.track(TrackEvent("user-1", "page_view", mapOf("page" to "/home")))

        val request = takeRequest()
        assertEquals("/api/v1/tracking/batch", request.path)
        val events = bodyOf(request).getJSONArray("events")
        assertEquals(2, events.length(), "one entry per cached assignment + one per cached flag")
        val first = events.getJSONObject(0)
        val second = events.getJSONObject(1)
        assertEquals("checkout_flow", first.getString("experiment_key"))
        assertFalse(first.has("feature_flag_key"))
        assertEquals("new_search", second.getString("feature_flag_key"))
        assertFalse(second.has("experiment_key"))
        for (i in 0 until events.length()) {
            val entry = events.getJSONObject(i)
            assertEquals("page_view", entry.getString("event_type"))
            assertEquals("page_view", entry.getString("event_name"))
            assertEquals("user-1", entry.getString("user_id"))
            assertEquals("/home", entry.getJSONObject("metadata").getString("page"))
        }
        assertEquals(4, server.requestCount)
    }

    @Test
    fun `track without a key and nothing cached sends nothing`() = runTest {
        routeByPath()
        client.track(TrackEvent("nobody", "page_view"))
        client.trackBatch(listOf(TrackEvent("nobody", "page_view")))
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `track never throws on server error, unreachable server or invalid input`() = runTest {
        // Any exception escaping track / trackBatch fails this test.
        server.enqueue(json(500, """{"detail":"server error"}"""))
        client.track(TrackEvent("u1", "click", experimentKey = "e"))
        takeRequest()

        client.track(TrackEvent("", "click", experimentKey = "e"))
        client.track(TrackEvent("u1", "", experimentKey = "e"))
        client.trackBatch(emptyList())
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS), "invalid events must not produce requests")
        assertEquals(1, server.requestCount)

        val unreachable = unreachableClient()
        unreachable.track(TrackEvent("u1", "click", experimentKey = "e"))
        unreachable.trackBatch(listOf(TrackEvent("u1", "click", featureFlagKey = "f")))
    }

    @Test
    fun `trackBatch sends keyed events in one tracking batch request`() = runTest {
        server.enqueue(json(200, """{"success_count":2,"failure_count":0,"errors":null}"""))
        client.trackBatch(listOf(
            TrackEvent("u1", "page_view", experimentKey = "e"),
            TrackEvent("u1", "page_view", featureFlagKey = "f")
        ))
        val request = takeRequest()
        assertEquals("/api/v1/tracking/batch", request.path)
        val events = bodyOf(request).getJSONArray("events")
        assertEquals(2, events.length())
        assertEquals("e", events.getJSONObject(0).getString("experiment_key"))
        assertEquals("f", events.getJSONObject(1).getString("feature_flag_key"))
    }

    @Test
    fun `trackBatch with a single keyed event still uses tracking batch`() = runTest {
        server.enqueue(json(200, """{"success_count":1,"failure_count":0,"errors":null}"""))
        client.trackBatch(listOf(TrackEvent("u1", "purchase", experimentKey = "e", value = 1.0)))
        val request = takeRequest()
        assertEquals("/api/v1/tracking/batch", request.path)
        val events = bodyOf(request).getJSONArray("events")
        assertEquals(1, events.length())
        assertEquals("e", events.getJSONObject(0).getString("experiment_key"))
        assertEquals(1.0, events.getJSONObject(0).getDouble("value"), 1e-9)
    }

    @Test
    fun `track with both keys sends experiment_key and feature_flag_key in one request`() = runTest {
        server.enqueue(json(200, "{}"))
        client.track(TrackEvent("u1", "click", experimentKey = "e", featureFlagKey = "f"))
        val request = takeRequest()
        assertEquals("/api/v1/tracking/track", request.path)
        assertEquals("application/json", request.getHeader("Accept"))
        val body = bodyOf(request)
        assertEquals("e", body.getString("experiment_key"))
        assertEquals("f", body.getString("feature_flag_key"))
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS), "exactly one request for a keyed event")
    }

    @Test
    fun `trackBatch chunks at 100 events per request`() = runTest {
        routeByPath()
        client.trackBatch((0 until 150).map { TrackEvent("u$it", "click", experimentKey = "e") })
        val sizes = (0 until 2).map {
            val request = takeRequest()
            assertEquals("/api/v1/tracking/batch", request.path)
            bodyOf(request).getJSONArray("events").length()
        }
        assertEquals(listOf(100, 50), sizes)
    }

    @Test
    fun `trackBatch expands unkeyed events from the cache and drops those with nothing cached`() = runTest {
        routeByPath()
        client.getAssignment("checkout_flow", User("user-1"))
        takeRequest()

        client.trackBatch(listOf(
            TrackEvent("user-1", "purchase", featureFlagKey = "f"),
            TrackEvent("user-1", "page_view"),   // fans out to checkout_flow
            TrackEvent("user-9", "page_view")    // nothing cached -> dropped
        ))

        val events = bodyOf(takeRequest()).getJSONArray("events")
        assertEquals(2, events.length())
        assertEquals("f", events.getJSONObject(0).getString("feature_flag_key"))
        assertEquals("checkout_flow", events.getJSONObject(1).getString("experiment_key"))
        assertEquals("page_view", events.getJSONObject(1).getString("event_name"))
    }

    // ============================
    // Cache access
    // ============================

    @Test
    fun `getCachedAssignments and getCachedFlagKeys list the user's live entries`() = runTest {
        routeByPath()
        client.getAssignment("checkout_flow", User("user-1"))
        client.evaluateFlag("new_search", User("user-1"))
        client.evaluateFlag("other", User("user-10"))

        assertEquals(listOf("checkout_flow"), client.getCachedAssignments("user-1").map { it.experimentKey })
        assertEquals(listOf("new_search"), client.getCachedFlagKeys("user-1"))
        assertTrue(client.getCachedAssignments("user-10").isEmpty())
        assertEquals(listOf("other"), client.getCachedFlagKeys("user-10"))
        assertTrue(client.getCachedFlagKeys("user-1x").isEmpty(), "prefix must not match a longer user id")
    }

    @Test
    fun `invalidateCache forces a fresh request for that user and key only`() = runTest {
        routeByPath()
        client.evaluateFlag("f", User("user-1"))
        client.getAssignment("checkout_flow", User("user-1"))
        assertEquals(2, server.requestCount)

        client.invalidateCache("user-1", "f")
        assertNull(offlineStore.loadFlag("user-1", "f"))
        client.evaluateFlag("f", User("user-1"))
        client.getAssignment("checkout_flow", User("user-1"))
        assertEquals(3, server.requestCount)
    }

    @Test
    fun `clearCache and close drop evaluations and assignments`() = runTest {
        routeByPath()
        client.evaluateFlag("f", User("user-1"))
        client.getAssignment("checkout_flow", User("user-1"))
        assertEquals(2, client.getCacheSize())

        client.clearCache()
        assertEquals(0, client.getCacheSize())
        client.evaluateFlag("f", User("user-1"))
        assertEquals(3, server.requestCount)

        client.close()
        assertEquals(0, client.getCacheSize())
    }

    @Test
    fun `multiple concurrent evaluateFlag calls are thread safe`() = runTest {
        routeByPath()
        val jobs = (1..10).map { i ->
            async { client.evaluateFlag("concurrent-flag", User("user-$i")) }
        }
        val results = jobs.map { it.await() }
        assertTrue(results.all { it.enabled }, "All concurrent evaluations should succeed")
        assertEquals(10, client.getCacheSize())
    }

    // ============================
    // OfflineStore tests
    // ============================

    @Test
    fun `offlineStore flag save and load roundtrip per user`() {
        val store = OfflineStore()
        store.saveFlag("user-1", EvalResult("stored-flag", true, mapOf("limit" to 3, "variant" to "blue")))
        val loaded = store.loadFlag("user-1", "stored-flag")
        assertNotNull(loaded)
        assertEquals("stored-flag", loaded!!.key)
        assertTrue(loaded.enabled)
        assertEquals(3, loaded.configMap!!["limit"])
        assertEquals("blue", loaded.variant)
        assertNull(store.loadFlag("user-2", "stored-flag"))
        assertNull(store.loadFlag("user-1", "does-not-exist"))
    }

    @Test
    fun `offlineStore assignment save and load roundtrip per user`() {
        val store = OfflineStore()
        val assignment = Assignment("exp", "user-1", "v-1", "treatment", false, mapOf("headline" to "Buy now"))
        store.saveAssignment("user-1", assignment)
        assertEquals(assignment, store.loadAssignment("user-1", "exp"))
        assertNull(store.loadAssignment("user-2", "exp"))

        val control = Assignment("exp", "user-1", null, "control", true, null)
        store.saveAssignment("user-1", control)
        assertEquals(control, store.loadAssignment("user-1", "exp"), "overwrite updates the entry")
    }

    @Test
    fun `offlineStore remove, size and clearAll`() {
        val store = OfflineStore()
        store.saveFlag("u", EvalResult("f1", true))
        store.saveFlag("u", EvalResult("f2", false))
        store.saveAssignment("u", Assignment("e", "u", "v", "control", true))
        assertEquals(3, store.size())
        store.removeFlag("u", "f1")
        store.removeAssignment("u", "e")
        assertNull(store.loadFlag("u", "f1"))
        assertNull(store.loadAssignment("u", "e"))
        assertNotNull(store.loadFlag("u", "f2"))
        store.clearAll()
        assertNull(store.loadFlag("u", "f2"))
        assertEquals(0, store.size())
    }

    @Test
    fun `offlineStore keys with underscores do not collide`() {
        val store = OfflineStore()
        store.saveFlag("a_b", EvalResult("c", true))
        store.saveFlag("a", EvalResult("b_c", false))
        assertTrue(store.loadFlag("a_b", "c")!!.enabled)
        assertFalse(store.loadFlag("a", "b_c")!!.enabled)
    }

    @Test
    fun `offlineStore returns null for corrupt entries`() {
        val prefs = object : OfflineStore.PrefsAdapter {
            val map = mutableMapOf<String, String>()
            override fun getString(key: String) = map[key]
            override fun putString(key: String, value: String) { map[key] = value }
            override fun remove(key: String) { map.remove(key) }
            override fun allKeys() = map.keys.toSet()
            override fun clear() = map.clear()
        }
        val store = OfflineStore(prefs)
        store.saveFlag("u", EvalResult("f", true))
        val key = prefs.map.keys.single()
        prefs.map[key] = "not json"
        assertNull(store.loadFlag("u", "f"))
    }

    // ============================
    // Model JSON tests
    // ============================

    @Test
    fun `user attributes are accessible`() {
        val user = User("u-1", mapOf("plan" to "premium", "age" to 30))
        assertEquals("u-1", user.id)
        assertEquals("premium", user.attributes["plan"])
        assertEquals(30, user.attributes["age"])
    }

    @Test
    fun `evalResult fromJson parses key, enabled and nested config`() {
        val json = JSONObject("""{"key":"my-flag","enabled":true,"config":{"a":[1,2,{"b":null}],"c":"d"}}""")
        val result = EvalResult.fromJson(json)
        assertEquals("my-flag", result.key)
        assertTrue(result.enabled)
        val a = result.configMap!!["a"] as List<*>
        assertEquals(listOf(1, 2, mapOf("b" to null)), a)
        assertEquals("d", result.configMap!!["c"])
        assertEquals(result, EvalResult.fromJson(result.toJson()), "toJson/fromJson roundtrip")
    }

    @Test
    fun `assignment fromJson parses the contract fields`() {
        val assignment = Assignment.fromJson(JSONObject(assignJson))
        assertEquals("checkout_flow", assignment.experimentKey)
        assertEquals("user-1", assignment.userId)
        assertEquals("8b6f1c2e-0000-4000-8000-000000000001", assignment.variantId)
        assertEquals("treatment", assignment.variantName)
        assertFalse(assignment.isControl)
        assertEquals(10, assignment.configuration!!["discount"])
        assertEquals(assignment, Assignment.fromJson(assignment.toJson()), "toJson/fromJson roundtrip")
    }

    @Test
    fun `trackEvent toJson serializes the contract body`() {
        val json = TrackEvent("u-1", "page_view", mapOf("url" to "/home", "referrer" to null)).toJson()
        assertEquals("u-1", json.getString("user_id"))
        assertEquals("page_view", json.getString("event_name"))
        assertEquals("page_view", json.getString("event_type"))
        val metadata = json.getJSONObject("metadata")
        assertEquals("/home", metadata.getString("url"))
        assertTrue(metadata.isNull("referrer"))
        assertFalse(json.has("experiment_key"))
        assertFalse(json.has("feature_flag_key"))
        assertFalse(json.has("value"))
        assertFalse(json.has("timestamp"))
    }

    @Test
    fun `trackEvent with empty properties omits metadata`() {
        val json = TrackEvent("u-2", "app_open").toJson()
        assertEquals("u-2", json.getString("user_id"))
        assertFalse(json.has("metadata"))
        assertFalse(TrackEvent("u-2", "app_open").hasKey())
        assertTrue(TrackEvent("u-2", "app_open", experimentKey = "e").hasKey())
    }

    @Test
    fun `trackEvent isoTimestamp formats UTC`() {
        assertEquals("1970-01-01T00:00:00.000Z", TrackEvent.isoTimestamp(0L))
        assertEquals("2026-09-11T22:30:00.500Z", TrackEvent.isoTimestamp(1_789_165_800_500L))
    }

    @Test
    fun `exception types have correct messages`() {
        val networkEx = ExperimentationException.NetworkException("timeout")
        assertTrue(networkEx.message!!.contains("timeout"))

        val notFoundEx = ExperimentationException.FlagNotFoundException("my-flag")
        assertTrue(notFoundEx.message!!.contains("my-flag"))

        val serverEx = ExperimentationException.ServerException(500, "Internal Server Error")
        assertTrue(serverEx.message!!.contains("500"))
        assertEquals(500, serverEx.statusCode)
    }
}

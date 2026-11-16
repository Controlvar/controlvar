import com.controlvar.android.*
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.json.JSONObject
import org.junit.jupiter.api.*
import org.junit.jupiter.api.Assertions.*
import java.io.IOException
import java.net.URLDecoder
import java.util.Date
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
    fun `evaluateFlag removes the stored answer on 404 and reports not found`() = runTest {
        val user = User("user-1")
        // 1. A 200 with enabled:true is stored.
        server.enqueue(json(200, flagJson("f", true)))
        assertTrue(client.evaluateFlag("f", user).enabled)
        assertNotNull(offlineStore.loadFlag("user-1", "f"))

        // 2. After the memory cache is gone, a 404 throws and does not serve the stored answer.
        client.clearCache()
        server.enqueue(json(404, """{"detail":"Feature flag not found"}"""))
        assertThrows<ExperimentationException.FlagNotFoundException> {
            client.evaluateFlag("f", user)
        }
    }

    @Test
    fun `evaluateFlag 404 leaves nothing stored, so a later disconnect is a NetworkException`() = runTest {
        val user = User("user-1")
        server.enqueue(json(200, flagJson("f", true)))
        client.evaluateFlag("f", user)
        client.clearCache()
        server.enqueue(json(404, """{"detail":"Feature flag not found"}"""))
        runCatching { client.evaluateFlag("f", user) }

        // 3. The stored answer is gone.
        assertNull(offlineStore.loadFlag("user-1", "f"))

        // 4. A disconnect afterwards gives NetworkException, not the removed answer.
        val offline = ExperimentationClient(
            SdkConfig(baseUrl = "http://127.0.0.1:1", apiKey = "k", timeoutMs = 500L),
            HttpClient("http://127.0.0.1:1", "k", 500L),
            ResultCache(maxSize = 100, ttlMs = 300_000L),
            ResultCache(maxSize = 100, ttlMs = 300_000L),
            offlineStore,
        )
        try {
            assertThrows<ExperimentationException.NetworkException> {
                offline.evaluateFlag("f", user)
            }
        } finally {
            offline.close()
        }
    }

    @Test
    fun `evaluateFlag still serves the stored answer after a 500`() = runTest {
        val user = User("user-1")
        server.enqueue(json(200, flagJson("f", true)))
        assertTrue(client.evaluateFlag("f", user).enabled)

        client.clearCache()
        server.enqueue(json(500, """{"detail":"boom"}"""))
        val result = client.evaluateFlag("f", user)
        assertTrue(result.enabled, "A 500 keeps serving the stored answer")
        assertNotNull(offlineStore.loadFlag("user-1", "f"))
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
    // Flag evaluation sends the user's attributes (#438)
    // ============================

    /** Alphanumeric on purpose: a non-ASCII canary would hide inside `%C3%A9`. */
    private val canary = "attrcanary7f3"

    /**
     * The server's reading of a request line's query: split on `&`, `+` is a space, then
     * percent-decode as UTF-8 (what the server's query parser does). It is deliberately not
     * `HttpUrl.queryParameter`, so these checks do not share the SDK's own library.
     */
    private fun serverQuery(path: String): List<Pair<String, String>> =
        path.substringAfter('?', "").split('&').filter { it.isNotEmpty() }.map { part ->
            URLDecoder.decode(part.substringBefore('='), Charsets.UTF_8) to
                URLDecoder.decode(part.substringAfter('=', ""), Charsets.UTF_8)
        }

    /** The JSON object in `context`, read with [serverQuery]; null when there is no `context`. */
    private fun serverContext(path: String): JSONObject? =
        serverQuery(path).firstOrNull { it.first == "context" }?.let { JSONObject(it.second) }

    /** Answers `enabled` when [rule] holds for the decoded context, and reports the rule's reason. */
    private fun answerWhen(rule: (JSONObject?) -> Boolean) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val matched = rule(request.path?.let { serverContext(it) })
                val reason = if (matched) "targeting_rule" else "rollout"
                return json(200, """{"key":"f","enabled":$matched,"reason":"$reason","config":null}""")
            }
        }
    }

    /** The live check's rule: `plan == "pro"`. */
    private fun targetingServer() = answerWhen { it?.optString("plan") == "pro" }

    private val pro = User("u1", mapOf("plan" to "pro"))
    private val free = User("u1", mapOf("plan" to "free"))
    private val bare = User("u1")

    private val v3 = mapOf("q" to "a+b&c=d#e f/é😀!'()*~")
    private val v3a = mapOf("q" to "a+b&c=d#e fé")
    private val v4 = mapOf(
        "age" to 30,
        "beta" to true,
        "device" to mapOf("os" to "ios", "version" to "17.4.0"),
        "tags" to listOf("a", "b")
    )

    private suspend fun enabled(user: User): Boolean = client.evaluateFlag("f", user).enabled

    @Test
    @Tag("regression")
    fun `regression 438 - evaluateFlag sends the attributes as context after user_id`() = runTest {
        server.enqueue(json(200, flagJson("f", true)))

        client.evaluateFlag("f", pro)

        assertEquals(
            "/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D",
            takeRequest().path
        )
    }

    @Test
    @Tag("regression")
    fun `regression 438 - a targeting rule on attributes matches`() = runTest {
        targetingServer()

        assertTrue(enabled(pro), "the rule plan == pro must match")
        // A user the rule does not match is answered by the rollout.
        assertFalse(client.evaluateFlag("f", User("u2", mapOf("plan" to "free"))).enabled)
    }

    @Test
    fun `evaluateFlag - no attributes and an empty map send the user id alone`() = runTest {
        server.enqueue(json(200, flagJson("f", true)))
        client.evaluateFlag("f", bare)
        assertEquals("/api/v1/feature-flags/evaluate/f?user_id=u1", takeRequest().path)

        client.clearCache()
        server.enqueue(json(200, flagJson("f", true)))
        client.evaluateFlag("f", User("u1", emptyMap()))
        assertEquals("/api/v1/feature-flags/evaluate/f?user_id=u1", takeRequest().path)
    }

    @Test
    fun `evaluateFlag - the context reads back as the attributes with the server's rules`() = runTest {
        val vectors = listOf(
            v3 to JSONObject().put("q", "a+b&c=d#e f/é😀!'()*~"),
            v3a to JSONObject().put("q", "a+b&c=d#e fé"),
            v4 to JSONObject(
                """{"age":30,"beta":true,"device":{"os":"ios","version":"17.4.0"},"tags":["a","b"]}"""
            )
        )
        for ((attributes, expected) in vectors) {
            client.clearCache()
            server.enqueue(json(200, flagJson("f", true)))
            client.evaluateFlag("f", User("u1", attributes))

            val request = takeRequest()
            val query = serverQuery(request.path!!)
            assertEquals(listOf("user_id", "context"), query.map { it.first }, "no other parameter")
            assertEquals("u1", query[0].second)
            val actual = serverContext(request.path!!)!!
            assertTrue(expected.similar(actual), "context decoded to $actual, expected $expected")
            // HttpUrl reads the same object, so the two decoders agree on every vector.
            assertTrue(expected.similar(JSONObject(request.requestUrl!!.queryParameter("context")!!)))
        }
    }

    @Test
    fun `serverQuery reads a plus as a space and a raw ampersand as a new parameter`() {
        val plus = serverContext("/p?user_id=u1&context=%7B%22q%22%3A%22a+b%22%7D")!!
        assertEquals("a b", plus.getString("q"))
        // The context left unencoded: the value is cut at the `&`, so it is no JSON object.
        assertThrows<org.json.JSONException> {
            serverContext("/p?user_id=u1&context={\"q\":\"a&b\"}")
        }
        // A raw `+` in the value reads back as a space: not the attribute that was sent.
        assertEquals("a b", serverContext("/p?user_id=u1&context={\"q\":\"a+b\"}")!!.getString("q"))
    }

    @Test
    fun `evaluateFlag - the exact request for each pinned vector`() = runTest {
        // V2 is pinned byte for byte. V3 and V3a are measured at this head (OkHttp's encoder
        // writes `!'()*~` as %21%27%28%29*%7E); what makes them right is the decode test above.
        val expected = mapOf(
            mapOf("plan" to "pro") to
                "/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D",
            v3a to
                "/api/v1/feature-flags/evaluate/f?user_id=u1" +
                "&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%C3%A9%22%7D",
            v3 to
                "/api/v1/feature-flags/evaluate/f?user_id=u1" +
                "&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%2F%C3%A9%F0%9F%98%80%21%27%28%29*%7E%22%7D"
        )
        for ((attributes, path) in expected) {
            client.clearCache()
            server.enqueue(json(200, flagJson("f", true)))
            client.evaluateFlag("f", User("u1", attributes))
            assertEquals(path, takeRequest().path)
        }
    }

    @Test
    fun `evaluateFlag - the context is the attributes with the keys of every map sorted`() = runTest {
        val keys = (1..20).map { "k%02d".format(it) }
        val forward = LinkedHashMap<String, Any?>()
        keys.forEach { forward[it] = mapOf("b" to 1, "a" to 2) }
        val backward = LinkedHashMap<String, Any?>()
        keys.reversed().forEach { backward[it] = mapOf("a" to 2, "b" to 1) }

        server.enqueue(json(200, flagJson("f", true)))
        client.evaluateFlag("f", User("u1", forward))
        // The same attributes, written in another order, are the same context: one request.
        client.evaluateFlag("f", User("u1", backward))

        assertEquals(1, server.requestCount)
        val context = serverQuery(takeRequest().path!!)[1].second
        assertEquals(keys.joinToString(",", "{", "}") { "\"$it\":{\"a\":2,\"b\":1}" }, context)
    }

    // ---- The cache: one slot per user + flag, remembering the context it answered ----

    @Test
    fun `cache - other attributes ask the server again`() = runTest {
        targetingServer()
        assertTrue(enabled(pro))
        assertFalse(enabled(free))
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `cache - minimal pairs of attributes are different contexts`() = runTest {
        targetingServer()
        assertTrue(enabled(pro))
        assertFalse(enabled(User("u1", mapOf("tier" to "pro"))))
        assertEquals(2, server.requestCount)

        // {n: 1} and {n: "1"}: a number and a string are different attributes.
        answerWhen { it?.opt("n") is Int }
        assertTrue(enabled(User("u2", mapOf("n" to 1))))
        assertFalse(enabled(User("u2", mapOf("n" to "1"))))
        assertEquals(4, server.requestCount)
    }

    @Test
    fun `cache - one entry holds the latest answer, so A then B then A is three requests`() = runTest {
        targetingServer()
        assertEquals(listOf(true, false, true), listOf(enabled(pro), enabled(free), enabled(pro)))
        assertEquals(3, server.requestCount)
        assertEquals(1, client.getCacheSize(), "still one entry per user and flag")
    }

    @Test
    fun `cache - attributes then none is two requests, and none gets the no-attribute answer`() = runTest {
        targetingServer()
        assertTrue(enabled(pro))
        assertFalse(enabled(bare), "a stored attribute answer must not be served to a call without any")
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `cache - an empty map is the same context as none`() = runTest {
        targetingServer()
        enabled(User("u1", emptyMap()))
        enabled(bare)
        enabled(User("u1", emptyMap()))
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `cache - the same attributes are one request`() = runTest {
        targetingServer()
        assertTrue(enabled(pro))
        assertTrue(enabled(User("u1", mapOf("plan" to "pro"))))
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `resultCache get with a context returns only an entry stored with it`() {
        val cache = ResultCache<String>(maxSize = 10, ttlMs = 300_000L)
        val key = ResultCache.key("u1", "f")
        cache.set(key, "answer", "ctx-a")

        assertEquals("answer", cache.get(key, "ctx-a"))
        assertNull(cache.get(key, "ctx-b"))
        assertNull(cache.get(key, ""), "no attributes is its own context")
        assertEquals("answer", cache.get(key), "no context given means no check")
        assertEquals("answer", cache.get(key, null))
        assertEquals(1, cache.size(), "a mismatch keeps the entry until the new answer replaces it")

        cache.set(key, "newer", "ctx-b")
        assertEquals("newer", cache.get(key, "ctx-b"))
        assertNull(cache.get(key, "ctx-a"))
        cache.set(key, "plain")
        assertEquals("plain", cache.get(key, ""))
    }

    @Test
    fun `evaluateFlag - the keyless track fan-out lists one flag, with the latest answer`() = runTest {
        targetingServer()
        enabled(pro)
        enabled(free)
        assertEquals(listOf("f"), client.getCachedFlagKeys("u1"))
        takeRequest()
        takeRequest()

        client.track(TrackEvent("u1", "purchase"))

        val batch = takeRequest()
        assertEquals("/api/v1/tracking/batch", batch.path)
        val events = bodyOf(batch).getJSONArray("events")
        assertEquals(1, events.length(), "one entry per flag, not per attribute set")
        assertEquals("f", events.getJSONObject(0).getString("feature_flag_key"))
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    // ---- The offline store: the key is user + flag, as before ----

    private class FakePrefs : OfflineStore.PrefsAdapter {
        val map = linkedMapOf<String, String>()
        override fun getString(key: String): String? = map[key]
        override fun putString(key: String, value: String) { map[key] = value }
        override fun remove(key: String) { map.remove(key) }
        override fun allKeys(): Set<String> = map.keys.toSet()
        override fun clear() = map.clear()
    }

    @Test
    fun `evaluateFlag - the offline store key is user and flag as before`() = runTest {
        val prefs = FakePrefs()
        val store = OfflineStore(prefs)
        val withStore = ExperimentationClient(
            SdkConfig(baseUrl = server.url("/").toString().trimEnd('/'), apiKey = "k"),
            HttpClient(server.url("/").toString().trimEnd('/'), "k", 5_000L),
            ResultCache(maxSize = 100, ttlMs = 300_000L),
            ResultCache(maxSize = 100, ttlMs = 300_000L),
            store
        )
        targetingServer()

        withStore.evaluateFlag("f", pro)
        withStore.evaluateFlag("f", free)

        // A write under A and then under B lands in one key, holding B's answer.
        assertEquals(setOf("ep_flag_2_u1_f"), prefs.map.keys)
        assertFalse(store.loadFlag("u1", "f")!!.enabled)
        // invalidateCache removes it; clearAll leaves the backing store empty.
        withStore.invalidateCache("u1", "f")
        assertTrue(prefs.map.isEmpty())
        withStore.evaluateFlag("f", pro)
        assertEquals(1, prefs.map.size)
        store.clearAll()
        assertTrue(prefs.map.isEmpty(), "the raw backing store, not size()")
        withStore.close()
    }

    @Test
    fun `evaluateFlag - a 404 under other attributes removes the stored answer`() = runTest {
        // A is stored; a 404 arrives under B; then the network fails under A.
        server.enqueue(json(200, flagJson("f", true)))
        server.enqueue(json(404, """{"detail":"Feature flag not found"}"""))
        assertTrue(client.evaluateFlag("f", pro).enabled)
        assertNotNull(offlineStore.loadFlag("u1", "f"))

        assertThrows<ExperimentationException.FlagNotFoundException> {
            client.evaluateFlag("f", free)
        }
        assertNull(offlineStore.loadFlag("u1", "f"))

        val offline = ExperimentationClient(
            SdkConfig(baseUrl = "http://127.0.0.1:1", apiKey = "k", timeoutMs = 500L),
            HttpClient("http://127.0.0.1:1", "k", 500L),
            ResultCache(maxSize = 100, ttlMs = 300_000L),
            ResultCache(maxSize = 100, ttlMs = 300_000L),
            offlineStore
        )
        try {
            assertThrows<ExperimentationException.NetworkException> { offline.evaluateFlag("f", pro) }
        } finally {
            offline.close()
        }
    }

    @Test
    fun `evaluateFlag - a stored answer for other attributes is served when the server cannot answer`() = runTest {
        server.enqueue(json(200, flagJson("f", true)))
        server.enqueue(json(503, "{}"))
        assertTrue(client.evaluateFlag("f", pro).enabled)

        // By design: the last answer for this user and flag, whatever attributes it was asked with.
        assertTrue(client.evaluateFlag("f", free).enabled)
    }

    // ---- Errors and logs carry no query (the canary is in the attributes) ----

    private val canaryUser = User("u1", mapOf("c" to "attrcanary7f3", "plan" to "pro"))

    /** Every message in the chain, as `toString()` shows it, holds neither the canary nor the query. */
    private fun assertNoCanary(error: Throwable) {
        var cause: Throwable? = error
        var depth = 0
        while (cause != null && depth++ < 20) {
            val text = cause.toString()
            assertFalse(text.contains(canary), "the canary is in: $text")
            assertFalse(text.contains("context="), "the query is in: $text")
            assertFalse(text.contains("user_id="), "the query is in: $text")
            cause = cause.cause
        }
    }

    private fun chainText(error: Throwable): String =
        generateSequence(error) { it.cause }.take(20).joinToString(" <- ") { it.toString() }

    private fun clientFor(baseUrl: String, timeoutMs: Long = 2_000L) = ExperimentationClient(
        SdkConfig(baseUrl = baseUrl, apiKey = "k", timeoutMs = timeoutMs),
        HttpClient(baseUrl, "k", timeoutMs),
        ResultCache(maxSize = 100, ttlMs = 300_000L),
        ResultCache(maxSize = 100, ttlMs = 300_000L),
        OfflineStore()
    )

    @Test
    fun `errors - the canary check sees a canary anywhere in a cause chain`() {
        assertThrows<AssertionError> { assertNoCanary(RuntimeException("x", IOException("c=attrcanary7f3"))) }
        assertThrows<AssertionError> { assertNoCanary(RuntimeException("GET /f?user_id=u1&context=%7B%7D")) }
        assertNoCanary(RuntimeException("x", IOException("Failed to connect to localhost/127.0.0.1:1")))
    }

    @Test
    fun `errors - connection refused names no query anywhere in the cause chain`() = runTest {
        val error = assertThrows<ExperimentationException.NetworkException> {
            clientFor("http://127.0.0.1:1").evaluateFlag("f", canaryUser)
        }
        assertNoCanary(error)
        assertNotNull(error.cause, "the cause is kept when it is clean")
    }

    @Test
    fun `errors - a dropped connection names no query anywhere in the cause chain`() = runTest {
        // OkHttp retries a GET once on a new connection, so the server drops two.
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        val error = assertThrows<ExperimentationException.NetworkException> {
            clientFor("http://127.0.0.1:${server.port}").evaluateFlag("f", canaryUser)
        }
        assertNoCanary(error)
        assertTrue(chainText(error).contains("unexpected end of stream"), chainText(error))
    }

    @Test
    fun `errors - an unknown host names no query anywhere in the cause chain`() = runTest {
        val error = assertThrows<ExperimentationException> {
            clientFor("http://nohost.invalid").evaluateFlag("f", canaryUser)
        }
        assertNoCanary(error)
    }

    @Test
    fun `errors - a read timeout names no query anywhere in the cause chain`() = runTest {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        val error = assertThrows<ExperimentationException.NetworkException> {
            clientFor("http://127.0.0.1:${server.port}", timeoutMs = 300L).evaluateFlag("f", canaryUser)
        }
        assertNoCanary(error)
        assertTrue(chainText(error).contains("SocketTimeoutException"), chainText(error))
    }

    @Test
    fun `errors - an invalid base URL names no query`() = runTest {
        val error = assertThrows<IllegalArgumentException> {
            clientFor("http://local host:8000").evaluateFlag("f", canaryUser)
        }
        assertNoCanary(error)
    }

    @Test
    fun `errors - a transport message that repeats the URL is cut at its query and its cause dropped`() = runTest {
        val path = "/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22c%22%3A%22$canary%22%7D"
        fun clientThrowing(error: (okhttp3.Request) -> IOException) = HttpClient(
            "http://localhost:1", "k", 500L,
            OkHttpClient.Builder().addInterceptor(Interceptor { chain -> throw error(chain.request()) }).build()
        )

        // The IOExceptions in a chain (coroutine stack-trace recovery adds copies of the
        // NetworkException itself, which are not IOExceptions).
        fun transport(error: Throwable): List<Throwable> =
            generateSequence(error) { it.cause }.take(20).filter { it is IOException }.toList()

        // The message and the URL's query in a nested cause: the SDK's text is cut, the cause dropped.
        val repeated = assertThrows<ExperimentationException.NetworkException> {
            clientThrowing { IOException("failed: ${it.url}", IOException("inner ${it.url.query}")) }.get(path)
        }
        assertNoCanary(repeated)
        assertEquals(emptyList<Throwable>(), transport(repeated), "the cause that repeats the query is dropped")
        assertTrue(repeated.message!!.startsWith("Network error: failed: http://localhost:1/"), repeated.message)

        // Control: a clean message and cause are kept.
        val clean = assertThrows<ExperimentationException.NetworkException> {
            clientThrowing { IOException("socket closed", IOException("reset")) }.get(path)
        }
        assertEquals("Network error: socket closed", clean.message)
        assertEquals(listOf("socket closed", "reset"), transport(clean).map { it.message })

        // Control: a request with no query is as before.
        val noQuery = assertThrows<ExperimentationException.NetworkException> {
            clientThrowing { IOException("failed: ${it.url}") }.get("/health")
        }
        assertEquals("Network error: failed: http://localhost:1/health", noQuery.message)
        assertEquals(1, transport(noQuery).size)
    }

    // ---- The two writers agree, and what cannot be written as JSON ----

    @Test
    fun `evaluateFlag - the flag context and the assignment context decode to the same object`() = runTest {
        val attributes = mapOf(
            "n" to 1,
            "big" to 12345678901L,
            "ratio" to 1.5f,
            "when" to Date(0),
            "nested" to mapOf("z" to 1, "a" to listOf(1, "two", mapOf("k" to null))),
            "nothing" to null,
            "on" to true,
            "text" to "x/y é + & ="
        )
        server.enqueue(json(200, flagJson("f", true)))
        server.enqueue(json(200, assignJson))

        client.evaluateFlag("f", User("u1", attributes))
        client.getAssignment("checkout_flow", User("u1", attributes))

        val flagContext = serverContext(takeRequest().path!!)!!
        val assignContext = bodyOf(takeRequest()).getJSONObject("context")
        assertTrue(assignContext.similar(flagContext), "flag $flagContext, assignment $assignContext")
        // A value that is not a JSON type goes as its toString(), the same on both calls.
        assertEquals(Date(0).toString(), flagContext.getString("when"))
    }

    @Test
    fun `evaluateFlag - attributes that cannot be written as JSON throw and send nothing`() = runTest {
        offlineStore.saveFlag("u1", EvalResult("f", true))
        val unencodable = listOf<Any?>(
            Double.NaN,
            Double.POSITIVE_INFINITY,
            Double.NEGATIVE_INFINITY,
            Float.NaN,
            mapOf("deep" to mapOf("x" to Double.NaN)),
            listOf(1, Double.POSITIVE_INFINITY)
        )
        for (bad in unencodable) {
            val error = assertThrows<IllegalArgumentException> {
                client.evaluateFlag("f", User("u1", mapOf("c" to canary, "bad" to bad)))
            }
            assertEquals("user.attributes cannot be sent as JSON", error.message)
            assertNull(error.cause)
        }

        assertEquals(0, server.requestCount, "no request is made")
        assertEquals(0, client.getCacheSize(), "nothing is cached")
        // The offline store was not consulted: the stored answer is not served, and not removed.
        assertNotNull(offlineStore.loadFlag("u1", "f"))
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

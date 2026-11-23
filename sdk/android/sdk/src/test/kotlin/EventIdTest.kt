import com.controlvar.android.*
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.json.JSONObject
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import java.net.InetAddress
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Every event the SDK sends carries an `event_id`, so the server stores an event that is sent
 * again only once (#1543).
 *
 * Every client here is built by the public constructors, so it sends with the `OkHttpClient`
 * an app gets: `ExperimentationClient(SdkConfig(...))`, or `HttpClient(baseUrl, apiKey,
 * timeoutMs)`. `track` and `trackBatch` are fire-and-forget, so each test reads the request
 * the server received with a bounded `takeRequest` before it tracks the next event; nothing
 * asserts a duration.
 */
class EventIdTest {

    private lateinit var server: MockWebServer
    private lateinit var client: ExperimentationClient

    /** A lowercase random (version 4) UUID, as `UUID.randomUUID().toString()` writes it. */
    private val uuidV4 = Regex("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

    @BeforeEach
    fun setUp() {
        server = MockWebServer()
        server.start(InetAddress.getByName("127.0.0.1"), 0)
        client = ExperimentationClient(SdkConfig(baseUrl = baseUrl(), apiKey = "test-key", timeoutMs = 5_000L))
    }

    @AfterEach
    fun tearDown() {
        client.close()
        server.shutdown()
    }

    private fun baseUrl(): String = "http://127.0.0.1:${server.port}"

    private fun json(body: String): MockResponse =
        MockResponse().setResponseCode(200).setHeader("Content-Type", "application/json").setBody(body)

    /**
     * Answers every route the client uses. A request for which [drop] is true is read in full,
     * and then its connection is closed without an answer.
     */
    private fun serve(drop: (RecordedRequest) -> Boolean = { false }) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (drop(request)) return MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST)
                val path = request.path ?: ""
                return when {
                    // No experiment_key in the answer: the client keeps the key it asked for.
                    path == "/api/v1/tracking/assign" -> json("""{"variant_name":"treatment","is_control":false}""")
                    path.startsWith("/api/v1/feature-flags/evaluate/") -> {
                        val key = path.removePrefix("/api/v1/feature-flags/evaluate/").substringBefore('?')
                        json("""{"key":"$key","enabled":true,"config":null}""")
                    }
                    path == "/api/v1/tracking/track" -> json("""{"id":"evt-1"}""")
                    path == "/api/v1/tracking/batch" -> json("""{"success_count":1,"failure_count":0,"errors":null}""")
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
    }

    private fun takeRequest(): RecordedRequest {
        val request = server.takeRequest(5, TimeUnit.SECONDS)
        assertNotNull(request, "expected a request to reach the server")
        return request!!
    }

    private fun bodyOf(request: RecordedRequest): JSONObject = JSONObject(request.body.readUtf8())

    private fun entriesOf(body: JSONObject): List<JSONObject> {
        val events = body.getJSONArray("events")
        return (0 until events.length()).map { events.getJSONObject(it) }
    }

    private fun eventIdOf(body: JSONObject): String {
        assertTrue(body.has("event_id"), "no event_id in $body")
        return body.getString("event_id")
    }

    private fun assertUuidV4(id: String) {
        assertTrue(uuidV4.matches(id), "not a lowercase UUID v4: $id")
    }

    /** Caches two assignments and one flag for [userId]: an unkeyed event fans out to three entries. */
    private suspend fun cacheThreeTargets(userId: String) {
        val user = User(userId)
        client.getAssignment("checkout", user)
        client.getAssignment("pricing", user)
        client.evaluateFlag("new-search", user)
        repeat(3) { takeRequest() }
    }

    @Test
    fun `a keyed track sends a generated lowercase UUID v4 as event_id`() = runTest {
        serve()
        client.track(TrackEvent("u1", "purchase", experimentKey = "checkout"))

        val request = takeRequest()
        assertEquals("/api/v1/tracking/track", request.path)
        val id = eventIdOf(bodyOf(request))
        assertUuidV4(id)
        // Many ids, so a version or variant digit that is wrong one time in a few shows.
        repeat(64) { assertUuidV4(eventIdOf(trackOne())) }
    }

    private suspend fun trackOne(): JSONObject {
        client.track(TrackEvent("u1", "purchase", experimentKey = "checkout"))
        return bodyOf(takeRequest())
    }

    @Test
    fun `three track calls send three ids, the same TrackEvent twice among them`() = runTest {
        serve()
        val event = TrackEvent("u1", "purchase", experimentKey = "checkout")
        val ids = mutableListOf<String>()

        client.track(event)
        ids.add(eventIdOf(bodyOf(takeRequest())))
        client.track(event)
        ids.add(eventIdOf(bodyOf(takeRequest())))
        client.track(TrackEvent("u1", "purchase", experimentKey = "checkout"))
        ids.add(eventIdOf(bodyOf(takeRequest())))

        assertEquals(3, ids.toSet().size, "one new id per call: $ids")
        assertNull(event.eventId, "track does not change the caller's TrackEvent")
    }

    @Test
    fun `an unkeyed track fanned out to two assignments and a flag sends one id in every entry`() = runTest {
        serve()
        cacheThreeTargets("user-1")

        client.track(TrackEvent("user-1", "page_view"))

        val request = takeRequest()
        assertEquals("/api/v1/tracking/batch", request.path)
        val entries = entriesOf(bodyOf(request))
        assertEquals(3, entries.size, "two assignments and one flag")
        val ids = entries.map { eventIdOf(it) }.toSet()
        assertEquals(1, ids.size, "one id per event, shared by its fan-out entries: $ids")
        assertUuidV4(ids.single())
    }

    @Test
    fun `in trackBatch each event has its own id, shared by its own fan-out entries`() = runTest {
        serve()
        cacheThreeTargets("user-1")

        client.trackBatch(
            listOf(
                TrackEvent("user-1", "page_view"), // 3 entries
                TrackEvent("user-1", "purchase", experimentKey = "checkout"), // 1 entry
                TrackEvent("user-1", "page_view"), // 3 entries
            )
        )

        val ids = entriesOf(bodyOf(takeRequest())).map { eventIdOf(it) }
        assertEquals(7, ids.size)
        assertEquals(1, ids.subList(0, 3).toSet().size, "first event: $ids")
        assertEquals(1, ids.subList(4, 7).toSet().size, "third event: $ids")
        assertEquals(3, setOf(ids[0], ids[3], ids[4]).size, "three events, three ids: $ids")
        ids.forEach { assertUuidV4(it) }
    }

    @Test
    fun `the caller's eventId wins, on track, on every fan-out entry and on a second call`() = runTest {
        serve()
        cacheThreeTargets("user-1")
        val keyed = TrackEvent("user-1", "purchase", experimentKey = "checkout", eventId = "order-42")
        val unkeyed = TrackEvent("user-1", "page_view", eventId = "view-7")

        client.track(keyed)
        assertEquals("order-42", eventIdOf(bodyOf(takeRequest())))
        client.track(keyed)
        assertEquals("order-42", eventIdOf(bodyOf(takeRequest())), "the same id on a second call")
        client.track(unkeyed)
        val entries = entriesOf(bodyOf(takeRequest()))
        assertEquals(3, entries.size)
        entries.forEach { assertEquals("view-7", eventIdOf(it)) }

        client.trackBatch(listOf(keyed, unkeyed))
        val batch = entriesOf(bodyOf(takeRequest())).map { eventIdOf(it) }
        assertEquals(listOf("order-42", "view-7", "view-7", "view-7"), batch)
    }

    @Test
    fun `a null or empty eventId sets none, and the client makes one`() = runTest {
        serve()
        client.track(TrackEvent("u1", "purchase", experimentKey = "checkout", eventId = null))
        val fromNull = eventIdOf(bodyOf(takeRequest()))
        client.track(TrackEvent("u1", "purchase", experimentKey = "checkout", eventId = ""))
        val fromEmpty = eventIdOf(bodyOf(takeRequest()))

        assertUuidV4(fromNull)
        assertUuidV4(fromEmpty)
        assertNotEquals(fromNull, fromEmpty)
        // toJson makes no id: it writes event_id only when one is set.
        assertFalse(TrackEvent("u1", "purchase").toJson().has("event_id"))
        assertFalse(TrackEvent("u1", "purchase", eventId = "").toJson().has("event_id"))
        assertEquals("order-42", TrackEvent("u1", "purchase", eventId = "order-42").toJson().getString("event_id"))
    }

    /**
     * The connection drops after the server has read the whole request, and OkHttp, as the
     * public constructor configures it, sends the request again on a new connection by itself.
     * The pool is seeded only with awaited calls, whose connection is back in the pool when they
     * return, so the dropped request reuses a pooled connection, which is when OkHttp sends a
     * written request again.
     */
    @Test
    fun `OkHttp's re-send of a written POST carries the same event_id, on track and batch`() = runTest {
        val tracks = AtomicInteger()
        val batches = AtomicInteger()
        serve { request ->
            (request.path == "/api/v1/tracking/track" && tracks.incrementAndGet() == 1) ||
                (request.path == "/api/v1/tracking/batch" && batches.incrementAndGet() == 1)
        }
        val user = User("user-1")
        client.getAssignment("checkout", user)
        takeRequest()

        // /track
        client.track(TrackEvent("user-1", "purchase", experimentKey = "checkout"))
        val (trackBody, _) = assertSentAgain(takeRequest(), takeRequest(), "/api/v1/tracking/track")
        assertUuidV4(eventIdOf(trackBody))
        assertEquals(3, server.requestCount)

        // /batch: an unkeyed event, fanned out to the cached assignment and a flag
        client.evaluateFlag("new-search", user)
        takeRequest()
        client.track(TrackEvent("user-1", "page_view"))
        val (batchBody, _) = assertSentAgain(takeRequest(), takeRequest(), "/api/v1/tracking/batch")
        val ids = entriesOf(batchBody).map { eventIdOf(it) }
        assertEquals(2, ids.size)
        assertEquals(1, ids.toSet().size, "one id in every entry: $ids")
        assertEquals(6, server.requestCount)
    }

    /** The dropped request and the one OkHttp sent again: same path, same id(s), same bytes. */
    private fun assertSentAgain(
        dropped: RecordedRequest,
        again: RecordedRequest,
        path: String,
    ): Pair<JSONObject, JSONObject> {
        assertEquals(path, dropped.path)
        assertEquals(path, again.path)
        assertTrue(
            dropped.sequenceNumber > 0,
            "precondition: the dropped request went out on a new connection " +
                "(sequence ${dropped.sequenceNumber}), not a pooled one, so OkHttp had no reason to send it again"
        )
        assertEquals(0, again.sequenceNumber, "the request sent again came on a new connection")
        val first = dropped.body.readUtf8()
        val second = again.body.readUtf8()
        val firstBody = JSONObject(first)
        val secondBody = JSONObject(second)
        assertEquals(idsOf(firstBody), idsOf(secondBody), "the request sent again must carry the same event_id")
        assertEquals(first, second, "the request sent again must be the same bytes")
        return firstBody to secondBody
    }

    private fun idsOf(body: JSONObject): List<String> =
        if (body.has("events")) entriesOf(body).map { eventIdOf(it) } else listOf(eventIdOf(body))

    /**
     * A request that times out is not sent again. The timeout is driven through an awaited call
     * (`HttpClient.post` on the public constructor's client), and the window in which no second
     * request may arrive starts only after that call has failed.
     */
    @Test
    fun `a request that times out is not sent again`() = runTest {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        server.enqueue(json("""{"id":"evt-2"}"""))
        val http = HttpClient(baseUrl(), "test-key", 300L)
        val body = TrackEvent("u1", "purchase", experimentKey = "checkout", eventId = "order-42").toJson()

        val failure = runCatching { http.post("/api/v1/tracking/track", body) }.exceptionOrNull()

        assertTrue(failure is ExperimentationException.NetworkException, "expected a timeout, got $failure")
        assertEquals("order-42", eventIdOf(bodyOf(takeRequest())))
        assertNull(server.takeRequest(500, TimeUnit.MILLISECONDS), "a timed-out request was sent again")
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `an eventId the server would refuse throws IllegalArgumentException, from the constructor and copy`() {
        val refused = linkedMapOf(
            "256 characters" to "a".repeat(256),
            "a NUL" to "order\u0000-42",
            "a lone high surrogate" to "order-\uD83D",
            "a lone low surrogate" to "\uDE00-order",
        )
        for ((why, id) in refused) {
            val thrown = assertThrows(IllegalArgumentException::class.java, { TrackEvent("u1", "purchase", eventId = id) }, why)
            assertFalse(thrown.message.orEmpty().contains(id), "the message holds the id ($why)")
            assertThrows(IllegalArgumentException::class.java, { TrackEvent("u1", "purchase").copy(eventId = id) }, "copy: $why")
        }
    }

    @Test
    fun `an eventId of 255 characters is accepted, a four-byte character counted once`() {
        val ascii = "a".repeat(255)
        val fourByte = "😀".repeat(255) // 255 characters, 510 UTF-16 units
        assertEquals(255, fourByte.codePointCount(0, fourByte.length))
        assertEquals(ascii, TrackEvent("u1", "purchase", eventId = ascii).toJson().getString("event_id"))
        assertEquals(fourByte, TrackEvent("u1", "purchase", eventId = fourByte).toJson().getString("event_id"))
    }
}

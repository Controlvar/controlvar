import XCTest
@testable import Controlvar

// MARK: - Mock URLProtocol

/// URLProtocol subclass that intercepts every URLSession request during tests, records it
/// (including the JSON body) and answers with whatever `handler` returns.
final class MockURLProtocol: URLProtocol {
    struct Recorded {
        let request: URLRequest
        let body: Data?

        var method: String { request.httpMethod ?? "" }
        var path: String { request.url?.path ?? "" }
        var query: String { request.url?.query ?? "" }
        var json: [String: Any] {
            guard let body = body,
                  let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
            else { return [:] }
            return object
        }
        func header(_ name: String) -> String? { request.value(forHTTPHeaderField: name) }
    }

    static var handler: ((URLRequest) throws -> (status: Int, body: Data))?
    static var recorded: [Recorded] = []
    private static let lock = NSLock()

    static func reset() {
        lock.lock(); defer { lock.unlock() }
        handler = nil
        recorded = []
    }

    static func respond(status: Int = 200, json: String) {
        lock.lock(); defer { lock.unlock() }
        handler = { _ in (status, json.data(using: .utf8)!) }
    }

    static func fail(with error: Error) {
        lock.lock(); defer { lock.unlock() }
        handler = { _ in throw error }
    }

    static var requests: [Recorded] {
        lock.lock(); defer { lock.unlock() }
        return recorded
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body = MockURLProtocol.readBody(of: request)
        MockURLProtocol.lock.lock()
        MockURLProtocol.recorded.append(Recorded(request: request, body: body))
        let handler = MockURLProtocol.handler
        MockURLProtocol.lock.unlock()

        guard let handler = handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }

        do {
            let (status, data) = try handler(request)
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: status,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    /// URLSession hands the body to protocols as a stream, not `httpBody`.
    private static func readBody(of request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }
}

// MARK: - ExperimentationClientTests

final class ExperimentationClientTests: XCTestCase {

    // MARK: - Helpers

    private static let assignJSON = """
    {"experiment_key":"exp-1","user_id":"user-abc","variant_id":"11111111-2222-3333-4444-555555555555",
     "variant_name":"treatment","is_control":false,"configuration":{"headline":"Go","limit":3}}
    """

    private static let flagJSON = """
    {"key":"new-ui","enabled":true,"config":{"variant":"blue","limit":5}}
    """

    private var stores: [OfflineStore] = []

    private func makeClient(
        ttl: TimeInterval = 300,
        offlineFallback: Bool = false,
        apiKey: String = "test-key",
        baseURL: String = "http://localhost:8000",
        keyPrefix: String = "test_ep_\(UUID().uuidString)_"
    ) -> (client: ExperimentationClient, store: OfflineStore) {
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: sessionConfig)

        let store = OfflineStore(keyPrefix: keyPrefix)
        stores.append(store)
        let config = SdkConfig(
            baseURL: baseURL,
            apiKey: apiKey,
            cacheTTL: ttl,
            enableOfflineFallback: offlineFallback
        )
        return (ExperimentationClient(config: config, session: session, offlineStore: store), store)
    }

    private func makeUser(_ id: String = "user-abc", attributes: [String: Any]? = nil) -> User {
        User(id: id, attributes: attributes)
    }

    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    override func tearDown() {
        stores.forEach { $0.clearAll() }
        stores.removeAll()
        MockURLProtocol.reset()
        super.tearDown()
    }

    // MARK: - SdkConfig

    func testSdkConfig_defaults() {
        let config = SdkConfig(apiKey: "my-key")
        XCTAssertEqual(config.baseURL, "http://localhost:8000")
        XCTAssertEqual(config.apiKey, "my-key")
        XCTAssertEqual(config.timeout, 10.0)
        XCTAssertEqual(config.cacheSize, 1000)
        XCTAssertEqual(config.cacheTTL, 300.0)
        XCTAssertTrue(config.enableOfflineFallback)
    }

    func testSdkConfig_customValues() {
        let config = SdkConfig(
            baseURL: "https://api.example.com",
            apiKey: "prod-key",
            timeout: 30.0,
            cacheSize: 500,
            cacheTTL: 60.0,
            enableOfflineFallback: false
        )
        XCTAssertEqual(config.baseURL, "https://api.example.com")
        XCTAssertEqual(config.timeout, 30.0)
        XCTAssertEqual(config.cacheSize, 500)
        XCTAssertEqual(config.cacheTTL, 60.0)
        XCTAssertFalse(config.enableOfflineFallback)
    }

    // MARK: - UserKeyCache

    func testUserKeyCache_setAndGet_perUser() {
        let cache = UserKeyCache<EvalResult>(ttl: 300)
        cache.set(userId: "u1", key: "f", value: EvalResult(key: "f", enabled: true))
        XCTAssertEqual(cache.get(userId: "u1", key: "f")?.enabled, true)
        XCTAssertNil(cache.get(userId: "u2", key: "f"), "cache must be per user")
        XCTAssertNil(cache.get(userId: "u1", key: "g"))
    }

    func testUserKeyCache_expiry() async throws {
        let cache = UserKeyCache<EvalResult>(ttl: 0.02)
        cache.set(userId: "u1", key: "f", value: EvalResult(key: "f", enabled: true))
        try await Task.sleep(nanoseconds: 60_000_000)
        XCTAssertNil(cache.get(userId: "u1", key: "f"), "expired entry must return nil")
        XCTAssertEqual(cache.entries(userId: "u1").count, 0)
    }

    func testUserKeyCache_entries_insertionOrderAndLiveOnly() {
        let cache = UserKeyCache<Assignment>(ttl: 300)
        let a = Assignment(experimentKey: "a", userId: "u", variantId: nil, variantName: "control", isControl: true)
        let b = Assignment(experimentKey: "b", userId: "u", variantId: nil, variantName: "treatment", isControl: false)
        cache.set(userId: "u", key: "a", value: a)
        cache.set(userId: "u", key: "b", value: b)
        cache.set(userId: "u", key: "a", value: a) // overwrite keeps position
        XCTAssertEqual(cache.entries(userId: "u").map { $0.key }, ["a", "b"])
        cache.remove(userId: "u", key: "a")
        XCTAssertEqual(cache.entries(userId: "u").map { $0.key }, ["b"])
        XCTAssertEqual(cache.count, 1)
        cache.removeAll()
        XCTAssertEqual(cache.entries(userId: "u").count, 0)
        XCTAssertEqual(cache.count, 0)
    }

    func testUserKeyCache_keysDoNotCollideAcrossUsers() {
        let cache = UserKeyCache<EvalResult>(ttl: 300)
        cache.set(userId: "a:b", key: "c", value: EvalResult(key: "c", enabled: true))
        XCTAssertNil(cache.get(userId: "a", key: "b:c"))
    }

    func testUserKeyCache_contextIsCheckedOnlyWhenGiven() {
        let cache = UserKeyCache<EvalResult>(ttl: 300)
        cache.set(userId: "u1", key: "f", value: EvalResult(key: "f", enabled: true), context: #"{"plan":"pro"}"#)

        XCTAssertEqual(cache.get(userId: "u1", key: "f", context: #"{"plan":"pro"}"#)?.enabled, true)
        XCTAssertNil(cache.get(userId: "u1", key: "f", context: #"{"plan":"free"}"#))
        XCTAssertNil(cache.get(userId: "u1", key: "f", context: ""))
        XCTAssertEqual(cache.get(userId: "u1", key: "f")?.enabled, true, "no context given: no check (assignments)")
        XCTAssertEqual(cache.entries(userId: "u1").map { $0.key }, ["f"], "a miss on the context keeps the entry")
    }

    // MARK: - OfflineStore

    func testOfflineStore_evaluationRoundtrip() {
        let store = OfflineStore(keyPrefix: "test_ep_\(UUID().uuidString)_")
        defer { store.clearAll() }
        store.saveEvaluation(EvalResult(key: "flag", enabled: true, config: ["x": 1]), userId: "u1")

        let loaded = store.loadEvaluation(flagKey: "flag", userId: "u1")
        XCTAssertEqual(loaded?.key, "flag")
        XCTAssertEqual(loaded?.enabled, true)
        XCTAssertEqual(loaded?.config?["x"] as? Int, 1)
        XCTAssertNil(store.loadEvaluation(flagKey: "flag", userId: "u2"), "store is per user")
        XCTAssertNil(store.loadEvaluation(flagKey: "other", userId: "u1"))
    }

    func testOfflineStore_assignmentRoundtrip_removeAndClear() {
        let store = OfflineStore(keyPrefix: "test_ep_\(UUID().uuidString)_")
        defer { store.clearAll() }
        let assignment = Assignment(
            experimentKey: "exp", userId: "u1", variantId: "v-1", variantName: "treatment",
            isControl: false, configuration: ["cta": "Buy"]
        )
        store.saveAssignment(assignment, userId: "u1")

        let loaded = store.loadAssignment(experimentKey: "exp", userId: "u1")
        XCTAssertEqual(loaded?.variantName, "treatment")
        XCTAssertEqual(loaded?.variantId, "v-1")
        XCTAssertEqual(loaded?.isControl, false)
        XCTAssertEqual(loaded?.configuration?["cta"] as? String, "Buy")

        store.removeAssignment(experimentKey: "exp", userId: "u1")
        XCTAssertNil(store.loadAssignment(experimentKey: "exp", userId: "u1"))

        store.saveEvaluation(EvalResult(key: "f", enabled: false), userId: "u1")
        store.saveAssignment(assignment, userId: "u2")
        store.clearAll()
        XCTAssertNil(store.loadEvaluation(flagKey: "f", userId: "u1"))
        XCTAssertNil(store.loadAssignment(experimentKey: "exp", userId: "u2"))
    }

    // MARK: - evaluateFlag: request shape

    func testEvaluateFlag_sendsGetWithEncodedKeyAndUserIdQuery() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient(apiKey: "secret-key")

        _ = try await client.evaluateFlag("new ui/v2", user: makeUser("user a&b"))

        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 1)
        let request = requests[0]
        XCTAssertEqual(request.method, "GET")
        XCTAssertEqual(request.request.url?.absoluteString,
                       "http://localhost:8000/api/v1/feature-flags/evaluate/new%20ui%2Fv2?user_id=user%20a%26b")
        XCTAssertEqual(request.header("X-API-Key"), "secret-key")
        XCTAssertEqual(request.header("Accept"), "application/json")
        XCTAssertEqual(request.header("Content-Type"), "application/json")
        XCTAssertNil(request.body)
    }

    func testEvaluateFlag_mapsResponse() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient()

        let result = try await client.evaluateFlag("new-ui", user: makeUser())
        XCTAssertEqual(result.key, "new-ui")
        XCTAssertTrue(result.enabled)
        XCTAssertEqual(result.config?["variant"] as? String, "blue")
        XCTAssertEqual(result.config?["limit"] as? Int, 5)
    }

    func testEvaluateFlag_disabledWithNullConfig() async throws {
        MockURLProtocol.respond(json: #"{"key":"off","enabled":false,"config":null}"#)
        let (client, _) = makeClient()

        let result = try await client.evaluateFlag("off", user: makeUser())
        XCTAssertFalse(result.enabled)
        XCTAssertNil(result.config)
    }

    func testEvaluateFlag_nonObjectConfigIsNil() async throws {
        MockURLProtocol.respond(json: #"{"key":"scalar","enabled":true,"config":"blue"}"#)
        let (client, _) = makeClient()

        let result = try await client.evaluateFlag("scalar", user: makeUser())
        XCTAssertTrue(result.enabled)
        XCTAssertNil(result.config)
    }

    // MARK: - evaluateFlag: caching

    func testEvaluateFlag_secondCallIsServedFromCache() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient()
        let user = makeUser()

        _ = try await client.evaluateFlag("new-ui", user: user)
        MockURLProtocol.respond(status: 500, json: "gone")
        let result = try await client.evaluateFlag("new-ui", user: user)

        XCTAssertTrue(result.enabled)
        XCTAssertEqual(MockURLProtocol.requests.count, 1)
        XCTAssertEqual(client.getEvaluatedFlags(for: user.id), ["new-ui"])
    }

    func testEvaluateFlag_cacheIsPerUser() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient()

        _ = try await client.evaluateFlag("new-ui", user: makeUser("u1"))
        _ = try await client.evaluateFlag("new-ui", user: makeUser("u2"))

        XCTAssertEqual(MockURLProtocol.requests.count, 2)
    }

    func testEvaluateFlag_cacheExpiresAfterTTL() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient(ttl: 0.05)
        let user = makeUser()

        _ = try await client.evaluateFlag("new-ui", user: user)
        try await Task.sleep(nanoseconds: 120_000_000)
        MockURLProtocol.respond(json: #"{"key":"new-ui","enabled":false,"config":null}"#)
        let result = try await client.evaluateFlag("new-ui", user: user)

        XCTAssertFalse(result.enabled, "after TTL expiry the flag must be re-fetched")
        XCTAssertEqual(MockURLProtocol.requests.count, 2)
    }

    func testEvaluateFlag_concurrentCallsShareOneRequest() async throws {
        MockURLProtocol.handler = { _ in
            Thread.sleep(forTimeInterval: 0.05)
            return (200, Self.flagJSON.data(using: .utf8)!)
        }
        let (client, _) = makeClient()
        let user = makeUser()

        async let a = client.evaluateFlag("new-ui", user: user)
        async let b = client.evaluateFlag("new-ui", user: user)
        async let c = client.evaluateFlag("new-ui", user: user)
        let results = try await [a, b, c]

        XCTAssertEqual(results.map { $0.enabled }, [true, true, true])
        XCTAssertEqual(MockURLProtocol.requests.count, 1)
    }

    // MARK: - evaluateFlag: failures

    func testEvaluateFlag_404ThrowsFlagNotFoundAndIsNotCached() async throws {
        MockURLProtocol.respond(status: 404, json: #"{"detail":"Feature flag with key 'ghost' not found"}"#)
        let (client, _) = makeClient()

        do {
            _ = try await client.evaluateFlag("ghost", user: makeUser())
            XCTFail("Expected flagNotFound")
        } catch ExperimentationError.flagNotFound(let key) {
            XCTAssertEqual(key, "ghost")
        }
        XCTAssertEqual(client.getEvaluatedFlags(for: "user-abc"), [])

        _ = try? await client.evaluateFlag("ghost", user: makeUser())
        XCTAssertEqual(MockURLProtocol.requests.count, 2, "failures are never cached")
    }

    func testEvaluateFlag_serverErrorThrowsWhenNothingCached() async throws {
        MockURLProtocol.respond(status: 500, json: #"{"error":"boom"}"#)
        let (client, _) = makeClient()

        do {
            _ = try await client.evaluateFlag("any", user: makeUser())
            XCTFail("Expected serverError")
        } catch ExperimentationError.serverError(let code, _) {
            XCTAssertEqual(code, 500)
        }
    }

    func testEvaluateFlag_networkErrorThrowsWhenNothingCached() async throws {
        MockURLProtocol.fail(with: URLError(.notConnectedToInternet))
        let (client, _) = makeClient()

        do {
            _ = try await client.evaluateFlag("any", user: makeUser())
            XCTFail("Expected networkError")
        } catch ExperimentationError.networkError {
            // expected
        }
    }

    func testEvaluateFlag_decodingErrorOnMalformedBody() async throws {
        MockURLProtocol.respond(json: "not json")
        let (client, _) = makeClient()

        do {
            _ = try await client.evaluateFlag("bad", user: makeUser())
            XCTFail("Expected decodingError")
        } catch ExperimentationError.decodingError {
            // expected
        }
    }

    func testEvaluateFlag_networkErrorFallsBackToOfflineStore() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, store) = makeClient(offlineFallback: true)
        let user = makeUser()

        _ = try await client.evaluateFlag("new-ui", user: user)
        XCTAssertEqual(store.loadEvaluation(flagKey: "new-ui", userId: user.id)?.enabled, true)

        client.clearCache()
        MockURLProtocol.fail(with: URLError(.notConnectedToInternet))
        let result = try await client.evaluateFlag("new-ui", user: user)
        XCTAssertTrue(result.enabled, "last known value is served offline")
    }

    func testEvaluateFlag_404DoesNotUseOfflineStore() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, store) = makeClient(offlineFallback: true)
        let user = makeUser()
        _ = try await client.evaluateFlag("new-ui", user: user)
        client.clearCache()

        MockURLProtocol.respond(status: 404, json: #"{"detail":"Feature flag with key 'new-ui' not found"}"#)
        do {
            _ = try await client.evaluateFlag("new-ui", user: user)
            XCTFail("Expected flagNotFound")
        } catch ExperimentationError.flagNotFound {
            // expected: a definitive 404 never serves a stale value
        }
        XCTAssertNil(store.loadEvaluation(flagKey: "new-ui", userId: user.id))
    }

    // MARK: - evaluateFlag: the user's attributes as `context` (#438)

    /// For errors and logs: alphanumeric, so percent-encoding cannot hide it.
    private static let canary = "attrcanary7f3"
    private static let planPro: [String: Any] = ["plan": "pro"]
    private static let planFree: [String: Any] = ["plan": "free"]

    /// A recorded URL's query as the server reads it (Starlette's `parse_qsl`): the part after
    /// `?` and before any `#`, split on `&`, then on the first `=`; `+` is a space; then
    /// percent-decoded as UTF-8. Not `URLComponents.queryItems`, which leaves `+` as `+` where
    /// the server reads a space.
    static func serverQuery(of url: URL?) -> [(name: String, value: String)] {
        let string = url?.absoluteString ?? ""
        let sent = string.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)[0]
        guard let mark = sent.firstIndex(of: "?") else { return [] }
        return sent[sent.index(after: mark)...].split(separator: "&").map { pair in
            let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            func decode(_ part: Substring) -> String {
                let spaced = part.replacingOccurrences(of: "+", with: " ")
                return spaced.removingPercentEncoding ?? spaced
            }
            return (decode(parts[0]), parts.count > 1 ? decode(parts[1]) : "")
        }
    }

    /// The `context` the server reads from a recorded URL, parsed as JSON; `nil` without one.
    static func serverContext(of url: URL?) -> [String: Any]? {
        guard let value = serverQuery(of: url).first(where: { $0.name == "context" })?.value else {
            return nil
        }
        return (try? JSONSerialization.jsonObject(with: Data(value.utf8))) as? [String: Any]
    }

    /// A fake evaluate route for one flag, `f`, with the rule `plan equals pro` and a 0% rollout:
    /// on only when the `context` it reads has `plan == "pro"`.
    private static func answerByRule(delay: TimeInterval = 0) {
        MockURLProtocol.handler = { request in
            if delay > 0 { Thread.sleep(forTimeInterval: delay) }
            let matched = (ExperimentationClientTests.serverContext(of: request.url)?["plan"] as? String) == "pro"
            let json = matched
                ? #"{"key":"f","enabled":true,"config":null,"reason":"targeting_rule"}"#
                : #"{"key":"f","enabled":false,"config":null,"reason":"rollout"}"#
            return (200, Data(json.utf8))
        }
    }

    private func enabled(_ client: ExperimentationClient, _ attributes: [String: Any]?) async throws -> Bool {
        try await client.evaluateFlag("f", user: makeUser("u1", attributes: attributes)).enabled
    }

    /// P1, V2, byte for byte: `user_id` first, then `context`.
    func testRegression438_evaluateFlag_sendsAttributesAsContextAfterUserId() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient()

        _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: Self.planPro))

        XCTAssertEqual(
            MockURLProtocol.requests.map { $0.request.url?.absoluteString },
            ["http://localhost:8000/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D"]
        )
    }

    /// P2, V0 and V1, byte for byte: the request sent before attributes were.
    func testEvaluateFlag_noOrEmptyAttributesSendTheUserIdAlone() async throws {
        for attributes in [nil, [:]] as [[String: Any]?] {
            MockURLProtocol.reset()
            MockURLProtocol.respond(json: Self.flagJSON)
            let (client, _) = makeClient()

            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: attributes))

            XCTAssertEqual(
                MockURLProtocol.requests.map { $0.request.url?.absoluteString },
                ["http://localhost:8000/api/v1/feature-flags/evaluate/f?user_id=u1"],
                "attributes: \(String(describing: attributes))"
            )
        }
    }

    /// P1, V3, V3a and V4: the server reads back the attributes the caller passed, and nothing
    /// else, with its own decoding (`+` is a space, then percent-decoding, then JSON).
    func testEvaluateFlag_contextReadsBackAsTheAttributesWithTheServersRules() async throws {
        let vectors: [(name: String, attributes: [String: Any])] = [
            ("V3", ["q": "a+b&c=d#e f/é😀!'()*~"]),
            ("V3a", ["q": "a+b&c=d#e fé"]),
            ("V4", ["age": 30, "beta": true, "device": ["os": "ios", "version": "17.4.0"], "tags": ["a", "b"]]),
        ]
        for vector in vectors {
            MockURLProtocol.reset()
            MockURLProtocol.respond(json: Self.flagJSON)
            let (client, _) = makeClient()

            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: vector.attributes))

            let url = MockURLProtocol.requests.first?.request.url
            let query = Self.serverQuery(of: url)
            XCTAssertEqual(query.map { $0.name }, ["user_id", "context"], "\(vector.name): \(url?.absoluteString ?? "no request")")
            XCTAssertEqual(query.first?.value, "u1", vector.name)
            let context = Self.serverContext(of: url)
            XCTAssertNotNil(context, "\(vector.name): no JSON object in \(url?.absoluteString ?? "no request")")
            XCTAssertEqual(context.map { NSDictionary(dictionary: $0) }, NSDictionary(dictionary: vector.attributes), vector.name)
        }
    }

    /// #1720's exact-URL test, on V3a. Measured at 0e7e4ce2 plus this change (Swift 6.2.4,
    /// macOS 26); correctness is the decode assertion above, and this pins the bytes.
    func testEvaluateFlag_exactURLForPlusAmpersandEqualsHashSpaceAndAccent() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient()

        _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: ["q": "a+b&c=d#e fé"]))

        XCTAssertEqual(
            MockURLProtocol.requests.map { $0.request.url?.absoluteString },
            ["http://localhost:8000/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%C3%A9%22%7D"]
        )
    }

    /// P4: a rule on an attribute matches from the attributes the SDK sends.
    func testRegression438_aTargetingRuleOnAttributesMatches() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()

        let pro = try await client.evaluateFlag("f", user: makeUser("u1", attributes: Self.planPro))
        let free = try await client.evaluateFlag("f", user: makeUser("u2", attributes: Self.planFree))
        let none = try await client.evaluateFlag("f", user: makeUser("u3"))

        XCTAssertTrue(pro.enabled, "the rule plan == pro must match from the user's attributes")
        XCTAssertFalse(free.enabled)
        XCTAssertFalse(none.enabled)
    }

    /// P5(a): two calls that differ only in attributes get two answers.
    func testEvaluateFlag_cache_otherAttributesAskTheServerAgain() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()

        let first = try await enabled(client, Self.planPro)
        let second = try await enabled(client, Self.planFree)

        XCTAssertEqual([first, second], [true, false])
        XCTAssertEqual(MockURLProtocol.requests.count, 2)
    }

    /// P5(b): the minimal pairs are two entries' worth of requests.
    func testEvaluateFlag_cache_minimalPairsAskTwice() async throws {
        let pairs: [([String: Any], [String: Any])] = [
            (["plan": "pro"], ["tier": "pro"]),
            (["n": 1], ["n": "1"]),
        ]
        for (a, b) in pairs {
            MockURLProtocol.reset()
            Self.answerByRule()
            let (client, _) = makeClient()

            _ = try await enabled(client, a)
            _ = try await enabled(client, b)

            XCTAssertEqual(MockURLProtocol.requests.count, 2, "\(a) then \(b)")
        }
    }

    /// P5(c): one entry per user + flag, holding the newest answer: A, B, A is three requests.
    func testEvaluateFlag_cache_oneEntryPerUserAndFlag() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()

        let first = try await enabled(client, Self.planPro)
        let second = try await enabled(client, Self.planFree)
        let third = try await enabled(client, Self.planPro)

        XCTAssertEqual([first, second, third], [true, false, true])
        XCTAssertEqual(MockURLProtocol.requests.count, 3)
    }

    /// P5(d): attributes, then none: the second call gets the answer for no attributes.
    func testEvaluateFlag_cache_attributesThenNoneAsksAgain() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()

        let first = try await enabled(client, Self.planPro)
        let second = try await enabled(client, nil)

        XCTAssertEqual([first, second], [true, false])
        XCTAssertEqual(MockURLProtocol.requests.count, 2)
    }

    /// P5(e): no attributes and an empty map are the same call.
    func testEvaluateFlag_cache_noneAndEmptyAreOneEntry() async throws {
        for (a, b) in [([:], [:]), (nil, [:])] as [([String: Any]?, [String: Any]?)] {
            MockURLProtocol.reset()
            Self.answerByRule()
            let (client, _) = makeClient()

            _ = try await enabled(client, a)
            _ = try await enabled(client, b)

            XCTAssertEqual(MockURLProtocol.requests.count, 1, "\(String(describing: a)) then \(String(describing: b))")
        }
    }

    /// P5(f): the same attributes again are answered from the cache.
    func testEvaluateFlag_cache_sameAttributesAreOneRequest() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()

        _ = try await enabled(client, Self.planPro)
        let again = try await enabled(client, Self.planPro)

        XCTAssertTrue(again)
        XCTAssertEqual(MockURLProtocol.requests.count, 1)
    }

    /// P5(g): the keys are written sorted, so two dictionaries with the same 20 entries, whatever
    /// order they iterate in, make one context and one request.
    func testEvaluateFlag_cache_sameAttributesInAnotherOrderAreOneRequest() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()
        var small = [String: Any](minimumCapacity: 20)
        var large = [String: Any](minimumCapacity: 1000)
        for i in 0..<20 {
            small["k\(i)"] = i
            large["k\(i)"] = i
        }

        _ = try await enabled(client, small)
        _ = try await enabled(client, large)

        XCTAssertEqual(MockURLProtocol.requests.count, 1)
    }

    /// P6: concurrent calls share one request only when their attributes are the same.
    func testEvaluateFlag_concurrentCallsWithOtherAttributesDoNotShareARequest() async throws {
        Self.answerByRule(delay: 0.05)
        let (client, _) = makeClient()

        async let a = client.evaluateFlag("f", user: makeUser("u1", attributes: Self.planPro))
        async let b = client.evaluateFlag("f", user: makeUser("u1", attributes: Self.planFree))
        let answers = try await [a.enabled, b.enabled]

        XCTAssertEqual(answers, [true, false])
        XCTAssertEqual(MockURLProtocol.requests.count, 2)
    }

    func testEvaluateFlag_concurrentCallsWithTheSameAttributesShareARequest() async throws {
        Self.answerByRule(delay: 0.05)
        let (client, _) = makeClient()

        async let a = client.evaluateFlag("f", user: makeUser("u1", attributes: Self.planPro))
        async let b = client.evaluateFlag("f", user: makeUser("u1", attributes: Self.planPro))
        let answers = try await [a.enabled, b.enabled]

        XCTAssertEqual(answers, [true, true])
        XCTAssertEqual(MockURLProtocol.requests.count, 1)
    }

    /// P8: a flag evaluated with two sets of attributes is still one flag in the keyless-track
    /// fan-out, and its entry holds the latest answer.
    func testTrack_withoutKey_aFlagEvaluatedWithTwoSetsOfAttributesIsSentOnce() async throws {
        Self.answerByRule()
        let (client, _) = makeClient()
        _ = try await enabled(client, Self.planPro)
        _ = try await enabled(client, Self.planFree)
        XCTAssertEqual(client.getEvaluatedFlags(for: "u1"), ["f"])

        MockURLProtocol.reset()
        MockURLProtocol.respond(json: #"{"success_count":1,"failure_count":0,"errors":null}"#)
        try await client.track(TrackEvent(userId: "u1", eventName: "page_view"))

        XCTAssertEqual(MockURLProtocol.requests.count, 1)
        let events = MockURLProtocol.requests.first?.json["events"] as? [[String: Any]]
        XCTAssertEqual(events?.count, 1)
        XCTAssertEqual(events?.first?["feature_flag_key"] as? String, "f")

        MockURLProtocol.reset()
        let latest = try await enabled(client, Self.planFree)
        XCTAssertFalse(latest)
        XCTAssertEqual(MockURLProtocol.requests.count, 0, "answered from the entry")
    }

    /// P9: the offline store's key is user + flag, as before: answers under two sets of
    /// attributes land in one entry, holding the latest, and `clearOfflineCache` leaves nothing.
    func testEvaluateFlag_offlineStoreKeyIsUserAndFlagAsBefore() async throws {
        Self.answerByRule()
        let prefix = "test_ep_\(UUID().uuidString)_"
        let (client, store) = makeClient(offlineFallback: true, keyPrefix: prefix)
        func stored() -> [String] {
            UserDefaults.standard.dictionaryRepresentation().keys.filter { $0.hasPrefix(prefix) }.sorted()
        }

        _ = try await enabled(client, Self.planPro)
        _ = try await enabled(client, Self.planFree)

        XCTAssertEqual(stored(), [prefix + "_users", prefix + "flags.u1"])
        let bucket = UserDefaults.standard.dictionary(forKey: prefix + "flags.u1") as? [String: Data]
        XCTAssertEqual(bucket.map { Array($0.keys) }, ["f"])
        XCTAssertEqual(store.loadEvaluation(flagKey: "f", userId: "u1")?.enabled, false, "the latest answer")

        client.clearOfflineCache()
        XCTAssertEqual(stored(), [])
    }

    /// P10: a 404 under any attributes removes the stored answer, which a later network error
    /// under other attributes does not bring back.
    func testEvaluateFlag_a404UnderOtherAttributesRemovesTheStoredAnswer() async throws {
        Self.answerByRule()
        let (client, store) = makeClient(offlineFallback: true)
        let first = try await enabled(client, Self.planPro)
        XCTAssertTrue(first)

        MockURLProtocol.respond(status: 404, json: #"{"detail":"Feature flag with key 'f' not found"}"#)
        do {
            _ = try await enabled(client, Self.planFree)
            XCTFail("Expected flagNotFound")
        } catch ExperimentationError.flagNotFound {
            // expected
        }
        XCTAssertNil(store.loadEvaluation(flagKey: "f", userId: "u1"))

        client.clearCache()
        MockURLProtocol.fail(with: URLError(.notConnectedToInternet))
        do {
            _ = try await enabled(client, Self.planPro)
            XCTFail("the answer stored before the 404 must not be served")
        } catch ExperimentationError.networkError {
            // expected
        }
    }

    /// No attribute, and no query at all, in anything the thrown error prints: its description,
    /// its message, its userInfo, and the same for the error it wraps.
    private func assertNoCanary(_ error: Error, file: StaticString = #filePath, line: UInt = #line) {
        var errors = [error]
        if case ExperimentationError.networkError(let inner) = error {
            errors.append(inner)
        }
        for each in errors {
            let texts = [
                String(describing: each),
                String(reflecting: each),
                each.localizedDescription,
                String(describing: (each as NSError).userInfo),
            ]
            for text in texts {
                for marker in [Self.canary, "?", "context="] {
                    XCTAssertFalse(text.contains(marker), "\(marker) in \(text)", file: file, line: line)
                }
            }
        }
    }

    /// P11: URLSession's error names the request's URL, query and attributes included, in its
    /// userInfo; the error the SDK throws keeps the code and the system's sentence, nothing else.
    func testEvaluateFlag_aNetworkErrorCarriesNoAttribute() async throws {
        MockURLProtocol.handler = { request in
            throw URLError(.cannotConnectToHost, userInfo: [
                NSURLErrorFailingURLStringErrorKey: request.url?.absoluteString ?? "",
                NSURLErrorFailingURLErrorKey: request.url ?? URL(fileURLWithPath: "/"),
            ])
        }
        let (client, _) = makeClient()

        do {
            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: ["note": Self.canary]))
            XCTFail("Expected networkError")
        } catch {
            guard case ExperimentationError.networkError(let inner) = error else {
                return XCTFail("Expected networkError, got \(error)")
            }
            XCTAssertEqual((inner as? URLError)?.code, .cannotConnectToHost, "the code is kept")
            assertNoCanary(error)
        }
    }

    /// P11 for an error that is not a URLError (an injected URLProtocol's own): its domain and
    /// code are kept, its userInfo is not.
    func testEvaluateFlag_anotherErrorCarriesNoAttributeEither() async throws {
        MockURLProtocol.handler = { request in
            throw NSError(domain: "test.transport", code: 7, userInfo: [
                NSURLErrorFailingURLStringErrorKey: request.url?.absoluteString ?? "",
                NSLocalizedDescriptionKey: "failed: \(request.url?.absoluteString ?? "")",
            ])
        }
        let (client, _) = makeClient()

        do {
            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: ["note": Self.canary]))
            XCTFail("Expected networkError")
        } catch {
            guard case ExperimentationError.networkError(let inner) = error else {
                return XCTFail("Expected networkError, got \(error)")
            }
            XCTAssertEqual((inner as NSError).domain, "test.transport")
            XCTAssertEqual((inner as NSError).code, 7)
            assertNoCanary(error)
        }
    }

    /// P11 on a real URLSession error: nothing listens on port 1 of the loopback address.
    func testEvaluateFlag_aRealConnectionErrorCarriesNoAttribute() async throws {
        let client = ExperimentationClient(
            config: SdkConfig(baseURL: "http://127.0.0.1:1", apiKey: "k", timeout: 5, enableOfflineFallback: false),
            session: URLSession(configuration: .ephemeral)
        )

        do {
            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: ["note": Self.canary]))
            XCTFail("Expected networkError")
        } catch {
            guard case ExperimentationError.networkError = error else {
                return XCTFail("Expected networkError, got \(error)")
            }
            assertNoCanary(error)
        }
    }

    /// The error keeps the system's readable sentence for its code ("Could not connect to the
    /// server."), taken here from a plain URLSession request to the same closed port rather than
    /// typed, since a bare `URLError(code)` only reads "The operation couldn't be completed".
    func testEvaluateFlag_aRealConnectionErrorKeepsTheSystemsSentence() async throws {
        let systemSentence: String = await withCheckedContinuation { continuation in
            URLSession(configuration: .ephemeral).dataTask(with: URL(string: "http://127.0.0.1:1/")!) { _, _, error in
                continuation.resume(returning: error?.localizedDescription ?? "no error")
            }.resume()
        }
        XCTAssertNotEqual(systemSentence, URLError(.cannotConnectToHost).localizedDescription,
                          "the system's sentence is a readable one, not the bare code's")
        let client = ExperimentationClient(
            config: SdkConfig(baseURL: "http://127.0.0.1:1", apiKey: "k", timeout: 5, enableOfflineFallback: false),
            session: URLSession(configuration: .ephemeral)
        )

        do {
            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: ["note": Self.canary]))
            XCTFail("Expected networkError")
        } catch {
            guard case ExperimentationError.networkError(let inner) = error else {
                return XCTFail("Expected networkError, got \(error)")
            }
            XCTAssertEqual((inner as? URLError)?.code, .cannotConnectToHost)
            XCTAssertEqual(inner.localizedDescription, systemSentence)
            assertNoCanary(error)
        }
    }

    /// P11: a URL that cannot be built is named without its query.
    func testEvaluateFlag_anInvalidURLIsNamedWithoutItsQuery() async throws {
        let (client, _) = makeClient(baseURL: "http://local host:8000")

        do {
            _ = try await client.evaluateFlag("f", user: makeUser("u1", attributes: ["note": Self.canary]))
            XCTFail("Expected invalidConfig")
        } catch {
            guard case ExperimentationError.invalidConfig(let message) = error else {
                return XCTFail("Expected invalidConfig, got \(error)")
            }
            XCTAssertEqual(message, "Invalid URL: http://local host:8000/api/v1/feature-flags/evaluate/f")
            assertNoCanary(error)
        }
        XCTAssertEqual(MockURLProtocol.requests.count, 0)
    }

    /// P16: a flag evaluation and an assignment send the same attributes as the same object.
    func testEvaluateFlag_flagContextAndAssignmentContextAreTheSameObject() async throws {
        let attributes: [String: Any] = [
            "path": "a/b", "n": 3, "beta": true, "tags": ["x", "y"],
            "device": ["os": "ios", "version": "17.4.0"],
        ]
        MockURLProtocol.handler = { request in
            let assign = request.url?.path == "/api/v1/tracking/assign"
            return (200, Data((assign ? ExperimentationClientTests.assignJSON : ExperimentationClientTests.flagJSON).utf8))
        }
        let (client, _) = makeClient()
        let user = makeUser("u1", attributes: attributes)

        _ = try await client.evaluateFlag("f", user: user)
        _ = try await client.getAssignment("exp-1", user: user)

        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 2)
        let flag = Self.serverContext(of: requests.first?.request.url).map { NSDictionary(dictionary: $0) }
        let assign = (requests.last?.json["context"] as? [String: Any]).map { NSDictionary(dictionary: $0) }
        XCTAssertEqual(flag, NSDictionary(dictionary: attributes))
        XCTAssertEqual(flag, assign)
    }

    /// P17: attributes that cannot be written as JSON (a `Date`, a NaN) throw a fixed sentence
    /// that names no attribute; nothing is sent or cached, and the stored answer is not served.
    func testEvaluateFlag_attributesThatAreNotJSONThrowAndSendNothing() async throws {
        let unwritable: [[String: Any]] = [
            [Self.canary: Date(timeIntervalSince1970: 0)],
            ["n": Double.nan, "note": Self.canary],
        ]
        for attributes in unwritable {
            MockURLProtocol.reset()
            MockURLProtocol.respond(json: #"{"key":"f","enabled":true,"config":null}"#)
            let (client, store) = makeClient(offlineFallback: true)
            _ = try await enabled(client, nil)
            client.clearCache()
            XCTAssertEqual(store.loadEvaluation(flagKey: "f", userId: "u1")?.enabled, true)

            do {
                _ = try await enabled(client, attributes)
                XCTFail("Expected invalidConfig for \(attributes.keys.sorted())")
            } catch {
                guard case ExperimentationError.invalidConfig(let message) = error else {
                    XCTFail("Expected invalidConfig, got \(error)")
                    continue
                }
                XCTAssertEqual(message, "user.attributes cannot be sent as JSON")
                assertNoCanary(error)
            }
            XCTAssertEqual(MockURLProtocol.requests.count, 1, "nothing is sent for these attributes")
            XCTAssertEqual(client.getEvaluatedFlags(for: "u1"), [], "nothing is cached")
        }
    }

    // MARK: - getAssignment: request shape

    func testGetAssignment_postsExperimentKeyUserIdAndContext() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient(apiKey: "secret-key")
        let user = makeUser("user-abc", attributes: ["plan": "pro", "age": 30, "beta": true])

        _ = try await client.getAssignment("exp-1", user: user)

        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 1)
        let request = requests[0]
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.request.url?.absoluteString, "http://localhost:8000/api/v1/tracking/assign")
        XCTAssertEqual(request.header("X-API-Key"), "secret-key")
        XCTAssertEqual(request.header("Content-Type"), "application/json")
        XCTAssertEqual(request.header("Accept"), "application/json")

        let body = request.json
        XCTAssertEqual(body["experiment_key"] as? String, "exp-1")
        XCTAssertEqual(body["user_id"] as? String, "user-abc")
        let context = body["context"] as? [String: Any]
        XCTAssertEqual(context?["plan"] as? String, "pro")
        XCTAssertEqual(context?["age"] as? Int, 30)
        XCTAssertEqual(context?["beta"] as? Bool, true)
    }

    func testGetAssignment_omitsContextWithoutAttributes() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient()

        _ = try await client.getAssignment("exp-1", user: makeUser())

        let body = MockURLProtocol.requests[0].json
        XCTAssertNil(body["context"])
        XCTAssertEqual(Set(body.keys), ["experiment_key", "user_id"])
    }

    func testGetAssignment_mapsResponse() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient()

        let assignment = try await client.getAssignment("exp-1", user: makeUser())
        XCTAssertEqual(assignment.experimentKey, "exp-1")
        XCTAssertEqual(assignment.userId, "user-abc")
        XCTAssertEqual(assignment.variantId, "11111111-2222-3333-4444-555555555555")
        XCTAssertEqual(assignment.variantName, "treatment")
        XCTAssertFalse(assignment.isControl)
        XCTAssertEqual(assignment.configuration?["headline"] as? String, "Go")
        XCTAssertEqual(assignment.configuration?["limit"] as? Int, 3)
    }

    func testGetAssignment_controlWithNullConfiguration() async throws {
        MockURLProtocol.respond(json: #"{"experiment_key":"exp-1","user_id":"u","variant_id":"v","variant_name":"control","is_control":true,"configuration":null}"#)
        let (client, _) = makeClient()

        let assignment = try await client.getAssignment("exp-1", user: makeUser("u"))
        XCTAssertTrue(assignment.isControl)
        XCTAssertEqual(assignment.variantName, "control")
        XCTAssertNil(assignment.configuration)
    }

    // MARK: - getAssignment: caching and failures

    func testGetAssignment_isStickyFromCache() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient()
        let user = makeUser()

        let first = try await client.getAssignment("exp-1", user: user)
        MockURLProtocol.respond(status: 500, json: "gone")
        let second = try await client.getAssignment("exp-1", user: user)

        XCTAssertEqual(first.variantName, second.variantName)
        XCTAssertEqual(MockURLProtocol.requests.count, 1)
        XCTAssertEqual(client.getAssignments(for: user.id).map { $0.experimentKey }, ["exp-1"])
    }

    func testGetAssignment_404ThrowsExperimentNotFound() async throws {
        MockURLProtocol.respond(status: 404, json: #"{"detail":"No experiment has the key 'ghost-exp'."}"#)
        let (client, _) = makeClient()

        do {
            _ = try await client.getAssignment("ghost-exp", user: makeUser())
            XCTFail("Expected experimentNotFound")
        } catch ExperimentationError.experimentNotFound(let key) {
            XCTAssertEqual(key, "ghost-exp")
        }
        XCTAssertEqual(client.getAssignments(for: "user-abc").count, 0, "failures are never cached")
    }

    func testGetAssignment_networkErrorThrowsWithoutCache() async throws {
        MockURLProtocol.fail(with: URLError(.timedOut))
        let (client, _) = makeClient()

        do {
            _ = try await client.getAssignment("exp-1", user: makeUser())
            XCTFail("Expected networkError")
        } catch ExperimentationError.networkError {
            // expected
        }
    }

    func testGetAssignment_networkErrorFallsBackToOfflineStore() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient(offlineFallback: true)
        let user = makeUser()
        _ = try await client.getAssignment("exp-1", user: user)
        client.clearCache()

        MockURLProtocol.fail(with: URLError(.notConnectedToInternet))
        let assignment = try await client.getAssignment("exp-1", user: user)
        XCTAssertEqual(assignment.variantName, "treatment")
    }

    func testGetAssignment_concurrentCallsShareOneRequest() async throws {
        MockURLProtocol.handler = { _ in
            Thread.sleep(forTimeInterval: 0.05)
            return (200, Self.assignJSON.data(using: .utf8)!)
        }
        let (client, _) = makeClient()
        let user = makeUser()

        async let a = client.getAssignment("exp-1", user: user)
        async let b = client.getAssignment("exp-1", user: user)
        _ = try await [a, b]

        XCTAssertEqual(MockURLProtocol.requests.count, 1)
    }

    // MARK: - track with a key

    func testTrack_withExperimentKey_postsToTrackingTrack() async throws {
        MockURLProtocol.respond(json: #"{"id":"evt-1"}"#)
        let (client, _) = makeClient(apiKey: "secret-key")
        let when = Date(timeIntervalSince1970: 1_700_000_000)

        try await client.track(TrackEvent(
            userId: "user-abc",
            eventName: "purchase",
            properties: ["sku": "pro", "qty": 2],
            experimentKey: "exp-1",
            value: 12.5,
            timestamp: when
        ))

        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 1)
        let request = requests[0]
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.path, "/api/v1/tracking/track")
        XCTAssertEqual(request.header("X-API-Key"), "secret-key")
        XCTAssertEqual(request.header("Content-Type"), "application/json")

        let body = request.json
        XCTAssertEqual(body["event_type"] as? String, "purchase", "event_type defaults to the event name")
        XCTAssertEqual(body["event_name"] as? String, "purchase")
        XCTAssertEqual(body["user_id"] as? String, "user-abc")
        XCTAssertEqual(body["experiment_key"] as? String, "exp-1")
        XCTAssertNil(body["feature_flag_key"])
        XCTAssertEqual(body["value"] as? Double, 12.5)
        XCTAssertEqual((body["metadata"] as? [String: Any])?["sku"] as? String, "pro")
        XCTAssertEqual((body["metadata"] as? [String: Any])?["qty"] as? Int, 2)
        XCTAssertEqual(body["timestamp"] as? String, "2023-11-14T22:13:20.000Z")
    }

    func testTrack_withFeatureFlagKeyAndCustomEventType() async throws {
        MockURLProtocol.respond(json: "{}")
        let (client, _) = makeClient()

        try await client.track(TrackEvent(
            userId: "u", eventName: "search", featureFlagKey: "new-search", eventType: "interaction"
        ))

        let body = MockURLProtocol.requests[0].json
        XCTAssertEqual(body["event_type"] as? String, "interaction")
        XCTAssertEqual(body["event_name"] as? String, "search")
        XCTAssertEqual(body["feature_flag_key"] as? String, "new-search")
        XCTAssertNil(body["experiment_key"])
        XCTAssertNil(body["value"])
        XCTAssertNil(body["metadata"])
        XCTAssertNil(body["timestamp"])
    }

    // MARK: - track without a key (fan-out)

    func testTrack_withoutKey_andNothingCached_sendsNothing() async throws {
        MockURLProtocol.respond(json: "{}")
        let (client, _) = makeClient()

        try await client.track(TrackEvent(userId: "user-abc", eventName: "page_view"))
        let ok = await client.trackWithStatus(TrackEvent(userId: "user-abc", eventName: "page_view"))

        XCTAssertTrue(ok)
        XCTAssertEqual(MockURLProtocol.requests.count, 0)
    }

    func testTrack_withoutKey_fansOutToCachedAssignmentsAndFlags() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient()
        let user = makeUser("user-abc")
        _ = try await client.getAssignment("exp-1", user: user)
        MockURLProtocol.respond(json: Self.flagJSON)
        _ = try await client.evaluateFlag("new-ui", user: user)
        _ = try await client.evaluateFlag("new-ui", user: makeUser("someone-else"))
        MockURLProtocol.reset()
        MockURLProtocol.respond(json: #"{"success_count":2,"failure_count":0,"errors":null}"#)

        try await client.track(TrackEvent(userId: "user-abc", eventName: "page_view", properties: ["page": "/"], value: 1))

        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests[0].method, "POST")
        XCTAssertEqual(requests[0].path, "/api/v1/tracking/batch")

        let events = requests[0].json["events"] as? [[String: Any]]
        XCTAssertEqual(events?.count, 2, "one per cached assignment + one per cached flag of this user")
        XCTAssertEqual(events?[0]["experiment_key"] as? String, "exp-1")
        XCTAssertNil(events?[0]["feature_flag_key"])
        XCTAssertEqual(events?[1]["feature_flag_key"] as? String, "new-ui")
        XCTAssertNil(events?[1]["experiment_key"])
        for event in events ?? [] {
            XCTAssertEqual(event["event_type"] as? String, "page_view")
            XCTAssertEqual(event["event_name"] as? String, "page_view")
            XCTAssertEqual(event["user_id"] as? String, "user-abc")
            XCTAssertEqual(event["value"] as? Double, 1)
            XCTAssertEqual((event["metadata"] as? [String: Any])?["page"] as? String, "/")
        }
    }

    func testTrack_withoutKey_ignoresExpiredCacheEntries() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient(ttl: 0.05)
        _ = try await client.evaluateFlag("new-ui", user: makeUser())
        try await Task.sleep(nanoseconds: 120_000_000)
        MockURLProtocol.reset()
        MockURLProtocol.respond(json: "{}")

        try await client.track(TrackEvent(userId: "user-abc", eventName: "page_view"))

        XCTAssertEqual(MockURLProtocol.requests.count, 0)
    }

    // MARK: - track never throws

    func testTrack_neverThrows_onServerError() async throws {
        MockURLProtocol.respond(status: 500, json: #"{"detail":"boom"}"#)
        let (client, _) = makeClient()

        try await client.track(TrackEvent(userId: "u", eventName: "e", experimentKey: "x"))
        let ok = await client.trackWithStatus(TrackEvent(userId: "u", eventName: "e", experimentKey: "x"))
        XCTAssertFalse(ok)
    }

    func testTrack_neverThrows_onNetworkError() async throws {
        MockURLProtocol.fail(with: URLError(.notConnectedToInternet))
        let (client, _) = makeClient()

        try await client.track(TrackEvent(userId: "u", eventName: "e", featureFlagKey: "f"))
        let ok = await client.trackWithStatus(TrackEvent(userId: "u", eventName: "e", featureFlagKey: "f"))
        XCTAssertFalse(ok)
    }

    func testTrack_neverThrows_on422() async throws {
        MockURLProtocol.respond(status: 422, json: #"{"detail":"invalid"}"#)
        let (client, _) = makeClient()

        try await client.track(TrackEvent(userId: "u", eventName: "e", experimentKey: "x"))
        XCTAssertEqual(MockURLProtocol.requests.count, 1)
    }

    // MARK: - trackBatch

    func testTrackBatch_sendsOneBatchRequest() async throws {
        MockURLProtocol.respond(json: #"{"success_count":2,"failure_count":0,"errors":null}"#)
        let (client, _) = makeClient()

        let ok = await client.trackBatch([
            TrackEvent(userId: "u", eventName: "add_to_cart", experimentKey: "exp-1", value: 1),
            TrackEvent(userId: "u", eventName: "checkout", featureFlagKey: "new-ui"),
        ])

        XCTAssertTrue(ok)
        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests[0].path, "/api/v1/tracking/batch")
        let events = requests[0].json["events"] as? [[String: Any]]
        XCTAssertEqual(events?.count, 2)
        XCTAssertEqual(events?[0]["experiment_key"] as? String, "exp-1")
        XCTAssertEqual(events?[1]["feature_flag_key"] as? String, "new-ui")
    }

    func testTrackBatch_emptyAndUnkeyedWithoutCacheSendNothing() async throws {
        MockURLProtocol.respond(json: "{}")
        let (client, _) = makeClient()

        let ok1 = await client.trackBatch([])
        let ok2 = await client.trackBatch([TrackEvent(userId: "u", eventName: "page_view")])

        XCTAssertTrue(ok1)
        XCTAssertTrue(ok2)
        XCTAssertEqual(MockURLProtocol.requests.count, 0)
    }

    func testTrackBatch_fansOutUnkeyedEventsAndChunksBy100() async throws {
        MockURLProtocol.respond(json: Self.assignJSON)
        let (client, _) = makeClient()
        _ = try await client.getAssignment("exp-1", user: makeUser("u"))
        MockURLProtocol.reset()
        MockURLProtocol.respond(json: "{}")

        var events = (0..<149).map { TrackEvent(userId: "u", eventName: "e\($0)", experimentKey: "exp-1") }
        events.append(TrackEvent(userId: "u", eventName: "page_view")) // fans out to exp-1 → 150 total
        let ok = await client.trackBatch(events)

        XCTAssertTrue(ok)
        let requests = MockURLProtocol.requests
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual((requests[0].json["events"] as? [Any])?.count, 100)
        XCTAssertEqual((requests[1].json["events"] as? [Any])?.count, 50)
        let last = (requests[1].json["events"] as? [[String: Any]])?.last
        XCTAssertEqual(last?["event_name"] as? String, "page_view")
        XCTAssertEqual(last?["experiment_key"] as? String, "exp-1")
    }

    func testTrackBatch_reportsFailure() async throws {
        MockURLProtocol.respond(status: 429, json: #"{"detail":"rate limited"}"#)
        let (client, _) = makeClient()

        let ok = await client.trackBatch([TrackEvent(userId: "u", eventName: "e", experimentKey: "x")])
        XCTAssertFalse(ok)
    }

    // MARK: - refreshFlags / close

    @available(*, deprecated)
    func testRefreshFlags_isANoOp() async throws {
        MockURLProtocol.respond(json: "[]")
        let (client, _) = makeClient()

        try await client.refreshFlags()

        XCTAssertEqual(MockURLProtocol.requests.count, 0)
    }

    func testClose_clearsInMemoryCaches() async throws {
        MockURLProtocol.respond(json: Self.flagJSON)
        let (client, _) = makeClient()
        let user = makeUser()
        _ = try await client.evaluateFlag("new-ui", user: user)

        client.close()
        XCTAssertEqual(client.getEvaluatedFlags(for: user.id), [])

        _ = try await client.evaluateFlag("new-ui", user: user)
        XCTAssertEqual(MockURLProtocol.requests.count, 2)
    }

    func testClient_convenienceInit() {
        let client = ExperimentationClient(baseURL: "https://api.example.com", apiKey: "key-123")
        XCTAssertNotNil(client)
    }

    // MARK: - Models

    func testUser_encoding() throws {
        let user = User(id: "user-xyz")
        let data = try JSONEncoder().encode(user)
        let decoded = try JSONDecoder().decode(User.self, from: data)
        XCTAssertEqual(decoded.id, "user-xyz")
        XCTAssertNil(decoded.attributes)
    }

    func testUser_withAttributes() throws {
        let user = User(id: "user-123", attributes: ["plan": "pro", "age": 30, "beta": true])
        XCTAssertEqual(user.attributes?["plan"]?.value as? String, "pro")
        XCTAssertEqual(user.attributes?["age"]?.value as? Int, 30)
        XCTAssertEqual(user.attributes?["beta"]?.value as? Bool, true)
    }

    func testTrackEvent_encodesWireFormatAndDecodes() throws {
        let event = TrackEvent(
            userId: "u1", eventName: "clicked", properties: ["page": "home"],
            experimentKey: "exp", value: 2.5, timestamp: Date(timeIntervalSince1970: 0)
        )
        let data = try JSONEncoder().encode(event)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(json?["event_type"] as? String, "clicked")
        XCTAssertEqual(json?["event_name"] as? String, "clicked")
        XCTAssertEqual(json?["user_id"] as? String, "u1")
        XCTAssertEqual(json?["experiment_key"] as? String, "exp")
        XCTAssertEqual((json?["metadata"] as? [String: Any])?["page"] as? String, "home")
        XCTAssertEqual(json?["timestamp"] as? String, "1970-01-01T00:00:00.000Z")

        let decoded = try JSONDecoder().decode(TrackEvent.self, from: data)
        XCTAssertEqual(decoded.userId, "u1")
        XCTAssertEqual(decoded.eventName, "clicked")
        XCTAssertEqual(decoded.experimentKey, "exp")
        XCTAssertEqual(decoded.value, 2.5)
        XCTAssertEqual(decoded.timestamp, Date(timeIntervalSince1970: 0))
        XCTAssertEqual(decoded.properties?["page"]?.value as? String, "home")
    }

    func testTrackEvent_attributedCopies() {
        let base = TrackEvent(userId: "u", eventName: "e")
        XCTAssertFalse(base.hasKey)
        XCTAssertTrue(base.attributed(experimentKey: "x").hasKey)
        XCTAssertEqual(base.attributed(featureFlagKey: "f").featureFlagKey, "f")
        XCTAssertNil(base.attributed(featureFlagKey: "f").experimentKey)
    }

    func testEvalResult_codableRoundtrip() throws {
        let result = EvalResult(key: "k", enabled: true, config: ["variant": "blue", "n": 1])
        let data = try JSONEncoder().encode(result)
        let decoded = try JSONDecoder().decode(EvalResult.self, from: data)
        XCTAssertEqual(decoded.key, "k")
        XCTAssertTrue(decoded.enabled)
        XCTAssertEqual(decoded.config?["variant"] as? String, "blue")
        XCTAssertEqual(decoded.config?["n"] as? Int, 1)
    }

    func testAssignment_decodesContractShape() throws {
        let assignment = try JSONDecoder().decode(Assignment.self, from: Self.assignJSON.data(using: .utf8)!)
        XCTAssertEqual(assignment.experimentKey, "exp-1")
        XCTAssertEqual(assignment.variantName, "treatment")
        XCTAssertEqual(assignment.isControl, false)
        XCTAssertEqual(assignment.configuration?["limit"] as? Int, 3)
    }

    // MARK: - AnyCodable

    func testAnyCodable_roundtrips() throws {
        let values: [Any] = ["hello", true, 42, 3.14, ["a", "b"], ["x": 1]]
        for value in values {
            let data = try JSONEncoder().encode(AnyCodable(value))
            XCTAssertNoThrow(try JSONDecoder().decode(AnyCodable.self, from: data))
        }
        let decoded = try JSONDecoder().decode(AnyCodable.self, from: try JSONEncoder().encode(AnyCodable(3.14)))
        XCTAssertEqual(decoded.value as? Double ?? 0, 3.14, accuracy: 0.001)
    }

    // MARK: - ExperimentationError

    func testError_descriptions() {
        XCTAssertTrue(ExperimentationError.flagNotFound("my-flag").errorDescription?.contains("my-flag") ?? false)
        XCTAssertTrue(ExperimentationError.experimentNotFound("exp").errorDescription?.contains("exp") ?? false)
        XCTAssertTrue(ExperimentationError.serverError(500, "Internal").errorDescription?.contains("500") ?? false)
        XCTAssertTrue(ExperimentationError.invalidConfig("bad URL").errorDescription?.contains("bad URL") ?? false)
        XCTAssertNotNil(ExperimentationError.cancelled.errorDescription)
    }

    // MARK: - HTTPClient helpers

    func testHTTPClient_encodeComponent() {
        XCTAssertEqual(HTTPClient.encodeComponent("a b/c?d&e=f"), "a%20b%2Fc%3Fd%26e%3Df")
        XCTAssertEqual(HTTPClient.encodeComponent("plain-key_1.~"), "plain-key_1.~")
    }

    func testHTTPClient_buildRequest_handlesTrailingSlashBaseURL() throws {
        let http = HTTPClient(baseURL: "http://localhost:8000/", apiKey: "k")
        let request = try http.buildRequest("/api/v1/tracking/assign", method: "POST")
        XCTAssertEqual(request.url?.absoluteString, "http://localhost:8000/api/v1/tracking/assign")
    }
}

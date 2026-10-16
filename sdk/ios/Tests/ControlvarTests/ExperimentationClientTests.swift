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
        apiKey: String = "test-key"
    ) -> (client: ExperimentationClient, store: OfflineStore) {
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: sessionConfig)

        let store = OfflineStore(keyPrefix: "test_ep_\(UUID().uuidString)_")
        stores.append(store)
        let config = SdkConfig(
            baseURL: "http://localhost:8000",
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
        MockURLProtocol.respond(status: 404, json: #"{"detail":"not active"}"#)
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

        MockURLProtocol.respond(status: 404, json: #"{"detail":"archived"}"#)
        do {
            _ = try await client.evaluateFlag("new-ui", user: user)
            XCTFail("Expected flagNotFound")
        } catch ExperimentationError.flagNotFound {
            // expected: a definitive 404 never serves a stale value
        }
        XCTAssertNil(store.loadEvaluation(flagKey: "new-ui", userId: user.id))
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
        MockURLProtocol.respond(status: 404, json: #"{"detail":"Experiment not active"}"#)
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

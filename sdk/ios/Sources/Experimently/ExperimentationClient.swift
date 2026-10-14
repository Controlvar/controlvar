import Foundation

// MARK: - Protocol

/// Defines the public interface for the Experimently client.
///
/// Use this protocol for dependency injection and testing.
public protocol ExperimentationClientProtocol {
    /// Evaluates a feature flag for the given user (decided by the server).
    func evaluateFlag(_ flagKey: String, user: User) async throws -> EvalResult
    /// Retrieves the user's (sticky) experiment assignment from the server.
    func getAssignment(_ experimentKey: String, user: User) async throws -> Assignment
    /// Records a tracking event. Implementations must never throw; the `throws` is kept for
    /// source compatibility only.
    func track(_ event: TrackEvent) async throws
    /// Records several tracking events in one batch request. Never throws; returns `false`
    /// when at least one request failed.
    @discardableResult
    func trackBatch(_ events: [TrackEvent]) async -> Bool
    /// Deprecated no-op kept for source compatibility. Flags are evaluated per user by the
    /// server, so there is nothing to refresh.
    @available(*, deprecated, message: "Flags are evaluated by the server per user; refreshFlags() is a no-op.")
    func refreshFlags() async throws
    /// Clears caches and releases resources.
    func close()
}

public extension ExperimentationClientProtocol {
    /// Default: send every event through ``track(_:)``.
    @discardableResult
    func trackBatch(_ events: [TrackEvent]) async -> Bool {
        for event in events {
            try? await track(event)
        }
        return true
    }

    /// Default: no-op.
    func refreshFlags() async throws {}
}

// MARK: - ExperimentationClient

/// The main entry point for the Experimently SDK.
///
/// ## How it works
/// - **The server decides.** `evaluateFlag` calls `GET /api/v1/feature-flags/evaluate/{key}`
///   and `getAssignment` calls `POST /api/v1/tracking/assign`; no bucketing happens on device.
/// - **Caching.** Successful results are cached in memory per user + key for `cacheTTL`
///   seconds (NSCache) and, when `enableOfflineFallback` is on, persisted to `UserDefaults`.
///   Failures are never cached.
/// - **Failure.** On a network or server error the cached value is returned when present;
///   otherwise the call throws (`flagNotFound` / `experimentNotFound` for HTTP 404, which never
///   falls back to a stale persisted value).
/// - **Tracking.** `track` never throws. An event carrying `experimentKey`/`featureFlagKey`
///   goes to `POST /api/v1/tracking/track`; an event without keys is fanned out through
///   `POST /api/v1/tracking/batch` to every cached assignment and evaluated flag of that user
///   (nothing cached → nothing sent).
///
/// ## Thread Safety
/// The client is safe to use from multiple threads and Swift concurrency tasks. Caches are
/// lock-protected and concurrent calls for the same user + key share one in-flight request.
public class ExperimentationClient: ExperimentationClientProtocol {

    /// Maximum events per `POST /api/v1/tracking/batch` request.
    public static let batchLimit = 100

    // MARK: - Properties

    private let config: SdkConfig
    internal var http: HTTPClient
    private let flagCache: UserKeyCache<EvalResult>
    private let assignmentCache: UserKeyCache<Assignment>
    private let offlineStore: OfflineStore

    private var inflightFlags: [String: Task<EvalResult, Error>] = [:]
    private var inflightAssignments: [String: Task<Assignment, Error>] = [:]
    private let inflightLock = NSLock()

    // MARK: - Initializers

    /// Initializes the client with a full configuration object.
    ///
    /// - Parameters:
    ///   - config: The `SdkConfig` specifying base URL, API key, cache settings, etc.
    ///   - session: URLSession used for all requests. Inject a custom session for testing.
    ///   - offlineStore: Persistent store for offline fallback. Defaults to `UserDefaults.standard`
    ///     under the `ep_sdk_` prefix.
    public init(config: SdkConfig, session: URLSession = .shared, offlineStore: OfflineStore? = nil) {
        self.config = config
        self.http = HTTPClient(
            baseURL: config.baseURL,
            apiKey: config.apiKey,
            timeout: config.timeout,
            session: session
        )
        self.flagCache = UserKeyCache(ttl: config.cacheTTL, countLimit: config.cacheSize)
        self.assignmentCache = UserKeyCache(ttl: config.cacheTTL, countLimit: config.cacheSize)
        self.offlineStore = offlineStore ?? OfflineStore()
    }

    /// Convenience initializer for the most common use case.
    ///
    /// - Parameters:
    ///   - baseURL: The API origin (e.g., `"http://localhost:8000"`).
    ///   - apiKey: The API key for authentication.
    public convenience init(baseURL: String, apiKey: String) {
        self.init(config: SdkConfig(baseURL: baseURL, apiKey: apiKey))
    }

    // MARK: - Feature flags

    /// Evaluates a feature flag for the given user via
    /// `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id={user.id}`.
    ///
    /// - Returns: The server's decision (`enabled`) plus the flag's `config`.
    /// - Throws: `ExperimentationError.flagNotFound` when the flag key is unknown;
    ///   `.networkError` / `.serverError` / `.decodingError` when the request failed and no
    ///   cached value exists.
    public func evaluateFlag(_ flagKey: String, user: User) async throws -> EvalResult {
        if let cached = flagCache.get(userId: user.id, key: flagKey) {
            return cached
        }

        return try await dedupeFlag(key: "\(user.id)\u{0}\(flagKey)") { [self] in
            do {
                let path = "/api/v1/feature-flags/evaluate/\(HTTPClient.encodeComponent(flagKey))"
                let response = try await http.get(path, query: ["user_id": user.id], as: EvalResult.self)
                let result = EvalResult(
                    key: response.key.isEmpty ? flagKey : response.key,
                    enabled: response.enabled,
                    config: response.config
                )
                flagCache.set(userId: user.id, key: flagKey, value: result)
                if config.enableOfflineFallback {
                    offlineStore.saveEvaluation(result, userId: user.id)
                }
                return result
            } catch ExperimentationError.serverError(let code, _) where code == 404 {
                if config.enableOfflineFallback {
                    offlineStore.removeEvaluation(flagKey: flagKey, userId: user.id)
                }
                throw ExperimentationError.flagNotFound(flagKey)
            } catch {
                if config.enableOfflineFallback,
                   let stored = offlineStore.loadEvaluation(flagKey: flagKey, userId: user.id) {
                    return stored
                }
                throw error
            }
        }
    }

    // MARK: - Experiments

    /// Retrieves the user's experiment assignment via `POST /api/v1/tracking/assign`.
    /// The server makes the assignment sticky and records the exposure; `user.attributes`
    /// are sent as `context` for targeting.
    ///
    /// - Throws: `ExperimentationError.experimentNotFound` when the experiment is unknown or not
    ///   ACTIVE; `.networkError` / `.serverError` / `.decodingError` when the request failed and
    ///   no cached value exists.
    public func getAssignment(_ experimentKey: String, user: User) async throws -> Assignment {
        if let cached = assignmentCache.get(userId: user.id, key: experimentKey) {
            return cached
        }

        return try await dedupeAssignment(key: "\(user.id)\u{0}\(experimentKey)") { [self] in
            do {
                let body = AssignRequest(experimentKey: experimentKey, userId: user.id, context: user.attributes)
                let response = try await http.post("/api/v1/tracking/assign", body: body, as: Assignment.self)
                let assignment = Assignment(
                    experimentKey: response.experimentKey.isEmpty ? experimentKey : response.experimentKey,
                    userId: response.userId.isEmpty ? user.id : response.userId,
                    variantId: response.variantId,
                    variantName: response.variantName,
                    isControl: response.isControl,
                    configuration: response.configuration
                )
                assignmentCache.set(userId: user.id, key: experimentKey, value: assignment)
                if config.enableOfflineFallback {
                    offlineStore.saveAssignment(assignment, userId: user.id)
                }
                return assignment
            } catch ExperimentationError.serverError(let code, _) where code == 404 {
                if config.enableOfflineFallback {
                    offlineStore.removeAssignment(experimentKey: experimentKey, userId: user.id)
                }
                throw ExperimentationError.experimentNotFound(experimentKey)
            } catch {
                if config.enableOfflineFallback,
                   let stored = offlineStore.loadAssignment(experimentKey: experimentKey, userId: user.id) {
                    return stored
                }
                throw error
            }
        }
    }

    // MARK: - Tracking

    /// Records an analytics or conversion event. **Never throws** — the `throws` in the
    /// signature is kept for source compatibility; use ``trackWithStatus(_:)`` to learn whether
    /// delivery succeeded.
    ///
    /// - With `experimentKey` / `featureFlagKey`: one `POST /api/v1/tracking/track`.
    /// - Without keys: one `POST /api/v1/tracking/batch` containing one entry per cached
    ///   assignment and one per cached evaluated flag of `event.userId`. Nothing cached → nothing
    ///   is sent.
    public func track(_ event: TrackEvent) async throws {
        _ = await trackWithStatus(event)
    }

    /// Same as ``track(_:)`` but reports the outcome: `true` when every request succeeded
    /// (or there was nothing to send), `false` on any network / server failure.
    @discardableResult
    public func trackWithStatus(_ event: TrackEvent) async -> Bool {
        if event.hasKey {
            do {
                try await http.post("/api/v1/tracking/track", body: event)
                return true
            } catch {
                return false
            }
        }
        return await sendBatches(fanOut(event))
    }

    /// Records several events with `POST /api/v1/tracking/batch` (chunked by
    /// ``batchLimit``). Keyed events are sent as-is; events without keys are fanned out to the
    /// user's cached assignments and flags. Never throws.
    ///
    /// - Returns: `true` when every batch request succeeded (or nothing had to be sent).
    @discardableResult
    public func trackBatch(_ events: [TrackEvent]) async -> Bool {
        let expanded = events.flatMap { $0.hasKey ? [$0] : fanOut($0) }
        return await sendBatches(expanded)
    }

    /// Deprecated no-op. Flags are evaluated per user by the server; there is no flag list to
    /// download. Kept so existing call sites compile.
    @available(*, deprecated, message: "Flags are evaluated by the server per user; refreshFlags() is a no-op.")
    public func refreshFlags() async throws {}

    // MARK: - Cache access

    /// Cached (successful, unexpired) assignments for the user, in the order they were made.
    public func getAssignments(for userId: String) -> [Assignment] {
        assignmentCache.entries(userId: userId).map { $0.value }
    }

    /// Keys of flags successfully evaluated (and still cached) for the user.
    public func getEvaluatedFlags(for userId: String) -> [String] {
        flagCache.entries(userId: userId).map { $0.key }
    }

    /// Drops every in-memory cached evaluation and assignment (the offline store is kept).
    public func clearCache() {
        flagCache.removeAll()
        assignmentCache.removeAll()
    }

    /// Removes everything the SDK persisted to `UserDefaults`.
    public func clearOfflineCache() {
        offlineStore.clearAll()
    }

    /// Clears all in-memory caches and releases resources.
    ///
    /// Call this when the SDK is no longer needed (e.g., on app termination or sign-out).
    /// `OfflineStore` data is not cleared so it remains available on the next launch.
    public func close() {
        clearCache()
        inflightLock.lock()
        inflightFlags.values.forEach { $0.cancel() }
        inflightAssignments.values.forEach { $0.cancel() }
        inflightFlags.removeAll()
        inflightAssignments.removeAll()
        inflightLock.unlock()
    }

    // MARK: - Private helpers

    private struct AssignRequest: Encodable {
        let experimentKey: String
        let userId: String
        let context: [String: AnyCodable]?

        enum CodingKeys: String, CodingKey {
            case experimentKey = "experiment_key"
            case userId = "user_id"
            case context
        }
    }

    private struct BatchRequest: Encodable {
        let events: [TrackEvent]
    }

    /// One entry per cached assignment (`experiment_key`) plus one per cached flag
    /// (`feature_flag_key`) of the event's user.
    private func fanOut(_ event: TrackEvent) -> [TrackEvent] {
        let assignments = getAssignments(for: event.userId).map { event.attributed(experimentKey: $0.experimentKey) }
        let flags = getEvaluatedFlags(for: event.userId).map { event.attributed(featureFlagKey: $0) }
        return assignments + flags
    }

    private func sendBatches(_ events: [TrackEvent]) async -> Bool {
        guard !events.isEmpty else { return true }
        var ok = true
        var index = 0
        while index < events.count {
            let chunk = Array(events[index..<min(index + Self.batchLimit, events.count)])
            do {
                try await http.post("/api/v1/tracking/batch", body: BatchRequest(events: chunk))
            } catch {
                ok = false
            }
            index += Self.batchLimit
        }
        return ok
    }

    /// Share a single in-flight request between concurrent calls for the same user + flag.
    private func dedupeFlag(key: String, _ run: @escaping () async throws -> EvalResult) async throws -> EvalResult {
        let (task, owner) = registerFlagTask(key: key, run)
        defer { if owner { unregisterFlagTask(key: key) } }
        return try await task.value
    }

    /// Share a single in-flight request between concurrent calls for the same user + experiment.
    private func dedupeAssignment(key: String, _ run: @escaping () async throws -> Assignment) async throws -> Assignment {
        let (task, owner) = registerAssignmentTask(key: key, run)
        defer { if owner { unregisterAssignmentTask(key: key) } }
        return try await task.value
    }

    /// Returns the pending task for `key`, or starts one. `owner` is `true` for the caller that
    /// started it (and must unregister it when done). Synchronous so the lock is never held
    /// across a suspension point.
    private func registerFlagTask(
        key: String,
        _ run: @escaping () async throws -> EvalResult
    ) -> (task: Task<EvalResult, Error>, owner: Bool) {
        inflightLock.lock()
        defer { inflightLock.unlock() }
        if let pending = inflightFlags[key] {
            return (pending, false)
        }
        let task = Task { try await run() }
        inflightFlags[key] = task
        return (task, true)
    }

    private func unregisterFlagTask(key: String) {
        inflightLock.lock()
        inflightFlags[key] = nil
        inflightLock.unlock()
    }

    private func registerAssignmentTask(
        key: String,
        _ run: @escaping () async throws -> Assignment
    ) -> (task: Task<Assignment, Error>, owner: Bool) {
        inflightLock.lock()
        defer { inflightLock.unlock() }
        if let pending = inflightAssignments[key] {
            return (pending, false)
        }
        let task = Task { try await run() }
        inflightAssignments[key] = task
        return (task, true)
    }

    private func unregisterAssignmentTask(key: String) {
        inflightLock.lock()
        inflightAssignments[key] = nil
        inflightLock.unlock()
    }
}

import Foundation

/// UserDefaults-based persistent storage for offline fallback.
///
/// The client writes every **successful** server result (flag evaluation or experiment
/// assignment) here, keyed by user + key. When a later request fails because the network is
/// unreachable (or the server errors), the last known value is served instead.
///
/// Entries have no TTL of their own: they stay until overwritten, removed, or ``clearAll()``.
/// Keys are namespaced with a configurable prefix to avoid collisions with application data.
public final class OfflineStore {
    private let defaults: UserDefaults
    private let keyPrefix: String
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let lock = NSLock()

    private var usersIndexKey: String { keyPrefix + "_users" }

    /// - Parameters:
    ///   - suiteName: An App Group suite name for sharing data between app and extensions.
    ///                Pass `nil` to use `UserDefaults.standard`.
    ///   - keyPrefix: Prefix prepended to all stored keys. Default is `"ep_sdk_"`.
    public init(suiteName: String? = nil, keyPrefix: String = "ep_sdk_") {
        self.defaults = suiteName.flatMap { UserDefaults(suiteName: $0) } ?? .standard
        self.keyPrefix = keyPrefix
    }

    // MARK: - Flag evaluations

    /// Persists a flag evaluation for `userId` (keyed by `result.key`).
    public func saveEvaluation(_ result: EvalResult, userId: String) {
        save(result, bucket: flagsKey(userId), userId: userId, key: result.key)
    }

    /// Loads the last persisted evaluation of `flagKey` for `userId`, or `nil`.
    public func loadEvaluation(flagKey: String, userId: String) -> EvalResult? {
        load(EvalResult.self, bucket: flagsKey(userId), key: flagKey)
    }

    /// Removes the persisted evaluation of `flagKey` for `userId`.
    public func removeEvaluation(flagKey: String, userId: String) {
        remove(bucket: flagsKey(userId), key: flagKey)
    }

    // MARK: - Assignments

    /// Persists an experiment assignment for `userId` (keyed by `assignment.experimentKey`).
    public func saveAssignment(_ assignment: Assignment, userId: String) {
        save(assignment, bucket: assignmentsKey(userId), userId: userId, key: assignment.experimentKey)
    }

    /// Loads the last persisted assignment of `experimentKey` for `userId`, or `nil`.
    public func loadAssignment(experimentKey: String, userId: String) -> Assignment? {
        load(Assignment.self, bucket: assignmentsKey(userId), key: experimentKey)
    }

    /// Removes the persisted assignment of `experimentKey` for `userId`.
    public func removeAssignment(experimentKey: String, userId: String) {
        remove(bucket: assignmentsKey(userId), key: experimentKey)
    }

    // MARK: - Bulk

    /// Removes all SDK data written by this store (every user, flags and assignments).
    public func clearAll() {
        lock.lock()
        defer { lock.unlock() }
        let users = (defaults.array(forKey: usersIndexKey) as? [String]) ?? []
        for userId in users {
            defaults.removeObject(forKey: flagsKey(userId))
            defaults.removeObject(forKey: assignmentsKey(userId))
        }
        defaults.removeObject(forKey: usersIndexKey)
    }

    // MARK: - Private Helpers

    private func flagsKey(_ userId: String) -> String { keyPrefix + "flags." + userId }
    private func assignmentsKey(_ userId: String) -> String { keyPrefix + "assignments." + userId }

    private func save<T: Encodable>(_ value: T, bucket: String, userId: String, key: String) {
        guard let data = try? encoder.encode(value) else { return }
        lock.lock()
        defer { lock.unlock() }
        var entries = (defaults.dictionary(forKey: bucket) as? [String: Data]) ?? [:]
        entries[key] = data
        defaults.set(entries, forKey: bucket)

        var users = (defaults.array(forKey: usersIndexKey) as? [String]) ?? []
        if !users.contains(userId) {
            users.append(userId)
            defaults.set(users, forKey: usersIndexKey)
        }
    }

    private func load<T: Decodable>(_ type: T.Type, bucket: String, key: String) -> T? {
        lock.lock()
        defer { lock.unlock() }
        guard let entries = defaults.dictionary(forKey: bucket) as? [String: Data],
              let data = entries[key] else { return nil }
        return try? decoder.decode(type, from: data)
    }

    private func remove(bucket: String, key: String) {
        lock.lock()
        defer { lock.unlock() }
        guard var entries = defaults.dictionary(forKey: bucket) as? [String: Data] else { return }
        entries.removeValue(forKey: key)
        if entries.isEmpty {
            defaults.removeObject(forKey: bucket)
        } else {
            defaults.set(entries, forKey: bucket)
        }
    }
}

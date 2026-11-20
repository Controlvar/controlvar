import Foundation

// MARK: - CacheBox

/// A reference-type wrapper that stores a value alongside its expiry date.
/// Required because NSCache only accepts AnyObject as its value type.
private final class CacheBox<T> {
    let value: T
    let expiresAt: Date
    /// What the value was answered for: in ``UserKeyCache``, the exact `context` a flag
    /// evaluation sent (`""` for none). ``Cache`` leaves it empty.
    let context: String

    init(_ value: T, expiresAt: Date, context: String = "") {
        self.value = value
        self.expiresAt = expiresAt
        self.context = context
    }

    var isExpired: Bool { Date() > expiresAt }
}

// MARK: - Cache

/// A thread-safe, size-bounded, TTL-aware cache backed by NSCache.
///
/// Key must be an `AnyObject` subclass (e.g., `NSString`).
/// Value can be any Swift type; it is wrapped internally in a `CacheBox`.
public final class Cache<Key: AnyObject, Value: AnyObject> {
    private let nsCache: NSCache<Key, CacheBox<Value>>
    private let ttl: TimeInterval
    private let lock = NSLock()

    /// - Parameters:
    ///   - maxSize: Maximum number of entries. NSCache may evict earlier under memory pressure.
    ///   - ttl: Time-to-live for each entry in seconds.
    public init(maxSize: Int, ttl: TimeInterval) {
        self.ttl = ttl
        self.nsCache = NSCache<Key, CacheBox<Value>>()
        self.nsCache.countLimit = maxSize
    }

    /// Returns the cached value for `key`, or `nil` if it is absent or expired.
    public func get(_ key: Key) -> Value? {
        lock.lock()
        defer { lock.unlock() }
        guard let box = nsCache.object(forKey: key) else { return nil }
        if box.isExpired {
            nsCache.removeObject(forKey: key)
            return nil
        }
        return box.value
    }

    /// Stores `value` under `key` with a TTL expiry.
    public func set(_ key: Key, value: Value) {
        lock.lock()
        defer { lock.unlock() }
        let box = CacheBox(value, expiresAt: Date().addingTimeInterval(ttl))
        nsCache.setObject(box, forKey: key)
    }

    /// Removes the entry for `key`.
    public func remove(_ key: Key) {
        lock.lock()
        defer { lock.unlock() }
        nsCache.removeObject(forKey: key)
    }

    /// Removes all cached entries.
    public func removeAll() {
        lock.lock()
        defer { lock.unlock() }
        nsCache.removeAllObjects()
    }
}

// MARK: - UserKeyCache

/// A thread-safe, TTL-aware cache of server results keyed by **user + key**, backed by NSCache.
///
/// Used by ``ExperimentationClient`` for flag evaluations and experiment assignments. Only
/// successful results are ever stored. Unlike a bare NSCache it can enumerate the live entries
/// of one user (needed for the track fan-out), via a small per-user key index.
///
/// Each user + key has one entry. It can remember a `context`, the attributes it was answered
/// for: a flag evaluation stores the exact `context` it sent and reads only an entry stored
/// with the same one, so a call with other attributes asks the server, and its answer replaces
/// the entry. The listing (``entries(userId:)``) is unchanged: still one entry per key.
public final class UserKeyCache<Value> {
    private let store = NSCache<NSString, CacheBox<Value>>()
    /// userId → keys in insertion order. Entries evicted by NSCache are dropped lazily.
    private var index: [String: [String]] = [:]
    private let lock = NSLock()
    private let ttl: TimeInterval

    /// - Parameters:
    ///   - ttl: Time-to-live for each entry in seconds.
    ///   - countLimit: Advisory maximum number of entries (NSCache `countLimit`).
    public init(ttl: TimeInterval, countLimit: Int = 1000) {
        self.ttl = ttl
        store.countLimit = countLimit
    }

    /// Returns the cached value for the user + key, or `nil` if absent or expired.
    ///
    /// - Parameter context: when given, the entry must also have been stored with exactly this
    ///   `context`; otherwise the result is `nil` and the entry is kept (a new answer replaces
    ///   it). `nil` means no check, which is what assignments use.
    public func get(userId: String, key: String, context: String? = nil) -> Value? {
        lock.lock()
        defer { lock.unlock() }
        let storageKey = Self.storageKey(userId, key)
        guard let box = store.object(forKey: storageKey) else {
            dropFromIndex(userId, key)
            return nil
        }
        if box.isExpired {
            store.removeObject(forKey: storageKey)
            dropFromIndex(userId, key)
            return nil
        }
        if let context = context, box.context != context {
            return nil
        }
        return box.value
    }

    /// Stores `value` for the user + key with a TTL expiry, replacing any entry, together with
    /// the `context` it was answered for (`""` for none).
    public func set(userId: String, key: String, value: Value, context: String = "") {
        lock.lock()
        defer { lock.unlock() }
        let box = CacheBox(value, expiresAt: Date().addingTimeInterval(ttl), context: context)
        store.setObject(box, forKey: Self.storageKey(userId, key))
        var keys = index[userId] ?? []
        if !keys.contains(key) {
            keys.append(key)
        }
        index[userId] = keys
    }

    /// All live (non-expired) entries for a user, in insertion order.
    public func entries(userId: String) -> [(key: String, value: Value)] {
        lock.lock()
        defer { lock.unlock() }
        var live: [(key: String, value: Value)] = []
        var liveKeys: [String] = []
        for key in index[userId] ?? [] {
            let storageKey = Self.storageKey(userId, key)
            if let box = store.object(forKey: storageKey), !box.isExpired {
                live.append((key: key, value: box.value))
                liveKeys.append(key)
            } else {
                store.removeObject(forKey: storageKey)
            }
        }
        index[userId] = liveKeys.isEmpty ? nil : liveKeys
        return live
    }

    /// Removes the entry for the user + key.
    public func remove(userId: String, key: String) {
        lock.lock()
        defer { lock.unlock() }
        store.removeObject(forKey: Self.storageKey(userId, key))
        dropFromIndex(userId, key)
    }

    /// Removes all cached entries.
    public func removeAll() {
        lock.lock()
        defer { lock.unlock() }
        store.removeAllObjects()
        index.removeAll()
    }

    /// Number of live entries across all users.
    public var count: Int {
        lock.lock()
        let users = Array(index.keys)
        lock.unlock()
        return users.reduce(0) { $0 + entries(userId: $1).count }
    }

    // MARK: Private

    /// Length-prefixed so a user ID containing the separator cannot collide with another pair.
    private static func storageKey(_ userId: String, _ key: String) -> NSString {
        "\(userId.utf8.count):\(userId):\(key)" as NSString
    }

    private func dropFromIndex(_ userId: String, _ key: String) {
        guard var keys = index[userId] else { return }
        keys.removeAll { $0 == key }
        index[userId] = keys.isEmpty ? nil : keys
    }
}

/// In-memory cache of server results keyed by user + key, with TTL.
library controlvar_cache;

/// A single entry in the cache.
class _CacheEntry<T> {
  final T value;
  final DateTime expiresAt;

  /// The attributes the answer was given for, as the JSON text that was sent
  /// (`''` when none were sent).
  final String context;

  _CacheEntry(this.value, this.expiresAt, this.context);

  bool get isExpired => DateTime.now().isAfter(expiresAt);
}

/// Single-isolate in-memory cache with per-entry TTL, keyed by **user + key**.
///
/// Only successful server results are stored by the client. Expired entries
/// are evicted lazily on the next access. Entries of one user can be listed in
/// insertion order (used for the track fan-out).
class EvaluationCache<T> {
  final Duration ttl;
  final Map<String, Map<String, _CacheEntry<T>>> _users = {};

  EvaluationCache({required this.ttl});

  /// Returns the cached value for [userId] + [key], or `null` if absent or expired.
  ///
  /// With a [context] (the attributes' JSON text, `''` for none) the entry is
  /// a hit only if it was stored for exactly that context; without one there
  /// is no such check, which is what assignments and the listings use.
  T? get(String userId, String key, {String? context}) {
    final byKey = _users[userId];
    final entry = byKey?[key];
    if (entry == null) return null;
    if (entry.isExpired) {
      byKey!.remove(key);
      return null;
    }
    if (context != null && entry.context != context) return null;
    return entry.value;
  }

  /// Stores [value] for [userId] + [key] with the configured [ttl]. There is
  /// one entry per user + key: it replaces whatever [context] the old one had.
  void set(String userId, String key, T value, {String context = ''}) {
    final byKey = _users.putIfAbsent(userId, () => {});
    byKey[key] = _CacheEntry(value, DateTime.now().add(ttl), context);
  }

  /// All live (non-expired) entries for [userId], in insertion order.
  List<MapEntry<String, T>> entries(String userId) {
    final byKey = _users[userId];
    if (byKey == null) return <MapEntry<String, T>>[];
    byKey.removeWhere((_, entry) => entry.isExpired);
    return byKey.entries.map((e) => MapEntry(e.key, e.value.value)).toList();
  }

  /// Removes the entry for [userId] + [key] if present.
  void invalidate(String userId, String key) {
    _users[userId]?.remove(key);
  }

  /// Removes all entries.
  void clear() {
    _users.clear();
  }

  /// Number of non-expired entries across all users.
  int get size {
    var total = 0;
    for (final byKey in _users.values) {
      byKey.removeWhere((_, entry) => entry.isExpired);
      total += byKey.length;
    }
    return total;
  }

  /// Whether a non-expired entry exists for [userId] + [key].
  bool containsKey(String userId, String key) => get(userId, key) != null;
}

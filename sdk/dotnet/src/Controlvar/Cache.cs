namespace Controlvar;

/// <summary>
/// Thread-safe in-memory cache with TTL expiration and LRU-style eviction.
/// </summary>
/// <typeparam name="TValue">The type of cached values.</typeparam>
public class SdkCache<TValue>
{
    private readonly int _maxSize;
    private readonly TimeSpan _defaultTtl;

    // Dictionary<K,V> does NOT guarantee enumeration order: after a removal the
    // freed slot is reused by the next insertion, so "the first key enumerated"
    // is not the oldest and eviction would drop an arbitrary entry. Insertion
    // order is tracked explicitly instead.
    private readonly Dictionary<string, CacheEntry> _store;
    private readonly LinkedList<string> _insertionOrder = new();
    private readonly Dictionary<string, LinkedListNode<string>> _orderNodes = new();
    private readonly object _lock = new();

    private struct CacheEntry
    {
        public TValue Value;
        public DateTime ExpiresAt;
    }

    /// <summary>
    /// Creates a new cache.
    /// </summary>
    /// <param name="maxSize">Maximum number of entries before oldest is evicted.</param>
    /// <param name="defaultTtl">Default time-to-live for entries.</param>
    public SdkCache(int maxSize = 1000, TimeSpan? defaultTtl = null)
    {
        _maxSize = maxSize;
        _defaultTtl = defaultTtl ?? TimeSpan.FromSeconds(300);
        _store = new Dictionary<string, CacheEntry>(_maxSize);
    }

    /// <summary>Gets a cached value, or default if not found or expired.</summary>
    /// <remarks>
    /// For a value type this cannot distinguish "missing" from a cached default
    /// (<c>SdkCache&lt;int&gt;.Get</c> returns 0 either way). Use
    /// <see cref="TryGet"/> when that difference matters.
    /// </remarks>
    public TValue? Get(string key)
    {
        return TryGet(key, out var value) ? value : default;
    }

    /// <summary>Gets a cached value; false when the key is absent or expired.</summary>
    public bool TryGet(string key, out TValue value)
    {
        lock (_lock)
        {
            if (!_store.TryGetValue(key, out var entry))
            {
                value = default!;
                return false;
            }

            if (DateTime.UtcNow >= entry.ExpiresAt)
            {
                RemoveLocked(key);
                value = default!;
                return false;
            }

            value = entry.Value;
            return true;
        }
    }

    /// <summary>Removes a key and its order entry. Caller holds the lock.</summary>
    private void RemoveLocked(string key)
    {
        _store.Remove(key);
        if (_orderNodes.TryGetValue(key, out var node))
        {
            _insertionOrder.Remove(node);
            _orderNodes.Remove(key);
        }
    }

    /// <summary>Sets a value in the cache.</summary>
    /// <param name="key">The cache key.</param>
    /// <param name="value">The value to store.</param>
    /// <param name="ttl">Override TTL. Uses the cache's default TTL if null.</param>
    public void Set(string key, TValue value, TimeSpan? ttl = null)
    {
        lock (_lock)
        {
            // If key already exists, remove it first to refresh insertion order.
            RemoveLocked(key);

            // Evict the oldest entry if at capacity.
            while (_store.Count >= _maxSize && _insertionOrder.First is { } oldest)
            {
                RemoveLocked(oldest.Value);
            }

            _store[key] = new CacheEntry
            {
                Value = value,
                ExpiresAt = DateTime.UtcNow + (ttl ?? _defaultTtl)
            };
            _orderNodes[key] = _insertionOrder.AddLast(key);
        }
    }

    /// <summary>Removes a specific key from the cache.</summary>
    public void Delete(string key)
    {
        lock (_lock)
        {
            RemoveLocked(key);
        }
    }

    /// <summary>Removes all entries from the cache.</summary>
    public void Clear()
    {
        lock (_lock)
        {
            _store.Clear();
            _insertionOrder.Clear();
            _orderNodes.Clear();
        }
    }

    /// <summary>Returns the current number of entries in the cache (including expired ones not yet purged).</summary>
    public int Count
    {
        get
        {
            lock (_lock)
            {
                return _store.Count;
            }
        }
    }

    /// <summary>
    /// Returns the number of non-expired entries in the cache.
    /// Note: This iterates through all entries and is O(n).
    /// </summary>
    public int ActiveCount
    {
        get
        {
            lock (_lock)
            {
                var now = DateTime.UtcNow;
                int count = 0;
                foreach (var entry in _store.Values)
                {
                    if (now < entry.ExpiresAt)
                        count++;
                }
                return count;
            }
        }
    }
}

/// <summary>
/// Thread-safe, TTL-aware cache of server results keyed by <b>user + key</b>, built on
/// <see cref="SdkCache{TValue}"/>. Used by <see cref="ExperimentationClient"/> for flag
/// evaluations and experiment assignments; only successful results are ever stored. Unlike a bare
/// <see cref="SdkCache{TValue}"/> it can enumerate the live entries of one user (needed for the
/// track fan-out) via a small per-user key index.
/// </summary>
/// <typeparam name="TValue">The cached value type.</typeparam>
public class UserKeyCache<TValue> where TValue : class
{
    private readonly SdkCache<TValue> _store;
    // userId → keys in insertion order. Entries evicted or expired in _store are dropped lazily.
    private readonly Dictionary<string, List<string>> _index = new();
    private readonly object _lock = new();

    /// <summary>Creates a new cache.</summary>
    /// <param name="maxSize">Maximum number of entries across all users before the oldest is evicted.</param>
    /// <param name="ttl">Time-to-live of each entry.</param>
    public UserKeyCache(int maxSize, TimeSpan ttl)
    {
        _store = new SdkCache<TValue>(maxSize, ttl);
    }

    /// <summary>Returns the cached value for the user + key, or <c>null</c> if absent or expired.</summary>
    public TValue? Get(string userId, string key)
    {
        lock (_lock)
        {
            var value = _store.Get(StorageKey(userId, key));
            if (value == null)
                DropFromIndex(userId, key);
            return value;
        }
    }

    /// <summary>Stores <paramref name="value"/> for the user + key with the cache TTL.</summary>
    public void Set(string userId, string key, TValue value)
    {
        lock (_lock)
        {
            _store.Set(StorageKey(userId, key), value);
            if (!_index.TryGetValue(userId, out var keys))
            {
                keys = new List<string>();
                _index[userId] = keys;
            }
            if (!keys.Contains(key))
                keys.Add(key);
        }
    }

    /// <summary>All live (non-expired) entries of a user, in insertion order.</summary>
    public IReadOnlyList<KeyValuePair<string, TValue>> Entries(string userId)
    {
        lock (_lock)
        {
            var live = new List<KeyValuePair<string, TValue>>();
            if (!_index.TryGetValue(userId, out var keys))
                return live;

            var liveKeys = new List<string>();
            foreach (var key in keys)
            {
                var value = _store.Get(StorageKey(userId, key));
                if (value != null)
                {
                    live.Add(new KeyValuePair<string, TValue>(key, value));
                    liveKeys.Add(key);
                }
            }

            if (liveKeys.Count == 0)
                _index.Remove(userId);
            else
                _index[userId] = liveKeys;
            return live;
        }
    }

    /// <summary>Removes the entry for the user + key.</summary>
    public void Remove(string userId, string key)
    {
        lock (_lock)
        {
            _store.Delete(StorageKey(userId, key));
            DropFromIndex(userId, key);
        }
    }

    /// <summary>Removes all entries.</summary>
    public void Clear()
    {
        lock (_lock)
        {
            _store.Clear();
            _index.Clear();
        }
    }

    /// <summary>Number of live entries across all users.</summary>
    public int Count
    {
        get
        {
            List<string> users;
            lock (_lock)
            {
                users = new List<string>(_index.Keys);
            }
            int total = 0;
            foreach (var userId in users)
                total += Entries(userId).Count;
            return total;
        }
    }

    // Length-prefixed so a user ID containing the separator cannot collide with another pair.
    private static string StorageKey(string userId, string key) => $"{userId.Length}:{userId}:{key}";

    private void DropFromIndex(string userId, string key)
    {
        if (!_index.TryGetValue(userId, out var keys))
            return;
        keys.Remove(key);
        if (keys.Count == 0)
            _index.Remove(userId);
    }
}

package com.controlvar.android

import java.util.concurrent.locks.ReentrantReadWriteLock
import kotlin.concurrent.read
import kotlin.concurrent.write

/**
 * Cache entry wrapping a value with an expiry timestamp.
 */
data class CacheEntry<T>(
    val value: T,
    val expiresAt: Long  // System.currentTimeMillis() epoch ms
) {
    val isExpired: Boolean get() = System.currentTimeMillis() > expiresAt
}

/**
 * Thread-safe LRU cache with TTL support for server results (flag evaluations and
 * experiment assignments), keyed per user + key (see [ResultCache.key]).
 *
 * Uses a LinkedHashMap in access-order mode for LRU eviction.
 * Safe for concurrent use via ReentrantReadWriteLock.
 * Does NOT depend on android.util.LruCache — works in plain JVM test environments.
 */
class ResultCache<T>(
    private val maxSize: Int = 1000,
    private val ttlMs: Long = 300_000L
) {
    private val lock = ReentrantReadWriteLock()
    private val store = object : LinkedHashMap<String, CacheEntry<T>>(
        16, 0.75f, true  // accessOrder = true enables LRU
    ) {
        override fun removeEldestEntry(eldest: Map.Entry<String, CacheEntry<T>>): Boolean {
            return size > maxSize
        }
    }

    /**
     * Retrieves a value from the cache.
     * Returns null if the key is absent or the entry has expired.
     */
    fun get(key: String): T? = lock.write {
        val entry = store[key] ?: return null
        if (entry.isExpired) {
            store.remove(key)
            null
        } else {
            entry.value
        }
    }

    /**
     * Stores a value in the cache with the configured TTL.
     */
    fun set(key: String, value: T) = lock.write {
        store[key] = CacheEntry(value, System.currentTimeMillis() + ttlMs)
    }

    /**
     * Removes a specific key from the cache.
     */
    fun remove(key: String) = lock.write { store.remove(key) }

    /**
     * Removes all entries from the cache.
     */
    fun clear() = lock.write { store.clear() }

    /**
     * Returns the current number of entries (including expired ones not yet evicted).
     */
    fun size(): Int = lock.read { store.size }

    /**
     * Returns only non-expired entries as a snapshot.
     */
    fun entries(): Map<String, T> = lock.read {
        store.filter { !it.value.isExpired }.mapValues { it.value.value }
    }

    /**
     * Returns the live (non-expired) values whose key starts with [prefix],
     * least recently used first. Expired entries encountered are removed.
     */
    fun valuesWithPrefix(prefix: String): List<T> = lock.write {
        val values = mutableListOf<T>()
        val iterator = store.entries.iterator()
        while (iterator.hasNext()) {
            val (k, entry) = iterator.next()
            if (entry.isExpired) {
                iterator.remove()
            } else if (k.startsWith(prefix)) {
                values.add(entry.value)
            }
        }
        values
    }

    companion object {
        /** Separator inside cache keys; a NUL keeps "a"+"b:c" and "a:b"+"c" distinct. */
        const val SEPARATOR: Char = '\u0000'

        /** Prefix shared by all of a user's keys. */
        fun userPrefix(userId: String): String = userId + SEPARATOR

        /** Cache key for one user + flag/experiment key. */
        fun key(userId: String, key: String): String = userPrefix(userId) + key
    }
}

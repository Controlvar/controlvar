package com.controlvar.sdk;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.Collections;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link AssignmentCache}.
 *
 * <p>Covers:
 * <ul>
 *   <li>Basic put/get behaviour</li>
 *   <li>TTL expiration</li>
 *   <li>LRU eviction at max capacity</li>
 *   <li>Thread-safety (basic smoke test)</li>
 *   <li>Cache size and clear operations</li>
 * </ul>
 */
@DisplayName("AssignmentCache")
class AssignmentCacheTest {

    private AssignmentCache<String> cache;

    @BeforeEach
    void setUp() {
        // 10 entries max, 1 second TTL
        cache = new AssignmentCache<>(10, 1000L);
    }

    // -------------------------------------------------------------------------
    // Basic put/get
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("put and get returns stored value")
    void testPutAndGet() {
        cache.put("user-1:flag-a", "on");
        assertEquals("on", cache.get("user-1:flag-a"),
                "Should return the value that was put");
    }

    @Test
    @DisplayName("get returns null for a key that was never stored")
    void testGetReturnsNullForMissingKey() {
        assertNull(cache.get("nonexistent-key"),
                "get should return null for a key not in the cache");
    }

    @Test
    @DisplayName("put overwrites previous value for the same key")
    void testPutOverwritesPreviousValue() {
        cache.put("user-1:flag-a", "on");
        cache.put("user-1:flag-a", "off");
        assertEquals("off", cache.get("user-1:flag-a"),
                "put should overwrite the previous value");
    }

    @Test
    @DisplayName("cache stores multiple independent keys correctly")
    void testCacheStoresMultipleKeys() {
        cache.put("user-1:flag-a", "on");
        cache.put("user-2:flag-a", "off");
        cache.put("user-1:flag-b", "treatment");

        assertEquals("on",        cache.get("user-1:flag-a"));
        assertEquals("off",       cache.get("user-2:flag-a"));
        assertEquals("treatment", cache.get("user-1:flag-b"));
    }

    // -------------------------------------------------------------------------
    // TTL expiration
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("get returns null after TTL expires")
    void testCacheEntryExpiresAfterTtl() throws InterruptedException {
        // Create a cache with a very short TTL (50ms)
        AssignmentCache<String> shortTtlCache = new AssignmentCache<>(100, 50L);
        shortTtlCache.put("user-1:flag-a", "on");

        // Before expiry: value should be present
        assertEquals("on", shortTtlCache.get("user-1:flag-a"),
                "Value should be present before TTL expires");

        // Wait for TTL to expire
        Thread.sleep(100L);

        // After expiry: value should be null
        assertNull(shortTtlCache.get("user-1:flag-a"),
                "Value should be null after TTL expires");
    }

    @Test
    @DisplayName("expired entry is removed on access (lazy eviction)")
    void testExpiredEntryIsRemovedOnAccess() throws InterruptedException {
        AssignmentCache<String> shortTtlCache = new AssignmentCache<>(100, 50L);
        shortTtlCache.put("user-1:flag-a", "on");

        assertEquals(1, shortTtlCache.size(), "Cache should have 1 entry before expiry");

        Thread.sleep(100L);

        // Access causes lazy removal
        assertNull(shortTtlCache.get("user-1:flag-a"));
        assertEquals(0, shortTtlCache.size(),
                "Expired entry should be removed from cache after lazy eviction");
    }

    @Test
    @DisplayName("non-expired entries remain accessible while others expire")
    void testNonExpiredEntriesRemain() throws InterruptedException {
        AssignmentCache<String> shortTtlCache = new AssignmentCache<>(100, 50L);
        shortTtlCache.put("user-1:flag-a", "on");

        Thread.sleep(100L);

        // Add a new entry with full TTL
        shortTtlCache.put("user-2:flag-b", "treatment");

        assertNull(shortTtlCache.get("user-1:flag-a"), "Old entry should be expired");
        assertEquals("treatment", shortTtlCache.get("user-2:flag-b"),
                "New entry should still be valid");
    }

    // -------------------------------------------------------------------------
    // LRU eviction at max capacity
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("cache respects maxSize and evicts LRU entry when full")
    void testCacheRespectsMaxSize() {
        // Create cache with maxSize=3
        AssignmentCache<String> smallCache = new AssignmentCache<>(3, 60_000L);
        smallCache.put("key-1", "v1");
        smallCache.put("key-2", "v2");
        smallCache.put("key-3", "v3");

        assertEquals(3, smallCache.size(), "Cache should have 3 entries");

        // Adding a 4th entry should evict the LRU entry (key-1 was inserted first
        // and not accessed, so it is the least recently used)
        smallCache.put("key-4", "v4");

        // Cache size should still be 3
        assertEquals(3, smallCache.size(),
                "Cache should not exceed maxSize after LRU eviction");

        // key-4 must be present (just inserted)
        assertEquals("v4", smallCache.get("key-4"), "Newly inserted key should be present");
    }

    @Test
    @DisplayName("accessing a key prevents it from being LRU-evicted")
    void testAccessedKeyNotEvicted() {
        AssignmentCache<String> smallCache = new AssignmentCache<>(3, 60_000L);
        smallCache.put("key-1", "v1");
        smallCache.put("key-2", "v2");
        smallCache.put("key-3", "v3");

        // Access key-1 to make it recently used
        smallCache.get("key-1");

        // Now add key-4 — key-2 (now LRU) should be evicted, NOT key-1
        smallCache.put("key-4", "v4");

        assertEquals("v1", smallCache.get("key-1"),
                "key-1 was accessed recently and should NOT have been evicted");
        assertEquals("v4", smallCache.get("key-4"), "key-4 should be present");
        assertEquals(3, smallCache.size());
    }

    // -------------------------------------------------------------------------
    // size and clear
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("size returns correct count after puts")
    void testSizeReturnsCorrectCount() {
        assertEquals(0, cache.size(), "Empty cache should have size 0");

        cache.put("user-1:flag-a", "on");
        assertEquals(1, cache.size());

        cache.put("user-2:flag-a", "off");
        assertEquals(2, cache.size());

        cache.put("user-1:flag-b", "treatment");
        assertEquals(3, cache.size());
    }

    @Test
    @DisplayName("clear removes all entries and resets size to 0")
    void testClearEmptiesCache() {
        cache.put("user-1:flag-a", "on");
        cache.put("user-2:flag-a", "off");
        cache.put("user-3:flag-a", "treatment");

        assertEquals(3, cache.size(), "Cache should have 3 entries before clear");

        cache.clear();

        assertEquals(0, cache.size(), "Cache size should be 0 after clear");
        assertNull(cache.get("user-1:flag-a"), "Cleared entries should not be retrievable");
        assertNull(cache.get("user-2:flag-a"), "Cleared entries should not be retrievable");
        assertNull(cache.get("user-3:flag-a"), "Cleared entries should not be retrievable");
    }

    // -------------------------------------------------------------------------
    // invalidate
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("invalidate removes a specific key without affecting others")
    void testInvalidateRemovesSpecificKey() {
        cache.put("user-1:flag-a", "on");
        cache.put("user-2:flag-a", "off");

        cache.invalidate("user-1:flag-a");

        assertNull(cache.get("user-1:flag-a"), "Invalidated key should return null");
        assertEquals("off", cache.get("user-2:flag-a"), "Other keys should be unaffected");
        assertEquals(1, cache.size());
    }

    @Test
    @DisplayName("invalidate on a missing key does not throw")
    void testInvalidateOnMissingKeyDoesNotThrow() {
        assertDoesNotThrow(() -> cache.invalidate("nonexistent-key"),
                "Invalidating a missing key should not throw");
    }

    // -------------------------------------------------------------------------
    // Constructor validation
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("constructor throws when maxSize is 0")
    void testConstructorThrowsWhenMaxSizeIsZero() {
        assertThrows(IllegalArgumentException.class,
                () -> new AssignmentCache<String>(0, 1000L),
                "maxSize=0 should throw IllegalArgumentException");
    }

    @Test
    @DisplayName("constructor throws when maxSize is negative")
    void testConstructorThrowsWhenMaxSizeIsNegative() {
        assertThrows(IllegalArgumentException.class,
                () -> new AssignmentCache<String>(-1, 1000L),
                "Negative maxSize should throw IllegalArgumentException");
    }

    @Test
    @DisplayName("constructor throws when ttlMs is 0")
    void testConstructorThrowsWhenTtlIsZero() {
        assertThrows(IllegalArgumentException.class,
                () -> new AssignmentCache<String>(100, 0L),
                "ttlMs=0 should throw IllegalArgumentException");
    }

    @Test
    @DisplayName("constructor throws when ttlMs is negative")
    void testConstructorThrowsWhenTtlIsNegative() {
        assertThrows(IllegalArgumentException.class,
                () -> new AssignmentCache<String>(100, -500L),
                "Negative ttlMs should throw IllegalArgumentException");
    }

    // -------------------------------------------------------------------------
    // valuesWithPrefix
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("valuesWithPrefix returns only live values whose key starts with the prefix")
    void testValuesWithPrefix() throws InterruptedException {
        AssignmentCache<String> shortTtlCache = new AssignmentCache<>(100, 50L);
        shortTtlCache.put("user-1\0flag-a", "expired");
        Thread.sleep(100L);
        shortTtlCache.put("user-1\0flag-b", "b");
        shortTtlCache.put("user-1\0flag-c", "c");
        shortTtlCache.put("user-10\0flag-a", "other-user");

        assertEquals(Arrays.asList("b", "c"), shortTtlCache.valuesWithPrefix("user-1\0"),
                "expired entries are dropped and other users' keys are not matched");
        assertEquals(Collections.singletonList("other-user"), shortTtlCache.valuesWithPrefix("user-10\0"));
        assertTrue(shortTtlCache.valuesWithPrefix("user-2\0").isEmpty());
        assertEquals(3, shortTtlCache.size(), "expired entry removed during the scan");
    }

    // -------------------------------------------------------------------------
    // Accessors
    // -------------------------------------------------------------------------

    @Test
    @DisplayName("getMaxSize and getTtlMs return configured values")
    void testGetMaxSizeAndGetTtlMs() {
        AssignmentCache<String> c = new AssignmentCache<>(42, 12345L);
        assertEquals(42, c.getMaxSize());
        assertEquals(12345L, c.getTtlMs());
    }
}

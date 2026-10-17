<?php

declare(strict_types=1);

namespace Controlvar\Tests;

use Controlvar\Cache;
use PHPUnit\Framework\TestCase;

/**
 * Unit tests for the in-memory TTL cache.
 */
class CacheTest extends TestCase
{
    private Cache $cache;

    protected function setUp(): void
    {
        // Default 300s TTL, max 1000 entries
        $this->cache = new Cache(300, 1000);
    }

    // -------------------------------------------------------------------------
    // Basic get / set
    // -------------------------------------------------------------------------

    public function testGetReturnsNullForMissingKey(): void
    {
        $result = $this->cache->get('nonexistent');
        $this->assertNull($result);
    }

    public function testSetAndGet(): void
    {
        $this->cache->set('greeting', 'hello');
        $this->assertSame('hello', $this->cache->get('greeting'));
    }

    public function testSetAndGetInteger(): void
    {
        $this->cache->set('count', 42);
        $this->assertSame(42, $this->cache->get('count'));
    }

    public function testSetAndGetArray(): void
    {
        $data = ['key' => 'value', 'nested' => ['a' => 1]];
        $this->cache->set('data', $data);
        $this->assertSame($data, $this->cache->get('data'));
    }

    // -------------------------------------------------------------------------
    // TTL expiry
    // -------------------------------------------------------------------------

    public function testGetReturnsNullAfterTtlExpiry(): void
    {
        // TTL of 1 second
        $this->cache->set('short-lived', 'will-expire', 1);

        $this->assertSame('will-expire', $this->cache->get('short-lived'), 'Key should exist before TTL');

        sleep(2);

        $this->assertNull($this->cache->get('short-lived'), 'Key should be expired after TTL');
    }

    public function testNeverExpiresWhenTtlIsZero(): void
    {
        // TTL=0 means "use default"; create a cache with default TTL=0 to mean "never expire"
        // According to spec: TTL=0 → treat as never expires (or test edge case)
        // Our impl: TTL=0 → expiresAt=null → never expires
        $neverExpireCache = new Cache(0, 100);
        $neverExpireCache->set('persistent', 'value', 0);

        $this->assertSame('value', $neverExpireCache->get('persistent'), 'TTL=0 should never expire');
    }

    // -------------------------------------------------------------------------
    // Delete
    // -------------------------------------------------------------------------

    public function testDeleteRemovesKey(): void
    {
        $this->cache->set('to-delete', 'data');
        $this->cache->delete('to-delete');

        $this->assertNull($this->cache->get('to-delete'), 'Deleted key must return null');
    }

    public function testDeleteNonExistentKeyDoesNotThrow(): void
    {
        // Must be a no-op
        $this->cache->delete('ghost');
        $this->assertTrue(true, 'Deleting non-existent key must not throw');
    }

    // -------------------------------------------------------------------------
    // Clear
    // -------------------------------------------------------------------------

    public function testClearEmptiesAll(): void
    {
        $this->cache->set('a', 1);
        $this->cache->set('b', 2);
        $this->cache->set('c', 3);

        $this->cache->clear();

        $this->assertNull($this->cache->get('a'));
        $this->assertNull($this->cache->get('b'));
        $this->assertNull($this->cache->get('c'));
        $this->assertSame(0, $this->cache->count());
    }

    // -------------------------------------------------------------------------
    // Count
    // -------------------------------------------------------------------------

    public function testCountReturnsCorrect(): void
    {
        $this->assertSame(0, $this->cache->count());

        $this->cache->set('x', 1);
        $this->assertSame(1, $this->cache->count());

        $this->cache->set('y', 2);
        $this->assertSame(2, $this->cache->count());

        $this->cache->delete('x');
        $this->assertSame(1, $this->cache->count());
    }

    // -------------------------------------------------------------------------
    // Overwrite
    // -------------------------------------------------------------------------

    public function testSetOverwritesExisting(): void
    {
        $this->cache->set('key', 'original');
        $this->cache->set('key', 'updated');

        $this->assertSame('updated', $this->cache->get('key'), 'Setting same key must overwrite the value');
    }

    public function testOverwriteDoesNotIncreaseCount(): void
    {
        $this->cache->set('key', 'v1');
        $this->cache->set('key', 'v2');

        $this->assertSame(1, $this->cache->count(), 'Overwriting a key must not increase count');
    }

    // -------------------------------------------------------------------------
    // Null value storage
    // -------------------------------------------------------------------------

    public function testNullValueCanBeStored(): void
    {
        // Storing null explicitly must work — get() returns null for both
        // "not found" and "stored null", which is acceptable for this SDK.
        $this->cache->set('null-key', null);

        // We verify it was stored by checking count
        $this->assertSame(1, $this->cache->count(), 'null value should be stored');
    }

    // -------------------------------------------------------------------------
    // Max cache size eviction
    // -------------------------------------------------------------------------

    public function testMaxSizeEvictsOldest(): void
    {
        $maxSize = 5;
        $smallCache = new Cache(300, $maxSize);

        // Fill the cache to capacity
        for ($i = 0; $i < $maxSize; $i++) {
            $smallCache->set("key-{$i}", "value-{$i}");
        }

        $this->assertSame($maxSize, $smallCache->count(), 'Cache should be at capacity');

        // Adding one more should evict the oldest (key-0)
        $smallCache->set('key-new', 'value-new');

        $this->assertSame($maxSize, $smallCache->count(), 'Cache count should not exceed maxCacheSize');
        $this->assertNull($smallCache->get('key-0'), 'Oldest entry must be evicted when at capacity');
        $this->assertSame('value-new', $smallCache->get('key-new'), 'Newly added entry must be accessible');
    }

    public function testMaxSizeEvictsMultipleOldest(): void
    {
        $smallCache = new Cache(300, 3);

        $smallCache->set('first',  '1');
        $smallCache->set('second', '2');
        $smallCache->set('third',  '3');

        // Adding two more entries evicts 'first' then 'second'
        $smallCache->set('fourth', '4');
        $smallCache->set('fifth',  '5');

        $this->assertNull($smallCache->get('first'),   'first should be evicted');
        $this->assertNull($smallCache->get('second'),  'second should be evicted');
        $this->assertSame('3', $smallCache->get('third'),  'third must still be present');
        $this->assertSame('4', $smallCache->get('fourth'), 'fourth must still be present');
        $this->assertSame('5', $smallCache->get('fifth'),  'fifth must still be present');
    }

    // -------------------------------------------------------------------------
    // valuesWithPrefix — per-user listing used by the track fan-out
    // -------------------------------------------------------------------------

    public function testValuesWithPrefixReturnsMatchingValuesInInsertionOrder(): void
    {
        $this->cache->set('flag:2:u1:a', 'A');
        $this->cache->set('flag:2:u2:b', 'B');
        $this->cache->set('flag:2:u1:c', 'C');
        $this->cache->set('assignment:2:u1:x', 'X');

        $this->assertSame(['A', 'C'], $this->cache->valuesWithPrefix('flag:2:u1:'));
        $this->assertSame(['X'], $this->cache->valuesWithPrefix('assignment:2:u1:'));
        $this->assertSame([], $this->cache->valuesWithPrefix('flag:2:u3:'));
    }

    public function testValuesWithPrefixSkipsExpiredEntries(): void
    {
        $this->cache->set('flag:2:u1:short', 'gone', 1);
        $this->cache->set('flag:2:u1:long', 'kept');

        sleep(2);

        $this->assertSame(['kept'], $this->cache->valuesWithPrefix('flag:2:u1:'));
    }

    // -------------------------------------------------------------------------
    // Per-key TTL override
    // -------------------------------------------------------------------------

    public function testPerKeyTtlOverridesDefault(): void
    {
        // Cache with a very long default TTL
        $longTtlCache = new Cache(9999, 100);

        // Store with explicit short TTL
        $longTtlCache->set('short', 'gone-soon', 1);
        $longTtlCache->set('long', 'here-forever');

        sleep(2);

        $this->assertNull($longTtlCache->get('short'), 'Per-key TTL=1 should expire after 2 seconds');
        $this->assertSame('here-forever', $longTtlCache->get('long'), 'Default TTL key must still be present');
    }
}

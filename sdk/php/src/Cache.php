<?php

declare(strict_types=1);

namespace Controlvar;

/**
 * In-memory TTL cache for the Controlvar PHP SDK.
 *
 * PHP is single-threaded within a request, so no mutex/locking is needed.
 * When the cache reaches maxCacheSize the oldest entry (by insertion order) is evicted.
 *
 * An entry may also remember a context: the client stores a flag evaluation with
 * the exact context string it sent ('' when it sent none), and reads it back only
 * for an equal string. One key still holds one entry, so a call with other
 * attributes replaces it rather than adding a second.
 */
class Cache
{
    /**
     * Cache store: key => ['value' => mixed, 'expires_at' => float|null, 'context' => string|null]
     * Uses SplDoublyLinkedList semantics via ordered array + index map for O(1) eviction.
     *
     * @var array<string, array{value: mixed, expires_at: float|null, context: string|null}>
     */
    private array $store = [];

    /**
     * Insertion-order tracking for LRU-style eviction (oldest first).
     *
     * @var list<string>
     */
    private array $insertionOrder = [];

    /**
     * @param int $defaultTtl   Default TTL in seconds (0 = never expires)
     * @param int $maxCacheSize Maximum number of entries before eviction
     */
    public function __construct(
        private readonly int $defaultTtl = 300,
        private readonly int $maxCacheSize = 1000,
    ) {
    }

    /**
     * Retrieve a cached value.
     *
     * @param string      $key     Cache key
     * @param string|null $context When given ('' included), the entry is returned only if it
     *                             was stored with this same context; null (the default) does
     *                             not compare contexts, which is what assignments use
     * @return mixed The cached value, or null if missing, expired or stored for another context
     */
    public function get(string $key, ?string $context = null): mixed
    {
        if (!isset($this->store[$key])) {
            return null;
        }

        $entry = $this->store[$key];

        // Check expiry
        if ($entry['expires_at'] !== null && microtime(true) > $entry['expires_at']) {
            $this->delete($key);
            return null;
        }

        if ($context !== null && $entry['context'] !== $context) {
            return null;
        }

        return $entry['value'];
    }

    /**
     * Store a value in the cache, replacing any entry under the same key.
     *
     * @param string      $key     Cache key
     * @param mixed       $value   Value to store (may be null)
     * @param int|null    $ttl     TTL in seconds; null uses the default TTL; 0 = never expires
     * @param string|null $context What a get() with a context must match
     */
    public function set(string $key, mixed $value, ?int $ttl = null, ?string $context = null): void
    {
        // If key already exists, remove from insertion order list before re-adding
        if (isset($this->store[$key])) {
            $this->removeFromInsertionOrder($key);
        } else {
            // Evict oldest entry if we are at capacity
            if (count($this->store) >= $this->maxCacheSize) {
                $this->evictOldest();
            }
        }

        $effectiveTtl = $ttl ?? $this->defaultTtl;
        $expiresAt = ($effectiveTtl > 0) ? microtime(true) + $effectiveTtl : null;

        $this->store[$key] = [
            'value'      => $value,
            'expires_at' => $expiresAt,
            'context'    => $context,
        ];
        $this->insertionOrder[] = $key;
    }

    /**
     * Values of all non-expired entries whose key starts with $prefix, in insertion order.
     * Expired entries are pruned on the way.
     *
     * Used by the client to list everything cached for one user (the track fan-out).
     *
     * @return list<mixed>
     */
    public function valuesWithPrefix(string $prefix): array
    {
        $this->pruneExpired();

        $values = [];
        foreach ($this->insertionOrder as $key) {
            if (str_starts_with($key, $prefix) && isset($this->store[$key])) {
                $values[] = $this->store[$key]['value'];
            }
        }

        return $values;
    }

    /**
     * Remove a specific key from the cache.
     *
     * @param string $key Cache key
     */
    public function delete(string $key): void
    {
        if (!isset($this->store[$key])) {
            return;
        }

        unset($this->store[$key]);
        $this->removeFromInsertionOrder($key);
    }

    /**
     * Remove all entries from the cache.
     */
    public function clear(): void
    {
        $this->store = [];
        $this->insertionOrder = [];
    }

    /**
     * Return the number of non-expired entries currently in the cache.
     */
    public function count(): int
    {
        // Prune expired entries first for an accurate count
        $this->pruneExpired();
        return count($this->store);
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    /**
     * Evict the oldest inserted entry.
     */
    private function evictOldest(): void
    {
        while (!empty($this->insertionOrder)) {
            $oldest = array_shift($this->insertionOrder);
            if (isset($this->store[$oldest])) {
                unset($this->store[$oldest]);
                return;
            }
        }
    }

    /**
     * Remove a key from the insertion-order list.
     */
    private function removeFromInsertionOrder(string $key): void
    {
        $index = array_search($key, $this->insertionOrder, true);
        if ($index !== false) {
            array_splice($this->insertionOrder, $index, 1);
        }
    }

    /**
     * Remove all entries that have passed their TTL.
     */
    private function pruneExpired(): void
    {
        $now = microtime(true);
        foreach (array_keys($this->store) as $key) {
            $entry = $this->store[$key];
            if ($entry['expires_at'] !== null && $now > $entry['expires_at']) {
                unset($this->store[$key]);
                $this->removeFromInsertionOrder($key);
            }
        }
    }
}

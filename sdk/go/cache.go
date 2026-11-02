package experimentation

import (
	"container/list"
	"sync"
	"time"
)

// cacheEntry holds a single cached value with its expiry time and LRU list element.
type cacheEntry struct {
	key       string
	value     interface{}
	expiresAt time.Time
	element   *list.Element
}

// Cache is a thread-safe LRU cache with per-entry TTL expiry.
//
// When the cache is full, the least recently used entry is evicted to make room
// for the new entry. Expired entries are lazily removed on access.
type Cache struct {
	mu      sync.Mutex
	maxSize int
	ttl     time.Duration
	items   map[string]*cacheEntry
	lruList *list.List
}

// NewCache creates a new Cache with the given capacity and TTL.
// If maxSize <= 0, it defaults to 1. If ttl <= 0, entries never expire.
func NewCache(maxSize int, ttl time.Duration) *Cache {
	if maxSize <= 0 {
		maxSize = 1
	}
	return &Cache{
		maxSize: maxSize,
		ttl:     ttl,
		items:   make(map[string]*cacheEntry, maxSize),
		lruList: list.New(),
	}
}

// Get returns the cached value for key and true if found and not expired.
// If the entry has expired it is removed and (nil, false) is returned.
func (c *Cache) Get(key string) (interface{}, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	entry, ok := c.items[key]
	if !ok {
		return nil, false
	}

	// Check TTL expiry (ttl == 0 means no expiry).
	if c.ttl > 0 && time.Now().After(entry.expiresAt) {
		c.removeEntry(entry)
		return nil, false
	}

	// Move to front (most recently used).
	c.lruList.MoveToFront(entry.element)
	return entry.value, true
}

// Set stores a value in the cache under key.
// If the cache is full, the LRU entry is evicted first.
// If key already exists, its value and TTL are updated and it becomes MRU.
func (c *Cache) Set(key string, value interface{}) {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.set(key, value)
}

// fill stores value under key as Set does, and reports whether key held no
// unexpired entry until then: true when this call filled an empty or expired
// slot. The check and the store are one step under the lock, so of several
// goroutines storing the same key at once, exactly one finds the slot empty.
func (c *Cache) fill(key string, value interface{}) bool {
	c.mu.Lock()
	defer c.mu.Unlock()

	entry, ok := c.items[key]
	held := ok && !(c.ttl > 0 && time.Now().After(entry.expiresAt))
	c.set(key, value)
	return !held
}

// set is Set's body. Must be called with c.mu held.
func (c *Cache) set(key string, value interface{}) {
	// Update existing entry.
	if entry, ok := c.items[key]; ok {
		entry.value = value
		if c.ttl > 0 {
			entry.expiresAt = time.Now().Add(c.ttl)
		}
		c.lruList.MoveToFront(entry.element)
		return
	}

	// Evict LRU if at capacity.
	if len(c.items) >= c.maxSize {
		c.evict()
	}

	// Insert new entry at front.
	var expiresAt time.Time
	if c.ttl > 0 {
		expiresAt = time.Now().Add(c.ttl)
	}
	entry := &cacheEntry{
		key:       key,
		value:     value,
		expiresAt: expiresAt,
	}
	entry.element = c.lruList.PushFront(entry)
	c.items[key] = entry
}

// Delete removes the entry for key from the cache, if it exists.
func (c *Cache) Delete(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if entry, ok := c.items[key]; ok {
		c.removeEntry(entry)
	}
}

// Clear removes all entries from the cache.
func (c *Cache) Clear() {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.items = make(map[string]*cacheEntry, c.maxSize)
	c.lruList.Init()
}

// Len returns the current number of entries in the cache (including expired ones
// that have not been lazily cleaned up yet).
func (c *Cache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()

	return len(c.items)
}

// Range calls fn for every live (non-expired) entry, most recently used first.
// Expired entries encountered during the scan are removed. The snapshot is
// taken under the lock and fn is invoked after it is released, so fn may call
// back into the cache. Range does not update recency.
func (c *Cache) Range(fn func(key string, value interface{})) {
	type kv struct {
		key   string
		value interface{}
	}

	c.mu.Lock()
	now := time.Now()
	live := make([]kv, 0, len(c.items))
	for el := c.lruList.Front(); el != nil; {
		next := el.Next()
		entry := el.Value.(*cacheEntry)
		if c.ttl > 0 && now.After(entry.expiresAt) {
			c.removeEntry(entry)
		} else {
			live = append(live, kv{entry.key, entry.value})
		}
		el = next
	}
	c.mu.Unlock()

	for _, item := range live {
		fn(item.key, item.value)
	}
}

// evict removes the least recently used item from the cache.
// Must be called with c.mu held.
func (c *Cache) evict() {
	back := c.lruList.Back()
	if back == nil {
		return
	}
	entry := back.Value.(*cacheEntry)
	c.removeEntry(entry)
}

// removeEntry deletes an entry from both the map and the LRU list.
// Must be called with c.mu held.
func (c *Cache) removeEntry(entry *cacheEntry) {
	c.lruList.Remove(entry.element)
	delete(c.items, entry.key)
}

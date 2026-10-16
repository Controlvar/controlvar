module Controlvar
  # Thread-safe TTL cache with LRU eviction.
  #
  # Entries are stored as { value:, expires_at: } pairs.
  # The insertion order of @store (Hash in Ruby >= 1.9) is used for LRU eviction:
  # the first entry is the oldest.
  #
  # Keys may be any hashable object; the client uses Array keys such as
  # [:flag, user_id, flag_key] so entries can be listed per user.
  class Cache
    # @param ttl          [Integer] default time-to-live in seconds
    # @param max_size     [Integer] maximum number of entries; oldest evicted when exceeded
    def initialize(ttl: 300, max_size: 1000)
      @default_ttl = ttl.to_i
      @max_size    = max_size.to_i
      @store       = {}          # key => { value:, expires_at: }
      @mutex       = Mutex.new
    end

    # Retrieve a cached value.
    #
    # @param key [Object]
    # @return [Object, nil] the cached value, or nil if missing or expired
    def get(key)
      @mutex.synchronize do
        entry = @store[key]
        return nil if entry.nil?

        if Time.now >= entry[:expires_at]
          @store.delete(key)
          return nil
        end

        # Refresh LRU position
        @store.delete(key)
        @store[key] = entry

        entry[:value]
      end
    end

    # Store a value in the cache.
    #
    # @param key   [Object]
    # @param value [Object]
    # @param ttl   [Integer, nil] seconds; uses default_ttl when nil
    def set(key, value, ttl: nil)
      effective_ttl = (ttl || @default_ttl).to_i
      expires_at    = Time.now + effective_ttl

      @mutex.synchronize do
        # Remove existing entry to refresh LRU position
        @store.delete(key)

        # Evict oldest entry if at capacity
        if @store.size >= @max_size
          oldest_key = @store.keys.first
          @store.delete(oldest_key)
        end

        @store[key] = { value: value, expires_at: expires_at }
      end

      value
    end

    # Values of all live (non-expired) entries whose key satisfies the block,
    # in insertion order. Expired entries encountered on the way are pruned.
    # The block receives the key only and must not call back into the cache.
    #
    # @yieldparam key [Object]
    # @return [Array<Object>]
    def live_values
      now = Time.now
      @mutex.synchronize do
        expired = []
        values  = []
        @store.each do |key, entry|
          next unless yield(key)

          if now >= entry[:expires_at]
            expired << key
          else
            values << entry[:value]
          end
        end
        expired.each { |key| @store.delete(key) }
        values
      end
    end

    # Remove a specific entry.
    #
    # @param key [Object]
    def delete(key)
      @mutex.synchronize { @store.delete(key) }
    end

    # Remove all entries.
    def clear
      @mutex.synchronize { @store.clear }
    end

    # Return the number of entries currently stored (including expired ones not yet evicted).
    #
    # @return [Integer]
    def size
      @mutex.synchronize { @store.size }
    end
  end
end

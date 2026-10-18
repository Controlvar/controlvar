require 'spec_helper'

RSpec.describe Controlvar::Cache do
  subject(:cache) { described_class.new(ttl: 60, max_size: 5) }

  # -------------------------------------------------------------------------
  # Basic get/set
  # -------------------------------------------------------------------------
  describe '#get' do
    it 'returns nil for a missing key' do
      expect(cache.get('nonexistent')).to be_nil
    end

    it 'returns the stored value after set' do
      cache.set('key1', 'value1')
      expect(cache.get('key1')).to eq('value1')
    end

    it 'returns complex objects (Hash, Array)' do
      cache.set('hash-key', { a: 1, b: 2 })
      expect(cache.get('hash-key')).to eq({ a: 1, b: 2 })
    end
  end

  # -------------------------------------------------------------------------
  # TTL expiration
  # -------------------------------------------------------------------------
  describe 'TTL expiration' do
    it 'returns nil for an expired entry' do
      cache.set('expiring', 'gone-soon', ttl: 1)
      # Travel past TTL using time stubbing
      future = Time.now + 2
      allow(Time).to receive(:now).and_return(future)
      expect(cache.get('expiring')).to be_nil
    end

    it 'returns the value before TTL expires' do
      cache.set('fresh', 'still-here', ttl: 60)
      expect(cache.get('fresh')).to eq('still-here')
    end

    it 'supports per-entry TTL overriding the default' do
      short_cache = described_class.new(ttl: 300, max_size: 10)
      short_cache.set('override', 'value', ttl: 1)

      future = Time.now + 2
      allow(Time).to receive(:now).and_return(future)
      expect(short_cache.get('override')).to be_nil
    end
  end

  # -------------------------------------------------------------------------
  # live_values (per-user listing used by the track fan-out)
  # -------------------------------------------------------------------------
  describe '#live_values' do
    it 'returns the values of live entries whose key satisfies the block, in insertion order' do
      cache.set([:flag, 'u1', 'a'], 'A')
      cache.set([:flag, 'u2', 'x'], 'X')
      cache.set([:flag, 'u1', 'b'], 'B')

      result = cache.live_values { |key| key[0] == :flag && key[1] == 'u1' }
      expect(result).to eq(%w[A B])
    end

    it 'returns an empty Array when nothing matches' do
      cache.set('plain', 1)
      expect(cache.live_values { |key| key.is_a?(Array) }).to eq([])
    end

    it 'skips and prunes expired entries' do
      cache.set([:flag, 'u1', 'old'], 'OLD', ttl: 1)
      cache.set([:flag, 'u1', 'new'], 'NEW', ttl: 60)

      allow(Time).to receive(:now).and_return(Time.now + 2)

      expect(cache.live_values { |key| key[1] == 'u1' }).to eq(['NEW'])
      expect(cache.size).to eq(1)
    end
  end

  # -------------------------------------------------------------------------
  # delete
  # -------------------------------------------------------------------------
  describe '#delete' do
    it 'removes a specific key' do
      cache.set('del-key', 'del-value')
      cache.delete('del-key')
      expect(cache.get('del-key')).to be_nil
    end

    it 'does not raise when deleting a nonexistent key' do
      expect { cache.delete('no-such-key') }.not_to raise_error
    end
  end

  # -------------------------------------------------------------------------
  # clear
  # -------------------------------------------------------------------------
  describe '#clear' do
    it 'removes all entries' do
      cache.set('a', 1)
      cache.set('b', 2)
      cache.clear
      expect(cache.get('a')).to be_nil
      expect(cache.get('b')).to be_nil
    end

    it 'resets size to zero' do
      cache.set('x', 99)
      cache.clear
      expect(cache.size).to eq(0)
    end
  end

  # -------------------------------------------------------------------------
  # size
  # -------------------------------------------------------------------------
  describe '#size' do
    it 'returns 0 for an empty cache' do
      expect(cache.size).to eq(0)
    end

    it 'returns the correct count after insertions' do
      cache.set('k1', 1)
      cache.set('k2', 2)
      expect(cache.size).to eq(2)
    end

    it 'does not double-count overwritten keys' do
      cache.set('dup', 'first')
      cache.set('dup', 'second')
      expect(cache.size).to eq(1)
    end
  end

  # -------------------------------------------------------------------------
  # LRU eviction at max_size
  # -------------------------------------------------------------------------
  describe 'LRU eviction' do
    it 'evicts the oldest entry when max_size is exceeded' do
      # Cache has max_size: 5
      5.times { |i| cache.set("key-#{i}", i) }
      # key-0 is oldest; inserting key-5 should evict it
      cache.set('key-5', 5)

      expect(cache.get('key-0')).to be_nil
      expect(cache.get('key-5')).to eq(5)
    end

    it 'keeps recently accessed entries when evicting' do
      5.times { |i| cache.set("key-#{i}", i) }
      # Access key-0 to refresh its LRU position
      cache.get('key-0')
      # key-1 is now oldest; inserting key-5 should evict key-1
      cache.set('key-5', 5)

      expect(cache.get('key-0')).not_to be_nil
      expect(cache.get('key-1')).to be_nil
    end

    it 'does not exceed max_size after many insertions' do
      20.times { |i| cache.set("flood-#{i}", i) }
      expect(cache.size).to be <= 5
    end
  end

  # -------------------------------------------------------------------------
  # Thread safety
  # -------------------------------------------------------------------------
  describe 'thread safety' do
    it 'handles 50 concurrent writer threads without deadlock or data corruption' do
      thread_cache = described_class.new(ttl: 60, max_size: 1000)
      threads = 50.times.map do |i|
        Thread.new do
          thread_cache.set("key-#{i}", i)
          thread_cache.get("key-#{i}")
        end
      end
      expect { threads.each(&:join) }.not_to raise_error
    end

    it 'handles concurrent reads and writes without errors' do
      thread_cache = described_class.new(ttl: 60, max_size: 500)
      25.times { |i| thread_cache.set("pre-#{i}", i) }

      writers = 25.times.map { |i| Thread.new { thread_cache.set("new-#{i}", i * 2) } }
      readers = 25.times.map { |i| Thread.new { thread_cache.get("pre-#{i}") } }

      expect { (writers + readers).each(&:join) }.not_to raise_error
    end

    it 'handles concurrent deletes without errors' do
      thread_cache = described_class.new(ttl: 60, max_size: 200)
      50.times { |i| thread_cache.set("del-#{i}", i) }

      threads = 50.times.map { |i| Thread.new { thread_cache.delete("del-#{i}") } }
      expect { threads.each(&:join) }.not_to raise_error
      expect(thread_cache.size).to eq(0)
    end
  end
end

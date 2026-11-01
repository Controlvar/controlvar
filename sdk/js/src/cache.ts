interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

/**
 * Per-user, per-key TTL cache. Only successful results are ever stored. An entry is live for
 * `ttlMs` after it is set and expired from then on (`>=`), so a TTL of 0 caches nothing, even
 * within the same millisecond.
 */
export class UserKeyCache<T> {
  private readonly users = new Map<string, Map<string, CacheEntry<T>>>();

  constructor(private readonly ttlMs: number) {}

  get(userId: string, key: string): T | null {
    const entry = this.users.get(userId)?.get(key);
    if (!entry) return null;
    if (Date.now() >= entry.expiresAt) {
      this.users.get(userId)?.delete(key);
      return null;
    }
    return entry.value;
  }

  set(userId: string, key: string, value: T): void {
    let byKey = this.users.get(userId);
    if (!byKey) {
      byKey = new Map();
      this.users.set(userId, byKey);
    }
    byKey.set(key, { value, expiresAt: Date.now() + this.ttlMs });
  }

  /** All live (non-expired) entries for a user, in insertion order. */
  entries(userId: string): Array<[string, T]> {
    const byKey = this.users.get(userId);
    if (!byKey) return [];
    const now = Date.now();
    const live: Array<[string, T]> = [];
    for (const [key, entry] of byKey) {
      if (now >= entry.expiresAt) byKey.delete(key);
      else live.push([key, entry.value]);
    }
    return live;
  }

  clear(): void {
    this.users.clear();
  }
}
